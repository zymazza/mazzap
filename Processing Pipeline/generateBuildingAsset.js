#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const {
  ensureDir,
  resolveProcessedDir,
  requireCommand,
  loadJsonFile,
  writeJsonFile
} = require("./pipelineRunner");

const rootDir = path.resolve(__dirname, "..");
const SUPPORTED_MESH_EXTENSIONS = [".glb", ".gltf", ".obj", ".ply"];
const RAW_DATA_INPUTS_DIR_CANDIDATES = [
  path.join(rootDir, "Raw Data Inputs"),
  path.join(rootDir, "Raw_Data_Inputs")
];

function usageAndExit(message) {
  if (message) {
    console.error(message);
    console.error("");
  }
  console.error("Usage:");
  console.error("  node \"Processing Pipeline/generateBuildingAsset.js\" --mesh <path> [options]");
  console.error("");
  console.error("Options:");
  console.error(
    "  --mesh <path>            Raw photogrammetry mesh path or basename (.obj/.glb/.gltf/.ply). " +
    "Basenames are resolved against Raw Data Inputs/ (required)"
  );
  console.error("  --footprints <path>      Footprints GeoJSON (default: Processed Data/buildings/footprints_clipped.geojson)");
  console.error("  --footprint_id <id>      Footprint ID (required unless --feature_index is set)");
  console.error("  --feature_index <n>      Footprint feature index (alternative to --footprint_id)");
  console.error("  --origin <x,y>           Origin UTM (default from Processed Data/buildings/buildings_meta.json)");
  console.error("  --crs <EPSG:XXXX>        CRS label (default from buildings_meta.json)");
  console.error("  --dem <path>             Optional DEM path (default: Processed Data/dem.tif if present)");
  console.error("  --outdir <path>          Output directory (default: Processed Data/buildings/assets/<footprint_id>)");
  console.error("  --tex_size <int>         Bake atlas size (default 2048)");
  console.error(
    "  --texture_mode <mode>    Texture mode: preserve_multi_material|reuse_existing|bake_basecolor " +
    "(default preserve_multi_material)"
  );
  console.error("  --unwrap_angle <deg>     Smart UV angle limit degrees (default 66)");
  console.error("  --unwrap_margin <value>  Smart UV island margin (default 0.02)");
  console.error("  --export_tangents        Include tangents in GLB export (default off)");
  console.error("  --target_faces <int>     Decimation target face count (default 250000)");
  console.error("  --clip_mode <mode>       Clipping strategy: none|ground_outside (default none)");
  console.error("  --lods <int>             Number of LODs (default 3)");
  console.error("  --simplify <float>       LOD simplify ratio floor (default 0.35)");
  console.error("  --matrix <path>          4x4 transform matrix JSON path");
  console.error("  --translate <x,y,z>      Translation if no matrix (default 0,0,0)");
  console.error("  --rotate_deg <y,p,r>     Yaw,Pitch,Roll in degrees if no matrix (default 0,0,0)");
  console.error("  --scale <s>              Uniform scale if no matrix (default 1)");
  console.error("  --help                   Show help");
  process.exit(message ? 1 : 0);
}

function parseNumberList(raw, count, flagName) {
  const values = String(raw || "")
    .split(",")
    .map((item) => Number(item.trim()));
  if (values.length !== count || values.some((item) => !Number.isFinite(item))) {
    usageAndExit(`Invalid ${flagName}: ${raw}`);
  }
  return values;
}

function parsePositiveInt(raw, flagName) {
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    usageAndExit(`Invalid ${flagName}: ${raw}`);
  }
  return parsed;
}

function parsePositiveNumber(raw, flagName) {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    usageAndExit(`Invalid ${flagName}: ${raw}`);
  }
  return parsed;
}

function parseNonNegativeNumber(raw, flagName) {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0) {
    usageAndExit(`Invalid ${flagName}: ${raw}`);
  }
  return parsed;
}

function parseFlagValue(argv, index, inlineValue) {
  if (inlineValue !== undefined) {
    return { value: inlineValue, nextIndex: index };
  }
  const value = argv[index + 1];
  if (value === undefined || value.startsWith("--")) {
    usageAndExit(`Missing value for ${argv[index]}`);
  }
  return { value, nextIndex: index + 1 };
}

