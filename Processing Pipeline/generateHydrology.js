#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const {
  ensureDir,
  requireCommand,
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
  console.error("  node \"Processing Pipeline/generateHydrology.js\" [options]");
  console.error("");
  console.error("Options:");
  console.error("  --input <path>       Hydrology source .shp file or folder (default: Raw Data Inputs/Hydrology)");
  console.error("  --dem <path>         DEM path used for bounds/CRS (default: Processed Data/dem.tif)");
  console.error("  --outdir <path>      Output folder (default: Processed Data/hydrology)");
  console.error("  --help               Show this help");
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

function parseArgs(argv) {
  const options = {
    input: path.join(rawInputsDir, "Hydrology"),
    dem: null,
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
    } else if (flag === "--input") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.input = value;
      i = nextIndex;
    } else if (flag === "--dem") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.dem = value;
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

function safeRelative(targetPath) {
  const rel = path.relative(rootDir, targetPath);
  return rel.startsWith("..") ? targetPath : rel;
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
        continue;
      }
      if (entry.isFile() && /\.shp$/i.test(entry.name)) {
        out.push(fullPath);
      }
    }
  }

  out.sort((a, b) => a.localeCompare(b));
  return out;
}

function resolveHydrologyShapefiles(inputPathRaw) {
  const inputPath = normalizePath(inputPathRaw);
  if (!inputPath || !fs.existsSync(inputPath)) {
    throw new Error(`Hydrology input path not found: ${inputPath}`);
  }

  const stat = fs.statSync(inputPath);
  if (stat.isFile()) {
    if (!/\.shp$/i.test(path.basename(inputPath))) {
      throw new Error(`Hydrology input file must be a .shp: ${inputPath}`);
    }
    return [inputPath];
  }

  const shapefiles = findShapefilesRecursively(inputPath);
  if (shapefiles.length === 0) {
    throw new Error(`No .shp files found in hydrology input directory: ${inputPath}`);
  }
  return shapefiles;
}

