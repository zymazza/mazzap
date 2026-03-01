import * as THREE from "three";
import { OBJLoader } from "/node_modules/three/examples/jsm/loaders/OBJLoader.js";
import { MTLLoader } from "/node_modules/three/examples/jsm/loaders/MTLLoader.js";
import { GLTFLoader } from "/node_modules/three/examples/jsm/loaders/GLTFLoader.js";
import { TransformControls } from "/node_modules/three/examples/jsm/controls/TransformControls.js";

const viewerRoot = document.getElementById("viewerRoot");
const statusEl = document.getElementById("status");
const coordReadoutEl = document.getElementById("coordReadout");
const menuToggleButton = document.getElementById("menuToggle");
const sideMenu = document.getElementById("sideMenu");
const verticalScaleInput = document.getElementById("verticalScale");
const verticalScaleValue = document.getElementById("verticalScaleValue");
const shrubDensityInput = document.getElementById("shrubDensity");
const shrubDensityValue = document.getElementById("shrubDensityValue");
const treeDensityInput = document.getElementById("treeDensity");
const treeDensityValue = document.getElementById("treeDensityValue");
const showShrubsInput = document.getElementById("showShrubs");
const showTreesInput = document.getElementById("showTrees");
const showBuildingAssetsInput = document.getElementById("showBuildingAssets");
const showBuildingFootprintsInput = document.getElementById("showBuildingFootprints");
const showSoilDataInput = document.getElementById("showSoilData");
const showHydrologyInput = document.getElementById("showHydrology");
const hydrologyWidthInput = document.getElementById("hydrologyWidth");
const hydrologyWidthValue = document.getElementById("hydrologyWidthValue");
const hydrologyDepthInput = document.getElementById("hydrologyDepth");
const hydrologyDepthValue = document.getElementById("hydrologyDepthValue");
const hydrologyFlowSpeedInput = document.getElementById("hydrologyFlowSpeed");
const hydrologyFlowSpeedValue = document.getElementById("hydrologyFlowSpeedValue");
const snapHydrologyToTerrainButton = document.getElementById("snapHydrologyToTerrain");
const resetViewButton = document.getElementById("resetView");
const toggleLayersSectionButton = document.getElementById("toggleLayersSection");
const layersSectionBody = document.getElementById("layersSectionBody");
const toggleHydrologySectionButton = document.getElementById("toggleHydrologySection");
const hydrologySectionBody = document.getElementById("hydrologySectionBody");
const toggleDensitySectionButton = document.getElementById("toggleDensitySection");
const densitySectionBody = document.getElementById("densitySectionBody");
const toggleTerrainSectionButton = document.getElementById("toggleTerrainSection");
const terrainSectionBody = document.getElementById("terrainSectionBody");
const toggleBuildingsSectionButton = document.getElementById("toggleBuildingsSection");
const buildingsSectionBody = document.getElementById("buildingsSectionBody");
const toggleStatusSectionButton = document.getElementById("toggleStatusSection");
const statusSectionBody = document.getElementById("statusSectionBody");
const buildingSelectionLabel = document.getElementById("buildingSelectionLabel");
const buildingNameInput = document.getElementById("buildingNameInput");
const saveBuildingNameButton = document.getElementById("saveBuildingName");
const clearBuildingNameButton = document.getElementById("clearBuildingName");
const buildingNameListEl = document.getElementById("buildingNameList");
const openUploadModalButton = document.getElementById("openUploadModal");
const openManageDataModalButton = document.getElementById("openManageDataModal");
const uploadModalEl = document.getElementById("uploadModal");
const closeUploadModalButton = document.getElementById("closeUploadModal");
const uploadDropZoneEl = document.getElementById("uploadDropZone");
const uploadFilesInputEl = document.getElementById("uploadFilesInput");
const uploadFolderInputEl = document.getElementById("uploadFolderInput");
const selectUploadFilesButton = document.getElementById("selectUploadFiles");
const selectUploadFolderButton = document.getElementById("selectUploadFolder");
const clearUploadQueueButton = document.getElementById("clearUploadQueue");
const uploadQueueListEl = document.getElementById("uploadQueueList");
const uploadStatusEl = document.getElementById("uploadStatus");
const submitUploadButton = document.getElementById("submitUploadButton");
const uploadProgressModalEl = document.getElementById("uploadProgressModal");
const uploadProgressTitleEl = document.getElementById("uploadProgressTitle");
const uploadProgressFillEl = document.getElementById("uploadProgressFill");
const uploadProgressPercentEl = document.getElementById("uploadProgressPercent");
const uploadProgressExplanationEl = document.getElementById("uploadProgressExplanation");
const uploadProgressDetailEl = document.getElementById("uploadProgressDetail");
const closeUploadProgressButton = document.getElementById("closeUploadProgress");
const manageDataModalEl = document.getElementById("manageDataModal");
const closeManageDataModalButton = document.getElementById("closeManageDataModal");
const refreshDataSourcesButton = document.getElementById("refreshDataSources");
const clearAllDataButton = document.getElementById("clearAllData");
const manageDataSourcesListEl = document.getElementById("manageDataSourcesList");
const manageDataStatusEl = document.getElementById("manageDataStatus");
const soilLegendEl = document.getElementById("soilLegend");
const soilLegendItemsEl = document.getElementById("soilLegendItems");
const soilLegendDetailsEl = document.getElementById("soilLegendDetails");
let processBuildingAssetButton = null;
let loadBuildingAssetButton = null;
let buildingAssetStatusEl = null;
let buildingTransformModeTranslateButton = null;
let buildingTransformModeRotateButton = null;
let buildingTransformSpaceButton = null;
let buildingPoseReadoutEl = null;
let buildingPoseHintEl = null;
let saveBuildingPoseButton = null;
let resetBuildingPoseButton = null;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1220);

const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 50000);
camera.up.set(0, 0, 1);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
viewerRoot.appendChild(renderer.domElement);

const ambientLight = new THREE.AmbientLight(0xffffff, 0.45);
const directionalLight = new THREE.DirectionalLight(0xffffff, 1.1);
directionalLight.position.set(500, -400, 700);
scene.add(ambientLight, directionalLight);

const grid = new THREE.GridHelper(1200, 24, 0x4e5f84, 0x2a3859);
grid.rotation.x = Math.PI / 2;
scene.add(grid);

let terrainGeometry = null;
let terrainMesh = null;
let baseHeights = null;
let demGridWidth = 0;
let demGridHeight = 0;
let demGridXStep = 1;
let demGridYStep = 1;
let terrainWidthMeters = 1;
let terrainHeightMeters = 1;
let elevationRange = 1;
let demMeta = null;

let shrubTemplates = [];
let shrubInstances = [];
let shrubAnchorsAll = [];
let treeTemplatesByCategory = {
  short: [],
  mid: [],
  tall: [],
  all: []
};
let treeInstances = [];
let treeAnchorsAll = [];
let soilFeaturesGeoJson = null;
let soilLegendData = [];
let soilPolygonsLocal = [];
let soilMeshes = [];
let soilGroup = null;
let hydrologyFeaturesGeoJson = null;
let hydrologyPolylinesWorld = [];
let hydrologyPolylinesLocal = [];
let hydrologyMeshes = [];
let hydrologyGroup = null;
let hydrologyFlowTexture = null;
let hydrologyFlowMaterial = null;
let buildingLinesGroup = null;
let buildingFeaturesGeoJson = null;
let buildingsMeta = null;
let buildingPolygonsLocal = [];
let buildingRecords = [];
let selectedBuildingId = null;
let buildingNameMap = {};
let buildingPoseMap = {};
let buildingAssetRootGroup = null;
const loadedBuildingAssets = new Map();
const gltfLoader = new GLTFLoader();
let buildingTransformControls = null;
let buildingTransformIsDragging = false;
let suppressBuildingPickOnPointerUp = false;
let suspendTransformPoseSync = false;

let demStatusText = "";
let shrubsStatusText = "";
let treesStatusText = "";
let buildingsStatusText = "";
let soilsStatusText = "";
let hydrologyStatusText = "";
let uploadQueueItems = [];
let uploadInProgress = false;
let uploadProgressState = {
  totalUnits: 1,
  completedUnits: 0,
  done: false,
  error: false
};
let manageDataSources = [];
let manageDataBusy = false;
let soilSelectedClassFilter = null;
let soilSelectedPolygon = null;
let terrainBaseColors = null;

const cameraState = {
  radius: 1200,
  theta: -Math.PI / 4,
  phi: 1.05,
  target: new THREE.Vector3(0, 0, 0)
};

const pointerState = {
  dragging: false,
  downX: 0,
  downY: 0,
  lastX: 0,
  lastY: 0,
  movedDistance: 0
};
const keyboardPanState = {
  ArrowUp: false,
  ArrowDown: false,
  ArrowLeft: false,
  ArrowRight: false
};
let lastFrameTimeMs = performance.now();
const hoverNdc = new THREE.Vector2(0, 0);
const raycaster = new THREE.Raycaster();
const hoverState = {
  insideCanvas: false
};
let copyFeedbackUntilMs = 0;
let copyFeedbackText = "";

const BUSH_RENDER_SCALE_MULTIPLIER = 4.0;
const TREE_MAX_INSTANCES = 2880;
const TREE_DISPERSION_CELL_SIZE = 2.75;
const BUILDING_EDGE_CLEARANCE_METERS = 5;
const BUILDING_PUSH_EPSILON = 0.05;
const BUILDING_BASE_COLOR = 0x66e6ff;
const BUILDING_SELECTED_COLOR = 0xffcc4d;
const HYDROLOGY_SURFACE_OFFSET_METERS = 0.08;
const HYDROLOGY_DEPTH_SLIDER_MIDPOINT = 5;
const HYDROLOGY_EDGE_CLEARANCE_METERS = 1;
const HYDROLOGY_PUSH_EPSILON = 0.02;
const BUILDING_NAME_STORAGE_KEY = "mazzap.buildingNames.v1";
const BUILDING_POSE_STORAGE_KEY = "mazzap.buildingAssetPose.v1";
const SECTION_COLLAPSED_STORAGE_PREFIX = "mazzap.sectionCollapsed.v1.";
const BUILDING_UV_NORMALIZE_SPAN_THRESHOLD = 10;
const BUILDING_ASSET_UNLIT_DEBUG = true;
const BUILDING_RENDER_PATH_LABEL = "raw-gltf";
const DATA_SOURCE_TYPE_OPTIONS = ["lidar", "footprints", "soils", "hydrology", "photogrammetry"];
const DATA_SOURCE_TYPE_LABELS = {
  lidar: "LiDAR",
  footprints: "Footprints",
  soils: "Soils (SSURGO)",
  photogrammetry: "Photogrammetry",
  hydrology: "Hydrology"
};
const BUILDING_TEXTURE_PROPERTY_KEYS = [
  "map",
  "alphaMap",
  "aoMap",
  "bumpMap",
  "clearcoatMap",
  "clearcoatNormalMap",
  "clearcoatRoughnessMap",
  "displacementMap",
  "emissiveMap",
  "envMap",
  "iridescenceMap",
  "iridescenceThicknessMap",
  "lightMap",
  "metalnessMap",
  "normalMap",
  "roughnessMap",
  "sheenColorMap",
  "sheenRoughnessMap",
  "specularColorMap",
  "specularIntensityMap",
  "specularMap",
  "thicknessMap",
  "transmissionMap"
];

console.info(
  `[building-assets] render path: ${BUILDING_RENDER_PATH_LABEL} (default)`
);

function setMenuOpen(isOpen) {
  document.body.classList.toggle("menu-open", isOpen);
  menuToggleButton.setAttribute("aria-expanded", isOpen ? "true" : "false");
  sideMenu.setAttribute("aria-hidden", isOpen ? "false" : "true");
}

function isShrubsVisible() {
  return Boolean(showShrubsInput?.checked);
}

function isTreesVisible() {
  return Boolean(showTreesInput?.checked);
}

function isBuildingAssetsVisible() {
  return Boolean(showBuildingAssetsInput?.checked);
}

function isBuildingFootprintsVisible() {
  return Boolean(showBuildingFootprintsInput?.checked);
}

function isSoilsVisible() {
  return Boolean(showSoilDataInput?.checked);
}

function isHydrologyVisible() {
  return Boolean(showHydrologyInput?.checked);
}

function applyBuildingAssetsVisibility() {
  if (!buildingAssetRootGroup) {
    return;
  }
  buildingAssetRootGroup.visible = isBuildingAssetsVisible();
}

function applyBuildingFootprintsVisibility() {
  if (!buildingLinesGroup) {
    return;
  }
  buildingLinesGroup.visible = isBuildingFootprintsVisible();
}

function applySoilsVisibility() {
  if (soilGroup) {
    soilGroup.visible = isSoilsVisible();
  }

  applySoilsToTerrainColors();

  if (soilLegendEl) {
    const hasLegendItems = Array.isArray(soilLegendData) && soilLegendData.length > 0;
    soilLegendEl.hidden = !(isSoilsVisible() && hasLegendItems);
  }
}

function applyHydrologyVisibility() {
  if (!hydrologyGroup) {
    return;
  }
  hydrologyGroup.visible = isHydrologyVisible();
}

function getSectionCollapsedStorageKey(sectionId) {
  return `${SECTION_COLLAPSED_STORAGE_PREFIX}${sectionId}`;
}

function loadSectionCollapsedFromStorage(sectionId) {
  const key = getSectionCollapsedStorageKey(sectionId);
  try {
    const raw = localStorage.getItem(key);
    return raw === "1";
  } catch (error) {
    return false;
  }
}

function setSectionCollapsed(sectionId, bodyEl, toggleEl, collapsed, persist = true) {
  if (!bodyEl || !toggleEl) {
    return;
  }

  const isCollapsed = Boolean(collapsed);
  bodyEl.classList.toggle("isCollapsed", isCollapsed);
  toggleEl.textContent = isCollapsed ? "+" : "-";
  toggleEl.setAttribute("aria-expanded", isCollapsed ? "false" : "true");

  if (!persist) {
    return;
  }

  try {
    localStorage.setItem(getSectionCollapsedStorageKey(sectionId), isCollapsed ? "1" : "0");
  } catch (error) {
    // Ignore storage write failures.
  }
}

function initializeSectionCollapse(sectionId, bodyEl, toggleEl) {
  if (!bodyEl || !toggleEl) {
    return;
  }

  setSectionCollapsed(sectionId, bodyEl, toggleEl, loadSectionCollapsedFromStorage(sectionId), false);
  toggleEl.addEventListener("click", () => {
    const currentlyCollapsed = bodyEl.classList.contains("isCollapsed");
    setSectionCollapsed(sectionId, bodyEl, toggleEl, !currentlyCollapsed, true);
  });
}

function refreshStatus() {
  const parts = [];
  if (demStatusText) {
    parts.push(demStatusText);
  }
  if (shrubsStatusText) {
    parts.push(shrubsStatusText);
  }
  if (treesStatusText) {
    parts.push(treesStatusText);
  }
  if (buildingsStatusText) {
    parts.push(buildingsStatusText);
  }
  if (soilsStatusText) {
    parts.push(soilsStatusText);
  }
  if (hydrologyStatusText) {
    parts.push(hydrologyStatusText);
  }
  statusEl.textContent = parts.join(" | ");
}

