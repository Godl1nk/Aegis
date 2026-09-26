import test from 'node:test';
import assert from 'node:assert/strict';
import { contextPercentLabel, contextView, mergeDiscoverySnapshot, mergeStreamingSnapshot, sanitizeUsageData, sameContextSelection } from '../static/js/contextUsage.js';

test('model window changes match only the selected endpoint and model', () => {
  const selected = { sessionId: 's', model: 'm', endpointUrl: 'http://host/v1/chat/completions' };
  assert.equal(sameContextSelection(selected, 'http://host/v1', 'm'), true);
  assert.equal(sameContextSelection(selected, 'http://other/v1', 'm'), false);
  assert.equal(sameContextSelection(selected, 'http://host/v1', 'other'), false);
});

test('small nonzero usage is not displayed as zero', () => {
  assert.equal(contextPercentLabel(102 / 131072 * 100), '<1%');
  assert.equal(contextPercentLabel(0), '0%');
  assert.equal(contextPercentLabel(3.8), '4%');
});

test('context percentage and thresholds', () => {
  for (const [used, expected] of [[0, 'normal'], [699, 'normal'], [700, 'warning'], [850, 'danger'], [1200, 'danger']]) {
    const view = contextView({ used_tokens: used, context_length: 1000, context_length_known: true });
    assert.ok(Math.abs(view.percent - used / 10) < 1e-9);
    assert.equal(view.level, expected);
  }
});
test('unknown context never uses a fallback limit', () => {
  assert.equal(contextView({ used_tokens: 1000, context_length: 128000, context_length_known: false }).percent, null);
  assert.equal(contextView(null).percent, null);
});
test('draft uses local estimate and marks real count approximate', () => {
  const data = { used_tokens: 100, context_length: 1000, context_length_known: true, usage_source: 'real' };
  assert.equal(contextView(data).estimated, false);
  const view = contextView(data, 'a'.repeat(100));
  assert.equal(view.used, 134);
  assert.equal(view.estimated, true);
  assert.equal(data.used_tokens, 100);
});
test('invalid counts cannot produce NaN or a negative ring', () => {
  assert.equal(contextView({ used_tokens: -100 }).used, 0);
  assert.equal(contextView({ used_tokens: 'bad' }).used, 0);
  assert.equal(contextView({ context_length_known: true, context_length: Infinity }).percent, null);
});
test('idle discovery replaces the snapshot wholesale', () => {
  const prev = { used_tokens: 100, model: 'm', trimmed: true };
  const fresh = { used_tokens: 50, model: 'm', context_length: 1000, context_length_known: true };
  assert.deepEqual(mergeDiscoverySnapshot(prev, fresh), fresh);
});
test('busy merge keeps counts but drops stale status flags', () => {
  const prev = { used_tokens: 100, model: 'm', trimmed: true, compacted: true };
  const fresh = { used_tokens: 50, model: 'm', context_length: 1000, context_length_known: true };
  const merged = mergeDiscoverySnapshot(prev, fresh, { busy: true });
  assert.equal(merged.used_tokens, 100);
  assert.equal(merged.context_length, 1000);
  assert.equal('trimmed' in merged, false);
  assert.equal('compacted' in merged, false);
});
test('busy merge keeps freshly re-asserted flags and nulls switched-model limits', () => {
  const prev = { used_tokens: 100, model: 'm', trimmed: true };
  const fresh = { used_tokens: 50, model: 'm', context_length: 1000, context_length_known: true, trimmed: true };
  assert.equal(mergeDiscoverySnapshot(prev, fresh, { busy: true }).trimmed, true);
  const switched = mergeDiscoverySnapshot(prev, { ...fresh, model: 'n' }, { busy: true });
  assert.equal(switched.context_length, null);
  assert.equal(switched.context_length_known, false);
});
test('zero or missing counts never clobber a known value', () => {
  const prev = { used_tokens: 6500, model: 'm' };
  assert.equal(sanitizeUsageData(prev, { used_tokens: 0, model: 'm' }).used_tokens, 6500);
  assert.equal(sanitizeUsageData(prev, { used_tokens: -5, model: 'm' }).used_tokens, 6500);
  const noKey = sanitizeUsageData(prev, { model: 'm', compacted: true });
  assert.equal(noKey.used_tokens, 6500);
  assert.equal(noKey.compacted, true);
});
test('zero counts pass through with nothing known', () => {
  assert.equal(sanitizeUsageData(null, { used_tokens: 0 }).used_tokens, 0);
  assert.equal(sanitizeUsageData({ used_tokens: 0 }, { used_tokens: 0 }).used_tokens, 0);
});
test('positive counts always replace', () => {
  const prev = { used_tokens: 6500, model: 'm' };
  assert.equal(sanitizeUsageData(prev, { used_tokens: 6600, model: 'm' }).used_tokens, 6600);
  assert.equal(sanitizeUsageData(null, { used_tokens: 100, model: 'm' }).used_tokens, 100);
});

test('streaming model switch drops the previous model window', () => {
  const fallback = { model: 'fallback', used_tokens: 5057, context_length: 32768, context_length_known: true };
  const selected = mergeStreamingSnapshot(fallback, { model: 'qwen', used_tokens: 6000, basis: 'request' });
  assert.equal(selected.model, 'qwen');
  assert.equal(selected.context_length, null);
  assert.equal(selected.context_length_known, false);
  assert.equal(contextView(selected).percent, null);
  const discovered = mergeStreamingSnapshot(selected, { model: 'qwen', context_length: 131072, context_length_known: true });
  assert.equal(discovered.context_length, 131072);
});