function parseEpsgCodesFromText(text) {
  const raw = String(text || "");
  const patterns = [
    /EPSG\s*[:"]\s*(\d+)/gi,
    /"authority"\s*:\s*"EPSG"\s*,\s*"code"\s*:\s*(\d+)/gi,
    /ID\["EPSG"\s*,\s*(\d+)\]/gi,
    /AUTHORITY\["EPSG"\s*,\s*"?(\d+)"?\]/gi
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

  return Array.from(new Set(codes));
}

function resolveDemCrs(demPath, wktText) {
  try {
    const raw = runCommand("gdalsrsinfo", ["-o", "epsg", demPath], {
      cwd: rootDir,
      maxBuffer: 32 * 1024 * 1024
    });
    const match = /EPSG\s*:\s*(\d+)/i.exec(String(raw || ""));
    if (match) {
      const code = Number(match[1]);
      if (Number.isInteger(code) && code > 0) {
        return `EPSG:${code}`;
      }
    }
  } catch (error) {
    // Fall back to WKT parsing below.
  }

  const candidates = parseEpsgCodesFromText(wktText);
  const excluded = new Set([9001, 9122, 8901, 7030]);
  const preferred = candidates.find((code) => Number.isInteger(code) && code > 1024 && !excluded.has(code));
  if (preferred) {
    return `EPSG:${preferred}`;
  }

  const fallback = candidates.find((code) => Number.isInteger(code) && code > 0);
  if (fallback) {
    return `EPSG:${fallback}`;
  }

  throw new Error("Unable to determine DEM EPSG code from GDAL metadata.");
}

function getDemClipInfo(demPath) {
  const raw = runCommand("gdalinfo", ["-json", demPath], {
    cwd: rootDir,
    maxBuffer: 128 * 1024 * 1024
  });
  const parsed = JSON.parse(raw);

  const corners = parsed && parsed.cornerCoordinates ? parsed.cornerCoordinates : null;
  if (!corners) {
    throw new Error("Unable to read DEM corner coordinates from gdalinfo.");
  }

  const cornerValues = [
    corners.upperLeft,
    corners.upperRight,
    corners.lowerRight,
    corners.lowerLeft
  ]
    .filter((value) => Array.isArray(value) && value.length >= 2)
    .map((value) => [Number(value[0]), Number(value[1])])
    .filter((value) => Number.isFinite(value[0]) && Number.isFinite(value[1]));

  if (cornerValues.length < 4) {
    throw new Error("DEM corner coordinates are invalid.");
  }

  const xs = cornerValues.map((pair) => pair[0]);
  const ys = cornerValues.map((pair) => pair[1]);
  const bounds = {
    minx: Math.min(...xs),
    miny: Math.min(...ys),
    maxx: Math.max(...xs),
    maxy: Math.max(...ys)
  };

  const coordinateSystem = parsed.coordinateSystem || {};
  const wkt = String(coordinateSystem.wkt || coordinateSystem.wkt2 || "");
  const crs = resolveDemCrs(demPath, wkt);

  return {
    bounds,
    crs
  };
}

function reprojectHydrologySources({ shapefiles, outMergedPath, layerName, targetCrs }) {
  for (let index = 0; index < shapefiles.length; index += 1) {
    const shapefile = shapefiles[index];
    const args = [
      "--config",
      "SHAPE_RESTORE_SHX",
      "YES",
      ...(index === 0 ? ["-overwrite"] : ["-update", "-append"]),
      "-skipfailures",
      "-nlt",
      "PROMOTE_TO_MULTI",
      "-nln",
      layerName,
      "-t_srs",
      targetCrs,
      outMergedPath,
      shapefile
    ];

    runCommand("ogr2ogr", args, {
      cwd: rootDir,
      maxBuffer: 256 * 1024 * 1024
    });
  }
}

function clipReprojectedHydrology({ mergedPath, mergedLayerName, outGpkgPath, outLayerName, bounds }) {
  runCommand("ogr2ogr", [
    "-overwrite",
    "-skipfailures",
    "-nlt",
    "PROMOTE_TO_MULTI",
    "-nln",
    outLayerName,
    "-clipsrc",
    String(bounds.minx),
    String(bounds.miny),
    String(bounds.maxx),
    String(bounds.maxy),
    outGpkgPath,
    mergedPath,
    mergedLayerName
  ], {
    cwd: rootDir,
    maxBuffer: 256 * 1024 * 1024
  });
}

function main() {
  requireCommand("gdalinfo");
  requireCommand("ogr2ogr");

  const options = parseArgs(process.argv.slice(2));
  const processedDir = resolveProcessedDir(rootDir);
  const demPath = normalizePath(options.dem || path.join(processedDir, "dem.tif"));
  if (!demPath || !fs.existsSync(demPath)) {
    throw new Error(`DEM not found: ${demPath}`);
  }

  const shapefiles = resolveHydrologyShapefiles(options.input);
  const clipInfo = getDemClipInfo(demPath);

  const outDir = ensureDir(normalizePath(options.outdir || path.join(processedDir, "hydrology")));
  const mergedPath = path.join(outDir, "hydrology_reprojected.gpkg");
  const mergedLayerName = "hydrology_reprojected";
  const outGpkgPath = path.join(outDir, "hydrology_clipped.gpkg");
  const outGeoJsonPath = path.join(outDir, "hydrology_clipped.geojson");
  const outLocalGeoJsonPath = path.join(outDir, "hydrology_clipped_local.geojson");
  const layerName = "hydrology_clipped";

  fs.rmSync(mergedPath, { force: true });
  fs.rmSync(outGpkgPath, { force: true });
  fs.rmSync(outGeoJsonPath, { force: true });
  fs.rmSync(outLocalGeoJsonPath, { force: true });

  reprojectHydrologySources({
    shapefiles,
    outMergedPath: mergedPath,
    layerName: mergedLayerName,
    targetCrs: clipInfo.crs
  });

  clipReprojectedHydrology({
    mergedPath,
    mergedLayerName,
    outGpkgPath,
    outLayerName: layerName,
    bounds: clipInfo.bounds
  });

  runCommand("ogr2ogr", [
    "-overwrite",
    "-f",
    "GeoJSON",
    "-lco",
    "RFC7946=YES",
    outGeoJsonPath,
    outGpkgPath,
    layerName
  ], {
    cwd: rootDir,
    maxBuffer: 256 * 1024 * 1024
  });

  runCommand("ogr2ogr", [
    "-overwrite",
    "-f",
    "GeoJSON",
    "-lco",
    "RFC7946=NO",
    outLocalGeoJsonPath,
    outGpkgPath,
    layerName
  ], {
    cwd: rootDir,
    maxBuffer: 256 * 1024 * 1024
  });

  const meta = {
    generated_at: new Date().toISOString(),
    source_files: shapefiles.map((filePath) => safeRelative(filePath)),
    dem: safeRelative(demPath),
    crs: clipInfo.crs,
    clip_bounds: clipInfo.bounds,
    output: {
      directory: safeRelative(outDir),
      gpkg: path.basename(outGpkgPath),
      geojson: path.basename(outGeoJsonPath),
      local_geojson: path.basename(outLocalGeoJsonPath)
    }
  };

  writeJsonFile(path.join(outDir, "hydrology_meta.json"), meta);

  console.log(`Hydrology source files: ${shapefiles.length}`);
  console.log(`DEM: ${safeRelative(demPath)}`);
  console.log(`CRS: ${clipInfo.crs}`);
  console.log(`Clip bounds: ${clipInfo.bounds.minx}, ${clipInfo.bounds.miny}, ${clipInfo.bounds.maxx}, ${clipInfo.bounds.maxy}`);
  console.log(`Hydrology output: ${safeRelative(outDir)}`);
}

try {
  main();
} catch (error) {
  console.error(`\nHydrology generation failed: ${error.message}`);
  process.exit(1);
}
