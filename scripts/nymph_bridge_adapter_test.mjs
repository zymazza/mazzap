import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const {
  NymphBridgeError,
  NymphBridgeService,
  UnixFlightClient,
  projectCommandResult,
  projectStatus,
  publicBridgeError,
  routeDocumentSha256,
  safeRoute,
  safeTelemetry,
} = require('./nymph_bridge_adapter.js');

const CAPABILITIES = {
  schema: 'veil.route-revision.v1',
  route_engine: 'bridge_virtual_stick',
  revision_acceptance: true,
  mid_flight_replacement: true,
  aircraft_resident_route: false,
};

function rawStatus(ageMs = 25) {
  return {
    ok: true,
    state: 'armed',
    monitor_thread_alive: true,
    route_thread_alive: true,
    local_api: { healthy: true, socket_path: '/private/flight.sock' },
    telemetry: { arrival_age_ms: ageMs, private_diagnostic: 'DO_NOT_EXPOSE' },
    telemetry_snapshot: {
      available: true,
      arrival_age_ms: ageMs,
      fresh: ageMs <= 350,
      stale: ageMs > 350,
      product_connected: true,
      aircraft_connected: true,
      remote_controller_connected: true,
      airlink_connected: true,
      is_flying: true,
      motors_on: true,
      flight_mode: 'P-GPS',
      latitude_deg: 43.6,
      longitude_deg: -74.1,
      relative_altitude_m: 24.5,
      yaw_deg: 90,
      velocity_north_mps: 1,
      velocity_east_mps: 2,
      velocity_down_mps: -0.5,
      gps_signal_level: 'strong',
      gps_satellite_count: 18,
      battery_percent: 72,
      authority_owner: 'veil',
      airlink_signal_quality: 88,
      aircraft_serial_number: 'DO_NOT_EXPOSE',
    },
    route: {
      route: {
        route_id: 'route-a',
        active_revision: 7,
        newest_accepted_revision: 8,
        phase: 'running',
        target_waypoint_index: 2,
        pending_revision: 8,
        pending_target_waypoint_index: 3,
      },
      capabilities: CAPABILITIES,
    },
    capabilities: CAPABILITIES,
    control: { armed: true, last_error: 'DO_NOT_EXPOSE' },
    authority: { owner: 'veil', mode: 'advanced' },
    token: 'DO_NOT_EXPOSE',
  };
}

async function fakeUnixDaemon(onCommand) {
  const directory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'veil-nymph-adapter-'));
  const socketPath = path.join(directory, 'flight.sock');
  let connections = 0;
  const commands = [];
  const sockets = new Set();
  const server = net.createServer((socket) => {
    connections += 1;
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
        onCommand({ socket, command, commands });
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
    get connections() { return connections; },
    async close() {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      await fs.promises.rm(directory, { recursive: true, force: true });
    },
  };
}

test('status projection follows the daemon flat route contract and fails stale telemetry closed', () => {
  const fresh = projectStatus(rawStatus(25), {
    nowMs: 10_000,
    envelopeReady: true,
    rcTakeoverVerified: true,
  });
  assert.deepEqual(fresh.route, {
    routeId: 'route-a',
    phase: 'running',
    activeRevision: 7,
    newestAcceptedRevision: 8,
    targetWaypointIndex: 2,
    pendingRevision: 8,
    pendingTargetWaypointIndex: 3,
  });
  assert.equal(fresh.link.controlChannelReady, true);
  assert.equal(fresh.telemetry.received_at, 9_975);
  assert.equal(fresh.control.lastError, null);
  assert.doesNotMatch(JSON.stringify(fresh), /DO_NOT_EXPOSE|private\/flight\.sock/);

  const stale = projectStatus(rawStatus(351), { nowMs: 10_000 });
  assert.equal(stale.telemetry.fresh, false);
  assert.equal(stale.link.controlChannelReady, false);

  const disarmed = rawStatus(25);
  disarmed.state = 'disarmed';
  disarmed.control.armed = false;
  assert.equal(projectStatus(disarmed, { nowMs: 10_000 }).aircraftMode, 'MANUAL');
});

test('scalar and revision projections reject coercion and unsafe integers', () => {
  const telemetry = safeTelemetry(
    { available: true, arrival_age_ms: '0', fresh: true, stale: false },
    1_000,
  );
  assert.equal(telemetry.arrival_age_ms, null);
  assert.equal(telemetry.fresh, false);
  assert.equal(safeRoute({ active_revision: Number.MAX_SAFE_INTEGER + 1 }).activeRevision, null);
});