function parseArgs(argv) {
  const options = {
    mesh: null,
    footprints: null,
    footprintId: null,
    featureIndex: null,
    origin: null,
    crs: null,
    dem: null,
    outdir: null,
    texSize: 2048,
    textureMode: "preserve_multi_material",
    unwrapAngle: 66,
    unwrapMargin: 0.02,
    exportTangents: false,
    targetFaces: 250000,
    clipMode: "none",
    lods: 3,
    simplify: 0.35,
    matrix: null,
    translate: [0, 0, 0],
    rotateDeg: [0, 0, 0],
    scale: 1
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const eqPos = arg.indexOf("=");
    const hasInline = eqPos !== -1;
    const flag = hasInline ? arg.slice(0, eqPos) : arg;
    const inlineValue = hasInline ? arg.slice(eqPos + 1) : undefined;

    if (flag === "--help") {
      usageAndExit();
    } else if (flag === "--mesh") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.mesh = value;
      i = nextIndex;
    } else if (flag === "--footprints") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.footprints = value;
      i = nextIndex;
    } else if (flag === "--footprint_id") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.footprintId = value;
      i = nextIndex;
    } else if (flag === "--feature_index") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      const parsed = Number(value);
      if (!Number.isInteger(parsed) || parsed < 0) {
        usageAndExit(`Invalid --feature_index: ${value}`);
      }
      options.featureIndex = parsed;
      i = nextIndex;
    } else if (flag === "--origin") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.origin = parseNumberList(value, 2, "--origin");
      i = nextIndex;
    } else if (flag === "--crs") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.crs = value;
      i = nextIndex;
    } else if (flag === "--dem") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.dem = value;
      i = nextIndex;
    } else if (flag === "--outdir") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.outdir = value;
      i = nextIndex;
    } else if (flag === "--tex_size") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.texSize = parsePositiveInt(value, "--tex_size");
      i = nextIndex;
    } else if (flag === "--texture_mode") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      const normalized = String(value || "").trim().toLowerCase();
      if (
        normalized !== "preserve_multi_material" &&
        normalized !== "reuse_existing" &&
        normalized !== "bake_basecolor"
      ) {
        usageAndExit(
          `Invalid --texture_mode: ${value} ` +
          "(expected preserve_multi_material|reuse_existing|bake_basecolor)"
        );
      }
      options.textureMode = normalized;
      i = nextIndex;
    } else if (flag === "--unwrap_angle") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.unwrapAngle = parsePositiveNumber(value, "--unwrap_angle");
      i = nextIndex;
    } else if (flag === "--unwrap_margin") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.unwrapMargin = parseNonNegativeNumber(value, "--unwrap_margin");
      i = nextIndex;
    } else if (flag === "--export_tangents") {
      options.exportTangents = true;
    } else if (flag === "--target_faces") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.targetFaces = parsePositiveInt(value, "--target_faces");
      i = nextIndex;
    } else if (flag === "--clip_mode") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      const normalized = String(value || "").trim().toLowerCase();
      if (normalized !== "none" && normalized !== "ground_outside") {
        usageAndExit(`Invalid --clip_mode: ${value} (expected none|ground_outside)`);
      }
      options.clipMode = normalized;
      i = nextIndex;
    } else if (flag === "--lods") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.lods = parsePositiveInt(value, "--lods");
      i = nextIndex;
    } else if (flag === "--simplify") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.simplify = parsePositiveNumber(value, "--simplify");
      i = nextIndex;
    } else if (flag === "--matrix") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.matrix = value;
      i = nextIndex;
    } else if (flag === "--translate") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.translate = parseNumberList(value, 3, "--translate");
      i = nextIndex;
    } else if (flag === "--rotate_deg") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.rotateDeg = parseNumberList(value, 3, "--rotate_deg");
      i = nextIndex;
    } else if (flag === "--scale") {
      const { value, nextIndex } = parseFlagValue(argv, i, inlineValue);
      options.scale = parsePositiveNumber(value, "--scale");
      i = nextIndex;
    } else {
      usageAndExit(`Unknown option: ${arg}`);
    }
  }

  if (!options.mesh) {
    usageAndExit("Missing required --mesh argument.");
  }

  if (options.footprintId === null && options.featureIndex === null) {
    usageAndExit("Provide --footprint_id or --feature_index.");
  }

  return options;
}

