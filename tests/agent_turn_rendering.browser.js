// Browser regression fixture: served by browser_agent_turn_server.py.
// Runs real chatRenderer and chat streaming code against offline SSE fixtures.
import renderer from '/static/js/chatRenderer.js';
import chat from '/static/js/chat.js';
import sessions from '/static/js/sessions.js';
import { mergeAgentTurnActivity } from '/static/js/agentTurn.js';
import { setContextSession } from '/static/js/contextUsage.js';

const session = { id: 'agent-turn-qa', model: 'Qwen-QA', endpoint_url: 'http://offline-fixture', history: [] };
sessions.getCurrentSessionId = () => session.id;
sessions.getSessions = () => [session];
sessions.getCurrentModel = () => session.model;
sessions.loadSessions = async () => {};
sessions.selectSession = async () => {};
window.sessionModule = sessions;
window.chatModule = chat;
const events = Array.from({ length: 4 }, (_, index) => ({
  round: index + 1, tool: 'web_search', command: `Search fixture ${index + 1}`,
  output: index === 1 ? 'Offline fixture failure' : 'Fixture source verified', exit_code: index === 1 ? 1 : 0,
}));
const rounds = events.map((_, i) => `<think>Checking fixture ${i + 1}.</think>Intermediate update ${i + 1}.`);
const finalText = 'Final answer: all four source checks are complete.';
const meta = { _db_id: 'qa-response', model: 'Qwen-QA', timestamp: '2026-08-27T03:50:00Z',
  tool_events: events, round_texts: [...rounds, finalText],
  web_sources: [{ title: 'Fixture source', url: 'https://example.org/source' }],
  memories_used: [{ type: 'recalled', text: 'A fixture recall' }],
};
const results = [];

function assert(condition, label) {
  if (!condition) throw new Error(label);
  results.push(label);
}

function restore() {
  const box = document.getElementById('chat-history');
  box.replaceChildren();
  renderer.addMessage('user', 'QA question: check four sources.', null, { _db_id: 'qa-user' });
  return renderer.addMessage('assistant', finalText, 'Qwen-QA', meta);
}

function checkHistory() {
  results.length = 0;
  const card = restore();
  const box = document.getElementById('chat-history');
  assert(box.querySelectorAll('.msg').length === 2, 'One user message and one assistant message');
  assert(card.classList.contains('agent-turn'), 'History returns the response card');
  assert(card.dataset.dbId === 'qa-response', 'Persisted message ID belongs to the response card');
  assert(card.querySelectorAll('.msg-footer').length === 1, 'One footer for all tool rounds');
  assert(card.querySelectorAll('.agent-thread-node').length === 4, 'All tool calls retained');
  assert(card.querySelector('.agent-turn-activity').open, 'Activity stays showing by default');
  assert(card.querySelector('.agent-turn-activity > summary').textContent.includes('1 failed'), 'Failure count remains visible');
  assert(card.querySelector('.body').textContent.includes(finalText), 'Final answer stays outside Activity');
  assert(!card.querySelector('.body').textContent.includes('Intermediate update'), 'Intermediate prose stays in Activity');
  assert(card.querySelector('.agent-turn-steps').textContent.includes('Intermediate update 1'), 'Intermediate prose is not discarded');
  assert(card.querySelector('.body .sources-section'), 'Final answer retains its sources');
  assert(renderer.copyMessageText(card) === finalText, 'Copy uses the answer, not hidden tool output');
  let actionTarget;
  const realDelete = chat.deleteMessage;
  chat.deleteMessage = node => { actionTarget = node; };
  card.querySelector('[data-action="delete"]').click();
  chat.deleteMessage = realDelete;
  assert(actionTarget === card, 'Message action targets the whole response');

  renderer.addMessage('user', 'Second independent question');
  renderer.addMessage('assistant', 'Second final answer', 'Qwen-QA', { ...meta, _db_id: 'qa-response-2', round_texts: ['Second final answer'], tool_events: [{round: 2, tool: 'web_fetch', exit_code: 0}] });
  assert(box.querySelectorAll('.agent-turn').length === 2, 'Different questions never merge');
  assert(box.querySelectorAll('.msg').length === 4, 'Tool rounds do not affect message indexing');

  box.replaceChildren();
  const legacy = renderer.addMessage('assistant', 'Legacy saved answer', 'Qwen-QA', { tool_events: events });
  assert(legacy.querySelector('.body').textContent.includes('Legacy saved answer'), 'Legacy traces without round text keep the answer');
  box.replaceChildren();
  const toolOnly = renderer.addMessage('assistant', '', 'Qwen-QA', { _db_id: 'tool-only', tool_events: events });
  assert(toolOnly.querySelector('.msg-footer'), 'Tool-only responses have message actions');
  assert(toolOnly.querySelector('.role').textContent.includes('Qwen-QA'), 'Tool-only responses have a model label');
  box.replaceChildren();
  const documentTurn = renderer.addMessage('assistant', '', 'Qwen-QA', { tool_events: [
    { round: 1, tool: 'create_document', exit_code: 0, doc_id: 'qa-doc', doc_title: 'Fixture document' },
  ] });
  assert(documentTurn.querySelector('.agent-turn-answer .agent-doc-open-btn'), 'Document remains accessible outside Activity');
  box.replaceChildren();
  const partial = renderer.addMessage('assistant', 'Partial answer', 'Qwen-QA', { _db_id: 'qa-partial' });
  const continuation = renderer.addMessage('assistant', finalText, 'Qwen-QA', meta);
  const merged = mergeAgentTurnActivity(partial, continuation);
  continuation.remove();
  assert(merged.dataset.dbId === 'qa-partial', 'Continue preserves the original message ID');
  assert(merged.dataset.raw === 'Partial answer', 'Grouping a plain continuation preserves its raw text');
  assert(merged.querySelectorAll('.agent-thread-node').length === 4, 'Continue retains the new tool transcript');
  assert(box.querySelectorAll('.msg').length === 1, 'Continue does not create nested messages');
  assert(merged.querySelectorAll('.msg-footer').length === 1, 'Continue keeps one footer');
  restore();
  document.getElementById('qa-status').textContent = `${results.length} assertions passed`;
}

