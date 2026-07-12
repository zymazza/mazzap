/* "Simulation" window: the viewer-side home for twin simulations.
   First resident: the hydrology system —
     - Terrain hydrology toggles: the Tier-1 derived drape layers
       (flow paths, wetness, ponding, seep candidates) that
       analyze_hydrology.py exports into data/hydrology/.
     - Water scenario runner (snowmelt or rainstorm): posts parameters to
       /api/simulate (scripts/hydro_scenario.py), shows the result numbers,
       and auto-enables the freshly written scenario drape layers. Presets
       for both modes come from the twin's 45-year Daymet climatology.
     - interpretAt(): the natural-language voice of click-to-identify for
       simulation layers — app.js hands it the sampled values and it returns
       one synthesized "Water at this spot" card (with soil context from the
       SSURGO tabular fetch and scenario context from the last run) instead
       of bare numbers.
   The window owns no pixels itself — layer data, draping and identify all
   flow through app.js via the small api object it passes in, so simulation
   layers behave exactly like atlas layers (drape, click-to-identify, key).
   Future simulations get their own panel-group here + a catalog "group". */
(function attachSimulation(global) {
  'use strict';

  const IN_PER_MM = 1 / 25.4;

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
    return n == null ? 'unknown' : `${value}%`;
  }

  function textMaybeNumber(value, fallback = 'unknown') {
    return numberOrNull(value) == null ? fallback : String(value);
  }

  function missingSimulationResultFields(result) {
    if (!isRecord(result)) return ['result'];
    const fields = [
      ['scenario.label', result.scenario?.label, (v) => v != null && String(v) !== ''],
      ['water_input.total_mm', result.water_input?.total_mm, (v) => numberOrNull(v) != null],
      ['water_input.total_m3_on_aoi', result.water_input?.total_m3_on_aoi, (v) => numberOrNull(v) != null],
      ['partition.runoff_mm_mean', result.partition?.runoff_mm_mean, (v) => numberOrNull(v) != null],
      ['partition.runoff_pct', result.partition?.runoff_pct, (v) => numberOrNull(v) != null],
      ['partition.runoff_m3', result.partition?.runoff_m3, (v) => numberOrNull(v) != null],
      ['partition.infiltration_mm_mean', result.partition?.infiltration_mm_mean, (v) => numberOrNull(v) != null],
      ['partition.infiltration_m3', result.partition?.infiltration_m3, (v) => numberOrNull(v) != null],
      ['outlet.peak_discharge_cfs_est', result.outlet?.peak_discharge_cfs_est,
        (v) => numberOrNull(v) != null || result.outlet?.below_screening_resolution === true],
      ['outlet.event_volume_m3', result.outlet?.event_volume_m3, (v) => numberOrNull(v) != null],
    ];
    return fields.filter(([, value, present]) => !present(value)).map(([path]) => path);
  }

  function simulationResultView(result) {
    const r = isRecord(result) ? result : {};
    const waterInput = isRecord(r.water_input) ? r.water_input : {};
    const partition = isRecord(r.partition) ? r.partition : {};
    const outlet = isRecord(r.outlet) ? r.outlet : {};
    const scenario = isRecord(r.scenario) ? r.scenario : {};
    const ponding = isRecord(r.ponding) ? r.ponding : {};

    const rows = [
      ['Water input', `${fmtMaybe(waterInput.total_mm)} mm · ${fmtMaybe(waterInput.total_m3_on_aoi)} m³ on the land`],
      ['Not infiltrated', `${fmtMaybe(partition.runoff_mm_mean)} mm (${pctMaybe(partition.runoff_pct)}) · ${fmtMaybe(partition.runoff_m3)} m³`],
      ['Infiltrated', `${fmtMaybe(partition.infiltration_mm_mean)} mm · ${fmtMaybe(partition.infiltration_m3)} m³`],
    ];
    if (numberOrNull(partition.root_zone_storage_mm_mean) != null) {
      rows.push(['Profile water gain', `${fmtMaybe(partition.root_zone_storage_mm_mean)} mm · ${fmtMaybe(partition.root_zone_storage_m3)} m³`]);
    }
    if (numberOrNull(partition.deep_drainage_mm_mean) != null) {
      rows.push(['Profile percolation', `${fmtMaybe(partition.deep_drainage_mm_mean)} mm · ${fmtMaybe(partition.deep_drainage_m3)} m³`]);
    }
    if (numberOrNull(partition.saturation_excess_mm_mean) != null) {
      rows.push(['Local saturation excess', `${fmtMaybe(partition.saturation_excess_mm_mean)} mm · ${fmtMaybe(partition.saturation_excess_m3)} m³ generated`]);
    }
    if (numberOrNull(partition.profile_saturation_pct_mean) != null) {
      const initial = numberOrNull(partition.initial_profile_saturation_pct_mean);
      const change = numberOrNull(partition.profile_saturation_change_pct_points_mean);
      rows.push(['Mean profile saturation',
        `${fmtMaybe(partition.profile_saturation_pct_mean, 1)}% final`
        + `${initial != null ? ` · ${fmt(initial, 1)}% antecedent` : ''}`
        + `${change != null ? ` · +${fmt(change, 1)} points` : ''}`]);
    }
    const belowResolution = outlet.below_screening_resolution === true;
    rows.push(
      ['Outlet peak', belowResolution
        ? 'Below screening resolution (<0.2% of input)'
        : `~${textMaybeNumber(outlet.peak_discharge_cfs_est)} cfs (screening)`],
      ['Outlet volume', `${fmtMaybe(outlet.event_volume_m3)} m³ over the event`
        + `${belowResolution ? ' · budget bookkeeping only' : ''}`],
    );
    const storageM3 = numberOrNull(ponding.depression_storage_m3);
    if (storageM3 != null) {
      const retainedM3 = numberOrNull(ponding.retained_water_m3);
      const fillPct = numberOrNull(ponding.storage_fill_pct);
      rows.push(['Ponds & pools', retainedM3 != null
        ? `${fmt(retainedM3)} m³ retained${fillPct != null ? ` · ${fmt(fillPct, 1)}% of ${fmt(storageM3)} m³ capacity` : ''}`
        : ponding.storage_filled
          ? `fill (${fmt(storageM3)} m³ of storage)`
          : 'partial filling']);
    }
    const missing = [];
    if (r.soil_available === false) missing.push('soil');
    if (r.climate_available === false) missing.push('climate');
    const degraded = missing.length
      ? `No ${missing.join(' or ')} data for this twin — showing terrain flow geometry`
        + `${missing.includes('soil') ? ' with a uniform woods curve number' : ''}.`
        + ' Where water concentrates is reliable; the partition is coarser.'
      : '';
    return {
      label: scenario.label != null && String(scenario.label) !== ''
        ? String(scenario.label)
        : 'Scenario result',
      rows,
      degraded,
      note: Array.isArray(r.notes) ? r.notes.slice(0, 2).filter(Boolean).join(' ') : '',
      missingFields: missingSimulationResultFields(result),
    };
  }

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

  function cellAreaM2(sample) {
    const raw = firstMetaValue(sample, ['cell_area_m2', 'cellAreaM2', 'cell_area']);
    const area = Number(raw);
    return Number.isFinite(area) && area > 0 ? area : null;
  }

  function flowSampleToHa(sample) {
    const value = Number(sample?.value);
    if (!Number.isFinite(value)) return { ha: null, reason: 'missing-value' };
    const unit = normalizeUnit(firstMetaValue(sample, [
      'flow_unit',
      'flow_units',
      'value_unit',
      'value_units',
      'units',
    ]));
    if (!unit) return { ha: null, reason: 'missing-unit' };
    if (['ha', 'hectare', 'hectares'].includes(unit)) return { ha: value, reason: null };
    if (['m2', 'sq_m', 'sqm', 'square_meter', 'square_meters'].includes(unit)) {
      return { ha: value / 10000, reason: null };
    }
    if (['cell', 'cells', 'grid_cell', 'grid_cells'].includes(unit)) {
      const area = cellAreaM2(sample);
      return area ? { ha: (value * area) / 10000, reason: null }
        : { ha: null, reason: 'missing-cell-area' };
    }
    return { ha: null, reason: 'unknown-unit' };
  }

  function flowSentenceForHa(ha, maxHa) {
    const m2 = ha * 10000;
    if (ha < 0.05) {
      return `Only local water passes here — about ${fmt(m2)} m² drains through this spot.`;
    }
    if (ha < 0.5) {
      return `A defined flow path: water from about ${fmt(m2)} m² (${ha.toFixed(2)} ha) upslope funnels through here when it runs.`;
    }
    const pct = maxHa ? Math.round((ha / maxHa) * 100) : null;
    if (ha < 2) {
      return `A significant drainage line — roughly ${ha.toFixed(1)} ha drains through this point${pct ? ` (${pct}% of the property's largest drainage)` : ''}.`;
    }
    return `A main channel: about ${ha.toFixed(1)} ha — ${pct ? `${pct}% of ` : ''}the property's biggest drainage — passes through here. Expect real flow in any melt or storm.`;
  }

  function create(api) {
    const els = {
      panel: document.getElementById('simulation-panel'),
      hydroToggles: document.getElementById('sim-hydro-toggles'),
      mode: document.getElementById('sim-mode'),
      presets: document.getElementById('sim-presets'),
      form: document.getElementById('sim-form'),
      snowFields: document.getElementById('sim-snow-fields'),
      rainFields: document.getElementById('sim-rain-fields'),
      swe: document.getElementById('sim-swe'),
      days: document.getElementById('sim-days'),
      rain: document.getElementById('sim-rain'),
      stormIn: document.getElementById('sim-storm-in'),
      stormHours: document.getElementById('sim-storm-hours'),
      antecedent: document.getElementById('sim-antecedent'),
      frozen: document.getElementById('sim-frozen'),
      run: document.getElementById('sim-run'),
      status: document.getElementById('sim-status'),
      scenarioGroup: document.getElementById('sim-scenario-group'),
      scenarioToggles: document.getElementById('sim-scenario-toggles'),
      results: document.getElementById('sim-results'),
    };
    if (!els.panel) return null;

    const state = {
      busy: false,
      mode: 'snowmelt',
      climatology: null,
      lastResult: null,   // last scenario result (restored from disk on boot)
      summary: null,      // Tier-1 summary.json (outlet, storage, candidates)
      soils: null,        // {features, tabular} for point-in-polygon soil facts
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

    function activeDataUrl(relativePath) {
      const root = global.__twin?.plan?.assetRoot?.() || '/data';
      return `${String(root).replace(/\/$/, '')}/${relativePath.replace(/^\//, '')}`;
    }

    /* ----------------------------------------------- layer toggle sections */

    function toggleRow(layer) {
      const row = document.createElement('label');
      row.className = 'toggle-row';
      const swatch = layer.swatch || (layer.group === 'scenario' ? '#f57e3c'
        : layer.group === 'water_balance' ? '#2aa198' : '#3e7cb1');
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
          if (e.target.checked && layer.group === 'scenario') {
            const otherScenarioLayers = (api.catalog()?.layers || []).filter((candidate) =>
              candidate.group === 'scenario'
              && candidate.id !== layer.id
              && api.isEnabled(candidate.id));
            for (const candidate of otherScenarioLayers) {
              await api.setEnabled(candidate, false);
            }
          }
          await api.setEnabled(layer, e.target.checked);
        } finally {
          renderToggles();
        }
      });
      return row;
    }

    function renderToggles() {
      const layers = (api.catalog()?.layers) || [];
      // Terrain hydrology only; the ET/water-balance drape moved to the ET tab (et.js).
      const hydro = layers.filter((l) => l.group === 'hydrology');
      const scenario = layers.filter((l) => l.group === 'scenario');
      if (hydro.length) {
        els.hydroToggles.replaceChildren(...hydro.map(toggleRow));
      }
      els.scenarioGroup.hidden = !scenario.length && !state.lastResult;
      els.scenarioToggles.replaceChildren(...scenario.map(toggleRow));
    }

    /* ------------------ presets from the twin's 45-year Daymet climatology */

    function presetButton(label, title, apply) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = label;
      btn.title = title;
      btn.addEventListener('click', () => {
        apply();
        els.presets.querySelectorAll('button')
          .forEach((b) => b.classList.toggle('active', b === btn));
      });
      return btn;
    }

    function renderPresets() {
      const c = state.climatology;
      els.presets.replaceChildren();
      if (!c) return;
      const yrs = `from ${c.n_full_water_years || '~45'} water years of Daymet at this twin`;
      const toIn = (mm) => (mm == null ? null : mm * IN_PER_MM);
      if (state.mode === 'snowmelt') {
        [['Median winter', toIn(c.peak_swe_kg_m2_median)],
         ['Big winter (p90)', toIn(c.peak_swe_kg_m2_p90)],
         ['Record snowpack', toIn(c.peak_swe_kg_m2_max)]]
          .filter(([, v]) => v != null)
          .forEach(([label, swe]) => {
            els.presets.appendChild(presetButton(
              `${label} · ${swe.toFixed(1)}″`, `Peak snow-water-equivalent ${yrs}`,
              () => { els.swe.value = swe.toFixed(1); }));
          });
      } else {
        const storms = [
          ['Median annual storm', toIn(c.storm_1day_mm_median), 24],
          ['Big storm (p90)', toIn(c.storm_1day_mm_p90), 12],
          ['Record day of rain', toIn(c.storm_1day_mm_max), 24],
          ['Record 3-day soaker', toIn(c.storm_3day_mm_max), 72],
        ].filter(([, v]) => v != null);
        storms.forEach(([label, inches, hours]) => {
          els.presets.appendChild(presetButton(
            `${label} · ${inches.toFixed(1)}″`,
            `Annual-maximum precipitation series ${yrs}`,
            () => { els.stormIn.value = inches.toFixed(1); els.stormHours.value = hours; }));
        });
      }
    }

    function setMode(mode) {
      state.mode = mode;
      els.snowFields.hidden = mode !== 'snowmelt';
      els.rainFields.hidden = mode !== 'rain';
      els.mode.querySelectorAll('button').forEach((b) =>
        b.classList.toggle('active', b.dataset.mode === mode));
      renderPresets();
    }

    els.mode.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-mode]');
      if (btn) setMode(btn.dataset.mode);
    });

    /* --------------------------------------------- scenario run + results */

    function renderResult(r) {
      state.lastResult = isRecord(r) ? r : {};
      const view = simulationResultView(r);
      els.results.innerHTML =
        `<p class="sim-scenario-label">${esc(view.label)}</p>` +
        (view.degraded ? `<p class="readout-hint sim-degraded">${esc(view.degraded)}</p>` : '') +
        view.rows.map(([k, v]) =>
          `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${esc(v)}</span></div>`).join('') +
        `<p class="readout-hint sim-note">${esc(view.note)}</p>`;
      els.scenarioGroup.hidden = false;
      return view;
    }

    function buildParams() {
      if (state.mode === 'rain') {
        return {
          mode: 'rain',
          rain_in: parseFloat(els.stormIn.value) || 0,
          storm_hours: parseFloat(els.stormHours.value) || 12,
          antecedent: els.antecedent.value,
          frozen: els.frozen.checked,
        };
      }
      return {
        mode: 'snowmelt',
        swe_in: parseFloat(els.swe.value),
        melt_days: parseFloat(els.days.value),
        rain_in: parseFloat(els.rain.value) || 0,
        antecedent: els.antecedent.value,
        frozen: els.frozen.checked,
      };
    }

    els.form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (state.busy) return;
      state.busy = true;
      els.run.disabled = true;
      els.status.textContent = state.mode === 'rain'
        ? 'Simulating the storm…' : 'Simulating snowmelt…';
      try {
        const params = buildParams();
        const plan = global.__twin?.plan;
        let data;
        if (plan?.activeRevisionId?.()) {
          data = await plan.runSimulation('hydrology', params);
        } else {
          const res = await fetch('/api/simulate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params),
          });
          data = await res.json();
        }
        if (!data || typeof data !== 'object') throw new Error('Scenario returned an empty result.');
        if (data.error) throw new Error(data.error);
        await api.refresh(data.default_visible_layers || data.layers || []);
        if (plan?.activeRevisionId?.()) await reloadPlanContext();
        const view = renderResult(data);
        els.status.textContent = view.missingFields.length
          ? 'Scenario result is incomplete; showing available values.'
          : '';
        renderToggles();
      } catch (err) {
        els.status.textContent = 'Scenario failed: ' + (err?.message || err);
      } finally {
        state.busy = false;
        els.run.disabled = false;
      }
    });

    /* --------------- natural-language identify (called from app.js) -------
       samples: [{layer, grid, value}] for every enabled simulation layer at the
       clicked point. Returns one card's inner HTML, or null. */

    function pointInRings(rings, x, y) {
      let inside = false;
      rings.forEach((ring) => {
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
          const [xi, yi] = ring[i];
          const [xj, yj] = ring[j];
          if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
            inside = !inside;
          }
        }
      });
      return inside;
    }

    function soilAt(x, y) {
      if (!state.soils?.features) return null;
      for (const f of state.soils.features) {
        const g = f.geometry || {};
        const polys = g.type === 'Polygon' ? [g.coordinates]
          : g.type === 'MultiPolygon' ? g.coordinates : [];
        if (polys.some((rings) => pointInRings(rings, x, y))) {
          const mukey = String(f.properties?.mukey || '');
          return { mukey, ...(state.soils.tabular?.[mukey] || {}), props: f.properties };
        }
      }
      return null;
    }

    function flowSentence(sample) {
      const { ha } = flowSampleToHa(sample);
      if (ha == null) {
        return 'Flow-path accumulation is available here, but its unit metadata is missing or unknown, so the drainage area is not displayed.';
      }
      return flowSentenceForHa(ha, state.summary?.max_contributing_ha);
    }

    function wetnessSentence(pctile) {
      if (pctile >= 90) return `Among the wettest ground on the property (wetter than ${Math.round(pctile)}% of it) — expect soft, saturated soil much of the year.`;
      if (pctile >= 70) return `Wetter than ${Math.round(pctile)}% of the property — likely damp after rain and in spring.`;
      if (pctile >= 30) return `Middling wetness for this land (${Math.round(pctile)}th percentile).`;
      return `Dry ground by this property's standards (${Math.round(pctile)}th percentile) — water sheds away rather than collecting.`;
    }

    function pondingSentence(depthM) {
      const cm = depthM * 100;
      if (cm < 8) return `A shallow pool forms here (~${Math.round(cm)} cm) before water finds its way out.`;
      return `Water pools here up to ~${cm < 100 ? Math.round(cm) + ' cm' : (depthM).toFixed(1) + ' m'} deep before spilling — a real depression in the LiDAR surface.`;
    }

    function seepSentence(score, soil) {
      const geo = soil?.depth_to_bedrock_min_cm
        ? `bedrock as shallow as ${Math.round(soil.depth_to_bedrock_min_cm)} cm here`
        : soil?.water_table_depth_annual_min_cm != null
          ? `a seasonal water table at ~${Math.round(soil.water_table_depth_annual_min_cm)} cm`
          : 'the soil profile';
      if (score >= 75) return `Strong spring/seep candidate (${Math.round(score)}/100): converging water, a slope break, and ${geo} all line up. Worth a field check.`;
      if (score >= 60) return `Moderate spring/seep candidate (${Math.round(score)}/100) — conditions partly favor groundwater surfacing near here.`;
      if (score >= 45) return `Weak seep signal (${Math.round(score)}/100); damp ground is plausible, a flowing spring unlikely.`;
      return `Little to suggest a spring here (score ${Math.round(score)}/100).`;
    }

    function soilSentence(soil) {
      if (!soil?.muname) return null;
      const bits = [];
      if (soil.hydrologic_group) bits.push(`hydrologic group ${soil.hydrologic_group}`);
      if (soil.drainage_class) bits.push(soil.drainage_class.toLowerCase());
      if (soil.depth_to_bedrock_min_cm) bits.push(`bedrock from ${Math.round(soil.depth_to_bedrock_min_cm)} cm`);
      else if (soil.water_table_depth_annual_min_cm != null) bits.push(`water table from ${Math.round(soil.water_table_depth_annual_min_cm)} cm`);
      if (soil.surface_ksat_mm_hr) bits.push(`soaks ~${Math.round(soil.surface_ksat_mm_hr)} mm/hr at the surface`);
      return `Soil: ${String(soil.muname).replace(/"/g, '')}${bits.length ? ` — ${bits.join(', ')}` : ''}.`;
    }

    function scenarioSentences(byId) {
      const r = state.lastResult;
      const label = r?.scenario?.label
        || (api.catalog()?.layers || []).find((l) => l.scenario)?.scenario;
      const parts = [];
      const absorbed = numberOrNull(byId.scenario_infiltration);
      const stored = numberOrNull(byId.scenario_soil_storage);
      const drained = numberOrNull(byId.scenario_deep_drainage);
      const saturationExcess = numberOrNull(byId.scenario_saturation_excess);
      const saturationPct = numberOrNull(byId.scenario_saturation);
      const runon = numberOrNull(byId.scenario_runon);
      const ponded = numberOrNull(byId.scenario_ponded_water);
      if (byId.scenario_runoff != null && r) {
        const total = numberOrNull(r?.water_input?.total_mm);
        const ro = numberOrNull(byId.scenario_runoff);
        if (ro != null && total != null) {
          const pct = total ? Math.round((ro / total) * 100) : null;
          parts.push(`~${Math.round(ro)} mm of the local ${Math.round(total)} mm input stays on the surface here${pct != null ? ` (${pct}% locally unabsorbed)` : ''} before routing`);
        } else if (ro != null) {
          parts.push(`~${Math.round(ro)} mm of local input stays on the surface here before routing`);
        }
      } else if (byId.scenario_runoff != null) {
        const ro = numberOrNull(byId.scenario_runoff);
        if (ro != null) parts.push(`~${Math.round(ro)} mm of local input stays on the surface here before routing`);
      }
      if (absorbed != null) {
        const total = numberOrNull(r?.water_input?.total_mm);
        parts.push(`~${Math.round(absorbed)} mm enters the soil${total != null && absorbed > total + 0.5 ? ', including upstream runon' : ''}`);
      }
      if (stored != null) {
        parts.push(`~${Math.round(stored)} mm remains as modeled profile water gain`);
      }
      if (drained != null) {
        parts.push(`~${Math.round(drained)} mm percolates below the modeled profile`);
      }
      if (saturationExcess != null) {
        parts.push(saturationExcess >= 0.05
          ? `~${Math.round(saturationExcess)} mm of local input becomes saturation excess here and may re-infiltrate downslope`
          : 'no local saturation excess is generated here');
      }
      if (saturationPct != null) {
        parts.push(`the modeled profile ends ~${Math.round(saturationPct)}% water-filled`);
      }
      if (runon != null) {
        parts.push(runon >= 0.05
          ? `~${fmt(runon, runon < 10 ? 1 : 0)} m³ of upstream surface water arrives here before infiltration`
          : 'almost no upstream surface runon arrives here');
      }
      if (ponded != null && ponded >= 0.05) {
        parts.push(`~${fmt(ponded, ponded < 10 ? 1 : 0)} mm remains ponded at event end`);
      }
      if (byId.scenario_flow != null) {
        const v = numberOrNull(byId.scenario_flow);
        if (v == null) return parts.length
          ? `In the simulated ${label ? `“${label}”` : 'scenario'}: ${parts.join('; ')}.`
          : null;
        const outlet = numberOrNull(r?.outlet?.event_volume_m3);
        const pct = outlet ? Math.round((v / outlet) * 100) : null;
        if (v >= 1) {
          parts.push(`about ${fmt(v)} m³ of surface water leaves this cell over the event${pct ? ` — ${pct}% of the combined outlet volume` : ''}`);
        } else {
          parts.push('almost no surface throughflow leaves this exact spot');
        }
      }
      if (!parts.length) return null;
      return `In the simulated ${label ? `“${label}”` : 'scenario'}: ${parts.join('; ')}.`;
    }

    function interpretAt(x, y, samples) {
      if (!samples.length) return null;
      const byId = {};
      const bySample = {};
      samples.forEach((s) => {
        byId[s.layer.id] = s.value;
        bySample[s.layer.id] = s;
      });
      const soil = soilAt(x, y);

      const sentences = [];
      if (byId.flow_paths != null) sentences.push(flowSentence(bySample.flow_paths));
      if (byId.wetness_index != null) sentences.push(wetnessSentence(byId.wetness_index));
      if (byId.ponding != null) sentences.push(pondingSentence(byId.ponding));
      if (byId.seep_candidates != null) sentences.push(seepSentence(byId.seep_candidates, soil));
      const scen = scenarioSentences(byId);
      if (scen) sentences.push(scen);
      const soilLine = soilSentence(soil);
      if (soilLine) sentences.push(soilLine);
      if (!sentences.length) return null;

      return (
        `<p class="info-layer">Simulation</p>` +
        `<p class="info-title">Water at this spot</p>` +
        sentences.map((s) => `<p class="sim-sentence">${esc(s)}</p>`).join('')
      );
    }

    /* ------------------------------------------------------------ boot ---- */

    async function reloadPlanContext() {
      const [clim, summary, last, soilFeats, soilTab] = await Promise.all([
        quietFetch(activeDataUrl('climate/forcing-summary.json')),
        quietFetch(activeDataUrl('hydrology/summary.json')),
        fetchOptionalJson(activeDataUrl('hydrology/last-scenario.json')),
        quietFetch(activeDataUrl('soils/features.geojson')),
        quietFetch(activeDataUrl('soils/tabular.json')),
      ]);
      state.climatology = clim;
      state.summary = summary;
      state.lastResult = null;
      els.results.replaceChildren();
      els.scenarioGroup.hidden = true;
      els.status.textContent = '';
      state.soils = { features: soilFeats?.features || [], tabular: soilTab?.map_units || {} };
      renderPresets();
      if (last?.error) {
        els.status.textContent = `Last scenario could not be restored: ${last.error}`;
      } else if (last?.data) {
        try {
          const view = renderResult(last.data);
          if (view.missingFields.length) {
            els.status.textContent = 'Last scenario is incomplete; showing available values.';
          }
        } catch (err) {
          state.lastResult = null;
          els.status.textContent = `Last scenario could not be restored: ${err?.message || err}`;
        }
      }
      renderToggles();
    }

    renderToggles();
    reloadPlanContext().catch((err) => {
      els.status.textContent = `Simulation panel could not finish loading: ${err?.message || err}`;
      renderToggles();
    });
    return { state, renderToggles, interpretAt, reloadPlanContext,
      _renderResult: renderResult };
  }

  global.VEILSimulation = {
    create,
    _test: global.__VEIL_SIMULATION_TEST__ ? {
      flowSentenceForHa,
      flowSampleToHa,
      normalizeUnit,
      missingSimulationResultFields,
      simulationResultView,
    } : undefined,
  };
})(typeof window !== 'undefined' ? window : globalThis);