function normalizePath(inputPath) {
  if (!inputPath) {
    return inputPath;
  }
  return path.isAbsolute(inputPath) ? inputPath : path.resolve(rootDir, inputPath);
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

function hasSupportedMeshExtension(filePath) {
  const ext = path.extname(String(filePath || "")).toLowerCase();
  return SUPPORTED_MESH_EXTENSIONS.includes(ext);
}

function listDirectoryCaseInsensitive(dirPath) {
  try {
    return fs.readdirSync(dirPath);
  } catch (error) {
    return [];
  }
}

function resolveExistingFileCaseInsensitive(candidatePath) {
  if (!candidatePath) {
    return null;
  }
  if (fs.existsSync(candidatePath) && fs.statSync(candidatePath).isFile()) {
    return candidatePath;
  }

  const dir = path.dirname(candidatePath);
  const base = path.basename(candidatePath).toLowerCase();
  const entries = listDirectoryCaseInsensitive(dir);
  const match = entries.find((name) => name.toLowerCase() === base);
  if (!match) {
    return null;
  }
  const resolved = path.join(dir, match);
  return fs.existsSync(resolved) && fs.statSync(resolved).isFile() ? resolved : null;
}

function resolveMeshInput(meshArgRaw) {
  const meshArg = String(meshArgRaw || "").trim();
  if (!meshArg) {
    return null;
  }

  const resolveFromBasePath = (basePath) => {
    const direct = resolveExistingFileCaseInsensitive(basePath);
    if (direct && hasSupportedMeshExtension(direct)) {
      return direct;
    }

    const ext = path.extname(basePath).toLowerCase();
    if (ext && !SUPPORTED_MESH_EXTENSIONS.includes(ext)) {
      return null;
    }

    const stem = ext ? basePath.slice(0, -ext.length) : basePath;
    for (const supportedExt of SUPPORTED_MESH_EXTENSIONS) {
      const candidate = resolveExistingFileCaseInsensitive(stem + supportedExt);
      if (candidate) {
        return candidate;
      }
    }
    return null;
  };

  const fromProvidedPath = resolveFromBasePath(normalizePath(meshArg));
  if (fromProvidedPath) {
    return fromProvidedPath;
  }

  if (!path.isAbsolute(meshArg)) {
    const rawInputsDir = resolveRawDataInputsDir(true);
    if (rawInputsDir) {
      const fromRawInputs = resolveFromBasePath(path.join(rawInputsDir, meshArg));
      if (fromRawInputs) {
        return fromRawInputs;
      }

      const meshBaseName = path.basename(meshArg);
      if (meshBaseName !== meshArg) {
        const fromRawInputsBaseName = resolveFromBasePath(path.join(rawInputsDir, meshBaseName));
        if (fromRawInputsBaseName) {
          return fromRawInputsBaseName;
        }
      }
    }
  }

  return null;
}

function commandExists(commandName) {
  const probe = spawnSync(commandName, ["--version"], { encoding: "utf8" });
  return !(probe.error && probe.error.code === "ENOENT");
}

function runCommand(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || rootDir,
    encoding: "utf8",
    stdio: options.stdio || "pipe",
    maxBuffer: options.maxBuffer || 256 * 1024 * 1024
  });

  if (result.error && (result.status === null || result.status === undefined)) {
    throw result.error;
  }

  if (result.status !== 0) {
    const details = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new Error(`Command failed: ${command} ${args.join(" ")}\n${details}`);
  }

  return result.stdout || "";
}

function safeRelative(targetPath) {
  return path.relative(rootDir, targetPath) || ".";
}

function slugify(value) {
  return String(value || "")
    .trim()
    .replace(/[^a-zA-Z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "") || "footprint";
}

function isLikelyWgs84FeatureCollection(geojson) {
  const features = Array.isArray(geojson && geojson.features) ? geojson.features : [];
  if (features.length === 0) {
    return false;
  }

  const stack = [features[0].geometry && features[0].geometry.coordinates];
  while (stack.length > 0) {
    const item = stack.pop();
    if (!Array.isArray(item)) {
      continue;
    }
    if (item.length >= 2 && Number.isFinite(Number(item[0])) && Number.isFinite(Number(item[1]))) {
      const x = Number(item[0]);
      const y = Number(item[1]);
      return Math.abs(x) <= 180 && Math.abs(y) <= 90;
    }
    for (let i = item.length - 1; i >= 0; i -= 1) {
      stack.push(item[i]);
    }
  }
  return false;
}

function featureCandidateIds(feature, index) {
  const props = feature && feature.properties ? feature.properties : {};
  const bases = [
    props.BuildingID,
    props.BUILDINGID,
    props.BLDG_ID,
    props.OBJECTID,
    props.ObjectID,
    props.FID,
    props.id,
    feature && feature.id
  ];

  const ids = [];
  for (const raw of bases) {
    if (raw === undefined || raw === null) {
      continue;
    }
    const text = String(raw).trim();
    if (!text) {
      continue;
    }
    ids.push(text);
    ids.push(`B-${text}`);
  }
  ids.push(`B-${index + 1}`);
  ids.push(String(index));
  ids.push(String(index + 1));
  return ids;
}

function resolveFeatureSelection(geojson, requestedFootprintId, requestedFeatureIndex) {
  const features = Array.isArray(geojson && geojson.features) ? geojson.features : [];
  if (features.length === 0) {
    throw new Error("Footprints GeoJSON has no features.");
  }

  if (Number.isInteger(requestedFeatureIndex)) {
    if (requestedFeatureIndex < 0 || requestedFeatureIndex >= features.length) {
      throw new Error(`feature_index ${requestedFeatureIndex} is out of range (0..${features.length - 1}).`);
    }
    const feature = features[requestedFeatureIndex];
    const fallbackId = `B-${requestedFeatureIndex + 1}`;
    const firstId = featureCandidateIds(feature, requestedFeatureIndex)[0] || fallbackId;
    return {
      featureIndex: requestedFeatureIndex,
      footprintId: requestedFootprintId || firstId,
      feature
    };
  }

  const lookup = String(requestedFootprintId || "").trim().toLowerCase();
  if (!lookup) {
    throw new Error("Footprint selection is empty.");
  }

  for (let i = 0; i < features.length; i += 1) {
    const feature = features[i];
    const ids = featureCandidateIds(feature, i);
    if (ids.some((id) => id.toLowerCase() === lookup)) {
      return {
        featureIndex: i,
        footprintId: requestedFootprintId,
        feature
      };
    }
  }

  const bMatch = lookup.match(/^b-(\d+)$/);
  if (bMatch) {
    const index = Number(bMatch[1]) - 1;
    if (Number.isInteger(index) && index >= 0 && index < features.length) {
      return {
        featureIndex: index,
        footprintId: requestedFootprintId,
        feature: features[index]
      };
    }
  }

  throw new Error(`Unable to resolve footprint_id '${requestedFootprintId}' to a feature.`);
}

function findFirstCoordinate(geometry) {
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

function computeFeatureCentroidUtm(feature) {
  const geometry = feature && feature.geometry;
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
      sumX += Number(item[0]);
      sumY += Number(item[1]);
      count += 1;
      continue;
    }
    for (let i = item.length - 1; i >= 0; i -= 1) {
      stack.push(item[i]);
    }
  }

  if (count <= 0) {
    return null;
  }
  return [sumX / count, sumY / count];
}

