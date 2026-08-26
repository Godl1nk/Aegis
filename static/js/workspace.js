// static/js/workspace.js
//
// Workspace picker: browse server directories in a draggable modal, choose a
// folder, and show it as a removable pill in the chat input bar. While set, the
// chat request sends `workspace` so the agent's file/shell tools are confined
// to that folder (see routes/chat_routes.py + src/tool_execution.py).

import Storage, { KEYS } from './storage.js';
import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';

const API_BASE = window.location.origin;
// Same folder glyph as the overflow menu item + pill (not an emoji).
const _FOLDER_SVG = '<svg class="workspace-row-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const _FILE_SVG = '<svg class="workspace-row-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
let _modal = null;
let _curPath = '';
let _explorerPath = '';
let _explorerParent = null;
let _filePath = '';
let _fileRevision = null;
let _fileOriginal = '';
let _pickerLoadToken = 0;
let _entryLoadToken = 0;
let _fileLoadToken = 0;
let _escapeBound = false;
let _activeSessionId = null;
let _pendingWorkspace = '';

function _workspaceMap() {
  const saved = Storage.getJSON(KEYS.WORKSPACE_SESSIONS, {});
  return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
}

function _saveWorkspaceMap(workspaces) {
  if (Object.keys(workspaces).length) Storage.setJSON(KEYS.WORKSPACE_SESSIONS, workspaces);
  else Storage.remove(KEYS.WORKSPACE_SESSIONS);
}

export function getWorkspace(sessionId = _activeSessionId) {
  if (!sessionId) return _pendingWorkspace;
  const path = _workspaceMap()[String(sessionId)];
  return typeof path === 'string' ? path : '';
}

