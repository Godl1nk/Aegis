import time
from types import SimpleNamespace


def _memory_texts(processor):
    return [row["text"] for row in processor._last_used_memories]


def test_chat_processor_leaves_skill_prompting_to_agent_loop():
    from src.chat_processor import ChatProcessor

    class Skills:
        calls = 0

        def index_for(self, **_kwargs):
            self.calls += 1
            return [{"name": "duplicate", "description": "must not appear"}]

    skills = Skills()
    processor = ChatProcessor(
        memory_manager=SimpleNamespace(load=lambda owner=None: []),
        personal_docs_manager=SimpleNamespace(),
        skills_manager=skills,
    )

    preface, _, _ = processor.build_context_preface(
        "perform a domain task",
        session=SimpleNamespace(),
        use_memory=False,
        use_rag=False,
        agent_mode=True,
        use_skills=True,
    )

    assert skills.calls == 0
    assert not any("available skills" in str(msg.get("content", "")).lower() for msg in preface)


def test_skill_index_formatter_respects_item_and_character_budgets():
    from src.agent_loop import _format_skill_index

    skills = [
        {
            "name": f"skill-{idx:03d}",
            "description": "x" * 500,
            "category": "general",
            "status": "published",
        }
        for idx in range(100)
    ]

    block = _format_skill_index(skills, max_items=7, max_chars=1800)

    assert len(block) <= 1800
    assert sum(f"`skill-{idx:03d}`" in block for idx in range(100)) <= 7
    assert "omitted" in block


def test_pinned_memory_context_is_bounded_and_overflow_remains_retrievable(tmp_path, monkeypatch):
    from src.chat_processor import ChatProcessor
    from src.memory import MemoryManager

    monkeypatch.setattr(ChatProcessor, "PINNED_MEMORY_MAX_ITEMS", 2)
    monkeypatch.setattr(ChatProcessor, "PINNED_MEMORY_MAX_CHARS", 500)

    manager = MemoryManager(str(tmp_path))
    memories = []
    for idx in range(4):
        entry = manager.add_entry(
            f"User project codename is PROJECT-{idx}",
            category="project",
            owner="alice",
        )
        entry["pinned"] = True
        entry["priority"] = 100 - idx
        memories.append(entry)
    manager.save(memories)

    processor = ChatProcessor(manager, personal_docs_manager=SimpleNamespace())
    processor.build_context_preface(
        "hello",
        session=SimpleNamespace(),
        use_memory=True,
        use_rag=False,
        owner="alice",
    )
    assert _memory_texts(processor) == [
        "User project codename is PROJECT-0",
        "User project codename is PROJECT-1",
    ]

    processor.build_context_preface(
        "What is PROJECT-3?",
        session=SimpleNamespace(),
        use_memory=True,
        use_rag=False,
        owner="alice",
    )
    assert "User project codename is PROJECT-3" in _memory_texts(processor)


def test_memory_v2_owner_load_does_not_call_global_load_all(tmp_path, monkeypatch):
    import src.constants as constants
    from src.memory import MemoryManager

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    manager = MemoryManager(str(tmp_path))
    alice = manager.add_entry("Alice memory", owner="alice")
    bob = manager.add_entry("Bob memory", owner="bob")
    manager.save([alice, bob])

    def fail_global_load():
        raise AssertionError("owner-scoped load must query SQLite directly")

    monkeypatch.setattr(manager._v2, "load_all", fail_global_load)

    assert [row["text"] for row in manager._v2.load(owner="alice")] == ["Alice memory"]


def test_skill_parse_cache_reuses_unchanged_markdown(tmp_path, monkeypatch):
    from services.memory.skill_format import Skill
    from services.memory.skills import SkillsManager

    skill_dir = tmp_path / "skills" / "general" / "cached-skill"
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\nname: cached-skill\ndescription: cached\ncategory: general\n"
        "status: published\nowner: alice\n---\n\n## Procedure\n1. Test\n",
        encoding="utf-8",
    )

    calls = 0
    original = Skill.from_markdown

    def counted(text, path=None):
        nonlocal calls
        calls += 1
        return original(text, path=path)

    monkeypatch.setattr(Skill, "from_markdown", counted)
    manager = SkillsManager(str(tmp_path))

    assert manager.load(owner="alice")[0]["name"] == "cached-skill"
    assert manager.load(owner="alice")[0]["name"] == "cached-skill"
    assert calls == 1

    manager.update_skill("cached-skill", {"description": "updated"}, owner="alice")
    assert manager.load(owner="alice")[0]["description"] == "updated"
    assert calls == 2


def test_skill_catalog_cache_avoids_repeated_filesystem_walks(tmp_path, monkeypatch):
    from services.memory.skills import SkillsManager

    skill_dir = tmp_path / "skills" / "general" / "cached-catalog"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: cached-catalog\ndescription: cached\ncategory: general\n"
        "status: published\nowner: alice\n---\n\n## Procedure\n1. Test\n",
        encoding="utf-8",
    )

    calls = 0
    original = SkillsManager._iter_skill_files

    def counted(self):
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(SkillsManager, "_iter_skill_files", counted)

    assert SkillsManager(str(tmp_path)).load(owner="alice")
    assert SkillsManager(str(tmp_path)).load(owner="alice")
    assert calls == 1


