import asyncio
import json
import time


def _search_result(claim: str, urls: list[str]):
    sources = [
        {"url": url, "title": f"Source {idx}"}
        for idx, url in enumerate(urls, 1)
    ]
    blocks = []
    for idx, url in enumerate(urls, 1):
        blocks.append(
            f"[CONTENT {idx}] From: {url}\n"
            f"Title: Source {idx}\n"
            "------------------------------\n"
            f"Source {idx} independently documents that {claim}."
        )
    return "\n".join(blocks), sources


class _MemoryManager:
    def __init__(self):
        self.saved = None
        self.deleted = None

    def upsert_knowledge(self, **kwargs):
        self.saved = kwargs
        return {
            "id": "knowledge-1",
            "text": kwargs["text"],
            "kind": "knowledge",
            "source_refs": kwargs["source_refs"],
            "expires_at": kwargs["expires_at"],
        }

    def load_knowledge(self, owner=None, include_expired=False, limit=50):
        return []

    def delete_entry(self, memory_id, owner=None):
        self.deleted = (memory_id, owner)
        return True


class _Vector:
    healthy = True

    def __init__(self):
        self.added = None

    def add(self, memory_id, text, owner=None, kind=None):
        self.added = (memory_id, text, owner, kind)

    def search(self, *args, **kwargs):
        return []

    def remove(self, _memory_id):
        return None


def test_knowledge_learn_requires_two_independent_supporting_sources(monkeypatch):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    vector = _Vector()
    monkeypatch.setattr(
        knowledge_tools,
        "_get_memory_dependencies",
        lambda: (manager, vector),
    )
    monkeypatch.setattr(
        knowledge_tools,
        "comprehensive_web_search",
        lambda *args, **kwargs: _search_result(
            "SQLite FTS5 provides full text search",
            ["https://sqlite.org/fts5.html"],
        ),
    )

    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn",
        "claim": "SQLite FTS5 provides full text search",
        "query": "SQLite FTS5 full text search",
    }), {"owner": "alice"}))

    assert "error" in result
    assert "independent" in result["error"].lower()
    assert manager.saved is None


def test_knowledge_learn_persists_provenance_expiry_and_vector(monkeypatch):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    vector = _Vector()
    monkeypatch.setattr(
        knowledge_tools,
        "_get_memory_dependencies",
        lambda: (manager, vector),
    )
    monkeypatch.setattr(
        knowledge_tools,
        "comprehensive_web_search",
        lambda *args, **kwargs: _search_result(
            "SQLite FTS5 provides full text search",
            ["https://sqlite.org/fts5.html", "https://docs.python.org/sqlite3.html"],
        ),
    )

    before = int(time.time())
    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn",
        "claim": "SQLite FTS5 provides full text search",
        "query": "SQLite FTS5 full text search",
    }), {"owner": "alice"}))

    assert result["validated"] is True
    assert manager.saved["owner"] == "alice"
    assert manager.saved["kind"] == "knowledge"
    assert len(manager.saved["source_refs"]) == 2
    assert manager.saved["validated_at"] >= before
    assert manager.saved["expires_at"] > manager.saved["validated_at"]
    assert vector.added == (
        "knowledge-1",
        "SQLite FTS5 provides full text search",
        "alice",
        "knowledge",
    )


def test_knowledge_learn_refuses_high_stakes_claims(monkeypatch):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    monkeypatch.setattr(
        knowledge_tools,
        "_get_memory_dependencies",
        lambda: (manager, _Vector()),
    )

    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn",
        "claim": "A patient should take 50 mg of this medication daily",
        "query": "medication dosage",
    }), {"owner": "alice"}))

    assert "high-stakes" in result["error"].lower()
    assert manager.saved is None


def test_knowledge_learn_never_searches_private_or_secret_claims(monkeypatch):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    searched = False

    def fail_search(*args, **kwargs):
        nonlocal searched
        searched = True
        raise AssertionError("private claim must not leave the process")

    monkeypatch.setattr(
        knowledge_tools,
        "_get_memory_dependencies",
        lambda: (manager, _Vector()),
    )
    monkeypatch.setattr(knowledge_tools, "comprehensive_web_search", fail_search)

    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn",
        "claim": "My API key is sk-private-secret-value",
        "query": "check my API key sk-private-secret-value",
    }), {"owner": "alice"}))

    assert "private" in result["error"].lower()
    assert searched is False
    assert manager.saved is None


def test_private_data_detection_covers_phone_postal_and_payment_card():
    from src.agent_tools.knowledge_tools import _contains_private_data

    assert _contains_private_data("My phone is +65 9123 4567")
    assert _contains_private_data("My postal code is 238801")
    assert _contains_private_data("Use card 4111 1111 1111 1111")
    assert not _contains_private_data("SQLite version 3.49.1 was released")


def test_support_score_rejects_opposite_negation():
    from src.agent_tools.knowledge_tools import _support_score

    claim = "SQLite FTS5 provides full text search"
    assert _support_score(claim, "SQLite FTS5 does not provide full text search.") == 0.0
    assert _support_score(claim, "SQLite FTS5 provides full text search.") == 1.0


def test_supporting_sources_do_not_count_copied_content_twice(monkeypatch):
    from src.agent_tools import knowledge_tools

    claim = "SQLite FTS5 provides full text search"
    urls = ["https://one.example/docs", "https://two.example/docs"]
    sources = [{"url": url, "title": "Copied page"} for url in urls]
    context = "\n".join(
        f"[CONTENT {idx}] From: {url}\nTitle: Copied page\n"
        f"------------------------------\nDocumentation confirms that {claim}."
        for idx, url in enumerate(urls, 1)
    )
    monkeypatch.setattr(knowledge_tools, "get_setting", lambda *_args, **_kwargs: 0.55)

    assert len(knowledge_tools._supporting_sources(claim, context, sources)) == 1


def test_knowledge_learn_blocks_possible_contradiction(monkeypatch):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    manager.load_by_ids = lambda ids, owner=None: [{
        "id": "old-knowledge",
        "text": "Product X version is 4",
        "kind": "knowledge",
        "owner": owner,
    }]
    vector = _Vector()
    vector.search = lambda *args, **kwargs: [{
        "memory_id": "old-knowledge",
        "score": 0.93,
    }]
    monkeypatch.setattr(
        knowledge_tools,
        "_get_memory_dependencies",
        lambda: (manager, vector),
    )
    monkeypatch.setattr(
        knowledge_tools,
        "comprehensive_web_search",
        lambda *args, **kwargs: _search_result(
            "Product X version is 5",
            ["https://vendor.example/releases", "https://docs.example/version"],
        ),
    )

    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn",
        "claim": "Product X version is 5",
        "query": "Product X current version",
    }), {"owner": "alice"}))

    assert "conflict" in result["error"].lower()
    assert result["conflict_with"] == "old-knowledge"
    assert manager.saved is None


def test_manage_knowledge_native_schema_maps_to_json_block():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    names = {
        row["function"]["name"]
        for row in FUNCTION_TOOL_SCHEMAS
    }
    assert "manage_knowledge" in names

    block = function_call_to_tool_block(
        "manage_knowledge",
        json.dumps({
            "action": "learn",
            "claim": "A reusable fact",
            "query": "fact sources",
        }),
    )
    assert block.tool_type == "manage_knowledge"
    assert json.loads(block.content)["claim"] == "A reusable fact"
