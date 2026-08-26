from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHAT_JS = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
CHAT_RENDERER_JS = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
DOCUMENT_JS = (ROOT / "static/js/document.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")
AGENT_LOOP = (ROOT / "src/agent_loop.py").read_text(encoding="utf-8")
CHAT_ROUTES = (ROOT / "routes/chat_routes.py").read_text(encoding="utf-8")


def test_chat_routes_code_stream_lifecycle_events_to_document_editor():
    for event, handler in (
        ("doc_stream_phase", "streamDocPhase"),
        ("doc_stream_cancel", "streamDocCancel"),
    ):
        assert f"json.type === '{event}'" in CHAT_JS
        assert f"documentModule?.{handler}" in CHAT_JS


def test_server_route_forwards_code_stream_lifecycle_events():
    for event in ("doc_stream_phase", "doc_stream_cancel"):
        assert f'"{event}"' in CHAT_ROUTES
    assert '"doc_stream_prepare"' not in CHAT_ROUTES


def test_document_module_exports_code_stream_lifecycle():
    for name in ("streamDocPhase", "streamDocCancel"):
        assert f"export function {name}" in DOCUMENT_JS
        assert name in DOCUMENT_JS[DOCUMENT_JS.index("const documentModule ="):]


def test_document_stream_opens_editor_panel_for_live_view():
    # Reversed by user request: a streaming doc MOUNTS the editor pane so the
    # code is watched being written live, instead of landing invisibly behind
    # a closed panel with only a raw fence tag flashing in chat.
    open_start = DOCUMENT_JS.index("export function streamDocOpen")
    open_end = DOCUMENT_JS.index("/** Simulate streaming effect", open_start)
    open_body = DOCUMENT_JS[open_start:open_end]

    assert "_ensureDocPaneMounted();" in open_body
    assert "streamDocPrepare" not in DOCUMENT_JS
    assert "doc_stream_prepare" not in CHAT_JS


def test_real_document_stream_exposes_clickable_writing_indicator():
    assert 'id="doc-indicator-btn"' in INDEX_HTML
    assert "doc-writing" in STYLE_CSS
    assert "_setDocWritingIndicator(true, 'generating');" in DOCUMENT_JS
    assert "_setDocWritingIndicator(false, 'complete');" in DOCUMENT_JS
    assert "classList.contains('doc-writing')" in APP_JS
    assert "ensurePaneMounted" in APP_JS


def test_hybrid_parameter_envelope_does_not_open_or_render_as_document():
    assert "roundText.match(/```create_document" not in CHAT_JS
    assert "HYBRID_DOC_ENVELOPE_RE" in CHAT_RENDERER_JS


def test_cancel_preserves_partial_generated_code():
    start = DOCUMENT_JS.index("export function streamDocCancel")
    end = DOCUMENT_JS.index("export function streamDocOpen", start)
    body = DOCUMENT_JS[start:end]

    assert "_setStreamPhase('error')" in body
    assert "docs.delete" not in body


def test_standalone_code_request_uses_direct_chat_without_document_tools():
    assert "_code_chat_direct = bool(" in AGENT_LOOP
    assert "STANDALONE CODE ARTIFACT" in AGENT_LOOP
    assert '"create_document", "update_document", "edit_document"' in AGENT_LOOP
    assert "Output the complete implementation directly now" in AGENT_LOOP
    assert 'messages[0]["content"] = _code_chat_directive' in AGENT_LOOP
    assert "if _code_chat_direct:\n            all_tool_schemas = []" in AGENT_LOOP
    assert "not _code_chat_direct\n            and not _code_project_enabled\n            and not has_doc_tool" in AGENT_LOOP
    assert "if _code_chat_direct and tool_blocks:" in AGENT_LOOP
    assert "_document_block_as_chat_code" in AGENT_LOOP
    assert '"type": "doc_stream_prepare"' not in AGENT_LOOP
    assert "window._livePreview && window._livePreview.open();" not in CHAT_JS


def test_existing_session_document_does_not_hide_create_document_tool():
    start = AGENT_LOOP.index("# If this turn targets the open document, keep editing tools available")
    end = AGENT_LOOP.index("# The skill index injected", start)
    tool_gate = AGENT_LOOP[start:end]

    # A non-email working doc is always editable (Codex model), so edit tools are
    # offered whenever one is open — but create_document must NOT be hidden: the
    # user may still want a genuinely new artifact while a doc is open.
    assert "or _active_document_editable):" in tool_gate
    assert '_relevant_tools.update({"edit_document", "update_document", "suggest_document"})' in tool_gate
    assert "\n    if (\n        _relevant_tools is not None\n        and _code_artifact" in tool_gate
    assert '_relevant_tools.add("create_document")' in tool_gate
    assert '_relevant_tools.discard("create_document")' not in tool_gate


def test_code_artifact_streams_document_live_on_round_1():
    """A code-artifact request tells the model to emit create_document as its
    FIRST output. Live doc streaming was gated to round_num>1, so the huge
    round-1 code generated silently for minutes with no doc pane — looking
    'stuck'. The gate must also open on round 1 when _code_stream_enabled."""
    assert "round_num > 1 or _ody_doc_stream_create_mode or _code_stream_enabled" in AGENT_LOOP


def test_plain_language_fence_streams_live_on_code_artifact_turns():
    """Weak models ignore the create_document directive and emit a plain
    ```html / ```python fence — the artifact then generated invisibly for
    minutes ('stuck') and only became a doc via the post-round fallback. On a
    code-artifact turn with no open doc, plain fences must live-stream into
    the doc pane, and the fallback must not re-emit the same content."""
    assert "'```html\\n', '```python\\n', '```javascript\\n'," in AGENT_LOOP
    assert "_code_stream_enabled and active_document is None" in AGENT_LOOP
    assert "_plain_code_fence_streamed = True" in AGENT_LOOP
    # Fallback double-render guard.
    assert "if not _plain_code_fence_streamed:" in AGENT_LOOP


def test_multi_file_project_uses_workspace_tools_not_document_stream():
    assert "_code_project_enabled = bool(" in AGENT_LOOP
    assert "MULTI-FILE CODE PROJECT" in AGENT_LOOP
    assert '"write_file", "edit_file", "bash", "python"' in AGENT_LOOP
    assert "and not _code_project_enabled\n            and not has_doc_tool" in AGENT_LOOP


def test_document_to_workspace_followup_requires_real_file_writes():
    assert "EDITOR DOCUMENTS TO WORKSPACE" in AGENT_LOOP
    assert '_relevant_tools.add("manage_documents")' in AGENT_LOOP
    assert "action `read`" in AGENT_LOOP
    assert "Continue the task now by calling `write_file`" in AGENT_LOOP
    assert "No project file has been written yet" in AGENT_LOOP
    assert "Do not write files elsewhere" in AGENT_LOOP


def test_dead_parallel_live_preview_panel_is_removed():
    assert "live-preview-panel" not in INDEX_HTML
    assert ".live-preview-panel" not in STYLE_CSS
