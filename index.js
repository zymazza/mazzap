#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const http = require("http");
const { spawnSync } = require("child_process");
const { URL } = require("url");

const HOST = process.env.HOST || "127.0.0.1";
const PORT = Number(process.env.PORT || 3000);

const ROOT_DIR = __dirname;
const VIEWER_DIR = path.join(ROOT_DIR, "Frontend Web Viewer");
const NODE_MODULES_DIR = path.join(ROOT_DIR, "node_modules");
const GENERATE_BUILDING_ASSET_SCRIPT = path.join(ROOT_DIR, "generateBuildingAsset.js");
const GENERATE_DEM_SCRIPT = path.join(ROOT_DIR, "generateDEM.js");
const GENERATE_BUILDINGS_SCRIPT = path.join(ROOT_DIR, "generateBuildings.js");
const GENERATE_VEGETATION_SCRIPT = path.join(ROOT_DIR, "Processing Pipeline", "generateVegetation.js");
const GENERATE_TREES_SCRIPT = path.join(ROOT_DIR, "Processing Pipeline", "generateTrees.js");
const GENERATE_HYDROLOGY_SCRIPT = path.join(ROOT_DIR, "Processing Pipeline", "generateHydrology.js");
const GENERATE_SOILS_SCRIPT = path.join(ROOT_DIR, "generateSoils.js");
const ASSETS_DIR_CANDIDATES = [
  path.join(ROOT_DIR, "assets"),
  path.join(ROOT_DIR, "Assets")
];
const RAW_DATA_INPUTS_DIR_CANDIDATES = [
  path.join(ROOT_DIR, "Raw Data Inputs"),
  path.join(ROOT_DIR, "Raw_Data_Inputs")
];
const SUPPORTED_BUILDING_MESH_EXTENSIONS = [".glb", ".gltf", ".obj", ".ply"];
const PROCESSED_DIR_CANDIDATES = [
  path.join(ROOT_DIR, "Processed Data"),
  path.join(ROOT_DIR, "Processed_Data")
];
const MAX_MULTIPART_UPLOAD_BYTES = 1024 * 1024 * 1024;
const DATA_SOURCE_TYPE_SET = new Set(["lidar", "footprints", "photogrammetry", "hydrology", "soils"]);
const DATA_SOURCE_PROCESS_STEP_CONFIG = {
  dem: {
    id: "dem",
    title: "Generate DEM",
    explanation: "Derive terrain elevation surface from the uploaded LiDAR source.",
    scriptPath: GENERATE_DEM_SCRIPT
  },
  vegetation: {
    id: "vegetation",
    title: "Generate Shrubs",
    explanation: "Extract low vegetation candidates and shrub density from LiDAR.",
    scriptPath: GENERATE_VEGETATION_SCRIPT
  },
  trees: {
    id: "trees",
    title: "Generate Trees",
    explanation: "Create tree candidates, density products, and tree instances from LiDAR.",
    scriptPath: GENERATE_TREES_SCRIPT
  },
  buildings: {
    id: "buildings",
    title: "Generate Footprints",
    explanation: "Clip and export footprint layers for building placement in the viewer.",
    scriptPath: GENERATE_BUILDINGS_SCRIPT
  },
  hydrology: {
    id: "hydrology",
    title: "Generate Hydrology",
    explanation: "Clip and prepare hydrology features for terrain-aligned stream rendering.",
    scriptPath: GENERATE_HYDROLOGY_SCRIPT
  },
  soils: {
    id: "soils",
    title: "Generate Soils",
    explanation: "Parse SSURGO export, join geometry with tabular attributes, and clip to DEM extent.",
    scriptPath: GENERATE_SOILS_SCRIPT
  }
};

const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".obj": "text/plain; charset=utf-8",
  ".mtl": "text/plain; charset=utf-8",
  ".glb": "model/gltf-binary",
  ".gltf": "model/gltf+json",
  ".ktx2": "image/ktx2",
  ".tif": "image/tiff",
  ".tiff": "image/tiff",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg"
};

function getMimeType(filePath) {
  return MIME_TYPES[path.extname(filePath).toLowerCase()] || "application/octet-stream";
}

function resolveProcessedDir() {
  for (const candidate of PROCESSED_DIR_CANDIDATES) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return candidate;
    }
  }
  return PROCESSED_DIR_CANDIDATES[0];
}

function resolveDemPath() {
  return path.join(resolveProcessedDir(), "dem.tif");
}

function resolveShrubsPointsPath() {
  return path.join(resolveProcessedDir(), "vegetation", "shrubs_points.laz");
}

function resolveAssetsDir(optional = false) {
  for (const candidate of ASSETS_DIR_CANDIDATES) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return candidate;
    }
  }

  if (optional) {
    return null;
  }
  throw new Error("Assets directory not found. Expected ./assets or ./Assets.");
}

function resolveRawDataInputsDir(optional = false) {
  for (const candidate of RAW_DATA_INPUTS_DIR_CANDIDATES) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return candidate;
    }
  }

  if (optional) {
    return null;
  }
  throw new Error("Raw Data Inputs directory not found. Expected ./Raw Data Inputs.");
}

function safeJoin(baseDir, relativePath) {
  const clean = relativePath.replace(/^\/+/, "");
  const resolved = path.resolve(baseDir, clean);
  if (!resolved.startsWith(baseDir)) {
    return null;
  }
  return resolved;
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body)
  });
  res.end(body);
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 2 * 1024 * 1024) {
        reject(new Error("Request body too large."));
        req.destroy();
      }
    });
    req.on("end", () => {
      if (!raw.trim()) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(raw));
      } catch (error) {
        reject(new Error("Invalid JSON request body."));
      }
    });
    req.on("error", (error) => {
      reject(error);
    });
  });
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
  return dirPath;
}

function ensureProjectDataDirectories() {
  const rawInputsDir = ensureDir(resolveRawDataInputsDir(true) || RAW_DATA_INPUTS_DIR_CANDIDATES[0]);
  const processedDir = ensureDir(resolveProcessedDir());

  const rawSubdirs = [
    "Hydrology",
    "SSURGO",
    path.join("SSURGO", "spatial"),
    path.join("SSURGO", "tabular"),
    path.join("SSURGO", "thematic")
  ];
  for (const subdir of rawSubdirs) {
    ensureDir(path.join(rawInputsDir, subdir));
  }

  const processedSubdirs = [
    "vegetation",
    "trees",
    "buildings",
    path.join("buildings", "assets"),
    "hydrology",
    "soils"
  ];
  for (const subdir of processedSubdirs) {
    ensureDir(path.join(processedDir, subdir));
  }

  return {
    rawInputsDir,
    processedDir
  };
}

