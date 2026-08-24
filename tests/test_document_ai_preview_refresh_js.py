"""Regression guards for AI document updates while Markdown Preview is visible (#2182)."""

import re
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "static/js/document.js"


def _function_body(name: str) -> str:
    text = SRC.read_text(encoding="utf-8")
    match = re.search(rf"\n\s*(?:export\s+)?(?:async\s+)?function\s+{name}\([^)]*\)\s*\{{", text)
    assert match, f"{name} not found"

    start = match.end()
    depth = 1
    i = start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return text[start : i - 1]


def test_markdown_preview_refresh_rerenders_visible_preview():
    body = _function_body("_refreshMarkdownPreviewIfVisible")

    assert "_isMarkdownPreviewVisible()" in body
    assert "lang !== 'markdown'" in body
    assert "textarea.value = content;" in body
    assert "syncHighlighting();" in body
    assert "_setMarkdownPreviewActive(true, { remember: false });" in body


def test_doc_update_refreshes_preview_instead_of_hidden_editor_diff():
    body = _function_body("handleDocUpdate")

    visible = "const markdownPreviewWasVisible = _isMarkdownPreviewVisible();"
    refresh = "markdownPreviewWasVisible && _refreshMarkdownPreviewIfVisible(docId, newContent)"
    # Code edits render a coalesced accept/reject diff (baseline → final).
    diff = "enterDiffMode(base, finalContent);"

    assert visible in body
    assert refresh in body
    assert diff in body
    # Markdown preview refresh must take precedence over the code-edit diff path.
    assert body.index(refresh) < body.index(diff)


def test_multi_edit_turn_coalesces_diff_render():
    # A spam-edit turn must not rebuild the diff overlay per edit — the coalesce
    # timer collapses the burst into ONE diff (baseline captured once).
    body = _function_body("handleDocUpdate")
    assert "_coalesceDiffBaseline == null" in body
    assert "_coalesceDiffTimer = setTimeout(" in body
    assert "enterDiffMode(base, finalContent);" in body


def test_stream_open_resets_stale_views_before_live_writing():
    body = _function_body("streamDocOpen")

    assert "saveCurrentToMap();" in body
    assert "_resetTransientDocViews();" in body
    assert body.index("_resetTransientDocViews();") < body.index("_ensureDocPaneMounted();")


def test_transient_view_reset_hides_old_preview_and_run_output():
    body = _function_body("_resetTransientDocViews")

    assert "exitHtmlPreview();" in body
    assert "_setMarkdownPreviewActive(false, { remember: false });" in body
    for element_id in ("doc-csv-preview", "doc-run-output", "doc-pdf-view"):
        assert element_id in body


def test_html_preview_refreshes_after_ai_update():
    refresh_body = _function_body("_refreshHtmlPreviewIfVisible")
    update_body = _function_body("handleDocUpdate")

    assert "preview.srcdoc = content;" in refresh_body
    assert "htmlPreviewWasVisible && _refreshHtmlPreviewIfVisible(docId, newContent)" in update_body
