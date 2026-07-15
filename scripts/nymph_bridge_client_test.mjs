import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadClientApi() {
  const source = fs.readFileSync(new URL('../public/nymph-bridge-client.js', import.meta.url), 'utf8');
  const window = {
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(source, {
    window,
    AbortController,
    Error,
    JSON,
    setTimeout,
    clearTimeout,
  });
  return window.VEILNymphBridgeClient;
}

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
  };
}

test('offline snapshots fail closed and expose no executable capability', () => {
  const api = loadClientApi();
  const status = JSON.parse(JSON.stringify(api._test.offlineStatus({
    code: 'bridge_unavailable',
    message: 'offline',
  })));

  assert.equal(status.link.bridgeConnected, false);
  assert.equal(status.link.controlChannelReady, false);
  assert.equal(status.control.armed, false);
  assert.equal(status.capabilities.virtualStick, false);
  assert.equal(status.capabilities.routeRevisions, false);
  assert.equal(status.route, null);
  assert.ok(api._test.COMMANDS.land.timeoutMs >= 120000);
});

test('polling uses same-origin, no-store requests and publishes bridge status', async () => {
  const api = loadClientApi();
  const calls = [];
  const ready = {
    ok: true,
    link: { bridgeConnected: true, controlChannelReady: true },
    telemetry: { received_at: 1234 },
    control: { armed: false },
  };
  const client = api.create({
    fetch: async (path, options) => {
      calls.push({ path, options });
      return response(ready);
    },
  });
  let published = null;
  client.setStatusSink((status) => { published = status; });

  const status = await client.pollNow();

  assert.equal(status, ready);
  assert.equal(published, ready);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/nymphs/dji/status');
  assert.equal(calls[0].options.method, 'GET');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(calls[0].options.body, undefined);
});

test('explicit controls send only allowlisted commands and exact confirmations', async () => {
  const api = loadClientApi();
  const calls = [];
  const client = api.create({
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (options.method === 'POST') return response({ ok: true, applied: true });
      return response({ ok: true, link: {}, capabilities: {}, control: { armed: true } });
    },
  });

  const armed = await client.arm({ mode: 'GUIDED-VS' });
  const paused = await client.pauseRoute();
  const rejectedDirect = await client.arm({ mode: 'DIRECT' });
  const rejectedNative = await client.arm({ mode: 'GUIDED-N' });

  assert.equal(armed.applied, true);
  assert.equal(paused.ok, true);
  assert.equal(rejectedDirect.applied, false);
  assert.equal(rejectedNative.applied, false);
  const posts = calls.filter((call) => call.options.method === 'POST');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].path, '/api/nymphs/dji/arm');
  assert.deepEqual(JSON.parse(posts[0].options.body), { confirm: 'ARM_VIRTUAL_STICK' });
  assert.equal(posts[1].path, '/api/nymphs/dji/pause');
  assert.deepEqual(JSON.parse(posts[1].options.body), { confirm: 'PAUSE_ROUTE' });
  assert.equal(posts.some((call) => /token|socket/i.test(call.options.body || '')), false);
});

test('a failed poll immediately clears readiness and never issues a mutation', async () => {
  const api = loadClientApi();
  let calls = 0;
  const client = api.create({
    fetch: async () => {
      calls += 1;
      throw new Error('connection dropped');
    },
  });

  const status = await client.pollNow();

  assert.equal(calls, 1);
  assert.equal(status.link.bridgeConnected, false);
  assert.equal(status.link.controlChannelReady, false);
  assert.equal(status.control.armed, false);
  assert.equal(status.error.code, 'bridge_unavailable');
});

test('HTTP error details are reduced to a public code and message', async () => {
  const api = loadClientApi();
  const client = api.create({
    fetch: async () => response({
      error: {
        code: 'flight_policy_not_ready',
        message: 'execution interlocked',
        private_detail: 'must not be copied to Error',
      },
    }, 412),
  });

  await assert.rejects(
    client.pauseRoute(),
    (error) => error.code === 'flight_policy_not_ready'
      && error.status === 412
      && !Object.hasOwn(error, 'private_detail'),
  );
});