function sanitizeUploadPathSegment(segmentRaw) {
  const cleaned = String(segmentRaw || "")
    .replace(/[\u0000-\u001f<>:"|?*]/g, "_")
    .trim();
  if (!cleaned || cleaned === "." || cleaned === "..") {
    return "_";
  }
  return cleaned;
}

function sanitizeUploadRelativePath(relativePathRaw) {
  return String(relativePathRaw || "")
    .replace(/\\+/g, "/")
    .split("/")
    .map((segment) => sanitizeUploadPathSegment(segment))
    .filter((segment) => segment && segment !== "." && segment !== "..")
    .join("/");
}

function parseMultipartForm(req) {
  return new Promise((resolve, reject) => {
    const contentType = String(req.headers["content-type"] || "");
    const boundaryMatch = /boundary=([^;]+)/i.exec(contentType);
    if (!boundaryMatch) {
      reject(new Error("Missing multipart boundary."));
      return;
    }

    const boundary = `--${boundaryMatch[1].trim().replace(/^"|"$/g, "")}`;
    const boundaryBuffer = Buffer.from(boundary);
    const headerSplitBuffer = Buffer.from("\r\n\r\n");

    const chunks = [];
    let totalSize = 0;
    req.on("data", (chunk) => {
      totalSize += chunk.length;
      if (totalSize > MAX_MULTIPART_UPLOAD_BYTES) {
        reject(new Error(`Upload exceeds ${Math.round(MAX_MULTIPART_UPLOAD_BYTES / (1024 * 1024))} MB limit.`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });

    req.on("error", (error) => {
      reject(error);
    });

    req.on("end", () => {
      const bodyBuffer = Buffer.concat(chunks);
      const fields = new Map();
      const files = [];

      let cursor = 0;
      while (cursor < bodyBuffer.length) {
        const boundaryIndex = bodyBuffer.indexOf(boundaryBuffer, cursor);
        if (boundaryIndex === -1) {
          break;
        }
        cursor = boundaryIndex + boundaryBuffer.length;

        if (bodyBuffer[cursor] === 45 && bodyBuffer[cursor + 1] === 45) {
          break;
        }
        if (bodyBuffer[cursor] === 13 && bodyBuffer[cursor + 1] === 10) {
          cursor += 2;
        }

        const headersEnd = bodyBuffer.indexOf(headerSplitBuffer, cursor);
        if (headersEnd === -1) {
          break;
        }

        const headerText = bodyBuffer.slice(cursor, headersEnd).toString("utf8");
        const headers = {};
        for (const lineRaw of headerText.split(/\r?\n/)) {
          const line = String(lineRaw || "").trim();
          if (!line) {
            continue;
          }
          const colonIndex = line.indexOf(":");
          if (colonIndex <= 0) {
            continue;
          }
          const key = line.slice(0, colonIndex).trim().toLowerCase();
          const value = line.slice(colonIndex + 1).trim();
          headers[key] = value;
        }

        const disposition = String(headers["content-disposition"] || "");
        const nameMatch = /name="([^"]+)"/i.exec(disposition);
        const filenameMatch = /filename="([^"]*)"/i.exec(disposition);
        const fieldName = nameMatch ? nameMatch[1] : null;

        const dataStart = headersEnd + headerSplitBuffer.length;
        const nextBoundary = bodyBuffer.indexOf(boundaryBuffer, dataStart);
        if (nextBoundary === -1) {
          break;
        }

        let dataEnd = nextBoundary;
        if (bodyBuffer[dataEnd - 2] === 13 && bodyBuffer[dataEnd - 1] === 10) {
          dataEnd -= 2;
        }
        const data = bodyBuffer.slice(dataStart, dataEnd);

        if (fieldName) {
          if (filenameMatch && filenameMatch[1] !== "") {
            files.push({
              fieldName,
              fileName: filenameMatch[1],
              contentType: String(headers["content-type"] || "application/octet-stream"),
              data
            });
          } else {
            const textValue = data.toString("utf8");
            const existing = fields.get(fieldName);
            if (existing) {
              existing.push(textValue);
            } else {
              fields.set(fieldName, [textValue]);
            }
          }
        }

        cursor = nextBoundary;
      }

      resolve({ fields, files });
    });
  });
}

function resolveLidarTargetName(fileNameRaw) {
  const baseName = path.basename(String(fileNameRaw || ""));
  const lower = baseName.toLowerCase();
  if (lower.endsWith(".copc.laz")) {
    return "lidar_input.copc.laz";
  }
  const ext = path.extname(baseName);
  if (!ext) {
    throw new Error(`LiDAR file is missing extension: ${baseName}`);
  }
  return `lidar_input${ext.toLowerCase()}`;
}

function resolveLidarInputPathIfExists() {
  const rawInputsDir = resolveRawDataInputsDir(true);
  if (!rawInputsDir) {
    return null;
  }
  const candidates = [
    path.join(rawInputsDir, "lidar_input.copc.laz"),
    path.join(rawInputsDir, "lidar_input.laz"),
    path.join(rawInputsDir, "lidar_input.las")
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      return candidate;
    }
  }
  return null;
}

function hasFootprintsInput() {
  const rawInputsDir = resolveRawDataInputsDir(true);
  if (!rawInputsDir) {
    return false;
  }
  const footprintsPath = path.join(rawInputsDir, "Footprints.gdb");
  return fs.existsSync(footprintsPath) && fs.statSync(footprintsPath).isDirectory();
}

function findShapefilesRecursively(baseDir) {
  if (!baseDir || !fs.existsSync(baseDir) || !fs.statSync(baseDir).isDirectory()) {
    return [];
  }

  const out = [];
  const stack = [baseDir];
  while (stack.length > 0) {
    const current = stack.pop();
    const entries = fs.readdirSync(current, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(fullPath);
      } else if (entry.isFile() && /\.shp$/i.test(entry.name)) {
        out.push(fullPath);
      }
    }
  }
  out.sort((a, b) => a.localeCompare(b));
  return out;
}

function hasHydrologyInput() {
  const rawInputsDir = resolveRawDataInputsDir(true);
  if (!rawInputsDir) {
    return false;
  }
  const hydrologyDir = path.join(rawInputsDir, "Hydrology");
  return findShapefilesRecursively(hydrologyDir).length > 0;
}

function hasSoilsInput() {
  const rawInputsDir = resolveRawDataInputsDir(true);
  if (!rawInputsDir) {
    return false;
  }

  const soilsRoot = path.join(rawInputsDir, "SSURGO");
  const spatialDir = path.join(soilsRoot, "spatial");
  if (findShapefilesRecursively(spatialDir).length > 0) {
    return true;
  }

  if (!fs.existsSync(soilsRoot) || !fs.statSync(soilsRoot).isDirectory()) {
    return false;
  }

  const nestedCandidates = fs.readdirSync(soilsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(soilsRoot, entry.name, "spatial"));
  return nestedCandidates.some((candidate) => findShapefilesRecursively(candidate).length > 0);
}

function hasDemOutput() {
  const demPath = path.join(resolveProcessedDir(), "dem.tif");
  return fs.existsSync(demPath) && fs.statSync(demPath).isFile();
}

function writeRequestBodyToFile(req, targetPath, maxBytes = MAX_MULTIPART_UPLOAD_BYTES) {
  return new Promise((resolve, reject) => {
    let writtenBytes = 0;
    const output = fs.createWriteStream(targetPath);

    const onError = (error) => {
      output.destroy();
      reject(error);
    };

    req.on("error", onError);
    output.on("error", onError);

    req.on("data", (chunk) => {
      writtenBytes += chunk.length;
      if (writtenBytes > maxBytes) {
        onError(new Error(`Upload exceeds ${Math.round(maxBytes / (1024 * 1024))} MB limit.`));
        req.destroy();
      }
    });

    output.on("finish", () => {
      resolve(writtenBytes);
    });

    req.pipe(output);
  });
}

