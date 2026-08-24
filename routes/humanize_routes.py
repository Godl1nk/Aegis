"""Sample-grounded text rewriting.

This route intentionally keeps rewriting model-driven. It does not score text
as "AI" or mutate prose with detector-oriented typo, punctuation, or synonym
heuristics.
"""

import asyncio
import logging
import math
import os
import re
import time
import uuid
from collections import Counter
from difflib import SequenceMatcher

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.auth_helpers import require_user
from src.endpoint_resolver import resolve_endpoint_by_id
from src.llm_core import llm_call_async

logger = logging.getLogger(__name__)

_MAX_CHARS = 20_000
_MIN_LENGTH_RATIO = 0.40
_MAX_LENGTH_RATIO = 1.60
_PARAGRAPH_SEPARATOR_RE = re.compile(r"(\r?\n(?:[ \t]*\r?\n)+)")
_SENTENCE_RE = re.compile(r"(?<=[.!?])(?:[\"')\]]*)\s+")
_JOB_TTL_SECONDS = 3600
_HUMANIZE_JOBS: dict[str, dict] = {}
_HUMANIZE_TASKS: set[asyncio.Task] = set()

# Rewriting runs as a background job the client polls, so nothing is holding an
# HTTP request open and the budget doesn't need to fit inside one. It was a flat
# 28s, which a reasoning model on a multi-paragraph document cannot meet — the
# first attempt blew the deadline, the retry then got the 1s floor and died
# instantly, and the whole job failed with "All rewrite attempts timed out".
# Scale with input size and let deployments override it.
_TIMEOUT_BASE_S = float(os.getenv("HUMANIZE_TIMEOUT_S", "120"))
_TIMEOUT_PER_1K_CHARS_S = float(os.getenv("HUMANIZE_TIMEOUT_PER_1K_S", "30"))
_TIMEOUT_MAX_S = float(os.getenv("HUMANIZE_TIMEOUT_MAX_S", "480"))
# Below this much remaining budget a fresh model call isn't worth starting.
_MIN_ATTEMPT_S = 20.0
# Share of the budget the whole-document attempt may use. The remainder backs
# the retry (when the first pass returns something weak) or the batched
# fallback (when it returns nothing at all).
_FIRST_ATTEMPT_SHARE = 0.55


def _budget_seconds(text: str) -> float:
    """Total wall-clock budget for every attempt on this document."""
    scaled = _TIMEOUT_BASE_S + _TIMEOUT_PER_1K_CHARS_S * (len(text) / 1000.0)
    return max(_MIN_ATTEMPT_S, min(_TIMEOUT_MAX_S, scaled))

# Heuristic: sentences that open with these patterns likely start a new
# logical paragraph even when the input has no blank-line separators.
_PARAGRAPH_OPENER_RE = re.compile(
    r"^(?:"
    r"(?:However|Moreover|Furthermore|Additionally|In addition|Meanwhile|"
    r"Nevertheless|Nonetheless|Consequently|Therefore|Thus|Hence|As a result|"
    r"On the other hand|In contrast|Conversely|Similarly|Likewise|Indeed|"
    r"In fact|For example|For instance|Specifically|In particular|Overall|"
    r"In summary|To summarize|In conclusion|Finally|Lastly|First(?:ly)?|Second(?:ly)?|Third(?:ly)?|"
    r"Current(?:ly)?|Today|Nowadays|Recently|At present|In recent years|"
    r"The development|The (?:use|rise|growth|impact|role|future|history)|"
    r"This (?:has|is|was|means|shows|suggests|demonstrates|indicates|leads)|"
    r"These (?:advances|changes|developments|improvements|technologies|tools|systems))"
    r"(?:,|\s))",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """You are an experienced editor. Rewrite the text so it reads like careful professional prose — the register of a good trade journal or an internal report, not a blog post and not a lecture.

### SENTENCE LENGTH — build a varied distribution, not a target average
- Most sentences land between 10 and 25 words.
- Roughly one in five is short: under 9 words. Use them to land a point.
- Keep some long ones too: a 30-40 word sentence is fine when the idea genuinely needs subordination. Do not chop every complex thought into fragments — that reads mechanical in its own way.
- What matters is the SEQUENCE, not the average. Never let three consecutive sentences sit within a few words of each other in length. Follow a long sentence with a short one.
- Vary the shape of openings as well as their words: some sentences open on the subject, others on a subordinate clause, a prepositional phrase, or a transition.

### REGISTER — professional, not chatty
- No filler openers ("It is important to note", "It is worth mentioning", "In today's world").
- No hype ("revolutionary", "game-changing", "cutting-edge") unless the source says it.
- No rhetorical questions, no addressing the reader, no exclamations added.
- Contractions are fine where the source tone allows, but do not add slang or asides.
- Prefer concrete verbs over nominalizations: "we measured" over "measurement was performed".

### FIDELITY
1. Preserve every fact and meaning exactly. Do not add or remove anything.
2. Keep every number, unit, date, proper noun, acronym and quoted phrase exactly as written in the source.
3. Keep technical terms accurate. Do not swap a term of art for a loose synonym.
4. Maintain paragraph count and order of ideas.

### REBUILD
5. Rebuild every sentence from scratch with different structure and wording. Reordering clauses is not enough.
6. Change sentence openings completely — no copied starts.
7. Do not reuse the source's distinctive phrasing where a natural alternative exists.
8. Perfect grammar and natural flow.

Output format:
Wrap each rewritten paragraph in <paragraph id="N">...</paragraph> tags that
match the source paragraph IDs. Return only the tagged rewritten paragraphs.
Do not include commentary, explanation, or meta-text outside the tags."""

class HumanizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=_MAX_CHARS)
    endpoint_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=512)


