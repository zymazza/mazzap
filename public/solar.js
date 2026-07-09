/* "Solar" tab of the Simulation window: fixed-panel solar/PV siting.
   Layer catalogs, draping and identify sampling stay in app.js; this module
   owns the panel controls, pick flow and proposed-site result card. */
(function attachSolar(global) {
  'use strict';

  const SOLAR_SWATCH = '#e8b84a';

  function fmt(n, digits = 0) {
    return Number(n).toLocaleString(undefined, {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  }

  function num(value) {
    if (value === '' || value === null || value === undefined) return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function create(api) {
    const { viewer, scene } = api;
    const els = {
      panel: document.getElementById('solar-panel'),
      layerToggles: document.getElementById('solar-layer-toggles'),
      surface: document.getElementById('solar-surface'),
      objective: document.getElementById('solar-objective'),
      systemKw: document.getElementById('solar-system-kw'),
      tilt: document.getElementById('solar-tilt'),
      azimuth: document.getElementById('solar-azimuth'),
      run: document.getElementById('solar-run'),
      status: document.getElementById('solar-status'),
      pick: document.getElementById('solar-pick'),
      pickReadout: document.getElementById('solar-pick-readout'),
      siteResults: document.getElementById('solar-site-results'),
      summaryGroup: document.getElementById('solar-summary-group'),
      summary: document.getElementById('solar-summary'),
    };
    if (!els.panel) return null;

    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const georef = VEILGeoref.createSceneGeoref(scene.origin_utm, viewer?.terrainGrid?.minElevation || 0);
    const state = {
      busy: false,
      picking: false,
      summary: null,
      marker: null,
      focusFrame: null,
      lastSite: null,
      selectedSiteKey: null,
    };

    function setStatus(text, kind = '') {
      els.status.textContent = text || '';
      els.status.classList.remove('ok', 'warn', 'err');
      if (kind) els.status.classList.add(kind);
    }

    function fieldNumber(el, fallback = null) {
      const n = num(el?.value);
      return n == null ? fallback : n;
    }

    function selectedParams(point = null) {
      const params = {
        surface: els.surface?.value || 'canopy',
        objective: els.objective?.value || 'annual_kwh',
        system_kw: fieldNumber(els.systemKw, 1),
      };
      if (point) params.point = point;
      const tilt = fieldNumber(els.tilt, null);
      const az = fieldNumber(els.azimuth, null);
      if (tilt != null) params.tilt_deg = tilt;
      if (az != null) params.azimuth_deg = az;
      return params;
    }

    async function quietFetch(url) {
      try {
        const r = await fetch(url);
        return r.ok ? await r.json() : null;
      } catch (_err) {
        return null;
      }
    }

    function solarLayers() {
      return ((api.catalog?.() || {}).layers || []).filter((l) => l.group === 'solar');
    }

    function toggleRow(layer) {
      const row = document.createElement('label');
      row.className = 'toggle-row';
      const loading = !!api.isLoading?.(layer.id);
      row.classList.toggle('loading', loading);
      row.innerHTML =
        `<input type="checkbox" ${api.isEnabled(layer.id) ? 'checked' : ''} ${loading ? 'disabled' : ''} />` +
        `<span class="swatch" style="background:${SOLAR_SWATCH}"></span>` +
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
      const layers = solarLayers();
      if (!layers.length) {
        const hint = document.createElement('p');
        hint.className = 'hint';
        hint.innerHTML = 'Run <code>npm run analyze-solar</code> to build solar planning layers.';
        els.layerToggles.replaceChildren(hint);
        return;
      }
      els.layerToggles.replaceChildren(...layers.map(toggleRow));
    }

    function renderSummary(summary = state.summary) {
      state.summary = summary;
      const vegetationSites = Array.isArray(summary?.vegetation_aware_sites)
        ? summary.vegetation_aware_sites
        : (Array.isArray(summary?.recommended_sites) ? summary.recommended_sites : []);
      const bareSites = Array.isArray(summary?.bare_earth_sites) ? summary.bare_earth_sites : [];
      if (!vegetationSites.length && !bareSites.length) {
        els.summaryGroup.hidden = true;
        return;
      }
      const rowHtml = (site, listKey) => {
        const a = site.annual || {};
        const p = site.point || {};
        const v = site.vegetation || {};
        const rank = Number(site.rank) || 0;
        const key = `${listKey}:${rank}`;
        const active = state.selectedSiteKey === key ? ' active' : '';
        const vegText = listKey === 'bare'
          ? (v.installable === false
              ? `bare-earth potential · clear ${fmt(v.intersecting_crowns_count || 0)} crown conflicts`
              : 'bare-earth potential · open today')
          : (v.installable === false
              ? `clearing required · ${fmt(v.intersecting_crowns_count || 0)} crown conflicts`
              : v.installable === true
                ? `open footprint · ${fmt(v.nearest_crown_clearance_m || 0, 1)} m clearance`
                : 'vegetation unknown');
        return `<button type="button" class="solar-site-row${active}" data-list="${esc(listKey)}" data-rank="${esc(rank)}">` +
          `<strong>#${esc(site.rank)} · ${fmt(a.pv_kwh_per_kwdc || 0)} kWh/kWdc/yr</strong>` +
          `<span>${fmt(site.tilt_deg || 0)} deg tilt · ${fmt(site.azimuth_deg || 0)} deg az · shade ${fmt(a.shade_loss_pct || 0, 1)}%</span>` +
          `<span>${esc(vegText)} · ${num(p.lat) != null ? Number(p.lat).toFixed(6) : '?'} , ${num(p.lon) != null ? Number(p.lon).toFixed(6) : '?'}</span>` +
          `</button>`;
      };
      const sectionHtml = (title, sites, listKey, emptyText) =>
        `<div class="solar-site-section"><p class="solar-site-heading">${esc(title)}</p>` +
        (sites.length
          ? sites.slice(0, 3).map((site) => rowHtml(site, listKey)).join('')
          : `<p class="readout-hint">${esc(emptyText)}</p>`) +
        `</div>`;
      els.summary.innerHTML =
        sectionHtml('Best as-is sites', vegetationSites, 'vegetation', 'No installable open-footprint sites found at this sample spacing.') +
        sectionHtml('Best bare-earth sites', bareSites, 'bare', 'No bare-earth candidates found.');
      els.summaryGroup.hidden = false;
    }

    function vegetationText(vegetation = {}) {
      if (vegetation.installable === false) {
        return `clearing required · ${fmt(vegetation.intersecting_crowns_count || 0)} crown conflicts`;
      }
      if (vegetation.installable === true) {
        return `open footprint · nearest crown clearance ${fmt(vegetation.nearest_crown_clearance_m || 0, 1)} m`;
      }
      return vegetation.available === false ? 'vegetation inventory unavailable' : 'vegetation status unknown';
    }

    function siteResultHtml(result) {
      const a = result?.annual || {};
      const rec = result?.recommendation || {};
      const climate = result?.climate || {};
      const horizon = result?.horizon || {};
      const vegetation = result?.vegetation || {};
      const rows = [
        ['Panel angle', `${fmt(rec.tilt_deg || result.tilt_deg || 0)} deg tilt · ${fmt(rec.azimuth_deg || result.azimuth_deg || 0)} deg az`],
        ['Annual PV', `${fmt(a.pv_kwh || 0)} kWh for ${fmt(result.system_kw || 1, 2)} kWdc · ${fmt(a.pv_kwh_per_kwdc || 0)} kWh/kWdc`],
        ['Panel radiation', `${fmt(a.poa_kwh_m2 || 0)} kWh/m2/yr`],
        ['Winter panel radiation', `${fmt(a.winter_poa_kwh_m2 || 0)} kWh/m2`],
        ['Shade loss', `${fmt(a.shade_loss_pct || 0, 1)}% (${esc(horizon.available ? horizon.surface : 'horizon unavailable')})`],
        ['Cloud loss', `${fmt(climate.cloud_loss_pct || 0, 1)}% ${climate.available ? 'from Daymet normals' : 'clear-sky fallback'}`],
        ['Vegetation', vegetationText(vegetation)],
      ];
      return `<p class="sim-scenario-label">Solar site</p>` +
        rows.map(([k, v]) =>
          `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${v}</span></div>`).join('') +
        `<p class="sim-note readout-hint">Planning-grade fixed-panel estimate. Validate with NSRDB/PVGIS or site measurements before spending money.</p>`;
    }

    async function analyzeSite(point) {
      const res = await fetch('/api/solar/site', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(selectedParams(point)),
      });
      const data = await res.json();
      if (!data || typeof data !== 'object') throw new Error('Solar site returned an empty result.');
      if (data.error) throw new Error(data.error);
      state.lastSite = data;
      els.siteResults.innerHTML = siteResultHtml(data);
      return data;
    }

    function recommendedSiteHtml(site, listKey = 'vegetation') {
      const a = site?.annual || {};
      const p = site?.point || {};
      const vegetation = site?.vegetation || {};
      const title = listKey === 'bare'
        ? `Best bare-earth solar site #${esc(site?.rank || '')}`
        : `Best as-is solar site #${esc(site?.rank || '')}`;
      const rows = [
        ['Panel angle', `${fmt(site?.tilt_deg || 0)} deg tilt · ${fmt(site?.azimuth_deg || 0)} deg az`],
        ['Annual PV', `${fmt(a.pv_kwh || 0)} kWh for ${fmt(fieldNumber(els.systemKw, 1), 2)} kWdc · ${fmt(a.pv_kwh_per_kwdc || 0)} kWh/kWdc`],
        ['Panel radiation', `${fmt(a.poa_kwh_m2 || 0)} kWh/m2/yr`],
        ['Winter panel radiation', `${fmt(a.winter_poa_kwh_m2 || 0)} kWh/m2`],
        ['Shade loss', `${fmt(a.shade_loss_pct || 0, 1)}%`],
        ['Vegetation', vegetationText(vegetation)],
        ['Location', `${num(p.lat) != null ? Number(p.lat).toFixed(6) : '?'} , ${num(p.lon) != null ? Number(p.lon).toFixed(6) : '?'}`],
      ];
      const note = listKey === 'bare'
        ? 'Bare-earth list is cleared/no-vegetation potential. The vegetation row shows what exists there today.'
        : 'Click any other terrain spot to overwrite this marker.';
      return `<p class="sim-scenario-label">${title}</p>` +
        rows.map(([k, v]) =>
          `<div class="info-row"><span class="info-k">${esc(k)}</span><span class="info-v">${v}</span></div>`).join('') +
        `<p class="sim-note readout-hint">${esc(note)}</p>`;
    }

    function localTerrainPoint(x, yNorth) {
      const terrainY = global.VEILTerrain?.sampleTerrainHeightAtLocal
        ? global.VEILTerrain.sampleTerrainHeightAtLocal(viewer.terrainGrid, x, yNorth)
        : 0;
      return new THREE.Vector3(x, terrainY, -yNorth);
    }

    function focusCameraOn(point) {
      const camera = viewer.camera;
      const controls = viewer.controls;
      if (!camera || !controls || !point) return;
      if (state.focusFrame) cancelAnimationFrame(state.focusFrame);
      const target = point.clone();
      target.y += 2;
      let offset = camera.position.clone().sub(controls.target);
      if (!Number.isFinite(offset.lengthSq()) || offset.lengthSq() < 1e-6) {
        offset = new THREE.Vector3(95, 70, 95);
      }
      const horizontal = new THREE.Vector3(offset.x, 0, offset.z);
      if (horizontal.lengthSq() < 1e-6) horizontal.set(1, 0, 1);
      horizontal.normalize();
      const up = Math.max(0.34, Math.min(0.62, offset.normalize().y || 0.42));
      const flat = Math.sqrt(Math.max(0.01, 1 - up * up));
      const dir = new THREE.Vector3(horizontal.x * flat, up, horizontal.z * flat);
      const distance = Math.max(70, Math.min(145, camera.position.distanceTo(controls.target) * 0.38 || 110));
      const endPos = target.clone().addScaledVector(dir, distance);
      const startPos = camera.position.clone();
      const startTarget = controls.target.clone();
      const started = performance.now();
      const duration = 420;
      const ease = (t) => 1 - Math.pow(1 - t, 3);
      const step = (now) => {
        const t = Math.min(1, (now - started) / duration);
        const k = ease(t);
        camera.position.lerpVectors(startPos, endPos, k);
        controls.target.lerpVectors(startTarget, target, k);
        camera.lookAt(controls.target);
        controls.update();
        if (t < 1) state.focusFrame = requestAnimationFrame(step);
        else state.focusFrame = null;
      };
      state.focusFrame = requestAnimationFrame(step);
    }

    function selectRecommendedSite(site, listKey = 'vegetation') {
      const p = site?.point || {};
      const x = num(p.x);
      const y = num(p.y);
      if (x == null || y == null) {
        setStatus('Recommended site has no local map coordinate.', 'err');
        return;
      }
      const point = localTerrainPoint(x, y);
      const g = georef.worldToGeo(point.x, point.y, point.z);
      placeMarker(point);
      focusCameraOn(point);
      const lat = num(g.lat) ?? num(p.lat);
      const lon = num(g.lon) ?? num(p.lon);
      if (lat != null && lon != null) els.pickReadout.textContent = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
      els.siteResults.innerHTML = recommendedSiteHtml(site, listKey);
      state.selectedSiteKey = `${listKey}:${Number(site.rank) || 0}`;
      renderSummary(state.summary);
      setStatus(`Highlighted ${listKey === 'bare' ? 'bare-earth' : 'as-is'} site #${site.rank || ''}`, 'ok');
    }

    async function runAnalyze() {
      if (state.busy) return;
      state.busy = true;
      els.run.disabled = true;
      setStatus('Analyzing solar resource...');
      try {
        const res = await fetch('/api/solar/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(selectedParams()),
        });
        const data = await res.json();
        if (!data || typeof data !== 'object') throw new Error('Solar analysis returned an empty result.');
        if (data.error) throw new Error(data.error);
        await api.refresh?.(data.layers || []);
        renderToggles();
        state.summary = await quietFetch('/data/solar/solar-summary.json');
        renderSummary(state.summary);
        const valid = data.valid_points || 0;
        const installable = data.installable_points ?? valid;
        const excluded = data.vegetation_excluded_points || 0;
        setStatus(`Solar layers ready · ${installable}/${valid} installable sites${excluded ? ` · ${excluded} vegetation conflicts` : ''}`, 'ok');
      } catch (err) {
        setStatus('Solar analysis failed: ' + (err?.message || err), 'err');
      } finally {
        state.busy = false;
        els.run.disabled = false;
      }
    }

    function placeMarker(point) {
      if (!state.marker) {
        state.marker = new THREE.Mesh(
          new THREE.SphereGeometry(2.8, 16, 12),
          new THREE.MeshBasicMaterial({ color: 0xe8b84a })
        );
        state.marker.renderOrder = 999;
        viewer.scene.add(state.marker);
      }
      state.marker.position.copy(point);
      state.marker.visible = true;
    }

    function clearMarker() {
      if (state.marker) state.marker.visible = false;
      if (state.focusFrame) {
        cancelAnimationFrame(state.focusFrame);
        state.focusFrame = null;
      }
    }

    function pickAtScreen(clientX, clientY) {
      if (!state.picking) return false;
      const canvas = viewer.renderer?.domElement;
      if (!canvas) return false;
      const rect = canvas.getBoundingClientRect();
      ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, viewer.camera);
      const hit = raycaster.intersectObject(viewer.terrainMesh, false)[0];
      if (!hit) return true;
      const point = { x: hit.point.x, y: -hit.point.z };
      const geo = georef.worldToGeo(hit.point.x, hit.point.y, hit.point.z);
      placeMarker(hit.point);
      els.pickReadout.textContent = `${geo.lat.toFixed(6)}, ${geo.lon.toFixed(6)}`;
      els.siteResults.innerHTML = '<p class="readout-hint">Calculating solar profile...</p>';
      state.selectedSiteKey = null;
      renderSummary(state.summary);
      analyzeSite(point).catch((err) => {
        els.siteResults.innerHTML = `<p class="readout-hint">Solar site failed: ${esc(err?.message || err)}</p>`;
      });
      setPicking(false);
      return true;
    }

    function setPicking(on) {
      state.picking = !!on;
      els.pick.classList.toggle('active', state.picking);
      els.pick.textContent = state.picking ? 'Click terrain for panel site' : 'Pick panel site';
      const canvas = viewer.renderer?.domElement;
      if (canvas) canvas.style.cursor = state.picking ? 'crosshair' : '';
      if (state.picking) els.pickReadout.textContent = 'click the terrain';
    }

    function interpretAt(_x, _y, samples) {
      const annual = (samples || []).find((s) => s.layer?.id === 'solar_pv_annual');
      const poa = (samples || []).find((s) => s.layer?.id === 'solar_poa_annual');
      const shade = (samples || []).find((s) => s.layer?.id === 'solar_shade_loss');
      const winter = (samples || []).find((s) => s.layer?.id === 'solar_winter_poa');
      const vegetation = (samples || []).find((s) => s.layer?.id === 'solar_vegetation_clearance');
      const parts = [];
      if (num(annual?.value) != null) parts.push(`PV yield <strong>${fmt(annual.value)}</strong> kWh/kWdc/yr`);
      if (num(poa?.value) != null) parts.push(`panel radiation <strong>${fmt(poa.value)}</strong> kWh/m2/yr`);
      if (num(winter?.value) != null) parts.push(`winter <strong>${fmt(winter.value)}</strong> kWh/m2`);
      if (num(shade?.value) != null) parts.push(`shade loss <strong>${fmt(shade.value, 1)}%</strong>`);
      if (num(vegetation?.value) != null) parts.push(`vegetation clearance <strong>${fmt(vegetation.value, 1)}</strong> m`);
      if (!parts.length) return null;
      return `<p class="info-layer">Solar</p>` +
        `<p class="info-title">Solar potential here</p>` +
        `<p class="sim-sentence">${parts.join(' · ')}</p>` +
        `<p class="sim-sentence">Layer estimate from the sampled solar lattice; use Pick panel site for a point-specific horizon and angle recommendation.</p>`;
    }

    els.run?.addEventListener('click', runAnalyze);
    els.pick?.addEventListener('click', () => setPicking(!state.picking));
    els.summary?.addEventListener('click', (e) => {
      const row = e.target.closest?.('.solar-site-row');
      if (!row) return;
      const rank = Number(row.dataset.rank);
      const listKey = row.dataset.list === 'bare' ? 'bare' : 'vegetation';
      const sites = listKey === 'bare'
        ? (Array.isArray(state.summary?.bare_earth_sites) ? state.summary.bare_earth_sites : [])
        : (Array.isArray(state.summary?.vegetation_aware_sites)
            ? state.summary.vegetation_aware_sites
            : (Array.isArray(state.summary?.recommended_sites) ? state.summary.recommended_sites : []));
      const site = sites.find((s) => Number(s.rank) === rank);
      if (site) selectRecommendedSite(site, listKey);
    });
    document.addEventListener('veil:map-pick', (e) => {
      if (e.detail?.source === 'solar-recommended') return;
      if (state.selectedSiteKey == null) return;
      state.selectedSiteKey = null;
      clearMarker();
      renderSummary(state.summary);
    });

    async function boot() {
      renderToggles();
      state.summary = await quietFetch('/data/solar/solar-summary.json');
      renderSummary(state.summary);
    }
    boot();

    return {
      isPicking: () => state.picking,
      pickAtScreen,
      interpretAt,
      _runAnalyze: runAnalyze,
    };
  }

  global.VEILSolar = { create };
})(window);
