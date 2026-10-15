import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Run the real editor functions with a small DOM/fetch harness. The backend has
// already saved doc_update content; merely viewing or closing it must not undo it.
const source = readFileSync(new URL('../static/js/document.js', import.meta.url), 'utf8');
function functionSource(name) {
  const match = source.match(new RegExp(`^  (?:export )?(?:async )?function ${name}\\(`, 'm'));
  assert.ok(match, `missing ${name}`);
  const end = source.indexOf('\n  }', match.index) + '\n  }'.length;
  return source.slice(match.index, end).replace('export ', '');
}

function harness(content = 'Week 6:\nBlank', language = 'text') {
  const element = (value = '') => ({
    value, style: {}, dataset: {}, classList: { add() {}, remove() {} },
    addEventListener() {}, remove() {}, scrollHeight: 0,
  });
  const textarea = element(content);
  const frame = element();
  let frameLoads = 0;
  Object.defineProperty(frame, 'src', {
    set(value) { this.url = value; frameLoads++; }, get() { return this.url; },
  });
  const wordPane = element();
  wordPane.querySelector = () => frame;
  const elements = new Map([
    ['doc-editor-textarea', textarea], ['doc-editor-code', element()],
    ['doc-editor-wrap', element()], ['doc-word-view', wordPane],
    ['doc-title-input', element('Logbook')], ['doc-language-select', element(language)],
    ['doc-version-badge', element()],
  ]);
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const ctx = {
    document: { getElementById: id => elements.get(id) || null, querySelectorAll: () => [] },
    docs: new Map([['doc-a', { id: 'doc-a', content, language, title: 'Logbook', version: 3 }]]),
    activeDocId: 'doc-a', API_BASE: '', uiModule: { showToast() {}, showError() {} },
    sessionModule: { getCurrentSessionId: () => '' },
    _diffModeActive: false, _diffOldContent: null, _diffNewContent: null,
    _diffChunks: [], _diffUnresolvedCount: 0, _lastAutoSaveErrorAt: 0,
    _coalesceDiffBaseline: null, _coalesceDiffDocId: null, _coalesceDiffTimer: null,
    _pdfViewState: new Map(), DIFF_MODE_THRESHOLD: 1,
    streamDocFinalize: () => null, _isHtmlPreviewVisible: () => false,
    _isMarkdownPreviewVisible: () => false, _parseEmailHeader: () => ({}),
    _syncDocIndicator() {}, _ensureDocPaneMounted() {}, _resetTransientDocViews() {},
    syncHighlighting() {}, updateLineNumbers() {}, renderTabs() {},
    _renderDiffOverlay() { textarea.value = 'INTERLEAVED OLD AND NEW DISPLAY'; },
    _renderDiffToolbar() {}, _renderDiffGutter() {}, _scrollToDiffChunk() {},
    _updateDiffStatus() {}, _setMarkdownPreviewActive() {},
    _refreshPdfPreviewIframe() { ctx.pdfRefreshes++; }, pdfRefreshes: 0,
    closePanel() { ctx.exitDiffMode(true); },
    requestAnimationFrame: callback => callback(),
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      requests.push({ url, ...JSON.parse(options.body) });
      return { ok: true, status: 200, json: async () => ({ version_count: 4 }) };
    },
    console,
  };
  vm.createContext(ctx);
  const functions = [
    '_isFormBackedDoc', '_isWordBackedDoc', '_computeLineDiff', '_buildDiffChunks',
    'enterDiffMode', '_getReviewedContent', '_applyResolvedChunksToTextarea',
    '_resolveAllChunks', 'exitDiffMode', 'saveCurrentToMap', 'saveDocument',
    '_syncHeaderActions', 'handleDocUpdate', '_detachDocFromSession', 'closeTab',
  ];
  for (const name of functions) vm.runInContext(functionSource(name), ctx);
  return { ctx, textarea, frame, elements, requests, frameLoads: () => frameLoads,
    flushTimers() {
      const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn());
    },
    update(next, version = 4) {
      ctx.handleDocUpdate({ doc_id: 'doc-a', content: next, version, title: 'Logbook', language });
    },
  };
}

