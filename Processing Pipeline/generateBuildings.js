#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const {
  ensureDir,
  requireCommand,
  resolveLidarInput,
  resolveProcessedDir,
  runCommand,
  writeJsonFile
} = require("./pipelineRunner");

const rootDir = path.resolve(__dirname, "..");
const rawInputsDir = path.join(rootDir, "Raw Data Inputs");

function usageAndExit(message) {
  if (message) {
    console.error(message);
    console.error("");
  }
  console.error("Usage:");
  console.error("  node \"Processing Pipeline/generateBuildings.js\" [options]");
  console.error("");
  console.error("Options:");
  console.error("  --lidar <path>         LiDAR file (.laz/.las/.copc.laz) or directory of LiDAR tiles");
  console.error("                         (default: Raw Data Inputs/lidar_input.copc.laz else lidar_input.laz)");
  console.error("  --footprints <path>    Input FileGDB directory");
  console.error("                         (default: Raw Data Inputs/Footprints.gdb)");
  console.error("  --layer <name>         Optional footprint layer override (default: first polygon layer)");
  console.error("  --simplify <meters>    Optional topology-preserving simplify tolerance for GeoJSON");
  console.error("  --outdir <path>        Output directory (default: Processed Data/buildings)");
  console.error("  --help                 Show this help");
  process.exit(message ? 1 : 0);
}

function readFlagValue(argv, index, inlineValue) {
  if (inlineValue !== undefined) {
    return { value: inlineValue, nextIndex: index };
  }

  const value = argv[index + 1];
  if (value === undefined || value.startsWith("--")) {
    usageAndExit(`Missing value for ${argv[index]}`);
  }
  return { value, nextIndex: index + 1 };
}

function toPositiveNumber(raw, flagName) {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    usageAndExit(`Invalid ${flagName}: ${raw}`);
  }
  return parsed;
}

function parseArgs(argv) {
  const options = {
    lidar: null,
    footprints: path.join(rawInputsDir, "Footprints.gdb"),
    layer: null,
    simplify: null,
    outdir: null
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const eqPos = arg.indexOf("=");
    const hasInlineValue = eqPos !== -1;
    const flag = hasInlineValue ? arg.slice(0, eqPos) : arg;
    const inlineValue = hasInlineValue ? arg.slice(eqPos + 1) : undefined;

    if (flag === "--help") {
      usageAndExit();
    } else if (flag === "--lidar") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.lidar = value;
      i = nextIndex;
    } else if (flag === "--footprints") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.footprints = value;
      i = nextIndex;
    } else if (flag === "--layer") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.layer = value;
      i = nextIndex;
    } else if (flag === "--simplify") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.simplify = toPositiveNumber(value, "--simplify");
      i = nextIndex;
    } else if (flag === "--outdir") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.outdir = value;
      i = nextIndex;
    } else {
      usageAndExit(`Unknown option: ${arg}`);
    }
  }

  return options;
}

function normalizePath(inputPath) {
  if (!inputPath) {
    return inputPath;
  }
  return path.isAbsolute(inputPath) ? inputPath : path.resolve(rootDir, inputPath);
}

function listLidarFilesRecursively(dirPath) {
  const files = [];
  const stack = [dirPath];

  while (stack.length > 0) {
    const currentDir = stack.pop();
    const entries = fs.readdirSync(currentDir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(currentDir, entry.name);
      if (entry.isDirectory()) {
        stack.push(fullPath);
        continue;
      }
      if (/\.(las|laz)$/i.test(entry.name)) {
        files.push(fullPath);
      }
    }
  }

  files.sort((a, b) => a.localeCompare(b));
  return files;
}

function resolveLidarSource(options) {
  const lidarPath = options.lidar ? normalizePath(options.lidar) : resolveLidarInput(rootDir);
  if (!fs.existsSync(lidarPath)) {
    throw new Error(`LiDAR input path does not exist: ${lidarPath}`);
  }

  const stat = fs.statSync(lidarPath);
  if (stat.isDirectory()) {
    const files = listLidarFilesRecursively(lidarPath);
    if (files.length === 0) {
      throw new Error(`No .las/.laz files found in LiDAR directory: ${lidarPath}`);
    }
    return { mode: "tiles", inputPath: lidarPath, lidarFiles: files };
  }

  if (!/\.(las|laz)$/i.test(lidarPath)) {
    throw new Error(`LiDAR file must end with .las or .laz: ${lidarPath}`);
  }

  return { mode: "single", inputPath: lidarPath, lidarFiles: [lidarPath] };
}

