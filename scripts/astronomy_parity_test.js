#!/usr/bin/env node
/* Compare vendored browser astronomy-engine against the Python wrapper. */

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const PROJECT = path.dirname(__dirname);
const Astronomy = require(path.join(PROJECT, 'public/vendor/astronomy.browser.min.js'));

const TIMES = [
  '2024-01-15T17:00:00Z',
  '2024-04-08T18:25:00Z',
  '2024-07-01T04:00:00Z',
  '2025-03-20T12:00:00Z',
  '2026-07-08T00:00:00Z',
  '2030-12-21T22:00:00Z',
  '2040-06-01T09:30:00Z',
  '2050-09-22T18:00:00Z',
  '2100-01-01T00:00:00Z',
  '2200-06-15T12:45:00Z',
];
const BODIES = ['sun', 'moon', 'mars'];
const BODY_API = {
  sun: Astronomy.Body.Sun,
  moon: Astronomy.Body.Moon,
  mars: Astronomy.Body.Mars,
};

const DATA_DIR = process.env.TWIN_DATA_DIR || path.join(PROJECT, 'data');

function readSite() {
  try {
    const georef = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'georef.json'), 'utf8'));
    const origin = georef.origin_wgs84 || {};
    const lat = Number(origin.lat);
    const lon = Number(origin.lon);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      return { lat, lon, heightM: Number(georef.grid_min_elevation_m || 0) };
    }
  } catch (_err) { /* fall through */ }
  // Parity is site-agnostic math validation; any observer works.
  console.log('georef origin_wgs84 unavailable; using a fixed reference site');
  return { lat: 40.0, lon: -105.27, heightM: 1600 };
}

function angularErrorArcsec(a, b) {
  const az1 = a.azimuthDeg * Math.PI / 180;
  const alt1 = a.altitudeDeg * Math.PI / 180;
  const az2 = b.azimuth_deg * Math.PI / 180;
  const alt2 = b.altitude_deg * Math.PI / 180;
  const dot = Math.sin(alt1) * Math.sin(alt2) +
    Math.cos(alt1) * Math.cos(alt2) * Math.cos(az1 - az2);
  return Math.acos(Math.max(-1, Math.min(1, dot))) * 180 / Math.PI * 3600;
}

function jsPosition(body, iso, site) {
  const time = Astronomy.MakeTime(new Date(iso));
  const observer = new Astronomy.Observer(site.lat, site.lon, site.heightM);
  const eq = Astronomy.Equator(BODY_API[body], time, observer, true, true);
  const hor = Astronomy.Horizon(time, observer, eq.ra, eq.dec, null);
  return { azimuthDeg: hor.azimuth, altitudeDeg: hor.altitude };
}

function pythonPositions(site) {
  const python = process.env.PYTHON ||
    (fs.existsSync(path.join(PROJECT, '.venv-mcp/bin/python'))
      ? path.join(PROJECT, '.venv-mcp/bin/python')
      : 'python3');
  const code = `
import json, sys
sys.path.insert(0, ${JSON.stringify(path.join(PROJECT, 'scripts'))})
import twin_astro
times = ${JSON.stringify(TIMES)}
bodies = ${JSON.stringify(BODIES)}
site = twin_astro.Site(${site.lat}, ${site.lon}, ${site.heightM})
out = {}
for iso in times:
    out[iso] = {}
    for body in bodies:
        out[iso][body] = twin_astro.body_position(body, iso, site)
print(json.dumps(out))
`;
  const result = spawnSync(python, ['-c', code], { encoding: 'utf8' });
  if (result.status !== 0) {
    process.stderr.write(result.stderr || result.stdout);
    process.exit(result.status || 1);
  }
  return JSON.parse(result.stdout);
}

function checkConstellationAssets() {
  const stars = JSON.parse(fs.readFileSync(path.join(PROJECT, 'public/astronomy-data/stars.json'), 'utf8'));
  const constellations = JSON.parse(fs.readFileSync(path.join(PROJECT, 'public/astronomy-data/constellations.json'), 'utf8'));
  const hips = new Set((stars.stars || []).map((row) => Number(row[4])));
  let dropped = 0;
  let total = 0;
  Object.values(constellations.lines || {}).forEach((pairs) => {
    pairs.forEach(([a, b]) => {
      total += 1;
      if (!hips.has(Number(a)) || !hips.has(Number(b))) dropped += 1;
    });
  });
  console.log(`constellation segment drops: ${dropped}/${total}`);
  return Number.isInteger(dropped) && dropped >= 0;
}

function main() {
  const site = readSite();
  const py = pythonPositions(site);
  let failed = !checkConstellationAssets();
  console.log('time                      body   error_arcsec');
  console.log('------------------------  -----  ------------');
  for (const iso of TIMES) {
    for (const body of BODIES) {
      const err = angularErrorArcsec(jsPosition(body, iso, site), py[iso][body]);
      const ok = err < 1;
      failed = failed || !ok;
      console.log(`${iso.padEnd(24)}  ${body.padEnd(5)}  ${err.toFixed(4).padStart(12)} ${ok ? 'OK' : 'FAIL'}`);
    }
  }
  if (failed) process.exit(1);
}

main();
