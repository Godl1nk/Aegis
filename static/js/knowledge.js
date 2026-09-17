import uiModule from './ui.js';

const PAGE_SIZE = 25;
let offset = 0;
let matched = 0;
let controller;
let initialized = false;
let debounce;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function formatDate(value) {
  const date = new Date(Number(value) * 1000);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Not recorded';
}

function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function initialize() {
  if (initialized) return;
  initialized = true;
  document.getElementById('knowledge-search').addEventListener('input', () => {
    clearTimeout(debounce);
    // Invalidate an old response immediately, not only after the debounce.
    controller?.abort();
    debounce = setTimeout(() => { offset = 0; loadKnowledge(); }, 250);
  });
  document.getElementById('knowledge-filter').addEventListener('change', () => { offset = 0; loadKnowledge(); });
  document.getElementById('knowledge-refresh').addEventListener('click', () => loadKnowledge());
  document.getElementById('knowledge-prev').addEventListener('click', () => { offset = Math.max(0, offset - PAGE_SIZE); loadKnowledge(); });
  document.getElementById('knowledge-next').addEventListener('click', () => { if (offset + PAGE_SIZE < matched) { offset += PAGE_SIZE; loadKnowledge(); } });
}

async function mutate(row, action, card, button) {
  const deleting = action === 'delete';
  const message = deleting
    ? 'Delete this knowledge entry? It will no longer be recalled. You can learn the claim again after validation.'
    : 'Revalidate this claim? Its saved validation query will be sent to web search. Sources and expiry update only if validation succeeds.';
  if (!await uiModule.styledConfirm(message, { confirmText: deleting ? 'Delete' : 'Revalidate', danger: deleting })) return;
  const buttons = card.querySelectorAll('button');
  const disabledStates = [...buttons].map(item => item.disabled);
  buttons.forEach(item => { item.disabled = true; });
  const previousText = button.textContent;
  button.textContent = deleting ? 'Deleting…' : 'Validating…';
  const outcome = card.querySelector('.knowledge-outcome');
  outcome.textContent = '';
  try {
    const response = await fetch(`/api/knowledge/${encodeURIComponent(row.id)}${deleting ? '' : '/revalidate'}`, {
      method: deleting ? 'DELETE' : 'POST', credentials: 'same-origin',
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    uiModule.showToast(deleting ? 'Knowledge deleted' : 'Knowledge revalidated');
    await loadKnowledge();
  } catch (error) {
    outcome.textContent = `${deleting ? 'Delete' : 'Revalidation'} failed: ${error.message}`;
  } finally {
    buttons.forEach((item, index) => { item.disabled = disabledStates[index]; });
    button.textContent = previousText;
  }
}

function renderCard(row, learningEnabled) {
  const card = el('article', 'knowledge-card');
  const header = el('div', 'knowledge-card-header');
  header.append(el('span', `knowledge-badge ${row.freshness === 'fresh' ? 'fresh' : 'expired'}`,
    row.freshness === 'fresh' ? 'Fresh' : 'Needs review'));
  header.append(el('span', 'memory-desc', `Confidence: ${row.confidence || 'not recorded'}`));
  card.append(header, el('p', 'knowledge-claim', row.text));
  const details = el('details', 'knowledge-details');
  details.append(el('summary', '', 'Sources and details'));
  const metadata = el('dl');
  for (const [label, value] of [
    ['Knowledge ID', row.id], ['Validated', formatDate(row.validated_at)],
    ['Expires', formatDate(row.expires_at)], ['Validation query', row.query || 'Not recorded'],
    ['Times recalled', String(row.uses || 0)],
  ]) metadata.append(el('dt', '', label), el('dd', '', value));
  details.append(metadata);
  const sources = el('ul', 'knowledge-sources');
  for (const source of Array.isArray(row.source_refs) ? row.source_refs : []) {
    if (!source || typeof source !== 'object') continue;
    const li = el('li');
    const url = safeSourceUrl(source.url);
    if (url) {
      const link = el('a', '', source.title || source.domain || new URL(url).hostname);
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      li.append(link, el('span', 'memory-desc', ` — ${source.domain || new URL(url).hostname}`));
    } else li.textContent = 'Source link unavailable';
    if (source.retrieved_at) li.append(el('div', 'memory-desc', `Fetched ${formatDate(source.retrieved_at)}`));
    sources.append(li);
  }
  if (!sources.childElementCount) sources.append(el('li', '', 'No source provenance recorded. Revalidate before trusting this entry.'));
  details.append(sources);
  card.append(details);
  const actions = el('div', 'knowledge-actions');
  const revalidate = el('button', 'memory-toolbar-btn', 'Revalidate');
  revalidate.type = 'button';
  revalidate.disabled = !learningEnabled;
  revalidate.title = learningEnabled ? 'Check this claim against web sources again' : 'Knowledge learning is disabled in settings';
  const remove = el('button', 'memory-toolbar-btn danger', 'Delete');
  remove.type = 'button';
  revalidate.addEventListener('click', () => mutate(row, 'revalidate', card, revalidate));
  remove.addEventListener('click', () => mutate(row, 'delete', card, remove));
  actions.append(revalidate, remove);
  card.append(actions, el('p', 'knowledge-outcome memory-desc'));
  card.querySelector('.knowledge-outcome').setAttribute('role', 'status');
  return card;
}

export async function loadKnowledge() {
  initialize();
  clearTimeout(debounce);
  controller?.abort();
  const current = new AbortController();
  controller = current;
  const list = document.getElementById('knowledge-list');
  const status = document.getElementById('knowledge-status');
  const prev = document.getElementById('knowledge-prev');
  const next = document.getElementById('knowledge-next');
  prev.disabled = next.disabled = true;
  list.setAttribute('aria-busy', 'true');
  status.textContent = 'Loading knowledge…';
  const query = new URLSearchParams({ q: document.getElementById('knowledge-search').value.trim(),
    freshness: document.getElementById('knowledge-filter').value, offset, limit: PAGE_SIZE });
  try {
    const response = await fetch(`/api/knowledge?${query}`, { credentials: 'same-origin', signal: current.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    if (current.signal.aborted) return;
    matched = data.matched;
    if (offset > 0 && offset >= matched) { offset = Math.max(0, Math.floor((matched - 1) / PAGE_SIZE) * PAGE_SIZE); return loadKnowledge(); }
    list.replaceChildren();
    document.getElementById('knowledge-stats').textContent = `${data.total} stored · ${data.fresh} fresh · ${data.expired} needing review`;
    status.textContent = data.learning_enabled ? '' : 'Knowledge learning is disabled. You can still inspect and delete entries.';
    for (const row of data.knowledge) list.append(renderCard(row, data.learning_enabled));
    if (!data.knowledge.length) list.append(el('p', 'knowledge-empty memory-desc', data.total
      ? 'No claims match these filters.'
      : 'No knowledge saved yet. A skill such as epistemic-honesty can use manage_knowledge to validate and retain reusable facts. This tab does not enable automatic learning by itself.'));
    document.getElementById('knowledge-page').textContent = matched ? `${offset + 1}–${offset + data.knowledge.length} of ${matched}` : '0 entries';
    prev.disabled = offset === 0;
    next.disabled = offset + PAGE_SIZE >= matched;
  } catch (error) {
    if (error.name !== 'AbortError') {
      status.textContent = `Could not load knowledge: ${error.message}. Use Refresh to retry.`;
      list.replaceChildren();
      document.getElementById('knowledge-stats').textContent = '';
      document.getElementById('knowledge-page').textContent = '';
    }
  } finally { if (controller === current) list.setAttribute('aria-busy', 'false'); }
}
