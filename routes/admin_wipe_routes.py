"""Backward-compat shim — canonical location is routes/admin_wipe/admin_wipe_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.admin_wipe_routes``, ``from routes.admin_wipe_routes
import X``, ``importlib.import_module("routes.admin_wipe_routes")``, and the
``import ... as admin_wipe_routes`` + ``monkeypatch.setattr(admin_wipe_routes,
"SessionLocal", ...)`` / ``"require_admin"`` pattern used by
test_admin_wipe_gallery.py all operate on the *same* object the application
actually uses. Keeps existing import paths working after slice 2h
(#4082/#4071).
"""

import sys as _sys

from core.middleware import require_admin
from core.database import (
    SessionLocal,
    Session as DbSession,
    ChatMessage as DbChatMessage,
    Memory,
    MemoryItem,
    MemoryObservation,
    MemorySummaryVersion,
    MemoryFeedback,
    MemoryJob,
    Note,
    ScheduledTask,
    TaskRun,
    Document,
    DocumentVersion,
    GalleryImage,
    GalleryAlbum,
    CalendarEvent,
    CalendarCal,
)
from src.constants import DATA_DIR, SKILLS_DIR, SKILLS_FILE, GALLERY_DIR, GALLERY_UPLOADS_DIR

logger = logging.getLogger(__name__)


def _wipe_memory_files():
    """Blank memory.json + drop the per-owner tidy-state sidecar so the
    next audit doesn't try to diff against gone memories."""
    for name in ("memory.json", "memory_tidy_state.json", "memory_v2_state.json"):
        p = os.path.join(DATA_DIR, name)
        if not os.path.exists(p):
            continue
        try:
            if name == "memory.json":
                with open(p, "w", encoding="utf-8") as f:
                    json.dump([], f)
            else:
                os.remove(p)
        except OSError as e:
            logger.warning(f"Could not reset {name}: {e}")


def _rmtree_quiet(path: str):
    """rmtree that doesn't crash if the path doesn't exist."""
    if os.path.isdir(path):
        try:
            shutil.rmtree(path)
        except OSError as e:
            logger.warning(f"Could not remove {path}: {e}")


def setup_admin_wipe_routes(session_manager):
    """The session_manager is passed in so we can also clear its
    in-memory cache when wiping chats — without it the DB is empty
    but the next /api/sessions returns stale entries."""
    router = APIRouter(prefix="/api/admin")

    @router.delete("/wipe/{kind}")
    def wipe(kind: str, request: Request):
        require_admin(request)
        kind = (kind or "").strip().lower()

        db = SessionLocal()
        try:
            if kind == "chats":
                count = db.query(DbSession).count()
                db.query(DbChatMessage).delete()
                db.query(DbSession).delete()
                db.commit()
                try:
                    session_manager.sessions.clear()
                except Exception:
                    pass
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "memory":
                count = db.query(Memory).count() + db.query(MemoryItem).count()
                db.query(Memory).delete()
                db.query(MemoryFeedback).delete()
                db.query(MemorySummaryVersion).delete()
                db.query(MemoryObservation).delete()
                db.query(MemoryJob).delete()
                db.query(MemoryItem).delete()
                db.commit()
                _wipe_memory_files()
                # Drop the vector store too so semantic search doesn't
                # return ghosts. Lazy import — chromadb may not be
                # initialised in every deployment.
                try:
                    from src.memory_vector import get_memory_vector_store
                    mv = get_memory_vector_store()
                    if mv and hasattr(mv, "clear"):
                        mv.clear()
                except Exception as e:
                    logger.info(f"Memory vector clear skipped: {e}")
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "skills":
                # Skills live as SKILL.md files under data/skills/. Drop
                # the entire directory; the SkillsManager re-creates the
                # tree on next write.
                skills_dir = SKILLS_DIR
                count = 0
                if os.path.isdir(skills_dir):
                    # Count SKILL.md files for the response — quick walk.
                    for _, _, files in os.walk(skills_dir):
                        count += sum(1 for f in files if f == "SKILL.md")
                    _rmtree_quiet(skills_dir)
                # Legacy fallback file
                legacy = SKILLS_FILE
                if os.path.exists(legacy):
                    try:
                        os.remove(legacy)
                    except OSError:
                        pass
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "notes":
                count = db.query(Note).count()
                db.query(Note).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "tasks":
                # TaskRun rows reference tasks via FK — clear them first.
                db.query(TaskRun).delete()
                count = db.query(ScheduledTask).count()
                db.query(ScheduledTask).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "documents":
                # DocumentVersion FKs Document — clear children first.
                db.query(DocumentVersion).delete()
                count = db.query(Document).count()
                db.query(Document).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "gallery":
                count = db.query(GalleryImage).count() + db.query(GalleryAlbum).count()
                db.query(GalleryImage).delete()
                db.query(GalleryAlbum).delete()
                db.commit()
                # Also drop the upload dir so disk doesn't keep orphans.
                _rmtree_quiet(GALLERY_DIR)
                _rmtree_quiet(GALLERY_UPLOADS_DIR)
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "calendar":
                # Events FK calendars — clear children first, then both.
                db.query(CalendarEvent).delete()
                count = db.query(CalendarCal).count()
                db.query(CalendarCal).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            raise HTTPException(400, f"Unknown wipe kind: {kind!r}")
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.exception(f"Wipe {kind} failed")
            raise HTTPException(500, f"Wipe {kind} failed: {e}")
        finally:
            db.close()

    return router
