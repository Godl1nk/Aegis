"""Background Dreaming memory synthesis.

This is intentionally conservative: observations are source-grounded, derived
memory items are marked `kind=synthesized`, and summaries are reviewable. The
legacy extractor can continue to add explicit/auto saved memories while this
layer builds cross-chat continuity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Optional

from core.database import utcnow_naive
from services.memory.memory_extractor import _fallback_memory_candidates

logger = logging.getLogger(__name__)


def _session_messages(session) -> list[dict]:
    try:
        messages = session.get_context_messages()
    except Exception:
        messages = getattr(session, "history", []) or []
    out = []
    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "role", "")
            content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = " ".join(
                str(block.get("text") or block.get("content") or "")
                for block in content
                if isinstance(block, dict)
            )
        out.append({"role": role, "content": str(content or "")})
    return out


def _is_sensitive(text: str) -> bool:
    """Avoid proactive sensitive memories unless explicit remember/save wording exists."""
    t = (text or "").lower()
    explicit = any(x in t for x in ("remember", "save this", "memorize", "store this"))
    sensitive = any(
        x in t
        for x in (
            "diagnosed", "diagnosis", "medication", "therapy", "religion",
            "political", "sexual", "ssn", "social security", "passport",
            "credit card", "password",
        )
    )
    return sensitive and not explicit


def _dream_sync(
    session,
    memory_manager,
    memory_vector,
    *,
    owner: Optional[str] = None,
) -> dict:
    """Synchronous core of dream_from_session — runs in a worker thread."""
    v2 = getattr(memory_manager, "_v2", None)
    if v2 is None:
        return {"ok": False, "reason": "memory_v2_unavailable"}

    from core.database import MemoryJob

    job_id = str(uuid.uuid4())
    source_id = getattr(session, "session_id", None) or getattr(session, "id", None) or ""
    now = utcnow_naive()
    with v2._db() as db:
        running = (
            db.query(MemoryJob)
            .filter(
                MemoryJob.owner == owner,
                MemoryJob.source_type == "chat",
                MemoryJob.source_id == source_id,
                MemoryJob.status == "running",
            )
            .first()
        )
        if running:
            return {"ok": True, "status": "already_running", "job_id": running.id}

        recent_success = (
            db.query(MemoryJob)
            .filter(
                MemoryJob.owner == owner,
                MemoryJob.source_type == "chat",
                MemoryJob.source_id == source_id,
                MemoryJob.status == "success",
                MemoryJob.updated_at >= now - timedelta(seconds=60),
            )
            .first()
        )
        if recent_success:
            return {"ok": True, "status": "recently_processed", "job_id": recent_success.id}

        db.add(MemoryJob(
            id=job_id,
            owner=owner,
            job_type="dream",
            status="running",
            source_type="chat",
            source_id=source_id,
            progress=10,
        ))

    added_obs = []
    added_items = []
    try:
        messages = _session_messages(session)
        source_label = getattr(session, "name", None) or source_id or "Chat"

        # Check if any user message has explicit save intent — if so, bypass
        # the sensitive-keyword filter entirely. The extracted candidate text
        # won't contain "remember" but the user clearly asked to remember.
        user_texts = " ".join(
            m["content"] for m in messages if m.get("role") == "user"
        ).lower()
        has_explicit_save = any(
            x in user_texts
            for x in ("remember", "save this", "memorize", "store this")
        )

        candidates = _fallback_memory_candidates(messages)
        for cand in candidates:
            text = cand.get("text", "").strip()
            if not text:
                continue
            if not has_explicit_save and _is_sensitive(text):
                continue
            obs = v2.add_observation(
                owner=owner,
                text=text,
                category=cand.get("category") or "fact",
                source_type="chat",
                source_id=source_id,
                source_label=source_label,
                confidence="heuristic",
            )
            if not obs:
                continue
            added_obs.append(obs)
            refs = [{
                "type": "chat",
                "id": source_id,
                "label": source_label,
                "observation_id": obs["id"],
            }]
            item = v2.upsert_synthesized_item(
                owner=owner,
                text=obs["text"],
                category=obs.get("category") or "fact",
                source_refs=refs,
            )
            if item:
                added_items.append(item)
                if memory_vector and getattr(memory_vector, "healthy", False):
                    try:
                        # Remove stale embedding before re-adding (upsert path)
                        memory_vector.remove(item["id"])
                        memory_vector.add(item["id"], item["text"], owner=owner, kind=item.get("kind"))
                    except TypeError:
                        memory_vector.add(item["id"], item["text"])
                    except Exception as e:
                        logger.warning("Dream memory vector add failed: %s", e)

        result = {
            "ok": True,
            "observations": len(added_obs),
            "items": len(added_items),
        }
        # Only write a new summary version when we actually added items;
        # otherwise we'd accumulate identical summary rows on every run.
        if added_items:
            summary = v2.write_summary(owner)
            result["summary_id"] = summary.get("id")

        with v2._db() as db:
            row = db.query(MemoryJob).filter(MemoryJob.id == job_id).first()
            if row:
                row.status = "success"
                row.progress = 100
                row.result_json = json.dumps(result)
                row.updated_at = utcnow_naive()
            else:
                logger.warning("Dream job %s disappeared before status update", job_id)
        return result
    except Exception as e:
        logger.exception("Dreaming memory synthesis failed")
        with v2._db() as db:
            row = db.query(MemoryJob).filter(MemoryJob.id == job_id).first()
            if row:
                row.status = "error"
                row.error = str(e)
                row.progress = 100
                row.result_json = json.dumps({"ok": False, "error": str(e)})
                row.updated_at = utcnow_naive()
        return {"ok": False, "error": str(e)}


async def dream_from_session(
    session,
    memory_manager,
    memory_vector=None,
    *,
    owner: Optional[str] = None,
) -> dict:
    """Create observations and synthesized memories from a completed chat.

    Delegates to _dream_sync in a worker thread so the event loop stays
    responsive during the DB-heavy extraction work.
    """
    return await asyncio.to_thread(
        _dream_sync, session, memory_manager, memory_vector, owner=owner,
    )


def response_memory_sources(response_id: str, owner: Optional[str]) -> dict:
    """Return memory sources stored on an assistant response metadata blob."""
    from core.database import ChatMessage, SessionLocal, Session

    db = SessionLocal()
    try:
        q = db.query(ChatMessage).join(Session).filter(ChatMessage.id == response_id)
        if owner is not None:
            q = q.filter(Session.owner == owner)
        msg = q.first()
        if not msg:
            return {"sources": []}
        try:
            meta = json.loads(msg.meta_data or "{}")
        except Exception:
            meta = {}
        memories = meta.get("memories_used") or []
        return {
            "response_id": response_id,
            "sources": [
                {
                    "type": m.get("source_type") or m.get("kind") or m.get("type") or "memory",
                    "id": m.get("id"),
                    "text": m.get("text"),
                    "category": m.get("category"),
                    "source_refs": m.get("source_refs") or [],
                }
                for m in memories
                if isinstance(m, dict)
            ],
        }
    finally:
        db.close()
