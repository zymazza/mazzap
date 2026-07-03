/* Boot the VEIL digital twin: load the static scene, drive the ported
   3D viewer, drape atlas layers onto the terrain surface as colored pixels
   (no floating 3D geometry), and answer click-to-identify queries. */
(function boot() {
  'use strict';

  const { THREE, VEILViewer, VEILGeoref } = window;
  const rootEl = document.getElementById('viewer-root');
  const loadingEl = document.getElementById('loading');
  const loadingText = document.getElementById('loading-text');

  // Scene layers rendered by the ported viewer (lines/instances — these don't
  // clip). Soils moved to the atlas drape so it colors pixels instead.
  const SCENE_LAYERS = [
    { id: 'parcels', label: 'Parcels', color: '#f2c14e' },
    { id: 'buildings', label: 'Buildings', color: '#e07a5f' },
    { id: 'hydrology', label: 'Streams & water', color: '#4ea8de' },
    { id: 'roads', label: 'Roads', color: '#cfd8dc' },
    { id: 'vegetation', label: 'Vegetation', color: '#6fcf97' },
  ];

  function sceneLayerDefaultVisible(layerId) {
    return layerId !== 'parcels';
  }

  // Layers whose polygons should each get their own stable color (categorical).
  const COLOR_BY_LABEL = new Set([
    'gssurgo_soils', 'hudson_mohawk_surficial_geology', 'apa_land_classification',
  ]);

  const state = {
    viewer: null,
    scene: null,
    atlas: null,            // viewer-layers.json
    survey: null,           // surveys/survey-layers.json (QField uploads)
    simulation: null,       // hydrology/simulation-layers.json (Simulation window)
    layerData: new Map(),   // id -> geojson | {image, grid}
    enabled: new Map(),     // id -> bool
    drape: null,            // {mesh, canvas, ctx, texture, bounds}
    speciesGrids: null,
    surroundingVegetation: null,
    layerFilters: new Map(),  // id -> {field, values} (MCP filter_layer)
    agentLayers: new Set(),   // ids the MCP server currently drives
    toggleInputs: new Map(),  // id -> checkbox, so agent views move the UI
    layerLoads: new Map(),    // id -> in-flight ensureLayerData promise
    layerErrors: new Map(),   // id -> visible load failure message
  };

  // every drape-able / identify-able vector+raster layer (atlas + survey +
  // derived simulation layers)
  const allLayers = () => state.atlas.layers
    .concat(state.survey?.layers || [])
    .concat(state.simulation?.layers || []);

  function fail(message) {
    loadingText.textContent = message;
    loadingEl.querySelector('.spinner')?.remove();
    console.error(message);
  }

  const pretty = (s) => s.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function safeCssColor(value, fallback = '#ccc') {
    const s = String(value || '').trim();
    if (/^#[0-9a-fA-F]{3,8}$/.test(s)) return s;
    if (/^rgba?\(\s*(\d{1,3}\s*,\s*){2}\d{1,3}(\s*,\s*(0|1|0?\.\d+))?\s*\)$/.test(s)) return s;
    if (/^hsla?\(\s*\d{1,3}\s*,\s*\d{1,3}%\s*,\s*\d{1,3}%(\s*,\s*(0|1|0?\.\d+))?\s*\)$/.test(s)) return s;
    return fallback;
  }

  function safeDataAssetSrc(path) {
    if (typeof path !== 'string') return '';
    const raw = path.trim();
    if (!raw || raw.startsWith('/') || raw.startsWith('\\') || raw.startsWith('//')) return '';
    if (/^[a-zA-Z][a-zA-Z\d+.-]*:/.test(raw)) return '';
    if (/[\x00-\x1F\x7F\\?#]/.test(raw)) return '';
    const parts = raw.split('/');
    if (parts.some((part) => !part || part === '.' || part === '..')) return '';
    return `/data/${parts.map((part) => encodeURIComponent(part)).join('/')}`;
  }

  function photoHtml(photo) {
    const src = safeDataAssetSrc(photo);
    return src
      ? `<img class="info-photo" src="${escapeHtml(src)}" alt="survey photo" loading="lazy" />`
      : '';
  }

  // Stable readable color from a string (for categorical per-feature fills).
  function labelColor(label, alpha) {
    let h = 0;
    const s = String(label || '');
    for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    const hue = h % 360;
    const sat = 45 + (h >> 9) % 30;
    const light = 45 + (h >> 16) % 20;
    return `hsla(${hue},${sat}%,${light}%,${alpha})`;
  }

  async function fetchJson(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  }

  function loadImageAsset(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error(`${url}: image failed to load`));
      img.src = url;
    });
  }

  async function loadLayerData(layer, deps = {}) {
    const fetchJsonFn = deps.fetchJson || fetchJson;
    const loadImageFn = deps.loadImage || loadImageAsset;
    if (layer.type === 'raster') {
      if (!layer.image) throw new Error('Raster layer has no image asset.');
      if (!layer.grid) throw new Error('Raster layer has no identify grid.');
      const image = await loadImageFn('/data/' + layer.image);
      const grid = await fetchJsonFn('/data/' + layer.grid);
      return { image, grid };
    }
    if (!layer.file) throw new Error('Vector layer has no feature file.');
    return fetchJsonFn('/data/' + layer.file);
  }

  function layerLoadFailureMessage(layer, err) {
    const label = layer?.label || layer?.id || 'Layer';
    const reason = err?.message || String(err || 'load failed');
    return `${label} failed to load: ${reason}`;
  }

  async function main() {
    let scene;
    try {
      scene = await fetchJson('/data/scene.json');
    } catch (err) {
      return fail('Could not load scene.json — is the server running?');
    }
    state.scene = scene;

    // twin title comes from the scene (each twin names itself); the engine
    // ships a generic default
    if (scene.name) {
      document.getElementById('twin-title').textContent = scene.name;
      document.title = `${scene.name} — VEIL`;
    }
    const sub = document.getElementById('twin-subtitle');
    if (scene.subtitle) { sub.textContent = scene.subtitle; sub.hidden = false; }

    const viewer = VEILViewer.create(rootEl);
    state.viewer = viewer;
    window.__twin = { viewer, state };
    viewer.updateScenePayload?.(scene);
    state.hosted = await loadHostedStatus();
    applyHostedChrome();
    SCENE_LAYERS.forEach((layer) => viewer.setLayerVisibility(layer.id, sceneLayerDefaultVisible(layer.id)));
    try {
      await viewer.streamLoad(scene, { onLayerState() {}, onTerrainReady() {} });
    } catch (err) {
      return fail('Failed to build the 3D scene: ' + (err?.message || err));
    }
    viewer.setVegetationDensity('trees', 1);
    viewer.setVegetationDensity('shrubs', 1);
    await viewer.setTerrainRenderMode('ortho');

    // 3D building models on their footprints (part of the buildings layer).
    window.VEILBuildings3D
      ?.load(viewer, '/data/buildings/models/manifest.json')
      .then(() => {
        window.__twin.buildingEditor = window.VEILBuildingEditor?.create(viewer);
      })
      .catch((err) => console.error('building models failed:', err));

    state.atlas = await loadAtlasCatalog();
    state.atlas.layers.forEach((l) => state.enabled.set(l.id, false));
    state.survey = await loadSurveyCatalog();
    state.survey.layers.forEach((l) => state.enabled.set(l.id, false));
    state.simulation = await loadSimulationCatalog();
    state.simulation.layers.forEach((l) => state.enabled.set(l.id, false));

    state.apron = await buildApron(viewer);
    state.surroundingVegetation = await buildSurroundingVegetation(viewer, state.apron);
    initDrape(viewer);
    raiseOverlaysAboveDrape(viewer);
    await ensureEnabledLayerData(allLayers());
    redrawDrape();

    loadingEl.classList.add('hidden');

    setupTerrainModes(viewer);
    setupLayerToggles(viewer);
    setupVegetation(viewer);
    setupPicking(viewer, scene);
    setupPOV(viewer, scene);
    window.__twin.applyLayerViews = applyLayerViews;
    window.__twin.refreshAtlasLayers = refreshAtlasLayers;
    window.__twin.annotations = window.VEILAnnotations?.create(viewer, scene);
    window.__twin.chat = window.VEILChat?.create(viewer, scene);
    window.__twin.survey = window.VEILSurvey?.create(refreshSurveyLayers);
    window.__twin.simulation = window.VEILSimulation?.create({
      catalog: () => state.simulation,
      isEnabled: (id) => !!state.enabled.get(id),
      isLoading: (id) => isLayerLoading(id),
      setEnabled: async (layer, on) => {
        await setDrapeLayerEnabled(layer, on);
      },
      refresh: refreshSimulationLayers,
    });
    window.__twin.live = state.hosted?.hosted ? null : window.VEILLiveInputs?.create(viewer, scene);
    renderKey();
    loadSpeciesGrids();
  }

  async function loadHostedStatus() {
    try {
      return await fetchJson('/api/hosted/status');
    } catch (_err) {
      return { hosted: false };
    }
  }

  function applyHostedChrome() {
    if (!state.hosted?.hosted) return;
    document.querySelectorAll('[data-mode="telemetry"], [data-pane="telemetry"], [data-mode="survey"], [data-pane="survey"]')
      .forEach((el) => {
        el.hidden = true;
        el.style.display = 'none';
      });
  }

  async function loadAtlasCatalog() {
    try {
      return await fetchJson('/data/atlas/local/viewer-layers.json');
    } catch (_e) {
      return { layers: [] };
    }
  }

  async function refreshAtlasLayers(options = {}) {
    const fresh = options.catalog || await loadAtlasCatalog();
    const oldAtlasIds = new Set((state.atlas?.layers || []).map((l) => l.id));
    state.atlas = fresh;
    state.atlas.layers.forEach((layer) => {
      if (!state.enabled.has(layer.id)) state.enabled.set(layer.id, false);
      if (!oldAtlasIds.has(layer.id)) state.layerData.delete(layer.id);
    });
    buildAtlasToggles();
    const enableIds = new Set(options.enableIds || []);
    for (const layer of state.atlas.layers) {
      if (enableIds.has(layer.id)) await setDrapeLayerEnabled(layer, true);
    }
    redrawDrape();
    renderKey();
    return state.atlas;
  }

  /* ---------------- simulation layers (Simulation window — hydrology) ----- */

  async function loadSimulationCatalog() {
    try {
      return await fetchJson('/data/hydrology/simulation-layers.json');
    } catch (_e) {
      return { layers: [] }; // analyze_hydrology.py hasn't been run yet
    }
  }

  // Called by the Simulation window after a scenario run: refetch the catalog
  // (the scenario layers were just rewritten on disk), drop stale pixel data,
  // optionally enable the new layers, and repaint.
  async function refreshSimulationLayers(enableIds) {
    const fresh = await loadSimulationCatalog();
    fresh.layers.forEach((l) => {
      state.layerData.delete(l.id);
      if (enableIds && enableIds.includes(l.id)) state.enabled.set(l.id, true);
      if (!state.enabled.has(l.id)) state.enabled.set(l.id, false);
    });
    state.simulation = fresh;
    await ensureEnabledLayerData(fresh.layers);
    redrawDrape();
    renderKey();
    return fresh;
  }

  /* ---------------- survey layers (QField uploads — see docs/survey.md) ---- */

  async function loadSurveyCatalog() {
    try {
      return await fetchJson('/data/surveys/survey-layers.json');
    } catch (_e) {
      return { layers: [] }; // nothing surveyed yet
    }
  }

  // Called by the Survey panel after a successful upload: refetch the catalog
  // and the layer GeoJSON, enable the layers so the new data is visible.
  async function refreshSurveyLayers() {
    const fresh = await loadSurveyCatalog();
    fresh.layers.forEach((l) => {
      state.layerData.delete(l.id); // stale geojson — refetch on draw
      state.enabled.set(l.id, true);
    });
    state.survey = fresh;
    buildSurveyToggles();
    await ensureEnabledLayerData(fresh.layers);
    redrawDrape();
    renderKey();
  }

  function buildSurveyToggles() {
    const group = document.getElementById('survey-group');
    const host = document.getElementById('survey-toggles');
    if (!group || !host) return;
    host.replaceChildren();
    group.hidden = !state.survey.layers.length;
    state.survey.layers.forEach((layer) => {
      const row = makeToggleRow(layer.label, layer.stroke || '#ccc',
        state.enabled.get(layer.id), async (on) => {
          await setDrapeLayerEnabled(layer, on);
        });
      state.toggleInputs.set(layer.id, row.querySelector('input'));
      host.appendChild(row);
    });
  }

  async function setupVegetation(viewer) {
    let meta;
    try {
      meta = await fetchJson('/data/vegetation/metadata.json');
    } catch (_e) { return; }
    const top = (meta.communities || [])[0];
    const community = top ? top.name.replace(/ Forest.*/, '') : '';
    document.getElementById('veg-summary').innerHTML =
      `<div class="veg-stat"><span class="veg-dot" style="background:#1f4030"></span>` +
      `${meta.evergreen_pct}% evergreen &nbsp;` +
      `<span class="veg-dot" style="background:#6a8f3f"></span>${meta.deciduous_pct}% deciduous</div>` +
      `<div class="veg-note">${meta.canopy_cover_pct}% canopy · ${community}</div>`;

    const filter = document.getElementById('veg-type-filter');
    filter.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-vt]');
      if (!btn) return;
      viewer.vegetationRenderer?.setTypeFilter(btn.dataset.vt);
      state.surroundingVegetation?.renderer?.setTypeFilter(btn.dataset.vt);
      filter.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
    });
  }

  /* ---------------- terrain-draped canvas overlay ---------------- */

  function initDrape(viewer) {
    const grid = viewer.terrainGrid;
    if (!grid || !viewer.terrainMesh) return;
    const bounds = {
      minX: grid.outerMinX ?? grid.minX, maxX: grid.outerMaxX ?? grid.maxX,
      minY: grid.outerMinY ?? grid.minY, maxY: grid.outerMaxY ?? grid.maxY,
    };
    const spanX = bounds.maxX - bounds.minX;
    const spanY = bounds.maxY - bounds.minY;
    const W = 1024;
    const H = Math.round(W * (spanY / spanX));
    const canvas = document.createElement('canvas');
    canvas.width = W;
    canvas.height = H;
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    // Same pattern as the viewer's own ortho drape: share the terrain geometry,
    // float the material with polygon offset so it colors the surface pixels.
    const material = new THREE.MeshBasicMaterial({
      map: texture, transparent: true, depthWrite: false,
      polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -3,
    });
    const mesh = new THREE.Mesh(viewer.terrainMesh.geometry, material);
    mesh.renderOrder = (viewer.terrainMesh.renderOrder || 0) + 2;
    viewer.scene.add(mesh);
    state.drape = { mesh, canvas, ctx: canvas.getContext('2d'), texture, bounds, W, H };
  }

  // Real USGS 3DEP DEM that fills the square grid beyond the parcel AOI (no
  // vegetation, no atlas drape). Rendered as a separate mesh that follows the
  // terrain-surface mode (aerial / false color / hillshade / elevation) so the
  // surrounding area carries the same imagery as the parcel, and can be toggled.
  const APRON_IMAGERY = {
    ortho: '/data/imagery/naip_rgb.png',
    false_color: '/data/imagery/false_color.png',
    hillshade: '/data/imagery/hillshade_surrounding.png',
  };

  async function buildApron(viewer) {
    if (!window.VEILTerrain?.buildTerrainMesh) return null;
    let grid;
    try {
      grid = await fetchJson('/data/terrain/grid.apron.json');
    } catch (_e) {
      return null;
    }
    const built = window.VEILTerrain.buildTerrainMesh(grid);
    const mesh = built.mesh;
    mesh.renderOrder = -1;
    mesh.visible = false;
    viewer.scene.add(mesh);
    const apron = {
      mesh,
      grid,
      elevationMaterial: built.elevationMaterial,
      materials: {},
      loader: new THREE.TextureLoader(),
    };
    applyApronMode(apron, 'ortho');
    return apron;
  }

  async function buildSurroundingVegetation(viewer, apron) {
    if (!apron?.grid || !window.VEILVegetation?.create) return null;
    const vegetation = state.scene?.vegetation || {};
    const treeUrl = vegetation.surrounding_tree_instances_url ||
      '/data/vegetation/surrounding_tree_instances.json';
    const shrubUrl = vegetation.surrounding_shrub_points_url ||
      '/data/vegetation/surrounding_shrub_points.json';
    try {
      const [treeInstances, shrubPoints] = await Promise.all([
        fetchJson(treeUrl),
        fetchJson(shrubUrl),
      ]);
      const renderer = window.VEILVegetation.create(viewer.scene);
      renderer.load({ treeInstances, shrubPoints, grid: apron.grid });
      renderer.setDensity('trees', 1);
      renderer.setDensity('shrubs', 1);
      renderer.setVisible(false);
      return { renderer, treeInstances, shrubPoints };
    } catch (err) {
      console.error('surrounding vegetation failed:', err);
      return null;
    }
  }

  function applyApronMode(apron, mode) {
    if (!apron) return;
    if (mode === 'elevation') {
      apron.mesh.material = apron.elevationMaterial;
      return;
    }
    const url = APRON_IMAGERY[mode];
    if (!url) return;
    if (!apron.materials[mode]) {
      const tex = apron.loader.load(url);
      tex.colorSpace = THREE.SRGBColorSpace;
      apron.materials[mode] = new THREE.MeshStandardMaterial({ map: tex, roughness: 1, metalness: 0.02 });
    }
    apron.mesh.material = apron.materials[mode];
  }

  // The atlas drape colors the terrain surface; parcel lot lines are created by
  // the ported viewer at renderOrder 0, which would let the drape paint over
  // them. Lift every vector overlay above the drape so lot/building lines always
  // draw on top (buildings stay above parcels, as before).
  function raiseOverlaysAboveDrape(viewer) {
    const base = (state.drape?.mesh.renderOrder ?? 2) + 1;
    const group = viewer.parcelGroup;
    if (group) {
      group.renderOrder = base;
      group.traverse((o) => { if (o.isLine || o.isLineLoop || o.isLineSegments) o.renderOrder = base; });
    }
  }

  function toCanvas(x, y) {
    const d = state.drape;
    return [
      ((x - d.bounds.minX) / (d.bounds.maxX - d.bounds.minX)) * d.W,
      ((d.bounds.maxY - y) / (d.bounds.maxY - d.bounds.minY)) * d.H,
    ];
  }

  async function ensureLayerData(layer) {
    if (state.layerData.has(layer.id)) return state.layerData.get(layer.id);
    if (state.layerLoads.has(layer.id)) return state.layerLoads.get(layer.id);
    const request = (async () => {
      setLayerLoading(layer.id, true);
      state.layerErrors.delete(layer.id);
      try {
        const data = await loadLayerData(layer);
        state.layerData.set(layer.id, data);
        state.layerErrors.delete(layer.id);
        return data;
      } catch (err) {
        const message = layerLoadFailureMessage(layer, err);
        state.layerData.delete(layer.id);
        state.enabled.set(layer.id, false);
        setToggleChecked(layer.id, false);
        state.layerErrors.set(layer.id, message);
        console.error(message, err);
        throw err;
      } finally {
        state.layerLoads.delete(layer.id);
        setLayerLoading(layer.id, false);
      }
    })();
    state.layerLoads.set(layer.id, request);
    return request;
  }

  async function ensureEnabledLayerData(layers) {
    const active = layers.filter((l) => state.enabled.get(l.id));
    const results = await Promise.allSettled(active.map((l) => ensureLayerData(l)));
    return results.every((r) => r.status === 'fulfilled');
  }

  function drawPolygonPath(ctx, rings) {
    ctx.beginPath();
    rings.forEach((ring) => {
      ring.forEach(([x, y], i) => {
        const [cx, cy] = toCanvas(x, y);
        if (i === 0) ctx.moveTo(cx, cy); else ctx.lineTo(cx, cy);
      });
      ctx.closePath();
    });
  }

  function eachPolygon(geometry, cb) {
    if (!geometry) return;
    if (geometry.type === 'Polygon') cb(geometry.coordinates);
    else if (geometry.type === 'MultiPolygon') geometry.coordinates.forEach(cb);
  }

  function eachLine(geometry, cb) {
    if (!geometry) return;
    if (geometry.type === 'LineString') cb(geometry.coordinates);
    else if (geometry.type === 'MultiLineString') geometry.coordinates.forEach(cb);
    // boundary-style polygons render as outlines via the line path too
    else if (geometry.type === 'Polygon') geometry.coordinates.forEach(cb);
    else if (geometry.type === 'MultiPolygon') geometry.coordinates.forEach((p) => p.forEach(cb));
  }

  function eachPoint(geometry, cb) {
    if (!geometry) return;
    if (geometry.type === 'Point') cb(geometry.coordinates);
    else if (geometry.type === 'MultiPoint') geometry.coordinates.forEach(cb);
  }

  // --- MCP layer filters: keep only the features/cells the agent selected ---

  function featureMatchesFilter(f, filter) {
    const field = filter.field || '__label';
    const v = (f.properties || {})[field];
    if (v === undefined || v === null) return false;
    const have = String(v).toLowerCase();
    return filter.values.some((w) => String(w).toLowerCase() === have);
  }

  // Re-draw a categorical raster from its value grid, painting only the cells
  // whose legend class the agent picked (matched by name or numeric value).
  function drawFilteredRaster(ctx, layer, data, filter) {
    const grid = data.grid;
    if (!grid || !grid.values) return; // no value grid -> nothing to filter on
    const want = new Set(filter.values.map((s) => String(s).toLowerCase()));
    const legend = grid.legend || {};
    const keep = new Map(); // value -> [r,g,b]
    Object.entries(legend).forEach(([val, meta]) => {
      const name = String((meta && meta.name) || '').toLowerCase();
      if (want.has(name) || want.has(String(val).toLowerCase())) {
        keep.set(Number(val), (meta && meta.color) || [255, 140, 26]);
      }
    });
    if (!keep.size) return;
    const [minx, miny, maxx, maxy] = layer.bounds_local;
    const [x0, y0] = toCanvas(minx, maxy);
    const [x1, y1] = toCanvas(maxx, miny);
    const cw = (x1 - x0) / grid.width;
    const ch = (y1 - y0) / grid.height;
    ctx.save();
    ctx.globalAlpha = 0.85;
    for (let r = 0; r < grid.height; r += 1) {
      const row = grid.values[r] || [];
      for (let c = 0; c < grid.width; c += 1) {
        const col = keep.get(row[c]);
        if (!col) continue;
        ctx.fillStyle = `rgb(${col[0]},${col[1]},${col[2]})`;
        ctx.fillRect(x0 + c * cw, y0 + r * ch, cw + 0.6, ch + 0.6);
      }
    }
    ctx.restore();
  }

  // Paint an orange habitat mask: cells where any selected GAP species has
  // modeled habitat (the per-species bitmask grids loaded at boot).
  function drawSpeciesMask(ctx, filter) {
    const sg = state.speciesGrids;
    if (!sg || !sg.species) return false;
    const want = new Set(filter.values.map((s) => String(s).toLowerCase()));
    const picked = Object.values(sg.species)
      .filter((s) => want.has(String(s.common_name).toLowerCase()));
    if (!picked.length) return false;
    const [minx, miny, maxx, maxy] = sg.bounds_local;
    const [x0, y0] = toCanvas(minx, maxy);
    const [x1, y1] = toCanvas(maxx, miny);
    const cw = (x1 - x0) / sg.width;
    const ch = (y1 - y0) / sg.height;
    ctx.save();
    ctx.globalAlpha = 0.55;
    ctx.fillStyle = '#ff8c1a';
    for (let r = 0; r < sg.height; r += 1) {
      for (let c = 0; c < sg.width; c += 1) {
        const present = picked.some((s) => s.rows[r] && s.rows[r][c] === '1');
        if (present) ctx.fillRect(x0 + c * cw, y0 + r * ch, cw + 0.6, ch + 0.6);
      }
    }
    ctx.restore();
    return true;
  }

  function redrawDrape() {
    const d = state.drape;
    if (!d) return;
    d.ctx.clearRect(0, 0, d.W, d.H);
    const order = { raster: 0, polygon: 1, line: 2, point: 3 };
    const layers = allLayers()
      .filter((l) => state.enabled.get(l.id) && state.layerData.has(l.id))
      .sort((a, b) => (order[a.type] ?? 9) - (order[b.type] ?? 9));

    layers.forEach((layer) => {
      const data = state.layerData.get(layer.id);
      const ctx = d.ctx;
      const filter = state.layerFilters.get(layer.id);
      if (layer.type === 'raster') {
        // A species filter on the GAP grid paints a habitat mask; any other
        // filter re-renders the value grid keeping only the chosen classes;
        // unfiltered, the pre-colored ortho image drapes as before.
        if (filter && filter.field === 'species' && drawSpeciesMask(ctx, filter)) return;
        if (filter) { drawFilteredRaster(ctx, layer, data, filter); return; }
        const [minx, miny, maxx, maxy] = layer.bounds_local;
        const [x0, y0] = toCanvas(minx, maxy);
        const [x1, y1] = toCanvas(maxx, miny);
        ctx.save();
        ctx.imageSmoothingEnabled = false;
        ctx.globalAlpha = 0.8;
        ctx.drawImage(data.image, x0, y0, x1 - x0, y1 - y0);
        ctx.restore();
        return;
      }
      const perFeature = COLOR_BY_LABEL.has(layer.id);
      (data.features || []).forEach((f) => {
        if (filter && !featureMatchesFilter(f, filter)) return;
        const label = f.properties?.__label;
        if (layer.type === 'polygon') {
          eachPolygon(f.geometry, (rings) => {
            drawPolygonPath(ctx, rings);
            ctx.fillStyle = perFeature ? labelColor(label, 0.45) : layer.fill;
            ctx.fill('evenodd');
            ctx.strokeStyle = perFeature ? labelColor(label, 0.95) : layer.stroke;
            ctx.lineWidth = 2;
            ctx.stroke();
          });
        } else if (layer.type === 'point') {
          ctx.fillStyle = layer.fill;
          ctx.strokeStyle = layer.stroke;
          ctx.lineWidth = 2;
          eachPoint(f.geometry, ([x, y]) => {
            const [cx, cy] = toCanvas(x, y);
            ctx.beginPath();
            ctx.arc(cx, cy, 5, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
          });
        } else {
          ctx.strokeStyle = layer.stroke;
          ctx.lineWidth = 3;
          eachLine(f.geometry, (line) => {
            ctx.beginPath();
            line.forEach(([x, y], i) => {
              const [cx, cy] = toCanvas(x, y);
              if (i === 0) ctx.moveTo(cx, cy); else ctx.lineTo(cx, cy);
            });
            ctx.stroke();
          });
        }
      });
    });
    d.texture.needsUpdate = true;
  }

  /* ---------------- UI: terrain modes, toggles, density ---------------- */

  function setupTerrainModes(viewer) {
    const row = document.getElementById('terrain-modes');
    row.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-mode]');
      if (!btn) return;
      const ok = await viewer.setTerrainRenderMode(btn.dataset.mode);
      if (ok === false) return;
      applyApronMode(state.apron, btn.dataset.mode); // surrounding terrain follows the mode
      row.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
    });
  }

  function makeToggleRow(label, color, checked, onChange) {
    const row = document.createElement('label');
    row.className = 'toggle-row';
    row.innerHTML =
      `<input type="checkbox" ${checked ? 'checked' : ''} />` +
      `<span class="swatch" style="background:${safeCssColor(color)}"></span>` +
      `<span class="toggle-label">${escapeHtml(label)}</span>`;
    row.querySelector('input').addEventListener('change', (e) => onChange(e.target.checked));
    return row;
  }

  function isLayerLoading(id) {
    return state.layerLoads.has(id);
  }

  function setupLayerToggles(viewer) {
    const sceneHost = document.getElementById('layer-toggles');
    SCENE_LAYERS.forEach((layer) => {
      const checked = sceneLayerDefaultVisible(layer.id);
      sceneHost.appendChild(makeToggleRow(layer.label, layer.color, checked,
        (on) => viewer.setLayerVisibility(layer.id, on)));
      viewer.setLayerVisibility(layer.id, checked);
    });
    if (state.apron) {
      const row = makeToggleRow('Surrounding area', '#9c9484', false,
        (on) => { state.apron.mesh.visible = on; });
      row.title = 'Real USGS 3DEP terrain beyond the parcel AOI';
      sceneHost.appendChild(row);
    }
    if (state.surroundingVegetation?.renderer) {
      const row = makeToggleRow('Surrounding vegetation', '#4d8f58', false,
        (on) => state.surroundingVegetation.renderer.setVisible(on));
      row.title = 'Estimated trees and shrubs on the surrounding terrain apron';
      sceneHost.appendChild(row);
    }

    buildAtlasToggles();

    buildSurveyToggles();
  }

  function buildAtlasToggles() {
    const atlasHost = document.getElementById('atlas-toggles');
    if (!atlasHost) return;
    atlasHost.innerHTML = '';
    for (const [id, input] of state.toggleInputs.entries()) {
      if ((state.atlas.layers || []).some((layer) => layer.id === id)) {
        state.toggleInputs.delete(id);
        input.closest?.('.toggle-row')?.remove();
      }
    }
    state.atlas.layers.forEach((layer) => {
      const swatch = layer.type === 'raster' ? '#888' : (layer.stroke || '#ccc');
      const row = makeToggleRow(layer.label, swatch, state.enabled.get(layer.id),
        async (on) => {
          // a manual toggle takes the layer back from the agent (and drops any
          // agent filter on it); the next MCP directive can reclaim it.
          state.agentLayers.delete(layer.id);
          state.layerFilters.delete(layer.id);
          await setDrapeLayerEnabled(layer, on);
        });
      state.toggleInputs.set(layer.id, row.querySelector('input'));
      atlasHost.appendChild(row);
    });
  }

  /* ---- MCP layer-view overrides (set_layer_visibility / filter_layer) ----
     annotations.js polls data/annotations.json and hands us its layer_views
     array whenever the file changes; we move the matching toggles and drape
     filters to match. Edge-triggered like the drawings: between directive
     changes the user's manual toggles win. */
  const layerById = (id) => allLayers().find((l) => l.id === id);

  function setToggleChecked(id, checked) {
    const el = state.toggleInputs.get(id);
    if (el) el.checked = checked;
  }

  function setLayerLoading(id, loading) {
    const el = state.toggleInputs.get(id);
    if (!el) return;
    el.disabled = loading;
    el.closest?.('.toggle-row')?.classList.toggle('loading', loading);
  }

  async function setDrapeLayerEnabled(layer, on) {
    if (isLayerLoading(layer.id)) {
      setToggleChecked(layer.id, !!state.enabled.get(layer.id));
      return false;
    }
    state.enabled.set(layer.id, on);
    setToggleChecked(layer.id, on);
    if (!on) {
      redrawDrape();
      renderKey();
      return true;
    }
    try {
      await ensureLayerData(layer);
    } catch (_err) {
      // ensureLayerData recorded the visible failure and rolled the toggle back.
      redrawDrape();
      renderKey();
      return false;
    }
    redrawDrape();
    renderKey();
    return true;
  }

  async function applyOneLayerView(v) {
    const layer = layerById(v.layer_id);
    if (!layer) return; // unknown layer id — ignore quietly
    state.agentLayers.add(v.layer_id);
    const visible = v.visible !== false;
    state.enabled.set(v.layer_id, visible);
    setToggleChecked(v.layer_id, visible);
    if (v.filter && Array.isArray(v.filter.values) && v.filter.values.length) {
      state.layerFilters.set(v.layer_id, v.filter);
    } else {
      state.layerFilters.delete(v.layer_id);
    }
    if (visible) await ensureLayerData(layer);
  }

  async function applyLayerViews(views) {
    const list = Array.isArray(views) ? views : [];
    const wanted = new Set(list.map((v) => v.layer_id));
    // release any layer the agent drove before but no longer mentions
    [...state.agentLayers].forEach((id) => {
      if (wanted.has(id)) return;
      state.agentLayers.delete(id);
      state.layerFilters.delete(id);
      if (layerById(id)) { state.enabled.set(id, false); setToggleChecked(id, false); }
    });
    await Promise.allSettled(list.map(applyOneLayerView));
    redrawDrape();
    renderKey();
  }

  /* ---------------- map key + click-to-identify ---------------- */

  function renderKey() {
    const host = document.getElementById('key-list');
    host.replaceChildren();
    const active = allLayers().filter((l) => state.enabled.get(l.id));
    const errors = allLayers().filter((l) => state.layerErrors.has(l.id));
    document.getElementById('key-empty').hidden = active.length > 0 || errors.length > 0;
    active.forEach((layer) => {
      const item = document.createElement('div');
      item.className = 'atlas-item';
      const sw = layer.type === 'raster' ? '#888' : safeCssColor(layer.stroke || '#ccc');
      const filter = state.layerFilters.get(layer.id);
      const feat = filter
        ? `only ${filter.values.slice(0, 3).join(', ')}${filter.values.length > 3 ? '…' : ''}`
        : (layer.type === 'raster' ? 'raster' : `${layer.feature_count} feat`);
      item.innerHTML =
        `<span><span class="swatch" style="background:${sw};display:inline-block;margin-right:7px"></span>${escapeHtml(layer.label)}</span>` +
        `<span class="feat">${escapeHtml(feat)}</span>`;
      host.appendChild(item);
      // simulation layers carry a description — surface it in the key so the
      // legend explains what the colors mean, not just the layer name
      if (layer.description) {
        const d = document.createElement('p');
        d.className = 'atlas-desc';
        d.textContent = layer.scenario
          ? `${layer.description} (scenario: ${layer.scenario})`
          : layer.description;
        host.appendChild(d);
      }
    });
    errors.forEach((layer) => {
      const item = document.createElement('div');
      item.className = 'atlas-item atlas-error';
      item.innerHTML =
        `<span>${escapeHtml(layer.label || layer.id)}</span>` +
        '<span class="feat">failed</span>';
      item.title = state.layerErrors.get(layer.id);
      host.appendChild(item);
    });
  }

  async function loadSpeciesGrids() {
    if (!state.atlas.gap_species_grids) return;
    try {
      state.speciesGrids = await fetchJson('/data/' + state.atlas.gap_species_grids);
    } catch (_e) { /* species-at-point disabled */ }
  }

  function pointInRings(rings, x, y) {
    // even-odd across all rings (holes handled naturally)
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

  function pointInGeometry(geometry, x, y) {
    let hit = false;
    eachPolygon(geometry, (rings) => { if (pointInRings(rings, x, y)) hit = true; });
    return hit;
  }

  function distToLine(geometry, x, y) {
    let best = Infinity;
    eachLine(geometry, (line) => {
      for (let i = 1; i < line.length; i += 1) {
        const [x1, y1] = line[i - 1];
        const [x2, y2] = line[i];
        const dx = x2 - x1; const dy = y2 - y1;
        const len2 = dx * dx + dy * dy || 1e-9;
        const t = Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / len2));
        const px = x1 + t * dx; const py = y1 + t * dy;
        best = Math.min(best, Math.hypot(x - px, y - py));
      }
    });
    return best;
  }

  function sampleGrid(grid, bounds, x, y) {
    const [minx, miny, maxx, maxy] = bounds;
    if (x < minx || x > maxx || y < miny || y > maxy) return null;
    const col = Math.min(grid.width - 1, Math.floor(((x - minx) / (maxx - minx)) * grid.width));
    const row = Math.min(grid.height - 1, Math.floor(((maxy - y) / (maxy - miny)) * grid.height));
    return { row, col, value: grid.values[row][col] };
  }

  function formatRasterValue(value, grid = {}, layer = {}) {
    const unit = grid.value_unit || layer.value_unit;
    if (unit && unit !== 'year') return `${value} ${unit}`;
    return String(value);
  }

  const HIDE_PROPS = new Set(['__label', 'OBJECTID', 'Shape_Length', 'Shape_Area',
    'Shape__Area', 'Shape__Length', 'SHAPE.AREA', 'SHAPE.LEN', 'SPATIALVER', 'GlobalID',
    'photo']); // photo paths render as an <img>, not a property row

  function propRows(props) {
    return Object.entries(props || {})
      .filter(([k, v]) => !HIDE_PROPS.has(k) && v !== undefined && v !== null && v !== '' && v !== ' ')
      .slice(0, 14)
      .map(([k, v]) => `<div class="info-row"><span class="info-k">${escapeHtml(pretty(k))}</span><span class="info-v">${escapeHtml(v)}</span></div>`)
      .join('');
  }

  function speciesCardHtml(names) {
    if (!names.length) return '';
    return `<div class="info-card"><p class="info-title">Species habitat here (${names.length})</p>` +
      `<p class="info-species">${names.map(escapeHtml).join(' · ')}</p></div>`;
  }

  function notifyInspect(source) {
    if (!document?.dispatchEvent) return;
    try {
      const detail = { source };
      let event = null;
      if (typeof CustomEvent === 'function') {
        event = new CustomEvent('veil:inspect', { detail });
      } else if (document.createEvent) {
        event = document.createEvent('CustomEvent');
        event.initCustomEvent('veil:inspect', false, false, detail);
      }
      if (event) document.dispatchEvent(event);
    } catch (_err) {
      // Inspector reveal is chrome-only; identify results are already rendered.
    }
  }

  function identifyResultsHtml(results, speciesHtml, simHtml) {
    return (simHtml ? `<div class="info-card sim-info-card">${simHtml}</div>` : '') +
      results.map((r) => {
        const layerLabel = r.layer?.label || 'Layer';
        const title = r.title || layerLabel;
        const bodyHtml = r.bodyHtml !== undefined ? r.bodyHtml : escapeHtml(r.html || '');
        return `<div class="info-card">
         <p class="info-layer">${escapeHtml(layerLabel)}</p>
         <p class="info-title">${escapeHtml(title)}</p>
         ${bodyHtml}
       </div>`;
      }).join('') + speciesHtml;
  }

  function distToPoint(geometry, x, y) {
    let best = Infinity;
    eachPoint(geometry, ([px, py]) => {
      best = Math.min(best, Math.hypot(x - px, y - py));
    });
    return best;
  }

  const IDENTIFY_HIT_RADIUS = {
    fallbackMeters: 8,
    minMeters: 2.5,
    maxMeters: 18,
    targetPixels: 10,
  };

  function clampNumber(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function identifyHitRadiusMetersFromView(view, opts = {}) {
    const fallback = Number(opts.fallbackMeters ?? IDENTIFY_HIT_RADIUS.fallbackMeters);
    const minMeters = Number(opts.minMeters ?? IDENTIFY_HIT_RADIUS.minMeters);
    const maxMeters = Number(opts.maxMeters ?? IDENTIFY_HIT_RADIUS.maxMeters);
    const targetPixels = Number(opts.targetPixels ?? IDENTIFY_HIT_RADIUS.targetPixels);
    const distance = Number(view?.cameraDistanceMeters);
    const viewportHeight = Number(view?.viewportHeightPx);
    const fovDegrees = Number(view?.fovDegrees);
    if (![distance, viewportHeight, fovDegrees, fallback, minMeters, maxMeters, targetPixels].every(Number.isFinite)) {
      return fallback;
    }
    if (distance <= 0 || viewportHeight <= 0 || fovDegrees <= 0 || targetPixels <= 0 || minMeters > maxMeters) {
      return fallback;
    }
    const visibleHeightMeters = 2 * distance * Math.tan((fovDegrees * Math.PI) / 360);
    const metersPerPixel = visibleHeightMeters / viewportHeight;
    return clampNumber(metersPerPixel * targetPixels, minMeters, maxMeters);
  }

  function identifyHitRadiusMeters(viewer) {
    const camera = viewer?.camera;
    const controls = viewer?.controls;
    const canvas = viewer?.renderer?.domElement;
    const distance = camera?.position?.distanceTo && controls?.target
      ? camera.position.distanceTo(controls.target)
      : NaN;
    const viewportHeight = canvas?.clientHeight || canvas?.height || window.innerHeight;
    return identifyHitRadiusMetersFromView({
      cameraDistanceMeters: distance,
      viewportHeightPx: viewportHeight,
      fovDegrees: camera?.fov,
    });
  }

  function identify(x, y, hitRadiusMeters = IDENTIFY_HIT_RADIUS.fallbackMeters) {
    const results = [];
    const simSamples = []; // simulation layers speak in sentences, not rows
    const hitRadius = Number.isFinite(hitRadiusMeters)
      ? clampNumber(hitRadiusMeters, IDENTIFY_HIT_RADIUS.minMeters, IDENTIFY_HIT_RADIUS.maxMeters)
      : IDENTIFY_HIT_RADIUS.fallbackMeters;
    allLayers().forEach((layer) => {
      if (!state.enabled.get(layer.id)) return;
      const data = state.layerData.get(layer.id);
      if (!data) return;
      if (layer.type === 'raster') {
        const grid = data.grid;
        if (!grid) return;
        const s = sampleGrid(grid, layer.bounds_local, x, y);
        if (!s || s.value === null || s.value === grid.nodata) return;
        // simulation layers get one synthesized natural-language card
        // (simulation.js interpretAt) instead of a bare number row
        if (layer.group === 'hydrology' || layer.group === 'scenario') {
          simSamples.push({ layer, grid, value: s.value });
          return;
        }
        const leg = grid.legend && grid.legend[String(s.value)];
        results.push({
          layer,
          title: leg ? leg.name : formatRasterValue(s.value, grid, layer),
          bodyHtml: '',
        });
        return;
      }
      (data.features || []).forEach((f) => {
        const g = f.geometry;
        if (!g) return;
        const isHit = layer.type === 'polygon' ? pointInGeometry(g, x, y)
          : layer.type === 'point' ? distToPoint(g, x, y) < hitRadius
            : distToLine(g, x, y) < hitRadius;
        if (isHit) {
          const photo = f.properties?.photo;
          results.push({
            layer,
            title: f.properties?.__label || layer.label,
            bodyHtml: propRows(f.properties) + photoHtml(photo),
          });
        }
      });
    });

    // species with modeled habitat at this exact spot
    let speciesHtml = '';
    if (state.speciesGrids && state.enabled.get('gap_species_richness')) {
      const sg = state.speciesGrids;
      const [minx, miny, maxx, maxy] = sg.bounds_local;
      if (x >= minx && x <= maxx && y >= miny && y <= maxy) {
        const col = Math.min(sg.width - 1, Math.floor(((x - minx) / (maxx - minx)) * sg.width));
        const row = Math.min(sg.height - 1, Math.floor(((maxy - y) / (maxy - miny)) * sg.height));
        const names = Object.values(sg.species)
          .filter((s) => s.rows[row] && s.rows[row][col] === '1')
          .map((s) => s.common_name)
          .sort();
        if (names.length) speciesHtml = speciesCardHtml(names);
      }
    }
    // one combined plain-English card for everything the simulation layers
    // know about this spot (water, soil, scenario)
    const simHtml = simSamples.length
      ? (window.__twin?.simulation?.interpretAt?.(x, y, simSamples) || '')
      : '';
    return { results, speciesHtml, simHtml };
  }

  let warnedMissingIdentifyHost = false;

  function renderIdentify(x, y, hitRadiusMeters) {
    const host = document.getElementById('identify-results');
    if (!host) {
      if (!warnedMissingIdentifyHost) {
        console.warn('VEIL identify results are unavailable; missing optional element: identify-results.');
        warnedMissingIdentifyHost = true;
      }
      return;
    }
    const { results, speciesHtml, simHtml } = identify(x, y, hitRadiusMeters);
    if (!results.length && !speciesHtml && !simHtml) {
      host.innerHTML = '<p class="readout-hint">No active layer has a feature at that spot.</p>';
      notifyInspect('identify');
      return;
    }
    host.innerHTML = identifyResultsHtml(results, speciesHtml, simHtml);
    notifyInspect('identify');
  }

  /* ---------------- picking: GPS readout + identify ---------------- */

  const PICK_READOUT_SELECTORS = {
    hintEl: 'readout-hint',
    gridEl: 'readout-grid',
    actionsEl: 'readout-actions',
    latlonEl: 'r-latlon',
    elevEl: 'r-elev',
    utmEl: 'r-utm',
    gmaps: 'gmaps-link',
    copyBtn: 'copy-coord',
  };

  function getPickReadoutElements(doc = document) {
    return Object.fromEntries(
      Object.entries(PICK_READOUT_SELECTORS).map(([key, id]) => [key, doc.getElementById(id)])
    );
  }

  function missingPickReadoutIds(readout) {
    return Object.entries(PICK_READOUT_SELECTORS)
      .filter(([key]) => !readout[key])
      .map(([, id]) => id);
  }

  function formatPickReadout(g) {
    const lat = g.lat.toFixed(6);
    const lon = g.lon.toFixed(6);
    return {
      lat,
      lon,
      latlonText: `${lat}, ${lon}`,
      elevText: `${g.elevation_m.toFixed(1)} m  (${Math.round(g.elevation_m * 3.28084)} ft)`,
      utmText: `${g.easting.toFixed(1)} E, ${g.northing.toFixed(1)} N`,
      gmapsHref: `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`,
      copyCoord: `${lat},${lon}`,
    };
  }

  function updatePickReadout(readout, g) {
    const formatted = formatPickReadout(g);
    if (readout.hintEl) readout.hintEl.hidden = true;
    if (readout.gridEl) readout.gridEl.hidden = false;
    if (readout.actionsEl) readout.actionsEl.hidden = false;
    if (readout.latlonEl) readout.latlonEl.textContent = formatted.latlonText;
    if (readout.elevEl) readout.elevEl.textContent = formatted.elevText;
    if (readout.utmEl) readout.utmEl.textContent = formatted.utmText;
    if (readout.gmaps) readout.gmaps.href = formatted.gmapsHref;
    if (readout.copyBtn) readout.copyBtn.dataset.coord = formatted.copyCoord;
    return formatted;
  }

  /* ---------------- first-person POV explorer ---------------- */

  async function setupPOV(viewer, scene) {
    const btn = document.getElementById('pov-toggle');
    if (!btn || !window.VEILPOV) return;

    const pov = window.VEILPOV.create(viewer, scene, {
      apron: state.apron,
      apronGrid: state.apron?.grid || null,
      surroundingVeg: state.surroundingVegetation,
      onStateChange: (label) => {
        btn.textContent = label;
        btn.classList.toggle('active', pov.active || pov.placing);
      },
    });
    window.__twin.pov = pov;

    // building footprints become walls you can't walk through
    try {
      const footprints = viewer.overlayData?.buildings?.features?.length
        ? viewer.overlayData.buildings
        : await fetchJson('/data/buildings/footprints.geojson');
      pov.setFootprints(footprints);
    } catch (_e) { /* no buildings — open ground */ }

    // real animated water (streams + pond + ponded cells) for the walk
    try {
      const [features, ponding] = await Promise.all([
        fetchJson('/data/hydrology/features.geojson').catch(() => null),
        fetchJson('/data/hydrology/local/ponding.grid.json').catch(() => null),
      ]);
      if (features || ponding) pov.setWaterSources({ features, ponding });
    } catch (_e) { /* no hydrology built — walk stays dry */ }

    btn.addEventListener('click', () => pov.enterPlacement());
  }

  function setupPicking(viewer, scene) {
    const grid = viewer.terrainGrid || {};
    const georef = VEILGeoref.createSceneGeoref(scene.origin_utm, grid.minElevation);
    const canvas = viewer.renderer?.domElement;
    if (!canvas) return;

    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    let marker = null;
    let downAt = null;

    const readout = getPickReadoutElements();
    const missingReadouts = missingPickReadoutIds(readout);
    if (missingReadouts.length) {
      console.warn(`VEIL coordinate readout is partially unavailable; missing optional element(s): ${missingReadouts.join(', ')}.`);
    }
    const { copyBtn } = readout;

    function placeMarker(point) {
      if (!marker) {
        marker = new THREE.Mesh(
          new THREE.SphereGeometry(2.2, 16, 12),
          new THREE.MeshBasicMaterial({ color: 0x6fcf97 })
        );
        marker.renderOrder = 999;
        viewer.scene.add(marker);
      }
      marker.position.copy(point);
    }

    function pick(clientX, clientY) {
      if (window.__twin?.live?.pickAtScreen?.(clientX, clientY)) return;
      const rect = canvas.getBoundingClientRect();
      ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, viewer.camera);
      const hit = raycaster.intersectObject(viewer.terrainMesh, false)[0];
      if (!hit) return;
      const g = georef.worldToGeo(hit.point.x, hit.point.y, hit.point.z);
      placeMarker(hit.point);
      updatePickReadout(readout, g);

      renderIdentify(hit.point.x, -hit.point.z, identifyHitRadiusMeters(viewer));
      window.__twin?.live?.selectNear?.(hit.point.x, -hit.point.z);
    }

    canvas.addEventListener('pointerdown', (e) => { downAt = { x: e.clientX, y: e.clientY }; });
    canvas.addEventListener('pointerup', (e) => {
      if (!downAt) return;
      const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
      downAt = null;
      // while the chat panel is drawing a region / picking a point, those
      // clicks belong to it (chat.js), not the GPS readout; same for the POV
      // explorer's drop-in click / locked first-person session
      if (window.__twin?.chat?.state?.mode) return;
      if (window.__twin?.pov?.isBusy?.()) return;
      if (moved < 5) pick(e.clientX, e.clientY);
    });

    if (copyBtn) {
      copyBtn.addEventListener('click', async () => {
        const coord = copyBtn.dataset.coord;
        if (!coord) return;
        try {
          if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable.');
          await navigator.clipboard.writeText(coord);
          copyBtn.textContent = 'Copied ✓';
          copyBtn.title = 'Coordinates copied to clipboard';
        } catch (err) {
          copyBtn.textContent = 'Copy failed';
          copyBtn.title = err?.message || 'Clipboard write was rejected.';
        }
        setTimeout(() => {
          copyBtn.textContent = 'Copy lat,lon';
          copyBtn.title = 'Copy selected latitude and longitude';
        }, 1400);
      });
    }
  }

  if (window.__VEIL_APP_TEST__) {
    window.VEILApp = {
      _test: {
        escapeHtml,
        identifyResultsHtml,
        photoHtml,
        propRows,
        safeCssColor,
        safeDataAssetSrc,
        sceneLayerDefaultVisible,
        speciesCardHtml,
        layerLoadFailureMessage,
        loadLayerData,
        notifyInspect,
        formatPickReadout,
        identifyHitRadiusMetersFromView,
        missingPickReadoutIds,
        updatePickReadout,
      },
    };
    return;
  }

  main();
})();
