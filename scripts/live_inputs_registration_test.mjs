import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadLiveInputsTestApi() {
  const source = fs.readFileSync(new URL('../public/live-inputs.js', import.meta.url), 'utf8');
  const window = {};
  vm.runInNewContext(source, { window });
  return window.VEILLiveInputs?._test;
}

test('gateway registration success uses payload.gateway when present', () => {
  const api = loadLiveInputsTestApi();
  const payload = {
    ok: true,
    gateway: {
      id: 'field-relay',
      name: 'Field Relay',
      bridge: { state: 'connected' },
    },
  };

  assert.equal(
    api.gatewaySuccessMessage(payload, { name: 'Requested Name' }, []),
    'Gateway Field Relay connected.',
  );
});

test('gateway registration success handles registry-only response', () => {
  const api = loadLiveInputsTestApi();
  const payload = {
    ok: true,
    registry: {
      gateways: [{
        id: 'trail-relay',
        name: 'Trail Relay',
        transport: 'bluetooth',
        address: 'ble:relay-1',
      }],
    },
  };

  assert.equal(
    api.gatewaySuccessMessage(payload, {
      name: 'Trail Relay',
      transport: 'bluetooth',
      address: 'ble:relay-1',
    }, []),
    'Gateway Trail Relay registered.',
  );
});

test('gateway registration success falls back to request name without unguarded gateway access', () => {
  const api = loadLiveInputsTestApi();

  assert.equal(
    api.gatewaySuccessMessage({ ok: true }, { name: 'Meshtastic gateway' }, []),
    'Gateway Meshtastic gateway registered.',
  );
});

test('gateway registration success falls back to gateway id', () => {
  const api = loadLiveInputsTestApi();
  const payload = {
    ok: true,
    registry: {
      gateways: [{
        id: 'serial-relay',
        transport: 'serial',
        address: '/dev/ttyUSB0',
      }],
    },
  };

  assert.equal(
    api.gatewaySuccessMessage(payload, {
      transport: 'serial',
      address: '/dev/ttyUSB0',
    }, []),
    'Gateway serial-relay registered.',
  );
});

test('destructive confirmation message warns gateway removal also removes child devices', () => {
  const api = loadLiveInputsTestApi();
  const message = api.destructiveActionConfirmationMessage('remove-gateway', 'Ridge relay');

  assert.match(message, /Remove Ridge relay/);
  assert.match(message, /Linked\/current child devices/);
  assert.match(message, /will also be removed/);
});

test('destructive confirmation delegates to confirm function', () => {
  const api = loadLiveInputsTestApi();
  const prompts = [];

  assert.equal(api.confirmDestructiveAction('remove-device', 'tracker-1', (message) => {
    prompts.push(message);
    return true;
  }), true);
  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /Remove tracker-1/);

  assert.equal(api.confirmDestructiveAction('remove-device', 'tracker-1', () => false), false);
  assert.equal(api.confirmDestructiveAction('remove-device', 'tracker-1', null), false);
});

test('prompt label normalization rejects blank names and trims valid names', () => {
  const api = loadLiveInputsTestApi();

  assert.equal(api.normalizePromptLabel(null), null);
  assert.equal(api.normalizePromptLabel(''), null);
  assert.equal(api.normalizePromptLabel('   \n\t  '), null);
  assert.equal(api.normalizePromptLabel('  Ridge tracker  '), 'Ridge tracker');
});

test('sorted time index helper finds latest item at or before target', () => {
  const api = loadLiveInputsTestApi();
  const items = [{ ms: 1000 }, { ms: 2000 }, { ms: 5000 }];

  assert.equal(api.sortedTimeIndexFor(items, 999, (item) => item.ms), -1);
  assert.equal(api.sortedTimeIndexFor(items, 1000, (item) => item.ms), 0);
  assert.equal(api.sortedTimeIndexFor(items, 3500, (item) => item.ms), 1);
  assert.equal(api.sortedTimeIndexFor(items, 8000, (item) => item.ms), 2);
  assert.equal(api.sortedTimeIndexFor([], 1000, (item) => item.ms), -1);
  assert.equal(api.sortedTimeIndexFor(items, null, (item) => item.ms), -1);
});