def _clean_model_output(value: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", value or "", flags=re.DOTALL | re.IGNORECASE).strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```[^\n]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    text = re.sub(
        r"^\s*(?:rewritten\s+text|rewrite|output)\s*:\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    # Strip chain-of-thought preamble. Match paragraph tags with or without quotes
    # around numeric IDs so echoes like <paragraph id="N"> are ignored.
    first_tag = re.search(r'<paragraph\s+id=["\']?\d+["\']?\s*>', text)
    if first_tag and first_tag.start() > 0:
        text = text[first_tag.start():]
    # Strip everything after the last closing </paragraph> tag.
    last_close = None
    for m in re.finditer(r'</paragraph>', text):
        last_close = m
    if first_tag and last_close:
        text = text[:last_close.end()]
    # If no real tags found, strip common reasoning headers/content.
    if not first_tag:
        text = _strip_reasoning_content(text)
    return text.strip()


def _strip_reasoning_content(text: str) -> str:
    """Remove chain-of-thought reasoning patterns from model output."""
    # Strip reasoning headers.
    text = re.sub(
        r"^\s*(?:Here'?s?\s+(?:a|my)\s+thinking\s+process"
        r"|## ?Thinking|## ?Reasoning"
        r"|\*\*(?:Thinking|Analysis|Reasoning)\*\*)"
        r"\s*:?\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    lines = text.split("\n")
    kept_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept_lines.append("")
            continue
            
        # Strip lines that consist entirely of brackets or isolated punctuation
        if re.match(r"^[)\]}\s]+$", stripped):
            continue
        if re.match(r"^(?:\d+\.|\*|-)\s+\*\*", stripped):
            continue
            
        # Strip lines containing typical reasoning word/sentence metrics
        if re.search(r"\b(?:lengths|avg|sentences|word count|total words|constraints|adjacent pair)\b", stripped, re.IGNORECASE):
            continue
            
        # Strip lines with conversational meta-commentary
        if re.match(r"^(?:wait|let'?s|i\s+(?:need|will|should|must|have|am|aim|plan|draft|tweak|combine|accept|count)|actually|here'?s)\b", stripped, re.IGNORECASE):
            continue
        if re.match(r"^(?:this|that|these|those)\s+(?:looks?|seems?|is\s+solid|matches?|fits?|works?|changes?)\b", stripped, re.IGNORECASE):
            continue

        # Strip lines that look like draft/revision headers
        if re.match(r"^\s*(?:\*|-)?\s*(?:original|draft|revised|final|check|goal|key facts|deconstruct|analysis|source|paragraph)\b", stripped, re.IGNORECASE):
            continue
            
        kept_lines.append(line)
        
    cleaned_text = "\n".join(kept_lines)
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text)
    return cleaned_text.strip()


def _clean_paragraph_output(value: str) -> str:
    text = _clean_model_output(value)
    return _PARAGRAPH_SEPARATOR_RE.sub(" ", text).strip()


def _length_ratio(source: str, candidate: str) -> float:
    source_words = re.findall(r"\b[\w'-]+\b", source, flags=re.UNICODE)
    candidate_words = re.findall(r"\b[\w'-]+\b", candidate, flags=re.UNICODE)
    return len(candidate_words) / max(1, len(source_words))


def _rewrite_similarity(source: str, candidate: str) -> float:
    def words(value: str) -> list[str]:
        return re.findall(r"\b[\w'-]+\b", value.casefold(), flags=re.UNICODE)

    return SequenceMatcher(None, words(source), words(candidate), autojunk=False).ratio()


def _sentence_count(paragraph: str) -> int:
    return len([part for part in _SENTENCE_RE.split(paragraph.strip()) if part.strip()])


def _sentence_length_stats(text: str) -> dict:
    sentences = _split_sentences(text)
    if not sentences:
        return {"mean": 0.0, "std": 0.0, "cv": 0.0, "min": 0, "max": 0}
    lengths = [len(s.split()) for s in sentences]
    n = len(lengths)
    mean = sum(lengths) / n
    variance = sum((l - mean) ** 2 for l in lengths) / max(1, n - 1)
    std = math.sqrt(variance)
    cv = std / max(0.01, mean)
    return {"mean": mean, "std": std, "cv": cv, "min": min(lengths), "max": max(lengths)}


def _ngram_diversity(text: str, n: int = 2) -> float:
    words = re.findall(r"\b\w+\b", text.casefold())
    if len(words) < n + 1:
        return 1.0
    ngrams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    return len(set(ngrams)) / max(1, len(ngrams))


def _sentence_starter_diversity(text: str) -> float:
    sentences = _split_sentences(text)
    if not sentences:
        return 1.0
    starters = []
    for s in sentences:
        tokens = s.split()
        if tokens:
            starters.append(tokens[0].casefold())
    if not starters:
        return 1.0
    return len(set(starters)) / max(1, len(starters))


# Closed-class word lists, enough to bucket a sentence's opening by grammatical
# shape without a POS tagger.
_OPENER_DETERMINER = frozenset(
    "the a an this that these those it its they their we our he she his her you your i my there".split()
)
_OPENER_SUBORDINATOR = frozenset(
    "although though because since while when whenever whereas if unless once after before as until".split()
)
_OPENER_PREPOSITION = frozenset(
    "in on at for with by from over under across through between during within about against beyond "
    "toward towards among behind beside despite via per".split()
)
_OPENER_CONJUNCTION = frozenset("and but or yet so nor".split())


def _opening_shape(sentence: str) -> str:
    """Bucket a sentence by what grammatical element it opens on."""
    tokens = re.findall(r"[\w']+", sentence.casefold())
    if not tokens:
        return "other"
    head = tokens[0]
    if head in _OPENER_CONJUNCTION:
        return "conjunction"
    if head in _OPENER_SUBORDINATOR:
        return "subordinate"
    if head in _OPENER_PREPOSITION:
        return "prepositional"
    if head in _OPENER_DETERMINER:
        return "subject"
    if head.endswith("ly"):
        return "adverbial"
    if head.endswith("ing"):
        return "participial"
    return "other"


def _opening_shape_diversity(text: str) -> float:
    """How many DIFFERENT grammatical shapes the sentence openings use (0..1).

    Distinct opening *words* are not the same thing as varied openings: a
    paragraph of "In 2021, ... In 2022, ... In 2023, ..." has perfectly
    distinct two-word openings and one single monotonous shape. Bucketing by
    grammatical element is what the prompt actually asks the model to vary.
    """
    sentences = _split_sentences(text)
    if not sentences:
        return 1.0
    shapes = [_opening_shape(s) for s in sentences]
    # Normalise against how many shapes this many sentences could show, so a
    # two-sentence paragraph isn't marked down for using only two.
    reachable = min(len(shapes), 5)
    return min(1.0, len(set(shapes)) / max(1, reachable))


def _burstiness(text: str) -> float:
    """How much sentence length moves from one sentence to the NEXT (0..1).

    The coefficient of variation only measures spread, so a text that runs
    5,5,5,30,30,30 scores as well as one that genuinely alternates. Human prose
    alternates; this measures the mean step between neighbours, normalised
    against the text's own mean length so it doesn't just reward long writing.
    """
    lengths = [len(s.split()) for s in _split_sentences(text)]
    if len(lengths) < 2:
        return 0.0
    steps = [abs(b - a) for a, b in zip(lengths, lengths[1:])]
    mean_len = sum(lengths) / len(lengths)
    mean_step = sum(steps) / len(steps)
    return min(1.0, mean_step / max(1.0, mean_len * 0.6))


# Phrases that mark generated prose more reliably than any single word: filler
# openers, hedging throat-clearing, and hype. Penalised rather than banned —
# the source may legitimately contain them, and the rewrite keeps meaning.
_FILLER_RE = re.compile(
    r"\b(?:"
    r"it(?:'s| is) (?:important|worth|crucial|essential) to (?:note|mention|remember|understand|consider)|"
    r"it should be noted|needless to say|as we all know|"
    r"in (?:today's|the modern) (?:world|era|landscape|society)|"
    r"in the (?:fast[- ]paced|ever[- ]changing|rapidly evolving) world|"
    r"plays? a (?:crucial|key|vital|significant|pivotal) role|"
    r"delv(?:e|ing) into|"
    r"when it comes to|"
    r"a testament to|"
    r"navigat(?:e|ing) the (?:complexities|landscape|challenges)|"
    r"unlock(?:ing)? the (?:potential|power)|"
    r"in the realm of|"
    r"game[- ]chang(?:er|ing)|cutting[- ]edge|state[- ]of[- ]the[- ]art|revolutioniz(?:e|ing|ed)|"
    r"seamless(?:ly)?|robust solution|"
    r"furthermore|moreover"
    r")\b",
    re.IGNORECASE,
)


def _filler_density(text: str) -> float:
    """Filler hits per 100 words."""
    words = re.findall(r"\b\w+\b", text)
    if not words:
        return 0.0
    return 100.0 * len(_FILLER_RE.findall(text)) / len(words)


def _compute_human_score(text: str) -> float:
    if not text.strip():
        return 0.0
    stats = _sentence_length_stats(text)
    cv_score = min(stats["cv"] / 0.7, 1.0)
    bigram_div = _ngram_diversity(text, 2)
    trigram_div = _ngram_diversity(text, 3)
    starter_div = _sentence_starter_diversity(text)
    opening_div = _opening_shape_diversity(text)
    burst = _burstiness(text)
    score = (
        0.24 * cv_score
        + 0.20 * burst
        + 0.20 * trigram_div
        + 0.14 * starter_div
        + 0.12 * opening_div
        + 0.10 * bigram_div
    )
    # Filler is the register problem, not a rhythm problem — subtract it rather
    # than let a well-varied but clichéd rewrite pass. ~1 hit per 100 words
    # costs 0.06; capped so a filler-heavy source can't zero out a good rewrite.
    return max(0.0, score - min(0.18, 0.06 * _filler_density(text)))


# High-precision fidelity anchors. Numbers and acronyms are what a rewrite
# actually loses (a model drops "17%" or turns "TLS 1.3" into "the protocol"),
# and losing one is a correctness bug, not a style preference. Deliberately not
# checking general capitalised words — sentence-initial casing makes that noisy.
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*\s*%?")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}(?:[-–][A-Z0-9]+)?\b")


