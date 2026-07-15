#!/usr/bin/env node
'use strict';

// Local, token-free adapter between VEIL's Node process and the retained
// veil_dji_flight.py session. The Unix connection is deliberately persistent:
// the flight daemon neutralizes translation and pauses route ownership when its
// command client disconnects.

const fs = require('fs');
const net = require('net');
const crypto = require('crypto');

const DEFAULT_SOCKET_PATH = process.env.VEIL_DJI_UNIX_SOCKET
  || process.env.VEIL_DJI_FLIGHT_SOCKET
  || '/tmp/veil-dji-flight.sock';
const MAX_LINE_BYTES = 2 * 1024 * 1024;
const MAX_ROUTE_DOCUMENT_BYTES = 512 * 1024;
const DEFAULT_CONNECT_TIMEOUT_MS = 1500;
const DEFAULT_COMMAND_TIMEOUT_MS = 5000;
const LONG_COMMAND_TIMEOUT_MS = 20000;
const LAND_COMMAND_TIMEOUT_MS = 120000;
const CONTROL_TELEMETRY_MAX_AGE_MS = 350;

const COMMAND_CONFIRMATIONS = Object.freeze({
  arm: 'ARM_VIRTUAL_STICK',
  route_start: 'START_ROUTE',
  route_pause: 'PAUSE_ROUTE',
  route_resume: 'RESUME_ROUTE',
  route_abort: 'ABORT_ROUTE',
  neutral: 'NEUTRAL',
  handoff: 'HANDOFF_TO_RC',
  land: 'LAND',
});

class NymphBridgeError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = 'NymphBridgeError';
    this.code = code;
    this.httpStatus = options.httpStatus || 503;
    this.details = options.details || null;
  }

  toPublic() {
    // Details are retained for trusted callers and diagnostics, but never cross
    // the browser boundary. Bridge failures can contain response bodies, local
    // paths, request identifiers, and aircraft diagnostics.
    return { code: this.code, message: this.message };
  }
}

function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function safeInteger(value) {
  return Number.isSafeInteger(value) ? value : null;
}

function boundedText(value, limit = 500) {
  if (typeof value !== 'string') return null;
  const text = value;
  return text.length <= limit ? text : `${text.slice(0, limit)}…`;
}

function safeProtocolCode(value, limit = 100) {
  if (typeof value !== 'string' || value.length > limit) return null;
  return /^[a-z0-9_]+$/.test(value) ? value : null;
}

function safeIssuePath(value) {
  if (typeof value !== 'string' || value.length > 240) return null;
  return /^[a-z0-9_$.[\]-]+$/.test(value) ? value : null;
}

function normalizedSha256(value) {
  if (typeof value !== 'string') return null;
  const digest = value.trim().toLowerCase();
  return /^[a-f0-9]{64}$/.test(digest) ? digest : null;
}

function routeDocumentSha256(document) {
  return crypto.createHash('sha256').update(document, 'utf8').digest('hex');
}

function publicBridgeError(error) {
  if (error instanceof NymphBridgeError) return error;
  return new NymphBridgeError(
    'bridge_unavailable',
    'The local DJI flight session is unavailable.',
    { httpStatus: 503 },
  );
}

class UnixFlightClient {
  constructor(options = {}) {
    this.socketPath = options.socketPath || DEFAULT_SOCKET_PATH;
    this.fs = options.fs || fs;
    this.net = options.net || net;
    this.connectTimeoutMs = options.connectTimeoutMs || DEFAULT_CONNECT_TIMEOUT_MS;
    this.commandTimeoutMs = options.commandTimeoutMs || DEFAULT_COMMAND_TIMEOUT_MS;
    this.maxLineBytes = options.maxLineBytes || MAX_LINE_BYTES;
    this.getuid = options.getuid || (() => (
      typeof process.getuid === 'function' ? process.getuid() : null
    ));

    this.socket = null;
    this.ready = false;
    this.buffer = '';
    this.connecting = null;
    this.connectWaiter = null;
    this.pending = new Map();
    this.nextRequest = 1;
    this.mutationActive = false;
    this.connectionEpoch = 0;
    this.lastReady = null;
    this.lastEvent = null;
    this.closed = false;
  }

