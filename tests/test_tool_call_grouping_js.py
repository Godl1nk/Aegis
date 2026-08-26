"""Regression guards for compact repeated tool-call rendering."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RENDERER_JS = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
CHAT_JS = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


def test_saved_and_live_tool_calls_use_same_compactor():
    assert "export function compactAgentToolThread(threadWrap)" in RENDERER_JS
    assert "compactAgentToolThread(threadWrap);" in RENDERER_JS
    assert "chatRenderer.compactAgentToolThread(currentToolBubble.closest('.agent-thread'))" in CHAT_JS


def test_tool_identity_is_retained_for_saved_calls():
    assert "node.dataset.tool = String(ev.tool || '');" in RENDERER_JS


def test_groups_keep_individual_calls_expandable():
    assert "calls.forEach((call) => items.appendChild(call));" in RENDERER_JS
    assert "agent-thread-group.open > .agent-thread-group-items" in STYLE_CSS
    assert "header.closest('.agent-thread-node, .agent-thread-group')" in CHAT_JS


def test_group_summary_reports_count_and_mixed_results():
    assert "`\\u00d7${groupedCalls.length}`" in RENDERER_JS
    assert "`${done} done \\u00b7 ${failed} failed`" in RENDERER_JS
