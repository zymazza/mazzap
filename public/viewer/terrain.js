(function attachTerrainHelpers(global) {
  const { THREE } = global;

  function colorForHeight(ratio) {
    const palette = [
      new THREE.Color('#2a6041'),
      new THREE.Color('#4f7459'),
      new THREE.Color('#8d98a7'),
      new THREE.Color('#dcccbb'),
      new THREE.Color('#eab464'),
    ];
    const scaled = Math.max(0, Math.min(0.9999, ratio)) * (palette.length - 1);
    const lowerIndex = Math.floor(scaled);
    const upperIndex = Math.min(palette.length - 1, lowerIndex + 1);
    const blend = scaled - lowerIndex;
    return palette[lowerIndex].clone().lerp(palette[upperIndex], blend);
  }

  function gridSteps(grid) {
    return {
      x:
        grid.width > 1 ? (grid.maxX - grid.minX) / (grid.width - 1) : grid.xStep || 1,
      y:
        grid.height > 1 ? (grid.maxY - grid.minY) / (grid.height - 1) : grid.yStep || 1,
    };
  }

  function isValidGridIndex(grid, index) {
    return Number.isFinite(grid.heights[index]);
  }

  function localVertexForGridIndex(grid, index, xStep, yStep, yOverride = null) {
    const column = index % grid.width;
    const row = Math.floor(index / grid.width);
    const localX = grid.minX + column * xStep;
    const localY = grid.maxY - row * yStep;
    const elevation = grid.heights[index];
    const safeElevation = Number.isFinite(elevation) ? elevation : grid.minElevation;
    return [
      localX,
      yOverride === null ? safeElevation - grid.minElevation : yOverride,
      -localY,
    ];
  }

  function buildTerrainTriangleIndices(grid) {
    function isValidIndex(index) {
      return isValidGridIndex(grid, index);
    }

    const indices = [];
    for (let row = 0; row < grid.height - 1; row += 1) {
      for (let column = 0; column < grid.width - 1; column += 1) {
        const topLeft = row * grid.width + column;
        const topRight = topLeft + 1;
        const bottomLeft = topLeft + grid.width;
        const bottomRight = bottomLeft + 1;

        if (isValidIndex(topLeft) && isValidIndex(bottomLeft) && isValidIndex(topRight)) {
          indices.push(topLeft, bottomLeft, topRight);
        }
        if (isValidIndex(topRight) && isValidIndex(bottomLeft) && isValidIndex(bottomRight)) {
          indices.push(topRight, bottomLeft, bottomRight);
        }
      }
    }
    return indices;
  }

  function buildTerrainMesh(grid) {
    const geometry = new THREE.BufferGeometry();
    const positions = new Float32Array(grid.width * grid.height * 3);
    const colors = new Float32Array(grid.width * grid.height * 3);
    const uvs = new Float32Array(grid.width * grid.height * 2);
    const elevationRange = Math.max(1, grid.maxElevation - grid.minElevation);
    const { x: xStep, y: yStep } = gridSteps(grid);

    for (let index = 0; index < grid.heights.length; index += 1) {
      const column = index % grid.width;
      const row = Math.floor(index / grid.width);
      const elevation = grid.heights[index];
      const valid = Number.isFinite(elevation);
      const safeElevation = valid ? elevation : grid.minElevation;
      const vertex = localVertexForGridIndex(grid, index, xStep, yStep);
      positions[index * 3] = vertex[0];
      positions[index * 3 + 1] = vertex[1];
      positions[index * 3 + 2] = vertex[2];
      const ratio = (safeElevation - grid.minElevation) / elevationRange;
      const color = colorForHeight(ratio);
      colors[index * 3] = color.r;
      colors[index * 3 + 1] = color.g;
      colors[index * 3 + 2] = color.b;
      // Terrain vertices are sampled at DEM cell centers, while drape imagery is defined on
      // the raster's outer edges. Offset the UVs by half a pixel so textures line up with the
      // true raster footprint instead of stretching edge-to-edge across the center samples.
      uvs[index * 2] = grid.width > 0 ? (column + 0.5) / grid.width : 0;
      uvs[index * 2 + 1] = grid.height > 0 ? 1 - (row + 0.5) / grid.height : 1;
    }

    const indices = buildTerrainTriangleIndices(grid);

    geometry.setIndex(indices);
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
    geometry.computeVertexNormals();

    const material = new THREE.MeshStandardMaterial({
      vertexColors: true,
      metalness: 0.06,
      roughness: 0.92,
    });

    return {
      elevationMaterial: material,
      geometry,
      mesh: new THREE.Mesh(geometry, material),
    };
  }

  function buildTerrainBaseMesh(grid, options = {}) {
    if (!grid || !Array.isArray(grid.heights) || !grid.heights.length) {
      return null;
    }

    const validBounds = { minColumn: Infinity, maxColumn: -Infinity, minRow: Infinity, maxRow: -Infinity };
    for (let index = 0; index < grid.heights.length; index += 1) {
      if (!isValidGridIndex(grid, index)) {
        continue;
      }
      const column = index % grid.width;
      const row = Math.floor(index / grid.width);
      validBounds.minColumn = Math.min(validBounds.minColumn, column);
      validBounds.maxColumn = Math.max(validBounds.maxColumn, column);
      validBounds.minRow = Math.min(validBounds.minRow, row);
      validBounds.maxRow = Math.max(validBounds.maxRow, row);
    }
    if (!Number.isFinite(validBounds.minColumn) || validBounds.minColumn === validBounds.maxColumn ||
        validBounds.minRow === validBounds.maxRow) {
      return null;
    }

    const floorY = Number.isFinite(options.floorY) ? options.floorY : -0.03;
    const { x: xStep, y: yStep } = gridSteps(grid);
    const positions = [];
    const indices = [];

    function pushVertex(vertex) {
      const index = positions.length / 3;
      positions.push(vertex[0], vertex[1], vertex[2]);
      return index;
    }

    function gridIndex(column, row) {
      return row * grid.width + column;
    }

    function nearestValidIndex(column, row, axis) {
      const start = axis === 'row' ? validBounds.minRow : validBounds.minColumn;
      const end = axis === 'row' ? validBounds.maxRow : validBounds.maxColumn;
      let best = null;
      let bestDistance = Infinity;
      for (let cursor = start; cursor <= end; cursor += 1) {
        const candidate = axis === 'row' ? gridIndex(column, cursor) : gridIndex(cursor, row);
        if (!isValidGridIndex(grid, candidate)) {
          continue;
        }
        const distance = Math.abs(cursor - (axis === 'row' ? row : column));
        if (distance < bestDistance) {
          best = candidate;
          bestDistance = distance;
        }
      }
      return best;
    }

    function perimeterIndex(column, row, axis) {
      const index = gridIndex(column, row);
      return isValidGridIndex(grid, index) ? index : nearestValidIndex(column, row, axis);
    }

    function addWallSegment(firstGridIndex, secondGridIndex) {
      if (firstGridIndex === null || secondGridIndex === null) {
        return;
      }
      const topA = pushVertex(localVertexForGridIndex(grid, firstGridIndex, xStep, yStep));
      const topB = pushVertex(localVertexForGridIndex(grid, secondGridIndex, xStep, yStep));
      const bottomB = pushVertex(localVertexForGridIndex(grid, secondGridIndex, xStep, yStep, floorY));
      const bottomA = pushVertex(localVertexForGridIndex(grid, firstGridIndex, xStep, yStep, floorY));
      indices.push(topA, topB, bottomB, topA, bottomB, bottomA);
    }

    for (let column = validBounds.minColumn; column < validBounds.maxColumn; column += 1) {
      addWallSegment(
        perimeterIndex(column, validBounds.minRow, 'row'),
        perimeterIndex(column + 1, validBounds.minRow, 'row')
      );
      addWallSegment(
        perimeterIndex(column + 1, validBounds.maxRow, 'row'),
        perimeterIndex(column, validBounds.maxRow, 'row')
      );
    }
    for (let row = validBounds.minRow; row < validBounds.maxRow; row += 1) {
      addWallSegment(
        perimeterIndex(validBounds.maxColumn, row, 'column'),
        perimeterIndex(validBounds.maxColumn, row + 1, 'column')
      );
      addWallSegment(
        perimeterIndex(validBounds.minColumn, row + 1, 'column'),
        perimeterIndex(validBounds.minColumn, row, 'column')
      );
    }

    if (!indices.length) {
      return null;
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices);
    geometry.computeVertexNormals();

    const material = new THREE.MeshBasicMaterial({
      color: options.color || 0xc2b29e,
      side: THREE.DoubleSide,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.renderOrder = -2;
    mesh.name = 'terrain-base-pedestal';
    return mesh;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function sampleTerrainHeightAtLocal(grid, localX, localY) {
    if (!grid || !Array.isArray(grid.heights) || !grid.heights.length) {
      return 0;
    }

    const widthMeters = Math.max(1e-9, grid.maxX - grid.minX);
    const heightMeters = Math.max(1e-9, grid.maxY - grid.minY);
    const xRatio = clamp((localX - grid.minX) / widthMeters, 0, 0.999999);
    const yRatio = clamp((localY - grid.minY) / heightMeters, 0, 0.999999);
    const xIndex = xRatio * (grid.width - 1);
    const yIndex = (1 - yRatio) * (grid.height - 1);
    const x0 = Math.floor(xIndex);
    const y0 = Math.floor(yIndex);
    const x1 = Math.min(grid.width - 1, x0 + 1);
    const y1 = Math.min(grid.height - 1, y0 + 1);
    const tx = xIndex - x0;
    const ty = yIndex - y0;
    const indexAt = (x, y) => y * grid.width + x;
    const h00 = grid.heights[indexAt(x0, y0)];
    const h10 = grid.heights[indexAt(x1, y0)];
    const h01 = grid.heights[indexAt(x0, y1)];
    const h11 = grid.heights[indexAt(x1, y1)];
    const samples = [
      { value: h00, weight: (1 - tx) * (1 - ty) },
      { value: h10, weight: tx * (1 - ty) },
      { value: h01, weight: (1 - tx) * ty },
      { value: h11, weight: tx * ty },
    ].filter((sample) => Number.isFinite(sample.value));

    if (!samples.length) {
      return 0;
    }

    const totalWeight = samples.reduce((sum, sample) => sum + sample.weight, 0);
    if (totalWeight <= 0) {
      return samples[0].value - grid.minElevation;
    }

    const weighted =
      samples.reduce((sum, sample) => sum + sample.value * sample.weight, 0) / totalWeight;
    return weighted - grid.minElevation;
  }

  // True only where the DEM has a real elevation (inside the rendered terrain).
  // Used to keep vegetation from floating over the nodata area beyond the parcel.
  function hasValidTerrainAtLocal(grid, localX, localY) {
    if (!grid || !Array.isArray(grid.heights) || !grid.heights.length) {
      return false;
    }
    if (localX < grid.minX || localX > grid.maxX || localY < grid.minY || localY > grid.maxY) {
      return false;
    }
    const widthMeters = Math.max(1e-9, grid.maxX - grid.minX);
    const heightMeters = Math.max(1e-9, grid.maxY - grid.minY);
    const col = Math.round(((localX - grid.minX) / widthMeters) * (grid.width - 1));
    const row = Math.round((1 - (localY - grid.minY) / heightMeters) * (grid.height - 1));
    if (col < 0 || col >= grid.width || row < 0 || row >= grid.height) {
      return false;
    }
    return Number.isFinite(grid.heights[row * grid.width + col]);
  }

  function dataUrl(path) {
    const clean = String(path || '').replace(/^\/+/, '');
    return clean.startsWith('data/') ? `/${clean}` : `/data/${clean}`;
  }

  async function fetchJson(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`${path} returned ${res.status}`);
    return res.json();
  }

  async function fetchBinary(path) {
    const url = dataUrl(path);
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} returned ${res.status}`);
    return res.arrayBuffer();
  }

  function ringBounds(item) {
    const b = item?.bounds_local || [0, 0, 0, 0];
    return {
      minX: Number(b[0]),
      minY: Number(b[1]),
      maxX: Number(b[2]),
      maxY: Number(b[3]),
    };
  }

  function tileShape(ring, tile) {
    const size = Number(ring.tileSize || 256);
    const row0 = Number(tile.j) * size;
    const col0 = Number(tile.i) * size;
    return {
      row0,
      col0,
      rows: Math.max(0, Math.min(ring.height, row0 + size) - row0),
      cols: Math.max(0, Math.min(ring.width, col0 + size) - col0),
    };
  }

  function copyGroundTile(ring, tile, buffer) {
    const src = new Int16Array(buffer);
    const shape = tileShape(ring, tile);
    let k = 0;
    let validCells = 0;
    for (let r = 0; r < shape.rows; r += 1) {
      const dstRow = shape.row0 + r;
      for (let c = 0; c < shape.cols; c += 1) {
        const dm = src[k];
        const idx = dstRow * ring.width + shape.col0 + c;
        if (dm === -32768) {
          ring.ground[idx] = NaN;
        } else {
          ring.ground[idx] = dm / 10;
          validCells += 1;
        }
        k += 1;
      }
    }
    tile.validCells = validCells;
  }

  function copyCanopyTile(ring, tile, buffer) {
    if (!ring.canopy) return;
    const src = new Uint8Array(buffer);
    const shape = tileShape(ring, tile);
    let k = 0;
    for (let r = 0; r < shape.rows; r += 1) {
      const dstRow = shape.row0 + r;
      for (let c = 0; c < shape.cols; c += 1) {
        ring.canopy[dstRow * ring.width + shape.col0 + c] = src[k] / 10;
        k += 1;
      }
    }
  }

  async function loadManifestRing(item) {
    const b = ringBounds(item);
    const ring = {
      id: String(item.id || item.name || ''),
      width: Number(item.width),
      height: Number(item.height),
      tileSize: Number(item.tile_size || 256),
      minX: b.minX,
      maxX: b.maxX,
      minY: b.minY,
      maxY: b.maxY,
      resolutionM: Number(item.resolution_m || 1),
      innerM: Number(item.inner_m || 0),
      outerM: Number(item.outer_m || 0),
      cellAreaM2: 0,
      ground: null,
      canopy: item.canopy_available ? null : null,
      tiles: [],
      source: item,
    };
    ring.cellAreaM2 = Math.abs(
      ((ring.maxX - ring.minX) / Math.max(1, ring.width - 1)) *
      ((ring.maxY - ring.minY) / Math.max(1, ring.height - 1))
    );
    ring.ground = new Float32Array(ring.width * ring.height);
    ring.ground.fill(NaN);
    const hasCanopy = (item.tiles || []).some((tile) => tile.canopy);
    ring.canopy = hasCanopy ? new Float32Array(ring.width * ring.height) : null;
    ring.tiles = (item.tiles || []).map((tile) => ({
      i: Number(tile.i),
      j: Number(tile.j),
      ground: tile.ground,
      canopy: tile.canopy || null,
      imagery: tile.imagery || null,
      key: `${ring.id}:${Number(tile.i)}:${Number(tile.j)}`,
      validCells: 0,
    }));
    await Promise.all(ring.tiles.map(async (tile) => {
      const ground = await fetchBinary(tile.ground);
      copyGroundTile(ring, tile, ground);
      if (tile.canopy && ring.canopy) {
        copyCanopyTile(ring, tile, await fetchBinary(tile.canopy));
      }
    }));
    return ring;
  }

  const DISTANT_VERT = `
    uniform float uBaseElevation;
    varying vec3 vWorld;
    varying float vElevation;
    varying vec2 vUv;
    void main() {
      vec4 world = modelMatrix * vec4(position, 1.0);
      vWorld = world.xyz;
      vElevation = position.y + uBaseElevation;
      vUv = uv;
      gl_Position = projectionMatrix * viewMatrix * world;
    }
  `;

  const DISTANT_FRAG = `
    precision highp float;
    uniform float uVisible;
    uniform float uHasImagery;
    uniform float uMinElevation;
    uniform float uMaxElevation;
    uniform float uFogNear;
    uniform float uFogFar;
    uniform vec3 uLowColor;
    uniform vec3 uHighColor;
    uniform vec3 uHazeColor;
    uniform vec3 uLightDir;
    uniform sampler2D uImagery;
    varying vec3 vWorld;
    varying float vElevation;
    varying vec2 vUv;

    void main() {
      vec3 dx = dFdx(vWorld);
      vec3 dy = dFdy(vWorld);
      vec3 n = normalize(cross(dx, dy));
      if (n.y < 0.0) n = -n;
      float light = clamp(dot(n, normalize(uLightDir)), 0.0, 1.0);
      float rampShade = clamp(light * 0.62 + 0.38, 0.18, 1.0);
      float imageryShade = mix(0.85, 1.0, light);
      float e = clamp((vElevation - uMinElevation) / max(1.0, uMaxElevation - uMinElevation), 0.0, 1.0);
      vec3 rampBase = mix(uLowColor, uHighColor, e) * rampShade;
      vec3 imageBase = texture2D(uImagery, clamp(vUv, 0.0, 1.0)).rgb * imageryShade;
      vec3 base = mix(rampBase, imageBase, step(0.5, uHasImagery));
      float dist = length(cameraPosition - vWorld);
      float haze = smoothstep(uFogNear, uFogFar, dist);
      vec3 color = mix(base, uHazeColor, clamp(haze, 0.0, 0.88));
      float vis = clamp(uVisible, 0.0, 1.0);
      if (vis <= 0.004) discard;
      gl_FragColor = vec4(color, vis);
      #include <tonemapping_fragment>
      #include <colorspace_fragment>
    }
  `;

  let distantFallbackTexture = null;

  function getDistantFallbackTexture() {
    if (!distantFallbackTexture) {
      distantFallbackTexture = new THREE.DataTexture(
        new Uint8Array([128, 128, 128, 255]),
        1,
        1,
        THREE.RGBAFormat
      );
      distantFallbackTexture.colorSpace = THREE.SRGBColorSpace;
      distantFallbackTexture.needsUpdate = true;
    }
    return distantFallbackTexture;
  }

  function makeDistantMaterial(baseElevation, minElevation, maxElevation) {
    return new THREE.ShaderMaterial({
      uniforms: {
        uVisible: { value: 0 },
        uHasImagery: { value: 0 },
        uBaseElevation: { value: baseElevation },
        uMinElevation: { value: minElevation },
        uMaxElevation: { value: maxElevation },
        uFogNear: { value: 18000 },
        uFogFar: { value: 190000 },
        uLowColor: { value: new THREE.Color(0x526f64) },
        uHighColor: { value: new THREE.Color(0xb8b49f) },
        uHazeColor: { value: new THREE.Color(0xbfd4df) },
        uLightDir: { value: new THREE.Vector3(-0.35, 0.72, 0.48).normalize() },
        uImagery: { value: getDistantFallbackTexture() },
      },
      vertexShader: DISTANT_VERT,
      fragmentShader: DISTANT_FRAG,
      transparent: true,
      depthWrite: true,
      depthTest: true,
      fog: false,
      side: THREE.FrontSide,
      extensions: { derivatives: true },
    });
  }

  function decimatedIndices(start, count, stride) {
    const out = [];
    const end = start + count - 1;
    for (let v = start; v <= end; v += stride) {
      out.push(v);
    }
    if (out[out.length - 1] !== end) {
      out.push(end);
    }
    return out;
  }

  function terrainClipBounds(viewer) {
    const grid = viewer?.terrainGrid;
    if (!grid) return null;
    return {
      minX: Number(grid.minX),
      maxX: Number(grid.maxX),
      minY: Number(grid.minY),
      maxY: Number(grid.maxY),
    };
  }

  function insideClipBounds(bounds, x, y, margin = 0) {
    if (!bounds) return false;
    return (
      x >= bounds.minX - margin && x <= bounds.maxX + margin &&
      y >= bounds.minY - margin && y <= bounds.maxY + margin
    );
  }

  function buildDistantTileMesh(ring, tile, baseElevation, minElevation, maxElevation, clipBounds = null) {
    const stride = ring.id === 'B'
      ? Math.max(1, Math.round(60 / Math.max(1, ring.resolutionM)))
      : Math.max(1, Math.round(300 / Math.max(1, ring.resolutionM)));
    const shape = tileShape(ring, tile);
    if (shape.rows < 2 || shape.cols < 2 || tile.validCells <= 0) {
      return null;
    }
    const xStep = (ring.maxX - ring.minX) / Math.max(1, ring.width - 1);
    const yStep = (ring.maxY - ring.minY) / Math.max(1, ring.height - 1);
    // Distant meshes are decimated, so triangles can span well beyond one
    // source pixel. Clip them back by the decimated triangle scale; otherwise
    // semi-transparent POV viewshed tiles can bleed over the high-resolution
    // local AOI and make valid land appear blue/transparent.
    const clipMargin = Math.max(
      Math.abs(xStep),
      Math.abs(yStep),
      Math.max(0, Number(ring.resolutionM || 0)) * Math.max(2, stride * 2)
    );
    const meshRows = shape.rows + (shape.row0 + shape.rows < ring.height ? 1 : 0);
    const meshCols = shape.cols + (shape.col0 + shape.cols < ring.width ? 1 : 0);
    const rows = decimatedIndices(shape.row0, meshRows, stride);
    const cols = decimatedIndices(shape.col0, meshCols, stride);
    const positions = new Float32Array(rows.length * cols.length * 3);
    const uvs = new Float32Array(rows.length * cols.length * 2);
    const valid = new Uint8Array(rows.length * cols.length);
    let p = 0;
    let uvp = 0;
    for (let rr = 0; rr < rows.length; rr += 1) {
      const row = rows[rr];
      const y = ring.maxY - row * yStep;
      for (let cc = 0; cc < cols.length; cc += 1) {
        const col = cols[cc];
        const x = ring.minX + col * xStep;
        const elevation = ring.ground[row * ring.width + col];
        const ok = Number.isFinite(elevation) &&
          !insideClipBounds(clipBounds, x, y, clipMargin);
        const vi = rr * cols.length + cc;
        valid[vi] = ok ? 1 : 0;
        positions[p] = x;
        positions[p + 1] = (ok ? elevation : minElevation) - baseElevation;
        positions[p + 2] = -y;
        p += 3;
        uvs[uvp] = shape.cols > 0 ? (col - shape.col0 + 0.5) / shape.cols : 0;
        uvs[uvp + 1] = shape.rows > 0 ? 1 - ((row - shape.row0 + 0.5) / shape.rows) : 1;
        uvp += 2;
      }
    }
    const indices = [];
    for (let rr = 0; rr < rows.length - 1; rr += 1) {
      for (let cc = 0; cc < cols.length - 1; cc += 1) {
        const a = rr * cols.length + cc;
        const b = (rr + 1) * cols.length + cc;
        const c = rr * cols.length + cc + 1;
        const d = (rr + 1) * cols.length + cc + 1;
        if (valid[a] && valid[b] && valid[c]) indices.push(a, b, c);
        if (valid[c] && valid[b] && valid[d]) indices.push(c, b, d);
      }
    }
    if (!indices.length) {
      return null;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
    geometry.setIndex(indices);
    geometry.computeBoundingSphere();
    const material = makeDistantMaterial(baseElevation, minElevation, maxElevation);
    const mesh = new THREE.Mesh(geometry, material);
    mesh.name = `distant-terrain-${tile.key}`;
    mesh.visible = false;
    mesh.frustumCulled = true;
    mesh.renderOrder = -3;
    mesh.userData.distantTile = {
      key: tile.key,
      ringId: ring.id,
      i: tile.i,
      j: tile.j,
      validCells: tile.validCells,
      imagery: tile.imagery || null,
      texture: null,
      targetVisible: 0,
      currentVisible: 0,
    };
    return mesh;
  }

  function maxAnisotropy(viewer) {
    const getMax = viewer?.renderer?.capabilities?.getMaxAnisotropy;
    return typeof getMax === 'function' ? Math.max(1, Math.min(8, getMax.call(viewer.renderer.capabilities))) : 1;
  }

  function isPowerOfTwo(value) {
    return value > 0 && (value & (value - 1)) === 0;
  }

  function configureDistantTexture(texture, viewer) {
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = maxAnisotropy(viewer);
    texture.wrapS = THREE.ClampToEdgeWrapping;
    texture.wrapT = THREE.ClampToEdgeWrapping;
    texture.magFilter = THREE.LinearFilter;
    const image = texture.image;
    const canMipmap = image && isPowerOfTwo(image.width || 0) && isPowerOfTwo(image.height || 0);
    texture.generateMipmaps = Boolean(canMipmap);
    texture.minFilter = canMipmap ? THREE.LinearMipmapLinearFilter : THREE.LinearFilter;
    texture.needsUpdate = true;
  }

  function loadDistantTileImagery(viewer, mesh, tile) {
    if (!tile?.imagery || !mesh?.material?.uniforms) {
      return;
    }
    const loader = viewer?.textureLoader || new THREE.TextureLoader();
    const url = dataUrl(tile.imagery);
    const texture = loader.load(
      url,
      (loaded) => {
        configureDistantTexture(loaded, viewer);
        if (mesh.material?.uniforms) {
          mesh.material.uniforms.uImagery.value = loaded;
          mesh.material.uniforms.uHasImagery.value = 1;
        }
      },
      undefined,
      (err) => {
        console.warn(`distant imagery failed for ${tile.key}:`, err);
        if (mesh.material?.uniforms) {
          mesh.material.uniforms.uHasImagery.value = 0;
        }
        texture.dispose?.();
      }
    );
    configureDistantTexture(texture, viewer);
    mesh.userData.distantTile.texture = texture;
  }

  function finiteRange(rings, fallbackMin, fallbackMax) {
    let min = Infinity;
    let max = -Infinity;
    rings.forEach((ring) => {
      for (let i = 0; i < ring.ground.length; i += 1) {
        const v = ring.ground[i];
        if (Number.isFinite(v)) {
          min = Math.min(min, v);
          max = Math.max(max, v);
        }
      }
    });
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
      return { min: fallbackMin, max: Math.max(fallbackMax, fallbackMin + 1) };
    }
    return { min, max };
  }

  function createDistantState(viewer, manifest, rings) {
    const group = new THREE.Group();
    group.name = 'distant-terrain-rings';
    viewer.scene.add(group);
    const maxOuter = Math.max(0, ...rings.map((ring) => Number(ring.outerM || 0)));
    if (viewer.camera && maxOuter > 0 && viewer.camera.far < maxOuter * 1.15) {
      viewer.camera.far = maxOuter * 1.15;
      viewer.camera.updateProjectionMatrix();
    }
    const baseElevation = Number(viewer.terrainGrid?.minElevation || 0);
    const clipBounds = terrainClipBounds(viewer);
    const range = finiteRange(
      rings,
      Number(viewer.terrainGrid?.minElevation || 0),
      Number(viewer.terrainGrid?.maxElevation || 1)
    );
    const meshes = [];
    rings.forEach((ring) => {
      ring.tiles.forEach((tile) => {
        const mesh = buildDistantTileMesh(ring, tile, baseElevation, range.min, range.max, clipBounds);
        if (mesh) {
          group.add(mesh);
          meshes.push(mesh);
          loadDistantTileImagery(viewer, mesh, tile);
        }
      });
    });
    let enabled = true;

    // Opacity for a tile with the given viewshed-visible fraction. The mask is
    // cell-accurate, but distant meshes are coarse tiles; suppress tiny slivers
    // so one visible cell does not reveal an entire distant tile.
    const SOLID_ALPHA = 0.96;
    const MIN_VISIBLE_TILE_FRACTION = 0.015;
    function targetFromFraction(fraction) {
      const f = clamp(Number(fraction) || 0, 0, 1);
      return f >= MIN_VISIBLE_TILE_FRACTION ? Math.min(SOLID_ALPHA, Math.max(0.82, 0.82 + 0.14 * Math.sqrt(f))) : 0;
    }

    return {
      manifest,
      rings,
      group,
      meshes,
      setEnabled(on) {
        enabled = Boolean(on);
        group.visible = enabled;
      },
      showAll(value = SOLID_ALPHA) {
        meshes.forEach((mesh) => {
          mesh.userData.distantTile.targetVisible = enabled ? clamp(value, 0, 1) : 0;
          if (enabled) mesh.visible = true;
        });
      },
      setVisibilityFractions(fractions) {
        const map = fractions instanceof Map ? fractions : new Map(Object.entries(fractions || {}));
        meshes.forEach((mesh) => {
          const info = mesh.userData.distantTile;
          info.targetVisible = enabled ? targetFromFraction(map.get(info.key) || 0) : 0;
          if (info.targetVisible > 0) mesh.visible = true;
        });
      },
      update(dt) {
        const blend = 1 - Math.exp(-Math.max(0, dt) / 0.3);
        meshes.forEach((mesh) => {
          const info = mesh.userData.distantTile;
          const target = enabled ? info.targetVisible : 0;
          info.currentVisible += (target - info.currentVisible) * blend;
          const eased = info.currentVisible * info.currentVisible * (3 - 2 * info.currentVisible);
          mesh.material.uniforms.uVisible.value = eased;
          if (target <= 0 && info.currentVisible <= 0.01) {
            info.currentVisible = 0;
            mesh.material.uniforms.uVisible.value = 0;
            mesh.visible = false;
          }
        });
      },
      dispose() {
        viewer.scene.remove(group);
        meshes.forEach((mesh) => {
          mesh.geometry.dispose();
          mesh.userData.distantTile?.texture?.dispose?.();
          mesh.material.dispose();
        });
      },
    };
  }

  async function ensureDistantTerrain(viewer) {
    if (!viewer) return null;
    if (viewer.distantTerrain) return viewer.distantTerrain;
    if (viewer.distantTerrainPromise) return viewer.distantTerrainPromise;
    viewer.distantTerrainPromise = (async () => {
      const manifest = await fetchJson('/data/terrain/distant/manifest.json');
      const items = (manifest.rings || []).filter((item) => item && !item.kind && item.tiles && item.tiles.length);
      const rings = await Promise.all(items.map(loadManifestRing));
      const state = createDistantState(viewer, manifest, rings);
      viewer.distantTerrain = state;
      viewer.onFrame?.((dt) => state.update(dt));
      return state;
    })().catch((err) => {
      viewer.distantTerrainPromise = null;
      throw err;
    });
    return viewer.distantTerrainPromise;
  }

  global.VEILTerrain = {
    buildTerrainMesh,
    buildTerrainBaseMesh,
    sampleTerrainHeightAtLocal,
    hasValidTerrainAtLocal,
    ensureDistantTerrain,
  };
})(window);
