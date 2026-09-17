"""Source-grounded durable web knowledge for the agent.

Knowledge is persisted after deterministic evidence checks, not semantic proof.
One explicitly trusted host can suffice with strong support; otherwise two
independent fetched domains are required. High-stakes claims are not learned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import unquote, urlparse

from services.search import comprehensive_web_search
from services.search.content import fetch_webpage_content
from src.settings import DEFAULT_SETTINGS, get_setting


_STOPWORDS = {
    "about", "after", "also", "and", "are", "been", "being", "but", "can",
    "does", "for", "from", "had", "has", "have", "into", "its", "not", "of",
    "on", "or", "that", "the", "their", "this", "through", "to", "was", "were",
    "which", "with", "would",
}
_HIGH_STAKES_RE = re.compile(
    r"\b(?:diagnos(?:is|e)|dosage|dose|medication|patient|treatment|prescription|"
    r"legal advice|lawsuit|criminal|tax advice|invest(?:ment|ing)|financial advice|"
    r"buy (?:stock|shares|crypto)|sell (?:stock|shares|crypto)|trade (?:stock|crypto)|"
    r"suicide|self-harm|emergency)\b",
    re.IGNORECASE,
)
_PRIVATE_RE = re.compile(
    r"(?:\b(?:my\s+)?(?:password|passphrase|api[_ -]?key|access[_ -]?token|"
    r"private[_ -]?key|secret)\s*(?:is|=|:)\s*\S+|\bsk-[a-z0-9_-]{12,}\b|"
    r"\bmy name is\b|\bi live at\b|\b(?:my\s+)?(?:home|postal|mailing)?\s*"
    r"address\s*(?:is|=|:)\s*\S|\b(?:postal|zip)\s*(?:code)?\s*(?:is|=|:)\s*"
    r"[a-z0-9][a-z0-9 -]{2,10}\b|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"\b\d{3}-\d{2}-\d{4}\b|\b(?:phone|mobile|cell|telephone|contact\s+number)"
    r"\s*(?:is|=|:)?\s*\+?[\d(). -]{7,}\d\b|"
    r"(?<!\w)\+\d[\d(). -]{6,}\d(?!\w))",
    re.IGNORECASE,
)
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|neither|without|cannot|can't|doesn't|isn't|aren't|"
    r"wasn't|weren't|won't|false|incorrect|unsupported)\b",
    re.IGNORECASE,
)
_VERY_FRESH_RE = re.compile(
    r"\b(?:price|weather|score|standings|breaking|today|right now|currently)\b",
    re.IGNORECASE,
)
_FRESH_RE = re.compile(
    r"\b(?:latest|current|newest|version|release|president|ceo|schedule|law|rule)\b",
    re.IGNORECASE,
)
_MAX_EVIDENCE_CHARS = 20000
_SINGLE_SOURCE_SUPPORT = 0.8


def _get_memory_dependencies():
    from src.ai_interaction import get_memory_manager, get_memory_vector

    return get_memory_manager(), get_memory_vector()


def _meaningful_tokens(text: str) -> set[str]:
    tokens = {
        token.strip("._+-")
        for token in re.findall(r"[a-z0-9][a-z0-9_.+-]*", text.lower())
    }
    return {token for token in tokens if len(token) >= 3 and token not in _STOPWORDS}


def _looks_like_payment_card(text: str) -> bool:
    """Detect plausible card numbers without rejecting arbitrary long IDs."""
    for match in _CARD_CANDIDATE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if not 13 <= len(digits) <= 19:
            continue
        checksum = 0
        parity = len(digits) % 2
        for idx, char in enumerate(digits):
            value = int(char)
            if idx % 2 == parity:
                value *= 2
                if value > 9:
                    value -= 9
            checksum += value
        if checksum % 10 == 0:
            return True
    return False


def _contains_private_data(text: str) -> bool:
    return bool(_PRIVATE_RE.search(text or "") or _looks_like_payment_card(text or ""))


def _root_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in {"co.uk", "org.uk", "com.au", "co.jp", "com.sg"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _support_score(claim: str, content: str, *, opposite: bool = False) -> float:
    claim_tokens = _meaningful_tokens(claim)
    if not claim_tokens:
        return 0.0
    numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", claim))
    claim_is_negated = bool(_NEGATION_RE.search(claim))
    if opposite:
        claim_is_negated = not claim_is_negated
    chunks = [
        chunk.strip()
        for chunk in re.split(r"(?<=[.!?])\s+|\n+", content or "")
        if chunk.strip()
    ]
    # A relevant assertion can span a heading and its following sentence, but
    # page-wide bag-of-words matching is too permissive and misses negation.
    candidates = chunks + [
        f"{chunks[idx]} {chunks[idx + 1]}"
        for idx in range(len(chunks) - 1)
    ]
    best = 0.0
    for candidate in candidates:
        if bool(_NEGATION_RE.search(candidate)) != claim_is_negated:
            continue
        if numbers and not numbers.issubset(
            set(re.findall(r"\b\d+(?:\.\d+)?\b", candidate))
        ):
            continue
        score = len(claim_tokens & _meaningful_tokens(candidate)) / len(claim_tokens)
        best = max(best, score)
    return best


def _ttl_days(claim: str, query: str) -> int:
    text = f"{claim} {query}"
    if _VERY_FRESH_RE.search(text):
        return 1
    if _FRESH_RE.search(text):
        return 30
    try:
        configured = int(get_setting("knowledge_default_ttl_days", 180) or 180)
    except (TypeError, ValueError):
        configured = 180
    return max(1, min(configured, 365))


def _source_urls(value) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValueError("source_urls must contain 1-5 public HTTP(S) URLs")
    urls = []
    for raw in value:
        if not isinstance(raw, str) or len(raw) > 2000 or re.search(r"[\s\\]", raw):
            raise ValueError("source_urls must contain valid HTTP(S) URLs")
        parsed = urlparse(raw)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443}):
            raise ValueError("source_urls must contain public HTTP(S) URLs without credentials")
        if _contains_private_data(unquote(raw)):
            raise ValueError("Private or secret source URLs cannot be used for knowledge validation")
        url = parsed._replace(fragment="").geturl()
        if url not in urls:
            urls.append(url)
    return urls


def _trusted_source(url: str) -> bool:
    hosts = get_setting("knowledge_single_source_hosts", DEFAULT_SETTINGS["knowledge_single_source_hosts"])
    if not isinstance(hosts, list):
        return False
    # Exact hosts only, never suffix matches or all subdomains of shared hosts.
    allowed = {str(host).strip().lower().removeprefix("www.") for host in hosts}
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None
                and (parsed.hostname or "").lower().removeprefix("www.") in allowed)
    except ValueError:
        return False


async def _fetched_evidence(urls: list[str]) -> list[dict]:
    async def fetch(url):
        try:
            # Shares the guarded public-page cache with web_search/web_fetch.
            result = await asyncio.to_thread(fetch_webpage_content, url, timeout=8)
            if not result.get("success") or result.get("error") or result.get("truncated"):
                return None
            final_url = result.get("final_url")
            if not final_url or not result.get("content"):
                return None
            final_url = _source_urls([final_url])[0]
            return {
                "url": final_url,
                "title": str(result.get("title") or "")[:300],
                "content": str(result["content"])[:_MAX_EVIDENCE_CHARS],
                "retrieved_at": result.get("fetched_at") or int(time.time()),
            }
        except Exception:
            return None
    return [row for row in await asyncio.gather(*(fetch(url) for url in urls)) if row]


def _supporting_sources(claim: str, evidence: list[dict]) -> list[dict]:
    try:
        threshold = float(get_setting("knowledge_support_threshold", 0.55) or 0.55)
    except (TypeError, ValueError):
        threshold = 0.55
    threshold = max(0.4, min(threshold, 0.9))
    accepted = []
    seen_domains = set()
    seen_content_hashes = set()
    scored = [(_support_score(claim, source["content"]), source) for source in evidence]
    scored.sort(key=lambda pair: (
        pair[0] >= _SINGLE_SOURCE_SUPPORT and _trusted_source(pair[1]["url"]), pair[0]
    ), reverse=True)
    for score, source in scored:
        content = source["content"]
        url = source["url"]
        domain = _root_domain(url)
        if not domain or domain in seen_domains:
            continue
        if score < threshold:
            continue
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_hash in seen_content_hashes:
            continue
        seen_domains.add(domain)
        seen_content_hashes.add(content_hash)
        accepted.append({
            "url": url,
            "title": source["title"],
            "domain": domain,
            "support_score": score,  # Do not round a near-miss up to the trust threshold.
            "content_hash": content_hash,
            "retrieved_at": source["retrieved_at"],
        })
    return accepted


def _is_fresh(item: dict) -> bool:
    try:
        return int(item.get("expires_at") or 0) > int(time.time())
    except (TypeError, ValueError):
        return False


def _vector_search(vector, query: str, *, k: int, owner, kind: str) -> list[dict]:
    try:
        return vector.search(query, k=k, owner=owner, kind=kind)
    except TypeError:
        try:
            return vector.search(query, k=k, owner=owner)
        except Exception:
            return []
    except Exception:
        return []


class KnowledgeTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content or "{}")
        except json.JSONDecodeError:
            return {"error": "manage_knowledge expects a JSON object", "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": "manage_knowledge expects a JSON object", "exit_code": 1}

        action = str(args.get("action") or "").strip().lower()
        owner = (ctx or {}).get("owner")
        manager, vector = _get_memory_dependencies()
        if manager is None:
            return {"error": "Knowledge store is unavailable", "exit_code": 1}

        if action == "learn":
            return await self._learn(args, owner, manager, vector)
        if action == "list":
            rows = manager.load_knowledge(owner=owner, limit=50)
            return self._format_rows(rows, "Validated knowledge")
        if action == "search":
            query = str(args.get("query") or args.get("text") or "").strip()
            if not query:
                return {"error": "Knowledge search requires query", "exit_code": 1}
            rows = []
            if vector and getattr(vector, "healthy", False) and hasattr(manager, "load_by_ids"):
                hits = _vector_search(
                    vector,
                    query,
                    k=20,
                    owner=owner,
                    kind="knowledge",
                )
                rows = manager.load_by_ids(
                    [hit.get("memory_id") for hit in hits if isinstance(hit, dict)],
                    owner=owner,
                )
                rows = [row for row in rows if row.get("kind") == "knowledge" and _is_fresh(row)]
            if not rows:
                terms = _meaningful_tokens(query)
                candidates = manager.load_knowledge(owner=owner, limit=200)
                rows = sorted(
                    candidates,
                    key=lambda row: len(terms & _meaningful_tokens(str(row.get("text") or ""))),
                    reverse=True,
                )[:20]
                rows = [row for row in rows if terms & _meaningful_tokens(str(row.get("text") or ""))]
            return self._format_rows(rows, f"Knowledge matching {query!r}")
        if action == "delete":
            memory_id = str(args.get("knowledge_id") or args.get("memory_id") or "").strip()
            if not memory_id:
                return {"error": "Knowledge delete requires knowledge_id", "exit_code": 1}
            matches = manager.load_by_ids([memory_id], owner=owner)
            if not matches or matches[0].get("kind") != "knowledge":
                return {"error": "Knowledge entry not found", "exit_code": 1}
            if not manager.delete_entry(matches[0]["id"], owner=owner):
                return {"error": "Knowledge entry not found", "exit_code": 1}
            if vector and getattr(vector, "healthy", False):
                vector.remove(matches[0]["id"])
            return {"results": "Knowledge entry deleted", "knowledge_id": matches[0]["id"]}
        return {
            "error": "Unknown action. Use learn, list, search, or delete",
            "exit_code": 1,
        }

    async def _learn(self, args, owner, manager, vector) -> dict:
        enabled = get_setting("knowledge_auto_learn_enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        if not bool(enabled):
            return {"error": "Automatic knowledge learning is disabled", "exit_code": 1}
        claim = " ".join(str(args.get("claim") or "").split()).strip()
        query = " ".join(str(args.get("query") or claim).split()).strip()
        if len(claim) < 12 or len(claim) > 2000:
            return {"error": "Knowledge claim must be 12-2000 characters", "exit_code": 1}
        if _contains_private_data(f"{claim} {query}"):
            return {
                "error": "Private, personal, or secret content is never sent to web validation or saved as knowledge.",
                "exit_code": 1,
            }
        if _HIGH_STAKES_RE.search(f"{claim} {query}"):
            return {
                "error": "High-stakes claims are never auto-learned; keep cited sources in the answer instead.",
                "exit_code": 1,
            }

        try:
            minimum = int(get_setting("knowledge_min_sources", 1))
        except (TypeError, ValueError, OverflowError):
            minimum = 2
        minimum = max(1, min(minimum, 5))
        try:
            urls = _source_urls(args["source_urls"]) if "source_urls" in args else None
        except ValueError as exc:
            return {"error": str(exc), "exit_code": 1, "validated": False}

        async def gather_evidence():
            selected = urls
            if selected is None:
                # Compatibility for older skills. One bounded discovery pass,
                # then use actual fetched pages, never snippets or model text.
                page_limit = max(3, minimum)
                _, sources = await asyncio.to_thread(
                    comprehensive_web_search, query, max_pages=page_limit, return_sources=True,
                )
                selected = []
                for source in (sources or [])[:page_limit]:
                    try:
                        selected.extend(_source_urls([source.get("url")]))
                    except (ValueError, AttributeError):
                        continue
                selected = list(dict.fromkeys(selected))
            return await _fetched_evidence(selected)

        try:
            evidence = await asyncio.wait_for(gather_evidence(), timeout=20)
        except asyncio.TimeoutError:
            return {"error": "Knowledge validation timed out; not saved. Do not retry this turn.", "exit_code": 1, "validated": False}
        except Exception as exc:
            return {"error": f"Knowledge validation search failed: {exc}", "exit_code": 1}

        if any(_support_score(claim, row["content"], opposite=True) >= _SINGLE_SOURCE_SUPPORT for row in evidence):
            return {"error": "Fetched evidence may contradict this claim; not saved. Review the sources instead of retrying.", "exit_code": 1, "validated": False}
        supporting = _supporting_sources(claim, evidence)
        primary_supported = any(
            _trusted_source(ref["url"]) and ref["support_score"] >= _SINGLE_SOURCE_SUPPORT
            for ref in supporting
        )
        required = minimum if primary_supported else max(2, minimum)
        if len(supporting) < required:
            return {
                "error": (
                    f"Claim not saved: only {len(supporting)} independent fetched source(s) "
                    f"supported it; {required} required for this evidence. "
                    "One source is sufficient only when the configured minimum is 1 and a trusted host strongly supports the claim. "
                    "Do not retry learning this turn."
                ),
                "exit_code": 1,
                "validated": False,
                "supporting_sources": len(supporting),
                "required_sources": required,
            }

        if vector and getattr(vector, "healthy", False) and hasattr(manager, "load_by_ids"):
            similar_hits = _vector_search(
                vector,
                claim,
                k=3,
                owner=owner,
                kind="knowledge",
            )
            possible_ids = [
                hit.get("memory_id") for hit in similar_hits
                if isinstance(hit, dict) and float(hit.get("score") or 0) >= 0.86
            ]
            existing_rows = manager.load_by_ids(possible_ids, owner=owner)
            normalized_claim = claim.casefold()
            for existing in existing_rows:
                if existing.get("kind") != "knowledge":
                    continue
                existing_text = " ".join(str(existing.get("text") or "").split()).casefold()
                if existing_text and existing_text != normalized_claim:
                    return {
                        "error": (
                            "Possible conflict with existing validated knowledge; "
                            "review or delete the older entry before saving this claim."
                        ),
                        "exit_code": 1,
                        "validated": False,
                        "conflict_with": existing.get("id"),
                        "existing_claim": existing.get("text"),
                    }

        validated_at = int(time.time())
        expires_at = validated_at + (_ttl_days(claim, query) * 86400)
        confidence = (
            "high"
            if len(supporting) >= 3
            and sum(ref["support_score"] for ref in supporting) / len(supporting) >= 0.75
            else "medium"
        )
        entry = manager.upsert_knowledge(
            owner=owner,
            text=claim,
            source_refs=supporting,
            query=query,
            confidence=confidence,
            validated_at=validated_at,
            expires_at=expires_at,
            kind="knowledge",
        )
        if vector and getattr(vector, "healthy", False):
            try:
                vector.add(entry["id"], claim, owner=owner, kind="knowledge")
            except Exception:
                pass
        return {
            "results": f"Validated and saved knowledge from {len(supporting)} fetched source(s): {claim}",
            "knowledge_id": entry["id"],
            "validated": True,
            "confidence": confidence,
            "expires_at": expires_at,
            "sources": [{"title": ref["title"], "url": ref["url"]} for ref in supporting],
        }

    @staticmethod
    def _format_rows(rows: list[dict], heading: str) -> dict:
        if not rows:
            return {"results": f"{heading}: none found."}
        lines = [f"{heading}: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}"]
        for row in rows[:50]:
            domains = [
                str(ref.get("domain") or "")
                for ref in row.get("source_refs") or []
                if isinstance(ref, dict) and ref.get("domain")
            ]
            source_note = f"; sources: {', '.join(domains[:3])}" if domains else ""
            lines.append(f"- `{str(row.get('id') or '')[:8]}` {row.get('text', '')}{source_note}")
        return {"results": "\n".join(lines), "knowledge": rows[:50]}