function resolveUploadDestination({
  sourceType,
  relativePath,
  originalName,
  replaceExisting
}) {
  const normalizedType = String(sourceType || "").trim().toLowerCase();
  if (!DATA_SOURCE_TYPE_SET.has(normalizedType)) {
    throw new Error(`Invalid sourceType: ${sourceType}`);
  }

  const rawInputsDir = ensureDir(resolveRawDataInputsDir(false));
  const safeOriginalName = sanitizeUploadPathSegment(path.basename(String(originalName || "upload.bin")));
  const safeRelativePath = sanitizeUploadRelativePath(relativePath || safeOriginalName);

  if (normalizedType === "lidar") {
    const targetName = resolveLidarTargetName(safeOriginalName);
    const targetPath = path.join(rawInputsDir, targetName);
    fs.rmSync(path.join(rawInputsDir, "lidar_input.copc.laz"), { force: true });
    fs.rmSync(path.join(rawInputsDir, "lidar_input.laz"), { force: true });
    fs.rmSync(path.join(rawInputsDir, "lidar_input.las"), { force: true });
    return {
      rawInputsDir,
      targetPath,
      sourceType: normalizedType,
      relativePath: safeRelativePath,
      originalName: safeOriginalName
    };
  }

  if (normalizedType === "footprints") {
    const footprintRoot = path.join(rawInputsDir, "Footprints.gdb");
    if (replaceExisting) {
      fs.rmSync(footprintRoot, { recursive: true, force: true });
    }
    ensureDir(footprintRoot);

    const relativeParts = safeRelativePath.split("/").filter(Boolean);
    const firstPart = relativeParts[0] || "";
    const remaining = firstPart.toLowerCase().endsWith(".gdb")
      ? relativeParts.slice(1)
      : relativeParts;
    const innerPath = remaining.join("/");
    if (!innerPath) {
      throw new Error(`Unable to derive destination path for footprint upload: ${safeOriginalName}`);
    }

    const targetPath = path.join(footprintRoot, innerPath);
    return {
      rawInputsDir,
      targetPath,
      sourceType: normalizedType,
      relativePath: safeRelativePath,
      originalName: safeOriginalName
    };
  }

  if (normalizedType === "hydrology") {
    const hydrologyRoot = path.join(rawInputsDir, "Hydrology");
    if (replaceExisting) {
      fs.rmSync(hydrologyRoot, { recursive: true, force: true });
    }
    ensureDir(hydrologyRoot);

    const relativeParts = safeRelativePath.split("/").filter(Boolean);
    const innerPath = relativeParts.length > 1 ? relativeParts.slice(1).join("/") : relativeParts[0];
    if (!innerPath) {
      throw new Error(`Unable to derive destination path for hydrology upload: ${safeOriginalName}`);
    }

    const targetPath = path.join(hydrologyRoot, innerPath);
    return {
      rawInputsDir,
      targetPath,
      sourceType: normalizedType,
      relativePath: safeRelativePath,
      originalName: safeOriginalName
    };
  }

  if (normalizedType === "soils") {
    const soilsRoot = path.join(rawInputsDir, "SSURGO");
    if (replaceExisting) {
      fs.rmSync(soilsRoot, { recursive: true, force: true });
    }
    ensureDir(soilsRoot);

    const relativeParts = safeRelativePath.split("/").filter(Boolean);
    const first = String(relativeParts[0] || "").toLowerCase();
    const hasCanonicalTopLevel = first === "spatial" || first === "tabular" || first === "thematic";
    const innerPath = hasCanonicalTopLevel
      ? relativeParts.join("/")
      : (relativeParts.length > 1 ? relativeParts.slice(1).join("/") : relativeParts[0]);
    if (!innerPath) {
      throw new Error(`Unable to derive destination path for soils upload: ${safeOriginalName}`);
    }

    const targetPath = path.join(soilsRoot, innerPath);
    return {
      rawInputsDir,
      targetPath,
      sourceType: normalizedType,
      relativePath: safeRelativePath,
      originalName: safeOriginalName
    };
  }

  const targetPath = path.join(rawInputsDir, safeOriginalName);
  return {
    rawInputsDir,
    targetPath,
    sourceType: normalizedType,
    relativePath: safeRelativePath,
    originalName: safeOriginalName
  };
}

function runNodeScript(scriptPath, args = []) {
  if (!fs.existsSync(scriptPath)) {
    throw new Error(`Missing script: ${scriptPath}`);
  }

  const result = spawnSync(process.execPath, [scriptPath, ...args], {
    cwd: ROOT_DIR,
    encoding: "utf8",
    maxBuffer: 1024 * 1024 * 1024
  });

  if (result.error && (result.status === null || result.status === undefined)) {
    throw result.error;
  }

  if (result.status !== 0) {
    const details = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new Error(details || `Script failed: ${path.basename(scriptPath)}`);
  }

  return {
    script: path.relative(ROOT_DIR, scriptPath),
    stdout: String(result.stdout || "").split(/\r?\n/).filter(Boolean).slice(-40)
  };
}

function parseUploadedTypes(uploadedTypesInput) {
  return new Set(
    Array.isArray(uploadedTypesInput)
      ? uploadedTypesInput
        .map((value) => String(value || "").trim().toLowerCase())
        .filter((value) => DATA_SOURCE_TYPE_SET.has(value))
      : []
  );
}

function buildAutoProcessPlan(uploadedTypesInput) {
  const uploadedTypes = parseUploadedTypes(uploadedTypesInput);
  const lidarPath = resolveLidarInputPathIfExists();
  const hasLidar = Boolean(lidarPath);
  const hasFootprints = hasFootprintsInput();
  const hasHydrology = hasHydrologyInput();
  const hasSoils = hasSoilsInput();
  const demExists = hasDemOutput();

  const shouldRunLidarProducts = hasLidar && (uploadedTypes.size === 0 || uploadedTypes.has("lidar"));
  const shouldRunDem = hasLidar && (
    uploadedTypes.size === 0 ||
    uploadedTypes.has("lidar") ||
    (uploadedTypes.has("hydrology") && !demExists)
  );
  const shouldRunBuildings = hasLidar && hasFootprints && (
    uploadedTypes.size === 0 || uploadedTypes.has("lidar") || uploadedTypes.has("footprints")
  );
  const shouldRunHydrology = hasHydrology && (demExists || shouldRunDem) && (
    uploadedTypes.size === 0 || uploadedTypes.has("lidar") || uploadedTypes.has("hydrology")
  );
  const shouldRunSoils = hasSoils && (demExists || shouldRunDem) && (
    uploadedTypes.size === 0 || uploadedTypes.has("lidar") || uploadedTypes.has("soils")
  );

  const steps = [];
  if (shouldRunDem) {
    steps.push(DATA_SOURCE_PROCESS_STEP_CONFIG.dem);
  }
  if (shouldRunHydrology) {
    steps.push(DATA_SOURCE_PROCESS_STEP_CONFIG.hydrology);
  }
  if (shouldRunSoils) {
    steps.push(DATA_SOURCE_PROCESS_STEP_CONFIG.soils);
  }
  if (shouldRunLidarProducts) {
    steps.push(DATA_SOURCE_PROCESS_STEP_CONFIG.vegetation);
    steps.push(DATA_SOURCE_PROCESS_STEP_CONFIG.trees);
  }
  if (shouldRunBuildings) {
    steps.push(DATA_SOURCE_PROCESS_STEP_CONFIG.buildings);
  }

  return {
    hasLidar,
    hasFootprints,
    hasHydrology,
    hasSoils,
    lidarPath: lidarPath ? path.relative(ROOT_DIR, lidarPath) : null,
    uploadedTypes: Array.from(uploadedTypes),
    steps: steps.map((step) => ({
      id: step.id,
      title: step.title,
      explanation: step.explanation
    }))
  };
}

function runProcessStep(stepIdRaw) {
  const stepId = String(stepIdRaw || "").trim().toLowerCase();
  const config = DATA_SOURCE_PROCESS_STEP_CONFIG[stepId];
  if (!config) {
    throw new Error(`Unknown process step: ${stepIdRaw}`);
  }

  const run = runNodeScript(config.scriptPath);
  resetDataCaches();

  return {
    step: {
      id: config.id,
      title: config.title,
      explanation: config.explanation
    },
    run
  };
}

function runPipelinesFromAvailableInputs(uploadedTypesInput) {
  const plan = buildAutoProcessPlan(uploadedTypesInput);
  const runs = [];
  for (const step of plan.steps) {
    const executed = runProcessStep(step.id);
    runs.push({
      step: executed.step,
      ...executed.run
    });
  }

  return {
    hasLidar: plan.hasLidar,
    hasFootprints: plan.hasFootprints,
    hasHydrology: plan.hasHydrology,
    hasSoils: plan.hasSoils,
    lidarPath: plan.lidarPath,
    executedCount: runs.length,
    runs
  };
}

