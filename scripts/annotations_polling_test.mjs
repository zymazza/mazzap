import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadAnnotationsTestApi() {
  const source = fs.readFileSync(new URL('../public/annotations.js', import.meta.url), 'utf8');
  const window = {};
  vm.runInNewContext(source, { window });
  return window.VEILAnnotations?._test;
}

function createEventTargetStub(initial = {}) {
  const listeners = new Map();
  const removed = [];
  return {
    ...initial,
    listeners,
    removed,
    addEventListener(type, handler) {
      listeners.set(type, handler);
    },
    removeEventListener(type, handler) {
      removed.push({ type, handler });
      if (listeners.get(type) === handler) listeners.delete(type);
    },
  };
}

test('annotation content signature is stable and changes with content', () => {
  const api = loadAnnotationsTestApi();
  const first = '{"annotations":[{"type":"point","x":1,"y":2}]}';
  const second = '{"annotations":[{"type":"point","x":1,"y":3}]}';

  assert.equal(api.annotationContentSignature(first), api.annotationContentSignature(first));
  assert.notEqual(api.annotationContentSignature(first), api.annotationContentSignature(second));
  assert.match(api.annotationContentSignature(first), /^\d+:[0-9a-z]+$/);
});

test('plan directive signature distinguishes a live preview from its clear event', () => {
  const api = loadAnnotationsTestApi();
  const preview = { plan_id: 'plan_one', revision_id: 'rev_one', view: 'difference' };

  assert.equal(api.planDirectiveSignature(null), 'none');
  assert.equal(api.planDirectiveSignature(preview), api.planDirectiveSignature({ ...preview }));
  assert.notEqual(api.planDirectiveSignature(preview), api.planDirectiveSignature(null));
  assert.notEqual(
    api.planDirectiveSignature(preview),
    api.planDirectiveSignature({ ...preview, revision_id: 'rev_two' }),
  );
});

test('an existing plan directive is applied on the first viewer read', async () => {
  const api = loadAnnotationsTestApi();
  const preview = { plan_id: 'plan_one', proposal_id: 'proposal_one', view: 'difference' };
  const calls = [];
  const planApi = { async applyDirective(value) { calls.push(value); } };

  assert.equal(await api.applyPlanDirectiveIfChanged(planApi, preview, true), true);
  assert.deepEqual(calls, [preview]);
  assert.equal(await api.applyPlanDirectiveIfChanged(planApi, preview, false), false);
  assert.deepEqual(calls, [preview]);
});

test('coalesced async gate prevents overlapping refresh work', async () => {
  const api = loadAnnotationsTestApi();
  const calls = [];
  let active = 0;
  let maxActive = 0;
  let releaseFirst;

  const gate = api.createCoalescedAsyncGate(async (label) => {
    calls.push(label);
    active += 1;
    maxActive = Math.max(maxActive, active);
    if (label === 'first') {
      await new Promise((resolve) => {
        releaseFirst = resolve;
      });
    }
    active -= 1;
  });

  const first = gate('first');
  const second = gate('second');

  assert.deepEqual(calls, ['first']);
  releaseFirst();
  await Promise.all([first, second]);

  assert.deepEqual(calls, ['first', 'second']);
  assert.equal(maxActive, 1);
});

test('polling lifecycle pauses while hidden and refreshes when visible again', () => {
  const api = loadAnnotationsTestApi();
  const doc = createEventTargetStub({ hidden: false });
  const win = createEventTargetStub();
  const intervals = [];
  const cleared = [];
  let refreshes = 0;

  const polling = api.createPollingLifecycle({
    pollMs: 4000,
    refresh: () => {
      refreshes += 1;
    },
    doc,
    win,
    setIntervalFn: (fn, ms) => {
      const handle = { fn, ms };
      intervals.push(handle);
      return handle;
    },
    clearIntervalFn: (handle) => {
      cleared.push(handle);
    },
  });

  polling.start();
  assert.equal(polling.state.polling, true);
  assert.equal(intervals.length, 1);
  assert.equal(intervals[0].ms, 4000);

  intervals[0].fn();
  assert.equal(refreshes, 1);

  doc.hidden = true;
  doc.listeners.get('visibilitychange')();
  assert.equal(polling.state.polling, false);
  assert.deepEqual(cleared, [intervals[0]]);

  intervals[0].fn();
  assert.equal(refreshes, 1);

  doc.hidden = false;
  doc.listeners.get('visibilitychange')();
  assert.equal(polling.state.polling, true);
  assert.equal(intervals.length, 2);
  assert.equal(refreshes, 2);
});

test('polling lifecycle clears interval on pagehide, beforeunload, and destroy', () => {
  const api = loadAnnotationsTestApi();
  const doc = createEventTargetStub({ hidden: false });
  const win = createEventTargetStub();
  const intervals = [];
  const cleared = [];

  const polling = api.createPollingLifecycle({
    pollMs: 4000,
    refresh: () => {},
    doc,
    win,
    setIntervalFn: (fn, ms) => {
      const handle = { fn, ms };
      intervals.push(handle);
      return handle;
    },
    clearIntervalFn: (handle) => {
      cleared.push(handle);
    },
  });

  polling.start();
  win.listeners.get('pagehide')();
  assert.deepEqual(cleared, [intervals[0]]);
  assert.equal(polling.state.polling, false);

  win.listeners.get('pageshow')();
  assert.equal(intervals.length, 2);
  assert.equal(polling.state.polling, true);

  win.listeners.get('beforeunload')();
  assert.deepEqual(cleared, [intervals[0], intervals[1]]);
  assert.equal(polling.state.polling, false);

  polling.start();
  polling.destroy();
  assert.deepEqual(cleared, [intervals[0], intervals[1], intervals[2]]);
  assert.equal(polling.state.polling, false);
  assert.equal(doc.removed.some((entry) => entry.type === 'visibilitychange'), true);
  assert.equal(win.removed.some((entry) => entry.type === 'pagehide'), true);
  assert.equal(win.removed.some((entry) => entry.type === 'pageshow'), true);
  assert.equal(win.removed.some((entry) => entry.type === 'beforeunload'), true);
});
