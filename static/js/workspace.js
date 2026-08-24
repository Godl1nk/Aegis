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

export function getWorkspace() {
  return Storage.get(KEYS.WORKSPACE, '') || '';
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
  if (path) Storage.set(KEYS.WORKSPACE, path);
  else Storage.remove(KEYS.WORKSPACE);
  syncWorkspaceIndicator(path || '');
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
    _renderPicker(data);
    return true;
  } catch (e) {
    if (token !== _pickerLoadToken) return false;
    throw e;
  }
}

function _renderPicker(data) {
  _curPath = data.path;
  const body = _modal.querySelector('#workspace-body');
  const pathEl = _modal.querySelector('#workspace-cur-path');
  if (pathEl) {
    // Reflect the resolved (realpath) location back into the editable field.
    pathEl.value = data.path;
    pathEl.title = data.path;
  }
  let rows = '';
  if (data.parent) {
    rows += `<div class="workspace-row workspace-up" role="button" tabindex="0" data-path="${encodeURIComponent(data.parent)}">↑ ..</div>`;
  }
  for (const d of data.dirs) {
    // Backend supplies the full child path (os.path.join → cross-platform).
    rows += `<div class="workspace-row" role="button" tabindex="0" data-path="${encodeURIComponent(d.path)}">${_FOLDER_SVG}<span>${uiModule.esc(d.name)}</span></div>`;
  }
  if (data.truncated) {
    rows += '<div class="workspace-empty">Too many folders to list. Type or paste a path above to jump in.</div>';
  }
  if (!data.dirs.length && !data.parent) rows = '<div class="workspace-empty">No subfolders</div>';
  body.innerHTML = rows || '<div class="workspace-empty">No subfolders</div>';
  body.querySelectorAll('.workspace-row').forEach((row) => {
    const activate = () => _navigate(decodeURIComponent(row.dataset.path));
    row.addEventListener('click', activate);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activate();
      }
    });
  });
  // Filesystem roots (and sensitive dirs) can be browsed through but never
  // bound as the workspace; the backend rejects them too.
  const useBtn = _modal.querySelector('#workspace-use');
  if (useBtn) {
    useBtn.disabled = data.selectable === false;
    useBtn.title = data.selectable === false ? 'This folder cannot be used as a workspace' : '';
  }
  const createBtn = _modal.querySelector('#workspace-create');
  if (createBtn) {
    createBtn.disabled = data.selectable === false;
    createBtn.title = data.selectable === false ? 'Choose a valid parent folder first' : '';
  }
}

async function _navigate(path) {
  try {
    await _loadAndRenderPicker(path);
  } catch (e) {
    if (uiModule && uiModule.showError) uiModule.showError('Could not open folder');
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
  await _loadAndRenderPicker(path);
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
               spellcheck="false" autocomplete="off" autocapitalize="off" autocorrect="off"
               placeholder="Type or paste a folder path, then press Enter" />
        <p class="muted workspace-note">File tools are <strong>confined</strong> to this folder. Shell commands start here but are <strong>not sandboxed</strong> and can reach outside it. A workspace scopes the tools; it is not a security boundary.</p>
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
  // Editable path bar: Enter navigates to a typed/pasted folder.
  _modal.querySelector('#workspace-cur-path').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const v = e.target.value.trim();
      if (v) _navigate(v);
    }
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
  // Restore persisted workspace into the pill on load.
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

export default { initWorkspace, openWorkspaceBrowser, closeWorkspaceBrowser, getWorkspace, setWorkspace, vetAndSetWorkspace, clearWorkspace, syncWorkspaceIndicator, applyMode };
