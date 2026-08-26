"""Regression checks for tool approval and terminal-card rendering."""

from pathlib import Path


CHAT_JS = (Path(__file__).resolve().parent.parent / "static/js/chat.js").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = CHAT_JS.index(name)
    next_export = CHAT_JS.find("\n  export ", start + len(name))
    return CHAT_JS[start: next_export if next_export > 0 else len(CHAT_JS)]


def test_resumed_stream_renders_approval_request():
    body = _function_source("export async function resumeStream")
    assert "json.type === 'approval_request'" in body
    assert "_renderApprovalCard(json" in body


def test_background_stream_preserves_and_renders_approval_request():
    assert "bgApproval.pendingApproval = json" in CHAT_JS
    body = _function_source("export async function checkBackgroundStream")
    assert "entry.pendingApproval" in body
    assert "_renderApprovalCard(curPoll.pendingApproval" in body


def test_tool_output_cannot_overwrite_a_different_tool_node():
    assert "node.dataset.tool = String(json.tool || '')" in CHAT_JS
    assert "currentToolBubble.dataset.tool !== String(json.tool || '')" in CHAT_JS


def test_image_choice_drops_stale_generic_tool_pointer():
    marker = "json.type === 'image_model_choice'"
    branch = CHAT_JS[CHAT_JS.index(marker):]
    branch = branch[:branch.index("json.type === 'plan_update'")]
    assert "currentToolBubble = null" in branch


def test_mid_task_detector_uses_the_tool_timeline_not_message_children():
    assert "!!(lastToolThread && lastToolThread.isConnected)" in CHAT_JS
    assert "_stallHost.appendChild(_stall)" in CHAT_JS
    assert "holder.querySelector('.agent-thread-node')" not in CHAT_JS


def test_detached_reader_does_not_paint_duplicate_tool_nodes_after_return():
    assert "if (_isBg || _backgroundStreams.has(streamSessionId)) continue;" in CHAT_JS
    assert "bgToolOutput.pendingApproval = null" in CHAT_JS
