from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static/js/workspace.js").read_text(encoding="utf-8")
STORAGE_JS = (ROOT / "static/js/storage.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static/js/sessions.js").read_text(encoding="utf-8")
CHAT_JS = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
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
    assert 'class="workspace-tree" role="tree"' in WORKSPACE_JS
    assert 'class="workspace-tree-row" role="treeitem" tabindex="0"' in WORKSPACE_JS


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


def test_workspace_management_actions_are_confined_and_confirmed():
    assert 'id="workspace-download"' in WORKSPACE_JS
    assert 'id="workspace-rename"' in WORKSPACE_JS
    assert 'id="workspace-delete"' in WORKSPACE_JS
    assert "/api/workspace/download?workspace=${encodeURIComponent(workspace)}" in WORKSPACE_JS
    assert "/api/workspace/rename" in WORKSPACE_JS
    assert "/api/workspace/root" in WORKSPACE_JS
    assert "data.managed === true" in WORKSPACE_JS
    assert "Available for folders inside Workspaces" in WORKSPACE_JS
    assert 'Delete workspace "${name}" and all its contents? This cannot be undone.' in WORKSPACE_JS
    assert "confirmText: 'Delete workspace', danger: true" in WORKSPACE_JS
    assert ".workspace-delete-btn:hover:not(:disabled)" in STYLE_CSS


def test_workspace_picker_has_a_fixed_read_only_root():
    assert 'aria-label="Workspaces path" readonly' in WORKSPACE_JS
    assert "Type or paste a folder path" not in WORKSPACE_JS
    assert "<strong>Workspaces</strong> is the fixed root" in WORKSPACE_JS
    assert "Workspace reset to Workspaces" in WORKSPACE_JS


def test_workspace_picker_uses_lazy_compact_tree_navigation():
    assert "async function _compactPickerBranch" in WORKSPACE_JS
    assert "['java', 'kotlin', 'scala', 'groovy'].includes(folderName)" in WORKSPACE_JS
    assert "data.has_files === false" in WORKSPACE_JS
    assert "names.join('.')" in WORKSPACE_JS
    assert "async function _renderPickerChildren" in WORKSPACE_JS
    assert "workspace-tree-node workspace-tree-file" in WORKSPACE_JS
    assert "branch.data.files" in WORKSPACE_JS
    assert "workspace-tree-node.expanded > .workspace-tree-children" in STYLE_CSS
    assert "aria-selected=\"true\"" in STYLE_CSS


def test_workspace_selection_is_scoped_to_each_chat_session():
    assert "WORKSPACE_SESSIONS: 'odysseus-workspaces-by-session'" in STORAGE_JS
    assert "Storage.getJSON(KEYS.WORKSPACE_SESSIONS, {})" in WORKSPACE_JS
    assert "workspaces[String(nextSessionId)] = pending" in WORKSPACE_JS
    assert "workspaceModule.getWorkspace()" in CHAT_JS
    assert "Storage.get(Storage.KEYS.WORKSPACE" not in CHAT_JS
    assert "workspaceModule.syncWorkspaceForSession(id)" in SESSIONS_JS
    assert "workspaceModule.resetPendingWorkspace()" in SESSIONS_JS
    assert "materializePending: true" in SESSIONS_JS


def test_workspace_modal_resets_drag_position_on_mobile():
    workspace_css = STYLE_CSS[STYLE_CSS.index(".workspace-file-status") :]
    assert "#workspace-modal .workspace-modal-content" in workspace_css
    assert "left: auto !important" in workspace_css
    assert "#workspace-modal.workspace-explorer-open .workspace-modal-content" in workspace_css
    assert "left: 8px !important" in workspace_css
    assert "width: calc(100vw - 16px) !important" in workspace_css


def test_disabled_workspace_save_is_visually_distinct():
    assert "#workspace-file-save:disabled" in STYLE_CSS
    assert "opacity: 0.38" in STYLE_CSS
