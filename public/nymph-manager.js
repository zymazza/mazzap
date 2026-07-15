/* VEIL Nymph Manager: shared UI for external devices, robots, and actuators.
   The first nymph is a DJI Mini 4 Pro. A same-origin control client may be
   injected for explicit, acknowledged commands. RTS clicks remain unchecked
   drafts and are never sent to the aircraft by this module. */
(function nymphManagerModule(global) {
  'use strict';

  const MODES = Object.freeze({
    controller: Object.freeze({
      label: 'Controller',
      aircraftMode: 'MANUAL',
      description: 'Fly on the physical controller while VEIL receives video, telemetry, and the live aircraft track.',
    }),
    'virtual-stick': Object.freeze({
      label: 'Virtual stick',
      aircraftMode: 'DIRECT',
      description: 'Direct setpoints require the retained Mac flight API. This browser facade does not emit a joystick stream.',
    }),
    rts: Object.freeze({
      label: 'RTS click',
      aircraftMode: 'GUIDED',
      description: 'Scroll to set the next AGL, then click the twin to draft live mini-waypoint legs. Altitude stays fixed until you scroll again.',
    }),
  });

  const ALTITUDE_LIMITS = Object.freeze({ min: 10, max: 120, step: 1, coarseStep: 5 });
  const TELEMETRY_MAX_AGE_MS = 1500;

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function finiteNumber(value, fallback) {
    if (value === undefined || value === null || value === '') return fallback;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function normalizeAltitude(value, limits = ALTITUDE_LIMITS) {
    const min = finiteNumber(limits.min, ALTITUDE_LIMITS.min);
    const max = finiteNumber(limits.max, ALTITUDE_LIMITS.max);
    return clamp(Math.round(finiteNumber(value, min) * 10) / 10, min, max);
  }

  function adjustAltitude(current, deltaY, options = {}) {
    const direction = Number(deltaY) < 0 ? 1 : Number(deltaY) > 0 ? -1 : 0;
    const limits = options.limits || ALTITUDE_LIMITS;
    const step = options.coarse
      ? finiteNumber(limits.coarseStep, ALTITUDE_LIMITS.coarseStep)
      : finiteNumber(limits.step, ALTITUDE_LIMITS.step);
    return normalizeAltitude(finiteNumber(current, limits.min) + direction * step, limits);
  }

  function timestampMs(value) {
    if (value === undefined || value === null || value === '') return null;
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) return null;
      return value < 1e11 ? value * 1000 : value;
    }
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function telemetryAgeMs(telemetry, nowMs = Date.now()) {
    const observed = timestampMs(telemetry?.received_at ?? telemetry?.t ?? telemetry?.observed_at);
    if (observed === null) return Infinity;
    return Math.max(0, finiteNumber(nowMs, Date.now()) - observed);
  }

  function videoAgeMs(video, nowMs = Date.now()) {
    const observed = timestampMs(video?.received_at ?? video?.t);
    if (observed === null) return Infinity;
    return Math.max(0, finiteNumber(nowMs, Date.now()) - observed);
  }

  function guidedCapability(state) {
    if (state?.capabilities?.nativeMissions === true) return 'GUIDED-N';
    if (state?.capabilities?.virtualStick === true) return 'GUIDED-VS';
    return null;
  }

  function requestedAircraftMode(selectedMode, state = {}) {
    if (selectedMode === 'rts') return guidedCapability(state) || MODES.rts.aircraftMode;
    return MODES[selectedMode]?.aircraftMode || 'IDLE';
  }

  function interlockItems(state, selectedMode = state?.selectedMode, nowMs = Date.now()) {
    const link = state?.link || {};
    const telemetryFresh = telemetryAgeMs(state?.telemetry, nowMs) <= TELEMETRY_MAX_AGE_MS;
    if (selectedMode === 'controller') {
      return [
        { key: 'rc-authority', ok: true, text: 'RC-N2 retains flight authority and immediate stick takeover.' },
        { key: 'bridge', ok: link.bridgeConnected === true, text: link.bridgeConnected ? 'Android bridge connected.' : 'Android bridge is offline; VEIL telemetry is unavailable.' },
        { key: 'video', ok: state?.video?.connected === true, text: state?.video?.connected ? 'Drone video is reaching VEIL.' : 'Drone video is not reaching VEIL.' },
      ];
    }

    const items = [
      { key: 'bridge', ok: link.bridgeConnected === true, text: link.bridgeConnected ? 'Android bridge connected.' : 'Android bridge is offline.' },
      { key: 'control', ok: link.controlChannelReady === true, text: link.controlChannelReady ? 'Control channel ready.' : 'Control channel is not ready.' },
      { key: 'telemetry', ok: telemetryFresh, text: telemetryFresh ? 'Telemetry is fresh (≤ 1.5 s).' : 'Fresh telemetry is required (≤ 1.5 s).' },
      { key: 'envelope', ok: link.envelopeReady === true, text: link.envelopeReady ? 'Terrain floor and geofence are loaded.' : 'Terrain floor and geofence are not loaded.' },
      { key: 'takeover', ok: link.rcTakeoverVerified === true, text: link.rcTakeoverVerified ? 'RC pause takeover is verified for this firmware.' : 'RC pause takeover still needs B2 verification.' },
    ];

    if (selectedMode === 'virtual-stick') {
      const directControl = state?.capabilities?.browserDirectControl === true;
      items.push({
        key: 'capability',
        ok: state?.capabilities?.virtualStick === true && directControl,
        text: state?.capabilities?.virtualStick !== true
          ? 'Virtual-stick capability has not passed B2.'
          : directControl
            ? 'A continuous direct-control source is available.'
            : 'The browser facade has no continuous setpoint source; use the retained Mac API.',
      });
    } else if (selectedMode === 'rts') {
      const capability = guidedCapability(state);
      items.push({
        key: 'capability',
        ok: capability !== null,
        text: capability
          ? `${capability} execution is available.`
          : 'RTS needs native missions (B1) or guarded virtual stick (B2).',
      });
    }
    return items;
  }

  function canArm(state, selectedMode = state?.selectedMode, nowMs = Date.now()) {
    if (!MODES[selectedMode] || selectedMode === 'controller') return false;
    return interlockItems(state, selectedMode, nowMs).every((item) => item.ok);
  }

  function createRtsWaypoint(pick, targetAglM, surface, index = 0) {
    const lat = finiteNumber(pick?.geo?.lat, NaN);
    const lon = finiteNumber(pick?.geo?.lon, NaN);
    const x = finiteNumber(pick?.point?.x, NaN);
    const y = finiteNumber(pick?.point?.y, NaN);
    if (![lat, lon, x, y].every(Number.isFinite)) return null;
    return {
      n: Math.max(0, Math.trunc(finiteNumber(index, 0))),
      lat,
      lon,
      x,
      y,
      terrain_elevation_m: finiteNumber(pick?.geo?.elevation_m, null),
      agl_mode: surface === 'ground' ? 'ground' : 'canopy',
      target_agl_m: normalizeAltitude(targetAglM),
      checked: false,
      sent: false,
    };
  }

  function formatAge(ageMs) {
    if (!Number.isFinite(ageMs)) return '—';
    if (ageMs < 1000) return `${Math.round(ageMs)} ms`;
    if (ageMs < 10000) return `${(ageMs / 1000).toFixed(1)} s`;
    return `${Math.round(ageMs / 1000)} s`;
  }

  function cloneState(state) {
    return JSON.parse(JSON.stringify(state));
  }

  function create(options = {}) {
    const document = global.document;
    const panel = document?.getElementById?.('nymph-manager-panel');
    if (!panel) return null;

    const viewer = options.viewer;
    const controlClient = options.controlClient || null;
    const canvas = viewer?.renderer?.domElement || null;
    const THREE = global.THREE;
    const els = {
      aircraftMode: document.getElementById('nymph-aircraft-mode'),
      connectionDot: document.getElementById('nymph-connection-dot'),
      bridgeState: document.getElementById('nymph-bridge-state'),
      telemetryAge: document.getElementById('nymph-telemetry-age'),
      videoAge: document.getElementById('nymph-video-age'),
      executionStatus: document.getElementById('nymph-execution-status'),
      videoBadge: document.getElementById('nymph-video-badge'),
      video: document.getElementById('nymph-video'),
      videoEmpty: document.getElementById('nymph-video-empty'),
      modes: document.getElementById('nymph-flight-modes'),
      modeDescription: document.getElementById('nymph-mode-description'),
      guidedTools: document.getElementById('nymph-guided-tools'),
      altitudeRibbon: document.getElementById('nymph-altitude-ribbon'),
      altitude: document.getElementById('nymph-target-altitude'),
      altitudeFt: document.getElementById('nymph-target-altitude-ft'),
      surfaceSelect: document.getElementById('nymph-surface-select'),
      routeStatus: document.getElementById('nymph-route-status'),
      clearRoute: document.getElementById('nymph-clear-route'),
      interlocks: document.getElementById('nymph-interlocks'),
      arm: document.getElementById('nymph-arm-controls'),
      hold: document.getElementById('nymph-hold'),
      handoff: document.getElementById('nymph-handoff'),
    };

    const state = {
      selectedMode: 'controller',
      aircraftMode: 'IDLE',
      activePane: false,
      controlsArmed: false,
      commandPending: false,
      control: { armed: false, state: 'offline', authorityOwner: null, authorityMode: null },
      executionRoute: null,
      nextAltitudeM: 25,
      surface: 'canopy',
      route: [],
      link: {
        bridgeConnected: false,
        controlChannelReady: false,
        envelopeReady: false,
        rcTakeoverVerified: false,
      },
      capabilities: { nativeMissions: false, virtualStick: false, browserDirectControl: false },
      telemetry: null,
      video: { connected: false, received_at: null, latency_ms: null, source: null },
      lastError: null,
    };

    let routeGroup = null;
    if (viewer?.scene && THREE?.Group) {
      routeGroup = new THREE.Group();
      routeGroup.name = 'veil-nymph-guided-draft';
      routeGroup.visible = false;
      viewer.scene.add(routeGroup);
    }

    function disposeObject(object) {
      object?.geometry?.dispose?.();
      if (Array.isArray(object?.material)) object.material.forEach((material) => material?.dispose?.());
      else object?.material?.dispose?.();
    }

    function clearRouteGroup() {
      if (!routeGroup) return;
      while (routeGroup.children.length) {
        const child = routeGroup.children[routeGroup.children.length - 1];
        routeGroup.remove(child);
        disposeObject(child);
      }
    }

    function waypointWorld(waypoint, includeAltitude = true) {
      const terrainMin = finiteNumber(viewer?.terrainGrid?.minElevation, 0);
      const groundWorldY = finiteNumber(waypoint.terrain_elevation_m, terrainMin) - terrainMin;
      return new THREE.Vector3(
        waypoint.x,
        groundWorldY + (includeAltitude ? waypoint.target_agl_m : 0),
        -waypoint.y,
      );
    }

    function renderRouteOverlay() {
      if (!routeGroup || !THREE) return;
      clearRouteGroup();
      routeGroup.visible = state.activePane && state.selectedMode === 'rts';
      if (!state.route.length) return;

      const targets = state.route.map((waypoint) => waypointWorld(waypoint, true));
      const stems = [];
      state.route.forEach((waypoint, index) => {
        const target = targets[index];
        stems.push(waypointWorld(waypoint, false), target);
        const marker = new THREE.Mesh(
          new THREE.SphereGeometry(1.4, 12, 8),
          new THREE.MeshBasicMaterial({ color: 0xf2c14e, depthTest: false }),
        );
        marker.position.copy(target);
        marker.renderOrder = 970;
        routeGroup.add(marker);
      });

      const stemGeometry = new THREE.BufferGeometry().setFromPoints(stems);
      const stemMaterial = new THREE.LineBasicMaterial({ color: 0xf2c14e, transparent: true, opacity: 0.42, depthTest: false });
      const stemLines = new THREE.LineSegments(stemGeometry, stemMaterial);
      stemLines.renderOrder = 968;
      routeGroup.add(stemLines);

      if (targets.length > 1) {
        const routeGeometry = new THREE.BufferGeometry().setFromPoints(targets);
        const routeMaterial = new THREE.LineBasicMaterial({ color: 0xf2c14e, transparent: true, opacity: 0.9, depthTest: false });
        const routeLine = new THREE.Line(routeGeometry, routeMaterial);
        routeLine.renderOrder = 969;
        routeGroup.add(routeLine);
      }
    }

    function setClass(element, className, enabled) {
      element?.classList?.toggle?.(className, !!enabled);
    }

    function renderLinkStatus(nowMs = Date.now()) {
      const connected = state.link.bridgeConnected === true;
      if (els.bridgeState) {
        els.bridgeState.textContent = connected ? 'Connected' : 'Offline';
        els.bridgeState.className = connected ? 'ok' : '';
      }
      setClass(els.connectionDot, 'online', connected);

      const telemetryAge = telemetryAgeMs(state.telemetry, nowMs);
      if (els.telemetryAge) {
        els.telemetryAge.textContent = formatAge(telemetryAge);
        els.telemetryAge.className = telemetryAge <= TELEMETRY_MAX_AGE_MS ? 'ok' : (Number.isFinite(telemetryAge) ? 'warn' : '');
      }

      const currentVideoAge = videoAgeMs(state.video, nowMs);
      if (els.videoAge) {
        els.videoAge.textContent = state.video.connected ? formatAge(currentVideoAge) : '—';
        els.videoAge.className = state.video.connected && currentVideoAge <= 3000 ? 'ok' : (state.video.connected ? 'warn' : '');
      }
      if (els.videoBadge) {
        const latency = finiteNumber(state.video.latency_ms, null);
        els.videoBadge.textContent = state.video.connected
          ? (latency === null ? 'SIGNAL' : `LIVE · ${Math.round(latency)} MS`)
          : 'NO SIGNAL';
        setClass(els.videoBadge, 'signal', state.video.connected);
      }
      if (els.videoEmpty) els.videoEmpty.hidden = state.video.connected && !els.video?.hidden;
      if (els.executionStatus) {
        const route = state.executionRoute;
        if (state.lastError) {
          els.executionStatus.textContent = `Bridge: ${state.lastError}`;
        } else if (!route) {
          els.executionStatus.textContent = 'No accepted route.';
        } else {
          const activeRevision = route.activeRevision ?? route.activePlan?.revision;
          const pendingRevision = route.pendingRevision ?? route.pendingPlan?.revision;
          const targetIndex = route.targetWaypointIndex;
          const details = [
            route.phase ? `Route ${route.phase}` : 'Route accepted',
            Number.isInteger(activeRevision) ? `revision ${activeRevision}` : null,
            Number.isInteger(pendingRevision) ? `pending ${pendingRevision}` : null,
            Number.isInteger(targetIndex) ? `target ${targetIndex + 1}` : null,
          ].filter(Boolean);
          els.executionStatus.textContent = `${details.join(' · ')}.`;
        }
      }
    }

    function renderModes() {
      els.modes?.querySelectorAll?.('[data-nymph-mode]').forEach((button) => {
        const active = button.dataset.nymphMode === state.selectedMode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      if (els.modeDescription) els.modeDescription.textContent = MODES[state.selectedMode].description;
      if (els.guidedTools) els.guidedTools.hidden = state.selectedMode !== 'rts';
      if (routeGroup) routeGroup.visible = state.activePane && state.selectedMode === 'rts';
    }

    function renderAltitude() {
      const altitude = normalizeAltitude(state.nextAltitudeM);
      const percent = ((altitude - ALTITUDE_LIMITS.min) / (ALTITUDE_LIMITS.max - ALTITUDE_LIMITS.min)) * 100;
      if (els.altitudeRibbon) {
        els.altitudeRibbon.style.setProperty('--altitude-percent', `${percent.toFixed(2)}%`);
        els.altitudeRibbon.setAttribute('aria-valuenow', String(altitude));
        els.altitudeRibbon.setAttribute('aria-valuetext', `${altitude} metres AGL over ${state.surface}`);
      }
      if (els.altitude) els.altitude.textContent = `${altitude} m`;
      if (els.altitudeFt) els.altitudeFt.textContent = `${Math.round(altitude * 3.28084)} ft · ${state.surface} AGL`;
      els.surfaceSelect?.querySelectorAll?.('[data-nymph-surface]').forEach((button) => {
        button.classList.toggle('active', button.dataset.nymphSurface === state.surface);
      });
      if (els.routeStatus) {
        const count = state.route.length;
        els.routeStatus.textContent = count
          ? `${count} draft waypoint${count === 1 ? '' : 's'} · next click ${altitude} m ${state.surface} AGL · unchecked and unsent.`
          : `Click terrain to draft the first target at ${altitude} m ${state.surface} AGL.`;
      }
      if (els.clearRoute) els.clearRoute.disabled = state.route.length === 0;
    }

    function renderInterlocks(nowMs = Date.now()) {
      const items = interlockItems(state, state.selectedMode, nowMs);
      if (els.interlocks) {
        els.interlocks.replaceChildren(...items.map((item) => {
          const li = document.createElement('li');
          if (item.ok) li.className = 'ok';
          li.textContent = item.text;
          return li;
        }));
      }

      const hasArmClient = typeof controlClient?.arm === 'function';
      const armable = canArm(state, state.selectedMode, nowMs) && hasArmClient;
      if (els.arm) {
        els.arm.disabled = state.controlsArmed || !armable || state.commandPending;
        if (state.controlsArmed) els.arm.textContent = `${state.aircraftMode || 'Flight controls'} armed`;
        else if (state.selectedMode === 'controller') els.arm.textContent = 'RC-N2 has authority';
        else if (!canArm(state, state.selectedMode, nowMs)) els.arm.textContent = 'Controls interlocked';
        else if (!hasArmClient) els.arm.textContent = 'Control client not installed';
        else els.arm.textContent = `Arm ${requestedAircraftMode(state.selectedMode, state)}`;
      }
      const phase = state.executionRoute?.phase;
      const hasHoldClient = phase === 'paused'
        ? typeof controlClient?.resumeRoute === 'function'
        : typeof controlClient?.pauseRoute === 'function';
      const holdAllowed = phase === 'running'
        || (phase === 'paused' && canArm(state, state.selectedMode, nowMs));
      if (els.hold) {
        els.hold.disabled = !hasHoldClient || !holdAllowed || state.commandPending;
        els.hold.textContent = phase === 'paused' ? 'Resume route' : 'Hold';
      }
      if (els.handoff) {
        els.handoff.disabled = typeof controlClient?.handoff !== 'function'
          || !state.controlsArmed || state.commandPending;
      }
    }

    function render(nowMs = Date.now()) {
      if (els.aircraftMode) {
        els.aircraftMode.textContent = state.aircraftMode || 'IDLE';
        setClass(els.aircraftMode, 'live', state.aircraftMode && state.aircraftMode !== 'IDLE');
      }
      renderLinkStatus(nowMs);
      renderModes();
      renderAltitude();
      renderInterlocks(nowMs);
    }

    function selectMode(mode) {
      if (!MODES[mode]) return false;
      state.selectedMode = mode;
      state.lastError = null;
      render();
      return true;
    }

    function setNextAltitude(value) {
      state.nextAltitudeM = normalizeAltitude(value);
      renderAltitude();
      return state.nextAltitudeM;
    }

    function nudgeAltitude(deltaY, coarse = false) {
      return setNextAltitude(adjustAltitude(state.nextAltitudeM, deltaY, { coarse }));
    }

    function announceRouteChange() {
      try {
        document.dispatchEvent(new CustomEvent('veil:nymph-routechange', {
          detail: { route: cloneState(state.route), checked: false, sent: false },
        }));
      } catch (_error) { /* route events are an optional integration seam */ }
    }

    function addGuidedTarget(pick) {
      const waypoint = createRtsWaypoint(pick, state.nextAltitudeM, state.surface, state.route.length);
      if (!waypoint) return null;
      state.route.push(waypoint);
      renderAltitude();
      renderRouteOverlay();
      announceRouteChange();
      options.onGuidedDraft?.(cloneState(waypoint), cloneState(state.route));
      return waypoint;
    }

    function clearRoute() {
      state.route = [];
      clearRouteGroup();
      renderAltitude();
      announceRouteChange();
    }

    function updateBridge(update = {}) {
      const linkUpdate = update.link ? { ...update.link } : {};
      ['bridgeConnected', 'controlChannelReady', 'envelopeReady', 'rcTakeoverVerified']
        .forEach((key) => {
          if (Object.prototype.hasOwnProperty.call(update, key)) linkUpdate[key] = update[key];
        });
      state.link = { ...state.link, ...linkUpdate };
      if (update.capabilities) state.capabilities = { ...state.capabilities, ...update.capabilities };
      if (update.control && typeof update.control === 'object') {
        state.control = { ...state.control, ...update.control };
        state.controlsArmed = update.control.armed === true;
      }
      if (Object.prototype.hasOwnProperty.call(update, 'route')) {
        state.executionRoute = update.route && typeof update.route === 'object'
          ? { ...update.route } : null;
      }
      if (state.link.bridgeConnected !== true) state.controlsArmed = false;
      if (update.aircraftMode || update.mode) state.aircraftMode = update.aircraftMode || update.mode;
      if (Object.prototype.hasOwnProperty.call(update, 'error')) {
        state.lastError = update.error ? String(update.error) : null;
      }
      render();
      return cloneState(state);
    }

    function updateTelemetry(telemetry = {}) {
      state.telemetry = {
        ...telemetry,
        received_at: telemetry.received_at ?? telemetry.t ?? Date.now(),
      };
      if (telemetry.mode) state.aircraftMode = telemetry.mode;
      render();
      return cloneState(state.telemetry);
    }

    function updateVideo(video = {}) {
      state.video = {
        ...state.video,
        ...video,
        connected: video.connected !== false,
        received_at: video.received_at ?? video.t ?? Date.now(),
      };
      renderLinkStatus();
      return cloneState(state.video);
    }

    function attachVideo(source, metadata = {}) {
      if (!els.video || !source) return false;
      if (typeof source === 'string') {
        els.video.removeAttribute('src');
        els.video.srcObject = null;
        els.video.src = source;
      } else {
        els.video.removeAttribute('src');
        els.video.srcObject = source;
      }
      els.video.hidden = false;
      els.video.play?.().catch?.(() => {});
      updateVideo({ ...metadata, connected: true });
      return true;
    }

    async function armControls() {
      if (!canArm(state) || typeof controlClient?.arm !== 'function' || state.commandPending) return false;
      state.commandPending = true;
      renderInterlocks();
      try {
        const requestedMode = requestedAircraftMode(state.selectedMode, state);
        const result = await controlClient.arm({ mode: requestedMode });
        if (!result?.applied) throw new Error(result?.error || 'bridge did not acknowledge arm request');
        return true;
      } catch (error) {
        state.lastError = error?.message || String(error);
        state.controlsArmed = false;
        return false;
      } finally {
        state.commandPending = false;
        render();
      }
    }

    async function holdOrResumeRoute() {
      if (state.commandPending) return false;
      const phase = state.executionRoute?.phase;
      const command = phase === 'running'
        ? controlClient?.pauseRoute
        : phase === 'paused' && canArm(state) ? controlClient?.resumeRoute : null;
      if (typeof command !== 'function') return false;
      state.commandPending = true;
      renderInterlocks();
      try {
        const result = await command.call(controlClient);
        if (!result?.applied && result?.ok !== true) {
          throw new Error(result?.error || 'bridge did not acknowledge route command');
        }
        return true;
      } catch (error) {
        state.lastError = error?.message || String(error);
        return false;
      } finally {
        state.commandPending = false;
        render();
      }
    }

    async function handoffControls() {
      if (!state.controlsArmed || state.commandPending || typeof controlClient?.handoff !== 'function') {
        return false;
      }
      state.commandPending = true;
      renderInterlocks();
      try {
        const result = await controlClient.handoff();
        if (!result?.applied && result?.ok !== true) {
          throw new Error(result?.error || 'bridge did not confirm RC handoff');
        }
        return true;
      } catch (error) {
        state.lastError = error?.message || String(error);
        return false;
      } finally {
        state.commandPending = false;
        render();
      }
    }

    function onModeClick(event) {
      const button = event.target.closest?.('[data-nymph-mode]');
      if (button) selectMode(button.dataset.nymphMode);
    }

    function onSurfaceClick(event) {
      const button = event.target.closest?.('[data-nymph-surface]');
      if (!button) return;
      state.surface = button.dataset.nymphSurface === 'ground' ? 'ground' : 'canopy';
      renderAltitude();
    }

    function onRibbonWheel(event) {
      event.preventDefault();
      nudgeAltitude(event.deltaY, event.shiftKey);
    }

    function onRibbonKey(event) {
      if (!['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === 'Home') setNextAltitude(ALTITUDE_LIMITS.min);
      else if (event.key === 'End') setNextAltitude(ALTITUDE_LIMITS.max);
      else nudgeAltitude(
        event.key === 'ArrowUp' || event.key === 'PageUp' ? -1 : 1,
        event.key === 'PageUp' || event.key === 'PageDown',
      );
    }

    function onCanvasWheel(event) {
      if (!state.activePane || state.selectedMode !== 'rts') return;
      event.preventDefault();
      event.stopImmediatePropagation?.();
      nudgeAltitude(event.deltaY, event.shiftKey);
    }

    function onMapPick(event) {
      if (!state.activePane || state.selectedMode !== 'rts') return;
      addGuidedTarget(event.detail || {});
    }

    function onPaneChange(event) {
      state.activePane = event.detail?.mode === 'nymphs' && event.detail?.open !== false;
      if (routeGroup) routeGroup.visible = state.activePane && state.selectedMode === 'rts';
    }

    function onBridgeEvent(event) { updateBridge(event.detail || {}); }
    function onTelemetryEvent(event) { updateTelemetry(event.detail || {}); }
    function onVideoEvent(event) { updateVideo(event.detail || {}); }

    els.modes?.addEventListener('click', onModeClick);
    els.surfaceSelect?.addEventListener('click', onSurfaceClick);
    els.altitudeRibbon?.addEventListener('wheel', onRibbonWheel, { passive: false });
    els.altitudeRibbon?.addEventListener('keydown', onRibbonKey);
    els.clearRoute?.addEventListener('click', clearRoute);
    els.arm?.addEventListener('click', armControls);
    els.hold?.addEventListener('click', holdOrResumeRoute);
    els.handoff?.addEventListener('click', handoffControls);
    canvas?.addEventListener('wheel', onCanvasWheel, { passive: false, capture: true });
    document.addEventListener('veil:map-pick', onMapPick);
    document.addEventListener('veil:panechange', onPaneChange);
    document.addEventListener('veil:nymph-bridge', onBridgeEvent);
    document.addEventListener('veil:nymph-telemetry', onTelemetryEvent);
    document.addEventListener('veil:nymph-video', onVideoEvent);

    const freshnessTimer = global.setInterval?.(() => renderLinkStatus(), 1000);
    render();

    return {
      state,
      selectMode,
      setNextAltitude,
      addGuidedTarget,
      clearRoute,
      updateBridge,
      updateTelemetry,
      updateVideo,
      attachVideo,
      isDrafting: () => state.activePane && state.selectedMode === 'rts',
      getState: () => cloneState(state),
      destroy() {
        if (freshnessTimer !== undefined) global.clearInterval?.(freshnessTimer);
        els.modes?.removeEventListener('click', onModeClick);
        els.surfaceSelect?.removeEventListener('click', onSurfaceClick);
        els.altitudeRibbon?.removeEventListener('wheel', onRibbonWheel);
        els.altitudeRibbon?.removeEventListener('keydown', onRibbonKey);
        els.clearRoute?.removeEventListener('click', clearRoute);
        els.arm?.removeEventListener('click', armControls);
        els.hold?.removeEventListener('click', holdOrResumeRoute);
        els.handoff?.removeEventListener('click', handoffControls);
        canvas?.removeEventListener('wheel', onCanvasWheel, true);
        document.removeEventListener('veil:map-pick', onMapPick);
        document.removeEventListener('veil:panechange', onPaneChange);
        document.removeEventListener('veil:nymph-bridge', onBridgeEvent);
        document.removeEventListener('veil:nymph-telemetry', onTelemetryEvent);
        document.removeEventListener('veil:nymph-video', onVideoEvent);
        clearRouteGroup();
        if (routeGroup?.parent) routeGroup.parent.remove(routeGroup);
      },
    };
  }

  global.VEILNymphManager = {
    create,
    _test: {
      ALTITUDE_LIMITS,
      TELEMETRY_MAX_AGE_MS,
      adjustAltitude,
      canArm,
      createRtsWaypoint,
      guidedCapability,
      interlockItems,
      normalizeAltitude,
      requestedAircraftMode,
      telemetryAgeMs,
      timestampMs,
    },
  };
})(typeof window !== 'undefined' ? window : globalThis);
