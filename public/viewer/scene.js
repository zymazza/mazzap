(function attachSceneViewer(global) {
  const { THREE, VEILCamera, VEILTerrain, VEILVegetation, VEILOverlays } = global;
  const VIEWER_CAMERA_DEFAULTS = {
    far: 10000,
    fov: 50,
    maxDevicePixelRatio: 2,
    near: 0.1,
    position: { x: 140, y: 120, z: 140 },
  };
  const GRID_HELPER_CONFIG = {
    clipBoundsHalfSpanMeters: 300,
    divisions: 24,
    initialSizeMeters: 600,
    majorColor: 0x2a6041,
    minSizeMeters: 120,
    minorColor: 0x8d98a7,
    sizePaddingMultiplier: 1.08,
    sizeSnapIncrementMeters: 20,
  };
  const PHOTOMETRIC_CONFIG = {
    dayExposure: 0.55,
    nightExposure: 2.6,
    sunIntensity: 3.2,
    moonIntensity: 0.12,
    shadowMapSize: 4096,
    shadowFallbackMapSize: 2048,
    shadowSunMoveThresholdDeg: 0.1,
    shadowCameraMargin: 1.2,
    lightDistanceMeters: 2000,
  };
  const LAYER_JOB_TYPE_BY_ID = {
    buildings: 'buildings_fetch',
    hydrology: 'hydrology_fetch',
    parcels: 'parcels_fetch',
    roads: 'roads_fetch',
    soils: 'site_snapshot_compute',
    trails: 'trails_fetch',
    vegetation: 'vegetation_extract',
  };

  function debugLog(event, payload = {}) {
    global.__VEIL_DEBUG__?.log?.(event, payload);
  }

  function createAbortError() {
    try {
      return new DOMException('The operation was aborted.', 'AbortError');
    } catch (_error) {
      const error = new Error('The operation was aborted.');
      error.name = 'AbortError';
      return error;
    }
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, Number(value) || 0));
  }

  function smoothstep(edge0, edge1, value) {
    const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
    return t * t * (3 - 2 * t);
  }

  function isAbortError(error) {
    return error?.name === 'AbortError';
  }

  function linkAbortSignal(signal, controller) {
    if (!signal) {
      return () => {};
    }
    if (signal.aborted) {
      controller.abort(signal.reason);
      return () => {};
    }

    const onAbort = () => {
      controller.abort(signal.reason);
    };

    signal.addEventListener('abort', onAbort, { once: true });
    return () => signal.removeEventListener('abort', onAbort);
  }

  function getVegetationItemCount(payload) {
    if (Array.isArray(payload)) {
      return payload.length;
    }
    if (payload && (Array.isArray(payload.items) || ArrayBuffer.isView(payload.items)) && Number(payload.stride) > 0) {
      return Math.floor(payload.items.length / Number(payload.stride));
    }
    return 0;
  }

  function readSceneComputeJob(scenePayload, layerId) {
    const jobType = LAYER_JOB_TYPE_BY_ID[layerId];
    if (!jobType || !Array.isArray(scenePayload?.compute_status?.jobs)) {
      return null;
    }

    return scenePayload.compute_status.jobs.find((job) => job?.id === jobType) || null;
  }

  function normalizeLayerMessage(...candidates) {
    const value = candidates.find((candidate) => typeof candidate === 'string' && candidate.trim());
    return value ? value.trim() : '';
  }

  function resolveDeferredLayerState(scenePayload, layerId, options = {}) {
    const job = readSceneComputeJob(scenePayload, layerId);
    const layerBlock = options.layerBlock && typeof options.layerBlock === 'object' ? options.layerBlock : {};
    const layerLabel = options.layerLabel || layerId;
    const rawStatus =
      job?.status === 'error' || job?.status === 'canceled'
        ? 'error'
        : options.layerStatus || layerBlock.status || job?.status || 'queued';
    const message = normalizeLayerMessage(
      job?.message,
      layerBlock.message,
      layerBlock.error_message,
      layerBlock.degraded_reason
    );

    if (rawStatus === 'error') {
      return {
        message: message || `${layerLabel} layer failed.`,
        status: 'error',
      };
    }

    if (rawStatus === 'ready') {
      return {
        message: message || `${layerLabel} layer is marked ready, but no data URL is available.`,
        status: 'error',
      };
    }

    if (['degraded', 'fallback', 'polygon_only', 'unavailable'].includes(rawStatus)) {
      return {
        message: message || `${layerLabel} layer unavailable.`,
        status: rawStatus,
      };
    }

    if (rawStatus === 'waiting') {
      return {
        message: message || options.waitingMessage || `Waiting for ${layerLabel.toLowerCase()} layer`,
        status: 'loading',
      };
    }

    return {
      message: message || options.waitingMessage || `Waiting for ${layerLabel.toLowerCase()} layer`,
      status: 'queued',
    };
  }

  class WorkspaceViewer {
    constructor(rootEl) {
      this.rootEl = rootEl;
      this.destroyed = false;
      this.scene = new THREE.Scene();
      this.scene.background = new THREE.Color('#dcccbb');

      this.camera = new THREE.PerspectiveCamera(
        VIEWER_CAMERA_DEFAULTS.fov,
        1,
        VIEWER_CAMERA_DEFAULTS.near,
        VIEWER_CAMERA_DEFAULTS.far
      );
      this.camera.position.set(
        VIEWER_CAMERA_DEFAULTS.position.x,
        VIEWER_CAMERA_DEFAULTS.position.y,
        VIEWER_CAMERA_DEFAULTS.position.z
      );

      this.renderer = new THREE.WebGLRenderer({ antialias: true });
      this.renderer.localClippingEnabled = true;
      this.renderer.setPixelRatio(
        Math.min(window.devicePixelRatio || 1, VIEWER_CAMERA_DEFAULTS.maxDevicePixelRatio)
      );
      this.rootEl.replaceChildren(this.renderer.domElement);

      this.controls = VEILCamera.create(this.camera, this.renderer.domElement);

      this.ambientLight = new THREE.AmbientLight(0xffffff, 0.85);
      this.sunLight = new THREE.DirectionalLight(0xffffff, 1.15);
      this.sunLight.position.set(180, 240, 220);
      this.scene.add(this.ambientLight, this.sunLight);
      this.moonLight = new THREE.DirectionalLight(0xbfcfff, 0);
      this.moonLight.castShadow = false;
      this.hemiLight = new THREE.HemisphereLight(0xbfd7ff, 0x3a3428, 0);

      this.gridHelper = new THREE.GridHelper(
        GRID_HELPER_CONFIG.initialSizeMeters,
        GRID_HELPER_CONFIG.divisions,
        GRID_HELPER_CONFIG.majorColor,
        GRID_HELPER_CONFIG.minorColor
      );
      this.scene.add(this.gridHelper);
      this.overlayClipBounds = {
        minX: -GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
        minY: -GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
        maxX: GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
        maxY: GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
      };

      this.terrainMesh = null;
      this.terrainBaseMesh = null;
      this.terrainDrapeOverlayMesh = null;
      this.aoiBoundary = null;
      this.parcelGroup = null;
      this.scenePayload = null;
      this.terrainGrid = null;
      this.elevationMaterial = null;
      this.textureLoader = new THREE.TextureLoader();
      this.textureCache = new Map();
      this.textureLoadPromises = new Map();
      this.activeTerrainMode = 'elevation';
      this.layerVisibility = new Map();
      this.skyPass = null;
      this.skyBackgroundBeforePass = null;
      this.photometricMode = false;
      this.photometricLastState = null;
      this.photometricLastShadowSunDir = null;
      this.shadowMapDirty = false;
      this.photometricModeCallbacks = new Set();
      this.photometricSnapshot = {
        ambientColor: this.ambientLight.color.clone(),
        ambientIntensity: this.ambientLight.intensity,
        rendererShadowAutoUpdate: this.renderer.shadowMap.autoUpdate,
        rendererShadowEnabled: this.renderer.shadowMap.enabled,
        rendererShadowType: this.renderer.shadowMap.type,
        rendererToneMapping: this.renderer.toneMapping,
        rendererToneMappingExposure: this.renderer.toneMappingExposure,
        sunCastShadow: this.sunLight.castShadow,
        sunColor: this.sunLight.color.clone(),
        sunIntensity: this.sunLight.intensity,
        sunPosition: this.sunLight.position.clone(),
        sunVisible: this.sunLight.visible,
        sunTargetPosition: this.sunLight.target.position.clone(),
      };
      this.vegetationRenderer = VEILVegetation.create(this.scene, {
        onAssetLoad: () => this.invalidateShadowMap('vegetation-asset'),
      });
      this.overlayRenderer = VEILOverlays.create(this.scene);
      this.renderStats = {
        aoi: 0,
        buildings: 0,
        hydrology: 0,
        parcels: 0,
        roads: 0,
        shrubs: 0,
        soils: 0,
        trails: 0,
        trees: 0,
      };
      this.overlayData = {
        buildings: { type: 'FeatureCollection', features: [] },
        hydrology: { type: 'FeatureCollection', features: [] },
        roads: { type: 'FeatureCollection', features: [] },
        soils: { type: 'FeatureCollection', features: [] },
        trails: { type: 'FeatureCollection', features: [] },
      };
      this.vegetationData = {
        treeInstances: [],
        shrubPoints: [],
      };
      this.loadToken = 0;
      this.animationFrame = null;
      this.frameCallbacks = new Set();
      this.activeFetchControllers = new Set();
      this.keyboardPanState = VEILCamera.createKeyboardPanState();
      this.unbindKeyboardPan = VEILCamera.bindKeyboardPan(this.keyboardPanState);
      this.lastFrameTime = performance.now();

      this.animate = this.animate.bind(this);
      this.resize = this.resize.bind(this);
      window.addEventListener('resize', this.resize);
      this.resize();
      this.animate();
    }

    updateScenePayload(scenePayload) {
      if (this.destroyed) {
        return;
      }
      this.scenePayload = scenePayload;
    }

    syncDeferredLayerStates(callbacks = {}) {
      if (this.destroyed || !this.scenePayload) {
        return;
      }

      const vegetation = this.scenePayload.vegetation || {};
      const deferredLayerSpecs = [
        ['parcels', !this.scenePayload.parcels?.features_url, {
          layerBlock: this.scenePayload.parcels,
          layerLabel: 'Parcel',
          waitingMessage: 'Waiting for parcel layer',
        }],
        ['buildings', !this.scenePayload.buildings?.footprints_url, {
          layerBlock: this.scenePayload.buildings,
          layerLabel: 'Buildings',
        }],
        ['hydrology', !this.scenePayload.hydrology?.features_url, {
          layerBlock: this.scenePayload.hydrology,
          layerLabel: 'Hydrology',
        }],
        ['roads', !this.scenePayload.roads_trails?.roads_url, {
          layerBlock: this.scenePayload.roads_trails,
          layerLabel: 'Roads',
        }],
        ['soils', !this.scenePayload.soils?.features_url, {
          layerBlock: this.scenePayload.soils,
          layerLabel: 'Soils',
        }],
        ['trails', !this.scenePayload.roads_trails?.trails_url, {
          layerBlock: this.scenePayload.roads_trails,
          layerLabel: 'Trails',
        }],
        ['vegetation', !vegetation.tree_instances_url && !vegetation.shrub_points_url, {
          layerBlock: vegetation,
          layerLabel: 'Vegetation',
        }],
      ];

      deferredLayerSpecs.forEach(([layerId, shouldReport, options]) => {
        if (!shouldReport) {
          return;
        }

        const nextState = resolveDeferredLayerState(this.scenePayload, layerId, options);
        callbacks.onLayerState?.(layerId, nextState.status, { message: nextState.message });
      });
    }

    abortPendingFetches() {
      this.activeFetchControllers.forEach((controller) => controller.abort());
      this.activeFetchControllers.clear();
    }

    async fetchJson(url, options = {}) {
      if (this.destroyed) {
        throw createAbortError();
      }

      const startedAt = performance.now();
      const controller = new AbortController();
      const unlinkAbortSignal = linkAbortSignal(options.signal, controller);
      this.activeFetchControllers.add(controller);

      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) {
          debugLog('viewer-fetch-error', {
            duration_ms: Math.round(performance.now() - startedAt),
            status: response.status,
            url,
          });
          throw new Error(`Failed to load ${url}: ${response.status}`);
        }
        const payload = await response.json();
        debugLog('viewer-fetch-success', {
          duration_ms: Math.round(performance.now() - startedAt),
          status: response.status,
          url,
        });
        return payload;
      } catch (error) {
        if (isAbortError(error)) {
          debugLog('viewer-fetch-abort', {
            duration_ms: Math.round(performance.now() - startedAt),
            url,
          });
        }
        throw error;
      } finally {
        unlinkAbortSignal();
        this.activeFetchControllers.delete(controller);
      }
    }

    resetLoadedLayerState() {
      this.overlayData = {
        buildings: { type: 'FeatureCollection', features: [] },
        hydrology: { type: 'FeatureCollection', features: [] },
        roads: { type: 'FeatureCollection', features: [] },
        soils: { type: 'FeatureCollection', features: [] },
        trails: { type: 'FeatureCollection', features: [] },
      };
      this.vegetationData = {
        treeInstances: [],
        shrubPoints: [],
      };
    }

    disposeTextureCache() {
      this.textureLoadPromises.clear();
      this.textureCache.forEach((texture) => texture.dispose());
      this.textureCache.clear();
    }

    disposeTerrainDrapeOverlay() {
      if (!this.terrainDrapeOverlayMesh) {
        return;
      }

      this.scene.remove(this.terrainDrapeOverlayMesh);
      if (this.terrainDrapeOverlayMesh.material) {
        this.terrainDrapeOverlayMesh.material.dispose();
      }
      this.terrainDrapeOverlayMesh = null;
    }

    disposeActiveTerrainMaterial() {
      if (this.terrainMesh?.material && this.terrainMesh.material !== this.elevationMaterial) {
        this.terrainMesh.material.dispose();
      }
    }

    vectorFromDirection(value) {
      if (value?.isVector3) {
        return value.clone().normalize();
      }
      const vector = new THREE.Vector3(
        Number(value?.x),
        Number(value?.y),
        Number(value?.z)
      );
      return Number.isFinite(vector.x) && Number.isFinite(vector.y) && Number.isFinite(vector.z)
        ? vector.normalize()
        : null;
    }

    terrainLightingCenter() {
      const grid = this.terrainGrid;
      if (!grid) {
        return new THREE.Vector3(0, 0, 0);
      }
      return new THREE.Vector3(
        (Number(grid.minX) + Number(grid.maxX)) * 0.5,
        Math.max(0, (Number(grid.maxElevation) - Number(grid.minElevation)) * 0.5),
        -((Number(grid.minY) + Number(grid.maxY)) * 0.5)
      );
    }

    aimDirectionalLight(light, direction, center = this.terrainLightingCenter()) {
      if (!light || !direction) {
        return;
      }
      light.position.copy(center).add(
        direction.clone().multiplyScalar(PHOTOMETRIC_CONFIG.lightDistanceMeters)
      );
      light.target.position.copy(center);
      light.target.updateMatrixWorld();
    }

    configureSunShadowCamera() {
      if (!this.terrainGrid || !this.sunLight?.shadow) {
        return;
      }
      const grid = this.terrainGrid;
      const width = Math.max(1, Number(grid.maxX) - Number(grid.minX));
      const depth = Math.max(1, Number(grid.maxY) - Number(grid.minY));
      const elevRange = Math.max(1, Number(grid.maxElevation) - Number(grid.minElevation));
      const halfSpan = Math.max(width, depth) * PHOTOMETRIC_CONFIG.shadowCameraMargin * 0.5;
      const camera = this.sunLight.shadow.camera;
      camera.left = -halfSpan;
      camera.right = halfSpan;
      camera.top = halfSpan;
      camera.bottom = -halfSpan;
      camera.near = 1;
      camera.far = Math.max(3000, PHOTOMETRIC_CONFIG.lightDistanceMeters * 2 + elevRange + 400);
      camera.updateProjectionMatrix();

      const maxTextureSize = this.renderer.capabilities?.maxTextureSize || PHOTOMETRIC_CONFIG.shadowMapSize;
      const mapSize = maxTextureSize >= PHOTOMETRIC_CONFIG.shadowMapSize
        ? PHOTOMETRIC_CONFIG.shadowMapSize
        : Math.min(maxTextureSize, PHOTOMETRIC_CONFIG.shadowFallbackMapSize);
      this.sunLight.shadow.mapSize.set(mapSize, mapSize);
      this.sunLight.shadow.bias = -0.0004;
      this.sunLight.shadow.normalBias = 1.5;
    }

    setTerrainShadowFlags(on) {
      const enabled = Boolean(on);
      if (this.terrainMesh) {
        this.terrainMesh.castShadow = enabled;
        this.terrainMesh.receiveShadow = enabled;
      }
      if (this.terrainBaseMesh) {
        this.terrainBaseMesh.castShadow = false;
        this.terrainBaseMesh.receiveShadow = enabled;
      }
      if (this.terrainDrapeOverlayMesh) {
        this.terrainDrapeOverlayMesh.castShadow = false;
        this.terrainDrapeOverlayMesh.receiveShadow = enabled;
      }
    }

    createTerrainDrapeMaterial(texture) {
      const base = {
        depthWrite: false,
        map: texture,
        opacity: 0.85,
        polygonOffset: true,
        polygonOffsetFactor: -1,
        polygonOffsetUnits: -1,
        transparent: true,
      };
      if (this.photometricMode) {
        return new THREE.MeshStandardMaterial({
          ...base,
          metalness: 0,
          roughness: 1,
        });
      }
      return new THREE.MeshBasicMaterial(base);
    }

    syncTerrainDrapeMaterialForPhotometricMode() {
      if (!this.terrainDrapeOverlayMesh?.material?.map) {
        return;
      }
      const oldMaterial = this.terrainDrapeOverlayMesh.material;
      this.terrainDrapeOverlayMesh.material = this.createTerrainDrapeMaterial(oldMaterial.map);
      oldMaterial.dispose();
      this.setTerrainShadowFlags(this.photometricMode);
      this.invalidateShadowMap('terrain-drape-material');
    }

    applyBuildingShadows(on = this.photometricMode) {
      if (!this.buildingModelsGroup) {
        return;
      }
      const enabled = Boolean(on);
      this.buildingModelsGroup.traverse((child) => {
        if (child?.isMesh) {
          child.castShadow = enabled;
          child.receiveShadow = enabled;
        }
      });
    }

    invalidateShadowMap(reason = 'scene-change') {
      if (!this.photometricMode) {
        return;
      }
      this.shadowMapDirty = true;
      debugLog('viewer-shadow-invalidated', {
        reason,
        workspace_id: this.scenePayload?.workspace_id || null,
      });
    }

    onPhotometricModeChange(callback) {
      if (typeof callback !== 'function') {
        return () => {};
      }
      this.photometricModeCallbacks.add(callback);
      try {
        callback(this.photometricMode);
      } catch (err) {
        console.warn('photometric mode subscriber failed:', err);
      }
      return () => this.photometricModeCallbacks.delete(callback);
    }

    notifyPhotometricModeChange() {
      this.photometricModeCallbacks.forEach((callback) => {
        try {
          callback(this.photometricMode);
        } catch (err) {
          console.warn('photometric mode subscriber failed:', err);
        }
      });
    }

    setPhotometricMode(on) {
      if (this.destroyed) {
        return;
      }
      const enabled = Boolean(on);
      if (enabled === this.photometricMode) {
        if (enabled) {
          this.updatePhotometricSky(this.photometricLastState);
        }
        return;
      }

      this.photometricMode = enabled;
      if (enabled) {
        this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
        this.renderer.toneMappingExposure = this.photometricSnapshot.rendererToneMappingExposure;
        this.renderer.shadowMap.enabled = true;
        this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        this.renderer.shadowMap.autoUpdate = false;
        this.ambientLight.intensity = 0;
        this.sunLight.castShadow = true;
        if (!this.sunLight.target.parent) this.scene.add(this.sunLight.target);
        if (!this.moonLight.parent) this.scene.add(this.moonLight);
        if (!this.moonLight.target.parent) this.scene.add(this.moonLight.target);
        if (!this.hemiLight.parent) this.scene.add(this.hemiLight);
        this.configureSunShadowCamera();
        this.setTerrainShadowFlags(true);
        this.vegetationRenderer?.setShadows?.(true);
        this.applyBuildingShadows(true);
        this.syncTerrainDrapeMaterialForPhotometricMode();
        this.updatePhotometricSky(this.photometricLastState);
        this.invalidateShadowMap('photometric-on');
        this.notifyPhotometricModeChange();
        return;
      }

      this.renderer.toneMapping = this.photometricSnapshot.rendererToneMapping;
      this.renderer.toneMappingExposure = this.photometricSnapshot.rendererToneMappingExposure;
      this.renderer.shadowMap.enabled = this.photometricSnapshot.rendererShadowEnabled;
      this.renderer.shadowMap.type = this.photometricSnapshot.rendererShadowType;
      this.renderer.shadowMap.autoUpdate = this.photometricSnapshot.rendererShadowAutoUpdate;
      this.ambientLight.color.copy(this.photometricSnapshot.ambientColor);
      this.ambientLight.intensity = this.photometricSnapshot.ambientIntensity;
      this.sunLight.color.copy(this.photometricSnapshot.sunColor);
      this.sunLight.intensity = this.photometricSnapshot.sunIntensity;
      this.sunLight.position.copy(this.photometricSnapshot.sunPosition);
      this.sunLight.visible = this.photometricSnapshot.sunVisible;
      this.sunLight.castShadow = this.photometricSnapshot.sunCastShadow;
      this.sunLight.target.position.copy(this.photometricSnapshot.sunTargetPosition);
      this.sunLight.target.updateMatrixWorld();
      this.scene.remove(this.sunLight.target);
      this.scene.remove(this.moonLight);
      this.scene.remove(this.moonLight.target);
      this.scene.remove(this.hemiLight);
      this.moonLight.intensity = 0;
      this.hemiLight.intensity = 0;
      this.photometricLastShadowSunDir = null;
      this.shadowMapDirty = false;
      this.setTerrainShadowFlags(false);
      this.vegetationRenderer?.setShadows?.(false);
      this.applyBuildingShadows(false);
      this.syncTerrainDrapeMaterialForPhotometricMode();
      this.notifyPhotometricModeChange();
    }

    updatePhotometricSky(state) {
      this.photometricLastState = state || null;
      if (!this.photometricMode || !state?.sun) {
        return;
      }

      const sunDir = this.vectorFromDirection(state.sun.direction);
      const moonDir = this.vectorFromDirection(state.moon?.direction);
      if (!sunDir) {
        return;
      }
      const center = this.terrainLightingCenter();
      const sunAlt = Number(state.sun.altitudeDeg) || 0;
      const moonAlt = Number(state.moon?.altitudeDeg) || 0;
      const obscuration = clamp(state.solarObscuration, 0, 1);
      const sunUp = Math.pow(clamp(Math.sin(THREE.MathUtils.degToRad(sunAlt)), 0, 1), 0.6);
      const sunlightFactor = Math.max(0, 1 - obscuration);
      const sunIntensity = PHOTOMETRIC_CONFIG.sunIntensity * sunUp * sunlightFactor;
      this.aimDirectionalLight(this.sunLight, sunDir, center);
      this.sunLight.intensity = sunIntensity;
      this.sunLight.visible = sunIntensity > 0.0001;
      const sunriseColor = new THREE.Color(0xff9955).lerp(
        new THREE.Color(0xffd9b0),
        smoothstep(-4, 2, sunAlt)
      );
      this.sunLight.color.copy(sunriseColor.lerp(new THREE.Color(0xffffff), smoothstep(2, 16, sunAlt)));

      if (moonDir) {
        const moonUp = clamp(Math.sin(THREE.MathUtils.degToRad(moonAlt)), 0, 1);
        const phase = clamp(state.moon?.illuminatedFraction ?? 0, 0, 1);
        this.aimDirectionalLight(this.moonLight, moonDir, center);
        this.moonLight.color.set(0xbfcfff);
        this.moonLight.intensity = PHOTOMETRIC_CONFIG.moonIntensity * phase * moonUp;
      } else {
        this.moonLight.intensity = 0;
      }

      const eclipseDim = 1 - 0.82 * obscuration;
      const hemiT = smoothstep(-12, 10, sunAlt);
      this.hemiLight.color.set(0xbfd7ff).lerp(new THREE.Color(0xffe0bd), 1 - smoothstep(0, 12, sunAlt));
      this.hemiLight.groundColor.set(0x3a3428);
      this.hemiLight.intensity = THREE.MathUtils.lerp(0.02, 0.55, hemiT) * eclipseDim;

      const baseExposure = THREE.MathUtils.lerp(
        PHOTOMETRIC_CONFIG.nightExposure,
        PHOTOMETRIC_CONFIG.dayExposure,
        smoothstep(-12, 8, sunAlt)
      );
      const eclipseBoost = Math.min(3, 1 / (0.15 + 0.85 * Math.pow(1 - obscuration, 3)));
      this.renderer.toneMappingExposure = baseExposure * eclipseBoost;

      if (!this.photometricLastShadowSunDir) {
        this.photometricLastShadowSunDir = sunDir.clone();
        this.invalidateShadowMap('sun-initial');
      } else {
        const movedDeg = THREE.MathUtils.radToDeg(Math.acos(clamp(
          this.photometricLastShadowSunDir.dot(sunDir),
          -1,
          1
        )));
        if (movedDeg >= PHOTOMETRIC_CONFIG.shadowSunMoveThresholdDeg) {
          this.photometricLastShadowSunDir.copy(sunDir);
          this.invalidateShadowMap('sun-moved');
        }
      }
    }

    async loadTerrainTexture(textureUrl) {
      if (this.destroyed) {
        return null;
      }

      let texture = this.textureCache.get(textureUrl);
      if (!texture) {
        let pending = this.textureLoadPromises.get(textureUrl);
        if (!pending) {
          const loadPromise = this.textureLoader.loadAsync(textureUrl)
            .then((loadedTexture) => {
              if (this.destroyed) {
                loadedTexture.dispose?.();
                return null;
              }
              loadedTexture.colorSpace = THREE.SRGBColorSpace;
              this.textureCache.set(textureUrl, loadedTexture);
              return loadedTexture;
            })
            .finally(() => {
              if (this.textureLoadPromises.get(textureUrl) === loadPromise) {
                this.textureLoadPromises.delete(textureUrl);
              }
            });
          this.textureLoadPromises.set(textureUrl, loadPromise);
          pending = loadPromise;
        }
        texture = await pending;
      }
      return texture;
    }

    prefetchJson(url) {
      if (!url) {
        return Promise.resolve({ error: null, payload: null });
      }

      return this.fetchJson(url)
        .then((payload) => ({ error: null, payload }))
        .catch((error) => ({ error, payload: null }));
    }

    warmTerrainTextureCache(imagery = this.scenePayload?.imagery || {}) {
      const textureUrls = [imagery.hillshade_url, imagery.false_color_url, imagery.drape_url].filter(Boolean);
      if (!textureUrls.length || this.destroyed) {
        return;
      }

      Promise.allSettled(textureUrls.map((url) => this.loadTerrainTexture(url)))
        .then((results) => {
          if (this.destroyed) {
            return;
          }
          debugLog('viewer-terrain-textures-warmed', {
            attempted_count: textureUrls.length,
            failed_count: results.filter((result) => result.status === 'rejected').length,
            workspace_id: this.scenePayload?.workspace_id || null,
          });
        });
    }

    rerenderOverlayLayers() {
      this.overlayRenderer.load({
        buildings: this.overlayData.buildings,
        grid: this.terrainGrid,
        clipBounds: this.overlayClipBounds,
        hydrology: this.overlayData.hydrology,
        roads: this.overlayData.roads,
        soils: this.overlayData.soils,
        trails: this.overlayData.trails,
      });
      Object.assign(this.renderStats, this.overlayRenderer.getRenderStats());
    }

    rerenderOverlayLayer(layerId) {
      this.overlayRenderer.loadLayer(
        layerId,
        this.overlayData[layerId],
        this.terrainGrid,
        this.overlayClipBounds
      );
      Object.assign(this.renderStats, this.overlayRenderer.getRenderStats());
      debugLog('viewer-overlay-rerender', {
        layerId,
        rendered_count: this.renderStats[layerId] || 0,
        workspace_id: this.scenePayload?.workspace_id || null,
      });
    }

    rerenderVegetation() {
      this.vegetationRenderer.load({
        grid: this.terrainGrid,
        shrubPoints: this.vegetationData.shrubPoints,
        treeInstances: this.vegetationData.treeInstances,
      });
      this.vegetationRenderer.setShadows?.(this.photometricMode);
      this.invalidateShadowMap('vegetation-render');
      Object.assign(this.renderStats, this.vegetationRenderer.getRenderStats());
      debugLog('viewer-vegetation-rerender', {
        loaded_shrub_count: getVegetationItemCount(this.vegetationData.shrubPoints),
        loaded_tree_count: getVegetationItemCount(this.vegetationData.treeInstances),
        rendered_shrubs: this.renderStats.shrubs || 0,
        rendered_trees: this.renderStats.trees || 0,
        workspace_id: this.scenePayload?.workspace_id || null,
      });
    }

    async streamLoad(scenePayload, callbacks = {}) {
      if (this.destroyed) {
        return;
      }
      this.abortPendingFetches();
      this.clearScene();
      const loadToken = ++this.loadToken;
      const vegetation = scenePayload.vegetation || {};
      const reportSuccess = (layerId, detail = null) => {
        callbacks.onLayerState?.(layerId, 'ready', detail);
      };
      const reportError = (layerId, error) => {
        if (this.destroyed || loadToken !== this.loadToken || isAbortError(error)) {
          return;
        }
        callbacks.onLayerState?.(layerId, 'error', { message: error.message });
      };
      const reportLoading = (layerId, detail = null) => {
        callbacks.onLayerState?.(layerId, 'loading', detail);
      };

      this.resetLoadedLayerState();
      this.scenePayload = scenePayload;
      this.syncDeferredLayerStates(callbacks);
      debugLog('viewer-stream-load-start', {
        load_token: loadToken,
        workspace_id: scenePayload?.workspace_id || null,
      });
      const gridPromise = this.prefetchJson(scenePayload.terrain.grid_url);
      const aoiBoundaryPromise = this.prefetchJson(scenePayload.aoi_boundary?.geojson_url);
      const parcelsPromise = this.prefetchJson(scenePayload.parcels?.features_url);
      const overlayRequests = new Map(
        [
          ['buildings', scenePayload.buildings?.footprints_url],
          ['hydrology', scenePayload.hydrology?.features_url],
          ['roads', scenePayload.roads_trails?.roads_url],
          ['soils', scenePayload.soils?.features_url],
          ['trails', scenePayload.roads_trails?.trails_url],
        ]
          .filter(([, url]) => Boolean(url))
          .map(([layerId, url]) => [layerId, this.prefetchJson(url)])
      );
      const treeInstancesPromise =
        vegetation.tree_instances_url
          ? this.prefetchJson(vegetation.tree_instances_url)
          : Promise.resolve({ error: null, payload: [] });
      const shrubPointsPromise =
        vegetation.shrub_points_url
          ? this.prefetchJson(vegetation.shrub_points_url)
          : Promise.resolve({ error: null, payload: [] });
      reportLoading('terrain');
      const gridResult = await gridPromise;
      if (gridResult.error) {
        throw gridResult.error;
      }
      const grid = gridResult.payload;
      if (loadToken !== this.loadToken) {
        return;
      }
      this.terrainGrid = grid;
      const terrain = VEILTerrain.buildTerrainMesh(grid);
      this.terrainMesh = terrain.mesh;
      this.terrainBaseMesh = VEILTerrain.buildTerrainBaseMesh?.(grid) || null;
      this.elevationMaterial = terrain.mesh.material;
      if (this.terrainBaseMesh) {
        this.scene.add(this.terrainBaseMesh);
      }
      this.scene.add(this.terrainMesh);
      this.configureSunShadowCamera();
      this.setTerrainShadowFlags(this.photometricMode);
      this.invalidateShadowMap('terrain-ready');
      debugLog('viewer-terrain-ready', {
        grid_height: grid.height,
        grid_width: grid.width,
        max_x: grid.maxX,
        max_y: grid.maxY,
        min_x: grid.minX,
        min_y: grid.minY,
        workspace_id: scenePayload?.workspace_id || null,
      });
      this.updateGridHelper(grid);
      this.setTerrainRenderMode('elevation');
      this.setCameraFromGrid(grid);
      reportSuccess('terrain', { count: 1 });
      callbacks.onTerrainReady?.();
      this.warmTerrainTextureCache(scenePayload.imagery);

      reportLoading('aoi');
      aoiBoundaryPromise.then(({ error, payload: aoiBoundary }) => {
        if (error) {
          reportError('aoi', error);
          return;
        }
        if (loadToken !== this.loadToken) {
          return;
        }
        this.addAoiBoundary(aoiBoundary, this.terrainGrid);
        this.renderStats.aoi = aoiBoundary.features?.length ? 1 : 0;
        reportSuccess('aoi', { count: this.renderStats.aoi, rendered_count: this.renderStats.aoi });
      });

      if (!scenePayload.parcels.features_url) {
        const nextState = resolveDeferredLayerState(scenePayload, 'parcels', {
          layerBlock: scenePayload.parcels,
          layerLabel: 'Parcel',
          waitingMessage: 'Waiting for parcel layer',
        });
        callbacks.onLayerState?.('parcels', nextState.status, { message: nextState.message });
      } else {
        reportLoading('parcels');
        parcelsPromise.then(({ error, payload: parcelFeatures }) => {
          if (error) {
            reportError('parcels', error);
            return;
          }
          if (loadToken !== this.loadToken) {
            return;
          }
          this.addParcelOutlines(parcelFeatures, this.terrainGrid);
          reportSuccess('parcels', {
            count: parcelFeatures.features?.length || 0,
            rendered_count: this.renderStats.parcels,
          });
        });
      }

      [
        ['buildings', scenePayload.buildings?.footprints_url],
        ['hydrology', scenePayload.hydrology?.features_url],
        ['roads', scenePayload.roads_trails?.roads_url],
        ['soils', scenePayload.soils?.features_url],
        ['trails', scenePayload.roads_trails?.trails_url],
      ].forEach(([layerId, url]) => {
        if (!url) {
          const layerBlock =
            layerId === 'buildings'
              ? scenePayload.buildings
              : layerId === 'hydrology'
                ? scenePayload.hydrology
                : layerId === 'soils'
                  ? scenePayload.soils
                  : scenePayload.roads_trails;
          const nextState = resolveDeferredLayerState(scenePayload, layerId, {
            layerBlock,
            layerLabel: layerId.charAt(0).toUpperCase() + layerId.slice(1),
          });
          callbacks.onLayerState?.(layerId, nextState.status, { message: nextState.message });
          return;
        }
        reportLoading(layerId);
        overlayRequests.get(layerId)
          .then(({ error, payload: featureCollection }) => {
            if (error) {
              reportError(layerId, error);
              return;
            }
            if (loadToken !== this.loadToken) {
              return;
            }
            this.overlayData[layerId] = featureCollection;
            this.rerenderOverlayLayer(layerId);
            reportSuccess(layerId, {
              count: featureCollection.features?.length || 0,
              legend_entries: featureCollection.legend_entries || null,
              predominant_soil: featureCollection.predominant_soil || null,
              rendered_count: this.renderStats[layerId] || 0,
            });
          });
      });

      if (!vegetation.tree_instances_url && !vegetation.shrub_points_url) {
        const nextState = resolveDeferredLayerState(scenePayload, 'vegetation', {
          layerBlock: vegetation,
          layerLabel: 'Vegetation',
        });
        callbacks.onLayerState?.('vegetation', nextState.status, { message: nextState.message });
        debugLog('viewer-vegetation-waiting', {
          workspace_id: scenePayload?.workspace_id || null,
        });
        return;
      }
      reportLoading('vegetation');

      Promise.all([
        treeInstancesPromise,
        shrubPointsPromise,
      ])
        .then(([treeInstancesResult, shrubPointsResult]) => {
          if (treeInstancesResult.error) {
            throw treeInstancesResult.error;
          }
          if (shrubPointsResult.error) {
            throw shrubPointsResult.error;
          }
          if (loadToken !== this.loadToken) {
            return;
          }
          const treeInstances = treeInstancesResult.payload;
          const shrubPoints = shrubPointsResult.payload;
          this.vegetationData = {
            treeInstances,
            shrubPoints,
          };
          this.rerenderVegetation();
          reportSuccess('vegetation', {
            count: getVegetationItemCount(treeInstances) + getVegetationItemCount(shrubPoints),
            rendered_count: (this.renderStats.trees || 0) + (this.renderStats.shrubs || 0),
            trees: this.renderStats.trees || 0,
            shrubs: this.renderStats.shrubs || 0,
          });
          debugLog('viewer-vegetation-ready', {
            mode: 'full-payload',
            rendered_shrubs: this.renderStats.shrubs || 0,
            rendered_trees: this.renderStats.trees || 0,
            workspace_id: scenePayload?.workspace_id || null,
          });
        })
        .catch((error) => reportError('vegetation', error));
    }

    addAoiBoundary(featureCollection, grid) {
      if (!featureCollection.features.length) {
        return;
      }

      const feature = featureCollection.features[0];
      const polygon = feature.geometry.type === 'Polygon'
        ? feature.geometry.coordinates
        : feature.geometry.coordinates[0];

      if (!Array.isArray(polygon) || !polygon.length) {
        return;
      }

      const outerRing = polygon[0];
      const drapedRing = this.densifyRingForTerrain(outerRing, 4);
      const points = drapedRing.map(([x, y]) => {
        const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + 0.18;
        return new THREE.Vector3(x, terrainY, -y);
      });
      const lineGeometry = new THREE.BufferGeometry().setFromPoints(points);
      const lineMaterial = new THREE.LineBasicMaterial({
        color: 0xeab464,
        depthTest: false,
        opacity: 0.98,
        transparent: true,
        linewidth: 2,
      });
      this.aoiBoundary = new THREE.LineLoop(lineGeometry, lineMaterial);
      this.aoiBoundary.renderOrder = 12;
      this.aoiBoundary.frustumCulled = false;
      this.scene.add(this.aoiBoundary);
    }

    addParcelOutlines(featureCollection, grid) {
      if (!featureCollection.features.length) {
        return;
      }

      const group = new THREE.Group();
      let renderedCount = 0;

      featureCollection.features.forEach((feature) => {
        const coords =
          feature.geometry.type === 'Polygon'
            ? [feature.geometry.coordinates]
            : feature.geometry.coordinates;

        coords.forEach((polygon) => {
          const ring = this.densifyRingForTerrain(polygon[0], 6);
          const points = ring.map(([x, y]) => {
            const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + 0.12;
            return new THREE.Vector3(x, terrainY, -y);
          });
          const geometry = new THREE.BufferGeometry().setFromPoints(points);
          const material = new THREE.LineBasicMaterial({
            color: feature.properties?.in_aoi ? 0xeab464 : 0xfcfaf7,
            depthTest: false,
            opacity: feature.properties?.in_aoi ? 0.94 : 0.42,
            transparent: true,
          });
          group.add(new THREE.LineLoop(geometry, material));
          renderedCount += 1;
        });
      });

      this.parcelGroup = group;
      this.parcelGroup.visible = this.isLayerVisible('parcels');
      this.renderStats.parcels = renderedCount;
      this.scene.add(group);
    }

    densifyRingForTerrain(ring, maxSegmentLength) {
      if (!Array.isArray(ring) || ring.length < 2) {
        return Array.isArray(ring) ? ring : [];
      }

      const densified = [];

      for (let index = 0; index < ring.length - 1; index += 1) {
        const start = ring[index];
        const end = ring[index + 1];
        const dx = end[0] - start[0];
        const dy = end[1] - start[1];
        const distance = Math.hypot(dx, dy);
        const segments = Math.max(1, Math.ceil(distance / maxSegmentLength));

        for (let step = 0; step < segments; step += 1) {
          const t = step / segments;
          densified.push([start[0] + dx * t, start[1] + dy * t]);
        }
      }

      densified.push(ring[ring.length - 1]);
      return densified;
    }

    async setTerrainRenderMode(mode) {
      if (this.destroyed) {
        return false;
      }
      if (!this.terrainMesh || !this.scenePayload) {
        return false;
      }

      if (mode === 'elevation') {
        this.disposeTerrainDrapeOverlay();
        this.disposeActiveTerrainMaterial();
        this.terrainMesh.material = this.elevationMaterial;
        this.setTerrainShadowFlags(this.photometricMode);
        this.invalidateShadowMap('terrain-mode');
        this.activeTerrainMode = mode;
        return true;
      }

      const imagery = this.scenePayload.imagery || {};
      const urlMap = {
        false_color: imagery.false_color_url,
        hillshade: imagery.hillshade_url,
        ortho: imagery.drape_url,
      };
      const textureUrl = urlMap[mode];

      if (!textureUrl) {
        return false;
      }

      if (mode === 'ortho' && imagery.hillshade_url) {
        // Keep hillshade as the base terrain material and float the ortho drape above it.
        const [hillshadeTexture, orthoTexture] = await Promise.all([
          this.loadTerrainTexture(imagery.hillshade_url),
          this.loadTerrainTexture(imagery.drape_url),
        ]);
        if (this.destroyed || !hillshadeTexture || !orthoTexture) {
          return false;
        }

        this.disposeTerrainDrapeOverlay();
        this.disposeActiveTerrainMaterial();
        this.terrainMesh.material = new THREE.MeshStandardMaterial({
          map: hillshadeTexture,
          metalness: 0.06,
          roughness: 0.9,
        });

        const drapeMaterial = this.createTerrainDrapeMaterial(orthoTexture);
        this.terrainDrapeOverlayMesh = new THREE.Mesh(this.terrainMesh.geometry, drapeMaterial);
        this.terrainDrapeOverlayMesh.renderOrder = (this.terrainMesh.renderOrder || 0) + 1;
        this.terrainDrapeOverlayMesh.castShadow = false;
        this.terrainDrapeOverlayMesh.receiveShadow = this.photometricMode;
        this.scene.add(this.terrainDrapeOverlayMesh);
        this.setTerrainShadowFlags(this.photometricMode);
        this.invalidateShadowMap('terrain-mode');
        this.activeTerrainMode = mode;
        return true;
      }

      const texture = await this.loadTerrainTexture(textureUrl);
      if (this.destroyed || !texture) {
        return false;
      }

      this.disposeTerrainDrapeOverlay();
      this.disposeActiveTerrainMaterial();
      this.terrainMesh.material = new THREE.MeshStandardMaterial({
        map: texture,
        metalness: 0.06,
        roughness: 0.9,
      });
      this.setTerrainShadowFlags(this.photometricMode);
      this.invalidateShadowMap('terrain-mode');
      this.activeTerrainMode = mode;
      return true;
    }

    setCameraFromGrid(grid) {
      if (this.destroyed) {
        return;
      }
      VEILCamera.frameGrid(this.camera, this.controls, grid);
    }

    disposeGridHelper() {
      if (!this.gridHelper) {
        return;
      }

      this.scene.remove(this.gridHelper);
      this.gridHelper.geometry.dispose();
      if (Array.isArray(this.gridHelper.material)) {
        this.gridHelper.material.forEach((material) => material.dispose());
      } else {
        this.gridHelper.material.dispose();
      }
      this.gridHelper = null;
    }

    updateGridHelper(grid) {
      if (!this.gridHelper || !grid) {
        return;
      }

      const width = Math.max(1, grid.maxX - grid.minX);
      const height = Math.max(1, grid.maxY - grid.minY);
      const span = Math.max(width, height);
      const helperSize = Math.max(
        GRID_HELPER_CONFIG.minSizeMeters,
        Math.ceil((span * GRID_HELPER_CONFIG.sizePaddingMultiplier) / GRID_HELPER_CONFIG.sizeSnapIncrementMeters) *
          GRID_HELPER_CONFIG.sizeSnapIncrementMeters
      );
      const halfSize = helperSize / 2;
      const centerX = (grid.minX + grid.maxX) * 0.5;
      const centerY = (grid.minY + grid.maxY) * 0.5;

      this.disposeGridHelper();
      this.gridHelper = new THREE.GridHelper(
        helperSize,
        GRID_HELPER_CONFIG.divisions,
        GRID_HELPER_CONFIG.majorColor,
        GRID_HELPER_CONFIG.minorColor
      );
      this.gridHelper.position.set(centerX, 0, -centerY);
      this.scene.add(this.gridHelper);

      this.overlayClipBounds = {
        minX: centerX - halfSize,
        minY: centerY - halfSize,
        maxX: centerX + halfSize,
        maxY: centerY + halfSize,
      };
    }

    clearScene() {
      this.overlayRenderer?.clear?.();
      this.vegetationRenderer?.clear?.();
      this.disposeTerrainDrapeOverlay();

      if (this.terrainMesh) {
        this.scene.remove(this.terrainMesh);
        this.terrainMesh.geometry.dispose();
        this.disposeActiveTerrainMaterial();
        this.terrainMesh = null;
      }
      if (this.terrainBaseMesh) {
        this.scene.remove(this.terrainBaseMesh);
        this.terrainBaseMesh.geometry.dispose();
        this.terrainBaseMesh.material.dispose();
        this.terrainBaseMesh = null;
      }
      if (this.elevationMaterial) {
        this.elevationMaterial.dispose();
      }
      this.disposeTextureCache();

      if (this.aoiBoundary) {
        this.scene.remove(this.aoiBoundary);
        this.aoiBoundary.geometry.dispose();
        this.aoiBoundary.material.dispose();
        this.aoiBoundary = null;
      }

      if (this.parcelGroup) {
        this.scene.remove(this.parcelGroup);
        this.parcelGroup.traverse((child) => {
          if (child.geometry) {
            child.geometry.dispose();
          }
          if (child.material) {
            child.material.dispose();
          }
        });
        this.parcelGroup = null;
      }

      this.scenePayload = null;
      this.terrainGrid = null;
      this.elevationMaterial = null;
      this.activeTerrainMode = 'elevation';
      this.renderStats = {
        aoi: 0,
        buildings: 0,
        hydrology: 0,
        parcels: 0,
        roads: 0,
        shrubs: 0,
        soils: 0,
        trails: 0,
        trees: 0,
      };
      this.resetLoadedLayerState();
    }

    setVegetationDensity(kind, density) {
      if (this.destroyed) {
        return;
      }
      this.vegetationRenderer.setDensity(kind, density);
      this.invalidateShadowMap('vegetation-density');
    }

    isLayerVisible(layerId) {
      return this.layerVisibility.get(layerId) !== false;
    }

    setLayerVisibility(layerId, visible) {
      if (this.destroyed) {
        return;
      }
      this.layerVisibility.set(layerId, Boolean(visible));
      if (layerId === 'vegetation') {
        this.vegetationRenderer.setVisible(visible);
        return;
      }
      if (layerId === 'buildings' && this.buildingModelsGroup) {
        this.buildingModelsGroup.visible = visible;
      }
      if (layerId === 'parcels') {
        if (this.parcelGroup) {
          this.parcelGroup.visible = visible;
        }
        return;
      }
      this.overlayRenderer.setLayerVisible(layerId, visible);
    }

    resize() {
      if (this.destroyed) {
        return;
      }
      const width = Math.max(1, this.rootEl.clientWidth);
      const height = Math.max(1, this.rootEl.clientHeight);
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(width, height, false);
    }

    setSkyPass(passOrNull) {
      if (this.destroyed) {
        return;
      }
      const next = passOrNull || null;
      if (next && !this.skyPass) {
        this.skyBackgroundBeforePass = this.scene.background;
        this.scene.background = null;
      } else if (!next && this.skyPass) {
        this.scene.background = this.skyBackgroundBeforePass;
        this.skyBackgroundBeforePass = null;
      }
      this.skyPass = next;
    }

    renderFrame() {
      if (this.photometricMode && this.shadowMapDirty) {
        this.renderer.shadowMap.needsUpdate = true;
        this.shadowMapDirty = false;
      }
      if (this.skyPass) {
        const previousAutoClear = this.renderer.autoClear;
        this.renderer.autoClear = false;
        this.renderer.clear();
        this.skyPass.render(this.renderer, this.camera);
        this.renderer.render(this.scene, this.camera);
        this.renderer.autoClear = previousAutoClear;
        return;
      }
      this.renderer.render(this.scene, this.camera);
    }

    animate() {
      if (this.destroyed) {
        this.animationFrame = null;
        return;
      }
      this.animationFrame = window.requestAnimationFrame(this.animate);
      const now = performance.now();
      const deltaSeconds = Math.min(0.05, Math.max(0, (now - this.lastFrameTime) / 1000));
      this.lastFrameTime = now;
      // First-person explorer takes over the camera: drive movement directly and
      // skip the orbit controls / keyboard-pan (which would fight for the camera).
      if (this.povController?.active) {
        this.povController.update(deltaSeconds);
        this.overlayRenderer.tick(deltaSeconds);
        this.runFrameCallbacks(deltaSeconds);
        this.renderFrame();
        return;
      }
      VEILCamera.applyKeyboardPan(this.camera, this.controls, this.keyboardPanState, deltaSeconds);
      this.overlayRenderer.tick(deltaSeconds);
      this.runFrameCallbacks(deltaSeconds);
      this.controls.update();
      this.renderFrame();
    }

    onFrame(fn) {
      if (typeof fn !== 'function' || this.destroyed) {
        return () => {};
      }
      this.frameCallbacks.add(fn);
      return () => this.frameCallbacks.delete(fn);
    }

    runFrameCallbacks(deltaSeconds) {
      if (!this.frameCallbacks.size) {
        return;
      }
      Array.from(this.frameCallbacks).forEach((fn) => {
        try {
          fn(deltaSeconds);
        } catch (err) {
          this.frameCallbacks.delete(fn);
          console.error('viewer frame callback failed:', err);
        }
      });
    }

    destroy() {
      if (this.destroyed) {
        return;
      }

      this.destroyed = true;
      this.loadToken += 1;
      this.abortPendingFetches();
      if (this.animationFrame) {
        window.cancelAnimationFrame(this.animationFrame);
        this.animationFrame = null;
      }
      window.removeEventListener('resize', this.resize);
      this.frameCallbacks.clear();
      this.unbindKeyboardPan?.();
      this.unbindKeyboardPan = null;
      this.clearScene();
      this.overlayRenderer?.dispose?.();
      this.vegetationRenderer?.dispose?.();
      this.disposeGridHelper();
      this.controls?.dispose?.();
      this.renderer?.renderLists?.dispose?.();
      this.renderer?.dispose?.();
      this.renderer?.forceContextLoss?.();
      this.rootEl.replaceChildren();
    }

    getRenderStats() {
      return { ...this.renderStats };
    }
  }

  global.VEILViewer = {
    create(rootEl) {
      return new WorkspaceViewer(rootEl);
    },
  };
})(window);
