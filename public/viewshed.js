(function attachViewshed(global) {
  'use strict';

  const { THREE, VEILTerrain } = global;
  const EARTH_RADIUS_M = 6371000;
  const REFRACTION_K = { optical: 1 / 7, radio: 0.25, radio_4_3: 0.25 };

  function esc(text) {
    return String(text ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function refractionK(value) {
    if (value == null) return REFRACTION_K.optical;
    if (typeof value === 'string' && REFRACTION_K[value]) return REFRACTION_K[value];
    const n = Number(value);
    if (!Number.isFinite(n)) throw new Error(`unknown refraction preset: ${value}`);
    return n;
  }

  function decodeVat(vat) {
    const out = new Map();
    Object.entries(vat || {}).forEach(([raw, meta]) => {
      const code = Number(raw);
      if (!Number.isFinite(code)) return;
      const name = String(meta?.name || '');
      const m = name.match(/(?:Tree|Shrub|Herb(?:aceous)?)\s+Height\s*=\s*(\d+(?:\.\d+)?)\s*meter/i);
      out.set(code, m ? Number(m[1]) : 0);
    });
    return out;
  }

  function nearestGridValue(grid, x, y) {
    const b = grid.bounds_local;
    if (!b || x < b[0] || x > b[2] || y < b[1] || y > b[3]) return null;
    const col = Math.min(grid.width - 1, Math.max(0, Math.floor((x - b[0]) / Math.max(1e-9, b[2] - b[0]) * grid.width)));
    const row = Math.min(grid.height - 1, Math.max(0, Math.floor((b[3] - y) / Math.max(1e-9, b[3] - b[1]) * grid.height)));
    return grid.values?.[row]?.[col] ?? null;
  }

  function buildCanopy(grid, evhGrid, vat) {
    const map = decodeVat(vat);
    const canopy = new Float32Array(grid.width * grid.height);
    const xStep = (grid.maxX - grid.minX) / Math.max(1, grid.width - 1);
    const yStep = (grid.maxY - grid.minY) / Math.max(1, grid.height - 1);
    for (let row = 0; row < grid.height; row += 1) {
      const y = grid.maxY - row * yStep;
      for (let col = 0; col < grid.width; col += 1) {
        const x = grid.minX + col * xStep;
        canopy[row * grid.width + col] = map.get(Number(nearestGridValue(evhGrid, x, y))) || 0;
      }
    }
    return canopy;
  }

  function flattenGround(grid) {
    const out = new Float32Array(grid.width * grid.height);
    for (let i = 0; i < out.length; i += 1) {
      const v = grid.heights[i];
      out[i] = Number.isFinite(v) ? Number(v) : NaN;
    }
    return out;
  }

  function coreFactory() {
    const EARTH_RADIUS_M = 6371000;
    const REFRACTION_K = { optical: 1 / 7, radio: 0.25, radio_4_3: 0.25 };
    function refractionK(value) {
      if (value == null) return REFRACTION_K.optical;
      if (typeof value === 'string' && REFRACTION_K[value]) return REFRACTION_K[value];
      const n = Number(value);
      if (!Number.isFinite(n)) throw new Error(`unknown refraction preset: ${value}`);
      return n;
    }
    function normalizeStack(input) {
      const rings = Array.isArray(input?.rings) ? input.rings : [input].filter(Boolean);
      return rings.slice().sort((a, b) => (a.resolutionM || 0) - (b.resolutionM || 0));
    }
    function sampleGround(ring, x, y) {
      if (x < ring.minX || x > ring.maxX || y < ring.minY || y > ring.maxY) return NaN;
      const xr = Math.min(0.999999, Math.max(0, (x - ring.minX) / Math.max(1e-9, ring.maxX - ring.minX)));
      const yr = Math.min(0.999999, Math.max(0, (y - ring.minY) / Math.max(1e-9, ring.maxY - ring.minY)));
      const xi = xr * (ring.width - 1);
      const yi = (1 - yr) * (ring.height - 1);
      const x0 = Math.floor(xi); const y0 = Math.floor(yi);
      const x1 = Math.min(ring.width - 1, x0 + 1); const y1 = Math.min(ring.height - 1, y0 + 1);
      const tx = xi - x0; const ty = yi - y0;
      const vals = [
        ring.ground[y0 * ring.width + x0],
        ring.ground[y0 * ring.width + x1],
        ring.ground[y1 * ring.width + x0],
        ring.ground[y1 * ring.width + x1],
      ];
      const wgts = [(1 - tx) * (1 - ty), tx * (1 - ty), (1 - tx) * ty, tx * ty];
      let sum = 0; let den = 0;
      for (let i = 0; i < 4; i += 1) {
        if (Number.isFinite(vals[i])) { sum += vals[i] * wgts[i]; den += wgts[i]; }
      }
      return den > 0 ? sum / den : NaN;
    }
    function sampleCanopy(ring, x, y) {
      if (!ring.canopy || x < ring.minX || x > ring.maxX || y < ring.minY || y > ring.maxY) return 0;
      const xr = Math.min(1, Math.max(0, (x - ring.minX) / Math.max(1e-9, ring.maxX - ring.minX)));
      const yr = Math.min(1, Math.max(0, (y - ring.minY) / Math.max(1e-9, ring.maxY - ring.minY)));
      const col = Math.round(xr * (ring.width - 1));
      const row = Math.round((1 - yr) * (ring.height - 1));
      return ring.canopy[row * ring.width + col] || 0;
    }
    function rowCol(ring, x, y) {
      if (x < ring.minX || x > ring.maxX || y < ring.minY || y > ring.maxY) return null;
      const col = Math.round((x - ring.minX) / Math.max(1e-9, ring.maxX - ring.minX) * (ring.width - 1));
      const row = Math.round((ring.maxY - y) / Math.max(1e-9, ring.maxY - ring.minY) * (ring.height - 1));
      return [Math.max(0, Math.min(ring.height - 1, row)), Math.max(0, Math.min(ring.width - 1, col))];
    }
    function sampleComponents(rings, x, y) {
      for (let i = 0; i < rings.length; i += 1) {
        const ring = rings[i];
        const g = sampleGround(ring, x, y);
        if (Number.isFinite(g)) {
          return { ring, ground: g, canopy: sampleCanopy(ring, x, y) };
        }
      }
      return { ring: null, ground: NaN, canopy: 0 };
    }
    function radialDistances(rings, maxM) {
      const cap = maxM != null && Number.isFinite(Number(maxM))
        ? Math.max(1, Number(maxM))
        : Math.max(...rings.map((ring) => Number(ring.outerM || 0)));
      const vals = [];
      rings.slice().sort((a, b) => (a.innerM || 0) - (b.innerM || 0)).forEach((ring) => {
        const inner = Math.max(0, Number(ring.innerM || 0));
        const outer = Math.min(cap, Number(ring.outerM || cap));
        if (outer <= inner) return;
        const step = Math.max(1, Number(ring.resolutionM || 1));
        for (let d = Math.max(step * 0.5, inner + step * 0.5); d <= outer + step * 0.25; d += step) {
          vals.push(d);
        }
      });
      vals.sort((a, b) => a - b);
      const out = [];
      let prev = -Infinity;
      vals.forEach((v) => {
        if (v <= cap && Math.abs(v - prev) > 1e-4) {
          out.push(v);
          prev = v;
        }
      });
      return new Float32Array(out);
    }
    function sweep(stack, opts) {
      const rings = normalizeStack(stack);
      if (!rings.length) throw new Error('viewshed worker has no terrain rings');
      const nAz = Math.max(8, Math.floor(opts.nAz || 720));
      const surface = opts.surface === 'bare_earth' ? 'bare_earth' : 'canopy';
      const k = refractionK(opts.k ?? 'optical');
      const distances = radialDistances(rings, opts.maxKm ? opts.maxKm * 1000 : null);
      if (!distances.length) throw new Error('no radial samples inside loaded viewshed rings');
      const obsGround = sampleComponents(rings, opts.x, opts.y).ground;
      if (!Number.isFinite(obsGround)) throw new Error('observer point is outside available terrain');
      const eye = obsGround + Number(opts.aglM || 1.7);
      const masks = rings.map((ring) => ({ id: ring.id, width: ring.width, height: ring.height, mask: new Uint8Array(ring.width * ring.height) }));
      const horizon = new Float32Array(nAz);
      let maxVisibleM = 0;
      for (let a = 0; a < nAz; a += 1) {
        const az = a * Math.PI * 2 / nAz;
        const sx = Math.sin(az); const cy = Math.cos(az);
        let running = -1e30;
        horizon[a] = NaN;
        for (let j = 0; j < distances.length; j += 1) {
          const d = distances[j];
          const x = opts.x + sx * d;
          const y = opts.y + cy * d;
          const sample = sampleComponents(rings, x, y);
          const g = sample.ground;
          if (!Number.isFinite(g) || !sample.ring) continue;
          const canopy = surface === 'canopy' ? sample.canopy : 0;
          const drop = (1 - k) * d * d / (2 * EARTH_RADIUS_M);
          const blockerAngle = Math.atan2(g + canopy - drop - eye, d);
          const targetAngle = Math.atan2(g + Number(opts.targetAglM || 0) - drop - eye, d);
          const visible = targetAngle > running + 1e-7;
          if (visible) {
            const maskEntry = masks.find((entry) => entry.id === sample.ring.id);
            const rc = rowCol(sample.ring, x, y);
            if (maskEntry && rc) maskEntry.mask[rc[0] * sample.ring.width + rc[1]] = 1;
            maxVisibleM = Math.max(maxVisibleM, d);
          }
          if (blockerAngle > running) running = blockerAngle;
        }
        horizon[a] = running > -1e20 ? running * 180 / Math.PI : NaN;
      }
      const perRing = {};
      let visibleKm2 = 0;
      masks.forEach((entry) => {
        const ring = rings.find((candidate) => candidate.id === entry.id);
        let visibleCells = 0; let validCells = 0;
        for (let i = 0; i < ring.ground.length; i += 1) {
          if (Number.isFinite(ring.ground[i])) {
            validCells += 1;
            if (entry.mask[i]) visibleCells += 1;
          }
        }
        const areaKm2 = visibleCells * ring.cellAreaM2 / 1000000;
        visibleKm2 += areaKm2;
        perRing[ring.id] = {
          visibleCells,
          validCells,
          fraction: validCells ? visibleCells / validCells : 0,
          visibleKm2: areaKm2,
          resolutionM: ring.resolutionM,
          canopyAvailable: Boolean(ring.canopy),
        };
      });
      return {
        horizonDeg: horizon,
        masks,
        stats: {
          visibleKm2,
          maxVisibleKm: maxVisibleM / 1000,
          perRing,
          skyOpenFractionGe2Deg: Array.from(horizon).filter((v) => Number.isFinite(v) && v <= 2).length / nAz,
          analyzedExtentKm: distances[distances.length - 1] / 1000,
        },
        surface,
        k,
        cc: 1 - k,
      };
    }
    function horizonAt(horizon, azimuthDeg) {
      if (!horizon || !horizon.length) return NaN;
      const pos = (((Number(azimuthDeg) % 360) + 360) % 360) / 360 * horizon.length;
      const i0 = Math.floor(pos) % horizon.length;
      const i1 = (i0 + 1) % horizon.length;
      const t = pos - Math.floor(pos);
      return horizon[i0] * (1 - t) + horizon[i1] * t;
    }
    return { sweep, horizonAt };
  }

  const core = coreFactory();

  function workerSource() {
    return `
      const core = (${coreFactory.toString()})();
      let stack = null;
      self.onmessage = (event) => {
        const msg = event.data || {};
        try {
          if (msg.type === 'init') {
            stack = msg.stack;
            self.postMessage({ type: 'ready' });
          } else if (msg.type === 'sweep') {
            if (!stack) throw new Error('viewshed worker is not initialized');
            const result = core.sweep(stack, msg.options || {});
            const transfer = [result.horizonDeg.buffer, ...result.masks.map((entry) => entry.mask.buffer)];
            self.postMessage({ type: 'result', requestId: msg.requestId, result }, transfer);
          }
        } catch (err) {
          self.postMessage({ type: 'error', requestId: msg.requestId, error: err && err.message ? err.message : String(err) });
        }
      };
    `;
  }

  function makeRingPayload(grid, canopy) {
    const xStep = (grid.maxX - grid.minX) / Math.max(1, grid.width - 1);
    const yStep = (grid.maxY - grid.minY) / Math.max(1, grid.height - 1);
    const outerM = Math.max(Math.abs(grid.minX), Math.abs(grid.maxX), Math.abs(grid.minY), Math.abs(grid.maxY)) * Math.SQRT2;
    return {
      id: 'A',
      width: grid.width,
      height: grid.height,
      minX: grid.minX,
      maxX: grid.maxX,
      minY: grid.minY,
      maxY: grid.maxY,
      resolutionM: Math.min(Math.abs(xStep), Math.abs(yStep)),
      cellAreaM2: Math.abs(xStep * yStep),
      innerM: 0,
      outerM,
      ground: flattenGround(grid),
      canopy,
      tiles: [],
    };
  }

  async function fetchJson(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`${path} returned ${res.status}`);
    return res.json();
  }

  // Composite parcel LiDAR over the apron (parcel where finite, else apron) so
  // ring A is fully-populated 3 m terrain. Must match the Python
  // merge_local_grids exactly (same files, same rule) for JS<->Python sweep
  // parity; the parcel interior is otherwise nodata in the apron and the sweep
  // would fall through to the coarse 30 m distant ring.
  function mergeLocalGrids(base, overlay) {
    if (!overlay?.heights || !base?.heights || overlay.heights.length !== base.heights.length) {
      return base;
    }
    const heights = base.heights.slice();
    for (let i = 0; i < heights.length; i += 1) {
      if (overlay.heights[i] != null) heights[i] = overlay.heights[i];
    }
    return { ...base, heights };
  }

  async function loadStack(viewer) {
    const apron = await fetchJson('/data/terrain/grid.apron.json').catch(() => null);
    const parcel = await fetchJson('/data/terrain/grid.json').catch(() => null);
    const grid = apron ? mergeLocalGrids(apron, parcel) : (parcel || viewer?.terrainGrid);
    let canopy = new Float32Array(grid.width * grid.height);
    try {
      const [evh, vat] = await Promise.all([
        fetchJson('/data/atlas/local/landfire_evh_2024.grid.json'),
        fetchJson('/data/atlas/vat/landfire_evh_2024.json'),
      ]);
      canopy = buildCanopy(grid, evh, vat);
    } catch (err) {
      console.warn('viewshed canopy unavailable; using bare-earth blockers', err);
    }
    const rings = [makeRingPayload(grid, canopy)];
    try {
      const distant = await VEILTerrain.ensureDistantTerrain(viewer);
      if (distant?.rings?.length) {
        distant.rings.forEach((ring) => rings.push(ring));
      }
    } catch (err) {
      console.warn('distant viewshed terrain unavailable; using local ring only', err);
    }
    return { rings };
  }

  function createMarker(viewer, point, aglM) {
    const group = new THREE.Group();
    group.name = 'viewshed-observer';
    const pole = new THREE.Mesh(
      new THREE.CylinderGeometry(0.6, 0.6, Math.max(1, aglM), 10),
      new THREE.MeshBasicMaterial({ color: 0xff8c1a })
    );
    pole.position.y = aglM / 2;
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(3.5, 0.18, 8, 32),
      new THREE.MeshBasicMaterial({ color: 0xff8c1a })
    );
    ring.rotation.x = Math.PI / 2;
    group.add(pole, ring);
    group.position.copy(point);
    viewer.scene.add(group);
    return group;
  }

  function create(api) {
    const els = {
      toggle: document.getElementById('viewshed-enable'),
      agl: document.getElementById('viewshed-agl'),
      surface: document.getElementById('viewshed-surface'),
      status: document.getElementById('viewshed-status'),
      povCull: document.getElementById('viewshed-pov-cull'),
      distant: document.getElementById('viewshed-distant-toggle'),
    };
    if (!els.toggle || !api?.viewer || !THREE) return null;
    const viewer = api.viewer;
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    let worker = null;
    let stackPromise = null;
    let stackPayload = null;
    let requestId = 0;
    let latest = null;
    let marker = null;
    let observer = null;
    let picking = false;
    let povTimer = null;
    let lastPov = null;
    let previousPovActive = Boolean(viewer.povController?.active);
    const requestPurpose = new Map();

    function setStatus(text, tone = '') {
      if (!els.status) return;
      els.status.textContent = text || '';
      els.status.className = `pov-status${tone ? ` ${tone}` : ''}`;
    }

    async function ensureWorker() {
      if (worker) return worker;
      stackPromise = stackPromise || loadStack(viewer);
      stackPayload = await stackPromise;
      worker = new Worker(URL.createObjectURL(new Blob([workerSource()], { type: 'application/javascript' })));
      worker.onmessage = (event) => {
        const msg = event.data || {};
        const purpose = requestPurpose.get(msg.requestId) || 'observer';
        requestPurpose.delete(msg.requestId);
        if (msg.type === 'result') {
          if (purpose === 'pov') {
            if (viewer.povController?.active && povCullEnabled()) {
              applyDistantVisibility(msg.result).catch((err) => {
                console.warn('could not apply POV distant terrain visibility', err);
              });
            }
          } else {
            latest = msg.result;
            renderResult();
          }
        } else if (msg.type === 'error') {
          if (purpose === 'pov') {
            console.warn('POV distant terrain cull failed:', msg.error);
          } else {
            setStatus(`Viewshed failed: ${msg.error}`, 'err');
          }
        }
      };
      worker.postMessage({ type: 'init', stack: stackPayload });
      return worker;
    }

    function currentOptions() {
      return {
        x: observer?.x,
        y: observer?.y,
        aglM: Number(els.agl?.value || 1.7),
        surface: els.surface?.value || 'bare_earth',
        k: 'optical',
        nAz: 720,
      };
    }

    async function runSweep() {
      if (!observer) return;
      setStatus('Computing viewshed...');
      const w = await ensureWorker();
      requestId += 1;
      requestPurpose.set(requestId, 'observer');
      w.postMessage({ type: 'sweep', requestId, options: currentOptions() });
    }

    function tileVisibilityFractions(result) {
      const out = new Map();
      if (!result?.masks || !stackPayload?.rings) return out;
      result.masks.forEach((entry) => {
        if (entry.id === 'A') return;
        const ring = stackPayload.rings.find((candidate) => candidate.id === entry.id);
        if (!ring?.tiles?.length) return;
        const mask = entry.mask;
        ring.tiles.forEach((tile) => {
          const size = Number(ring.tileSize || 256);
          const row0 = tile.j * size;
          const col0 = tile.i * size;
          const rows = Math.max(0, Math.min(ring.height, row0 + size) - row0);
          const cols = Math.max(0, Math.min(ring.width, col0 + size) - col0);
          let visible = 0;
          for (let r = 0; r < rows; r += 1) {
            const offset = (row0 + r) * ring.width + col0;
            for (let c = 0; c < cols; c += 1) {
              if (mask[offset + c]) visible += 1;
            }
          }
          out.set(tile.key, tile.validCells > 0 ? visible / tile.validCells : 0);
        });
      });
      return out;
    }

    async function applyDistantVisibility(result) {
      if (!els.distant || els.distant.checked === false) return;
      try {
        const distant = await VEILTerrain.ensureDistantTerrain(viewer);
        distant.setEnabled(true);
        distant.setVisibilityFractions(tileVisibilityFractions(result));
      } catch (err) {
        console.warn('could not update distant terrain visibility', err);
      }
    }

    async function showAllDistantTerrain(value) {
      if (els.distant && els.distant.checked === false) return;
      const distant = await VEILTerrain.ensureDistantTerrain(viewer);
      distant.setEnabled(true);
      distant.showAll(value);
    }

    async function restoreDistantTerrainAfterPov() {
      if (els.distant && els.distant.checked === false) return;
      const distant = await VEILTerrain.ensureDistantTerrain(viewer);
      distant.setEnabled(true);
      if (observer && latest) {
        distant.setVisibilityFractions(tileVisibilityFractions(latest));
      } else {
        distant.showAll();
      }
    }

    function renderResult() {
      if (!latest) return;
      const s = latest.stats || {};
      setStatus(`${(s.visibleKm2 || 0).toFixed(3)} km² visible · furthest ${(s.maxVisibleKm || 0).toFixed(2)} km · ${(100 * (s.skyOpenFractionGe2Deg || 0)).toFixed(0)}% sky-open azimuths`);
      applyDistantVisibility(latest);
    }

    function pickAtScreen(clientX, clientY) {
      if (!picking) return false;
      const canvas = viewer.renderer?.domElement;
      if (!canvas) return false;
      const rect = canvas.getBoundingClientRect();
      ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, viewer.camera);
      const hit = raycaster.intersectObject(viewer.terrainMesh, false)[0];
      if (!hit) return true;
      observer = { x: hit.point.x, y: -hit.point.z };
      if (marker) viewer.scene.remove(marker);
      marker = createMarker(viewer, hit.point, Number(els.agl?.value || 1.7));
      runSweep();
      return true;
    }

    function setPicking(on) {
      picking = !!on;
      els.toggle.classList.toggle('active', picking);
      els.toggle.textContent = picking ? 'Click terrain for observer' : 'Analyze views';
      const canvas = viewer.renderer?.domElement;
      if (canvas) canvas.style.cursor = picking ? 'crosshair' : '';
      if (picking) setStatus('Click the terrain to place the observer.');
      if (picking) {
        VEILTerrain.ensureDistantTerrain(viewer)
          .then((distant) => {
            distant.setEnabled(els.distant ? els.distant.checked !== false : true);
            if (!latest) distant.showAll();
          })
          .catch(() => {});
      }
    }

    els.toggle.addEventListener('click', () => setPicking(!picking));
    els.agl?.addEventListener('change', () => {
      if (marker) {
        viewer.scene.remove(marker);
        const base = new THREE.Vector3(observer.x, VEILTerrain.sampleTerrainHeightAtLocal(viewer.terrainGrid, observer.x, observer.y), -observer.y);
        marker = createMarker(viewer, base, Number(els.agl.value || 1.7));
      }
      runSweep();
    });
    els.surface?.addEventListener('change', runSweep);
    els.distant?.addEventListener('change', async () => {
      try {
        const distant = await VEILTerrain.ensureDistantTerrain(viewer);
        distant.setEnabled(els.distant.checked !== false);
        if (latest) {
          distant.setVisibilityFractions(tileVisibilityFractions(latest));
        } else if (els.distant.checked !== false) {
          distant.showAll();
        }
      } catch (err) {
        setStatus(`Distant terrain unavailable: ${err.message || err}`, 'err');
      }
    });
    els.povCull?.addEventListener('change', async () => {
      if (!viewer.povController?.active) return;
      try {
        if (els.povCull.checked === false) {
          await showAllDistantTerrain();
        } else {
          await runPovCull(true);
        }
      } catch (_err) {
        // POV remains usable without distant terrain.
      }
    });

    async function runPovCull(force = false) {
      if (!viewer.povController?.active || !povCullEnabled()) return;
      const camera = viewer.camera;
      const local = { x: camera.position.x, y: -camera.position.z };
      const moved = lastPov ? Math.hypot(local.x - lastPov.x, local.y - lastPov.y) : Infinity;
      const now = performance.now();
      if (!force && lastPov && moved < 8 && now - lastPov.t < 500) return;
      lastPov = { ...local, t: now };
      const w = await ensureWorker();
      requestId += 1;
      requestPurpose.set(requestId, 'pov');
      w.postMessage({
        type: 'sweep',
        requestId,
        options: {
          x: local.x,
          y: local.y,
          aglM: 1.7,
          surface: 'bare_earth',
          k: 'optical',
          nAz: 360,
        },
      });
    }

    viewer.onFrame?.(() => {
      const povActive = Boolean(viewer.povController?.active);
      if (povActive !== previousPovActive) {
        lastPov = null;
        povTimer = null;
        previousPovActive = povActive;
        if (!povActive) {
          restoreDistantTerrainAfterPov().catch(() => {});
          return;
        }
      }
      if (!povActive) return;
      if (!povCullEnabled()) {
        if (!povTimer) {
          showAllDistantTerrain().catch(() => {});
          povTimer = true;
        }
        return;
      }
      povTimer = null;
      runPovCull(false).catch(() => {});
    });

    function povCullEnabled() {
      return els.povCull ? els.povCull.checked !== false : true;
    }

    return {
      isPicking: () => picking,
      pickAtScreen,
      horizonAt(azimuthDeg) {
        return latest?.horizonDeg ? core.horizonAt(latest.horizonDeg, azimuthDeg) : null;
      },
      activeResult: () => latest,
      povCullEnabled,
      _runSweep: runSweep,
      _runPovCull: runPovCull,
    };
  }

  global.VEILViewshed = {
    create,
    _test: {
      core,
      buildCanopy,
      flattenGround,
      makeRingPayload,
      refractionK,
    },
  };
})(typeof window !== 'undefined' ? window : globalThis);
