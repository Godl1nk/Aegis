// Agent Activity open-state QA: expanded by default, explicit toggles persist
// per message across reloads. Run headless against the repo (python http
// server + --dump-dom) or open via a served page; #qa-status shows PASS/FAIL.
import { createAgentTurn, readActivityOpen } from '/static/js/agentTurn.js';

const results = [];
function assert(condition, label) {
  if (!condition) throw new Error(label);
  results.push(label);
}

function makeTurn(dbId) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const turn = createAgentTurn(host);
  if (dbId) turn.finish({ dbId });
  return turn;
}

// The toggle event dispatches asynchronously after summary.click(), so
// storage assertions must yield first — otherwise they race the handler.
const nextTask = () => new Promise((resolve) => setTimeout(resolve, 0));

async function check() {
  results.length = 0;
  localStorage.clear();

  // 1. Fresh turns start showing.
  const t1 = makeTurn('qa-turn-1');
  assert(t1.root.querySelector('.agent-turn-activity').open === true,
    'fresh turn activity starts open');

  // 2. A real user collapse persists; a re-created turn restores it.
  t1.root.querySelector('.agent-turn-activity > summary').click();
  assert(t1.root.querySelector('.agent-turn-activity').open === false,
    'click collapses');
  await nextTask();
  assert(readActivityOpen('qa-turn-1') === false, 'collapse is stored');
  t1.root.remove();
  const t1b = makeTurn('qa-turn-1');
  assert(t1b.root.querySelector('.agent-turn-activity').open === false,
    'collapsed turn stays collapsed on re-render');
  t1b.root.remove();

  // 3. Other turns are unaffected by a sibling's stored state.
  const t2 = makeTurn('qa-turn-2');
  assert(t2.root.querySelector('.agent-turn-activity').open === true,
    'sibling turn still starts open');
  t2.root.remove();

  // 4. Re-opening stores too.
  const t3 = makeTurn('qa-turn-3');
  const act3 = t3.root.querySelector('.agent-turn-activity');
  act3.open = false;
  act3.querySelector('summary').click();
  assert(act3.open === true, 'click re-opens');
  await nextTask();
  assert(readActivityOpen('qa-turn-3') === true, 're-open is stored');
  t3.root.remove();

  // 5. A mid-stream toggle before any dbId never writes junk keys.
  const t4 = createAgentTurn(document.body);
  t4.root.querySelector('.agent-turn-activity > summary').click();
  await nextTask();
  assert(readActivityOpen('') === null && readActivityOpen(null) === null,
    'toggle without dbId stores nothing');
  t4.root.remove();

  localStorage.clear();
  return `PASS (${results.length} assertions)`;
}

document.addEventListener('DOMContentLoaded', () => {
  const bar = document.createElement('div');
  bar.id = 'qa-controls';
  bar.style.cssText = 'position:fixed;top:8px;right:12px;z-index:99999;padding:8px;background:var(--panel);border:1px solid var(--border);display:flex;gap:8px';
  const button = document.createElement('button');
  button.textContent = 'Run activity assertions';
  const status = document.createElement('span');
  status.id = 'qa-status';
  status.setAttribute('role', 'status');
  const run = async () => {
    try { status.textContent = await check(); }
    catch (error) { status.textContent = `FAIL: ${error.message}`; }
  };
  button.addEventListener('click', run);
  bar.append(button, status);
  document.body.appendChild(bar);
  run();
});
