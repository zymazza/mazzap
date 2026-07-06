(function attachVaporRenderer(global) {
  'use strict';

  const { THREE, VEILTerrain } = global;

  const DEFAULT_MAX_PARTICLES = 1000;
  const DEFAULT_MM_PER_DAY = 3.2;
  const FULL_SCALE_MM_PER_DAY = 11;
  const MIN_AET_WEIGHT = 0.015;

  const VERTEX_SHADER = `
    uniform float uTime;
    uniform float uIntensity;
    uniform float uPixelRatio;

    attribute float aWeight;
    attribute float aPhase;
    attribute float aSeed;
    attribute float aSpeed;
    attribute float aRise;
    attribute float aSize;
    attribute float aGate;

    varying float vAlpha;
    varying float vMist;

    void main() {
      float strength = clamp(uIntensity, 0.0, 1.35);
      float cycle = fract(aPhase + uTime * aSpeed * mix(0.68, 1.45, strength));
      float liftEase = cycle * cycle * (3.0 - 2.0 * cycle);
      float fadeIn = smoothstep(0.0, 0.16, cycle);
      float fadeOut = 1.0 - smoothstep(0.58, 1.0, cycle);
      float emission = smoothstep(aGate - 0.09, aGate + 0.09, clamp(strength * (0.30 + aWeight * 0.72), 0.0, 1.0));

      float swirlA = sin(aSeed * 17.31 + uTime * (0.34 + aWeight * 0.18) + cycle * 6.28318);
      float swirlB = cos(aSeed * 11.73 + uTime * (0.27 + aWeight * 0.22) + cycle * 7.9);
      float drift = (0.35 + aWeight * 1.65) * liftEase;
      vec3 animated = position + vec3(swirlA * drift, aRise * liftEase * mix(0.72, 1.22, strength), swirlB * drift);

      vec4 mvPosition = modelViewMatrix * vec4(animated, 1.0);
      float distanceScale = clamp(220.0 / max(32.0, -mvPosition.z), 0.36, 2.35);
      float sizePulse = 0.86 + 0.14 * sin(aSeed * 9.1 + cycle * 6.28318);
      gl_PointSize = aSize * uPixelRatio * distanceScale * sizePulse * mix(0.86, 1.06, strength);
      gl_Position = projectionMatrix * mvPosition;

      // Low per-particle alpha: additive blending stacks through the field at
      // grazing angles, so keep each puff faint to stay translucent mist rather
      // than a blown-out white wall.
      vAlpha = fadeIn * fadeOut * emission * (0.024 + aWeight * 0.11) * clamp(strength * 0.85 + 0.25, 0.0, 1.0);
      vMist = clamp(0.25 + aWeight * 0.62 + cycle * 0.18, 0.0, 1.0);
    }
  `;

  const FRAGMENT_SHADER = `
    precision mediump float;

    varying float vAlpha;
    varying float vMist;

    void main() {
      vec2 p = gl_PointCoord - vec2(0.5);
      float r = length(p) * 2.0;
      // Soft gaussian puff — no hard disc edge, so particles read as mist that
      // blends together rather than distinct dots.
      float haze = exp(-r * r * 2.3);
      float alpha = vAlpha * haze;
      if (alpha < 0.003) discard;
      // Pale water-vapor cyan brightening toward a soft white at the wispy tops;
      // deliberately not pure white, so it looks like mist, not snow.
      vec3 cyan = vec3(0.52, 0.82, 0.98);
      vec3 white = vec3(0.86, 0.95, 1.0);
      gl_FragColor = vec4(mix(cyan, white, vMist * 0.7), alpha);
    }
  `;

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function valueAt(grid, row, col) {
    const rows = grid?.values;
    if (!rows) return undefined;
    if (Array.isArray(rows[row]) || ArrayBuffer.isView(rows[row])) {
      return rows[row][col];
    }
    return rows[row * grid.width + col];
  }

  function isNoData(grid, value) {
    if (value === null || value === undefined) return true;
    const n = Number(value);
    if (!Number.isFinite(n)) return true;
    const nodata = grid?.nodata;
    if (nodata === null || nodata === undefined) return false;
    const nd = Number(nodata);
    return Number.isFinite(nd) ? n === nd : String(value) === String(nodata);
  }

  function boundsFor(grid, terrainGrid) {
    if (Array.isArray(grid?.bounds_local) && grid.bounds_local.length >= 4) {
      return grid.bounds_local.map(Number);
    }
    if (terrainGrid) {
      return [terrainGrid.minX, terrainGrid.minY, terrainGrid.maxX, terrainGrid.maxY].map(Number);
    }
    return null;
  }

  function defaultSampleGrid(grid, bounds, x, y) {
    const [minx, miny, maxx, maxy] = bounds;
    if (x < minx || x > maxx || y < miny || y > maxy) return null;
    const col = Math.min(grid.width - 1, Math.max(0, Math.floor(((x - minx) / (maxx - minx)) * grid.width)));
    const row = Math.min(grid.height - 1, Math.max(0, Math.floor(((maxy - y) / (maxy - miny)) * grid.height)));
    return { row, col, value: valueAt(grid, row, col) };
  }

  function buildWeightedCells(grid) {
    const width = Math.floor(Number(grid?.width));
    const height = Math.floor(Number(grid?.height));
    if (!grid?.values || width <= 0 || height <= 0) {
      return { cells: [], maxValue: 0, totalWeight: 0 };
    }

    let maxValue = 0;
    for (let row = 0; row < height; row += 1) {
      for (let col = 0; col < width; col += 1) {
        const value = valueAt(grid, row, col);
        if (isNoData(grid, value)) continue;
        maxValue = Math.max(maxValue, Number(value));
      }
    }
    if (maxValue <= 0) {
      return { cells: [], maxValue: 0, totalWeight: 0 };
    }

    const minValue = maxValue * MIN_AET_WEIGHT;
    const cells = [];
    let totalWeight = 0;
    for (let row = 0; row < height; row += 1) {
      for (let col = 0; col < width; col += 1) {
        const value = valueAt(grid, row, col);
        if (isNoData(grid, value)) continue;
        const n = Number(value);
        if (n <= minValue) continue;
        const weight = clamp(n / maxValue, 0, 1);
        totalWeight += weight;
        cells.push({ row, col, weight, cumulative: totalWeight });
      }
    }
    return { cells, maxValue, totalWeight };
  }

  function pickWeightedCell(cells, totalWeight) {
    const target = Math.random() * totalWeight;
    let lo = 0;
    let hi = cells.length - 1;
    while (lo < hi) {
      const mid = Math.floor((lo + hi) / 2);
      if (cells[mid].cumulative < target) lo = mid + 1;
      else hi = mid;
    }
    return cells[lo];
  }

  function trimArray(array, used, stride) {
    const size = used * stride;
    return size === array.length ? array : array.slice(0, size);
  }

  function mmPerDayToIntensity(mmPerDay) {
    const mm = Number(mmPerDay);
    if (!Number.isFinite(mm)) {
      return DEFAULT_MM_PER_DAY / FULL_SCALE_MM_PER_DAY;
    }
    return clamp(mm / FULL_SCALE_MM_PER_DAY, 0, 1.35);
  }

  function createMaterial(initialIntensity) {
    return new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uIntensity: { value: initialIntensity },
        uPixelRatio: { value: Math.min(global.devicePixelRatio || 1, 2) },
      },
      vertexShader: VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
      blending: THREE.AdditiveBlending,
      depthTest: true,
      depthWrite: false,
      transparent: true,
    });
  }

  class VaporRenderer {
    constructor(scene, opts = {}) {
      this.scene = scene;
      this.sampleGrid = opts.sampleGrid || defaultSampleGrid;
      this.maxParticles = Math.min(DEFAULT_MAX_PARTICLES, Math.max(300, Math.floor(opts.maxParticles || DEFAULT_MAX_PARTICLES)));
      this.terrainGrid = opts.terrainGrid || null;
      this.aetGrid = null;
      this.points = null;
      this.material = null;
      this.visible = false;
      this.disposed = false;
      this.time = 0;
      this.intensity = mmPerDayToIntensity(opts.initialMmPerDay);
      this.unregisterFrame = typeof opts.onFrame === 'function'
        ? opts.onFrame((deltaSeconds) => this.update(deltaSeconds))
        : null;
    }

    setField(aetGrid, terrainGrid) {
      if (this.disposed) return;
      this.aetGrid = aetGrid || null;
      this.terrainGrid = terrainGrid || this.terrainGrid || null;
      if (!this.aetGrid || !this.terrainGrid) {
        this.clearPoints();
        return;
      }

      const geometry = this.buildGeometry();
      this.clearPoints();
      if (!geometry) return;
      if (!this.material) {
        this.material = createMaterial(this.intensity);
      }
      this.points = new THREE.Points(geometry, this.material);
      this.points.name = 'et-water-vapor';
      this.points.renderOrder = 14;
      this.points.frustumCulled = false;
      this.points.visible = this.visible;
      this.points.raycast = () => {};
      this.scene.add(this.points);
    }

    setIntensity(mmPerDay) {
      this.intensity = mmPerDayToIntensity(mmPerDay);
      if (this.material) {
        this.material.uniforms.uIntensity.value = this.intensity;
      }
    }

    setVisible(visible) {
      this.visible = Boolean(visible);
      if (this.points) {
        this.points.visible = this.visible;
      }
    }

    update(deltaSeconds) {
      if (this.disposed || !this.visible || !this.points || !this.material) {
        return;
      }
      const dt = Math.min(0.1, Math.max(0, Number(deltaSeconds) || 0));
      this.time = (this.time + dt) % 10000;
      this.material.uniforms.uTime.value = this.time;
      this.material.uniforms.uPixelRatio.value = Math.min(global.devicePixelRatio || 1, 2);
    }

    dispose() {
      if (this.disposed) return;
      this.unregisterFrame?.();
      this.unregisterFrame = null;
      this.clearPoints();
      this.material?.dispose();
      this.material = null;
      this.disposed = true;
    }

    clearPoints() {
      if (!this.points) return;
      this.scene.remove(this.points);
      this.points.geometry?.dispose();
      this.points = null;
    }

    buildGeometry() {
      const bounds = boundsFor(this.aetGrid, this.terrainGrid);
      if (!bounds || bounds.some((value) => !Number.isFinite(value)) || bounds[2] <= bounds[0] || bounds[3] <= bounds[1]) {
        return null;
      }

      const { cells, maxValue, totalWeight } = buildWeightedCells(this.aetGrid);
      if (!cells.length || totalWeight <= 0) {
        return null;
      }

      const span = Math.max(bounds[2] - bounds[0], bounds[3] - bounds[1]);
      // Taller lift so vapor visibly rises off the surface in columns rather
      // than hugging the ground like a speckle.
      const fieldRise = clamp(span * 0.06, 20, 48);
      const targetCount = Math.min(this.maxParticles, Math.max(420, Math.round(Math.sqrt(cells.length) * 46)));
      const positions = new Float32Array(targetCount * 3);
      const weights = new Float32Array(targetCount);
      const phases = new Float32Array(targetCount);
      const seeds = new Float32Array(targetCount);
      const speeds = new Float32Array(targetCount);
      const rises = new Float32Array(targetCount);
      const sizes = new Float32Array(targetCount);
      const gates = new Float32Array(targetCount);
      const width = this.aetGrid.width;
      const height = this.aetGrid.height;
      const cellW = (bounds[2] - bounds[0]) / width;
      const cellH = (bounds[3] - bounds[1]) / height;

      let used = 0;
      let guard = 0;
      while (used < targetCount && guard < targetCount * 18) {
        guard += 1;
        const cell = pickWeightedCell(cells, totalWeight);
        const x = bounds[0] + (cell.col + Math.random()) * cellW;
        const y = bounds[3] - (cell.row + Math.random()) * cellH;
        if (VEILTerrain?.hasValidTerrainAtLocal && !VEILTerrain.hasValidTerrainAtLocal(this.terrainGrid, x, y)) {
          continue;
        }
        const sampled = this.sampleGrid(this.aetGrid, bounds, x, y);
        const sampleValue = sampled && !isNoData(this.aetGrid, sampled.value) ? Number(sampled.value) : null;
        const weight = sampleValue != null ? clamp(sampleValue / Math.max(1e-9, maxValue), 0, 1) : cell.weight;
        const baseHeight = VEILTerrain.sampleTerrainHeightAtLocal(this.terrainGrid, x, y);
        const offset = used * 3;
        positions[offset] = x;
        positions[offset + 1] = baseHeight + 0.10 + Math.random() * 0.18;
        positions[offset + 2] = -y;
        weights[used] = clamp(Number.isFinite(weight) ? weight : cell.weight, 0.03, 1);
        phases[used] = Math.random();
        seeds[used] = Math.random() * 1000;
        speeds[used] = 0.05 + weights[used] * 0.09 + Math.random() * 0.025;
        rises[used] = fieldRise * (0.32 + Math.pow(weights[used], 0.72) * 0.82) * (0.8 + Math.random() * 0.4);
        sizes[used] = 34 + weights[used] * 54 + Math.random() * 18;
        gates[used] = Math.random();
        used += 1;
      }

      if (!used) {
        return null;
      }

      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(trimArray(positions, used, 3), 3));
      geometry.setAttribute('aWeight', new THREE.BufferAttribute(trimArray(weights, used, 1), 1));
      geometry.setAttribute('aPhase', new THREE.BufferAttribute(trimArray(phases, used, 1), 1));
      geometry.setAttribute('aSeed', new THREE.BufferAttribute(trimArray(seeds, used, 1), 1));
      geometry.setAttribute('aSpeed', new THREE.BufferAttribute(trimArray(speeds, used, 1), 1));
      geometry.setAttribute('aRise', new THREE.BufferAttribute(trimArray(rises, used, 1), 1));
      geometry.setAttribute('aSize', new THREE.BufferAttribute(trimArray(sizes, used, 1), 1));
      geometry.setAttribute('aGate', new THREE.BufferAttribute(trimArray(gates, used, 1), 1));
      geometry.computeBoundingSphere();
      return geometry;
    }
  }

  global.VEILVapor = {
    create(scene, opts) {
      return new VaporRenderer(scene, opts);
    },
  };
})(typeof window !== 'undefined' ? window : globalThis);