def _fidelity_report(source: str, candidate: str) -> dict:
    """Anchors present in the source but missing from the rewrite."""

    def _numbers(value: str) -> list[str]:
        return [m.group(0).replace(" ", "").rstrip(".,") for m in _NUMBER_RE.finditer(value)]

    src_numbers = Counter(_numbers(source))
    out_numbers = Counter(_numbers(candidate))
    missing_numbers = sorted((src_numbers - out_numbers).elements())

    src_acronyms = Counter(_ACRONYM_RE.findall(source))
    out_acronyms = Counter(_ACRONYM_RE.findall(candidate))
    missing_acronyms = sorted((src_acronyms - out_acronyms).elements())

    total = sum(src_numbers.values()) + sum(src_acronyms.values())
    missing = len(missing_numbers) + len(missing_acronyms)
    return {
        "missing_numbers": missing_numbers,
        "missing_acronyms": missing_acronyms,
        # 1.0 when every anchor survived; 1.0 when there were none to lose.
        "score": 1.0 if not total else max(0.0, 1.0 - missing / total),
    }


def _infer_paragraph_breaks(text: str) -> str:
    """Insert explicit paragraph separators when sentences begin with typical
    paragraph-opening patterns but the source has no blank-line breaks."""
    # Only run when the entire text is a single block (no blank lines).
    if _PARAGRAPH_SEPARATOR_RE.search(text):
        return text
    sentences = _SENTENCE_RE.split(text)
    if len(sentences) <= 2:
        return text
    pieces: list[str] = [sentences[0]]
    for sentence in sentences[1:]:
        stripped = sentence.lstrip()
        if stripped and _PARAGRAPH_OPENER_RE.match(stripped):
            pieces.append("\n\n")
        else:
            pieces.append(" ")
        pieces.append(sentence)
    return "".join(pieces)


