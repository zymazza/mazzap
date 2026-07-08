(function attachAstronomy(global) {
  'use strict';

  const { Astronomy, THREE } = global;
  const MIN_MS = Date.UTC(1600, 0, 1, 0, 0, 0);
  const MAX_MS = Date.UTC(2500, 0, 1, 0, 0, 0);
  const MAX_RATE = 604800;
  const LAYER_STORAGE = 'veil.astro.layers';
  const DEFAULT_LAYERS = {
    sun: true,
    moon: true,
    planets: true,
    stars: true,
    constellations: true,
  };
  const LAYER_ROWS = [
    ['sun', 'Sun', '#ffcf5a'],
    ['moon', 'Moon', '#d8e3ff'],
    ['planets', 'Planets', '#ff9f6e'],
    ['stars', 'Stars', '#d7ecff'],
    ['constellations', 'Constellations', '#6f9bd1'],
  ];

  function clamp(n, min, max) {
    return Math.max(min, Math.min(max, n));
  }

  function clampMs(ms) {
    return clamp(Number(ms) || Date.now(), MIN_MS, MAX_MS);
  }

  function clampRate(rate) {
    const n = Number(rate);
    return Number.isFinite(n) ? clamp(n, -MAX_RATE, MAX_RATE) : 1;
  }

  function esc(text) {
    return String(text ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function fmtDeg(value, digits = 1) {
    const n = Number(value);
    return Number.isFinite(n) ? `${n.toFixed(digits)}°` : '—';
  }

  function fmtTime(ms) {
    return new Date(ms).toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  function timeToMs(time) {
    if (time?.date instanceof Date) return time.date.getTime();
    const parsed = new Date(time?.toString?.() || time).getTime();
    return Number.isFinite(parsed) ? parsed : null;
  }

  function datetimeLocalValue(ms) {
    const d = new Date(clampMs(ms));
    const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 19);
  }

  function parseDatetimeLocal(value) {
    const t = new Date(value).getTime();
    return Number.isFinite(t) ? clampMs(t) : Date.now();
  }

  function dispatchInspect() {
    try {
      document.dispatchEvent(new CustomEvent('veil:inspect', { detail: { source: 'astronomy' } }));
    } catch (_err) {}
  }

  function readLayerPrefs() {
    try {
      return { ...DEFAULT_LAYERS, ...(JSON.parse(localStorage.getItem(LAYER_STORAGE) || '{}') || {}) };
    } catch (_err) {
      return { ...DEFAULT_LAYERS };
    }
  }

  function writeLayerPrefs(layers) {
    try { localStorage.setItem(LAYER_STORAGE, JSON.stringify(layers)); } catch (_err) {}
  }

  function createClock() {
    const subscribers = new Set();
    const state = {
      mode: 'realtime',
      epochMs: Date.now(),
      anchorPerfMs: performance.now(),
      rate: 1,
      playing: false,
      lastMs: Date.now(),
    };

    function now() {
      if (state.mode === 'realtime') return Date.now();
      const elapsed = performance.now() - state.anchorPerfMs;
      return clampMs(state.epochMs + elapsed * state.rate * (state.playing ? 1 : 0));
    }

    function tick() {
      const ms = now();
      const dt = ms - state.lastMs;
      state.lastMs = ms;
      subscribers.forEach((fn) => fn(ms, dt));
      return ms;
    }

    function setRealtime() {
      state.mode = 'realtime';
      state.playing = false;
      state.rate = 1;
      state.lastMs = Date.now();
    }

    function setManual(ms, opts = {}) {
      state.mode = 'manual';
      state.epochMs = clampMs(ms);
      state.anchorPerfMs = performance.now();
      state.rate = clampRate(opts.rate ?? state.rate ?? 1);
      state.playing = opts.playing ?? state.rate !== 0;
      state.lastMs = state.epochMs;
    }

    function setPlaying(on) {
      if (state.mode === 'realtime') {
        setManual(Date.now(), { rate: state.rate || 1, playing: !!on });
        return;
      }
      state.epochMs = now();
      state.anchorPerfMs = performance.now();
      state.playing = !!on;
      state.lastMs = state.epochMs;
    }

    function setRate(rate) {
      const current = now();
      state.mode = 'manual';
      state.epochMs = current;
      state.anchorPerfMs = performance.now();
      state.rate = clampRate(rate);
      state.playing = state.rate !== 0 && state.playing;
      state.lastMs = current;
    }

    return {
      state,
      now,
      tick,
      setRealtime,
      setManual,
      setPlaying,
      setRate,
      onTick(fn) {
        subscribers.add(fn);
        return () => subscribers.delete(fn);
      },
    };
  }

  function moonPhaseName(angle) {
    const a = ((Number(angle) % 360) + 360) % 360;
    if (a < 22.5 || a >= 337.5) return 'new';
    if (a < 67.5) return 'waxing crescent';
    if (a < 112.5) return 'first quarter';
    if (a < 157.5) return 'waxing gibbous';
    if (a < 202.5) return 'full';
    if (a < 247.5) return 'waning gibbous';
    if (a < 292.5) return 'last quarter';
    return 'waning crescent';
  }

  function create(api) {
    const els = {
      panel: document.getElementById('astronomy-panel'),
      master: document.getElementById('astro-master'),
      toggles: document.getElementById('astro-toggles'),
      time: document.getElementById('astro-time'),
      now: document.getElementById('astro-now'),
      play: document.getElementById('astro-play'),
      rate: document.getElementById('astro-rate'),
      jumps: document.getElementById('astro-jumps'),
      status: document.getElementById('astro-status'),
    };
    if (!els.panel || !api?.viewer || !api?.site || !Astronomy) return null;

    const viewer = api.viewer;
    const observer = new Astronomy.Observer(api.site.lat, api.site.lon, api.site.heightM || 0);
    const sky = global.VEILSky?.create({ viewer, site: api.site });
    const clock = createClock();
    const layers = readLayerPrefs();
    let statusLastAt = 0;
    let lastInputSyncAt = 0;
    const lastDirectiveSignature = {
      viewTime: JSON.stringify(null),
      skyViews: JSON.stringify([]),
    };

    if (sky?.pass) viewer.setSkyPass?.(sky.pass);
    clock.onTick((ms) => {
      sky?.setTime(ms);
      viewer.updatePhotometricSky?.(sky?.getLightingState?.());
    });
    viewer.onFrame?.(() => {
      const ms = clock.tick();
      if (performance.now() - statusLastAt > 500) {
        statusLastAt = performance.now();
        renderStatus(ms);
      }
      if (els.time && performance.now() - lastInputSyncAt > 1000) {
        lastInputSyncAt = performance.now();
        if (document.activeElement !== els.time) els.time.value = datetimeLocalValue(ms);
      }
    });

    function renderToggles() {
      if (!els.toggles) return;
      const rows = LAYER_ROWS.map(([kind, label, color]) => {
        const row = document.createElement('label');
        row.className = 'toggle-row';
        if (kind === 'constellations') row.classList.add('astro-subtoggle');
        row.innerHTML =
          `<input type="checkbox" ${layers[kind] ? 'checked' : ''} ${kind === 'constellations' && !layers.stars ? 'disabled' : ''} />` +
          `<span class="swatch" style="background:${color}"></span>` +
          `<span class="toggle-label">${esc(label)}</span>`;
        row.querySelector('input').addEventListener('change', (e) => {
          layers[kind] = e.target.checked;
          if (kind === 'stars' && !layers.stars) layers.constellations = false;
          writeLayerPrefs(layers);
          applyLayers();
          renderToggles();
        });
        return row;
      });
      els.toggles.replaceChildren(...rows);
    }

    function applyLayers() {
      Object.entries(layers).forEach(([kind, on]) => sky?.setLayerVisible(kind, on));
    }

    function renderStatus(ms) {
      if (!els.status) return;
      const s = sky?.getStatus?.();
      if (!s?.loaded && s?.error) {
        els.status.textContent = `Sky assets failed: ${s.error}`;
        els.status.className = 'sim-status warn';
        return;
      }
      const phase = s?.moonPhase ?? 0;
      const phasePct = s?.moonPhasePct ?? 0;
      const sun = s?.sun;
      const terrainH = Number.isFinite(sun?.azimuthDeg)
        ? global.__twin?.viewshed?.horizonAt?.(sun.azimuthDeg)
        : null;
      const terrainNote = Number.isFinite(terrainH)
        ? (sun.altitudeDeg < terrainH
          ? ` · Sun behind terrain (${terrainH.toFixed(1)}° horizon)`
          : ` · Terrain horizon ${terrainH.toFixed(1)}°`)
        : '';
      els.status.className = 'sim-status';
      els.status.textContent =
        `${fmtTime(ms)} · Sun ${fmtDeg(sun?.altitudeDeg)} alt / ${fmtDeg(sun?.azimuthDeg, 0)} az${terrainNote} · Moon ${phasePct.toFixed(0)}% ${moonPhaseName(phase)}`;
    }

    function setManualTime(ms, rate = 1, playing = true) {
      clock.setManual(ms, { rate, playing });
      if (els.time) els.time.value = datetimeLocalValue(clock.now());
      if (els.play) els.play.textContent = clock.state.playing ? 'Pause' : 'Play';
      if (els.rate) els.rate.value = String(clock.state.rate);
      renderStatus(clock.now());
    }

    function jumpTo(time, rate = 1) {
      const astroTime = Astronomy.MakeTime(time);
      const ms = astroTime.date instanceof Date ? astroTime.date.getTime() : new Date(time.toString()).getTime();
      setManualTime(ms, rate, rate !== 0);
    }

    function setNow() {
      clock.setRealtime();
      if (els.time) els.time.value = datetimeLocalValue(Date.now());
      if (els.play) els.play.textContent = 'Play';
      renderStatus(Date.now());
    }

    function nextRiseSet(body, direction) {
      return Astronomy.SearchRiseSet(body, observer, direction, Astronomy.MakeTime(new Date(clock.now())), 370);
    }

    function handleJump(kind) {
      const start = Astronomy.MakeTime(new Date(clock.now()));
      try {
        if (kind === 'sunset') jumpTo(nextRiseSet(Astronomy.Body.Sun, -1), 1);
        if (kind === 'sunrise') jumpTo(nextRiseSet(Astronomy.Body.Sun, +1), 1);
        if (kind === 'midday') jumpTo(Astronomy.SearchHourAngle(Astronomy.Body.Sun, observer, 0, start).time, 1);
        if (kind === 'night') jumpTo(Astronomy.SearchHourAngle(Astronomy.Body.Sun, observer, 12, start).time, 1);
      } catch (err) {
        if (els.status) {
          els.status.textContent = err?.message || String(err);
          els.status.className = 'sim-status warn';
        }
      }
    }

    function altAzFromDir(dir) {
      const alt = THREE.MathUtils.radToDeg(Math.asin(clamp(dir.y, -1, 1)));
      const az = (THREE.MathUtils.radToDeg(Math.atan2(dir.x, -dir.z)) + 360) % 360;
      return { alt, az };
    }

    function constellationName(raHours, decDeg) {
      try {
        const info = Astronomy.Constellation(raHours, decDeg);
        return info?.name || info?.symbol || '';
      } catch (_err) {
        return '';
      }
    }

    function enrichIdentifyInfo(info) {
      if (!info) return null;
      const enriched = { ...info };
      const time = Astronomy.MakeTime(new Date(clock.now()));
      if (info.kind === 'star') {
        enriched.constellation = constellationName(info.raJ2000Hours, info.decJ2000Deg);
        return enriched;
      }
      const body = Astronomy.Body[info.name];
      if (!body) return enriched;
      try {
        const eq = Astronomy.Equator(body, time, observer, true, true);
        const eqj = Astronomy.Equator(body, time, observer, false, true);
        const hor = Astronomy.Horizon(time, observer, eq.ra, eq.dec, null);
        const refr = Astronomy.Horizon(time, observer, eq.ra, eq.dec, 'normal');
        enriched.azimuthDeg = hor.azimuth;
        enriched.altitudeDeg = hor.altitude;
        enriched.altitudeRefractedDeg = refr.altitude;
        enriched.raHours = eq.ra;
        enriched.decDeg = eq.dec;
        enriched.raJ2000Hours = eqj.ra;
        enriched.decJ2000Deg = eqj.dec;
        enriched.constellation = constellationName(eqj.ra, eqj.dec);
        try {
          const illum = Astronomy.Illumination(body, time);
          enriched.magnitude = illum.mag;
          enriched.phaseFraction = illum.phase_fraction;
        } catch (_err) {}
      } catch (_err) {
        return enriched;
      }

      if (info.name === 'Moon') {
        try { enriched.nextFullMoonMs = timeToMs(Astronomy.SearchMoonPhase(180, time, 40)); } catch (_err) {}
        try { enriched.nextNewMoonMs = timeToMs(Astronomy.SearchMoonPhase(0, time, 40)); } catch (_err) {}
      } else if (info.name === 'Sun') {
        const d = new Date(clock.now());
        const dayStart = Astronomy.MakeTime(new Date(Date.UTC(
          d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 0, 0, 0
        )));
        try { enriched.todaySunriseMs = timeToMs(Astronomy.SearchRiseSet(body, observer, +1, dayStart, 1.5)); } catch (_err) {}
        try { enriched.todaySunsetMs = timeToMs(Astronomy.SearchRiseSet(body, observer, -1, dayStart, 1.5)); } catch (_err) {}
      }
      return enriched;
    }

    function identifyCard(info) {
      if (!info) return '';
      info = enrichIdentifyInfo(info);
      const rows = [];
      if (info.kind === 'star') {
        const aa = altAzFromDir(info.dir);
        rows.push(['Alt / az', `${fmtDeg(aa.alt)} / ${fmtDeg(aa.az, 0)}`]);
        rows.push(['RA / Dec', `${(info.raJ2000Hours ?? 0).toFixed(2)}h / ${fmtDeg(info.decJ2000Deg)}`]);
        if (info.constellation) rows.push(['Constellation', info.constellation]);
        rows.push(['Magnitude', Number(info.magnitude).toFixed(2)]);
        rows.push(['Catalog', `HIP ${info.hip}`]);
      } else {
        rows.push(['Alt / az', `${fmtDeg(info.altitudeDeg)} / ${fmtDeg(info.azimuthDeg, 0)}`]);
        rows.push(['RA / Dec', `${Number(info.raHours).toFixed(2)}h / ${fmtDeg(info.decDeg)}`]);
        if (info.constellation) rows.push(['Constellation', info.constellation]);
        if (Number.isFinite(info.magnitude)) rows.push(['Magnitude', Number(info.magnitude).toFixed(2)]);
        if (Number.isFinite(info.phaseFraction)) rows.push(['Illuminated', `${Math.round(info.phaseFraction * 100)}%`]);
        if (info.name === 'Moon') {
          if (Number.isFinite(info.nextFullMoonMs)) rows.push(['Next full', fmtTime(info.nextFullMoonMs)]);
          if (Number.isFinite(info.nextNewMoonMs)) rows.push(['Next new', fmtTime(info.nextNewMoonMs)]);
        } else if (info.name === 'Sun') {
          const rise = Number.isFinite(info.todaySunriseMs) ? fmtTime(info.todaySunriseMs) : '—';
          const set = Number.isFinite(info.todaySunsetMs) ? fmtTime(info.todaySunsetMs) : '—';
          rows.push(['Today rise / set', `${rise} / ${set}`]);
        }
      }
      return `<div class="info-card">
        <p class="info-layer">Astronomy</p>
        <p class="info-title">${esc(info.name)}</p>
        ${rows.map(([k, v]) => `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${esc(v)}</span></div>`).join('')}
      </div>`;
    }

    function renderSkyIdentify(info) {
      const host = document.getElementById('identify-results');
      if (!host) return;
      host.innerHTML = identifyCard(info);
      dispatchInspect();
    }

    function pickSky(ndc) {
      const info = sky?.pickAt?.(ndc, viewer.camera);
      if (!info) return false;
      renderSkyIdentify(info);
      return true;
    }

    function applySkyDirectives(skyViews, viewTime) {
      const normalizedViewTime = viewTime === undefined ? null : viewTime;
      const nextViewTimeSig = JSON.stringify(normalizedViewTime);
      if (nextViewTimeSig !== lastDirectiveSignature.viewTime) {
        lastDirectiveSignature.viewTime = nextViewTimeSig;
        if (normalizedViewTime === null) {
          setNow();
        } else if (normalizedViewTime?.iso) {
          const rate = clampRate(normalizedViewTime.rate ?? 1);
          setManualTime(new Date(normalizedViewTime.iso).getTime(), rate, rate !== 0);
        }
      }

      const normalizedSkyViews = Array.isArray(skyViews) ? skyViews : [];
      const nextSkyViewsSig = JSON.stringify(normalizedSkyViews);
      if (nextSkyViewsSig !== lastDirectiveSignature.skyViews) {
        lastDirectiveSignature.skyViews = nextSkyViewsSig;
        sky?.clearHighlights?.();
        normalizedSkyViews.forEach((view) => sky?.highlight?.(view));
      }
    }

    els.master?.addEventListener('change', (e) => {
      const on = e.target.checked;
      sky?.setPhysicalMode?.(on);
      viewer.setPhotometricMode?.(on, { astronomy: api.self || null });
      viewer.updatePhotometricSky?.(sky?.getLightingState?.());
    });
    els.now?.addEventListener('click', setNow);
    els.play?.addEventListener('click', () => {
      clock.setPlaying(!clock.state.playing);
      els.play.textContent = clock.state.playing ? 'Pause' : 'Play';
    });
    els.rate?.addEventListener('change', () => {
      clock.setRate(els.rate.value);
      if (clock.state.playing === false && clock.state.rate !== 0) clock.setPlaying(true);
      els.play.textContent = clock.state.playing ? 'Pause' : 'Play';
    });
    els.time?.addEventListener('change', () => {
      setManualTime(parseDatetimeLocal(els.time.value), Number(els.rate?.value || 1), false);
    });
    els.jumps?.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-astro-jump]');
      if (btn) handleJump(btn.dataset.astroJump);
    });

    if (els.time) {
      els.time.min = '1600-01-01T00:00:00';
      els.time.max = '2500-01-01T00:00:00';
      els.time.step = 'any';
      els.time.value = datetimeLocalValue(Date.now());
    }
    applyLayers();
    renderToggles();
    renderStatus(Date.now());

    const apiOut = {
      clock,
      sky,
      pickSky,
      applySkyDirectives,
      photometricOn: () => Boolean(viewer.photometricMode),
      setViewTime: setManualTime,
      highlightSky: (target) => sky?.highlight?.(target),
      clearSkyHighlights: () => sky?.clearHighlights?.(),
    };
    api.self = apiOut;
    return apiOut;
  }

  global.VEILAstronomy = {
    create,
    _test: { clampMs, clampRate, datetimeLocalValue, parseDatetimeLocal, moonPhaseName },
  };
})(window);
