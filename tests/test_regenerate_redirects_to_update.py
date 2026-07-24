"""When a working document is open and a coder model answers an edit request by
re-emitting create_document with the whole artifact, the agent loop redirects
that call to update_document on the open doc — so it versions in place (diff
view) instead of spawning a duplicate / re-streaming with no diff, and the
anti-loop nudge stops the create->recreate loop.
"""

import re
from pathlib import Path

import src.agent_tools  # noqa: F401  (resolve circular init before importing loop)
from src.agent_loop import _split_create_document_block

AGENT_LOOP = Path("src/agent_loop.py").read_text(encoding="utf-8")


def test_split_line_form():
    assert _split_create_document_block("Apple.svelte\nsvelte\n<b>x</b>") == (
        "Apple.svelte", "svelte", "<b>x</b>",
    )


def test_split_line_form_no_language():
    assert _split_create_document_block("Apple.svelte\n<b>x</b>") == (
        "Apple.svelte", "", "<b>x</b>",
    )


def test_split_xml_form():
    got = _split_create_document_block(
        "<title>Apple.svelte</title><language>svelte</language><content><b>x</b></content>"
    )
    assert got == ("Apple.svelte", "svelte", "<b>x</b>")


def test_split_empty():
    assert _split_create_document_block("") == ("", "", "")


def test_agent_loop_redirects_create_to_update_for_open_doc():
    # The redirect block must exist and rewrite create_document -> update_document.
    assert 'redirecting create_document' in AGENT_LOOP
    assert 'tool_blocks[_bi] = ToolBlock("update_document", _cbody)' in AGENT_LOOP
    # And it must be gated on a working doc being open + a title match.
    assert "_active_document_editable and active_document is not None and tool_blocks" in AGENT_LOOP
    assert "_ct_l == _open_title or (_ct_stem and _ct_stem == _open_stem)" in AGENT_LOOP


def test_agent_loop_has_anti_loop_doc_write_nudge():
    assert "_doc_write_succeeded" in AGENT_LOOP
    assert "_doc_write_nudged" in AGENT_LOOP
    assert "Do NOT recreate or" in AGENT_LOOP


def test_chat_code_block_fallback_updates_open_doc_and_ends_turn():
    # When the model answers in prose + a big code block (no tool call), the
    # fallback must: fire at most once per turn, UPDATE the open working doc
    # (not create "Code (lang)" duplicates), and end the turn afterwards —
    # feeding "Document created" back provoked weak coder models into echoing
    # the code again, re-triggering the fallback in an endless create loop.
    assert "_auto_doc_from_chat = False" in AGENT_LOOP
    assert "and not _auto_doc_from_chat" in AGENT_LOOP
    assert 'ToolBlock("update_document", code_body)' in AGENT_LOOP
    assert "Auto-updating open doc" in AGENT_LOOP
    assert "turn complete after auto doc from chat code block" in AGENT_LOOP
    # The auto-update path must NOT emit doc streaming events (a streaming temp
    # doc suppresses the frontend's accept/reject diff).
    _upd_start = AGENT_LOOP.index('ToolBlock("update_document", code_body)')
    _upd_end = AGENT_LOOP.index("else:", _upd_start)
    assert "doc_stream_open" not in AGENT_LOOP[_upd_start:_upd_end]


def test_doc_write_persists_doc_id_and_title_on_tool_event():
    # The "Open document" button is rendered by the frontend from the persisted
    # doc tool_event (doc_id + doc_title), NOT a fragile text anchor — that is
    # what makes it survive a full page reload and reopen after the panel closes.
    assert 'tool_event["doc_id"] = result["doc_id"]' in AGENT_LOOP
    assert 'tool_event["doc_title"] = result.get("title", "")' in AGENT_LOOP
    # The old text-anchor approach must be gone (avoids a duplicate button).
    assert "[Open {_dtitle}](#document-" not in AGENT_LOOP
    assert "_doc_link_ids" not in AGENT_LOOP


def test_code_artifact_directive_does_not_leak_generating_title():
    # The tool-first directive must instruct a real filename-style title and
    # must NOT hand the model the "Generating <lang>" status label as the doc
    # title (which leaked verbatim into document titles).
    _d = AGENT_LOOP.index("CODE ARTIFACT — TOOL FIRST")
    _d_end = AGENT_LOOP.index("Do not emit prose before the tool call", _d)
    seg = AGENT_LOOP[_d:_d_end]
    assert "the word 'Generating'" in seg          # explicitly forbids it
    assert "_code_artifact['title']" not in seg     # no status label as title


def test_update_document_pre_stream_gated_when_working_doc_open():
    # The fenced update_document pre-stream must be skipped when the update
    # targets the open working doc, or the streaming temp doc suppresses the
    # accept/reject diff in handleDocUpdate (isEdit requires no streamingId).
    _ps = AGENT_LOOP.index('elif block.tool_type == "update_document":')
    _ps_end = AGENT_LOOP.index("# Execute each tool block", _ps)
    seg = AGENT_LOOP[_ps:_ps_end]
    assert "if _active_document_editable:" in seg
    assert seg.index("if _active_document_editable:") < seg.index("doc_stream_open")
