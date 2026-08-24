// effortPicker.js — thinking-effort chip in the composer, beside the model chip.
//
// The backend decides WHICH control a model honours (reasoning_effort /
// chat_template_kwargs / think / thinking object / budget); this only asks
// /api/models/reasoning-control what that model supports and offers exactly
// those levels. A model whose control is a boolean has no low/medium/high, so
// showing them would be a lie — and a model with no known control hides the
// chip completely rather than presenting a setting that does nothing.
//
// The choice is stored as a per-model override in settings, so switching models
// switches the displayed level with it.

import { bindMenuDismiss } from './escMenuStack.js';

const LEVELS = [
  { value: 'auto', label: 'Auto', sub: 'provider default' },
  { value: 'off', label: 'Off', sub: 'no thinking' },
  { value: 'low', label: 'Low', sub: '' },
  { value: 'medium', label: 'Medium', sub: '' },
  { value: 'high', label: 'High', sub: '' },
];

let _current = null;       // last capability payload
let _currentKey = '';      // model + endpoint; model IDs are not globally unique
let _closeMenu = null;
let _inflight = null;      // dedupes rapid model switches

function _el(id) { return document.getElementById(id); }

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function _hide() {
  const wrap = _el('effort-picker-wrap');
  if (wrap) wrap.classList.add('hidden');
  // Switching to a model with no control while the menu is open would
  // otherwise leave the menu floating over a chip that is no longer there.
  _closeIfOpen();
}

/** Read the model the composer is currently pointed at. */
function _currentModel() {
  const wrap = _el('model-picker-wrap');
  const label = _el('model-picker-label');
  const src = (wrap && wrap.dataset.modelId) ? wrap.dataset : (label ? label.dataset : null);
  if (!src || !src.modelId) return null;
  return { model: src.modelId, baseUrl: src.endpointUrl || '', endpointId: src.endpointId || '' };
}

function _closeIfOpen() {
  const menu = _el('effort-picker-menu');
  const btn = _el('effort-picker-btn');
  if (menu) menu.classList.add('hidden');
  if (btn) btn.setAttribute('aria-expanded', 'false');
  if (_closeMenu) { try { _closeMenu(); } catch (_) {} _closeMenu = null; }
}

async function _savePreference(model, value) {
  // Merge rather than replace: other models' overrides must survive.
  let existing = {};
  try {
    const res = await fetch('/api/auth/settings', { credentials: 'same-origin' });
    if (res.ok) {
      const data = await res.json();
      const stored = data.reasoning_effort_by_model;
      if (stored && typeof stored === 'object' && !Array.isArray(stored)) existing = { ...stored };
    }
  } catch (_) { /* fall through with an empty map */ }

  if (value === 'auto') delete existing[model];
  else existing[model] = value;

  const res = await fetch('/api/auth/settings', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reasoning_effort_by_model: existing }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

function _renderMenu() {
  const menu = _el('effort-picker-menu');
  if (!menu || !_current) return;
  const supported = Array.isArray(_current.supported) && _current.supported.length
    ? _current.supported
    : LEVELS.map(l => l.value);

  const rows = LEVELS.filter(l => supported.includes(l.value)).map(l => (
    `<button type="button" role="menuitem" class="effort-picker-item${l.value === _current.preference ? ' is-active' : ''}" data-value="${l.value}">`
    + '<svg class="effort-check" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
    + `<span>${_esc(l.label)}</span>`
    + (l.sub ? `<span class="effort-sub">${_esc(l.sub)}</span>` : '')
    + '</button>'
  )).join('');

  // Say why the graded levels are missing, rather than leaving a short list
  // looking like a bug.
  let note = '';
  if (!_current.supports_effort && _current.mechanism) {
    note = `<div class="effort-picker-note">${_esc(_current.model)} supports on/off only — it has no graded effort levels.</div>`;
  }
  menu.innerHTML = rows + note;

  menu.querySelectorAll('.effort-picker-item').forEach(item => {
    item.addEventListener('click', async (e) => {
      e.stopPropagation();
      const value = item.dataset.value;
      _closeIfOpen();
      const prev = _current.preference;
      _current.preference = value;          // optimistic
      _paint();
      try {
        await _savePreference(_current.model, value);
      } catch (err) {
        _current.preference = prev;
        _paint();
        if (window.uiModule && window.uiModule.showError) {
          window.uiModule.showError(
            err && String(err).includes('403')
              ? 'Thinking effort is an admin setting'
              : `Could not save thinking effort: ${err.message || err}`,
          );
        }
      }
    });
  });
}

function _paint() {
  const wrap = _el('effort-picker-wrap');
  const btn = _el('effort-picker-btn');
  const label = _el('effort-picker-label');
  if (!wrap || !btn || !label) return;
  if (!_current || !_current.mechanism) { _hide(); return; }
  const level = LEVELS.find(l => l.value === _current.preference) || LEVELS[0];
  label.textContent = level.label;
  btn.classList.toggle('is-set', _current.preference !== 'auto');
  btn.title = `Thinking effort for ${_current.model} (${_current.mechanism})`;
  wrap.classList.remove('hidden');
}

/**
 * Re-read the capability for whatever model the composer points at.
 * Safe to call often — modelPicker calls it on every update.
 */
export async function refreshEffortPicker() {
  const target = _currentModel();
  if (!target) { _hide(); return; }
  const targetKey = `${target.model}\n${target.endpointId || ''}\n${target.baseUrl || ''}`;
  if (_current && _currentKey === targetKey) { _paint(); return; }

  const token = {};
  _inflight = token;
  try {
    const url = `/api/models/reasoning-control?model=${encodeURIComponent(target.model)}`
      + `&base_url=${encodeURIComponent(target.baseUrl)}`
      + `&endpoint_id=${encodeURIComponent(target.endpointId || '')}`;
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) { _hide(); return; }
    const data = await res.json();
    if (_inflight !== token) return;   // a newer switch won
    _current = data;
    _currentKey = targetKey;
    _paint();
    _renderMenu();
  } catch (_) {
    _hide();
  }
}

export function initEffortPicker() {
  const btn = _el('effort-picker-btn');
  const menu = _el('effort-picker-menu');
  if (!btn || !menu) return;
  // Idempotent: a second bind would toggle the menu twice per click, which
  // reads as "the button does nothing".
  if (btn.dataset.effortBound === '1') { refreshEffortPicker(); return; }
  btn.dataset.effortBound = '1';

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!menu.classList.contains('hidden')) { _closeIfOpen(); return; }
    _renderMenu();
    menu.classList.remove('hidden');
    btn.setAttribute('aria-expanded', 'true');
    _closeMenu = bindMenuDismiss(menu, () => {
      menu.classList.add('hidden');
      btn.setAttribute('aria-expanded', 'false');
      _closeMenu = null;
    }, (ev) => !menu.contains(ev.target) && ev.target !== btn && !btn.contains(ev.target));
  });

  refreshEffortPicker();
}

export default { initEffortPicker, refreshEffortPicker };
