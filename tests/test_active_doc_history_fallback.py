"""Mid-conversation document editing must survive losing the 'active' pointer.

The working doc is resolved from: frontend active_doc_id -> session email/doc
fallbacks -> in-memory pointer. All of those can miss (panel closed after a
reload; doc rebound to another session by the cross-session accept), and when
they did, edit_document silently vanished from the toolset — the model
concluded "edit_document isn't a real tool" and started treating its own
artifact as a file on disk. The last-resort lookup recovers the doc_id from
the session's persisted tool_events (the same data the Open-document button
uses)."""

from pathlib import Path

SRC = Path("routes/chat_routes.py").read_text(encoding="utf-8")


def test_history_fallback_exists_and_orders_after_memory_fallback():
    mem_idx = SRC.index("found by in-memory active id")
    hist_idx = SRC.index("recovered from session tool history")
    none_idx = SRC.index("no active doc for session")
    assert mem_idx < hist_idx < none_idx


def test_history_fallback_reads_tool_events_and_respects_deletion():
    idx = SRC.index("recovered from session tool history")
    block = SRC[idx - 2000:idx]
    # Walks the session history newest-first for a doc_id from tool_events.
    assert '_ev.get("doc_id")' in block
    assert 'reversed(getattr(_sess_obj, "history", [])' in block
    # A deleted doc (is_active=False) must NOT be resurrected.
    assert "DBDocument.is_active == True" in block
    # Owner check still applies.
    assert "_owner_session_filter(_hist_q, ctx.user)" in block
