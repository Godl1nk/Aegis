"""Source-grounded durable web knowledge for the agent.

Knowledge is persisted only after conservative, deterministic checks: at
least two independent fetched domains must contain the claim's meaningful
terms and exact numeric values. High-stakes claims are never auto-learned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlparse

from services.search import comprehensive_web_search
from src.settings import get_setting


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
    r"\bmy name is\b|\bi live at\b|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"\b\d{3}-\d{2}-\d{4}\b)",
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
_CONTENT_RE = re.compile(
    r"\[CONTENT\s+(\d+)\]\s+From:\s*([^\n]+)\n"
    r"Title:\s*([^\n]*)\n-+\n(.*?)"
    r"(?=\n\[CONTENT\s+\d+\]\s+From:|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _get_memory_dependencies():
    from src.ai_interaction import get_memory_manager, get_memory_vector

    return get_memory_manager(), get_memory_vector()


def _meaningful_tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9][a-z0-9_.+-]*", text.lower())
        if len(token) >= 3 and token not in _STOPWORDS
    }


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


def _support_score(claim: str, content: str) -> float:
    claim_tokens = _meaningful_tokens(claim)
    content_tokens = _meaningful_tokens(content)
    if not claim_tokens:
        return 0.0
    numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", claim))
    if numbers and not numbers.issubset(set(re.findall(r"\b\d+(?:\.\d+)?\b", content))):
        return 0.0
    return len(claim_tokens & content_tokens) / len(claim_tokens)


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


def _supporting_sources(claim: str, context: str, sources: list[dict]) -> list[dict]:
    try:
        threshold = float(get_setting("knowledge_support_threshold", 0.55) or 0.55)
    except (TypeError, ValueError):
        threshold = 0.55
    threshold = max(0.4, min(threshold, 0.9))
    by_index = {
        idx: source for idx, source in enumerate(sources, 1)
        if isinstance(source, dict)
    }
    accepted = []
    seen_domains = set()
    for match in _CONTENT_RE.finditer(context or ""):
        index = int(match.group(1))
        fetched_url = match.group(2).strip()
        title = match.group(3).strip()
        content = match.group(4).strip()
        source = by_index.get(index, {})
        url = str(source.get("url") or fetched_url).strip()
        domain = _root_domain(url)
        if not domain or domain in seen_domains:
            continue
        score = _support_score(claim, content)
        if score < threshold:
            continue
        seen_domains.add(domain)
        accepted.append({
            "url": url,
            "title": str(source.get("title") or title)[:300],
            "domain": domain,
            "support_score": round(score, 3),
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
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
        if _PRIVATE_RE.search(f"{claim} {query}"):
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
            search_context, sources = await asyncio.wait_for(
                asyncio.to_thread(
                    comprehensive_web_search,
                    query,
                    max_pages=6,
                    return_sources=True,
                ),
                timeout=45,
            )
        except asyncio.TimeoutError:
            return {"error": "Knowledge validation search timed out", "exit_code": 1}
        except Exception as exc:
            return {"error": f"Knowledge validation search failed: {exc}", "exit_code": 1}

        supporting = _supporting_sources(claim, search_context, sources or [])
        try:
            minimum = int(get_setting("knowledge_min_sources", 2) or 2)
        except (TypeError, ValueError):
            minimum = 2
        minimum = max(2, min(minimum, 5))
        if len(supporting) < minimum:
            return {
                "error": (
                    f"Claim not saved: only {len(supporting)} independent fetched source(s) "
                    f"supported it; {minimum} required."
                ),
                "exit_code": 1,
                "validated": False,
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
        for ref in supporting:
            ref["retrieved_at"] = validated_at
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
            "results": f"Validated and saved knowledge from {len(supporting)} independent sources: {claim}",
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