function _basename(p) {
  if (!p) return '';
  // Handle both POSIX (/) and Windows (\) separators.
  const parts = p.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

// Workspace only applies to agent mode (it scopes the file/shell tools), so the
// pill + overflow entry are hidden in chat mode, like the bash toggle.
function _isChatMode() {
  const b = document.getElementById('mode-chat-btn');
  return !!(b && b.classList.contains('active'));
}

export function syncWorkspaceIndicator(path) {
  const chat = _isChatMode();
  const pill = document.getElementById('workspace-indicator-btn');
  const name = document.getElementById('workspace-indicator-name');
  const overflow = document.getElementById('overflow-workspace-btn');
  if (pill) {
    pill.style.display = (path && !chat) ? '' : 'none';
    pill.classList.toggle('active', !!path);
    if (path) pill.title = `Workspace: ${path}\nFile tools are confined here; shell commands start here but are not sandboxed and can reach outside it.\nClick to clear.`;
  }
  if (name) name.textContent = path ? _basename(path) : '';
  if (overflow) {
    overflow.style.display = chat ? 'none' : '';
    overflow.classList.toggle('active', !!path);
  }
  // Recompute the "+" overflow dot (app.js owns updatePlusDot via this event).
  try { document.dispatchEvent(new CustomEvent('overflow-state-change')); } catch (_) {}
}

// Called by the agent/chat mode toggle so the pill + overflow entry follow mode.
export function applyMode(_mode) {
  syncWorkspaceIndicator(getWorkspace());
}

export function setWorkspace(path) {
  const value = typeof path === 'string' ? path : '';
  if (_activeSessionId) {
    const workspaces = _workspaceMap();
    const key = String(_activeSessionId);
    if (value) workspaces[key] = value;
    else delete workspaces[key];
    _saveWorkspaceMap(workspaces);
  } else {
    _pendingWorkspace = value;
  }
  syncWorkspaceIndicator(value);
}

// A new chat has no workspace until the user assigns one. If they assign it
// before sending the first message, materialization transfers that temporary
// selection to the newly created session.
export function resetPendingWorkspace() {
  _activeSessionId = null;
  _pendingWorkspace = '';
  syncWorkspaceIndicator('');
}

export function syncWorkspaceForSession(sessionId, { materializePending = false } = {}) {
  const nextSessionId = sessionId || null;
  const pending = _pendingWorkspace;
  _activeSessionId = nextSessionId;
  _pendingWorkspace = '';

  if (nextSessionId && materializePending && pending) {
    const workspaces = _workspaceMap();
    workspaces[String(nextSessionId)] = pending;
    _saveWorkspaceMap(workspaces);
  }

  syncWorkspaceIndicator(getWorkspace());
}

/**
 * Validate a manually entered path server-side, then persist the canonical
 * form. Returns {ok, path|null}. Without this, a typo / file path / deleted
 * folder / filesystem root would be stored and shown as active while the
 * backend silently refuses to bind it on every send.
 */
export async function vetAndSetWorkspace(path) {
  try {
    const res = await fetch(`${API_BASE}/api/workspace/vet?path=${encodeURIComponent(path)}`, { credentials: 'same-origin' });
    if (!res.ok) return { ok: false, path: null };
    const data = await res.json();
    if (data.ok && data.path) {
      setWorkspace(data.path);
      return { ok: true, path: data.path };
    }
    return { ok: false, path: null };
  } catch (e) {
    return { ok: false, path: null };
  }
}

export function clearWorkspace() {
  setWorkspace('');
  if (uiModule && uiModule.showToast) uiModule.showToast('Workspace cleared');
}

async function _fetchJSON(url, options, fallback) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : fallback;
    const error = new Error(detail || `Request failed: ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return data;
}

async function _loadPicker(path) {
  const url = `${API_BASE}/api/workspace/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`;
  return _fetchJSON(url, { credentials: 'same-origin' }, 'Could not browse folders');
}

async function _loadAndRenderPicker(path) {
  const token = ++_pickerLoadToken;
  try {
    const data = await _loadPicker(path);
    if (token !== _pickerLoadToken) return false;
    await _renderPicker(data, token);
    return true;
  } catch (e) {
    if (token !== _pickerLoadToken) return false;
    throw e;
  }
}

function _selectPickerFolder(path, selectable = true) {
  _curPath = path;
  const body = _modal.querySelector('#workspace-body');
  const pathEl = _modal.querySelector('#workspace-cur-path');
  if (pathEl) {
    // Show the backend-resolved location. It is intentionally read-only:
    // Workspaces is the fixed root and the backend enforces the same boundary.
    pathEl.value = path;
    pathEl.title = path;
  }
  body?.querySelectorAll('.workspace-tree-row[aria-selected="true"]').forEach((row) => {
    row.setAttribute('aria-selected', 'false');
  });
  const encodedPath = encodeURIComponent(path);
  const selectedNode = body
    ? Array.from(body.querySelectorAll('.workspace-tree-node')).find((node) => node.dataset.path === encodedPath)
    : null;
  const selected = selectedNode?.querySelector(':scope > .workspace-tree-row');
  if (selected) selected.setAttribute('aria-selected', 'true');
  const useBtn = _modal.querySelector('#workspace-use');
  if (useBtn) {
    useBtn.disabled = !selectable;
    useBtn.title = !selectable ? 'This folder cannot be used as a workspace' : '';
  }
  const createBtn = _modal.querySelector('#workspace-create');
  if (createBtn) {
    createBtn.disabled = !selectable;
    createBtn.title = !selectable ? 'Choose a valid parent folder first' : '';
  }
}

async function _compactPickerBranch(dir, token, compactPackages = false) {
  const names = [String(dir.name || _basename(dir.path))];
  let path = String(dir.path || '');
  let data = await _loadPicker(path);
  let depth = 0;
  while (
    token === _pickerLoadToken
    && compactPackages
    && depth < 24
    && data.has_files === false
    && data.truncated !== true
    && Array.isArray(data.dirs)
    && data.dirs.length === 1
  ) {
    const only = data.dirs[0];
    names.push(String(only.name || _basename(only.path)));
    path = String(only.path || path);
    data = await _loadPicker(path);
    depth += 1;
  }
  return { path, label: names.join('.'), data };
}

async function _renderPickerChildren(container, dirs, files, token, compactPackages = false) {
  if (!container || token !== _pickerLoadToken) return;
  const list = Array.isArray(dirs) ? dirs : [];
  const fileList = Array.isArray(files) ? files : [];
  if (!list.length && !fileList.length) {
    container.innerHTML = '<div class="workspace-empty workspace-tree-empty">Empty folder</div>';
    return;
  }

  container.innerHTML = '<div class="workspace-tree-loading">Loading folders…</div>';
  const branches = await Promise.all(list.map((dir) => _compactPickerBranch(dir, token, compactPackages)));
  if (token !== _pickerLoadToken) return;
  container.innerHTML = '';

  for (const branch of branches) {
    const hasChildren = (Array.isArray(branch.data.dirs) && branch.data.dirs.length > 0)
      || (Array.isArray(branch.data.files) && branch.data.files.length > 0);
    const node = document.createElement('div');
    node.className = 'workspace-tree-node';
    node.dataset.path = encodeURIComponent(branch.path);
    node.innerHTML = `
      <div class="workspace-tree-row" role="treeitem" tabindex="0" aria-selected="false" aria-expanded="false">
        <span class="workspace-tree-chevron${hasChildren ? '' : ' is-leaf'}" aria-hidden="true">›</span>
        ${_FOLDER_SVG}
        <span class="workspace-tree-label">${uiModule.esc(branch.label)}</span>
      </div>
      <div class="workspace-tree-children" role="group"></div>`;
    container.appendChild(node);

    const row = node.querySelector('.workspace-tree-row');
    const children = node.querySelector('.workspace-tree-children');
    let loaded = false;
    const toggle = async (forceOpen = null) => {
      if (!hasChildren) return;
      const open = forceOpen === null ? !node.classList.contains('expanded') : forceOpen;
      node.classList.toggle('expanded', open);
      row.setAttribute('aria-expanded', String(open));
      if (open && !loaded) {
        loaded = true;
        try {
          const folderName = _basename(branch.path).toLowerCase();
          const compactChildren = branch.label.includes('.')
            || ['java', 'kotlin', 'scala', 'groovy'].includes(folderName);
          await _renderPickerChildren(
            children,
            branch.data.dirs,
            branch.data.files,
            token,
            compactChildren,
          );
        } catch (e) {
          loaded = false;
          children.innerHTML = '<div class="workspace-empty workspace-tree-empty">Could not open folder</div>';
        }
      }
    };
    const activate = () => {
      _selectPickerFolder(branch.path, branch.data.selectable !== false);
      toggle();
    };
    row.addEventListener('click', activate);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activate();
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        toggle(true);
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        if (node.classList.contains('expanded')) toggle(false);
        else node.parentElement?.closest('.workspace-tree-node')?.querySelector(':scope > .workspace-tree-row')?.focus();
      }
    });
  }

  for (const file of fileList) {
    const name = String(file.name || _basename(file.path));
    const node = document.createElement('div');
    node.className = 'workspace-tree-node workspace-tree-file';
    node.innerHTML = `
      <div class="workspace-tree-row" role="treeitem" tabindex="0" aria-selected="false" title="${uiModule.esc(name)}">
        <span class="workspace-tree-chevron is-leaf" aria-hidden="true">›</span>
        ${_FILE_SVG}
        <span class="workspace-tree-label">${uiModule.esc(name)}</span>
      </div>`;
    container.appendChild(node);
  }
}

async function _renderPicker(data, token) {
  const body = _modal.querySelector('#workspace-body');
  body.innerHTML = `
    <div class="workspace-tree" role="tree" aria-label="Workspace folders">
      <div class="workspace-tree-node workspace-tree-root expanded" data-path="${encodeURIComponent(data.path)}">
        <div class="workspace-tree-row" role="treeitem" tabindex="0" aria-selected="true" aria-expanded="true">
          <span class="workspace-tree-chevron" aria-hidden="true">›</span>
          ${_FOLDER_SVG}
          <span class="workspace-tree-label">Workspaces</span>
        </div>
        <div class="workspace-tree-children" role="group"></div>
      </div>
    </div>`;
  _selectPickerFolder(data.path, data.selectable !== false);
  const root = body.querySelector('.workspace-tree-root');
  const rootRow = root.querySelector(':scope > .workspace-tree-row');
  const rootChildren = root.querySelector(':scope > .workspace-tree-children');
  rootRow.addEventListener('click', () => {
    _selectPickerFolder(data.path, data.selectable !== false);
    const open = !root.classList.contains('expanded');
    root.classList.toggle('expanded', open);
    rootRow.setAttribute('aria-expanded', String(open));
  });
  rootRow.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      rootRow.click();
    } else if (e.key === 'ArrowRight') {
      root.classList.add('expanded');
      rootRow.setAttribute('aria-expanded', 'true');
    } else if (e.key === 'ArrowLeft') {
      root.classList.remove('expanded');
      rootRow.setAttribute('aria-expanded', 'false');
    }
  });
  await _renderPickerChildren(rootChildren, data.dirs, data.files, token);
  if (data.truncated && token === _pickerLoadToken) {
    rootChildren.insertAdjacentHTML('beforeend', '<div class="workspace-empty workspace-tree-empty">Folder list truncated.</div>');
  }
}

function _fileIsDirty() {
  if (!_filePath || !_modal) return false;
  const editor = _modal.querySelector('#workspace-file-editor');
  return !!editor && editor.value !== _fileOriginal;
}

function _syncFileActions() {
  if (!_modal) return;
  const dirty = _fileIsDirty();
  const save = _modal.querySelector('#workspace-file-save');
  const status = _modal.querySelector('#workspace-file-status');
  if (save) save.disabled = !_filePath || !dirty;
  if (status) status.textContent = dirty ? 'Unsaved changes' : '';
}

async function _confirmDiscard() {
  if (!_fileIsDirty()) return true;
  const ask = uiModule?.styledConfirm || window.styledConfirm;
  if (ask) {
    return ask('Discard unsaved workspace file changes?', {
      confirmText: 'Discard',
      danger: true,
    });
  }
  return window.confirm('Discard unsaved workspace file changes?');
}

function _resetFile() {
  _fileLoadToken += 1;
  _filePath = '';
  _fileRevision = null;
  _fileOriginal = '';
  if (!_modal) return;
  const empty = _modal.querySelector('#workspace-file-empty');
  const selected = _modal.querySelector('#workspace-file-selected');
  const editor = _modal.querySelector('#workspace-file-editor');
  if (empty) empty.classList.remove('hidden');
  if (selected) selected.classList.add('hidden');
  if (editor) editor.value = '';
  _syncFileActions();
}

function _setView(view) {
  const picker = _modal.querySelector('#workspace-picker');
  const explorer = _modal.querySelector('#workspace-explorer');
  const title = _modal.querySelector('#workspace-title-text');
  const isExplorer = view === 'explorer';
  if (isExplorer) _pickerLoadToken += 1;
  else _entryLoadToken += 1;
  picker.classList.toggle('hidden', isExplorer);
  explorer.classList.toggle('hidden', !isExplorer);
  _modal.classList.toggle('workspace-explorer-open', isExplorer);
  _modal.classList.toggle('workspace-picker-open', !isExplorer);
  if (title) title.textContent = isExplorer ? 'Workspace' : 'Select workspace';
}

function _formatSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function _loadEntries(path = '') {
  const workspace = getWorkspace();
  if (!workspace) throw new Error('Select a workspace first');
  const query = new URLSearchParams({ workspace, path: path || '' });
  return _fetchJSON(
    `${API_BASE}/api/workspace/entries?${query}`,
    { credentials: 'same-origin' },
    'Could not open workspace',
  );
}

async function _loadAndRenderEntries(path = '') {
  const token = ++_entryLoadToken;
  try {
    const data = await _loadEntries(path);
    if (token !== _entryLoadToken) return false;
    _renderEntries(data);
    return true;
  } catch (e) {
    if (token !== _entryLoadToken) return false;
    throw e;
  }
}

function _renderEntries(data) {
  const entries = Array.isArray(data.entries) ? data.entries : [];
  _explorerPath = data.path || '';
  _explorerParent = data.parent ?? null;

  const back = _modal.querySelector('#workspace-explorer-back');
  const pathEl = _modal.querySelector('#workspace-explorer-path');
  const body = _modal.querySelector('#workspace-entry-list');
  const workspace = data.workspace || getWorkspace();
  if (back) back.disabled = _explorerParent === null;
  if (pathEl) {
    const shown = _explorerPath ? `${_basename(workspace)} / ${_explorerPath}` : _basename(workspace);
    pathEl.textContent = shown;
    pathEl.title = _explorerPath ? `${workspace} / ${_explorerPath}` : workspace;
  }
  const managed = data.managed === true;
  for (const id of ['workspace-rename', 'workspace-delete']) {
    const button = _modal.querySelector(`#${id}`);
    if (!button) continue;
    button.disabled = !managed;
    button.title = managed
      ? (id === 'workspace-rename' ? 'Rename workspace' : 'Delete workspace')
      : 'Available for folders inside Workspaces';
  }

  let rows = '';
  for (const entry of entries) {
    const path = String(entry.path || '');
    const name = String(entry.name || _basename(path));
    const isDir = entry.type === 'directory';
    const kind = isDir ? 'directory' : 'file';
    const size = isDir ? '' : _formatSize(entry.size);
    rows += `<div class="workspace-entry-row" role="button" tabindex="0" data-entry-path="${encodeURIComponent(path)}" data-entry-kind="${kind}">`
      + `${isDir ? _FOLDER_SVG : _FILE_SVG}<span class="workspace-entry-name">${uiModule.esc(name)}</span>`
      + `<span class="workspace-entry-size">${uiModule.esc(size)}</span>`
      + `<button type="button" class="workspace-entry-delete" aria-label="Delete ${uiModule.esc(name)}" title="Delete">`
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>'
      + '</button></div>';
  }
  if (data.truncated) {
    rows += '<div class="workspace-empty">Folder list truncated.</div>';
  }
  body.innerHTML = rows || '<div class="workspace-empty">This folder is empty</div>';

  body.querySelectorAll('.workspace-entry-row').forEach((row) => {
    const activate = () => {
      const path = decodeURIComponent(row.dataset.entryPath || '');
      if (row.dataset.entryKind === 'directory') _openDirectory(path);
      else _openFile(path);
    };
    row.addEventListener('click', activate);
    row.addEventListener('keydown', (e) => {
      if (e.target !== row) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activate();
      }
    });
    row.querySelector('.workspace-entry-delete')?.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      _deleteEntry(
        decodeURIComponent(row.dataset.entryPath || ''),
        row.dataset.entryKind,
      );
    });
  });
}

