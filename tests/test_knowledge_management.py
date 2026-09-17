"""Brain knowledge management: real SQLite queries and owner-scoped HTTP APIs."""
import json
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, MemoryItem
from routes.knowledge_routes import setup_knowledge_routes
from src.memory import MemoryManager
from src.memory_v2 import MemoryV2Store


@pytest.fixture
def manager(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    store = MemoryV2Store(str(tmp_path))

    @contextmanager
    def db():
        with factory.begin() as session:
            yield session

    monkeypatch.setattr(store, "_db", db)
    monkeypatch.setattr(store, "migrate_from_json_once", lambda: None)
    memory = MemoryManager(str(tmp_path))
    memory._v2 = store
    yield memory
    engine.dispose()


def seed(manager, id="claim", owner="alice", **extra):
    values = dict(id=id, owner=owner, text="SQLite FTS5 provides full text search", kind="knowledge",
                  status="active", timestamp=100, source="web_validated", confidence="medium",
                  source_refs='[{"url":"https://sqlite.org/fts5.html","title":"FTS5"}]',
                  metadata_json=json.dumps({"expires_at": 4_000_000_000, "validated_at": 100, "query": "SQLite FTS5"}))
    values.update(extra)
    with manager._v2._db() as db:
        db.add(MemoryItem(**values))


def test_single_source_learning_is_recalled_from_real_store_only_for_its_owner(manager, monkeypatch):
    from src.agent_tools import knowledge_tools

    claim = "SQLite FTS5 provides full text search"
    url = "https://sqlite.org/fts5.html"
    monkeypatch.setattr(knowledge_tools, "get_setting", lambda key, default=None: knowledge_tools.DEFAULT_SETTINGS.get(key, default))
    monkeypatch.setattr(knowledge_tools, "_get_memory_dependencies", lambda: (manager, None))
    monkeypatch.setattr(knowledge_tools, "fetch_webpage_content", lambda *a, **kw: {
        "success": True, "final_url": url, "content": claim, "title": "FTS5",
    })
    tool = knowledge_tools.KnowledgeTool()
    saved = asyncio.run(tool.execute(json.dumps({
        "action": "learn", "claim": claim, "source_urls": [url],
    }), {"owner": "alice"}))
    assert saved["validated"] is True
    query = json.dumps({"action": "search", "query": "SQLite FTS5"})
    recalled = asyncio.run(tool.execute(query, {"owner": "alice"}))
    assert recalled["knowledge"][0]["id"] == saved["knowledge_id"]
    assert recalled["knowledge"][0]["text"] == claim
    assert len(recalled["knowledge"][0]["source_refs"]) == 1
    assert "none found" in asyncio.run(tool.execute(query, {"owner": "bob"}))["results"]


def client_for(manager, monkeypatch, *, privileges=None, vector=None):
    import routes.knowledge_routes as routes
    import src.tool_security as security
    monkeypatch.setattr(routes, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(security, "blocked_tools_for_owner", lambda owner: set())
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, get_privileges=lambda user: privileges or {})

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(setup_knowledge_routes(manager, vector))
    return TestClient(app)


def test_pagination_filters_before_limit_and_never_materializes_library(manager, monkeypatch):
    for n in range(60):
        seed(manager, f"expired-{n:03}", timestamp=200, metadata_json='{"expires_at":1}')
    for n in range(30):
        seed(manager, f"fresh-{n:03}")
    seed(manager, "foreign", owner="bob")
    seed(manager, "personal", kind="saved")
    seed(manager, "deleted", status="deleted")
    serialize = Mock(wraps=manager._v2._item_to_dict)
    monkeypatch.setattr(manager._v2, "_item_to_dict", serialize)
    monkeypatch.setattr(manager, "load", Mock(side_effect=AssertionError("full library load")))
    result = manager.query_knowledge("alice", freshness="fresh", offset=25, limit=25)
    assert (result["total"], result["fresh"], result["expired"], result["matched"]) == (90, 30, 60, 30)
    assert [row["id"] for row in result["knowledge"]] == [f"fresh-{n:03}" for n in range(25, 30)]
    assert serialize.call_count == 5


def test_literal_search_malformed_metadata_and_null_owner(manager):
    seed(manager, "literal", text="A literal 100%_ match")
    seed(manager, "normal", text="A literal 100abc match")
    for n, metadata in enumerate(['not-json', '[]', '{}', '{"expires_at":"4000000000bad"}']):
        seed(manager, f"bad-{n}", metadata_json=metadata)
    seed(manager, "unclaimed", owner=None)
    assert [row["id"] for row in manager.query_knowledge("alice", query="  100%_  ")["knowledge"]] == ["literal"]
    result = manager.query_knowledge("alice", freshness="expired")
    assert result["matched"] == 4
    assert all(row["freshness"] == "expired" for row in result["knowledge"])
    assert [row["id"] for row in manager.query_knowledge(None)["knowledge"]] == ["unclaimed"]


def test_personal_memory_query_excludes_knowledge_without_changing_default(manager):
    seed(manager)
    seed(manager, "personal", kind="saved")
    assert len(manager.load("alice")) == 2
    assert [row["id"] for row in manager.load("alice", exclude_knowledge=True)] == ["personal"]
    assert [row["id"] for row in manager.load(exclude_knowledge=True)] == ["personal"]
    with manager._v2._db() as db:
        db.query(MemoryItem).filter(MemoryItem.id == "personal").update({MemoryItem.kind: None})
    assert [row["id"] for row in manager.load("alice", exclude_knowledge=True)] == ["personal"]


def test_api_auth_filters_validation_and_scoped_delete(manager, monkeypatch):
    seed(manager)
    seed(manager, "foreign", owner="bob")
    seed(manager, "personal", kind="saved")
    vector = SimpleNamespace(healthy=True, remove=Mock(side_effect=RuntimeError("offline")))
    client = client_for(manager, monkeypatch, vector=vector)
    headers = {"x-test-user": "alice"}
    assert client.get("/api/knowledge").status_code == 401
    assert client.get("/api/knowledge", headers=headers).json()["total"] == 1
    for params in ["limit=101", "offset=-1", "freshness=invalid", "q=" + "x" * 501]:
        assert client.get("/api/knowledge?" + params, headers=headers).status_code == 422
    for id in ["foreign", "personal", "missing"]:
        assert client.delete("/api/knowledge/" + id, headers=headers).status_code == 404
    assert client.delete("/api/knowledge/claim", headers=headers).status_code == 200
    assert client.delete("/api/knowledge/claim", headers=headers).status_code == 404
    assert manager.query_knowledge("alice")["total"] == 0
    assert manager.load_by_ids(["foreign", "personal"])
    vector.remove.assert_called_once_with("claim")


def test_revalidation_reuses_validator_and_preserves_failed_claim(manager, monkeypatch):
    from src.agent_tools.knowledge_tools import KnowledgeTool
    seed(manager)
    client = client_for(manager, monkeypatch)
    headers = {"x-test-user": "alice"}
    learn = AsyncMock(return_value={"validated": False, "error": "Insufficient independent evidence"})
    monkeypatch.setattr(KnowledgeTool, "_learn", learn)
    before = manager.load_by_ids(["claim"], owner="alice")
    response = client.post("/api/knowledge/claim/revalidate", headers=headers)
    assert response.status_code == 422
    assert manager.load_by_ids(["claim"], owner="alice") == before
    assert learn.call_args.args[:2] == ({"claim": before[0]["text"], "query": "SQLite FTS5"}, "alice")
    learn.return_value = {"validated": True, "knowledge_id": "claim"}
    assert client.post("/api/knowledge/claim/revalidate", headers=headers).status_code == 200
    assert client.post("/api/knowledge/missing/revalidate", headers=headers).status_code == 404


@pytest.mark.parametrize("privilege", ["can_manage_memory", "can_use_research"])
def test_revalidation_respects_privileges(manager, monkeypatch, privilege):
    seed(manager)
    client = client_for(manager, monkeypatch, privileges={privilege: False})
    assert client.post("/api/knowledge/claim/revalidate", headers={"x-test-user": "alice"}).status_code == 403


def test_revalidation_respects_disabled_tools(manager, monkeypatch):
    import routes.knowledge_routes as routes
    seed(manager)
    client = client_for(manager, monkeypatch)
    monkeypatch.setattr(routes, "get_setting", lambda key, default=None: ["web_search"] if key == "disabled_tools" else default)
    assert client.post("/api/knowledge/claim/revalidate", headers={"x-test-user": "alice"}).status_code == 403


def test_general_memory_editor_cannot_rewrite_validated_claim(manager):
    from fastapi import HTTPException, Request
    from routes.memory.memory_routes import setup_memory_routes
    seed(manager)
    router = setup_memory_routes(manager, SimpleNamespace())
    update = next(route.endpoint for route in router.routes if route.path == "/api/memory/{memory_id}" and "PUT" in route.methods)
    request = Request({"type": "http", "headers": []})
    request.state.current_user = "alice"
    with pytest.raises(HTTPException) as error:
        update(request, "claim", "Unverified replacement", None)
    assert error.value.status_code == 409
    assert manager.load_by_ids(["claim"])[0]["text"] == "SQLite FTS5 provides full text search"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["alice", None])
