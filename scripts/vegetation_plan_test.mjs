import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadVegetationTestApi() {
  const source = fs.readFileSync(new URL('../public/viewer/vegetation.js', import.meta.url), 'utf8');
  const window = { THREE: {}, VEILTerrain: {} };
  vm.runInNewContext(source, { window });
  return window.VEILVegetation?._test;
}

test('live terrain synchronization translates every mesh part in an instance', () => {
  const api = loadVegetationTestApi();
  const trunk = { instanceMatrix: { array: new Float32Array(32) } };
  const canopy = { instanceMatrix: { array: new Float32Array(32) } };
  trunk.instanceMatrix.array[29] = 5;
  canopy.instanceMatrix.array[29] = 12;
  const slot = { groundHeight: 100, instanceIndex: 1, meshes: [trunk, canopy] };
  const touched = new Set();

  assert.equal(api.shiftPlanTerrainSlot(slot, 102.25, touched), true);
  assert.equal(trunk.instanceMatrix.array[29], 7.25);
  assert.equal(canopy.instanceMatrix.array[29], 14.25);
  assert.equal(slot.groundHeight, 102.25);
  assert.equal(touched.has(trunk), true);
  assert.equal(touched.has(canopy), true);
});

test('vegetation normalization preserves IDs beside valid packed rows', () => {
  const api = loadVegetationTestApi();
  const trees = api.normalizeTreePayload([
    { id: 'tree:one', x: 1, y: 2, height: 8, radius: 2, type: 'evergreen' },
    { id: 'tree:invalid', x: 'bad', y: 3, height: 8, radius: 2, type: 'deciduous' },
    { id: 'tree:two', x: 4, y: 5, height: 9, radius: 3, type: 'deciduous' },
  ]);
  const shrubs = api.normalizeShrubPayload([
    { id: 'shrub:one', x: 1, y: 2, baseScale: 1.2 },
  ]);

  assert.equal(JSON.stringify(trees.entityIds), JSON.stringify(['tree:one', 'tree:two']));
  assert.equal(JSON.stringify(shrubs.entityIds), JSON.stringify(['shrub:one']));
});

test('removal preview hides one slot and restores its original matrix', () => {
  const api = loadVegetationTestApi();
  const values = new Float32Array(32);
  values.set([2, 0, 0, 0, 0, 3, 0, 0, 0, 0, 4, 0, 11, 12, 13, 1], 16);
  const mesh = { instanceMatrix: { array: values } };
  const slot = {
    instances: [{ mesh, instanceIndex: 1 }], originalMatrices: null,
    planRemovalHidden: false,
  };

  assert.equal(api.hidePlanTerrainSlot(slot, new Set()), true);
  assert.deepEqual(Array.from(values.slice(16, 32)), [
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 11, 12, 13, 1,
  ]);
  assert.equal(api.restorePlanTerrainSlot(slot, new Set()), true);
  assert.deepEqual(Array.from(values.slice(16, 32)), [
    2, 0, 0, 0, 0, 3, 0, 0, 0, 0, 4, 0, 11, 12, 13, 1,
  ]);
});

test('partial instance uploads coalesce adjacent matrix slots', () => {
  const api = loadVegetationTestApi();
  const ranges = [];
  const attribute = {
    addUpdateRange(start, count) { ranges.push({ start, count }); }, needsUpdate: false,
  };
  api.markInstanceMatrixIndices({ instanceMatrix: attribute }, new Set([4, 5, 8]));
  assert.deepEqual(ranges, [
    { start: 4 * 16, count: 2 * 16 },
    { start: 8 * 16, count: 16 },
  ]);
  assert.equal(attribute.needsUpdate, true);
});

test('terrain synchronization queries only vegetation buckets in the footprint', () => {
  const api = loadVegetationTestApi();
  const index = api.createPlanTerrainIndex(10);
  api.addPlanTerrainSlot(index, { id: 'inside', x: -2, y: 3 });
  api.addPlanTerrainSlot(index, { id: 'edge', x: 5, y: 5 });
  api.addPlanTerrainSlot(index, { id: 'outside', x: 14, y: 5 });

  const selected = api.planTerrainSlotsInBounds(index, {
    minX: -3, maxX: 5, minY: 2, maxY: 5,
  }).map((slot) => slot.id).sort();
  assert.equal(JSON.stringify(selected), JSON.stringify(['edge', 'inside']));
});
