import asyncio
from types import SimpleNamespace


def test_memory_v2_activates_for_absolute_data_dir(monkeypatch, tmp_path):
    import src.constants as constants
    from src.memory import MemoryManager

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)

    manager = MemoryManager(str(tmp_path))

    assert manager._v2 is not None


def test_reference_chat_history_off_filters_dream_memories(tmp_path):
    from src.chat_processor import ChatProcessor
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    saved = manager.add_entry("User prefers concise replies", category="preference", owner="alice")
    saved["pinned"] = True
    dream = manager.add_entry("User lives in Singapore", source="dream", category="identity", owner="alice")
    manager.save([saved, dream])

    processor = ChatProcessor(manager, personal_docs_manager=SimpleNamespace())
    preface, _, _ = processor.build_context_preface(
        "What do you know about me?",
        session=SimpleNamespace(),
        use_memory=True,
        owner="alice",
        reference_saved_memories=True,
        reference_chat_history=False,
    )

    text = "\n".join(msg["content"] for msg in preface)
    assert "concise replies" in text
    assert "Singapore" not in text


def test_reference_saved_memories_off_keeps_chat_history_memory(tmp_path):
    from src.chat_processor import ChatProcessor
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    saved = manager.add_entry("User prefers concise replies", category="preference", owner="alice")
    saved["pinned"] = True
    dream = manager.add_entry("User lives in Singapore", source="dream", category="identity", owner="alice")
    manager.save([saved, dream])

    processor = ChatProcessor(manager, personal_docs_manager=SimpleNamespace())
    preface, _, _ = processor.build_context_preface(
        "What do you know about me?",
        session=SimpleNamespace(),
        use_memory=True,
        owner="alice",
        reference_saved_memories=False,
        reference_chat_history=True,
    )

    text = "\n".join(msg["content"] for msg in preface)
    assert "concise replies" not in text
    assert "Singapore" in text


def test_memory_reference_toggles_can_disable_both_sources(tmp_path):
    from src.chat_processor import ChatProcessor
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    saved = manager.add_entry("User prefers concise replies", category="preference", owner="alice")
    saved["pinned"] = True
    dream = manager.add_entry("User lives in Singapore", source="dream", category="identity", owner="alice")
    manager.save([saved, dream])

    processor = ChatProcessor(manager, personal_docs_manager=SimpleNamespace())
    preface, _, _ = processor.build_context_preface(
        "What do you know about me?",
        session=SimpleNamespace(),
        use_memory=True,
        owner="alice",
        reference_saved_memories=False,
        reference_chat_history=False,
    )

    text = "\n".join(msg["content"] for msg in preface)
    assert "concise replies" not in text
    assert "Singapore" not in text
    assert processor._last_used_memories == []


def test_inline_memory_command_is_owner_scoped(monkeypatch, tmp_path):
    from core.models import Session
    from src.chat_handler import ChatHandler
    from src.memory import MemoryManager
    # conftest.py replaces src.database with a stub module that lacks this
    # attribute, so patch the module object directly with raising=False —
    # the string form ("src.database.update_session_last_accessed") can't
    # resolve through the stub either.
    import src.database as src_database

    class FakeSessionManager:
        def save_sessions(self):
            pass

    monkeypatch.setattr(
        src_database, "update_session_last_accessed", lambda session_id: None, raising=False
    )

    manager = MemoryManager(str(tmp_path))
    existing = manager.add_entry("Bob prefers verbose replies", owner="bob")
    manager.save([existing])
    handler = ChatHandler(
        FakeSessionManager(),
        manager,
        chat_processor=None,
        research_handler=None,
        preset_manager=None,
        upload_handler=None,
    )
    session = Session(id="s1", name="Test", endpoint_url="", model="", owner="alice")

    response = asyncio.run(
        handler.handle_memory_command(session, "remember: Alice likes brief replies", owner="alice")
    )

    memories = manager.load_all()
    assert response == "Saved to memory: Alice likes brief replies"
    assert any(m["text"] == "Alice likes brief replies" and m.get("owner") == "alice" for m in memories)
    assert any(m["text"] == "Bob prefers verbose replies" and m.get("owner") == "bob" for m in memories)


def test_delete_last_v2_memory_removes_it(monkeypatch, tmp_path):
    import src.constants as constants
    from src.memory import MemoryManager

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)

    manager = MemoryManager(str(tmp_path))
    entry = manager.add_entry("User likes precise answers", owner="alice")
    manager.save([entry])

    assert manager.delete_entry(entry["id"], owner="alice") is True
    assert manager.load(owner="alice") == []


def test_inflection_variants_still_retrieve_memories(tmp_path):
    """Keyword recall must survive simple English inflection: 'where do I
    live' ↔ 'User lives in Singapore', 'my projects' ↔ '…side project'.
    Without the light stemmer these were exact-token misses, so dream
    memories silently failed to surface whenever the vector index was down."""
    from types import SimpleNamespace
    from src.chat_processor import ChatProcessor
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    city = manager.add_entry("User lives in Singapore", source="dream", category="identity", owner="alice")
    proj = manager.add_entry("User is building a browser OS side project", source="dream", category="project", owner="alice")
    manager.save([city, proj])

    processor = ChatProcessor(manager, personal_docs_manager=SimpleNamespace())

    def preface_text(q):
        preface, _, _ = processor.build_context_preface(
            q, session=SimpleNamespace(), use_memory=True, owner="alice",
            reference_saved_memories=True, reference_chat_history=True,
        )
        return " ".join(m["content"] for m in preface)

    assert "Singapore" in preface_text("where do i live")
    assert "browser OS" in preface_text("tell me about my projects")
    assert "browser OS" in preface_text("what am i building")
    # Casual greeting must not drag memories in.
    joined = preface_text("hi")
    assert "Singapore" not in joined and "browser OS" not in joined
