/* VEIL Nymph DJI client. The browser talks only to same-origin, sanitized Node
   endpoints; the Android token and local Unix socket never enter this process. */
(function nymphBridgeClientModule(global) {
  'use strict';

  const DEFAULT_POLL_MS = 500;
  const REQUEST_TIMEOUT_MS = 4000;
  const COMMANDS = Object.freeze({
    arm: Object.freeze({ path: 'arm', confirm: 'ARM_VIRTUAL_STICK', timeoutMs: 35000 }),
    startRoute: Object.freeze({ path: 'start', confirm: 'START_ROUTE' }),
    pauseRoute: Object.freeze({ path: 'pause', confirm: 'PAUSE_ROUTE' }),
    resumeRoute: Object.freeze({ path: 'resume', confirm: 'RESUME_ROUTE' }),
    abortRoute: Object.freeze({ path: 'abort', confirm: 'ABORT_ROUTE' }),
    neutral: Object.freeze({ path: 'neutral', confirm: 'NEUTRAL' }),
    handoff: Object.freeze({ path: 'handoff', confirm: 'HANDOFF_TO_RC', timeoutMs: 35000 }),
    land: Object.freeze({ path: 'land', confirm: 'LAND', timeoutMs: 125000 }),
  });

  function publicError(payload, fallback = 'DJI bridge request failed') {
    const source = payload?.error;
    if (source && typeof source === 'object') {
      return {
        code: String(source.code || 'bridge_request_failed'),
        message: String(source.message || fallback),
      };
    }
    return { code: 'bridge_request_failed', message: fallback };
  }

  function offlineStatus(error = null) {
    return {
      ok: false,
      daemonConnected: false,
      aircraftMode: 'IDLE',
      link: {
        bridgeConnected: false,
        controlChannelReady: false,
        envelopeReady: false,
        rcTakeoverVerified: false,
      },
      capabilities: {
        nativeMissions: false,
        virtualStick: false,
        browserDirectControl: false,
        routeRevisions: false,
        midFlightReplacement: false,
      },
      telemetry: null,
      video: { connected: false, received_at: null, latency_ms: null, source: null, url: null },
      route: null,
      control: { armed: false, state: 'offline', authorityOwner: null, authorityMode: null },
      error,
    };
  }

  async function requestJson(fetchImpl, path, options = {}) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = global.setTimeout?.(() => controller?.abort(), options.timeoutMs || REQUEST_TIMEOUT_MS);
    try {
      const response = await fetchImpl(path, {
        method: options.method || 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: options.body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller?.signal,
      });
      let payload;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      if (!response.ok) {
        const safe = publicError(payload, `DJI bridge request failed (${response.status})`);
        const error = new Error(safe.message);
        error.code = safe.code;
        error.status = response.status;
        throw error;
      }
      return payload;
    } finally {
      if (timer !== undefined) global.clearTimeout?.(timer);
    }
  }

  function create(options = {}) {
    const fetchImpl = options.fetch || global.fetch?.bind(global);
    if (typeof fetchImpl !== 'function') return null;
    const pollMs = Math.max(200, Number(options.pollMs) || DEFAULT_POLL_MS);
    let timer = null;
    let running = false;
    let pollPending = false;
    let mutationPending = false;
    let statusSink = typeof options.onStatus === 'function' ? options.onStatus : null;
    let lastStatus = offlineStatus();

    function publish(status) {
      lastStatus = status;
      statusSink?.(status);
      return status;
    }

    async function pollNow() {
      if (pollPending) return lastStatus;
      pollPending = true;
      try {
        const status = await requestJson(fetchImpl, '/api/nymphs/dji/status');
        return publish(status);
      } catch (error) {
        return publish(offlineStatus({
          code: String(error?.code || 'bridge_unavailable'),
          message: String(error?.message || 'DJI bridge unavailable'),
        }));
      } finally {
        pollPending = false;
      }
    }

    function schedule() {
      if (!running) return;
      timer = global.setTimeout?.(async () => {
        await pollNow();
        schedule();
      }, pollMs);
    }

    function start() {
      if (running) return;
      running = true;
      pollNow().finally(schedule);
    }

    function stop() {
      running = false;
      if (timer !== null) global.clearTimeout?.(timer);
      timer = null;
      publish(offlineStatus());
    }

    async function mutate(path, body, timeoutMs = 25000) {
      if (mutationPending) {
        const error = new Error('Another DJI command is still pending.');
        error.code = 'command_pending';
        throw error;
      }
      mutationPending = true;
      try {
        const result = await requestJson(fetchImpl, path, {
          method: 'POST',
          body,
          timeoutMs,
        });
        await pollNow();
        return result;
      } finally {
        mutationPending = false;
      }
    }

    async function command(name) {
      const config = COMMANDS[name];
      if (!config) throw new Error(`Unsupported DJI command: ${name}`);
      return mutate(
        `/api/nymphs/dji/${config.path}`,
        { confirm: config.confirm },
        config.timeoutMs,
      );
    }

    return {
      start,
      stop,
      pollNow,
      setStatusSink(callback) {
        statusSink = typeof callback === 'function' ? callback : null;
        if (statusSink) statusSink(lastStatus);
      },
      getStatus: () => JSON.parse(JSON.stringify(lastStatus)),
      async arm({ mode } = {}) {
        if (mode !== 'GUIDED-VS') {
          return { applied: false, error: `unsupported Nymph arm mode: ${mode || 'missing'}` };
        }
        const result = await command('arm');
        return { ...result, applied: result?.ok === true };
      },
      async acceptRoute(document) {
        return mutate('/api/nymphs/dji/route-accept', {
          confirm: 'ACCEPT_CHECKED_ROUTE',
          document,
        });
      },
      startRoute: () => command('startRoute'),
      pauseRoute: () => command('pauseRoute'),
      resumeRoute: () => command('resumeRoute'),
      abortRoute: () => command('abortRoute'),
      neutral: () => command('neutral'),
      handoff: () => command('handoff'),
      land: () => command('land'),
      isMutationPending: () => mutationPending,
    };
  }

  global.VEILNymphBridgeClient = {
    create,
    _test: { COMMANDS, offlineStatus, publicError, requestJson },
  };
})(typeof window !== 'undefined' ? window : globalThis);