def _split_document(text: str) -> tuple[list[str], list[str]]:
    text = _infer_paragraph_breaks(text)
    parts = _PARAGRAPH_SEPARATOR_RE.split(text)
    return parts[::2], parts[1::2]


def _split_sentences(paragraph: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.split(paragraph.strip()) if part.strip()]


def _document_prompt(paragraphs: list[str]) -> str:
    """Build the user message with numbered paragraphs and sentence counts."""
    lines: list[str] = [
        "/no_think",
        "Rewrite the following document. Each paragraph is tagged with an ID.",
        "Change as much wording as possible — rebuild phrases and clauses, not just swap words.",
        "Keep the exact same meaning, facts, tone, and style.",
        "Output only the tags with the rewritten content, matching each source paragraph ID.",
        "Do NOT output any thinking, reasoning, planning, or analysis — ONLY the paragraph tags.",
        "",
    ]
    for idx, para in enumerate(paragraphs, 1):
        count = _sentence_count(para)
        lines.append(
            f'<paragraph id="{idx}">{para}</paragraph>'
            f"  <!-- {count} source sentence(s) -->"
        )
    return "\n".join(lines)


_PARAGRAPH_TAG_RE = re.compile(
    r'<paragraph\s+id=["\']?(\d+)["\']?\s*>(.*?)</paragraph>', re.DOTALL
)


