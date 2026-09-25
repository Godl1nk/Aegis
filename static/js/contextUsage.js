// Persistent composer meter. Reads existing accounting; no model calls, polling,
// or network requests on keystrokes. Dynamic prompt additions are only knowable
// once a request has been prepared, so history and request counts stay labelled.
let selection = null;
let selectionKey = '';
let snapshot = null;
let revision = 0;
let pending = null;
let initialized = false;
let popup = null;
let showTimer = null;
let closeTimer = null;
let keyboardNavigation = false;

const number = value => Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : 0;
const fmt = value => Math.round(value).toLocaleString();
export const contextPercentLabel = percent => percent > 0 && percent < 1 ? '<1%' : `${Math.round(percent)}%`;

export function contextView(data, draft = '') {
  const draftTokens = draft ? Math.floor(draft.length * 0.3) + 4 : 0;
  const used = number(data?.used_tokens) + draftTokens;
  const limit = data?.context_length_known ? number(data.context_length) : 0;
  const percent = limit ? used / limit * 100 : null;
  return {
    used, limit, percent, draftTokens,
    level: percent == null ? 'unknown' : percent >= 85 ? 'danger' : percent >= 70 ? 'warning' : 'normal',
    estimated: data?.usage_source !== 'real' || draftTokens > 0,
  };
}

function closeDetails() {
  clearTimeout(showTimer);
  clearTimeout(closeTimer);
  showTimer = null;
  closeTimer = null;
  popup?.remove();
  popup = null;
  document.getElementById('composer-context')?.removeAttribute('aria-describedby');
}

function showDetails() {
  clearTimeout(showTimer);
  clearTimeout(closeTimer);
  showTimer = null;
  closeTimer = null;
  if (popup) return;
  const button = document.getElementById('composer-context');
  if (!button) return;
  popup = document.createElement('div');
  popup.id = 'composer-context-details';
  popup.className = 'composer-context-details';
  popup.setAttribute('role', 'tooltip');
  document.body.appendChild(popup);
  button.setAttribute('aria-describedby', popup.id);
  popup.addEventListener('mouseenter', () => {
    clearTimeout(closeTimer);
    closeTimer = null;
  });
  popup.addEventListener('mouseleave', scheduleClose);
  render();
  if (!snapshot && !pending) void refresh();
}

function scheduleShow() {
  clearTimeout(closeTimer);
  if (popup || showTimer) return;
  showTimer = setTimeout(showDetails, 180);
}

function scheduleClose() {
  clearTimeout(showTimer);
  clearTimeout(closeTimer);
  showTimer = null;
  closeTimer = setTimeout(() => {
    if (!keyboardNavigation || document.activeElement !== document.getElementById('composer-context')) closeDetails();
  }, 140);
}

function render() {
  const button = document.getElementById('composer-context');
  if (!button) return;
  const view = contextView(snapshot, document.getElementById('message')?.value || '');
  const available = snapshot != null;
  button.dataset.level = view.level;
  button.style.setProperty('--context-fill', Math.min(view.percent || 0, 100));
  button.querySelector('.context-percent').textContent = view.percent == null ? '—' : contextPercentLabel(view.percent);
  const scope = snapshot?.basis === 'request' ? 'Last request' : 'Saved chat';
  const label = !selection?.sessionId ? 'Context usage available after starting a chat'
    : !available ? 'Context usage unavailable'
    : `${scope}: ${view.estimated ? '~' : ''}${fmt(view.used)} tokens${view.limit ? ` / ${fmt(view.limit)} (${contextPercentLabel(view.percent)} used)` : ' — model limit unknown'}`;
  button.setAttribute('aria-label', label);
  if (!popup) return;
  popup.replaceChildren();
  const add = (text, className = '') => {
    const el = document.createElement('div');
    el.className = className;
    el.textContent = text;
    popup.appendChild(el);
  };
  add('Context usage', 'context-heading');
  add(label);
  if (available) {
    if (view.percent >= 85) add('Near the listed model window', 'context-status');
    if (snapshot.basis !== 'request' && snapshot.last_request) {
      const prior = snapshot.last_request;
      const model = String(prior.model || '').split('/').pop();
      const percent = Number(prior.context_percent);
      add(`Last reply used ${fmt(number(prior.input_tokens))} input tokens${percent > 0 && Number.isFinite(percent) ? ` (${contextPercentLabel(percent)} shown)` : ''}${model ? ` with ${model}` : ''}.`, 'context-note');
    }
    add(snapshot.basis === 'request'
      ? 'Last prepared request, including its response output when available. The next request may differ.'
      : 'Saved chat only. The reply footer measures its prepared request; instructions, tools, retrieved content and attachments can make it much larger.', 'context-note');
    const _autoParts = [
      snapshot.compact_threshold ? `at about ${Math.round(number(snapshot.compact_threshold) * 100)}% usage` : null,
      number(snapshot.compact_token_cap) ? `past ${fmt(number(snapshot.compact_token_cap))} tokens` : null,
    ].filter(Boolean);
    add(_autoParts.length
      ? `Auto-compaction: older messages are summarized before a request ${_autoParts.join(' or ')}, when enough history exists. Recent messages are kept.`
      : 'Automatic compaction is off — trimming still guards against overloads. Recent messages are kept.', 'context-note');
    if (view.draftTokens) add(`Draft: ~${fmt(view.draftTokens)} additional tokens.`, 'context-note');
    if (view.estimated) add('Approximate token count; a prepared request may report usage for its own model.', 'context-note');
    if (view.limit) add('The model window may come from a catalog when the server does not report its active limit.', 'context-note');
    add('Output needs room too. Agent input budgets or fallback trimming may reduce context earlier. Compaction is lossy, not unlimited memory.', 'context-note');
    if (!view.limit) add('The model limit could not be verified; no percentage is assumed.', 'context-note');
    if (snapshot.compacted) add('Earlier messages have been compacted.', 'context-status');
    if (snapshot.trimmed) add('Context was trimmed for this request.', 'context-status');
  }
  const rect = button.getBoundingClientRect();
  popup.style.left = `${Math.max(8, Math.min(rect.right - popup.offsetWidth, window.innerWidth - popup.offsetWidth - 8))}px`;
  popup.style.bottom = `${Math.max(8, window.innerHeight - rect.top + 8)}px`;
}

