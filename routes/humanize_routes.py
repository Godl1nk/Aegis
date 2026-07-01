"""Sample-grounded text rewriting.

This route intentionally keeps rewriting model-driven. It does not score text
as "AI" or mutate prose with detector-oriented typo, punctuation, or synonym
heuristics.
"""

import asyncio
import logging
import math
import re
import time
import uuid
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
_CALL_TIMEOUT = 28

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

_SYSTEM_PROMPT = """You are a skilled human writer. Rewrite text so it reads as if a talented journalist or author wrote it.

### STRICT SENTENCE LENGTH RULES (Follow these closely):
- Aim for an average sentence length of 18-21 words.
- Use a lot of short sentences (many 6-14 words long).
- Include several very short sentences (4-9 words) for rhythm and punch.
- Maximum sentence length is 24 words. If an idea is longer, split it into 2 or more sentences.
- Create strong variation: alternate between short and medium sentences. Do not let several medium or long sentences appear in a row.
- Avoid smooth, flowing multi-clause sentences. Break them up.

### OTHER RULES:
1. Preserve every fact and meaning exactly. Do not add or remove anything.
2. Rebuild every sentence from scratch with different structure and wording.
3. Use natural, conversational but professional tone.
4. Use phrases like "Back then", "Nowadays", "These days" where they fit naturally.
5. Change sentence openings completely — no copied starts.
6. Keep technical terms accurate.
7. Maintain paragraph count and order of ideas.
8. Perfect grammar and natural flow.

Focus especially on creating obvious sentence length variation and many short sentences. This is the highest priority.

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


def _compute_human_score(text: str) -> float:
    if not text.strip():
        return 0.0
    stats = _sentence_length_stats(text)
    cv_score = min(stats["cv"] / 0.7, 1.0)
    bigram_div = _ngram_diversity(text, 2)
    trigram_div = _ngram_diversity(text, 3)
    starter_div = _sentence_starter_diversity(text)
    return 0.35 * cv_score + 0.25 * trigram_div + 0.25 * starter_div + 0.15 * bigram_div


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


def _join_paragraphs(paragraphs: list[str], separators: list[str]) -> str:
    pieces: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        pieces.append(paragraph)
        if index < len(separators):
            pieces.append(separators[index])
    return "".join(pieces)


async def _rewrite_document(
    text: str,
    *,
    url: str,
    model: str,
    headers: dict,
) -> str:
    """Rewrite with sequential attempts under a shared 28s deadline.

    First attempt uses a normal prompt.  If the result fails similarity or
    human-score checks, a second divergent attempt fires with whatever time
    remains.  Returns early when a candidate passes both gates.
    """
    source_paragraphs, separators = _split_document(text)
    deadline = time.monotonic() + _CALL_TIMEOUT
    _HUMAN_SCORE_THRESHOLD = 0.35

    async def _call(*, divergent: bool = False) -> list[str] | None:
        remaining = max(1.0, deadline - time.monotonic())
        user_content = _document_prompt(source_paragraphs)
        if divergent:
            user_content = (
                "Produce a substantially different version of the text. "
                "Use very different sentence structures, clauses, "
                "and word choices from the original.\n\n"
                + user_content
            )
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
                    timeout=120,
                ),
                timeout=remaining,
            )
            return _extract_rewritten_paragraphs(raw, source_paragraphs)
        except asyncio.TimeoutError:
            return None

    def _score(paragraphs: list[str], joined: str) -> float:
        sim = sum(
            _rewrite_similarity(s, r)
            for s, r in zip(source_paragraphs, paragraphs)
        ) / max(1, len(source_paragraphs))
        return _compute_human_score(joined) - sim * 0.3

    # First attempt
    rewritten = await _call(divergent=False)

    if rewritten is not None:
        joined = _join_paragraphs(rewritten, separators)
        too_similar = _needs_stronger_rewrite(source_paragraphs, rewritten)

        if not too_similar and _compute_human_score(joined) >= _HUMAN_SCORE_THRESHOLD:
            return joined

        # Retry with divergent prompt if time remains
        stronger = await _call(divergent=True)
        if stronger is not None:
            stronger_joined = _join_paragraphs(stronger, separators)
            if _score(stronger, stronger_joined) > _score(rewritten, joined):
                return stronger_joined
        return joined

    raise HTTPException(502, "All rewrite attempts timed out")


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
    job.update(
        status="running",
        step="Rewriting",
        detail="Generating a structurally distinct rewrite",
        progress=0.25,
    )
    try:
        rewritten = await _rewrite_document(
            text,
            url=url,
            model=model,
            headers=headers,
        )
        _validate_candidate(text, rewritten)
        job.update(
            status="done",
            step="Complete",
            detail="Rewrite finished",
            progress=1.0,
            result={"text": rewritten},
            elapsed=time.monotonic() - started_at,
        )
    except HTTPException as exc:
        job.update(
            status="error",
            step="Error",
            detail="Rewrite model failed",
            error=str(exc.detail),
            elapsed=time.monotonic() - started_at,
        )
    except Exception as exc:
        logger.exception("Rewrite background job failed")
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