  async validateSocket() {
    let metadata;
    try {
      metadata = await this.fs.promises.lstat(this.socketPath);
    } catch (error) {
      if (error && error.code === 'ENOENT') {
        throw new NymphBridgeError(
          'flight_session_offline',
          'The local DJI flight session is not running.',
          { httpStatus: 503 },
        );
      }
      throw new NymphBridgeError(
        'flight_socket_unreadable',
        'The local DJI flight socket could not be inspected.',
        { httpStatus: 503 },
      );
    }
    const expectedUid = this.getuid();
    const privateMode = (metadata.mode & 0o077) === 0;
    const sameOwner = expectedUid === null || metadata.uid === expectedUid;
    if (!metadata.isSocket() || !privateMode || !sameOwner) {
      throw new NymphBridgeError(
        'flight_socket_unsafe',
        'The local DJI flight socket failed its ownership or permission check.',
        { httpStatus: 503 },
      );
    }
  }

  async ensureConnected() {
    if (this.closed) {
      throw new NymphBridgeError(
        'flight_client_closed',
        'The local DJI flight client has been closed.',
        { httpStatus: 503 },
      );
    }
    if (this.ready && this.socket && !this.socket.destroyed) return;
    if (this.connecting) return this.connecting;
    this.connecting = this._connect();
    try {
      await this.connecting;
    } finally {
      this.connecting = null;
    }
  }

  async _connect() {
    await this.validateSocket();
    return new Promise((resolve, reject) => {
      let settled = false;
      const socket = this.net.createConnection({ path: this.socketPath });
      this.socket = socket;
      this.buffer = '';
      this.ready = false;

      const finish = (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.connectWaiter = null;
        if (error) reject(error);
        else resolve();
      };
      this.connectWaiter = finish;
      const timer = setTimeout(() => {
        const error = new NymphBridgeError(
          'flight_socket_timeout',
          'The local DJI flight session did not complete its handshake.',
          { httpStatus: 504 },
        );
        finish(error);
        socket.destroy();
      }, this.connectTimeoutMs);

      socket.setEncoding('utf8');
      socket.on('data', (chunk) => this._onData(socket, chunk));
      socket.on('error', () => {
        const connectionError = new NymphBridgeError(
          'flight_socket_error',
          'The local DJI flight socket could not be reached.',
          { httpStatus: 503 },
        );
        const commandError = this.pending.size ? new NymphBridgeError(
          'flight_command_outcome_unknown',
          'The local DJI flight socket failed after a command was sent; its outcome is unknown.',
          { httpStatus: 504 },
        ) : connectionError;
        finish(connectionError);
        this._dropSocket(socket, commandError);
      });
      socket.on('close', () => {
        const connectionError = new NymphBridgeError(
          'flight_socket_closed',
          'The local DJI flight session disconnected.',
          { httpStatus: 503 },
        );
        const commandError = this.pending.size ? new NymphBridgeError(
          'flight_command_outcome_unknown',
          'The local DJI flight session disconnected after a command was sent; its outcome is unknown.',
          { httpStatus: 504 },
        ) : connectionError;
        finish(connectionError);
        this._dropSocket(socket, commandError);
      });
    });
  }

  _onData(socket, chunk) {
    if (socket !== this.socket) return;
    this.buffer += chunk;
    const rejectOversizedResponse = () => {
      const error = new NymphBridgeError(
        'flight_response_too_large',
        'The local DJI flight session returned an oversized response.',
        { httpStatus: 502 },
      );
      this._dropSocket(socket, error);
      socket.destroy();
    };
    let newline;
    while ((newline = this.buffer.indexOf('\n')) >= 0) {
      const rawLine = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (Buffer.byteLength(rawLine, 'utf8') > this.maxLineBytes) {
        rejectOversizedResponse();
        return;
      }
      const line = rawLine.trim();
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch (_error) {
        const error = new NymphBridgeError(
          'flight_response_invalid',
          'The local DJI flight session returned invalid JSON.',
          { httpStatus: 502 },
        );
        this._dropSocket(socket, error);
        socket.destroy();
        return;
      }
      this._onMessage(socket, message);
    }
    // Cap the incomplete line, not the entire chunk. A chunk may legitimately
    // contain several individually bounded command results.
    if (Buffer.byteLength(this.buffer, 'utf8') > this.maxLineBytes) {
      rejectOversizedResponse();
    }
  }