function normalizeFootprintId(rawId) {
  return String(rawId || "")
    .trim()
    .replace(/[^a-zA-Z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function resolveBuildingsAssetsRoot() {
  return path.join(resolveProcessedDir(), "buildings", "assets");
}

function hasSupportedBuildingMeshExtension(filePath) {
  return SUPPORTED_BUILDING_MESH_EXTENSIONS.includes(path.extname(String(filePath || "")).toLowerCase());
}

function resolveExistingFileCaseInsensitive(candidatePath) {
  if (!candidatePath) {
    return null;
  }
  if (fs.existsSync(candidatePath) && fs.statSync(candidatePath).isFile()) {
    return candidatePath;
  }
  const dir = path.dirname(candidatePath);
  const targetBase = path.basename(candidatePath).toLowerCase();
  let entries = [];
  try {
    entries = fs.readdirSync(dir);
  } catch (error) {
    return null;
  }
  const match = entries.find((name) => name.toLowerCase() === targetBase);
  if (!match) {
    return null;
  }
  const resolved = path.join(dir, match);
  return fs.existsSync(resolved) && fs.statSync(resolved).isFile() ? resolved : null;
}

function resolveMeshPathAgnostic(meshInputRaw) {
  const meshInput = String(meshInputRaw || "").trim();
  if (!meshInput) {
    return null;
  }

  const basePath = path.isAbsolute(meshInput) ? meshInput : path.resolve(ROOT_DIR, meshInput);
  const direct = resolveExistingFileCaseInsensitive(basePath);
  if (direct && hasSupportedBuildingMeshExtension(direct)) {
    return direct;
  }

  const ext = path.extname(basePath).toLowerCase();
  if (ext && !SUPPORTED_BUILDING_MESH_EXTENSIONS.includes(ext)) {
    return null;
  }

  const stem = ext ? basePath.slice(0, -ext.length) : basePath;
  for (const supportedExt of SUPPORTED_BUILDING_MESH_EXTENSIONS) {
    const candidate = resolveExistingFileCaseInsensitive(stem + supportedExt);
    if (candidate) {
      return candidate;
    }
  }

  return null;
}

function findPhotogrammetryAssetByName(name) {
  const rawInputsDir = resolveRawDataInputsDir(true);
  if (!rawInputsDir) {
    return null;
  }
  const normalized = String(name || "").trim().toLowerCase();
  if (!normalized) {
    return null;
  }

  const entries = fs.readdirSync(rawInputsDir, { withFileTypes: true });
  const supported = entries
    .filter((entry) => entry.isFile() && hasSupportedBuildingMeshExtension(entry.name))
    .map((entry) => entry.name);

  const directNameMatch = supported.find((fileName) => fileName.toLowerCase() === normalized);
  if (directNameMatch) {
    return path.join(rawInputsDir, directNameMatch);
  }

  const sameStem = supported
    .filter((fileName) => path.parse(fileName).name.toLowerCase() === normalized)
    .sort((a, b) => a.localeCompare(b));

  if (sameStem.length === 0) {
    return null;
  }
  return path.join(rawInputsDir, sameStem[0]);
}

function runBuildingAssetPipeline({
  meshPath,
  footprintId,
  featureIndex,
  footprintsPath = null
}) {
  if (!fs.existsSync(GENERATE_BUILDING_ASSET_SCRIPT)) {
    throw new Error(`Missing generator script: ${GENERATE_BUILDING_ASSET_SCRIPT}`);
  }

  const args = [
    GENERATE_BUILDING_ASSET_SCRIPT,
    "--mesh",
    meshPath
  ];

  if (footprintId) {
    args.push("--footprint_id", footprintId);
  }
  if (Number.isInteger(featureIndex)) {
    args.push("--feature_index", String(featureIndex));
  }
  if (footprintsPath) {
    args.push("--footprints", footprintsPath);
  }

  const result = spawnSync(process.execPath, args, {
    cwd: ROOT_DIR,
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024
  });

  if (result.error && (result.status === null || result.status === undefined)) {
    throw result.error;
  }

  if (result.status !== 0) {
    const details = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new Error(details || "Unknown pipeline failure.");
  }

  const normalizedId = normalizeFootprintId(footprintId);
  const outDir = path.join(resolveBuildingsAssetsRoot(), normalizedId || `feature_${featureIndex ?? 0}`);
  const assetMetaPath = path.join(outDir, "asset_meta.json");

  return {
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    outDir,
    assetMetaPath
  };
}

function parseRangeHeader(rangeHeader, fileSize) {
  const match = /^bytes=(\d*)-(\d*)$/i.exec(rangeHeader || "");
  if (!match) {
    return null;
  }

  let start = match[1] ? Number(match[1]) : null;
  let end = match[2] ? Number(match[2]) : null;

  if (start === null && end === null) {
    return null;
  }

  if (start === null) {
    const suffixLength = end;
    if (!Number.isFinite(suffixLength)) {
      return null;
    }
    start = Math.max(fileSize - suffixLength, 0);
    end = fileSize - 1;
  } else if (end === null) {
    end = fileSize - 1;
  }

  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start || end >= fileSize) {
    return "invalid";
  }

  return { start, end };
}

function streamFile(req, res, absolutePath) {
  fs.stat(absolutePath, (statError, stats) => {
    if (statError || !stats.isFile()) {
      sendJson(res, 404, { error: "File not found" });
      return;
    }

    const mimeType = getMimeType(absolutePath);
    const range = parseRangeHeader(req.headers.range, stats.size);

    if (range === "invalid") {
      res.writeHead(416, {
        "Content-Range": `bytes */${stats.size}`
      });
      res.end();
      return;
    }

    if (range) {
      const { start, end } = range;
      res.writeHead(206, {
        "Content-Type": mimeType,
        "Content-Length": end - start + 1,
        "Accept-Ranges": "bytes",
        "Content-Range": `bytes ${start}-${end}/${stats.size}`
      });
      fs.createReadStream(absolutePath, { start, end }).pipe(res);
      return;
    }

    res.writeHead(200, {
      "Content-Type": mimeType,
      "Content-Length": stats.size,
      "Accept-Ranges": "bytes"
    });
    fs.createReadStream(absolutePath).pipe(res);
  });
}

function routeToFile(pathname) {
  if (pathname === "/" || pathname === "/index.html") {
    return path.join(VIEWER_DIR, "index.html");
  }

  if (pathname === "/viewer.js" || pathname === "/styles.css") {
    return path.join(VIEWER_DIR, pathname.slice(1));
  }

  if (pathname.startsWith("/node_modules/")) {
    return safeJoin(NODE_MODULES_DIR, pathname.slice("/node_modules/".length));
  }

  if (pathname.startsWith("/assets/")) {
    const assetsDir = resolveAssetsDir(true);
    if (!assetsDir) {
      return null;
    }
    const relative = decodeURIComponent(pathname.slice("/assets/".length));
    return safeJoin(assetsDir, relative);
  }

  if (pathname === "/data/dem.tif") {
    return resolveDemPath();
  }

  if (pathname.startsWith("/viewer/")) {
    return safeJoin(VIEWER_DIR, pathname.slice("/viewer/".length));
  }

  if (pathname.startsWith("/data/")) {
    return safeJoin(resolveProcessedDir(), pathname.slice("/data/".length));
  }

  return null;
}

let demGridCache = null;
let shrubsPointsCache = null;
let shrubsAssetManifestCache = null;
let treesAssetManifestCache = null;
let demSourceSrsCache = null;

function resetDataCaches() {
  demGridCache = null;
  shrubsPointsCache = null;
  demSourceSrsCache = null;
}

function parseXyzGrid(xyzText) {
  const lines = xyzText
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  if (lines.length === 0) {
    throw new Error("No DEM samples returned by GDAL.");
  }

  const xs = [];
  const ys = [];
  const zs = [];
  let minX = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const line of lines) {
    const parts = line.split(/\s+/);
    if (parts.length < 3) {
      continue;
    }
    const x = Number(parts[0]);
    const y = Number(parts[1]);
    const z = Number(parts[2]);
    xs.push(x);
    ys.push(y);
    zs.push(z);
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  }

  if (zs.length === 0) {
    throw new Error("Unable to parse DEM samples.");
  }

  const firstY = ys[0];
  let width = 0;
  for (let i = 0; i < ys.length; i += 1) {
    if (Math.abs(ys[i] - firstY) < 1e-9) {
      width += 1;
    } else {
      break;
    }
  }

  if (width < 2 || zs.length % width !== 0) {
    throw new Error("Unable to infer DEM grid dimensions.");
  }

  const height = Math.floor(zs.length / width);
  const nodata = -9999;
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  const heights = new Array(zs.length);

  for (let i = 0; i < zs.length; i += 1) {
    const v = zs[i];
    const invalid = !Number.isFinite(v) || Math.abs(v - nodata) < 1e-9;
    heights[i] = invalid ? null : v;
    if (!invalid) {
      min = Math.min(min, v);
      max = Math.max(max, v);
    }
  }

  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    throw new Error("DEM sample set contains no valid elevations.");
  }

  for (let i = 0; i < heights.length; i += 1) {
    if (heights[i] === null) {
      heights[i] = min;
    }
  }

  const xStep = width > 1 ? Math.abs(xs[1] - xs[0]) : 1;
  const yStep = height > 1 ? Math.abs(ys[0] - ys[width]) : 1;

  return {
    width,
    height,
    minX,
    maxX,
    minY,
    maxY,
    minElevation: min,
    maxElevation: max,
    xStep: Number.isFinite(xStep) && xStep > 0 ? xStep : 1,
    yStep: Number.isFinite(yStep) && yStep > 0 ? yStep : 1,
    heights
  };
}

