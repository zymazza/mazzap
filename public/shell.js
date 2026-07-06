/* ============================================================================
   VEIL — viewer shell behaviour
   Owns ONLY the new chrome: rail mode-switching, the single flyout panel,
   the contextual inspector + chat dock, atlas-layer search, reset-view and
   the help sheet. The engine (app.js) and feature modules are untouched and
   keep binding to the same element IDs re-homed into this shell.
   ========================================================================== */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const rail = $('rail');
  const flyout = $('flyout');
  const flyoutTitle = $('flyout-title');
  const panes = [...document.querySelectorAll('.pane')];
  const inspector = $('inspector');
  const chatDock = $('chat-panel');

  const TITLES = {
    layers: 'Layers',
    explore: 'Explore',
    simulate: 'Simulate water',
    fire: 'Simulate fire',
    survey: 'Field surveys',
    telemetry: 'Live telemetry',
  };

  let activeMode = null;

  function showPane(mode) {
    panes.forEach((p) => p.classList.toggle('active', p.dataset.pane === mode));
    flyoutTitle.textContent = TITLES[mode] || mode;
    flyout.hidden = false;
    document.body.classList.add('flyout-open');
    activeMode = mode;
    syncRail();
  }

  function closeFlyout() {
    flyout.hidden = true;
    document.body.classList.remove('flyout-open');
    activeMode = null;
    syncRail();
  }

  function toggleChat(force) {
    const open = force != null ? force : chatDock.hidden;
    chatDock.hidden = !open;
    document.body.classList.toggle('chat-open', open);
    syncRail();
  }

  function syncRail() {
    rail.querySelectorAll('.rail-btn').forEach((b) => {
      const m = b.dataset.mode;
      if (m === 'ask') b.classList.toggle('active', !chatDock.hidden);
      else b.classList.toggle('active', !flyout.hidden && m === activeMode);
    });
  }

  rail.querySelectorAll('.rail-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.mode;
      if (mode === 'ask') { toggleChat(); return; }
      if (!flyout.hidden && activeMode === mode) closeFlyout();
      else showPane(mode);
    });
  });

  $('flyout-close').addEventListener('click', closeFlyout);
  $('chat-close').addEventListener('click', () => toggleChat(false));

  // open Layers by default so the workspace doesn't read as empty on first load
  showPane('layers');

  /* ---------------- contextual inspector ---------------- */
  // Feature modules dispatch veil:inspect after a real map pick/marker pick.
  // Hidden inspector DOM can still change in the background without reopening it.
  const inspectorClose = $('inspector-close');
  inspectorClose.addEventListener('click', () => { inspector.hidden = true; });

  const revealInspector = () => { if (inspector.hidden) inspector.hidden = false; };
  document.addEventListener('veil:inspect', revealInspector);

  /* ---------------- atlas layer search ---------------- */
  const search = $('atlas-search');
  if (search) {
    search.addEventListener('input', () => {
      const q = search.value.trim().toLowerCase();
      document.querySelectorAll('#atlas-toggles .toggle-row').forEach((row) => {
        const label = (row.textContent || '').toLowerCase();
        row.style.display = !q || label.includes(q) ? '' : 'none';
      });
    });
  }

  /* ---------------- reset view ---------------- */
  // capture a "home" camera pose shortly after boot, restore it on demand
  let homePose = null;
  function snapshotHome() {
    const v = window.__twin && window.__twin.viewer;
    if (!v || !v.camera || !v.controls) return false;
    homePose = {
      pos: v.camera.position.clone(),
      target: v.controls.target.clone(),
    };
    return true;
  }
  const homeTimer = setInterval(() => { if (snapshotHome()) clearInterval(homeTimer); }, 800);

  $('reset-view').addEventListener('click', () => {
    const v = window.__twin && window.__twin.viewer;
    if (!v || !homePose) return;
    v.camera.position.copy(homePose.pos);
    v.controls.target.copy(homePose.target);
    v.camera.lookAt(homePose.target);
    v.controls.update();
  });

  /* ---------------- help sheet ---------------- */
  const helpSheet = $('help-sheet');
  $('help-btn').addEventListener('click', () => { helpSheet.hidden = false; });
  $('help-close').addEventListener('click', () => { helpSheet.hidden = true; });
  helpSheet.addEventListener('click', (e) => { if (e.target === helpSheet) helpSheet.hidden = true; });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!helpSheet.hidden) helpSheet.hidden = true;
    }
  });
})();