  _onMessage(socket, message) {
    if (socket !== this.socket || !message || typeof message !== 'object') return;
    this.lastEvent = message.event || null;
    if (message.event === 'repl_ready') {
      if (message.protocol !== 'veil.flight-repl.v1') {
        const error = new NymphBridgeError(
          'flight_protocol_unsupported',
          'The local DJI flight protocol version is unsupported.',
          { httpStatus: 502 },
        );
        this.connectWaiter?.(error);
        this._dropSocket(socket, error);
        socket.destroy();
        return;
      }
      this.ready = true;
      this.lastReady = Date.now();
      this.connectWaiter?.(null);
      return;
    }
    if (message.event === 'server_busy') {
      const error = new NymphBridgeError(
        'flight_session_busy',
        'Another local client owns the DJI flight session.',
        { httpStatus: 409 },
      );
      this.connectWaiter?.(error);
      this._dropSocket(socket, error);
      socket.destroy();
      return;
    }
    if (message.event !== 'command_result') return;
    let requestId = Object.prototype.hasOwnProperty.call(message, 'request_id')
      && message.request_id !== null
      ? String(message.request_id) : null;
    // The retained REPL currently omits request_id when its synchronous
    // dispatch path raises. Commands are serialized, so a sole pending waiter
    // is the unambiguous correlation target. The daemon should still echo the
    // ID; this fallback prevents a false 5-second timeout during migration.
    if (requestId === null && this.pending.size === 1) {
      [requestId] = this.pending.keys();
    }
    if (requestId === null) return;
    const waiter = this.pending.get(requestId);
    if (!waiter) return;
    this.pending.delete(requestId);
    clearTimeout(waiter.timer);
    Object.defineProperty(message, '_nymphConnectionEpoch', {
      value: this.connectionEpoch,
      enumerable: false,
    });
    waiter.resolve(message);
  }

  _dropSocket(socket, error) {
    if (socket !== this.socket) return;
    this.socket = null;
    this.ready = false;
    this.buffer = '';
    for (const waiter of this.pending.values()) {
      clearTimeout(waiter.timer);
      const rejection = waiter.mutating
        && error?.code !== 'flight_command_outcome_unknown'
        ? new NymphBridgeError(
          'flight_command_outcome_unknown',
          'The flight connection failed after a command was sent; its outcome is unknown.',
          { httpStatus: 504 },
        )
        : error;
      waiter.reject(rejection);
    }
    this.pending.clear();
    this.connectionEpoch += 1;
  }

  command(command, options = {}) {
    const readOnly = command?.command === 'status' || command?.command === 'route_status';
    if (readOnly) return this._commandNow(command, options);
    if (this.mutationActive) {
      return Promise.reject(new NymphBridgeError(
        'flight_command_busy',
        'Another DJI mutation is still in progress; this command was not queued.',
        { httpStatus: 409 },
      ));
    }
    this.mutationActive = true;
    return this._commandNow(command, options)
      .finally(() => { this.mutationActive = false; });
  }

