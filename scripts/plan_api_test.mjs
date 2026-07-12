#!/usr/bin/env node
/* End-to-end Plan REST test against a disposable twin and the real server. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'veil-plan-api-'));
const port = 43000 + (process.pid % 1000);
const origin = `http://127.0.0.1:${port}`;

function write(relative, value) {
  const target = path.join(tmp, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, JSON.stringify(value));
}

const grid = {
  width: 9, height: 9, heights: Array(81).fill(100),
  minX: 0, maxX: 8, minY: 0, maxY: 8,
  outerMinX: -0.5, outerMaxX: 8.5, outerMinY: -0.5, outerMaxY: 8.5,
  minElevation: 100, maxElevation: 100,
};
write('terrain/grid.json', grid);
write('terrain/aoi_local.geojson', {
  type: 'FeatureCollection', features: [{ type: 'Feature', properties: {},
    geometry: { type: 'Polygon', coordinates: [[[0, 0], [8, 0], [8, 8], [0, 8], [0, 0]]] } }],
});
write('scene.json', {
  name: 'Plan API fixture', origin_utm: [0, 0],
  terrain: { grid_url: '/data/terrain/grid.json' },
  vegetation: {
    tree_instances_url: '/data/vegetation/tree_instances.json',
    shrub_points_url: '/data/vegetation/shrub_points.json',
  },
});
write('georef.json', { analysis_crs: 'EPSG:3857', origin_utm: [0, 0] });
write('vegetation/tree_instances.json', [
  { id: 'tree:fixture', x: 2, y: 2, height: 8, radius: 2,
    type: 'deciduous', species: 'Maple', source: 'fixture' },
]);
write('vegetation/shrub_points.json', []);
write('vegetation/metadata.json', { canopy_cover_pct: 12 });

const server = spawn(process.execPath, ['server.js'], {
  cwd: ROOT,
  env: { ...process.env, TWIN_DATA_DIR: tmp, PORT: String(port), VEIL_OPEN_BROWSER: '0' },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let logs = '';
server.stdout.on('data', (chunk) => { logs += chunk; });
server.stderr.on('data', (chunk) => { logs += chunk; });

async function request(relative, options = {}) {
  const response = await fetch(origin + relative, {
    ...options,
    headers: {
      ...(options.method && options.method !== 'GET' ? { Origin: origin } : {}),
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); } catch (_err) { body = text; }
  return { response, body };
}

async function waitForServer() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const { response } = await request('/api/plans');
      if (response.ok) return;
    } catch (_err) { /* starting */ }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`server did not start\n${logs}`);
}

try {
  await waitForServer();
  const createdResponse = await request('/api/plans', {
    method: 'POST', body: JSON.stringify({ name: 'API plan', author: 'test' }),
  });
  assert.equal(createdResponse.response.status, 200, JSON.stringify(createdResponse.body));
  const created = createdResponse.body;
  assert.match(created.plan.plan_id, /^plan_/);
  assert.match(created.revision.revision_id, /^rev_/);
  assert.match(created.materialized.asset_root, /^\/data\/plans\/revisions\/rev_/);

  const edits = [{
    kind: 'terrain_cut', geometry: { type: 'Point', coordinates: [4, 4] },
    params: { radius_m: 2, depth_m: 0.75 }, label: 'API depression',
  }];
  const committedResponse = await request(`/api/plans/${created.plan.plan_id}/commit`, {
    method: 'POST', body: JSON.stringify({
      expected_revision_id: created.revision.revision_id, edits, author: 'test',
    }),
  });
  assert.equal(committedResponse.response.status, 200, JSON.stringify(committedResponse.body));
  const committed = committedResponse.body;
  assert.ok(committed.materialized.diff.terrain.cut_m3 > 0);

  const terrainResponse = await request(committed.materialized.terrain_grid_url);
  assert.equal(terrainResponse.response.status, 200);
  assert.ok(terrainResponse.body.heights[4 * 9 + 4] < 100);

  const staleResponse = await request(`/api/plans/${created.plan.plan_id}/commit`, {
    method: 'POST', body: JSON.stringify({
      expected_revision_id: created.revision.revision_id, edits, author: 'stale-test',
    }),
  });
  assert.equal(staleResponse.response.status, 409);
  assert.equal(staleResponse.body.error, 'plan_conflict');

  const branchResponse = await request(`/api/plans/${created.plan.plan_id}/branch`, {
    method: 'POST', body: JSON.stringify({
      name: 'API alternative', revision_id: created.revision.revision_id,
    }),
  });
  assert.equal(branchResponse.response.status, 200, JSON.stringify(branchResponse.body));
  assert.notEqual(branchResponse.body.plan.plan_id, created.plan.plan_id);
  assert.equal(branchResponse.body.revision.revision_id, created.revision.revision_id);

  const listResponse = await request('/api/plans');
  assert.equal(listResponse.response.status, 200);
  assert.equal(listResponse.body.plans.length, 2);

  const discardResponse = await request(`/api/plans/${branchResponse.body.plan.plan_id}/update`, {
    method: 'POST', body: JSON.stringify({ archived: true }),
  });
  assert.equal(discardResponse.response.status, 200, JSON.stringify(discardResponse.body));
  assert.ok(discardResponse.body.plan.archived_at);

  const activeListResponse = await request('/api/plans');
  assert.equal(activeListResponse.response.status, 200);
  assert.equal(activeListResponse.body.plans.length, 1);
  const archivedListResponse = await request('/api/plans?include_archived=1');
  assert.equal(archivedListResponse.response.status, 200);
  assert.equal(archivedListResponse.body.plans.length, 2);
  console.log('plan_api_test: ok');
} finally {
  server.kill('SIGTERM');
  await new Promise((resolve) => {
    if (server.exitCode != null) resolve();
    else {
      server.once('exit', resolve);
      setTimeout(resolve, 2000).unref();
    }
  });
  fs.rmSync(tmp, { recursive: true, force: true });
}