def _extract_rewritten_paragraphs(raw: str, source: list[str]) -> list[str]:
    """Parse <paragraph id="N"> tags from model output, restoring source order.

    Falls back to repartitioning flat (untagged) text by proportional sentence
    counts when the model omits the tags.  Detects and strips chain-of-thought
    reasoning dumps before repartitioning.
    """
    cleaned = _clean_model_output(raw)
    matches = _PARAGRAPH_TAG_RE.findall(cleaned)
    if matches:
        by_id: dict[int, str] = {}
        for pid, body in matches:
            by_id[int(pid)] = _clean_paragraph_output(body)
        return [by_id.get(i + 1, source[i]) for i in range(len(source))]

    # If the cleaned output is absurdly long compared to the source, it is
    # almost certainly a thinking dump that leaked through.  Run an extra
    # aggressive strip before repartitioning.
    source_text = " ".join(source)
    if _length_ratio(source_text, cleaned) > 3.0:
        cleaned = _strip_reasoning_content(cleaned)

    # Fallback: repartition flat text by source sentence counts.
    sentences = _split_sentences(cleaned)
    source_counts = [_sentence_count(p) for p in source]
    total_source = sum(source_counts)
    total_rewritten = len(sentences)
    result: list[str] = []
    idx = 0
    for i, count in enumerate(source_counts):
        if i == len(source_counts) - 1:
            result.append(" ".join(sentences[idx:]))
        else:
            n = round(count / max(1, total_source) * total_rewritten)
            n = max(1, min(n, total_rewritten - idx - (len(source_counts) - i - 1)))
            result.append(" ".join(sentences[idx : idx + n]))
            idx += n
    return result


def _needs_stronger_rewrite(source: list[str], rewritten: list[str]) -> bool:
    """Return True if any aligned paragraph pair is too similar to the original."""
    for s, r in zip(source, rewritten):
        if _rewrite_similarity(s, r) > 0.75:
            return True
    return False


def _validate_candidate(
    source: str,
    candidate: str,
) -> None:
    if not candidate:
        raise HTTPException(502, "Rewrite model returned no text")
    ratio = _length_ratio(source, candidate)
    if ratio < _MIN_LENGTH_RATIO or ratio > _MAX_LENGTH_RATIO:
        logger.warning("Length ratio %.3f outside soft bounds — output may be too long or short", ratio)
    fidelity = _fidelity_report(source, candidate)
    dropped = fidelity["missing_numbers"] + fidelity["missing_acronyms"]
    if dropped:
        # Surfaced, not fatal: the rewrite may still be the best available, and
        # the caller gets the same list back in `metrics.missing_anchors`.
        logger.warning("Rewrite dropped source anchors: %s", ", ".join(dropped[:10]))


def _join_paragraphs(paragraphs: list[str], separators: list[str]) -> str:
    pieces: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        pieces.append(paragraph)
        if index < len(separators):
            pieces.append(separators[index])
    return "".join(pieces)


def _revision_notes(source: str, candidate: str, paragraphs: list[str], source_paragraphs: list[str]) -> list[str]:
    """Name what was actually wrong with a candidate.

    The retry used to be a generic "make it more different" nudge, which asks
    the model to guess which axis failed. These notes quote the measurement, so
    a rewrite that was varied-but-clichéd gets told about the clichés instead of
    being pushed to churn its sentence lengths again.
    """
    notes: list[str] = []

    stats = _sentence_length_stats(candidate)
    lengths = [len(s.split()) for s in _split_sentences(candidate)]
    if lengths and _burstiness(candidate) < 0.45:
        preview = ", ".join(str(n) for n in lengths[:12])
        notes.append(
            f"Sentence lengths barely moved (in words: {preview}). Adjacent sentences must differ "
            "sharply — follow a long sentence with one under 9 words, and let at least one sentence "
            "run past 30 words where the idea earns it."
        )
    elif stats["max"] and stats["max"] - stats["min"] < 8:
        notes.append(
            f"Every sentence landed between {stats['min']} and {stats['max']} words. Widen that range."
        )

    if _sentence_starter_diversity(candidate) < 0.7 or _opening_shape_diversity(candidate) < 0.7:
        openings = [" ".join(s.split()[:2]) for s in _split_sentences(candidate)]
        repeated = sorted({o for o in openings if openings.count(o) > 1})
        if repeated:
            notes.append(
                "These openings repeat: " + "; ".join(repeated[:5])
                + ". Start those sentences on a different grammatical element."
            )

    fillers = sorted({m.group(0).lower() for m in _FILLER_RE.finditer(candidate)})
    if fillers:
        notes.append(
            "Remove this filler/hype, it did not come from the source: " + ", ".join(fillers[:6]) + "."
        )

    if _ngram_diversity(candidate, 3) < 0.85:
        notes.append("Three-word sequences repeat too often. Vary the phrasing, not just the sentence order.")

    fidelity = _fidelity_report(source, candidate)
    dropped = fidelity["missing_numbers"] + fidelity["missing_acronyms"]
    if dropped:
        notes.append(
            "You dropped or altered these, which must appear verbatim: " + ", ".join(dropped[:8]) + "."
        )

    stale = [
        str(i + 1)
        for i, (s, r) in enumerate(zip(source_paragraphs, paragraphs))
        if _rewrite_similarity(s, r) > 0.75
    ]
    if stale:
        notes.append(
            f"Paragraph(s) {', '.join(stale)} are still close paraphrases of the source. "
            "Rebuild them from the meaning, not from the original wording."
        )
    return notes