async function _openDirectory(path) {
  if (!(await _confirmDiscard())) return;
  _resetFile();
  try {
    await _loadAndRenderEntries(path);
  } catch (e) {
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not open folder');
  }
}

async function _loadFile(path) {
  const query = new URLSearchParams({ workspace: getWorkspace(), path });
  return _fetchJSON(
    `${API_BASE}/api/workspace/file?${query}`,
    { credentials: 'same-origin' },
    'Could not open file',
  );
}

async function _openFile(path, { skipDiscard = false } = {}) {
  if (!skipDiscard && !(await _confirmDiscard())) return;
  // Once discard is accepted, do not leave the old dirty editor visible if
  // the next file turns out to be binary, too large, or otherwise unreadable.
  _resetFile();
  const loadToken = ++_fileLoadToken;
  try {
    const data = await _loadFile(path);
    if (loadToken !== _fileLoadToken) return;
    _filePath = data.path || path;
    _fileRevision = data.revision;
    const content = typeof data.content === 'string' ? data.content : '';

    const empty = _modal.querySelector('#workspace-file-empty');
    const selected = _modal.querySelector('#workspace-file-selected');
    const name = _modal.querySelector('#workspace-file-name');
    const meta = _modal.querySelector('#workspace-file-meta');
    const editor = _modal.querySelector('#workspace-file-editor');
    if (empty) empty.classList.add('hidden');
    if (selected) selected.classList.remove('hidden');
    if (name) {
      name.textContent = _basename(_filePath);
      name.title = _filePath;
    }
    if (meta) meta.textContent = `${_filePath}${data.size !== undefined ? ` · ${_formatSize(data.size)}` : ''}`;
    if (editor) {
      editor.value = content;
      // Textareas normalize CRLF to LF. Compare against the actual DOM value
      // so opening a Windows file does not immediately look unsaved.
      _fileOriginal = editor.value;
    } else {
      _fileOriginal = content;
    }
    _syncFileActions();
  } catch (e) {
    if (loadToken !== _fileLoadToken) return;
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not open file');
  }
}

