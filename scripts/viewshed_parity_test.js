#!/usr/bin/env node
// Compare public/viewshed.js worker math with scripts/twin_viewshed.py.

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const dataDir = path.resolve(process.env.TWIN_DATA_DIR || path.join(root, 'data'));
function readJsonIfExists(...parts) {
  const p = path.join(...parts);
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

const apronGrid = readJsonIfExists(dataDir, 'terrain/grid.apron.json');
const parcelGrid = readJsonIfExists(dataDir, 'terrain/grid.json');
const grid = apronGrid || parcelGrid;
if (!grid) {
  throw new Error(`no terrain grid found under ${dataDir}`);
}
// Composite parcel LiDAR over the apron, matching RingStack merge_local_grids /
// from_local_files on the Python side — otherwise the JS ring A is hollow in the
// parcel interior and the horizon diverges from Python's merged 3 m ring A.
if (apronGrid && parcelGrid?.heights && parcelGrid.heights.length === grid.heights.length) {
  for (let i = 0; i < grid.heights.length; i += 1) {
    if (parcelGrid.heights[i] != null) {
      grid.heights[i] = parcelGrid.heights[i];
    }
  }
}
const evh = readJsonIfExists(dataDir, 'atlas/local/landfire_evh_2024.grid.json');
const vat = readJsonIfExists(dataDir, 'atlas/vat/landfire_evh_2024.json');

const context = {
  console,
  Float32Array,
  Uint8Array,
  Map,
  Number,
  Math,
  Array,
  String,
  RegExp,
  Error,
  globalThis: null,
};
context.globalThis = context;
vm.runInNewContext(fs.readFileSync(path.join(root, 'public/viewshed.js'), 'utf8'), context);
const api = context.VEILViewshed._test;

function nearestFinite() {
  let best = null;
  const xStep = (grid.maxX - grid.minX) / Math.max(1, grid.width - 1);
  const yStep = (grid.maxY - grid.minY) / Math.max(1, grid.height - 1);
  for (let i = 0; i < grid.heights.length; i += 1) {
    const v = grid.heights[i];
    if (!Number.isFinite(v)) continue;
    const row = Math.floor(i / grid.width);
    const col = i % grid.width;
    const x = grid.minX + col * xStep;
    const y = grid.maxY - row * yStep;
    const d2 = x * x + y * y;
    if (!best || d2 < best.d2) best = { d2, x, y };
  }
  return best;
}

const obs = nearestFinite();
const canopy = evh && vat ? api.buildCanopy(grid, evh, vat) : new Float32Array(grid.width * grid.height);
const ring = api.makeRingPayload(grid, canopy);
const js = api.core.sweep({ rings: [ring] }, { x: obs.x, y: obs.y, aglM: 1.7, nAz: 720, surface: 'canopy', k: 'optical' });

const pyCode = `
import json, sys
sys.path.insert(0, 'scripts')
import twin_viewshed as v
s=v.RingStack.from_local_files(sys.argv[3])
x=float(sys.argv[1]); y=float(sys.argv[2])
r=v.sweep(s,x,y,1.7,n_az=720,surface='canopy',k='optical')
ring=s.rings[0]
print(json.dumps({
  'horizon': [float(x) for x in r['horizon_deg']],
  'visible_fraction': r['stats']['per_ring'][ring.name]['fraction'],
  'visible_km2': r['stats']['visible_km2'],
}))
`;
const py = spawnSync('python3', ['-c', pyCode, String(obs.x), String(obs.y), dataDir], {
  cwd: root,
  env: { ...process.env, TWIN_DATA_DIR: dataDir },
  encoding: 'utf8',
});
if (py.status !== 0) {
  process.stderr.write(py.stderr || py.stdout);
  process.exit(py.status || 1);
}
const pyResult = JSON.parse(py.stdout);
let maxAbs = 0;
let sumSq = 0;
let n = 0;
for (let i = 0; i < js.horizonDeg.length; i += 1) {
  const a = js.horizonDeg[i];
  const b = pyResult.horizon[i];
  if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
  const d = Math.abs(a - b);
  maxAbs = Math.max(maxAbs, d);
  sumSq += d * d;
  n += 1;
}
const jsFrac = js.stats.perRing.A.fraction;
const visibleFractionDelta = Math.abs(jsFrac - pyResult.visible_fraction);
const result = {
  observer: [Number(obs.x.toFixed(3)), Number(obs.y.toFixed(3))],
  horizon_max_abs_deg: maxAbs,
  horizon_rms_deg: Math.sqrt(sumSq / Math.max(1, n)),
  visible_fraction_delta: visibleFractionDelta,
  js_visible_fraction: jsFrac,
  python_visible_fraction: pyResult.visible_fraction,
  thresholds: { horizon_max_abs_deg: 0.05, visible_fraction_delta: 0.005 },
};
console.log(JSON.stringify(result, null, 2));
if (result.horizon_max_abs_deg > 0.05 || result.visible_fraction_delta > 0.005) {
  process.exit(1);
}
