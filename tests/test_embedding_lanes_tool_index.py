import pytest

from src.embedding_lanes import (
    EmbeddingLane,
    LANE_CUSTOM,
    LANE_FASTEMBED,
)
from tests.helpers.embedding_lanes import (
    FakeChroma,
    FakeCollection,
    FakeEmbedder,
    FailingEmbedder,
    patch_chroma,
)


def test_tool_index_indexes_and_retrieves_from_available_lanes(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_custom_client", lambda: FakeEmbedder(768, "nomic", "http://embeddings/v1"))
    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: FakeEmbedder(384, "mini", "local://fastembed"))

    from src.tool_index import ToolIndex

    index = ToolIndex()
    index.index_builtin_tools()

    assert fake.collections["odysseus_tool_index_custom"].count() > 0
    assert fake.collections["odysseus_tool_index_fastembed"].count() > 0
    assert "bash" in index.retrieve("run a shell command", k=10)


def test_tool_index_builtin_indexing_fails_when_all_lanes_fail():
    custom_lane = EmbeddingLane(
        name=LANE_CUSTOM,
        client=FailingEmbedder(768, "nomic", "http://embeddings/v1"),
        collection=FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"}),
        collection_name="odysseus_tool_index_custom",
        model="nomic",
        url="http://embeddings/v1",
        dimension=768,
        fingerprint="custom",
    )
    fast_lane = EmbeddingLane(
        name=LANE_FASTEMBED,
        client=FailingEmbedder(384, "mini", "local://fastembed"),
        collection=FakeCollection("odysseus_tool_index_fastembed", metadata={"embedding_lane": "fastembed"}),
        collection_name="odysseus_tool_index_fastembed",
        model="mini",
        url="local://fastembed",
        dimension=384,
        fingerprint="fast",
    )

    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [custom_lane, fast_lane]
    index._healthy = True

    with pytest.raises(RuntimeError, match="all embedding lanes"):
        index.index_builtin_tools()
    assert not index.healthy


def test_tool_index_retrieval_continues_when_custom_lane_query_fails():
    custom_collection = FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"})
    fast_collection = FakeCollection("odysseus_tool_index_fastembed", metadata={"embedding_lane": "fastembed"})
    fast_collection.add(
        ids=["builtin_bash"],
        embeddings=[[0.0] * 384],
        documents=["Tool: bash\nRun shell commands"],
        metadatas=[{"tool_name": "bash", "tool_type": "builtin"}],
    )

    def fail_query(*_args, **_kwargs):
        raise RuntimeError("custom endpoint down")

    custom_collection.add(
        ids=["builtin_python"],
        embeddings=[[0.0] * 768],
        documents=["Tool: python\nRun Python"],
        metadatas=[{"tool_name": "python", "tool_type": "builtin"}],
    )
    custom_collection.query = fail_query

    custom_lane = EmbeddingLane(
        name=LANE_CUSTOM,
        client=FakeEmbedder(768, "nomic", "http://embeddings/v1"),
        collection=custom_collection,
        collection_name="odysseus_tool_index_custom",
        model="nomic",
        url="http://embeddings/v1",
        dimension=768,
        fingerprint="custom",
    )
    fast_lane = EmbeddingLane(
        name=LANE_FASTEMBED,
        client=FakeEmbedder(384, "mini", "local://fastembed"),
        collection=fast_collection,
        collection_name="odysseus_tool_index_fastembed",
        model="mini",
        url="local://fastembed",
        dimension=384,
        fingerprint="fast",
    )

    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [custom_lane, fast_lane]

    assert index.retrieve("run shell", k=5) == ["bash"]