def test_skill_usage_update_does_not_force_catalog_rescan(tmp_path, monkeypatch):
    from services.memory.skills import SkillsManager

    skill_dir = tmp_path / "skills" / "general" / "used-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: used-skill\ndescription: used\ncategory: general\n"
        "status: published\nowner: alice\n---\n\n## Procedure\n1. Test\n",
        encoding="utf-8",
    )

    calls = 0
    original = SkillsManager._iter_skill_files

    def counted(self):
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(SkillsManager, "_iter_skill_files", counted)
    manager = SkillsManager(str(tmp_path))
    assert manager.load(owner="alice")[0]["uses"] == 0
    manager.record_use("used-skill", owner="alice")
    assert manager.load(owner="alice")[0]["uses"] == 1
    assert calls == 1


def test_web_knowledge_policy_is_not_globally_prompted():
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_index import ALWAYS_AVAILABLE

    assert "manage_knowledge" not in ALWAYS_AVAILABLE
    assert "manage_knowledge" not in TOOL_SECTIONS


def test_vector_candidate_loading_avoids_full_owner_memory_load():
    from src.chat_processor import ChatProcessor

    class MemoryManager:
        def __init__(self):
            self.used = []

        def load(self, owner=None):
            raise AssertionError("full owner memory load must not run on indexed path")

        def load_pinned(self, owner=None, limit=12, kinds=None):
            return []

        def load_by_ids(self, ids, owner=None):
            return [{
                "id": "knowledge-1",
                "text": "SQLite FTS5 provides full text search",
                "timestamp": int(time.time()),
                "category": "knowledge",
                "kind": "knowledge",
                "owner": owner,
                "status": "active",
                "validated_at": int(time.time()),
                "expires_at": int(time.time()) + 3600,
                "source_refs": [{"url": "https://sqlite.org/fts5.html"}],
            }]

        def increment_uses(self, ids):
            self.used.extend(ids)

    class Vector:
        healthy = True

        def search(self, query, k=8, owner=None):
            return [{"memory_id": "knowledge-1", "score": 0.95}]

    manager = MemoryManager()
    processor = ChatProcessor(
        manager,
        personal_docs_manager=SimpleNamespace(),
        memory_vector=Vector(),
    )
    preface, _, _ = processor.build_context_preface(
        "What does SQLite FTS5 provide?",
        session=SimpleNamespace(),
        use_memory=True,
        use_rag=False,
        owner="alice",
    )

    assert any("SQLite FTS5" in str(row.get("content", "")) for row in preface)
    assert manager.used == ["knowledge-1"]


def test_expired_web_knowledge_is_not_injected():
    from src.chat_processor import ChatProcessor

    expired = {
        "id": "expired",
        "text": "Old version is current",
        "timestamp": int(time.time()),
        "category": "knowledge",
        "kind": "knowledge",
        "expires_at": int(time.time()) - 1,
    }
    processor = ChatProcessor(
        memory_manager=SimpleNamespace(load=lambda owner=None: [expired]),
        personal_docs_manager=SimpleNamespace(),
    )

    preface, _, _ = processor.build_context_preface(
        "What is the current version?",
        session=SimpleNamespace(),
        use_memory=True,
        use_rag=False,
        owner="alice",
    )

    assert not any("Old version is current" in str(row.get("content", "")) for row in preface)


def test_memory_v2_knowledge_is_owner_scoped_and_survives_legacy_save(tmp_path, monkeypatch):
    import src.constants as constants
    from src.memory import MemoryManager

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    manager = MemoryManager(str(tmp_path))
    now = int(time.time())
    created_ids = []
    try:
        alice = manager.upsert_knowledge(
            owner="alice",
            text="SQLite FTS5 provides full text search",
            source_refs=[{"url": "https://sqlite.org/fts5.html"}],
            query="SQLite FTS5",
            confidence="medium",
            validated_at=now,
            expires_at=now + 86400,
            kind="knowledge",
        )
        created_ids.append((alice["id"], "alice"))
        bob = manager.upsert_knowledge(
            owner="bob",
            text="Bob-only knowledge",
            source_refs=[{"url": "https://example.org/bob"}],
            query="Bob fact",
            confidence="medium",
            validated_at=now,
            expires_at=now + 86400,
            kind="knowledge",
        )
        created_ids.append((bob["id"], "bob"))

        regular = manager.add_entry("Alice preference", owner="alice")
        manager.save([regular])

        alice_rows = [
            row for row in manager.load_knowledge(owner="alice")
            if row["id"] == alice["id"]
        ]
        assert [row["id"] for row in alice_rows] == [alice["id"]]
        assert manager.load_by_ids([alice["id"]], owner="bob") == []
    finally:
        if manager._v2 is not None:
            for memory_id, owner in created_ids:
                manager._v2.delete_item(memory_id, owner=owner)