async def _rewrite_document(
    text: str,
    *,
    url: str,
    model: str,
    headers: dict,
    on_progress=None,
) -> tuple[str, dict]:
    """Rewrite with sequential attempts under a shared, size-scaled deadline.

    First attempt uses a normal prompt. If the result fails the similarity,
    rhythm or register checks, a second attempt fires with whatever time
    remains — carrying specific notes on what missed. Returns early when a
    candidate passes the gates.

    If the whole-document call times out (slow model, long document), the
    remaining budget goes on rewriting the paragraphs in small batches: each
    call is far smaller, so the work that does finish is kept instead of the
    whole job failing.

    Returns (text, metrics).
    """
    source_paragraphs, separators = _split_document(text)
    budget = _budget_seconds(text)
    deadline = time.monotonic() + budget
    _HUMAN_SCORE_THRESHOLD = 0.35

    def _remaining() -> float:
        return deadline - time.monotonic()

    def _note(step: str, detail: str) -> None:
        if on_progress:
            on_progress(step, detail)

    async def _ask(paragraphs: list[str], user_content: str, *, limit: float | None = None) -> list[str] | None:
        remaining = _remaining()
        if limit is not None:
            remaining = min(remaining, limit)
        if remaining < _MIN_ATTEMPT_S:
            return None
        try:
            raw = await asyncio.wait_for(
                llm_call_async(
                    url=url,
                    model=model,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    headers=headers,
                    omit_generation_params=True,
                    # Track the shared budget: a fixed inner timeout either
                    # capped the outer one or outlived it.
                    timeout=remaining,
                ),
                timeout=remaining,
            )
            return _extract_rewritten_paragraphs(raw, paragraphs)
        except asyncio.TimeoutError:
            _note("Rewriting", f"Model did not answer within {remaining:.0f}s")
            return None
        except HTTPException as exc:
            # Configuration/auth faults (401, 404 model, ...) will fail the same
            # way on every retry — surface them instead of burning the budget.
            if exc.status_code in (401, 403, 404):
                raise
            _note("Retrying", f"Endpoint returned {exc.status_code} — retrying")
            logger.warning("Rewrite call failed with HTTP %s: %s", exc.status_code, exc.detail)
            return None
        except Exception as exc:
            # A transient blip (connection reset, read error, malformed chunk)
            # used to abort the whole job, which is why a rewrite would fail and
            # then succeed on a second click. Treat it like a timeout: return
            # None so the caller can spend the remaining budget on another try.
            _note("Retrying", f"{type(exc).__name__} from endpoint — retrying")
            logger.warning("Rewrite call raised %s: %s", type(exc).__name__, str(exc)[:200])
            return None

    async def _call(
        *,
        divergent: bool = False,
        notes: list[str] | None = None,
        limit: float | None = None,
    ) -> list[str] | None:
        user_content = _document_prompt(source_paragraphs)
        if divergent:
            preamble = [
                "Your previous attempt was rejected. Produce a substantially different version.",
            ]
            if notes:
                preamble.append("Fix specifically:")
                preamble.extend(f"- {n}" for n in notes)
            else:
                preamble.append(
                    "Use very different sentence structures, clauses, and word choices from the original."
                )
            user_content = "\n".join(preamble) + "\n\n" + user_content
        return await _ask(source_paragraphs, user_content, limit=limit)

    async def _batched() -> tuple[list[str], int]:
        """Rewrite in small batches. Returns (paragraphs, rewritten_count);
        any batch that doesn't finish keeps its source text."""
        result = list(source_paragraphs)
        done = 0
        batch_size = 2
        for start in range(0, len(source_paragraphs), batch_size):
            if _remaining() < _MIN_ATTEMPT_S:
                break
            chunk = source_paragraphs[start : start + batch_size]
            _note(
                "Rewriting",
                f"Working through paragraph {start + 1}-{min(start + batch_size, len(source_paragraphs))}"
                f" of {len(source_paragraphs)}",
            )
            rewritten = await _ask(chunk, _document_prompt(chunk))
            if rewritten:
                for offset, para in enumerate(rewritten):
                    if start + offset < len(result) and para.strip():
                        result[start + offset] = para
                        done += 1
        return result, done

    def _score(paragraphs: list[str], joined: str) -> float:
        sim = sum(
            _rewrite_similarity(s, r)
            for s, r in zip(source_paragraphs, paragraphs)
        ) / max(1, len(source_paragraphs))
        # Fidelity is weighted hard: a fluent rewrite that lost "17%" or "TLS"
        # is worse than a stiffer one that kept them.
        fidelity = _fidelity_report(text, joined)["score"]
        return _compute_human_score(joined) - sim * 0.3 - (1.0 - fidelity) * 0.5

    def _metrics(joined: str, *, paragraphs_rewritten: int | None = None) -> dict:
        stats = _sentence_length_stats(joined)
        fidelity = _fidelity_report(text, joined)
        extra = {}
        if paragraphs_rewritten is not None:
            # Set only on the batched fallback, where some paragraphs may have
            # been left as source text — the caller shouldn't have to guess.
            extra = {
                "paragraphs_rewritten": paragraphs_rewritten,
                "paragraphs_total": len(source_paragraphs),
                "partial": paragraphs_rewritten < len(source_paragraphs),
            }
        return {
            **extra,
            "human_score": round(_compute_human_score(joined), 3),
            "burstiness": round(_burstiness(joined), 3),
            "sentence_length_cv": round(stats["cv"], 3),
            "sentence_length_min": stats["min"],
            "sentence_length_max": stats["max"],
            "trigram_diversity": round(_ngram_diversity(joined, 3), 3),
            "opening_diversity": round(_opening_shape_diversity(joined), 3),
            "filler_per_100_words": round(_filler_density(joined), 2),
            "fidelity": round(fidelity["score"], 3),
            "missing_anchors": fidelity["missing_numbers"] + fidelity["missing_acronyms"],
        }

    # Batching only helps when there is more than one paragraph to split — on a
    # single-paragraph document a "batch" IS the whole document, so cutting the
    # first attempt short just to re-run identical work doubles the wait for no
    # gain (the "Working through paragraph 1-1 of 1" case). Give the whole
    # budget to the first attempt whenever the fallback has nothing to offer.
    can_batch = len(source_paragraphs) > 1
    first_limit = budget * _FIRST_ATTEMPT_SHARE if can_batch else None

    _note("Rewriting", "Generating a structurally distinct rewrite")
    rewritten = await _call(divergent=False, limit=first_limit)

    if rewritten is None and not can_batch and _remaining() >= _MIN_ATTEMPT_S:
        # Nothing to fall back to, but budget is left (the call failed fast —
        # a transient endpoint error rather than a timeout). One clean retry
        # here is what stops "click Rewrite, it fails, click again, it works".
        _note("Retrying", "First attempt did not return — retrying")
        rewritten = await _call(divergent=False)

    if rewritten is not None:
        joined = _join_paragraphs(rewritten, separators)
        too_similar = _needs_stronger_rewrite(source_paragraphs, rewritten)
        fidelity_ok = _fidelity_report(text, joined)["score"] >= 1.0

        if (
            not too_similar
            and fidelity_ok
            and _compute_human_score(joined) >= _HUMAN_SCORE_THRESHOLD
        ):
            return joined, _metrics(joined)

        # Retry with the specific failures named, if time remains.
        notes = _revision_notes(text, joined, rewritten, source_paragraphs)
        _note("Refining", "First pass missed the quality bar — retrying with notes")
        stronger = await _call(divergent=True, notes=notes)
        if stronger is not None:
            stronger_joined = _join_paragraphs(stronger, separators)
            if _score(stronger, stronger_joined) > _score(rewritten, joined):
                return stronger_joined, _metrics(stronger_joined)
        return joined, _metrics(joined)

    # The whole-document call didn't finish. Rather than failing outright, spend
    # what's left on small batches — each call is a fraction of the size, so a
    # slow model usually lands most of them.
    if can_batch:
        logger.warning(
            "Whole-document rewrite timed out after %.0fs budget; falling back to batches", budget
        )
        _note("Rewriting", f"Rewriting {len(source_paragraphs)} paragraphs in batches")
        batched, done = await _batched()
        if done:
            joined = _join_paragraphs(batched, separators)
            return joined, _metrics(joined, paragraphs_rewritten=done)

    raise HTTPException(
        504,
        f"The model did not return a rewrite within {budget:.0f}s. "
        "Try a shorter passage, a faster model, or raise HUMANIZE_TIMEOUT_S.",
    )