  async _commandNow(command, options) {
    if (!command || typeof command !== 'object' || Array.isArray(command)) {
      throw new NymphBridgeError(
        'flight_command_invalid',
        'The DJI flight command must be an object.',
        { httpStatus: 400 },
      );
    }
    await this.ensureConnected();
    const socket = this.socket;
    if (!socket || socket.destroyed || !this.ready) {
      throw new NymphBridgeError(
        'flight_session_offline',
        'The local DJI flight session is not connected.',
        { httpStatus: 503 },
      );
    }
    const requestId = `nymph-${process.pid}-${this.nextRequest++}`;
    const payload = { ...command, request_id: requestId };
    const line = `${JSON.stringify(payload)}\n`;
    if (Buffer.byteLength(line, 'utf8') > MAX_ROUTE_DOCUMENT_BYTES + 4096) {
      throw new NymphBridgeError(
        'flight_command_too_large',
        'The DJI flight command exceeds the local API limit.',
        { httpStatus: 413 },
      );
    }
    const timeoutMs = options.timeoutMs || this.commandTimeoutMs;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (!this.pending.delete(requestId)) return;
        const error = new NymphBridgeError(
          'flight_command_outcome_unknown',
          'The local DJI flight command timed out; its outcome is unknown.',
          { httpStatus: 504 },
        );
        reject(error);
        // Disconnecting invokes the daemon's neutral/pause boundary rather than
        // leaving a possibly moving command client in an unknown state.
        this._dropSocket(socket, error);
        socket.destroy();
      }, timeoutMs);
      const mutating = command.command !== 'status' && command.command !== 'route_status';
      this.pending.set(requestId, { resolve, reject, timer, mutating });
      socket.write(line, 'utf8', (error) => {
        if (!error) return;
        const waiter = this.pending.get(requestId);
        if (!waiter) return;
        this.pending.delete(requestId);
        clearTimeout(waiter.timer);
        const outcomeError = new NymphBridgeError(
          'flight_command_outcome_unknown',
          'The command write failed after transmission began; its outcome is unknown.',
          { httpStatus: 504 },
        );
        waiter.reject(outcomeError);
        this._dropSocket(socket, outcomeError);
        socket.destroy();
      });
    });
  }

  close() {
    this.closed = true;
    const socket = this.socket;
    const error = new NymphBridgeError(
      'flight_client_closed',
      'The local DJI flight client has been closed.',
      { httpStatus: 503 },
    );
    if (socket) this._dropSocket(socket, error);
    if (socket && !socket.destroyed) socket.destroy();
  }
}

function safeCapabilities(raw) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const routeRevision = source.revision_acceptance === true
    && source.route_engine === 'bridge_virtual_stick';
  return {
    nativeMissions: false,
    virtualStick: routeRevision,
    browserDirectControl: false,
    routeRevisions: routeRevision,
    midFlightReplacement: routeRevision && source.mid_flight_replacement === true,
    routeEngine: boundedText(source.route_engine, 80),
    schema: boundedText(source.schema, 80),
    aircraftResidentRoute: source.aircraft_resident_route === true,
  };
}

function safeRoute(raw) {
  if (!raw || typeof raw !== 'object') return null;
  return {
    routeId: boundedText(raw.route_id, 128),
    phase: boundedText(raw.phase, 40),
    activeRevision: safeInteger(raw.active_revision),
    newestAcceptedRevision: safeInteger(raw.newest_accepted_revision),
    targetWaypointIndex: safeInteger(raw.target_waypoint_index),
    pendingRevision: safeInteger(raw.pending_revision),
    pendingTargetWaypointIndex: safeInteger(raw.pending_target_waypoint_index),
  };
}

function safeTelemetry(raw, nowMs) {
  if (!raw || typeof raw !== 'object' || raw.available === false) return null;
  const reportedAgeMs = finiteNumber(raw.arrival_age_ms);
  const arrivalAgeMs = reportedAgeMs !== null && reportedAgeMs >= 0
    ? reportedAgeMs : null;
  const fresh = arrivalAgeMs !== null
    && raw.fresh === true
    && raw.stale === false;
  return {
    received_at: arrivalAgeMs === null ? null : Math.max(0, nowMs - arrivalAgeMs),
    arrival_age_ms: arrivalAgeMs,
    fresh,
    stale: !fresh,
    product_connected: raw.product_connected === true,
    aircraft_connected: raw.aircraft_connected === true,
    remote_controller_connected: raw.remote_controller_connected === true,
    airlink_connected: raw.airlink_connected === true,
    is_flying: raw.is_flying === true,
    motors_on: raw.motors_on === true,
    flight_mode: boundedText(raw.flight_mode, 80),
    lat: finiteNumber(raw.latitude_deg),
    lon: finiteNumber(raw.longitude_deg),
    alt_rel_m: finiteNumber(raw.relative_altitude_m),
    heading_deg: finiteNumber(raw.yaw_deg),
    velocity_ned: {
      north_mps: finiteNumber(raw.velocity_north_mps),
      east_mps: finiteNumber(raw.velocity_east_mps),
      down_mps: finiteNumber(raw.velocity_down_mps),
    },
    gps_signal: boundedText(raw.gps_signal_level, 40),
    gps_satellites: finiteNumber(raw.gps_satellite_count),
    battery_pct: finiteNumber(raw.battery_percent),
    authority_owner: boundedText(raw.authority_owner, 40),
    link_quality_pct: finiteNumber(raw.airlink_signal_quality),
  };
}

