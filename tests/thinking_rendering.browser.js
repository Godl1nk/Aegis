// Thinking-block extraction/rendering regression fixture.
// Run headless against the repo (python http server + --dump-dom) or open
// via a served page; #qa-status shows PASS or the first FAIL.
// Covers the multi-round leak class: short per-round thinking must never eat
// the reply that follows it, and fenced code must stay opaque to parsing.
import * as md from '/static/js/markdown.js';

const results = [];
function assert(condition, label) {
  if (!condition) throw new Error(label);
  results.push(label);
}

const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();

function checkSplit(name, input, wantThinking, wantContent) {
  const out = md.extractThinkingBlocks(input);
  assert(norm(out.thinkingBlocks.join('\n')) === norm(wantThinking),
    `${name}: thinking is ${JSON.stringify(out.thinkingBlocks)}`);
  assert(norm(out.content) === norm(wantContent),
    `${name}: content is ${JSON.stringify(out.content)}`);
  const html = md.processWithThinking(input);
  assert(!/<(\/)?think(ing)?[\s>]/.test(html), `${name}: no raw think tag leaks`);
  if (norm(wantThinking)) {
    assert(html.includes('thinking-section'), `${name}: thinking renders collapsed`);
  }
}

function check() {
  results.length = 0;
  // The continuous-tool-call shape: brief per-round thinking + reply, twice.
  checkSplit('multi-closed',
    '<think>A</think>\n\nR1 text\n\n<think>B</think>\n\nR2 text',
    'A\n\nB', 'R1 text\n\nR2 text');
  checkSplit('three-blocks',
    '<think>a</think>\n\nX\n\n<think>b</think>\n\nY\n\n<think>c</think>\n\nZ',
    'a\n\nb\n\nc', 'X\n\nY\n\nZ');
  checkSplit('short-think-round',
    '<think>Checking.</think>\n\nHere are the results:\n\n- one\n- two',
    'Checking.', 'Here are the results:\n\n- one\n- two');
  checkSplit('consecutive-blocks',
    '<think>first</think><think>second</think>\n\nReply',
    'first\n\nsecond', 'Reply');
  // Untagged thinking closed later still merges (orphan rule, not the old
  // short-block collapse).
  checkSplit('untagged-between-closes',
    '<think>ok</think>\nSome more thinking here\nand here</think>\n\nFinal reply',
    'ok\n\nSome more thinking here\nand here', 'Final reply');
  // Fences are opaque: tags inside stay literal, fences survive intact.
  checkSplit('think-in-fence',
    '```\n<think>not really thinking</think>\n```\n\nReply',
    '', '```\n<think>not really thinking</think>\n```\n\nReply');
  checkSplit('think-around-fence',
    '<think>see code:\n```\nx = 1\n```\nmore thought</think>\n\nReply',
    'see code:\n```\nx = 1\n```\nmore thought', 'Reply');
  checkSplit('inline-code-think',
    'Use `<think>` tags for reasoning.\n\nReply',
    '', 'Use `<think>` tags for reasoning.\n\nReply');
  // Orphaned close still rescues leaked thinking.
  checkSplit('orphan-close',
    'leaked reasoning here</think>\n\nThe reply.',
    'leaked reasoning here', 'The reply.');
  // Deliberate tradeoff, pinned so it is never "fixed" blindly: an unclosed
  // opener at the start is shown as the reply. Quantized models emit a
  // literal <think> on every reply without closing; treating it as thinking
  // would hide answers (or blank the bubble). Interrupted turns therefore
  // stay visible as text.
  checkSplit('unclosed-stays-visible',
    '<think>partial reasoning that got cut off',
    '', 'partial reasoning that got cut off');
  return `PASS (${results.length} assertions)`;
}

document.addEventListener('DOMContentLoaded', () => {
  const bar = document.createElement('div');
  bar.id = 'qa-controls';
  bar.style.cssText = 'position:fixed;top:8px;right:12px;z-index:99999;padding:8px;background:var(--panel);border:1px solid var(--border);display:flex;gap:8px';
  const button = document.createElement('button');
  button.textContent = 'Run thinking assertions';
  const status = document.createElement('span');
  status.id = 'qa-status';
  status.setAttribute('role', 'status');
  const run = () => {
    try { status.textContent = check(); }
    catch (error) { status.textContent = `FAIL: ${error.message}`; }
  };
  button.addEventListener('click', run);
  bar.append(button, status);
  document.body.appendChild(bar);
  run();
});
