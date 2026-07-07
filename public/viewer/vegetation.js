(function attachVegetationHelpers(global) {
  const { THREE, VEILTerrain } = global;

  const BUILDING_EXCLUSION_BUFFER_METERS = 1.75;
  const TREE_STRIDE = 6; // x, y, height, radius, typeFlag (1=evergreen, 0=deciduous), assetId
  const SHRUB_STRIDE = 3;
  // Fire-reveal tinting: a passing fire front runs bright yellow -> orange, then
  // cools through char brown to near-black as the burn ages behind the front.
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
    values: new Float32Array(0),
  });
  const EMPTY_SHRUB_DATA = Object.freeze({
    count: 0,
    values: new Float32Array(0),
  });

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
      const maxCount = Math.floor(source.length / sourceStride);
      const values = new Float32Array(maxCount * TREE_STRIDE);
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
        categoryCounts[evergreen ? 'evergreen' : 'deciduous'] += 1;
        assetCounts[assetKey] = (assetCounts[assetKey] || 0) + 1;
        count += 1;
      }

      return {
        assetCounts,
        categoryCounts,
        count,
        values: trimFloat32Array(values, count, TREE_STRIDE),
      };
    }

    const rows = Array.isArray(payload) ? payload : [];
    if (!rows.length) {
      return EMPTY_TREE_DATA;
    }

    const values = new Float32Array(rows.length * TREE_STRIDE);
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
      categoryCounts[key] += 1;
      assetCounts[assetKey] = (assetCounts[assetKey] || 0) + 1;
      count += 1;
    });

    return {
      assetCounts,
      categoryCounts,
      count,
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
      const maxCount = Math.floor(source.length / sourceStride);
      const values = new Float32Array(maxCount * SHRUB_STRIDE);
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
        count += 1;
      }

      return {
        count,
        values: trimFloat32Array(values, count, SHRUB_STRIDE),
      };
    }

    const rows = Array.isArray(payload) ? payload : [];
    if (!rows.length) {
      return EMPTY_SHRUB_DATA;
    }

    const values = new Float32Array(rows.length * SHRUB_STRIDE);
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
      count += 1;
    });

    return {
      count,
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
    constructor(scene) {
      this.scene = scene;
      this.group = new THREE.Group();
      this.scene.add(this.group);
      this.disposed = false;
      this.renderFrameId = null;
      this.renderQueued = false;
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
      // Fire-reveal state driven by app.js applyFireTint(); appliedKey lets a
      // re-render skip retinting when the reveal time/grid have not changed.
      this.fireTint = {
        active: false,
        sampleArrival: null,
        revealTime: null,
        duration: null,
        appliedKey: null,
      };
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

    async loadTreeAsset(state) {
      if (!state || state.loaded || state.loading || this.disposed) {
        return;
      }
      state.loading = true;
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
        this.requestRender();
      } catch (err) {
        state.error = err;
        console.warn('tree library asset failed:', state.asset.key, err);
      } finally {
        state.loading = false;
      }
    }

    load({ treeInstances, shrubPoints, grid }) {
      if (this.disposed) {
        return;
      }
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
      });
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
      state.trunkMesh.castShadow = false;
      state.canopyMesh.castShadow = false;
      state.trunkMesh.receiveShadow = false;
      state.canopyMesh.receiveShadow = false;
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
        part.mesh.castShadow = false;
        part.mesh.receiveShadow = false;
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
      this.shrubMeshState.mesh.castShadow = false;
      this.shrubMeshState.mesh.receiveShadow = false;
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
      if (!this.grid) {
        return;
      }

      this.renderTrees();
      this.renderShrubs();
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
        state.trunkMesh.instanceMatrix.needsUpdate = true;
        state.canopyMesh.instanceMatrix.needsUpdate = true;
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
          part.mesh.instanceMatrix.needsUpdate = true;
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
        if (!VEILTerrain.hasValidTerrainAtLocal(this.grid, x, y)) {
          continue; // off the parcel terrain -> don't float it
        }
        if (!passesDensity('shrub', this.density.shrubs, index, x, y, baseScale)) {
          continue;
        }

        const baseHeight = VEILTerrain.sampleTerrainHeightAtLocal(this.grid, x, y);
        const rotation = hashUnit(`shrub:${Math.round(x * 10)}:${Math.round(y * 10)}`) * Math.PI * 2;
        this.quaternion.setFromAxisAngle(this.rotationAxis, rotation);
        this.position.set(x, baseHeight + baseScale * 0.42, -y);
        this.scale.set(baseScale, baseScale * 0.9, baseScale);
        this.transform.compose(this.position, this.quaternion, this.scale);
        shrubMesh.setMatrixAt(visibleCount, this.transform);
        visibleCount += 1;
      }

      shrubMesh.count = visibleCount;
      shrubMesh.instanceMatrix.needsUpdate = true;
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
    create(scene) {
      return new VegetationRenderer(scene);
    },
  };
})(window);