test('in-flight guard skips repeated action and restores controls', async () => {
  const api = loadLiveInputsTestApi();
  const guard = api.createInFlightGuard();
  const button = { disabled: false };
  let runs = 0;
  let releaseFirst;

  const first = guard.run('request-position', async () => {
    runs += 1;
    await new Promise((resolve) => {
      releaseFirst = resolve;
    });
    return 'ok';
  }, button);

  assert.equal(button.disabled, true);
  assert.equal(guard.isPending('request-position'), true);

  const second = await guard.run('request-position', async () => {
    runs += 1;
  }, button);

  assert.equal(second.skipped, true);
  assert.equal(runs, 1);
  assert.equal(button.disabled, true);

  releaseFirst();
  const firstResult = await first;
  assert.equal(firstResult.skipped, false);
  assert.equal(firstResult.value, 'ok');
  assert.equal(button.disabled, false);
  assert.equal(guard.isPending('request-position'), false);
});

test('in-flight guard preserves controls that were already disabled', async () => {
  const api = loadLiveInputsTestApi();
  const guard = api.createInFlightGuard();
  const control = { disabled: true };

  await guard.run('append-export', async () => 'done', control);

  assert.equal(control.disabled, true);
});

test('safe SSE JSON parser returns payloads and diagnostics without throwing', () => {
  const api = loadLiveInputsTestApi();

  const parsed = api.safeParseSseJson('{"device_id":"dev-1"}', 'live');
  assert.equal(parsed.ok, true);
  assert.equal(parsed.value.device_id, 'dev-1');

  const malformed = api.safeParseSseJson('{"device_id":', 'snapshot');
  assert.equal(malformed.ok, false);
  assert.match(malformed.message, /Ignored malformed live telemetry snapshot frame/);
  assert.match(malformed.message, /JSON/);
});

test('SSE error status escalates after repeated or long failures', () => {
  const api = loadLiveInputsTestApi();

  const initial = api.sseErrorStatus({ errorCount: 1, disconnectedMs: 5000 });
  assert.equal(initial.tone, 'warn');
  assert.equal(initial.text, 'Live stream disconnected; retrying in the background.');

  const repeated = api.sseErrorStatus({ errorCount: 3, disconnectedMs: 5000 });
  assert.equal(repeated.tone, 'err');
  assert.match(repeated.text, /after 3 retry events/);
  assert.match(repeated.text, /telemetry may be stale/);

  const long = api.sseErrorStatus({ errorCount: 1, disconnectedMs: 31000 });
  assert.equal(long.tone, 'err');
  assert.match(long.text, /31s/);
});

test('recording SSE success resets error count and last-ok timestamp', () => {
  const api = loadLiveInputsTestApi();
  const state = { liveStreamErrorCount: 4, liveStreamLastOkMs: 100 };

  api.recordSseSuccess(state, 12345);

  assert.equal(state.liveStreamErrorCount, 0);
  assert.equal(state.liveStreamLastOkMs, 12345);
});

test('live freshness recomputes active, stale, and offline from position timestamp', () => {
  const api = loadLiveInputsTestApi();
  const observed = Date.parse('2026-01-02T03:00:00Z');
  const device = {
    device_id: 'tracker-1',
    position: { lat: 39.98, lon: -105.27 },
    freshness: {
      state: 'active',
      position_observed_at: '2026-01-02T03:00:00Z',
      active_after_seconds: 120,
      offline_after_seconds: 900,
    },
  };

  const active = api.computeLiveDeviceFreshness(device, observed + 30_000);
  assert.equal(active.state, 'active');
  assert.equal(active.age_seconds, 30);
  assert.equal(active.active, true);

  const stale = api.computeLiveDeviceFreshness(device, observed + 121_000);
  assert.equal(stale.state, 'stale');
  assert.equal(stale.reason, 'location has not updated recently');
  assert.equal(stale.active, false);

  const offline = api.computeLiveDeviceFreshness(device, observed + 901_000);
  assert.equal(offline.state, 'offline');
  assert.equal(offline.reason, 'location is too old');
});

test('live freshness falls back to packet timestamps and keeps no-location devices no_location', () => {
  const api = loadLiveInputsTestApi();
  const now = Date.parse('2026-01-02T03:05:00Z');
  const device = {
    device_id: 'tracker-2',
    observed_at: '2026-01-02T03:04:50Z',
    received_at: '2026-01-02T03:04:52Z',
    freshness: {
      state: 'active',
      age_seconds: 1,
      last_event_received_at: '2026-01-02T03:04:52Z',
    },
  };

  assert.equal(api.liveFreshnessPositionMs(device), Date.parse('2026-01-02T03:04:50Z'));
  assert.equal(api.liveFreshnessLastPacketMs(device), Date.parse('2026-01-02T03:04:52Z'));

  const freshness = api.computeLiveDeviceFreshness(device, now);
  assert.equal(freshness.state, 'no_location');
  assert.equal(freshness.reason, 'no location packet has been received');
  assert.equal(freshness.age_seconds, 10);
  assert.equal(freshness.last_packet_age_seconds, 8);
});

