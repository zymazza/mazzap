/* "Water & ET" tab of the Simulation window: the viewer-side readout for the
   twin's evapotranspiration pipeline —
     - Reference ET0 ensemble (derive_et0_daily.py -> data/et/et0-summary.json):
       the four FAO-56 methods as annual mm, their ensemble mean + spread, and a
       12-month climatology of atmospheric water demand. Humidity/wind
       provenance is shown so the reduced-data caveats are visible.
     - Water balance (et_water_balance.py -> data/et/summary.json): per-year
       precip / reference ET0 / actual ET / AET-over-precip with Budyko position
       / climatic deficit / recharge, plus the current root-zone moisture state.
     - The "Annual actual ET" drape rides here (catalog group "water_balance"),
       toggled and click-identified through the same app.js api as every layer.
   Like simulation.js this window owns no pixels: draping + identify flow through
   app.js. Everything degrades gracefully when the ET pipeline has not run — the
   summary groups stay hidden and only the build hint shows. */
(function attachET(global) {
  'use strict';

  const DAYS_PER_YEAR = 365.25;
  const WB_SWATCH = '#2aa198';          // teal — matches simulation.js water_balance
  const SCENARIO_SWATCH = '#34a7c1';    // brighter cyan — simulated daily ET
  const MONTHS = ['J', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'];
  // FAO-56 reference-ET methods, best-supported first (drives display order).
  const METHOD_ORDER = [
    'fao56_pm_reduced_mm',
    'priestley_taylor_mm',
    'hargreaves_samani_mm',
    'oudin_mm',
  ];
  const METHOD_LABELS = {   // full names — tooltip
    fao56_pm_reduced_mm: 'FAO-56 Penman-Monteith',
    priestley_taylor_mm: 'Priestley-Taylor',
    hargreaves_samani_mm: 'Hargreaves-Samani',
    oudin_mm: 'Oudin (temperature)',
  };
  const METHOD_SHORT = {    // compact names — the row label
    fao56_pm_reduced_mm: 'FAO-56 PM',
    priestley_taylor_mm: 'Priestley-Taylor',
    hargreaves_samani_mm: 'Hargreaves-Samani',
    oudin_mm: 'Oudin',
  };
  const LIMIT_LABELS = {
    water: 'water-limited',
    energy: 'energy-limited',
    snow: 'snow',
  };
  const ET_PRESETS = [
    {
      label: 'Hot clear summer day',
      values: { date: '2024-07-15', tmax: 32, tmin: 16, sky: 'clear', rh: 25, wind: 4, rain: 0, soil: 'current', days: 1 },
    },
    {
      label: 'Cool cloudy day',
      values: { date: '2024-05-15', tmax: 16, tmin: 6, sky: 'cloudy', rh: 75, wind: 1.5, rain: 0, soil: 'current', days: 1 },
    },
    {
      label: 'Warm day after rain',
      values: { date: '2024-08-10', tmax: 26, tmin: 14, sky: 'partly', rh: 65, wind: 2, rain: 6, soil: 'wet', days: 3 },
    },
    {
      label: 'Cold winter day',
      values: { date: '2024-01-15', tmax: 0, tmin: -8, sky: 'overcast', rh: 85, wind: 2, rain: 12, soil: 'current', days: 1 },
    },
  ];

  function fmt(n, digits = 0) {
    return Number(n).toLocaleString(undefined, {
      maximumFractionDigits: digits, minimumFractionDigits: digits,
    });
  }
  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function fmtMaybe(value, digits = 0, fallback = 'unknown') {
    const n = num(value);
    return n == null ? fallback : fmt(n, digits);
  }

  function create(api) {
    const els = {
      panel: document.getElementById('et-panel'),
      layerToggles: document.getElementById('et-layer-toggles'),
      vaporRow: document.getElementById('et-vapor-row'),
      vaporToggle: document.getElementById('et-vapor-toggle'),
      et0Group: document.getElementById('et-et0-group'),
      et0Methods: document.getElementById('et-et0-methods'),
      et0Ensemble: document.getElementById('et-et0-ensemble'),
      clim: document.getElementById('et-clim'),
      et0Note: document.getElementById('et-et0-note'),
      wbGroup: document.getElementById('et-wb-group'),
      year: document.getElementById('et-year'),
      wbResults: document.getElementById('et-wb-results'),
      antGroup: document.getElementById('et-antecedent-group'),
      antecedent: document.getElementById('et-antecedent'),
      scenarioGroup: document.getElementById('et-scenario-group'),
      scenarioPresets: document.getElementById('et-scenario-presets'),
      scenarioForm: document.getElementById('et-scenario-form'),
      scenarioDate: document.getElementById('et-scenario-date'),
      scenarioTmax: document.getElementById('et-scenario-tmax'),
      scenarioTmin: document.getElementById('et-scenario-tmin'),
      scenarioSky: document.getElementById('et-scenario-sky'),
      scenarioRh: document.getElementById('et-scenario-rh'),
      scenarioWind: document.getElementById('et-scenario-wind'),
      scenarioRain: document.getElementById('et-scenario-rain'),
      scenarioSoil: document.getElementById('et-scenario-soil'),
      scenarioDays: document.getElementById('et-scenario-days'),
      scenarioRun: document.getElementById('et-scenario-run'),
      scenarioStatus: document.getElementById('et-scenario-status'),
      scenarioResults: document.getElementById('et-scenario-results'),
      status: document.getElementById('et-status'),
    };
    if (!els.panel) return null;

    const state = {
      et0: null,        // data/et/et0-summary.json
      wb: null,         // data/et/summary.json
      aetMeanMm: null,  // twin-mean annual AET, for click-identify context
      scenarioBusy: false,
      lastScenario: null,
      vaporVisible: false,
      vaporLoading: false,
    };

    const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    function setPanelStatus(text, kind = '') {
      els.status.textContent = text || '';
      els.status.classList.remove('ok', 'warn', 'err');
      if (kind) els.status.classList.add(kind);
    }

    function climatologicalDailyAetMm() {
      const annual = num(state.aetMeanMm);
      return annual == null ? null : annual / DAYS_PER_YEAR;
    }

    function setVaporIntensity(mmPerDay) {
      window.__twin?.vapor?.setIntensity(mmPerDay);
    }

    async function quietFetch(url) {
      try {
        const r = await fetch(url);
        return r.ok ? await r.json() : null;
      } catch (_e) { return null; }
    }

    async function fetchOptionalJson(url) {
      try {
        const r = await fetch(url);
        if (!r.ok) return { data: null, error: r.status === 404 ? null : `HTTP ${r.status}` };
        return { data: await r.json(), error: null };
      } catch (err) {
        return { data: null, error: err?.message || String(err || 'load failed') };
      }
    }

    /* ------------- layer toggle (the "Annual actual ET" drape) ------------- */

    function toggleRow(layer) {
      const row = document.createElement('label');
      row.className = 'toggle-row';
      const loading = !!api.isLoading?.(layer.id);
      row.classList.toggle('loading', loading);
      row.innerHTML =
        `<input type="checkbox" ${api.isEnabled(layer.id) ? 'checked' : ''} ${loading ? 'disabled' : ''} />` +
        `<span class="swatch" style="background:${layer.group === 'et_scenario' ? SCENARIO_SWATCH : WB_SWATCH}"></span>` +
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

    function findAetLayer() {
      const layers = (api.catalog()?.layers) || [];
      return layers.find((l) => l.id === 'scenario_aet') ||
        layers.find((l) => l.id === 'aet_annual') ||
        layers.find((l) => l.group === 'water_balance');
    }

    async function ensureVaporField() {
      if (!window.__twin?.vapor) {
        throw new Error('vapor renderer is not available.');
      }
      const layer = findAetLayer();
      if (!layer) {
        throw new Error('Annual actual ET grid is not available for this twin.');
      }
      const data = await api.ensureData?.(layer.id);
      window.__twin?.syncVaporField?.();
      if (!data?.grid?.values) {
        throw new Error('Annual actual ET grid could not be loaded.');
      }
      return data;
    }

    function updateVaporRow(hasEtLayers) {
      if (!els.vaporRow || !els.vaporToggle) return;
      els.vaporRow.hidden = !hasEtLayers;
      els.vaporRow.title = 'Rising water vapor scaled by the Annual actual ET grid and the latest ET scenario.';
      els.vaporRow.classList.toggle('loading', state.vaporLoading);
      els.vaporToggle.checked = state.vaporVisible;
      els.vaporToggle.disabled = state.vaporLoading || !window.__twin?.vapor;
    }

    function renderToggles() {
      const layers = (api.catalog()?.layers) || [];
      const et = layers.filter((l) => l.group === 'water_balance' || l.group === 'et_scenario');
      if (!et.length) {
        const hint = document.createElement('p');
        hint.className = 'hint';
        hint.innerHTML = 'Run <code>npm run derive-et0</code> then <code>npm run et-water-balance</code> to build the ET layers.';
        updateVaporRow(false);
        els.layerToggles.replaceChildren(hint, ...(els.vaporRow ? [els.vaporRow] : []));
        return;
      }
      updateVaporRow(true);
      els.layerToggles.replaceChildren(...et.map(toggleRow), ...(els.vaporRow ? [els.vaporRow] : []));
    }

    /* ----------------------- reference ET0 ensemble ----------------------- */

    function renderEt0() {
      const s = state.et0;
      const o = s?.overall_means_mm_day;
      const methods = o ? METHOD_ORDER.filter((k) => num(o[k]) != null) : [];
      if (!methods.length) { els.et0Group.hidden = true; return; }

      const annual = (k) => num(o[k]) * DAYS_PER_YEAR;   // mm/day mean -> mm/yr
      const vals = methods.map(annual);
      const maxV = Math.max(...vals);
      els.et0Methods.innerHTML = methods.map((k) => {
        const mm = annual(k);
        const pct = maxV ? Math.round((mm / maxV) * 100) : 0;
        const full = METHOD_LABELS[k] || k;
        const short = METHOD_SHORT[k] || full;
        return `<div class="et-method" title="${esc(full)}: ${fmt(mm)} mm/yr (${fmt(o[k], 2)} mm/day mean)">` +
          `<span class="et-method-name">${esc(short)}</span>` +
          `<span class="et-method-bar"><span style="width:${pct}%"></span></span>` +
          `<span class="et-method-val">${fmt(mm)}</span></div>`;
      }).join('');

      const mean = num(o.method_mean_mm) != null ? num(o.method_mean_mm) * DAYS_PER_YEAR : null;
      const lo = Math.min(...vals), hi = Math.max(...vals);
      els.et0Ensemble.innerHTML = mean != null
        ? `Ensemble <strong>${fmt(mean)} mm/yr</strong> &middot; methods span ${fmt(lo)}&ndash;${fmt(hi)} mm/yr`
        : `Methods span ${fmt(lo)}&ndash;${fmt(hi)} mm/yr`;

      renderClimatology(s);

      const hum = Array.isArray(s.humidity_provenance) && s.humidity_provenance.length
        ? s.humidity_provenance.join(', ') : 'estimated from Tmin';
      const wp = s.wind_provenance || {};
      const wind = wp.wind_assumed === false
        ? `real wind (${wp.u2_source || 'gridMET'}${num(wp.u2_annual_mean_m_s) != null ? `, ${fmt(wp.u2_annual_mean_m_s, 1)} m/s` : ''})`
        : `assumed wind (u2 = ${fmt(num(wp.u2_m_s) != null ? wp.u2_m_s : 2, 1)} m/s)`;
      els.et0Note.textContent =
        `${s.records ? fmt(s.records) + ' days · ' : ''}humidity: ${hum} · ${wind}. ` +
        'Reduced-data reference ET₀ — read the method spread as uncertainty, not flux-tower ET.';
      els.et0Group.hidden = false;
    }

    // 12-month climatology of the ensemble-mean daily demand, averaged over all
    // years in the record — the seasonal shape of atmospheric water demand.
    function renderClimatology(s) {
      const monthly = s.monthly_means_mm_day || {};
      const sums = Array(12).fill(0);
      const counts = Array(12).fill(0);
      Object.entries(monthly).forEach(([key, rec]) => {
        const m = parseInt(String(key).slice(5, 7), 10);   // "YYYY-MM"
        const v = num(rec?.method_mean_mm);
        if (m >= 1 && m <= 12 && v != null) { sums[m - 1] += v; counts[m - 1] += 1; }
      });
      const means = sums.map((sm, i) => (counts[i] ? sm / counts[i] : null));
      const valid = means.filter((v) => v != null);
      if (!valid.length) { els.clim.hidden = true; return; }
      const maxM = Math.max(...valid);
      els.clim.innerHTML =
        `<p class="et-clim-label">Monthly demand (ensemble mean, mm/day)</p>` +
        `<div class="et-clim-bars">` +
        means.map((v, i) => {
          const h = v != null && maxM ? Math.max(2, Math.round((v / maxM) * 100)) : 0;
          const t = v != null ? `${MONTHS[i]}: ${fmt(v, 2)} mm/day` : `${MONTHS[i]}: n/a`;
          return `<span class="et-clim-bar" title="${esc(t)}">` +
            `<span style="height:${h}%"></span><em>${MONTHS[i]}</em></span>`;
        }).join('') +
        `</div>`;
      els.clim.hidden = false;
    }

    /* --------------------------- water balance ---------------------------- */

    // Climatological mean across every full year — the default, more robust
    // than any single year for a reduced-data product.
    function averageAnnual(annual) {
      const rows = Object.values(annual).filter((r) => r && typeof r === 'object');
      if (!rows.length) return null;
      const keys = ['precip_mm', 'et0_mm', 'aet_mm', 'deficit_mm',
        'recharge_residual_mm', 'modeled_runoff_mm', 'aet_over_p',
        'budyko_aridity_index', 'budyko_expected_aet_over_p'];
      const avg = {};
      keys.forEach((k) => {
        const vs = rows.map((r) => num(r[k])).filter((v) => v != null);
        avg[k] = vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
      });
      // Derive the Budyko position from the averaged ratios (same wording as a
      // single year) rather than carrying a per-year label through the mean.
      const a = avg.aet_over_p, e = avg.budyko_expected_aet_over_p;
      avg.budyko_position = (a != null && e != null)
        ? (a < e - 0.02 ? 'below_expected' : a > e + 0.02 ? 'above_expected' : 'near_expected')
        : null;
      return avg;
    }

    function renderWaterBalance() {
      const s = state.wb;
      if (!s?.annual || !Object.keys(s.annual).length) { els.wbGroup.hidden = true; return; }
      const sel = els.year.value || '__mean__';
      const r = sel === '__mean__' ? averageAnnual(s.annual) : s.annual[sel];
      if (!r) { els.wbResults.innerHTML = ''; els.wbGroup.hidden = false; return; }

      const rows = [];
      const p = num(r.precip_mm), et0 = num(r.et0_mm), aet = num(r.aet_mm);
      if (p != null) rows.push(['Precipitation', `${fmt(p)} mm`]);
      if (et0 != null) {
        // name the single method that drove this balance (distinct from the
        // top section's method ensemble) so the number is traceable.
        const driver = METHOD_SHORT[s.et0_method];
        rows.push(['Reference ET₀', `${fmt(et0)} mm${driver ? ` <span class="et-pm">${esc(driver)}</span>` : ''}`]);
      }
      if (aet != null) rows.push(['Actual ET', `${fmt(aet)} mm <span class="et-pm">±20–35%</span>`]);
      const aop = num(r.aet_over_p);
      if (aop != null) {
        const be = num(r.budyko_expected_aet_over_p);
        const pos = r.budyko_position ? String(r.budyko_position).replace(/_/g, ' ') : '';
        rows.push(['Actual ET / precip',
          `${aop.toFixed(2)}${be != null ? ` &middot; Budyko ${esc(pos)} (${be.toFixed(2)})` : ''}`]);
      }
      const def = num(r.deficit_mm);
      if (def != null) rows.push(['Climatic deficit', `${fmt(def)} mm unmet demand`]);
      const rech = num(r.recharge_residual_mm), ro = num(r.modeled_runoff_mm);
      if (rech != null) {
        rows.push(['Recharge + runoff',
          `${fmt(rech + (ro || 0))} mm leaves as deep percolation${ro ? ' + runoff' : ''}`]);
      }
      els.wbResults.innerHTML =
        rows.map(([k, v]) => `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${v}</span></div>`).join('') +
        `<p class="sim-note readout-hint">${esc(s.uncertainty_note || '')}</p>`;
      els.wbGroup.hidden = false;
    }

    /* ------------------------- root-zone moisture ------------------------- */

    function renderAntecedent() {
      const a = state.wb?.latest_antecedent;
      if (!a) { els.antGroup.hidden = true; return; }
      const rows = [];
      if (a.date) rows.push(['As of', esc(String(a.date))]);
      const depl = num(a.root_zone_depletion_fraction);
      if (depl != null) {
        const pct = Math.round(depl * 100);
        const label = pct <= 5 ? 'essentially full'
          : pct >= 50 ? 'drawn down past the stress point'
            : `${100 - pct}% of plant-available water remains`;
        rows.push(['Root-zone moisture', `${label} (${pct}% depleted)`]);
      }
      const ks = num(a.Ks);
      if (ks != null) {
        rows.push(['Plant water stress',
          ks >= 0.99 ? 'none (Ks 1.00)' : `Ks ${ks.toFixed(2)} — transpiration limited`]);
      }
      const w30 = num(a.wetness_30d), w5 = num(a.wetness_5d);
      if (w30 != null) {
        rows.push(['Recent wetness',
          `last 30 d ${(w30 * 100).toFixed(0)}%${w5 != null ? ` &middot; last 5 d ${(w5 * 100).toFixed(0)}%` : ''} of a wet reference`]);
      }
      els.antecedent.innerHTML =
        rows.map(([k, v]) => `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${v}</span></div>`).join('') +
        `<p class="sim-note readout-hint">The soil-water state on the last record — the starting wetness for water-balance reasoning.</p>`;
      els.antGroup.hidden = false;
    }

    /* ---------------------------- ET scenario ----------------------------- */

    function fieldValue(el, fallback = null) {
      const n = num(el?.value);
      return n == null ? fallback : n;
    }

    function setScenarioStatus(text, kind = '') {
      els.scenarioStatus.textContent = text || '';
      els.scenarioStatus.classList.remove('ok', 'warn', 'err');
      if (kind) els.scenarioStatus.classList.add(kind);
    }

    function applyScenarioValues(values) {
      if (!values) return;
      if (values.date) els.scenarioDate.value = values.date;
      if (values.tmax != null) els.scenarioTmax.value = values.tmax;
      if (values.tmin != null) els.scenarioTmin.value = values.tmin;
      if (values.sky) els.scenarioSky.value = values.sky;
      if (values.rh != null) els.scenarioRh.value = values.rh;
      if (values.wind != null) els.scenarioWind.value = values.wind;
      if (values.rain != null) els.scenarioRain.value = values.rain;
      if (values.soil) els.scenarioSoil.value = values.soil;
      if (values.days != null) els.scenarioDays.value = values.days;
    }

    function applyScenarioResultToForm(result) {
      const s = result?.scenario || {};
      const w = s.weather || {};
      applyScenarioValues({
        date: s.date,
        tmax: w.tmax_c,
        tmin: w.tmin_c,
        sky: w.sky,
        rh: w.rh_pct,
        wind: w.wind_m_s,
        rain: w.rain_mm,
        soil: s.soil_state,
        days: s.days,
      });
    }

    function scenarioPresetButton(preset) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = preset.label;
      btn.addEventListener('click', () => {
        applyScenarioValues(preset.values);
        els.scenarioPresets.querySelectorAll('button')
          .forEach((b) => b.classList.toggle('active', b === btn));
      });
      return btn;
    }

    function renderScenarioPresets() {
      els.scenarioPresets.replaceChildren(...ET_PRESETS.map(scenarioPresetButton));
    }

    function buildScenarioParams() {
      return {
        date: els.scenarioDate.value || '2024-07-15',
        tmax_c: fieldValue(els.scenarioTmax, 30),
        tmin_c: fieldValue(els.scenarioTmin, 15),
        sky: els.scenarioSky.value || 'clear',
        rh_pct: fieldValue(els.scenarioRh, 45),
        wind_m_s: fieldValue(els.scenarioWind, 2),
        rain_mm: fieldValue(els.scenarioRain, 0),
        soil_state: els.scenarioSoil.value || 'current',
        days: fieldValue(els.scenarioDays, 1),
      };
    }

    function renderScenarioResult(result) {
      const r = result && typeof result === 'object' ? result : {};
      state.lastScenario = r;
      const scenario = r.scenario || {};
      const aet = r.aet || {};
      const et0 = r.et0 || {};
      const dec = r.decomposition || {};
      const seed = r.seed_state || {};
      const end = r.end_state || {};
      const range = Array.isArray(et0.range_mm) ? et0.range_mm : [];
      const label = scenario.label || 'ET scenario';
      const rows = [
        ['Reference ET₀', `${fmtMaybe(et0.pm_mm, 1)} mm PM · ensemble ${fmtMaybe(et0.ensemble_mean_mm, 1)} mm · range ${fmtMaybe(range[0], 1)}–${fmtMaybe(range[1], 1)} mm`],
        ['Dual Kc terms', `Ks ${fmtMaybe(dec.Ks, 2)} · Kcb ${fmtMaybe(dec.Kcb, 2)} · Ke ${fmtMaybe(dec.Ke, 2)}`],
        ['Flux split', `${fmtMaybe(dec.transpiration_mm, 1)} mm transpiration · ${fmtMaybe(dec.soil_evap_mm, 1)} mm soil evaporation · ${fmtMaybe(dec.interception_mm, 1)} mm interception`],
        ['Limiting factor', LIMIT_LABELS[r.limiting_factor] || String(r.limiting_factor || 'unknown')],
        ['Seed soil', `${esc(seed.source || 'unknown')} · Dr ${fmtMaybe(seed.Dr_mm, 1)} mm (${fmtMaybe(seed.depletion_pct, 0)}% depleted) · TAW ${fmtMaybe(seed.TAW_mm, 1)} mm`],
      ];
      if (scenario.days > 1) {
        rows.push(['End soil', `Dr ${fmtMaybe(end.Dr_mm, 1)} mm (${fmtMaybe(end.depletion_pct, 0)}% depleted)`]);
      }
      const series = Array.isArray(r.series) ? r.series : [];
      const seriesHtml = scenario.days > 1 && series.length
        ? `<div class="info-row"><span class="info-k">Drydown</span><span class="info-v et-series">` +
          series.map((d) => `Day ${fmtMaybe(d.day)}: ${fmtMaybe(d.aet_mm, 1)} mm, Ks ${fmtMaybe(d.Ks, 2)}, Dr ${fmtMaybe(d.Dr_mm, 1)} mm`).map(esc).join('<br>') +
          `</span></div>`
        : '';
      els.scenarioResults.innerHTML =
        `<p class="sim-scenario-label">${esc(label)}</p>` +
        `<p class="et-scenario-head">${fmtMaybe(aet.mm, 1)} mm today · ${fmtMaybe(aet.l_per_m2, 1)} L/m² · ${fmtMaybe(aet.m3_over_aoi, 0)} m³ over the land</p>` +
        rows.map(([k, v]) =>
          `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${v}</span></div>`).join('') +
        seriesHtml +
        `<p class="sim-note readout-hint">${esc(r.uncertainty_note || '')}</p>`;
      setVaporIntensity(aet.mm);
      return r;
    }

    els.vaporToggle?.addEventListener('change', async (e) => {
      const on = e.target.checked;
      state.vaporLoading = on;
      updateVaporRow(true);
      try {
        if (on) {
          await ensureVaporField();
          setVaporIntensity(state.lastScenario?.aet?.mm ?? climatologicalDailyAetMm());
          window.__twin?.vapor?.setVisible(true);
          state.vaporVisible = true;
          setPanelStatus('');
        } else {
          window.__twin?.vapor?.setVisible(false);
          state.vaporVisible = false;
        }
      } catch (err) {
        state.vaporVisible = false;
        window.__twin?.vapor?.setVisible(false);
        setPanelStatus('Water vapor unavailable: ' + (err?.message || err), 'warn');
      } finally {
        state.vaporLoading = false;
        renderToggles();
      }
    });

    els.scenarioForm?.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (state.scenarioBusy) return;
      state.scenarioBusy = true;
      els.scenarioRun.disabled = true;
      setScenarioStatus('Running ET scenario...');
      try {
        const res = await fetch('/api/et-scenario', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildScenarioParams()),
        });
        // Parse defensively: a non-JSON body is almost always a stale server
        // missing this route (404) or a cross-origin rejection, so surface an
        // actionable message instead of a raw JSON.parse "unexpected character".
        const raw = await res.text();
        let data;
        try {
          data = JSON.parse(raw);
        } catch (_parse) {
          throw new Error(res.status === 404
            ? 'endpoint not found — restart the server to load /api/et-scenario.'
            : `server returned a non-JSON response (HTTP ${res.status}).`);
        }
        if (!data || typeof data !== 'object') throw new Error('ET scenario returned an empty result.');
        if (data.error) throw new Error(data.error);
        renderScenarioResult(data);
        if (data.drape?.layer_id && api.refresh) {
          await api.refresh([data.drape.layer_id]);
          renderToggles();
        }
        setScenarioStatus('', 'ok');
      } catch (err) {
        setScenarioStatus('ET scenario failed: ' + (err?.message || err), 'err');
      } finally {
        state.scenarioBusy = false;
        els.scenarioRun.disabled = false;
      }
    });

    /* --------- natural-language identify for the AET drape (app.js) --------
       samples: [{layer, grid, value}] for enabled water_balance layers at the
       clicked point. Returns one card's inner HTML, or null. */

    function interpretAt(_x, _y, samples) {
      const scen = (samples || []).find((it) => it.layer?.group === 'et_scenario' || it.layer?.id === 'scenario_aet');
      const sv = num(scen?.value);
      if (sv != null) {
        const label = scen.layer?.scenario_label || scen.grid?.scenario_label ||
          state.lastScenario?.scenario?.label || 'ET scenario';
        return (
          `<p class="info-layer">Evapotranspiration</p>` +
          `<p class="info-title">Simulated ET here</p>` +
          `<p class="sim-sentence">About <strong>${fmt(sv, 2)} mm/day</strong> of evapotranspiration at this spot under the “${esc(label)}” scenario.</p>` +
          `<p class="sim-sentence">Scenario AOI-average ET redistributed by this point's wetness/soil/canopy — the relative pattern is meaningful; the absolute per-point value is a distribution of the scenario average, not an independent per-pixel water balance.</p>`
        );
      }
      const s = (samples || []).find((it) => it.layer?.group === 'water_balance' || it.layer?.id === 'aet_annual');
      const v = num(s?.value);
      if (v == null) return null;
      let ctx = '';
      const mean = state.aetMeanMm;
      if (mean != null && mean > 0) {
        const rel = v / mean;
        ctx = rel >= 1.15 ? ' — higher water use than most of this land (wetter, more vegetated ground draws down more soil water)'
          : rel <= 0.85 ? ' — lower water use than most of this land'
            : ' — typical water use for this land';
      }
      return (
        `<p class="info-layer">Evapotranspiration</p>` +
        `<p class="info-title">Water leaving as ET here</p>` +
        `<p class="sim-sentence">About <strong>${fmt(v)} mm/yr</strong> returns to the atmosphere as actual evapotranspiration at this spot${esc(ctx)}.</p>` +
        `<p class="sim-sentence">FAO-56 root-zone water-balance estimate, distributed by terrain wetness and soil available water (±20–35% absent local validation).</p>`
      );
    }

    /* ------------------------------------------------------------ boot ---- */

    async function boot() {
      const [et0, wb, lastScenario] = await Promise.all([
        quietFetch('/data/et/et0-summary.json'),
        quietFetch('/data/et/summary.json'),
        fetchOptionalJson('/data/et/last-et-scenario.json'),
      ]);
      state.et0 = et0;
      state.wb = wb;

      if (wb?.annual) {
        const aets = Object.values(wb.annual).map((r) => num(r?.aet_mm)).filter((x) => x != null);
        state.aetMeanMm = aets.length ? aets.reduce((a, b) => a + b, 0) / aets.length : null;
        const years = Object.keys(wb.annual).sort().reverse();
        els.year.innerHTML =
          `<option value="__mean__">${years.length}-yr average</option>` +
          years.map((y) => `<option value="${esc(y)}">${esc(y)}</option>`).join('');
      }
      setVaporIntensity(climatologicalDailyAetMm());

      renderEt0();
      renderWaterBalance();
      renderAntecedent();
      renderScenarioPresets();
      if (lastScenario?.error) {
        setScenarioStatus(`Last ET scenario could not be restored: ${lastScenario.error}`, 'warn');
      } else if (lastScenario?.data) {
        try {
          applyScenarioResultToForm(lastScenario.data);
          renderScenarioResult(lastScenario.data);
        } catch (err) {
          state.lastScenario = null;
          setScenarioStatus(`Last ET scenario could not be restored: ${err?.message || err}`, 'warn');
        }
      } else if (!wb?.latest_antecedent) {
        setScenarioStatus('Current soil state unavailable; dry and wet soil scenarios still run.', 'warn');
      }
    }

    els.year?.addEventListener('change', renderWaterBalance);

    renderScenarioPresets();
    renderToggles();
    boot().catch((err) => {
      els.status.textContent = `Water & ET panel could not finish loading: ${err?.message || err}`;
    });
    return { state, renderToggles, interpretAt };
  }

  global.VEILET = { create };
})(typeof window !== 'undefined' ? window : globalThis);
