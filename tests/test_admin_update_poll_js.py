"""Exercise the real updater polling function with deterministic status replies."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is unavailable")
def test_apply_poll_ignores_previous_attempt_results():
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/js/admin.js', 'utf8');
const poll = source.slice(source.indexOf('async function _pollApplyDone('),
                          source.indexOf('async function _pollRebuildDone('));
(async () => {
  for (const oldOk of [false, true]) {
    for (const newOk of [false, true]) {
      let reads = 0;
      const context = {
        setTimeout: fn => fn(),
        fetch: async () => ({ok: true, json: async () => ({last_apply:
          ++reads === 1
            ? {commit: 'same-sha', attempt_id: 'old', ok: oldOk, error: 'old failure'}
            : {commit: 'same-sha', attempt_id: 'new', ok: newOk,
               result: {applied: true}, error: 'new failure'},
        })}),
      };
      vm.createContext(context);
      vm.runInContext(poll, context);
      const result = await context._pollApplyDone('same-sha', 'new',
                                                {dismissed: false, sub() {}});
      assert.equal(reads, 2);
      assert.equal(result.outcome, newOk ? 'result' : 'error');
      if (newOk) assert.equal(result.result.applied, true);
      else assert.equal(result.error, 'new failure');
    }
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", script],
                            cwd=Path(__file__).resolve().parent.parent,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