def test_tool_index_merges_fallback_tool_results_before_limit():
    custom_collection = FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"})
    fast_collection = FakeCollection("odysseus_tool_index_fastembed", metadata={"embedding_lane": "fastembed"})
    custom_collection.add(
        ids=["builtin_one", "builtin_two"],
        embeddings=[[0.0] * 768, [0.0] * 768],
        documents=["Tool: one", "Tool: two"],
        metadatas=[
            {"tool_name": "one", "tool_type": "builtin"},
            {"tool_name": "two", "tool_type": "builtin"},
        ],
    )
    fast_collection.add(
        ids=["mcp_current"],
        embeddings=[[0.0] * 384],
        documents=["Tool: current MCP"],
        metadatas=[{"tool_name": "current_mcp", "tool_type": "mcp"}],
    )

    custom_collection.query = lambda **_kwargs: {
        "ids": [["builtin_one", "builtin_two"]],
        "metadatas": [[
            {"tool_name": "one", "tool_type": "builtin"},
            {"tool_name": "two", "tool_type": "builtin"},
        ]],
        "distances": [[0.20, 0.21]],
    }
    fast_collection.query = lambda **_kwargs: {
        "ids": [["mcp_current"]],
        "metadatas": [[{"tool_name": "current_mcp", "tool_type": "mcp"}]],
        "distances": [[0.05]],
    }

    custom_lane = EmbeddingLane(
        name=LANE_CUSTOM,
        client=FakeEmbedder(768, "nomic", "http://embeddings/v1"),
        collection=custom_collection,
        collection_name="odysseus_tool_index_custom",
        model="nomic",
        url="http://embeddings/v1",
        dimension=768,
        fingerprint="custom",
    )
    fast_lane = EmbeddingLane(
        name=LANE_FASTEMBED,
        client=FakeEmbedder(384, "mini", "local://fastembed"),
        collection=fast_collection,
        collection_name="odysseus_tool_index_fastembed",
        model="mini",
        url="local://fastembed",
        dimension=384,
        fingerprint="fast",
    )

    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [custom_lane, fast_lane]

    assert index.retrieve("current mcp", k=2) == ["current_mcp", "one"]


def test_shell_keyword_hints_force_include_bash():
    """bash/python previously relied SOLELY on embedding retrieval, so with the
    vector index down (ChromaDB unreachable) or a poorly-embedding query the
    model's schema list had no shell tool and it intermittently claimed to have
    no shell access. Deterministic keyword hints make availability stable."""
    import re
    from src.tool_index import ToolIndex

    def hint_tools(query):
        ql = query.lower()
        out = set()
        for keywords, tools in ToolIndex._KEYWORD_HINTS.items():
            if any(re.search(rf"\b{re.escape(kw)}\b", ql) for kw in keywords):
                out.update(tools)
        return out

    assert "bash" in hint_tools("can you check my gpu temps")
    assert "bash" in hint_tools("run nvidia-smi for me")
    assert "bash" in hint_tools("you have shell access, use it")
    assert "bash" in hint_tools("install ffmpeg")
    # Unrelated queries must not drag shell tools in.
    assert "bash" not in hint_tools("write me a poem")
    # Word-boundary guards: substrings must not fire short hints.
    assert "bash" not in hint_tools("the pipeline is broken")
    assert "bash" not in hint_tools("that legitimate digit issue")


def test_lexical_fallback_retrieves_correct_tools_without_embeddings():
    """When ChromaDB/embeddings are down, the fallback must still do REAL
    retrieval (BM25-style over tool descriptions), not just static keywords —
    otherwise any uncovered intent silently lost its tools."""
    from src.tool_index import lexical_tool_retrieval

    assert "bash" in lexical_tool_retrieval("run a shell command", k=8)
    assert "web_search" in lexical_tool_retrieval("search the web for news", k=8)
    assert "send_email" in lexical_tool_retrieval("send an email to bob", k=8)
    assert "manage_calendar" in lexical_tool_retrieval("what is on my calendar tomorrow", k=8)
    assert "edit_file" in lexical_tool_retrieval("edit the file config.json on disk", k=8)
    # Empty / no-token queries return nothing rather than noise.
    assert lexical_tool_retrieval("", k=8) == []
    assert lexical_tool_retrieval("!!!", k=8) == []


def test_session_sticky_tools_survive_and_evict():
    """A tool that executed in a session stays available on later turns; the
    store is bounded so it can't grow unboundedly."""
    from src.agent_loop import (
        _remember_sticky_tool, _sticky_tools_for, _SESSION_STICKY_TOOLS,
        _SESSION_STICKY_CAP,
    )

    _SESSION_STICKY_TOOLS.clear()
    _remember_sticky_tool("sess-a", "bash")
    _remember_sticky_tool("sess-a", "web_search")
    assert _sticky_tools_for("sess-a") == {"bash", "web_search"}
    assert _sticky_tools_for(None) == set()
    assert _sticky_tools_for("unknown") == set()
    # Bounded: oldest session evicted past the cap.
    for i in range(_SESSION_STICKY_CAP + 5):
        _remember_sticky_tool(f"sess-fill-{i}", "python")
    assert len(_SESSION_STICKY_TOOLS) <= _SESSION_STICKY_CAP
    assert _sticky_tools_for("sess-a") == set()  # evicted
    _SESSION_STICKY_TOOLS.clear()