def _cleanup_jobs(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.monotonic()) - _JOB_TTL_SECONDS
    expired = [
        job_id
        for job_id, job in _HUMANIZE_JOBS.items()
        if job.get("created_at", 0) < cutoff
    ]
    for job_id in expired:
        _HUMANIZE_JOBS.pop(job_id, None)


async def _run_humanize_job(
    job_id: str,
    *,
    text: str,
    url: str,
    model: str,
    headers: dict,
) -> None:
    job = _HUMANIZE_JOBS[job_id]
    started_at = time.monotonic()
    # Append-only phase log. The status endpoint returns the whole list, so the
    # UI can show what happened across a long run — including phases that have
    # already been superseded by the time it next polls.
    job.setdefault("log", [])

    def _log(step: str, detail: str) -> None:
        job["log"].append({
            "t": round(time.monotonic() - started_at, 1),
            "step": step,
            "detail": detail,
        })

    job.update(
        status="running",
        step="Rewriting",
        detail="Generating a structurally distinct rewrite",
        progress=0.25,
    )
    _log("Queued", f"{len(text)} chars, budget {_budget_seconds(text):.0f}s")

    def _progress(step: str, detail: str) -> None:
        # A long rewrite used to sit on "Rewriting" for its whole run; the
        # phases now report as they happen.
        job.update(step=step, detail=detail, progress=min(0.9, job.get("progress", 0.25) + 0.15))
        _log(step, detail)

    try:
        rewritten, metrics = await _rewrite_document(
            text,
            url=url,
            model=model,
            headers=headers,
            on_progress=_progress,
        )
        _validate_candidate(text, rewritten)
        elapsed = time.monotonic() - started_at
        _log(
            "Complete",
            f"Finished in {elapsed:.1f}s · human score {metrics.get('human_score')} · "
            f"fidelity {metrics.get('fidelity')}"
            + (f" · {len(metrics['missing_anchors'])} anchor(s) dropped"
               if metrics.get("missing_anchors") else ""),
        )
        job.update(
            status="done",
            step="Complete",
            detail="Rewrite finished",
            progress=1.0,
            result={"text": rewritten, "metrics": metrics},
            elapsed=elapsed,
        )
    except HTTPException as exc:
        _log("Error", str(exc.detail))
        job.update(
            status="error",
            step="Error",
            detail="Rewrite model failed",
            error=str(exc.detail),
            elapsed=time.monotonic() - started_at,
        )
    except Exception as exc:
        logger.exception("Rewrite background job failed")
        _log("Error", f"{type(exc).__name__}: {str(exc)[:200]}")
        job.update(
            status="error",
            step="Error",
            detail="Rewrite pipeline failed",
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
            elapsed=time.monotonic() - started_at,
        )