test('replay timestamp display uses one formatter for event and frame clock paths', () => {
  const api = loadLiveInputsTestApi();
  const event = {
    observed_at: '2026-01-02T03:04:05+00:00',
    received_at: '2026-01-02T03:04:06Z',
  };
  const formatter = (date) => `local:${date.getTime()}`;

  assert.equal(
    api.formatReplayTimestamp(null, event, formatter),
    `local:${Date.parse(event.observed_at)}`,
  );
  assert.equal(
    api.formatReplayTimestamp(Date.parse(event.observed_at), event, formatter),
    `local:${Date.parse(event.observed_at)}`,
  );
});

test('replay export timestamp follows interpolated frame clock', () => {
  const api = loadLiveInputsTestApi();
  const event = {
    observed_at: '2026-01-02T03:04:05Z',
    received_at: '2026-01-02T03:04:06Z',
  };
  const frameMs = Date.parse('2026-01-02T03:04:05.500Z');

  assert.equal(api.replayFrameTimestampMs(frameMs, event), frameMs);
  assert.equal(api.replayFrameExportTimestamp(frameMs, event), '2026-01-02T03:04:05.500Z');
  assert.equal(api.replayFrameExportTimestamp(null, event), '2026-01-02T03:04:05.000Z');
});

test('replay JSON response parser reports non-OK server errors without raw parse failures', async () => {
  const api = loadLiveInputsTestApi();

  await assert.rejects(
    api.parseReplayJsonResponse({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      async json() {
        throw new SyntaxError('Unexpected token < in JSON at position 0');
      },
    }, 'replay events'),
    /Could not load replay events \(500 Internal Server Error\)\./,
  );

  await assert.rejects(
    api.parseReplayJsonResponse({
      ok: false,
      status: 503,
      statusText: 'Service Unavailable',
      async json() {
        return { error: 'telemetry store unavailable' };
      },
    }, 'replay days'),
    /telemetry store unavailable/,
  );
});

test('replay JSON response parser reports malformed successful payloads as user-facing messages', async () => {
  const api = loadLiveInputsTestApi();

  await assert.rejects(
    api.parseReplayJsonResponse({
      ok: true,
      status: 200,
      async json() {
        throw new SyntaxError('Unexpected end of JSON input');
      },
    }, 'replay days'),
    /Could not read replay days: server returned invalid JSON\./,
  );
});

test('live freshness preserves server gateway-offline state while still aging timestamps', () => {
  const api = loadLiveInputsTestApi();
  const device = {
    device_id: 'tracker-3',
    position: { lat: 39.98, lon: -105.27 },
    freshness: {
      state: 'offline',
      gateway_id: 'relay-1',
      gateway_state: 'retrying',
      position_observed_at: '2026-01-02T03:00:00Z',
      active_after_seconds: 120,
      offline_after_seconds: 900,
    },
  };

  const freshness = api.computeLiveDeviceFreshness(device, Date.parse('2026-01-02T03:00:05Z'));
  assert.equal(freshness.state, 'offline');
  assert.equal(freshness.reason, 'gateway bridge is retrying');
  assert.equal(freshness.age_seconds, 5);
});

test('live metadata panel escapes device-supplied row values without double-escaping JSON sections', () => {
  const api = loadLiveInputsTestApi();
  const html = api.buildLiveMetadataHtml({
    device_id: 'dev-<script>',
    label: 'Tracker <img src=x onerror=alert(1)>',
    observed_at: '2026-01-02T03:00:00Z',
    received_at: '2026-01-02T03:00:01Z',
    freshness: {
      reason: 'gateway <b>offline</b>',
      position_observed_at: '2026-01-02T03:00:00Z',
    },
    kind: 'mesh <node>',
    link: {
      gateway_id: 'relay-<svg onload=alert(1)>',
      snr_db: 7,
      rssi_dbm: -92,
    },
    source: {
      protocol: 'meshtastic<script>',
      transport: 'serial<img>',
      ingress_transport: 'sse<iframe>',
    },
    message: 'hello <strong>field</strong>',
    data: {
      note: '<json-tag>',
    },
  }, () => 'Active <fresh>');

  assert.match(html, /Tracker &lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /Active &lt;fresh&gt;/);
  assert.match(html, /gateway &lt;b&gt;offline&lt;\/b&gt;/);
  assert.match(html, /relay-&lt;svg onload=alert\(1\)&gt;/);
  assert.match(html, /meshtastic&lt;script&gt; \/ serial&lt;img&gt;/);
  assert.match(html, /sse&lt;iframe&gt;/);
  assert.match(html, /hello &lt;strong&gt;field&lt;\/strong&gt;/);
  assert.match(html, /&quot;note&quot;: &quot;&lt;json-tag&gt;&quot;/);
  assert.doesNotMatch(html, /&amp;lt;json-tag/);
  assert.doesNotMatch(html, /<img|<svg|<script|<iframe|<strong>/);
});

