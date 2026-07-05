/* VEIL new-twin setup — shell enhancement.
   /init.js still owns map drawing, layer scans, and build orchestration. This
   layer reflects that state into the stepper/live feed and adds system + AOI
   safety checks around the init flow. */
(function () {
  const $ = (id) => document.getElementById(id);

  const hint = $('map-hint');
  const pointCount = $('point-count');
  const setButton = $('set-aoi');
  const undoButton = $('undo-point');
  const clearButton = $('clear-aoi');
  const statusLabel = $('status-label');
  const statusPill = $('status-pill');
  const viewerLink = $('viewer-link');
  const log = $('log');
  const dialog = $('layer-dialog');
  const steps = Array.from(document.querySelectorAll('.step'));
  const manifestItems = Array.from(document.querySelectorAll('#manifest-list li'));

  // system safety status
  const systemState = $('system-check-state');
  const systemDisk = $('system-disk');
  const systemRam = $('system-ram');
  const systemGdal = $('system-gdal');
  const systemWarning = $('system-warning');

  // AOI disk estimate
  const aoiEstimate = $('aoi-estimate');
  const aoiEstimateSize = $('aoi-estimate-size');
  const aoiEstimateDetail = $('aoi-estimate-detail');
  const aoiEstimateWarning = $('aoi-estimate-warning');
  const buildWithoutLayers = $('build-without-layers');
  const buildWithLayers = $('build-with-layers');

  // scan feed
  const scanCard = $('scan-feed') && document.querySelector('.scan-card');
  const scanFeed = $('scan-feed');
  const scanDone = $('scan-done');
  const scanTotal = $('scan-total');
  const scanBar = $('scan-bar-fill');
  const scanSummary = $('scan-summary');

  // build feedback
  const buildCurrent = $('build-current');
  const buildElapsed = $('build-elapsed');

  const STEP_ORDER = ['locate', 'layers', 'build'];
  const WORLD_BOUNDS = {
    minLat: -60,
    maxLat: 85,
    minLon: -180,
    maxLon: 180,
  };
  const rawFetch = window.fetch ? window.fetch.bind(window) : null;

  function statusText() {
    return (statusLabel ? statusLabel.textContent : '').trim();
  }
  function dialogOpen() {
    return !!dialog && (dialog.open || dialog.hasAttribute('open'));
  }
  function formatBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n < 0) return 'unknown';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = n;
    let idx = 0;
    while (value >= 1024 && idx < units.length - 1) {
      value /= 1024;
      idx += 1;
    }
    return idx === 0 ? `${Math.round(value)} ${units[idx]}` : `${value.toFixed(1)} ${units[idx]}`;
  }
  function formatArea(areaKm2) {
    const n = Number(areaKm2);
    if (!Number.isFinite(n) || n <= 0) return 'unknown area';
    if (n < 0.01) return `${(n * 100).toFixed(2)} ha`;
    if (n < 1) return `${n.toFixed(3)} km2`;
    return `${n.toFixed(1)} km2`;
  }
  function setSystemState(text, cls) {
    if (!systemState) return;
    systemState.textContent = text;
    systemState.classList.toggle('is-ok', cls === 'ok');
    systemState.classList.toggle('is-warn', cls === 'warn');
    systemState.classList.toggle('is-error', cls === 'error');
  }
  function renderSystemCheck(payload) {
    const warnings = Array.isArray(payload && payload.warnings) ? payload.warnings : [];
    if (systemDisk) {
      const free = payload && payload.disk ? payload.disk.free_bytes : null;
      systemDisk.textContent = Number.isFinite(Number(free)) ? `${formatBytes(free)} free` : 'Unknown';
    }
    if (systemRam) {
      const mem = payload && payload.memory ? payload.memory : {};
      const free = Number(mem.free_bytes);
      const total = Number(mem.total_bytes);
      systemRam.textContent = Number.isFinite(free) && Number.isFinite(total)
        ? `${formatBytes(free)} / ${formatBytes(total)}` : 'Unknown';
    }
    if (systemGdal) {
      const gdal = payload && payload.tools ? payload.tools.gdal : null;
      systemGdal.textContent = gdal && gdal.ok ? (gdal.version || 'OK') : 'Missing';
    }
    if (!payload || payload.ok === false) setSystemState('Check failed', 'error');
    else setSystemState(warnings.length ? 'Review' : 'OK', warnings.length ? 'warn' : 'ok');
    if (systemWarning) {
      systemWarning.textContent = warnings.join(' ');
      systemWarning.hidden = warnings.length === 0;
    }
  }
  async function loadSystemCheck() {
    if (!rawFetch || !systemState) return;
    setSystemState('Checking', '');
    try {
      const res = await rawFetch('/api/system-check');
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || 'system check unavailable');
      renderSystemCheck(payload);
    } catch (err) {
      setSystemState('Unavailable', 'error');
      if (systemWarning) {
        systemWarning.textContent = err.message || 'System check unavailable';
        systemWarning.hidden = false;
      }
    }
  }

  let latestEstimate = null;
  function syncBuildButtonsWithEstimate() {
    const blocked = !!(latestEstimate && (latestEstimate.blocked || latestEstimate.ok === false));
    const advisory = latestEstimate && latestEstimate.advisory ? latestEstimate.advisory : '';
    if (setButton) {
      if (blocked) setButton.disabled = true;
      setButton.title = blocked ? advisory : '';
    }
    [buildWithoutLayers, buildWithLayers].forEach((button) => {
      if (!button) return;
      if (blocked) button.disabled = true;
      button.title = blocked ? advisory : '';
    });
  }
  function renderEstimate(payload, pending) {
    if (!aoiEstimate) return;
    if (!payload && !pending) {
      latestEstimate = null;
      aoiEstimate.hidden = true;
      aoiEstimate.classList.remove('is-warn', 'is-blocked');
      syncBuildButtonsWithEstimate();
      return;
    }
    aoiEstimate.hidden = false;
    if (pending) {
      aoiEstimate.classList.remove('is-warn', 'is-blocked');
      if (aoiEstimateSize) aoiEstimateSize.textContent = 'Estimating';
      if (aoiEstimateDetail) aoiEstimateDetail.textContent = 'Checking disk footprint for this AOI.';
      if (aoiEstimateWarning) aoiEstimateWarning.hidden = true;
      return;
    }
    latestEstimate = payload;
    const blocked = !!(payload.blocked || payload.ok === false);
    const hasWarning = !!payload.advisory;
    aoiEstimate.classList.toggle('is-warn', hasWarning && !blocked);
    aoiEstimate.classList.toggle('is-blocked', blocked);
    if (aoiEstimateSize) {
      aoiEstimateSize.textContent = Number.isFinite(Number(payload.est_bytes))
        ? formatBytes(payload.est_bytes) : 'Unknown';
    }
    if (aoiEstimateDetail) {
      const freeAfter = Number(payload.projected_free_bytes);
      const freeNow = Number(payload.disk_free_bytes);
      const freeText = Number.isFinite(freeAfter)
        ? `${formatBytes(Math.max(0, freeAfter))} free after build`
        : Number.isFinite(freeNow) ? `${formatBytes(freeNow)} free now` : 'disk space unknown';
      aoiEstimateDetail.textContent = `${formatArea(payload.area_km2)} AOI; ${freeText}.`;
    }
    if (aoiEstimateWarning) {
      aoiEstimateWarning.textContent = payload.advisory || '';
      aoiEstimateWarning.hidden = !payload.advisory;
    }
    syncBuildButtonsWithEstimate();
  }

  let trackedPoints = [];
  let estimateTimer = null;
  let estimateSeq = 0;

  function pointInsideSupportedMap(point) {
    const lat = Number(point && point.lat);
    const lng = Number(point && point.lng);
    return Number.isFinite(lat) && Number.isFinite(lng)
      && lat >= WORLD_BOUNDS.minLat && lat <= WORLD_BOUNDS.maxLat
      && lng >= WORLD_BOUNDS.minLon && lng <= WORLD_BOUNDS.maxLon;
  }
  function estimateCoordinates() {
    return trackedPoints.map((point) => [
      Number(point.lng.toFixed(7)),
      Number(point.lat.toFixed(7)),
    ]);
  }
  async function requestEstimate(coordinates) {
    if (!rawFetch || !Array.isArray(coordinates) || coordinates.length < 3) {
      renderEstimate(null);
      return;
    }
    const seq = ++estimateSeq;
    renderEstimate(null, true);
    try {
      const res = await rawFetch('/api/init-estimate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ coordinates }),
      });
      const payload = await res.json();
      if (seq !== estimateSeq) return;
      if (!res.ok) throw new Error(payload.error || 'estimate failed');
      renderEstimate(payload);
    } catch (err) {
      if (seq !== estimateSeq) return;
      renderEstimate({
        ok: true,
        blocked: false,
        area_km2: null,
        est_bytes: null,
        disk_free_bytes: null,
        projected_free_bytes: null,
        advisory: `Build size could not be estimated: ${err.message || err}. Keep the AOI small.`,
      });
    }
  }
  function scheduleEstimate() {
    clearTimeout(estimateTimer);
    if (trackedPoints.length < 3) {
      estimateSeq += 1;
      renderEstimate(null);
      return;
    }
    estimateTimer = setTimeout(() => requestEstimate(estimateCoordinates()), 250);
  }
  function replaceTrackedCoordinates(coordinates) {
    if (!Array.isArray(coordinates)) return;
    trackedPoints = coordinates
      .filter((point) => Array.isArray(point) && point.length >= 2)
      .map((point) => ({ lng: Number(point[0]), lat: Number(point[1]) }))
      .filter((point) => Number.isFinite(point.lng) && Number.isFinite(point.lat));
    scheduleEstimate();
  }
  function installAoiTracker() {
    if (window.L && window.L.Map && !window.L.Map.prototype._veilSafetyHooked) {
      const originalFire = window.L.Map.prototype.fire;
      window.L.Map.prototype.fire = function patchedFire(type, data, propagate) {
        if (type === 'click' && data && pointInsideSupportedMap(data.latlng)) {
          trackedPoints.push({ lat: Number(data.latlng.lat), lng: Number(data.latlng.lng) });
          scheduleEstimate();
        }
        return originalFire.call(this, type, data, propagate);
      };
      window.L.Map.prototype._veilSafetyHooked = true;
    }
    if (undoButton) {
      undoButton.addEventListener('click', () => {
        if (trackedPoints.length) trackedPoints.pop();
        scheduleEstimate();
      });
    }
    if (clearButton) {
      clearButton.addEventListener('click', () => {
        trackedPoints = [];
        scheduleEstimate();
      });
    }
  }
  function fetchPath(input) {
    const raw = typeof input === 'string' ? input : input && input.url;
    if (!raw) return '';
    try { return new URL(raw, window.location.href).pathname; } catch (_err) { return ''; }
  }
  function requestCoordinatesFromInit(init) {
    const body = init && init.body;
    if (typeof body !== 'string') return null;
    try {
      const payload = JSON.parse(body);
      return Array.isArray(payload.coordinates) ? payload.coordinates : null;
    } catch (_err) {
      return null;
    }
  }
  function installFetchObserver() {
    if (!rawFetch || window.fetch._veilSafetyObserved) return;
    window.fetch = async function observedFetch(input, init) {
      const pathname = fetchPath(input);
      const coordinates = requestCoordinatesFromInit(init);
      if (coordinates && (
        pathname === '/api/init-layer-scan'
        || pathname === '/api/init-layer-scan-stream'
        || pathname === '/api/init-aoi'
      )) {
        replaceTrackedCoordinates(coordinates);
      }
      const response = await rawFetch(input, init);
      if (pathname === '/api/init-aoi') {
        response.clone().json().then((payload) => {
          const estimate = payload && payload.estimate ? payload.estimate : payload;
          if (estimate && (estimate.est_bytes || estimate.advisory)) renderEstimate(estimate);
        }).catch(() => {});
      }
      return response;
    };
    window.fetch._veilSafetyObserved = true;
  }

  function readPhase() {
    const s = statusText().toLowerCase();
    const complete = s.includes('complete') || (viewerLink && !viewerLink.hidden);
    const running = s.includes('building') || s.includes('starting build');
    const error = s.includes('failed');
    let phase = 'locate';
    if (complete || running) phase = 'build';
    else if (dialogOpen() || s.includes('scanning')) phase = 'layers';
    return { phase, complete, running, error };
  }

  function syncStepper(state) {
    const active = STEP_ORDER.indexOf(state.phase);
    steps.forEach((li, i) => {
      const done = i < active || (state.complete && i <= active);
      li.classList.toggle('is-done', done);
      li.classList.toggle('is-active', i === active && !state.complete);
    });
  }

  function syncPill(state) {
    if (!statusPill) return;
    statusPill.classList.toggle('is-running', state.running && !state.complete && !state.error);
    statusPill.classList.toggle('is-done', state.complete);
    statusPill.classList.toggle('is-error', state.error && !state.complete);
    document.body.classList.toggle('is-building', state.running || state.complete);
  }

  function syncManifest(state) {
    if (!log) return;
    const text = (log.textContent || '').toLowerCase();
    let firstPending = -1;
    manifestItems.forEach((li, i) => {
      const re = li.getAttribute('data-match') || '';
      const matched = state.complete || (text && new RegExp(re).test(text));
      li.classList.toggle('is-done', !!matched);
      li.classList.remove('is-active');
      if (!matched && firstPending === -1) firstPending = i;
    });
    if (state.running && !state.complete && firstPending >= 0) {
      manifestItems[firstPending].classList.add('is-active');
    }
  }

  function syncHint() {
    if (!hint || !pointCount) return;
    const drawn = !/^0\b/.test((pointCount.textContent || '').trim());
    hint.classList.toggle('is-hidden', drawn);
  }

  // ---- build "current step" caption + elapsed timer --------------------
  let buildStartMs = null;
  function lastLogLine() {
    const lines = (log && log.textContent ? log.textContent : '').split('\n').filter((l) => l.trim());
    return lines.length ? lines[lines.length - 1] : '';
  }
  function fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
  function syncBuild(state) {
    if (state.running && buildStartMs === null) buildStartMs = Date.now();
    if (!state.running && !state.complete) buildStartMs = null;

    if (buildCurrent) {
      const line = state.running ? lastLogLine() : '';
      buildCurrent.textContent = line;
      buildCurrent.hidden = !line;
    }
    if (buildElapsed) {
      buildElapsed.textContent = (buildStartMs !== null && (state.running || state.complete))
        ? `· ${fmtElapsed(Date.now() - buildStartMs)}` : '';
    }
  }

  function syncAll() {
    const state = readPhase();
    syncStepper(state);
    syncPill(state);
    syncManifest(state);
    syncHint();
    syncBuild(state);
    syncBuildButtonsWithEstimate();
  }

  // ---- live scan feed (driven by /init.js's veil-scan events) ----------
  let scanCount = 0;
  let scanHits = 0;

  function resetScan(total) {
    scanCount = 0;
    scanHits = 0;
    if (scanFeed) scanFeed.textContent = '';
    if (scanDone) scanDone.textContent = '0';
    if (scanTotal) scanTotal.textContent = String(total || 0);
    if (scanBar) scanBar.style.width = '0%';
    if (scanSummary) scanSummary.textContent = '';
    if (scanCard) scanCard.hidden = false;
  }

  function badgeFor(layer) {
    const status = layer.status;
    if (status === 'file_download' || status === 'big_download') {
      const label = layer.download_class ? `${layer.download_class} download` : 'download';
      return { cls: 'manual', text: layer.download_size ? `${label} ${layer.download_size}` : label };
    }
    if (status === 'downloadable') return { cls: 'manual', text: 'download later' };
    if (status === 'manual' || status === 'not_interactive') return { cls: 'manual', text: 'manual source' };
    if (status === 'error') return { cls: 'err', text: 'error' };
    if (status === 'ok' && layer.intersects) {
      const n = layer.feature_count;
      return { cls: 'hit', text: typeof n === 'number' ? `${n.toLocaleString()} feature${n === 1 ? '' : 's'}` : 'coverage' };
    }
    if (status === 'ok') return { cls: 'miss', text: 'no features' };
    return { cls: 'miss', text: status || 'skipped' };
  }

  function addScanRow(layer) {
    if (!scanFeed) return;
    const row = document.createElement('li');
    row.className = 'scan-row';
    const name = document.createElement('span');
    name.className = 'scan-name';
    name.textContent = layer.label || layer.id || 'layer';
    if (layer.category) {
      const cat = document.createElement('span');
      cat.className = 'scan-cat';
      cat.textContent = layer.category;
      name.append(' ', cat);
    }
    const badge = badgeFor(layer);
    const b = document.createElement('span');
    b.className = `scan-badge ${badge.cls}`;
    b.textContent = badge.text;
    row.append(name, b);
    scanFeed.appendChild(row);
    if (badge.cls === 'hit') scanHits += 1;
    scanCount += 1;
    if (scanDone) scanDone.textContent = String(scanCount);
    const total = Number(scanTotal && scanTotal.textContent) || 0;
    if (scanBar && total) scanBar.style.width = `${Math.min(100, Math.round((scanCount / total) * 100))}%`;
  }

  function finishScan() {
    if (scanBar) scanBar.style.width = '100%';
    if (scanSummary) {
      scanSummary.textContent = scanHits
        ? `${scanHits} optional layer${scanHits === 1 ? '' : 's'} intersect this area — choose which to import.`
        : 'No optional national layers reported features here. The base twin still builds.';
    }
  }

  installAoiTracker();
  installFetchObserver();
  loadSystemCheck();

  window.addEventListener('veil-scan', (e) => {
    const d = e.detail || {};
    if (d.type === 'start') {
      document.body.classList.add('is-scanning');
      resetScan(d.total);
    } else if (d.type === 'layer' && d.layer) {
      addScanRow(d.layer);
    } else if (d.type === 'fallback') {
      if (scanCard) scanCard.hidden = false;
      if (scanSummary) scanSummary.textContent = 'Live scan unavailable — checking all layers in one pass…';
    } else if (d.type === 'done') {
      document.body.classList.remove('is-scanning');
      finishScan();
    }
    syncAll();
  });

  // React to the DOM mutations /init.js makes, instead of duplicating its polling.
  const mo = new MutationObserver(syncAll);
  [statusLabel, log, pointCount].forEach((el) => {
    if (el) mo.observe(el, { childList: true, characterData: true, subtree: true });
  });
  if (dialog) mo.observe(dialog, { attributes: true, attributeFilter: ['open'] });
  if (viewerLink) mo.observe(viewerLink, { attributes: true, attributeFilter: ['hidden'] });

  // Heartbeat: covers state changes that don't mutate a watched node, and keeps
  // the elapsed timer ticking during a build.
  setInterval(syncAll, 1000);
  syncAll();
}());
