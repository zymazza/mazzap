/* VEIL Plan: branchable terrain/vegetation editing over an immutable twin. */
(function attachPlan(global) {
  'use strict';

  const { THREE } = global;
  const TERRAIN_ACCUMULATION_INTERVAL_MS = 100;
  const TERRAIN_ACCUMULATION_LAYER_MS = 1000;
  const MAX_TERRAIN_ACCUMULATION_STAMPS = 10000;

  function isTerrainBrush(tool) {
    return tool === 'cut' || tool === 'fill';
  }

  function brushPointerBlocked(state) {
    return Boolean(state?.busy);
  }

  async function drainOrderedEditQueue(queue, callbacks) {
    while (queue.length) {
      const item = queue[0];
      const current = callbacks.current();
      if (!current) throw new Error('the active plan closed before its edit could save');
      const previous = clone(current.revision?.edits || []);
      const payload = await callbacks.save({
        current,
        item,
        nextEdits: [...previous, item.edit],
      });
      callbacks.saved({ item, payload, previous });
      queue.shift();
    }
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function esc(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function fmt(value, digits = 1) {
    const n = Number(value);
    return Number.isFinite(n)
      ? n.toLocaleString(undefined, { maximumFractionDigits: digits })
      : '—';
  }

  function editId() {
    const random = global.crypto?.randomUUID?.().replace(/-/g, '')
      || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
    return `edit_${random.slice(0, 24)}`;
  }

  function hashUnit(value) {
    const text = String(value);
    let hash = 2166136261;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return ((hash >>> 0) % 1000000) / 1000000;
  }

  function clearDirectiveState(state) {
    if (!state?.directiveActive) return false;
    state.directiveActive = false;
    state.previewEdits = [];
    state.viewMode = 'planned';
    return true;
  }

  function pointSegmentDistance(px, py, ax, ay, bx, by) {
    const dx = bx - ax;
    const dy = by - ay;
    const denom = dx * dx + dy * dy;
    const t = denom > 1e-12 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / denom)) : 0;
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  function distanceToPath(x, y, points) {
    if (!points?.length) return Infinity;
    if (points.length === 1) return Math.hypot(x - points[0][0], y - points[0][1]);
    let best = Infinity;
    for (let index = 1; index < points.length; index += 1) {
      best = Math.min(best, pointSegmentDistance(x, y, ...points[index - 1], ...points[index]));
    }
    return best;
  }

  function incrementalPlantPointsForSegment(stroke, start, end, radius, spacing, isValid) {
    if (!stroke || !Array.isArray(start) || !Array.isArray(end)) return [];
    const cellSize = Math.max(0.1, Number(spacing) || 0.1);
    const brushRadius = Math.max(0.1, Number(radius) || 0.1);
    const visited = stroke.plantVisitedCells || (stroke.plantVisitedCells = new Set());
    const output = [];
    const bounds = {
      minX: Math.min(start[0], end[0]) - brushRadius,
      maxX: Math.max(start[0], end[0]) + brushRadius,
      minY: Math.min(start[1], end[1]) - brushRadius,
      maxY: Math.max(start[1], end[1]) + brushRadius,
    };
    const firstColumn = Math.floor(bounds.minX / cellSize) - 1;
    const lastColumn = Math.ceil(bounds.maxX / cellSize) + 1;
    const firstRow = Math.floor(bounds.minY / cellSize) - 1;
    const lastRow = Math.ceil(bounds.maxY / cellSize) + 1;
    const alreadyPlanted = stroke.previewPlantPoints?.length || 0;
    for (let row = firstRow; row <= lastRow; row += 1) {
      for (let column = firstColumn; column <= lastColumn; column += 1) {
        const cellKey = `${row}:${column}`;
        if (visited.has(cellKey)) continue;
        const hashKey = `${stroke.editId}:${row}:${column}`;
        const x = column * cellSize + (hashUnit(`${hashKey}:x`) - 0.5) * cellSize * 0.5;
        const y = row * cellSize + (hashUnit(`${hashKey}:y`) - 0.5) * cellSize * 0.5;
        // A cell outside this capsule may become relevant to a later segment,
        // so mark it visited only once its jittered candidate is actually hit.
        if (pointSegmentDistance(x, y, ...start, ...end) > brushRadius) continue;
        visited.add(cellKey);
        if (isValid && !isValid(x, y)) continue;
        output.push([Number(x.toFixed(3)), Number(y.toFixed(3))]);
        if (alreadyPlanted + output.length >= 10000) return output;
      }
    }
    return output;
  }

  function markInstanceMatrixRange(mesh, firstInstance, instanceCount) {
    const attribute = mesh?.instanceMatrix;
    if (!attribute || !(instanceCount > 0)) return;
    if (typeof attribute.addUpdateRange === 'function') {
      attribute.addUpdateRange(firstInstance * 16, instanceCount * 16);
    }
    attribute.needsUpdate = true;
  }

  function mergeBounds(current, next) {
    if (!next) return current;
    if (!current) return { ...next };
    current.minX = Math.min(current.minX, next.minX);
    current.maxX = Math.max(current.maxX, next.maxX);
    current.minY = Math.min(current.minY, next.minY);
    current.maxY = Math.max(current.maxY, next.maxY);
    return current;
  }

  function applyTerrainPreviewInfluence(preview, start, end, options = {}) {
    if (!preview) return { changed: false, bounds: null };
    const {
      grid, geometry, baseHeights, baseWeights, accumulationWeights, radius, amount,
    } = preview;
    const strength = Number(options.strength ?? 1);
    const accumulate = options.accumulate === true;
    if (!(strength > 0)) return { changed: false, bounds: null };
    const xStep = (grid.maxX - grid.minX) / Math.max(1, grid.width - 1);
    const yStep = (grid.maxY - grid.minY) / Math.max(1, grid.height - 1);
    const bounds = {
      minX: Math.min(start[0], end[0]) - radius,
      maxX: Math.max(start[0], end[0]) + radius,
      minY: Math.min(start[1], end[1]) - radius,
      maxY: Math.max(start[1], end[1]) + radius,
    };
    const firstColumn = Math.max(0, Math.floor((bounds.minX - grid.minX) / xStep));
    const lastColumn = Math.min(grid.width - 1, Math.ceil((bounds.maxX - grid.minX) / xStep));
    const firstRow = Math.max(0, Math.floor((grid.maxY - bounds.maxY) / yStep));
    const lastRow = Math.min(grid.height - 1, Math.ceil((grid.maxY - bounds.minY) / yStep));
    const positions = geometry.attributes.position;
    const direction = preview.tool === 'cut' ? -1 : 1;
    let changed = false;
    for (let row = firstRow; row <= lastRow; row += 1) {
      const y = grid.maxY - row * yStep;
      for (let column = firstColumn; column <= lastColumn; column += 1) {
        const index = row * grid.width + column;
        const baseline = baseHeights[index];
        if (!Number.isFinite(baseline)) continue;
        const x = grid.minX + column * xStep;
        const distance = pointSegmentDistance(x, y, ...start, ...end);
        if (distance > radius) continue;
        const t = Math.max(0, Math.min(1, 1 - distance / Math.max(radius, 1e-6)));
        const weight = t * t * (3 - 2 * t);
        if (weight <= 0) continue;
        if (accumulate) {
          accumulationWeights[index] += weight * strength;
        } else {
          if (weight <= baseWeights[index]) continue;
          baseWeights[index] = weight;
        }
        if (!preview.touchedFlags[index]) {
          preview.touchedFlags[index] = 1;
          preview.touched.push(index);
        }
        const combinedWeight = baseWeights[index] + accumulationWeights[index];
        const elevation = baseline + direction * amount * combinedWeight;
        grid.heights[index] = elevation;
        positions.setY(index, elevation - grid.minElevation);
        changed = true;
      }
    }
    if (changed) positions.needsUpdate = true;
    return { changed, bounds };
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: options.body ? { 'Content-Type': 'application/json', ...(options.headers || {}) } : options.headers,
    });
    let payload;
    try { payload = await response.json(); } catch (_err) { payload = {}; }
    if (!response.ok || payload?.error) {
      const error = new Error(payload?.message || payload?.error || `request failed (${response.status})`);
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function create(api) {
    const viewer = api.viewer;
    const canvas = viewer?.renderer?.domElement;
    const panel = document.getElementById('plan-panel');
    if (!viewer || !canvas || !panel) return null;

    const els = {
      select: document.getElementById('plan-select'),
      revision: document.getElementById('plan-revision'),
      revisionField: document.getElementById('plan-revision-field'),
      newPlan: document.getElementById('plan-new'),
      branch: document.getElementById('plan-branch'),
      saveVersion: document.getElementById('plan-save-version'),
      discard: document.getElementById('plan-discard'),
      status: document.getElementById('plan-status'),
      saveState: document.getElementById('plan-save-state'),
      viewGroup: document.getElementById('plan-view-group'),
      viewMode: document.getElementById('plan-view-mode'),
      toolsGroup: document.getElementById('plan-tools-group'),
      tools: document.getElementById('plan-tools'),
      radius: document.getElementById('plan-radius'),
      radiusValue: document.getElementById('plan-radius-value'),
      earth: document.getElementById('plan-earth'),
      earthField: document.getElementById('plan-earth-field'),
      spacing: document.getElementById('plan-spacing'),
      spacingField: document.getElementById('plan-spacing-field'),
      species: document.getElementById('plan-species'),
      speciesOptions: document.getElementById('plan-species-options'),
      speciesField: document.getElementById('plan-species-field'),
      stage: document.getElementById('plan-stage'),
      stageField: document.getElementById('plan-stage-field'),
      undo: document.getElementById('plan-undo'),
      redo: document.getElementById('plan-redo'),
      summaryGroup: document.getElementById('plan-summary-group'),
      summary: document.getElementById('plan-summary'),
      resolution: document.getElementById('plan-resolution-note'),
    };

    const state = {
      plans: [],
      catalog: { species: [] },
      current: null,
      assets: null,
      busy: false,
      saving: false,
      saveQueue: [],
      saveWorker: null,
      tool: 'navigate',
      viewMode: 'planned',
      stroke: null,
      undo: [],
      redo: [],
      previewEdits: [],
      directiveActive: false,
      overlay: new THREE.Group(),
      brushRing: null,
      brushRingPositions: null,
      livePreview: null,
      pendingPointerStart: null,
      pendingPointerMove: null,
      pointerMoveFrame: null,
      plantPreviewGroup: new THREE.Group(),
      plantPreviewMeshes: {},
      optimisticPlantSlots: [],
    };
    state.overlay.name = 'veil-plan-difference';
    state.overlay.renderOrder = 800;
    viewer.scene.add(state.overlay);
    state.plantPreviewGroup.name = 'veil-plan-live-plants';
    state.plantPreviewGroup.renderOrder = 930;
    viewer.scene.add(state.plantPreviewGroup);

    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const previewResources = {
      trunkGeometry: new THREE.CylinderGeometry(1, 1, 1, 7),
      canopyGeometry: new THREE.IcosahedronGeometry(1, 1),
      shrubGeometry: new THREE.DodecahedronGeometry(1, 0),
      trunkMaterial: new THREE.MeshStandardMaterial({
        color: 0x6f5137, roughness: 0.92, transparent: true, opacity: 0.94,
      }),
      evergreenCanopyMaterial: new THREE.MeshStandardMaterial({
        color: 0x4ca774, emissive: 0x183e29, emissiveIntensity: 0.35,
        roughness: 0.82, transparent: true, opacity: 0.94,
      }),
      deciduousCanopyMaterial: new THREE.MeshStandardMaterial({
        color: 0x7ee0a6, emissive: 0x183e29, emissiveIntensity: 0.35,
        roughness: 0.82, transparent: true, opacity: 0.94,
      }),
      shrubMaterial: new THREE.MeshStandardMaterial({
        color: 0x8ad06f, emissive: 0x183718, emissiveIntensity: 0.3,
        roughness: 0.9, transparent: true, opacity: 0.94,
      }),
    };
    const previewTransform = new THREE.Matrix4();
    const previewPosition = new THREE.Vector3();
    const previewScale = new THREE.Vector3();
    const previewRotation = new THREE.Quaternion();
    const previewAxis = new THREE.Vector3(0, 1, 0);

    function setStatus(text, kind = '') {
      els.status.textContent = text || '';
      els.status.className = `sim-status${kind ? ` ${kind}` : ''}`;
    }

    function setSaveState(text, kind = '') {
      els.saveState.textContent = text;
      els.saveState.className = `plan-save-state${kind ? ` ${kind}` : ''}`;
    }

    function edits() {
      return clone(state.current?.revision?.edits || []);
    }

    function updateHistoryButtons() {
      const locked = state.busy || state.saving;
      els.undo.disabled = locked || !state.undo.length;
      els.redo.disabled = locked || !state.redo.length;
    }

    function planName() {
      return state.current?.plan?.name || 'plan';
    }

    function isHeadRevision() {
      return !!state.current
        && state.current.plan.head_revision_id === state.current.revision?.revision_id;
    }

    function renderPlanList() {
      const selected = state.current?.plan?.plan_id || '';
      els.select.replaceChildren();
      const baseline = document.createElement('option');
      baseline.value = '';
      baseline.textContent = 'Baseline — no plan';
      els.select.appendChild(baseline);
      state.plans.forEach((plan) => {
        const option = document.createElement('option');
        option.value = plan.plan_id;
        option.textContent = `${plan.name}${plan.checkpoint_name ? ` · ${plan.checkpoint_name}` : ''}`;
        els.select.appendChild(option);
      });
      els.select.value = selected;
    }

    function renderRevisionList() {
      const history = state.current?.history || [];
      els.revisionField.hidden = !state.current;
      els.revision.replaceChildren();
      history.slice().reverse().forEach((item, reverseIndex) => {
        const option = document.createElement('option');
        option.value = item.revision_id;
        const sequence = history.length - reverseIndex;
        const title = item.checkpoint_name || item.message || `Revision ${sequence}`;
        const isHead = item.revision_id === state.current.plan.head_revision_id;
        option.textContent = `${isHead ? 'Current · ' : ''}${title}`;
        els.revision.appendChild(option);
      });
      els.revision.value = state.current?.revision?.revision_id || '';
    }

    function renderSummary() {
      const diff = state.current?.materialized?.diff;
      const visible = !!diff;
      els.summaryGroup.hidden = !visible;
      if (!visible) {
        els.summary.replaceChildren();
        return;
      }
      const terrain = diff.terrain || {};
      const vegetation = diff.vegetation || {};
      const rows = [
        ['Excavation', `${fmt(terrain.cut_m3)} m³`],
        ['Fill', `${fmt(terrain.fill_m3)} m³`],
        ['Disturbed ground', `${fmt(terrain.disturbed_m2)} m²`],
        ['Trees', `${fmt(vegetation.effective_trees, 0)} effective · +${fmt(vegetation.trees_added, 0)} planned`],
        ['Bushes', `${fmt(vegetation.effective_shrubs, 0)} effective · +${fmt(vegetation.shrubs_added, 0)} planned`],
        ['Removed plants', fmt(vegetation.entities_removed, 0)],
        ['Canopy cover', `${fmt(vegetation.canopy_cover_pct)}%`],
      ];
      els.summary.innerHTML = rows.map(([label, value]) =>
        `<div class="plan-summary-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
      const cell = Number(terrain.analysis_cell_m);
      els.resolution.textContent = Number.isFinite(cell)
        ? `Analysis cell ${fmt(cell, 2)} m. Earthworks narrower than ${fmt(cell * 3, 1)} m are shown but not simulation-resolvable.`
        : '';
    }

    function activeSpeciesRows() {
      const habit = state.tool === 'shrub' ? 'shrub' : 'tree';
      return (state.catalog.species || []).filter((row) => row.habit === habit);
    }

    function selectedSpecies() {
      const value = String(els.species.value || '').trim();
      if (!value) return null;
      const normalized = value.toLocaleLowerCase();
      const known = activeSpeciesRows().find((row) =>
        [row.id, row.common_name, row.scientific_name]
          .some((candidate) => String(candidate || '').toLocaleLowerCase() === normalized));
      if (known) return known;
      const habit = state.tool === 'shrub' ? 'shrub' : 'tree';
      const evergreen = habit === 'tree' && /\b(pine|spruce|fir|hemlock|cedar|juniper|evergreen)\b/i.test(value);
      return {
        id: null,
        common_name: value.slice(0, 160),
        scientific_name: null,
        habit,
        type: evergreen ? 'evergreen' : 'deciduous',
        asset_key: evergreen ? 'pine' : (habit === 'shrub' ? 'shrub' : 'maple'),
        stages: habit === 'shrub'
          ? { young: { height: 0.7, radius: 0.5 }, mature: { height: 1.8, radius: 1.1 } }
          : { sapling: { height: 2.5, radius: 0.9 }, mature: { height: 10, radius: 3.5 } },
        default_stage: habit === 'shrub' ? 'young' : 'sapling',
        default_spacing_m: habit === 'shrub' ? 1.8 : 6,
        custom: true,
      };
    }

    function renderStages({ updateSpacing = true } = {}) {
      const species = selectedSpecies();
      const previous = els.stage.value;
      els.stage.replaceChildren();
      Object.entries(species?.stages || {}).forEach(([id, dimensions]) => {
        const option = document.createElement('option');
        option.value = id;
        option.textContent = `${id} · ${dimensions.height || '?'} m tall`;
        els.stage.appendChild(option);
      });
      if ([...els.stage.options].some((option) => option.value === previous)) {
        els.stage.value = previous;
      } else if (species?.default_stage) {
        els.stage.value = species.default_stage;
      }
      if (updateSpacing && species?.default_spacing_m) els.spacing.value = species.default_spacing_m;
    }

    function renderSpecies() {
      const previous = String(els.species.value || '').trim();
      const previousKnown = (state.catalog.species || []).find((row) =>
        [row.id, row.common_name, row.scientific_name]
          .some((candidate) => String(candidate || '').toLocaleLowerCase() === previous.toLocaleLowerCase()));
      els.speciesOptions.replaceChildren();
      activeSpeciesRows().forEach((row) => {
        const option = document.createElement('option');
        option.value = row.common_name;
        option.label = row.scientific_name || row.type || '';
        els.speciesOptions.appendChild(option);
      });
      if (!previous || (previousKnown && !activeSpeciesRows().includes(previousKnown))) {
        els.species.value = activeSpeciesRows()[0]?.common_name || '';
      }
      renderStages();
    }

    function renderToolFields() {
      const plants = ['tree', 'shrub'].includes(state.tool);
      const earth = ['cut', 'fill'].includes(state.tool);
      els.speciesField.hidden = !plants;
      els.stageField.hidden = !plants;
      els.spacingField.hidden = !plants;
      els.earthField.hidden = !earth;
      els.tools.querySelectorAll('[data-plan-tool]').forEach((button) => {
        button.classList.toggle('active', button.dataset.planTool === state.tool);
      });
      renderSpecies();
      updateBrushRingColor();
    }

    function renderCurrent() {
      const active = !!state.current;
      const editable = active && isHeadRevision();
      const locked = state.busy || state.saving;
      els.select.disabled = locked;
      els.revision.disabled = locked;
      els.newPlan.disabled = locked;
      els.branch.disabled = !active || locked;
      els.saveVersion.disabled = !editable || locked;
      els.discard.disabled = !active || locked;
      els.viewGroup.hidden = !active;
      els.toolsGroup.hidden = !editable;
      els.viewMode.querySelectorAll('[data-plan-view]').forEach((button) => {
        button.classList.toggle('active', button.dataset.planView === state.viewMode);
        button.disabled = locked;
      });
      els.tools.querySelectorAll('[data-plan-tool]').forEach((button) => {
        button.disabled = state.busy;
      });
      renderPlanList();
      renderRevisionList();
      renderSummary();
      updateHistoryButtons();
      if (active) {
        const revision = state.current.revision;
        const suffix = revision?.checkpoint_name ? ` · ${revision.checkpoint_name}` : '';
        const mode = editable ? '' : ' · historical, branch to edit';
        setStatus(`${state.current.plan.name}${suffix} · ${revision?.edits?.length || 0} edits${mode}`,
          editable ? '' : 'warn');
      }
    }

    async function refreshPlans() {
      const payload = await request('/api/plans');
      state.plans = payload.plans || [];
      renderPlanList();
    }

    function clearObject(object) {
      while (object.children.length) {
        const child = object.children[0];
        object.remove(child);
        child.geometry?.dispose?.();
        if (Array.isArray(child.material)) child.material.forEach((material) => material.dispose?.());
        else child.material?.dispose?.();
      }
    }

    function activeToolColor() {
      return {
        remove: 0xf2766b, tree: 0x7ee0a6, shrub: 0x8ad06f,
        cut: 0x59a8ff, fill: 0xf2c14e,
      }[state.tool] || 0x7ee0a6;
    }

    function lineForCoordinates(coordinates, color, opacity = 0.9) {
      const points = coordinates.map(([x, y]) => {
        const height = global.VEILTerrain.sampleTerrainHeightAtLocal(viewer.terrainGrid, x, y) + 0.35;
        return new THREE.Vector3(x, height, -y);
      });
      if (points.length === 1) points.push(points[0].clone().add(new THREE.Vector3(0.01, 0, 0.01)));
      const geometry = new THREE.BufferGeometry().setFromPoints(points);
      const material = new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity, depthTest: false });
      const line = new THREE.Line(geometry, material);
      line.renderOrder = 900;
      return line;
    }

    function nextPowerOfTwo(value) {
      let capacity = 1;
      while (capacity < value) capacity *= 2;
      return capacity;
    }

    function replacePreviewSlotMesh(slots, previousMesh, nextMesh) {
      (slots || []).forEach((slot) => {
        (slot.instances || []).forEach((entry) => {
          if (entry.mesh === previousMesh) entry.mesh = nextMesh;
        });
      });
    }

    function ensurePlantPreviewMesh(key, count, geometry, material) {
      let record = state.plantPreviewMeshes[key];
      if (record && record.capacity >= count) return record;
      const previousMesh = record?.mesh || null;
      const previousCount = previousMesh?.count || 0;
      const committedCount = record?.committedCount || 0;
      if (record) {
        state.plantPreviewGroup.remove(previousMesh);
      }
      const capacity = nextPowerOfTwo(Math.max(1, count));
      const mesh = new THREE.InstancedMesh(geometry, material, capacity);
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      if (previousMesh && previousCount) {
        mesh.instanceMatrix.array.set(
          previousMesh.instanceMatrix.array.subarray(0, previousCount * 16), 0);
        mesh.count = previousCount;
        markInstanceMatrixRange(mesh, 0, previousCount);
        replacePreviewSlotMesh(state.stroke?.previewPlantSlots, previousMesh, mesh);
        viewer.vegetationRenderer?.replaceOptimisticPlanVegetationMesh?.(previousMesh, mesh);
      } else {
        mesh.count = 0;
      }
      mesh.renderOrder = 930;
      mesh.castShadow = false;
      mesh.receiveShadow = false;
      mesh.frustumCulled = false;
      state.plantPreviewGroup.add(mesh);
      state.plantPreviewMeshes[key] = { mesh, capacity, committedCount };
      previousMesh?.dispose?.();
      return state.plantPreviewMeshes[key];
    }

    function clearPlantPreview({ includeCommitted = false } = {}) {
      Object.values(state.plantPreviewMeshes).forEach((record) => {
        record.mesh.count = includeCommitted ? 0 : record.committedCount;
        if (includeCommitted) record.committedCount = 0;
      });
    }

    function clearOptimisticPlantPreviews() {
      clearPlantPreview({ includeCommitted: true });
      state.optimisticPlantSlots = [];
      viewer.vegetationRenderer?.clearOptimisticPlanVegetation?.();
    }

    function resetActivePlantPreview(stroke) {
      clearPlantPreview();
      if (!stroke) return;
      stroke.previewPlantPoints = [];
      stroke.previewPlantSlots = [];
      stroke.plantVisitedCells = new Set();
    }

    function appendLibraryTreePreview(points, dimensions, stroke, renderSpec, pointOffset, slots) {
      const partRecords = renderSpec.parts.map((part) => {
        const key = `tree-library:${renderSpec.assetKey}:${part.key}`;
        const currentCount = state.plantPreviewMeshes[key]?.mesh.count || 0;
        const record = ensurePlantPreviewMesh(
          key, currentCount + points.length, part.geometry, part.material);
        return { record, firstInstance: record.mesh.count };
      });
      points.forEach(([x, y], index) => {
        const ground = global.VEILTerrain.sampleTerrainHeightAtLocal(viewer.terrainGrid, x, y);
        const radius = Math.max(0.35, dimensions.radius);
        const height = Math.max(1, dimensions.height);
        const rotation = hashUnit(
          `tree:${Math.round(x * 10)}:${Math.round(y * 10)}`) * Math.PI * 2;
        previewRotation.setFromAxisAngle(previewAxis, rotation);
        previewPosition.set(x, ground, -y);
        const crownDiameter = Math.max(1.8, radius * 2);
        previewScale.set(
          crownDiameter / Math.max(1, renderSpec.diameter),
          height / Math.max(1, renderSpec.height),
          crownDiameter / Math.max(1, renderSpec.diameter)
        );
        previewTransform.compose(previewPosition, previewRotation, previewScale);
        const instances = partRecords.map(({ record, firstInstance }) => {
          const instanceIndex = firstInstance + index;
          record.mesh.setMatrixAt(instanceIndex, previewTransform);
          return { mesh: record.mesh, instanceIndex };
        });
        slots.push({
          entityId: `planned:${stroke.editId}:${pointOffset + index}`,
          kind: 'tree', x, y, groundHeight: ground, instances,
        });
      });
      partRecords.forEach(({ record, firstInstance }) => {
        record.mesh.count = firstInstance + points.length;
        markInstanceMatrixRange(record.mesh, firstInstance, points.length);
      });
    }

    function appendPlantPreview(points, dimensions, stroke) {
      if (!points?.length || !dimensions?.species || !stroke) return [];
      const habit = dimensions.species.habit === 'shrub' ? 'shrub' : 'tree';
      const grid = viewer.terrainGrid;
      if (!grid) return [];
      const pointOffset = stroke.previewPlantPoints?.length || 0;
      const slots = [];
      if (habit === 'shrub') {
        const currentCount = state.plantPreviewMeshes.shrub?.mesh.count || 0;
        const record = ensurePlantPreviewMesh(
          'shrub', currentCount + points.length,
          previewResources.shrubGeometry, previewResources.shrubMaterial);
        const mesh = record.mesh;
        const firstInstance = mesh.count;
        points.forEach(([x, y], index) => {
          const ground = global.VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y);
          const radius = Math.max(0.25, dimensions.radius);
          const height = Math.max(0.35, dimensions.height);
          previewPosition.set(x, ground + height * 0.48, -y);
          previewRotation.setFromAxisAngle(previewAxis, hashUnit(`${x}:${y}`) * Math.PI * 2);
          previewScale.set(radius, height * 0.65, radius);
          previewTransform.compose(previewPosition, previewRotation, previewScale);
          const instanceIndex = firstInstance + index;
          mesh.setMatrixAt(instanceIndex, previewTransform);
          slots.push({
            entityId: `planned:${stroke.editId}:${pointOffset + index}`,
            kind: 'shrub', x, y, groundHeight: ground,
            instances: [{ mesh, instanceIndex }],
          });
        });
        mesh.count = firstInstance + points.length;
        markInstanceMatrixRange(mesh, firstInstance, points.length);
      } else {
        const evergreen = dimensions.species.type === 'evergreen';
        const renderSpec = stroke.treeRenderSpec
          || viewer.vegetationRenderer?.getPlanTreeRenderSpec?.(
            dimensions.species.asset_key, dimensions.species.type);
        if (renderSpec?.parts?.length) {
          appendLibraryTreePreview(
            points, dimensions, stroke, renderSpec, pointOffset, slots);
          stroke.previewPlantPoints.push(...points);
          stroke.previewPlantSlots.push(...slots);
          return slots;
        }
        const canopyKey = evergreen ? 'canopy-evergreen' : 'canopy-deciduous';
        const trunkCount = state.plantPreviewMeshes.trunk?.mesh.count || 0;
        const canopyCount = state.plantPreviewMeshes[canopyKey]?.mesh.count || 0;
        const trunkRecord = ensurePlantPreviewMesh(
          'trunk', trunkCount + points.length,
          previewResources.trunkGeometry, previewResources.trunkMaterial);
        const canopyRecord = ensurePlantPreviewMesh(
          canopyKey, canopyCount + points.length, previewResources.canopyGeometry,
          evergreen ? previewResources.evergreenCanopyMaterial : previewResources.deciduousCanopyMaterial);
        const trunk = trunkRecord.mesh;
        const canopy = canopyRecord.mesh;
        const firstTrunk = trunk.count;
        const firstCanopy = canopy.count;
        points.forEach(([x, y], index) => {
          const ground = global.VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y);
          const radius = Math.max(0.35, dimensions.radius);
          const height = Math.max(1, dimensions.height);
          const trunkHeight = Math.max(0.7, height * (evergreen ? 0.3 : 0.42));
          const rotation = hashUnit(`${x}:${y}`) * Math.PI * 2;
          previewRotation.setFromAxisAngle(previewAxis, rotation);
          previewPosition.set(x, ground + trunkHeight / 2, -y);
          previewScale.set(Math.max(0.09, radius * 0.1), trunkHeight, Math.max(0.09, radius * 0.1));
          previewTransform.compose(previewPosition, previewRotation, previewScale);
          const trunkInstance = firstTrunk + index;
          trunk.setMatrixAt(trunkInstance, previewTransform);
          previewPosition.set(x, ground + trunkHeight + Math.max(0.5, height - trunkHeight) * 0.42, -y);
          previewScale.set(radius, Math.max(0.7, height - trunkHeight), radius);
          previewTransform.compose(previewPosition, previewRotation, previewScale);
          const canopyInstance = firstCanopy + index;
          canopy.setMatrixAt(canopyInstance, previewTransform);
          slots.push({
            entityId: `planned:${stroke.editId}:${pointOffset + index}`,
            kind: 'tree', x, y, groundHeight: ground,
            instances: [
              { mesh: trunk, instanceIndex: trunkInstance },
              { mesh: canopy, instanceIndex: canopyInstance },
            ],
          });
        });
        trunk.count = firstTrunk + points.length;
        canopy.count = firstCanopy + points.length;
        markInstanceMatrixRange(trunk, firstTrunk, points.length);
        markInstanceMatrixRange(canopy, firstCanopy, points.length);
      }
      stroke.previewPlantPoints.push(...points);
      stroke.previewPlantSlots.push(...slots);
      return slots;
    }

    function promotePlantPreview(stroke) {
      Object.values(state.plantPreviewMeshes).forEach((record) => {
        record.committedCount = record.mesh.count;
      });
      const registered = viewer.vegetationRenderer?.registerOptimisticPlanVegetation?.(
        stroke?.previewPlantSlots || []) || [];
      state.optimisticPlantSlots.push(...registered);
      return registered;
    }

    function beginTerrainPreview(stroke) {
      const grid = viewer.terrainGrid;
      const geometry = viewer.terrainMesh?.geometry;
      if (!grid?.heights?.length || !geometry?.attributes?.position) return null;
      const preview = {
        kind: 'terrain',
        tool: stroke.tool,
        grid,
        geometry,
        baseHeights: grid.heights.slice(),
        baseWeights: new Float32Array(grid.heights.length),
        accumulationWeights: new Float32Array(grid.heights.length),
        touchedFlags: new Uint8Array(grid.heights.length),
        touched: [],
        radius: stroke.radius,
        amount: stroke.amount,
        normalFrame: null,
        affectedVegetationBounds: null,
      };
      state.livePreview = preview;
      return preview;
    }

    function vegetationSamplingBounds(preview, bounds) {
      if (!preview || !bounds) return null;
      const xStep = (preview.grid.maxX - preview.grid.minX) / Math.max(1, preview.grid.width - 1);
      const yStep = (preview.grid.maxY - preview.grid.minY) / Math.max(1, preview.grid.height - 1);
      return {
        minX: bounds.minX - Math.abs(xStep),
        maxX: bounds.maxX + Math.abs(xStep),
        minY: bounds.minY - Math.abs(yStep),
        maxY: bounds.maxY + Math.abs(yStep),
      };
    }

    function queueTerrainPreviewRefresh(preview, bounds) {
      const vegetationBounds = vegetationSamplingBounds(preview, bounds);
      preview.affectedVegetationBounds = mergeBounds(preview.affectedVegetationBounds, vegetationBounds);
      viewer.vegetationRenderer?.syncPlanTerrainHeights?.(vegetationBounds);
      refreshTerrainNormals(preview);
    }

    function refreshTerrainNormals(preview) {
      if (!preview || preview.normalFrame) return;
      preview.normalFrame = requestAnimationFrame(() => {
        preview.normalFrame = null;
        if (preview.geometry === viewer.terrainMesh?.geometry) {
          preview.geometry.computeVertexNormals();
          if (preview.geometry.attributes.normal) preview.geometry.attributes.normal.needsUpdate = true;
          viewer.invalidateShadowMap?.('plan-live-terrain');
        }
      });
    }

    function applyTerrainPreviewSegment(preview, start, end) {
      const update = applyTerrainPreviewInfluence(preview, start, end);
      if (update.changed) queueTerrainPreviewRefresh(preview, update.bounds);
    }

    function applyTerrainPreviewStamp(preview, point, strength) {
      const update = applyTerrainPreviewInfluence(preview, point, point, {
        accumulate: true,
        strength,
      });
      if (update.changed) queueTerrainPreviewRefresh(preview, update.bounds);
    }

    function restoreTerrainPreview(preview) {
      if (!preview || preview.grid !== viewer.terrainGrid || preview.geometry !== viewer.terrainMesh?.geometry) return;
      const positions = preview.geometry.attributes.position;
      preview.touched.forEach((index) => {
        const elevation = preview.baseHeights[index];
        preview.grid.heights[index] = elevation;
        positions.setY(index, elevation - preview.grid.minElevation);
      });
      positions.needsUpdate = true;
      preview.geometry.computeVertexNormals();
      if (preview.geometry.attributes.normal) preview.geometry.attributes.normal.needsUpdate = true;
      viewer.vegetationRenderer?.syncPlanTerrainHeights?.(preview.affectedVegetationBounds);
      viewer.invalidateShadowMap?.('plan-live-terrain-cancel');
    }

    function clearLivePreview({ restore = true } = {}) {
      const preview = state.livePreview;
      if (preview?.normalFrame) cancelAnimationFrame(preview.normalFrame);
      if (restore && preview?.kind === 'terrain') restoreTerrainPreview(preview);
      viewer.vegetationRenderer?.clearPlanRemovalPreview?.({ restore });
      clearPlantPreview();
      state.livePreview = null;
    }

    function appendIncrementalPlantSegment(stroke, start, end) {
      const points = incrementalPlantPointsForSegment(
        stroke, start, end, stroke.radius, stroke.spacing,
        (x, y) => global.VEILTerrain.hasValidTerrainAtLocal(viewer.terrainGrid, x, y));
      appendPlantPreview(points, stroke.speciesDimensions, stroke);
      return points;
    }

    function updateLivePreview(stroke, previousPoint = null) {
      if (!stroke?.points?.length) return;
      if (stroke.tool === 'cut' || stroke.tool === 'fill') {
        const preview = state.livePreview?.kind === 'terrain'
          ? state.livePreview : beginTerrainPreview(stroke);
        const end = stroke.points[stroke.points.length - 1];
        applyTerrainPreviewSegment(preview, previousPoint || end, end);
        return;
      }
      if (stroke.tool === 'remove') {
        if (state.livePreview?.kind !== 'remove') {
          viewer.vegetationRenderer?.beginPlanRemovalPreview?.();
        }
        state.livePreview = { kind: 'remove' };
        const end = stroke.points[stroke.points.length - 1];
        viewer.vegetationRenderer?.applyPlanRemovalSegment?.(
          previousPoint || end, end, stroke.radius);
        return;
      }
      if (stroke.tool === 'tree' || stroke.tool === 'shrub') {
        const dimensions = stroke.speciesDimensions;
        const click = stroke.travelPx < 5;
        state.livePreview = { kind: 'plants' };
        if (click) {
          if (!stroke.previewPlantPoints.length) {
            stroke.plantPreviewMode = 'click';
            appendPlantPreview([stroke.points[0]], dimensions, stroke);
          }
          return;
        }
        if (stroke.plantPreviewMode !== 'brush') {
          resetActivePlantPreview(stroke);
          stroke.plantPreviewMode = 'brush';
          if (stroke.points.length === 1) {
            appendIncrementalPlantSegment(stroke, stroke.points[0], stroke.points[0]);
          } else {
            for (let index = 1; index < stroke.points.length; index += 1) {
              appendIncrementalPlantSegment(stroke, stroke.points[index - 1], stroke.points[index]);
            }
          }
          return;
        }
        const end = stroke.points[stroke.points.length - 1];
        appendIncrementalPlantSegment(stroke, previousPoint || end, end);
      }
    }


    function geometryPaths(geometry) {
      if (!geometry) return [];
      if (geometry.type === 'Point') return [[geometry.coordinates]];
      if (geometry.type === 'MultiPoint') return geometry.coordinates.map((point) => [point]);
      if (geometry.type === 'LineString') return [geometry.coordinates];
      if (geometry.type === 'Polygon') return geometry.coordinates;
      if (geometry.type === 'MultiPolygon') return geometry.coordinates.flat();
      return [];
    }

    function renderDifferenceOverlay() {
      clearObject(state.overlay);
      state.overlay.visible = state.viewMode === 'difference';
      if (!state.current || !state.overlay.visible) return;
      const colors = {
        terrain_cut: 0x59a8ff,
        terrain_fill: 0xf2c14e,
        swale: 0x5ec8e0,
        orchard: 0x7ee0a6,
        garden: 0xd5a6ff,
        vegetation_add: 0x7ee0a6,
      };
      const removed = new Set();
      [...(state.current.revision?.edits || []), ...(state.previewEdits || [])].forEach((edit) => {
        if (edit.kind === 'vegetation_remove') {
          (edit.params?.entity_ids || []).forEach((id) => removed.add(id));
          return;
        }
        const color = colors[edit.kind];
        if (!color) return;
        geometryPaths(edit.geometry).slice(0, 2500).forEach((path) => {
          if (path.length) state.overlay.add(lineForCoordinates(path, color));
        });
      });
      const baselinePlants = [...(state.assets?.baselineTrees || []), ...(state.assets?.baselineShrubs || [])];
      baselinePlants.filter((plant) => removed.has(String(plant.id))).slice(0, 3000).forEach((plant) => {
        const h = global.VEILTerrain.sampleTerrainHeightAtLocal(viewer.terrainGrid, plant.x, plant.y) + 1.1;
        const mesh = new THREE.Mesh(
          new THREE.SphereGeometry(0.65, 7, 5),
          new THREE.MeshBasicMaterial({ color: 0xf2766b, transparent: true, opacity: 0.75, depthTest: false })
        );
        mesh.position.set(plant.x, h, -plant.y);
        mesh.renderOrder = 901;
        state.overlay.add(mesh);
      });
    }

    async function applyCurrentView() {
      clearOptimisticPlantPreviews();
      state.assets = await api.applyRevision(state.current, state.viewMode);
      renderDifferenceOverlay();
    }

    async function loadPlan(planId, revisionId = null) {
      const previousBusy = state.busy;
      let loadError = null;
      state.stroke = null;
      viewer.controls.enabled = true;
      clearLivePreview();
      state.busy = true;
      setSaveState('loading', 'busy');
      renderCurrent();
      try {
        if (!planId) {
          state.current = null;
          state.previewEdits = [];
          state.undo = [];
          state.redo = [];
          state.tool = 'navigate';
          state.viewMode = 'planned';
          clearOptimisticPlantPreviews();
          await api.applyRevision(null, 'baseline');
          clearObject(state.overlay);
          setSaveState('ready');
          setStatus('Baseline active. Create or select a plan to edit.');
          return;
        }
        const suffix = revisionId ? `/revisions/${encodeURIComponent(revisionId)}` : '';
        state.current = await request(`/api/plans/${encodeURIComponent(planId)}${suffix}`);
        state.previewEdits = [];
        state.undo = [];
        state.redo = [];
        state.tool = 'navigate';
        state.viewMode = 'planned';
        await applyCurrentView();
        setSaveState('saved', 'saved');
      } catch (error) {
        loadError = error;
        setSaveState('error', 'error');
      } finally {
        state.busy = previousBusy;
        renderCurrent();
        if (loadError) setStatus(`Could not load plan: ${loadError.message}`, 'err');
      }
    }

    async function commit(nextEdits, message, options = {}) {
      if (!state.current || state.busy || state.saving) return false;
      const previous = edits();
      state.busy = true;
      setSaveState('saving', 'busy');
      updateHistoryButtons();
      try {
        const payload = await request(`/api/plans/${encodeURIComponent(state.current.plan.plan_id)}/commit`, {
          method: 'POST',
          body: JSON.stringify({
            expected_revision_id: state.current.plan.head_revision_id,
            edits: nextEdits,
            message,
            author: 'viewer',
          }),
        });
        if (options.recordHistory !== false) {
          state.undo.push(previous);
          if (state.undo.length > 100) state.undo.shift();
          state.redo = [];
        }
        state.current = payload;
        state.previewEdits = [];
        await applyCurrentView();
        await refreshPlans();
        setSaveState('saved', 'saved');
        renderCurrent();
        return true;
      } catch (error) {
        setSaveState('error', 'error');
        if (error.payload?.error === 'plan_conflict') {
          setStatus('This plan changed in another tool. Reloading its newest revision…', 'warn');
          await loadPlan(state.current.plan.plan_id);
        } else {
          setStatus(`Plan save failed: ${error.message}`, 'err');
        }
        return false;
      } finally {
        state.busy = false;
        renderCurrent();
      }
    }

    async function processOptimisticSaveQueue() {
      if (state.saveWorker || !state.saveQueue.length) {
        return state.saveWorker;
      }
      state.saving = true;
      setSaveState('saving', 'busy');
      renderCurrent();

      const worker = (async () => {
        try {
          await drainOrderedEditQueue(state.saveQueue, {
            current: () => state.current,
            save: ({ current, item, nextEdits }) => request(
              `/api/plans/${encodeURIComponent(current.plan.plan_id)}/commit`,
              {
                method: 'POST',
                body: JSON.stringify({
                  expected_revision_id: current.plan.head_revision_id,
                  edits: nextEdits,
                  message: item.message,
                  author: 'viewer',
                }),
              }
            ),
            saved: ({ payload, previous }) => {
              state.undo.push(previous);
              if (state.undo.length > 100) state.undo.shift();
              state.redo = [];
              state.current = payload;
              state.previewEdits = [];
              api.updateRevisionContext?.(payload);
              renderDifferenceOverlay();
              renderCurrent();
            },
          });

          state.saving = false;
          setSaveState('saved', 'saved');
          void refreshPlans().catch((error) => {
            console.warn('plan list refresh after optimistic save failed:', error);
          });
          renderCurrent();
          return true;
        } catch (error) {
          const planId = state.current?.plan?.plan_id;
          state.saveQueue.length = 0;
          state.saving = false;
          if (state.stroke || state.pendingPointerStart || state.livePreview) cancelStroke();
          state.busy = true;
          try {
            if (error.payload?.error === 'plan_conflict' && planId) {
              state.current = await request(`/api/plans/${encodeURIComponent(planId)}`);
            }
            if (state.current) await applyCurrentView();
          } catch (reconcileError) {
            console.error('optimistic save reconciliation failed:', reconcileError);
          } finally {
            state.busy = false;
          }
          setSaveState('error', 'error');
          if (error.payload?.error === 'plan_conflict') {
            setStatus('This plan changed in another tool. Its newest revision has been loaded.', 'warn');
          } else {
            setStatus(`Plan save failed: ${error.message}`, 'err');
          }
          renderCurrent();
          return false;
        } finally {
          state.saveWorker = null;
        }
      })();
      state.saveWorker = worker;
      return worker;
    }

    function enqueueOptimisticSave(edit) {
      state.saveQueue.push({ edit, message: edit.label || 'Plan edit' });
      state.saving = true;
      setSaveState('saving', 'busy');
      renderCurrent();
      void processOptimisticSaveQueue();
    }

    function stageDimensions() {
      const species = selectedSpecies();
      const dimensions = species?.stages?.[els.stage.value] || {};
      return {
        species,
        height: Number(dimensions.height) || (species?.habit === 'shrub' ? 1.2 : 4),
        radius: Number(dimensions.radius) || (species?.habit === 'shrub' ? 0.7 : 1.5),
      };
    }

    function strokeGeometry(points) {
      if (points.length <= 1) return { type: 'Point', coordinates: points[0] };
      return { type: 'LineString', coordinates: points };
    }

    function stopTerrainAccumulation(stroke) {
      if (stroke?.terrainFrame) cancelAnimationFrame(stroke.terrainFrame);
      if (stroke) stroke.terrainFrame = null;
    }

    function recordTerrainAccumulation(stroke, strength) {
      if (state.stroke !== stroke) return false;
      if (!stroke.currentPoint) return true;
      if (stroke.accumulationStamps.length >= MAX_TERRAIN_ACCUMULATION_STAMPS) {
        stroke.terrainAccumulationStopped = true;
        setStatus('This earthwork reached the maximum gesture length; release to save it.', 'warn');
        return false;
      }
      const roundedStrength = Number(Math.max(0.0001, strength).toFixed(4));
      const point = [stroke.currentPoint[0], stroke.currentPoint[1]];
      stroke.accumulationStamps.push([point[0], point[1], roundedStrength]);
      const preview = state.livePreview?.kind === 'terrain' ? state.livePreview : beginTerrainPreview(stroke);
      applyTerrainPreviewStamp(preview, point, roundedStrength);
      return true;
    }

    function startTerrainAccumulation(stroke) {
      if (!stroke || !['cut', 'fill'].includes(stroke.tool)) return;
      const now = global.performance?.now?.() ?? Date.now();
      stroke.nextTerrainAccumulationAt = now + TERRAIN_ACCUMULATION_INTERVAL_MS;
      const tick = (timestamp) => {
        stroke.terrainFrame = null;
        if (state.stroke !== stroke || stroke.terrainAccumulationStopped) return;
        if (timestamp >= stroke.nextTerrainAccumulationAt) {
          const elapsedIntervals = Math.min(10, Math.floor(
            (timestamp - stroke.nextTerrainAccumulationAt) / TERRAIN_ACCUMULATION_INTERVAL_MS
          ) + 1);
          stroke.nextTerrainAccumulationAt += elapsedIntervals * TERRAIN_ACCUMULATION_INTERVAL_MS;
          const strength = elapsedIntervals * TERRAIN_ACCUMULATION_INTERVAL_MS
            / TERRAIN_ACCUMULATION_LAYER_MS;
          if (!recordTerrainAccumulation(stroke, strength)) return;
        }
        stroke.terrainFrame = requestAnimationFrame(tick);
      };
      stroke.terrainFrame = requestAnimationFrame(tick);
    }

    async function finishStroke() {
      const stroke = state.stroke;
      stopTerrainAccumulation(stroke);
      state.stroke = null;
      viewer.controls.enabled = true;
      if (!stroke?.points?.length) { clearLivePreview(); return; }
      const radius = stroke.radius;
      const amount = stroke.amount;
      const spacing = stroke.spacing;
      const tool = stroke.tool;
      const geometry = strokeGeometry(stroke.points);
      let edit = null;
      if (tool === 'cut' || tool === 'fill') {
        const params = {
          radius_m: radius,
          [tool === 'cut' ? 'depth_m' : 'height_m']: amount,
          falloff: 'smoothstep',
        };
        if (stroke.accumulationStamps?.length) {
          params.accumulation_stamps = stroke.accumulationStamps;
        }
        edit = {
          edit_id: stroke.editId,
          kind: tool === 'cut' ? 'terrain_cut' : 'terrain_fill',
          geometry,
          params,
          label: tool === 'cut' ? 'Depression' : 'Mound',
        };
      } else if (tool === 'remove') {
        const ids = viewer.vegetationRenderer?.getPlanRemovalPreviewIds?.() || [];
        if (!ids.length) {
          setStatus('No effective trees or bushes fell inside that brush.', 'warn');
          clearLivePreview();
          return;
        }
        edit = { edit_id: stroke.editId, kind: 'vegetation_remove', geometry,
          params: { entity_ids: ids, kinds: ['tree', 'shrub'] }, label: `Remove ${ids.length} plants` };
      } else if (tool === 'tree' || tool === 'shrub') {
        const dimensions = stroke.speciesDimensions;
        if (!dimensions.species) {
          setStatus('Choose or type a species before planting.', 'warn');
          clearLivePreview();
          return;
        }
        const params = {
          habit: tool === 'shrub' ? 'shrub' : 'tree',
          species: dimensions.species.common_name,
          type: dimensions.species.type,
          height: dimensions.height,
          radius: dimensions.radius,
          spacing_m: spacing,
          radius_m: radius,
          stage: stroke.stage,
          asset_key: dimensions.species.asset_key,
        };
        const click = stroke.points.length === 1 || stroke.travelPx < 5;
        if (!click && stroke.plantPreviewMode !== 'brush') {
          resetActivePlantPreview(stroke);
          stroke.plantPreviewMode = 'brush';
          if (stroke.points.length === 1) {
            appendIncrementalPlantSegment(stroke, stroke.points[0], stroke.points[0]);
          } else {
            for (let index = 1; index < stroke.points.length; index += 1) {
              appendIncrementalPlantSegment(stroke, stroke.points[index - 1], stroke.points[index]);
            }
          }
        }
        if (!stroke.previewPlantPoints.length) {
          appendPlantPreview([stroke.points[0]], dimensions, stroke);
        }
        const planted = stroke.previewPlantPoints.slice();
        edit = {
          edit_id: stroke.editId,
          kind: 'vegetation_add',
          geometry: click
            ? { type: 'Point', coordinates: planted[0] }
            : { type: 'MultiPoint', coordinates: planted },
          params,
          label: dimensions.species.common_name,
        };
      }
      if (!edit) { clearLivePreview(); return; }
      // Each live preview is already the exact local result. Promote/detach it
      // and persist edits in order without refetching immutable artifacts.
      if (isTerrainBrush(tool)) {
        state.livePreview = null;
      } else if (tool === 'remove') {
        viewer.vegetationRenderer?.commitPlanRemovalPreview?.();
        state.livePreview = null;
      } else if (tool === 'tree' || tool === 'shrub') {
        promotePlantPreview(stroke);
        state.livePreview = null;
      }
      enqueueOptimisticSave(edit);
    }

    function pickAt(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, viewer.camera);
      const hit = raycaster.intersectObject(viewer.terrainMesh, false)[0];
      return hit ? { hit, point: [Number(hit.point.x.toFixed(3)), Number((-hit.point.z).toFixed(3))] } : null;
    }

    function createBrushRing() {
      const geometry = new THREE.BufferGeometry();
      const positions = new THREE.BufferAttribute(new Float32Array(64 * 3), 3);
      positions.setUsage(THREE.DynamicDrawUsage);
      geometry.setAttribute('position', positions);
      const material = new THREE.LineBasicMaterial({ color: 0x7ee0a6, transparent: true, opacity: 0.9, depthTest: false });
      const ring = new THREE.LineLoop(geometry, material);
      ring.visible = false;
      ring.renderOrder = 950;
      viewer.scene.add(ring);
      state.brushRing = ring;
      state.brushRingPositions = positions;
    }

    function updateBrushRingColor() {
      if (!state.brushRing) return;
      state.brushRing.material.color.setHex(activeToolColor());
    }

    function updateBrushRing(point) {
      if (!state.brushRing || state.tool === 'navigate' || !state.current || !point) {
        if (state.brushRing) state.brushRing.visible = false;
        return;
      }
      const radius = Number(els.radius.value) || 6;
      const positions = state.brushRingPositions;
      for (let index = 0; index < 64; index += 1) {
        const angle = index / 64 * Math.PI * 2;
        const x = point[0] + Math.cos(angle) * radius;
        const y = point[1] + Math.sin(angle) * radius;
        const height = global.VEILTerrain.sampleTerrainHeightAtLocal(viewer.terrainGrid, x, y) + 0.4;
        positions.setXYZ(index, x, height, -y);
      }
      positions.clearUpdateRanges?.();
      if (typeof positions.addUpdateRange === 'function') positions.addUpdateRange(0, positions.array.length);
      positions.needsUpdate = true;
      state.brushRing.visible = true;
    }

    function pointerSample(event) {
      return {
        pointerId: event.pointerId,
        clientX: event.clientX,
        clientY: event.clientY,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
      };
    }

    function cancelPendingPointerMove() {
      if (state.pointerMoveFrame) cancelAnimationFrame(state.pointerMoveFrame);
      state.pointerMoveFrame = null;
      state.pendingPointerMove = null;
    }

    function processPointerMove(sample) {
      if (!sample) return;
      if ((sample.ctrlKey || sample.metaKey) && !state.stroke) {
        if (state.brushRing) state.brushRing.visible = false;
        return;
      }
      const picked = pickAt(sample.clientX, sample.clientY);
      if (picked) updateBrushRing(picked.point);
      if (state.stroke?.pointerId === sample.pointerId && !picked) state.stroke.currentPoint = null;
      if (!state.stroke || state.stroke.pointerId !== sample.pointerId || !picked) return;
      state.stroke.currentPoint = picked.point;
      const [lx, ly] = state.stroke.lastClient;
      const travel = Math.hypot(sample.clientX - lx, sample.clientY - ly);
      state.stroke.travelPx += travel;
      if (travel >= 3) {
        const last = state.stroke.points[state.stroke.points.length - 1];
        if (Math.hypot(picked.point[0] - last[0], picked.point[1] - last[1]) >= 0.35) {
          state.stroke.points.push(picked.point);
          updateLivePreview(state.stroke, last);
        }
        state.stroke.lastClient = [sample.clientX, sample.clientY];
      }
    }

    function flushPendingPointerMove(sample = null) {
      if (sample) state.pendingPointerMove = sample;
      if (state.pointerMoveFrame) cancelAnimationFrame(state.pointerMoveFrame);
      state.pointerMoveFrame = null;
      const latest = state.pendingPointerMove;
      state.pendingPointerMove = null;
      processPointerMove(latest);
    }

    async function onPointerDown(event) {
      if (event.ctrlKey || event.metaKey) {
        cancelPendingPointerMove();
        if (state.brushRing) state.brushRing.visible = false;
        return;
      }
      if (!state.current || state.tool === 'navigate' || event.button !== 0) return;
      if (brushPointerBlocked(state)) {
        // Never let a temporarily unavailable brush fall through to
        // OrbitControls and turn an intended edit into a camera drag.
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      cancelPendingPointerMove();
      const pending = { ...pointerSample(event), cancelled: false, tool: state.tool };
      state.pendingPointerStart = pending;
      if (state.viewMode === 'baseline') {
        state.viewMode = 'planned';
        els.viewMode.querySelectorAll('[data-plan-view]').forEach((button) =>
          button.classList.toggle('active', button.dataset.planView === 'planned'));
        state.busy = true;
        setSaveState('loading', 'busy');
        renderCurrent();
        try {
          await applyCurrentView();
          setSaveState('saved', 'saved');
        } catch (error) {
          pending.cancelled = true;
          state.pendingPointerStart = null;
          setSaveState('error', 'error');
          setStatus(`Could not open the planned land: ${error.message}`, 'err');
          return;
        } finally {
          state.busy = false;
          renderCurrent();
        }
      }
      if (pending.cancelled || state.pendingPointerStart !== pending) return;
      const speciesDimensions = (pending.tool === 'tree' || pending.tool === 'shrub')
        ? stageDimensions() : null;
      const treeRenderSpec = pending.tool === 'tree' && speciesDimensions?.species
        ? await viewer.vegetationRenderer?.ensurePlanTreeRenderSpec?.(
          speciesDimensions.species.asset_key, speciesDimensions.species.type)
        : null;
      if (pending.cancelled || state.pendingPointerStart !== pending) return;
      state.pendingPointerStart = null;
      const picked = pickAt(pending.clientX, pending.clientY);
      if (!picked) return;
      clearLivePreview();
      updateBrushRing(picked.point);
      canvas.setPointerCapture?.(pending.pointerId);
      viewer.controls.enabled = false;
      state.stroke = {
        editId: editId(),
        tool: pending.tool,
        pointerId: pending.pointerId,
        points: [picked.point],
        currentPoint: picked.point,
        lastClient: [pending.clientX, pending.clientY],
        travelPx: 0,
        radius: Number(els.radius.value) || 6,
        amount: Number(els.earth.value) || 0.35,
        spacing: Number(els.spacing.value) || 6,
        stage: els.stage.value,
        speciesDimensions,
        treeRenderSpec,
        accumulationStamps: [],
        terrainFrame: null,
        terrainAccumulationStopped: false,
        previewPlantPoints: [],
        previewPlantSlots: [],
        plantVisitedCells: new Set(),
        plantPreviewMode: null,
      };
      updateLivePreview(state.stroke);
      startTerrainAccumulation(state.stroke);
    }

    function onPointerMove(event) {
      if (!state.stroke && (state.tool === 'navigate' || !state.current)) {
        cancelPendingPointerMove();
        if (state.brushRing) state.brushRing.visible = false;
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !state.stroke) {
        cancelPendingPointerMove();
        if (state.brushRing) state.brushRing.visible = false;
        return;
      }
      if (state.stroke?.pointerId === event.pointerId) {
        event.preventDefault();
        event.stopPropagation();
      }
      state.pendingPointerMove = pointerSample(event);
      if (state.pointerMoveFrame) return;
      state.pointerMoveFrame = requestAnimationFrame(() => {
        state.pointerMoveFrame = null;
        const latest = state.pendingPointerMove;
        state.pendingPointerMove = null;
        processPointerMove(latest);
      });
    }

    function onPointerUp(event) {
      if (state.pendingPointerStart?.pointerId === event.pointerId) {
        state.pendingPointerStart.cancelled = true;
        state.pendingPointerStart = null;
        cancelPendingPointerMove();
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (!state.stroke || state.stroke.pointerId !== event.pointerId) return;
      event.preventDefault();
      event.stopPropagation();
      flushPendingPointerMove(pointerSample(event));
      canvas.releasePointerCapture?.(event.pointerId);
      finishStroke();
    }

    function cancelStroke() {
      cancelPendingPointerMove();
      if (state.pendingPointerStart) {
        state.pendingPointerStart.cancelled = true;
        state.pendingPointerStart = null;
      }
      if (!state.stroke && !state.livePreview) return;
      const pointerId = state.stroke?.pointerId;
      stopTerrainAccumulation(state.stroke);
      state.stroke = null;
      if (pointerId !== undefined && canvas.hasPointerCapture?.(pointerId)) {
        canvas.releasePointerCapture?.(pointerId);
      }
      viewer.controls.enabled = true;
      clearLivePreview();
      setStatus('Gesture cancelled.');
    }

    function stopEditingMode() {
      cancelPendingPointerMove();
      const wasEditing = state.tool !== 'navigate' || !!state.stroke
        || !!state.pendingPointerStart || !!state.livePreview;
      if (state.stroke || state.pendingPointerStart) cancelStroke();
      else if (state.livePreview && !state.busy) clearLivePreview();
      state.tool = 'navigate';
      viewer.controls.enabled = true;
      if (state.brushRing) state.brushRing.visible = false;
      if (wasEditing) renderToolFields();
      return wasEditing;
    }

    function onPaneChange(event) {
      if (event?.detail?.open && event.detail.mode === 'plan') return;
      stopEditingMode();
    }

    els.select.addEventListener('change', () => loadPlan(els.select.value));
    els.revision.addEventListener('change', () => {
      if (state.current && els.revision.value) {
        loadPlan(state.current.plan.plan_id, els.revision.value);
      }
    });
    els.newPlan.addEventListener('click', async () => {
      const name = global.prompt?.('Plan name', 'New land plan');
      if (name == null) return;
      state.busy = true;
      setSaveState('creating', 'busy');
      try {
        const payload = await request('/api/plans', { method: 'POST', body: JSON.stringify({ name, author: 'viewer' }) });
        state.current = payload;
        state.previewEdits = [];
        state.undo = [];
        state.redo = [];
        await refreshPlans();
        await applyCurrentView();
        setSaveState('saved', 'saved');
      } catch (error) {
        setSaveState('error', 'error');
        setStatus(`Could not create plan: ${error.message}`, 'err');
      } finally {
        state.busy = false;
        renderCurrent();
      }
    });
    els.branch.addEventListener('click', async () => {
      if (!state.current || state.busy) return;
      const name = global.prompt?.('Branch name', `${planName()} alternative`);
      if (name == null) return;
      state.busy = true;
      try {
        const payload = await request(`/api/plans/${encodeURIComponent(state.current.plan.plan_id)}/branch`, {
          method: 'POST', body: JSON.stringify({ name, revision_id: state.current.revision.revision_id, author: 'viewer' }),
        });
        state.current = payload;
        state.previewEdits = [];
        state.undo = [];
        state.redo = [];
        await refreshPlans();
        await applyCurrentView();
        setSaveState('saved', 'saved');
      } catch (error) {
        setStatus(`Branch failed: ${error.message}`, 'err');
      } finally {
        state.busy = false;
        renderCurrent();
      }
    });
    els.saveVersion.addEventListener('click', async () => {
      if (!state.current || state.busy) return;
      const name = global.prompt?.('Version name', `Version ${state.current.history.length + 1}`);
      if (name == null) return;
      state.busy = true;
      setSaveState('saving', 'busy');
      try {
        state.current = await request(`/api/plans/${encodeURIComponent(state.current.plan.plan_id)}/checkpoint`, {
          method: 'POST', body: JSON.stringify({ expected_revision_id: state.current.plan.head_revision_id, name, author: 'viewer' }),
        });
        state.previewEdits = [];
        await refreshPlans();
        await applyCurrentView();
        setSaveState('saved', 'saved');
      } catch (error) {
        setSaveState('error', 'error');
        setStatus(`Could not save version: ${error.message}`, 'err');
      } finally {
        state.busy = false;
        renderCurrent();
      }
    });
    els.discard.addEventListener('click', async () => {
      if (!state.current || state.busy) return;
      const planId = state.current.plan.plan_id;
      const name = state.current.plan.name;
      if (!global.confirm?.(`Discard “${name}”?\n\nIts saved versions will be hidden from the plan list.`)) return;
      cancelStroke();
      state.busy = true;
      setSaveState('discarding', 'busy');
      renderCurrent();
      try {
        await request(`/api/plans/${encodeURIComponent(planId)}/update`, {
          method: 'POST', body: JSON.stringify({ archived: true }),
        });
        await refreshPlans();
        await loadPlan('');
        setSaveState('ready');
        setStatus(`“${name}” was discarded. The baseline is active.`);
      } catch (error) {
        setSaveState('error', 'error');
        setStatus(`Could not discard plan: ${error.message}`, 'err');
      } finally {
        state.busy = false;
        renderCurrent();
      }
    });
    els.tools.addEventListener('click', (event) => {
      const button = event.target.closest('[data-plan-tool]');
      if (!button || button.disabled) return;
      cancelPendingPointerMove();
      if (state.stroke || state.pendingPointerStart || state.livePreview) cancelStroke();
      state.tool = button.dataset.planTool;
      renderToolFields();
    });
    els.radius.addEventListener('input', () => { els.radiusValue.textContent = `${els.radius.value} m`; });
    els.species.addEventListener('input', () => renderStages());
    els.viewMode.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-plan-view]');
      if (!button || !state.current || state.busy || state.saving) return;
      if (state.stroke || state.pendingPointerStart || state.livePreview) cancelStroke();
      state.viewMode = button.dataset.planView;
      els.viewMode.querySelectorAll('[data-plan-view]').forEach((candidate) =>
        candidate.classList.toggle('active', candidate === button));
      await applyCurrentView();
    });
    els.undo.addEventListener('click', async () => {
      if (!state.undo.length || state.busy || state.saving) return;
      const target = state.undo.pop();
      state.redo.push(edits());
      await commit(target, 'Undo plan edit', { recordHistory: false });
      updateHistoryButtons();
    });
    els.redo.addEventListener('click', async () => {
      if (!state.redo.length || state.busy || state.saving) return;
      const target = state.redo.pop();
      state.undo.push(edits());
      await commit(target, 'Redo plan edit', { recordHistory: false });
      updateHistoryButtons();
    });

    canvas.addEventListener('pointerdown', onPointerDown, true);
    canvas.addEventListener('pointermove', onPointerMove, true);
    canvas.addEventListener('pointerup', onPointerUp, true);
    canvas.addEventListener('pointercancel', cancelStroke, true);
    canvas.addEventListener('pointerleave', () => {
      if (!state.stroke) {
        cancelPendingPointerMove();
        if (state.brushRing) state.brushRing.visible = false;
      }
    });
    canvas.addEventListener('wheel', (event) => {
      if (!state.current || state.tool === 'navigate' || !event.shiftKey) return;
      event.preventDefault();
      const next = Math.max(Number(els.radius.min), Math.min(Number(els.radius.max),
        Number(els.radius.value) + (event.deltaY > 0 ? -1 : 1)));
      els.radius.value = next;
      els.radiusValue.textContent = `${next} m`;
    }, { passive: false, capture: true });
    document.addEventListener('keydown', (event) => {
      const target = event.target;
      if (target?.matches?.('input, textarea, select')) return;
      if (event.key === 'Escape') cancelStroke();
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        if (event.shiftKey) els.redo.click(); else els.undo.click();
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') {
        event.preventDefault();
        els.redo.click();
      }
    });
    document.addEventListener('veil:panechange', onPaneChange);

    createBrushRing();
    renderToolFields();
    renderCurrent();
    Promise.all([
      refreshPlans(),
      request('/api/plans/catalog').then((payload) => { state.catalog = payload; renderSpecies(); }),
    ]).catch((error) => setStatus(`Plan service unavailable: ${error.message}`, 'err'));

    return {
      state,
      isEditing: () => !!state.current && (state.tool !== 'navigate' || !!state.stroke),
      activeRevisionId: () => state.current?.revision?.revision_id || null,
      activePlanId: () => state.current?.plan?.plan_id || null,
      assetRoot: () => state.current?.materialized?.asset_root || null,
      async runSimulation(simulator, parameters) {
        if (state.saveWorker) {
          const saved = await state.saveWorker;
          if (!saved) return null;
        }
        if (!state.current) return null;
        const planId = state.current.plan.plan_id;
        const revisionId = state.current.revision.revision_id;
        return request(
          `/api/plans/${encodeURIComponent(planId)}/revisions/${encodeURIComponent(revisionId)}` +
          `/simulations/${encodeURIComponent(simulator)}`,
          { method: 'POST', body: JSON.stringify({ parameters: parameters || {} }) }
        );
      },
      async applyDirective(directive) {
        if (!directive || typeof directive !== 'object') {
          if (!clearDirectiveState(state)) return;
          if (state.current) {
            els.viewMode.querySelectorAll('[data-plan-view]').forEach((button) =>
              button.classList.toggle('active', button.dataset.planView === state.viewMode));
            await applyCurrentView();
            renderToolFields();
            renderCurrent();
          }
          return;
        }
        global.VEILShell?.showPane?.('plan');
        if (directive.plan_id) {
          await loadPlan(directive.plan_id, directive.revision_id || null);
        }
        if (!state.current) return;
        state.directiveActive = true;
        state.previewEdits = Array.isArray(directive.preview_edits)
          ? clone(directive.preview_edits) : [];
        state.tool = 'navigate';
        state.viewMode = directive.view === 'baseline' ? 'baseline'
          : (directive.view === 'planned' && !state.previewEdits.length ? 'planned' : 'difference');
        els.viewMode.querySelectorAll('[data-plan-view]').forEach((button) =>
          button.classList.toggle('active', button.dataset.planView === state.viewMode));
        await applyCurrentView();
        if (state.previewEdits.length) {
          setStatus(`${directive.label || 'GAIA proposal'} preview · not applied`, 'warn');
        }
        renderToolFields();
      },
      reload: () => state.current ? loadPlan(state.current.plan.plan_id) : refreshPlans(),
      loadPlan,
      stopEditing: stopEditingMode,
      destroy() {
        document.removeEventListener('veil:panechange', onPaneChange);
        cancelPendingPointerMove();
        cancelStroke();
        clearLivePreview();
        clearObject(state.overlay);
        viewer.scene.remove(state.overlay);
        viewer.scene.remove(state.plantPreviewGroup);
        Object.values(state.plantPreviewMeshes).forEach((record) => record.mesh.dispose?.());
        previewResources.trunkGeometry.dispose();
        previewResources.canopyGeometry.dispose();
        previewResources.shrubGeometry.dispose();
        previewResources.trunkMaterial.dispose();
        previewResources.evergreenCanopyMaterial.dispose();
        previewResources.deciduousCanopyMaterial.dispose();
        previewResources.shrubMaterial.dispose();
        if (state.brushRing) {
          viewer.scene.remove(state.brushRing);
          state.brushRing.geometry.dispose();
          state.brushRing.material.dispose();
        }
      },
    };
  }

  global.VEILPlan = {
    create,
    _test: {
      applyTerrainPreviewInfluence,
      brushPointerBlocked,
      clearDirectiveState,
      drainOrderedEditQueue,
      incrementalPlantPointsForSegment,
      isTerrainBrush,
      markInstanceMatrixRange,
    },
  };
})(window);
