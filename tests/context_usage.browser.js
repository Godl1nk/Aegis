import { setContextSession, updateContextUsage } from '/static/js/contextUsage.js';

document.getElementById('app-loader')?.remove();
document.querySelector('.chat-input-bottom').style.visibility = 'visible';
document.querySelector('.chat-input-bar').style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);width:calc(100% - 32px);box-sizing:border-box';
const button = document.getElementById('composer-context');
const input = document.getElementById('message');
const bar = document.createElement('div');
bar.style.cssText = 'position:fixed;top:12px;left:16px;right:16px;display:flex;flex-wrap:wrap;gap:8px;z-index:9999';
const status = document.createElement('div');
status.setAttribute('role', 'status');
status.id = 'context-qa-status';
let sessionId = 'known';
function select(id) {
  sessionId = id;
  setContextSession({ sessionId: id, model: 'Qwen-QA', endpointUrl: 'http://offline' });
}
const waitFor = async predicate => {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  throw new Error('UI update timed out');
};
const assert = (condition, message) => { if (!condition) throw new Error(message); };
async function checks() {
  status.textContent = 'Running…';
  select('known');
  await waitFor(() => button.textContent.includes('20%'));
  input.value = 'a'.repeat(1000);
  input.dispatchEvent(new Event('input'));
  assert(button.textContent.includes('23%'), 'Draft not reflected');
  input.value = '';
  input.dispatchEvent(new Event('input'));
  updateContextUsage('different', {used_tokens: 9999});
  assert(button.textContent.includes('20%'), 'Background chat contaminated meter');
  updateContextUsage('known', {used_tokens: 7500, basis: 'request', usage_source: 'real', model: 'Qwen-QA'});
  assert(button.dataset.level === 'warning', 'Amber warning missing');
  updateContextUsage('known', {used_tokens: 9500});
  assert(button.dataset.level === 'danger', 'Red warning missing');
  select('slow');
  select('unknown');
  await waitFor(() => button.getAttribute('aria-label').includes('limit unknown'));
  await new Promise(resolve => setTimeout(resolve, 350));
  assert(button.textContent.trim() === '—', 'Stale response contaminated unknown model');
  select('known');
  updateContextUsage('known', {used_tokens: 8800, basis: 'request', usage_source: 'real'});
  await waitFor(() => button.textContent.includes('88%'));
  assert(button.textContent.includes('88%'), 'Discovery overwrote live usage');
  select('compacted');
  await waitFor(() => button.textContent.includes('10%'));
  button.dispatchEvent(new MouseEvent('mouseenter'));
  await waitFor(() => document.getElementById('composer-context-details'));
  assert(document.getElementById('composer-context-details').textContent.includes('Earlier messages have been compacted'), 'Compaction status missing');
  button.dispatchEvent(new MouseEvent('mouseleave'));
  await waitFor(() => !document.getElementById('composer-context-details'));
  assert(!document.getElementById('composer-context-details'), 'Mouse-out did not close hover details');
  button.dispatchEvent(new PointerEvent('pointerdown'));
  button.click();
  await new Promise(resolve => setTimeout(resolve, 220));
  assert(!document.getElementById('composer-context-details'), 'Click must not open context details');
  document.dispatchEvent(new KeyboardEvent('keydown', {key:'Tab'}));
  button.focus();
  await waitFor(() => document.getElementById('composer-context-details'));
  assert(document.getElementById('composer-context-details').getAttribute('role') === 'tooltip', 'Hover details need tooltip semantics');
  document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}));
  assert(!document.getElementById('composer-context-details'), 'Escape failed');
  button.blur();
  select('fallback');
  await waitFor(() => button.textContent.includes('4%'));
  assert(button.getAttribute('aria-label').includes('Last request (fallback model): 5,057 tokens'), 'Fallback request label missing');
  button.dispatchEvent(new MouseEvent('mouseenter'));
  await waitFor(() => document.getElementById('composer-context-details'));
  const details = document.getElementById('composer-context-details').textContent;
  assert(details.includes('A fallback model answered (Qwen-QA-fallback)'), 'Fallback model not explained');
  assert(!details.includes('tokens remaining'), 'History must not claim exact remaining capacity');
  document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}));
  history.replaceState(null, '', '#6c5db906-46c7-465d-bc28-1f0f031e8f4f');
  setContextSession({sessionId: null, model: 'Qwen-QA', endpointUrl: 'http://offline'});
  assert(button.getAttribute('aria-label') === 'Loading chat context…', 'Restoring chat shown as new');
  history.replaceState(null, '', location.pathname);
  setContextSession(null);
  assert(button.getAttribute('aria-label') === 'Context usage available after starting a chat', 'New chat retained old context');
  select('known');
  await waitFor(() => button.textContent.includes('20%'));
  status.textContent = 'PASS: hover, mouse-out, no-click, keyboard focus, Escape, draft, warnings, isolation, races, unknown limit, compaction, fallback context, new chat';
}
for (const [label, action] of [
  ['Run checks', checks], ['Known model', () => select('known')], ['Unknown limit', () => select('unknown')],
  ['85% usage', () => updateContextUsage(sessionId, {used_tokens:8500, basis:'request', usage_source:'real'})],
  ['Compacted history', () => select('compacted')],
  ['Narrow composer', () => { document.querySelector('.chat-input-bar').style.maxWidth = '360px'; }],
]) {
  const control = document.createElement('button');
  control.textContent = label;
  control.addEventListener('click', () => Promise.resolve().then(action).catch(e => { status.textContent = `FAIL: ${e.message}`; }));
  bar.appendChild(control);
}
bar.appendChild(status);
document.body.appendChild(bar);
select('known');