function copyFileSync(src, dst) {
  ensureDir(path.dirname(dst));
  fs.copyFileSync(src, dst);
}

function getFileSizeSafe(filePath) {
  try {
    return fs.statSync(filePath).size;
  } catch (error) {
    return null;
  }
}

function listTextureOutputs(textureDir) {
  const entries = fs.existsSync(textureDir) ? fs.readdirSync(textureDir, { withFileTypes: true }) : [];
  return entries
    .filter((entry) => entry.isFile())
    .map((entry) => {
      const abs = path.join(textureDir, entry.name);
      return {
        file: entry.name,
        size: getFileSizeSafe(abs)
      };
    })
    .sort((a, b) => a.file.localeCompare(b.file));
}

function accessorComponentCount(type) {
  if (type === "SCALAR") return 1;
  if (type === "VEC2") return 2;
  if (type === "VEC3") return 3;
  if (type === "VEC4") return 4;
  if (type === "MAT2") return 4;
  if (type === "MAT3") return 9;
  if (type === "MAT4") return 16;
  return 1;
}

function componentTypeByteSize(componentType) {
  if (componentType === 5120 || componentType === 5121) return 1;
  if (componentType === 5122 || componentType === 5123) return 2;
  if (componentType === 5125 || componentType === 5126) return 4;
  return 0;
}

function readComponentValue(dataView, byteOffset, componentType) {
  if (componentType === 5120) return dataView.getInt8(byteOffset);
  if (componentType === 5121) return dataView.getUint8(byteOffset);
  if (componentType === 5122) return dataView.getInt16(byteOffset, true);
  if (componentType === 5123) return dataView.getUint16(byteOffset, true);
  if (componentType === 5125) return dataView.getUint32(byteOffset, true);
  if (componentType === 5126) return dataView.getFloat32(byteOffset, true);
  return NaN;
}

function parseGlb(filePath) {
  const buffer = fs.readFileSync(filePath);
  if (buffer.length < 20) {
    throw new Error(`GLB too small: ${filePath}`);
  }

  const magic = buffer.readUInt32LE(0);
  if (magic !== 0x46546c67) {
    throw new Error(`Not a GLB file: ${filePath}`);
  }

  let offset = 12;
  let jsonChunk = null;
  let binChunk = null;
  while (offset + 8 <= buffer.length) {
    const chunkLength = buffer.readUInt32LE(offset);
    const chunkType = buffer.readUInt32LE(offset + 4);
    const start = offset + 8;
    const end = start + chunkLength;
    if (end > buffer.length) {
      break;
    }
    if (chunkType === 0x4E4F534A) {
      jsonChunk = buffer.slice(start, end);
    } else if (chunkType === 0x004e4942) {
      binChunk = buffer.slice(start, end);
    }
    offset = end;
  }

  if (!jsonChunk) {
    throw new Error(`Missing JSON chunk in GLB: ${filePath}`);
  }
  const jsonText = jsonChunk.toString("utf8").replace(/\u0000+$/g, "");
  const gltf = JSON.parse(jsonText);
  return { gltf, binChunk: binChunk || Buffer.alloc(0) };
}

