/* "Fire" window: viewer-side wildfire simulation controls.
   Mirrors simulation.js: this module owns panel state and HTML only. Layer
   catalogs, draping, identify sampling, and terrain picking stay in app.js. */
(function attachWildfire(global) {
  'use strict';

  const WEATHER_PRESETS = [
    { id: 'normal_spring', label: 'Normal spring', title: 'Moderate spring fire-weather preset' },
    { id: 'high_spring', label: 'High - dry windy spring', title: 'Dry windy spring preset' },
    { id: 'extreme_redflag', label: 'Extreme - spring Red Flag', title: 'Severe Red Flag-style spring preset' },
    { id: 'summer_drought', label: 'Late-summer drought', title: 'Hot dry late-summer preset' },
    { id: 'dormant_fall', label: 'Dormant fall', title: 'Dormant-season fall preset' },
  ];
  const CUSTOM_WEATHER_ID = 'custom';
  const FUEL_SOURCE_TITLES = {
    landfire: 'National 30 m; may miss parcel-scale conifer',
    computed: 'LiDAR + LANDFIRE crosswalk; local, higher uncertainty',
  };

  function fmt(n, digits = 0) {
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
  }

  function isRecord(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function numberOrNull(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function fmtMaybe(value, digits = 0, fallback = 'unknown') {
    const n = numberOrNull(value);
    return n == null ? fallback : fmt(n, digits);
  }

  function pctMaybe(value) {
    const n = numberOrNull(value);
    return n == null ? 'unknown' : `${fmt(n * 100, 0)}%`;
  }

  function create(api) {
    const els = {
      panel: document.getElementById('fire-panel'),
      tier1Toggles: document.getElementById('fire-tier1-toggles'),
      pick: document.getElementById('fire-pick'),
      ignitionReadout: document.getElementById('fire-ignition-readout'),
      presets: document.getElementById('fire-presets'),
      customWeather: document.getElementById('fire-custom-weather'),
      customDate: document.getElementById('fire-custom-date'),
      customTemp: document.getElementById('fire-custom-temp'),
      customRh: document.getElementById('fire-custom-rh'),
      customWind: document.getElementById('fire-custom-wind'),
      customWindDir: document.getElementById('fire-custom-wind-dir'),
      customDaysRain: document.getElementById('fire-custom-days-rain'),
      customDrought: document.getElementById('fire-custom-drought'),
      fuelSource: document.getElementById('fire-fuel-source'),
      hydrology: document.getElementById('fire-hydrology'),
      form: document.getElementById('fire-form'),
      duration: document.getElementById('fire-duration'),
      run: document.getElementById('fire-run'),
      status: document.getElementById('fire-status'),
      scenarioGroup: document.getElementById('fire-scenario-group'),
      scenarioToggles: document.getElementById('fire-scenario-toggles'),
      results: document.getElementById('fire-results'),
    };
    if (!els.panel) return null;

    const state = {
      busy: false,
      mode: null,
      weatherClass: 'normal_spring',
      lastPresetWeatherClass: 'normal_spring',
      presetValues: {},
      ignition: null,
      scenarioActive: false,
      lastResult: null,
      summary: null,
      buildings: [],
      marker: null,
      anim: {
        t: 0,
        duration: 0,
        speed: 12,
        playing: false,
        raf: 0,
        lastMs: null,
      },
    };

    const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    async function quietFetch(url) {
      try {
        const r = await fetch(url);
        return r.ok ? await r.json() : null;
      } catch (_e) { return null; }
    }

    async function fetchOptionalJson(url) {
      try {
        const r = await fetch(url);
        if (!r.ok) {
          return { data: null, error: r.status === 404 ? null : `HTTP ${r.status}` };
        }
        return { data: await r.json(), error: null };
      } catch (err) {
        return { data: null, error: err?.message || String(err || 'load failed') };
      }
    }

    function presetLabel(id) {
      if (id === CUSTOM_WEATHER_ID) return 'Custom weather';
      const values = isRecord(state.presetValues[id]) ? state.presetValues[id] : {};
      return values.label || WEATHER_PRESETS.find((p) => p.id === id)?.label || id;
    }

    function presetById(id) {
      const meta = WEATHER_PRESETS.find((p) => p.id === id) || WEATHER_PRESETS[0];
      const values = isRecord(state.presetValues[meta.id]) ? state.presetValues[meta.id] : {};
      return { ...meta, ...values };
    }

    function writeCustomWeather(values) {
      if (els.customDate) els.customDate.value = values.date || '';
      if (els.customTemp) els.customTemp.value = String(values.temp_f ?? '');
      if (els.customRh) els.customRh.value = String(values.rh_min ?? '');
      if (els.customWind) els.customWind.value = String(values.wind_mph ?? '');
      if (els.customWindDir) els.customWindDir.value = String(values.wind_dir ?? '');
      if (els.customDaysRain) els.customDaysRain.value = String(values.days_since_rain ?? '');
      if (els.customDrought) els.customDrought.value = values.drought || 'normal';
    }

    function writeSelectedPresetWeather() {
      writeCustomWeather(presetById(state.lastPresetWeatherClass));
    }

    function formNumber(el, fallback) {
      const n = numberOrNull(el?.value);
      return n == null ? fallback : n;
    }

    function readCustomWeather() {
      const preset = presetById(state.lastPresetWeatherClass);
      return {
        date: els.customDate?.value || preset.date,
        temp_f: formNumber(els.customTemp, preset.temp_f),
        rh_min: formNumber(els.customRh, preset.rh_min),
        wind_mph: formNumber(els.customWind, preset.wind_mph),
        wind_dir: formNumber(els.customWindDir, preset.wind_dir),
        days_since_rain: formNumber(els.customDaysRain, preset.days_since_rain),
        drought: els.customDrought?.value || preset.drought,
      };
    }

    function markCustomWeather() {
      if (state.weatherClass !== CUSTOM_WEATHER_ID) {
        state.lastPresetWeatherClass = state.weatherClass;
        state.weatherClass = CUSTOM_WEATHER_ID;
        renderPresets();
      }
      syncControls();
    }

    function syncControls() {
      els.pick.classList.toggle('active', state.mode === 'pick');
      els.pick.textContent = state.mode === 'pick' ? 'Picking ignition...' : 'Pick ignition';
      if (state.ignition) {
        els.ignitionReadout.textContent =
          `${state.ignition.lat.toFixed(6)}, ${state.ignition.lon.toFixed(6)}`;
      } else {
        els.ignitionReadout.textContent = 'none - pick a point on the terrain';
      }
      els.run.disabled = state.busy || !state.ignition;
      if (els.fuelSource) {
        els.fuelSource.title = FUEL_SOURCE_TITLES[els.fuelSource.value] || '';
      }
      if (els.customWeather) {
        els.customWeather.hidden = false;
      }
    }

    function renderPresets() {
      els.presets.replaceChildren();
      WEATHER_PRESETS.forEach((preset) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = presetLabel(preset.id);
        btn.title = preset.title;
        btn.classList.toggle('active', state.weatherClass === preset.id);
        btn.addEventListener('click', () => {
          state.weatherClass = preset.id;
          state.lastPresetWeatherClass = preset.id;
          writeCustomWeather(presetById(preset.id));
          renderPresets();
          syncControls();
        });
        els.presets.appendChild(btn);
      });
      if (state.weatherClass === CUSTOM_WEATHER_ID) {
        const custom = document.createElement('p');
        custom.className = 'readout-hint sim-note';
        custom.textContent = `Custom weather values, based on ${presetLabel(state.lastPresetWeatherClass)}`;
        els.presets.appendChild(custom);
      }
    }

    /* ----------------------------------------------- layer toggle sections */

    function toggleRow(layer) {
      const row = document.createElement('label');
      row.className = 'toggle-row';
      const swatch = layer.group === 'fire_scenario' ? '#e0562a' : '#d98a3a';
      const loading = !!api.isLoading?.(layer.id);
      row.classList.toggle('loading', loading);
      row.innerHTML =
        `<input type="checkbox" ${api.isEnabled(layer.id) ? 'checked' : ''} ${loading ? 'disabled' : ''} />` +
        `<span class="swatch" style="background:${swatch}"></span>` +
        `<span class="toggle-label">${esc(layer.label)}</span>`;
      if (layer.description) row.title = layer.description;
      row.querySelector('input').addEventListener('change', async (e) => {
        e.target.disabled = true;
        try {
          await api.setEnabled(layer, e.target.checked);
        } finally {
          renderToggles();
        }
      });
      return row;
    }

    function renderToggles() {
      const layers = (api.catalog()?.layers) || [];
      const fire = layers.filter((l) => l.group === 'fire');
      const scenario = layers.filter((l) => l.group === 'fire_scenario');
      if (fire.length) {
        els.tier1Toggles.replaceChildren(...fire.map(toggleRow));
      }
      const showScenario = state.scenarioActive && scenario.length;
      els.scenarioGroup.hidden = !showScenario;
      els.scenarioToggles.replaceChildren(...(showScenario ? scenario.map(toggleRow) : []));
    }

    /* --------------------------------------------- scenario run + results */

    function resultRows(r) {
      const scenario = isRecord(r.scenario) ? r.scenario : {};
      const ignition = isRecord(r.ignition) ? r.ignition : {};
      const ros = isRecord(r.ros_at_ignition) ? r.ros_at_ignition : {};
      const moist = isRecord(r.derived_moistures) ? r.derived_moistures : {};
      const crown = isRecord(r.crown_fractions_burned_area) ? r.crown_fractions_burned_area : {};
      const burned = isRecord(r.burned_area) ? r.burned_area : {};
      const validity = isRecord(r.crown_model_validity) ? r.crown_model_validity : {};
      const spotting = isRecord(r.spotting) ? r.spotting : {};
      const rows = [
        ['Ignition', numberOrNull(ignition.lat) != null && numberOrNull(ignition.lon) != null
          ? `${Number(ignition.lat).toFixed(6)}, ${Number(ignition.lon).toFixed(6)}`
          : 'unknown'],
        ['Weather', `${scenario.date ? `${scenario.date}, ` : ''}${scenario.temp_f ?? '?'} F, Relative humidity ${scenario.rh_min ?? '?'}%, wind ${scenario.wind_mph ?? '?'} mph 20-ft open toward ${scenario.wind_dir ?? '?'} deg`],
        ['ROS at ignition', `head ${fmtMaybe(ros.head_m_min, 2)} m/min / flank ${fmtMaybe(ros.flank_m_min, 2)} / back ${fmtMaybe(ros.back_m_min, 2)}`],
        ['Max flame length', `${fmtMaybe(r.max_flame_length_m, 2)} m`],
        ['Crown fractions', `surface ${pctMaybe(crown.surface?.fraction)} / passive ${pctMaybe(crown.passive?.fraction)} / active ${pctMaybe(crown.active?.fraction)}`],
        ['Burned area', `${fmtMaybe(burned.ha, 2)} ha over ${fmtMaybe(burned.duration_min || scenario.duration_min, 0)} min`],
        ['Ember exposure', `${fmtMaybe(spotting.max_downwind_distance_m, 0, '0')} m downwind band / ${fmtMaybe(spotting.exposed_cells, 0, '0')} cells`],
        ['Dead moistures', `1h ${fmtMaybe(moist.dead_1h_pct, 1)}% / 10h ${fmtMaybe(moist.dead_10h_pct, 1)}% / 100h ${fmtMaybe(moist.dead_100h_pct, 1)}%`],
        ['Live / FMC', `herb ${fmtMaybe(moist.live_herb_pct, 0)}% / woody ${fmtMaybe(moist.live_woody_pct, 0)}% / FMC ${fmtMaybe(moist.fmc_pct, 0)}% (${r.fmc_method || 'method unknown'})`],
        ['Crown validity', `${pctMaybe(validity.valid_fraction_of_footprint)} of footprint`],
      ];
      if (ignition.source_on_nonburnable) {
        rows.splice(1, 0, ['Ignition source',
          `structure / developed ground — fire carries into wildland fuel ~${fmtMaybe(ignition.fuel_seed_distance_m, 0)} m away`]);
      }
      return rows;
    }

    function durationFromResult(r) {
      return numberOrNull(r?.scenario?.duration_min) ??
        numberOrNull(r?.burned_area?.duration_min) ??
        numberOrNull(els.duration?.value) ??
        240;
    }

    function defaultAnimSpeed(duration) {
      return Math.max(1, Math.round((Number(duration) || 120) / 12));
    }

    function speedOptions(duration, selected) {
      return [...new Set([1, 4, 12, defaultAnimSpeed(duration), Number(selected)])]
        .filter((n) => Number.isFinite(n) && n > 0)
        .sort((a, b) => a - b);
    }

    function fireAnimControlsHtml(duration, speed) {
      const safeDuration = Math.max(1, Math.round(Number(duration) || 1));
      const opts = speedOptions(safeDuration, speed).map((value) =>
        `<option value="${value}" ${Number(value) === Number(speed) ? 'selected' : ''}>${value} min/s</option>`).join('');
      return (
        '<div id="fire-animation-controls" class="sim-form">' +
          '<div class="sim-field-row">' +
            '<label class="grow" for="fire-anim-slider" style="flex-direction:column;align-items:stretch;gap:4px">' +
              '<span id="fire-anim-time">t = 0 min</span>' +
              `<input id="fire-anim-slider" type="range" min="0" max="${safeDuration}" step="1" value="${safeDuration}" aria-label="Fire scenario time" />` +
            '</label>' +
          '</div>' +
          '<div class="sim-field-row">' +
            '<button id="fire-anim-play" type="button" class="wide-btn">Play</button>' +
            `<label for="fire-anim-speed">Speed <select id="fire-anim-speed" aria-label="Fire animation speed">${opts}</select></label>` +
          '</div>' +
        '</div>'
      );
    }

    function reducedMotion() {
      return !!global.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
    }

    function animEls() {
      return {
        slider: document.getElementById('fire-anim-slider'),
        time: document.getElementById('fire-anim-time'),
        play: document.getElementById('fire-anim-play'),
        speed: document.getElementById('fire-anim-speed'),
      };
    }

    function syncAnimControls() {
      const a = state.anim;
      const el = animEls();
      if (el.slider) {
        el.slider.max = String(Math.max(1, Math.round(a.duration || 1)));
        el.slider.value = String(Math.round(Math.max(0, Math.min(a.duration || 0, a.t || 0))));
      }
      if (el.time) el.time.textContent = `t = ${fmt(Math.round(a.t || 0), 0)} min`;
      if (el.play) el.play.textContent = a.playing ? 'Pause' : 'Play';
      if (el.speed) el.speed.value = String(a.speed);
    }

    function stopAnimation() {
      if (state.anim.raf) {
        global.cancelAnimationFrame(state.anim.raf);
        state.anim.raf = 0;
      }
      state.anim.playing = false;
      state.anim.lastMs = null;
      syncAnimControls();
    }

    function setRevealTime(t) {
      const duration = Math.max(1, Number(state.anim.duration) || 1);
      state.anim.t = Math.max(0, Math.min(duration, Number(t) || 0));
      global.__twin?.fireReveal?.set?.(state.anim.t, duration);
      syncAnimControls();
    }

    function stepAnimation(ms) {
      if (!state.anim.playing) return;
      if (state.anim.lastMs == null) state.anim.lastMs = ms;
      const dt = Math.max(0, (ms - state.anim.lastMs) / 1000);
      state.anim.lastMs = ms;
      const next = state.anim.t + state.anim.speed * dt;
      if (next >= state.anim.duration) {
        setRevealTime(state.anim.duration);
        stopAnimation();
        return;
      }
      setRevealTime(next);
      state.anim.raf = global.requestAnimationFrame(stepAnimation);
    }

    function playAnimation(fromStart = false) {
      if (!state.anim.duration) return;
      if (fromStart || state.anim.t >= state.anim.duration) setRevealTime(0);
      stopAnimation();
      state.anim.playing = true;
      syncAnimControls();
      state.anim.raf = global.requestAnimationFrame(stepAnimation);
    }

    function bindAnimationControls() {
      const el = animEls();
      if (!el.slider || !el.play || !el.speed) return;
      el.slider.addEventListener('input', () => {
        // read the dragged value BEFORE stopAnimation(), which resyncs the
        // slider back to the current time and would otherwise clobber it
        const t = Number(el.slider.value);
        stopAnimation();
        setRevealTime(t);
      });
      el.play.addEventListener('click', () => {
        if (state.anim.playing) {
          stopAnimation();
        } else {
          playAnimation(state.anim.t >= state.anim.duration);
        }
      });
      el.speed.addEventListener('change', () => {
        const speed = numberOrNull(el.speed.value);
        if (speed != null && speed > 0) state.anim.speed = speed;
        syncAnimControls();
      });
      syncAnimControls();
    }

    function prepareAnimation(r) {
      const duration = durationFromResult(r);
      state.anim.duration = Math.max(1, Math.round(duration));
      state.anim.speed = defaultAnimSpeed(state.anim.duration);
      state.anim.t = state.anim.duration;
      state.anim.playing = false;
      state.anim.lastMs = null;
    }

    function startAnimationOnce() {
      setRevealTime(0);
      if (!reducedMotion()) playAnimation(true);
    }

    function renderResult(r, opts = {}) {
      state.scenarioActive = opts.active !== false;
      state.lastResult = isRecord(r) ? r : {};
      prepareAnimation(state.lastResult);
      const scenario = isRecord(state.lastResult.scenario) ? state.lastResult.scenario : {};
      const label = state.weatherClass === CUSTOM_WEATHER_ID
        ? presetLabel(CUSTOM_WEATHER_ID)
        : (scenario.weather_label || presetLabel(scenario.weather_class || state.weatherClass));
      const shift = isRecord(state.lastResult.fuel_model_shift) ? state.lastResult.fuel_model_shift : {};
      const shifted = numberOrNull(shift.changed_cells);
      const fuelNote = state.lastResult.fuel_source === 'computed' && shifted != null && shifted > 0
        ? `<p class="readout-hint sim-note">Computed fuel: ${esc(fmt(shifted, 0))} cells reclassified, forest now carries fire.</p>`
        : '';
      const hydro = isRecord(state.lastResult.hydrology) ? state.lastResult.hydrology : {};
      const hydroBarrier = numberOrNull(hydro.barrier_cells);
      const hydroWet = numberOrNull(hydro.wet_cells);
      const hydroNote = hydro.on !== false && (hydroBarrier != null || hydroWet != null)
        ? `<p class="readout-hint sim-note">Hydrology: ${esc(fmt(hydroBarrier || 0, 0))} cells blocked by water, ${esc(fmt(hydroWet || 0, 0))} wet-damped.</p>`
        : '';
      els.results.innerHTML =
        `<p class="sim-scenario-label">${esc(label)}</p>` +
        resultRows(state.lastResult).map(([k, v]) =>
          `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${esc(v)}</span></div>`).join('') +
        fuelNote +
        hydroNote +
        '<p class="readout-hint sim-note">Crown potential depends heavily on the fuel model - compare LANDFIRE vs Computed.</p>' +
        '<p class="readout-hint sim-note">Unsuppressed potential with uniform wind; embers can cross water, wetlands, roads, and cleared gaps.</p>' +
        '<p class="readout-hint sim-note">Scenario-grade spread screen: geometry reliable, magnitude +/- class.</p>' +
        fireAnimControlsHtml(state.anim.duration, state.anim.speed);
      renderToggles();
      bindAnimationControls();
      return state.lastResult;
    }

    function buildParams() {
      const params = {
        ignition_x: state.ignition.x,
        ignition_y: state.ignition.y,
        fuel_source: els.fuelSource?.value || 'landfire',
        hydrology: els.hydrology?.checked === false ? 'off' : 'on',
        duration_min: parseFloat(els.duration.value) || 240,
      };
      if (state.weatherClass === CUSTOM_WEATHER_ID) {
        const custom = readCustomWeather();
        Object.assign(params, {
          weather_class: CUSTOM_WEATHER_ID,
          temp_f: custom.temp_f,
          rh_min: custom.rh_min,
          wind_mph: custom.wind_mph,
          wind_dir: custom.wind_dir,
          days_since_rain: custom.days_since_rain,
          drought: custom.drought,
          date: custom.date,
        });
        // TODO: add the deferred gridMET/local-percentile option here when
        // the scenario API exposes a percentile source.
      } else {
        params.weather_class = state.weatherClass;
      }
      return params;
    }

    els.fuelSource?.addEventListener('change', syncControls);
    els.hydrology?.addEventListener('change', syncControls);
    [
      els.customDate,
      els.customTemp,
      els.customRh,
      els.customWind,
      els.customWindDir,
      els.customDaysRain,
      els.customDrought,
    ].forEach((el) => {
      el?.addEventListener('input', markCustomWeather);
      el?.addEventListener('change', markCustomWeather);
    });

    els.form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (state.busy || !state.ignition) return;
      stopAnimation();
      global.__twin?.fireReveal?.clear?.();
      state.scenarioActive = false;
      renderToggles();
      state.busy = true;
      syncControls();
      els.status.textContent = 'Simulating fire scenario...';
      try {
        const res = await fetch('/api/fire-simulate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildParams()),
        });
        const data = await res.json();
        if (!data || typeof data !== 'object') throw new Error('Scenario returned an empty result.');
        if (data.error) throw new Error(data.error);
        renderResult(data);
        els.status.textContent = '';
        await api.refresh(data.layers || []);
        renderToggles();
        startAnimationOnce();
      } catch (err) {
        els.status.textContent = 'Fire scenario failed: ' + (err?.message || err);
      } finally {
        state.busy = false;
        syncControls();
      }
    });

    /* ------------------------------------------------ ignition picker ---- */

    function markerHeight(x, yNorth, point) {
      const grid = global.__twin?.viewer?.terrainGrid;
      if (grid && global.VEILTerrain?.sampleTerrainHeightAtLocal) {
        return global.VEILTerrain.sampleTerrainHeightAtLocal(grid, x, yNorth) + 2.2;
      }
      return (point?.y || 0) + 2.2;
    }

    function placeIgnitionMarker(point, x, y) {
      const viewer = global.__twin?.viewer;
      const THREE = global.THREE;
      if (!viewer?.scene || !THREE) return;
      if (!state.marker) {
        state.marker = new THREE.Mesh(
          new THREE.SphereGeometry(2.8, 16, 12),
          new THREE.MeshBasicMaterial({ color: 0xe0562a })
        );
        state.marker.renderOrder = 1000;
        viewer.scene.add(state.marker);
      }
      state.marker.position.set(x, markerHeight(x, y, point), -y);
    }

    function pickIgnition(hit, geo) {
      if (!hit?.point || !geo) return false;
      const x = Math.round(hit.point.x * 100) / 100;
      const y = Math.round(-hit.point.z * 100) / 100;
      state.ignition = {
        x,
        y,
        lat: Number(geo.lat),
        lon: Number(geo.lon),
      };
      state.mode = null;
      placeIgnitionMarker(hit.point, x, y);
      syncControls();
      els.status.textContent = '';
      return true;
    }

    els.pick.addEventListener('click', () => {
      state.mode = state.mode === 'pick' ? null : 'pick';
      if (state.mode === 'pick' && global.__twin?.chat?.state) {
        global.__twin.chat.state.mode = null;
      }
      syncControls();
    });

    const flyout = document.getElementById('flyout');
    if (flyout && typeof MutationObserver === 'function') {
      new MutationObserver(() => {
        if (flyout.hidden && els.panel.classList.contains('active')) stopAnimation();
      }).observe(flyout, { attributes: true, attributeFilter: ['hidden'] });
      new MutationObserver(() => {
        if (!els.panel.classList.contains('active')) stopAnimation();
      }).observe(els.panel, { attributes: true, attributeFilter: ['class'] });
    }

    /* --------------- natural-language identify (called from app.js) -------
       samples: [{layer, grid, value}] for every enabled fire layer at the
       clicked point. Returns one card's inner HTML, or null. */

    function firstMetaValue(sample, keys) {
      const sources = [sample?.grid, sample?.layer, sample?.grid?.metadata, sample?.layer?.metadata];
      for (const source of sources) {
        if (!source) continue;
        for (const key of keys) {
          if (source[key] !== undefined && source[key] !== null && source[key] !== '') {
            return source[key];
          }
        }
      }
      return null;
    }

    function normalizeUnit(unit) {
      return String(unit || '').trim().toLowerCase()
        .replace(/²/g, '2')
        .replace(/\s+/g, '_')
        .replace(/-/g, '_');
    }

    function sampleMeta(sample, kind, units) {
      const valueKind = firstMetaValue(sample, ['value_kind', 'valueKind', 'kind']);
      const unitRaw = firstMetaValue(sample, ['value_unit', 'value_units', 'unit', 'units']);
      const unit = normalizeUnit(unitRaw);
      const kindOk = String(valueKind || '') === kind;
      const unitOk = units.includes(unit);
      return { ok: kindOk && unitOk, valueKind, unitRaw, unit };
    }

    function fmtRaw(value) {
      const n = numberOrNull(value);
      return n == null ? String(value) : fmt(n, 2);
    }

    function genericSentence(sample) {
      const label = sample?.layer?.label || sample?.layer?.id || 'Fire layer';
      const unit = firstMetaValue(sample, ['value_unit', 'value_units', 'unit', 'units']);
      return `${label} reports raw value ${fmtRaw(sample?.value)}${unit ? ` ${unit}` : ''} here; its unit metadata is incomplete or unexpected, so this card is not interpreting it further.`;
    }

    function fmtFireMeasure(value, unit) {
      const n = Number(value);
      if (!Number.isFinite(n)) return 'unknown';
      const rounded = Math.abs(n) < 10 ? fmt(n, 1) : fmt(Math.round(n), 0);
      return `${rounded} ${unit}`;
    }

    function fmtArrival(value) {
      const minutes = Number(value);
      if (!Number.isFinite(minutes)) return 'unknown';
      if (minutes < 90) return `${Math.round(minutes)} min`;
      return `${(minutes / 60).toFixed(1)} hr`;
    }

    function fuelFriendlyName(name) {
      const text = String(name || '').replace(/\s+/g, ' ').trim();
      if (!text) return '';
      return text.toLowerCase()
        .replace(/^(low|moderate|high) load\b/, '$1-load');
    }

    function fuelSentence(sample) {
      const meta = sampleMeta(sample, 'fbfm40_fuel_model', ['code']);
      if (!meta.ok) return genericSentence(sample);
      const code = numberOrNull(sample?.value);
      if (code == null) return genericSentence(sample);
      const key = String(Math.round(code));
      const legend = sample?.grid?.legend?.[key] || {};
      const summaryFuel = (state.summary?.fuel_model_breakdown || [])
        .find((f) => Number(f.code) === Number(key));
      const shortName = summaryFuel?.short_name || legend.short_name || legend.name || key;
      const friendly = fuelFriendlyName(summaryFuel?.name || legend.friendly_name || '');
      return friendly
        ? `Fuel here: ${shortName} \u2014 ${friendly}.`
        : `Fuel here: ${shortName}.`;
    }

    function rosValue(sample) {
      if (!sample) return { value: null, sentence: null };
      const meta = sampleMeta(sample, 'surface_rate_of_spread', ['m/min', 'm_per_min', 'meter/min', 'meters/minute', 'meters_per_minute']);
      if (!meta.ok) return { value: null, sentence: genericSentence(sample) };
      const value = numberOrNull(sample.value);
      return value == null ? { value: null, sentence: genericSentence(sample) } : { value, sentence: null };
    }

    function rosSentence(baseSample, slopeSample) {
      const base = rosValue(baseSample);
      const slope = rosValue(slopeSample);
      if (baseSample && base.sentence) return base.sentence;
      if (!baseSample && slopeSample && slope.sentence) return slope.sentence;
      if (base.value != null) {
        const slopePart = slope.value != null
          ? `, ~${fmtFireMeasure(slope.value, 'm/min')} with this slope`
          : '';
        return `On a moderate day this fuel carries fire at ~${fmtFireMeasure(base.value, 'm/min')} on flat ground${slopePart}.`;
      }
      if (slope.value != null) {
        return `On a moderate day this slope-adjusted fuel carries fire at ~${fmtFireMeasure(slope.value, 'm/min')}.`;
      }
      return null;
    }

    function arrivalSentence(sample) {
      const meta = sampleMeta(sample, 'fire_arrival_time', ['min', 'minute', 'minutes']);
      if (!meta.ok) return genericSentence(sample);
      const value = numberOrNull(sample.value);
      if (value == null) return genericSentence(sample);
      const duration = numberOrNull(state.lastResult?.scenario?.duration_min);
      if (duration != null && value > duration) {
        return `The fire never reaches this spot in this scenario (within its ${Math.round(duration)}-minute window).`;
      }
      return `Fire reaches this spot ~${fmtArrival(value)} after ignition (\u00b1class; one wind guess).`;
    }

    function flameCharacter(flameM) {
      if (flameM < 0.5) return 'a low creeping surface fire';
      if (flameM < 1.2) return 'an easy surface fire \u2014 hand-tool territory';
      if (flameM < 2.4) return 'a serious surface fire \u2014 beyond direct hand attack';
      if (flameM < 3.5) return 'torching range; equipment territory';
      return 'crown-fire intensity';
    }

    function intensityValue(sample) {
      if (!sample) return { value: null, sentence: null };
      const meta = sampleMeta(sample, 'fireline_intensity', ['kw/m', 'kw_per_m', 'kw_per_meter', 'kilowatt/m', 'kilowatts_per_meter']);
      if (!meta.ok) return { value: null, sentence: genericSentence(sample) };
      const value = numberOrNull(sample.value);
      return value == null ? { value: null, sentence: genericSentence(sample) } : { value, sentence: null };
    }

    function flameSentence(flameSample, intensitySample) {
      const meta = sampleMeta(flameSample, 'flame_length', ['m', 'meter', 'meters']);
      if (!meta.ok) return { sentence: genericSentence(flameSample), usedIntensity: false };
      const flameM = numberOrNull(flameSample.value);
      if (flameM == null) return { sentence: genericSentence(flameSample), usedIntensity: false };
      const intensity = intensityValue(intensitySample);
      const intensityPart = intensity.value != null
        ? ` (~${fmtFireMeasure(intensity.value, 'kW/m')})`
        : '';
      return {
        sentence: `~${fmtFireMeasure(flameM, 'm')} flames here${intensityPart} \u2014 ${flameCharacter(flameM)}.`,
        usedIntensity: intensity.value != null,
      };
    }

    function intensitySentence(sample) {
      const intensity = intensityValue(sample);
      if (intensity.sentence) return intensity.sentence;
      if (intensity.value == null) return null;
      return `Fireline intensity is ~${fmtFireMeasure(intensity.value, 'kW/m')} here.`;
    }

    function crownSentence(sample, context) {
      const meta = sampleMeta(sample, 'crown_fire_class', ['class']);
      if (!meta.ok) return genericSentence(sample);
      const cls = Math.round(Number(sample.value));
      const suffix = context === 'scenario' ? 'under this scenario' : 'under the reference worst-case day';
      if (cls === 0) return `Modeled as a surface fire here ${suffix} \u2014 the canopy is not predicted to ignite.`;
      if (cls === 1) return `Passive crown fire (torching) is modeled here ${suffix} \u2014 individual trees or clumps candle.`;
      if (cls === 2) return `Active crown fire is modeled here ${suffix} \u2014 fire carries through the canopy.`;
      return genericSentence(sample);
    }

    function emberSentence(sample) {
      const meta = sampleMeta(sample, 'ember_exposure', ['class']);
      if (!meta.ok) return genericSentence(sample);
      const value = numberOrNull(sample.value);
      if (value == null || value < 1) return null;
      return 'This spot is in the downwind ember-exposure band; firebrands can cross water, wetlands, roads, and cleared gaps even when the surface-arrival layer stops.';
    }

    function thresholdLabel(kind) {
      if (kind === 'torching_index') {
        return { verb: 'torches', noun: 'torch', event: 'torching' };
      }
      return { verb: 'actively crowns', noun: 'actively crown', event: 'active crowning' };
    }

    function crownThresholdSentence(sample) {
      const kind = sample?.layer?.id;
      const meta = sampleMeta(sample, kind, ['mph_20ft_open', 'mph']);
      if (!meta.ok || (kind !== 'torching_index' && kind !== 'crowning_index')) {
        return genericSentence(sample);
      }
      const value = numberOrNull(sample.value);
      if (value == null) return genericSentence(sample);
      const cap = numberOrNull(firstMetaValue(sample, ['threshold_cap_mph'])) || 120;
      const notReached = numberOrNull(firstMetaValue(sample, ['not_reached_value']));
      const label = thresholdLabel(kind);
      if (notReached != null && Math.round(value) === Math.round(notReached)) {
        return `Crown-resistant here: does not ${label.noun} below ${fmt(cap, 0)} mph 20-ft open wind (given this fuel).`;
      }
      const reading = `~${fmt(value, value < 10 ? 1 : 0)} mph 20-ft open wind`;
      const character = value <= 30 ? 'crown-prone' : value <= 60 ? 'moderately crown-prone' : 'crown-resistant';
      return `This stand ${label.verb} at ${reading} \u2014 ${character}.`;
    }

    function monthDay(dateText) {
      const m = String(dateText || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
      if (!m) return '';
      const names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      return `${names[Number(m[2]) - 1] || m[2]} ${Number(m[3])}`;
    }

    function scenarioRecapSentence() {
      if (!state.lastResult) return null;
      const scenario = isRecord(state.lastResult.scenario) ? state.lastResult.scenario : {};
      const moist = isRecord(state.lastResult.derived_moistures) ? state.lastResult.derived_moistures : {};
      const label = String(state.weatherClass === CUSTOM_WEATHER_ID
        ? presetLabel(CUSTOM_WEATHER_ID)
        : (scenario.weather_label || scenario.label || presetLabel(scenario.weather_class || state.weatherClass) || 'Scenario'))
        .replace(/\s+-\s+/g, ' \u2014 ');
      const facts = [];
      const date = monthDay(scenario.date);
      if (date) facts.push(date);
      if (numberOrNull(scenario.rh_min) != null) facts.push(`Relative humidity ${fmt(scenario.rh_min, 0)}%`);
      if (numberOrNull(scenario.wind_mph) != null) {
        const dir = numberOrNull(scenario.wind_dir);
        facts.push(dir == null
          ? `${fmt(scenario.wind_mph, 0)} mph 20-ft wind`
          : `${fmt(scenario.wind_mph, 0)} mph 20-ft wind toward ${fmt(dir, 0)} deg`);
      }
      const moistureBits = [];
      if (numberOrNull(moist.dead_1h_pct) != null) moistureBits.push(`1h ${fmt(moist.dead_1h_pct, 1)}%`);
      if (numberOrNull(moist.fmc_pct) != null) moistureBits.push(`FMC ${fmt(moist.fmc_pct, 0)}%`);
      const method = String(state.lastResult.fmc_method || state.lastResult.fmc_method_selected || '')
        .replace(/^fbp_/, '').replace(/_/g, ' ');
      const factText = facts.length ? ` (${facts.join(', ')})` : '';
      const moistText = moistureBits.length
        ? ` - moistures ${moistureBits.join(' / ')}${method ? ` (${method})` : ''}`
        : '';
      return `Scenario: ${label}${factText}${moistText}.`;
    }

    function buildingPlacements(manifest) {
      const items = Array.isArray(manifest?.buildings) ? manifest.buildings
        : Array.isArray(manifest?.placements) ? manifest.placements
          : Array.isArray(manifest) ? manifest : [];
      return items.map((item) => {
        const placement = isRecord(item?.placement) ? item.placement : item;
        const x = numberOrNull(placement?.x ?? placement?.scene_x ?? placement?.local_x);
        const y = numberOrNull(placement?.y ?? placement?.scene_y ?? placement?.local_y);
        if (x == null || y == null) return null;
        return { id: item?.id || item?.name || 'building', name: item?.name || item?.id || 'structure', x, y };
      }).filter(Boolean);
    }

    function nearestBuilding(x, y) {
      let best = null;
      (state.buildings || []).forEach((b) => {
        const distance = Math.hypot(x - b.x, y - b.y);
        if (!best || distance < best.distance) best = { ...b, distance };
      });
      return best;
    }

    function hizSentence(x, y, bySample) {
      const flame = bySample.flame_length ? numberOrNull(bySample.flame_length.value) : null;
      const crownClass = bySample.crown_class ? numberOrNull(bySample.crown_class.value) : null;
      const crownPotential = bySample.crown_potential ? numberOrNull(bySample.crown_potential.value) : null;
      const enoughFire = (flame != null && flame >= 1.2)
        || (crownClass != null && crownClass >= 1)
        || (crownPotential != null && crownPotential >= 1);
      if (!enoughFire) return null;
      const building = nearestBuilding(x, y);
      if (!building || building.distance > 60) return null;
      return `A structure stands within ~${Math.round(building.distance)} m \u2014 within its Home Ignition Zone; embers and radiant heat matter more than the flame front here.`;
    }

    function interpretAt(x, y, samples) {
      if (!samples?.length) return null;
      const bySample = {};
      samples.forEach((s) => {
        if (s?.layer?.id) bySample[s.layer.id] = s;
      });
      const hasScenario = samples.some((s) => s?.layer?.group === 'fire_scenario');
      const sentences = [];

      if (hasScenario) {
        if (bySample.fire_arrival) sentences.push(arrivalSentence(bySample.fire_arrival));
        if (bySample.flame_length) {
          const flame = flameSentence(bySample.flame_length, bySample.fireline_intensity);
          sentences.push(flame.sentence);
          if (bySample.fireline_intensity && !flame.usedIntensity) {
            const intensity = intensitySentence(bySample.fireline_intensity);
            if (intensity) sentences.push(intensity);
          }
        } else if (bySample.fireline_intensity) {
          const intensity = intensitySentence(bySample.fireline_intensity);
          if (intensity) sentences.push(intensity);
        }
        if (bySample.crown_class) sentences.push(crownSentence(bySample.crown_class, 'scenario'));
        if (bySample.ember_exposure) sentences.push(emberSentence(bySample.ember_exposure));
        if (bySample.torching_index) sentences.push(crownThresholdSentence(bySample.torching_index));
        if (bySample.crowning_index) sentences.push(crownThresholdSentence(bySample.crowning_index));
        if (bySample.fuel_model) sentences.push(fuelSentence(bySample.fuel_model));
        const recap = scenarioRecapSentence();
        if (recap) sentences.push(recap);
      } else {
        if (bySample.fuel_model) sentences.push(fuelSentence(bySample.fuel_model));
        const ros = rosSentence(bySample.base_ros, bySample.slope_hazard);
        if (ros) sentences.push(ros);
        if (bySample.torching_index) sentences.push(crownThresholdSentence(bySample.torching_index));
        if (bySample.crowning_index) sentences.push(crownThresholdSentence(bySample.crowning_index));
        if (bySample.crown_potential) sentences.push(crownSentence(bySample.crown_potential, 'tier1'));
      }

      const hiz = hizSentence(x, y, bySample);
      if (hiz) sentences.push(hiz);
      const clean = sentences.filter(Boolean);
      if (!clean.length) return null;

      return (
        '<p class="info-layer">Fire</p>' +
        '<p class="info-title">Fire at this spot</p>' +
        clean.map((s) => `<p class="sim-sentence">${esc(s)}</p>`).join('') +
        '<p class="readout-hint sim-note">Scenario-grade estimate: WHERE fire spreads is more reliable than exact times/lengths. Fuel is mapped at 30 m.</p>'
      );
    }

    /* ------------------------------------------------------------ boot ---- */

    async function boot() {
      const [presets, summary, last, buildings] = await Promise.all([
        fetchOptionalJson('/api/fire-presets'),
        quietFetch('/data/fire/summary.json'),
        fetchOptionalJson('/data/fire/last-fire-scenario.json'),
        quietFetch('/data/buildings/models/manifest.json'),
      ]);
      if (isRecord(presets?.data?.presets)) {
        state.presetValues = presets.data.presets;
        writeSelectedPresetWeather();
      } else if (presets?.error) {
        els.status.textContent = `Fire weather presets could not be loaded: ${presets.error}`;
      }
      state.summary = summary;
      state.buildings = buildingPlacements(buildings);
      renderPresets();
      if (last?.error) {
        els.status.textContent = `Last fire scenario could not be restored: ${last.error}`;
      } else if (last?.data) {
        try {
          renderResult(last.data, { active: false });
          const ignition = last.data.ignition;
          if (isRecord(ignition) && numberOrNull(ignition.x) != null && numberOrNull(ignition.y) != null) {
            state.ignition = {
              x: Number(ignition.x),
              y: Number(ignition.y),
              lat: numberOrNull(ignition.lat) ?? 0,
              lon: numberOrNull(ignition.lon) ?? 0,
            };
          }
          await api.refresh([]);
        } catch (err) {
          state.lastResult = null;
          els.status.textContent = `Last fire scenario could not be restored: ${err?.message || err}`;
        }
      }
      renderToggles();
      syncControls();
    }

    renderToggles();
    renderPresets();
    syncControls();
    boot().catch((err) => {
      els.status.textContent = `Fire panel could not finish loading: ${err?.message || err}`;
      renderToggles();
      syncControls();
    });
    return {
      state,
      catalog: api.catalog,
      isEnabled: api.isEnabled,
      isLoading: api.isLoading,
      setEnabled: api.setEnabled,
      refresh: api.refresh,
      renderToggles,
      interpretAt,
      pickIgnition,
      _renderResult: renderResult,
    };
  }

  global.VEILWildfire = { create };
})(typeof window !== 'undefined' ? window : globalThis);
