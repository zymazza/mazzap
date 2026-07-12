import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadPlanTestApi() {
  const source = fs.readFileSync(new URL('../public/plan.js', import.meta.url), 'utf8');
  const window = { THREE: {} };
  vm.runInNewContext(source, { window });
  return window.VEILPlan?._test;
}

test('clearing an active GAIA directive removes its preview and restores planned view', () => {
  const api = loadPlanTestApi();
  const state = {
    directiveActive: true,
    previewEdits: [{ edit_id: 'edit_preview' }],
    viewMode: 'difference',
  };

  assert.equal(api.clearDirectiveState(state), true);
  assert.equal(state.directiveActive, false);
  assert.equal(state.previewEdits.length, 0);
  assert.equal(state.viewMode, 'planned');
});

test('a null directive cannot disturb a manually selected Plan view', () => {
  const api = loadPlanTestApi();
  const state = {
    directiveActive: false,
    previewEdits: [{ edit_id: 'manual_preview' }],
    viewMode: 'baseline',
  };

  assert.equal(api.clearDirectiveState(state), false);
  assert.equal(state.directiveActive, false);
  assert.equal(state.previewEdits.length, 1);
  assert.equal(state.viewMode, 'baseline');
});

test('all live brushes remain available during ordered background autosave without leaking to navigation', () => {
  const api = loadPlanTestApi();

  assert.equal(api.brushPointerBlocked({ busy: false, saving: true, tool: 'fill' }), false);
  assert.equal(api.brushPointerBlocked({ busy: false, saving: true, tool: 'cut' }), false);
  assert.equal(api.brushPointerBlocked({ busy: false, saving: true, tool: 'tree' }), false);
  assert.equal(api.brushPointerBlocked({ busy: false, saving: true, tool: 'remove' }), false);
  assert.equal(api.brushPointerBlocked({ busy: true, saving: false, tool: 'fill' }), true);
});

test('plant brush visits lattice candidates incrementally and never emits a cell twice', () => {
  const api = loadPlanTestApi();
  const stroke = {
    editId: 'edit_incremental',
    plantVisitedCells: new Set(),
    previewPlantPoints: [],
  };
  const first = api.incrementalPlantPointsForSegment(
    stroke, [0, 0], [8, 0], 3, 2, () => true);
  assert.ok(first.length > 0);
  stroke.previewPlantPoints.push(...first);

  const repeated = api.incrementalPlantPointsForSegment(
    stroke, [0, 0], [8, 0], 3, 2, () => true);
  assert.equal(repeated.length, 0, 'repainting the same segment must not regenerate accepted cells');

  const next = api.incrementalPlantPointsForSegment(
    stroke, [8, 0], [16, 0], 3, 2, () => true);
  assert.ok(next.length > 0);
  const coordinates = first.concat(next).map((point) => point.join(':'));
  assert.equal(new Set(coordinates).size, coordinates.length);
});

test('plant preview matrix uploads address only the appended instance components', () => {
  const api = loadPlanTestApi();
  const ranges = [];
  const attribute = {
    addUpdateRange(start, count) { ranges.push({ start, count }); },
    needsUpdate: false,
  };
  api.markInstanceMatrixRange({ instanceMatrix: attribute }, 7, 3);
  assert.deepEqual(ranges, [{ start: 7 * 16, count: 3 * 16 }]);
  assert.equal(attribute.needsUpdate, true);
});

test('terrain edits added during an in-flight save commit against successive revision heads', async () => {
  const api = loadPlanTestApi();
  const firstEdit = { edit_id: 'edit_one', kind: 'terrain_fill' };
  const secondEdit = { edit_id: 'edit_two', kind: 'terrain_fill' };
  const queue = [{ edit: firstEdit, message: 'one' }];
  const requests = [];
  let current = {
    plan: { plan_id: 'plan_test', head_revision_id: 'rev_root' },
    revision: { revision_id: 'rev_root', edits: [] },
  };
  let releaseFirst;
  const firstPending = new Promise((resolve) => { releaseFirst = resolve; });

  const draining = api.drainOrderedEditQueue(queue, {
    current: () => current,
    async save({ current: savingCurrent, nextEdits }) {
      requests.push({
        expected: savingCurrent.plan.head_revision_id,
        editIds: nextEdits.map((edit) => edit.edit_id),
      });
      if (requests.length === 1) await firstPending;
      const revisionId = `rev_${requests.length}`;
      return {
        plan: { plan_id: 'plan_test', head_revision_id: revisionId },
        revision: { revision_id: revisionId, edits: nextEdits },
      };
    },
    saved({ payload }) { current = payload; },
  });

  assert.equal(requests.length, 1);
  queue.push({ edit: secondEdit, message: 'two' });
  releaseFirst();
  await draining;

  assert.equal(JSON.stringify(requests), JSON.stringify([
    { expected: 'rev_root', editIds: ['edit_one'] },
    { expected: 'rev_1', editIds: ['edit_one', 'edit_two'] },
  ]));
  assert.equal(queue.length, 0);
  assert.equal(current.plan.head_revision_id, 'rev_2');
});

test('terrain preview adds held-brush strength while a repeated base pass stays idempotent', () => {
  const api = loadPlanTestApi();
  const heights = new Array(9).fill(0);
  const positionValues = new Float32Array(9 * 3);
  const position = {
    needsUpdate: false,
    setY(index, value) { positionValues[index * 3 + 1] = value; },
  };
  const preview = {
    tool: 'fill',
    grid: {
      width: 3, height: 3, minX: 0, maxX: 2, minY: 0, maxY: 2,
      minElevation: 0, heights,
    },
    geometry: { attributes: { position } },
    baseHeights: heights.slice(),
    baseWeights: new Float32Array(9),
    accumulationWeights: new Float32Array(9),
    touchedFlags: new Uint8Array(9),
    touched: [],
    radius: 1,
    amount: 1,
  };

  assert.equal(api.applyTerrainPreviewInfluence(preview, [1, 1], [1, 1]).changed, true);
  assert.equal(heights[4], 1);
  assert.equal(api.applyTerrainPreviewInfluence(preview, [1, 1], [1, 1]).changed, false);
  assert.equal(heights[4], 1, 'replaying the base path alone must not accidentally accumulate');

  api.applyTerrainPreviewInfluence(preview, [1, 1], [1, 1], { accumulate: true, strength: 0.25 });
  api.applyTerrainPreviewInfluence(preview, [1, 1], [1, 1], { accumulate: true, strength: 0.75 });
  assert.equal(heights[4], 2, 'one second of held-brush strength adds one selected earth amount');
  assert.equal(positionValues[4 * 3 + 1], 2);
  assert.equal(preview.touched.length, 1);
});
