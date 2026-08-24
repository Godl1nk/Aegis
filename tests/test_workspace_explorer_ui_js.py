from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static/js/workspace.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


def test_workspace_tool_sidebar_opens_existing_workspace_module():
    assert 'id="tool-workspace-btn"' in INDEX_HTML
    assert 'id="tool-workspace-btn" role="button" tabindex="0"' in INDEX_HTML
    assert '<span class="grow">Workspace</span>' in INDEX_HTML
    assert "document.getElementById('tool-workspace-btn')" in WORKSPACE_JS
    assert "sidebar.addEventListener('click', openWorkspaceBrowser)" in WORKSPACE_JS
    assert "sidebar.addEventListener('keydown'" in WORKSPACE_JS
    assert "rail-workspace" not in INDEX_HTML


def test_workspace_explorer_uses_confined_file_api_contract():
    assert "/api/workspace/entries?${query}" in WORKSPACE_JS
    assert "/api/workspace/file?${query}" in WORKSPACE_JS
    assert "entry.type === 'directory'" in WORKSPACE_JS
    assert "method: 'PUT'" in WORKSPACE_JS
    assert "revision: _fileRevision" in WORKSPACE_JS
    assert "/api/workspace/entry?${query}" in WORKSPACE_JS
    assert "method: 'DELETE'" in WORKSPACE_JS


def test_workspace_file_editor_guards_dirty_changes_and_confirms_delete():
    assert 'id="workspace-file-editor"' in WORKSPACE_JS
    assert 'id="workspace-file-save"' in WORKSPACE_JS
    assert "editor.value !== _fileOriginal" in WORKSPACE_JS
    assert "_fileOriginal = editor.value" in WORKSPACE_JS
    assert WORKSPACE_JS.count("await _confirmDiscard()") >= 5
    assert "uiModule?.styledConfirm" in WORKSPACE_JS
    assert "confirmText: 'Delete', danger: true" in WORKSPACE_JS
    assert 'Delete empty folder "${label}"?' in WORKSPACE_JS
    assert 'Delete file "${label}"?' in WORKSPACE_JS
    assert "if (e.target !== row) return" in WORKSPACE_JS
    assert ".workspace-entry-delete { opacity: 1; }" in STYLE_CSS


def test_workspace_async_loads_and_escape_do_not_clobber_active_editor():
    assert "let _pickerLoadToken = 0" in WORKSPACE_JS
    assert "let _entryLoadToken = 0" in WORKSPACE_JS
    assert "let _fileLoadToken = 0" in WORKSPACE_JS
    assert "const token = ++_entryLoadToken" in WORKSPACE_JS
    assert "if (loadToken !== _fileLoadToken) return" in WORKSPACE_JS
    assert "function _bindWorkspaceEscape()" in WORKSPACE_JS
    assert "closeWorkspaceBrowser();" in WORKSPACE_JS
    assert "document.getElementById(cancelId)?.click()" in WORKSPACE_JS
    assert 'class="workspace-row" role="button" tabindex="0"' in WORKSPACE_JS


def test_workspace_explorer_respects_scaled_desktop_viewport():
    selector = ":root.ui-scale-125 #workspace-modal.workspace-explorer-open .workspace-modal-content"
    assert selector in STYLE_CSS
    assert "calc(92vw / 1.25)" in STYLE_CSS
    assert "calc(86dvh / 1.25)" in STYLE_CSS


def test_workspace_explorer_reuses_picker_and_stays_lightweight():
    assert 'id="workspace-picker"' in WORKSPACE_JS
    assert 'id="workspace-explorer"' in WORKSPACE_JS
    assert 'id="workspace-explorer-back"' in WORKSPACE_JS
    assert 'id="workspace-explorer-refresh"' in WORKSPACE_JS
    assert 'id="workspace-change"' in WORKSPACE_JS
    assert ".workspace-explorer-main" in STYLE_CSS
    assert "@media (max-width: 768px)" in STYLE_CSS
    assert "monaco" not in WORKSPACE_JS.lower()
    assert "documentModule" not in WORKSPACE_JS
    assert "WORKSPACES" not in WORKSPACE_JS
