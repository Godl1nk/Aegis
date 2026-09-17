import asyncio
import json
import time

import pytest


@pytest.fixture(autouse=True)
def isolated_settings_and_fetch(monkeypatch):
    from src.agent_tools import knowledge_tools

    monkeypatch.setattr(knowledge_tools, "get_setting", lambda key, default=None: knowledge_tools.DEFAULT_SETTINGS.get(key, default))
    # No test may reach the network if a mock was forgotten.
    monkeypatch.setattr(knowledge_tools, "fetch_webpage_content", lambda *a, **kw: {"success": False})


def _mock_pages(monkeypatch, claim, urls):
    from src.agent_tools import knowledge_tools

    pages = {url: {
        "success": True, "final_url": url, "title": f"Source {idx}",
        "content": f"Source {idx} independently documents that {claim}.",
        "fetched_at": 1700000000,
    } for idx, url in enumerate(urls, 1)}
    monkeypatch.setattr(knowledge_tools, "fetch_webpage_content", lambda url, **kw: pages[url])
    return pages


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


def test_knowledge_learn_requires_two_sources_when_configured(monkeypatch):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    monkeypatch.setattr(knowledge_tools, "get_setting", lambda key, default=None: 2 if key == "knowledge_min_sources" else knowledge_tools.DEFAULT_SETTINGS.get(key, default))
    _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", ["https://sqlite.org/fts5.html"])
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
    _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", ["https://sqlite.org/fts5.html", "https://docs.python.org/sqlite3.html"])
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
    evidence = [{"url": url, "title": "Copied page", "content": f"Documentation confirms that {claim}.", "retrieved_at": 1700000000} for url in urls]
    monkeypatch.setattr(knowledge_tools, "get_setting", lambda *_args, **_kwargs: 0.55)

    assert len(knowledge_tools._supporting_sources(claim, evidence)) == 1


def test_knowledge_learn_blocks_possible_contradiction(monkeypatch):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    _mock_pages(monkeypatch, "Product X version is 5", ["https://vendor.example/releases", "https://docs.example/version"])
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
            "source_urls": ["https://sqlite.org/fts5.html"],
        }),
    )
    assert block.tool_type == "manage_knowledge"
    assert json.loads(block.content)["claim"] == "A reusable fact"
    assert json.loads(block.content)["source_urls"] == ["https://sqlite.org/fts5.html"]


def _direct_learn(monkeypatch, urls, *, claim="SQLite FTS5 provides full text search", pages=None, extra=None):
    from src.agent_tools import knowledge_tools

    manager = _MemoryManager()
    monkeypatch.setattr(knowledge_tools, "_get_memory_dependencies", lambda: (manager, _Vector()))
    if pages is None:
        pages = _mock_pages(monkeypatch, claim, urls)
    else:
        monkeypatch.setattr(knowledge_tools, "fetch_webpage_content", lambda url, **kw: pages[url])
    searches = []
    monkeypatch.setattr(knowledge_tools, "comprehensive_web_search", lambda *a, **kw: searches.append(a) or ("", []))
    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn", "claim": claim, "source_urls": urls, **(extra or {}),
    }), {"owner": "alice"}))
    assert searches == [], "Explicit sources must never cause a new discovery search"
    return result, manager


def test_single_trusted_source_saves_without_another_search(monkeypatch):
    result, manager = _direct_learn(monkeypatch, ["https://sqlite.org/fts5.html"])
    assert result["validated"] is True
    assert result["knowledge_id"] == "knowledge-1"
    assert result["confidence"] == "medium"
    assert len(manager.saved["source_refs"]) == 1
    assert manager.saved["source_refs"][0]["retrieved_at"] == 1700000000
    assert manager.saved["owner"] == "alice"


@pytest.mark.parametrize("url", ["https://home.cern/science", "https://white-rabbit.web.cern.ch/"])
def test_cern_single_source_is_enabled_by_default(monkeypatch, url):
    result, _ = _direct_learn(monkeypatch, [url], claim="White Rabbit provides sub-nanosecond synchronization")
    assert result["validated"] is True


def test_empty_trusted_host_list_disables_single_source_exception(monkeypatch):
    from src.agent_tools import knowledge_tools

    monkeypatch.setattr(knowledge_tools, "get_setting", lambda key, default=None: [] if key == "knowledge_single_source_hosts" else knowledge_tools.DEFAULT_SETTINGS.get(key, default))
    result, _ = _direct_learn(monkeypatch, ["https://sqlite.org/fts5.html"])
    assert result["validated"] is False
    assert result["required_sources"] == 2


