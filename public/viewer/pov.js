/* First-person "POV" explorer: drop onto any spot of the land and walk it.

   Activated from the Controls menu, this puts you at eye height on the terrain
   with WASD + mouse-look (pointer lock), gravity-clamped to the ground so you
   follow the surface, blocked by building footprints, and bounded by the DEM.
   Escape exits and restores the orbit camera exactly where it was.

   While in POV the renderer switches to a higher-quality, near-field look:
   a sky background + soft exponential fog (so only the visible neighbourhood is
   drawn richly), the surrounding-terrain apron and its vegetation are forced on
   so the world is filled, and — the headline upgrade — real animated water
   surfaces (streams, the pond, and ponded cells) rendered with a live ripple +
   fresnel + sun-specular shader instead of the flat draped blue of the top-down
   map.

   Integration: app.js calls VEILPOV.create(viewer, scene, opts) and sets
   window.__twin.pov; scene.js's animate loop calls pov.update(dt) and skips the
   orbit controls while pov.active. Nothing here touches the store or exports. */
(function attachPOV(global) {
  const { THREE, VEILTerrain } = global;
  if (!THREE || !VEILTerrain) {
    return;
  }

  const EYE_HEIGHT = 1.7;        // metres, camera height above the ground
  const WALK_SPEED = 6.0;        // m/s
  const RUN_SPEED = 13.5;        // m/s with Shift
  const ACCEL = 9.0;             // velocity smoothing (1/s)
  const HEIGHT_LERP = 14.0;      // how fast the eye settles onto the ground
  const LOOK_SENSITIVITY = 0.0022;
  const PITCH_LIMIT = Math.PI / 2 - 0.05;
  const POV_FOV = 70;
  const FOG_DENSITY = 0.0016;    // soft near-field falloff
  const SKY_COLOR = 0x9fc6e8;

  function astronomyPhotometricOn() {
    return Boolean(global.__twin?.astronomy?.photometricOn?.());
  }
  const BUILDING_PAD = 1.2;      // metres of clearance kept around footprints
  const FOLLOW_POSITION_RESPONSE = 4.5; // camera follow smoothing (1/s)
  const FOLLOW_YAW_RESPONSE = 5.5;      // heading pan smoothing (1/s)
  const FOLLOW_PITCH_RESPONSE = 4.0;    // tilt smoothing (1/s)

  // --- animated water shader ------------------------------------------------
  // Procedural ripple normal (two scrolling wave trains) feeding a fresnel mix
  // between a deep colour and the sky, plus a tight sun specular. cameraPosition
  // is a three-supplied uniform for ShaderMaterial, so we only drive time + sun.
  const WATER_VERT = `
    uniform float uTime;
    varying vec3 vWorld;
    void main() {
      vec3 p = position;
      p.y += sin(p.x * 0.55 + uTime * 1.5) * 0.045
           + cos(p.z * 0.47 - uTime * 1.2) * 0.045;
      vec4 wp = modelMatrix * vec4(p, 1.0);
      vWorld = wp.xyz;
      gl_Position = projectionMatrix * viewMatrix * wp;
    }
  `;
  const WATER_FRAG = `
    precision highp float;
    uniform float uTime;
    uniform vec3 uSunDir;
    uniform vec3 uDeep;
    uniform vec3 uShallow;
    uniform vec3 uSky;
    uniform float uOpacity;
    varying vec3 vWorld;

    vec3 waveNormal(vec2 p) {
      float t = uTime;
      vec2 d1 = vec2(0.80, 0.30);
      vec2 d2 = vec2(-0.42, 0.91);
      float gx = 0.5 * 0.35 * cos(dot(p, d1) * 0.35 + t * 1.6) * d1.x
               + 0.35 * 0.70 * cos(dot(p, d2) * 0.70 + t * 2.1) * d2.x;
      float gz = 0.5 * 0.35 * cos(dot(p, d1) * 0.35 + t * 1.6) * d1.y
               + 0.35 * 0.70 * cos(dot(p, d2) * 0.70 + t * 2.1) * d2.y;
      return normalize(vec3(-gx, 1.0, -gz));
    }

    void main() {
      vec3 N = waveNormal(vWorld.xz);
      vec3 V = normalize(cameraPosition - vWorld);
      vec3 L = normalize(uSunDir);
      float fres = pow(1.0 - max(dot(N, V), 0.0), 3.0);
      vec3 base = mix(uDeep, uShallow, clamp(N.y * 0.5 + 0.25, 0.0, 1.0));
      vec3 col = mix(base, uSky, clamp(fres, 0.0, 0.85));
      vec3 H = normalize(L + V);
      float spec = pow(max(dot(N, H), 0.0), 140.0);
      col += vec3(1.0) * spec * 0.9;
      gl_FragColor = vec4(col, uOpacity);
    }
  `;

  function makeWaterMaterial(sunDir) {
    return new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uSunDir: { value: sunDir.clone() },
        uDeep: { value: new THREE.Color(0x10384f) },
        uShallow: { value: new THREE.Color(0x2f6f86) },
        uSky: { value: new THREE.Color(0xbfe0f2) },
        uOpacity: { value: 0.86 },
      },
      vertexShader: WATER_VERT,
      fragmentShader: WATER_FRAG,
      transparent: true,
      depthWrite: true,
      side: THREE.DoubleSide,
    });
  }

  class POVExplorer {
    constructor(viewer, scene, opts = {}) {
      this.viewer = viewer;
      this.scene = viewer.scene; // the THREE scene we add water/fog to
      this.scenePayload = scene; // the scene.json (georef, names) — unused here
      this.opts = opts;
      this.grid = viewer.terrainGrid;
      this.apronGrid = opts.apronGrid || null;
      this.apron = opts.apron || null;                   // {mesh,...}
      this.surroundingVeg = opts.surroundingVeg || null; // {renderer}
      this.onStateChange = opts.onStateChange || (() => {});

      this.active = false;
      this.placing = false;
      this.followMode = null;
      this.locked = false;
      this.yaw = 0;
      this.pitch = 0;
      this.velocity = new THREE.Vector3();
      this.keys = Object.create(null);
      this.targetEyeY = 0;
      this.footprints = [];   // array of rings ([[x,y],...]) in scene-local metres
      this.water = null;      // THREE.Group of animated water meshes
      this.waterMaterials = [];
      this.saved = null;
      this.replayFollowTarget = null;

      this._raycaster = new THREE.Raycaster();
      this._ndc = new THREE.Vector2();
      this._tmpF = new THREE.Vector3();
      this._tmpR = new THREE.Vector3();

      this._onKeyDown = this._onKeyDown.bind(this);
      this._onKeyUp = this._onKeyUp.bind(this);
      this._onMouseMove = this._onMouseMove.bind(this);
      this._onPointerLockChange = this._onPointerLockChange.bind(this);
      this._onPlacementUp = this._onPlacementUp.bind(this);
      this._placementDown = null;

      this._buildOverlay();
      window.addEventListener('keydown', this._onKeyDown);
      window.addEventListener('keyup', this._onKeyUp);
      document.addEventListener('pointerlockchange', this._onPointerLockChange);

      // let the viewer's render loop drive our movement and skip orbit controls
      viewer.povController = this;
    }

    isBusy() {
      return this.active || this.placing;
    }

    /* ---------------- placement (drop-in) ---------------- */

    enterPlacement() {
      if (this.active) {
        this.exit();
        return;
      }
      if (this.placing) {
        return;
      }
      this.placing = true;
      this._showBanner('Click anywhere on the land to drop in', 'Esc to cancel');
      this.viewer.renderer.domElement.style.cursor = 'crosshair';
      const canvas = this.viewer.renderer.domElement;
      canvas.addEventListener('pointerdown', this._storePlacementDown);
      canvas.addEventListener('pointerup', this._onPlacementUp);
      this.onStateChange(this.statusLabel());
    }

    cancelPlacement() {
      if (!this.placing) {
        return;
      }
      this.placing = false;
      const canvas = this.viewer.renderer.domElement;
      canvas.style.cursor = '';
      canvas.removeEventListener('pointerdown', this._storePlacementDown);
      canvas.removeEventListener('pointerup', this._onPlacementUp);
      this._hideOverlay();
      this.onStateChange(this.statusLabel());
    }

    _storePlacementDown = (e) => {
      this._placementDown = { x: e.clientX, y: e.clientY };
    };

    _onPlacementUp(e) {
      const down = this._placementDown;
      this._placementDown = null;
      if (!down) {
        return;
      }
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) {
        return; // a drag/orbit, not a drop-in click
      }
      const canvas = this.viewer.renderer.domElement;
      const rect = canvas.getBoundingClientRect();
      this._ndc.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      this._ndc.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      this._raycaster.setFromCamera(this._ndc, this.viewer.camera);
      const targets = [this.viewer.terrainMesh];
      if (this.apron?.mesh) {
        targets.push(this.apron.mesh);
      }
      const hit = this._raycaster.intersectObjects(targets.filter(Boolean), false)[0];
      if (!hit) {
        return;
      }
      this.placing = false;
      canvas.style.cursor = '';
      canvas.removeEventListener('pointerdown', this._storePlacementDown);
      canvas.removeEventListener('pointerup', this._onPlacementUp);
      this.dropInAt(hit.point);
    }

    /* ---------------- enter / exit POV ---------------- */

    dropInAt(point) {
      const viewer = this.viewer;
      const camera = viewer.camera;
      const controls = viewer.controls;

      // remember everything we override so exit() is exact
      this.saved = {
        position: camera.position.clone(),
        quaternion: camera.quaternion.clone(),
        fov: camera.fov,
        near: camera.near,
        far: camera.far,
        target: controls.target.clone(),
        controlsEnabled: controls.enabled,
        background: this.scene.background,
        fog: this.scene.fog,
        gridHelperVisible: viewer.gridHelper ? viewer.gridHelper.visible : null,
        apronVisible: this.apron?.mesh ? this.apron.mesh.visible : null,
        surroundingVegVisible: this.surroundingVeg?.renderer?.group
          ? this.surroundingVeg.renderer.group.visible
          : null,
      };

      controls.enabled = false;

      // face the way the orbit camera was looking, so the transition is smooth
      const look = this._tmpF.copy(controls.target).sub(point);
      this.yaw = Math.atan2(look.x, -look.z);
      this.pitch = 0;

      // fill the world: apron ground + its vegetation make the walk continuous
      if (this.apron?.mesh) {
        this.apron.mesh.visible = true;
      }
      if (this.surroundingVeg?.renderer?.setVisible) {
        this.surroundingVeg.renderer.setVisible(true);
      }
      if (viewer.gridHelper) {
        viewer.gridHelper.visible = false;
      }

      // higher-quality near-field look
      if (!astronomyPhotometricOn()) {
        this.scene.background = new THREE.Color(SKY_COLOR);
        this.scene.fog = new THREE.FogExp2(SKY_COLOR, FOG_DENSITY);
      }
      camera.fov = POV_FOV;
      camera.near = 0.05;
      camera.updateProjectionMatrix();

      this._ensureWater();
      if (this.water) {
        this.water.visible = true;
      }

      const groundY = this._sampleGround(point.x, point.z);
      this.targetEyeY = groundY + EYE_HEIGHT;
      camera.position.set(point.x, this.targetEyeY, point.z);
      this.velocity.set(0, 0, 0);
      this._applyLook();

      this.active = true;
      this.followMode = null;
      this._showBanner('Walking the land', 'WASD move · mouse look · Shift run · Esc exit');
      setTimeout(() => this._fadeBanner(), 2600);
      this._requestLock();
      this.onStateChange(this.statusLabel());
    }

    enterReplayFollow(point, headingDeg = null) {
      if (this.active && this.followMode !== 'replay') {
        this.exit();
      }
      if (this.placing) {
        this.cancelPlacement();
      }
      if (!this.active) {
        this._enterCameraTakeover('Tracker POV', 'Replay camera follows the selected tracker · Esc exit');
        this.followMode = 'replay';
      }
      this.updateReplayPose(point, headingDeg);
      this.onStateChange(this.statusLabel());
    }

    updateReplayPose(point, headingDeg = null) {
      if (!point || this.followMode !== 'replay') {
        return;
      }
      const camera = this.viewer.camera;
      const groundY = this._sampleGround(point.x, point.z);
      const eyeY = groundY + EYE_HEIGHT;
      const yaw = Number.isFinite(headingDeg)
        ? -(((headingDeg % 360) * Math.PI) / 180)
        : this.replayFollowTarget?.yaw ?? this.yaw;
      if (!this.replayFollowTarget) {
        this.replayFollowTarget = {
          position: new THREE.Vector3(point.x, eyeY, point.z),
          yaw,
          pitch: 0,
        };
        this.targetEyeY = eyeY;
        camera.position.copy(this.replayFollowTarget.position);
        this.yaw = yaw;
        this.pitch = 0;
        this.velocity.set(0, 0, 0);
        this._applyLook();
        return;
      }
      this.replayFollowTarget.position.set(point.x, eyeY, point.z);
      this.replayFollowTarget.yaw = yaw;
      this.replayFollowTarget.pitch = 0;
      this.targetEyeY = eyeY;
      this.velocity.set(0, 0, 0);
    }

    exitReplayFollow() {
      if (this.followMode === 'replay') {
        this.exit();
      }
    }

    _enterCameraTakeover(title, hint) {
      const viewer = this.viewer;
      const camera = viewer.camera;
      const controls = viewer.controls;

      this.saved = {
        position: camera.position.clone(),
        quaternion: camera.quaternion.clone(),
        fov: camera.fov,
        near: camera.near,
        far: camera.far,
        target: controls.target.clone(),
        controlsEnabled: controls.enabled,
        background: this.scene.background,
        fog: this.scene.fog,
        gridHelperVisible: viewer.gridHelper ? viewer.gridHelper.visible : null,
        apronVisible: this.apron?.mesh ? this.apron.mesh.visible : null,
        surroundingVegVisible: this.surroundingVeg?.renderer?.group
          ? this.surroundingVeg.renderer.group.visible
          : null,
      };

      controls.enabled = false;
      if (this.apron?.mesh) {
        this.apron.mesh.visible = true;
      }
      if (this.surroundingVeg?.renderer?.setVisible) {
        this.surroundingVeg.renderer.setVisible(true);
      }
      if (viewer.gridHelper) {
        viewer.gridHelper.visible = false;
      }
      if (!astronomyPhotometricOn()) {
        this.scene.background = new THREE.Color(SKY_COLOR);
        this.scene.fog = new THREE.FogExp2(SKY_COLOR, FOG_DENSITY);
      }
      camera.fov = POV_FOV;
      camera.near = 0.05;
      camera.updateProjectionMatrix();

      this._ensureWater();
      if (this.water) {
        this.water.visible = true;
      }

      this.keys = Object.create(null);
      this.velocity.set(0, 0, 0);
      this.replayFollowTarget = null;
      this.active = true;
      this._showBanner(title, hint);
      setTimeout(() => this._fadeBanner(), 2600);
    }

    exit() {
      if (!this.active && !this.placing) {
        return;
      }
      if (this.placing) {
        this.cancelPlacement();
        return;
      }
      this.active = false;
      this.followMode = null;
      this.replayFollowTarget = null;
      if (this.locked && document.pointerLockElement) {
        document.exitPointerLock();
      }
      this.keys = Object.create(null);

      const s = this.saved;
      const viewer = this.viewer;
      const camera = viewer.camera;
      const controls = viewer.controls;
      if (s) {
        camera.position.copy(s.position);
        camera.quaternion.copy(s.quaternion);
        camera.fov = s.fov;
        camera.near = s.near;
        camera.far = s.far;
        camera.updateProjectionMatrix();
        controls.target.copy(s.target);
        controls.enabled = s.controlsEnabled;
        this.scene.background = s.background;
        this.scene.fog = s.fog;
        if (viewer.gridHelper && s.gridHelperVisible !== null) {
          viewer.gridHelper.visible = s.gridHelperVisible;
        }
        if (this.apron?.mesh && s.apronVisible !== null) {
          this.apron.mesh.visible = s.apronVisible;
        }
        if (this.surroundingVeg?.renderer?.setVisible && s.surroundingVegVisible !== null) {
          this.surroundingVeg.renderer.setVisible(s.surroundingVegVisible);
        }
        controls.update();
      }
      this.saved = null;
      if (this.water) {
        this.water.visible = false;
      }
      this.viewer.renderer.domElement.style.cursor = '';
      this._hideOverlay();
      this.onStateChange(this.statusLabel());
    }

    statusLabel() {
      if (this.followMode === 'replay') return 'Exit replay POV (Esc)';
      if (this.active) return 'Exit POV (Esc)';
      if (this.placing) return 'Click to drop in…';
      return 'Walk the land (POV)';
    }

    /* ---------------- per-frame update (driven by scene.js) ---------------- */

    update(dt) {
      if (!this.active) {
        return;
      }
      const step = Math.min(0.05, Math.max(0, dt));
      if (this.followMode === 'replay') {
        this._updateReplayFollow(step);
        this._tickWater(step);
        return;
      }
      const camera = this.viewer.camera;

      // movement basis from the camera orientation, flattened to the ground plane
      this._tmpF.set(0, 0, -1).applyQuaternion(camera.quaternion);
      this._tmpF.y = 0;
      if (this._tmpF.lengthSq() < 1e-6) this._tmpF.set(0, 0, -1);
      this._tmpF.normalize();
      this._tmpR.set(1, 0, 0).applyQuaternion(camera.quaternion);
      this._tmpR.y = 0;
      if (this._tmpR.lengthSq() < 1e-6) this._tmpR.set(1, 0, 0);
      this._tmpR.normalize();

      let wishX = 0;
      let wishZ = 0;
      if (this.keys.forward) { wishX += this._tmpF.x; wishZ += this._tmpF.z; }
      if (this.keys.back) { wishX -= this._tmpF.x; wishZ -= this._tmpF.z; }
      if (this.keys.right) { wishX += this._tmpR.x; wishZ += this._tmpR.z; }
      if (this.keys.left) { wishX -= this._tmpR.x; wishZ -= this._tmpR.z; }
      const wishLen = Math.hypot(wishX, wishZ);
      const speed = this.keys.run ? RUN_SPEED : WALK_SPEED;
      let targetVX = 0;
      let targetVZ = 0;
      if (wishLen > 1e-6) {
        targetVX = (wishX / wishLen) * speed;
        targetVZ = (wishZ / wishLen) * speed;
      }
      const blend = 1 - Math.exp(-ACCEL * step);
      this.velocity.x += (targetVX - this.velocity.x) * blend;
      this.velocity.z += (targetVZ - this.velocity.z) * blend;

      const curX = camera.position.x;
      const curZ = camera.position.z;
      let nextX = curX + this.velocity.x * step;
      let nextZ = curZ + this.velocity.z * step;

      // resolve collisions per-axis so we slide along walls/edges
      if (!this._canStand(nextX, curZ)) { nextX = curX; this.velocity.x = 0; }
      if (!this._canStand(nextX, nextZ)) { nextZ = curZ; this.velocity.z = 0; }

      const groundY = this._sampleGround(nextX, nextZ);
      this.targetEyeY = groundY + EYE_HEIGHT;
      const yBlend = 1 - Math.exp(-HEIGHT_LERP * step);
      const eyeY = camera.position.y + (this.targetEyeY - camera.position.y) * yBlend;
      camera.position.set(nextX, eyeY, nextZ);

      this._tickWater(step);
    }

    _tickWater(step) {
      if (this.waterMaterials.length) {
        for (const m of this.waterMaterials) {
          m.uniforms.uTime.value += step;
        }
      }
    }

    _updateReplayFollow(step) {
      const target = this.replayFollowTarget;
      if (!target) {
        return;
      }
      const camera = this.viewer.camera;
      const posBlend = 1 - Math.exp(-FOLLOW_POSITION_RESPONSE * step);
      const yawBlend = 1 - Math.exp(-FOLLOW_YAW_RESPONSE * step);
      const pitchBlend = 1 - Math.exp(-FOLLOW_PITCH_RESPONSE * step);
      camera.position.lerp(target.position, posBlend);
      this.targetEyeY = target.position.y;
      this.yaw += shortestAngle(this.yaw, target.yaw) * yawBlend;
      this.pitch += (target.pitch - this.pitch) * pitchBlend;
      this._applyLook();
    }

    /* ---------------- ground sampling + collision ---------------- */

    // World y of the terrain surface at scene-x / scene-z (local y = -z).
    _sampleGround(x, z) {
      const localY = -z;
      if (VEILTerrain.hasValidTerrainAtLocal(this.grid, x, localY)) {
        return VEILTerrain.sampleTerrainHeightAtLocal(this.grid, x, localY);
      }
      if (this.apronGrid && VEILTerrain.hasValidTerrainAtLocal(this.apronGrid, x, localY)) {
        return VEILTerrain.sampleTerrainHeightAtLocal(this.apronGrid, x, localY) +
          (this.apronGrid.minElevation - this.grid.minElevation);
      }
      // fall back to the last known eye height minus eye, so we never plunge
      return this.targetEyeY - EYE_HEIGHT;
    }

    // Can the walker occupy (x, z)? Must be over real terrain and clear of
    // building footprints.
    _canStand(x, z) {
      const localY = -z;
      const onGrid = VEILTerrain.hasValidTerrainAtLocal(this.grid, x, localY) ||
        (this.apronGrid && VEILTerrain.hasValidTerrainAtLocal(this.apronGrid, x, localY));
      if (!onGrid) {
        return false;
      }
      for (const ring of this.footprints) {
        if (pointInRing(ring, x, z, BUILDING_PAD)) {
          return false;
        }
      }
      return true;
    }

    setFootprints(featureCollection) {
      const rings = [];
      for (const f of featureCollection?.features || []) {
        const g = f.geometry;
        if (!g) continue;
        const polys = g.type === 'Polygon' ? [g.coordinates]
          : g.type === 'MultiPolygon' ? g.coordinates : [];
        for (const poly of polys) {
          // outer ring only; map scene-local [x, y] -> [x, z] (z = -y)
          const outer = poly[0];
          if (Array.isArray(outer) && outer.length >= 4) {
            rings.push(outer.map(([px, py]) => [px, -py]));
          }
        }
      }
      this.footprints = rings;
    }

    /* ---------------- water surfaces ---------------- */

    setWaterSources(sources) {
      // { features: hydrology GeoJSON, ponding: grid }
      this._waterSources = sources;
      if (this.water) {
        // rebuild next time we enter
        this._disposeWater();
      }
    }

    _ensureWater() {
      if (this.water || !this._waterSources) {
        return;
      }
      const group = new THREE.Group();
      group.renderOrder = 5;
      const sunDir = this.viewer.sunLight
        ? this.viewer.sunLight.position.clone().normalize()
        : new THREE.Vector3(0.5, 0.8, 0.4).normalize();

      const features = this._waterSources.features?.features || [];
      for (const f of features) {
        const g = f.geometry;
        if (!g) continue;
        if (g.type === 'Polygon' || g.type === 'MultiPolygon') {
          const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
          polys.forEach((poly) => this._addWaterPolygon(group, poly[0], sunDir));
        } else if (g.type === 'LineString') {
          this._addWaterRibbon(group, g.coordinates, sunDir);
        } else if (g.type === 'MultiLineString') {
          g.coordinates.forEach((line) => this._addWaterRibbon(group, line, sunDir));
        }
      }
      this._addPonding(group, this._waterSources.ponding, sunDir);

      this.water = group;
      this.water.visible = false;
      this.scene.add(group);
    }

    _registerWaterMesh(group, geometry, sunDir) {
      const material = makeWaterMaterial(sunDir);
      this.waterMaterials.push(material);
      const mesh = new THREE.Mesh(geometry, material);
      mesh.frustumCulled = true; // only shade water that's actually in view
      group.add(mesh);
    }

    // Flat pool at the lowest terrain height along the ring (water finds the floor).
    _addWaterPolygon(group, ring, sunDir) {
      if (!Array.isArray(ring) || ring.length < 4) {
        return;
      }
      const contour = ring.map(([x, y]) => new THREE.Vector2(x, y));
      let faces;
      try {
        faces = THREE.ShapeUtils.triangulateShape(contour, []);
      } catch (_e) {
        return;
      }
      if (!faces.length) {
        return;
      }
      let level = Infinity;
      ring.forEach(([x, y]) => {
        level = Math.min(level, VEILTerrain.sampleTerrainHeightAtLocal(this.grid, x, y));
      });
      level += 0.25;
      const positions = new Float32Array(contour.length * 3);
      contour.forEach((p, i) => {
        positions[i * 3] = p.x;
        positions[i * 3 + 1] = level;
        positions[i * 3 + 2] = -p.y;
      });
      const indices = [];
      faces.forEach(([a, b, c]) => indices.push(a, b, c));
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geometry.setIndex(indices);
      this._registerWaterMesh(group, geometry, sunDir);
    }

    // A terrain-hugging ribbon along a stream centreline.
    _addWaterRibbon(group, line, sunDir, width = 2.4) {
      if (!Array.isArray(line) || line.length < 2) {
        return;
      }
      const half = width / 2;
      const positions = [];
      const indices = [];
      for (let i = 0; i < line.length; i += 1) {
        const [x, y] = line[i];
        const prev = line[Math.max(0, i - 1)];
        const next = line[Math.min(line.length - 1, i + 1)];
        let dx = next[0] - prev[0];
        let dy = next[1] - prev[1];
        const len = Math.hypot(dx, dy) || 1;
        dx /= len; dy /= len;
        const nx = -dy; // perpendicular in scene-local XY
        const ny = dx;
        const h = VEILTerrain.sampleTerrainHeightAtLocal(this.grid, x, y) + 0.12;
        const lx = x + nx * half;
        const ly = y + ny * half;
        const rx = x - nx * half;
        const ry = y - ny * half;
        positions.push(lx, h, -ly, rx, h, -ry);
        if (i > 0) {
          const a = (i - 1) * 2;
          indices.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
        }
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3));
      geometry.setIndex(indices);
      this._registerWaterMesh(group, geometry, sunDir);
    }

    // Merged quads for ponded cells (one geometry) from the ponding depth grid.
    _addPonding(group, grid, sunDir, threshold = 0.12) {
      if (!grid || !grid.values || !grid.bounds_local) {
        return;
      }
      const [minx, miny, maxx, maxy] = grid.bounds_local;
      const cw = (maxx - minx) / grid.width;
      const ch = (maxy - miny) / grid.height;
      const positions = [];
      const indices = [];
      let n = 0;
      for (let r = 0; r < grid.height; r += 1) {
        const row = grid.values[r] || [];
        for (let c = 0; c < grid.width; c += 1) {
          const v = row[c];
          if (v == null || v < threshold) {
            continue;
          }
          const x0 = minx + c * cw;
          const x1 = x0 + cw;
          const y1 = maxy - r * ch;     // top edge (north)
          const y0 = y1 - ch;
          const cx = (x0 + x1) / 2;
          const cy = (y0 + y1) / 2;
          const h = VEILTerrain.sampleTerrainHeightAtLocal(this.grid, cx, cy) + 0.05;
          // four corners: (x0,y0) (x1,y0) (x0,y1) (x1,y1) -> world (x, h, -y)
          positions.push(x0, h, -y0, x1, h, -y0, x0, h, -y1, x1, h, -y1);
          const b = n * 4;
          indices.push(b, b + 1, b + 2, b + 1, b + 3, b + 2);
          n += 1;
        }
      }
      if (!n) {
        return;
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3));
      geometry.setIndex(indices);
      this._registerWaterMesh(group, geometry, sunDir);
    }

    _disposeWater() {
      if (!this.water) {
        return;
      }
      this.scene.remove(this.water);
      this.water.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) o.material.dispose();
      });
      this.water = null;
      this.waterMaterials = [];
    }

    /* ---------------- input + pointer lock ---------------- */

    _requestLock() {
      try {
        this.viewer.renderer.domElement.requestPointerLock?.();
      } catch (_e) { /* not supported / blocked — keyboard still works */ }
    }

    _onPointerLockChange() {
      this.locked = document.pointerLockElement === this.viewer.renderer.domElement;
      if (this.locked) {
        window.addEventListener('mousemove', this._onMouseMove);
      } else {
        window.removeEventListener('mousemove', this._onMouseMove);
        // user released the lock (e.g. via Esc) — leave POV
        if (this.active) {
          this.exit();
        }
      }
    }

    _onMouseMove(e) {
      if (!this.active || !this.locked) {
        return;
      }
      this.yaw -= e.movementX * LOOK_SENSITIVITY;
      this.pitch -= e.movementY * LOOK_SENSITIVITY;
      this.pitch = Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT, this.pitch));
      this._applyLook();
    }

    _applyLook() {
      this.viewer.camera.rotation.set(this.pitch, this.yaw, 0, 'YXZ');
    }

    _onKeyDown(e) {
      if (e.key === 'Escape') {
        if (this.isBusy()) {
          this.exit();
        }
        return;
      }
      if (!this.active) {
        return;
      }
      if (this._mapKey(e.code, true)) {
        e.preventDefault();
      }
    }

    _onKeyUp(e) {
      if (!this.active) {
        return;
      }
      if (this._mapKey(e.code, false)) {
        e.preventDefault();
      }
    }

    _mapKey(code, down) {
      switch (code) {
        case 'KeyW': case 'ArrowUp': this.keys.forward = down; return true;
        case 'KeyS': case 'ArrowDown': this.keys.back = down; return true;
        case 'KeyA': case 'ArrowLeft': this.keys.left = down; return true;
        case 'KeyD': case 'ArrowRight': this.keys.right = down; return true;
        case 'ShiftLeft': case 'ShiftRight': this.keys.run = down; return true;
        default: return false;
      }
    }

    /* ---------------- overlay (crosshair + banner) ---------------- */

    _buildOverlay() {
      const root = document.createElement('div');
      root.id = 'pov-overlay';
      root.hidden = true;
      root.innerHTML =
        '<div id="pov-crosshair"></div>' +
        '<div id="pov-banner"><strong id="pov-banner-title"></strong>' +
        '<span id="pov-banner-hint"></span></div>';
      document.body.appendChild(root);
      this.overlayEl = root;
      this.bannerEl = root.querySelector('#pov-banner');
      this.bannerTitleEl = root.querySelector('#pov-banner-title');
      this.bannerHintEl = root.querySelector('#pov-banner-hint');
      this.crosshairEl = root.querySelector('#pov-crosshair');
    }

    _showBanner(title, hint) {
      this.overlayEl.hidden = false;
      this.bannerEl.classList.remove('fade');
      this.crosshairEl.style.display = this.active ? 'block' : 'none';
      this.bannerTitleEl.textContent = title;
      this.bannerHintEl.textContent = hint || '';
    }

    _fadeBanner() {
      if (this.active) {
        this.bannerEl.classList.add('fade');
      }
    }

    _hideOverlay() {
      this.overlayEl.hidden = true;
      this.bannerEl.classList.remove('fade');
    }

    destroy() {
      this.exit();
      window.removeEventListener('keydown', this._onKeyDown);
      window.removeEventListener('keyup', this._onKeyUp);
      window.removeEventListener('mousemove', this._onMouseMove);
      document.removeEventListener('pointerlockchange', this._onPointerLockChange);
      this._disposeWater();
      this.overlayEl?.remove();
      if (this.viewer.povController === this) {
        this.viewer.povController = null;
      }
    }
  }

  // point-in-polygon with an outward pad (treats the ring as slightly inflated
  // by testing the raw point; pad gives a small standoff via a 4-point probe)
  function pointInRing(ring, x, z, pad) {
    if (rawPointInRing(ring, x, z)) return true;
    if (pad > 0) {
      return rawPointInRing(ring, x + pad, z) || rawPointInRing(ring, x - pad, z) ||
        rawPointInRing(ring, x, z + pad) || rawPointInRing(ring, x, z - pad);
    }
    return false;
  }

  function rawPointInRing(ring, x, z) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      const xi = ring[i][0];
      const zi = ring[i][1];
      const xj = ring[j][0];
      const zj = ring[j][1];
      if ((zi > z) !== (zj > z) && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  function shortestAngle(from, to) {
    return Math.atan2(Math.sin(to - from), Math.cos(to - from));
  }

  global.VEILPOV = {
    create(viewer, scene, opts) {
      return new POVExplorer(viewer, scene, opts);
    },
  };
})(window);