function buildDemGrid() {
  const demPath = resolveDemPath();
  if (!fs.existsSync(demPath)) {
    throw new Error(`DEM not found at ${demPath}. Run "node generateDEM.js" first.`);
  }

  const translate = spawnSync(
    "gdal_translate",
    ["-of", "XYZ", "-outsize", "420", "0", demPath, "/vsistdout/"],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }
  );

  if (translate.error) {
    throw translate.error;
  }

  if (translate.status !== 0) {
    const details = [translate.stdout, translate.stderr].filter(Boolean).join("\n").trim();
    throw new Error(`gdal_translate failed.\n${details}`);
  }

  const grid = parseXyzGrid(translate.stdout || "");
  return grid;
}

function getDemGrid() {
  if (!demGridCache) {
    demGridCache = Promise.resolve().then(() => buildDemGrid());
  }
  return demGridCache;
}

function parseNumericTokens(text) {
  const matches = String(text || "").match(/[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?/g);
  if (!matches) {
    return [];
  }
  return matches.map((token) => Number(token)).filter((value) => Number.isFinite(value));
}

function buildDemSourceSrs() {
  const demPath = resolveDemPath();
  if (!fs.existsSync(demPath)) {
    throw new Error(`DEM not found at ${demPath}. Run "node generateDEM.js" first.`);
  }

  const epsgInfo = spawnSync(
    "gdalsrsinfo",
    ["-o", "epsg", demPath],
    { encoding: "utf8", maxBuffer: 4 * 1024 * 1024 }
  );
  if (epsgInfo.error) {
    throw epsgInfo.error;
  }
  if (epsgInfo.status === 0) {
    const epsgText = `${epsgInfo.stdout || ""}\n${epsgInfo.stderr || ""}`;
    const match = epsgText.match(/EPSG:\d+/i);
    if (match) {
      return match[0].toUpperCase();
    }
  }

  const proj4Info = spawnSync(
    "gdalsrsinfo",
    ["-o", "proj4", demPath],
    { encoding: "utf8", maxBuffer: 4 * 1024 * 1024 }
  );
  if (proj4Info.error) {
    throw proj4Info.error;
  }
  if (proj4Info.status !== 0) {
    const details = [proj4Info.stdout, proj4Info.stderr].filter(Boolean).join("\n").trim();
    throw new Error(`gdalsrsinfo failed.\n${details}`);
  }

  const proj4 = String(proj4Info.stdout || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line.startsWith("+proj="));
  if (!proj4) {
    throw new Error("Unable to determine DEM coordinate reference system.");
  }
  return proj4;
}

function getDemSourceSrs() {
  if (!demSourceSrsCache) {
    demSourceSrsCache = Promise.resolve().then(() => buildDemSourceSrs());
  }
  return demSourceSrsCache;
}

function convertDemXYToWgs84(x, y) {
  return getDemSourceSrs().then((sourceSrs) => {
    const transform = spawnSync(
      "gdaltransform",
      ["-s_srs", sourceSrs, "-t_srs", "EPSG:4326"],
      { encoding: "utf8", input: `${x} ${y}\n`, maxBuffer: 4 * 1024 * 1024 }
    );

    if (transform.error) {
      throw transform.error;
    }
    if (transform.status !== 0) {
      const details = [transform.stdout, transform.stderr].filter(Boolean).join("\n").trim();
      throw new Error(`gdaltransform failed.\n${details}`);
    }

    const nums = parseNumericTokens(transform.stdout || "");
    if (nums.length < 2) {
      throw new Error(`Unable to parse gdaltransform output: "${(transform.stdout || "").trim()}"`);
    }

    const lon = nums[0];
    const lat = nums[1];
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      throw new Error("Converted WGS84 coordinates are not finite numbers.");
    }
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
      throw new Error(`Converted WGS84 coordinates are out of bounds: lat=${lat}, lon=${lon}`);
    }

    return { lat, lon, sourceSrs };
  });
}

