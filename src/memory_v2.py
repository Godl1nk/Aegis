"""Memory V2 storage and lightweight dreaming helpers.

The legacy public API still speaks dictionaries shaped like memory.json. This
module stores those dictionaries in SQLite and keeps enough metadata for the
Dreaming memory layer: synthesized memories, source observations, reviewable
summaries, feedback, and job status.
"""

from __future__ import annotations

__all__ = ["MemoryV2Store", "source_fingerprint"]

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from typing import Iterable, Optional


def _json_loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
        return parsed if parsed is not None else fallback
    except Exception:
        return fallback


def _json_dumps(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def source_fingerprint(owner: Optional[str], source_type: str, source_id: str, text: str) -> str:
    h = hashlib.sha256()
    h.update((owner or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update(source_type.encode("utf-8"))
    h.update(b"\x1f")
    h.update((source_id or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update(" ".join((text or "").lower().split()).encode("utf-8"))
    return h.hexdigest()


class MemoryV2Store:
    """SQLite-backed memory store with legacy dict compatibility."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.memory_file = os.path.join(data_dir, "memory.json")
        self.migration_state_file = os.path.join(data_dir, "memory_v2_state.json")

    @contextmanager
    def _db(self):
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _item_to_dict(item) -> dict:
        metadata = _json_loads(getattr(item, "metadata_json", None), {})
        out = {
            "id": item.id,
            "text": item.text,
            "timestamp": item.timestamp or int(time.time()),
            "source": item.source or "unknown",
            "category": item.category or "fact",
            "uses": item.uses or 0,
            "kind": item.kind or "saved",
            "status": item.status or "active",
        }
        if item.owner:
            out["owner"] = item.owner
        if item.session_id:
            out["session_id"] = item.session_id
        if item.pinned:
            out["pinned"] = True
        if item.priority:
            out["priority"] = item.priority
        if item.confidence:
            out["confidence"] = item.confidence
        refs = _json_loads(item.source_refs, [])
        if refs:
            out["source_refs"] = refs
        if isinstance(metadata, dict):
            out.update(metadata)
        return out

    @staticmethod
    def _apply_dict(item, entry: dict):
        item.text = str(entry.get("text", "")).strip()
        item.category = entry.get("category") or "fact"
        item.source = entry.get("source") or "user"
        item.owner = entry.get("owner")
        item.session_id = entry.get("session_id")
        item.timestamp = int(entry.get("timestamp") or time.time())
        item.uses = int(entry.get("uses") or 0)
        item.pinned = bool(entry.get("pinned"))
        item.priority = int(entry.get("priority") or 0)
        item.kind = entry.get("kind") or ("synthesized" if item.source == "dream" else "saved")
        item.status = entry.get("status") or "active"
        item.confidence = str(entry.get("confidence")) if entry.get("confidence") is not None else None
        item.source_refs = _json_dumps(entry.get("source_refs") or [])
        core = {
            "id", "text", "timestamp", "source", "category", "uses", "owner",
            "session_id", "pinned", "priority", "kind", "status", "confidence",
            "source_refs",
        }
        metadata = {k: v for k, v in entry.items() if k not in core}
        item.metadata_json = _json_dumps(metadata) if metadata else None

    def migrate_from_json_once(self):
        from core.database import MemoryItem

        marker_says_migrated = False
        try:
            with open(self.migration_state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict) and state.get("json_migrated"):
                marker_says_migrated = True
        except Exception:
            pass

        if marker_says_migrated:
            try:
                with self._db() as db:
                    if db.query(MemoryItem.id).first() is not None:
                        return
            except Exception:
                return

        if not os.path.exists(self.memory_file):
            return
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            return
        if not isinstance(entries, list):
            return

        with self._db() as db:
            for entry in entries:
                if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
                    continue
                mid = entry.get("id") or str(uuid.uuid4())
                item = db.query(MemoryItem).filter(MemoryItem.id == mid).first()
                if item is None:
                    item = MemoryItem(id=mid)
                    db.add(item)
                self._apply_dict(item, entry)
        try:
            os.makedirs(os.path.dirname(self.migration_state_file) or ".", exist_ok=True)
            with open(self.migration_state_file, "w", encoding="utf-8") as f:
                json.dump({"json_migrated": True, "timestamp": int(time.time())}, f, indent=2)
        except OSError:
            pass

    def load_all(self) -> list[dict]:
        from core.database import MemoryItem

        self.migrate_from_json_once()
        with self._db() as db:
            rows = (
                db.query(MemoryItem)
                .filter(MemoryItem.status == "active")
                .order_by(MemoryItem.timestamp.desc())
                .all()
            )
            return [self._item_to_dict(row) for row in rows]

    def load(self, owner: Optional[str] = None) -> list[dict]:
        rows = self.load_all()
        if owner is None:
            return rows
        return [row for row in rows if row.get("owner") == owner]

    def save(self, entries: Iterable[dict]):
        from core.database import MemoryItem

        entries = [e for e in entries if isinstance(e, dict) and str(e.get("text", "")).strip()]
        for entry in entries:
            if not entry.get("id"):
                entry["id"] = str(uuid.uuid4())
        ids = {e["id"] for e in entries}
        with self._db() as db:
            for entry in entries:
                mid = entry["id"]
                item = db.query(MemoryItem).filter(MemoryItem.id == mid).first()
                if item is None:
                    item = MemoryItem(id=mid)
                db.add(item)
                self._apply_dict(item, entry)
            # Archive items NOT in the incoming set — but NEVER touch
            # synthesized/dream items; those are managed by the dreamer,
            # not the legacy save path. Without this guard every save()
            # call silently destroyed all dream-generated memories.
            from sqlalchemy import or_
            q = db.query(MemoryItem).filter(
                or_(MemoryItem.kind.is_(None), MemoryItem.kind != "synthesized")
            )
            if ids:
                owners = {entry.get("owner") for entry in entries}
                if None in owners:
                    concrete = [owner for owner in owners if owner is not None]
                    owner_filter = MemoryItem.owner == None
                    if concrete:
                        owner_filter = or_(owner_filter, MemoryItem.owner.in_(concrete))
                    q = q.filter(owner_filter)
                else:
                    q = q.filter(MemoryItem.owner.in_(list(owners)))
                q = q.filter(MemoryItem.id.notin_(ids))
            for item in q.all():
                item.status = "archived"

    def increment_uses(self, ids: list[str]) -> None:
        from core.database import MemoryItem

        if not ids:
            return
        with self._db() as db:
            for item in db.query(MemoryItem).filter(MemoryItem.id.in_(ids)).all():
                item.uses = int(item.uses or 0) + 1

    def delete_item(self, memory_id: str, owner: Optional[str] = None) -> bool:
        from core.database import MemoryItem

        with self._db() as db:
            q = db.query(MemoryItem).filter(MemoryItem.id == memory_id)
            if owner is not None:
                q = q.filter(MemoryItem.owner == owner)
            item = q.first()
            if item is None:
                return False
            item.status = "deleted"
            return True

    def claim_ownerless(self, owner: str):
        from core.database import MemoryItem

        with self._db() as db:
            db.query(MemoryItem).filter(MemoryItem.owner == None).update({"owner": owner})

    def add_observation(
        self,
        *,
        owner: Optional[str],
        text: str,
        category: str = "fact",
        source_type: str = "chat",
        source_id: str = "",
        source_label: str = "",
        confidence: str | None = None,
    ) -> dict | None:
        from core.database import MemoryObservation

        text = " ".join((text or "").split()).strip()
        if not text:
            return None
        fp = source_fingerprint(owner, source_type, source_id, text)
        with self._db() as db:
            existing = (
                db.query(MemoryObservation)
                .filter(MemoryObservation.owner == owner, MemoryObservation.fingerprint == fp)
                .first()
            )
            if existing:
                return {"id": existing.id, "text": existing.text, "category": existing.category}
            obs = MemoryObservation(
                id=str(uuid.uuid4()),
                owner=owner,
                text=text,
                category=category or "fact",
                source_type=source_type,
                source_id=source_id,
                source_label=source_label,
                source_timestamp=int(time.time()),
                fingerprint=fp,
                confidence=confidence,
            )
            db.add(obs)
            return {"id": obs.id, "text": obs.text, "category": obs.category}

    def upsert_synthesized_item(self, *, owner: Optional[str], text: str, category: str, source_refs: list[dict]):
        from core.database import MemoryItem

        text_norm = " ".join((text or "").split()).strip()
        if not text_norm:
            return None
        with self._db() as db:
            existing = (
                db.query(MemoryItem)
                .filter(
                    MemoryItem.owner == owner,
                    MemoryItem.kind == "synthesized",
                    MemoryItem.category == (category or "fact"),
                    MemoryItem.text == text_norm,
                    MemoryItem.status != "deleted",
                )
                .first()
            )
            if existing is None:
                existing = MemoryItem(id=str(uuid.uuid4()), owner=owner, kind="synthesized")
                db.add(existing)
            existing.text = text_norm
            existing.category = category or "fact"
            existing.source = "dream"
            existing.status = "active"
            existing.timestamp = int(time.time())
            existing.source_refs = _json_dumps(source_refs)
            return self._item_to_dict(existing)

    def write_summary(self, owner: Optional[str]):
        from core.database import MemoryItem, MemorySummaryVersion

        with self._db() as db:
            items = (
                db.query(MemoryItem)
                .filter(MemoryItem.owner == owner, MemoryItem.status == "active")
                .order_by(MemoryItem.pinned.desc(), MemoryItem.priority.desc(), MemoryItem.timestamp.desc())
                .limit(80)
                .all()
            )
            highlights = [self._item_to_dict(item) for item in items[:20]]
            lines = [f"- {item['text']}" for item in highlights]
            summary = "\n".join(lines)
            db.query(MemorySummaryVersion).filter(
                MemorySummaryVersion.owner == owner,
                MemorySummaryVersion.active == True,
            ).update({"active": False})
            row = MemorySummaryVersion(
                id=str(uuid.uuid4()),
                owner=owner,
                summary=summary,
                highlights_json=_json_dumps(highlights),
                item_ids_json=_json_dumps([item["id"] for item in highlights]),
                source_refs_json=_json_dumps([]),
                active=True,
            )
            db.add(row)
            return {"id": row.id, "summary": summary, "highlights": highlights}

    def get_summary(self, owner: Optional[str]) -> dict:
        from core.database import MemorySummaryVersion

        with self._db() as db:
            row = (
                db.query(MemorySummaryVersion)
                .filter(MemorySummaryVersion.owner == owner, MemorySummaryVersion.active == True)
                .order_by(MemorySummaryVersion.created_at.desc())
                .first()
            )
            if row is None:
                return self.write_summary(owner)
            return {
                "id": row.id,
                "summary": row.summary,
                "highlights": _json_loads(row.highlights_json, []),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }

    def summary_history(self, owner: Optional[str]) -> list[dict]:
        from core.database import MemorySummaryVersion

        with self._db() as db:
            rows = (
                db.query(MemorySummaryVersion)
                .filter(MemorySummaryVersion.owner == owner)
                .order_by(MemorySummaryVersion.created_at.desc())
                .limit(50)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "summary": row.summary,
                    "active": bool(row.active),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    def restore_summary(self, owner: Optional[str], summary_id: str) -> bool:
        from core.database import MemoryItem, MemorySummaryVersion

        with self._db() as db:
            row = (
                db.query(MemorySummaryVersion)
                .filter(MemorySummaryVersion.owner == owner, MemorySummaryVersion.id == summary_id)
                .first()
            )
            if row is None:
                return False
            db.query(MemorySummaryVersion).filter(
                MemorySummaryVersion.owner == owner,
                MemorySummaryVersion.active == True,
            ).update({"active": False})
            row.active = True
            if row.created_at:
                cutoff = int(row.created_at.timestamp())
                db.query(MemoryItem).filter(
                    MemoryItem.owner == owner,
                    MemoryItem.kind == "synthesized",
                    MemoryItem.status == "active",
                    MemoryItem.timestamp > cutoff,
                ).update({"status": "archived"})
            return True

    def job_status(self, owner: Optional[str]) -> dict:
        from core.database import MemoryJob

        with self._db() as db:
            row = (
                db.query(MemoryJob)
                .filter(MemoryJob.owner == owner)
                .order_by(MemoryJob.created_at.desc())
                .first()
            )
            if row is None:
                return {"status": "idle"}
            return {
                "id": row.id,
                "status": row.status,
                "job_type": row.job_type,
                "progress": row.progress or 0,
                "error": row.error,
                "result": _json_loads(row.result_json, {}),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }

    def record_feedback(self, owner: Optional[str], target_type: str, target_id: str, feedback: str, note: str = ""):
        from core.database import MemoryFeedback, MemoryItem

        with self._db() as db:
            row = MemoryFeedback(
                id=str(uuid.uuid4()),
                owner=owner,
                target_type=target_type,
                target_id=target_id,
                feedback=feedback,
                note=note or None,
            )
            db.add(row)
            if target_type == "memory_item":
                item = db.query(MemoryItem).filter(MemoryItem.id == target_id, MemoryItem.owner == owner).first()
                if item:
                    if feedback == "prioritized":
                        item.priority = max(int(item.priority or 0), 10)
                    elif feedback == "deprioritized":
                        item.priority = min(int(item.priority or 0), -10)
                    elif feedback == "deleted":
                        item.status = "deleted"
            return {"ok": True, "id": row.id}

    def invalidate_source(self, *, owner: Optional[str], source_type: str, source_id: str) -> int:
        from core.database import MemoryItem, MemoryObservation

        changed = 0
        with self._db() as db:
            observations = (
                db.query(MemoryObservation)
                .filter(
                    MemoryObservation.owner == owner,
                    MemoryObservation.source_type == source_type,
                    MemoryObservation.source_id == source_id,
                    MemoryObservation.status == "active",
                )
                .all()
            )
            obs_ids = {obs.id for obs in observations}
            for obs in observations:
                obs.status = "archived"
                changed += 1
            if obs_ids:
                for item in db.query(MemoryItem).filter(MemoryItem.owner == owner, MemoryItem.kind == "synthesized").all():
                    refs = _json_loads(item.source_refs, [])
                    if any(ref.get("observation_id") in obs_ids for ref in refs if isinstance(ref, dict)):
                        item.status = "stale"
                        changed += 1
        return changed
