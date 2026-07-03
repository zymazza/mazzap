/* Browser-side atlas upload. The server runs scripts/add_layer.py, which clips
   to the active twin footprint and emits the drape assets the viewer already
   knows how to render. */
(function layerUpload() {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const form = $('layer-upload-form');
  if (!form) return;

  const fileInput = $('layer-upload-files');
  const fileLabel = $('layer-upload-files-label');
  const dropzone = $('layer-dropzone');
  const nameInput = $('layer-upload-label');
  const crsInput = $('layer-upload-src-crs');
  const submit = $('layer-upload-submit');
  const status = $('layer-upload-status');
  let selectedFiles = [];

  function setStatus(text, tone) {
    status.textContent = text || '';
    status.className = `layer-upload-status${tone ? ` ${tone}` : ''}`;
  }

  function fileList(files) {
    selectedFiles = Array.from(files || []).filter((f) => f && f.name);
    if (!selectedFiles.length) {
      fileLabel.textContent = 'GeoTIFF, GeoJSON, GPKG, Shapefile, KML, GPX, CSV';
      return;
    }
    const names = selectedFiles.map((f) => f.name);
    fileLabel.textContent = names.length > 2
      ? `${names.slice(0, 2).join(', ')} +${names.length - 2}`
      : names.join(', ');
    if (!nameInput.value.trim()) {
      const first = selectedFiles.find((f) => /\.(gpkg|geojson|json|tiff?|shp|kml|kmz|gpx|csv|zip)$/i.test(f.name))
        || selectedFiles[0];
      nameInput.value = first.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
    }
  }

  fileInput.addEventListener('change', () => fileList(fileInput.files));

  ['dragenter', 'dragover'].forEach((type) => {
    dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.add('dragging');
    });
  });
  ['dragleave', 'drop'].forEach((type) => {
    dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragging');
    });
  });
  dropzone.addEventListener('drop', (e) => fileList(e.dataTransfer?.files));

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!selectedFiles.length) {
      setStatus('Choose geospatial files first.', 'err');
      return;
    }
    const body = new FormData();
    selectedFiles.forEach((file) => body.append('files', file, file.name));
    const label = nameInput.value.trim();
    const srcCrs = crsInput.value.trim();
    if (label) body.append('label', label);
    if (srcCrs) body.append('src_crs', srcCrs);

    submit.disabled = true;
    setStatus('Uploading and clipping layer…');
    try {
      const res = await fetch('/api/layers/upload', { method: 'POST', body });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || !payload.ok) {
        throw new Error(payload.error || `upload failed (${res.status})`);
      }
      await window.__twin?.refreshAtlasLayers?.({
        catalog: payload.atlas,
        enableIds: [payload.layer_id],
      });
      setStatus(`Added ${payload.label || payload.layer_id}.`, 'ok');
      selectedFiles = [];
      fileInput.value = '';
      crsInput.value = '';
      fileList([]);
    } catch (err) {
      setStatus(err.message || String(err), 'err');
    } finally {
      submit.disabled = false;
    }
  });
})();
