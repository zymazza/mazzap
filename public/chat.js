/* "Ask G.A.I.A." chat panel: a collapsible window above the coordinate
   readout that talks to POST /api/chat (GPT-5.5 with the twin MCP server's
   tools). The user can scope questions three ways:
     - the whole twin (default),
     - a polygon region drawn on the terrain (3+ points; clearable),
     - a single picked point, asked about with ~100 m of preloaded context.
   Drawing and picking reuse the viewer's terrain raycast (same picking as
   the GPS readout); while a draw/pick mode is active the click is consumed
   in the capture phase so identify/readout don't also fire. */
(function attachChat(global) {
  'use strict';

  const POINT_RADIUS_M = 100;
  const PLAN_PROPOSAL_TOOLS = new Set([
    'propose_plan_edits', 'propose_vegetation_clearance',
    'propose_swale', 'propose_orchard', 'propose_garden',
  ]);

  function tracePayload(value) {
    if (value && typeof value === 'object') return value;
    try { return JSON.parse(String(value || '')); } catch (_err) { return null; }
  }

  function planActionsFromTrace(trace) {
    const actions = [];
    (Array.isArray(trace) ? trace : []).forEach((entry) => {
      const result = tracePayload(entry?.result);
      if (!result) return;
      if (PLAN_PROPOSAL_TOOLS.has(entry.tool) && result.proposal_id) {
        actions.push({ type: 'proposal', proposal: result });
      } else if (entry.tool === 'apply_plan_proposal' && result.proposal?.proposal_id) {
        actions.push({ type: 'applied', proposal: result.proposal, revision: result.revision || null });
      }
    });
    return actions;
  }

  function create(viewer, scene) {
    const { THREE } = global;
    const canvas = viewer.renderer?.domElement;
    const grid = viewer.terrainGrid;
    if (!canvas || !grid) return null;
    const georef = global.VEILGeoref.createSceneGeoref(scene.origin_utm, grid.minElevation);

    const els = {
      panel: document.getElementById('chat-panel'),
      draw: document.getElementById('chat-draw'),
      pick: document.getElementById('chat-pick'),
      clear: document.getElementById('chat-clear'),
      key: document.getElementById('chat-key'),
      scope: document.getElementById('chat-scope'),
      messages: document.getElementById('chat-messages'),
      form: document.getElementById('chat-form'),
      input: document.getElementById('chat-input'),
      send: document.getElementById('chat-send'),
    };
    if (!els.panel) return null;

    /* ---------------- bring-your-own OpenAI key (browser-local) */

    const KEY_STORE = 'veil_openai_key';
    const getKey = () => {
      try { return (localStorage.getItem(KEY_STORE) || '').trim(); } catch (_e) { return ''; }
    };
    function renderKeyButton() {
      const has = !!getKey();
      els.key.textContent = has ? 'Key ✓' : 'Key';
      els.key.classList.toggle('active', has);
      els.key.title = has
        ? 'Your OpenAI key is set for this browser — click to change or remove it'
        : 'Set your own OpenAI API key (stored only in this browser; sent per request)';
    }
    function promptKey() {
      const current = getKey();
      const entered = window.prompt(
        current
          ? 'Your OpenAI API key is set for this browser. Paste a new one to replace it, or clear the field to remove it:'
          : 'Paste your OpenAI API key. It is stored only in this browser (localStorage) and sent with each question — it never touches the repo or the server\'s disk.',
        current,
      );
      if (entered === null) return; // cancelled
      try {
        const v = entered.trim();
        if (v) { localStorage.setItem(KEY_STORE, v); note('OpenAI key saved in this browser.'); }
        else { localStorage.removeItem(KEY_STORE); note('OpenAI key removed from this browser.'); }
      } catch (_e) { /* storage unavailable */ }
      renderKeyButton();
    }

    const state = {
      mode: null,            // null | 'draw' | 'pick'
      vertices: [],          // scene-local [x, y] (y = north)
      point: null,           // scene-local {x, y}
      scope: 'all',          // 'all' | 'region' | 'point'
      history: [],           // [{role, content}]
      busy: false,
      proposalCards: new Map(),
    };

    function setBusy(busy) {
      state.busy = !!busy;
      els.send.disabled = state.busy;
      els.input.readOnly = state.busy;
      els.input.setAttribute('aria-busy', state.busy ? 'true' : 'false');
      els.input.setAttribute('aria-disabled', state.busy ? 'true' : 'false');
      els.input.title = state.busy
        ? 'G.A.I.A. is answering; wait for the reply before sending another message.'
        : '';
      els.messages.querySelectorAll('[data-plan-proposal-approve]').forEach((button) => {
        button.disabled = state.busy || button.dataset.proposalStatus !== 'proposed';
      });
    }

    /* ---------------- 3D overlay: markers + terrain-following outline */

    const group = new THREE.Group();
    group.renderOrder = 998;
    viewer.scene.add(group);
    const vertexMat = new THREE.MeshBasicMaterial({ color: 0xf2c14e });
    const pointMat = new THREE.MeshBasicMaterial({ color: 0x4ea8de });
    const lineMat = new THREE.LineBasicMaterial({ color: 0x6fcf97 });
    let outline = null;
    const markers = [];

    // world y at a scene-local (x, yNorth), hugging the terrain
    function groundY(x, yNorth) {
      return global.VEILTerrain.sampleTerrainHeightAtLocal(grid, x, yNorth) + 1.2;
    }

    function addMarker(x, yNorth, mat, r) {
      const m = new THREE.Mesh(new THREE.SphereGeometry(r, 14, 10), mat);
      m.position.set(x, groundY(x, yNorth), -yNorth);
      m.renderOrder = 999;
      group.add(m);
      markers.push(m);
      return m;
    }

    function rebuildOutline() {
      if (outline) { group.remove(outline); outline.geometry.dispose(); outline = null; }
      const v = state.vertices;
      if (v.length < 2) return;
      const ring = v.length >= 3 ? v.concat([v[0]]) : v;
      const pts = [];
      for (let i = 1; i < ring.length; i += 1) {
        const [x1, y1] = ring[i - 1];
        const [x2, y2] = ring[i];
        const steps = Math.max(1, Math.ceil(Math.hypot(x2 - x1, y2 - y1) / 8));
        for (let s = 0; s <= steps; s += 1) {
          const x = x1 + ((x2 - x1) * s) / steps;
          const y = y1 + ((y2 - y1) * s) / steps;
          pts.push(new THREE.Vector3(x, groundY(x, y), -y));
        }
      }
      outline = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), lineMat);
      outline.renderOrder = 999;
      group.add(outline);
    }

    function clearOverlay() {
      markers.forEach((m) => { group.remove(m); m.geometry.dispose(); });
      markers.length = 0;
      if (outline) { group.remove(outline); outline.geometry.dispose(); outline = null; }
    }

    /* ---------------- selection state */

    function polygonAreaHa(v) {
      let a = 0;
      for (let i = 0; i < v.length; i += 1) {
        const [x1, y1] = v[(i + v.length - 1) % v.length];
        const [x2, y2] = v[i];
        a += x1 * y2 - x2 * y1;
      }
      return Math.abs(a) / 2 / 10000;
    }

    function setScope(scope) {
      state.scope = scope;
      renderScope();
    }

    function renderScope() {
      const n = state.vertices.length;
      if (state.mode === 'draw') {
        els.scope.textContent = n < 3
          ? `Drawing region — click the terrain (${n}/3 points minimum)`
          : `Drawing region — ${n} points (${polygonAreaHa(state.vertices).toFixed(2)} ha); click "Finish region" or just ask`;
      } else if (state.mode === 'pick') {
        els.scope.textContent = 'Pick a point — click the terrain';
      } else if (state.scope === 'region') {
        els.scope.textContent =
          `Scope: drawn region — ${n} points, ${polygonAreaHa(state.vertices).toFixed(2)} ha`;
      } else if (state.scope === 'point') {
        const g = georef.worldToGeo(state.point.x, 0, -state.point.y);
        els.scope.textContent =
          `Scope: point ${g.lat.toFixed(5)}, ${g.lon.toFixed(5)} (+${POINT_RADIUS_M} m around it)`;
      } else {
        els.scope.textContent = 'Scope: the whole land';
      }
      els.draw.textContent = state.mode === 'draw' ? 'Finish region' : 'Draw region';
      els.draw.classList.toggle('active', state.mode === 'draw');
      els.pick.classList.toggle('active', state.mode === 'pick');
      els.clear.disabled = state.scope === 'all' && !state.vertices.length && !state.point;
    }

    function clearSelection(noteText) {
      state.vertices = [];
      state.point = null;
      state.mode = null;
      clearOverlay();
      setScope('all');
      if (noteText) note(noteText);
    }

    function startDraw() {
      if (state.mode === 'draw') { // finish
        state.mode = null;
        if (state.vertices.length >= 3) {
          setScope('region');
          note(`Region selected (${state.vertices.length} points, ` +
               `${polygonAreaHa(state.vertices).toFixed(2)} ha). Questions now focus on it.`);
        } else {
          clearSelection('A region needs at least 3 points — cleared.');
        }
        renderScope();
        return;
      }
      // entering draw mode replaces any current selection
      state.point = null;
      state.vertices = [];
      clearOverlay();
      state.scope = 'all';
      state.mode = 'draw';
      renderScope();
    }

    function startPick() {
      if (state.mode === 'pick') { state.mode = null; renderScope(); return; }
      state.vertices = [];
      state.point = null;
      clearOverlay();
      state.scope = 'all';
      state.mode = 'pick';
      renderScope();
    }

    /* ---------------- terrain clicks (capture phase, same raycast as readout) */

    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    let downAt = null;

    function terrainHit(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, viewer.camera);
      return raycaster.intersectObject(viewer.terrainMesh, false)[0] || null;
    }

    // Plain listeners; while a mode is active the readout's own pick handler
    // stands down (it checks __twin.chat.state.mode), so clicks aren't
    // double-handled and OrbitControls dragging keeps working.
    canvas.addEventListener('pointerdown', (e) => {
      if (state.mode) downAt = { x: e.clientX, y: e.clientY };
    });

    canvas.addEventListener('pointerup', (e) => {
      if (!state.mode || !downAt) return;
      const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
      downAt = null;
      if (moved >= 5) return; // it was a camera drag, not a click
      const hit = terrainHit(e.clientX, e.clientY);
      if (!hit) return;
      const x = Math.round(hit.point.x * 100) / 100;
      const y = Math.round(-hit.point.z * 100) / 100; // scene-local north
      if (state.mode === 'draw') {
        state.vertices.push([x, y]);
        addMarker(x, y, vertexMat, 1.8);
        rebuildOutline();
        renderScope();
      } else if (state.mode === 'pick') {
        state.point = { x, y };
        addMarker(x, y, pointMat, 2.4);
        state.mode = null;
        setScope('point');
        const g = georef.worldToGeo(x, 0, -y);
        note(`Point selected at ${g.lat.toFixed(6)}, ${g.lon.toFixed(6)} — ` +
             `answers load ~${POINT_RADIUS_M} m of context around it.`);
      }
    });

    /* ---------------- transcript */

    const esc = (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

    // minimal markdown: bold + bullet lines + paragraphs
    function renderText(text) {
      return esc(text)
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/^[-•] (.*)$/gm, '<span class="chat-li">• $1</span>')
        .replace(/\n/g, '<br>');
    }

    function bubble(cls, html) {
      const div = document.createElement('div');
      div.className = `chat-msg ${cls}`;
      div.innerHTML = html;
      els.messages.appendChild(div);
      els.messages.scrollTop = els.messages.scrollHeight;
      return div;
    }

    function note(text) {
      bubble('note', esc(text));
    }

    function previewRows(proposal) {
      const preview = proposal?.preview || {};
      const vegetation = preview.vegetation || {};
      const terrain = preview.terrain || {};
      const rows = [];
      const edits = Number(preview.edit_count);
      const removed = Number(vegetation.entities_removed);
      const trees = Number(vegetation.trees_added);
      const shrubs = Number(vegetation.shrubs_added);
      const cut = Number(terrain.estimated_cut_m3);
      const fill = Number(terrain.estimated_fill_m3);
      if (Number.isFinite(edits)) rows.push(`${edits.toLocaleString()} proposed edits`);
      if (Number.isFinite(removed) && removed > 0) {
        rows.push(`${removed.toLocaleString()} existing plants resolved for removal`);
      }
      if ((Number.isFinite(trees) && trees > 0) || (Number.isFinite(shrubs) && shrubs > 0)) {
        rows.push(`+${(trees || 0).toLocaleString()} trees · +${(shrubs || 0).toLocaleString()} shrubs`);
      }
      if (Number.isFinite(cut) || Number.isFinite(fill)) {
        rows.push(`${Math.round(cut || 0).toLocaleString()} m³ cut · ${Math.round(fill || 0).toLocaleString()} m³ fill`);
      }
      return rows;
    }

    function renderPlanProposal(proposal) {
      if (!proposal?.proposal_id) return null;
      const id = String(proposal.proposal_id);
      const old = state.proposalCards.get(id);
      if (old) old.remove();
      const status = proposal.status === 'applied' ? 'applied' : 'proposed';
      const label = proposal.label || 'GAIA plan proposal';
      const rows = previewRows(proposal);
      const card = bubble('bot plan-proposal', `
        <div class="plan-proposal-head">
          <span>G.A.I.A. plan proposal</span>
          <span class="plan-proposal-status ${status}">${status === 'applied' ? 'applied' : 'preview · not applied'}</span>
        </div>
        <strong class="plan-proposal-title">${esc(label)}</strong>
        <div class="plan-proposal-stats">${rows.map((row) => `<span>${esc(row)}</span>`).join('')}</div>
        <div class="plan-proposal-note">The baseline remains untouched. Review the live difference in Plan before approving.</div>
        <button type="button" class="plan-proposal-approve"
          data-plan-proposal-approve="${esc(id)}" data-proposal-status="${status}">
          ${status === 'applied' ? 'Approved · immutable revision' : 'Approve as new revision'}
        </button>`);
      state.proposalCards.set(id, card);
      const button = card.querySelector('[data-plan-proposal-approve]');
      button.disabled = status !== 'proposed' || state.busy;
      button.addEventListener('click', () => {
        if (state.busy || button.dataset.proposalStatus !== 'proposed') return;
        button.disabled = true;
        sendMessage(`Apply ${id}`, { displayText: `Approve “${label}”` });
      });
      return card;
    }

    function markPlanProposalApplied(proposal, revision = null) {
      const id = String(proposal?.proposal_id || proposal || '');
      const card = state.proposalCards.get(id);
      if (!card) return null;
      const status = card.querySelector('.plan-proposal-status');
      const button = card.querySelector('[data-plan-proposal-approve]');
      status.textContent = 'applied';
      status.className = 'plan-proposal-status applied';
      button.textContent = 'Approved · immutable revision';
      button.dataset.proposalStatus = 'applied';
      button.disabled = true;
      const revisionId = revision?.revision_id || proposal?.applied_revision_id;
      if (revisionId) {
        const detail = document.createElement('div');
        detail.className = 'plan-proposal-revision';
        detail.textContent = `Immutable revision ${revisionId}`;
        card.appendChild(detail);
      }
      return card;
    }

    function handlePlanTrace(trace) {
      planActionsFromTrace(trace).forEach((action) => {
        if (action.type === 'proposal') renderPlanProposal(action.proposal);
        else markPlanProposalApplied(action.proposal, action.revision);
      });
    }

    function traceLine(t) {
      const args = JSON.stringify(t.args || {});
      bubble('trace', `⚙ ${esc(t.tool)} ${esc(args.length > 120 ? args.slice(0, 120) + '…' : args)}`);
    }

    const veilToolPhrases = [
      'G.A.I.A. slips a probe through the VEIL',
      'G.A.I.A. opens a quiet instrument drawer',
      'The VEIL threads a field tool into place',
      'G.A.I.A. asks the twin for a reading',
    ];
    const veilLayerPhrases = [
      'The VEIL brushes this atlas thread',
      'G.A.I.A. lifts a layer of ground memory',
      'The terrain ledger opens to',
      'A map stratum comes into view',
    ];

    function formatToolArgs(args) {
      const raw = JSON.stringify(args || {});
      return raw.length > 96 ? `${raw.slice(0, 96)}…` : raw;
    }

    function statusTextForEvent(event, index) {
      if (!event || event.type === 'pulse') return '';
      if (event.type === 'tool_call' && event.tool) {
        const phrase = veilToolPhrases[index % veilToolPhrases.length];
        return `${phrase}: ${event.tool} ${formatToolArgs(event.args)}`;
      }
      if (event.type === 'tool_result' && event.layers && event.layers.length) {
        const phrase = veilLayerPhrases[index % veilLayerPhrases.length];
        return `${phrase}: ${event.layers.slice(0, 4).join(', ')}`;
      }
      return '';
    }

    function updateVeilStatus(pending, event) {
      if (!pending) return;
      if (!pending._veilLines) pending._veilLines = [];
      const line = statusTextForEvent(event, pending._veilLines.length);
      if (line && !pending._veilLines.includes(line)) {
        pending._veilLines.push(line);
        pending._veilLines = pending._veilLines.slice(-7);
      }
      pending.innerHTML = [
        '<div class="veil-lift">lifting the VEIL</div>',
        ...pending._veilLines.map((l) => `<div class="veil-lift-line">${esc(l)}</div>`),
      ].join('');
      els.messages.scrollTop = els.messages.scrollHeight;
    }

    function buildScopePayload() {
      if (state.scope === 'region' && state.vertices.length >= 3) {
        return { type: 'region', polygon: state.vertices };
      }
      if (state.scope === 'point' && state.point) {
        return { type: 'point', point: state.point, radius_m: POINT_RADIUS_M };
      }
      return { type: 'all' };
    }

    async function readChatResponse(res) {
      const text = await res.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : {};
      } catch (_err) {
        const detail = text.trim().slice(0, 300);
        throw new Error(detail
          ? `Chat server returned HTTP ${res.status}: ${detail}`
          : `Chat server returned HTTP ${res.status} with an empty response`);
      }
      if (!res.ok && !data.error) {
        data.error = `Chat server returned HTTP ${res.status}`;
      }
      return data;
    }

    async function readChatStreamResponse(res, pending) {
      if (!res.body || !/application\/x-ndjson/i.test(res.headers.get('content-type') || '')) {
        const data = await readChatResponse(res);
        data.streamed = false;
        return data;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let final = null;
      let streamError = null;
      while (true) {
        const { value, done } = await reader.read();
        if (value) buf += decoder.decode(value, { stream: !done });
        const lines = buf.split(/\n/);
        buf = lines.pop() || '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          let event;
          try { event = JSON.parse(trimmed); } catch (_err) { continue; }
          if (event.type === 'final') {
            final = event;
          } else if (event.type === 'error') {
            streamError = event.error || 'Chat failed while lifting the VEIL.';
          } else {
            updateVeilStatus(pending, event);
          }
        }
        if (done) break;
      }
      if (buf.trim()) {
        try {
          const event = JSON.parse(buf.trim());
          if (event.type === 'final') final = event;
          else if (event.type === 'error') streamError = event.error || 'Chat failed while lifting the VEIL.';
          else updateVeilStatus(pending, event);
        } catch (_err) { /* ignore partial tail */ }
      }
      if (streamError) return { error: streamError, streamed: true };
      if (!final) return { error: `Chat server returned HTTP ${res.status} without a final answer`, streamed: true };
      final.streamed = true;
      return final;
    }

    async function sendMessage(text, options = {}) {
      state.history.push({ role: 'user', content: text });
      const userBubble = bubble('user', renderText(options.displayText || text));
      const pending = bubble('bot pending', '<div class="veil-lift">lifting the VEIL</div>');
      setBusy(true);
      const rollbackFailedSend = (message) => {
        pending.remove();
        state.history.pop(); // let the user retry the same question
        userBubble.remove();
        bubble('error', esc(message));
      };
      let data;
      try {
        const headers = {
          'Content-Type': 'application/json',
          Accept: 'application/x-ndjson',
          'X-Veil-Stream': '1',
        };
        const userKey = getKey();
        if (userKey) headers['X-OpenAI-Key'] = userKey;
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers,
          body: JSON.stringify({ messages: state.history, scope: buildScopePayload() }),
        });
        data = await readChatStreamResponse(res, pending);
      } catch (err) {
        rollbackFailedSend('Chat failed: ' + (err?.message || err));
        return;
      } finally {
        setBusy(false);
      }
      if (data.error) {
        rollbackFailedSend(String(data.error));
        return;
      }
      pending.remove();
      if (!data.streamed) (data.trace || []).forEach(traceLine);
      state.history.push({ role: 'assistant', content: data.reply });
      bubble('bot', renderText(data.reply || '(empty reply)'));
      handlePlanTrace(data.trace);
      // the model may have drawn on the map mid-answer; show it now
      // instead of waiting for the annotations poll
      global.__twin?.annotations?.refresh();
    }

    /* ---------------- wire up */

    els.draw.addEventListener('click', startDraw);
    els.pick.addEventListener('click', startPick);
    els.clear.addEventListener('click', () => clearSelection('Selection cleared — back to the whole land.'));
    els.key.addEventListener('click', promptKey);
    els.form.addEventListener('submit', (e) => {
      e.preventDefault();
      const text = els.input.value.trim();
      if (!text || state.busy) return;
      // asking mid-draw means "use what I drew": auto-finish the region
      // (3+ points) instead of silently falling back to whole-land scope
      if (state.mode === 'draw') {
        startDraw();
      } else if (state.mode === 'pick') {
        state.mode = null; // no point was picked; drop back to whole land
        renderScope();
      }
      els.input.value = '';
      sendMessage(text);
    });
    els.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (state.busy) return;
        els.form.requestSubmit();
      }
    });

    renderScope();
    renderKeyButton();

    // Ask the server which provider is active: a local model (Ollama) needs no
    // key, so hide the "Key" button and name the model in the intro.
    fetch('/api/chat/config').then((r) => r.json()).then((cfg) => {
      if (cfg && cfg.needs_key === false) {
        els.key.style.display = 'none';
        note(`Running locally on ${cfg.model} — no API key needed.`);
      }
    }).catch(() => { /* leave the OpenAI/BYOK defaults in place */ });

    return {
      state,
      clearSelection,
      // exposed for headless tests: add a vertex / pick as if clicked
      _addVertex(x, y) {
        state.vertices.push([x, y]);
        addMarker(x, y, vertexMat, 1.8);
        rebuildOutline();
        renderScope();
      },
      _setPoint(x, y) {
        state.point = { x, y };
        addMarker(x, y, pointMat, 2.4);
        setScope('point');
      },
      _setMode(m) { state.mode = m; renderScope(); },
      _send: sendMessage,
      _renderPlanProposal: renderPlanProposal,
      _markPlanProposalApplied: markPlanProposalApplied,
    };
  }

  global.VEILChat = { create, planActionsFromTrace };
})(typeof window !== 'undefined' ? window : globalThis);
