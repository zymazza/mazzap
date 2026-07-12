(function attachVegetationHelpers(global) {
  const { THREE, VEILTerrain } = global;

  const BUILDING_EXCLUSION_BUFFER_METERS = 1.75;
  const TREE_STRIDE = 6; // x, y, height, radius, typeFlag (1=evergreen, 0=deciduous), assetId
  const SHRUB_STRIDE = 3;
  // Fire-reveal tinting: a passing fire front runs bright yellow -> orange, then
  // cools through char brown to near-black as the burn ages behind the front.
  const SHRUB_GEOMETRY_RADIUS = 0.7;
  const PLAN_TERRAIN_INDEX_CELL_METERS = 16;
  const FIRE_COLOR_WHITE = Object.freeze({ r: 1, g: 1, b: 1 });
  const FIRE_COLOR_HOT_YELLOW = '#ffcc33';
  const FIRE_COLOR_HOT_ORANGE = '#ff6a1a';
  const FIRE_COLOR_CHAR_BROWN = '#3a2a20';
  const FIRE_COLOR_CHAR_BLACK = '#1a1512';
  const TREE_LIBRARY_ASSETS = Object.freeze([
    {
      id: 1,
      key: 'pine',
      url: '/assets/tree-library/pine_lod3.obj',
      label: 'Pine',
      leafColor: '#24573a',
      barkColor: '#6a5137',
    },
    {
      id: 2,
      key: 'spruce',
      url: '/assets/tree-library/spruce_lod3.obj',
      label: 'Spruce / Hemlock',
      leafColor: '#173f31',
      barkColor: '#584331',
    },
    {
      id: 3,
      key: 'fir',
      url: '/assets/tree-library/fir_lod3.obj',
      label: 'Fir',
      leafColor: '#1e4b36',
      barkColor: '#5b4733',
    },
    {
      id: 4,
      key: 'birch',
      url: '/assets/tree-library/birch_lod3.obj',
      label: 'Birch / Aspen',
      leafColor: '#678c43',
      barkColor: '#c9c4ad',
    },
    {
      id: 5,
      key: 'maple',
      url: '/assets/tree-library/maple_lod3.obj',
      label: 'Maple',
      leafColor: '#5d853e',
      barkColor: '#6d573f',
    },
    {
      id: 6,
      key: 'beech',
      url: '/assets/tree-library/beech_lod3.obj',
      label: 'Beech',
      leafColor: '#6f8741',
      barkColor: '#8a8068',
    },
    {
      id: 7,
      key: 'elm',
      url: '/assets/tree-library/elm_lod3.obj',
      label: 'Elm',
      leafColor: '#5f8b45',
      barkColor: '#6a5138',
    },
  ]);
  const TREE_LIBRARY_ASSET_BY_ID = Object.freeze(
    Object.fromEntries(TREE_LIBRARY_ASSETS.map((asset) => [asset.id, asset]))
  );
  const TREE_LIBRARY_ASSET_BY_KEY = Object.freeze(
    Object.fromEntries(TREE_LIBRARY_ASSETS.map((asset) => [asset.key, asset]))
  );
  const SPECIES_TREE_ASSET_KEYS = Object.freeze({
    'Eastern White Pine': 'pine',
    'Red Pine': 'pine',
    'Eastern Hemlock': 'spruce',
    'Red Spruce': 'spruce',
    'Balsam Fir': 'fir',
    'Paper Birch': 'birch',
    'Yellow Birch': 'birch',
    'Bigtooth Aspen': 'birch',
    'Sugar Maple': 'maple',
    'Red Maple': 'maple',
    'American Beech': 'beech',
  });
  const EMPTY_TREE_DATA = Object.freeze({
    assetCounts: Object.freeze({}),
    categoryCounts: Object.freeze({ evergreen: 0, deciduous: 0 }),
    count: 0,
    entityIds: Object.freeze([]),
    values: new Float32Array(0),
  });
  const EMPTY_SHRUB_DATA = Object.freeze({
    count: 0,
    entityIds: Object.freeze([]),
    values: new Float32Array(0),
  });

  function createPlanTerrainIndex(cellSize = PLAN_TERRAIN_INDEX_CELL_METERS) {
    return {
      cellSize: Math.max(1, Number(cellSize) || PLAN_TERRAIN_INDEX_CELL_METERS),
      buckets: new Map(),
      slots: [],
    };
  }

  function addPlanTerrainSlot(index, slot) {
    if (!index || !slot || !Number.isFinite(slot.x) || !Number.isFinite(slot.y)) return;
    const column = Math.floor(slot.x / index.cellSize);
    const row = Math.floor(slot.y / index.cellSize);
    const key = `${column}:${row}`;
    index.slots.push(slot);
    if (!index.buckets.has(key)) index.buckets.set(key, []);
    index.buckets.get(key).push(slot);
  }

  function planTerrainSlotsInBounds(index, bounds) {
    if (!index || !bounds || ![bounds.minX, bounds.maxX, bounds.minY, bounds.maxY].every(Number.isFinite)) {
      return [];
    }
    const firstColumn = Math.floor(bounds.minX / index.cellSize);
    const lastColumn = Math.floor(bounds.maxX / index.cellSize);
    const firstRow = Math.floor(bounds.minY / index.cellSize);
    const lastRow = Math.floor(bounds.maxY / index.cellSize);
    const slots = [];
    for (let column = firstColumn; column <= lastColumn; column += 1) {
      for (let row = firstRow; row <= lastRow; row += 1) {
        const candidates = index.buckets.get(`${column}:${row}`) || [];
        candidates.forEach((slot) => {
          if (slot.x >= bounds.minX && slot.x <= bounds.maxX
              && slot.y >= bounds.minY && slot.y <= bounds.maxY) slots.push(slot);
        });
      }
    }
    return slots;
  }

  function planSlotInstances(slot) {
    if (Array.isArray(slot?.instances)) return slot.instances;
    return (slot?.meshes || []).map((mesh) => ({
      mesh,
      instanceIndex: slot.instanceIndex,
    }));
  }

  function recordTouchedInstance(touched, mesh, instanceIndex) {
    if (!touched || !mesh || !Number.isInteger(instanceIndex)) return;
    if (touched instanceof Map) {
      if (!touched.has(mesh)) touched.set(mesh, new Set());
      touched.get(mesh).add(instanceIndex);
      return;
    }
    touched.add?.(mesh);
  }

  function markInstanceMatrixIndices(mesh, indices) {
    const attribute = mesh?.instanceMatrix;
    if (!attribute || !indices?.size) return;
    const ordered = [...indices]
      .filter((index) => Number.isInteger(index) && index >= 0)
      .sort((left, right) => left - right);
    if (!ordered.length) return;
    if (typeof attribute.addUpdateRange === 'function') {
      let first = ordered[0];
      let last = first;
      for (let index = 1; index <= ordered.length; index += 1) {
        const next = ordered[index];
        if (next === last + 1) {
          last = next;
          continue;
        }
        attribute.addUpdateRange(first * 16, (last - first + 1) * 16);
        first = next;
        last = next;
      }
    }
    attribute.needsUpdate = true;
  }

  function markInstanceMatrixFull(mesh) {
    const attribute = mesh?.instanceMatrix;
    if (!attribute) return;
    attribute.clearUpdateRanges?.();
    attribute.needsUpdate = true;
  }

  function flushInstanceMatrixUpdates(touched) {
    if (touched instanceof Map) {
      touched.forEach((indices, mesh) => markInstanceMatrixIndices(mesh, indices));
      return;
    }
    touched?.forEach?.((mesh) => { if (mesh?.instanceMatrix) mesh.instanceMatrix.needsUpdate = true; });
  }

  function capturePlanSlotMatrices(slot) {
    const instances = planSlotInstances(slot);
    slot.originalMatrices = instances.map(({ mesh, instanceIndex }) => {
      const values = mesh?.instanceMatrix?.array;
      const offset = Number(instanceIndex) * 16;
      return values && Number.isInteger(instanceIndex) && offset + 16 <= values.length
        ? values.slice(offset, offset + 16) : null;
    });
  }

  function hidePlanTerrainSlot(slot, touched = null) {
    if (!slot || slot.planRemovalHidden) return false;
    capturePlanSlotMatrices(slot);
    planSlotInstances(slot).forEach(({ mesh, instanceIndex }) => {
      const values = mesh?.instanceMatrix?.array;
      const offset = Number(instanceIndex) * 16;
      if (!values || !Number.isInteger(instanceIndex) || offset + 16 > values.length) return;
      // Preserve translation and the homogeneous component, but collapse all
      // three basis vectors so the instance disappears without repacking.
      [0, 1, 2, 4, 5, 6, 8, 9, 10].forEach((component) => {
        values[offset + component] = 0;
      });
      recordTouchedInstance(touched, mesh, instanceIndex);
    });
    slot.planRemovalHidden = true;
    return true;
  }

  function restorePlanTerrainSlot(slot, touched = null) {
    if (!slot?.planRemovalHidden || !Array.isArray(slot.originalMatrices)) return false;
    planSlotInstances(slot).forEach(({ mesh, instanceIndex }, index) => {
      const values = mesh?.instanceMatrix?.array;
      const original = slot.originalMatrices[index];
      const offset = Number(instanceIndex) * 16;
      if (!values || !original || !Number.isInteger(instanceIndex) || offset + 16 > values.length) return;
      values.set(original, offset);
      recordTouchedInstance(touched, mesh, instanceIndex);
    });
    slot.originalMatrices = null;
    slot.planRemovalHidden = false;
    return true;
  }

  function shiftPlanTerrainSlot(slot, nextGroundHeight, touchedMeshes = new Set()) {
    if (!slot || !Number.isFinite(nextGroundHeight) || !Number.isFinite(slot.groundHeight)) return false;
    const delta = nextGroundHeight - slot.groundHeight;
    if (Math.abs(delta) <= 1e-6) return false;
    planSlotInstances(slot).forEach(({ mesh, instanceIndex }, index) => {
      const values = mesh?.instanceMatrix?.array;
      const matrixOffset = Number(instanceIndex) * 16 + 13;
      if (!values || !Number.isInteger(instanceIndex) || matrixOffset >= values.length) return;
      values[matrixOffset] += delta;
      if (slot.originalMatrices?.[index]) slot.originalMatrices[index][13] += delta;
      recordTouchedInstance(touchedMeshes, mesh, instanceIndex);
    });
    slot.groundHeight = nextGroundHeight;
    return true;
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

  function treeTypeKey(type) {
    return type === 'deciduous' ? 'deciduous' : 'evergreen';
  }

  function treeAssetIdFor(row) {
    const species = String(row?.species || '').trim();
    const key = SPECIES_TREE_ASSET_KEYS[species] ||
      (treeTypeKey(row?.type) === 'deciduous' ? 'maple' : 'pine');
    return TREE_LIBRARY_ASSET_BY_KEY[key]?.id || TREE_LIBRARY_ASSET_BY_KEY.pine.id;
  }

  function treeAssetKeyFromId(id) {
    return TREE_LIBRARY_ASSET_BY_ID[id]?.key || 'pine';
  }

  function isBarkMaterialName(name) {
    return String(name || '').toLowerCase().includes('bark');
  }

  function pointInRing(point, ring) {
    let inside = false;
    for (let index = 0, prev = ring.length - 1; index < ring.length; prev = index, index += 1) {
      const xi = ring[index][0];
      const yi = ring[index][1];
      const xj = ring[prev][0];
      const yj = ring[prev][1];
      const intersects =
        yi > point[1] !== yj > point[1] &&
        point[0] < ((xj - xi) * (point[1] - yi)) / ((yj - yi) || 1e-12) + xi;
      if (intersects) {
        inside = !inside;
      }
    }
    return inside;
  }

  function pointInPolygon(point, polygonRings) {
    if (!Array.isArray(polygonRings) || !polygonRings.length || !pointInRing(point, polygonRings[0])) {
      return false;
    }
    for (let holeIndex = 1; holeIndex < polygonRings.length; holeIndex += 1) {
      if (pointInRing(point, polygonRings[holeIndex])) {
        return false;
      }
    }
    return true;
  }

  function polygonCentroid(rings) {
    const ring = rings?.[0] || [];
    if (!ring.length) {
      return { x: 0, y: 0 };
    }
    let sumX = 0;
    let sumY = 0;
    ring.forEach(([x, y]) => {
      sumX += x;
      sumY += y;
    });
    return {
      x: sumX / ring.length,
      y: sumY / ring.length,
    };
  }

  function clampToBounds(x, y, bounds, inset = 0.6) {
    if (!bounds) {
      return { x, y };
    }
    return {
      x: Math.min(bounds.maxX - inset, Math.max(bounds.minX + inset, x)),
      y: Math.min(bounds.maxY - inset, Math.max(bounds.minY + inset, y)),
    };
  }

  function pointToSegmentDistance(x, y, ax, ay, bx, by) {
    const abx = bx - ax;
    const aby = by - ay;
    const abLengthSq = abx * abx + aby * aby;
    const t =
      abLengthSq > 0 ? Math.max(0, Math.min(1, ((x - ax) * abx + (y - ay) * aby) / abLengthSq)) : 0;
    const nearestX = ax + abx * t;
    const nearestY = ay + aby * t;
    return Math.hypot(x - nearestX, y - nearestY);
  }

  function trimFloat32Array(values, usedCount, stride) {
    if (usedCount * stride === values.length) {
      return values;
    }
    return values.slice(0, usedCount * stride);
  }

  function isPackedItemsArray(value) {
    return Array.isArray(value) || ArrayBuffer.isView(value);
  }

  function normalizeTreePayload(payload) {
    if (payload && isPackedItemsArray(payload.items) && Number(payload.stride) >= 5) {
      const sourceStride = Number(payload.stride);
      const source = payload.items;
      const sourceEntityIds = payload.entity_ids || payload.ids || [];
      const maxCount = Math.floor(source.length / sourceStride);
      const values = new Float32Array(maxCount * TREE_STRIDE);
      const entityIds = [];
      const assetCounts = {};
      const categoryCounts = { evergreen: 0, deciduous: 0 };
      let count = 0;

      for (let index = 0; index < maxCount; index += 1) {
        const sourceOffset = index * sourceStride;
        const x = Number(source[sourceOffset]);
        const y = Number(source[sourceOffset + 1]);
        const height = Number(source[sourceOffset + 2]);
        const radius = Number(source[sourceOffset + 3]);
        if (![x, y, height, radius].every(Number.isFinite)) {
          continue;
        }
        const evergreen = sourceStride > 4 ? Number(source[sourceOffset + 4]) > 0.5 : true;
        const sourceAssetId = sourceStride > 5 ? Number(source[sourceOffset + 5]) : NaN;
        const assetId = TREE_LIBRARY_ASSET_BY_ID[sourceAssetId]?.id ||
          TREE_LIBRARY_ASSET_BY_KEY[evergreen ? 'pine' : 'maple'].id;
        const assetKey = treeAssetKeyFromId(assetId);
        const targetOffset = count * TREE_STRIDE;
        values[targetOffset] = x;
        values[targetOffset + 1] = y;
        values[targetOffset + 2] = height;
        values[targetOffset + 3] = radius;
        values[targetOffset + 4] = evergreen ? 1 : 0;
        values[targetOffset + 5] = assetId;
        entityIds[count] = String(sourceEntityIds[index] || '');
        categoryCounts[evergreen ? 'evergreen' : 'deciduous'] += 1;
        assetCounts[assetKey] = (assetCounts[assetKey] || 0) + 1;
        count += 1;
      }

      return {
        assetCounts,
        categoryCounts,
        count,
        entityIds,
        values: trimFloat32Array(values, count, TREE_STRIDE),
      };
    }

    const rows = Array.isArray(payload) ? payload : [];
    if (!rows.length) {
      return EMPTY_TREE_DATA;
    }

    const values = new Float32Array(rows.length * TREE_STRIDE);
    const entityIds = [];
    const assetCounts = {};
    const categoryCounts = { evergreen: 0, deciduous: 0 };
    let count = 0;

    rows.forEach((row) => {
      const x = Number(row?.x);
      const y = Number(row?.y);
      const height = Number(row?.height);
      const radius = Number(row?.radius);
      if (![x, y, height, radius].every(Number.isFinite)) {
        return;
      }
      const key = treeTypeKey(row?.type);
      const assetId = treeAssetIdFor(row);
      const assetKey = treeAssetKeyFromId(assetId);
      const offset = count * TREE_STRIDE;
      values[offset] = x;
      values[offset + 1] = y;
      values[offset + 2] = height;
      values[offset + 3] = radius;
      values[offset + 4] = key === 'evergreen' ? 1 : 0;
      values[offset + 5] = assetId;
      entityIds[count] = String(row?.id || '');
      categoryCounts[key] += 1;
      assetCounts[assetKey] = (assetCounts[assetKey] || 0) + 1;
      count += 1;
    });

    return {
      assetCounts,
      categoryCounts,
      count,
      entityIds,
      values: trimFloat32Array(values, count, TREE_STRIDE),
    };
  }

  function parseObjVertexIndex(token, vertexCount) {
    const raw = Number(String(token || '').split('/')[0]);
    if (!Number.isFinite(raw) || raw === 0) {
      return null;
    }
    return raw < 0 ? vertexCount + raw : raw - 1;
  }

  function createTreeLibraryGeometry(objText) {
    const vertices = [];
    const facesByKind = {
      bark: [],
      leaf: [],
    };
    let currentKind = 'leaf';
    const bbox = {
      minX: Number.POSITIVE_INFINITY,
      minY: Number.POSITIVE_INFINITY,
      minZ: Number.POSITIVE_INFINITY,
      maxX: Number.NEGATIVE_INFINITY,
      maxY: Number.NEGATIVE_INFINITY,
      maxZ: Number.NEGATIVE_INFINITY,
    };

    objText.split(/\r?\n/).forEach((line) => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) {
        return;
      }
      const parts = trimmed.split(/\s+/);
      if (parts[0] === 'v' && parts.length >= 4) {
        const x = Number(parts[1]);
        const y = Number(parts[2]);
        const z = Number(parts[3]);
        if (![x, y, z].every(Number.isFinite)) {
          return;
        }
        vertices.push([x, y, z]);
        bbox.minX = Math.min(bbox.minX, x);
        bbox.minY = Math.min(bbox.minY, y);
        bbox.minZ = Math.min(bbox.minZ, z);
        bbox.maxX = Math.max(bbox.maxX, x);
        bbox.maxY = Math.max(bbox.maxY, y);
        bbox.maxZ = Math.max(bbox.maxZ, z);
        return;
      }
      if (parts[0] === 'usemtl') {
        currentKind = isBarkMaterialName(parts.slice(1).join(' ')) ? 'bark' : 'leaf';
        return;
      }
      if (parts[0] !== 'f' || parts.length < 4) {
        return;
      }
      const indices = parts
        .slice(1)
        .map((token) => parseObjVertexIndex(token, vertices.length))
        .filter((index) => index !== null && vertices[index]);
      if (indices.length < 3) {
        return;
      }
      for (let index = 1; index < indices.length - 1; index += 1) {
        facesByKind[currentKind].push(indices[0], indices[index], indices[index + 1]);
      }
    });

    if (!vertices.length || !Number.isFinite(bbox.minX)) {
      return null;
    }

    const centerX = (bbox.minX + bbox.maxX) / 2;
    const centerZ = (bbox.minZ + bbox.maxZ) / 2;
    const makeGeometry = (indices) => {
      if (!indices.length) {
        return null;
      }
      const positions = new Float32Array(indices.length * 3);
      indices.forEach((vertexIndex, index) => {
        const vertex = vertices[vertexIndex];
        const offset = index * 3;
        positions[offset] = vertex[0] - centerX;
        positions[offset + 1] = vertex[1] - bbox.minY;
        positions[offset + 2] = vertex[2] - centerZ;
      });
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geometry.computeVertexNormals();
      return geometry;
    };

    const parts = [
      { kind: 'bark', geometry: makeGeometry(facesByKind.bark) },
      { kind: 'leaf', geometry: makeGeometry(facesByKind.leaf) },
    ].filter((part) => part.geometry);

    return {
      diameter: Math.max(1, bbox.maxX - bbox.minX, bbox.maxZ - bbox.minZ),
      height: Math.max(1, bbox.maxY - bbox.minY),
      parts,
    };
  }

  function normalizeShrubPayload(payload) {
    if (payload && isPackedItemsArray(payload.items) && Number(payload.stride) >= SHRUB_STRIDE) {
      const sourceStride = Number(payload.stride);
      const source = payload.items;
      const sourceEntityIds = payload.entity_ids || payload.ids || [];
      const maxCount = Math.floor(source.length / sourceStride);
      const values = new Float32Array(maxCount * SHRUB_STRIDE);
      const entityIds = [];
      let count = 0;

      for (let index = 0; index < maxCount; index += 1) {
        const sourceOffset = index * sourceStride;
        const x = Number(source[sourceOffset]);
        const y = Number(source[sourceOffset + 1]);
        const baseScale = Number(source[sourceOffset + 2]);
        if (![x, y, baseScale].every(Number.isFinite)) {
          continue;
        }
        const targetOffset = count * SHRUB_STRIDE;
        values[targetOffset] = x;
        values[targetOffset + 1] = y;
        values[targetOffset + 2] = baseScale;
        entityIds[count] = String(sourceEntityIds[index] || '');
        count += 1;
      }

      return {
        count,
        entityIds,
        values: trimFloat32Array(values, count, SHRUB_STRIDE),
      };
    }

    const rows = Array.isArray(payload) ? payload : [];
    if (!rows.length) {
      return EMPTY_SHRUB_DATA;
    }

    const values = new Float32Array(rows.length * SHRUB_STRIDE);
    const entityIds = [];
    let count = 0;

    rows.forEach((row) => {
      const x = Number(row?.x);
      const y = Number(row?.y);
      const baseScale = Number(row?.baseScale);
      if (![x, y, baseScale].every(Number.isFinite)) {
        return;
      }
      const offset = count * SHRUB_STRIDE;
      values[offset] = x;
      values[offset + 1] = y;
      values[offset + 2] = baseScale;
      entityIds[count] = String(row?.id || '');
      count += 1;
    });

    return {
      count,
      entityIds,
      values: trimFloat32Array(values, count, SHRUB_STRIDE),
    };
  }

  function passesDensity(kind, density, index, x, y, size) {
    if (density >= 0.999) {
      return true;
    }
    const key = `${kind}:${index}:${Math.round(x * 10)}:${Math.round(y * 10)}:${Math.round(size * 10)}`;
    return hashUnit(key) <= density;
  }

  function ensureFloat32Capacity(current, minItems, stride) {
    if (current && current.length >= minItems * stride) {
      return current;
    }
    return new Float32Array(Math.max(1, minItems) * stride);
  }

  function clamp01(value) {
    return Math.max(0, Math.min(1, value));
  }

  class VegetationRenderer {
    constructor(scene, options = {}) {
      this.scene = scene;
      this.onAssetLoad = typeof options.onAssetLoad === 'function' ? options.onAssetLoad : () => {};
      this.group = new THREE.Group();
      this.scene.add(this.group);
      this.disposed = false;
      this.renderFrameId = null;
      this.renderQueued = false;
      this.pendingAssetInvalidation = false;
      this.grid = null;
      this.data = {
        shrubs: EMPTY_SHRUB_DATA,
        trees: EMPTY_TREE_DATA,
      };
      this.density = {
        shrubs: 0.72,
        trees: 0.82,
      };
      this.typeFilter = 'all';
      this.avoidance = {
        buildingLines: [],
        buildingPolygons: [],
        hydrologyLines: [],
        roadLines: [],
        clipBounds: null,
      };
      this.renderStats = {
        shrubs: 0,
        trees: 0,
      };
      this.shadowsEnabled = false;
      // Fire-reveal state driven by app.js applyFireTint(); appliedKey lets a
      // re-render skip retinting when the reveal time/grid have not changed.
      this.fireTint = {
        active: false,
        sampleArrival: null,
        revealTime: null,
        duration: null,
        appliedKey: null,
      };
      // Plan removals mutate only the touched instance matrices. Preview
      // matrices are restorable; committed IDs remain hidden optimistically
      // until an authoritative plan/revision load replaces the land.
      this.planRemovalPreview = null;
      this.planCommittedRemovalIds = new Set();
      this.planExternalTerrainSlots = [];
      // Every effective entity is indexed by x/y; rendered entities additionally
      // carry their mesh + instance slots for live terrain/removal mutations.
      this.planTerrainIndex = createPlanTerrainIndex();
      this.fireColor = {
        natural: new THREE.Color(1, 1, 1),
        target: new THREE.Color(1, 1, 1),
        ratio: new THREE.Color(1, 1, 1),
        hotYellow: new THREE.Color(FIRE_COLOR_HOT_YELLOW),
        hotOrange: new THREE.Color(FIRE_COLOR_HOT_ORANGE),
        charBrown: new THREE.Color(FIRE_COLOR_CHAR_BROWN),
        charBlack: new THREE.Color(FIRE_COLOR_CHAR_BLACK),
      };
      this.rotationAxis = new THREE.Vector3(0, 1, 0);
      this.transform = new THREE.Matrix4();
      this.quaternion = new THREE.Quaternion();
      this.scale = new THREE.Vector3();
      this.position = new THREE.Vector3();
      this.treeAssetStates = new Map(
        TREE_LIBRARY_ASSETS.map((asset) => [asset.key, this.createTreeAssetState(asset)])
      );
      this.treeMeshState = {
        evergreen: this.createTreeState('evergreen'),
        deciduous: this.createTreeState('deciduous'),
      };
      this.shrubMeshState = this.createShrubState();
      this.loadTreeAssets();
    }

    createTreeState(type) {
      const evergreen = type === 'evergreen';
      // Canopy geometry built at unit radius so the per-tree matrix can scale it
      // straight to the crown radius. Evergreens are narrow, tall cones in a cool
      // blue-green; deciduous are rounded crowns in a warmer, lighter green.
      const canopyGeometry = evergreen
        ? new THREE.ConeGeometry(1, 2.6, 9)
        : new THREE.IcosahedronGeometry(1, 1);
      return {
        type,
        canopyGeometry,
        canopyMaterial: new THREE.MeshStandardMaterial({
          color: evergreen ? '#1f4030' : '#6a8f3f',
          roughness: 0.95,
          metalness: 0.02,
          flatShading: !evergreen,
        }),
        canopyMesh: null,
        capacity: 0,
        fireScenePositions: null,
        trunkGeometry: new THREE.CylinderGeometry(0.12, 0.16, 1, 6),
        trunkMaterial: new THREE.MeshStandardMaterial({
          color: evergreen ? '#5b4631' : '#7a5a3b',
          roughness: 1,
          metalness: 0,
        }),
        trunkMesh: null,
      };
    }

    createTreeAssetState(asset) {
      return {
        asset,
        loaded: false,
        loading: false,
        loadPromise: null,
        error: null,
        height: 1,
        diameter: 1,
        capacity: 0,
        visibleCount: 0,
        fireScenePositions: null,
        parts: [],
      };
    }

    createTreeAssetMaterial(asset, kind) {
      const isLeaf = kind === 'leaf';
      return new THREE.MeshStandardMaterial({
        color: isLeaf ? asset.leafColor : asset.barkColor,
        roughness: isLeaf ? 0.94 : 1,
        metalness: 0,
        flatShading: false,
        side: isLeaf ? THREE.DoubleSide : THREE.FrontSide,
      });
    }

    createShrubState() {
      return {
        capacity: 0,
        geometry: new THREE.DodecahedronGeometry(0.7, 0),
        material: new THREE.MeshStandardMaterial({
          color: '#5d7e4c',
          roughness: 0.98,
          metalness: 0.01,
        }),
        mesh: null,
      };
    }

    loadTreeAssets() {
      this.treeAssetStates.forEach((state) => this.loadTreeAsset(state));
    }

    loadTreeAsset(state) {
      if (!state || this.disposed) return Promise.resolve(null);
      if (state.loaded) return Promise.resolve(state);
      if (state.loadPromise) return state.loadPromise;
      state.loading = true;
      state.loadPromise = (async () => {
        try {
          const response = await fetch(state.asset.url);
          if (!response.ok) {
            throw new Error(`${state.asset.url}: ${response.status}`);
          }
          const parsed = createTreeLibraryGeometry(await response.text());
          if (!parsed || !parsed.parts.length) {
            throw new Error(`${state.asset.url}: no usable geometry`);
          }
          state.height = parsed.height;
          state.diameter = parsed.diameter;
          state.parts = parsed.parts.map((part) => ({
            kind: part.kind,
            geometry: part.geometry,
            material: this.createTreeAssetMaterial(state.asset, part.kind),
            mesh: null,
          }));
          state.loaded = true;
          state.error = null;
          this.pendingAssetInvalidation = true;
          this.requestRender();
          return state;
        } catch (err) {
          state.error = err;
          console.warn('tree library asset failed:', state.asset.key, err);
          return null;
        } finally {
          state.loading = false;
          if (!state.loaded) state.loadPromise = null;
        }
      })();
      return state.loadPromise;
    }

    planTreeAssetState(assetKey, type = 'evergreen') {
      return this.treeAssetStates.get(String(assetKey || ''))
        || this.treeAssetStates.get(type === 'deciduous' ? 'maple' : 'pine')
        || null;
    }

    getPlanTreeRenderSpec(assetKey, type = 'evergreen') {
      const state = this.planTreeAssetState(assetKey, type);
      if (!state?.loaded || !state.parts.length) return null;
      return {
        assetKey: state.asset.key,
        diameter: state.diameter,
        height: state.height,
        parts: state.parts.map((part, index) => ({
          geometry: part.geometry,
          key: `${part.kind}-${index}`,
          material: part.material,
        })),
      };
    }

    async ensurePlanTreeRenderSpec(assetKey, type = 'evergreen') {
      const state = this.planTreeAssetState(assetKey, type);
      if (!state) return null;
      await this.loadTreeAsset(state);
      return this.getPlanTreeRenderSpec(state.asset.key, type);
    }

    load({ treeInstances, shrubPoints, grid }) {
      if (this.disposed) {
        return;
      }
      this.clearPlanRemovalPreview();
      this.planCommittedRemovalIds.clear();
      this.planExternalTerrainSlots = [];
      this.data.trees = normalizeTreePayload(treeInstances);
      this.data.shrubs = normalizeShrubPayload(shrubPoints);
      this.grid = grid;
      this.renderNow();
    }

    clear() {
      if (this.renderFrameId) {
        cancelAnimationFrame(this.renderFrameId);
        this.renderFrameId = null;
      }
      this.renderQueued = false;
      this.grid = null;
      this.data = {
        shrubs: EMPTY_SHRUB_DATA,
        trees: EMPTY_TREE_DATA,
      };
      this.renderStats = {
        shrubs: 0,
        trees: 0,
      };
      this.fireTint.active = false;
      this.fireTint.sampleArrival = null;
      this.fireTint.revealTime = null;
      this.fireTint.duration = null;
      this.fireTint.appliedKey = null;
      this.clearPlanRemovalPreview();
      this.planCommittedRemovalIds.clear();
      this.planExternalTerrainSlots = [];
      this.planTerrainIndex = createPlanTerrainIndex();
      this.disposeTreeMeshes();
      this.disposeShrubMesh();
    }

    disposeInstancedMesh(mesh) {
      if (!mesh) {
        return;
      }
      this.group.remove(mesh);
      if (typeof mesh.dispose === 'function') {
        mesh.dispose();
      }
    }

    disposeTreeMeshes() {
      Object.values(this.treeMeshState).forEach((state) => {
        this.disposeInstancedMesh(state.trunkMesh);
        this.disposeInstancedMesh(state.canopyMesh);
        state.trunkMesh = null;
        state.canopyMesh = null;
        state.capacity = 0;
        state.fireScenePositions = null;
      });
      this.treeAssetStates.forEach((state) => {
        state.parts.forEach((part) => {
          this.disposeInstancedMesh(part.mesh);
          part.mesh = null;
        });
        state.capacity = 0;
        state.visibleCount = 0;
        state.fireScenePositions = null;
      });
    }

    disposeShrubMesh() {
      this.disposeInstancedMesh(this.shrubMeshState.mesh);
      this.shrubMeshState.mesh = null;
      this.shrubMeshState.capacity = 0;
    }

    setDensity(kind, value) {
      if (this.disposed) {
        return;
      }
      this.density[kind] = Math.max(0, Math.min(1, value));
      this.requestRender();
    }

    setTypeFilter(filter) {
      if (this.disposed) {
        return;
      }
      this.typeFilter = filter === 'evergreen' || filter === 'deciduous' ? filter : 'all';
      this.requestRender();
    }

    setVisible(visible) {
      if (this.disposed) {
        return;
      }
      this.group.visible = Boolean(visible);
    }

    beginPlanRemovalPreview() {
      this.clearPlanRemovalPreview();
      this.planRemovalPreview = {
        hiddenSlots: new Set(),
        ids: new Set(),
      };
    }

    applyPlanRemovalSegment(start, end, radius) {
      if (this.disposed) return [];
      const a = Array.isArray(start) ? start.map(Number) : [];
      const b = Array.isArray(end) ? end.map(Number) : a;
      if (![a[0], a[1], b[0], b[1]].every(Number.isFinite)) return [];
      if (!this.planRemovalPreview) this.beginPlanRemovalPreview();
      const brushRadius = Math.max(0.1, Number(radius) || 0);
      const bounds = {
        minX: Math.min(a[0], b[0]) - brushRadius,
        maxX: Math.max(a[0], b[0]) + brushRadius,
        minY: Math.min(a[1], b[1]) - brushRadius,
        maxY: Math.max(a[1], b[1]) + brushRadius,
      };
      const touched = new Map();
      const added = [];
      planTerrainSlotsInBounds(this.planTerrainIndex, bounds).forEach((slot) => {
        const entityId = String(slot.entityId || '');
        if (!entityId || this.planCommittedRemovalIds.has(entityId)
            || this.planRemovalPreview.ids.has(entityId)) return;
        if (pointToSegmentDistance(slot.x, slot.y, ...a, ...b) > brushRadius) return;
        this.planRemovalPreview.ids.add(entityId);
        this.planRemovalPreview.hiddenSlots.add(slot);
        hidePlanTerrainSlot(slot, touched);
        added.push(entityId);
      });
      flushInstanceMatrixUpdates(touched);
      return added;
    }

    getPlanRemovalPreviewIds() {
      return this.planRemovalPreview ? [...this.planRemovalPreview.ids] : [];
    }

    commitPlanRemovalPreview() {
      if (!this.planRemovalPreview) return [];
      const ids = [...this.planRemovalPreview.ids];
      ids.forEach((id) => this.planCommittedRemovalIds.add(id));
      this.planRemovalPreview.hiddenSlots.forEach((slot) => {
        slot.originalMatrices = null;
        slot.planRemovalHidden = true;
      });
      this.planRemovalPreview = null;
      return ids;
    }

    // Backward-compatible whole-path entry point. Plan's live brush uses the
    // segment method directly so each frame touches only the newest capsule.
    setPlanRemovalPreview(points, radius) {
      const path = (Array.isArray(points) ? points : [])
        .filter((point) => Array.isArray(point) && Number.isFinite(Number(point[0])) && Number.isFinite(Number(point[1])))
        .map((point) => [Number(point[0]), Number(point[1])]);
      if (!path.length) {
        this.clearPlanRemovalPreview();
        return;
      }
      this.beginPlanRemovalPreview();
      this.applyPlanRemovalSegment(path[0], path[0], radius);
      for (let index = 1; index < path.length; index += 1) {
        this.applyPlanRemovalSegment(path[index - 1], path[index], radius);
      }
    }

    clearPlanRemovalPreview({ restore = true } = {}) {
      if (!this.planRemovalPreview) return;
      if (restore) {
        const touched = new Map();
        this.planRemovalPreview.hiddenSlots.forEach((slot) => restorePlanTerrainSlot(slot, touched));
        flushInstanceMatrixUpdates(touched);
      }
      this.planRemovalPreview = null;
    }

    registerPlanTerrainSlot(x, y, groundHeight, instanceIndex, meshes, entityId = '', kind = '') {
      const slot = {
        x,
        y,
        groundHeight,
        entityId: String(entityId || ''),
        kind,
        instanceIndex,
        meshes: (meshes || []).filter(Boolean),
        instances: (meshes || []).filter(Boolean).map((mesh) => ({ mesh, instanceIndex })),
        originalMatrices: null,
        planRemovalHidden: false,
      };
      addPlanTerrainSlot(this.planTerrainIndex, slot);
      this.applyPlanRemovalStateToSlot(slot);
      return slot;
    }

    setPlanTerrainSlotInstances(slot, instanceIndex, meshes) {
      if (!slot) return slot;
      slot.instanceIndex = instanceIndex;
      slot.meshes = (meshes || []).filter(Boolean);
      slot.instances = slot.meshes.map((mesh) => ({ mesh, instanceIndex }));
      slot.originalMatrices = null;
      slot.planRemovalHidden = false;
      this.applyPlanRemovalStateToSlot(slot);
      return slot;
    }

    applyPlanRemovalStateToSlot(slot) {
      const entityId = String(slot?.entityId || '');
      if (!entityId) return;
      if (this.planRemovalPreview?.ids.has(entityId)) {
        this.planRemovalPreview.hiddenSlots.add(slot);
        hidePlanTerrainSlot(slot);
        return;
      }
      if (this.planCommittedRemovalIds.has(entityId)) {
        hidePlanTerrainSlot(slot);
        slot.originalMatrices = null;
      }
    }

    registerOptimisticPlanVegetation(slots) {
      const registered = (slots || []).map((source) => {
        const slot = {
          x: Number(source.x),
          y: Number(source.y),
          groundHeight: Number(source.groundHeight),
          entityId: String(source.entityId || ''),
          kind: source.kind || '',
          instances: (source.instances || []).filter((entry) => entry?.mesh && Number.isInteger(entry.instanceIndex)),
          originalMatrices: null,
          planRemovalHidden: false,
        };
        slot.meshes = slot.instances.map((entry) => entry.mesh);
        slot.instanceIndex = slot.instances[0]?.instanceIndex ?? -1;
        this.planExternalTerrainSlots.push(slot);
        addPlanTerrainSlot(this.planTerrainIndex, slot);
        this.applyPlanRemovalStateToSlot(slot);
        return slot;
      });
      return registered;
    }

    replaceOptimisticPlanVegetationMesh(previousMesh, nextMesh) {
      if (!previousMesh || !nextMesh || previousMesh === nextMesh) return;
      this.planExternalTerrainSlots.forEach((slot) => {
        slot.instances.forEach((entry) => {
          if (entry.mesh === previousMesh) entry.mesh = nextMesh;
        });
        slot.meshes = slot.instances.map((entry) => entry.mesh);
      });
    }

    clearOptimisticPlanVegetation() {
      this.clearPlanRemovalPreview();
      this.planCommittedRemovalIds.clear();
      this.planExternalTerrainSlots = [];
    }

    syncPlanTerrainHeights(bounds) {
      if (this.disposed || !this.grid || !bounds) return 0;
      const touchedMeshes = new Map();
      let changed = 0;
      planTerrainSlotsInBounds(this.planTerrainIndex, bounds).forEach((slot) => {
        if (!VEILTerrain.hasValidTerrainAtLocal(this.grid, slot.x, slot.y)) return;
        const groundHeight = VEILTerrain.sampleTerrainHeightAtLocal(this.grid, slot.x, slot.y);
        if (shiftPlanTerrainSlot(slot, groundHeight, touchedMeshes)) changed += 1;
      });
      flushInstanceMatrixUpdates(touchedMeshes);
      return changed;
    }

    applyMeshShadowFlags(mesh) {
      if (!mesh) {
        return;
      }
      mesh.castShadow = this.shadowsEnabled;
      mesh.receiveShadow = this.shadowsEnabled;
    }

    setShadows(on) {
      if (this.disposed) {
        return;
      }
      this.shadowsEnabled = Boolean(on);
      Object.values(this.treeMeshState).forEach((state) => {
        this.applyMeshShadowFlags(state.trunkMesh);
        this.applyMeshShadowFlags(state.canopyMesh);
      });
      this.treeAssetStates.forEach((state) => {
        state.parts.forEach((part) => this.applyMeshShadowFlags(part.mesh));
      });
      this.applyMeshShadowFlags(this.shrubMeshState.mesh);
    }

    setAvoidance(avoidance, options = {}) {
      this.avoidance = {
        buildingLines: [],
        buildingPolygons: [],
        hydrologyLines: [],
        roadLines: [],
        clipBounds: null,
      };
    }

    renderNow() {
      if (this.disposed) {
        return;
      }
      if (this.renderFrameId) {
        cancelAnimationFrame(this.renderFrameId);
        this.renderFrameId = null;
      }
      this.renderQueued = false;
      this.render();
      this.flushAssetLoadInvalidation();
    }

    requestRender() {
      if (this.disposed) {
        return;
      }
      if (this.renderQueued) {
        return;
      }
      this.renderQueued = true;
      this.renderFrameId = requestAnimationFrame(() => {
        this.renderFrameId = null;
        this.renderQueued = false;
        this.render();
        this.flushAssetLoadInvalidation();
      });
    }

    flushAssetLoadInvalidation() {
      if (!this.pendingAssetInvalidation || this.disposed) {
        return;
      }
      this.pendingAssetInvalidation = false;
      this.onAssetLoad();
    }

    // ---- fire-reveal tinting -------------------------------------------
    // Driven by app.js: as the wildfire arrival-time reveal is scrubbed/played,
    // each rendered stem samples its fire-arrival minute and shifts color from
    // the natural canopy through the flame front to char.
    fireTargetColor(ageMinutes, durationMinutes) {
      const duration = Number(durationMinutes) || 120;
      const frontWindow = Math.max(3, Math.min(9, duration * 0.04));
      if (ageMinutes <= frontWindow) {
        const hot = clamp01(ageMinutes / frontWindow);
        return this.fireColor.target.copy(this.fireColor.hotYellow).lerp(this.fireColor.hotOrange, hot);
      }
      const cool = clamp01((ageMinutes - frontWindow) / Math.max(18, duration * 0.35));
      if (cool < 0.2) {
        return this.fireColor.target.copy(this.fireColor.hotOrange).lerp(this.fireColor.charBrown, cool / 0.2);
      }
      return this.fireColor.target.copy(this.fireColor.charBrown).lerp(this.fireColor.charBlack, (cool - 0.2) / 0.8);
    }

    // InstancedMesh.setColorAt multiplies the material color; convert an
    // absolute target color into the per-instance multiplier that yields it.
    fireMultiplierForMaterial(targetColor, material) {
      if (!targetColor) {
        return this.fireColor.natural;
      }
      const base = material?.color || FIRE_COLOR_WHITE;
      const floor = 0.025;
      this.fireColor.ratio.setRGB(
        Math.min(16, targetColor.r / Math.max(floor, base.r || 0)),
        Math.min(16, targetColor.g / Math.max(floor, base.g || 0)),
        Math.min(16, targetColor.b / Math.max(floor, base.b || 0))
      );
      return this.fireColor.ratio;
    }

    sampleFireTarget(sampleArrival, sceneX, sceneY, revealTime, duration) {
      let arrival = null;
      try {
        arrival = sampleArrival(sceneX, sceneY);
      } catch (_err) {
        arrival = null;
      }
      if (arrival === null || arrival === undefined) {
        return null;
      }
      const a = Number(arrival);
      if (!Number.isFinite(a) || a > revealTime) {
        return null; // fire has not reached this stem yet at the reveal time
      }
      return this.fireTargetColor(Math.max(0, revealTime - a), duration);
    }

    tintMeshInstances(mesh, positions, count, material, sampleArrival, revealTime, duration) {
      if (!mesh || !positions || !count) {
        return;
      }
      for (let index = 0; index < count; index += 1) {
        const offset = index * 2;
        const target = this.sampleFireTarget(
          sampleArrival,
          positions[offset],
          positions[offset + 1],
          revealTime,
          duration
        );
        mesh.setColorAt(index, this.fireMultiplierForMaterial(target, material));
      }
      if (mesh.instanceColor) {
        mesh.instanceColor.needsUpdate = true;
      }
    }

    clearMeshFireTint(mesh, count) {
      if (!mesh?.instanceColor) {
        return;
      }
      const total = mesh.instanceColor.count || count || 0;
      for (let index = 0; index < total; index += 1) {
        mesh.setColorAt(index, this.fireColor.natural);
      }
      mesh.instanceColor.needsUpdate = true;
    }

    applyFireTintToRenderedTrees(force = false) {
      if (!this.fireTint.active || typeof this.fireTint.sampleArrival !== 'function') {
        return;
      }
      const sampleArrival = this.fireTint.sampleArrival;
      const revealTime = this.fireTint.revealTime;
      const duration = this.fireTint.duration;
      const key = `${sampleArrival.__twinFireSampleKey || 'grid'}:${revealTime}:${duration ?? ''}`;
      if (!force && this.fireTint.appliedKey === key) {
        return;
      }

      Object.values(this.treeMeshState).forEach((state) => {
        this.tintMeshInstances(
          state.canopyMesh,
          state.fireScenePositions,
          state.canopyMesh?.count || 0,
          state.canopyMaterial,
          sampleArrival,
          revealTime,
          duration
        );
      });

      this.treeAssetStates.forEach((state) => {
        state.parts.forEach((part) => {
          if (part.kind !== 'leaf') {
            return; // only the canopy/leaf parts char; trunks stay as-is
          }
          this.tintMeshInstances(
            part.mesh,
            state.fireScenePositions,
            part.mesh?.count || 0,
            part.material,
            sampleArrival,
            revealTime,
            duration
          );
        });
      });

      this.fireTint.appliedKey = key;
    }

    applyFireTint(sampleArrival, revealTime, duration) {
      const t = Number(revealTime);
      if (this.disposed || typeof sampleArrival !== 'function' || !Number.isFinite(t)) {
        this.clearFireTint();
        return;
      }
      this.fireTint.active = true;
      this.fireTint.sampleArrival = sampleArrival;
      this.fireTint.revealTime = t;
      this.fireTint.duration = Number.isFinite(Number(duration)) ? Number(duration) : null;
      this.applyFireTintToRenderedTrees(false);
    }

    clearFireTint() {
      const wasActive = this.fireTint.active || this.fireTint.appliedKey !== null;
      this.fireTint.active = false;
      this.fireTint.sampleArrival = null;
      this.fireTint.revealTime = null;
      this.fireTint.duration = null;
      this.fireTint.appliedKey = null;
      if (!wasActive) {
        return;
      }

      Object.values(this.treeMeshState).forEach((state) => {
        this.clearMeshFireTint(state.canopyMesh, state.canopyMesh?.count || 0);
      });
      this.treeAssetStates.forEach((state) => {
        state.parts.forEach((part) => {
          if (part.kind === 'leaf') {
            this.clearMeshFireTint(part.mesh, part.mesh?.count || 0);
          }
        });
      });
    }

    ensureTreeCapacity(category, minCapacity) {
      const state = this.treeMeshState[category];
      if (!state || state.capacity >= minCapacity) {
        return;
      }

      this.disposeInstancedMesh(state.trunkMesh);
      this.disposeInstancedMesh(state.canopyMesh);

      const capacity = Math.max(1, minCapacity);
      state.trunkMesh = new THREE.InstancedMesh(state.trunkGeometry, state.trunkMaterial, capacity);
      state.canopyMesh = new THREE.InstancedMesh(state.canopyGeometry, state.canopyMaterial, capacity);
      state.trunkMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      state.canopyMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      this.applyMeshShadowFlags(state.trunkMesh);
      this.applyMeshShadowFlags(state.canopyMesh);
      state.trunkMesh.count = 0;
      state.canopyMesh.count = 0;
      this.group.add(state.trunkMesh, state.canopyMesh);
      state.capacity = capacity;
      state.fireScenePositions = ensureFloat32Capacity(state.fireScenePositions, capacity, 2);
    }

    ensureTreeAssetCapacity(assetKey, minCapacity) {
      const state = this.treeAssetStates.get(assetKey);
      if (!state) {
        return false;
      }
      this.loadTreeAsset(state);
      if (!state.loaded || !state.parts.length) {
        return false;
      }
      if (state.capacity >= minCapacity) {
        return true;
      }

      state.parts.forEach((part) => {
        this.disposeInstancedMesh(part.mesh);
        part.mesh = null;
      });

      const capacity = Math.max(1, minCapacity);
      state.parts.forEach((part) => {
        part.mesh = new THREE.InstancedMesh(part.geometry, part.material, capacity);
        part.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        this.applyMeshShadowFlags(part.mesh);
        part.mesh.count = 0;
        this.group.add(part.mesh);
      });
      state.capacity = capacity;
      state.fireScenePositions = ensureFloat32Capacity(state.fireScenePositions, capacity, 2);
      return true;
    }

    ensureShrubCapacity(minCapacity) {
      if (this.shrubMeshState.capacity >= minCapacity) {
        return;
      }

      this.disposeInstancedMesh(this.shrubMeshState.mesh);

      const capacity = Math.max(1, minCapacity);
      this.shrubMeshState.mesh = new THREE.InstancedMesh(
        this.shrubMeshState.geometry,
        this.shrubMeshState.material,
        capacity
      );
      this.shrubMeshState.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      this.applyMeshShadowFlags(this.shrubMeshState.mesh);
      this.shrubMeshState.mesh.count = 0;
      this.group.add(this.shrubMeshState.mesh);
      this.shrubMeshState.capacity = capacity;
    }

    resetRenderedCounts() {
      this.renderStats = {
        shrubs: 0,
        trees: 0,
      };

      Object.values(this.treeMeshState).forEach((state) => {
        if (state.trunkMesh) {
          state.trunkMesh.count = 0;
        }
        if (state.canopyMesh) {
          state.canopyMesh.count = 0;
        }
      });

      this.treeAssetStates.forEach((state) => {
        state.visibleCount = 0;
        state.parts.forEach((part) => {
          if (part.mesh) {
            part.mesh.count = 0;
          }
        });
      });

      if (this.shrubMeshState.mesh) {
        this.shrubMeshState.mesh.count = 0;
      }
    }

    pushAwayFromLines(x, y, lines, minDistance) {
      let adjustedX = x;
      let adjustedY = y;

      for (const line of lines) {
        for (let index = 0; index < line.length - 1; index += 1) {
          const ax = line[index][0];
          const ay = line[index][1];
          const bx = line[index + 1][0];
          const by = line[index + 1][1];
          const abx = bx - ax;
          const aby = by - ay;
          const abLengthSq = abx * abx + aby * aby;
          const t =
            abLengthSq > 0
              ? Math.max(0, Math.min(1, ((adjustedX - ax) * abx + (adjustedY - ay) * aby) / abLengthSq))
              : 0;
          const nearestX = ax + abx * t;
          const nearestY = ay + aby * t;
          const dx = adjustedX - nearestX;
          const dy = adjustedY - nearestY;
          const distance = Math.hypot(dx, dy);

          if (distance < minDistance) {
            const safeDistance = distance > 1e-6 ? distance : 1e-6;
            const push = minDistance - safeDistance;
            adjustedX += (dx / safeDistance) * push;
            adjustedY += (dy / safeDistance) * push;
          }
        }
      }

      return { x: adjustedX, y: adjustedY };
    }

    pushOutOfBuildingPolygons(x, y, minDistance) {
      let adjustedX = x;
      let adjustedY = y;

      for (const polygon of this.avoidance.buildingPolygons) {
        if (!pointInPolygon([adjustedX, adjustedY], polygon)) {
          continue;
        }

        const ring = polygon[0] || [];
        if (ring.length < 2) {
          continue;
        }

        let bestNearestX = adjustedX;
        let bestNearestY = adjustedY;
        let bestDistanceSq = Number.POSITIVE_INFINITY;

        for (let index = 0; index < ring.length - 1; index += 1) {
          const ax = ring[index][0];
          const ay = ring[index][1];
          const bx = ring[index + 1][0];
          const by = ring[index + 1][1];
          const abx = bx - ax;
          const aby = by - ay;
          const abLengthSq = abx * abx + aby * aby;
          const t =
            abLengthSq > 0
              ? Math.max(0, Math.min(1, ((adjustedX - ax) * abx + (adjustedY - ay) * aby) / abLengthSq))
              : 0;
          const nearestX = ax + abx * t;
          const nearestY = ay + aby * t;
          const dx = adjustedX - nearestX;
          const dy = adjustedY - nearestY;
          const distanceSq = dx * dx + dy * dy;
          if (distanceSq < bestDistanceSq) {
            bestDistanceSq = distanceSq;
            bestNearestX = nearestX;
            bestNearestY = nearestY;
          }
        }

        let dirX = adjustedX - bestNearestX;
        let dirY = adjustedY - bestNearestY;
        let dirLength = Math.hypot(dirX, dirY);

        if (dirLength < 1e-6) {
          const centroid = polygonCentroid(polygon);
          dirX = bestNearestX - centroid.x;
          dirY = bestNearestY - centroid.y;
          dirLength = Math.hypot(dirX, dirY);
        }

        if (dirLength < 1e-6) {
          dirX = 1;
          dirY = 0;
          dirLength = 1;
        }

        adjustedX = bestNearestX + (dirX / dirLength) * minDistance;
        adjustedY = bestNearestY + (dirY / dirLength) * minDistance;
        const clamped = clampToBounds(adjustedX, adjustedY, this.avoidance.clipBounds, 0.8);
        adjustedX = clamped.x;
        adjustedY = clamped.y;

        if (pointInPolygon([adjustedX, adjustedY], polygon)) {
          adjustedX = bestNearestX + (dirX / dirLength) * (minDistance + 0.8);
          adjustedY = bestNearestY + (dirY / dirLength) * (minDistance + 0.8);
          const clampedRetry = clampToBounds(adjustedX, adjustedY, this.avoidance.clipBounds, 0.8);
          adjustedX = clampedRetry.x;
          adjustedY = clampedRetry.y;
        }
      }

      return { x: adjustedX, y: adjustedY };
    }

    overlapsBuildingPolygon(x, y, radius = 0) {
      const effectiveRadius = radius + BUILDING_EXCLUSION_BUFFER_METERS;
      for (const polygon of this.avoidance.buildingPolygons) {
        if (pointInPolygon([x, y], polygon)) {
          return true;
        }
        if (!(radius > 0)) {
          continue;
        }
        const ring = polygon[0] || [];
        for (let index = 0; index < ring.length - 1; index += 1) {
          if (
            pointToSegmentDistance(
              x,
              y,
              ring[index][0],
              ring[index][1],
              ring[index + 1][0],
              ring[index + 1][1]
            ) < effectiveRadius
          ) {
            return true;
          }
        }
      }
      return false;
    }

    applyAvoidance(x, y, hydrologyDistance, roadDistance) {
      const buildingPolygonAdjusted = this.pushOutOfBuildingPolygons(
        x,
        y,
        Math.max(1.4, roadDistance * 0.55)
      );
      const buildingAdjusted = this.pushAwayFromLines(
        buildingPolygonAdjusted.x,
        buildingPolygonAdjusted.y,
        this.avoidance.buildingLines,
        Math.max(roadDistance * 0.95, hydrologyDistance + 1.2)
      );
      const waterAdjusted = this.pushAwayFromLines(
        buildingAdjusted.x,
        buildingAdjusted.y,
        this.avoidance.hydrologyLines,
        hydrologyDistance
      );
      return this.pushAwayFromLines(
        waterAdjusted.x,
        waterAdjusted.y,
        this.avoidance.roadLines,
        roadDistance
      );
    }

    render() {
      if (this.disposed) {
        return;
      }
      this.resetRenderedCounts();
      if (this.planRemovalPreview) this.planRemovalPreview.hiddenSlots = new Set();
      this.planTerrainIndex = createPlanTerrainIndex();
      if (!this.grid) {
        return;
      }

      this.renderTrees();
      this.renderShrubs();
      this.planExternalTerrainSlots.forEach((slot) => {
        addPlanTerrainSlot(this.planTerrainIndex, slot);
        this.applyPlanRemovalStateToSlot(slot);
      });
      this.applyFireTintToRenderedTrees(true);
    }

    renderTrees() {
      const treeData = this.data.trees;
      if (!treeData.count) {
        return;
      }

      this.ensureTreeCapacity('evergreen', treeData.categoryCounts.evergreen);
      this.ensureTreeCapacity('deciduous', treeData.categoryCounts.deciduous);
      Object.entries(treeData.assetCounts || {}).forEach(([assetKey, count]) => {
        this.ensureTreeAssetCapacity(assetKey, count);
      });

      const visibleCounts = {
        evergreen: 0,
        deciduous: 0,
      };
      const values = treeData.values;

      for (let index = 0; index < treeData.count; index += 1) {
        const offset = index * TREE_STRIDE;
        const x = values[offset];
        const y = values[offset + 1];
        const totalHeight = Math.max(2.5, Math.min(30, values[offset + 2] || 6));
        const radius = Math.max(1.2, values[offset + 3] || 1.5);
        const evergreen = values[offset + 4] > 0.5;
        const assetKey = treeAssetKeyFromId(values[offset + 5]);
        const entityId = String(treeData.entityIds?.[index] || '');
        if (entityId && this.planCommittedRemovalIds.has(entityId)) continue;
        const planSlot = this.registerPlanTerrainSlot(
          x, y, Number.NaN, -1, [], entityId, 'tree');
        if (this.typeFilter === 'evergreen' && !evergreen) continue;
        if (this.typeFilter === 'deciduous' && evergreen) continue;
        if (!VEILTerrain.hasValidTerrainAtLocal(this.grid, x, y)) {
          continue; // off the parcel terrain -> don't float it
        }
        if (!passesDensity('tree', this.density.trees, index, x, y, totalHeight)) {
          continue;
        }

        const key = evergreen ? 'evergreen' : 'deciduous';
        const assetState = this.treeAssetStates.get(assetKey);
        const useLibraryAsset = Boolean(
          assetState?.loaded && assetState.parts.length && assetState.capacity > assetState.visibleCount
        );
        const baseHeight = VEILTerrain.sampleTerrainHeightAtLocal(this.grid, x, y);
        planSlot.groundHeight = baseHeight;
        const rotation = hashUnit(`tree:${Math.round(x * 10)}:${Math.round(y * 10)}`) * Math.PI * 2;
        this.quaternion.setFromAxisAngle(this.rotationAxis, rotation);

        if (useLibraryAsset) {
          const visibleIndex = assetState.visibleCount;
          const positionOffset = visibleIndex * 2;
          const crownDiameter = Math.max(1.8, radius * 2);
          const modelDiameter = Math.max(1, assetState.diameter);
          const modelHeight = Math.max(1, assetState.height);
          const widthJitter = 0.9 + hashUnit(`tree-width:${index}:${assetKey}`) * 0.2;
          const depthJitter = 0.9 + hashUnit(`tree-depth:${index}:${assetKey}`) * 0.2;
          this.position.set(x, baseHeight, -y);
          this.scale.set(
            (crownDiameter / modelDiameter) * widthJitter,
            totalHeight / modelHeight,
            (crownDiameter / modelDiameter) * depthJitter
          );
          this.transform.compose(this.position, this.quaternion, this.scale);
          assetState.parts.forEach((part) => {
            part.mesh?.setMatrixAt(visibleIndex, this.transform);
          });
          this.setPlanTerrainSlotInstances(
            planSlot, visibleIndex, assetState.parts.map((part) => part.mesh));
          if (assetState.fireScenePositions) {
            assetState.fireScenePositions[positionOffset] = x;
            assetState.fireScenePositions[positionOffset + 1] = y;
          }
          assetState.visibleCount += 1;
          continue;
        }

        const state = this.treeMeshState[key];
        if (!state?.trunkMesh || !state?.canopyMesh) {
          continue;
        }

        const trunkHeight = Math.max(1.2, totalHeight * (evergreen ? 0.32 : 0.46));
        const canopyHeight = Math.max(1.6, totalHeight - trunkHeight * 0.6);
        // Crown half-width: evergreens are narrower than their height, deciduous
        // broader. Driven by the LiDAR/estimated crown radius so canopy fills.
        const crownR = Math.max(1.2, radius * (evergreen ? 0.85 : 1.15));

        const visibleIndex = visibleCounts[key];
        const positionOffset = visibleIndex * 2;
        this.position.set(x, baseHeight + trunkHeight / 2, -y);
        const trunkR = Math.max(0.12, crownR * 0.12);
        this.scale.set(trunkR, trunkHeight, trunkR);
        this.transform.compose(this.position, this.quaternion, this.scale);
        state.trunkMesh.setMatrixAt(visibleIndex, this.transform);

        this.position.set(x, baseHeight + trunkHeight + canopyHeight * (evergreen ? 0.34 : 0.42), -y);
        this.scale.set(crownR, canopyHeight, crownR);
        this.transform.compose(this.position, this.quaternion, this.scale);
        state.canopyMesh.setMatrixAt(visibleIndex, this.transform);
        this.setPlanTerrainSlotInstances(
          planSlot, visibleIndex, [state.trunkMesh, state.canopyMesh]);
        if (state.fireScenePositions) {
          state.fireScenePositions[positionOffset] = x;
          state.fireScenePositions[positionOffset + 1] = y;
        }

        visibleCounts[key] += 1;
      }

      Object.entries(visibleCounts).forEach(([category, visibleCount]) => {
        const state = this.treeMeshState[category];
        if (!state?.trunkMesh || !state?.canopyMesh) {
          return;
        }
        state.trunkMesh.count = visibleCount;
        state.canopyMesh.count = visibleCount;
        markInstanceMatrixFull(state.trunkMesh);
        markInstanceMatrixFull(state.canopyMesh);
        this.renderStats.trees += visibleCount;
      });

      this.treeAssetStates.forEach((state) => {
        if (!state.parts.length) {
          return;
        }
        state.parts.forEach((part) => {
          if (!part.mesh) {
            return;
          }
          part.mesh.count = state.visibleCount;
          markInstanceMatrixFull(part.mesh);
        });
        this.renderStats.trees += state.visibleCount;
      });
    }

    renderShrubs() {
      const shrubData = this.data.shrubs;
      if (!shrubData.count) {
        return;
      }

      this.ensureShrubCapacity(shrubData.count);
      const shrubMesh = this.shrubMeshState.mesh;
      if (!shrubMesh) {
        return;
      }

      const values = shrubData.values;
      let visibleCount = 0;

      for (let index = 0; index < shrubData.count; index += 1) {
        const offset = index * SHRUB_STRIDE;
        const x = values[offset];
        const y = values[offset + 1];
        const baseScale = Math.max(0.55, Math.min(3.2, values[offset + 2] || 1));
        const entityId = String(shrubData.entityIds?.[index] || '');
        if (entityId && this.planCommittedRemovalIds.has(entityId)) continue;
        const planSlot = this.registerPlanTerrainSlot(
          x, y, Number.NaN, -1, [], entityId, 'shrub');
        if (!VEILTerrain.hasValidTerrainAtLocal(this.grid, x, y)) {
          continue; // off the parcel terrain -> don't float it
        }
        if (!passesDensity('shrub', this.density.shrubs, index, x, y, baseScale)) {
          continue;
        }

        const baseHeight = VEILTerrain.sampleTerrainHeightAtLocal(this.grid, x, y);
        planSlot.groundHeight = baseHeight;
        const rotation = hashUnit(`shrub:${Math.round(x * 10)}:${Math.round(y * 10)}`) * Math.PI * 2;
        this.quaternion.setFromAxisAngle(this.rotationAxis, rotation);
        this.position.set(x, baseHeight + baseScale * 0.42, -y);
        this.scale.set(baseScale, baseScale * 0.9, baseScale);
        this.transform.compose(this.position, this.quaternion, this.scale);
        shrubMesh.setMatrixAt(visibleCount, this.transform);
        this.setPlanTerrainSlotInstances(planSlot, visibleCount, [shrubMesh]);
        visibleCount += 1;
      }

      shrubMesh.count = visibleCount;
      markInstanceMatrixFull(shrubMesh);
      this.renderStats.shrubs = visibleCount;
    }

    getRenderStats() {
      return { ...this.renderStats };
    }

    dispose() {
      if (this.disposed) {
        return;
      }

      this.clear();
      Object.values(this.treeMeshState).forEach((state) => {
        state.canopyGeometry.dispose();
        state.canopyMaterial.dispose();
        state.trunkGeometry.dispose();
        state.trunkMaterial.dispose();
      });
      this.treeAssetStates.forEach((state) => {
        state.parts.forEach((part) => {
          part.geometry.dispose();
          part.material.dispose();
        });
        state.parts = [];
      });
      this.shrubMeshState.geometry.dispose();
      this.shrubMeshState.material.dispose();
      this.scene.remove(this.group);
      this.disposed = true;
    }
  }

  global.VEILVegetation = {
    create(scene, options) {
      return new VegetationRenderer(scene, options);
    },
    _test: {
      addPlanTerrainSlot,
      createPlanTerrainIndex,
      flushInstanceMatrixUpdates,
      hidePlanTerrainSlot,
      markInstanceMatrixIndices,
      normalizeShrubPayload,
      normalizeTreePayload,
      planTerrainSlotsInBounds,
      restorePlanTerrainSlot,
      shiftPlanTerrainSlot,
    },
  };
})(window);