function aircraftMode(raw, telemetry, route) {
  if (!telemetry || telemetry.aircraft_connected !== true) return 'IDLE';
  const armed = raw?.state === 'armed' && raw?.control?.armed === true;
  if (armed && route?.phase === 'running') return 'GUIDED-VS';
  if (armed) return 'DIRECT';
  return 'MANUAL';
}

function projectStatus(raw, options = {}) {
  const requestedNowMs = finiteNumber(options.nowMs);
  const nowMs = requestedNowMs === null ? Date.now() : requestedNowMs;
  const telemetry = safeTelemetry(raw?.telemetry_snapshot, nowMs);
  const diagnostics = raw?.telemetry && typeof raw.telemetry === 'object'
    ? raw.telemetry : {};
  const arrivalAgeMs = telemetry?.arrival_age_ms ?? finiteNumber(diagnostics.arrival_age_ms);
  const telemetryFresh = telemetry?.fresh === true
    && arrivalAgeMs !== null
    && arrivalAgeMs <= CONTROL_TELEMETRY_MAX_AGE_MS;
  const routeRaw = raw?.route?.route || null;
  const route = safeRoute(routeRaw);
  const capabilities = safeCapabilities(raw?.capabilities || raw?.route?.capabilities);
  const daemonHealthy = raw?.ok === true
    && raw?.monitor_thread_alive === true
    && raw?.route_thread_alive === true
    && raw?.local_api?.healthy === true;
  const bridgeConnected = daemonHealthy
    && telemetry?.product_connected === true;
  const controlChannelReady = bridgeConnected
    && telemetryFresh
    && telemetry?.aircraft_connected === true
    && telemetry?.remote_controller_connected === true
    && telemetry?.airlink_connected === true;
  const mode = aircraftMode(raw, telemetry, route);
  if (telemetry) telemetry.mode = mode;
  return {
    ok: true,
    daemonConnected: true,
    checked_at: nowMs,
    aircraftMode: mode,
    link: {
      bridgeConnected,
      controlChannelReady,
      envelopeReady: options.envelopeReady === true,
      rcTakeoverVerified: options.rcTakeoverVerified === true,
    },
    capabilities,
    telemetry,
    video: {
      connected: false,
      received_at: null,
      latency_ms: null,
      source: null,
      url: null,
    },
    route,
    control: {
      armed: raw?.control?.armed === true,
      state: boundedText(raw?.state, 40),
      authorityOwner: boundedText(raw?.authority?.owner, 40),
      authorityMode: boundedText(raw?.authority?.mode, 40),
      // Internal failures may contain bridge response text or local details.
      lastError: null,
    },
  };
}

function projectCommandResult(raw) {
  const capabilities = safeCapabilities(raw?.capabilities || raw?.route?.capabilities);
  return {
    ok: raw?.ok === true,
    applied: raw?.ok === true,
    state: safeProtocolCode(raw?.state, 80),
    command: safeProtocolCode(raw?.command, 80),
    error: safeProtocolCode(raw?.error, 100),
    // Free-form daemon/bridge exception text remains on the trusted side.
    message: null,
    acceptedRevision: safeInteger(raw?.accepted_revision),
    issues: Array.isArray(raw?.issues) ? raw.issues.slice(0, 100).map((issue) => ({
      path: safeIssuePath(issue?.path),
    })) : [],
    route: safeRoute(raw?.route),
    authority: {
      virtualStickEnabled: raw?.authority?.virtual_stick_enabled === true,
      owner: boundedText(raw?.authority?.owner, 40),
      mode: boundedText(raw?.authority?.mode, 40),
    },
    capabilities,
  };
}

class NymphBridgeService {
  constructor(options = {}) {
    this.client = options.client || new UnixFlightClient(options);
    this.envelopeReady = options.envelopeReady === true;
    this.rcTakeoverVerified = options.rcTakeoverVerified === true;
    this.checkedRouteSha256 = normalizedSha256(options.checkedRouteSha256);
    this.routeAttestationReady = this.envelopeReady && this.checkedRouteSha256 !== null;
    this.attestedConnectionEpoch = null;
    this.now = options.now || Date.now;
  }

  async status() {
    const raw = await this.client.command({ command: 'status' });
    return projectStatus(raw, {
      nowMs: this.now(),
      envelopeReady: this.envelopeReady,
      rcTakeoverVerified: this.rcTakeoverVerified,
    });
  }

