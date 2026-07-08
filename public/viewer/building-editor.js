/* Drag-gizmo editor for aligning the 3D building models — the TransformControls
   setup ported from the source viewer ("Asset Transform" panel).

   Press B to toggle edit mode, then click a model (or Tab) to attach the
   gizmo. The editor is button-driven: Move (G) / Rotate (R) / Scale (S) pick
   the gizmo mode (full 3-axis arrows and rings), "Space" flips the handles
   between world and the building's local axes, "Reset" restores the placement
   the page loaded with, and "Save Transform" POSTs the live values to the
   dev server, which writes data/buildings/models/manifest.json.

   Placements stay terrain-anchored: at drag end the live transform is
   normalized back into manifest fields (x, y, yaw_deg, rot_x/rot_z tilts,
   uniform scale, z_offset relative to the terrain at the new spot), and a
   horizontal move keeps the building glued to the terrain. */
(function attachBuildingEditor(global) {
  const { THREE, VEILTerrain } = global;

  function create(viewer) {
    const state = {
      active: false,
      selected: null,
      controls: null,
      host: null,
      readout: null,
      status: null,
      statusTimer: null,
      buttons: {},
      original: new Map(), // id -> placement at page load (for Reset)
    };

    const round = (value, factor) => {
      const rounded = Math.round(value * factor) / factor;
      return Object.is(rounded, -0) ? 0 : rounded;
    };
    const numberOr = (value, fallback = 0) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : fallback;
    };
    const deg360 = (rad) => ((THREE.MathUtils.radToDeg(rad) % 360) + 360) % 360;
    const degSigned = (rad) => {
      const deg = deg360(rad);
      return deg > 180 ? deg - 360 : deg;
    };

    function group() {
      return viewer.buildingModelsGroup;
    }

    function invalidateBuildingShadows() {
      viewer.invalidateShadowMap?.('building-placement');
    }

    function entryOf(child) {
      return child.userData.entry;
    }

    function rememberOriginal(child) {
      const e = entryOf(child);
      if (!state.original.has(e.id)) {
        state.original.set(e.id, { ...e.placement });
      }
    }

    function readoutText() {
      if (!state.selected) {
        return 'Click a building (or Tab) to select. Drag the gizmo handles.';
      }
      const e = entryOf(state.selected);
      const p = e.placement;
      return `${e.id} (${e.name})\n` +
        `T: x ${numberOr(p.x).toFixed(2)}m  y ${numberOr(p.y).toFixed(2)}m  dz ${numberOr(p.z_offset).toFixed(2)}m\n` +
        `R: x ${numberOr(p.rot_x_deg).toFixed(1)}°  y ${numberOr(p.yaw_deg).toFixed(1)}°  ` +
        `z ${numberOr(p.rot_z_deg).toFixed(1)}°   scale ${numberOr(p.scale, 1).toFixed(3)}`;
    }

    function refreshUi() {
      if (!state.host) return;
      state.readout.textContent = readoutText();
      const has = Boolean(state.selected);
      const mode = state.controls?.mode || 'translate';
      state.buttons.move.disabled = !has || mode === 'translate';
      state.buttons.rotate.disabled = !has || mode === 'rotate';
      state.buttons.scale.disabled = !has || mode === 'scale';
      state.buttons.scaleDown.disabled = !has;
      state.buttons.scaleUp.disabled = !has;
      state.buttons.space.disabled = !has;
      state.buttons.reset.disabled = !has;
      state.buttons.save.disabled = !(group()?.children || []).length;
      state.buttons.done.disabled = !state.active;
      state.buttons.move.classList.toggle('active', has && mode === 'translate');
      state.buttons.rotate.classList.toggle('active', has && mode === 'rotate');
      state.buttons.scale.classList.toggle('active', has && mode === 'scale');
      state.buttons.space.textContent =
        `Space: ${state.controls?.space === 'local' ? 'Local' : 'World'}`;
    }

    function setStatus(message) {
      if (!state.status) return;
      if (state.statusTimer) clearTimeout(state.statusTimer);
      state.status.textContent = message;
      state.statusTimer = message
        ? setTimeout(() => {
          state.status.textContent = '';
          state.statusTimer = null;
        }, 2500)
        : null;
    }

    function setPanelVisible(visible) {
      if (!state.host) return;
      state.host.hidden = !visible;
      state.host.style.display = visible ? '' : 'none';
    }

    // live object transform -> manifest placement fields
    function syncPlacementFromObject(child) {
      const p = entryOf(child).placement;
      p.x = round(child.position.x, 1000);
      p.y = round(-child.position.z, 1000);
      p.rot_x_deg = round(degSigned(child.rotation.x), 100);
      p.yaw_deg = round(deg360(child.rotation.y), 100);
      p.rot_z_deg = round(degSigned(child.rotation.z), 100);
      p.scale = round((child.scale.x + child.scale.y + child.scale.z) / 3, 10000);
      const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(viewer.terrainGrid, p.x, p.y);
      p.z_offset = round(child.position.y - terrainY, 1000);
      return p;
    }

    function setUniformScale(child, scale, options = {}) {
      const p = entryOf(child).placement;
      p.scale = round(Math.max(0.001, numberOr(scale, numberOr(p.scale, 1))), 10000);
      child.scale.setScalar(p.scale);
      if (options.applyPlacement) {
        global.VEILBuildings3D.applyPlacement(child, p, viewer.terrainGrid);
        invalidateBuildingShadows();
      }
      refreshUi();
      return p.scale;
    }

    function enforceUniformScale(child, dragInfo) {
      if (!child) return;
      const startScale = Math.max(0.001, numberOr(dragInfo?.scale, numberOr(entryOf(child).placement.scale, child.scale.x || 1)));
      const ratios = [child.scale.x, child.scale.y, child.scale.z]
        .map((value) => numberOr(value, startScale) / startScale)
        .filter(Number.isFinite);
      const ratio = ratios.reduce((best, value) =>
        Math.abs(value - 1) > Math.abs(best - 1) ? value : best, 1);
      setUniformScale(child, startScale * ratio);
    }

    function stepUniformScale(factor) {
      if (!state.selected) return;
      const p = entryOf(state.selected).placement;
      const next = numberOr(p.scale, 1) * factor;
      const scale = setUniformScale(state.selected, next, { applyPlacement: true });
      setStatus(`Scale ${scale.toFixed(3)}.`);
    }

    // snap the object exactly onto its (normalized) placement
    function normalizeSelected(dragInfo) {
      if (!state.selected) return;
      const p = syncPlacementFromObject(state.selected);
      // a horizontal move should follow the terrain: keep the old height
      // offset unless the drag actually used the Y handle
      if (dragInfo && !String(dragInfo.axis || '').includes('Y')) {
        p.z_offset = dragInfo.z_offset;
      }
      global.VEILBuildings3D.applyPlacement(state.selected, p, viewer.terrainGrid);
      invalidateBuildingShadows();
      refreshUi();
    }

    function setMode(mode) {
      if (!state.controls) return;
      state.controls.setMode(['rotate', 'scale'].includes(mode) ? mode : 'translate');
      state.controls.showX = true;
      state.controls.showY = true;
      state.controls.showZ = true;
      refreshUi();
    }

    function toggleSpace() {
      if (!state.controls) return;
      state.controls.setSpace(state.controls.space === 'local' ? 'world' : 'local');
      refreshUi();
    }

    function ensureControls() {
      if (state.controls) return;
      const c = new THREE.TransformControls(viewer.camera, viewer.renderer.domElement);
      c.visible = false;
      c.enabled = false;
      c.setMode('translate');
      c.setSpace('local');
      c.size = 0.8;
      c.showX = true;
      c.showY = true;
      c.showZ = true;
      c.userData = {};

      c.addEventListener('dragging-changed', (event) => {
        viewer.controls.enabled = !event.value;
        if (event.value) {
          if (!state.selected) {
            c.userData.drag = null;
          } else if (c.mode === 'translate') {
            c.userData.drag = { axis: c.axis, z_offset: entryOf(state.selected).placement.z_offset };
          } else if (c.mode === 'scale') {
            c.userData.drag = { scale: entryOf(state.selected).placement.scale };
          } else {
            c.userData.drag = null;
          }
        } else {
          normalizeSelected(c.userData.drag);
          c.userData.drag = null;
        }
      });
      c.addEventListener('objectChange', () => {
        if (state.selected) {
          if (c.mode === 'scale') {
            enforceUniformScale(state.selected, c.userData.drag);
          }
          syncPlacementFromObject(state.selected);
          invalidateBuildingShadows();
          refreshUi();
        }
      });

      viewer.scene.add(c);
      state.controls = c;
    }

    function select(child) {
      if (!child && !state.controls) {
        state.selected = null;
        refreshUi();
        return;
      }
      ensureControls();
      state.selected = child || null;
      if (child) {
        rememberOriginal(child);
        state.controls.attach(child);
        state.controls.visible = true;
        state.controls.enabled = true;
      } else {
        state.controls.detach();
        state.controls.visible = false;
        state.controls.enabled = false;
      }
      refreshUi();
    }

    function resetSelected() {
      if (!state.selected) return;
      const e = entryOf(state.selected);
      const original = state.original.get(e.id);
      if (!original) return;
      e.placement = { ...original };
      global.VEILBuildings3D.applyPlacement(state.selected, e.placement, viewer.terrainGrid);
      invalidateBuildingShadows();
      refreshUi();
      setStatus(`Reset ${e.id} to its loaded placement.`);
    }

    function closeEditor() {
      state.active = false;
      setPanelVisible(false);
      select(null);
    }

    function onKey(e) {
      if (e.target?.closest?.('input, textarea')) return;
      if (e.key === 'b' || e.key === 'B') {
        e.preventDefault();
        toggle();
        return;
      }
      if (!state.active) return;
      const handlers = {
        Tab: () => {
          const kids = group()?.children || [];
          if (!kids.length) return;
          const i = kids.indexOf(state.selected);
          select(kids[(i + 1) % kids.length]);
        },
        Esc: closeEditor,
        Escape: closeEditor,
        g: () => setMode('translate'),
        r: () => setMode('rotate'),
        s: () => setMode('scale'),
        '[': () => stepUniformScale(0.95),
        ']': () => stepUniformScale(1.05),
      };
      const handler = handlers[e.key.length === 1 ? e.key.toLowerCase() : e.key];
      if (handler) {
        e.preventDefault();
        e.stopPropagation();
        handler();
      }
    }

    function onPointerDown(e) {
      if (!state.active || !group()) return;
      // don't change selection while grabbing a gizmo handle
      if (state.controls?.axis) return;
      const canvas = viewer.renderer.domElement;
      const rect = canvas.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1
      );
      const ray = new THREE.Raycaster();
      ray.setFromCamera(ndc, viewer.camera);
      const hit = ray.intersectObjects(group().children, true)[0];
      if (!hit) return;
      let node = hit.object;
      while (node.parent && node.parent !== group()) node = node.parent;
      select(node);
    }

    async function save() {
      const placements = {};
      (group()?.children || []).forEach((child) => {
        const e = entryOf(child);
        placements[e.id] = e.placement;
      });
      setStatus('Saving placements...');
      try {
        const r = await fetch('/api/building-placements', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(placements),
        });
        setStatus(r.ok
          ? 'Saved to data/buildings/models/manifest.json ✓'
          : `Save failed: ${r.status}`);
      } catch (err) {
        setStatus(`Save failed: ${err?.message || err}`);
      }
    }

    function buildUi() {
      const host = document.createElement('div');
      host.id = 'building-editor';
      host.hidden = true;
      host.style.display = 'none';
      host.innerHTML =
        '<div class="be-row">' +
        '  <button id="be-move">Move (G)</button>' +
        '  <button id="be-rotate">Rotate (R)</button>' +
        '  <button id="be-scale">Scale (S)</button>' +
        '</div>' +
        '<div class="be-row">' +
        '  <button id="be-scale-down">Scale -5% ([)</button>' +
        '  <button id="be-scale-up">Scale +5% (])</button>' +
        '</div>' +
        '<div class="be-row be-single">' +
        '  <button id="be-space">Space: Local</button>' +
        '</div>' +
        '<pre id="be-readout"></pre>' +
        '<div class="be-row">' +
        '  <button id="be-reset">Reset Transform</button>' +
        '  <button id="be-save">Save Transform</button>' +
        '</div>' +
        '<div class="be-row be-single">' +
        '  <button id="be-done">Done</button>' +
        '</div>' +
        '<div id="be-status"></div>';
      document.body.appendChild(host);
      state.host = host;
      state.readout = host.querySelector('#be-readout');
      state.status = host.querySelector('#be-status');
      state.buttons = {
        move: host.querySelector('#be-move'),
        rotate: host.querySelector('#be-rotate'),
        scale: host.querySelector('#be-scale'),
        scaleDown: host.querySelector('#be-scale-down'),
        scaleUp: host.querySelector('#be-scale-up'),
        space: host.querySelector('#be-space'),
        reset: host.querySelector('#be-reset'),
        save: host.querySelector('#be-save'),
        done: host.querySelector('#be-done'),
      };
      state.buttons.move.addEventListener('click', () => setMode('translate'));
      state.buttons.rotate.addEventListener('click', () => setMode('rotate'));
      state.buttons.scale.addEventListener('click', () => setMode('scale'));
      state.buttons.scaleDown.addEventListener('click', () => stepUniformScale(0.95));
      state.buttons.scaleUp.addEventListener('click', () => stepUniformScale(1.05));
      state.buttons.space.addEventListener('click', toggleSpace);
      state.buttons.reset.addEventListener('click', resetSelected);
      state.buttons.save.addEventListener('click', save);
      state.buttons.done.addEventListener('click', closeEditor);
    }

    function toggle() {
      state.active = !state.active;
      setPanelVisible(state.active);
      if (!state.active) select(null);
      refreshUi();
    }

    buildUi();
    window.addEventListener('keydown', onKey, true);
    viewer.renderer.domElement.addEventListener('pointerdown', onPointerDown);

    return {
      // scriptable API (used by the screenshot tuning harness)
      set(id, patch) {
        const child = (group()?.children || []).find((c) => c.name === id);
        if (!child) return null;
        rememberOriginal(child);
        const p = entryOf(child).placement;
        Object.assign(p, patch);
        global.VEILBuildings3D.applyPlacement(child, p, viewer.terrainGrid);
        invalidateBuildingShadows();
        refreshUi();
        return { ...p };
      },
      get(id) {
        const child = (group()?.children || []).find((c) => c.name === id);
        return child ? { ...entryOf(child).placement } : null;
      },
      save,
      toggle,
      select,
    };
  }

  global.VEILBuildingEditor = { create };
})(window);