function loadBuildingNamesFromStorage() {
  try {
    const raw = localStorage.getItem(BUILDING_NAME_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    return parsed;
  } catch (error) {
    return {};
  }
}

function saveBuildingNamesToStorage() {
  try {
    localStorage.setItem(BUILDING_NAME_STORAGE_KEY, JSON.stringify(buildingNameMap));
  } catch (error) {
    // Ignore storage write failures.
  }
}

function sanitizeBuildingPose(rawPose) {
  const rotXDeg = clamp(Number(rawPose?.rotXDeg ?? 0), -180, 180);
  const rotYDeg = clamp(Number(rawPose?.rotYDeg ?? 0), -180, 180);
  const rotZDeg = clamp(Number(rawPose?.rotZDeg ?? 0), -180, 180);
  const xOffset = clamp(Number(rawPose?.xOffset ?? 0), -200, 200);
  const yOffset = clamp(Number(rawPose?.yOffset ?? 0), -200, 200);
  const zOffset = clamp(Number(rawPose?.zOffset ?? rawPose?.verticalOffset ?? 0), -120, 120);
  return { rotXDeg, rotYDeg, rotZDeg, xOffset, yOffset, zOffset };
}

function loadBuildingPosesFromStorage() {
  try {
    const raw = localStorage.getItem(BUILDING_POSE_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const out = {};
    for (const [key, value] of Object.entries(parsed)) {
      const buildingId = String(key || "").trim();
      if (!buildingId) {
        continue;
      }
      out[buildingId] = sanitizeBuildingPose(value);
    }
    return out;
  } catch (error) {
    return {};
  }
}

function saveBuildingPosesToStorage() {
  try {
    localStorage.setItem(BUILDING_POSE_STORAGE_KEY, JSON.stringify(buildingPoseMap));
  } catch (error) {
    // Ignore storage write failures.
  }
}

function getSavedBuildingPose(buildingId) {
  const id = String(buildingId || "").trim();
  if (!id) {
    return sanitizeBuildingPose({});
  }
  return sanitizeBuildingPose(buildingPoseMap[id]);
}

function ensureBuildingAssetRootGroup() {
  if (!buildingAssetRootGroup) {
    buildingAssetRootGroup = new THREE.Group();
    buildingAssetRootGroup.name = "building-assets";
    scene.add(buildingAssetRootGroup);
    buildingAssetRootGroup.visible = isBuildingAssetsVisible();
  }
  return buildingAssetRootGroup;
}

function updateBuildingAssetStatus(message, isError = false) {
  if (!buildingAssetStatusEl) {
    return;
  }
  buildingAssetStatusEl.textContent = message || "";
  buildingAssetStatusEl.style.color = isError ? "#ffd1d1" : "#bcd4f7";
}

function formatByteSize(bytesRaw) {
  const bytes = Number(bytesRaw);
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "--";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

function normalizeUploadRelativePath(rawPath) {
  return String(rawPath || "")
    .replace(/\\+/g, "/")
    .split("/")
    .map((segment) => String(segment || "").trim())
    .filter((segment) => segment && segment !== "." && segment !== "..")
    .join("/");
}

function inferDataSourceTypeForUpload(file, relativePath) {
  const target = String(relativePath || file?.name || "").toLowerCase();
  if (/\.copc\.laz$/i.test(target) || /\.(las|laz)$/i.test(target)) {
    return "lidar";
  }
  if (
    target.includes("ssurgo") ||
    target.includes("/tabular/") ||
    target.includes("/thematic/") ||
    target.includes("/spatial/") ||
    target.includes("soildb_") ||
    target.includes("soil_metadata") ||
    target.endsWith(".mdb")
  ) {
    return "soils";
  }
  if (
    target.includes(".gdb/") ||
    target.endsWith(".gdb") ||
    /\.(gpkg|geojson)$/i.test(target)
  ) {
    return "footprints";
  }
  if (/\.(shp|shx|dbf|prj)$/i.test(target) || target.includes("hydrology/")) {
    return "hydrology";
  }
  return "photogrammetry";
}

function setUploadStatus(message, isError = false) {
  if (!uploadStatusEl) {
    return;
  }
  uploadStatusEl.textContent = message || "";
  uploadStatusEl.style.color = isError ? "#ffd1d1" : "#bcd4f7";
}

function setManageDataStatus(message, isError = false) {
  if (!manageDataStatusEl) {
    return;
  }
  manageDataStatusEl.textContent = message || "";
  manageDataStatusEl.style.color = isError ? "#ffd1d1" : "#bcd4f7";
}

function setManageDataBusyState(busy) {
  manageDataBusy = Boolean(busy);
  if (refreshDataSourcesButton) {
    refreshDataSourcesButton.disabled = manageDataBusy;
  }
  if (clearAllDataButton) {
    clearAllDataButton.disabled = manageDataBusy;
  }
  renderManageDataSourcesList();
}

function renderManageDataSourcesList() {
  if (!manageDataSourcesListEl) {
    return;
  }

  manageDataSourcesListEl.innerHTML = "";
  const sources = Array.isArray(manageDataSources) ? manageDataSources : [];
  if (sources.length === 0) {
    const empty = document.createElement("p");
    empty.className = "manageDataEmpty";
    empty.textContent = "No raw data sources found.";
    manageDataSourcesListEl.appendChild(empty);
    return;
  }

  for (const source of sources) {
    const row = document.createElement("div");
    row.className = "manageDataItem";

    const labelWrap = document.createElement("div");
    const nameEl = document.createElement("div");
    nameEl.className = "manageDataName";
    nameEl.textContent = String(source.relativePath || source.name || "(unnamed)");
    const metaEl = document.createElement("div");
    metaEl.className = "manageDataMeta";
    metaEl.textContent = `${String(DATA_SOURCE_TYPE_LABELS[source.type] || source.type || "Unknown")} • ${formatByteSize(source.sizeBytes)}`;
    labelWrap.appendChild(nameEl);
    labelWrap.appendChild(metaEl);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "manageDataDeleteButton";
    deleteButton.textContent = "Delete";
    deleteButton.disabled = manageDataBusy;
    deleteButton.addEventListener("click", async () => {
      const label = String(source.relativePath || source.name || "this source");
      if (!window.confirm(`Delete ${label}?`)) {
        return;
      }

      try {
        setManageDataBusyState(true);
        setManageDataStatus(`Deleting ${label}...`);
        const response = await fetch("/api/data-sources/delete", {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({ relativePath: source.relativePath })
        });
        const payload = await response.json();
        if (!response.ok || !payload || payload.error) {
          throw new Error((payload && payload.error) || `Delete failed (${response.status})`);
        }

        setManageDataStatus(`Deleted ${label}.`);
        await loadManageDataSources();
      } catch (error) {
        setManageDataStatus(`Delete failed: ${error.message}`, true);
      } finally {
        setManageDataBusyState(false);
      }
    });

    row.appendChild(labelWrap);
    row.appendChild(deleteButton);
    manageDataSourcesListEl.appendChild(row);
  }
}

async function loadManageDataSources() {
  try {
    const payload = await fetchJson("/api/data-sources/list");
    manageDataSources = Array.isArray(payload?.sources) ? payload.sources : [];
    renderManageDataSourcesList();
    const sourceCount = manageDataSources.length;
    setManageDataStatus(`Found ${sourceCount} data source${sourceCount === 1 ? "" : "s"}.`);
  } catch (error) {
    manageDataSources = [];
    renderManageDataSourcesList();
    setManageDataStatus(`Unable to load data sources: ${error.message}`, true);
  }
}

function setManageDataModalOpen(isOpen) {
  if (!manageDataModalEl) {
    return;
  }
  manageDataModalEl.hidden = !isOpen;
  if (isOpen) {
    setManageDataStatus("");
    void loadManageDataSources();
  }
}

async function refreshDataProductsInViewer() {
  try {
    await loadDemGrid();
  } catch (error) {
    demStatusText = `DEM unavailable (${error.message})`;
    refreshStatus();
  }

  try {
    await loadSoils();
  } catch (error) {
    resetSoilsData();
    soilsStatusText = `soils unavailable (${error.message})`;
    refreshStatus();
  }

  try {
    await loadSoils();
  } catch (error) {
    resetSoilsData();
    soilsStatusText = `soils unavailable (${error.message})`;
    refreshStatus();
    console.error(error);
  }

  try {
    await loadHydrology();
  } catch (error) {
    resetHydrologyData();
    hydrologyStatusText = `hydrology unavailable (${error.message})`;
    refreshStatus();
  }

  try {
    await loadBuildings();
  } catch (error) {
    buildingsStatusText = `buildings unavailable (${error.message})`;
    refreshStatus();
  }

  try {
    await autoLoadExistingBuildingAssets();
  } catch (error) {
    updateBuildingAssetStatus(`Auto-load failed: ${error.message}`, true);
  }

  try {
    await loadShrubs();
  } catch (error) {
    shrubsStatusText = `shrubs unavailable (${error.message})`;
    refreshStatus();
  }

  try {
    await loadTrees();
  } catch (error) {
    treesStatusText = `trees unavailable (${error.message})`;
    refreshStatus();
  }
}

function setUploadModalOpen(isOpen) {
  if (!uploadModalEl) {
    return;
  }
  uploadModalEl.hidden = !isOpen;
  if (isOpen) {
    setUploadStatus("");
  }
}

function setUploadProgressModalOpen(isOpen) {
  if (!uploadProgressModalEl) {
    return;
  }
  uploadProgressModalEl.hidden = !isOpen;
}

function updateUploadProgressUi({
  title,
  explanation,
  detail,
  completedUnits,
  totalUnits,
  done,
  isError
}) {
  uploadProgressState = {
    totalUnits: Math.max(1, Number(totalUnits || uploadProgressState.totalUnits || 1)),
    completedUnits: Math.max(0, Number(completedUnits || 0)),
    done: Boolean(done),
    error: Boolean(isError)
  };

  const percent = Math.max(
    0,
    Math.min(100, Math.round((uploadProgressState.completedUnits / uploadProgressState.totalUnits) * 100))
  );

  if (uploadProgressTitleEl && title) {
    uploadProgressTitleEl.textContent = title;
  }
  if (uploadProgressFillEl) {
    uploadProgressFillEl.style.width = `${percent}%`;
  }
  if (uploadProgressPercentEl) {
    uploadProgressPercentEl.textContent = `${percent}%`;
  }
  if (uploadProgressExplanationEl && explanation !== undefined) {
    uploadProgressExplanationEl.textContent = explanation || "";
    uploadProgressExplanationEl.style.color = isError ? "#ffd1d1" : "#c9d7f5";
  }
  if (uploadProgressDetailEl && detail !== undefined) {
    uploadProgressDetailEl.textContent = detail || "";
  }
  if (closeUploadProgressButton) {
    closeUploadProgressButton.disabled = !done && !isError;
  }
}

function setUploadControlsDisabled(disabled) {
  const nextDisabled = Boolean(disabled);
  if (uploadFilesInputEl) uploadFilesInputEl.disabled = nextDisabled;
  if (uploadFolderInputEl) uploadFolderInputEl.disabled = nextDisabled;
  if (selectUploadFilesButton) selectUploadFilesButton.disabled = nextDisabled;
  if (selectUploadFolderButton) selectUploadFolderButton.disabled = nextDisabled;
  if (clearUploadQueueButton) clearUploadQueueButton.disabled = nextDisabled || uploadQueueItems.length === 0;
  if (submitUploadButton) submitUploadButton.disabled = nextDisabled || uploadQueueItems.length === 0;
}

function renderUploadQueue() {
  if (!uploadQueueListEl) {
    return;
  }

  uploadQueueListEl.innerHTML = "";
  if (uploadQueueItems.length === 0) {
    const empty = document.createElement("p");
    empty.className = "uploadQueueEmpty";
    empty.textContent = "No files queued.";
    uploadQueueListEl.appendChild(empty);
    setUploadControlsDisabled(uploadInProgress);
    return;
  }

  uploadQueueItems.forEach((item) => {
    const row = document.createElement("div");
    row.className = "uploadQueueItem";

    const fileLabel = document.createElement("div");
    fileLabel.className = "uploadFileLabel";
    const fileName = document.createElement("div");
    fileName.className = "uploadFileName";
    fileName.textContent = item.relativePath || item.file.name;
    const fileMeta = document.createElement("div");
    fileMeta.className = "uploadFileMeta";
    fileMeta.textContent = `${formatByteSize(item.file.size)} • ${item.file.name}`;
    fileLabel.appendChild(fileName);
    fileLabel.appendChild(fileMeta);

    const typeSelect = document.createElement("select");
    typeSelect.className = "uploadQueueTypeSelect";
    typeSelect.disabled = uploadInProgress;
    for (const optionValue of DATA_SOURCE_TYPE_OPTIONS) {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = DATA_SOURCE_TYPE_LABELS[optionValue] || optionValue;
      if (item.sourceType === optionValue) {
        option.selected = true;
      }
      typeSelect.appendChild(option);
    }
    typeSelect.addEventListener("change", () => {
      item.sourceType = String(typeSelect.value || "photogrammetry").toLowerCase();
    });

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "uploadQueueRemoveButton";
    removeButton.textContent = "Remove";
    removeButton.disabled = uploadInProgress;
    removeButton.addEventListener("click", () => {
      uploadQueueItems = uploadQueueItems.filter((candidate) => candidate.id !== item.id);
      renderUploadQueue();
    });

    row.appendChild(fileLabel);
    row.appendChild(typeSelect);
    row.appendChild(removeButton);
    uploadQueueListEl.appendChild(row);
  });

  setUploadControlsDisabled(uploadInProgress);
}

function isLikelyDirectoryPlaceholder(file, relativePath) {
  if (!file || !(file instanceof File)) {
    return false;
  }
  const normalizedPath = String(relativePath || "").toLowerCase();
  if (!normalizedPath.endsWith(".gdb")) {
    return false;
  }
  return file.size === 0 && !String(file.type || "").trim();
}

function addUploadEntriesToQueue(entries) {
  const normalizedEntries = Array.isArray(entries) ? entries : [];
  if (normalizedEntries.length === 0) {
    return;
  }

  let addedCount = 0;
  let skippedDirectoryPlaceholders = 0;

  for (const entry of normalizedEntries) {
    const file = entry instanceof File ? entry : entry && entry.file instanceof File ? entry.file : null;
    if (!file) {
      continue;
    }

    const hintedRelative = entry && typeof entry === "object" && "relativePath" in entry
      ? String(entry.relativePath || "").trim()
      : "";
    const relativePath = normalizeUploadRelativePath(hintedRelative || file.webkitRelativePath || file.name);
    const sourceType = inferDataSourceTypeForUpload(file, relativePath);

    if (isLikelyDirectoryPlaceholder(file, relativePath)) {
      skippedDirectoryPlaceholders += 1;
      continue;
    }

    const duplicate = uploadQueueItems.some((existing) => (
      existing.relativePath === relativePath &&
      existing.file.size === file.size &&
      existing.file.lastModified === file.lastModified
    ));
    if (duplicate) {
      continue;
    }

    uploadQueueItems.push({
      id: `${Date.now()}_${Math.random().toString(16).slice(2)}`,
      file,
      relativePath,
      sourceType
    });
    addedCount += 1;
  }

  renderUploadQueue();
  if (addedCount > 0) {
    let message = `Queued ${addedCount} file${addedCount === 1 ? "" : "s"}.`;
    if (skippedDirectoryPlaceholders > 0) {
      message += ` Ignored ${skippedDirectoryPlaceholders} folder placeholder${skippedDirectoryPlaceholders === 1 ? "" : "s"}.`;
    }
    setUploadStatus(message);
  } else if (skippedDirectoryPlaceholders > 0) {
    setUploadStatus(
      "Ignored folder placeholder-only drop. Drag the folder contents or use Select Folder for .gdb uploads.",
      true
    );
  }
}

function addUploadFilesToQueue(fileList, preferWebkitRelativePath = false) {
  const entries = Array.from(fileList || []).map((file) => {
    const incomingRelative = preferWebkitRelativePath
      ? String(file?.webkitRelativePath || "").trim()
      : "";
    return {
      file,
      relativePath: incomingRelative || file?.name || ""
    };
  });
  addUploadEntriesToQueue(entries);
}

function readDirectoryEntriesBatch(reader) {
  return new Promise((resolve, reject) => {
    reader.readEntries(resolve, reject);
  });
}

function readFileFromEntry(fileEntry) {
  return new Promise((resolve, reject) => {
    fileEntry.file(resolve, reject);
  });
}

async function flattenDroppedEntry(entry, parentPath = "") {
  if (!entry) {
    return [];
  }

  if (entry.isFile) {
    const file = await readFileFromEntry(entry);
    const relativePath = parentPath ? `${parentPath}/${entry.name}` : entry.name;
    return [{ file, relativePath }];
  }

  if (entry.isDirectory) {
    const nextParent = parentPath ? `${parentPath}/${entry.name}` : entry.name;
    const reader = entry.createReader();
    const children = [];
    while (true) {
      const batch = await readDirectoryEntriesBatch(reader);
      if (!Array.isArray(batch) || batch.length === 0) {
        break;
      }
      children.push(...batch);
    }

    const out = [];
    for (const child of children) {
      const flattened = await flattenDroppedEntry(child, nextParent);
      out.push(...flattened);
    }
    return out;
  }

  return [];
}

async function collectDroppedUploadEntries(dataTransfer) {
  if (!dataTransfer) {
    return [];
  }

  const items = Array.from(dataTransfer.items || []);
  const entryBased = [];
  for (const item of items) {
    if (!item || typeof item.webkitGetAsEntry !== "function") {
      continue;
    }
    const entry = item.webkitGetAsEntry();
    if (entry) {
      entryBased.push(entry);
    }
  }

  if (entryBased.length > 0) {
    const flattened = [];
    for (const entry of entryBased) {
      const nested = await flattenDroppedEntry(entry, "");
      flattened.push(...nested);
    }
    if (flattened.length > 0) {
      return flattened;
    }
  }

  return Array.from(dataTransfer.files || []).map((file) => ({
    file,
    relativePath: file?.name || ""
  }));
}

async function submitUploadQueue() {
  if (uploadInProgress) {
    return;
  }
  if (uploadQueueItems.length === 0) {
    setUploadStatus("No files queued to upload.", true);
    return;
  }

  uploadInProgress = true;
  setUploadControlsDisabled(true);
  const queuedItems = uploadQueueItems.slice();
  const uploadedTypes = Array.from(new Set(queuedItems.map((item) => String(item.sourceType || "").toLowerCase())));
  const shouldAutoProcess =
    uploadedTypes.includes("lidar") || uploadedTypes.includes("footprints") || uploadedTypes.includes("hydrology") || uploadedTypes.includes("soils");

  const firstIndexByType = new Map();
  queuedItems.forEach((item, index) => {
    const typeKey = String(item.sourceType || "").toLowerCase();
    if (!firstIndexByType.has(typeKey)) {
      firstIndexByType.set(typeKey, index);
    }
  });

  const parseApiJson = async (response) => {
    try {
      return await response.json();
    } catch (error) {
      const text = await response.text().catch(() => "");
      return { error: text || `Request failed (${response.status})` };
    }
  };

  let completedUnits = 0;
  let totalUnits = Math.max(1, queuedItems.length + (shouldAutoProcess ? 1 : 0));
  setUploadModalOpen(false);
  setUploadProgressModalOpen(true);
  updateUploadProgressUi({
    title: "Uploading Data Sources",
    explanation: "Copying source files into Raw Data Inputs.",
    detail: `Queued ${queuedItems.length} file(s).`,
    completedUnits,
    totalUnits,
    done: false,
    isError: false
  });

  try {
    for (let index = 0; index < queuedItems.length; index += 1) {
      const item = queuedItems[index];
      const sourceType = String(item.sourceType || "photogrammetry").toLowerCase();
      const replace = sourceType !== "photogrammetry" && firstIndexByType.get(sourceType) === index ? "1" : "0";

      setUploadStatus(`Uploading ${index + 1}/${queuedItems.length}: ${item.relativePath}`);
      updateUploadProgressUi({
        title: "Uploading Data Sources",
        explanation: `Uploading ${index + 1} of ${queuedItems.length}: ${item.relativePath}`,
        detail: `Type: ${DATA_SOURCE_TYPE_LABELS[sourceType] || sourceType}`,
        completedUnits,
        totalUnits,
        done: false,
        isError: false
      });

      const query = new URLSearchParams({
        sourceType,
        relativePath: item.relativePath,
        originalName: item.file.name,
        replace
      });

      const response = await fetch(`/api/data-sources/upload-item?${query.toString()}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream"
        },
        body: item.file
      });

      const payload = await parseApiJson(response);
      if (!response.ok || !payload || payload.error) {
        throw new Error((payload && payload.error) || `Upload failed (${response.status})`);
      }

      completedUnits += 1;
      updateUploadProgressUi({
        title: "Uploading Data Sources",
        explanation: `Uploaded ${index + 1} of ${queuedItems.length}`,
        detail: item.relativePath,
        completedUnits,
        totalUnits,
        done: false,
        isError: false
      });
    }

    if (shouldAutoProcess) {
      setUploadStatus("Upload complete. Running processing pipelines...");
      updateUploadProgressUi({
        title: "Planning Processing",
        explanation: "Determining which pipeline steps are required from uploaded data.",
        detail: "Checking LiDAR and footprints availability.",
        completedUnits,
        totalUnits,
        done: false,
        isError: false
      });

      const planResponse = await fetch("/api/data-sources/process-plan", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ uploadedTypes })
      });

      if (planResponse.status === 404) {
        totalUnits = Math.max(totalUnits, completedUnits + 1);
        updateUploadProgressUi({
          title: "Processing Data",
          explanation: "Running pipelines in compatibility mode (stepwise progress API unavailable).",
          detail: "Tip: restart server to enable detailed per-step progress.",
          completedUnits,
          totalUnits,
          done: false,
          isError: false
        });

        const legacyResponse = await fetch("/api/data-sources/process", {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({ uploadedTypes })
        });
        const legacyPayload = await parseApiJson(legacyResponse);
        if (!legacyResponse.ok || !legacyPayload || legacyPayload.error) {
          throw new Error((legacyPayload && legacyPayload.error) || `Processing failed (${legacyResponse.status})`);
        }

        const executedCount = Number(legacyPayload.executedCount || 0);
        const firstRunScript = Array.isArray(legacyPayload.runs) && legacyPayload.runs.length > 0
          ? String(legacyPayload.runs[0]?.script || "")
          : "";
        completedUnits = totalUnits;
        setUploadStatus(`Processing complete (${executedCount} pipeline step${executedCount === 1 ? "" : "s"}). Refreshing viewer...`);
        updateUploadProgressUi({
          title: "Processing Complete",
          explanation: "Pipeline run finished in compatibility mode.",
          detail: firstRunScript ? `First step: ${firstRunScript}` : "",
          completedUnits,
          totalUnits,
          done: false,
          isError: false
        });

        await refreshDataProductsInViewer();
        setUploadStatus(`Upload + processing complete. Saved ${queuedItems.length} file(s), ran ${executedCount} step${executedCount === 1 ? "" : "s"}.`);
        updateUploadProgressUi({
          title: "Upload Complete",
          explanation: "Files uploaded and data products regenerated successfully.",
          detail: `Saved ${queuedItems.length} files; ran ${executedCount} processing step${executedCount === 1 ? "" : "s"}.`,
          completedUnits: totalUnits,
          totalUnits,
          done: true,
          isError: false
        });
      } else {
        const planPayload = await parseApiJson(planResponse);
        if (!planResponse.ok || !planPayload || planPayload.error) {
          throw new Error((planPayload && planPayload.error) || `Processing plan failed (${planResponse.status})`);
        }

        const processSteps = Array.isArray(planPayload.steps) ? planPayload.steps : [];
        totalUnits = Math.max(totalUnits, completedUnits + processSteps.length);
        updateUploadProgressUi({
          title: "Processing Data",
          explanation: processSteps.length > 0
            ? `Running ${processSteps.length} processing step${processSteps.length === 1 ? "" : "s"}.`
            : "No processing steps required for current uploads.",
          detail: processSteps.length > 0
            ? "This generates DEM, soils, hydrology, vegetation, trees, and/or footprints based on available inputs."
            : "",
          completedUnits,
          totalUnits,
          done: processSteps.length === 0,
          isError: false
        });

        const executedRuns = [];
        for (let stepIndex = 0; stepIndex < processSteps.length; stepIndex += 1) {
          const step = processSteps[stepIndex];
          const stepId = String(step?.id || "").trim().toLowerCase();
          const stepTitle = String(step?.title || stepId || `step_${stepIndex + 1}`);
          const stepExplanation = String(step?.explanation || "Processing step in progress.");

          updateUploadProgressUi({
            title: `Processing: ${stepTitle}`,
            explanation: stepExplanation,
            detail: `Step ${stepIndex + 1} of ${processSteps.length}`,
            completedUnits,
            totalUnits,
            done: false,
            isError: false
          });

          const stepResponse = await fetch("/api/data-sources/process-step", {
            method: "POST",
            headers: {
              "Content-Type": "application/json"
            },
            body: JSON.stringify({ stepId })
          });
          const stepPayload = await parseApiJson(stepResponse);
          if (!stepResponse.ok || !stepPayload || stepPayload.error) {
            throw new Error((stepPayload && stepPayload.error) || `Processing step failed (${stepResponse.status})`);
          }

          executedRuns.push(stepPayload);

          if (stepId === "dem") {
            try {
              await loadDemGrid();
              updateUploadProgressUi({
                title: "DEM Ready",
                explanation: "Terrain loaded in viewer while vegetation processing continues.",
                detail: "Continuing with shrubs/trees generation.",
                completedUnits,
                totalUnits,
                done: false,
                isError: false
              });
            } catch (error) {
              demStatusText = `DEM unavailable (${error.message})`;
              refreshStatus();
            }
          }

          if (stepId === "soils") {
            try {
              await loadSoils();
              updateUploadProgressUi({
                title: "Soils Ready",
                explanation: "SSURGO soil polygons and legend loaded in viewer.",
                detail: "Continuing remaining processing steps.",
                completedUnits,
                totalUnits,
                done: false,
                isError: false
              });
            } catch (error) {
              soilsStatusText = `soils unavailable (${error.message})`;
              refreshStatus();
            }
          }

          completedUnits += 1;
          updateUploadProgressUi({
            title: `Completed: ${stepTitle}`,
            explanation: stepExplanation,
            detail: `Step ${stepIndex + 1} of ${processSteps.length} finished.`,
            completedUnits,
            totalUnits,
            done: false,
            isError: false
          });
        }

        const executedCount = executedRuns.length;
        setUploadStatus(`Processing complete (${executedCount} pipeline step${executedCount === 1 ? "" : "s"}). Refreshing viewer...`);
        await refreshDataProductsInViewer();
        setUploadStatus(`Upload + processing complete. Saved ${queuedItems.length} file(s), ran ${executedCount} step${executedCount === 1 ? "" : "s"}.`);
        updateUploadProgressUi({
          title: "Upload Complete",
          explanation: "Files uploaded and data products regenerated successfully.",
          detail: `Saved ${queuedItems.length} files; ran ${executedCount} processing step${executedCount === 1 ? "" : "s"}.`,
          completedUnits: totalUnits,
          totalUnits,
          done: true,
          isError: false
        });
      }
    } else {
      setUploadStatus(`Upload complete. Saved ${queuedItems.length} file(s).`);
      updateUploadProgressUi({
        title: "Upload Complete",
        explanation: "Files uploaded successfully.",
        detail: "No auto-processing steps were required for this upload.",
        completedUnits: totalUnits,
        totalUnits,
        done: true,
        isError: false
      });
    }

    uploadQueueItems = [];
    renderUploadQueue();
  } catch (error) {
    setUploadStatus(`Upload failed: ${error.message}`, true);
    updateUploadProgressUi({
      title: "Upload Failed",
      explanation: "A step failed before completion.",
      detail: error.message,
      completedUnits,
      totalUnits,
      done: false,
      isError: true
    });
  } finally {
    uploadInProgress = false;
    setUploadControlsDisabled(false);
    renderUploadQueue();
  }
}

function getSelectedFootprintDisplayName() {
  if (!selectedBuildingId) {
    return "";
  }
  const savedName = getBuildingName(selectedBuildingId);
  if (savedName) {
    return savedName;
  }
  return buildingNameInput ? String(buildingNameInput.value || "").trim() : "";
}

function updateBuildingPoseReadout(pose) {
  if (!buildingPoseReadoutEl) {
    return;
  }
  const safe = sanitizeBuildingPose(pose);
  buildingPoseReadoutEl.textContent =
    `T: x ${safe.xOffset.toFixed(2)}m  y ${safe.yOffset.toFixed(2)}m  z ${safe.zOffset.toFixed(2)}m\n` +
    `R: x ${safe.rotXDeg.toFixed(1)}°  y ${safe.rotYDeg.toFixed(1)}°  z ${safe.rotZDeg.toFixed(1)}°`;
}

function setBuildingTransformMode(mode) {
  const nextMode = mode === "rotate" ? "rotate" : "translate";
  if (buildingTransformControls) {
    buildingTransformControls.setMode(nextMode);
  }
  if (buildingTransformModeTranslateButton) {
    buildingTransformModeTranslateButton.disabled = nextMode === "translate";
  }
  if (buildingTransformModeRotateButton) {
    buildingTransformModeRotateButton.disabled = nextMode === "rotate";
  }
}

function toggleBuildingTransformSpace() {
  if (!buildingTransformControls) {
    return;
  }
  const nextSpace = buildingTransformControls.space === "local" ? "world" : "local";
  buildingTransformControls.setSpace(nextSpace);
  if (buildingTransformSpaceButton) {
    buildingTransformSpaceButton.textContent = `Space: ${nextSpace === "local" ? "Local" : "World"}`;
  }
}

function refreshBuildingTransformGizmoAttachment() {
  if (!buildingTransformControls) {
    return;
  }
  const selectedId = String(selectedBuildingId || "").trim();
  const loaded = selectedId ? loadedBuildingAssets.get(selectedId) : null;
  if (loaded && loaded.object3d) {
    buildingTransformControls.attach(loaded.object3d);
    buildingTransformControls.visible = true;
  } else {
    buildingTransformControls.detach();
    buildingTransformControls.visible = false;
  }
}

function getCurrentPoseFromLoadedAsset(buildingId) {
  const id = String(buildingId || "").trim();
  if (!id) {
    return sanitizeBuildingPose({});
  }
  const loaded = loadedBuildingAssets.get(id);
  if (!loaded || !loaded.object3d || !loaded.basePosition || !loaded.baseQuaternion) {
    return getSavedBuildingPose(id);
  }

  const obj = loaded.object3d;
  const positionDelta = new THREE.Vector3().copy(obj.position).sub(loaded.basePosition);
  const deltaQuat = new THREE.Quaternion()
    .copy(loaded.baseQuaternion)
    .invert()
    .multiply(obj.quaternion);
  const deltaEuler = new THREE.Euler().setFromQuaternion(deltaQuat, "XYZ");

  return sanitizeBuildingPose({
    xOffset: positionDelta.x,
    yOffset: positionDelta.y,
    zOffset: positionDelta.z,
    rotXDeg: THREE.MathUtils.radToDeg(deltaEuler.x),
    rotYDeg: THREE.MathUtils.radToDeg(deltaEuler.y),
    rotZDeg: THREE.MathUtils.radToDeg(deltaEuler.z)
  });
}

function applyPoseToLoadedBuildingAsset(buildingId, pose) {
  const id = String(buildingId || "").trim();
  if (!id) {
    return;
  }
  const loaded = loadedBuildingAssets.get(id);
  if (!loaded || !loaded.object3d || !loaded.basePosition || !loaded.baseQuaternion) {
    return;
  }

  const safePose = sanitizeBuildingPose(pose);
  const obj = loaded.object3d;
  suspendTransformPoseSync = true;
  obj.position.copy(loaded.basePosition);
  obj.quaternion.copy(loaded.baseQuaternion);
  const deltaQuat = new THREE.Quaternion().setFromEuler(
    new THREE.Euler(
      THREE.MathUtils.degToRad(safePose.rotXDeg),
      THREE.MathUtils.degToRad(safePose.rotYDeg),
      THREE.MathUtils.degToRad(safePose.rotZDeg),
      "XYZ"
    )
  );
  obj.quaternion.multiply(deltaQuat);
  obj.position.x += safePose.xOffset;
  obj.position.y += safePose.yOffset;
  obj.position.z += safePose.zOffset;
  obj.updateMatrixWorld(true);
  suspendTransformPoseSync = false;
  refreshBuildingTransformGizmoAttachment();
  updateBuildingPoseReadout(safePose);
}

function saveSelectedBuildingPose() {
  if (!selectedBuildingId) {
    return;
  }
  const pose = getCurrentPoseFromLoadedAsset(selectedBuildingId);
  buildingPoseMap[selectedBuildingId] = pose;
  saveBuildingPosesToStorage();
  applyPoseToLoadedBuildingAsset(selectedBuildingId, pose);
  updateBuildingAssetStatus(`Saved transform for ${selectedBuildingId}.`);
}

function resetSelectedBuildingPose() {
  if (!selectedBuildingId) {
    return;
  }
  delete buildingPoseMap[selectedBuildingId];
  saveBuildingPosesToStorage();
  const neutral = sanitizeBuildingPose({});
  applyPoseToLoadedBuildingAsset(selectedBuildingId, neutral);
  updateBuildingPoseReadout(neutral);
  updateBuildingAssetStatus(`Reset transform for ${selectedBuildingId}.`);
}

function refreshBuildingPoseControlsForSelection() {
  const enabled = Boolean(selectedBuildingId);
  const hasLoadedAsset = enabled && loadedBuildingAssets.has(selectedBuildingId);
  if (buildingTransformSpaceButton) buildingTransformSpaceButton.disabled = !hasLoadedAsset;
  if (saveBuildingPoseButton) saveBuildingPoseButton.disabled = !hasLoadedAsset;
  if (resetBuildingPoseButton) resetBuildingPoseButton.disabled = !enabled;
  if (buildingPoseHintEl) {
    buildingPoseHintEl.style.opacity = enabled ? "1" : "0.65";
  }

  const pose = enabled ? getSavedBuildingPose(selectedBuildingId) : sanitizeBuildingPose({});
  if (enabled) {
    applyPoseToLoadedBuildingAsset(selectedBuildingId, pose);
    if (hasLoadedAsset) {
      setBuildingTransformMode(buildingTransformControls?.mode || "translate");
    } else {
      if (buildingTransformModeTranslateButton) buildingTransformModeTranslateButton.disabled = true;
      if (buildingTransformModeRotateButton) buildingTransformModeRotateButton.disabled = true;
    }
    if (buildingTransformSpaceButton && buildingTransformControls && hasLoadedAsset) {
      buildingTransformSpaceButton.textContent =
        `Space: ${buildingTransformControls.space === "local" ? "Local" : "World"}`;
    }
  } else {
    if (buildingTransformModeTranslateButton) buildingTransformModeTranslateButton.disabled = true;
    if (buildingTransformModeRotateButton) buildingTransformModeRotateButton.disabled = true;
  }
  updateBuildingPoseReadout(enabled && hasLoadedAsset ? getCurrentPoseFromLoadedAsset(selectedBuildingId) : pose);
  refreshBuildingTransformGizmoAttachment();
}

function ensureBuildingTransformControls() {
  if (buildingTransformControls) {
    return;
  }

  buildingTransformControls = new TransformControls(camera, renderer.domElement);
  buildingTransformControls.visible = false;
  buildingTransformControls.enabled = true;
  buildingTransformControls.setMode("translate");
  buildingTransformControls.setSpace("local");
  buildingTransformControls.size = 0.8;
  buildingTransformControls.showX = true;
  buildingTransformControls.showY = true;
  buildingTransformControls.showZ = true;

  buildingTransformControls.addEventListener("dragging-changed", (event) => {
    buildingTransformIsDragging = Boolean(event.value);
    pointerState.dragging = false;
  });

  buildingTransformControls.addEventListener("mouseDown", () => {
    suppressBuildingPickOnPointerUp = true;
  });

  buildingTransformControls.addEventListener("objectChange", () => {
    if (suspendTransformPoseSync || !selectedBuildingId) {
      return;
    }
    const pose = getCurrentPoseFromLoadedAsset(selectedBuildingId);
    updateBuildingPoseReadout(pose);
  });

  scene.add(buildingTransformControls);
}

function createBuildingAssetControls() {
  if (processBuildingAssetButton || !buildingNameListEl) {
    return;
  }

  const section = buildingNameListEl.closest(".menuSection");
  if (!section) {
    return;
  }
  const sectionBody = buildingsSectionBody || section;

  const controlsWrap = document.createElement("div");
  controlsWrap.style.marginTop = "10px";

  processBuildingAssetButton = document.createElement("button");
  processBuildingAssetButton.type = "button";
  processBuildingAssetButton.textContent = "Clean + Build Asset";
  processBuildingAssetButton.style.width = "100%";
  controlsWrap.appendChild(processBuildingAssetButton);

  loadBuildingAssetButton = document.createElement("button");
  loadBuildingAssetButton.type = "button";
  loadBuildingAssetButton.textContent = "Load Selected Asset";
  loadBuildingAssetButton.style.width = "100%";
  loadBuildingAssetButton.style.marginTop = "8px";
  controlsWrap.appendChild(loadBuildingAssetButton);

  buildingAssetStatusEl = document.createElement("p");
  buildingAssetStatusEl.style.margin = "8px 0 0 0";
  buildingAssetStatusEl.style.fontSize = "12px";
  buildingAssetStatusEl.style.lineHeight = "1.35";
  buildingAssetStatusEl.style.color = "#bcd4f7";
  buildingAssetStatusEl.textContent = "";
  controlsWrap.appendChild(buildingAssetStatusEl);

  const transformHeading = document.createElement("h3");
  transformHeading.className = "subheading";
  transformHeading.textContent = "Asset Transform";
  controlsWrap.appendChild(transformHeading);

  const modeRow = document.createElement("div");
  modeRow.className = "buttonRow";
  buildingTransformModeTranslateButton = document.createElement("button");
  buildingTransformModeTranslateButton.type = "button";
  buildingTransformModeTranslateButton.textContent = "Move (G)";
  buildingTransformModeTranslateButton.disabled = true;
  buildingTransformModeRotateButton = document.createElement("button");
  buildingTransformModeRotateButton.type = "button";
  buildingTransformModeRotateButton.textContent = "Rotate (R)";
  buildingTransformModeRotateButton.disabled = true;
  modeRow.appendChild(buildingTransformModeTranslateButton);
  modeRow.appendChild(buildingTransformModeRotateButton);
  controlsWrap.appendChild(modeRow);

  const spaceRow = document.createElement("div");
  spaceRow.className = "buttonRow";
  buildingTransformSpaceButton = document.createElement("button");
  buildingTransformSpaceButton.type = "button";
  buildingTransformSpaceButton.textContent = "Space: Local";
  buildingTransformSpaceButton.disabled = true;
  spaceRow.appendChild(buildingTransformSpaceButton);
  controlsWrap.appendChild(spaceRow);

  buildingPoseHintEl = document.createElement("p");
  buildingPoseHintEl.style.margin = "6px 0 4px 0";
  buildingPoseHintEl.style.fontSize = "12px";
  buildingPoseHintEl.style.lineHeight = "1.35";
  buildingPoseHintEl.style.color = "#9fb8de";
  buildingPoseHintEl.textContent = "Use gizmo handles in scene. Keys: G move, R rotate, X/Y/Z axis visibility.";
  controlsWrap.appendChild(buildingPoseHintEl);

  buildingPoseReadoutEl = document.createElement("pre");
  buildingPoseReadoutEl.style.margin = "0 0 8px 0";
  buildingPoseReadoutEl.style.fontSize = "12px";
  buildingPoseReadoutEl.style.lineHeight = "1.35";
  buildingPoseReadoutEl.style.color = "#d8e7ff";
  buildingPoseReadoutEl.style.whiteSpace = "pre-wrap";
  buildingPoseReadoutEl.style.wordBreak = "break-word";
  controlsWrap.appendChild(buildingPoseReadoutEl);

  const poseButtonRow = document.createElement("div");
  poseButtonRow.className = "buttonRow";
  saveBuildingPoseButton = document.createElement("button");
  saveBuildingPoseButton.type = "button";
  saveBuildingPoseButton.textContent = "Save Transform";
  saveBuildingPoseButton.disabled = true;
  resetBuildingPoseButton = document.createElement("button");
  resetBuildingPoseButton.type = "button";
  resetBuildingPoseButton.textContent = "Reset Transform";
  resetBuildingPoseButton.disabled = true;
  poseButtonRow.appendChild(saveBuildingPoseButton);
  poseButtonRow.appendChild(resetBuildingPoseButton);
  controlsWrap.appendChild(poseButtonRow);

  sectionBody.appendChild(controlsWrap);

  processBuildingAssetButton.addEventListener("click", async () => {
    if (!selectedBuildingId) {
      updateBuildingAssetStatus("Select a footprint first.", true);
      return;
    }

    const selected = buildingRecords.find((record) => record.id === selectedBuildingId);
    if (!selected || !Number.isInteger(selected.featureIndex)) {
      updateBuildingAssetStatus("Selected footprint is missing feature index.", true);
      return;
    }

    const footprintName = getSelectedFootprintDisplayName();
    if (!footprintName) {
      updateBuildingAssetStatus("Name the selected footprint before processing.", true);
      return;
    }

    processBuildingAssetButton.disabled = true;
    if (loadBuildingAssetButton) {
      loadBuildingAssetButton.disabled = true;
    }

    updateBuildingAssetStatus(`Processing asset for ${selectedBuildingId} using "${footprintName}"...`);

    try {
      const response = await fetch("/api/buildings/process-selected-asset", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          footprintId: selectedBuildingId,
          featureIndex: selected.featureIndex,
          footprintName
        })
      });

      const payload = await response.json();
      if (!response.ok || !payload || payload.error) {
        throw new Error((payload && payload.error) || `Server returned ${response.status}`);
      }

      await loadBuildingAssetByFootprintId(selectedBuildingId, true);
      updateBuildingAssetStatus(`Asset processed and loaded for ${selectedBuildingId}.`);
    } catch (error) {
      updateBuildingAssetStatus(`Asset processing failed: ${error.message}`, true);
    } finally {
      processBuildingAssetButton.disabled = false;
      if (loadBuildingAssetButton) {
        loadBuildingAssetButton.disabled = !selectedBuildingId;
      }
    }
  });

  loadBuildingAssetButton.addEventListener("click", async () => {
    if (!selectedBuildingId) {
      updateBuildingAssetStatus("Select a footprint first.", true);
      return;
    }
    loadBuildingAssetButton.disabled = true;
    updateBuildingAssetStatus(`Loading asset for ${selectedBuildingId}...`);
    try {
      await loadBuildingAssetByFootprintId(selectedBuildingId, true);
      updateBuildingAssetStatus(`Loaded asset for ${selectedBuildingId}.`);
    } catch (error) {
      updateBuildingAssetStatus(`Load failed: ${error.message}`, true);
    } finally {
      loadBuildingAssetButton.disabled = !selectedBuildingId;
    }
  });

  buildingTransformModeTranslateButton?.addEventListener("click", () => {
    setBuildingTransformMode("translate");
  });
  buildingTransformModeRotateButton?.addEventListener("click", () => {
    setBuildingTransformMode("rotate");
  });
  buildingTransformSpaceButton?.addEventListener("click", () => {
    toggleBuildingTransformSpace();
  });
  saveBuildingPoseButton?.addEventListener("click", () => {
    saveSelectedBuildingPose();
  });
  resetBuildingPoseButton?.addEventListener("click", () => {
    resetSelectedBuildingPose();
  });

  ensureBuildingTransformControls();
  setBuildingTransformMode("translate");
  updateBuildingPoseReadout(sanitizeBuildingPose({}));
}

function isTypingContextActive() {
  const active = document.activeElement;
  if (!active) {
    return false;
  }
  const tag = active.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    return true;
  }
  return Boolean(active.isContentEditable);
}

function resize() {
  const width = Math.max(1, viewerRoot.clientWidth);
  const height = Math.max(1, viewerRoot.clientHeight);
  renderer.setSize(width, height);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

function colorForHeight(ratio) {
  const color = new THREE.Color();
  color.setHSL(0.36 - ratio * 0.28, 0.65, 0.24 + ratio * 0.44);
  return color;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function getShrubDensityPercent() {
  const value = Number(shrubDensityInput?.value ?? 50);
  return clamp(value, 0, 100);
}

function getShrubDensityMultiplier() {
  return getShrubDensityPercent() / 50;
}

function getTreeDensityPercent() {
  const value = Number(treeDensityInput?.value ?? 50);
  return clamp(value, 0, 100);
}

function getTreeDensityMultiplier() {
  return getTreeDensityPercent() / 50;
}

function anchorOrderValue(anchor) {
  const x = Number.isFinite(anchor.x) ? anchor.x : 0;
  const y = Number.isFinite(anchor.y) ? anchor.y : 0;
  const n = Math.sin(x * 12.9898 + y * 78.233 + 0.12345) * 43758.5453;
  return n - Math.floor(n);
}

function duplicateAnchorWithJitter(anchor, duplicateIndex, jitterRadius) {
  const seed = anchorOrderValue(anchor) + duplicateIndex * 0.61803398875;
  const angle = seed * Math.PI * 2;
  const distance = ((seed * 1.7) % 1) * jitterRadius;
  return {
    ...anchor,
    x: anchor.x + Math.cos(angle) * distance,
    y: anchor.y + Math.sin(angle) * distance
  };
}

function selectAnchorsByDensity(anchors, multiplier, jitterRadius = 0.8) {
  if (!Array.isArray(anchors) || anchors.length === 0 || multiplier <= 0) {
    return [];
  }

  const ordered = anchors.slice();
  ordered.sort((a, b) => anchorOrderValue(a) - anchorOrderValue(b));
  if (multiplier <= 1) {
    const target = Math.max(0, Math.round(ordered.length * multiplier));
    return ordered.slice(0, target);
  }

  const cappedMultiplier = Math.min(multiplier, 2);
  const extraCount = Math.round(ordered.length * (cappedMultiplier - 1));
  const result = ordered.slice();
  for (let i = 0; i < extraCount; i += 1) {
    const baseAnchor = ordered[i % ordered.length];
    result.push(duplicateAnchorWithJitter(baseAnchor, i + 1, jitterRadius));
  }
  return result;
}

function randomNormal(mean = 0, stddev = 1) {
  let u = 0;
  let v = 0;
  while (u === 0) {
    u = Math.random();
  }
  while (v === 0) {
    v = Math.random();
  }
  const z = Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v);
  return mean + z * stddev;
}

function updateCameraPosition() {
  const sinPhi = Math.sin(cameraState.phi);
  const x = cameraState.target.x + cameraState.radius * sinPhi * Math.cos(cameraState.theta);
  const y = cameraState.target.y + cameraState.radius * sinPhi * Math.sin(cameraState.theta);
  const z = cameraState.target.z + cameraState.radius * Math.cos(cameraState.phi);
  camera.position.set(x, y, z);
  camera.lookAt(cameraState.target);
}

function panCameraByLocalAxes(rightMeters, forwardMeters) {
  const forward = new THREE.Vector3().subVectors(cameraState.target, camera.position);
  forward.z = 0;
  if (forward.lengthSq() < 1e-8) {
    forward.set(Math.cos(cameraState.theta), Math.sin(cameraState.theta), 0);
  }
  forward.normalize();
  const right = new THREE.Vector3(-forward.y, forward.x, 0).normalize();

  cameraState.target.addScaledVector(right, rightMeters);
  cameraState.target.addScaledVector(forward, forwardMeters);
  updateCameraPosition();
}

function applyKeyboardPan(deltaSeconds) {
  if (isTypingContextActive()) {
    return;
  }

  let moveForward = 0;
  let moveRight = 0;
  if (keyboardPanState.ArrowUp) {
    moveForward += 1;
  }
  if (keyboardPanState.ArrowDown) {
    moveForward -= 1;
  }
  if (keyboardPanState.ArrowRight) {
    moveRight -= 1;
  }
  if (keyboardPanState.ArrowLeft) {
    moveRight += 1;
  }

  const magnitude = Math.hypot(moveForward, moveRight);
  if (magnitude < 1e-8) {
    return;
  }

  moveForward /= magnitude;
  moveRight /= magnitude;

  const panMetersPerSecond = Math.max(8, cameraState.radius * 0.55);
  const step = panMetersPerSecond * deltaSeconds;
  panCameraByLocalAxes(moveRight * step, moveForward * step);
}

function updateHoverFromPointerEvent(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  const px = (event.clientX - rect.left) / Math.max(rect.width, 1);
  const py = (event.clientY - rect.top) / Math.max(rect.height, 1);
  hoverNdc.x = px * 2 - 1;
  hoverNdc.y = -(py * 2 - 1);
  hoverState.insideCanvas = px >= 0 && px <= 1 && py >= 0 && py <= 1;
}

function getHoveredRealCoordinates() {
  if (!terrainMesh || !demMeta || !hoverState.insideCanvas) {
    return null;
  }
  raycaster.setFromCamera(hoverNdc, camera);
  const hit = raycaster.intersectObject(terrainMesh, false)[0];
  if (!hit) {
    return null;
  }

  const verticalScale = Number(verticalScaleInput.value || 1);
  return {
    x: hit.point.x + demMeta.centerX,
    y: hit.point.y + demMeta.centerY,
    z: hit.point.z / verticalScale + demMeta.minElevation
  };
}

function getTerrainLocalHitAtPointer(event) {
  if (!terrainMesh) {
    return null;
  }

  const rect = renderer.domElement.getBoundingClientRect();
  const px = (event.clientX - rect.left) / Math.max(rect.width, 1);
  const py = (event.clientY - rect.top) / Math.max(rect.height, 1);
  if (px < 0 || px > 1 || py < 0 || py > 1) {
    return null;
  }

  const ndc = new THREE.Vector2(px * 2 - 1, -(py * 2 - 1));
  raycaster.setFromCamera(ndc, camera);
  const hit = raycaster.intersectObject(terrainMesh, false)[0];
  if (!hit || !hit.point) {
    return null;
  }

  return {
    x: Number(hit.point.x),
    y: Number(hit.point.y)
  };
}

async function copyTextToClipboard(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    await navigator.clipboard.writeText(text);
    return true;
  }

  const textArea = document.createElement("textarea");
  textArea.value = text;
  textArea.style.position = "fixed";
  textArea.style.left = "-10000px";
  textArea.style.top = "-10000px";
  document.body.appendChild(textArea);
  textArea.focus();
  textArea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    copied = false;
  }
  document.body.removeChild(textArea);
  return copied;
}

function updateCoordinateReadout() {
  if (!coordReadoutEl) {
    return;
  }

  if (performance.now() < copyFeedbackUntilMs) {
    coordReadoutEl.textContent = copyFeedbackText;
    return;
  }

  const coords = getHoveredRealCoordinates();
  if (!coords) {
    coordReadoutEl.textContent = "X -- | Y -- | Z --";
    return;
  }
  coordReadoutEl.textContent =
    `X ${coords.x.toFixed(2)} | Y ${coords.y.toFixed(2)} | Z ${coords.z.toFixed(2)} m`;
}

function applyVerticalScale(scale) {
  if (!terrainGeometry || !baseHeights) {
    return;
  }
  const positions = terrainGeometry.attributes.position.array;
  for (let i = 0; i < baseHeights.length; i += 1) {
    positions[i * 3 + 2] = baseHeights[i] * scale;
  }
  terrainGeometry.attributes.position.needsUpdate = true;
  terrainGeometry.computeVertexNormals();
}

function applyShrubVerticalScale(scale) {
  for (const instance of shrubInstances) {
    const baseScale = instance.userData.baseScale;
    const baseZ = instance.userData.baseZ;
    instance.position.z = baseZ * scale + 0.06;
    instance.scale.set(baseScale, baseScale, baseScale * scale);
  }
}

function applyTreeVerticalScale(scale) {
  for (const instance of treeInstances) {
    const baseScale = instance.userData.baseScale;
    const baseZ = instance.userData.baseZ;
    instance.position.z = baseZ * scale + 0.04;
    instance.scale.set(baseScale, baseScale, baseScale * scale);
  }
}

function clearBuildingsOverlay() {
  if (buildingLinesGroup) {
    scene.remove(buildingLinesGroup);
    const materials = new Set();
    buildingLinesGroup.traverse((node) => {
      if (node.isLine) {
        if (node.geometry) {
          node.geometry.dispose();
        }
        if (node.material) {
          materials.add(node.material);
        }
      }
    });
    for (const material of materials) {
      material.dispose();
    }
    buildingLinesGroup = null;
  }
  buildingRecords = [];
  selectedBuildingId = null;
  updateBuildingEditorUI();
}

function getBuildingName(buildingId) {
  return String(buildingNameMap[buildingId] || "").trim();
}

function getBuildingLabel(record) {
  const savedName = getBuildingName(record.id);
  if (savedName) {
    return `${savedName} (${record.id})`;
  }
  return record.id;
}

function updateBuildingRecordStyles() {
  for (const record of buildingRecords) {
    if (!record.material) {
      continue;
    }
    const selected = record.id === selectedBuildingId;
    record.material.color.setHex(selected ? BUILDING_SELECTED_COLOR : BUILDING_BASE_COLOR);
    record.material.opacity = selected ? 1.0 : 0.9;
    record.material.needsUpdate = true;
  }
}

function selectBuilding(buildingId) {
  if (!buildingId) {
    selectedBuildingId = null;
  } else if (buildingRecords.some((record) => record.id === buildingId)) {
    selectedBuildingId = buildingId;
  } else {
    selectedBuildingId = null;
  }
  updateBuildingRecordStyles();
  updateBuildingEditorUI();
}

function updateBuildingEditorUI() {
  if (buildingSelectionLabel) {
    if (!selectedBuildingId) {
      buildingSelectionLabel.textContent = "No footprint selected";
    } else {
      const selected = buildingRecords.find((record) => record.id === selectedBuildingId);
      buildingSelectionLabel.textContent = selected
        ? `Selected: ${getBuildingLabel(selected)}`
        : "No footprint selected";
    }
  }

  if (buildingNameInput) {
    if (selectedBuildingId) {
      buildingNameInput.value = getBuildingName(selectedBuildingId);
      buildingNameInput.disabled = false;
    } else {
      buildingNameInput.value = "";
      buildingNameInput.disabled = true;
    }
  }

  if (saveBuildingNameButton) {
    saveBuildingNameButton.disabled = !selectedBuildingId;
  }
  if (clearBuildingNameButton) {
    clearBuildingNameButton.disabled = !selectedBuildingId;
  }
  if (processBuildingAssetButton) {
    processBuildingAssetButton.disabled = !selectedBuildingId;
  }
  if (loadBuildingAssetButton) {
    loadBuildingAssetButton.disabled = !selectedBuildingId;
  }
  refreshBuildingPoseControlsForSelection();

  if (!buildingNameListEl) {
    return;
  }

  buildingNameListEl.innerHTML = "";
  const namedRecords = buildingRecords
    .filter((record) => getBuildingName(record.id))
    .sort((a, b) => getBuildingName(a.id).localeCompare(getBuildingName(b.id)));

  if (namedRecords.length === 0) {
    const empty = document.createElement("p");
    empty.className = "buildingNameEmpty";
    empty.textContent = "No named footprints yet.";
    buildingNameListEl.appendChild(empty);
    return;
  }

  for (const record of namedRecords) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "buildingNameItem";
    if (record.id === selectedBuildingId) {
      btn.classList.add("active");
    }
    btn.textContent = getBuildingLabel(record);
    btn.addEventListener("click", () => {
      selectBuilding(record.id);
    });
    buildingNameListEl.appendChild(btn);
  }
}

function saveSelectedBuildingName() {
  if (!selectedBuildingId || !buildingNameInput) {
    return;
  }
  const name = buildingNameInput.value.trim();
  if (name) {
    buildingNameMap[selectedBuildingId] = name;
  } else {
    delete buildingNameMap[selectedBuildingId];
  }
  saveBuildingNamesToStorage();
  updateBuildingEditorUI();
}

function clearSelectedBuildingName() {
  if (!selectedBuildingId) {
    return;
  }
  delete buildingNameMap[selectedBuildingId];
  saveBuildingNamesToStorage();
  updateBuildingEditorUI();
}

function pickBuildingAtPointer(event) {
  if (!buildingLinesGroup || !isBuildingFootprintsVisible()) {
    return null;
  }

  const rect = renderer.domElement.getBoundingClientRect();
  const px = (event.clientX - rect.left) / Math.max(rect.width, 1);
  const py = (event.clientY - rect.top) / Math.max(rect.height, 1);
  if (px < 0 || px > 1 || py < 0 || py > 1) {
    return null;
  }

  const ndc = new THREE.Vector2(px * 2 - 1, -(py * 2 - 1));
  const lineThreshold = Math.max(0.6, cameraState.radius * 0.005);
  raycaster.params.Line.threshold = lineThreshold;
  raycaster.setFromCamera(ndc, camera);
  const hits = raycaster.intersectObjects(buildingLinesGroup.children, false);
  if (!hits || hits.length === 0) {
    return null;
  }
  for (const hit of hits) {
    const id = hit.object && hit.object.userData ? hit.object.userData.footprintId : null;
    if (id) {
      return id;
    }
  }
  return null;
}

function pickSoilPolygonAtPointer(event) {
  if (!isSoilsVisible() || !Array.isArray(soilPolygonsLocal) || soilPolygonsLocal.length === 0) {
    return null;
  }

  const localPoint = getTerrainLocalHitAtPointer(event);
  if (!localPoint) {
    return null;
  }

  const polygon = findSoilPolygonAtLocal(localPoint.x, localPoint.y);
  if (!polygon) {
    return null;
  }

  if (soilSelectedClassFilter && polygon.classLabel !== soilSelectedClassFilter) {
    return null;
  }

  return polygon;
}

function computeRingBBox(ring) {
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const p of ring) {
    minX = Math.min(minX, p[0]);
    minY = Math.min(minY, p[1]);
    maxX = Math.max(maxX, p[0]);
    maxY = Math.max(maxY, p[1]);
  }
  return { minX, minY, maxX, maxY };
}

function computeRingCentroid(ring) {
  if (!Array.isArray(ring) || ring.length === 0) {
    return [0, 0];
  }
  let sumX = 0;
  let sumY = 0;
  let count = 0;
  for (const p of ring) {
    sumX += p[0];
    sumY += p[1];
    count += 1;
  }
  if (count <= 0) {
    return [0, 0];
  }
  return [sumX / count, sumY / count];
}

function isPointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const xi = ring[i][0];
    const yi = ring[i][1];
    const xj = ring[j][0];
    const yj = ring[j][1];
    const intersects =
      (yi > y) !== (yj > y) &&
      x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-12) + xi;
    if (intersects) {
      inside = !inside;
    }
  }
  return inside;
}

function squaredDistanceToSegment(pointX, pointY, ax, ay, bx, by) {
  const vx = bx - ax;
  const vy = by - ay;
  const wx = pointX - ax;
  const wy = pointY - ay;
  const vv = vx * vx + vy * vy;
  let t = vv > 0 ? (wx * vx + wy * vy) / vv : 0;
  t = clamp(t, 0, 1);
  const closestX = ax + t * vx;
  const closestY = ay + t * vy;
  const dx = pointX - closestX;
  const dy = pointY - closestY;
  return {
    distSq: dx * dx + dy * dy,
    closestX,
    closestY
  };
}

function nearestBoundaryPoint(pointX, pointY, polygon) {
  let best = null;
  const allRings = [polygon.outer, ...polygon.holes];
  for (const ring of allRings) {
    if (!Array.isArray(ring) || ring.length < 2) {
      continue;
    }
    for (let i = 0; i < ring.length; i += 1) {
      const a = ring[i];
      const b = ring[(i + 1) % ring.length];
      const candidate = squaredDistanceToSegment(pointX, pointY, a[0], a[1], b[0], b[1]);
      if (!best || candidate.distSq < best.distSq) {
        best = candidate;
      }
    }
  }
  return best;
}

function isInsideBuildingPolygon(x, y, polygon) {
  const b = polygon.bbox;
  if (x < b.minX || x > b.maxX || y < b.minY || y > b.maxY) {
    return false;
  }
  if (!isPointInRing(x, y, polygon.outer)) {
    return false;
  }
  for (const hole of polygon.holes) {
    if (isPointInRing(x, y, hole)) {
      return false;
    }
  }
  return true;
}

function findBuildingSeparationViolation(x, y, requiredDistance) {
  let worst = null;
  for (const polygon of buildingPolygonsLocal) {
    const b = polygon.bbox;
    const nearBBox =
      x >= b.minX - requiredDistance &&
      x <= b.maxX + requiredDistance &&
      y >= b.minY - requiredDistance &&
      y <= b.maxY + requiredDistance;
    if (!nearBBox) {
      continue;
    }

    const nearest = nearestBoundaryPoint(x, y, polygon);
    if (!nearest) {
      continue;
    }
    const dist = Math.sqrt(Math.max(0, nearest.distSq));
    const inside = isInsideBuildingPolygon(x, y, polygon);
    const violation = inside ? (requiredDistance + dist) : (requiredDistance - dist);
    if (violation <= 0) {
      continue;
    }

    let dirX = inside ? (nearest.closestX - x) : (x - nearest.closestX);
    let dirY = inside ? (nearest.closestY - y) : (y - nearest.closestY);
    let dirLen = Math.hypot(dirX, dirY);
    if (dirLen < 1e-6) {
      dirX = x - polygon.centroid[0];
      dirY = y - polygon.centroid[1];
      dirLen = Math.hypot(dirX, dirY);
      if (dirLen < 1e-6) {
        dirX = 1;
        dirY = 0;
        dirLen = 1;
      }
    }
    dirX /= dirLen;
    dirY /= dirLen;

    if (!worst || violation > worst.violation) {
      worst = { violation, dirX, dirY };
    }
  }
  return worst;
}

function enforceBuildingEdgeSeparation(x, y, requiredDistance) {
  if (!Array.isArray(buildingPolygonsLocal) || buildingPolygonsLocal.length === 0) {
    return { x, y, pushed: false };
  }

  let px = x;
  let py = y;
  let moved = false;

  for (let i = 0; i < 12; i += 1) {
    const violation = findBuildingSeparationViolation(px, py, requiredDistance);
    if (!violation) {
      break;
    }
    const moveBy = violation.violation + BUILDING_PUSH_EPSILON;
    px += violation.dirX * moveBy;
    py += violation.dirY * moveBy;
    moved = true;
  }

  return { x: px, y: py, pushed: moved };
}

function findHydrologySeparationViolation(x, y, requiredDistance) {
  if (!Array.isArray(hydrologyPolylinesLocal) || hydrologyPolylinesLocal.length === 0) {
    return null;
  }

  let worst = null;
  for (const line of hydrologyPolylinesLocal) {
    if (!Array.isArray(line) || line.length < 2) {
      continue;
    }

    for (let i = 0; i < line.length - 1; i += 1) {
      const a = line[i];
      const b = line[i + 1];
      const candidate = squaredDistanceToSegment(x, y, a[0], a[1], b[0], b[1]);
      const dist = Math.sqrt(Math.max(0, candidate.distSq));
      const violation = requiredDistance - dist;
      if (violation <= 0) {
        continue;
      }

      let dirX = x - candidate.closestX;
      let dirY = y - candidate.closestY;
      let dirLen = Math.hypot(dirX, dirY);

      if (dirLen < 1e-6) {
        const segX = b[0] - a[0];
        const segY = b[1] - a[1];
        const segLen = Math.hypot(segX, segY);
        if (segLen > 1e-6) {
          dirX = -segY / segLen;
          dirY = segX / segLen;
          dirLen = 1;
        } else {
          dirX = 1;
          dirY = 0;
          dirLen = 1;
        }
      }

      dirX /= dirLen;
      dirY /= dirLen;

      if (!worst || violation > worst.violation) {
        worst = { violation, dirX, dirY };
      }
    }
  }

  return worst;
}

function enforceHydrologySeparation(x, y, requiredDistance) {
  if (!Array.isArray(hydrologyPolylinesLocal) || hydrologyPolylinesLocal.length === 0) {
    return { x, y, pushed: false };
  }

  let px = x;
  let py = y;
  let moved = false;

  for (let i = 0; i < 12; i += 1) {
    const violation = findHydrologySeparationViolation(px, py, requiredDistance);
    if (!violation) {
      break;
    }

    const moveBy = violation.violation + HYDROLOGY_PUSH_EPSILON;
    px += violation.dirX * moveBy;
    py += violation.dirY * moveBy;
    moved = true;
  }

  return { x: px, y: py, pushed: moved };
}

function computeTemplateFootprintRadius(template) {
  const bbox = new THREE.Box3().setFromObject(template);
  if (!Number.isFinite(bbox.min.x) || !Number.isFinite(bbox.max.x) ||
      !Number.isFinite(bbox.min.y) || !Number.isFinite(bbox.max.y)) {
    return 0.5;
  }

  const corners = [
    [bbox.min.x, bbox.min.y],
    [bbox.min.x, bbox.max.y],
    [bbox.max.x, bbox.min.y],
    [bbox.max.x, bbox.max.y]
  ];
  let radius = 0;
  for (const c of corners) {
    radius = Math.max(radius, Math.hypot(c[0], c[1]));
  }
  return Math.max(radius, 0.1);
}

function toLocalRings(ringsCoords) {
  if (!Array.isArray(ringsCoords) || ringsCoords.length === 0) {
    return null;
  }

  const localRings = [];
  for (const ring of ringsCoords) {
    if (!Array.isArray(ring) || ring.length < 3) {
      continue;
    }
    const localRing = [];
    for (const coord of ring) {
      if (!Array.isArray(coord) || coord.length < 2) {
        continue;
      }
      const x = Number(coord[0]);
      const y = Number(coord[1]);
      if (!Number.isFinite(x) || !Number.isFinite(y)) {
        continue;
      }
      localRing.push(toViewerLocalFromUtm(x, y));
    }
    if (localRing.length >= 3) {
      localRings.push(localRing);
    }
  }

  if (localRings.length === 0) {
    return null;
  }
  return localRings;
}

function rebuildBuildingPolygonCache() {
  buildingPolygonsLocal = [];
  if (!buildingFeaturesGeoJson || !Array.isArray(buildingFeaturesGeoJson.features)) {
    return;
  }

  for (const feature of buildingFeaturesGeoJson.features) {
    const geometry = feature && feature.geometry;
    if (!geometry || !geometry.type || !Array.isArray(geometry.coordinates)) {
      continue;
    }

    const addPolygon = (polygonCoords) => {
      const localRings = toLocalRings(polygonCoords);
      if (!localRings || localRings.length === 0) {
        return;
      }

      const outer = localRings[0];
      const holes = localRings.slice(1);
      buildingPolygonsLocal.push({
        outer,
        holes,
        bbox: computeRingBBox(outer),
        centroid: computeRingCentroid(outer)
      });
    };

    if (geometry.type === "Polygon") {
      addPolygon(geometry.coordinates);
    } else if (geometry.type === "MultiPolygon") {
      for (const polygonCoords of geometry.coordinates) {
        addPolygon(polygonCoords);
      }
    }
  }
}

function sampleTerrainHeightAtLocal(localX, localY) {
  if (!baseHeights || demGridWidth <= 1 || demGridHeight <= 1) {
    return 0;
  }

  const col = Math.round((localX + terrainWidthMeters / 2) / demGridXStep);
  const row = Math.round((terrainHeightMeters / 2 - localY) / demGridYStep);
  const clampedCol = clamp(col, 0, demGridWidth - 1);
  const clampedRow = clamp(row, 0, demGridHeight - 1);
  const index = clampedRow * demGridWidth + clampedCol;
  const z = baseHeights[index];
  return Number.isFinite(z) ? z : 0;
}

function ensureSoilGroup() {
  if (!soilGroup) {
    soilGroup = new THREE.Group();
    soilGroup.name = "soil-overlay";
    scene.add(soilGroup);
    soilGroup.visible = isSoilsVisible();
  }
  return soilGroup;
}

function clearSoilMeshes() {
  if (!soilGroup) {
    soilMeshes = [];
    return;
  }

  for (const mesh of soilMeshes) {
    soilGroup.remove(mesh);
    if (mesh.geometry) {
      mesh.geometry.dispose();
    }
    if (mesh.material) {
      mesh.material.dispose();
    }
  }
  soilMeshes = [];
}

function resetSoilsData() {
  soilFeaturesGeoJson = null;
  soilLegendData = [];
  soilPolygonsLocal = [];
  soilSelectedClassFilter = null;
  soilSelectedPolygon = null;
  clearSoilMeshes();
  applySoilsToTerrainColors();
  updateSoilLegendDetails();
  applySoilsVisibility();
}

function toLocalRingFromWorldCoords(ringCoords) {
  if (!Array.isArray(ringCoords) || ringCoords.length < 3 || !demMeta) {
    return null;
  }

  const out = [];
  for (const coord of ringCoords) {
    if (!Array.isArray(coord) || coord.length < 2) {
      continue;
    }
    const x = Number(coord[0]);
    const y = Number(coord[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      continue;
    }
    out.push([x - demMeta.centerX, y - demMeta.centerY]);
  }

  if (out.length >= 2) {
    const first = out[0];
    const last = out[out.length - 1];
    if (Math.abs(first[0] - last[0]) < 1e-9 && Math.abs(first[1] - last[1]) < 1e-9) {
      out.pop();
    }
  }

  return out.length >= 3 ? out : null;
}

function isPointInsideSoilPolygon(x, y, polygon) {
  if (!polygon || !polygon.bbox) {
    return false;
  }
  const b = polygon.bbox;
  if (x < b.minX || x > b.maxX || y < b.minY || y > b.maxY) {
    return false;
  }
  if (!isPointInRing(x, y, polygon.outer)) {
    return false;
  }
  for (const hole of polygon.holes || []) {
    if (isPointInRing(x, y, hole)) {
      return false;
    }
  }
  return true;
}

function findSoilPolygonAtLocal(localX, localY) {
  if (!Array.isArray(soilPolygonsLocal) || soilPolygonsLocal.length === 0) {
    return null;
  }
  for (let i = soilPolygonsLocal.length - 1; i >= 0; i -= 1) {
    const polygon = soilPolygonsLocal[i];
    if (isPointInsideSoilPolygon(localX, localY, polygon)) {
      return polygon;
    }
  }
  return null;
}

function updateSoilLegendDetails(polygon = soilSelectedPolygon) {
  if (!soilLegendDetailsEl) {
    return;
  }

  if (!polygon) {
    if (soilSelectedClassFilter) {
      const selectedLegend = soilLegendData.find((item) => item.label === soilSelectedClassFilter);
      if (selectedLegend) {
        soilLegendDetailsEl.textContent = `Class filter: ${selectedLegend.label}\nPolygons: ${Number(selectedLegend.count || 0).toLocaleString()}`;
        return;
      }
    }
    soilLegendDetailsEl.textContent = "Click a soil polygon to view attributes.";
    return;
  }

  const props = polygon.properties || {};
  const getValue = (...keys) => {
    for (const key of keys) {
      const value = props[key];
      if (value !== null && value !== undefined && String(value).trim() !== "") {
        return String(value);
      }
    }
    return null;
  };

  const lines = [];
  const missing = [];
  const addField = (label, ...keys) => {
    const value = getValue(...keys);
    if (value) {
      lines.push(`${label}: ${value}`);
    } else {
      missing.push(label);
    }
  };

  addField("Class", "soil_class");
  addField("Map Unit Key", "soil_mukey", "MUKEY");
  addField("Map Unit Symbol", "soil_musym", "MUSYM");
  addField("Map Unit Name", "soil_muname", "MUNAME");
  addField("Description", "soil_mutext", "MUTEXT");
  addField("Hydrologic Group", "soil_hydgrpdcd", "HYDGRPDCD");
  addField("Drainage Class", "soil_drclassdcd", "DRCLASSDCD");
  addField("Flood Frequency", "soil_flodfreqdcd", "FLODFREQDCD");
  addField("Area Symbol", "AREASYMBOL");
  addField("Spatial Version", "SPATIALVER", "SPATIALVERSION");

  if (lines.length === 0) {
    lines.push("No attributes available for this polygon.");
  }
  if (missing.length > 0) {
    lines.push(`Missing attributes: ${missing.join(", ")}`);
  }
  soilLegendDetailsEl.textContent = lines.join("\n");
}

function rebuildSoilPolygonsLocal() {
  if (!soilFeaturesGeoJson || !demMeta) {
    soilPolygonsLocal = [];
    return;
  }

  const features = Array.isArray(soilFeaturesGeoJson.features) ? soilFeaturesGeoJson.features : [];
  const out = [];

  for (const feature of features) {
    const geometry = feature?.geometry;
    const props = feature?.properties || {};
    const polygons = [];
    collectSoilPolygons(geometry, polygons);

    for (const polygonCoords of polygons) {
      const outer = toLocalRingFromWorldCoords(polygonCoords[0]);
      if (!outer) {
        continue;
      }
      const holes = [];
      for (let i = 1; i < polygonCoords.length; i += 1) {
        const hole = toLocalRingFromWorldCoords(polygonCoords[i]);
        if (hole) {
          holes.push(hole);
        }
      }
      out.push({
        outer,
        holes,
        bbox: computeRingBBox(outer),
        color: String(props.soil_color || "#8c8c8c"),
        classLabel: String(props.soil_class || "Unknown"),
        properties: props
      });
    }
  }

  soilPolygonsLocal = out;
}

function applySoilsToTerrainColors() {
  if (!terrainGeometry || !terrainBaseColors) {
    return;
  }

  const colorAttr = terrainGeometry.getAttribute("color");
  if (!colorAttr || !colorAttr.array) {
    return;
  }

  const colors = colorAttr.array;
  colors.set(terrainBaseColors);

  if (!isSoilsVisible() || !Array.isArray(soilPolygonsLocal) || soilPolygonsLocal.length === 0) {
    colorAttr.needsUpdate = true;
    return;
  }

  for (const polygon of soilPolygonsLocal) {
    if (soilSelectedClassFilter && polygon.classLabel !== soilSelectedClassFilter) {
      continue;
    }

    const b = polygon.bbox;
    const colMin = clamp(Math.floor((b.minX + terrainWidthMeters / 2) / demGridXStep), 0, demGridWidth - 1);
    const colMax = clamp(Math.ceil((b.maxX + terrainWidthMeters / 2) / demGridXStep), 0, demGridWidth - 1);
    const rowMin = clamp(Math.floor((terrainHeightMeters / 2 - b.maxY) / demGridYStep), 0, demGridHeight - 1);
    const rowMax = clamp(Math.ceil((terrainHeightMeters / 2 - b.minY) / demGridYStep), 0, demGridHeight - 1);

    const tint = new THREE.Color(polygon.color || "#8c8c8c");
    if (soilSelectedPolygon === polygon) {
      tint.lerp(new THREE.Color("#ffffff"), 0.35);
    }
    for (let row = rowMin; row <= rowMax; row += 1) {
      const y = terrainHeightMeters / 2 - row * demGridYStep;
      for (let col = colMin; col <= colMax; col += 1) {
        const x = -terrainWidthMeters / 2 + col * demGridXStep;
        if (!isPointInsideSoilPolygon(x, y, polygon)) {
          continue;
        }
        const index = (row * demGridWidth + col) * 3;
        colors[index] = tint.r;
        colors[index + 1] = tint.g;
        colors[index + 2] = tint.b;
      }
    }
  }

  colorAttr.needsUpdate = true;
}

function buildSoilPolygonMesh(polygonCoords, color, verticalScale) {
  if (!Array.isArray(polygonCoords) || polygonCoords.length === 0 || !demMeta) {
    return null;
  }

  const outer = toLocalRingFromWorldCoords(polygonCoords[0]);
  if (!outer) {
    return null;
  }

  const shape = new THREE.Shape(outer.map((pair) => new THREE.Vector2(pair[0], pair[1])));
  for (let i = 1; i < polygonCoords.length; i += 1) {
    const hole = toLocalRingFromWorldCoords(polygonCoords[i]);
    if (!hole) {
      continue;
    }
    const holePath = new THREE.Path(hole.map((pair) => new THREE.Vector2(pair[0], pair[1])));
    shape.holes.push(holePath);
  }

  const geometry = new THREE.ShapeGeometry(shape);
  const positions = geometry.getAttribute("position");
  const scale = Math.max(0.01, Number(verticalScale || 1));
  for (let i = 0; i < positions.count; i += 1) {
    const x = positions.getX(i);
    const y = positions.getY(i);
    const z = sampleTerrainHeightAtLocal(x, y) * scale + 0.03;
    positions.setXYZ(i, x, y, z);
  }
  positions.needsUpdate = true;
  geometry.computeBoundingSphere();

  const material = new THREE.MeshBasicMaterial({
    color: new THREE.Color(String(color || "#8c8c8c")),
    transparent: true,
    opacity: 0.42,
    side: THREE.DoubleSide,
    depthWrite: false
  });

  const mesh = new THREE.Mesh(geometry, material);
  mesh.renderOrder = 1;
  return mesh;
}

function collectSoilPolygons(geometry, out) {
  if (!geometry || !geometry.type || !Array.isArray(geometry.coordinates)) {
    return;
  }
  if (geometry.type === "Polygon") {
    out.push(geometry.coordinates);
    return;
  }
  if (geometry.type === "MultiPolygon") {
    for (const polygon of geometry.coordinates) {
      if (Array.isArray(polygon)) {
        out.push(polygon);
      }
    }
  }
}

function buildSoilLegendFromFeatures(geojson) {
  const features = Array.isArray(geojson?.features) ? geojson.features : [];
  const map = new Map();
  for (const feature of features) {
    const props = feature?.properties || {};
    const label = String(props.soil_class || "Unknown").trim() || "Unknown";
    const color = String(props.soil_color || "#8c8c8c").trim() || "#8c8c8c";
    const existing = map.get(label) || { label, color, count: 0 };
    existing.count += 1;
    map.set(label, existing);
  }

  return Array.from(map.values()).sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function renderSoilLegend() {
  if (!soilLegendItemsEl) {
    return;
  }
  soilLegendItemsEl.innerHTML = "";

  const selectedClassFromPolygon = String(soilSelectedPolygon?.classLabel || "");

  for (const item of soilLegendData) {
    const row = document.createElement("div");
    row.className = "soilLegendItem";
    if ((soilSelectedClassFilter && soilSelectedClassFilter === item.label) ||
        (!soilSelectedClassFilter && selectedClassFromPolygon && selectedClassFromPolygon === item.label)) {
      row.classList.add("isActive");
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "soilLegendButton";
    button.title = `Filter by ${item.label}`;
    button.addEventListener("click", () => {
      soilSelectedClassFilter = soilSelectedClassFilter === item.label ? null : item.label;
      soilSelectedPolygon = null;
      applySoilsToTerrainColors();
      renderSoilLegend();
      updateSoilLegendDetails();
    });

    const swatch = document.createElement("span");
    swatch.className = "soilLegendSwatch";
    swatch.style.backgroundColor = String(item.color || "#8c8c8c");

    const label = document.createElement("span");
    label.className = "soilLegendLabel";
    label.textContent = String(item.label || "Unknown");

    const count = document.createElement("span");
    count.className = "soilLegendCount";
    count.textContent = Number(item.count || 0).toLocaleString();

    button.appendChild(swatch);
    button.appendChild(label);
    button.appendChild(count);
    row.appendChild(button);
    soilLegendItemsEl.appendChild(row);
  }

  updateSoilLegendDetails();
  applySoilsVisibility();
}

function renderSoilOverlay() {
  clearSoilMeshes();
  if (!demMeta || !soilFeaturesGeoJson) {
    return;
  }

  const features = Array.isArray(soilFeaturesGeoJson.features) ? soilFeaturesGeoJson.features : [];
  if (features.length === 0) {
    soilPolygonsLocal = [];
    applySoilsToTerrainColors();
    soilsStatusText = "soils: no polygons";
    refreshStatus();
    renderSoilLegend();
    return;
  }

  rebuildSoilPolygonsLocal();
  const polygonCount = soilPolygonsLocal.length;

  applySoilsVisibility();
  soilsStatusText = `soils: ${polygonCount.toLocaleString()} polygon${polygonCount === 1 ? "" : "s"}`;
  refreshStatus();
  renderSoilLegend();
}

async function loadSoils() {
  if (!demMeta) {
    soilsStatusText = "soils unavailable (DEM is required)";
    refreshStatus();
    resetSoilsData();
    return;
  }

  soilsStatusText = "Loading soils...";
  refreshStatus();

  let status;
  try {
    status = await fetchJson("/api/soils/status");
  } catch (error) {
    status = null;
  }

  let soilsData = null;
  let legendData = null;

  if (status && status.available && status.preferredPath) {
    try {
      soilsData = await fetchJson(String(status.preferredPath));
    } catch (error) {
      const fallback = String(status.fallbackPath || "");
      if (fallback) {
        soilsData = await fetchJson(fallback);
      } else {
        throw error;
      }
    }

    if (status.legendPath) {
      try {
        const legendPayload = await fetchJson(String(status.legendPath));
        legendData = Array.isArray(legendPayload?.classes) ? legendPayload.classes : null;
      } catch (error) {
        legendData = null;
      }
    }
  } else {
    const fallbackCandidates = [
      "/data/soils/soils_clipped_local.geojson",
      "/data/soils/soils_clipped.geojson"
    ];
    let lastError = null;
    for (const candidate of fallbackCandidates) {
      try {
        soilsData = await fetchJson(candidate);
        break;
      } catch (error) {
        lastError = error;
      }
    }
    if (!soilsData) {
      resetSoilsData();
      if (lastError && /404/.test(String(lastError.message || ""))) {
        soilsStatusText = "soils: no data";
      } else if (lastError) {
        throw lastError;
      } else {
        soilsStatusText = "soils: no data";
      }
      refreshStatus();
      return;
    }
  }

  soilFeaturesGeoJson = soilsData;
  soilLegendData = Array.isArray(legendData) && legendData.length > 0
    ? legendData
    : buildSoilLegendFromFeatures(soilsData);
  renderSoilOverlay();
}

function ensureHydrologyGroup() {
  if (!hydrologyGroup) {
    hydrologyGroup = new THREE.Group();
    hydrologyGroup.name = "hydrology";
    scene.add(hydrologyGroup);
    hydrologyGroup.visible = isHydrologyVisible();
  }
  return hydrologyGroup;
}

function getHydrologyDepthMeters() {
  const rawDepth = Number(hydrologyDepthInput?.value || HYDROLOGY_DEPTH_SLIDER_MIDPOINT);
  if (!Number.isFinite(rawDepth)) {
    return 0;
  }
  return rawDepth - HYDROLOGY_DEPTH_SLIDER_MIDPOINT;
}

function getHydrologyWidthMeters() {
  const rawWidth = Number(hydrologyWidthInput?.value || 4);
  if (!Number.isFinite(rawWidth) || rawWidth <= 0) {
    return 4;
  }
  return rawWidth;
}

function createHydrologyFlowTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 32;
  canvas.height = 256;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return null;
  }

  const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height);
  gradient.addColorStop(0, "rgba(75, 170, 255, 0.95)");
  gradient.addColorStop(1, "rgba(30, 100, 220, 0.9)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.strokeStyle = "rgba(210, 240, 255, 0.55)";
  ctx.lineWidth = 1.2;
  for (let y = 0; y < canvas.height; y += 18) {
    ctx.beginPath();
    ctx.moveTo(2, y);
    ctx.bezierCurveTo(10, y + 3, 22, y - 3, 30, y + 2);
    ctx.stroke();
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(1, 6);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function ensureHydrologyMaterial() {
  if (hydrologyFlowMaterial) {
    return hydrologyFlowMaterial;
  }

  hydrologyFlowTexture = createHydrologyFlowTexture();
  hydrologyFlowMaterial = new THREE.MeshStandardMaterial({
    color: 0x5aa9ff,
    map: hydrologyFlowTexture || null,
    transparent: true,
    opacity: 0.78,
    side: THREE.DoubleSide,
    depthWrite: false,
    metalness: 0.05,
    roughness: 0.65
  });
  return hydrologyFlowMaterial;
}

function clearHydrologyMeshes() {
  if (!hydrologyGroup) {
    hydrologyMeshes = [];
    return;
  }

  for (const mesh of hydrologyMeshes) {
    hydrologyGroup.remove(mesh);
    if (mesh.geometry) {
      mesh.geometry.dispose();
    }
  }
  hydrologyMeshes = [];
}

function resetHydrologyData() {
  hydrologyFeaturesGeoJson = null;
  hydrologyPolylinesWorld = [];
  hydrologyPolylinesLocal = [];
  clearHydrologyMeshes();
  applyHydrologyVisibility();
}

function collectHydrologyLineCoordinates(geometry, out) {
  if (!geometry || !geometry.type || !Array.isArray(geometry.coordinates)) {
    return;
  }

  if (geometry.type === "LineString") {
    out.push(geometry.coordinates);
    return;
  }
  if (geometry.type === "MultiLineString") {
    for (const line of geometry.coordinates) {
      out.push(line);
    }
    return;
  }
  if (geometry.type === "Polygon") {
    if (Array.isArray(geometry.coordinates[0])) {
      out.push(geometry.coordinates[0]);
    }
    return;
  }
  if (geometry.type === "MultiPolygon") {
    for (const polygon of geometry.coordinates) {
      if (Array.isArray(polygon) && Array.isArray(polygon[0])) {
        out.push(polygon[0]);
      }
    }
  }
}

function parseHydrologyPolylinesWorld(geojson) {
  const features = Array.isArray(geojson?.features) ? geojson.features : [];
  const linesRaw = [];
  for (const feature of features) {
    collectHydrologyLineCoordinates(feature?.geometry, linesRaw);
  }

  const lines = [];
  for (const line of linesRaw) {
    if (!Array.isArray(line) || line.length < 2) {
      continue;
    }
    const points = [];
    for (const coord of line) {
      if (!Array.isArray(coord) || coord.length < 2) {
        continue;
      }
      const x = Number(coord[0]);
      const y = Number(coord[1]);
      if (!Number.isFinite(x) || !Number.isFinite(y)) {
        continue;
      }
      const prev = points[points.length - 1];
      if (prev && Math.abs(prev.x - x) < 1e-9 && Math.abs(prev.y - y) < 1e-9) {
        continue;
      }
      points.push({ x, y });
    }
    if (points.length >= 2) {
      lines.push(points);
    }
  }
  return lines;
}

function rebuildHydrologyPolylinesLocal() {
  if (!demMeta || !Array.isArray(hydrologyPolylinesWorld) || hydrologyPolylinesWorld.length === 0) {
    hydrologyPolylinesLocal = [];
    return;
  }

  hydrologyPolylinesLocal = hydrologyPolylinesWorld
    .map((line) => line
      .map((point) => [point.x - demMeta.centerX, point.y - demMeta.centerY])
      .filter((pair) => Number.isFinite(pair[0]) && Number.isFinite(pair[1])))
    .filter((line) => Array.isArray(line) && line.length >= 2);
}

function buildHydrologyStripGeometry(worldPoints, widthMeters, depthMeters, verticalScale) {
  if (!Array.isArray(worldPoints) || worldPoints.length < 2 || !demMeta) {
    return null;
  }

  const halfWidth = Math.max(0.05, Number(widthMeters) * 0.5);
  const depth = Number(depthMeters);
  const scale = Math.max(0.01, Number(verticalScale));
  const leftRight = [];
  const uvPairs = [];
  let cumulative = 0;

  for (let i = 0; i < worldPoints.length; i += 1) {
    const current = worldPoints[i];
    const prev = worldPoints[Math.max(i - 1, 0)];
    const next = worldPoints[Math.min(i + 1, worldPoints.length - 1)];

    let dx = next.x - prev.x;
    let dy = next.y - prev.y;
    const len = Math.hypot(dx, dy);
    if (len < 1e-9) {
      dx = 1;
      dy = 0;
    } else {
      dx /= len;
      dy /= len;
    }

    const nx = -dy;
    const ny = dx;

    const localX = current.x - demMeta.centerX;
    const localY = current.y - demMeta.centerY;
    const terrainZ = sampleTerrainHeightAtLocal(localX, localY) * scale;
    const streamZ = terrainZ - depth * scale + HYDROLOGY_SURFACE_OFFSET_METERS;

    const left = {
      x: localX + nx * halfWidth,
      y: localY + ny * halfWidth,
      z: streamZ
    };
    const right = {
      x: localX - nx * halfWidth,
      y: localY - ny * halfWidth,
      z: streamZ
    };

    leftRight.push(left, right);
    if (i > 0) {
      const prevPoint = worldPoints[i - 1];
      cumulative += Math.hypot(current.x - prevPoint.x, current.y - prevPoint.y);
    }
    const v = cumulative / Math.max(1, halfWidth * 2);
    uvPairs.push({ u: 0, v }, { u: 1, v });
  }

  if (leftRight.length < 4) {
    return null;
  }

  const positions = [];
  const uvs = [];
  for (let i = 0; i < leftRight.length; i += 1) {
    const point = leftRight[i];
    const uv = uvPairs[i];
    positions.push(point.x, point.y, point.z);
    uvs.push(uv.u, uv.v);
  }

  const indices = [];
  const segmentCount = worldPoints.length - 1;
  for (let i = 0; i < segmentCount; i += 1) {
    const a = i * 2;
    const b = a + 1;
    const c = a + 2;
    const d = a + 3;
    indices.push(a, b, c, b, d, c);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return geometry;
}

function renderHydrologyOverlay() {
  clearHydrologyMeshes();
  if (!demMeta || hydrologyPolylinesWorld.length === 0) {
    if (hydrologyPolylinesWorld.length === 0) {
      hydrologyStatusText = "hydrology: no stream features";
      refreshStatus();
    }
    return;
  }

  const width = Number(hydrologyWidthInput?.value || 4);
  const depth = getHydrologyDepthMeters();
  const scale = Number(verticalScaleInput?.value || 1);
  const material = ensureHydrologyMaterial();
  const group = ensureHydrologyGroup();
  let meshCount = 0;

  for (const polyline of hydrologyPolylinesWorld) {
    const geometry = buildHydrologyStripGeometry(polyline, width, depth, scale);
    if (!geometry) {
      continue;
    }
    const mesh = new THREE.Mesh(geometry, material);
    mesh.renderOrder = 2;
    group.add(mesh);
    hydrologyMeshes.push(mesh);
    meshCount += 1;
  }

  applyHydrologyVisibility();
  hydrologyStatusText = meshCount > 0
    ? `hydrology: ${meshCount.toLocaleString()} stream segment${meshCount === 1 ? "" : "s"}`
    : "hydrology: no renderable stream features";
  refreshStatus();
}

function updateHydrologyFlowAnimation(deltaSeconds) {
  if (!hydrologyFlowTexture || !isHydrologyVisible() || deltaSeconds <= 0) {
    return;
  }
  const speed = Number(hydrologyFlowSpeedInput?.value || 1);
  if (!Number.isFinite(speed) || speed <= 0) {
    return;
  }
  hydrologyFlowTexture.offset.y = (hydrologyFlowTexture.offset.y - deltaSeconds * speed * 0.35) % 1;
}

async function loadHydrology() {
  if (!demMeta) {
    hydrologyStatusText = "hydrology unavailable (DEM is required)";
    refreshStatus();
    resetHydrologyData();
    return;
  }

  hydrologyStatusText = "Loading hydrology...";
  refreshStatus();

  let status;
  try {
    status = await fetchJson("/api/hydrology/status");
  } catch (error) {
    status = null;
  }

  if (!status || !status.available || !status.preferredPath) {
    const directCandidates = [
      "/data/hydrology/hydrology_clipped_local.geojson",
      "/data/hydrology/hydrology_clipped.geojson"
    ];
    let loaded = null;
    let lastError = null;
    for (const candidate of directCandidates) {
      try {
        loaded = await fetchJson(candidate);
        break;
      } catch (error) {
        lastError = error;
      }
    }

    if (!loaded) {
      resetHydrologyData();
      if (lastError && /404/.test(String(lastError.message || ""))) {
        hydrologyStatusText = "hydrology: no data";
      } else if (lastError) {
        throw lastError;
      } else {
        hydrologyStatusText = "hydrology: no data";
      }
      refreshStatus();
      return;
    }

    hydrologyFeaturesGeoJson = loaded;
    hydrologyPolylinesWorld = parseHydrologyPolylinesWorld(loaded);
    rebuildHydrologyPolylinesLocal();
    renderHydrologyOverlay();
    return;
  }

  const primaryPath = String(status.preferredPath || "");
  const fallbackPath = String(status.fallbackPath || "");

  let data;
  try {
    data = await fetchJson(primaryPath);
  } catch (error) {
    if (!fallbackPath || fallbackPath === primaryPath) {
      throw error;
    }
    data = await fetchJson(fallbackPath);
  }

  hydrologyFeaturesGeoJson = data;
  hydrologyPolylinesWorld = parseHydrologyPolylinesWorld(data);
  rebuildHydrologyPolylinesLocal();
  renderHydrologyOverlay();

  if (shrubAnchorsAll.length > 0 && shrubTemplates.length > 0) {
    renderShrubAssetInstances();
  }
  if (treeAnchorsAll.length > 0 && treeTemplatesByCategory.all.length > 0) {
    renderTreeAssetInstances();
  }
}

function toViewerLocalFromUtm(x, y) {
  if (!demMeta || !buildingsMeta || !Array.isArray(buildingsMeta.origin_utm)) {
    return [x, y];
  }
  const originX = Number(buildingsMeta.origin_utm[0]);
  const originY = Number(buildingsMeta.origin_utm[1]);
  const centerOffsetX = demMeta.centerX - originX;
  const centerOffsetY = demMeta.centerY - originY;
  return [x - originX - centerOffsetX, y - originY - centerOffsetY];
}

function addBuildingRingLine(ringCoords, material, footprintId) {
  if (!Array.isArray(ringCoords) || ringCoords.length < 2) {
    return 0;
  }
  const vertices = [];
  for (const coord of ringCoords) {
    if (!Array.isArray(coord) || coord.length < 2) {
      continue;
    }
    const x = Number(coord[0]);
    const y = Number(coord[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      continue;
    }
    const [localX, localY] = toViewerLocalFromUtm(x, y);
    const localZ = sampleTerrainHeightAtLocal(localX, localY) + 0.12;
    vertices.push(localX, localY, localZ);
  }

  if (vertices.length < 6) {
    return 0;
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
  const line = new THREE.Line(geometry, material);
  line.userData.footprintId = footprintId;
  buildingLinesGroup.add(line);
  return 1;
}

function computeFeatureLocalCentroid(geometry) {
  if (!geometry || !Array.isArray(geometry.coordinates)) {
    return null;
  }
  const stack = [geometry.coordinates];
  let sumX = 0;
  let sumY = 0;
  let count = 0;

  while (stack.length > 0) {
    const item = stack.pop();
    if (!Array.isArray(item)) {
      continue;
    }
    if (item.length >= 2 && Number.isFinite(Number(item[0])) && Number.isFinite(Number(item[1]))) {
      const [localX, localY] = toViewerLocalFromUtm(Number(item[0]), Number(item[1]));
      if (Number.isFinite(localX) && Number.isFinite(localY)) {
        sumX += localX;
        sumY += localY;
        count += 1;
      }
      continue;
    }
    for (let i = item.length - 1; i >= 0; i -= 1) {
      stack.push(item[i]);
    }
  }

  if (count <= 0) {
    return null;
  }

  return { x: sumX / count, y: sumY / count };
}

function computeFeatureLocalBounds(geometry) {
  if (!geometry || !Array.isArray(geometry.coordinates)) {
    return null;
  }
  const stack = [geometry.coordinates];
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  let count = 0;

  while (stack.length > 0) {
    const item = stack.pop();
    if (!Array.isArray(item)) {
      continue;
    }
    if (item.length >= 2 && Number.isFinite(Number(item[0])) && Number.isFinite(Number(item[1]))) {
      const [localX, localY] = toViewerLocalFromUtm(Number(item[0]), Number(item[1]));
      if (Number.isFinite(localX) && Number.isFinite(localY)) {
        minX = Math.min(minX, localX);
        minY = Math.min(minY, localY);
        maxX = Math.max(maxX, localX);
        maxY = Math.max(maxY, localY);
        count += 1;
      }
      continue;
    }
    for (let i = item.length - 1; i >= 0; i -= 1) {
      stack.push(item[i]);
    }
  }

  if (count <= 0) {
    return null;
  }
  return { minX, minY, maxX, maxY };
}

function getFeatureBuildingId(feature, featureIndex, existingIds) {
  const props = (feature && feature.properties) || {};
  const candidates = [
    props.BuildingID,
    props.BUILDINGID,
    props.BLDG_ID,
    props.OBJECTID,
    props.ObjectID,
    props.FID,
    props.id
  ];

  for (const candidate of candidates) {
    const value = String(candidate ?? "").trim();
    if (!value) {
      continue;
    }
    const normalized = `B-${value}`;
    if (!existingIds.has(normalized)) {
      return normalized;
    }
  }

  let index = featureIndex + 1;
  let fallback = `B-${index}`;
  while (existingIds.has(fallback)) {
    index += 1;
    fallback = `B-${index}`;
  }
  return fallback;
}

function renderBuildingsOverlay() {
  clearBuildingsOverlay();
  rebuildBuildingPolygonCache();

  if (!buildingFeaturesGeoJson || !demMeta) {
    return;
  }

  const features = Array.isArray(buildingFeaturesGeoJson.features) ? buildingFeaturesGeoJson.features : [];
  if (features.length === 0) {
    buildingsStatusText = "buildings: no footprints";
    refreshStatus();
    return;
  }

  buildingLinesGroup = new THREE.Group();
  buildingLinesGroup.name = "buildings-overlay";
  const usedIds = new Set();

  let featureCount = 0;
  let lineCount = 0;
  for (let featureIndex = 0; featureIndex < features.length; featureIndex += 1) {
    const feature = features[featureIndex];
    const geometry = feature && feature.geometry;
    if (!geometry || !geometry.type || !Array.isArray(geometry.coordinates)) {
      continue;
    }
    const buildingId = getFeatureBuildingId(feature, featureIndex, usedIds);
    usedIds.add(buildingId);
    const centroidLocal = computeFeatureLocalCentroid(geometry);
    const boundsLocal = computeFeatureLocalBounds(geometry);

    const lineMaterial = new THREE.LineBasicMaterial({
      color: BUILDING_BASE_COLOR,
      transparent: true,
      opacity: 0.9,
      depthWrite: false
    });
    let featureLineCount = 0;

    if (geometry.type === "Polygon") {
      featureCount += 1;
      for (const ring of geometry.coordinates) {
        featureLineCount += addBuildingRingLine(ring, lineMaterial, buildingId);
      }
    } else if (geometry.type === "MultiPolygon") {
      featureCount += 1;
      for (const polygon of geometry.coordinates) {
        if (!Array.isArray(polygon)) {
          continue;
        }
        for (const ring of polygon) {
          featureLineCount += addBuildingRingLine(ring, lineMaterial, buildingId);
        }
      }
    }

    if (featureLineCount > 0) {
      buildingRecords.push({
        id: buildingId,
        material: lineMaterial,
        featureIndex,
        centroidLocal,
        boundsLocal
      });
      lineCount += featureLineCount;
    } else {
      lineMaterial.dispose();
    }
  }

  if (lineCount === 0) {
    const materials = new Set();
    buildingLinesGroup.traverse((node) => {
      if (node.isLine && node.material) {
        materials.add(node.material);
      }
    });
    for (const material of materials) {
      material.dispose();
    }
    buildingLinesGroup = null;
    buildingRecords = [];
    selectedBuildingId = null;
    updateBuildingEditorUI();
    buildingsStatusText = "buildings: no linework";
    refreshStatus();
    return;
  }

  buildingLinesGroup.scale.z = Number(verticalScaleInput.value || 1);
  scene.add(buildingLinesGroup);
  applyBuildingFootprintsVisibility();
  updateBuildingRecordStyles();
  updateBuildingEditorUI();
  buildingsStatusText = `buildings: ${featureCount.toLocaleString()} footprints`;
  refreshStatus();
}

function applyBuildingsVerticalScale(scale) {
  if (buildingLinesGroup) {
    buildingLinesGroup.scale.z = scale;
  }
  if (buildingAssetRootGroup) {
    buildingAssetRootGroup.scale.z = scale;
  }
}

function resetView(scale = Number(verticalScaleInput.value || 1)) {
  const span = Math.max(terrainWidthMeters, terrainHeightMeters);
  const zSpan = Math.max(elevationRange * scale, 1);
  cameraState.radius = Math.max(span * 1.25, zSpan * 4.0, 200);
  cameraState.theta = -Math.PI / 4;
  cameraState.phi = 1.0;
  cameraState.target.set(0, 0, zSpan * 0.35);
  updateCameraPosition();
}

function buildTerrainMesh(gridData) {
  const {
    width,
    height,
    heights,
    minElevation,
    maxElevation,
    minX,
    maxX,
    minY,
    maxY,
    xStep,
    yStep
  } = gridData;

  if (terrainMesh) {
    scene.remove(terrainMesh);
    terrainGeometry.dispose();
    terrainMesh.material.dispose();
  }

  terrainWidthMeters = (width - 1) * xStep;
  terrainHeightMeters = (height - 1) * yStep;
  demGridWidth = width;
  demGridHeight = height;
  demGridXStep = xStep;
  demGridYStep = yStep;
  elevationRange = Math.max(maxElevation - minElevation, 1);
  baseHeights = new Float32Array(heights.length);

  demMeta = {
    minElevation,
    maxElevation,
    minX,
    maxX,
    minY,
    maxY,
    centerX: (minX + maxX) / 2,
    centerY: (minY + maxY) / 2
  };

  terrainGeometry = new THREE.PlaneGeometry(
    terrainWidthMeters,
    terrainHeightMeters,
    width - 1,
    height - 1
  );

  const positions = terrainGeometry.attributes.position.array;
  const colors = new Float32Array(width * height * 3);

  for (let i = 0; i < heights.length; i += 1) {
    const normalized = Number(heights[i]) - minElevation;
    baseHeights[i] = normalized;
    positions[i * 3 + 2] = normalized;
    const ratio = Math.max(0, Math.min(1, normalized / elevationRange));
    const color = colorForHeight(ratio);
    colors[i * 3] = color.r;
    colors[i * 3 + 1] = color.g;
    colors[i * 3 + 2] = color.b;
  }

  terrainBaseColors = new Float32Array(colors);

  terrainGeometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  terrainGeometry.computeVertexNormals();

  const material = new THREE.MeshStandardMaterial({
    vertexColors: true,
    metalness: 0.06,
    roughness: 0.9
  });

  terrainMesh = new THREE.Mesh(terrainGeometry, material);
  scene.add(terrainMesh);

  const scale = Number(verticalScaleInput.value || 1);
  applyVerticalScale(scale);
  applyShrubVerticalScale(scale);
  applyTreeVerticalScale(scale);
  applyBuildingsVerticalScale(scale);
  if (soilFeaturesGeoJson) {
    renderSoilOverlay();
  }
  if (hydrologyPolylinesWorld.length > 0) {
    rebuildHydrologyPolylinesLocal();
    renderHydrologyOverlay();
  }
  resetView(scale);

  demStatusText =
    `DEM: ${width}x${height} samples, elevation ${minElevation.toFixed(2)}m to ${maxElevation.toFixed(2)}m`;
  refreshStatus();
}

function clearShrubInstances() {
  for (const instance of shrubInstances) {
    scene.remove(instance);
  }
  shrubInstances = [];
}

function clearTreeInstances() {
  for (const instance of treeInstances) {
    scene.remove(instance);
  }
  treeInstances = [];
}

function fetchJson(url) {
  return fetch(url).then(async (response) => {
    if (!response.ok) {
      const message = await response.text();
      throw new Error(`${url} failed (${response.status}): ${message}`);
    }
    return response.json();
  });
}

function loadGlb(url) {
  return new Promise((resolve, reject) => {
    gltfLoader.load(
      url,
      (gltf) => resolve(gltf),
      undefined,
      (error) => reject(error)
    );
  });
}

function textureDebugName(texture, propertyName) {
  const explicitName = String(texture?.name || "").trim();
  if (explicitName) {
    return explicitName;
  }
  const sourcePath = String(texture?.source?.data?.src || texture?.image?.src || texture?.userData?.uri || "").trim();
  if (sourcePath) {
    const tail = sourcePath.split("/").pop() || sourcePath;
    return tail.split("?")[0] || tail;
  }
  return `${propertyName}:${String(texture?.uuid || "unknown").slice(0, 8)}`;
}

function collectBuildingAssetDiagnostics(object3d) {
  const materialByUuid = new Map();
  const textureByUuid = new Map();
  let meshCount = 0;
  let primitiveCount = 0;
  let materialSlotCount = 0;

  object3d.traverse((node) => {
    if (!node || !node.isMesh) {
      return;
    }

    meshCount += 1;
    const materials = Array.isArray(node.material)
      ? node.material.filter(Boolean)
      : node.material
        ? [node.material]
        : [];
    primitiveCount += Math.max(1, materials.length);
    materialSlotCount += materials.length;

    for (const material of materials) {
      const materialUuid = String(material?.uuid || `${material?.type || "material"}:${material?.name || ""}`);
      if (!materialByUuid.has(materialUuid)) {
        materialByUuid.set(materialUuid, material);
      }

      for (const propertyName of BUILDING_TEXTURE_PROPERTY_KEYS) {
        const texture = material ? material[propertyName] : null;
        if (!texture || !texture.isTexture) {
          continue;
        }
        const textureUuid = String(texture?.uuid || `${propertyName}:${texture?.id || ""}`);
        if (!textureByUuid.has(textureUuid)) {
          textureByUuid.set(textureUuid, textureDebugName(texture, propertyName));
        }
      }
    }
  });

  const materialNames = Array.from(materialByUuid.values())
    .map((material) => {
      const name = String(material?.name || "").trim();
      return name || "(unnamed)";
    })
    .sort((a, b) => a.localeCompare(b));

  const textureNames = Array.from(textureByUuid.values()).sort((a, b) => a.localeCompare(b));

  return {
    meshCount,
    primitiveCount,
    materialCount: materialByUuid.size,
    materialSlotCount,
    materialNames,
    textureCount: textureNames.length,
    textureNames
  };
}

function logBuildingAssetDiagnostics({
  footprintId,
  renderPath,
  glbUrl,
  diagnostics,
  materialReplacementOccurred,
  uvModificationOccurred,
  sanitizeSummary
}) {
  const payload = {
    footprintId,
    renderPath,
    glbUrl,
    meshCount: diagnostics.meshCount,
    primitiveCount: diagnostics.primitiveCount,
    materialCount: diagnostics.materialCount,
    materialSlotCount: diagnostics.materialSlotCount,
    materialNames: diagnostics.materialNames,
    textureCount: diagnostics.textureCount,
    textureNames: diagnostics.textureNames,
    materialReplacementOccurred: Boolean(materialReplacementOccurred),
    uvModificationOccurred: Boolean(uvModificationOccurred)
  };

  if (sanitizeSummary) {
    payload.sanitizeSummary = sanitizeSummary;
  }

  console.groupCollapsed(`[building-asset:${footprintId}] ${renderPath} diagnostics`);
  console.log(payload);
  console.groupEnd();
}

function getUvRange(geometry) {
  const uv = geometry && geometry.attributes ? geometry.attributes.uv : null;
  if (!uv || uv.count <= 0) {
    return null;
  }
  let minU = Number.POSITIVE_INFINITY;
  let minV = Number.POSITIVE_INFINITY;
  let maxU = Number.NEGATIVE_INFINITY;
  let maxV = Number.NEGATIVE_INFINITY;
  for (let i = 0; i < uv.count; i += 1) {
    const u = uv.getX(i);
    const v = uv.getY(i);
    if (!Number.isFinite(u) || !Number.isFinite(v)) {
      continue;
    }
    minU = Math.min(minU, u);
    minV = Math.min(minV, v);
    maxU = Math.max(maxU, u);
    maxV = Math.max(maxV, v);
  }
  if (!Number.isFinite(minU) || !Number.isFinite(minV) || !Number.isFinite(maxU) || !Number.isFinite(maxV)) {
    return null;
  }
  return {
    minU,
    maxU,
    minV,
    maxV,
    spanU: maxU - minU,
    spanV: maxV - minV
  };
}

function ensurePrimaryUvAttribute(geometry) {
  if (!geometry || !geometry.attributes) {
    return false;
  }
  const uv = geometry.attributes.uv;
  if (uv && uv.count > 0) {
    return false;
  }
  const uv2 = geometry.attributes.uv2;
  if (!uv2 || uv2.count <= 0) {
    return false;
  }
  geometry.setAttribute("uv", uv2.clone());
  geometry.attributes.uv.needsUpdate = true;
  return true;
}

function normalizeUvToUnitRange(geometry, uvRange) {
  const uv = geometry && geometry.attributes ? geometry.attributes.uv : null;
  if (!uv || !uvRange) {
    return false;
  }
  const spanU = Math.max(uvRange.spanU, 1e-9);
  const spanV = Math.max(uvRange.spanV, 1e-9);
  for (let i = 0; i < uv.count; i += 1) {
    const u = uv.getX(i);
    const v = uv.getY(i);
    if (!Number.isFinite(u) || !Number.isFinite(v)) {
      continue;
    }
    uv.setXY(i, (u - uvRange.minU) / spanU, (v - uvRange.minV) / spanV);
  }
  uv.needsUpdate = true;
  return true;
}

function configureAtlasTextureSampling(texture) {
  if (!texture) {
    return;
  }
  texture.wrapS = THREE.ClampToEdgeWrapping;
  texture.wrapT = THREE.ClampToEdgeWrapping;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.generateMipmaps = false;
  texture.anisotropy = Math.max(1, renderer.capabilities.getMaxAnisotropy());
  texture.flipY = false;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
}

function convertToPhotogrammetrySafeMaterial(sourceMaterial, mapTexture) {
  const baseParams = {
    map: mapTexture || null,
    transparent: Boolean(sourceMaterial?.transparent),
    opacity: Number.isFinite(sourceMaterial?.opacity) ? sourceMaterial.opacity : 1,
    alphaTest: Number.isFinite(sourceMaterial?.alphaTest) ? sourceMaterial.alphaTest : 0,
    side: sourceMaterial?.side ?? THREE.FrontSide
  };

  if (BUILDING_ASSET_UNLIT_DEBUG) {
    const unlit = new THREE.MeshBasicMaterial(baseParams);
    unlit.name = sourceMaterial?.name || "PhotogrammetryUnlit";
    return unlit;
  }

  const matte = new THREE.MeshStandardMaterial({
    ...baseParams,
    metalness: 0,
    roughness: 1
  });
  matte.name = sourceMaterial?.name || "PhotogrammetryMatte";
  return matte;
}

function makeBuildingAssetPhotogrammetrySafe(object3d, footprintId) {
  let meshesVisited = 0;
  let usedUv2AsUvCount = 0;
  let normalizedUvCount = 0;
  let replacedMaterialCount = 0;

  object3d.traverse((node) => {
    if (!node || !node.isMesh || !node.geometry) {
      return;
    }
    meshesVisited += 1;
    const geometry = node.geometry;
    if (ensurePrimaryUvAttribute(geometry)) {
      usedUv2AsUvCount += 1;
    }

    const range = getUvRange(geometry);
    if (range && (range.spanU > BUILDING_UV_NORMALIZE_SPAN_THRESHOLD || range.spanV > BUILDING_UV_NORMALIZE_SPAN_THRESHOLD)) {
      if (normalizeUvToUnitRange(geometry, range)) {
        normalizedUvCount += 1;
      }
    }

    const sourceMaterials = Array.isArray(node.material) ? node.material : [node.material];
    const validSourceMaterials = sourceMaterials.filter(Boolean);
    const nextMaterials = sourceMaterials.map((sourceMaterial) => {
      if (!sourceMaterial) {
        return sourceMaterial;
      }
      const baseMap = sourceMaterial.map || sourceMaterial.emissiveMap || null;
      if (baseMap) {
        configureAtlasTextureSampling(baseMap);
      }
      return convertToPhotogrammetrySafeMaterial(sourceMaterial, baseMap);
    });
    replacedMaterialCount += validSourceMaterials.length;

    for (const sourceMaterial of validSourceMaterials) {
      sourceMaterial.dispose();
    }
    node.material = Array.isArray(node.material) ? nextMaterials : nextMaterials[0];
  });

  const uvModificationOccurred = normalizedUvCount > 0 || usedUv2AsUvCount > 0;
  const materialReplacementOccurred = replacedMaterialCount > 0;

  if (normalizedUvCount > 0 || usedUv2AsUvCount > 0) {
    console.warn(
      `[building-asset:${footprintId}] UV fixes applied: ` +
      `meshes=${meshesVisited}, uv2->uv=${usedUv2AsUvCount}, normalized=${normalizedUvCount}, replacedMaterials=${replacedMaterialCount}`
    );
  }

  return {
    meshesVisited,
    usedUv2AsUvCount,
    normalizedUvCount,
    replacedMaterialCount,
    uvModificationOccurred,
    materialReplacementOccurred
  };
}

async function autoLoadExistingBuildingAssets() {
  let index;
  try {
    index = await fetchJson("/data/buildings/assets-index.json");
  } catch (error) {
    updateBuildingAssetStatus(`Asset index unavailable: ${error.message}`, true);
    return;
  }

  const assets = Array.isArray(index?.assets) ? index.assets : [];
  if (assets.length === 0) {
    updateBuildingAssetStatus("No previously built building assets found.");
    return;
  }

  let loadedCount = 0;
  let failedCount = 0;
  for (const entry of assets) {
    const footprintId = String(entry?.footprintId || "").trim();
    if (!footprintId) {
      continue;
    }
    try {
      await loadBuildingAssetByFootprintId(footprintId, true);
      loadedCount += 1;
    } catch (error) {
      failedCount += 1;
      console.error(`Failed to auto-load building asset ${footprintId}:`, error);
    }
  }

  applyBuildingAssetsVisibility();
  if (failedCount > 0) {
    updateBuildingAssetStatus(
      `Auto-loaded ${loadedCount}/${assets.length} building assets (${failedCount} failed).`,
      true
    );
    return;
  }

  updateBuildingAssetStatus(`Auto-loaded ${loadedCount} building assets.`);
}

function removeLoadedBuildingAsset(footprintId) {
  const existing = loadedBuildingAssets.get(footprintId);
  if (!existing) {
    return;
  }

  ensureBuildingAssetRootGroup().remove(existing.object3d);
  existing.object3d.traverse((node) => {
    if (!node) {
      return;
    }
    if (node.geometry) {
      node.geometry.dispose();
    }
    if (node.material) {
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      for (const material of materials) {
        if (!material) {
          continue;
        }
        if (material.map) {
          material.map.dispose();
        }
        if (material.normalMap) {
          material.normalMap.dispose();
        }
        if (material.aoMap) {
          material.aoMap.dispose();
        }
        material.dispose();
      }
    }
  });
  loadedBuildingAssets.delete(footprintId);
  refreshBuildingTransformGizmoAttachment();
}

function normalizeAssetRelativePath(relativePath) {
  const clean = String(relativePath || "").replace(/^\/+/, "");
  return clean
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

function getBuildingRecordById(footprintId) {
  return buildingRecords.find((record) => record.id === footprintId) || null;
}

function resolveBuildingAnchorLocal(footprintId, meta) {
  const centroid = meta && Array.isArray(meta.footprint_centroid_utm) ? meta.footprint_centroid_utm : null;
  if (centroid && centroid.length >= 2) {
    const ux = Number(centroid[0]);
    const uy = Number(centroid[1]);
    const looksGeographic = Math.abs(ux) <= 180 && Math.abs(uy) <= 90;
    if (Number.isFinite(ux) && Number.isFinite(uy) && !looksGeographic) {
      return toViewerLocalFromUtm(ux, uy);
    }
  }

  const record = getBuildingRecordById(footprintId);
  if (record && record.centroidLocal && Number.isFinite(record.centroidLocal.x) && Number.isFinite(record.centroidLocal.y)) {
    return [record.centroidLocal.x, record.centroidLocal.y];
  }

  return null;
}

function placeBuildingAssetAtAnchor(object3d, anchorLocal) {
  if (!Array.isArray(anchorLocal) || anchorLocal.length < 2) {
    return;
  }

  object3d.updateMatrixWorld(true);
  const bbox = new THREE.Box3().setFromObject(object3d);
  if (bbox.isEmpty()) {
    return;
  }

  const center = new THREE.Vector3();
  bbox.getCenter(center);
  const anchorX = Number(anchorLocal[0]);
  const anchorY = Number(anchorLocal[1]);
  if (!Number.isFinite(anchorX) || !Number.isFinite(anchorY)) {
    return;
  }

  const terrainZ = sampleTerrainHeightAtLocal(anchorX, anchorY) + 0.03;
  const offsetX = anchorX - center.x;
  const offsetY = anchorY - center.y;
  const offsetZ = terrainZ - bbox.min.z;

  object3d.position.add(new THREE.Vector3(offsetX, offsetY, offsetZ));
  object3d.updateMatrixWorld(true);
}

function maybeScaleBuildingAssetToFootprint(object3d, footprintId) {
  const record = getBuildingRecordById(footprintId);
  if (!record || !record.boundsLocal) {
    return;
  }
  const targetSpan = Math.max(
    Number(record.boundsLocal.maxX) - Number(record.boundsLocal.minX),
    Number(record.boundsLocal.maxY) - Number(record.boundsLocal.minY)
  );
  if (!Number.isFinite(targetSpan) || targetSpan <= 1e-6) {
    return;
  }

  object3d.updateMatrixWorld(true);
  const bbox = new THREE.Box3().setFromObject(object3d);
  if (bbox.isEmpty()) {
    return;
  }
  const currentSpan = Math.max(bbox.max.x - bbox.min.x, bbox.max.y - bbox.min.y);
  if (!Number.isFinite(currentSpan) || currentSpan <= 1e-9) {
    return;
  }

  const ratio = targetSpan / currentSpan;
  if (ratio < 0.25 || ratio > 4.0) {
    object3d.scale.multiplyScalar(ratio);
    object3d.updateMatrixWorld(true);
  }
}

function applyAssetUpAxisHint(object3d, upAxisHint) {
  const axis = String(upAxisHint || "").toLowerCase();
  object3d.rotation.set(0, 0, 0);
  if (axis === "y") {
    object3d.rotation.x = Math.PI / 2;
  }
}

function computeBottomTiltRadians(object3d) {
  object3d.updateMatrixWorld(true);
  const rootInverse = new THREE.Matrix4().copy(object3d.matrixWorld).invert();
  const tempVec = new THREE.Vector3();

  let minZ = Number.POSITIVE_INFINITY;
  let maxZ = Number.NEGATIVE_INFINITY;

  object3d.traverse((node) => {
    if (!node.isMesh || !node.geometry || !node.geometry.attributes?.position) {
      return;
    }
    const positions = node.geometry.attributes.position;
    const localMatrix = new THREE.Matrix4().multiplyMatrices(rootInverse, node.matrixWorld);
    const sampleStep = Math.max(1, Math.floor(positions.count / 50000));
    for (let i = 0; i < positions.count; i += sampleStep) {
      tempVec.fromBufferAttribute(positions, i).applyMatrix4(localMatrix);
      minZ = Math.min(minZ, tempVec.z);
      maxZ = Math.max(maxZ, tempVec.z);
    }
  });

  if (!Number.isFinite(minZ) || !Number.isFinite(maxZ)) {
    return Number.POSITIVE_INFINITY;
  }

  const zSpan = Math.max(1e-6, maxZ - minZ);
  const zLimit = minZ + zSpan * 0.12;

  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const ab = new THREE.Vector3();
  const ac = new THREE.Vector3();
  const triNormal = new THREE.Vector3();
  const triCenter = new THREE.Vector3();
  const normalSum = new THREE.Vector3();
  let normalWeight = 0;

  object3d.traverse((node) => {
    if (!node.isMesh || !node.geometry || !node.geometry.attributes?.position) {
      return;
    }

    const geometry = node.geometry;
    const positions = geometry.attributes.position;
    const localMatrix = new THREE.Matrix4().multiplyMatrices(rootInverse, node.matrixWorld);
    const index = geometry.index;

    const triangleCount = index ? Math.floor(index.count / 3) : Math.floor(positions.count / 3);
    if (triangleCount <= 0) {
      return;
    }
    const triStep = Math.max(1, Math.floor(triangleCount / 120000));

    for (let tri = 0; tri < triangleCount; tri += triStep) {
      let ia;
      let ib;
      let ic;
      if (index) {
        ia = index.getX(tri * 3);
        ib = index.getX(tri * 3 + 1);
        ic = index.getX(tri * 3 + 2);
      } else {
        ia = tri * 3;
        ib = tri * 3 + 1;
        ic = tri * 3 + 2;
      }

      a.fromBufferAttribute(positions, ia).applyMatrix4(localMatrix);
      b.fromBufferAttribute(positions, ib).applyMatrix4(localMatrix);
      c.fromBufferAttribute(positions, ic).applyMatrix4(localMatrix);

      triCenter.copy(a).add(b).add(c).multiplyScalar(1 / 3);
      if (triCenter.z > zLimit) {
        continue;
      }

      ab.copy(b).sub(a);
      ac.copy(c).sub(a);
      triNormal.copy(ab).cross(ac);
      const area2 = triNormal.length();
      if (area2 < 1e-10) {
        continue;
      }
      triNormal.multiplyScalar(1 / area2);
      if (triNormal.z < 0) {
        triNormal.multiplyScalar(-1);
      }

      normalSum.addScaledVector(triNormal, area2);
      normalWeight += area2;
    }
  });

  if (normalWeight <= 1e-8 || normalSum.lengthSq() <= 1e-8) {
    return Number.POSITIVE_INFINITY;
  }

  const bottomNormal = normalSum.normalize();
  const up = new THREE.Vector3(0, 0, 1);
  const tilt = Math.acos(clamp(bottomNormal.dot(up), -1, 1));
  if (!Number.isFinite(tilt)) {
    return Number.POSITIVE_INFINITY;
  }
  return tilt;
}

function snapBuildingAssetToOrthogonalBase(object3d) {
  const baseQuat = object3d.quaternion.clone();
  const candidates = [
    new THREE.Quaternion(),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI / 2),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), -Math.PI / 2),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 2),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), -Math.PI / 2)
  ];

  let bestQuat = baseQuat.clone();
  let bestScore = Number.POSITIVE_INFINITY;

  for (const candidate of candidates) {
    const testQuat = baseQuat.clone().multiply(candidate);
    object3d.quaternion.copy(testQuat);
    object3d.updateMatrixWorld(true);

    const tilt = computeBottomTiltRadians(object3d);
    const bbox = new THREE.Box3().setFromObject(object3d);
    if (bbox.isEmpty()) {
      continue;
    }
    const spanX = Math.max(1e-6, bbox.max.x - bbox.min.x);
    const spanY = Math.max(1e-6, bbox.max.y - bbox.min.y);
    const spanZ = Math.max(1e-6, bbox.max.z - bbox.min.z);
    const planSpan = Math.max(spanX, spanY);

    // Favor orientations with a flatter base and non-explosive height/plan ratio.
    const ratioPenalty = spanZ / planSpan > 4 ? 0.5 : 0;
    const score = tilt + ratioPenalty;
    if (score < bestScore) {
      bestScore = score;
      bestQuat = testQuat.clone();
    }
  }

  object3d.quaternion.copy(bestQuat);
  object3d.updateMatrixWorld(true);
}

function orientBuildingAssetForScene(object3d, footprintId) {
  const record = getBuildingRecordById(footprintId);
  const targetSpan = record && record.boundsLocal
    ? Math.max(
      Number(record.boundsLocal.maxX) - Number(record.boundsLocal.minX),
      Number(record.boundsLocal.maxY) - Number(record.boundsLocal.minY)
    )
    : null;

  const candidates = [0, Math.PI / 2, -Math.PI / 2];
  const originalRotationX = object3d.rotation.x;
  let bestRotation = originalRotationX;
  let bestScore = Number.POSITIVE_INFINITY;

  for (const rotationX of candidates) {
    object3d.rotation.x = rotationX;
    object3d.updateMatrixWorld(true);
    const bbox = new THREE.Box3().setFromObject(object3d);
    if (bbox.isEmpty()) {
      continue;
    }

    const spanXY = Math.max(bbox.max.x - bbox.min.x, bbox.max.y - bbox.min.y, 1e-6);
    const spanZ = Math.max(bbox.max.z - bbox.min.z, 1e-6);

    let score = 0;
    if (Number.isFinite(targetSpan) && targetSpan > 1e-6) {
      score += Math.abs(Math.log(spanXY / targetSpan));
    }

    // Penalize obviously flat or obviously vertical orientations.
    const aspect = spanZ / spanXY;
    if (aspect < 0.05) {
      score += 1.0;
    } else if (aspect < 0.09) {
      score += 0.35;
    } else if (aspect > 3.0) {
      score += 1.0;
    } else if (aspect > 2.0) {
      score += 0.2;
    }

    if (score < bestScore) {
      bestScore = score;
      bestRotation = rotationX;
    }
  }

  object3d.rotation.x = bestRotation;
  object3d.updateMatrixWorld(true);
}

async function loadBuildingAssetByFootprintId(footprintId, replaceExisting = true) {
  const normalizedId = String(footprintId || "").trim();
  if (!normalizedId) {
    throw new Error("Footprint ID is empty.");
  }

  const baseUrl = `/data/buildings/assets/${encodeURIComponent(normalizedId)}`;
  const cacheTag = Date.now();
  const meta = await fetchJson(`${baseUrl}/asset_meta.json?v=${cacheTag}`);

  const lods = Array.isArray(meta && meta.output && meta.output.lods) ? meta.output.lods : [];
  const lod0 = lods.find((lod) => Number(lod.level) === 0) || lods[0];
  const relPath = lod0 && lod0.path ? String(lod0.path) : "lod0.glb";
  const glbUrl = relPath.startsWith("/data/")
    ? relPath
    : `${baseUrl}/${normalizeAssetRelativePath(relPath)}`;
  const glbUrlWithCacheBust = glbUrl.includes("?")
    ? `${glbUrl}&v=${cacheTag}`
    : `${glbUrl}?v=${cacheTag}`;

  const gltf = await loadGlb(glbUrlWithCacheBust);
  const object3d = gltf.scene || gltf.scenes?.[0];
  if (!object3d) {
    throw new Error("GLB did not contain a scene.");
  }

  object3d.name = `building-asset-${normalizedId}`;
  object3d.userData.footprintId = normalizedId;

  const diagnostics = collectBuildingAssetDiagnostics(object3d);
  logBuildingAssetDiagnostics({
    footprintId: normalizedId,
    renderPath: BUILDING_RENDER_PATH_LABEL,
    glbUrl: glbUrlWithCacheBust,
    diagnostics,
    materialReplacementOccurred: false,
    uvModificationOccurred: false,
    sanitizeSummary: null
  });

  const assetUpAxis = String(meta?.placement?.asset_up_axis || "y").toLowerCase();
  applyAssetUpAxisHint(object3d, assetUpAxis);
  snapBuildingAssetToOrthogonalBase(object3d);
  maybeScaleBuildingAssetToFootprint(object3d, normalizedId);
  const anchorLocal = resolveBuildingAnchorLocal(normalizedId, meta);
  placeBuildingAssetAtAnchor(object3d, anchorLocal);
  object3d.updateMatrixWorld(true);
  const basePosition = object3d.position.clone();
  const baseQuaternion = object3d.quaternion.clone();

  if (replaceExisting) {
    removeLoadedBuildingAsset(normalizedId);
  }

  ensureBuildingAssetRootGroup().add(object3d);
  loadedBuildingAssets.set(normalizedId, {
    object3d,
    meta,
    url: glbUrlWithCacheBust,
    renderPath: BUILDING_RENDER_PATH_LABEL,
    diagnostics,
    basePosition,
    baseQuaternion
  });
  const savedPose = getSavedBuildingPose(normalizedId);
  applyPoseToLoadedBuildingAsset(normalizedId, savedPose);
  if (selectedBuildingId === normalizedId) {
    updateBuildingPoseReadout(savedPose);
  }
  applyBuildingsVerticalScale(Number(verticalScaleInput.value || 1));
  applyBuildingAssetsVisibility();
}

function loadObjWithMtl(variant) {
  return new Promise((resolve, reject) => {
    const finishLoad = (materials) => {
      const objLoader = new OBJLoader();
      if (materials) {
        objLoader.setMaterials(materials);
      }
      objLoader.load(
        variant.objUrl,
        (object) => resolve(object),
        undefined,
        reject
      );
    };

    if (variant.mtlUrl) {
      const mtlLoader = new MTLLoader();
      mtlLoader.load(
        variant.mtlUrl,
        (materials) => {
          materials.preload();
          finishLoad(materials);
        },
        undefined,
        reject
      );
      return;
    }

    finishLoad(null);
  });
}

function normalizeVariantObject(rawObject) {
  const template = new THREE.Group();
  template.add(rawObject);

  // Kenney OBJ foliage tends to be Y-up; rotate into Z-up world.
  rawObject.rotation.x = Math.PI / 2;

  rawObject.traverse((node) => {
    if (node.isMesh) {
      node.castShadow = false;
      node.receiveShadow = false;
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      for (const material of materials) {
        if (material) {
          material.side = THREE.DoubleSide;
        }
      }
    }
  });

  const bbox = new THREE.Box3().setFromObject(template);
  const size = new THREE.Vector3();
  bbox.getSize(size);
  const baseHeight = Math.max(size.z, 0.001);
  const normalizeScale = 1.4 / baseHeight;
  template.scale.setScalar(normalizeScale);

  const bbox2 = new THREE.Box3().setFromObject(template);
  template.position.z -= bbox2.min.z;

  return template;
}

async function loadShrubTemplates() {
  const manifest = await fetchJson("/data/vegetation/shrub-assets.json");
  const variants = Array.isArray(manifest.variants) ? manifest.variants : [];
  if (variants.length === 0) {
    throw new Error("No shrub asset variants were returned by shrub-assets manifest.");
  }

  const loaded = await Promise.all(
    variants.map(async (variant) => {
      const rawObject = await loadObjWithMtl(variant);
      const template = normalizeVariantObject(rawObject);
      return {
        name: variant.name,
        template,
        footprintRadius: computeTemplateFootprintRadius(template)
      };
    })
  );

  return loaded;
}

function normalizeTreeVariantObject(rawObject) {
  const template = new THREE.Group();
  template.add(rawObject);

  // Kenney OBJ foliage tends to be Y-up; rotate into Z-up world.
  rawObject.rotation.x = Math.PI / 2;

  rawObject.traverse((node) => {
    if (node.isMesh) {
      node.castShadow = false;
      node.receiveShadow = false;
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      for (const material of materials) {
        if (material) {
          material.side = THREE.DoubleSide;
        }
      }
    }
  });

  const bbox = new THREE.Box3().setFromObject(template);
  const size = new THREE.Vector3();
  bbox.getSize(size);
  const baseHeight = Math.max(size.z, 0.001);

  // Normalize to 1 meter base height so scale can come from LiDAR tree height directly.
  const normalizeScale = 1 / baseHeight;
  template.scale.setScalar(normalizeScale);

  const bbox2 = new THREE.Box3().setFromObject(template);
  template.position.z -= bbox2.min.z;

  return template;
}

function classifyTreeTemplateCategory(name) {
  const lower = String(name || "").toLowerCase();
  if (lower.includes("tall")) {
    return "tall";
  }
  if (lower.includes("small") || lower.includes("ground")) {
    return "short";
  }
  return "mid";
}

async function loadTreeTemplates() {
  const manifest = await fetchJson("/data/trees/tree-assets.json");
  const variants = Array.isArray(manifest.variants) ? manifest.variants : [];
  if (variants.length === 0) {
    throw new Error("No tree asset variants were returned by tree-assets manifest.");
  }

  const loaded = await Promise.all(
    variants.map(async (variant) => {
      const rawObject = await loadObjWithMtl(variant);
      const template = normalizeTreeVariantObject(rawObject);
      return {
        name: variant.name,
        category: classifyTreeTemplateCategory(variant.name),
        template,
        footprintRadius: computeTemplateFootprintRadius(template)
      };
    })
  );

  const categorized = {
    short: [],
    mid: [],
    tall: [],
    all: []
  };

  for (const variant of loaded) {
    categorized.all.push(variant);
    categorized[variant.category].push(variant);
  }

  return categorized;
}

function buildShrubAnchors(pointsData) {
  if (!demMeta) {
    throw new Error("DEM metadata is missing; cannot align shrub assets.");
  }

  const packed = Array.isArray(pointsData.points) ? pointsData.points : [];
  const count = Math.floor(packed.length / 4);
  if (count <= 0) {
    return [];
  }

  const cellSize = 1.8;
  const maxAnchors = 6000;
  const buckets = new Map();

  for (let i = 0; i < count; i += 1) {
    const x = Number(packed[i * 4]);
    const y = Number(packed[i * 4 + 1]);
    const z = Number(packed[i * 4 + 2]);
    const hag = Number(packed[i * 4 + 3]);

    const localX = x - demMeta.centerX;
    const localY = y - demMeta.centerY;
    const localZ = Math.max(z - demMeta.minElevation, 0);
    const cx = Math.floor(localX / cellSize);
    const cy = Math.floor(localY / cellSize);
    const key = `${cx}:${cy}`;

    if (!buckets.has(key)) {
      buckets.set(key, {
        count: 0,
        sumX: 0,
        sumY: 0,
        sumZ: 0,
        sumHag: 0
      });
    }

    const bucket = buckets.get(key);
    bucket.count += 1;
    bucket.sumX += localX;
    bucket.sumY += localY;
    bucket.sumZ += localZ;
    bucket.sumHag += hag;
  }

  const anchors = [];
  for (const bucket of buckets.values()) {
    if (bucket.count < 1) {
      continue;
    }

    const avgHag = bucket.sumHag / bucket.count;
    const avgZ = bucket.sumZ / bucket.count;
    const baseScale = clamp(
      0.62 + Math.sqrt(bucket.count) * 0.11 + avgHag * 0.22,
      0.55,
      2.45
    );

    anchors.push({
      x: bucket.sumX / bucket.count,
      y: bucket.sumY / bucket.count,
      z: avgZ,
      count: bucket.count,
      baseScale
    });
  }

  anchors.sort((a, b) => b.count - a.count);
  return anchors.slice(0, maxAnchors);
}

function renderShrubAssetInstances() {
  clearShrubInstances();
  if (!isShrubsVisible()) {
    shrubsStatusText = "shrubs: hidden";
    refreshStatus();
    return;
  }

  const anchors = selectAnchorsByDensity(shrubAnchorsAll, getShrubDensityMultiplier(), 0.55);
  if (anchors.length === 0) {
    shrubsStatusText = "shrubs: no placements";
    refreshStatus();
    return;
  }
  if (shrubTemplates.length === 0) {
    throw new Error("Shrub templates were not loaded.");
  }

  const vScale = Number(verticalScaleInput.value || 1);
  let pushedByBuildingsCount = 0;
  let pushedByHydrologyCount = 0;
  const hydrologyHalfWidth = getHydrologyWidthMeters() * 0.5;

  for (const anchor of anchors) {
    const variant = shrubTemplates[Math.floor(Math.random() * shrubTemplates.length)];
    const instance = variant.template.clone(true);
    const randomScale = Math.exp(randomNormal(0, 0.18));
    const scale = clamp(anchor.baseScale * randomScale, 0.5, 3.0) * BUSH_RENDER_SCALE_MULTIPLIER;
    const scaledRadius = Math.max(0.1, Number(variant.footprintRadius || 0.5) * scale);
    const requiredBuildingDistance = BUILDING_EDGE_CLEARANCE_METERS + scaledRadius;
    const afterBuilding = enforceBuildingEdgeSeparation(anchor.x, anchor.y, requiredBuildingDistance);
    if (afterBuilding.pushed) {
      pushedByBuildingsCount += 1;
    }

    const requiredHydrologyDistance = hydrologyHalfWidth + HYDROLOGY_EDGE_CLEARANCE_METERS + scaledRadius;
    const adjusted = enforceHydrologySeparation(afterBuilding.x, afterBuilding.y, requiredHydrologyDistance);
    if (adjusted.pushed) {
      pushedByHydrologyCount += 1;
    }

    const adjustedBaseZ = sampleTerrainHeightAtLocal(adjusted.x, adjusted.y);
    instance.userData.baseScale = scale;
    instance.userData.baseZ = adjustedBaseZ;

    instance.position.set(adjusted.x, adjusted.y, adjustedBaseZ * vScale + 0.06);
    instance.rotation.z = Math.random() * Math.PI * 2;
    instance.scale.set(scale, scale, scale * vScale);

    scene.add(instance);
    shrubInstances.push(instance);
  }

  shrubsStatusText =
    `shrubs: ${anchors.length.toLocaleString()} asset instances` +
    ` (${Math.round(getShrubDensityPercent())}%)` +
    ` (${shrubTemplates.length} variants, pushed bld ${pushedByBuildingsCount.toLocaleString()}, stream ${pushedByHydrologyCount.toLocaleString()})`;
  refreshStatus();
}

function buildShrubAssetInstances(pointsData) {
  shrubAnchorsAll = buildShrubAnchors(pointsData);
  renderShrubAssetInstances();
}

function buildTreeAnchors(instancesData) {
  if (!demMeta) {
    throw new Error("DEM metadata is missing; cannot align trees.");
  }

  const rows = Array.isArray(instancesData) ? instancesData : [];
  if (rows.length === 0) {
    return [];
  }

  const buckets = new Map();
  for (const row of rows) {
    const x = Number(row.x);
    const y = Number(row.y);
    const z = Number(row.z);
    const height = Number(row.height);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z) || !Number.isFinite(height)) {
      continue;
    }

    const localX = x - demMeta.centerX;
    const localY = y - demMeta.centerY;
    const localZ = Math.max(z - demMeta.minElevation, 0);
    const cellX = Math.floor(localX / TREE_DISPERSION_CELL_SIZE);
    const cellY = Math.floor(localY / TREE_DISPERSION_CELL_SIZE);
    const key = `${cellX}:${cellY}`;

    const candidate = {
      x: localX,
      y: localY,
      baseZ: localZ,
      height: Math.max(height, 1.0)
    };

    const existing = buckets.get(key);
    if (!existing) {
      buckets.set(key, candidate);
      continue;
    }

    // Prefer taller candidates, but keep slight randomness so distribution isn't perfectly regular.
    if (candidate.height > existing.height || Math.random() < 0.12) {
      buckets.set(key, candidate);
    }
  }

  const anchors = Array.from(buckets.values());
  if (anchors.length <= TREE_MAX_INSTANCES) {
    return anchors;
  }

  for (let i = anchors.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const tmp = anchors[i];
    anchors[i] = anchors[j];
    anchors[j] = tmp;
  }
  return anchors.slice(0, TREE_MAX_INSTANCES);
}

function pickTreeVariantForHeight(heightMeters) {
  const shortPool = treeTemplatesByCategory.short;
  const midPool = treeTemplatesByCategory.mid;
  const tallPool = treeTemplatesByCategory.tall;
  const allPool = treeTemplatesByCategory.all;

  if (allPool.length === 0) {
    throw new Error("Tree templates are not loaded.");
  }

  let pool = midPool;
  if (heightMeters >= 22) {
    pool = tallPool.length > 0 ? tallPool : midPool;
  } else if (heightMeters <= 10) {
    pool = shortPool.length > 0 ? shortPool : midPool;
  }

  if (!pool || pool.length === 0) {
    pool = allPool;
  }
  return pool[Math.floor(Math.random() * pool.length)];
}

function renderTreeAssetInstances() {
  clearTreeInstances();
  if (!isTreesVisible()) {
    treesStatusText = "trees: hidden";
    refreshStatus();
    return;
  }

  const anchors = selectAnchorsByDensity(treeAnchorsAll, getTreeDensityMultiplier(), 1.65);
  if (anchors.length === 0) {
    treesStatusText = "trees: no placements";
    refreshStatus();
    return;
  }
  if (!treeTemplatesByCategory.all || treeTemplatesByCategory.all.length === 0) {
    throw new Error("Tree templates were not loaded.");
  }

  const vScale = Number(verticalScaleInput.value || 1);
  let tallCount = 0;
  let shortCount = 0;
  let midCount = 0;
  let pushedByBuildingsCount = 0;
  let pushedByHydrologyCount = 0;
  const hydrologyHalfWidth = getHydrologyWidthMeters() * 0.5;

  for (const anchor of anchors) {
    const variant = pickTreeVariantForHeight(anchor.height);
    const instance = variant.template.clone(true);
    const lowerName = variant.name.toLowerCase();
    const scale = anchor.height;
    const scaledRadius = Math.max(0.1, Number(variant.footprintRadius || 0.5) * scale);
    const requiredBuildingDistance = BUILDING_EDGE_CLEARANCE_METERS + scaledRadius;
    const afterBuilding = enforceBuildingEdgeSeparation(anchor.x, anchor.y, requiredBuildingDistance);
    if (afterBuilding.pushed) {
      pushedByBuildingsCount += 1;
    }

    const requiredHydrologyDistance = hydrologyHalfWidth + HYDROLOGY_EDGE_CLEARANCE_METERS + scaledRadius;
    const adjusted = enforceHydrologySeparation(afterBuilding.x, afterBuilding.y, requiredHydrologyDistance);
    if (adjusted.pushed) {
      pushedByHydrologyCount += 1;
    }

    const adjustedBaseZ = sampleTerrainHeightAtLocal(adjusted.x, adjusted.y);

    if (lowerName.includes("tall")) {
      tallCount += 1;
    } else if (lowerName.includes("small") || lowerName.includes("ground")) {
      shortCount += 1;
    } else {
      midCount += 1;
    }

    instance.userData.baseScale = scale;
    instance.userData.baseZ = adjustedBaseZ;
    instance.position.set(adjusted.x, adjusted.y, adjustedBaseZ * vScale + 0.04);
    instance.rotation.z = Math.random() * Math.PI * 2;
    instance.scale.set(scale, scale, scale * vScale);

    scene.add(instance);
    treeInstances.push(instance);
  }

  treesStatusText =
    `trees: ${anchors.length.toLocaleString()} asset instances` +
    ` (${Math.round(getTreeDensityPercent())}%)` +
    ` (tall ${tallCount.toLocaleString()}, short ${shortCount.toLocaleString()}, mid ${midCount.toLocaleString()}, pushed bld ${pushedByBuildingsCount.toLocaleString()}, stream ${pushedByHydrologyCount.toLocaleString()})`;
  refreshStatus();
}

function buildTreeAssetInstances(instancesData) {
  treeAnchorsAll = buildTreeAnchors(instancesData);
  renderTreeAssetInstances();
}

function findFirstPositionCoordinate(geometry) {
  if (!geometry || !Array.isArray(geometry.coordinates)) {
    return null;
  }
  const stack = [geometry.coordinates];
  while (stack.length > 0) {
    const item = stack.pop();
    if (!Array.isArray(item)) {
      continue;
    }
    if (item.length >= 2 && Number.isFinite(Number(item[0])) && Number.isFinite(Number(item[1]))) {
      return [Number(item[0]), Number(item[1])];
    }
    for (let i = item.length - 1; i >= 0; i -= 1) {
      stack.push(item[i]);
    }
  }
  return null;
}

function isLikelyWgs84GeoJson(geojson) {
  const features = Array.isArray(geojson && geojson.features) ? geojson.features : [];
  if (features.length === 0) {
    return false;
  }
  for (const feature of features) {
    const first = findFirstPositionCoordinate(feature.geometry);
    if (!first) {
      continue;
    }
    const [x, y] = first;
    return Math.abs(x) <= 180 && Math.abs(y) <= 90;
  }
  return false;
}

async function loadBuildings() {
  buildingsStatusText = "Loading buildings...";
  refreshStatus();

  const [meta, clippedGeojson] = await Promise.all([
    fetchJson("/data/buildings/buildings_meta.json"),
    fetchJson("/data/buildings/footprints_clipped.geojson")
  ]);

  buildingsMeta = meta;
  buildingFeaturesGeoJson = clippedGeojson;

  if (isLikelyWgs84GeoJson(clippedGeojson)) {
    try {
      const localGeojson = await fetchJson("/data/buildings/footprints_clipped_local.geojson");
      buildingFeaturesGeoJson = localGeojson;
    } catch (error) {
      throw new Error(
        `footprints_clipped.geojson appears geographic (RFC7946), but local companion is unavailable: ${error.message}`
      );
    }
  }

  renderBuildingsOverlay();
  if (shrubAnchorsAll.length > 0 && shrubTemplates.length > 0) {
    renderShrubAssetInstances();
  }
  if (treeAnchorsAll.length > 0 && treeTemplatesByCategory.all.length > 0) {
    renderTreeAssetInstances();
  }
}

async function loadDemGrid() {
  demStatusText = "Loading DEM...";
  refreshStatus();

  const gridData = await fetchJson("/data/dem-grid.json");
  buildTerrainMesh(gridData);
}

async function loadShrubs() {
  shrubsStatusText = "Loading shrub assets...";
  refreshStatus();

  shrubTemplates = await loadShrubTemplates();
  shrubsStatusText = `Loaded ${shrubTemplates.length} shrub asset variants`;
  refreshStatus();

  const pointsData = await fetchJson("/data/vegetation/shrubs-points.json");
  buildShrubAssetInstances(pointsData);
}

async function loadTrees() {
  treesStatusText = "Loading tree assets...";
  refreshStatus();
  treeTemplatesByCategory = await loadTreeTemplates();
  treesStatusText = `Loaded ${treeTemplatesByCategory.all.length} tree asset variants`;
  refreshStatus();

  const treeData = await fetchJson("/data/trees/tree_instances.json");
  buildTreeAssetInstances(treeData);
}

renderer.domElement.addEventListener("pointerdown", (event) => {
  updateHoverFromPointerEvent(event);
  if (buildingTransformControls && (buildingTransformControls.axis || buildingTransformIsDragging)) {
    pointerState.dragging = false;
    return;
  }
  pointerState.dragging = true;
  pointerState.downX = event.clientX;
  pointerState.downY = event.clientY;
  pointerState.lastX = event.clientX;
  pointerState.lastY = event.clientY;
  pointerState.movedDistance = 0;
  renderer.domElement.setPointerCapture(event.pointerId);
});

renderer.domElement.addEventListener("pointerup", (event) => {
  pointerState.dragging = false;
  if (renderer.domElement.hasPointerCapture(event.pointerId)) {
    renderer.domElement.releasePointerCapture(event.pointerId);
  }

  if (suppressBuildingPickOnPointerUp || buildingTransformIsDragging) {
    suppressBuildingPickOnPointerUp = false;
    return;
  }

  const clickDistance = Math.hypot(event.clientX - pointerState.downX, event.clientY - pointerState.downY);
  const shouldPickBuilding = event.button === 0 && clickDistance <= 4 && pointerState.movedDistance <= 10;
  if (shouldPickBuilding) {
    const pickedId = pickBuildingAtPointer(event);
    selectBuilding(pickedId);

    if (isSoilsVisible()) {
      const pickedPolygon = pickedId ? null : pickSoilPolygonAtPointer(event);
      const selectionChanged = pickedPolygon !== soilSelectedPolygon;
      soilSelectedPolygon = pickedPolygon;
      if (selectionChanged) {
        renderSoilLegend();
      } else {
        updateSoilLegendDetails();
      }
    }
  }
});

renderer.domElement.addEventListener("pointermove", (event) => {
  updateHoverFromPointerEvent(event);
  if (buildingTransformIsDragging) {
    return;
  }
  if (!pointerState.dragging) {
    return;
  }
  const dx = event.clientX - pointerState.lastX;
  const dy = event.clientY - pointerState.lastY;
  pointerState.movedDistance += Math.hypot(dx, dy);
  pointerState.lastX = event.clientX;
  pointerState.lastY = event.clientY;

  cameraState.theta -= dx * 0.005;
  cameraState.phi += dy * 0.005;
  cameraState.phi = Math.max(0.2, Math.min(Math.PI - 0.2, cameraState.phi));
  updateCameraPosition();
});

renderer.domElement.addEventListener("pointerleave", () => {
  hoverState.insideCanvas = false;
});

renderer.domElement.addEventListener("contextmenu", async (event) => {
  event.preventDefault();
  updateHoverFromPointerEvent(event);

  const coords = getHoveredRealCoordinates();
  if (!coords) {
    copyFeedbackText = "No terrain coordinate under cursor";
    copyFeedbackUntilMs = performance.now() + 1400;
    return;
  }

  try {
    const wgs84 = await fetchJson(
      `/data/coords/wgs84.json?x=${encodeURIComponent(coords.x)}&y=${encodeURIComponent(coords.y)}`
    );
    const lat = Number(wgs84.lat);
    const lon = Number(wgs84.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      throw new Error("Invalid WGS84 response.");
    }

    const mapsText = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
    const copied = await copyTextToClipboard(mapsText);
    copyFeedbackText = copied ? `Copied for Google Maps: ${mapsText}` : "Clipboard copy failed";
  } catch (error) {
    copyFeedbackText = `Coordinate conversion failed: ${error.message || "unknown error"}`;
  }
  copyFeedbackUntilMs = performance.now() + 1400;
});

renderer.domElement.addEventListener("wheel", (event) => {
  if (buildingTransformIsDragging) {
    return;
  }
  event.preventDefault();
  const zoomFactor = event.deltaY > 0 ? 1.07 : 0.93;
  cameraState.radius = Math.max(30, Math.min(30000, cameraState.radius * zoomFactor));
  updateCameraPosition();
}, { passive: false });

verticalScaleInput.addEventListener("input", () => {
  const scale = Number(verticalScaleInput.value);
  verticalScaleValue.textContent = `${scale.toFixed(1)}x`;
  applyVerticalScale(scale);
  applyShrubVerticalScale(scale);
  applyTreeVerticalScale(scale);
  applyBuildingsVerticalScale(scale);
  if (soilFeaturesGeoJson) {
    renderSoilOverlay();
  }
  if (hydrologyPolylinesWorld.length > 0) {
    rebuildHydrologyPolylinesLocal();
    renderHydrologyOverlay();
  }
});

shrubDensityInput.addEventListener("input", () => {
  const percent = Math.round(Number(shrubDensityInput.value));
  shrubDensityValue.textContent = `${percent}%`;
  if (shrubAnchorsAll.length > 0 && shrubTemplates.length > 0) {
    renderShrubAssetInstances();
  }
});

treeDensityInput.addEventListener("input", () => {
  const percent = Math.round(Number(treeDensityInput.value));
  treeDensityValue.textContent = `${percent}%`;
  if (treeAnchorsAll.length > 0 && treeTemplatesByCategory.all.length > 0) {
    renderTreeAssetInstances();
  }
});

showShrubsInput.addEventListener("change", () => {
  if (shrubAnchorsAll.length > 0 && shrubTemplates.length > 0) {
    renderShrubAssetInstances();
  } else {
    shrubsStatusText = isShrubsVisible() ? "shrubs: loading..." : "shrubs: hidden";
    refreshStatus();
  }
});

showTreesInput.addEventListener("change", () => {
  if (treeAnchorsAll.length > 0 && treeTemplatesByCategory.all.length > 0) {
    renderTreeAssetInstances();
  } else {
    treesStatusText = isTreesVisible() ? "trees: loading..." : "trees: hidden";
    refreshStatus();
  }
});

showBuildingAssetsInput?.addEventListener("change", () => {
  applyBuildingAssetsVisibility();
});

showBuildingFootprintsInput?.addEventListener("change", () => {
  applyBuildingFootprintsVisibility();
});

showSoilDataInput?.addEventListener("change", () => {
  applySoilsVisibility();
  if (!isSoilsVisible()) {
    soilsStatusText = "soils: hidden";
    refreshStatus();
  } else if (soilFeaturesGeoJson) {
    renderSoilOverlay();
  } else {
    soilsStatusText = "soils: loading...";
    refreshStatus();
  }
});

showHydrologyInput?.addEventListener("change", () => {
  applyHydrologyVisibility();
  if (!isHydrologyVisible()) {
    hydrologyStatusText = "hydrology: hidden";
    refreshStatus();
  } else if (hydrologyPolylinesWorld.length > 0) {
    renderHydrologyOverlay();
  } else {
    hydrologyStatusText = "hydrology: loading...";
    refreshStatus();
  }
});

hydrologyWidthInput?.addEventListener("input", () => {
  const width = Number(hydrologyWidthInput.value);
  hydrologyWidthValue.textContent = `${width.toFixed(1)}m`;
  if (hydrologyPolylinesWorld.length > 0) {
    renderHydrologyOverlay();
    if (shrubAnchorsAll.length > 0 && shrubTemplates.length > 0) {
      renderShrubAssetInstances();
    }
    if (treeAnchorsAll.length > 0 && treeTemplatesByCategory.all.length > 0) {
      renderTreeAssetInstances();
    }
  }
});

hydrologyDepthInput?.addEventListener("input", () => {
  const depth = getHydrologyDepthMeters();
  hydrologyDepthValue.textContent = `${depth.toFixed(1)}m`;
  if (hydrologyPolylinesWorld.length > 0) {
    renderHydrologyOverlay();
  }
});

hydrologyFlowSpeedInput?.addEventListener("input", () => {
  const speed = Number(hydrologyFlowSpeedInput.value);
  hydrologyFlowSpeedValue.textContent = `${speed.toFixed(1)}x`;
});

snapHydrologyToTerrainButton?.addEventListener("click", () => {
  if (hydrologyPolylinesWorld.length === 0) {
    hydrologyStatusText = "hydrology: nothing to snap";
    refreshStatus();
    return;
  }
  renderHydrologyOverlay();
  if (shrubAnchorsAll.length > 0 && shrubTemplates.length > 0) {
    renderShrubAssetInstances();
  }
  if (treeAnchorsAll.length > 0 && treeTemplatesByCategory.all.length > 0) {
    renderTreeAssetInstances();
  }
  hydrologyStatusText = "hydrology: snapped to terrain";
  refreshStatus();
});

menuToggleButton.addEventListener("click", () => {
  const open = !document.body.classList.contains("menu-open");
  setMenuOpen(open);
});

openUploadModalButton?.addEventListener("click", () => {
  setUploadModalOpen(true);
});

openManageDataModalButton?.addEventListener("click", () => {
  setManageDataModalOpen(true);
});

closeUploadModalButton?.addEventListener("click", () => {
  setUploadModalOpen(false);
});

closeManageDataModalButton?.addEventListener("click", () => {
  if (manageDataBusy) {
    return;
  }
  setManageDataModalOpen(false);
});

closeUploadProgressButton?.addEventListener("click", () => {
  if (uploadInProgress) {
    return;
  }
  setUploadProgressModalOpen(false);
});

uploadModalEl?.addEventListener("click", (event) => {
  if (event.target === uploadModalEl) {
    setUploadModalOpen(false);
  }
});

manageDataModalEl?.addEventListener("click", (event) => {
  if (event.target === manageDataModalEl && !manageDataBusy) {
    setManageDataModalOpen(false);
  }
});

uploadProgressModalEl?.addEventListener("click", (event) => {
  if (event.target === uploadProgressModalEl && !uploadInProgress) {
    setUploadProgressModalOpen(false);
  }
});

selectUploadFilesButton?.addEventListener("click", () => {
  uploadFilesInputEl?.click();
});

selectUploadFolderButton?.addEventListener("click", () => {
  uploadFolderInputEl?.click();
});

clearUploadQueueButton?.addEventListener("click", () => {
  uploadQueueItems = [];
  renderUploadQueue();
  setUploadStatus("Queue cleared.");
});

refreshDataSourcesButton?.addEventListener("click", async () => {
  if (manageDataBusy) {
    return;
  }
  setManageDataBusyState(true);
  setManageDataStatus("Refreshing data sources...");
  await loadManageDataSources();
  setManageDataBusyState(false);
});

clearAllDataButton?.addEventListener("click", async () => {
  if (manageDataBusy) {
    return;
  }
  const confirmed = window.confirm(
    "Clear all raw data sources and processed outputs? This resets the project data."
  );
  if (!confirmed) {
    return;
  }

  try {
    setManageDataBusyState(true);
    setManageDataStatus("Clearing data...");
    const response = await fetch("/api/data-sources/clear", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({})
    });
    const payload = await response.json();
    if (!response.ok || !payload || payload.error) {
      throw new Error((payload && payload.error) || `Clear failed (${response.status})`);
    }

    manageDataSources = [];
    renderManageDataSourcesList();
    setManageDataStatus("All data cleared.");
    await refreshDataProductsInViewer();
  } catch (error) {
    setManageDataStatus(`Clear failed: ${error.message}`, true);
  } finally {
    setManageDataBusyState(false);
  }
});

uploadFilesInputEl?.addEventListener("change", () => {
  addUploadFilesToQueue(uploadFilesInputEl.files, false);
  uploadFilesInputEl.value = "";
});

uploadFolderInputEl?.addEventListener("change", () => {
  addUploadFilesToQueue(uploadFolderInputEl.files, true);
  uploadFolderInputEl.value = "";
});

uploadDropZoneEl?.addEventListener("dragover", (event) => {
  event.preventDefault();
  uploadDropZoneEl.classList.add("isDragOver");
});

uploadDropZoneEl?.addEventListener("dragleave", () => {
  uploadDropZoneEl.classList.remove("isDragOver");
});

uploadDropZoneEl?.addEventListener("drop", async (event) => {
  event.preventDefault();
  uploadDropZoneEl.classList.remove("isDragOver");
  try {
    const entries = await collectDroppedUploadEntries(event.dataTransfer);
    addUploadEntriesToQueue(entries);
  } catch (error) {
    setUploadStatus(`Unable to read dropped files: ${error.message}`, true);
  }
});

submitUploadButton?.addEventListener("click", async () => {
  await submitUploadQueue();
});

resetViewButton.addEventListener("click", () => {
  resetView(Number(verticalScaleInput.value));
});

saveBuildingNameButton?.addEventListener("click", () => {
  saveSelectedBuildingName();
});

clearBuildingNameButton?.addEventListener("click", () => {
  clearSelectedBuildingName();
});

buildingNameInput?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    saveSelectedBuildingName();
    event.preventDefault();
  }
});

window.addEventListener("resize", resize);

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && uploadModalEl && !uploadModalEl.hidden) {
    setUploadModalOpen(false);
    event.preventDefault();
    return;
  }
  if (event.key === "Escape" && uploadProgressModalEl && !uploadProgressModalEl.hidden && !uploadInProgress) {
    setUploadProgressModalOpen(false);
    event.preventDefault();
    return;
  }
  if (event.key === "Escape" && manageDataModalEl && !manageDataModalEl.hidden && !manageDataBusy) {
    setManageDataModalOpen(false);
    event.preventDefault();
    return;
  }

  if (!isTypingContextActive() && buildingTransformControls && selectedBuildingId) {
    const key = String(event.key || "").toLowerCase();
    if (key === "g") {
      setBuildingTransformMode("translate");
      event.preventDefault();
      return;
    }
    if (key === "r") {
      setBuildingTransformMode("rotate");
      event.preventDefault();
      return;
    }
    if (key === "x" || key === "y" || key === "z") {
      if (key === "x") {
        buildingTransformControls.showX = !buildingTransformControls.showX;
      } else if (key === "y") {
        buildingTransformControls.showY = !buildingTransformControls.showY;
      } else {
        buildingTransformControls.showZ = !buildingTransformControls.showZ;
      }
      event.preventDefault();
      return;
    }
  }

  if (!(event.key in keyboardPanState)) {
    return;
  }
  if (isTypingContextActive()) {
    return;
  }
  keyboardPanState[event.key] = true;
  event.preventDefault();
});

window.addEventListener("keyup", (event) => {
  if (!(event.key in keyboardPanState)) {
    return;
  }
  keyboardPanState[event.key] = false;
  event.preventDefault();
});

window.addEventListener("blur", () => {
  keyboardPanState.ArrowUp = false;
  keyboardPanState.ArrowDown = false;
  keyboardPanState.ArrowLeft = false;
  keyboardPanState.ArrowRight = false;
});

function animate(timeMs) {
  requestAnimationFrame(animate);
  const deltaSeconds = Math.min(0.05, Math.max(0, (Number(timeMs) - lastFrameTimeMs) / 1000));
  lastFrameTimeMs = Number.isFinite(timeMs) ? Number(timeMs) : performance.now();
  applyKeyboardPan(deltaSeconds || 0);
  updateHydrologyFlowAnimation(deltaSeconds || 0);
  updateCoordinateReadout();
  renderer.render(scene, camera);
}

async function initialize() {
  try {
    await loadDemGrid();
  } catch (error) {
    demStatusText = `Failed to load DEM: ${error.message}`;
    refreshStatus();
    console.error(error);
    return;
  }

  try {
    await loadSoils();
  } catch (error) {
    resetSoilsData();
    soilsStatusText = `soils unavailable (${error.message})`;
    refreshStatus();
    console.error(error);
  }

  try {
    await loadHydrology();
  } catch (error) {
    resetHydrologyData();
    hydrologyStatusText = `hydrology unavailable (${error.message})`;
    refreshStatus();
    console.error(error);
  }

  try {
    await loadBuildings();
  } catch (error) {
    buildingsStatusText = `buildings unavailable (${error.message})`;
    refreshStatus();
    console.error(error);
  }

  try {
    await autoLoadExistingBuildingAssets();
  } catch (error) {
    updateBuildingAssetStatus(`Auto-load failed: ${error.message}`, true);
    console.error(error);
  }

  try {
    await loadShrubs();
  } catch (error) {
    shrubsStatusText = `shrubs unavailable (${error.message})`;
    refreshStatus();
    console.error(error);
  }

  try {
    await loadTrees();
  } catch (error) {
    treesStatusText = `trees unavailable (${error.message})`;
    refreshStatus();
    console.error(error);
  }
}

verticalScaleValue.textContent = `${Number(verticalScaleInput.value).toFixed(1)}x`;
shrubDensityValue.textContent = `${Math.round(getShrubDensityPercent())}%`;
treeDensityValue.textContent = `${Math.round(getTreeDensityPercent())}%`;
hydrologyWidthValue.textContent = `${Number(hydrologyWidthInput.value).toFixed(1)}m`;
hydrologyDepthValue.textContent = `${getHydrologyDepthMeters().toFixed(1)}m`;
hydrologyFlowSpeedValue.textContent = `${Number(hydrologyFlowSpeedInput.value).toFixed(1)}x`;
buildingNameMap = loadBuildingNamesFromStorage();
buildingPoseMap = loadBuildingPosesFromStorage();
createBuildingAssetControls();
updateBuildingEditorUI();
initializeSectionCollapse("layers", layersSectionBody, toggleLayersSectionButton);
initializeSectionCollapse("hydrology", hydrologySectionBody, toggleHydrologySectionButton);
initializeSectionCollapse("density", densitySectionBody, toggleDensitySectionButton);
initializeSectionCollapse("terrain", terrainSectionBody, toggleTerrainSectionButton);
initializeSectionCollapse("buildings", buildingsSectionBody, toggleBuildingsSectionButton);
initializeSectionCollapse("status", statusSectionBody, toggleStatusSectionButton);
applyBuildingAssetsVisibility();
applyBuildingFootprintsVisibility();
applySoilsVisibility();
applyHydrologyVisibility();
setMenuOpen(false);
setUploadModalOpen(false);
setUploadProgressModalOpen(false);
setManageDataModalOpen(false);
setManageDataBusyState(false);
updateUploadProgressUi({
  title: "Preparing Upload",
  explanation: "Waiting to begin.",
  detail: "",
  completedUnits: 0,
  totalUnits: 1,
  done: false,
  isError: false
});
renderUploadQueue();
renderManageDataSourcesList();

resize();
updateCameraPosition();
animate();
initialize();