export function mergeDiscoverySnapshot(prev, data, { busy = false, sameRevision = true } = {}) {
  if (sameRevision && !busy) return data;
  const sameModel = !prev?.model || prev.model === data.model;
  // Status flags (trimmed/compacted) describe a past request, not the fresh
  // history: drop them from the carried-over snapshot unless the new payload
  // re-asserts them, so they can't stick around a turn later.
  const { trimmed: _droppedTrimmed, compacted: _droppedCompacted, ...carried } = prev || {};
  return { ...data, ...carried,
    context_length: sameModel ? data.context_length : null,
    context_length_known: sameModel && data.context_length_known };
}

async function refresh() {
  if (!selection?.sessionId || pending) return;
  const key = selectionKey;
  const before = revision;
  const controller = new AbortController();
  pending = controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`/api/session/${encodeURIComponent(selection.sessionId)}/context_info`, { signal: controller.signal });
    if (!response.ok) throw new Error('Context unavailable');
    const data = await response.json();
    if (data.used_tokens == null) throw new Error('Context unavailable');
    if (key !== selectionKey || pending !== controller || controller.signal.aborted) return;
    // A slow discovery request must not overwrite newer streaming usage.
    snapshot = mergeDiscoverySnapshot(snapshot, data, { busy: window.__odysseusChatBusy, sameRevision: before === revision });
    render();
  } catch {
    if (key === selectionKey && before === revision) { snapshot = null; render(); }
  } finally {
    clearTimeout(timeout);
    if (pending === controller) pending = null;
  }
}

function init() {
  if (initialized) return;
  const button = document.getElementById('composer-context');
  if (!button) return;
  initialized = true;
  document.getElementById('message')?.addEventListener('input', render);
  button.addEventListener('mouseenter', scheduleShow);
  button.addEventListener('mouseleave', scheduleClose);
  button.addEventListener('focus', () => {
    if (keyboardNavigation) showDetails();
  });
  button.addEventListener('blur', scheduleClose);
  button.addEventListener('pointerdown', () => {
    keyboardNavigation = false;
    closeDetails();
  });
  document.addEventListener('pointerdown', e => {
    keyboardNavigation = false;
    if (popup && !popup.contains(e.target) && !button.contains(e.target)) closeDetails();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Tab') keyboardNavigation = true;
    if (e.key === 'Escape') closeDetails();
  });
  window.addEventListener('resize', closeDetails);
  window.addEventListener('odysseus:chat-busy-change', e => {
    if (!e.detail?.active && (snapshot?.basis !== 'request' || snapshot?.model !== selection?.model)) void refresh();
  });
}

export function setContextSession(value) {
  init();
  const key = value ? JSON.stringify([value.sessionId, value.model, value.endpointUrl]) : '';
  if (key === selectionKey) return;
  pending?.abort();
  pending = null;
  selection = value;
  selectionKey = key;
  snapshot = null;
  revision++;
  closeDetails();
  render();
  void refresh();
}

export function refreshContextUsage() {
  pending?.abort();
  pending = null;
  void refresh();
}

export function sanitizeUsageData(prevSnapshot, data) {
  // A non-positive count is never a measurement: any real request carries at
  // least the user message, so providers reporting 0/0 (empty, error-adjacent
  // or uncounted turns) must not clobber a known-good value. Without this the
  // meter dropped to 0 mid-turn and stayed there until the next update.
  const incomingUsed = Number(data?.used_tokens);
  const prevUsed = Number(prevSnapshot?.used_tokens);
  if (!(incomingUsed > 0) && prevUsed > 0) {
    return { ...data, used_tokens: prevSnapshot.used_tokens };
  }
  return data;
}

export function updateContextUsage(sessionId, data) {
  if (!selection?.sessionId || sessionId !== selection.sessionId || !data) return;
  data = sanitizeUsageData(snapshot, data);
  if (data.model && data.model !== selection.model && data.model !== snapshot?.model) {
    // Never combine a fallback model's token count with the selected model's limit.
    snapshot = { ...data, context_length: null, context_length_known: false };
    revision++;
    render();
    return;
  }
  snapshot = { ...snapshot, ...data };
  revision++;
  render();
}