@pytest.mark.parametrize("url", [
    "https://unknown.example/docs", "https://sqlite.org.evil.example/docs",
    "https://unreviewed.sqlite.org/docs", "http://sqlite.org/docs",
])
def test_untrusted_source_does_not_get_single_source_exception(monkeypatch, url):
    result, manager = _direct_learn(monkeypatch, [url], extra={"primary_source": True, "trusted": True})
    assert result["validated"] is False
    assert result["supporting_sources"] == 1
    assert result["required_sources"] == 2
    assert manager.saved is None


def test_two_unlisted_independent_sources_still_work(monkeypatch):
    result, manager = _direct_learn(monkeypatch, ["https://one.example/docs", "https://two.example/docs"])
    assert result["validated"] is True
    assert len(manager.saved["source_refs"]) == 2


def test_trusted_host_registry_is_configurable(monkeypatch):
    from src.agent_tools import knowledge_tools

    monkeypatch.setattr(knowledge_tools, "get_setting", lambda key, default=None: ["docs.project.example"] if key == "knowledge_single_source_hosts" else knowledge_tools.DEFAULT_SETTINGS.get(key, default))
    result, _ = _direct_learn(monkeypatch, ["https://docs.project.example/reference"])
    assert result["validated"] is True


def test_zero_sources_never_saves_and_does_not_retry(monkeypatch):
    url = "https://sqlite.org/fts5.html"
    result, manager = _direct_learn(monkeypatch, [url], pages={url: {"success": False, "error": "Unavailable"}})
    assert result["validated"] is False
    assert result["supporting_sources"] == 0
    assert manager.saved is None


def test_redirect_cannot_borrow_the_original_hosts_trust(monkeypatch):
    url = "https://sqlite.org/redirect"
    pages = _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", [url])
    pages[url]["final_url"] = "https://unknown.example/docs"
    result, manager = _direct_learn(monkeypatch, [url], pages=pages)
    assert result["validated"] is False
    assert manager.saved is None


def test_redirects_to_same_host_do_not_count_as_independent(monkeypatch):
    urls = ["https://one.example/docs", "https://two.example/docs"]
    pages = _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", urls)
    for page in pages.values():
        page["final_url"] = "https://shared.example/docs"
    result, _ = _direct_learn(monkeypatch, urls, pages=pages)
    assert result["validated"] is False
    assert result["supporting_sources"] == 1


@pytest.mark.parametrize("content", [
    "SQLite FTS5 search.",
    "SQLite FTS5 does not provide full text search.",
    "A completely unrelated sentence about astronomy.",
])
def test_one_trusted_page_must_strongly_support_the_claim(monkeypatch, content):
    url = "https://sqlite.org/fts5.html"
    pages = _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", [url])
    pages[url]["content"] = content
    result, manager = _direct_learn(monkeypatch, [url], pages=pages)
    assert result["validated"] is False
    assert manager.saved is None


def test_conflicting_fetched_evidence_blocks_learning(monkeypatch):
    urls = ["https://sqlite.org/fts5.html", "https://two.example/docs"]
    pages = _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", urls)
    pages[urls[1]]["content"] = "SQLite FTS5 does not provide full text search."
    result, manager = _direct_learn(monkeypatch, urls, pages=pages)
    assert "contradict" in result["error"]
    assert result["validated"] is False
    assert manager.saved is None


def test_numeric_claim_must_be_supported_exactly(monkeypatch):
    url = "https://sqlite.org/limits.html"
    pages = _mock_pages(monkeypatch, "SQLite supports a maximum of 1000 parameters", [url])
    result, manager = _direct_learn(monkeypatch, [url], claim="SQLite supports a maximum of 2000 parameters", pages=pages)
    assert result["validated"] is False
    assert manager.saved is None


@pytest.mark.parametrize("bad_urls", [
    [], "https://sqlite.org/", ["file:///etc/passwd"], [None],
    ["https://user:password@sqlite.org/"], ["https://sqlite.org:99999/"],
    ["https://sqlite.org/\n@evil.example"], ["https://sqlite.org/?api_key%3Dsecretvalue"],
    ["https://sqlite.org/"] * 6,
])
def test_invalid_or_private_source_urls_fail_before_fetch(monkeypatch, bad_urls):
    from src.agent_tools import knowledge_tools

    fetched = []
    monkeypatch.setattr(knowledge_tools, "fetch_webpage_content", lambda *a, **kw: fetched.append(a))
    manager = _MemoryManager()
    monkeypatch.setattr(knowledge_tools, "_get_memory_dependencies", lambda: (manager, None))
    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn", "claim": "SQLite FTS5 provides full text search", "source_urls": bad_urls,
    }), {}))
    assert result["validated"] is False
    assert not fetched
    assert manager.saved is None