def setup_humanize_routes() -> APIRouter:
    router = APIRouter()

    @router.post("/api/humanize", status_code=202)
    async def humanize(payload: HumanizeRequest, request: Request) -> dict:
        owner = require_user(request)
        resolved = resolve_endpoint_by_id(payload.endpoint_id, payload.model, owner=owner)
        if not resolved:
            raise HTTPException(404, "Selected rewrite model is unavailable")

        url, model, headers = resolved
        _cleanup_jobs()
        job_id = str(uuid.uuid4())
        _HUMANIZE_JOBS[job_id] = {
            "owner": owner,
            "created_at": time.monotonic(),
            "status": "queued",
            "step": "Queued",
            "detail": "Waiting for rewrite worker",
            "progress": 0.1,
        }
        task = asyncio.create_task(
            _run_humanize_job(
                job_id,
                text=payload.text,
                url=url,
                model=model,
                headers=headers,
            )
        )
        _HUMANIZE_TASKS.add(task)
        task.add_done_callback(_HUMANIZE_TASKS.discard)
        return {"job_id": job_id}

    @router.get("/api/humanize/status/{job_id}")
    async def humanize_status(job_id: str, request: Request) -> dict:
        owner = require_user(request)
        _cleanup_jobs()
        job = _HUMANIZE_JOBS.get(job_id)
        if not job or (job.get("owner") and job.get("owner") != owner):
            raise HTTPException(404, "Rewrite job not found")
        return {key: value for key, value in job.items() if key not in {"owner", "created_at"}}

    return router
