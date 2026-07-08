/* 3D building models for the buildings scene layer.

   Loads the repacked photogrammetry GLBs from data/buildings/models/ (see
   scripts/build_building_models.py) and places each on its footprint. The
   vendored three.min.js has no GLTFLoader, so this includes a minimal GLB
   parser for what these assets actually use: glTF 2.0, tightly packed
   accessors, embedded PNG/JPEG baseColor textures, no Draco / no skinning.

   Placement comes from manifest.json: scene-local (x, y) meters, yaw degrees
   CCW from east, optional rot_x/rot_z tilt degrees, uniform scale, z_offset
   meters. The model is re-anchored so its footprint center sits at (x, y) and
   its base sits on the terrain. */
(function attachBuildings3D(global) {
  const { THREE, VEILTerrain } = global;

  const COMPONENT_ARRAYS = {
    5121: Uint8Array,
    5123: Uint16Array,
    5125: Uint32Array,
    5126: Float32Array,
  };
  const TYPE_SIZES = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 };

  function parseGlbChunks(arrayBuffer) {
    const header = new DataView(arrayBuffer);
    if (header.getUint32(0, true) !== 0x46546c67) {
      throw new Error('not a GLB file');
    }
    let offset = 12;
    let gltf = null;
    let binary = null;
    while (offset < header.getUint32(8, true)) {
      const length = header.getUint32(offset, true);
      const type = header.getUint32(offset + 4, true);
      const chunk = arrayBuffer.slice(offset + 8, offset + 8 + length);
      if (type === 0x4e4f534a) {
        gltf = JSON.parse(new TextDecoder().decode(chunk));
      } else if (type === 0x004e4942) {
        binary = chunk;
      }
      offset += 8 + length;
    }
    return { gltf, binary };
  }

  function accessorToArray(gltf, binary, accessorIndex) {
    const accessor = gltf.accessors[accessorIndex];
    const view = gltf.bufferViews[accessor.bufferView];
    const ArrayType = COMPONENT_ARRAYS[accessor.componentType];
    const itemSize = TYPE_SIZES[accessor.type];
    if (!ArrayType || !itemSize) {
      throw new Error(`unsupported accessor ${accessor.componentType}/${accessor.type}`);
    }
    const stride = view.byteStride || 0;
    const tight = itemSize * ArrayType.BYTES_PER_ELEMENT;
    if (stride && stride !== tight) {
      throw new Error('interleaved accessors not supported');
    }
    const start = (view.byteOffset || 0) + (accessor.byteOffset || 0);
    return {
      array: new ArrayType(binary, start, accessor.count * itemSize),
      itemSize,
    };
  }

  async function loadTexture(gltf, binary, textureIndex, maxAnisotropy = 1) {
    const image = gltf.images[gltf.textures[textureIndex].source];
    const view = gltf.bufferViews[image.bufferView];
    const blob = new Blob(
      [new Uint8Array(binary, view.byteOffset || 0, view.byteLength)],
      { type: image.mimeType }
    );
    const bitmap = await createImageBitmap(blob);
    const texture = new THREE.Texture(bitmap);
    texture.flipY = false; // glTF UV convention
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.wrapS = THREE.RepeatWrapping;
    texture.wrapT = THREE.RepeatWrapping;
    texture.anisotropy = Math.max(1, Math.min(8, maxAnisotropy || 1));
    texture.needsUpdate = true;
    return texture;
  }

  async function buildMaterial(gltf, binary, materialIndex, textureCache, maxAnisotropy, options = {}) {
    const def = gltf.materials?.[materialIndex] || {};
    const pbr = def.pbrMetallicRoughness || {};
    const params = {
      color: new THREE.Color().fromArray(pbr.baseColorFactor || [1, 1, 1]),
      side: def.doubleSided ? THREE.DoubleSide : THREE.FrontSide,
    };
    if (!options.unlit) {
      params.metalness = pbr.metallicFactor ?? 1;
      params.roughness = pbr.roughnessFactor ?? 1;
    }
    if (pbr.baseColorTexture) {
      const index = pbr.baseColorTexture.index;
      if (!textureCache.has(index)) {
        textureCache.set(index, loadTexture(gltf, binary, index, maxAnisotropy));
      }
      params.map = await textureCache.get(index);
    }
    if (options.unlit) {
      const material = new THREE.MeshBasicMaterial(params);
      material.toneMapped = false;
      return material;
    }
    return new THREE.MeshStandardMaterial(params);
  }

  async function loadGlbAsGroup(url, maxAnisotropy, options = {}) {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`Failed to load ${url}: ${response.status}`);
    }
    const { gltf, binary } = parseGlbChunks(await response.arrayBuffer());
    const group = new THREE.Group();
    const textureCache = new Map();

    for (const mesh of gltf.meshes || []) {
      for (const primitive of mesh.primitives || []) {
        const geometry = new THREE.BufferGeometry();
        for (const [attribute, name] of [
          ['POSITION', 'position'],
          ['NORMAL', 'normal'],
          ['TEXCOORD_0', 'uv'],
        ]) {
          if (primitive.attributes[attribute] === undefined) continue;
          const { array, itemSize } = accessorToArray(gltf, binary, primitive.attributes[attribute]);
          geometry.setAttribute(name, new THREE.BufferAttribute(array, itemSize));
        }
        if (primitive.indices !== undefined) {
          const { array } = accessorToArray(gltf, binary, primitive.indices);
          geometry.setIndex(new THREE.BufferAttribute(array, 1));
        }
        if (!geometry.getAttribute('normal')) {
          geometry.computeVertexNormals();
        }
        const material = await buildMaterial(
          gltf,
          binary,
          primitive.material,
          textureCache,
          maxAnisotropy,
          options
        );
        group.add(new THREE.Mesh(geometry, material));
      }
    }
    return group;
  }

  function applyPlacement(anchored, placement, grid) {
    const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, placement.x, placement.y);
    anchored.scale.setScalar(placement.scale || 1);
    // yaw (rotation.y) rotates +east toward +north, matching the footprint
    // angle convention (degrees CCW from east in map view); rot_x/rot_z are
    // optional tilts from the gizmo editor.
    anchored.rotation.set(
      THREE.MathUtils.degToRad(placement.rot_x_deg || 0),
      THREE.MathUtils.degToRad(placement.yaw_deg || 0),
      THREE.MathUtils.degToRad(placement.rot_z_deg || 0)
    );
    anchored.position.set(placement.x, terrainY + (placement.z_offset || 0), -placement.y);
  }

  function placeBuilding(model, entry, grid) {
    // These assets are Z-up; tip them into three.js Y-up before measuring.
    if (entry.up_axis === 'z') {
      model.rotation.x = -Math.PI / 2;
    }
    // Re-anchor: footprint center -> origin, base -> y=0.
    const bbox = new THREE.Box3().setFromObject(model);
    const recenter = new THREE.Group();
    recenter.position.set(
      -(bbox.min.x + bbox.max.x) / 2,
      -bbox.min.y,
      -(bbox.min.z + bbox.max.z) / 2
    );
    recenter.add(model);
    const anchored = new THREE.Group();
    anchored.add(recenter);
    anchored.userData.entry = entry;
    applyPlacement(anchored, entry.placement, grid);
    return anchored;
  }

  async function load(viewer, manifestUrl) {
    const response = await fetch(manifestUrl);
    if (!response.ok) {
      throw new Error(`Failed to load ${manifestUrl}: ${response.status}`);
    }
    const manifest = await response.json();
    const layerGroup = new THREE.Group();
    layerGroup.name = 'building-models';
    viewer.buildingModelsGroup = layerGroup;
    viewer.scene.add(layerGroup);
    const maxAnisotropy = viewer.renderer?.capabilities?.getMaxAnisotropy?.() || 1;

    await Promise.all((manifest.buildings || []).map(async (entry) => {
      try {
        const model = await loadGlbAsGroup(entry.url, maxAnisotropy, {
          unlit: entry.material === 'unlit',
        });
        const placed = placeBuilding(model, entry, viewer.terrainGrid);
        placed.name = entry.id;
        placed.traverse((child) => {
          if (child?.isMesh) {
            child.castShadow = Boolean(viewer.photometricMode);
            child.receiveShadow = Boolean(viewer.photometricMode);
          }
        });
        layerGroup.add(placed);
      } catch (error) {
        console.error(`building model ${entry.id} failed:`, error);
      }
    }));
    return layerGroup;
  }

  global.VEILBuildings3D = { load, applyPlacement };
})(window);