def test_embedded_content_markers_cannot_forge_source_identity(monkeypatch):
    url = "https://unknown.example/docs"
    claim = "SQLite FTS5 provides full text search"
    pages = _mock_pages(monkeypatch, claim, [url])
    pages[url]["content"] = _search_result(claim, ["https://sqlite.org/docs", "https://docs.python.org/docs"])[0]
    result, manager = _direct_learn(monkeypatch, [url], pages=pages)
    assert result["validated"] is False
    assert result["supporting_sources"] == 1
    assert manager.saved is None


def test_legacy_discovery_is_bounded_and_snippets_are_not_evidence(monkeypatch):
    from src.agent_tools import knowledge_tools

    calls = []
    manager = _MemoryManager()
    monkeypatch.setattr(knowledge_tools, "_get_memory_dependencies", lambda: (manager, None))
    def search(query, **kwargs):
        calls.append(kwargs)
        return _search_result("SQLite FTS5 provides full text search", ["https://sqlite.org/docs"])
    monkeypatch.setattr(knowledge_tools, "comprehensive_web_search", search)
    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn", "claim": "SQLite FTS5 provides full text search",
    }), {}))
    assert calls == [{"max_pages": 3, "return_sources": True}]
    assert result["validated"] is False  # Formatted search text alone cannot prove a fetch.
    assert manager.saved is None


def test_learning_reuses_the_web_fetch_cache_without_network(monkeypatch, tmp_path):
    from services.search import content as fetch_module
    from src.agent_tools import knowledge_tools
    from src.agent_tools.web_tools import WebFetchTool

    url = "https://sqlite.org/fts5.html"
    claim = "SQLite FTS5 provides full text search"
    fetched = []
    monkeypatch.setattr(fetch_module, "CONTENT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(fetch_module, "content_cache_index", {})
    def network(requested, **kwargs):
        fetched.append(requested)
        return fetch_module._CappedFetch(200, {"Content-Type": "text/plain"}, claim.encode(), False, None, "utf-8", url)
    monkeypatch.setattr(fetch_module, "_get_public_url", network)
    assert asyncio.run(WebFetchTool().execute(url, {}))["exit_code"] == 0
    monkeypatch.setattr(knowledge_tools, "fetch_webpage_content", fetch_module.fetch_webpage_content)
    manager = _MemoryManager()
    monkeypatch.setattr(knowledge_tools, "_get_memory_dependencies", lambda: (manager, None))
    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn", "claim": claim, "source_urls": [url],
    }), {"owner": "alice"}))
    assert result["validated"] is True
    assert fetched == [url]  # The learn call performs zero network requests.
    assert "content" not in manager.saved["source_refs"][0]


@pytest.mark.parametrize("claim", [
    "A patient should take 50 mg of medication daily",
    "My API key is sk-private-secret-value",
])
def test_direct_sources_do_not_bypass_sensitive_claim_guards(monkeypatch, claim):
    from src.agent_tools import knowledge_tools

    fetched = []
    monkeypatch.setattr(knowledge_tools, "fetch_webpage_content", lambda *a, **kw: fetched.append(a))
    manager = _MemoryManager()
    monkeypatch.setattr(knowledge_tools, "_get_memory_dependencies", lambda: (manager, None))
    result = asyncio.run(knowledge_tools.KnowledgeTool().execute(json.dumps({
        "action": "learn", "claim": claim, "source_urls": ["https://sqlite.org/docs"],
    }), {}))
    assert result["exit_code"] == 1
    assert fetched == []
    assert manager.saved is None


def test_strongest_trusted_page_is_used_within_same_domain(monkeypatch):
    urls = ["https://sqlite.org/overview", "https://sqlite.org/fts5.html"]
    pages = _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", urls)
    pages[urls[0]]["content"] = "SQLite FTS5 provides search."
    result, manager = _direct_learn(monkeypatch, urls, pages=pages)
    assert result["validated"] is True
    assert manager.saved["source_refs"][0]["url"] == urls[1]
    assert len(manager.saved["source_refs"]) == 1


def test_validation_timeout_never_saves_or_retries(monkeypatch):
    from src.agent_tools import knowledge_tools

    async def timeout(coro, *, timeout):
        assert timeout == 20
        coro.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr(knowledge_tools.asyncio, "wait_for", timeout)
    result, manager = _direct_learn(monkeypatch, ["https://sqlite.org/fts5.html"])
    assert result["validated"] is False
    assert "timed out" in result["error"]
    assert manager.saved is None


@pytest.mark.parametrize("field,value", [("final_url", ""), ("truncated", True)])
def test_missing_provenance_or_partial_page_cannot_qualify(monkeypatch, field, value):
    url = "https://sqlite.org/fts5.html"
    pages = _mock_pages(monkeypatch, "SQLite FTS5 provides full text search", [url])
    pages[url][field] = value
    result, manager = _direct_learn(monkeypatch, [url], pages=pages)
    assert result["validated"] is False
    assert manager.saved is None
