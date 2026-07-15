import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { routeDocumentSha256 } = require('./nymph_bridge_adapter.js');
const ROOT = path.resolve(new URL('..', import.meta.url).pathname);

const CAPABILITIES = {
  schema: 'veil.route-revision.v1',
  route_engine: 'bridge_virtual_stick',
  revision_acceptance: true,
  mid_flight_replacement: true,
  aircraft_resident_route: false,
};

const ROUTE = {
  route_id: 'checked-route',
  active_revision: 4,
  newest_accepted_revision: 4,
  phase: 'ready',
  target_waypoint_index: 0,
  pending_revision: null,
  pending_target_waypoint_index: null,
};

function statusResult(ageMs) {
  return {
    ok: true,
    state: 'armed',
    monitor_thread_alive: true,
    route_thread_alive: true,
    local_api: { healthy: true, socket_path: '/private/DO_NOT_EXPOSE.sock' },
    telemetry: { arrival_age_ms: ageMs, diagnostic: 'DO_NOT_EXPOSE' },
    telemetry_snapshot: {
      available: true,
      arrival_age_ms: ageMs,
      fresh: ageMs <= 350,
      stale: ageMs > 350,
      product_connected: true,
      aircraft_connected: true,
      remote_controller_connected: true,
      airlink_connected: true,
      is_flying: false,
      motors_on: true,
      flight_mode: 'P-GPS',
      latitude_deg: 43.6,
      longitude_deg: -74.1,
      relative_altitude_m: 12,
      yaw_deg: 90,
      velocity_north_mps: 0,
      velocity_east_mps: 0,
      velocity_down_mps: 0,
      gps_signal_level: 'strong',
      gps_satellite_count: 17,
      battery_percent: 75,
      authority_owner: 'veil',
      airlink_signal_quality: 90,
      serial_number: 'DO_NOT_EXPOSE',
    },
    route: { route: ROUTE, capabilities: CAPABILITIES },
    capabilities: CAPABILITIES,
    control: { armed: true, last_error: 'DO_NOT_EXPOSE' },
    authority: { owner: 'veil', mode: 'advanced' },
    token: 'DO_NOT_EXPOSE',
  };
}

async function createFakeDaemon() {
  const directory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'veil-nymph-http-'));
  const socketPath = path.join(directory, 'flight.sock');
  const commands = [];
  const sockets = new Set();
  const state = { ageMs: 25, routeAcceptMode: 'accept' };
  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    socket.setEncoding('utf8');
    socket.write(`${JSON.stringify({
      event: 'repl_ready',
      protocol: 'veil.flight-repl.v1',
    })}\n`);
    let buffer = '';
    socket.on('data', (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        const command = JSON.parse(line);
        commands.push(command);
        let result;
        if (command.command === 'status') {
          result = statusResult(state.ageMs);
        } else if (command.command === 'route_status') {
          result = { ok: true, state: 'route_status', route: ROUTE, capabilities: CAPABILITIES };
        } else if (command.command === 'route_accept' && state.routeAcceptMode === 'conflict') {
          result = {
            ok: false,
            state: 'route_revision_conflict',
            error: 'route_revision_conflict',
            message: 'DO_NOT_EXPOSE',
            issues: [{ path: 'expected_accepted_revision', message: 'DO_NOT_EXPOSE' }],
            route: ROUTE,
          };
        } else if (command.command === 'route_accept') {
          result = {
            ok: true,
            state: 'route_revision_accepted',
            accepted_revision: 4,
            route: ROUTE,
            capabilities: CAPABILITIES,
          };
        } else {
          result = {
            ok: true,
            state: command.command === 'route_start' ? 'route_started' : `${command.command}_ok`,
            route: ROUTE,
            capabilities: CAPABILITIES,
          };
        }
        socket.write(`${JSON.stringify({
          event: 'command_result',
          request_id: command.request_id,
          command: command.command,
          ...result,
        })}\n`);
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, resolve);
  });
  await fs.promises.chmod(socketPath, 0o600);
  return {
    socketPath,
    commands,
    state,
    async close() {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      await fs.promises.rm(directory, { recursive: true, force: true });
    },
  };
}

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

