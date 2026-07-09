(function attachSky(global) {
  'use strict';

  const { THREE, Astronomy } = global;
  const R_DOME = 100;
  const SUN_RADIUS_AU = 695700 / (Astronomy?.KM_PER_AU || 149597870.7);
  const STAR_FADE_DARK_ALT = -12;
  const STAR_FADE_DAY_ALT = 0;
  const PLANETS = ['Mercury', 'Venus', 'Mars', 'Jupiter', 'Saturn', 'Uranus', 'Neptune'];
  const BODY_NAMES = ['Sun', 'Moon', ...PLANETS];
  const BODY_NAME_BY_LOWER = new Map(BODY_NAMES.map((name) => [name.toLowerCase(), name]));
  const MOON_RADIUS_AU = 1737.4 / (Astronomy?.KM_PER_AU || 149597870.7);
  const PLANET_COLORS = {
    Mercury: 0xb8b0a5,
    Venus: 0xfff0c8,
    Mars: 0xff8866,
    Jupiter: 0xffe8c8,
    Saturn: 0xf5d8a8,
    Uranus: 0x9fd8ff,
    Neptune: 0x6f8cff,
  };

  function clamp(n, min, max) {
    return Math.max(min, Math.min(max, n));
  }

  function smoothstep(edge0, edge1, x) {
    const t = clamp((x - edge0) / (edge1 - edge0), 0, 1);
    return t * t * (3 - 2 * t);
  }

  function lensCoverageFraction(radiusA, radiusB, separation) {
    const rA = Math.max(0, Number(radiusA) || 0);
    const rB = Math.max(0, Number(radiusB) || 0);
    const d = Math.max(0, Number(separation) || 0);
    if (!rA || !rB || d >= rA + rB) return 0;
    if (d <= Math.abs(rA - rB)) {
      return rB >= rA ? 1 : clamp((rB * rB) / (rA * rA), 0, 1);
    }
    const a = rA * rA * Math.acos(clamp((d * d + rA * rA - rB * rB) / (2 * d * rA), -1, 1));
    const b = rB * rB * Math.acos(clamp((d * d + rB * rB - rA * rA) / (2 * d * rB), -1, 1));
    const c = 0.5 * Math.sqrt(Math.max(0, (-d + rA + rB) * (d + rA - rB) * (d - rA + rB) * (d + rA + rB)));
    return clamp((a + b - c) / (Math.PI * rA * rA), 0, 1);
  }

  function esc(text) {
    return String(text ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function timeFromMs(ms) {
    return Astronomy.MakeTime(new Date(ms));
  }

  function sceneDirectionFromAltAzInto(altDeg, azDeg, target) {
    const h = THREE.MathUtils.degToRad(altDeg);
    const a = THREE.MathUtils.degToRad(azDeg);
    return target.set(
      Math.cos(h) * Math.sin(a),
      Math.sin(h),
      -Math.cos(h) * Math.cos(a)
    ).normalize();
  }

  function sceneDirectionFromAltAz(altDeg, azDeg) {
    return sceneDirectionFromAltAzInto(altDeg, azDeg, new THREE.Vector3());
  }

  function eqjVector(raDeg, decDeg) {
    const ra = THREE.MathUtils.degToRad(raDeg);
    const dec = THREE.MathUtils.degToRad(decDeg);
    return new THREE.Vector3(
      Math.cos(dec) * Math.cos(ra),
      Math.cos(dec) * Math.sin(ra),
      Math.sin(dec)
    );
  }

  function matrixEqjToScene(time, observer, target = new THREE.Matrix4()) {
    const r = Astronomy.Rotation_EQJ_HOR(time, observer).rot;
    return target.set(
      -r[0][1], -r[1][1], -r[2][1], 0,
       r[0][2],  r[1][2],  r[2][2], 0,
      -r[0][0], -r[1][0], -r[2][0], 0,
       0,        0,        0,       1
    );
  }

  function colorFromBv(bv) {
    const t = clamp((Number(bv) + 0.3) / 2.3, 0, 1);
    const stops = [
      [0.00, [0.65, 0.75, 1.00]],
      [0.18, [0.78, 0.84, 1.00]],
      [0.35, [0.93, 0.95, 1.00]],
      [0.50, [1.00, 1.00, 0.94]],
      [0.68, [1.00, 0.90, 0.70]],
      [0.82, [1.00, 0.74, 0.45]],
      [1.00, [1.00, 0.52, 0.32]],
    ];
    for (let i = 1; i < stops.length; i += 1) {
      if (t <= stops[i][0]) {
        const [t0, c0] = stops[i - 1];
        const [t1, c1] = stops[i];
        const u = (t - t0) / (t1 - t0);
        return [
          c0[0] + (c1[0] - c0[0]) * u,
          c0[1] + (c1[1] - c0[1]) * u,
          c0[2] + (c1[2] - c0[2]) * u,
        ];
      }
    }
    return stops[stops.length - 1][1];
  }

  function makeStarMaterial() {
    return new THREE.ShaderMaterial({
      uniforms: {
        uFade: { value: 0 },
        uPixelRatio: { value: Math.min(global.devicePixelRatio || 1, 2) },
      },
      vertexShader: `
        attribute float aMag;
        attribute vec3 aColor;
        varying vec3 vColor;
        varying float vAlpha;
        uniform float uFade;
        uniform float uPixelRatio;
        void main() {
          vColor = aColor;
          float bright = clamp((6.6 - aMag) / 6.6, 0.15, 1.0);
          vAlpha = uFade * bright;
          vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
          gl_Position = projectionMatrix * mvPosition;
          gl_PointSize = clamp(7.5 - 1.1 * aMag, 1.0, 9.0) * uPixelRatio;
        }
      `,
      fragmentShader: `
        varying vec3 vColor;
        varying float vAlpha;
        void main() {
          vec2 d = gl_PointCoord - vec2(0.5);
          float r = dot(d, d);
          if (r > 0.25) discard;
          float soft = smoothstep(0.25, 0.02, r);
          gl_FragColor = vec4(vColor, vAlpha * soft);
        }
      `,
      blending: THREE.AdditiveBlending,
      depthTest: false,
      depthWrite: false,
      transparent: true,
      toneMapped: false,
    });
  }

  function radialTexture(inner, outer, size = 128) {
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    g.addColorStop(0, inner);
    g.addColorStop(1, outer);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  function labelTexture(text) {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    ctx.font = '600 24px system-ui, -apple-system, Segoe UI, sans-serif';
    const width = Math.ceil(ctx.measureText(text).width + 28);
    canvas.width = Math.max(64, width);
    canvas.height = 44;
    ctx.font = '600 24px system-ui, -apple-system, Segoe UI, sans-serif';
    ctx.fillStyle = 'rgba(8, 12, 18, 0.74)';
    ctx.strokeStyle = 'rgba(255, 140, 26, 0.88)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect(1, 1, canvas.width - 2, canvas.height - 2, 8);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = '#fff3df';
    ctx.fillText(text, 14, 29);
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  function billboard(mesh, dir, radius) {
    mesh.position.copy(dir).multiplyScalar(R_DOME);
    mesh.scale.setScalar(radius);
    mesh.lookAt(0, 0, 0);
  }

  function bodyApiName(name) {
    return Astronomy.Body[name];
  }

  function create(api) {
    if (!THREE || !Astronomy || !api?.viewer || !api?.site) return null;

    const site = api.site;
    const observer = new Astronomy.Observer(site.lat, site.lon, site.heightM || site.height_m || 0);
    const skyScene = new THREE.Scene();
    const skyCamera = new THREE.PerspectiveCamera();
    const raycaster = new THREE.Raycaster();
    const current = {
      timeMs: 0,
      starMatrix: new THREE.Matrix4(),
      sun: null,
      moon: null,
      bodies: new Map(),
      starFade: 0,
      sunAngularRadiusDeg: 0.2665,
      moonAngularRadiusDeg: 0.26,
      moonPhaseAngle: 0,
      moonIlluminatedFraction: 0,
      moonLibration: null,
      solarObscuration: 0,
      lunarEclipseTint: 0,
      physicalSky: false,
      lastPlanetMs: null,
      lastStarMatrixMs: null,
      lastMoonAppearanceMs: null,
    };
    const layers = {
      sun: true,
      moon: true,
      planets: true,
      stars: true,
      constellations: true,
    };
    const assets = {
      stars: [],
      starByHip: new Map(),
      starByName: new Map(),
      constellations: null,
      constellationSegments: new Map(),
      loaded: false,
      error: null,
    };
    const objects = {
      starPoints: null,
      constellationLines: null,
      constellationMaterial: null,
      sun: null,
      sunGlow: null,
      moon: null,
      moonLight: null,
      corona: null,
      skyDome: null,
      planetGroup: new THREE.Group(),
      planetMeshes: new Map(),
      highlights: new THREE.Group(),
      constellationHighlight: null,
    };
    const forcedLayers = new Set();
    const pendingHighlights = [];
    const scratch = {
      antiSun: new THREE.Vector3(),
      moonTint: new THREE.Color(0x883322),
      moonWhite: new THREE.Color(0xffffff),
      tmpColor: new THREE.Color(),
    };

    skyScene.background = new THREE.Color(0xdcccbb);
    objects.planetGroup.renderOrder = 3;
    objects.highlights.renderOrder = 6;
    skyScene.add(objects.planetGroup, objects.highlights);

    const pass = {
      render(renderer, mainCamera) {
        skyCamera.fov = mainCamera.fov;
        skyCamera.aspect = mainCamera.aspect;
        skyCamera.near = mainCamera.near;
        skyCamera.far = mainCamera.far;
        skyCamera.position.set(0, 0, 0);
        skyCamera.quaternion.copy(mainCamera.quaternion);
        skyCamera.updateProjectionMatrix();
        renderer.render(skyScene, skyCamera);
      },
    };

    function materialBase(options) {
      return {
        depthTest: false,
        depthWrite: false,
        toneMapped: false,
        ...options,
      };
    }

    function createBodyObjects() {
      if (THREE.Sky) {
        objects.skyDome = new THREE.Sky();
        objects.skyDome.scale.setScalar(R_DOME * 4);
        objects.skyDome.renderOrder = 0;
        objects.skyDome.visible = false;
        if (objects.skyDome.material) {
          objects.skyDome.material.depthTest = false;
          objects.skyDome.material.depthWrite = false;
          objects.skyDome.material.toneMapped = true;
          const uniforms = objects.skyDome.material.uniforms || {};
          if (uniforms.turbidity) uniforms.turbidity.value = 3.2;
          if (uniforms.rayleigh) uniforms.rayleigh.value = 1.2;
          if (uniforms.mieCoefficient) uniforms.mieCoefficient.value = 0.004;
          if (uniforms.mieDirectionalG) uniforms.mieDirectionalG.value = 0.8;
        }
        skyScene.add(objects.skyDome);
      }
      const sunMat = new THREE.MeshBasicMaterial(materialBase({ color: 0xfff1b0, side: THREE.DoubleSide }));
      objects.sun = new THREE.Mesh(new THREE.CircleGeometry(1, 48), sunMat);
      objects.sun.renderOrder = 5;
      objects.sunGlow = new THREE.Mesh(
        new THREE.PlaneGeometry(1, 1),
        new THREE.MeshBasicMaterial(materialBase({
          map: radialTexture('rgba(255,236,172,0.55)', 'rgba(255,210,96,0)'),
          transparent: true,
          side: THREE.DoubleSide,
        }))
      );
      objects.sunGlow.renderOrder = 4.9;
      objects.corona = new THREE.Mesh(
        new THREE.PlaneGeometry(1, 1),
        new THREE.MeshBasicMaterial(materialBase({
          map: radialTexture('rgba(255,255,255,0.5)', 'rgba(255,255,255,0)'),
          opacity: 0,
          transparent: true,
          side: THREE.DoubleSide,
        }))
      );
      objects.corona.renderOrder = 3.8;
      objects.moonLight = new THREE.DirectionalLight(0xffffff, 1.1);
      objects.moonLight.position.set(1, 1, 1);
      objects.moon = new THREE.Mesh(
        new THREE.SphereGeometry(1, 40, 20),
        new THREE.MeshLambertMaterial(materialBase({ color: 0xffffff }))
      );
      objects.moon.renderOrder = 4;
      new THREE.TextureLoader().load('/astronomy-data/moon_1k.jpg', (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace;
        objects.moon.material.map = tex;
        objects.moon.material.needsUpdate = true;
      });
      skyScene.add(objects.sunGlow, objects.corona, objects.sun, objects.moonLight, objects.moon);

      PLANETS.forEach((name) => {
        const mesh = new THREE.Mesh(
          new THREE.CircleGeometry(1, 24),
          new THREE.MeshBasicMaterial(materialBase({
            color: PLANET_COLORS[name],
            side: THREE.DoubleSide,
            transparent: true,
            opacity: 0.9,
          }))
        );
        mesh.renderOrder = 3;
        objects.planetMeshes.set(name, mesh);
        objects.planetGroup.add(mesh);
      });
    }

    async function loadAssets() {
      try {
        const [stars, constellations] = await Promise.all([
          fetch('/astronomy-data/stars.json').then((r) => r.json()),
          fetch('/astronomy-data/constellations.json').then((r) => r.json()),
        ]);
        assets.constellations = constellations;
        const positions = [];
        const mags = [];
        const colors = [];
        stars.stars.forEach(([raDeg, decDeg, mag, bv, hip, name]) => {
          const v = eqjVector(raDeg, decDeg);
          positions.push(v.x * R_DOME, v.y * R_DOME, v.z * R_DOME);
          mags.push(Number(mag));
          colors.push(...colorFromBv(bv));
          const star = {
            kind: 'star',
            name: name || `HIP ${hip}`,
            properName: name || '',
            hip,
            mag: Number(mag),
            bv: Number(bv),
            raDeg: Number(raDeg),
            decDeg: Number(decDeg),
            eqj: v,
          };
          assets.stars.push(star);
          assets.starByHip.set(Number(hip), star);
          if (name) assets.starByName.set(String(name).toLowerCase(), star);
        });

        const geom = new THREE.BufferGeometry();
        geom.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
        geom.setAttribute('aMag', new THREE.Float32BufferAttribute(mags, 1));
        geom.setAttribute('aColor', new THREE.Float32BufferAttribute(colors, 3));
        objects.starPoints = new THREE.Points(geom, makeStarMaterial());
        objects.starPoints.matrixAutoUpdate = false;
        objects.starPoints.renderOrder = 1;
        skyScene.add(objects.starPoints);

        const linePositions = [];
        const lineGeom = new THREE.BufferGeometry();
        objects.constellationMaterial = new THREE.LineBasicMaterial(materialBase({
          color: 0x5c7ea0,
          transparent: true,
          opacity: 0,
        }));
        objects.constellationLines = new THREE.LineSegments(lineGeom, objects.constellationMaterial);
        objects.constellationLines.matrixAutoUpdate = false;
        objects.constellationLines.renderOrder = 2;
        skyScene.add(objects.constellationLines);
        let droppedSegments = 0;
        Object.entries(constellations.lines || {}).forEach(([abbr, pairs]) => {
          const start = linePositions.length;
          pairs.forEach(([a, b]) => {
            const sa = assets.starByHip.get(Number(a));
            const sb = assets.starByHip.get(Number(b));
            if (!sa || !sb) {
              droppedSegments += 1;
              return;
            }
            linePositions.push(
              sa.eqj.x * R_DOME, sa.eqj.y * R_DOME, sa.eqj.z * R_DOME,
              sb.eqj.x * R_DOME, sb.eqj.y * R_DOME, sb.eqj.z * R_DOME
            );
          });
          assets.constellationSegments.set(abbr, linePositions.slice(start));
        });
        if (droppedSegments > 0) {
          console.warn(`astronomy constellations dropped ${droppedSegments} segments missing from the star catalog`);
        }
        lineGeom.setAttribute('position', new THREE.Float32BufferAttribute(linePositions, 3));
        assets.loaded = true;
        applyVisibility();
        flushPendingHighlights();
      } catch (err) {
        assets.error = err?.message || String(err);
        console.error('astronomy sky assets failed:', err);
      }
    }

    function updateStarMatrix(time) {
      matrixEqjToScene(time, observer, current.starMatrix);
      if (objects.starPoints) objects.starPoints.matrix.copy(current.starMatrix);
      if (objects.constellationLines) objects.constellationLines.matrix.copy(current.starMatrix);
      if (objects.constellationHighlight) objects.constellationHighlight.matrix.copy(current.starMatrix);
    }

    function fillBodyInfo(name, time, info, includeIllumination = false) {
      const eq = Astronomy.Equator(bodyApiName(name), time, observer, true, true);
      // 'normal' refraction: bodies render where the eye sees them (~0.5 deg
      // high at the horizon), so viewer sunsets match MCP rise/set times.
      // Stars stay airless (EQJ->HOR rotation); the sub-half-degree mismatch
      // against refracted bodies only exists at the horizon, where stars are
      // extincted anyway.
      const hor = Astronomy.Horizon(time, observer, eq.ra, eq.dec, 'normal');
      let illum = null;
      if (includeIllumination) {
        try { illum = Astronomy.Illumination(bodyApiName(name), time); } catch (_err) {}
      }
      info.kind = 'body';
      info.name = name;
      info.azimuthDeg = hor.azimuth;
      info.altitudeDeg = hor.altitude;
      info.raHours = eq.ra;
      info.decDeg = eq.dec;
      info.distanceAu = eq.dist;
      if (!info.dir) info.dir = new THREE.Vector3();
      sceneDirectionFromAltAzInto(hor.altitude, hor.azimuth, info.dir);
      if (illum) {
        info.magnitude = illum.mag;
        info.phaseFraction = illum.phase_fraction;
        info.phaseAngleDeg = illum.phase_angle;
      }
      return info;
    }

    function updateBodyInfo(name, time, includeIllumination = false) {
      const key = name.toLowerCase();
      const info = current.bodies.get(key) || { kind: 'body', name, dir: new THREE.Vector3() };
      current.bodies.set(key, fillBodyInfo(name, time, info, includeIllumination));
      return info;
    }

    function angularDiameterDeg(name, info, time) {
      if (name === 'Sun') {
        const distanceAu = Math.max(0.0001, Number(info?.distanceAu) || 1);
        return THREE.MathUtils.radToDeg(2 * Math.atan(SUN_RADIUS_AU / distanceAu));
      }
      if (name === 'Moon') {
        const distanceAu = Math.max(0.0001, Number(info?.distanceAu) || 0.00257);
        return THREE.MathUtils.radToDeg(2 * Math.atan(MOON_RADIUS_AU / distanceAu));
      }
      return 0.18;
    }

    function updateMoonAppearance(time, force = false) {
      if (!force && current.lastMoonAppearanceMs !== null && Math.abs(current.timeMs - current.lastMoonAppearanceMs) <= 60000) {
        return;
      }
      current.lastMoonAppearanceMs = current.timeMs;
      try {
        current.moonPhaseAngle = Astronomy.MoonPhase(time);
        current.moonIlluminatedFraction = (1 - Math.cos(THREE.MathUtils.degToRad(current.moonPhaseAngle))) / 2;
      } catch (_err) {
        current.moonPhaseAngle = 0;
        current.moonIlluminatedFraction = 0;
      }
      try {
        current.moonLibration = Astronomy.Libration(time);
      } catch (_err) {
        current.moonLibration = null;
      }
    }

    function updateEclipseState(time, sunInfo, moonInfo) {
      const sunRadius = THREE.MathUtils.degToRad(current.sunAngularRadiusDeg);
      const moonRadius = THREE.MathUtils.degToRad(current.moonAngularRadiusDeg);
      const separation = Math.acos(clamp(sunInfo.dir.dot(moonInfo.dir), -1, 1));
      current.solarObscuration = sunInfo.altitudeDeg > -1
        ? lensCoverageFraction(sunRadius, moonRadius, separation)
        : 0;

      let lunarTint = 0;
      try {
        const moonEcl = Astronomy.EclipticGeoMoon(time);
        const antiSolarSeparationDeg = THREE.MathUtils.radToDeg(
          Math.acos(clamp(moonInfo.dir.dot(scratch.antiSun.copy(sunInfo.dir).multiplyScalar(-1)), -1, 1))
        );
        const nodeFactor = 1 - smoothstep(0.55, 1.2, Math.abs(Number(moonEcl.lat) || 0));
        const oppositionFactor = 1 - smoothstep(0.65, 1.45, antiSolarSeparationDeg);
        lunarTint = clamp(nodeFactor * oppositionFactor, 0, 1);
      } catch (_err) {
        lunarTint = 0;
      }
      current.lunarEclipseTint = moonInfo.altitudeDeg > -2 ? lunarTint : 0;
    }

    function markerRadiusForMag(mag) {
      const px = clamp(7.5 - 1.1 * Number(mag ?? 2), 1.5, 8);
      return 0.08 + px * 0.028;
    }

    function updateSunMoon(time) {
      current.sun = updateBodyInfo('Sun', time, false);
      current.moon = updateBodyInfo('Moon', time, false);
      updateMoonAppearance(time);

      const sunInfo = current.sun;
      const moonInfo = current.moon;
      const sunDiam = angularDiameterDeg('Sun', sunInfo, time);
      current.sunAngularRadiusDeg = sunDiam / 2;
      const moonDiam = angularDiameterDeg('Moon', moonInfo, time);
      current.moonAngularRadiusDeg = moonDiam / 2;
      updateEclipseState(time, sunInfo, moonInfo);

      const sunRadius = R_DOME * Math.tan(THREE.MathUtils.degToRad(current.sunAngularRadiusDeg));
      billboard(objects.sun, sunInfo.dir, Math.max(0.35, sunRadius));
      billboard(objects.sunGlow, sunInfo.dir, Math.max(4.0, sunRadius * 9));
      billboard(objects.corona, sunInfo.dir, Math.max(5.4, sunRadius * 14));
      objects.corona.visible = !!(layers.sun && layers.moon && current.solarObscuration >= 0.94);
      objects.corona.material.opacity = smoothstep(0.94, 0.995, current.solarObscuration);

      const moonRadius = R_DOME * Math.tan(THREE.MathUtils.degToRad(current.moonAngularRadiusDeg));
      objects.moon.position.copy(moonInfo.dir).multiplyScalar(R_DOME);
      objects.moon.scale.setScalar(Math.max(0.32, moonRadius));
      objects.moon.lookAt(0, 0, 0);
      if (current.moonLibration) {
        objects.moon.rotateY(THREE.MathUtils.degToRad(Number(current.moonLibration.elon) || 0));
        objects.moon.rotateX(THREE.MathUtils.degToRad(-(Number(current.moonLibration.elat) || 0)));
      }
      objects.moonLight.position.copy(sunInfo.dir).multiplyScalar(50);
      if (objects.moon.material?.color) {
        objects.moon.material.color.copy(
          scratch.tmpColor.copy(scratch.moonWhite).lerp(scratch.moonTint, current.lunarEclipseTint)
        );
      }
      if (objects.skyDome?.material?.uniforms?.sunPosition) {
        objects.skyDome.material.uniforms.sunPosition.value.copy(sunInfo.dir);
      }
    }

    function updatePlanets(time) {
      PLANETS.forEach((name) => {
        const info = updateBodyInfo(name, time, true);
        const mesh = objects.planetMeshes.get(name);
        billboard(mesh, info.dir, markerRadiusForMag(info.magnitude));
      });
    }

    function applyVisibility() {
      const sunVisible = !!(layers.sun || forcedLayers.has('sun'));
      const moonVisible = !!(layers.moon || forcedLayers.has('moon'));
      if (objects.sun) objects.sun.visible = sunVisible;
      if (objects.sunGlow) objects.sunGlow.visible = sunVisible;
      if (objects.corona) objects.corona.visible = !!(sunVisible && moonVisible && current.solarObscuration >= 0.94);
      if (objects.moon) objects.moon.visible = moonVisible;
      objects.planetGroup.visible = !!(layers.planets || forcedLayers.has('planets'));
      if (objects.starPoints) objects.starPoints.visible = !!(layers.stars || forcedLayers.has('stars'));
      if (objects.constellationLines) objects.constellationLines.visible = !!(layers.stars && layers.constellations);
    }

    function setLayerVisible(kind, on) {
      if (kind in layers) {
        layers[kind] = !!on;
        forcedLayers.delete(kind);
        applyVisibility();
      }
    }

    function setPhysicalMode(on) {
      current.physicalSky = !!on;
      if (objects.skyDome) {
        objects.skyDome.visible = current.physicalSky;
      }
    }

    function setTime(utcMs) {
      current.timeMs = Number(utcMs) || Date.now();
      const time = timeFromMs(current.timeMs);
      updateSunMoon(time);
      if (current.lastPlanetMs === null || Math.abs(current.timeMs - current.lastPlanetMs) > 60000) {
        updatePlanets(time);
        current.lastPlanetMs = current.timeMs;
      }
      if (current.lastStarMatrixMs === null || Math.abs(current.timeMs - current.lastStarMatrixMs) > 1000) {
        updateStarMatrix(time);
        current.lastStarMatrixMs = current.timeMs;
      }
      const sunAlt = current.sun?.altitudeDeg ?? 90;
      const daylightFade = 1 - smoothstep(STAR_FADE_DARK_ALT, STAR_FADE_DAY_ALT, sunAlt);
      const eclipseFade = smoothstep(0.78, 0.995, current.solarObscuration) * smoothstep(-2, 8, sunAlt);
      current.starFade = Math.max(daylightFade, eclipseFade);
      if (objects.starPoints) objects.starPoints.material.uniforms.uFade.value = current.starFade;
      if (objects.constellationMaterial) objects.constellationMaterial.opacity = 0.35 * current.starFade;
      skyScene.background.set(sunAlt > -4 ? 0xdcccbb : 0x050711);
      if (objects.skyDome) objects.skyDome.visible = current.physicalSky;
      applyVisibility();
      updateHighlightPositions();
    }

    function pickRay(ndc, mainCamera) {
      skyCamera.fov = mainCamera.fov;
      skyCamera.aspect = mainCamera.aspect;
      skyCamera.near = mainCamera.near;
      skyCamera.far = mainCamera.far;
      skyCamera.position.set(0, 0, 0);
      skyCamera.quaternion.copy(mainCamera.quaternion);
      skyCamera.updateProjectionMatrix();
      raycaster.setFromCamera(ndc, skyCamera);
      return raycaster.ray.direction.clone().normalize();
    }

    function pickAt(ndc, mainCamera) {
      if (!mainCamera) return null;
      const ray = pickRay(ndc, mainCamera);
      const candidates = [];
      current.bodies.forEach((info, key) => {
        const layerOn = key === 'sun' ? layers.sun : key === 'moon' ? layers.moon : layers.planets;
        if (!layerOn) return;
        const angle = THREE.MathUtils.radToDeg(Math.acos(clamp(ray.dot(info.dir), -1, 1)));
        if (angle <= 1.2) candidates.push({ angle, priority: 0, info });
      });
      if (layers.stars && current.starFade > 0.08 && assets.loaded) {
        const m = current.starMatrix;
        assets.stars.forEach((star) => {
          const dir = star.eqj.clone().multiplyScalar(R_DOME).applyMatrix4(m).normalize();
          const angle = THREE.MathUtils.radToDeg(Math.acos(clamp(ray.dot(dir), -1, 1)));
          if (angle <= 0.9) {
            candidates.push({
              angle,
              priority: 1,
              info: {
                kind: 'star',
                name: star.name,
                hip: star.hip,
                magnitude: star.mag,
                raJ2000Hours: star.raDeg / 15,
                decJ2000Deg: star.decDeg,
                dir,
              },
            });
          }
        });
      }
      candidates.sort((a, b) => a.angle - b.angle || a.priority - b.priority);
      return candidates[0]?.info || null;
    }

    function makeRing() {
      return new THREE.Mesh(
        new THREE.RingGeometry(0.75, 0.95, 48),
        new THREE.MeshBasicMaterial(materialBase({
          color: 0xff8c1a,
          side: THREE.DoubleSide,
          transparent: true,
          opacity: 0.96,
        }))
      );
    }

    function addLabel(text) {
      const tex = labelTexture(text);
      return new THREE.Mesh(
        new THREE.PlaneGeometry(tex.image.width / 44, 1),
        new THREE.MeshBasicMaterial(materialBase({
          map: tex,
          transparent: true,
          side: THREE.DoubleSide,
        }))
      );
    }

    function findConstellation(name) {
      const lower = String(name || '').trim().toLowerCase();
      const entries = Object.entries(assets.constellations?.names || {});
      const found = entries.find(([abbr, full]) => (
        abbr.toLowerCase() === lower || String(full).toLowerCase() === lower
      ));
      return found ? { type: 'constellation', abbr: found[0], name: found[1] } : null;
    }

    function resolveHighlight(target) {
      const type = String(target?.target_type || target?.type || '').toLowerCase();
      const name = String(target?.name || '').trim();
      const lower = name.toLowerCase();
      if (!name) return null;
      if (type === 'body') {
        const bodyName = BODY_NAME_BY_LOWER.get(lower);
        return bodyName ? { type: 'body', name: bodyName } : null;
      }
      if (type === 'star') {
        const star = assets.starByName.get(lower);
        return star ? { type: 'star', star, name: star.name } : null;
      }
      if (type === 'constellation') return findConstellation(name);
      const bodyName = BODY_NAME_BY_LOWER.get(lower);
      if (bodyName) return { type: 'body', name: bodyName };
      const star = assets.starByName.get(lower);
      if (star) return { type: 'star', star, name: star.name };
      return findConstellation(name);
    }

    function disposeObject(obj) {
      obj.geometry?.dispose?.();
      const materials = Array.isArray(obj.material) ? obj.material : [obj.material];
      materials.forEach((material) => {
        material?.map?.dispose?.();
        material?.dispose?.();
      });
    }

    function clearHighlights() {
      pendingHighlights.length = 0;
      objects.highlights.children.slice().forEach((child) => {
        objects.highlights.remove(child);
        disposeObject(child);
      });
      objects.highlights.clear();
      if (objects.constellationHighlight) {
        skyScene.remove(objects.constellationHighlight);
        disposeObject(objects.constellationHighlight);
        objects.constellationHighlight = null;
      }
      forcedLayers.clear();
      applyVisibility();
    }

    function forceLayerForResolved(resolved) {
      if (resolved.type === 'star') return 'stars';
      if (resolved.type === 'body') {
        const lower = String(resolved.name || '').toLowerCase();
        if (lower === 'sun') return 'sun';
        if (lower === 'moon') return 'moon';
        return 'planets';
      }
      return null;
    }

    function needsCatalog(target) {
      const type = String(target?.target_type || target?.type || '').toLowerCase();
      if (type === 'star' || type === 'constellation') return true;
      const name = String(target?.name || '').trim().toLowerCase();
      return !BODY_NAME_BY_LOWER.has(name);
    }

    function flushPendingHighlights() {
      if (!assets.loaded || !pendingHighlights.length) return;
      const pending = pendingHighlights.splice(0);
      pending.forEach((target) => highlight(target));
    }

    function highlight(target) {
      if (!assets.loaded && needsCatalog(target)) {
        pendingHighlights.push(target);
        return true;
      }
      const resolved = resolveHighlight(target);
      if (!resolved) return false;
      if (resolved.type === 'constellation') {
        const coords = assets.constellationSegments.get(resolved.abbr) || [];
        const geom = new THREE.BufferGeometry();
        geom.setAttribute('position', new THREE.Float32BufferAttribute(coords, 3));
        objects.constellationHighlight = new THREE.LineSegments(
          geom,
          new THREE.LineBasicMaterial(materialBase({ color: 0xff8c1a, transparent: true, opacity: 1 }))
        );
        objects.constellationHighlight.matrixAutoUpdate = false;
        objects.constellationHighlight.matrix.copy(current.starMatrix);
        objects.constellationHighlight.renderOrder = 6;
        objects.constellationHighlight.userData.highlight = { ...resolved, label: target.label || resolved.name };
        skyScene.add(objects.constellationHighlight);
        return true;
      }
      const forcedLayer = forceLayerForResolved(resolved);
      if (forcedLayer) {
        forcedLayers.add(forcedLayer);
        applyVisibility();
      }
      const ring = makeRing();
      const label = addLabel(target.label || resolved.name);
      ring.userData.highlight = { ...resolved, labelMesh: label, label: target.label || resolved.name };
      label.userData.labelFor = ring;
      objects.highlights.add(ring, label);
      updateHighlightPositions();
      return true;
    }

    function updateHighlightPositions() {
      objects.highlights.children.forEach((child) => {
        if (child.userData.labelFor) return;
        const h = child.userData.highlight;
        let dir = null;
        if (h?.type === 'body') {
          dir = current.bodies.get(String(h.name).toLowerCase())?.dir;
        } else if (h?.type === 'star') {
          dir = h.star.eqj.clone().multiplyScalar(R_DOME).applyMatrix4(current.starMatrix).normalize();
        }
        if (!dir) return;
        billboard(child, dir, 1.45);
        const label = h.labelMesh;
        if (label) {
          label.position.copy(dir).multiplyScalar(R_DOME).add(new THREE.Vector3(0, 2.5, 0));
          label.scale.setScalar(2.4);
          label.lookAt(0, 0, 0);
        }
      });
    }

    function getStatus() {
      const moonPhase = current.timeMs ? current.moonPhaseAngle : 0;
      return {
        loaded: assets.loaded,
        error: assets.error,
        sun: current.sun,
        moon: current.moon,
        moonPhase,
        moonPhasePct: current.moonIlluminatedFraction * 100,
        physicalSky: current.physicalSky,
        hasSkyDome: Boolean(objects.skyDome),
        skyDomeVisible: Boolean(objects.skyDome?.visible),
        highlightCount: objects.highlights.children.length + (objects.constellationHighlight ? 1 : 0),
      };
    }

    function getLightingState() {
      return {
        timeMs: current.timeMs,
        sun: current.sun
          ? {
              altitudeDeg: current.sun.altitudeDeg,
              azimuthDeg: current.sun.azimuthDeg,
              direction: current.sun.dir.clone(),
              angularRadiusDeg: current.sunAngularRadiusDeg,
            }
          : null,
        moon: current.moon
          ? {
              altitudeDeg: current.moon.altitudeDeg,
              azimuthDeg: current.moon.azimuthDeg,
              direction: current.moon.dir.clone(),
              angularRadiusDeg: current.moonAngularRadiusDeg,
              illuminatedFraction: clamp(Number(current.moonIlluminatedFraction) || 0, 0, 1),
            }
          : null,
        solarObscuration: current.solarObscuration,
        lunarEclipseTint: current.lunarEclipseTint,
        starFade: current.starFade,
      };
    }

    createBodyObjects();
    loadAssets();
    applyVisibility();
    setTime(Date.now());

    return {
      pass,
      setTime,
      setLayerVisible,
      pickAt,
      highlight,
      clearHighlights,
      setPhysicalMode,
      getStatus,
      getLightingState,
      _test: { sceneDirectionFromAltAz, matrixEqjToScene },
    };
  }

  global.VEILSky = { create };
})(window);