function computeUvRangeFromAccessor(glb, accessorIndex) {
  const { gltf, binChunk } = glb;
  const accessors = Array.isArray(gltf.accessors) ? gltf.accessors : [];
  const bufferViews = Array.isArray(gltf.bufferViews) ? gltf.bufferViews : [];
  const accessor = accessors[accessorIndex];
  if (!accessor || accessor.bufferView === undefined || accessor.bufferView === null) {
    return null;
  }

  const bufferView = bufferViews[accessor.bufferView];
  if (!bufferView) {
    return null;
  }
  const componentCount = accessorComponentCount(accessor.type);
  const componentSize = componentTypeByteSize(accessor.componentType);
  if (!componentSize || componentCount < 2) {
    return null;
  }

  const stride = bufferView.byteStride || componentCount * componentSize;
  const baseOffset = (bufferView.byteOffset || 0) + (accessor.byteOffset || 0);
  const count = accessor.count || 0;
  const dataView = new DataView(
    binChunk.buffer,
    binChunk.byteOffset,
    binChunk.byteLength
  );

  let minU = Number.POSITIVE_INFINITY;
  let minV = Number.POSITIVE_INFINITY;
  let maxU = Number.NEGATIVE_INFINITY;
  let maxV = Number.NEGATIVE_INFINITY;

  for (let i = 0; i < count; i += 1) {
    const itemOffset = baseOffset + i * stride;
    if (itemOffset + componentSize * 2 > dataView.byteLength) {
      break;
    }
    const u = readComponentValue(dataView, itemOffset, accessor.componentType);
    const v = readComponentValue(dataView, itemOffset + componentSize, accessor.componentType);
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

function summarizeGlb(filePath) {
  const glb = parseGlb(filePath);
  const { gltf } = glb;

  const meshes = Array.isArray(gltf.meshes) ? gltf.meshes : [];
  const accessors = Array.isArray(gltf.accessors) ? gltf.accessors : [];

  let faceCount = 0;
  let hasUv = false;
  let uvRange = null;
  for (const mesh of meshes) {
    const primitives = Array.isArray(mesh.primitives) ? mesh.primitives : [];
    for (const primitive of primitives) {
      const attrs = primitive.attributes || {};
      if (attrs.TEXCOORD_0 !== undefined) {
        hasUv = true;
        const range = computeUvRangeFromAccessor(glb, attrs.TEXCOORD_0);
        if (range) {
          if (!uvRange) {
            uvRange = { ...range };
          } else {
            uvRange.minU = Math.min(uvRange.minU, range.minU);
            uvRange.maxU = Math.max(uvRange.maxU, range.maxU);
            uvRange.minV = Math.min(uvRange.minV, range.minV);
            uvRange.maxV = Math.max(uvRange.maxV, range.maxV);
            uvRange.spanU = uvRange.maxU - uvRange.minU;
            uvRange.spanV = uvRange.maxV - uvRange.minV;
          }
        }
      }

      if (primitive.indices !== undefined && accessors[primitive.indices]) {
        faceCount += Math.floor((accessors[primitive.indices].count || 0) / 3);
      } else if (attrs.POSITION !== undefined && accessors[attrs.POSITION]) {
        faceCount += Math.floor((accessors[attrs.POSITION].count || 0) / 3);
      }
    }
  }

  const materials = Array.isArray(gltf.materials) ? gltf.materials : [];
  const textures = Array.isArray(gltf.textures) ? gltf.textures : [];
  const images = Array.isArray(gltf.images) ? gltf.images : [];
  const baseColorTextureNames = [];
  for (const material of materials) {
    const texIndex = material?.pbrMetallicRoughness?.baseColorTexture?.index;
    if (texIndex === undefined || texIndex === null) {
      continue;
    }
    const tex = textures[texIndex];
    const sourceIndex = tex ? tex.source : null;
    const image = sourceIndex !== null && sourceIndex !== undefined ? images[sourceIndex] : null;
    const name = image?.name || image?.uri || `image_${sourceIndex}`;
    if (name) {
      baseColorTextureNames.push(String(name));
    }
  }

  return {
    faceCount,
    hasUv,
    uvRange,
    materialCount: materials.length,
    baseColorTextureNames: Array.from(new Set(baseColorTextureNames))
  };
}

function logGlbStage(stageName, filePath, options = {}) {
  const summary = summarizeGlb(filePath);
  const uv = summary.uvRange
    ? `u[${summary.uvRange.minU.toFixed(4)},${summary.uvRange.maxU.toFixed(4)}] ` +
      `v[${summary.uvRange.minV.toFixed(4)},${summary.uvRange.maxV.toFixed(4)}] ` +
      `span=(${summary.uvRange.spanU.toFixed(4)},${summary.uvRange.spanV.toFixed(4)})`
    : "none";
  const textures = summary.baseColorTextureNames.length > 0
    ? summary.baseColorTextureNames.join(", ")
    : "none";
  console.log(
    `[${stageName}] faces=${summary.faceCount} has_uv=${summary.hasUv} uv=${uv} ` +
    `materials=${summary.materialCount} baseColorTextures=${textures} ` +
    `atlas_generated=${Boolean(options.atlasGenerated)} ` +
    `uv_action='${options.uvAction || "none"}' texture_action='${options.textureAction || "none"}' ` +
    `output=${safeRelative(filePath)}`
  );
}

function normalizeNameSet(values) {
  return Array.from(new Set((values || []).map((v) => String(v).trim().toLowerCase()).filter(Boolean))).sort();
}

function isTextureSetEquivalent(a, b) {
  const na = normalizeNameSet(a);
  const nb = normalizeNameSet(b);
  if (na.length !== nb.length) {
    return false;
  }
  for (let i = 0; i < na.length; i += 1) {
    if (na[i] !== nb[i]) {
      return false;
    }
  }
  return true;
}

function buildBlenderArgs(config) {
  const args = [
    "-b",
    "--python-exit-code",
    "1",
    "-P",
    config.scriptPath,
    "--",
    "--input_mesh",
    config.meshPath,
    "--footprints",
    config.footprintsPath,
    "--feature_index",
    String(config.featureIndex),
    "--origin_utm",
    String(config.origin[0]),
    String(config.origin[1]),
    "--z_min",
    "-10",
    "--z_max",
    "200",
    "--clip_mode",
    config.clipMode || "none",
    "--target_faces",
    String(config.targetFaces),
    "--tex_size",
    String(config.texSize),
    "--texture_mode",
    String(config.textureMode || "preserve_multi_material"),
    "--unwrap_angle_limit",
    String(config.unwrapAngle),
    "--unwrap_island_margin",
    String(config.unwrapMargin),
    "--out_glb",
    config.lod0RawPath,
    "--out_raw_glb",
    config.rawBakedPath,
    "--out_textures_dir",
    config.texturesDir
  ];

  if (config.matrixPath) {
    args.push("--matrix", config.matrixPath);
  } else {
    args.push(
      "--translate",
      String(config.translate[0]),
      String(config.translate[1]),
      String(config.translate[2])
    );
    args.push(
      "--rotate_deg",
      String(config.rotateDeg[0]),
      String(config.rotateDeg[1]),
      String(config.rotateDeg[2])
    );
    args.push("--scale", String(config.scale));
  }

  if (config.demPath && fs.existsSync(config.demPath)) {
    args.push("--dem", config.demPath);
  }
  args.push("--ground_cut_mode", "none");
  if (config.exportTangents) {
    args.push("--export_tangents");
  }

  return args;
}

function maybeRunOptionalCommand(label, command, args) {
  try {
    runCommand(command, args, { stdio: "pipe" });
    console.log(`${label}: ok`);
    return true;
  } catch (error) {
    console.warn(`${label}: skipped (${error.message.split("\n")[0]})`);
    return false;
  }
}

function generateLods({ lod0InputPath, lodCount, simplifyFloor, outDir, textureMode }) {
  const lods = [];
  const hasGltfpack = commandExists("gltfpack");
  const hasGltfTransform = commandExists("gltf-transform");
  const preserveMultiMaterial = String(textureMode || "").toLowerCase() === "preserve_multi_material";

  const lod0Path = path.join(outDir, "lod0.glb");
  // Preserve LOD0 exactly as exported by Blender to avoid UV/material drift.
  copyFileSync(lod0InputPath, lod0Path);
  logGlbStage("lod0_stage_50_final", lod0Path, {
    uvAction: "reused UVs from blender output",
    textureAction: "reused baseColor texture bindings",
    atlasGenerated: false
  });

  lods.push({ level: 0, path: "lod0.glb" });
  const lod0Summary = summarizeGlb(lod0Path);

  const lod1StageSourcePath = path.join(outDir, "lod1_stage_00_source.glb");
  const lod1StageDecimatedPath = path.join(outDir, "lod1_stage_20_decimated.glb");
  const lod1StageFinalPath = path.join(outDir, "lod1_stage_50_final.glb");

  for (let i = 1; i < lodCount; i += 1) {
    const t = i / Math.max(1, lodCount - 1);
    const ratio = Math.max(0.03, 1 - t * (1 - simplifyFloor));
    const lodPath = path.join(outDir, `lod${i}.glb`);

    if (i === 1) {
      copyFileSync(lod0Path, lod1StageSourcePath);
      logGlbStage("lod1_stage_00_source", lod1StageSourcePath, {
        uvAction: "reused UVs from lod0",
        textureAction: "reused baseColor texture bindings",
        atlasGenerated: false
      });
    }

    let generated = false;
    let generationMode = "copy";
    let candidatePath = lodPath;
    if (preserveMultiMaterial) {
      candidatePath = path.join(outDir, `lod${i}.candidate.glb`);
      if (fs.existsSync(candidatePath)) {
        fs.rmSync(candidatePath, { force: true });
      }
    }
    if (hasGltfpack) {
      generated = maybeRunOptionalCommand(
        `gltfpack lod${i}`,
        "gltfpack",
        ["-i", lod0Path, "-o", candidatePath, "-si", ratio.toFixed(4), "-kn", "-km"]
      );
      generationMode = generated ? "gltfpack simplify" : "copy";
    } else if (hasGltfTransform) {
      generated = maybeRunOptionalCommand(
        `gltf-transform simplify lod${i}`,
        "gltf-transform",
        ["simplify", lod0Path, candidatePath, "--ratio", ratio.toFixed(4)]
      );
      generationMode = generated ? "gltf-transform simplify" : "copy";
    }

    let acceptedSimplified = false;
    if (generated && preserveMultiMaterial) {
      try {
        const candidateSummary = summarizeGlb(candidatePath);
        const uvOk = candidateSummary.hasUv;
        const materialCountOk = candidateSummary.materialCount === lod0Summary.materialCount;
        const textureSetOk = isTextureSetEquivalent(
          candidateSummary.baseColorTextureNames,
          lod0Summary.baseColorTextureNames
        );
        acceptedSimplified = uvOk && materialCountOk && textureSetOk;
        if (!acceptedSimplified) {
          console.warn(
            `lod${i}: rejected simplified candidate due to material/texture/UV mismatch ` +
            `(uvOk=${uvOk}, materialCountOk=${materialCountOk}, textureSetOk=${textureSetOk}); ` +
            "falling back to copy of lod0."
          );
        }
      } catch (error) {
        console.warn(`lod${i}: failed to inspect simplified candidate (${error.message}); falling back to copy.`);
      }
    } else if (generated) {
      acceptedSimplified = true;
    }

    if (acceptedSimplified && generated) {
      copyFileSync(candidatePath, lodPath);
    } else {
      copyFileSync(lod0Path, lodPath);
      generated = false;
      generationMode = preserveMultiMaterial ? "copy (preserve_multi_material fallback)" : "copy";
    }

    if (preserveMultiMaterial && fs.existsSync(candidatePath)) {
      fs.rmSync(candidatePath, { force: true });
    }

    if (i === 1) {
      copyFileSync(lodPath, lod1StageDecimatedPath);
      logGlbStage("lod1_stage_20_decimated", lod1StageDecimatedPath, {
        uvAction: generated ? "simplification attempted (UVs carried by tool)" : "no simplification; copied lod0 UVs",
        textureAction: generated
          ? `${generationMode}; texture bindings preserved by tool output`
          : "no simplification; copied lod0 texture bindings",
        atlasGenerated: false
      });
      copyFileSync(lodPath, lod1StageFinalPath);
      logGlbStage("lod1_stage_50_final", lod1StageFinalPath, {
        uvAction: generated ? "simplified UV state" : "copied UV state",
        textureAction: generated ? `${generationMode} output` : "copied output",
        atlasGenerated: false
      });
    }

    lods.push({ level: i, path: `lod${i}.glb` });
  }

  return lods;
}

function compressTexturesKtx2(textureDir) {
  const hasToktx = commandExists("toktx");
  if (!hasToktx || !fs.existsSync(textureDir)) {
    return [];
  }

  const entries = fs.readdirSync(textureDir, { withFileTypes: true });
  const outputs = [];

  for (const entry of entries) {
    if (!entry.isFile() || !/\.png$/i.test(entry.name)) {
      continue;
    }
    const src = path.join(textureDir, entry.name);
    const base = entry.name.replace(/\.png$/i, "");
    const dst = path.join(textureDir, `${base}.ktx2`);

    const ok = maybeRunOptionalCommand(
      `toktx ${entry.name}`,
      "toktx",
      ["--t2", "--genmipmap", "--bcmp", dst, src]
    );

    if (ok && fs.existsSync(dst)) {
      outputs.push({
        file: path.basename(dst),
        size: getFileSizeSafe(dst)
      });
    }
  }

  return outputs.sort((a, b) => a.file.localeCompare(b.file));
}

function main() {
  requireCommand("blender");

  const options = parseArgs(process.argv.slice(2));

  const processedDir = resolveProcessedDir(rootDir);
  const buildingsDir = path.join(processedDir, "buildings");

  const buildingsMetaPath = path.join(buildingsDir, "buildings_meta.json");
  if (!fs.existsSync(buildingsMetaPath)) {
    throw new Error(`Missing buildings_meta.json at ${buildingsMetaPath}. Run building footprint pipeline first.`);
  }
  const buildingsMeta = loadJsonFile(buildingsMetaPath);

  const defaultFootprintsPath = path.join(buildingsDir, "footprints_clipped.geojson");
  let footprintsPath = normalizePath(options.footprints || defaultFootprintsPath);

  if (!fs.existsSync(footprintsPath)) {
    throw new Error(`Footprints path not found: ${footprintsPath}`);
  }

  let footprintsGeoJson = loadJsonFile(footprintsPath);
  if (isLikelyWgs84FeatureCollection(footprintsGeoJson)) {
    const localCandidate = path.join(path.dirname(footprintsPath), "footprints_clipped_local.geojson");
    if (fs.existsSync(localCandidate)) {
      footprintsPath = localCandidate;
      footprintsGeoJson = loadJsonFile(footprintsPath);
    }
  }

  const selected = resolveFeatureSelection(footprintsGeoJson, options.footprintId, options.featureIndex);

  const meshPath = resolveMeshInput(options.mesh);
  if (!meshPath) {
    throw new Error(
      `Mesh input not found or unsupported: ${options.mesh}. ` +
      `Checked provided path and Raw Data Inputs/. ` +
      `Supported extensions: ${SUPPORTED_MESH_EXTENSIONS.join(", ")}`
    );
  }

  const origin = options.origin || [
    Number(buildingsMeta.origin_utm && buildingsMeta.origin_utm[0]),
    Number(buildingsMeta.origin_utm && buildingsMeta.origin_utm[1])
  ];
  if (!Number.isFinite(origin[0]) || !Number.isFinite(origin[1])) {
    throw new Error("Unable to resolve origin UTM. Provide --origin or regenerate buildings_meta.json.");
  }

  const crs = options.crs || String(buildingsMeta.crs || "unknown");

  let demPath = options.dem ? normalizePath(options.dem) : path.join(processedDir, "dem.tif");
  if (!fs.existsSync(demPath)) {
    demPath = null;
  }

  const footprintSlug = slugify(selected.footprintId || `feature_${selected.featureIndex}`);
  const outDir = ensureDir(
    options.outdir ? normalizePath(options.outdir) : path.join(buildingsDir, "assets", footprintSlug)
  );

  const texturesDir = ensureDir(path.join(outDir, "textures"));

  const rawBakedPath = path.join(outDir, "raw_baked.glb");
  const lod0RawPath = path.join(outDir, "lod0_raw.glb");
  const blenderScriptPath = path.join(rootDir, "Processing Pipeline", "tools", "bake_and_clip.py");
  if (!fs.existsSync(blenderScriptPath)) {
    throw new Error(`Blender script not found: ${blenderScriptPath}`);
  }

  const matrixPath = options.matrix ? normalizePath(options.matrix) : null;
  if (matrixPath && !fs.existsSync(matrixPath)) {
    throw new Error(`Matrix file not found: ${matrixPath}`);
  }

  console.log(`Footprint ID: ${selected.footprintId}`);
  console.log(`Feature index: ${selected.featureIndex}`);
  console.log(`Mesh: ${safeRelative(meshPath)}`);
  console.log(`Footprints: ${safeRelative(footprintsPath)}`);
  console.log(`Origin UTM: ${origin[0]}, ${origin[1]}`);
  console.log(`CRS: ${crs}`);
  console.log(`Output dir: ${safeRelative(outDir)}`);

  const blenderArgs = buildBlenderArgs({
    scriptPath: blenderScriptPath,
    meshPath,
    footprintsPath,
    featureIndex: selected.featureIndex,
    origin,
    clipMode: options.clipMode,
    targetFaces: options.targetFaces,
    texSize: options.texSize,
    textureMode: options.textureMode,
    unwrapAngle: options.unwrapAngle,
    unwrapMargin: options.unwrapMargin,
    exportTangents: options.exportTangents,
    lod0RawPath,
    rawBakedPath,
    texturesDir,
    demPath,
    matrixPath,
    translate: options.translate,
    rotateDeg: options.rotateDeg,
    scale: options.scale
  });

  runCommand("blender", blenderArgs, { stdio: "inherit", maxBuffer: 1024 * 1024 * 1024 });

  if (!fs.existsSync(lod0RawPath)) {
    throw new Error(`Expected Blender output not found: ${lod0RawPath}`);
  }

  const lods = generateLods({
    lod0InputPath: lod0RawPath,
    lodCount: options.lods,
    simplifyFloor: options.simplify,
    outDir,
    textureMode: options.textureMode
  });

  const ktx2Textures = compressTexturesKtx2(texturesDir);

  const lodThresholds = [1.0, 0.45, 0.2, 0.1, 0.05];
  const lodMeta = lods.map((lod, index) => {
    const abs = path.join(outDir, lod.path);
    return {
      level: lod.level,
      path: lod.path,
      size: getFileSizeSafe(abs),
      screenThreshold: lodThresholds[index] !== undefined ? lodThresholds[index] : 0.04
    };
  });

  const assetMeta = {
    footprint_id: selected.footprintId,
    feature_index: selected.featureIndex,
    footprint_centroid_utm: computeFeatureCentroidUtm(selected.feature),
    source_mesh: safeRelative(meshPath),
    footprints: safeRelative(footprintsPath),
    crs,
    origin_utm: [origin[0], origin[1], 0],
    transform: {
      matrix: matrixPath ? safeRelative(matrixPath) : null,
      translate: options.translate,
      rotate_deg: options.rotateDeg,
      scale: options.scale
    },
    processing: {
      clip_mode: options.clipMode,
      target_faces: options.targetFaces,
      tex_size: options.texSize,
      texture_mode: options.textureMode,
      unwrap_angle: options.unwrapAngle,
      unwrap_margin: options.unwrapMargin,
      export_tangents: options.exportTangents
    },
    placement: {
      coordinateFrame: "viewer_local",
      asset_up_axis: "y",
      axes: {
        x: "east",
        y: "south",
        z: "up"
      }
    },
    output: {
      directory: safeRelative(outDir),
      lods: lodMeta,
      textures: {
        png: listTextureOutputs(texturesDir),
        ktx2: ktx2Textures
      },
      raw_baked_glb: fs.existsSync(rawBakedPath) ? path.basename(rawBakedPath) : null
    }
  };

  const assetMetaPath = path.join(outDir, "asset_meta.json");
  writeJsonFile(assetMetaPath, assetMeta);

  console.log(`asset_meta.json: ${safeRelative(assetMetaPath)}`);
  for (const lod of lodMeta) {
    console.log(`LOD${lod.level}: ${lod.path} (${lod.size || 0} bytes)`);
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`\nBuilding asset generation failed: ${error.message}`);
    process.exit(1);
  }
}

module.exports = { main };