  async routeStatus() {
    const raw = await this.client.command({ command: 'route_status' });
    return projectCommandResult(raw);
  }

  async acceptRoute(document) {
    if (typeof document !== 'string' || !document.trim()) {
      throw new NymphBridgeError(
        'route_document_required',
        'A complete veil.route-revision.v1 document string is required.',
        { httpStatus: 400 },
      );
    }
    if (Buffer.byteLength(document, 'utf8') > MAX_ROUTE_DOCUMENT_BYTES) {
      throw new NymphBridgeError(
        'route_document_too_large',
        'The route revision document exceeds 512 KiB.',
        { httpStatus: 413 },
      );
    }
    if (!this.routeAttestationReady) {
      throw new NymphBridgeError(
        'route_attestation_unavailable',
        'A server-owned SHA-256 attestation for the checked route is required.',
        { httpStatus: 412 },
      );
    }
    const actual = Buffer.from(routeDocumentSha256(document), 'hex');
    const expected = Buffer.from(this.checkedRouteSha256, 'hex');
    if (!crypto.timingSafeEqual(actual, expected)) {
      throw new NymphBridgeError(
        'route_attestation_mismatch',
        'The supplied route does not match the server-owned checked route.',
        { httpStatus: 412 },
      );
    }
    const raw = await this.client.command({ command: 'route_accept', document });
    if (raw?.ok === true) {
      this.attestedConnectionEpoch = Number.isSafeInteger(raw._nymphConnectionEpoch)
        ? raw._nymphConnectionEpoch
        : Number.isSafeInteger(this.client.connectionEpoch) ? this.client.connectionEpoch : 0;
    }
    return projectCommandResult(raw);
  }

  get routeExecutionAttested() {
    const currentEpoch = Number.isSafeInteger(this.client.connectionEpoch)
      ? this.client.connectionEpoch : 0;
    return this.attestedConnectionEpoch !== null
      && this.attestedConnectionEpoch === currentEpoch;
  }

  async execute(command) {
    if (!Object.prototype.hasOwnProperty.call(COMMAND_CONFIRMATIONS, command)) {
      throw new NymphBridgeError(
        'flight_command_not_allowed',
        'That command is not exposed through the Nymph bridge.',
        { httpStatus: 400 },
      );
    }
    if (['route_start', 'route_resume'].includes(command) && !this.routeExecutionAttested) {
      throw new NymphBridgeError(
        'route_execution_unattested',
        'The active route was not accepted from the server-attested document on this connection.',
        { httpStatus: 412 },
      );
    }
    const timeoutMs = command === 'land'
      ? LAND_COMMAND_TIMEOUT_MS
      : ['arm', 'handoff'].includes(command)
        ? LONG_COMMAND_TIMEOUT_MS : DEFAULT_COMMAND_TIMEOUT_MS;
    const raw = await this.client.command({ command }, { timeoutMs });
    return projectCommandResult(raw);
  }

  close() {
    this.client.close();
  }
}

function createNymphBridgeService(options = {}) {
  return new NymphBridgeService({
    ...options,
    socketPath: options.socketPath
      || process.env.VEIL_DJI_UNIX_SOCKET
      || process.env.VEIL_DJI_FLIGHT_SOCKET,
    envelopeReady: options.envelopeReady ?? process.env.VEIL_DJI_ENVELOPE_READY === '1',
    rcTakeoverVerified: options.rcTakeoverVerified
      ?? process.env.VEIL_DJI_RC_TAKEOVER_VERIFIED === '1',
    checkedRouteSha256: options.checkedRouteSha256
      ?? process.env.VEIL_DJI_CHECKED_ROUTE_SHA256,
  });
}

module.exports = {
  COMMAND_CONFIRMATIONS,
  DEFAULT_SOCKET_PATH,
  MAX_ROUTE_DOCUMENT_BYTES,
  CONTROL_TELEMETRY_MAX_AGE_MS,
  NymphBridgeError,
  NymphBridgeService,
  UnixFlightClient,
  createNymphBridgeService,
  projectCommandResult,
  projectStatus,
  publicBridgeError,
  routeDocumentSha256,
  safeCapabilities,
  safeRoute,
  safeTelemetry,
};
