import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadAppTestApi() {
  const source = fs.readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  const window = { __VEIL_APP_TEST__: true };
  const document = { getElementById: () => ({}) };
  vm.runInNewContext(source, { console, document, window });
  return window.VEILApp._test;
}

function element() {
  return { hidden: true, textContent: '', href: '', dataset: {} };
}

const geo = {
  lat: 39.9806543,
  lon: -105.2705127,
  elevation_m: 382.46,
  easting: 478765.43,
  northing: 4426543.21,
};

test('pick readout formatting preserves existing coordinate display text', () => {
  const api = loadAppTestApi();

  assert.deepEqual(JSON.parse(JSON.stringify(api.formatPickReadout(geo))), {
    lat: '39.980654',
    lon: '-105.270513',
    latlonText: '39.980654, -105.270513',
    elevText: '382.5 m  (1255 ft)',
    utmText: '478765.4 E, 4426543.2 N',
    gmapsHref: 'https://www.google.com/maps/search/?api=1&query=39.980654,-105.270513',
    copyCoord: '39.980654,-105.270513',
  });
});

test('pick readout updates each available element independently', () => {
  const api = loadAppTestApi();
  const readout = {
    hintEl: element(),
    gridEl: element(),
    actionsEl: element(),
    latlonEl: element(),
    elevEl: null,
    utmEl: element(),
    gmaps: null,
    copyBtn: element(),
  };

  assert.doesNotThrow(() => api.updatePickReadout(readout, geo));
  assert.equal(readout.hintEl.hidden, true);
  assert.equal(readout.gridEl.hidden, false);
  assert.equal(readout.actionsEl.hidden, false);
  assert.equal(readout.latlonEl.textContent, '39.980654, -105.270513');
  assert.equal(readout.utmEl.textContent, '478765.4 E, 4426543.2 N');
  assert.equal(readout.copyBtn.dataset.coord, '39.980654,-105.270513');
});

test('pick readout missing-id helper reports only absent optional fields', () => {
  const api = loadAppTestApi();
  const readout = {
    hintEl: {},
    gridEl: null,
    actionsEl: {},
    latlonEl: undefined,
    elevEl: {},
    utmEl: {},
    gmaps: null,
    copyBtn: {},
  };

  assert.deepEqual([...api.missingPickReadoutIds(readout)], [
    'readout-grid',
    'r-latlon',
    'gmaps-link',
  ]);
});

test('identify hit radius follows camera scale with min and max bounds', () => {
  const api = loadAppTestApi();

  const close = api.identifyHitRadiusMetersFromView({
    cameraDistanceMeters: 25,
    viewportHeightPx: 1000,
    fovDegrees: 50,
  });
  const mid = api.identifyHitRadiusMetersFromView({
    cameraDistanceMeters: 600,
    viewportHeightPx: 1000,
    fovDegrees: 50,
  });
  const far = api.identifyHitRadiusMetersFromView({
    cameraDistanceMeters: 6000,
    viewportHeightPx: 1000,
    fovDegrees: 50,
  });

  assert.equal(close, 2.5);
  assert.ok(mid > close);
  assert.ok(mid < far);
  assert.equal(far, 18);
});

test('identify hit radius falls back when view geometry is unavailable', () => {
  const api = loadAppTestApi();

  assert.equal(api.identifyHitRadiusMetersFromView({
    cameraDistanceMeters: NaN,
    viewportHeightPx: 1000,
    fovDegrees: 50,
  }), 8);
  assert.equal(api.identifyHitRadiusMetersFromView({
    cameraDistanceMeters: 100,
    viewportHeightPx: 0,
    fovDegrees: 50,
  }), 8);
});

test('RTS Nymph drafting owns terrain picks without trusting a failing device hook', () => {
  const api = loadAppTestApi();

  assert.equal(api.nymphOwnsMapPick({ isDrafting: () => true }), true);
  assert.equal(api.nymphOwnsMapPick({ isDrafting: () => false }), false);
  assert.equal(api.nymphOwnsMapPick(null), false);
  assert.equal(api.nymphOwnsMapPick({ isDrafting: () => { throw new Error('bridge state failed'); } }), false);
});
