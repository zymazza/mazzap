const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const ROOT = path.resolve(__dirname, '..');
const TMP_DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'veil-live-a4-'));
process.env.TWIN_DATA_DIR = TMP_DATA;
process.env.VEIL_LIVE_GATEWAY_AUTOSTART = '0';
process.env.VEIL_LIVE_JSONL_MAX_BYTES = String(1024 * 1024);
process.env.VEIL_LIVE_JSONL_GENERATIONS = '3';
process.env.VEIL_LIVE_COMMAND_JSONL_MAX_BYTES = '90';
process.env.VEIL_LIVE_COMMAND_JSONL_GENERATIONS = '3';
process.env.VEIL_LIVE_HISTORY_MAX_LINES = '4';
process.env.VEIL_LIVE_HISTORY_TAIL_MAX_BYTES = String(1024 * 1024);
process.env.VEIL_LIVE_BRIDGE_LOG_SNAPSHOT_DEBOUNCE_MS = '20';

const liveApi = require('../server.js')._test;

function liveReq(headers = {}, remoteAddress = '127.0.0.1') {
  return { headers, socket: { remoteAddress } };
}

function liveQuery(token) {
  const query = new URLSearchParams();
  if (token) query.set('token', token);
  return query;
}

function functionBody(source, name) {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `expected function ${name} in server.js`);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') depth -= 1;
    if (depth === 0) return source.slice(open + 1, i);
  }
  throw new Error(`unterminated function ${name}`);
}

test('live auth is open by default when no live token is configured', () => {
  const warnings = [];
  const warn = (message) => warnings.push(message);
  // Local-first: with no token, any caller (loopback or remote) is allowed so
  // the live panel works with zero setup, and we warn once advising a token
  // for web exposure.
  assert.equal(
    liveApi.liveAuthorized(liveReq({}, '127.0.0.1'), liveQuery(), { token: null, warn }),
    true,
  );
  assert.equal(
    liveApi.liveAuthorized(liveReq({}, '203.0.113.7'), liveQuery(), { token: null, warn }),
    true,
  );
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /no token set/i);
  assert.match(warnings[0], /data\/\.live_token/);
});

test('live auth accepts env and file tokens through bearer, legacy header, or query token', () => {
  const tokenPath = path.join(TMP_DATA, '.live_token');
  const previousEnvToken = process.env.VEIL_LIVE_TOKEN;
  delete process.env.VEIL_LIVE_TOKEN;
  fs.writeFileSync(tokenPath, 'file-secret\n');

  try {
    assert.equal(liveApi.liveToken(), 'file-secret');
    assert.equal(liveApi.liveAuthorized(
      liveReq({ authorization: 'Bearer file-secret' }),
      liveQuery(),
    ), true);
    assert.equal(liveApi.liveAuthorized(
      liveReq({ 'x-veil-live-token': 'file-secret' }),
      liveQuery(),
    ), true);
    assert.equal(liveApi.liveAuthorized(liveReq(), liveQuery('file-secret')), true);
    assert.equal(liveApi.liveAuthorized(
      liveReq({ authorization: 'Bearer wrong-secret' }),
      liveQuery(),
    ), false);

    process.env.VEIL_LIVE_TOKEN = 'env-secret';
    assert.equal(liveApi.liveToken(), 'env-secret');
    assert.equal(liveApi.liveAuthorized(
      liveReq({ authorization: 'Bearer env-secret' }),
      liveQuery(),
    ), true);
    assert.equal(liveApi.liveAuthorized(liveReq(), liveQuery('file-secret')), false);
  } finally {
    if (previousEnvToken === undefined) delete process.env.VEIL_LIVE_TOKEN;
    else process.env.VEIL_LIVE_TOKEN = previousEnvToken;
    fs.rmSync(tokenPath, { force: true });
  }
});

test('a configured token still locks the live API for everyone without it', () => {
  // The open default only applies when NO token is set. Once a token is
  // configured (the web-publishing path), missing/wrong tokens are denied
  // regardless of source address.
  assert.equal(
    liveApi.liveAuthorized(liveReq({}, '203.0.113.7'), liveQuery(), { token: 'web-secret' }),
    false,
  );
  assert.equal(
    liveApi.liveAuthorized(
      liveReq({ authorization: 'Bearer web-secret' }, '203.0.113.7'),
      liveQuery(),
      { token: 'web-secret' },
    ),
    true,
  );
});