test('public bridge errors never expose trusted diagnostic details', () => {
  const error = new NymphBridgeError('test_failure', 'Safe message', {
    httpStatus: 418,
    details: { token: 'DO_NOT_EXPOSE' },
  });
  assert.deepEqual(publicBridgeError(error).toPublic(), {
    code: 'test_failure',
    message: 'Safe message',
  });
});

test('command projections drop free-form daemon and validation messages', () => {
  const projected = projectCommandResult({
    ok: false,
    state: 'failed',
    error: 'route_parse_error',
    message: 'DO_NOT_EXPOSE',
    issues: [{ path: 'plan.waypoints[0]', message: 'DO_NOT_EXPOSE' }],
    details: { token: 'DO_NOT_EXPOSE' },
  });
  assert.equal(projected.message, null);
  assert.deepEqual(projected.issues, [{ path: 'plan.waypoints[0]' }]);
  assert.doesNotMatch(JSON.stringify(projected), /DO_NOT_EXPOSE/);
});

test('route acceptance is bound to the exact server-attested document', async () => {
  const document = '{"schema":"veil.route-revision.v1"}';
  const calls = [];
  const client = {
    connectionEpoch: 0,
    async command(command, options) {
      calls.push({ command, options });
      return { ok: true, state: 'route_revision_accepted', accepted_revision: 1 };
    },
    close() {},
  };
  const service = new NymphBridgeService({
    client,
    envelopeReady: true,
    checkedRouteSha256: routeDocumentSha256(document),
  });
  await assert.rejects(
    service.execute('route_start'),
    (error) => error.code === 'route_execution_unattested' && error.httpStatus === 412,
  );
  await assert.rejects(
    service.acceptRoute(`${document} `),
    (error) => error.code === 'route_attestation_mismatch' && error.httpStatus === 412,
  );
  const result = await service.acceptRoute(document);
  assert.equal(result.ok, true);
  assert.deepEqual(calls[0].command, { command: 'route_accept', document });
  await service.execute('route_start');
  client.connectionEpoch += 1;
  await assert.rejects(
    service.execute('route_resume'),
    (error) => error.code === 'route_execution_unattested' && error.httpStatus === 412,
  );

  const unbound = new NymphBridgeService({ client, envelopeReady: true });
  await assert.rejects(
    unbound.acceptRoute(document),
    (error) => error.code === 'route_attestation_unavailable' && error.httpStatus === 412,
  );
  assert.equal(routeDocumentSha256(document), crypto.createHash('sha256').update(document).digest('hex'));
});

test('land uses a timeout beyond the daemon 90-second ground observation bound', async () => {
  let observedOptions;
  const service = new NymphBridgeService({
    client: {
      async command(_command, options) {
        observedOptions = options;
        return { ok: true, state: 'grounded_confirmed' };
      },
      close() {},
    },
  });
  await service.execute('land');
  assert.equal(observedOptions.timeoutMs, 120_000);
});

test('an untagged synchronous daemon failure resolves the sole pending request', async (t) => {
  const daemon = await fakeUnixDaemon(({ socket }) => {
    socket.write(`${JSON.stringify({
      event: 'command_result',
      ok: false,
      state: 'failed',
      error: 'route_unavailable',
      message: 'no accepted route is loaded',
    })}\n`);
  });
  t.after(() => daemon.close());
  const client = new UnixFlightClient({ socketPath: daemon.socketPath, commandTimeoutMs: 200 });
  t.after(() => client.close());
  const result = await client.command({ command: 'route_start' });
  assert.equal(result.error, 'route_unavailable');
});

test('concurrent mutations are rejected while status bypasses the mutation lock', async (t) => {
  const daemon = await fakeUnixDaemon(({ socket, command }) => {
    if (command.command === 'status') {
      socket.write(`${JSON.stringify({
        event: 'command_result',
        request_id: command.request_id,
        ok: true,
        state: 'armed',
      })}\n`);
    }
    // Deliberately never settle route_pause so its outcome becomes unknown.
  });
  t.after(() => daemon.close());
  const client = new UnixFlightClient({ socketPath: daemon.socketPath, commandTimeoutMs: 35 });
  t.after(() => client.close());

  const first = client.command({ command: 'route_pause' });
  const rejected = client.command({ command: 'route_abort' });
  const status = client.command({ command: 'status' });
  const firstAssertion = assert.rejects(first, (error) => (
    error.code === 'flight_command_outcome_unknown' && error.httpStatus === 504
  ));
  const rejectedAssertion = assert.rejects(
    rejected,
    (error) => error.code === 'flight_command_busy',
  );
  assert.equal((await status).state, 'armed');
  await rejectedAssertion;
  await firstAssertion;
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(daemon.commands.map((command) => command.command), ['route_pause', 'status']);
  assert.equal(daemon.connections, 1);
});