function parseJsonSafe(raw, contextLabel) {
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`Unable to parse JSON (${contextLabel}): ${error.message}`);
  }
}

function getPdalMetadata(lidarFilePath) {
  const raw = runCommand("pdal", ["info", "--metadata", lidarFilePath], {
    cwd: rootDir,
    maxBuffer: 128 * 1024 * 1024
  });
  const parsed = parseJsonSafe(raw, "pdal info --metadata");
  return parsed.metadata || {};
}

function pickBoundsFromMetadata(metadata) {
  const minx = Number(metadata.minx);
  const miny = Number(metadata.miny);
  const maxx = Number(metadata.maxx);
  const maxy = Number(metadata.maxy);
  if ([minx, miny, maxx, maxy].every((value) => Number.isFinite(value))) {
    return { minx, miny, maxx, maxy };
  }

  const bbox =
    metadata.bounds &&
    metadata.bounds.native &&
    metadata.bounds.native.bbox;
  if (bbox && [bbox.minx, bbox.miny, bbox.maxx, bbox.maxy].every((value) => Number.isFinite(Number(value)))) {
    return {
      minx: Number(bbox.minx),
      miny: Number(bbox.miny),
      maxx: Number(bbox.maxx),
      maxy: Number(bbox.maxy)
    };
  }

  throw new Error("Unable to determine LiDAR bounds from PDAL metadata.");
}