async function _saveFile() {
  if (!_filePath) return;
  const workspace = getWorkspace();
  const path = _filePath;
  const loadToken = _fileLoadToken;
  const editor = _modal.querySelector('#workspace-file-editor');
  const save = _modal.querySelector('#workspace-file-save');
  const content = editor?.value ?? '';
  if (save) save.disabled = true;
  try {
    const data = await _fetchJSON(`${API_BASE}/api/workspace/file`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workspace,
        path,
        content,
        revision: _fileRevision,
      }),
    }, 'Could not save file');
    if (loadToken !== _fileLoadToken || _filePath !== path || getWorkspace() !== workspace) return;
    _fileOriginal = content;
    if (Object.prototype.hasOwnProperty.call(data, 'revision')) _fileRevision = data.revision;
    _syncFileActions();
    if (uiModule?.showToast) uiModule.showToast(`Saved ${_basename(_filePath)}`);
  } catch (e) {
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not save file');
    _syncFileActions();
  }
}

async function _deleteEntry(path, kind) {
  if (!path) return;
  const workspace = getWorkspace();
  const label = _basename(path);
  const isDir = kind === 'directory';
  const ask = uiModule?.styledConfirm || window.styledConfirm;
  const message = isDir
    ? `Delete empty folder "${label}"?`
    : `Delete file "${label}"?`;
  const confirmed = ask
    ? await ask(message, { confirmText: 'Delete', danger: true })
    : window.confirm(message);
  if (!confirmed) return;

  try {
    const query = new URLSearchParams({ workspace, path });
    await _fetchJSON(`${API_BASE}/api/workspace/entry?${query}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    }, isDir ? 'Folder must be empty before deleting' : 'Could not delete file');
    if (getWorkspace() === workspace) {
      if (_filePath === path) _resetFile();
      await _loadAndRenderEntries(_explorerPath);
    }
    if (uiModule?.showToast) uiModule.showToast(`Deleted ${label}`);
  } catch (e) {
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not delete item');
  }
}

async function _refreshExplorer() {
  if (!(await _confirmDiscard())) return;
  const selected = _filePath;
  _resetFile();
  try {
    const rendered = await _loadAndRenderEntries(_explorerPath);
    if (rendered && selected) await _openFile(selected, { skipDiscard: true });
  } catch (e) {
    _resetFile();
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not refresh workspace');
  }
}

async function _showPicker(path = '') {
  _setView('picker');
  await _loadAndRenderPicker('');
}

async function _showExplorer(workspace = getWorkspace()) {
  if (!workspace) {
    await _showPicker('');
    return;
  }
  _resetFile();
  _setView('explorer');
  await _loadAndRenderEntries('');
}

async function _changeWorkspace() {
  if (!(await _confirmDiscard())) return;
  _resetFile();
  try {
    await _showPicker(getWorkspace());
  } catch (e) {
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not browse folders');
  }
}

function _downloadWorkspace() {
  const workspace = getWorkspace();
  if (!workspace) return;
  const link = document.createElement('a');
  link.href = `${API_BASE}/api/workspace/download?workspace=${encodeURIComponent(workspace)}`;
  link.download = `${_basename(workspace) || 'workspace'}.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function _renameWorkspace() {
  if (!(await _confirmDiscard())) return;
  const workspace = getWorkspace();
  if (!workspace) return;
  const currentName = _basename(workspace);
  const name = await uiModule.styledPrompt(`Rename "${currentName}"`, {
    title: 'Rename workspace',
    defaultValue: currentName,
    placeholder: 'Folder name',
    confirmText: 'Rename',
  });
  if (!name || name.trim() === currentName) return;

  const button = _modal.querySelector('#workspace-rename');
  if (button) button.disabled = true;
  try {
    const data = await _fetchJSON(`${API_BASE}/api/workspace/rename`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workspace, name }),
    }, 'Could not rename workspace');
    setWorkspace(data.path);
    if (uiModule?.showToast) uiModule.showToast(`Workspace renamed: ${_basename(data.path)}`);
    await _showExplorer(data.path);
  } catch (e) {
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not rename workspace');
    if (button) button.disabled = false;
  }
}