function fakeRes() {
  return {
    status: null,
    body: null,
    writeHead(status) { this.status = status; },
    end(body) { this.body = body ? JSON.parse(body) : null; },
  };
}

function streamReq(remoteAddress, payload) {
  const { Readable } = require('node:stream');
  const req = Readable.from([JSON.stringify(payload)]);
  req.headers = {};
  req.socket = { remoteAddress };
  return req;
}

test('live token can only be managed from loopback and respects the env lock', async () => {
  const tokenPath = path.join(TMP_DATA, '.live_token');
  const prevEnv = process.env.VEIL_LIVE_TOKEN;
  delete process.env.VEIL_LIVE_TOKEN;
  fs.rmSync(tokenPath, { force: true });
  try {
    // remote callers can read status but cannot set the token
    const status = fakeRes();
    liveApi.handleLiveTokenStatus(liveReq({}, '203.0.113.7'), status);
    assert.equal(status.body.can_manage, false);
    assert.equal(status.body.protected, false);

    const remote = fakeRes();
    liveApi.handleLiveTokenSet(streamReq('203.0.113.7', { generate: true }), remote);
    assert.equal(remote.status, 403);
    assert.equal(fs.existsSync(tokenPath), false);

    // loopback generate writes the file and returns the token
    const gen = fakeRes();
    await new Promise((resolve) => {
      const res = fakeRes();
      const orig = res.end.bind(res);
      res.end = (b) => { orig(b); Object.assign(gen, res); resolve(); };
      liveApi.handleLiveTokenSet(streamReq('127.0.0.1', { generate: true }), res);
    });
    assert.equal(gen.status, 200);
    assert.equal(gen.body.protected, true);
    assert.match(gen.body.token, /^[0-9a-f]{32}$/);
    assert.equal(fs.readFileSync(tokenPath, 'utf8').trim(), gen.body.token);

    // env-set token blocks file management with a 409
    process.env.VEIL_LIVE_TOKEN = 'env-secret';
    const locked = fakeRes();
    liveApi.handleLiveTokenSet(streamReq('127.0.0.1', { generate: true }), locked);
    assert.equal(locked.status, 409);
  } finally {
    if (prevEnv === undefined) delete process.env.VEIL_LIVE_TOKEN;
    else process.env.VEIL_LIVE_TOKEN = prevEnv;
    fs.rmSync(tokenPath, { force: true });
  }
});

