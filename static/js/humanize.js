import uiModule from './ui.js';
import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const API_BASE = window.location.origin;

let _modal = null;
let _inputText = '';
let _outputText = '';
let _selectedValue = '';
let _models = [];
let _loading = false;
let _startedAt = 0;
let _elapsedTimer = null;
let _hideProgressTimer = null;
// Session log. Module-level, so it survives closing/reopening the window and
// every subsequent rewrite — it is only lost on a page reload. The progress
// banner shows the CURRENT phase and then hides; this keeps the history.
let _log = [];
let _loggedRunKeys = new Set();


const ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function _splitValue(value) {
  const idx = String(value || '').indexOf('::');
  if (idx < 0) return { endpoint_id: '', model: '' };
  return {
    endpoint_id: value.slice(0, idx),
    model: value.slice(idx + 2),
  };
}

function _normalizePlainText(text) {
  return String(text || '')
    .normalize('NFKC')
    .replace(/[\u00ad\u034f\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufe00-\ufe0f\ufeff]/g, '')
    .replace(/[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]/g, ' ')
    .replace(/[\u0410\u0430\u0412\u0432\u0415\u0435\u041a\u043a\u041c\u043c\u041d\u043d\u041e\u043e\u0420\u0440\u0421\u0441\u0422\u0442\u0425\u0445\u0423\u0443]/g, ch => ({
      '\u0410': 'A', '\u0430': 'a',
      '\u0412': 'B', '\u0432': 'b',
      '\u0415': 'E', '\u0435': 'e',
      '\u041A': 'K', '\u043A': 'k',
      '\u041C': 'M', '\u043C': 'm',
      '\u041D': 'H', '\u043D': 'h',
      '\u041E': 'O', '\u043E': 'o',
      '\u0420': 'P', '\u0440': 'p',
      '\u0421': 'C', '\u0441': 'c',
      '\u0422': 'T', '\u0442': 't',
      '\u0425': 'X', '\u0445': 'x',
      '\u0423': 'Y', '\u0443': 'y',
    }[ch] || ch))
    .replace(/[ \t]+/g, ' ')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\r\n?/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function _modelValue(item) {
  return `${item.endpoint_id}::${item.model}`;
}

async function _loadModels(select) {
  if (!select) return;
  select.innerHTML = '<option value="">Loading models...</option>';
  try {
    const res = await fetch(`${API_BASE}/api/models`, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const items = [];
    for (const ep of data.items || []) {
      if (ep.offline || ep.model_type === 'image' || !ep.endpoint_id) continue;
      const models = [];
      for (const m of ep.models || []) models.push(m);
      for (const m of ep.models_extra || []) models.push(m);
      for (const model of models) {
        if (!model || items.some(x => x.endpoint_id === ep.endpoint_id && x.model === model)) continue;
        items.push({
          endpoint_id: ep.endpoint_id,
          endpoint_name: ep.endpoint_name || 'Model endpoint',
          model,
        });
      }
    }
    _models = items;
    if (!_models.length) {
      select.innerHTML = '<option value="">No chat models available</option>';
      return;
    }
    select.innerHTML = _models.map(item => {
      const value = _modelValue(item);
      const label = `${item.model.split('/').pop()} - ${item.endpoint_name}`;
      return `<option value="${_esc(value)}">${_esc(label)}</option>`;
    }).join('');
    if (_selectedValue && _models.some(item => _modelValue(item) === _selectedValue)) {
      select.value = _selectedValue;
    } else {
      select.value = _modelValue(_models[0]);
      _selectedValue = select.value;
    }
  } catch (e) {
    console.error('Failed to load rewrite models', e);
    select.innerHTML = '<option value="">Failed to load models</option>';
  }
}

function _setBusy(on) {
  _loading = !!on;
  const btn = _modal?.querySelector('#humanize-run-btn');
  if (!btn) return;
  btn.disabled = _loading;
  btn.textContent = _loading ? 'Rewriting...' : 'Rewrite';
  _modal?.querySelector('.humanize-modal-content')?.classList.toggle('humanize-working', _loading);
}

function _formatElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const min = Math.floor(total / 60);
  const sec = total % 60;
  return min ? `${min}:${String(sec).padStart(2, '0')}` : `${sec}s`;
}

function _logAppend(step, detail, state = 'info') {
  const stamp = new Date().toLocaleTimeString([], { hour12: false });
  _log.push({ stamp, step, detail, state });
  _renderLog();
}

function _renderLog() {
  const list = _modal?.querySelector('#humanize-log-list');
  if (!list) return;
  list.innerHTML = _log.map(e => (
    `<div class="humanize-log-row is-${_esc(e.state)}">`
    + `<span class="humanize-log-time">${_esc(e.stamp)}</span>`
    + `<span class="humanize-log-step">${_esc(e.step)}</span>`
    + `<span class="humanize-log-detail">${_esc(e.detail)}</span>`
    + '</div>'
  )).join('');
  const empty = _modal?.querySelector('#humanize-log-empty');
  if (empty) empty.hidden = _log.length > 0;
  list.scrollTop = list.scrollHeight;
}

// Fold the server's per-job phase log into the session log. Polling re-sends
// the whole list every 2s, so entries are keyed to stay idempotent.
function _mergeServerLog(jobId, entries) {
  if (!Array.isArray(entries)) return;
  for (const e of entries) {
    const key = `${jobId}:${e.t}:${e.step}:${e.detail}`;
    if (_loggedRunKeys.has(key)) continue;
    _loggedRunKeys.add(key);
    const state = e.step === 'Error' ? 'error' : e.step === 'Complete' ? 'done' : 'info';
    _log.push({ stamp: `+${Number(e.t).toFixed(1)}s`, step: e.step, detail: e.detail, state });
  }
  _renderLog();
}

function _setProgress({ visible = true, step = 'Preparing', detail = '', progress = null, state = 'running' } = {}) {
  const panel = _modal?.querySelector('#humanize-progress');
  if (!panel) return;
  panel.hidden = !visible;
  panel.classList.toggle('is-done', state === 'done');
  panel.classList.toggle('is-error', state === 'error');

  const stepEl = panel.querySelector('#humanize-progress-step');
  const detailEl = panel.querySelector('#humanize-progress-detail');
  const elapsedEl = panel.querySelector('#humanize-progress-elapsed');
  const fillEl = panel.querySelector('#humanize-progress-fill');
  const pct = Number.isFinite(progress) ? Math.max(0, Math.min(1, progress)) : null;

  if (stepEl) stepEl.textContent = step;
  if (detailEl) detailEl.textContent = detail || 'Working through the rewrite pipeline';
  if (elapsedEl) elapsedEl.textContent = _startedAt ? _formatElapsed(Date.now() - _startedAt) : '0s';
  if (fillEl) {
    fillEl.style.width = pct == null ? '18%' : `${Math.round(pct * 100)}%`;
    fillEl.classList.toggle('is-indeterminate', pct == null && state === 'running');
  }
}

function _startProgress(step = 'Preparing', detail = 'Starting rewrite') {
  clearTimeout(_hideProgressTimer);
  clearInterval(_elapsedTimer);
  _startedAt = Date.now();
  _setProgress({ visible: true, step, detail, progress: 0.04 });
  _elapsedTimer = setInterval(() => {
    const elapsedEl = _modal?.querySelector('#humanize-progress-elapsed');
    if (elapsedEl && _startedAt) elapsedEl.textContent = _formatElapsed(Date.now() - _startedAt);
  }, 500);
}

function _finishProgress(step = 'Complete', detail = 'Rewrite finished', state = 'done') {
  clearInterval(_elapsedTimer);
  _elapsedTimer = null;
  _setProgress({ visible: true, step, detail, progress: state === 'done' ? 1 : null, state });
  _hideProgressTimer = setTimeout(() => {
    if (!_loading) _setProgress({ visible: false });
  }, state === 'done' ? 2200 : 5000);
}

function _resetTextareaView(textarea) {
  if (!textarea) return;
  textarea.scrollTop = 0;
  textarea.scrollLeft = 0;
  try {
    textarea.selectionStart = 0;
    textarea.selectionEnd = 0;
  } catch {}
}

function _syncFieldsFromState() {
  const input = _modal?.querySelector('#humanize-input');
  const output = _modal?.querySelector('#humanize-output');
  if (input && input.value !== _inputText) input.value = _inputText;
  if (output && output.value !== _outputText) output.value = _outputText;
}

async function _run() {
  if (_loading) return;
  const input = _modal?.querySelector('#humanize-input');
  const output = _modal?.querySelector('#humanize-output');
  const select = _modal?.querySelector('#humanize-model-select');
  _inputText = input?.value || '';
  _selectedValue = select?.value || '';
  const text = _inputText.trim();
  const chosen = _splitValue(_selectedValue);
  if (!text) {
    uiModule.showToast('Paste text first');
    input?.focus();
    return;
  }
  if (!chosen.endpoint_id || !chosen.model) {
    uiModule.showError('Select a model first');
    return;
  }
  _outputText = '';
  if (output) {
    output.value = '';
    _resetTextareaView(output);
  }
  _startProgress('Preparing', 'Validating input and selected model');
  _logAppend('Rewrite', `${chosen.model} · ${text.length} chars`);
  _setBusy(true);
  try { 
    _setProgress({ step: 'Submitting', detail: 'Sending rewrite job to the server', progress: 0.08 });
    const res = await fetch(`${API_BASE}/api/humanize`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        endpoint_id: chosen.endpoint_id,
        model: chosen.model,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    
    if (data.job_id) {
      _setProgress({ step: 'Queued', detail: 'Waiting for the rewrite worker', progress: 0.14 });
      while (true) {
        await new Promise(r => setTimeout(r, 2000));
        const statusRes = await fetch(`${API_BASE}/api/humanize/status/${data.job_id}`);
        if (!statusRes.ok) throw new Error(`HTTP ${statusRes.status}`);
        const statusData = await statusRes.json();
        _mergeServerLog(data.job_id, statusData.log);
        _setProgress({
          step: statusData.step || 'Rewriting',
          detail: statusData.detail || 'Model is working through the rewrite',
          progress: Number.isFinite(statusData.progress) ? statusData.progress : null,
          state: statusData.status === 'error' ? 'error' : 'running',
        });
        if (statusData.status === 'done') {
          _outputText = statusData.result?.text || '';
          const elapsedMs = Number.isFinite(statusData.elapsed)
            ? statusData.elapsed * 1000
            : Date.now() - _startedAt;
          _finishProgress('Complete', `Rewrite finished in ${_formatElapsed(elapsedMs)}`);
          break;
        } else if (statusData.status === 'error') {
          throw new Error(statusData.error || 'Unknown error during rewrite');
        }
        // status is running, loop continues
      }
    } else {
      _outputText = data.text || '';
      _finishProgress();
    }

    if (output) {
      output.value = _outputText;
      _resetTextareaView(output);
    }
  } catch (e) {
    _finishProgress('Error', e.message || 'Rewrite failed', 'error');
    _logAppend('Failed', e.message || 'Rewrite failed', 'error');
    uiModule.showError(`Rewrite failed: ${e.message || e}`);
  } finally {
    _setBusy(false);
  }
}

async function _copy() {
  const output = _modal?.querySelector('#humanize-output');
  const text = _normalizePlainText(output?.value || _outputText || '');
  if (!text.trim()) {
    uiModule.showToast('Nothing to copy');
    return;
  }
  if (output && output.value !== text) output.value = text;
  _outputText = text;
  try {
    await navigator.clipboard.writeText(text);
    uiModule.showToast('Copied');
  } catch {
    output?.select();
    document.execCommand('copy');
    uiModule.showToast('Copied');
  }
}

function _swap() {
  if (!_outputText.trim()) {
    uiModule.showToast('No output to swap');
    return;
  }
  _inputText = _outputText;
  _outputText = '';
  _syncFieldsFromState();
  _resetTextareaView(_modal?.querySelector('#humanize-input'));
  _resetTextareaView(_modal?.querySelector('#humanize-output'));
}

function _clear() {
  _inputText = '';
  _outputText = '';
  _syncFieldsFromState();
  _resetTextareaView(_modal?.querySelector('#humanize-input'));
  _resetTextareaView(_modal?.querySelector('#humanize-output'));
}

function _close() {
  clearInterval(_elapsedTimer);
  clearTimeout(_hideProgressTimer);
  _elapsedTimer = null;
  _inputText = _modal?.querySelector('#humanize-input')?.value || _inputText;
  _outputText = _modal?.querySelector('#humanize-output')?.value || _outputText;
  _modal?.remove();
  _modal = null;
  document.getElementById('tool-humanize-btn')?.classList.remove('active');
  try { Modals.unregister('humanize-modal'); } catch {}
}

function _buildModal() {
  _modal = document.createElement('div');
  _modal.id = 'humanize-modal';
  _modal.className = 'modal';
  _modal.innerHTML = `
    <div class="modal-content humanize-modal-content" role="dialog" aria-label="Rewrite text">
      <div class="modal-header">
        <h4>${ICON}<span>Rewrite</span></h4>
        <button class="close-btn" id="close-humanize-modal" aria-label="Close Rewrite">&times;</button>
      </div>
      <div class="humanize-toolbar">
        <select id="humanize-model-select" aria-label="Rewrite model"></select>
        <button type="button" id="humanize-run-btn">Rewrite</button>
        <button type="button" id="humanize-copy-btn">Copy</button>
        <button type="button" id="humanize-swap-btn">Swap</button>
        <button type="button" id="humanize-clear-btn">Clear</button>
      </div>
      <div class="humanize-progress" id="humanize-progress" hidden>
        <div class="humanize-progress-head">
          <span class="humanize-progress-dot" aria-hidden="true"></span>
          <span class="humanize-progress-label">Thinking</span>
          <span class="humanize-progress-step" id="humanize-progress-step">Preparing</span>
          <span class="humanize-progress-elapsed" id="humanize-progress-elapsed">0s</span>
        </div>
        <div class="humanize-progress-track" aria-hidden="true">
          <span class="humanize-progress-fill" id="humanize-progress-fill"></span>
        </div>
        <div class="humanize-progress-detail" id="humanize-progress-detail">Starting rewrite</div>
      </div>
      <details class="humanize-log" id="humanize-log">
        <summary>Log <span class="humanize-log-hint">— kept until you reload the page</span></summary>
        <div class="humanize-log-list" id="humanize-log-list"></div>
        <div class="humanize-log-empty" id="humanize-log-empty">Nothing logged yet.</div>
      </details>
      <div class="humanize-body">
        <label class="humanize-pane">
          <span>Paste text</span>
          <textarea id="humanize-input" spellcheck="true" placeholder="Paste text to rewrite..."></textarea>
        </label>
        <label class="humanize-pane">
          <span>Rewritten</span>
          <textarea id="humanize-output" readonly placeholder="Your rewritten text will appear here..."></textarea>
        </label>
      </div>
    </div>
  `;
  document.body.appendChild(_modal);
  const content = _modal.querySelector('.humanize-modal-content');
  const header = _modal.querySelector('.modal-header');
  makeWindowDraggable(_modal, {
    content,
    header,
    skipSelector: 'button, input, select, textarea, label',
  });
  Modals.register('humanize-modal', {
    sidebarBtnId: 'tool-humanize-btn',
    label: 'Rewrite',
    icon: ICON,
    restoreFn: () => {
      _modal?.classList.remove('hidden', 'modal-minimized');
      document.getElementById('tool-humanize-btn')?.classList.add('active');
      _syncFieldsFromState();
    },
    closeFn: _close,
  });
  Modals.injectMinimizeButton?.(_modal, 'humanize-modal');
  _modal.querySelector('#close-humanize-modal')?.addEventListener('click', _close);
  _modal.querySelector('#humanize-run-btn')?.addEventListener('click', _run);
  _modal.querySelector('#humanize-copy-btn')?.addEventListener('click', _copy);
  _modal.querySelector('#humanize-swap-btn')?.addEventListener('click', _swap);
  _modal.querySelector('#humanize-clear-btn')?.addEventListener('click', _clear);
  _modal.querySelector('#humanize-input')?.addEventListener('input', e => { _inputText = e.target.value; });
  _modal.querySelector('#humanize-output')?.addEventListener('input', e => { _outputText = e.target.value; });
  _modal.querySelector('#humanize-model-select')?.addEventListener('change', e => { _selectedValue = e.target.value; });
  _modal.addEventListener('click', e => {
    if (e.target.closest('.modal-minimize-btn, .minimize-btn, [data-minimize]')) {
      Modals.resetDockPosition?.('humanize-modal');
    }
  }, true);
  _syncFieldsFromState();
  // The window is rebuilt on reopen; repaint the accumulated log into it so
  // closing and reopening doesn't look like the history was lost.
  _renderLog();
  _loadModels(_modal.querySelector('#humanize-model-select'));
  return _modal;
}

export function open() {
  if (!_modal) _buildModal();
  _modal.classList.remove('hidden', 'modal-minimized');
  _modal.style.display = 'flex';
  document.getElementById('tool-humanize-btn')?.classList.add('active');
  _syncFieldsFromState();
  _renderLog();
  setTimeout(() => _modal?.querySelector('#humanize-input')?.focus(), 50);
}

export function isOpen() {
  return !!_modal && !_modal.classList.contains('hidden') && getComputedStyle(_modal).display !== 'none';
}

export default { open, isOpen };