function request(port, pathname, options = {}) {
  const body = options.body === undefined
    ? null
    : typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
  const headers = {
    Host: `127.0.0.1:${port}`,
    ...(body === null ? {} : {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(body),
    }),
    ...options.headers,
  };
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1',
      port,
      path: pathname,
      method: options.method || 'GET',
      headers,
    }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try { json = JSON.parse(text); } catch (_error) { /* non-JSON denial */ }
        resolve({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on('error', reject);
    if (body !== null) req.write(body);
    req.end();
  });
}

async function startVeilServer(port, env) {
  const child = spawn(process.execPath, ['server.js'], {
    cwd: ROOT,
    env: { ...process.env, ...env, PORT: String(port), HOST: '127.0.0.1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '';
  child.stdout.on('data', (chunk) => { logs = `${logs}${chunk}`.slice(-20_000); });
  child.stderr.on('data', (chunk) => { logs = `${logs}${chunk}`.slice(-20_000); });
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`VEIL server exited early:\n${logs}`);
    try {
      const response = await request(port, '/');
      if (response.status === 200) return { child, get logs() { return logs; } };
    } catch (_error) { /* startup race */ }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  child.kill('SIGTERM');
  throw new Error(`Timed out starting VEIL server:\n${logs}`);
}

async function stopVeilServer(handle) {
  if (!handle || handle.child.exitCode !== null || handle.child.signalCode !== null) return;
  const exited = new Promise((resolve) => handle.child.once('exit', resolve));
  handle.child.kill('SIGTERM');
  let timer;
  await Promise.race([
    exited,
    new Promise((resolve) => { timer = setTimeout(resolve, 2_000); }),
  ]);
  clearTimeout(timer);
  if (handle.child.exitCode === null && handle.child.signalCode === null) {
    handle.child.kill('SIGKILL');
  }
}

test('Nymph HTTP boundary is host-pinned, attested, interlocked, bounded, and sanitized', async () => {
  const daemon = await createFakeDaemon();
  const port = await freePort();
  const document = '{"schema":"veil.route-revision.v1","route":"checked"}';
  let veil;
  try {
    veil = await startVeilServer(port, {
      VEIL_DJI_UNIX_SOCKET: daemon.socketPath,
      VEIL_DJI_FLIGHT_SOCKET: '',
      VEIL_DJI_ENVELOPE_READY: '1',
      VEIL_DJI_RC_TAKEOVER_VERIFIED: '1',
      VEIL_DJI_CHECKED_ROUTE_SHA256: routeDocumentSha256(document),
    });

    const status = await request(port, '/api/nymphs/dji/status');
    assert.equal(status.status, 200, veil.logs);
    assert.equal(status.headers['cache-control'], 'no-store');
    assert.equal(status.json.link.controlChannelReady, true);
    assert.equal(status.json.route.routeId, 'checked-route');
    assert.equal(status.json.route.activeRevision, 4);
    assert.doesNotMatch(status.text, /DO_NOT_EXPOSE|private\/DO_NOT_EXPOSE/);

    const beforeRebind = daemon.commands.length;
    const rebound = await request(port, '/api/nymphs/dji/status', {
      headers: { Host: `attacker.example:${port}`, Origin: `http://attacker.example:${port}` },
    });
    assert.equal(rebound.status, 403);
    assert.equal(rebound.json.error.code, 'control_host_not_allowed');
    assert.equal(daemon.commands.length, beforeRebind);

    const crossOrigin = await request(port, '/api/nymphs/dji/pause', {
      method: 'POST',
      headers: { Origin: 'https://attacker.example' },
      body: { confirm: 'PAUSE_ROUTE' },
    });
    assert.equal(crossOrigin.status, 403);

    const beforeConfirmation = daemon.commands.length;
    const wrongConfirmation = await request(port, '/api/nymphs/dji/pause', {
      method: 'POST',
      body: { confirm: 'pause' },
    });
    assert.equal(wrongConfirmation.status, 400);
    assert.equal(wrongConfirmation.json.error.code, 'command_confirmation_required');
    assert.equal(daemon.commands.length, beforeConfirmation);

    const oversized = await request(port, '/api/nymphs/dji/pause', {
      method: 'POST',
      body: `{"padding":"${'x'.repeat(1024 * 1024)}"}`,
    });
    assert.equal(oversized.status, 413);
    assert.equal(oversized.json.error.code, 'request_too_large');

    const beforeStart = daemon.commands.length;
    const unattestedStart = await request(port, '/api/nymphs/dji/start', {
      method: 'POST',
      body: { confirm: 'START_ROUTE' },
    });
    assert.equal(unattestedStart.status, 412);
    assert.equal(unattestedStart.json.error.code, 'route_execution_unattested');
    assert.equal(daemon.commands.length, beforeStart);

    const mismatchedRoute = await request(port, '/api/nymphs/dji/route-accept', {
      method: 'POST',
      body: { confirm: 'ACCEPT_CHECKED_ROUTE', document: `${document} ` },
    });
    assert.equal(mismatchedRoute.status, 412);
    assert.equal(mismatchedRoute.json.error.code, 'route_attestation_mismatch');

    const accepted = await request(port, '/api/nymphs/dji/route-accept', {
      method: 'POST',
      body: { confirm: 'ACCEPT_CHECKED_ROUTE', document },
    });
    assert.equal(accepted.status, 200, accepted.text);
    assert.equal(accepted.json.acceptedRevision, 4);
    assert.equal(daemon.commands.at(-1).command, 'route_accept');
    assert.equal(daemon.commands.at(-1).document, document);

    const started = await request(port, '/api/nymphs/dji/start', {
      method: 'POST',
      body: { confirm: 'START_ROUTE' },
    });
    assert.equal(started.status, 200, started.text);
    assert.deepEqual(daemon.commands.slice(-2).map((item) => item.command), ['status', 'route_start']);

    daemon.state.ageMs = 351;
    const beforeStaleArm = daemon.commands.filter((item) => item.command === 'arm').length;
    const staleArm = await request(port, '/api/nymphs/dji/arm', {
      method: 'POST',
      body: { confirm: 'ARM_VIRTUAL_STICK' },
    });
    assert.equal(staleArm.status, 412);
    assert.equal(staleArm.json.error.code, 'flight_control_channel_not_ready');
    assert.equal(
      daemon.commands.filter((item) => item.command === 'arm').length,
      beforeStaleArm,
    );

    daemon.state.ageMs = 25;
    const armed = await request(port, '/api/nymphs/dji/arm', {
      method: 'POST',
      body: { confirm: 'ARM_VIRTUAL_STICK' },
    });
    assert.equal(armed.status, 200, armed.text);
    assert.deepEqual(daemon.commands.slice(-2).map((item) => item.command), ['status', 'arm']);

    daemon.state.routeAcceptMode = 'conflict';
    const conflict = await request(port, '/api/nymphs/dji/route-accept', {
      method: 'POST',
      body: { confirm: 'ACCEPT_CHECKED_ROUTE', document },
    });
    assert.equal(conflict.status, 409, conflict.text);
    assert.equal(conflict.json.state, 'route_revision_conflict');
    assert.equal(conflict.json.message, null);
    assert.doesNotMatch(conflict.text, /DO_NOT_EXPOSE/);

    const missing = await request(port, '/api/nymphs/dji/not-a-route');
    assert.equal(missing.status, 404);
    assert.equal(missing.json.error.code, 'not_found');
  } finally {
    await stopVeilServer(veil);
    await daemon.close();
  }
});