async def test_personal_memory_tidy_preserves_knowledge(manager, monkeypatch, owner):
    from services.memory.memory_extractor import audit_memories
    import src.llm_core as llm
    seed(manager)
    seed(manager, "personal", kind="saved", text="User prefers short answers")
    before = manager.load_by_ids(["claim"])
    generate = AsyncMock(return_value='[{"id":"personal","text":"User prefers concise answers"}]')
    monkeypatch.setattr(llm, "llm_call_async", generate)
    result = await audit_memories(manager, None, "http://test", "test", owner=owner)
    assert result["before"] == result["after"] == 1
    assert "SQLite" not in generate.call_args.args[2][1]["content"]
    assert manager.load_by_ids(["claim"]) == before


def test_legacy_json_management_remains_scoped_and_paged(tmp_path):
    memory = MemoryManager(str(tmp_path))
    assert memory._v2 is None
    alice = memory.upsert_knowledge(owner="alice", text="SQLite FTS5 provides full text search",
                                    source_refs=[], query="SQLite FTS5", confidence="medium",
                                    validated_at=100, expires_at=4_000_000_000)
    memory.upsert_knowledge(owner="bob", text="A different owner's stored knowledge claim",
                            source_refs=[], query="Other topic", confidence="medium",
                            validated_at=100, expires_at=4_000_000_000)
    assert memory.query_knowledge("alice", query="  fts5  ", freshness="fresh", limit=1)["matched"] == 1
    assert memory.query_knowledge("alice", offset=1)["knowledge"] == []
    assert memory.query_knowledge(None)["total"] == 0
    assert not memory.delete_knowledge(alice["id"], owner="bob")
    assert memory.delete_knowledge(alice["id"], owner="alice")
    assert memory.query_knowledge("alice")["total"] == 0
    assert memory.query_knowledge("bob")["total"] == 1