test('rememberLiveEvent hot path does not use sync fs APIs or spawn directly', () => {
  const source = fs.readFileSync(path.join(ROOT, 'server.js'), 'utf8');
  const body = functionBody(source, 'rememberLiveEvent');

  assert.doesNotMatch(body, /\bfs\.\w+Sync\b/);
  assert.doesNotMatch(body, /\bspawn\s*\(/);
  assert.doesNotMatch(body, /\bsaveLiveRegistry\s*\(/);
  assert.match(body, /\bqueueLiveRegistrySave\s*\(/);
  assert.match(body, /\bqueueLiveEventFiles\s*\(/);
  assert.match(body, /\bqueueLiveDbAppend\s*\(/);
});

test('live_store.py subprocesses use the resolved live-store Python', () => {
  const source = fs.readFileSync(path.join(ROOT, 'server.js'), 'utf8');
  const appendBody = functionBody(source, 'runLiveDbAppend');
  const exportBody = functionBody(source, 'handleLiveExport');

  assert.match(appendBody, /spawn\(LIVE_STORE_PYTHON,\s*\[path\.join\(ROOT, 'scripts', 'live', 'live_store\.py'\), 'append'\]/);
  assert.match(exportBody, /spawn\(LIVE_STORE_PYTHON,\s*\[path\.join\(ROOT, 'scripts', 'live', 'live_store\.py'\), 'export'\]/);
  assert.doesNotMatch(appendBody, /spawn\(MCP_PYTHON,/);
  assert.doesNotMatch(exportBody, /spawn\(MCP_PYTHON,/);
});

test('live-store Python resolver skips candidates without geospatial imports', () => {
  const checked = [];
  const warnings = [];
  const selected = liveApi.resolveLiveStorePython({
    env: {},
    root: '/repo',
    mcpPython: '/broken-mcp/bin/python',
    livePython: '/repo/.venv-live/bin/python',
    canImportGeoDeps(candidate) {
      checked.push(candidate);
      return candidate === '/repo/.venv-mcp/bin/python';
    },
    warn(message) {
      warnings.push(message);
    },
  });

  assert.equal(selected, '/repo/.venv-mcp/bin/python');
  assert.deepEqual(checked, ['/broken-mcp/bin/python', '/repo/.venv-mcp/bin/python']);
  assert.deepEqual(warnings, []);
});

test('live-store Python resolver warns before falling back when no candidate has geospatial imports', () => {
  const warnings = [];
  const selected = liveApi.resolveLiveStorePython({
    env: {},
    root: '/repo',
    mcpPython: '/broken-mcp/bin/python',
    livePython: '/repo/.venv-live/bin/python',
    canImportGeoDeps() {
      return false;
    },
    warn(message) {
      warnings.push(message);
    },
  });

  assert.equal(selected, '/broken-mcp/bin/python');
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /No Python interpreter could import live-store export dependencies/);
  assert.match(warnings[0], /VEIL_LIVE_STORE_PYTHON/);
});

test('normalizeLiveEvent canonicalizes timestamp formats and rejects invalid timestamps', () => {
  const event = liveApi.normalizeLiveEvent({
    kind: 'position',
    device_id: 'dev-time',
    observed_at: '2026-01-02T03:04:05.987+00:00',
    received_at: '2026-01-02T03:04:07.123Z',
    position: { lat: 39.98, lon: -105.27 },
  });

  assert.equal(event.observed_at, '2026-01-02T03:04:05Z');
  assert.equal(event.received_at, '2026-01-02T03:04:07Z');

  assert.throws(
    () => liveApi.normalizeLiveEvent({
      kind: 'position',
      device_id: 'dev-time',
      observed_at: '2026-01-02T03:04:05',
      position: { lat: 39.98, lon: -105.27 },
    }),
    /observed_at must include an explicit timezone/,
  );
  assert.throws(
    () => liveApi.normalizeLiveEvent({
      kind: 'position',
      device_id: 'dev-time',
      observed_at: 'not-a-time',
      position: { lat: 39.98, lon: -105.27 },
    }),
    /observed_at must include an explicit timezone|observed_at must be an ISO timestamp/,
  );
  assert.throws(
    () => liveApi.normalizeLiveEvent({
      kind: 'position',
      device_id: 'dev-time',
      observed_at: '2026-01-02T03:04:05Z',
      received_at: 'not-a-time',
      position: { lat: 39.98, lon: -105.27 },
    }),
    /received_at must include an explicit timezone|received_at must be an ISO timestamp/,
  );
});

test('liveSnapshot filters explicit gateway self-node ids without BLE address inference', () => {
  const previousLatest = new Map(liveApi.liveLatest);
  liveApi.liveLatest.clear();

  try {
    const reg = {
      version: 1,
      gateways: [
        {
          id: 'gateway-a',
          name: 'Gateway A',
          transport: 'bluetooth',
          address: 'AA:BB:CC:DD:EE:FF',
          node_id: '287454020',
        },
      ],
      devices: {
        '!ccddeeff': { label: 'gateway self from BLE' },
        '!11223344': { label: 'gateway self from node_id' },
      },
    };

    liveApi.liveLatest.set('!ccddeeff', {
      device_id: '!ccddeeff',
      observed_at: '2026-01-02T03:04:05Z',
      received_at: '2026-01-02T03:04:06Z',
      position: { lat: 39.98, lon: -105.27 },
    });
    liveApi.liveLatest.set('!11223344', {
      device_id: '!11223344',
      observed_at: '2026-01-02T03:04:05Z',
      received_at: '2026-01-02T03:04:06Z',
      position: { lat: 40.08, lon: -105.27 },
    });
    for (let i = 0; i < 25; i += 1) {
      const deviceId = `device-${i}`;
      reg.devices[deviceId] = { label: `Device ${i}` };
      liveApi.liveLatest.set(deviceId, {
        device_id: deviceId,
        observed_at: '2026-01-02T03:04:05Z',
        received_at: '2026-01-02T03:04:06Z',
        position: { lat: 39.98 + i * 0.001, lon: -105.27 },
      });
    }

    let gatewayIdComputations = 0;
    const snapshot = liveApi.liveSnapshot({
      reg,
      computeGatewayNodeIds(inputReg) {
        gatewayIdComputations += 1;
        return liveApi.liveGatewayNodeIds(inputReg);
      },
    });

    assert.equal(gatewayIdComputations, 1);
    assert.equal(snapshot.devices.length, 26);
    assert.equal(snapshot.devices.some((device) => device.device_id === '!ccddeeff'), true);
    assert.equal(snapshot.devices.some((device) => device.device_id === '!11223344'), false);
    assert.equal(Object.prototype.hasOwnProperty.call(snapshot.preferences, '!ccddeeff'), true);
    assert.equal(Object.prototype.hasOwnProperty.call(snapshot.preferences, '!11223344'), false);
  } finally {
    liveApi.liveLatest.clear();
    previousLatest.forEach((value, key) => liveApi.liveLatest.set(key, value));
  }
});

test('liveSnapshot hides unconfigured Meshtastic node defaults unless discovery is enabled', () => {
  const previousLatest = new Map(liveApi.liveLatest);
  liveApi.liveLatest.clear();

  try {
    const reg = {
      version: 1,
      gateways: [{ id: 'gateway-a', name: 'Gateway A', transport: 'bluetooth', address: 'ble-a' }],
      devices: {
        '!28cc7953': { label: 'Khadijah Tracker', gateway_id: 'gateway-a' },
        '!050da23c': { label: 'Meshtastic a23c', gateway_id: 'gateway-a' },
      },
    };
    liveApi.liveLatest.set('!28cc7953', {
      device_id: '!28cc7953',
      label: 'Khadijah Tracker',
      observed_at: '2026-01-02T03:04:05Z',
      received_at: '2026-01-02T03:04:06Z',
      position: { lat: 39.98, lon: -105.27 },
      link: { gateway_id: 'gateway-a' },
    });
    liveApi.liveLatest.set('!050da23c', {
      device_id: '!050da23c',
      label: 'Meshtastic a23c',
      observed_at: '2026-01-02T03:04:05Z',
      received_at: '2026-01-02T03:04:06Z',
      position: { lat: 39.80, lon: -105.27 },
      link: { gateway_id: 'gateway-a' },
    });

    const normal = liveApi.liveSnapshot({ reg });
    assert.deepEqual(normal.devices.map((device) => device.device_id), ['!28cc7953']);
    assert.equal(normal.devices[0].configured, true);

    const discovery = liveApi.liveSnapshot({ reg, includeDiscovered: true });
    assert.deepEqual(
      discovery.devices.map((device) => device.device_id).sort(),
      ['!050da23c', '!28cc7953'],
    );
    assert.equal(discovery.devices.find((device) => device.device_id === '!050da23c').configured, false);
  } finally {
    liveApi.liveLatest.clear();
    previousLatest.forEach((value, key) => liveApi.liveLatest.set(key, value));
  }
});

test('rememberLiveEvent streams unconfigured devices only to discovery clients', () => {
  const previousLatest = new Map(liveApi.liveLatest);
  const reg = liveApi.liveRegistry();
  const previousDevices = reg.devices;
  liveApi.liveLatest.clear();
  reg.devices = {};

  const normalClient = {
    writes: [],
    write(payload) { this.writes.push(payload); },
  };
  const discoveryClient = {
    __liveOptions: { includeDiscovered: true },
    writes: [],
    write(payload) { this.writes.push(payload); },
  };
  liveApi.liveStreams.add(normalClient);
  liveApi.liveStreams.add(discoveryClient);

  try {
    liveApi.rememberLiveEvent(liveApi.normalizeLiveEvent({
      kind: 'position',
      device_id: '!050da23c',
      label: 'Meshtastic a23c',
      observed_at: '2026-01-02T03:04:05Z',
      position: { lat: 39.80, lon: -105.27 },
    }));

    assert.equal(normalClient.writes.length, 0);
    assert.equal(discoveryClient.writes.length, 1);
    assert.equal(reg.devices['!050da23c'], undefined);

    reg.devices['!050da23c'] = { label: 'Ridge Tracker', configured: true };
    liveApi.rememberLiveEvent(liveApi.normalizeLiveEvent({
      kind: 'position',
      device_id: '!050da23c',
      label: 'Meshtastic a23c',
      observed_at: '2026-01-02T03:04:06Z',
      position: { lat: 39.90, lon: -105.27 },
    }));

    assert.equal(normalClient.writes.length, 1);
    assert.equal(discoveryClient.writes.length, 2);
    const normalEvent = JSON.parse(normalClient.writes[0].split('\ndata: ')[1].trim());
    assert.equal(normalEvent.label, 'Ridge Tracker');
    assert.equal(normalEvent.configured, true);
  } finally {
    liveApi.liveStreams.delete(normalClient);
    liveApi.liveStreams.delete(discoveryClient);
    liveApi.liveLatest.clear();
    previousLatest.forEach((value, key) => liveApi.liveLatest.set(key, value));
    reg.devices = previousDevices;
  }
});

test('bridge log snapshots are coalesced and include the latest line', async () => {
  const reg = liveApi.liveRegistry();
  const previousGateways = reg.gateways;
  const bridge = {
    state: 'running',
    proc: { pid: 12345 },
    started_at: '2026-01-02T03:04:05Z',
    desired: true,
    last_line: null,
    error: null,
  };
  const gateway = {
    id: 'gateway-log-test',
    name: 'Gateway Log Test',
    transport: 'serial',
    address: '/dev/null',
  };
  const client = {
    __liveOptions: { includeDiscovered: true },
    writes: [],
    write(payload) {
      this.writes.push(payload);
    },
  };

  reg.gateways = [gateway];
  liveApi.liveBridgeProcesses.set(gateway.id, bridge);
  liveApi.liveStreams.add(client);
  try {
    for (let i = 0; i < 5; i += 1) {
      liveApi.captureLiveBridgeOutput(bridge, gateway, `line-${i}\n`, false, () => {});
    }

    assert.equal(client.writes.length, 0);
    assert.equal(bridge.last_line, 'line-4');
    assert.equal(bridge.error, null);

    await new Promise((resolve) => setTimeout(resolve, 60));

    assert.equal(client.writes.length, 1);
    const snapshot = JSON.parse(client.writes[0].split('\ndata: ')[1].trim());
    assert.equal(snapshot.gateways[0].bridge.last_line, 'line-4');

    liveApi.captureLiveBridgeOutput(bridge, gateway, 'important stderr\n', true, () => {});
    assert.equal(bridge.error, 'important stderr');

    await new Promise((resolve) => setTimeout(resolve, 60));

    assert.equal(client.writes.length, 2);
    const secondSnapshot = JSON.parse(client.writes[1].split('\ndata: ')[1].trim());
    assert.equal(secondSnapshot.gateways[0].bridge.error, 'important stderr');
  } finally {
    liveApi.liveStreams.delete(client);
    liveApi.liveBridgeProcesses.delete(gateway.id);
    reg.gateways = previousGateways;
  }
});

test('queued live persistence preserves event file order and serializes DB appends', async () => {
  await liveApi.drainLivePersistenceForTest();
  const dbStarted = [];
  const dbFinished = [];
  let activeDbAppends = 0;
  let maxActiveDbAppends = 0;

  liveApi.setLiveDbAppendRunnerForTest(async (event) => {
    dbStarted.push(event.message);
    activeDbAppends += 1;
    maxActiveDbAppends = Math.max(maxActiveDbAppends, activeDbAppends);
    await new Promise((resolve) => setTimeout(resolve, 5));
    activeDbAppends -= 1;
    dbFinished.push(event.message);
  });

  const client = {
    writes: [],
    write(payload) {
      this.writes.push(payload);
    },
  };
  liveApi.liveStreams.add(client);

  const base = Date.UTC(2026, 0, 2, 3, 4, 0);
  for (const [idx, message] of ['one', 'two', 'three'].entries()) {
    const event = liveApi.normalizeLiveEvent({
      kind: 'position',
      device_id: 'dev-a',
      label: 'Device A',
      message,
      observed_at: new Date(base + idx * 1000).toISOString(),
      position: { lat: 39.98 + idx * 0.001, lon: -105.27 },
    });
    liveApi.rememberLiveEvent(event);
  }

  assert.equal(liveApi.liveLatest.get('dev-a').message, 'three');
  assert.equal(client.writes.length, 3);

  await Promise.resolve();
  assert.deepEqual(dbStarted, ['one']);

  await liveApi.drainLivePersistenceForTest();

  assert.deepEqual(dbStarted, ['one', 'two', 'three']);
  assert.deepEqual(dbFinished, ['one', 'two', 'three']);
  assert.equal(maxActiveDbAppends, 1);

  const eventsPath = path.join(TMP_DATA, 'live', 'events.jsonl');
  const events = (await fsp.readFile(eventsPath, 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line).message)
    .filter((message) => message !== undefined);
  assert.deepEqual(events, ['one', 'two', 'three']);

  const dailyPath = path.join(TMP_DATA, 'live', 'daily', '2026-01-02.jsonl');
  const dailyEvents = (await fsp.readFile(dailyPath, 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line).message)
    .filter((message) => message !== undefined);
  assert.deepEqual(dailyEvents, ['one', 'two', 'three']);

  liveApi.liveStreams.delete(client);
  liveApi.setLiveDbAppendRunnerForTest(null);
});

test('bounded tail reader returns recent non-empty lines without requiring whole file', () => {
  const filePath = path.join(TMP_DATA, 'tail-reader.jsonl');
  fs.writeFileSync(filePath, [
    'older-ignored',
    '',
    JSON.stringify({ n: 1 }),
    JSON.stringify({ n: 2 }),
    JSON.stringify({ n: 3 }),
    JSON.stringify({ n: 4 }),
  ].join('\n') + '\n');

  assert.deepEqual(
    liveApi.readLastNonEmptyLinesSync(filePath, 3, 128).map((line) => JSON.parse(line).n),
    [2, 3, 4],
  );
  assert.equal(liveApi.readLastNonEmptyLinesSync(filePath, 100, 24).includes('older-ignored'), false);
});

test('rotating JSONL append bounds current file and keeps configured generations', async () => {
  const filePath = path.join(TMP_DATA, 'rotation', 'events.jsonl');
  for (let i = 0; i < 11; i += 1) {
    await liveApi.appendJsonlRotating(filePath, `${JSON.stringify({ i, pad: 'xxxxx' })}\n`, {
      maxBytes: 70,
      generations: 2,
    });
  }

  assert.ok(fs.existsSync(filePath));
  assert.ok(fs.existsSync(`${filePath}.1`));
  assert.ok(fs.existsSync(`${filePath}.2`));
  assert.equal(fs.existsSync(`${filePath}.3`), false);
  assert.ok(fs.statSync(filePath).size <= 70);

  const lines = await liveApi.readRecentJsonlLines(filePath, {
    maxLines: 20,
    maxBytes: 1024,
    generations: 2,
  });
  const values = lines.map((line) => JSON.parse(line).i);
  assert.deepEqual(values, [3, 4, 5, 6, 7, 8, 9, 10]);
});

test('live history loads a bounded async recent window across rotated daily files', async () => {
  const dailyPath = path.join(TMP_DATA, 'live', 'daily', '2026-01-03.jsonl');
  await fsp.mkdir(path.dirname(dailyPath), { recursive: true });
  await fsp.writeFile(`${dailyPath}.1`, [
    { device_id: 'dev-a', observed_at: '2026-01-03T00:00:01Z', message: 'old-a' },
    { device_id: 'dev-b', observed_at: '2026-01-03T00:00:02Z', message: 'old-b' },
  ].map((event) => JSON.stringify(event)).join('\n') + '\n');
  await fsp.writeFile(dailyPath, [
    { device_id: 'dev-a', observed_at: '2026-01-03T00:00:05Z', message: 'newer-a' },
    { device_id: 'dev-b', observed_at: '2026-01-03T00:00:03Z', message: 'new-b' },
    { device_id: 'dev-a', observed_at: '2026-01-03T00:00:04Z', message: 'new-a' },
    { device_id: 'dev-a', observed_at: '2026-01-03T00:00:06Z', message: 'newest-a' },
    { device_id: 'dev-a', observed_at: '2026-01-03T00:00:07Z', message: 'bounded-out' },
  ].map((event) => JSON.stringify(event)).join('\n') + '\n');

  const events = await liveApi.loadLiveHistoryEvents('2026-01-03', new Set(['dev-a']), {
    maxLines: 5,
    maxBytes: 1024,
    generations: 1,
  });

  assert.deepEqual(events.map((event) => event.message), ['new-a', 'newer-a', 'newest-a', 'bounded-out']);
});

test('queued command appends are async, ordered, and rotate per gateway', async () => {
  for (let i = 0; i < 7; i += 1) {
    liveApi.queueLiveCommandFile('gateway-a', { id: String(i), command: 'c', device_id: 'd' });
  }
  await liveApi.drainLivePersistenceForTest();

  const commandPath = path.join(liveApi.LIVE_COMMAND_DIR, 'gateway-a.jsonl');
  assert.ok(fs.existsSync(commandPath));
  assert.ok(fs.existsSync(`${commandPath}.1`));
  assert.equal(fs.existsSync(`${commandPath}.4`), false);
  assert.ok(fs.statSync(commandPath).size <= Number(process.env.VEIL_LIVE_COMMAND_JSONL_MAX_BYTES));

  const lines = await liveApi.readRecentJsonlLines(commandPath, {
    maxLines: 20,
    maxBytes: 1024,
    generations: 3,
  });
  assert.deepEqual(lines.map((line) => JSON.parse(line).id), ['0', '1', '2', '3', '4', '5', '6']);
});
