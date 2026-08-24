"""The "Open document" button must survive a turn that rendered no tool thread.

When the agent's whole reply goes into a document (`code me a X` -> the code is
the document), the assistant bubble ends up with just a thinking box: no tool
thread, no text. The button that reopens the document was attached to
`.agent-thread-node`, so in that shape it was silently dropped — closing the
editor left no way back to the document from the chat.

Two defects in the live path:
  * the node lookup was document-wide, so it could attach the button to an
    EARLIER message's thread;
  * with no thread node anywhere, nothing was appended and no fallback existed.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAT_JS = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
RENDERER_JS = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


def _live_doc_button_block() -> str:
    i = CHAT_JS.index("_docBtnAddedThisTurn.has(json.doc_id)")
    return CHAT_JS[i - 200: i + 1400]


def test_live_button_lookup_is_scoped_to_the_current_message():
    block = _live_doc_button_block()
    assert "document.querySelectorAll('.agent-thread-node')" not in block, (
        "a document-wide lookup can attach the button to an earlier turn"
    )
    assert "roundHolder" in block


def test_live_button_falls_back_to_the_bubble_body():
    """The reported case: no tool thread, so the button had nowhere to go."""
    block = _live_doc_button_block()
    assert "querySelector('.body')" in block


def test_reload_renders_a_button_for_docs_the_thread_missed():
    """Same shape has to survive a page reload, sourced from tool_events."""
    assert "_missed" in RENDERER_JS
    i = RENDERER_JS.index("const _missed = []")
    block = RENDERER_JS[i: i + 900]
    assert "ev.doc_id" in block
    assert "agent-doc-open-btn" in block
    assert "_docBtnSeen" in block, "must not duplicate a button the thread already rendered"


def test_button_has_a_base_style_outside_the_thread_node():
    """The only rule was `.agent-thread-node .agent-doc-open-btn`, so a
    bubble-level button rendered as a bare link."""
    assert "#chat-history .agent-doc-open-btn {" in CSS
    rule = CSS[CSS.index("#chat-history .agent-doc-open-btn {"):]
    rule = rule[: rule.index("\n}")]
    for prop in ("display", "border", "background", "padding"):
        assert prop in rule, f"base chip style missing {prop}"


def test_href_uses_the_routed_anchor_form():
    """`#document-<id>` is what the global click delegate intercepts."""
    assert "#document-${json.doc_id}" in CHAT_JS
    assert "#document-${ev.doc_id}" in RENDERER_JS
    delegate = RENDERER_JS[RENDERER_JS.index("^#(session|document|note"):]
    assert "document" in delegate[:120]