async function _deleteWorkspace() {
  if (!(await _confirmDiscard())) return;
  const workspace = getWorkspace();
  if (!workspace) return;
  const name = _basename(workspace);
  const ask = uiModule?.styledConfirm || window.styledConfirm;
  const message = `Delete workspace "${name}" and all its contents? This cannot be undone.`;
  const confirmed = ask
    ? await ask(message, { confirmText: 'Delete workspace', danger: true })
    : window.confirm(message);
  if (!confirmed) return;

  const button = _modal.querySelector('#workspace-delete');
  if (button) button.disabled = true;
  try {
    const data = await _fetchJSON(`${API_BASE}/api/workspace/root`, {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workspace, confirmation: name }),
    }, 'Could not delete workspace');
    setWorkspace('');
    _resetFile();
    if (uiModule?.showToast) uiModule.showToast(`Workspace deleted: ${name}`);
    await _showPicker(data.parent || '');
  } catch (e) {
    if (uiModule?.showError) uiModule.showError(e.message || 'Could not delete workspace');
    if (button) button.disabled = false;
  }
}

async function _createWorkspace() {
  const parent = _curPath;
  const name = await uiModule.styledPrompt(`Create inside ${parent}`, {
    title: 'New workspace',
    placeholder: 'Folder name',
    confirmText: 'Create',
  });
  if (!name) return;

  const createBtn = _modal.querySelector('#workspace-create');
  if (createBtn) createBtn.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/api/workspace/create`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, name }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.path) {
      throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not create workspace');
    }
    setWorkspace(data.path);
    if (uiModule.showToast) uiModule.showToast(`Workspace created: ${_basename(data.path)}`);
    await _showExplorer(data.path);
  } catch (e) {
    if (uiModule.showError) uiModule.showError(e.message || 'Could not create workspace');
    if (createBtn) createBtn.disabled = false;
  }
}

function _bindWorkspaceEscape() {
  if (_escapeBound) return;
  _escapeBound = true;
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !_modal || _modal.style.display === 'none') return;

    // Dismiss a confirmation/prompt above the workspace first. Handling this
    // in capture phase also prevents the app-level Escape shortcut from
    // minimizing the document behind the workspace modal.
    for (const [overlayId, cancelId] of [
      ['styled-confirm-overlay', 'styled-confirm-cancel'],
      ['styled-prompt-overlay', 'styled-prompt-cancel'],
    ]) {
      const overlay = document.getElementById(overlayId);
      if (overlay && !overlay.classList.contains('hidden') && overlay.style.display !== 'none') {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        document.getElementById(cancelId)?.click();
        return;
      }
    }

    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    closeWorkspaceBrowser();
  }, true);
}

function _getModal() {
  if (_modal) return _modal;
  _modal = document.createElement('div');
  _modal.id = 'workspace-modal';
  _modal.className = 'modal';
  _modal.style.display = 'none';
  _modal.innerHTML = `
    <div class="modal-content workspace-modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span id="workspace-title-text">Workspace</span></h4>
        <button class="close-btn" id="workspace-close" aria-label="Close">✖</button>
      </div>
      <section class="workspace-picker hidden" id="workspace-picker">
        <input type="text" class="styled-prompt-input workspace-cur" id="workspace-cur-path"
               aria-label="Workspaces path" readonly />
        <p class="muted workspace-note"><strong>Workspaces</strong> is the fixed root. Create or select a project folder here; workspace file tools cannot browse outside it.</p>
        <div class="modal-body workspace-body" id="workspace-body"></div>
        <div class="modal-footer workspace-footer">
          <button type="button" class="confirm-btn confirm-btn-secondary" id="workspace-create">New folder</button>
          <button type="button" class="confirm-btn confirm-btn-secondary" id="workspace-cancel">Cancel</button>
          <button type="button" class="confirm-btn confirm-btn-primary" id="workspace-use">Use this folder</button>
        </div>
      </section>
      <section class="workspace-explorer hidden" id="workspace-explorer">
        <div class="workspace-explorer-toolbar">
          <button type="button" class="workspace-toolbar-btn" id="workspace-explorer-back" title="Back" aria-label="Back to parent folder">←</button>
          <div class="workspace-explorer-path" id="workspace-explorer-path"></div>
          <button type="button" class="workspace-toolbar-btn" id="workspace-explorer-refresh" title="Refresh" aria-label="Refresh workspace">↻</button>
          <button type="button" class="workspace-toolbar-btn" id="workspace-download" title="Download workspace as ZIP" aria-label="Download workspace as ZIP">ZIP</button>
          <button type="button" class="workspace-toolbar-btn" id="workspace-rename" title="Rename workspace" aria-label="Rename workspace" disabled>Rename</button>
          <button type="button" class="workspace-toolbar-btn workspace-delete-btn" id="workspace-delete" title="Delete workspace" aria-label="Delete workspace" disabled>Delete</button>
          <button type="button" class="workspace-toolbar-btn workspace-change-btn" id="workspace-change">Change</button>
        </div>
        <div class="workspace-explorer-main">
          <div class="workspace-entry-pane" id="workspace-entry-list" aria-label="Workspace files"></div>
          <div class="workspace-file-pane">
            <div class="workspace-file-empty" id="workspace-file-empty">Select a text file to view or edit.</div>
            <div class="workspace-file-selected hidden" id="workspace-file-selected">
              <div class="workspace-file-header">
                <div class="workspace-file-heading">
                  <strong id="workspace-file-name"></strong>
                  <span id="workspace-file-meta"></span>
                </div>
                <button type="button" class="workspace-entry-delete workspace-file-delete" id="workspace-file-delete" title="Delete file" aria-label="Delete selected file">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>
                </button>
              </div>
              <textarea class="workspace-file-editor" id="workspace-file-editor" aria-label="Workspace file content" spellcheck="false"></textarea>
              <div class="workspace-file-footer">
                <span class="workspace-file-status" id="workspace-file-status"></span>
                <button type="button" class="confirm-btn confirm-btn-primary" id="workspace-file-save" disabled>Save</button>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>`;
  document.body.appendChild(_modal);
  _bindWorkspaceEscape();
  _modal.querySelector('#workspace-close').addEventListener('click', () => closeWorkspaceBrowser());
  _modal.querySelector('#workspace-create').addEventListener('click', _createWorkspace);
  _modal.querySelector('#workspace-cancel').addEventListener('click', async () => {
    if (getWorkspace()) await _showExplorer();
    else closeWorkspaceBrowser();
  });
  _modal.querySelector('#workspace-use').addEventListener('click', async () => {
    setWorkspace(_curPath);
    if (uiModule && uiModule.showToast) uiModule.showToast(`Workspace set: ${_basename(_curPath)}`);
    await _showExplorer(_curPath);
  });
  _modal.querySelector('#workspace-explorer-back').addEventListener('click', () => {
    if (_explorerParent !== null) _openDirectory(_explorerParent);
  });
  _modal.querySelector('#workspace-explorer-refresh').addEventListener('click', _refreshExplorer);
  _modal.querySelector('#workspace-download').addEventListener('click', _downloadWorkspace);
  _modal.querySelector('#workspace-rename').addEventListener('click', _renameWorkspace);
  _modal.querySelector('#workspace-delete').addEventListener('click', _deleteWorkspace);
  _modal.querySelector('#workspace-change').addEventListener('click', _changeWorkspace);
  _modal.querySelector('#workspace-file-editor').addEventListener('input', _syncFileActions);
  _modal.querySelector('#workspace-file-save').addEventListener('click', _saveFile);
  _modal.querySelector('#workspace-file-delete').addEventListener('click', () => {
    if (_filePath) _deleteEntry(_filePath, 'file');
  });
  _modal.addEventListener('click', (e) => {
    if (e.target === _modal) closeWorkspaceBrowser();
  });
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(_modal, { content, header });
  return _modal;
}

export async function openWorkspaceBrowser() {
  const modal = _getModal();
  modal.style.display = 'flex';
  try {
    if (getWorkspace()) await _showExplorer();
    else await _showPicker('');
  } catch (e) {
    if (getWorkspace() && e?.status === 400) {
      setWorkspace('');
      await _showPicker('');
      if (uiModule?.showToast) uiModule.showToast('Workspace reset to Workspaces');
      return;
    }
    if (uiModule && uiModule.showError) uiModule.showError(e.message || 'Could not open workspace');
  }
}

export async function closeWorkspaceBrowser(force = false) {
  if (!force && !(await _confirmDiscard())) return false;
  if (_modal) {
    _modal.style.display = 'none';
    _pickerLoadToken += 1;
    _entryLoadToken += 1;
    _resetFile();
  }
  return true;
}

export function initWorkspace() {
  // The old global key could leak a workspace into unrelated chats. It is no
  // longer used; each persisted selection is keyed by session ID instead.
  Storage.remove(KEYS.WORKSPACE);
  syncWorkspaceIndicator(getWorkspace());
  const overflow = document.getElementById('overflow-workspace-btn');
  if (overflow) overflow.addEventListener('click', openWorkspaceBrowser);
  const sidebar = document.getElementById('tool-workspace-btn');
  if (sidebar) {
    sidebar.addEventListener('click', openWorkspaceBrowser);
    sidebar.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openWorkspaceBrowser();
      }
    });
  }
  const pill = document.getElementById('workspace-indicator-btn');
  if (pill) pill.addEventListener('click', clearWorkspace);
}

export default { initWorkspace, openWorkspaceBrowser, closeWorkspaceBrowser, getWorkspace, setWorkspace, vetAndSetWorkspace, clearWorkspace, syncWorkspaceIndicator, syncWorkspaceForSession, resetPendingWorkspace, applyMode };