function parseEpsgCodeFromText(text) {
  const raw = String(text || "");
  const patterns = [
    /EPSG\s*[:"]\s*(\d+)/gi,
    /"authority"\s*:\s*"EPSG"\s*,\s*"code"\s*:\s*(\d+)/gi,
    /ID\["EPSG"\s*,\s*(\d+)\]/gi
  ];
  const codes = [];
  for (const pattern of patterns) {
    let match = pattern.exec(raw);
    while (match) {
      const code = Number(match[1]);
      if (Number.isInteger(code)) {
        codes.push(code);
      }
      match = pattern.exec(raw);
    }
  }

  if (codes.length === 0) {
    return null;
  }

  const preferred = codes.find((code) => code >= 2000 && code <= 100000 && code !== 4326 && code !== 4269);
  return preferred || codes[0];
}

function detectLidarSrs(metadata, lidarFilePath) {
  const srs = metadata.srs || {};
  const srsJsonId = srs.json && srs.json.id ? srs.json.id : null;

  if (srsJsonId && String(srsJsonId.authority).toUpperCase() === "EPSG" && Number.isInteger(Number(srsJsonId.code))) {
    const code = Number(srsJsonId.code);
    return {
      srsForOgr: `EPSG:${code}`,
      crsLabel: `EPSG:${code}`,
      summary: srs.prettywkt || srs.wkt || srs.proj4 || ""
    };
  }

  if (srsJsonId && srsJsonId.authority && srsJsonId.code) {
    const authority = String(srsJsonId.authority).toUpperCase();
    const code = Number(srsJsonId.code);
    if (Number.isInteger(code) && authority !== "EPSG") {
      try {
        const projInfo = runCommand("projinfo", [`${authority}:${code}`], {
          cwd: rootDir,
          maxBuffer: 8 * 1024 * 1024
        });
        const epsgCode = parseEpsgCodeFromText(projInfo);
        if (epsgCode) {
          return {
            srsForOgr: `EPSG:${epsgCode}`,
            crsLabel: `EPSG:${epsgCode}`,
            summary: srs.prettywkt || srs.wkt || projInfo
          };
        }
      } catch (error) {
        // fall through
      }
    }
  }

  const textCandidates = [
    metadata.comp_spatialreference,
    metadata.spatialreference,
    srs.compoundwkt,
    srs.horizontal,
    srs.wkt
  ].filter(Boolean);

  for (const text of textCandidates) {
    const code = parseEpsgCodeFromText(text);
    if (code) {
      return {
        srsForOgr: `EPSG:${code}`,
        crsLabel: `EPSG:${code}`,
        summary: srs.prettywkt || srs.wkt || text
      };
    }
  }

  try {
    const epsgInfo = runCommand("gdalsrsinfo", ["-o", "epsg", lidarFilePath], {
      cwd: rootDir,
      maxBuffer: 8 * 1024 * 1024
    });
    const match = String(epsgInfo).match(/EPSG:(\d+)/i);
    if (match) {
      const code = Number(match[1]);
      return {
        srsForOgr: `EPSG:${code}`,
        crsLabel: `EPSG:${code}`,
        summary: srs.prettywkt || srs.wkt || srs.proj4 || ""
      };
    }
  } catch (error) {
    // fall through
  }

  if (srsJsonId && srsJsonId.authority && srsJsonId.code) {
    const fallback = `${String(srsJsonId.authority).toUpperCase()}:${srsJsonId.code}`;
    return {
      srsForOgr: fallback,
      crsLabel: fallback,
      summary: srs.prettywkt || srs.wkt || srs.proj4 || ""
    };
  }

  if (srs.wkt) {
    return {
      srsForOgr: srs.wkt,
      crsLabel: "WKT",
      summary: srs.prettywkt || srs.wkt
    };
  }

  if (srs.proj4) {
    return {
      srsForOgr: srs.proj4,
      crsLabel: "PROJ4",
      summary: srs.proj4
    };
  }

  throw new Error("Unable to determine LiDAR spatial reference from PDAL metadata.");
}

function getLayerExtent(vectorPath, layerName) {
  const info = runCommand("ogrinfo", ["-ro", "-al", "-so", vectorPath, layerName], {
    cwd: rootDir,
    maxBuffer: 32 * 1024 * 1024
  });

  const match = info.match(/Extent:\s*\(([-+\d\.eE]+),\s*([-+\d\.eE]+)\)\s*-\s*\(([-+\d\.eE]+),\s*([-+\d\.eE]+)\)/);
  if (!match) {
    throw new Error(`Unable to parse extent from ogrinfo output (${vectorPath}:${layerName}).`);
  }

  return {
    minx: Number(match[1]),
    miny: Number(match[2]),
    maxx: Number(match[3]),
    maxy: Number(match[4])
  };
}

function listFootprintLayers(gdbPath) {
  const info = runCommand("ogrinfo", ["-ro", "-so", gdbPath], {
    cwd: rootDir,
    maxBuffer: 16 * 1024 * 1024
  });

  const layers = [];
  for (const line of info.split(/\r?\n/)) {
    const match = line.match(/^Layer:\s*(.+?)\s*\((.+)\)\s*$/);
    if (!match) {
      continue;
    }
    layers.push({
      name: match[1].trim(),
      geometryType: match[2].trim()
    });
  }

  if (layers.length === 0) {
    throw new Error(`No layers discovered in FileGDB: ${gdbPath}`);
  }

  return layers;
}

function selectFootprintLayer(gdbPath, requestedLayer) {
  const layers = listFootprintLayers(gdbPath);

  if (requestedLayer) {
    const exact = layers.find((layer) => layer.name === requestedLayer);
    if (exact) {
      return exact;
    }
    const insensitive = layers.find(
      (layer) => layer.name.toLowerCase() === requestedLayer.toLowerCase()
    );
    if (insensitive) {
      return insensitive;
    }
    throw new Error(`Requested layer not found in ${gdbPath}: ${requestedLayer}`);
  }

  const polygonLayer = layers.find((layer) => /polygon/i.test(layer.geometryType));
  if (!polygonLayer) {
    throw new Error(`No polygon layers found in ${gdbPath}.`);
  }
  return polygonLayer;
}

function clipFootprintsWithPolygon({ footprintsReprojPath, clipPath, footprintsClippedPath, outDir }) {
  const joinWorkspacePath = path.join(outDir, "_clip_join_workspace.gpkg");

  runCommand(
    "ogr2ogr",
    [
      "-overwrite",
      "-f",
      "GPKG",
      joinWorkspacePath,
      footprintsReprojPath,
      "footprints_reproj",
      "-nln",
      "footprints_reproj",
      "-lco",
      "GEOMETRY_NAME=geom"
    ],
    { cwd: rootDir }
  );

  runCommand(
    "ogr2ogr",
    [
      "-update",
      "-append",
      joinWorkspacePath,
      clipPath,
      "clip_union",
      "-nln",
      "clip_union"
    ],
    { cwd: rootDir }
  );

  const sql = "SELECT DISTINCT f.* FROM footprints_reproj f JOIN clip_union c ON ST_Intersects(f.geom, c.geom)";
  runCommand(
    "ogr2ogr",
    [
      "-overwrite",
      "-f",
      "GPKG",
      footprintsClippedPath,
      joinWorkspacePath,
      "-dialect",
      "SQLITE",
      "-sql",
      sql,
      "-nln",
      "footprints_clipped",
      "-lco",
      "GEOMETRY_NAME=geom"
    ],
    { cwd: rootDir, maxBuffer: 64 * 1024 * 1024 }
  );

  fs.rmSync(joinWorkspacePath, { recursive: true, force: true });
}

function clipFootprintsWithBbox({ footprintsReprojPath, footprintsClippedPath, bounds }) {
  runCommand(
    "ogr2ogr",
    [
      "-overwrite",
      "-f",
      "GPKG",
      footprintsClippedPath,
      footprintsReprojPath,
      "footprints_reproj",
      "-spat",
      String(bounds.minx),
      String(bounds.miny),
      String(bounds.maxx),
      String(bounds.maxy),
      "-nln",
      "footprints_clipped",
      "-lco",
      "GEOMETRY_NAME=geom"
    ],
    { cwd: rootDir }
  );
}

function exportGeoJsonOutputs({ footprintsClippedPath, footprintsGeoJsonPath, footprintsLocalGeoJsonPath, simplify }) {
  fs.rmSync(footprintsGeoJsonPath, { force: true });
  fs.rmSync(footprintsLocalGeoJsonPath, { force: true });

  runCommand(
    "ogr2ogr",
    [
      "-f",
      "GeoJSON",
      "-lco",
      "RFC7946=YES",
      footprintsGeoJsonPath,
      footprintsClippedPath,
      "footprints_clipped"
    ],
    { cwd: rootDir, maxBuffer: 64 * 1024 * 1024 }
  );

  runCommand(
    "ogr2ogr",
    [
      "-overwrite",
      "-f",
      "GeoJSON",
      footprintsLocalGeoJsonPath,
      footprintsClippedPath,
      "footprints_clipped"
    ],
    { cwd: rootDir, maxBuffer: 64 * 1024 * 1024 }
  );

  if (!simplify) {
    return null;
  }

  const simplifiedPath = footprintsGeoJsonPath.replace(/\.geojson$/i, "_simplified.geojson");
  fs.rmSync(simplifiedPath, { force: true });
  const sql = `SELECT ST_SimplifyPreserveTopology(geom, ${Number(simplify)}) AS geom FROM footprints_clipped`;
  runCommand(
    "ogr2ogr",
    [
      "-f",
      "GeoJSON",
      "-lco",
      "RFC7946=YES",
      simplifiedPath,
      footprintsClippedPath,
      "-dialect",
      "SQLITE",
      "-sql",
      sql
    ],
    { cwd: rootDir, maxBuffer: 64 * 1024 * 1024 }
  );

  return simplifiedPath;
}

function main() {
  requireCommand("pdal");
  requireCommand("ogrinfo");
  requireCommand("ogr2ogr");
  requireCommand("gdalsrsinfo");
  requireCommand("projinfo");

  const options = parseArgs(process.argv.slice(2));
  const lidarSource = resolveLidarSource(options);
  const footprintsPath = normalizePath(options.footprints);
  if (!footprintsPath || !fs.existsSync(footprintsPath) || !fs.statSync(footprintsPath).isDirectory()) {
    throw new Error(`Footprints FileGDB directory not found: ${footprintsPath}`);
  }

  const outDir = options.outdir
    ? ensureDir(normalizePath(options.outdir))
    : ensureDir(path.join(resolveProcessedDir(rootDir), "buildings"));

  const lidarIndexPath = path.join(outDir, "lidar_index.gpkg");
  const clipPath = path.join(outDir, "clip.gpkg");
  const footprintsReprojPath = path.join(outDir, "footprints_reproj.gpkg");
  const footprintsClippedPath = path.join(outDir, "footprints_clipped.gpkg");
  const footprintsGeoJsonPath = path.join(outDir, "footprints_clipped.geojson");
  const footprintsLocalGeoJsonPath = path.join(outDir, "footprints_clipped_local.geojson");
  const buildingsMetaPath = path.join(outDir, "buildings_meta.json");

  const lidarMetadata = getPdalMetadata(lidarSource.lidarFiles[0]);
  const lidarSrs = detectLidarSrs(lidarMetadata, lidarSource.lidarFiles[0]);

  let bounds = null;
  if (lidarSource.mode === "tiles") {
    const filesList = `${lidarSource.lidarFiles.join("\n")}\n`;
    runCommand(
      "pdal",
      [
        "tindex",
        "create",
        "--tindex",
        lidarIndexPath,
        "--ogrdriver",
        "GPKG",
        "--lyr_name",
        "lidar_index",
        "--tindex_name",
        "location",
        "--t_srs",
        lidarSrs.srsForOgr,
        "--fast_boundary",
        "--write_absolute_path",
        "--stdin"
      ],
      {
        cwd: rootDir,
        input: filesList,
        maxBuffer: 128 * 1024 * 1024
      }
    );

    runCommand(
      "ogr2ogr",
      [
        "-overwrite",
        "-f",
        "GPKG",
        clipPath,
        lidarIndexPath,
        "-dialect",
        "SQLITE",
        "-sql",
        "SELECT ST_Union(geom) AS geom FROM lidar_index",
        "-nln",
        "clip_union",
        "-lco",
        "GEOMETRY_NAME=geom"
      ],
      { cwd: rootDir, maxBuffer: 64 * 1024 * 1024 }
    );

    bounds = getLayerExtent(clipPath, "clip_union");
  } else {
    bounds = pickBoundsFromMetadata(lidarMetadata);
  }

  const selectedLayer = selectFootprintLayer(footprintsPath, options.layer);

  runCommand(
    "ogr2ogr",
    [
      "-overwrite",
      "-f",
      "GPKG",
      footprintsReprojPath,
      footprintsPath,
      selectedLayer.name,
      "-nln",
      "footprints_reproj",
      "-lco",
      "GEOMETRY_NAME=geom",
      "-nlt",
      "PROMOTE_TO_MULTI",
      "-t_srs",
      lidarSrs.srsForOgr
    ],
    { cwd: rootDir, maxBuffer: 128 * 1024 * 1024 }
  );

  if (lidarSource.mode === "tiles") {
    clipFootprintsWithPolygon({
      footprintsReprojPath,
      clipPath,
      footprintsClippedPath,
      outDir
    });
  } else {
    clipFootprintsWithBbox({
      footprintsReprojPath,
      footprintsClippedPath,
      bounds
    });
  }

  const simplifiedPath = exportGeoJsonOutputs({
    footprintsClippedPath,
    footprintsGeoJsonPath,
    footprintsLocalGeoJsonPath,
    simplify: options.simplify
  });

  const meta = {
    crs: lidarSrs.crsLabel,
    origin_utm: [bounds.minx, bounds.miny, 0],
    bounds_utm: [bounds.minx, bounds.miny, bounds.maxx, bounds.maxy]
  };
  writeJsonFile(buildingsMetaPath, meta);

  const outputs = [
    footprintsReprojPath,
    footprintsClippedPath,
    footprintsGeoJsonPath,
    buildingsMetaPath
  ];
  if (lidarSource.mode === "tiles") {
    outputs.push(lidarIndexPath, clipPath);
  }
  if (simplifiedPath) {
    outputs.push(simplifiedPath);
  }

  for (const outputPath of outputs) {
    if (!fs.existsSync(outputPath)) {
      throw new Error(`Expected output not found: ${outputPath}`);
    }
  }

  console.log(`LiDAR mode: ${lidarSource.mode === "tiles" ? "directory/tiles" : "single file"}`);
  console.log(`LiDAR input: ${path.relative(rootDir, lidarSource.inputPath)}`);
  console.log(`LiDAR files indexed: ${lidarSource.lidarFiles.length}`);
  console.log(`Detected LiDAR CRS: ${lidarSrs.crsLabel}`);
  if (lidarSrs.summary) {
    console.log(`CRS summary: ${String(lidarSrs.summary).replace(/\s+/g, " ").slice(0, 220)}...`);
  }
  console.log(
    `Bounds: minx=${bounds.minx.toFixed(3)}, miny=${bounds.miny.toFixed(3)}, ` +
      `maxx=${bounds.maxx.toFixed(3)}, maxy=${bounds.maxy.toFixed(3)}`
  );
  console.log(`Footprints source: ${path.relative(rootDir, footprintsPath)}`);
  console.log(`Selected layer: ${selectedLayer.name} (${selectedLayer.geometryType})`);
  console.log(`Output directory: ${path.relative(rootDir, outDir)}`);
  if (lidarSource.mode === "tiles") {
    console.log(`LiDAR index: ${path.relative(rootDir, lidarIndexPath)}`);
    console.log(`Clip polygon: ${path.relative(rootDir, clipPath)}`);
  }
  console.log(`Reprojected footprints: ${path.relative(rootDir, footprintsReprojPath)}`);
  console.log(`Clipped footprints: ${path.relative(rootDir, footprintsClippedPath)}`);
  console.log(`Clipped GeoJSON (RFC7946): ${path.relative(rootDir, footprintsGeoJsonPath)}`);
  console.log(`Viewer GeoJSON (LiDAR CRS): ${path.relative(rootDir, footprintsLocalGeoJsonPath)}`);
  if (simplifiedPath) {
    console.log(`Simplified GeoJSON: ${path.relative(rootDir, simplifiedPath)}`);
  }
  console.log(`Buildings metadata: ${path.relative(rootDir, buildingsMetaPath)}`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`\nBuilding footprint generation failed: ${error.message}`);
    process.exit(1);
  }
}

module.exports = { main };