test('timer cleanup helper clears stored interval handles once', () => {
  const api = loadLiveInputsTestApi();
  const cleared = [];
  const state = { freshnessTimer: 42 };

  assert.equal(api.clearTimerState(state, 'freshnessTimer', (timer) => {
    cleared.push(timer);
  }), true);
  assert.equal(state.freshnessTimer, null);
  assert.deepEqual(cleared, [42]);

  assert.equal(api.clearTimerState(state, 'freshnessTimer', (timer) => {
    cleared.push(timer);
  }), false);
  assert.deepEqual(cleared, [42]);
});

test('replay playback stop helper clears active timer and resets button label', () => {
  const api = loadLiveInputsTestApi();
  const cleared = [];
  const state = { replayTimer: 101 };
  const button = { textContent: 'Pause' };

  assert.equal(api.stopReplayPlayback(state, button, (timer) => {
    cleared.push(timer);
  }), true);
  assert.equal(state.replayTimer, null);
  assert.equal(button.textContent, 'Play');
  assert.deepEqual(cleared, [101]);

  assert.equal(api.stopReplayPlayback(state, button, (timer) => {
    cleared.push(timer);
  }), false);
  assert.equal(button.textContent, 'Play');
  assert.deepEqual(cleared, [101]);
});

test('active EventSource close helper closes and clears stored stream', () => {
  const api = loadLiveInputsTestApi();
  let closes = 0;
  const state = {
    liveStream: {
      close() {
        closes += 1;
      },
    },
  };

  assert.equal(api.closeActiveEventSource(state), true);
  assert.equal(closes, 1);
  assert.equal(state.liveStream, null);
  assert.equal(api.closeActiveEventSource(state), false);
  assert.equal(closes, 1);
});

test('active EventSource close helper clears stream even when close throws', () => {
  const api = loadLiveInputsTestApi();
  const state = {
    liveStream: {
      close() {
        throw new Error('already closed');
      },
    },
  };

  assert.equal(api.closeActiveEventSource(state), true);
  assert.equal(state.liveStream, null);
});

test('live hydration prune preserves loaded replay devices only while replay is active', () => {
  const api = loadLiveInputsTestApi();
  const incoming = new Set(['live-1']);
  const replayDevices = new Set(['replay-1']);

  assert.equal(api.shouldApplyLiveHydrationDevice('replay-1', {
    replayMode: true,
    replayDeviceIds: replayDevices,
  }), false);
  assert.equal(api.shouldApplyLiveHydrationDevice('replay-1', {
    replayMode: false,
    replayDeviceIds: replayDevices,
  }), true);
  assert.equal(api.shouldPruneLiveHydrationDevice('live-1', incoming, {
    replayMode: true,
    replayDeviceIds: replayDevices,
  }), false);
  assert.equal(api.shouldPruneLiveHydrationDevice('replay-1', incoming, {
    replayMode: true,
    replayDeviceIds: replayDevices,
  }), false);
  assert.equal(api.shouldPruneLiveHydrationDevice('stale-1', incoming, {
    replayMode: true,
    replayDeviceIds: replayDevices,
  }), true);
  assert.equal(api.shouldPruneLiveHydrationDevice('replay-1', incoming, {
    replayMode: false,
    replayDeviceIds: replayDevices,
  }), true);
});

test('gateway cleanup selection includes all devices linked to removed gateway', () => {
  const api = loadLiveInputsTestApi();
  const devices = new Map([
    ['tracker-a', { freshness: { gateway_id: 'relay-1' } }],
    ['tracker-b', { link: { gateway_id: 'relay-1' } }],
    ['tracker-c', { gateway_id: 'relay-2' }],
  ]);

  const linked = api.gatewayLinkedDeviceIds(devices, 'relay-1', (device) =>
    device?.freshness?.gateway_id || device?.link?.gateway_id || device?.gateway_id || null);

  assert.equal(JSON.stringify(linked), JSON.stringify(['tracker-a', 'tracker-b']));
});
