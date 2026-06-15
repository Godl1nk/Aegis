"""Memory routes must owner-scope caller-supplied session ids.

SessionManager.get_session returns any session by id (no owner scoping). The
/api/memory extract, audit, import, and by-session handlers accept a
caller-supplied session id, so without an ownership gate a user could target
another tenant's session and leak their chat history, session-scoped LLM
credentials, or session title.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.memory_routes as mr
from src.request_models import MemoryAddRequest


def _route(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(path)


def _router(monkeypatch, caller):
    monkeypatch.setattr(mr, "get_current_user", lambda request: caller, raising=False)
    monkeypatch.setattr(mr, "require_user", lambda request: caller, raising=False)
    sm = MagicMock()
    sm.sessions = {}
    sm.get_session = lambda sid: SimpleNamespace(
        owner="alice", name="Secret project", endpoint_url="http://x", model="m",
        headers={"Authorization": "Bearer victim-secret"},
        get_context_messages=lambda: [],
    )
    mem = MagicMock()
    mem.load = lambda owner=None: []
    return mr.setup_memory_routes(mem, sm)


def _request(user):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def _memory_delete_router(monkeypatch, caller, mem):
    monkeypatch.setattr(mr, "get_current_user", lambda request: caller, raising=False)
    monkeypatch.setattr(mr, "require_privilege", lambda request, key: caller, raising=False)
    return mr.setup_memory_routes(mem, MagicMock())


def test_extract_rejects_other_users_session(monkeypatch):
    router = _router(monkeypatch, caller="bob")
    extract = _route(router, "/api/memory/extract", "POST")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(extract(request=None, session="alice-sess"))
    assert exc.value.status_code == 404


def test_by_session_rejects_other_users_session(monkeypatch):
    router = _router(monkeypatch, caller="bob")
    gbs = _route(router, "/api/memory/by-session/{session_id}", "GET")
    with pytest.raises(HTTPException) as exc:
        gbs(request=None, session_id="alice-sess")
    assert exc.value.status_code == 404


def test_owner_can_access_own_session(monkeypatch):
    router = _router(monkeypatch, caller="alice")
    gbs = _route(router, "/api/memory/by-session/{session_id}", "GET")
    out = gbs(request=None, session_id="alice-sess")
    assert out["session_name"] == "Secret project"


def test_add_memory_rejects_other_users_session(monkeypatch):
    memory_manager = MagicMock()
    session_manager = MagicMock()
    memory_vector = MagicMock(healthy=True)
    router = mr.setup_memory_routes(
        memory_manager=memory_manager,
        session_manager=session_manager,
        memory_vector=memory_vector,
    )
    add_memory = _route(router, "/api/memory/add", "POST")

    memory_manager.load.return_value = []
    memory_manager.find_duplicates.return_value = False
    session_manager.get_session.return_value = SimpleNamespace(owner="bob", name="Bob session")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            add_memory(
                request=_request("alice"),
                memory_data=MemoryAddRequest(
                    text="Alice note",
                    category="fact",
                    source="user",
                    session_id="bob-session",
                ),
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"
    session_manager.get_session.assert_called_once_with("bob-session")
    memory_manager.add_entry.assert_not_called()
    memory_manager.save.assert_not_called()
    memory_vector.add.assert_not_called()


def test_timeline_does_not_expose_other_users_session_name():
    memory_manager = MagicMock()
    session_manager = MagicMock()
    session_manager.sessions = {"bob-session": object()}
    session_manager.get_session.return_value = SimpleNamespace(owner="bob", name="Bob roadmap")
    memory_manager.load.return_value = [
        {
            "id": "m1",
            "text": "Alice note",
            "owner": "alice",
            "session_id": "bob-session",
            "timestamp": 1,
        }
    ]
    router = mr.setup_memory_routes(memory_manager, session_manager)
    timeline = _route(router, "/api/memory/timeline", "GET")

    out = timeline(request=_request("alice"))

    assert out["timeline"][0]["session_name"] == "Unknown"


def test_delete_memory_is_idempotent_for_stale_rows(monkeypatch):
    mem = MagicMock()
    mem.load.return_value = []
    mem.delete_entry.return_value = False
    router = _memory_delete_router(monkeypatch, caller="alice", mem=mem)

    delete = _route(router, "/api/memory/{memory_id}", "DELETE")
    out = delete(request=None, memory_id="stale-id")

    assert out["ok"] is True
    assert out["already_deleted"] is True
    mem.delete_entry.assert_not_called()


def test_delete_memory_reports_storage_failure_for_visible_rows(monkeypatch):
    mem = MagicMock()
    mem.load.return_value = [{"id": "m1", "text": "keep", "owner": "alice"}]
    mem.delete_entry.return_value = False
    router = _memory_delete_router(monkeypatch, caller="alice", mem=mem)

    delete = _route(router, "/api/memory/{memory_id}", "DELETE")
    with pytest.raises(HTTPException) as exc:
        delete(request=None, memory_id="m1")

    assert exc.value.status_code == 500
    mem.delete_entry.assert_called_once_with("m1", owner="alice")


def test_delete_entry_removes_json_fallback_row_when_v2_missing(tmp_path):
    from src.memory import MemoryManager

    class EmptyV2:
        def load_all(self):
            return []

        def delete_item(self, memory_id, owner=None):
            return False

    manager = MemoryManager(str(tmp_path))
    manager._v2 = EmptyV2()
    (tmp_path / "memory.json").write_text(
        '[{"id":"m1","text":"stale","owner":"alice"}]',
        encoding="utf-8",
    )

    assert manager.delete_entry("m1", owner="alice") is True
    assert manager._load_json_entries() == []


def test_delete_entry_removes_json_row_after_v2_load_failure(tmp_path):
    from src.memory import MemoryManager

    class BrokenV2:
        def load_all(self):
            raise RuntimeError("db unavailable")

        def delete_item(self, memory_id, owner=None):
            raise RuntimeError("db unavailable")

    manager = MemoryManager(str(tmp_path))
    manager._v2 = BrokenV2()
    (tmp_path / "memory.json").write_text(
        '[{"id":"m1","text":"fallback","owner":"alice"}]',
        encoding="utf-8",
    )

    assert manager.delete_entry("m1", owner="alice") is True
    assert manager._load_json_entries() == []


def test_delete_entry_retries_v2_delete_by_id_for_owned_row(tmp_path):
    from src.memory import MemoryManager

    class DriftedV2:
        def __init__(self):
            self.calls = []

        def load_all(self):
            return [{"id": "m1", "text": "owned", "owner": "alice"}]

        def delete_item(self, memory_id, owner=None):
            self.calls.append((memory_id, owner))
            return owner is None

    manager = MemoryManager(str(tmp_path))
    v2 = DriftedV2()
    manager._v2 = v2

    assert manager.delete_entry("m1", owner="alice") is True
    assert v2.calls == [("m1", "alice"), ("m1", None)]