async function stream(message) {
  if (message != null) document.getElementById('message').value = message;
  const box = document.getElementById('chat-history');
  const before = box.querySelectorAll('.msg').length;
  let maxMessages = before;
  let hiddenApproval = false;
  const observer = new MutationObserver(() => {
    maxMessages = Math.max(maxMessages, box.querySelectorAll('.msg').length);
    hiddenApproval ||= !!box.querySelector('.agent-turn-activity .approval-card');
  });
  observer.observe(box, { childList: true, subtree: true });
  document.getElementById('qa-status').textContent = 'Streaming offline fixture…';
  try {
    await chat.handleChatSubmit(new Event('submit', { cancelable: true }));
  } finally {
    observer.disconnect();
  }
  const card = box.querySelector('.agent-turn:last-child');
  assert(maxMessages <= before + 2, 'Streaming never inserts a message per tool round');
  assert(card, 'Live response has one grouped card');
  assert(card.querySelectorAll('.msg').length === 0, 'Live card contains no nested messages');
  assert(card.querySelectorAll('.msg-footer').length === 1, 'Live card has one footer');
  assert(!hiddenApproval, 'Approval never appears inside collapsed Activity');
  if (card.querySelector('.stopped-indicator')) {
    assert(card.querySelector('.agent-turn-answer .stopped-indicator'), 'Stop indicator remains outside Activity');
    assert(card.querySelectorAll('.stopped-indicator').length === 1, 'Stop is rendered once');
    document.getElementById('qa-status').textContent = 'Stop assertions passed';
  } else {
    assert(card.querySelector('.body').textContent.includes(finalText), 'Live final answer remains outside Activity');
    assert(card.querySelectorAll('.agent-thread-node').length >= 4, 'Live tool transcript is retained');
    if (!card.dataset.raw.includes('*(continued)*')) {
      assert(renderer.copyMessageText(card) === finalText, 'Live copy excludes intermediate updates and reasoning');
    }
    document.getElementById('qa-status').textContent = 'Stream assertions passed';
    assert(document.getElementById('composer-context').textContent.includes('85%'), 'Composer uses last request and output, not cumulative billing');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('app-loader')?.remove();
  chat.init('');
  chat.initListeners();
  document.querySelector('.chat-input-bottom').style.visibility = 'visible';
  setContextSession({sessionId: session.id, model: session.model, endpointUrl: session.endpoint_url});
  document.getElementById('chat-form').addEventListener('submit', event => {
    event.preventDefault();
    stream(null).catch(error => { document.getElementById('qa-status').textContent = `FAIL: ${error.message}`; });
  });
  // Keep the offline QA toolbar separate from the application UI.
  const bar = document.createElement('div');
  bar.id = 'qa-controls';
  bar.style.cssText = 'position:fixed;top:8px;right:12px;z-index:99999;padding:8px;background:var(--panel);border:1px solid var(--border);display:flex;gap:8px';
  for (const [label, action] of [
    ['Run history assertions', checkHistory], ['Restore saved turn', restore],
    ['Stream four rounds', () => stream('QA streaming question')],
    ['Stream approval fixture', () => stream('QA approval question')],
    ['Stop fixture', () => chat.handleChatSubmit(new Event('submit', { cancelable: true }))],
    ['Clear test chat', () => document.getElementById('chat-history').replaceChildren()],
  ]) {
    const button = document.createElement('button');
    button.textContent = label;
    button.addEventListener('click', async () => {
      try { await action(); } catch (error) { document.getElementById('qa-status').textContent = `FAIL: ${error.message}`; }
    });
    bar.appendChild(button);
  }
  const status = document.createElement('span');
  status.id = 'qa-status';
  status.setAttribute('role', 'status');
  bar.appendChild(status);
  document.body.appendChild(bar);
  try { checkHistory(); } catch (error) { status.textContent = `FAIL: ${error.message}`; }
});
