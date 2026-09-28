"""Issue #1160 — route-level regression for clearing the active-document pointer.

Exercises the REAL ``PATCH /api/document/{id}`` (session_id=""),
``DELETE /api/document/{id}``, and restore handlers under actual owner/session
routing, including recovery into the Library after a soft delete.

Calls the route handler callables DIRECTLY (extracted from the router) instead of
through Starlette's TestClient. The TestClient path spun up a middleware app +
threadpool that could hang in some environments; calling the async handler with a
minimal fake request keeps the same real coverage (handler + DB + owner routing)
while completing reliably everywhere.
"""

import tempfile
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from unittest.mock import MagicMock

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
import routes.document_routes as droutes
from core.database import Document
from core.database import Session as DbSession
from routes.document_helpers import DocumentPatch
from src.agent_tools.document_tools import set_active_document, get_active_document

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
droutes.SessionLocal = _TS  # route handlers resolve SessionLocal at call time


def _req():
    return SimpleNamespace(state=SimpleNamespace(current_user="tester"))


def _endpoint(method, path):
    router = droutes.setup_document_routes(MagicMock(), None)
    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _make_doc():
    sid = "s-" + uuid.uuid4().hex[:8]
    db = _TS()
    try:
        db.add(DbSession(id=sid, owner="tester", name="s", model="m", endpoint_url="http://x"))
        doc = Document(
            id=str(uuid.uuid4()), session_id=sid, title="t",
            language="markdown", current_content="hi", version_count=1,
            is_active=True, owner="tester",
        )
        db.add(doc)
        db.commit()
        return doc.id
    finally:
        db.close()


async def test_patch_unlink_clears_active_document():
    patch_document = _endpoint("PATCH", "/api/document/{doc_id}")
    doc_id = _make_doc()
    set_active_document(doc_id)
    await patch_document(_req(), doc_id, DocumentPatch(session_id=""))
    assert get_active_document() is None


async def test_delete_clears_active_document():
    delete_document = _endpoint("DELETE", "/api/document/{doc_id}")
    doc_id = _make_doc()
    set_active_document(doc_id)
    await delete_document(_req(), doc_id)
    assert get_active_document() is None


async def test_unlinking_a_different_doc_leaves_pointer():
    patch_document = _endpoint("PATCH", "/api/document/{doc_id}")
    active_id = _make_doc()
    other_id = _make_doc()
    set_active_document(active_id)
    await patch_document(_req(), other_id, DocumentPatch(session_id=""))
    assert get_active_document() == active_id


async def test_restore_soft_deleted_document_preserves_content_and_owner():
    delete_document = _endpoint("DELETE", "/api/document/{doc_id}")
    restore_document = _endpoint("POST", "/api/document/{doc_id}/restore")
    documents_library = _endpoint("GET", "/api/documents/library")
    doc_id = _make_doc()
    await delete_document(_req(), doc_id)
    library_args = dict(search=None, language=None, sort="recent", offset=0, limit=20, archived=False)
    assert doc_id not in {d["id"] for d in (await documents_library(_req(), **library_args))["documents"]}
    restored = await restore_document(_req(), doc_id)
    assert restored["is_active"] is True
    assert restored["current_content"] == "hi"
    assert doc_id in {d["id"] for d in (await documents_library(_req(), **library_args))["documents"]}
    db = _TS()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).one()
        assert doc.owner == "tester"
        assert doc.version_count == 1
    finally:
        db.close()


async def test_restore_rejects_other_owner():
    restore_document = _endpoint("POST", "/api/document/{doc_id}/restore")
    doc_id = _make_doc()
    with pytest.raises(HTTPException) as exc:
        await restore_document(SimpleNamespace(state=SimpleNamespace(current_user="other")), doc_id)
    assert exc.value.status_code == 404
