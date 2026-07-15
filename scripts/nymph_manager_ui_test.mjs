import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadNymphTestApi() {
  const source = fs.readFileSync(new URL('../public/nymph-manager.js', import.meta.url), 'utf8');
  const window = {};
  vm.runInNewContext(source, { window });
  return window.VEILNymphManager?._test;
}

test('RTS altitude ribbon scrolls in one-metre steps and clamps to the Part 107 UI ceiling', () => {
  const api = loadNymphTestApi();

  assert.equal(api.adjustAltitude(25, -100), 26);
  assert.equal(api.adjustAltitude(25, 100), 24);
  assert.equal(api.adjustAltitude(25, -100, { coarse: true }), 30);
  assert.equal(api.adjustAltitude(120, -100), 120);
  assert.equal(api.adjustAltitude(10, 100), 10);
});

test('each RTS click captures the altitude and named surface active at that click', () => {
  const api = loadNymphTestApi();
  const pick = {
    point: { x: 12.5, y: 44.25 },
    geo: { lat: 43.601, lon: -74.101, elevation_m: 518.2 },
  };

  const canopy = api.createRtsWaypoint(pick, 25, 'canopy', 0);
  const ground = api.createRtsWaypoint(pick, 30, 'ground', 1);

  assert.deepEqual(
    JSON.parse(JSON.stringify(canopy)),
    {
      n: 0,
      lat: 43.601,
      lon: -74.101,
      x: 12.5,
      y: 44.25,
      terrain_elevation_m: 518.2,
      agl_mode: 'canopy',
      target_agl_m: 25,
      checked: false,
      sent: false,
    },
  );
  assert.equal(ground.n, 1);
  assert.equal(ground.agl_mode, 'ground');
  assert.equal(ground.target_agl_m, 30);
});

test('invalid terrain picks never become guided targets', () => {
  const api = loadNymphTestApi();

  assert.equal(api.createRtsWaypoint({ point: { x: 1, y: 2 }, geo: {} }, 25, 'canopy'), null);
  assert.equal(api.createRtsWaypoint(null, 25, 'canopy'), null);
});

test('manual controller mode cannot arm VEIL control authority', () => {
  const api = loadNymphTestApi();
  const state = {
    selectedMode: 'controller',
    link: {
      bridgeConnected: true,
      controlChannelReady: true,
      envelopeReady: true,
      rcTakeoverVerified: true,
    },
    capabilities: { virtualStick: true, nativeMissions: true },
    telemetry: { received_at: 10_000 },
    video: { connected: true },
  };

  assert.equal(api.canArm(state, 'controller', 10_100), false);
  assert.equal(api.requestedAircraftMode('controller', state), 'MANUAL');
});

test('virtual stick arms only with bridge, control, fresh telemetry, envelope, takeover, and B2 capability', () => {
  const api = loadNymphTestApi();
  const now = 1_784_052_000_000;
  const ready = {
    selectedMode: 'virtual-stick',
    link: {
      bridgeConnected: true,
      controlChannelReady: true,
      envelopeReady: true,
      rcTakeoverVerified: true,
    },
    capabilities: { virtualStick: true, nativeMissions: false, browserDirectControl: true },
    telemetry: { received_at: now - 100 },
  };

  assert.equal(api.canArm(ready, 'virtual-stick', now), true);
  assert.equal(api.requestedAircraftMode('virtual-stick', ready), 'DIRECT');
  assert.equal(api.canArm({ ...ready, telemetry: { received_at: now - 1600 } }, 'virtual-stick', now), false);
  assert.equal(api.canArm({ ...ready, link: { ...ready.link, envelopeReady: false } }, 'virtual-stick', now), false);
  assert.equal(api.canArm({ ...ready, capabilities: { virtualStick: false } }, 'virtual-stick', now), false);
  assert.equal(api.canArm({ ...ready, capabilities: { virtualStick: true } }, 'virtual-stick', now), false);
});

test('RTS prefers native GUIDED-N and falls back to GUIDED-VS only when capability gates allow it', () => {
  const api = loadNymphTestApi();

  assert.equal(api.guidedCapability({ capabilities: { nativeMissions: true, virtualStick: true } }), 'GUIDED-N');
  assert.equal(api.guidedCapability({ capabilities: { nativeMissions: false, virtualStick: true } }), 'GUIDED-VS');
  assert.equal(api.guidedCapability({ capabilities: { nativeMissions: false, virtualStick: false } }), null);
});

test('telemetry timestamps accept Unix seconds or milliseconds and age deterministically', () => {
  const api = loadNymphTestApi();
  const now = 1_784_052_000_000;

  assert.equal(api.timestampMs(10), 10_000);
  assert.equal(api.timestampMs(10_000_000_000), 10_000_000_000_000);
  assert.equal(api.timestampMs(100_000_000_000), 100_000_000_000);
  assert.equal(api.telemetryAgeMs({ received_at: now - 500 }, now), 500);
});

test('Nymph Manager appears directly after telemetry and loads before the app boot module', () => {
  const html = fs.readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  const telemetry = html.indexOf('data-mode="telemetry"');
  const nymphs = html.indexOf('data-mode="nymphs"');
  const bridgeScript = html.indexOf('<script src="/nymph-bridge-client.js"></script>');
  const nymphScript = html.indexOf('<script src="/nymph-manager.js"></script>');
  const appScript = html.indexOf('<script src="/app.js"></script>');

  assert.ok(telemetry >= 0);
  assert.ok(nymphs > telemetry);
  assert.ok(bridgeScript > nymphs);
  assert.ok(nymphScript > bridgeScript);
  assert.ok(appScript > nymphScript);
});

test('app boot injects the same-origin bridge while unchecked RTS drafts stay local', () => {
  const app = fs.readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  const manager = fs.readFileSync(new URL('../public/nymph-manager.js', import.meta.url), 'utf8');

  assert.match(app, /VEILNymphBridgeClient\?\.create\(\)/);
  assert.match(app, /controlClient:\s*nymphBridge/);
  assert.match(app, /nymphBridge\?\.setStatusSink/);
  assert.match(app, /nymphBridge\?\.start\(\)/);
  assert.match(manager, /checked:\s*false,\s*sent:\s*false/);
  assert.doesNotMatch(manager, /acceptRoute\s*\(/);
});