function parseShrubsCsv(csvText) {
  const lines = csvText
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  if (lines.length === 0) {
    throw new Error("No shrub point rows found in CSV export.");
  }

  const packed = [];
  let hagMin = Number.POSITIVE_INFINITY;
  let hagMax = Number.NEGATIVE_INFINITY;

  for (let i = 0; i < lines.length; i += 1) {
    const parts = lines[i].split(",");
    if (parts.length < 4) {
      continue;
    }

    const x = Number(parts[0].replace(/"/g, ""));
    const y = Number(parts[1].replace(/"/g, ""));
    const z = Number(parts[2].replace(/"/g, ""));
    const hag = Number(parts[3].replace(/"/g, ""));

    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z) || !Number.isFinite(hag)) {
      continue;
    }

    packed.push(x, y, z, hag);
    hagMin = Math.min(hagMin, hag);
    hagMax = Math.max(hagMax, hag);
  }

  if (packed.length === 0) {
    throw new Error("Shrub CSV parsed, but no valid rows found.");
  }

  return {
    count: packed.length / 4,
    hagMin: Number.isFinite(hagMin) ? hagMin : 0,
    hagMax: Number.isFinite(hagMax) ? hagMax : 0,
    points: packed
  };
}

function buildShrubsPoints() {
  const shrubsPath = resolveShrubsPointsPath();
  if (!fs.existsSync(shrubsPath)) {
    throw new Error(
      `Shrubs points not found at ${shrubsPath}. Run "npm run vegetation" first.`
    );
  }

  const csvPath = path.join("/tmp", `mazzap-shrubs-${process.pid}-${Date.now()}.csv`);
  const translate = spawnSync(
    "pdal",
    [
      "translate",
      shrubsPath,
      csvPath,
      "--writers.text.format=csv",
      "--writers.text.order=X,Y,Z,HeightAboveGround",
      "--writers.text.keep_unspecified=false"
    ],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }
  );

  if (translate.error) {
    throw translate.error;
  }

  if (translate.status !== 0) {
    const details = [translate.stdout, translate.stderr].filter(Boolean).join("\n").trim();
    throw new Error(`pdal translate (shrubs export) failed.\n${details}`);
  }

  const csvText = fs.readFileSync(csvPath, "utf8");
  fs.rmSync(csvPath, { force: true });
  return parseShrubsCsv(csvText);
}

function getShrubsPoints() {
  if (!shrubsPointsCache) {
    shrubsPointsCache = Promise.resolve().then(() => buildShrubsPoints());
  }
  return shrubsPointsCache;
}

function buildShrubAssetManifest() {
  const assetsDir = resolveAssetsDir();
  const entries = fs.readdirSync(assetsDir, { withFileTypes: true });
  const fileNames = new Set(entries.filter((entry) => entry.isFile()).map((entry) => entry.name));

  const variants = [];
  for (const fileName of fileNames) {
    if (!/^plant_bush.*\.obj$/i.test(fileName)) {
      continue;
    }

    const baseName = fileName.replace(/\.obj$/i, "");
    const mtlName = `${baseName}.mtl`;
    const hasMtl = fileNames.has(mtlName);

    variants.push({
      name: baseName,
      objUrl: `/assets/${encodeURIComponent(fileName)}`,
      mtlUrl: hasMtl ? `/assets/${encodeURIComponent(mtlName)}` : null
    });
  }

  variants.sort((a, b) => a.name.localeCompare(b.name));
  if (variants.length === 0) {
    throw new Error(`No plant_bush*.obj assets found in ${assetsDir}`);
  }

  return {
    assetsDir: path.basename(assetsDir),
    variantCount: variants.length,
    variants
  };
}

function getShrubAssetManifest() {
  if (!shrubsAssetManifestCache) {
    shrubsAssetManifestCache = Promise.resolve().then(() => buildShrubAssetManifest());
  }
  return shrubsAssetManifestCache;
}

function buildTreeAssetManifest() {
  const assetsDir = resolveAssetsDir();
  const entries = fs.readdirSync(assetsDir, { withFileTypes: true });
  const fileNames = new Set(entries.filter((entry) => entry.isFile()).map((entry) => entry.name));

  const variants = [];
  for (const fileName of fileNames) {
    if (!/^tree.*\.obj$/i.test(fileName)) {
      continue;
    }

    const baseName = fileName.replace(/\.obj$/i, "");
    const mtlName = `${baseName}.mtl`;
    const hasMtl = fileNames.has(mtlName);

    variants.push({
      name: baseName,
      objUrl: `/assets/${encodeURIComponent(fileName)}`,
      mtlUrl: hasMtl ? `/assets/${encodeURIComponent(mtlName)}` : null
    });
  }

  variants.sort((a, b) => a.name.localeCompare(b.name));
  if (variants.length === 0) {
    throw new Error(`No tree*.obj assets found in ${assetsDir}`);
  }

  return {
    assetsDir: path.basename(assetsDir),
    variantCount: variants.length,
    variants
  };
}

function getTreeAssetManifest() {
  if (!treesAssetManifestCache) {
    treesAssetManifestCache = Promise.resolve().then(() => buildTreeAssetManifest());
  }
  return treesAssetManifestCache;
}

function buildBuildingAssetsIndex() {
  const assetsRoot = resolveBuildingsAssetsRoot();
  if (!fs.existsSync(assetsRoot) || !fs.statSync(assetsRoot).isDirectory()) {
    return {
      assetsRoot: path.relative(ROOT_DIR, assetsRoot),
      count: 0,
      assets: []
    };
  }

  const entries = fs.readdirSync(assetsRoot, { withFileTypes: true });
  const assets = [];

  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue;
    }
    const dirName = entry.name;
    const assetDir = path.join(assetsRoot, dirName);
    const metaPath = path.join(assetDir, "asset_meta.json");
    if (!fs.existsSync(metaPath)) {
      continue;
    }

    let meta;
    try {
      meta = JSON.parse(fs.readFileSync(metaPath, "utf8"));
    } catch (error) {
      continue;
    }

    const lods = Array.isArray(meta?.output?.lods) ? meta.output.lods : [];
    const lod0 = lods.find((lod) => Number(lod?.level) === 0) || lods[0] || { path: "lod0.glb" };
    const lod0Path = path.join(assetDir, String(lod0?.path || "lod0.glb"));
    if (!fs.existsSync(lod0Path)) {
      continue;
    }

    assets.push({
      footprintId: String(meta?.footprint_id || dirName),
      featureIndex: Number.isInteger(meta?.feature_index) ? meta.feature_index : null,
      dir: path.relative(ROOT_DIR, assetDir),
      metaPath: path.relative(ROOT_DIR, metaPath),
      lod0Path: path.relative(ROOT_DIR, lod0Path)
    });
  }

  assets.sort((a, b) => a.footprintId.localeCompare(b.footprintId));
  return {
    assetsRoot: path.relative(ROOT_DIR, assetsRoot),
    count: assets.length,
    assets
  };
}

function getPathSizeBytes(targetPath) {
  if (!targetPath || !fs.existsSync(targetPath)) {
    return 0;
  }
  const stats = fs.statSync(targetPath);
  if (stats.isFile()) {
    return stats.size;
  }
  if (!stats.isDirectory()) {
    return 0;
  }

  let total = 0;
  const entries = fs.readdirSync(targetPath, { withFileTypes: true });
  for (const entry of entries) {
    total += getPathSizeBytes(path.join(targetPath, entry.name));
  }
  return total;
}

function classifyRawSourceType(name, isDirectory) {
  const lower = String(name || "").toLowerCase();
  if (/^lidar_input(\.|$)/i.test(lower)) {
    return "lidar";
  }
  if (lower === "ssurgo" || lower.endsWith(".mdb") || lower.startsWith("soil_metadata")) {
    return "soils";
  }
  if (lower === "footprints.gdb" || (isDirectory && lower.endsWith(".gdb"))) {
    return "footprints";
  }
  if (
    lower === "hydrology" ||
    lower.endsWith(".shp") ||
    lower.endsWith(".shx") ||
    lower.endsWith(".dbf") ||
    lower.endsWith(".prj")
  ) {
    return "hydrology";
  }
  return "photogrammetry";
}

function listManageDataSources() {
  const rawInputsDir = ensureDir(resolveRawDataInputsDir(false));
  const entries = fs.readdirSync(rawInputsDir, { withFileTypes: true });
  const sources = [];

  for (const entry of entries) {
    if (!entry || !entry.name || entry.name.startsWith(".")) {
      continue;
    }

    const absPath = path.join(rawInputsDir, entry.name);
    sources.push({
      name: entry.name,
      relativePath: entry.name,
      type: classifyRawSourceType(entry.name, entry.isDirectory()),
      isDirectory: entry.isDirectory(),
      sizeBytes: getPathSizeBytes(absPath)
    });
  }

  sources.sort((a, b) => {
    const typeCompare = String(a.type || "").localeCompare(String(b.type || ""));
    if (typeCompare !== 0) {
      return typeCompare;
    }
    return String(a.name || "").localeCompare(String(b.name || ""));
  });

  const processedDir = ensureDir(resolveProcessedDir());
  const processedEntries = fs.readdirSync(processedDir, { withFileTypes: true });
  const processedItems = processedEntries.filter((entry) => entry && !entry.name.startsWith(".")).length;

  return {
    rawInputsDir: path.relative(ROOT_DIR, rawInputsDir),
    processedDir: path.relative(ROOT_DIR, processedDir),
    sourceCount: sources.length,
    sources,
    processedSummary: {
      itemCount: processedItems,
      sizeBytes: getPathSizeBytes(processedDir)
    }
  };
}

function deleteManagedDataSource(relativePathRaw) {
  const rawInputsDir = ensureDir(resolveRawDataInputsDir(false));
  const relativePath = sanitizeUploadRelativePath(relativePathRaw);
  if (!relativePath) {
    throw new Error("Missing or invalid relativePath.");
  }

  const targetPath = safeJoin(rawInputsDir, relativePath);
  if (!targetPath || !targetPath.startsWith(rawInputsDir)) {
    throw new Error("Invalid data source path.");
  }
  if (!fs.existsSync(targetPath)) {
    throw new Error(`Data source not found: ${relativePath}`);
  }

  const wasDirectory = fs.statSync(targetPath).isDirectory();
  const sizeBytes = getPathSizeBytes(targetPath);
  fs.rmSync(targetPath, { recursive: true, force: true });
  resetDataCaches();

  return {
    relativePath,
    wasDirectory,
    sizeBytes
  };
}

function clearDirectoryContents(dirPath) {
  if (!dirPath || !fs.existsSync(dirPath) || !fs.statSync(dirPath).isDirectory()) {
    return { removedCount: 0, removedSizeBytes: 0 };
  }

  let removedCount = 0;
  let removedSizeBytes = 0;
  const entries = fs.readdirSync(dirPath, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry || !entry.name || entry.name === ".gitkeep") {
      continue;
    }
    const target = path.join(dirPath, entry.name);
    removedSizeBytes += getPathSizeBytes(target);
    fs.rmSync(target, { recursive: true, force: true });
    removedCount += 1;
  }

  return { removedCount, removedSizeBytes };
}

function clearAllProjectData() {
  const rawInputsDir = ensureDir(resolveRawDataInputsDir(false));
  const processedDir = ensureDir(resolveProcessedDir());

  const rawRemoved = clearDirectoryContents(rawInputsDir);
  const processedRemoved = clearDirectoryContents(processedDir);
  resetDataCaches();

  return {
    rawInputsDir: path.relative(ROOT_DIR, rawInputsDir),
    processedDir: path.relative(ROOT_DIR, processedDir),
    rawRemoved,
    processedRemoved
  };
}

function getHydrologyOutputStatus() {
  const processedDir = resolveProcessedDir();
  const hydrologyDir = path.join(processedDir, "hydrology");
  const localGeoJson = path.join(hydrologyDir, "hydrology_clipped_local.geojson");
  const geoJson = path.join(hydrologyDir, "hydrology_clipped.geojson");

  if (fs.existsSync(localGeoJson) && fs.statSync(localGeoJson).isFile()) {
    return {
      available: true,
      preferredPath: "/data/hydrology/hydrology_clipped_local.geojson",
      fallbackPath: "/data/hydrology/hydrology_clipped.geojson"
    };
  }

  if (fs.existsSync(geoJson) && fs.statSync(geoJson).isFile()) {
    return {
      available: true,
      preferredPath: "/data/hydrology/hydrology_clipped.geojson",
      fallbackPath: null
    };
  }

  return {
    available: false,
    preferredPath: null,
    fallbackPath: null
  };
}

function getSoilsOutputStatus() {
  const processedDir = resolveProcessedDir();
  const soilsDir = path.join(processedDir, "soils");
  const localGeoJson = path.join(soilsDir, "soils_clipped_local.geojson");
  const geoJson = path.join(soilsDir, "soils_clipped.geojson");
  const legend = path.join(soilsDir, "soil_legend.json");

  if (fs.existsSync(localGeoJson) && fs.statSync(localGeoJson).isFile()) {
    return {
      available: true,
      preferredPath: "/data/soils/soils_clipped_local.geojson",
      fallbackPath: "/data/soils/soils_clipped.geojson",
      legendPath: fs.existsSync(legend) ? "/data/soils/soil_legend.json" : null
    };
  }

  if (fs.existsSync(geoJson) && fs.statSync(geoJson).isFile()) {
    return {
      available: true,
      preferredPath: "/data/soils/soils_clipped.geojson",
      fallbackPath: null,
      legendPath: fs.existsSync(legend) ? "/data/soils/soil_legend.json" : null
    };
  }

  return {
    available: false,
    preferredPath: null,
    fallbackPath: null,
    legendPath: null
  };
}

const server = http.createServer((req, res) => {
  const requestUrl = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const { pathname } = requestUrl;

  if (pathname === "/health") {
    sendJson(res, 200, { ok: true });
    return;
  }

  if (pathname === "/api/hydrology/status") {
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "Method not allowed. Use GET." });
      return;
    }

    try {
      const status = getHydrologyOutputStatus();
      sendJson(res, 200, { ok: true, ...status });
    } catch (error) {
      sendJson(res, 500, { error: error.message });
    }
    return;
  }

  if (pathname === "/api/soils/status") {
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "Method not allowed. Use GET." });
      return;
    }

    try {
      const status = getSoilsOutputStatus();
      sendJson(res, 200, { ok: true, ...status });
    } catch (error) {
      sendJson(res, 500, { error: error.message });
    }
    return;
  }

  if (pathname === "/api/data-sources/list") {
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "Method not allowed. Use GET." });
      return;
    }

    try {
      const summary = listManageDataSources();
      sendJson(res, 200, { ok: true, ...summary });
    } catch (error) {
      sendJson(res, 500, { error: error.message });
    }
    return;
  }

  if (pathname === "/api/data-sources/delete") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    readJsonBody(req)
      .then((body) => {
        const deleted = deleteManagedDataSource(body?.relativePath || "");
        sendJson(res, 200, { ok: true, deleted });
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/api/data-sources/clear") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    try {
      const cleared = clearAllProjectData();
      sendJson(res, 200, { ok: true, cleared });
    } catch (error) {
      sendJson(res, 500, { error: error.message });
    }
    return;
  }

  if (pathname === "/api/data-sources/upload-item") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    try {
      const sourceType = String(requestUrl.searchParams.get("sourceType") || "").trim().toLowerCase();
      const relativePath = String(requestUrl.searchParams.get("relativePath") || "");
      const originalName = String(requestUrl.searchParams.get("originalName") || "");
      const replaceExisting = String(requestUrl.searchParams.get("replace") || "").trim() === "1";

      const destination = resolveUploadDestination({
        sourceType,
        relativePath,
        originalName,
        replaceExisting
      });

      const targetDir = path.dirname(destination.targetPath);
      ensureDir(targetDir);

      writeRequestBodyToFile(req, destination.targetPath)
        .then((writtenBytes) => {
          sendJson(res, 200, {
            ok: true,
            file: {
              sourceType: destination.sourceType,
              originalName: destination.originalName,
              relativePath: destination.relativePath,
              storedAs: path.relative(ROOT_DIR, destination.targetPath),
              size: writtenBytes
            }
          });
        })
        .catch((error) => {
          sendJson(res, 500, { error: error.message });
        });
    } catch (error) {
      sendJson(res, 400, { error: error.message });
    }
    return;
  }

  if (pathname === "/api/data-sources/process") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    readJsonBody(req)
      .then((body) => {
        const uploadedTypes = Array.isArray(body?.uploadedTypes) ? body.uploadedTypes : [];
        const result = runPipelinesFromAvailableInputs(uploadedTypes);
        resetDataCaches();

        sendJson(res, 200, {
          ok: true,
          hasLidar: result.hasLidar,
          hasFootprints: result.hasFootprints,
          hasHydrology: result.hasHydrology,
          hasSoils: result.hasSoils,
          lidarPath: result.lidarPath,
          executedCount: result.executedCount,
          runs: result.runs
        });
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/api/data-sources/process-plan") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    readJsonBody(req)
      .then((body) => {
        const uploadedTypes = Array.isArray(body?.uploadedTypes) ? body.uploadedTypes : [];
        const plan = buildAutoProcessPlan(uploadedTypes);
        sendJson(res, 200, {
          ok: true,
          hasLidar: plan.hasLidar,
          hasFootprints: plan.hasFootprints,
          hasHydrology: plan.hasHydrology,
          hasSoils: plan.hasSoils,
          lidarPath: plan.lidarPath,
          uploadedTypes: plan.uploadedTypes,
          stepCount: plan.steps.length,
          steps: plan.steps
        });
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/api/data-sources/process-step") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    readJsonBody(req)
      .then((body) => {
        const stepId = String(body?.stepId || "").trim().toLowerCase();
        if (!stepId) {
          throw new Error("Missing stepId.");
        }

        const executed = runProcessStep(stepId);
        sendJson(res, 200, {
          ok: true,
          step: executed.step,
          script: executed.run.script,
          stdout: executed.run.stdout
        });
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/api/data-sources/upload") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    parseMultipartForm(req)
      .then(({ fields, files }) => {
        if (!Array.isArray(files) || files.length === 0) {
          throw new Error("No uploaded files were provided.");
        }

        const rawInputsDir = ensureDir(resolveRawDataInputsDir(false));
        const filePartsByIndex = new Map();
        for (const filePart of files) {
          const match = /^file_(\d+)$/i.exec(String(filePart.fieldName || ""));
          if (!match) {
            continue;
          }
          filePartsByIndex.set(Number(match[1]), filePart);
        }

        const itemMetaByIndex = new Map();
        for (const [fieldName, values] of fields.entries()) {
          const match = /^meta_(\d+)$/i.exec(String(fieldName || ""));
          if (!match || !Array.isArray(values) || values.length === 0) {
            continue;
          }
          const raw = values[values.length - 1];
          let parsed = {};
          try {
            parsed = raw ? JSON.parse(raw) : {};
          } catch (error) {
            parsed = {};
          }
          itemMetaByIndex.set(Number(match[1]), parsed || {});
        }

        const sortedIndexes = Array.from(filePartsByIndex.keys()).sort((a, b) => a - b);
        if (sortedIndexes.length === 0) {
          throw new Error("No recognized upload fields found.");
        }

        let lidarPrepared = false;
        let footprintsPrepared = false;
        let hydrologyPrepared = false;
        let soilsPrepared = false;
        const saved = [];

        for (const index of sortedIndexes) {
          const filePart = filePartsByIndex.get(index);
          if (!filePart || !Buffer.isBuffer(filePart.data) || filePart.data.length === 0) {
            continue;
          }

          const meta = itemMetaByIndex.get(index) || {};
          const sourceType = String(meta.sourceType || "photogrammetry").trim().toLowerCase();
          if (!DATA_SOURCE_TYPE_SET.has(sourceType)) {
            throw new Error(`Invalid sourceType for upload item ${index}: ${sourceType}`);
          }

          const originalName = sanitizeUploadPathSegment(path.basename(String(filePart.fileName || "upload.bin")));
          const relativePathRaw = String(meta.relativePath || originalName);
          const relativePath = sanitizeUploadRelativePath(relativePathRaw || originalName);

          let targetPath;
          if (sourceType === "lidar") {
            if (!lidarPrepared) {
              fs.rmSync(path.join(rawInputsDir, "lidar_input.copc.laz"), { force: true });
              fs.rmSync(path.join(rawInputsDir, "lidar_input.laz"), { force: true });
              fs.rmSync(path.join(rawInputsDir, "lidar_input.las"), { force: true });
              lidarPrepared = true;
            }
            const targetName = resolveLidarTargetName(originalName);
            targetPath = path.join(rawInputsDir, targetName);
          } else if (sourceType === "footprints") {
            const footprintRoot = path.join(rawInputsDir, "Footprints.gdb");
            if (!footprintsPrepared) {
              fs.rmSync(footprintRoot, { recursive: true, force: true });
              ensureDir(footprintRoot);
              footprintsPrepared = true;
            }
            const parts = relativePath.split("/").filter(Boolean);
            const innerPath = parts.length > 1 ? parts.slice(1).join("/") : parts[0];
            if (!innerPath) {
              throw new Error(`Unable to derive destination path for footprint file: ${originalName}`);
            }
            targetPath = path.join(footprintRoot, innerPath);
          } else if (sourceType === "hydrology") {
            const hydrologyRoot = path.join(rawInputsDir, "Hydrology");
            if (!hydrologyPrepared) {
              fs.rmSync(hydrologyRoot, { recursive: true, force: true });
              ensureDir(hydrologyRoot);
              hydrologyPrepared = true;
            }
            const parts = relativePath.split("/").filter(Boolean);
            const innerPath = parts.length > 1 ? parts.slice(1).join("/") : parts[0];
            if (!innerPath) {
              throw new Error(`Unable to derive destination path for hydrology file: ${originalName}`);
            }
            targetPath = path.join(hydrologyRoot, innerPath);
          } else if (sourceType === "soils") {
            const soilsRoot = path.join(rawInputsDir, "SSURGO");
            if (!soilsPrepared) {
              fs.rmSync(soilsRoot, { recursive: true, force: true });
              ensureDir(soilsRoot);
              soilsPrepared = true;
            }
            const parts = relativePath.split("/").filter(Boolean);
            const first = String(parts[0] || "").toLowerCase();
            const hasCanonicalTopLevel = first === "spatial" || first === "tabular" || first === "thematic";
            const innerPath = hasCanonicalTopLevel
              ? parts.join("/")
              : (parts.length > 1 ? parts.slice(1).join("/") : parts[0]);
            if (!innerPath) {
              throw new Error(`Unable to derive destination path for soils file: ${originalName}`);
            }
            targetPath = path.join(soilsRoot, innerPath);
          } else {
            targetPath = path.join(rawInputsDir, originalName);
          }

          const targetDir = path.dirname(targetPath);
          ensureDir(targetDir);
          fs.writeFileSync(targetPath, filePart.data);

          saved.push({
            sourceType,
            originalName,
            relativePath,
            storedAs: path.relative(ROOT_DIR, targetPath),
            size: filePart.data.length
          });
        }

        sendJson(res, 200, {
          ok: true,
          savedCount: saved.length,
          rawInputsDir: path.relative(ROOT_DIR, rawInputsDir),
          files: saved
        });
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/api/buildings/process-selected-asset") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed. Use POST." });
      return;
    }

    readJsonBody(req)
      .then((body) => {
        const footprintId = normalizeFootprintId(body.footprintId);
        const featureIndexRaw = body.featureIndex;
        const featureIndex = Number.isInteger(featureIndexRaw)
          ? featureIndexRaw
          : Number.isInteger(Number(featureIndexRaw))
            ? Number(featureIndexRaw)
            : null;

        if (!footprintId && featureIndex === null) {
          throw new Error("Missing footprint selection. Provide footprintId or featureIndex.");
        }

        const requestedMesh = body.meshPath ? String(body.meshPath).trim() : "";
        let meshPath = null;

        if (requestedMesh) {
          meshPath = resolveMeshPathAgnostic(requestedMesh);
        }

        if (!meshPath) {
          meshPath = findPhotogrammetryAssetByName(body.footprintName || "");
        }

        if (!meshPath) {
          throw new Error("No matching mesh found in Raw Data Inputs/ by selected footprint name.");
        }

        const footprintsCandidate = path.join(resolveProcessedDir(), "buildings", "footprints_clipped.geojson");
        const run = runBuildingAssetPipeline({
          meshPath,
          footprintId,
          featureIndex,
          footprintsPath: footprintsCandidate
        });

        const assetMeta = fs.existsSync(run.assetMetaPath)
          ? JSON.parse(fs.readFileSync(run.assetMetaPath, "utf8"))
          : null;

        sendJson(res, 200, {
          ok: true,
          footprintId,
          featureIndex,
          meshPath: path.relative(ROOT_DIR, meshPath),
          outDir: path.relative(ROOT_DIR, run.outDir),
          assetMetaPath: path.relative(ROOT_DIR, run.assetMetaPath),
          assetMeta,
          stdout: String(run.stdout || "").split(/\r?\n/).filter(Boolean).slice(-20)
        });
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/data/dem-grid.json") {
    getDemGrid()
      .then((grid) => {
        sendJson(res, 200, grid);
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/data/vegetation/shrubs-points.json") {
    getShrubsPoints()
      .then((points) => {
        sendJson(res, 200, points);
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/data/vegetation/shrub-assets.json") {
    getShrubAssetManifest()
      .then((manifest) => {
        sendJson(res, 200, manifest);
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/data/trees/tree-assets.json") {
    getTreeAssetManifest()
      .then((manifest) => {
        sendJson(res, 200, manifest);
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  if (pathname === "/data/buildings/assets-index.json") {
    try {
      const index = buildBuildingAssetsIndex();
      sendJson(res, 200, index);
    } catch (error) {
      sendJson(res, 500, { error: error.message });
    }
    return;
  }

  if (pathname === "/data/coords/wgs84.json") {
    const x = Number(requestUrl.searchParams.get("x"));
    const y = Number(requestUrl.searchParams.get("y"));
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      sendJson(res, 400, { error: "Invalid x/y query values. Expected numeric ?x=<value>&y=<value>." });
      return;
    }

    convertDemXYToWgs84(x, y)
      .then(({ lat, lon, sourceSrs }) => {
        sendJson(res, 200, {
          lat,
          lon,
          googleMaps: `${lat.toFixed(6)}, ${lon.toFixed(6)}`,
          sourceSrs
        });
      })
      .catch((error) => {
        sendJson(res, 500, { error: error.message });
      });
    return;
  }

  const targetFile = routeToFile(pathname);
  if (!targetFile) {
    sendJson(res, 404, { error: "Route not found" });
    return;
  }

  streamFile(req, res, targetFile);
});

const bootstrappedDataDirs = ensureProjectDataDirectories();

server.listen(PORT, HOST, () => {
  console.log(`Viewer server listening at http://${HOST}:${PORT}`);
  console.log("Open / to load the 3D DEM viewer.");
  console.log(`Raw data root: ${path.relative(ROOT_DIR, bootstrappedDataDirs.rawInputsDir)}`);
  console.log(`Processed data root: ${path.relative(ROOT_DIR, bootstrappedDataDirs.processedDir)}`);
});

server.on("error", (error) => {
  console.error(`Server failed to start: ${error.message}`);
  process.exit(1);
});