test('closing a comparison retains saved edits and sends no rollback', () => {
  const h = harness();
  const filled = 'Week 6:\nCompleted CML markup';
  h.ctx.docs.get('doc-a').content = filled;
  h.ctx.enterDiffMode(h.textarea.value, filled);
  h.ctx.exitDiffMode(true);
  h.ctx.saveCurrentToMap();
  assert.equal(h.textarea.value, filled);
  assert.equal(h.ctx.docs.get('doc-a').content, filled);
  assert.equal(h.requests.length, 0);
});

test('saving and closing during review never persists interleaved diff text', async () => {
  const h = harness();
  const filled = 'Week 6:\nCompleted CML markup';
  h.ctx.enterDiffMode(h.textarea.value, filled);
  await h.ctx.saveDocument({ silent: true });
  assert.equal(h.requests[0].content, filled);
  await h.ctx.closeTab('doc-a');
  assert.equal(h.requests[1].content, filled);
  assert.equal(h.ctx._diffModeActive, false);
});

test('rejecting one change retains the other already-saved changes', () => {
  const h = harness();
  h.ctx.enterDiffMode('Blank A\nUnchanged\nBlank B', 'Filled A\nUnchanged\nFilled B');
  assert.equal(h.ctx._diffChunks.length, 2);
  h.ctx._diffChunks[0].resolved = true;
  h.ctx._diffChunks[0].accepted = false;
  h.ctx.exitDiffMode(true);
  assert.equal(h.textarea.value, 'Blank A\nUnchanged\nFilled B');
  assert.equal(h.requests.length, 0);
});

test('explicit Reject All still restores and saves the previous content', () => {
  const h = harness();
  const blank = h.textarea.value;
  h.ctx.enterDiffMode(blank, 'Week 6:\nFilled');
  h.ctx._resolveAllChunks(false);
  assert.equal(h.textarea.value, blank);
  assert.equal(h.requests[0].content, blank);
});

test('a second AI update dismisses the first review without writing its baseline', () => {
  const h = harness();
  h.update('Week 6:\nFirst fill');
  h.flushTimers();
  assert.equal(h.ctx._diffModeActive, true);
  h.update('Week 6:\nFinal fill', 5);
  h.flushTimers();
  h.ctx.exitDiffMode(true);
  assert.equal(h.textarea.value, 'Week 6:\nFinal fill');
  assert.equal(h.requests.length, 0);
});

test('Word AI fills refresh the same file preview and bypass hidden diff review', () => {
  const marker = '<!-- word_source upload_id="logbook.docx" -->\n';
  const h = harness(marker + 'Week 6:\nBlank', 'docx');
  h.ctx._syncHeaderActions();
  assert.equal(h.frameLoads(), 1);
  const filled = marker + 'Week 6:\nCompleted CML markup';
  h.update(filled);
  h.flushTimers();
  assert.equal(h.ctx._diffModeActive, false);
  assert.equal(h.textarea.value, filled);
  assert.equal(h.frameLoads(), 2);
  assert.equal(h.frame.dataset.version, '4');
  h.ctx._syncHeaderActions();
  assert.equal(h.frameLoads(), 2, 'unchanged versions must not continually reload');
  h.ctx.saveCurrentToMap();
  assert.equal(h.ctx.docs.get('doc-a').content, filled);
  assert.equal(h.requests.length, 0);
});

test('PDF AI fills update source and rendered view without a hidden review', () => {
  const marker = '<!-- pdf_form_source upload_id="form.pdf" -->\n';
  const h = harness(marker + 'Name: ', 'markdown');
  const filled = marker + 'Name: Example';
  h.update(filled);
  h.flushTimers();
  assert.equal(h.ctx._diffModeActive, false);
  assert.equal(h.textarea.value, filled);
  assert.equal(h.ctx.pdfRefreshes, 1);
  assert.equal(h.requests.length, 0);
});

test('late save responses cannot overwrite a newer AI version', async () => {
  const h = harness();
  let finishSave;
  h.ctx.fetch = () => new Promise(resolve => { finishSave = resolve; });
  const pending = h.ctx.saveDocument({ silent: true });
  h.update('Week 6:\nNewer AI fill', 5);
  finishSave({ ok: true, status: 200, json: async () => ({ version_count: 4 }) });
  await pending;
  assert.equal(h.ctx.docs.get('doc-a').content, 'Week 6:\nNewer AI fill');
  assert.equal(h.ctx.docs.get('doc-a').version, 5);
  assert.equal(h.elements.get('doc-version-badge').textContent, 'v5');
});
