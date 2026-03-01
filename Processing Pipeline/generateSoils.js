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

const HYDRO_GROUP_COLORS = {
  A: "#3c9d5a",
  "A/D": "#4ca56f",
  B: "#6ea24f",
  "B/D": "#84a84f",
  C: "#c39b43",
  "C/D": "#ba8740",
  D: "#aa5e3a"
};

function usageAndExit(message) {
  if (message) {
    console.error(message);
    console.error("");
  }
  console.error("Usage:");
  console.error("  node \"Processing Pipeline/generateSoils.js\" [options]");
  console.error("");
  console.error("Options:");
  console.error("  --input <path>       SSURGO export root folder (default: Raw Data Inputs/SSURGO)");
  console.error("  --dem <path>         DEM path used for bounds/CRS (default: Processed Data/dem.tif)");
  console.error("  --outdir <path>      Output folder (default: Processed Data/soils)");
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
    input: path.join(rawInputsDir, "SSURGO"),
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

function parseCsvLine(line) {
  const out = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (ch === "," && !inQuotes) {
      out.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  out.push(current);
  return out.map((value) => String(value || "").trim());
}

function parseCsvWithHeader(csvText) {
  const lines = String(csvText || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length < 2) {
    return [];
  }

  const header = parseCsvLine(lines[0]).map((name) => name.toLowerCase());
  const rows = [];
  for (let i = 1; i < lines.length; i += 1) {
    const cols = parseCsvLine(lines[i]);
    const row = {};
    for (let c = 0; c < header.length; c += 1) {
      row[header[c]] = cols[c] !== undefined ? cols[c] : "";
    }
    rows.push(row);
  }
  return rows;
}

function normalizeDelimitedValue(value) {
  const raw = String(value === undefined || value === null ? "" : value).trim();
  if (raw.length >= 2 && raw.startsWith('"') && raw.endsWith('"')) {
    return raw.slice(1, -1).replace(/""/g, '"').trim();
  }
  return raw;
}

function parseDelimitedFile(filePath) {
  const raw = fs.readFileSync(filePath, "utf8");
  const lines = raw.split(/\r?\n/).filter((line) => line.trim().length > 0);
  if (lines.length === 0) {
    return { delimiter: "|", rows: [] };
  }

  const first = lines[0];
  const delimiter = first.includes("|") ? "|" : first.includes("\t") ? "\t" : ",";
  const rows = lines.map((line) => line.split(delimiter).map((value) => normalizeDelimitedValue(value)));
  return { delimiter, rows };
}

function locateSsurgoRoot(inputPathRaw) {
  const inputPath = normalizePath(inputPathRaw);
  if (!inputPath || !fs.existsSync(inputPath) || !fs.statSync(inputPath).isDirectory()) {
    throw new Error(`SSURGO input directory not found: ${inputPath}`);
  }

  const hasSpatial = fs.existsSync(path.join(inputPath, "spatial"));
  const hasTabular = fs.existsSync(path.join(inputPath, "tabular"));
  if (hasSpatial) {
    return inputPath;
  }

  const entries = fs.readdirSync(inputPath, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue;
    }
    const candidate = path.join(inputPath, entry.name);
    if (fs.existsSync(path.join(candidate, "spatial")) && (fs.existsSync(path.join(candidate, "tabular")) || hasTabular)) {
      return candidate;
    }
  }

  throw new Error(`Could not locate SSURGO root containing a spatial/ directory under: ${inputPath}`);
}

function findFilesRecursive(baseDir, predicate) {
  if (!baseDir || !fs.existsSync(baseDir) || !fs.statSync(baseDir).isDirectory()) {
    return [];
  }

  const out = [];
  const stack = [baseDir];
  while (stack.length > 0) {
    const current = stack.pop();
    const entries = fs.readdirSync(current, { withFileTypes: true });
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
      } else if (entry.isFile() && predicate(entry.name, full)) {
        out.push(full);
      }
    }
  }
  out.sort((a, b) => a.localeCompare(b));
  return out;
}

function findMapUnitShapefile(spatialDir) {
  const shapefiles = findFilesRecursive(spatialDir, (name) => /\.shp$/i.test(name));
  if (shapefiles.length === 0) {
    throw new Error(`No shapefiles found under spatial directory: ${spatialDir}`);
  }

  const ranked = shapefiles
    .map((filePath) => {
      const base = path.basename(filePath).toLowerCase();
      let score = 0;
      if (base.includes("soilmu_a")) score += 100;
      if (base.includes("smu_a")) score += 90;
      if (base.includes("mu_a")) score += 60;
      if (base.includes("soil") && base.includes("mu")) score += 40;
      return { filePath, score };
    })
    .sort((a, b) => b.score - a.score || a.filePath.localeCompare(b.filePath));

  return ranked[0].filePath;
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

function reprojectSoilMapUnits({ mapUnitShapefile, outMergedPath, layerName, targetCrs }) {
  runCommand("ogr2ogr", [
    "--config",
    "SHAPE_RESTORE_SHX",
    "YES",
    "-overwrite",
    "-skipfailures",
    "-nlt",
    "PROMOTE_TO_MULTI",
    "-nln",
    layerName,
    "-t_srs",
    targetCrs,
    outMergedPath,
    mapUnitShapefile
  ], {
    cwd: rootDir,
    maxBuffer: 256 * 1024 * 1024
  });
}

function clipReprojectedSoils({ mergedPath, mergedLayerName, outGpkgPath, outLayerName, bounds }) {
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

function exportGeoJsonOutputs({ outGpkgPath, layerName, outGeoJsonPath, outLocalGeoJsonPath }) {
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
}

function findMdbPath(ssurgoRoot) {
  const mdbFiles = findFilesRecursive(ssurgoRoot, (name) => /\.mdb$/i.test(name));
  if (mdbFiles.length === 0) {
    return null;
  }
  const preferred = mdbFiles.find((filePath) => /soildb_.*\.mdb$/i.test(path.basename(filePath)));
  return preferred || mdbFiles[0];
}

function exportMdbLayerCsv(mdbPath, layerName, outputCsvPath, selectFields) {
  const args = [
    "-overwrite",
    "-f",
    "CSV",
    ...(selectFields && selectFields.length > 0 ? ["-select", selectFields.join(",")] : []),
    outputCsvPath,
    mdbPath,
    layerName
  ];

  runCommand("ogr2ogr", args, {
    cwd: rootDir,
    maxBuffer: 128 * 1024 * 1024
  });
}

function readMdbLookup(ssurgoRoot, outDir) {
  const mdbPath = findMdbPath(ssurgoRoot);
  if (!mdbPath) {
    return { mapunit: new Map(), muaggatt: new Map(), mutext: new Map(), source: null };
  }

  const tmpDir = ensureDir(path.join(outDir, "_tmp_soils_lookup"));
  const mapunitCsv = path.join(tmpDir, "mapunit.csv");
  const muaggattCsv = path.join(tmpDir, "muaggatt.csv");
  const mutextCsv = path.join(tmpDir, "mutext.csv");
  let mapRows = [];
  let aggRows = [];
  let textRows = [];

  try {
    exportMdbLayerCsv(mdbPath, "mapunit", mapunitCsv, ["mukey", "musym", "muname"]);
    mapRows = parseCsvWithHeader(fs.readFileSync(mapunitCsv, "utf8"));
  } catch (error) {
    mapRows = [];
  }

  try {
    exportMdbLayerCsv(mdbPath, "muaggatt", muaggattCsv, ["mukey", "hydgrpdcd", "drclassdcd", "flodfreqdcd"]);
    aggRows = parseCsvWithHeader(fs.readFileSync(muaggattCsv, "utf8"));
  } catch (error) {
    aggRows = [];
  }

  try {
    exportMdbLayerCsv(mdbPath, "mutext", mutextCsv, ["mukey", "mutext"]);
    textRows = parseCsvWithHeader(fs.readFileSync(mutextCsv, "utf8"));
  } catch (error) {
    textRows = [];
  }

  fs.rmSync(tmpDir, { recursive: true, force: true });

  const mapunit = new Map();
  for (const row of mapRows) {
    const mukey = String(row.mukey || "").trim();
    if (!mukey) continue;
    mapunit.set(mukey, {
      musym: String(row.musym || "").trim() || null,
      muname: String(row.muname || "").trim() || null
    });
  }

  const muaggatt = new Map();
  for (const row of aggRows) {
    const mukey = String(row.mukey || "").trim();
    if (!mukey) continue;
    muaggatt.set(mukey, {
      hydgrpdcd: String(row.hydgrpdcd || "").trim() || null,
      drclassdcd: String(row.drclassdcd || "").trim() || null,
      flodfreqdcd: String(row.flodfreqdcd || "").trim() || null
    });
  }

  const mutext = new Map();
  for (const row of textRows) {
    const mukey = String(row.mukey || "").trim();
    if (!mukey) continue;
    const text = String(row.mutext || "").trim();
    if (!text) continue;
    mutext.set(mukey, text);
  }

  return {
    mapunit,
    muaggatt,
    mutext,
    source: safeRelative(mdbPath)
  };
}

function findTabularFile(tabularDir, baseName) {
  if (!tabularDir || !fs.existsSync(tabularDir)) {
    return null;
  }
  const candidates = [
    path.join(tabularDir, `${baseName}.txt`),
    path.join(tabularDir, `${baseName}.csv`),
    path.join(tabularDir, `${baseName.toUpperCase()}.txt`),
    path.join(tabularDir, `${baseName.toUpperCase()}.csv`)
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      return candidate;
    }
  }
  return null;
}

function readTabularLookup(ssurgoRoot) {
  const tabularDir = path.join(ssurgoRoot, "tabular");
  const mapunitPath = findTabularFile(tabularDir, "mapunit");
  const muaggattPath = findTabularFile(tabularDir, "muaggatt");
  const mutextPath = findTabularFile(tabularDir, "mutext");

  const mapunit = new Map();
  const muaggatt = new Map();
  const mutext = new Map();

  if (mapunitPath) {
    const parsed = parseDelimitedFile(mapunitPath);
    const rows = parsed.rows;
    const hasHeader = rows.length > 0 && rows[0].some((cell) => String(cell).toLowerCase() === "mukey");

    if (hasHeader) {
      const headers = rows[0].map((cell) => String(cell).toLowerCase());
      const index = {
        mukey: headers.indexOf("mukey"),
        musym: headers.indexOf("musym"),
        muname: headers.indexOf("muname")
      };
      for (let i = 1; i < rows.length; i += 1) {
        const row = rows[i];
        const mukey = index.mukey >= 0 ? String(row[index.mukey] || "").trim() : "";
        if (!mukey) continue;
        mapunit.set(mukey, {
          musym: index.musym >= 0 ? String(row[index.musym] || "").trim() || null : null,
          muname: index.muname >= 0 ? String(row[index.muname] || "").trim() || null : null
        });
      }
    } else {
      for (const row of rows) {
        if (!Array.isArray(row) || row.length < 4) continue;
        const mukey = String(row[row.length - 1] || "").trim();
        if (!/^\d+$/.test(mukey)) continue;
        mapunit.set(mukey, {
          musym: String(row[0] || "").trim() || null,
          muname: String(row[1] || "").trim() || null
        });
      }
    }
  }

  if (muaggattPath) {
    const parsed = parseDelimitedFile(muaggattPath);
    const rows = parsed.rows;
    const hasHeader = rows.length > 0 && rows[0].some((cell) => String(cell).toLowerCase() === "mukey");

    if (hasHeader) {
      const headers = rows[0].map((cell) => String(cell).toLowerCase());
      const index = {
        mukey: headers.indexOf("mukey"),
        hydgrpdcd: headers.indexOf("hydgrpdcd"),
        drclassdcd: headers.indexOf("drclassdcd"),
        flodfreqdcd: headers.indexOf("flodfreqdcd")
      };
      for (let i = 1; i < rows.length; i += 1) {
        const row = rows[i];
        const mukey = index.mukey >= 0 ? String(row[index.mukey] || "").trim() : "";
        if (!mukey) continue;
        muaggatt.set(mukey, {
          hydgrpdcd: index.hydgrpdcd >= 0 ? String(row[index.hydgrpdcd] || "").trim() || null : null,
          drclassdcd: index.drclassdcd >= 0 ? String(row[index.drclassdcd] || "").trim() || null : null,
          flodfreqdcd: index.flodfreqdcd >= 0 ? String(row[index.flodfreqdcd] || "").trim() || null : null
        });
      }
    } else {
      const drainageHints = new Set([
        "excessively drained",
        "somewhat excessively drained",
        "well drained",
        "moderately well drained",
        "somewhat poorly drained",
        "poorly drained",
        "very poorly drained"
      ]);

      for (const row of rows) {
        if (!Array.isArray(row) || row.length < 1) continue;
        const mukey = String(row[row.length - 1] || "").trim();
        if (!/^\d+$/.test(mukey)) continue;

        let hydgrpdcd = null;
        let drclassdcd = null;
        let flodfreqdcd = null;
        for (const cellRaw of row) {
          const cell = String(cellRaw || "").trim();
          if (!cell) continue;
          const upper = cell.toUpperCase();
          const lower = cell.toLowerCase();
          if (!hydgrpdcd && /^(A|B|C|D)(\/[A-D])?$/.test(upper)) {
            hydgrpdcd = upper;
            continue;
          }
          if (!drclassdcd && drainageHints.has(lower)) {
            drclassdcd = cell;
            continue;
          }
          if (!flodfreqdcd && lower.includes("flood")) {
            flodfreqdcd = cell;
          }
        }
        muaggatt.set(mukey, { hydgrpdcd, drclassdcd, flodfreqdcd });
      }
    }
  }

  if (mutextPath) {
    const parsed = parseDelimitedFile(mutextPath);
    const rows = parsed.rows;
    const hasHeader = rows.length > 0 && rows[0].some((cell) => String(cell).toLowerCase() === "mukey");

    if (hasHeader) {
      const headers = rows[0].map((cell) => String(cell).toLowerCase());
      const index = {
        mukey: headers.indexOf("mukey"),
        mutext: headers.indexOf("mutext")
      };
      for (let i = 1; i < rows.length; i += 1) {
        const row = rows[i];
        const mukey = index.mukey >= 0 ? String(row[index.mukey] || "").trim() : "";
        const text = index.mutext >= 0 ? String(row[index.mutext] || "").trim() : "";
        if (!mukey || !text) continue;
        mutext.set(mukey, text);
      }
    } else {
      for (const row of rows) {
        if (!Array.isArray(row) || row.length < 2) continue;
        const mukey = String(row[row.length - 1] || "").trim();
        if (!/^\d+$/.test(mukey)) continue;
        const text = String(row[1] || "").trim();
        if (!text) continue;
        mutext.set(mukey, text);
      }
    }
  }

  return {
    mapunit,
    muaggatt,
    mutext,
    source: tabularDir
  };
}

function hashToColor(input) {
  const text = String(input || "unknown");
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 56%, 56%)`;
}

function pickSoilClassAndColor(info) {
  const muname = String(info.muname || "").trim();
  if (muname) {
    return {
      label: `Map Unit ${muname}`,
      color: hashToColor(`mu:${muname.toLowerCase()}`)
    };
  }

  const musym = String(info.musym || "").trim();
  if (musym) {
    return {
      label: `Map Unit ${musym}`,
      color: hashToColor(`musym:${musym.toLowerCase()}`)
    };
  }

  const hyd = String(info.hydgrpdcd || "").trim().toUpperCase();
  if (hyd) {
    return {
      label: `Hydrologic Group ${hyd}`,
      color: HYDRO_GROUP_COLORS[hyd] || hashToColor(`hyd:${hyd}`)
    };
  }

  const drainage = String(info.drclassdcd || "").trim();
  if (drainage) {
    return {
      label: `Drainage ${drainage}`,
      color: hashToColor(`dr:${drainage.toLowerCase()}`)
    };
  }

  return {
    label: "Unknown",
    color: "#8c8c8c"
  };
}

function getFeatureMukey(properties) {
  if (!properties || typeof properties !== "object") {
    return "";
  }
  const keys = ["mukey", "MUKEY", "MuKey"];
  for (const key of keys) {
    const raw = properties[key];
    const normalized = String(raw === undefined || raw === null ? "" : raw).trim();
    if (normalized) {
      return normalized;
    }
  }
  return "";
}

function enrichSoilGeoJson(localGeoJsonPath, lookupSources) {
  const geojson = JSON.parse(fs.readFileSync(localGeoJsonPath, "utf8"));
  const features = Array.isArray(geojson.features) ? geojson.features : [];

  const legendStats = new Map();
  let joinedCount = 0;

  for (const feature of features) {
    if (!feature || typeof feature !== "object") {
      continue;
    }
    if (!feature.properties || typeof feature.properties !== "object") {
      feature.properties = {};
    }

    const mukey = getFeatureMukey(feature.properties);
    const info = {
      mukey,
      musym: feature.properties.musym || feature.properties.MUSYM || null,
      muname: null,
      hydgrpdcd: null,
      drclassdcd: null,
      flodfreqdcd: null,
      mutext: null
    };

    if (mukey && lookupSources.mapunit.has(mukey)) {
      Object.assign(info, lookupSources.mapunit.get(mukey));
      joinedCount += 1;
    }
    if (mukey && lookupSources.muaggatt.has(mukey)) {
      Object.assign(info, lookupSources.muaggatt.get(mukey));
      joinedCount += 1;
    }
    if (mukey && lookupSources.mutext && lookupSources.mutext.has(mukey)) {
      info.mutext = String(lookupSources.mutext.get(mukey) || "").trim() || null;
      joinedCount += 1;
    }

    const themed = pickSoilClassAndColor(info);
    feature.properties.soil_mukey = mukey || null;
    feature.properties.soil_musym = info.musym || null;
    feature.properties.soil_muname = info.muname || null;
    feature.properties.soil_hydgrpdcd = info.hydgrpdcd || null;
    feature.properties.soil_drclassdcd = info.drclassdcd || null;
    feature.properties.soil_flodfreqdcd = info.flodfreqdcd || null;
    feature.properties.soil_mutext = info.mutext || null;
    feature.properties.soil_class = themed.label;
    feature.properties.soil_color = themed.color;

    const existing = legendStats.get(themed.label) || {
      label: themed.label,
      color: themed.color,
      count: 0
    };
    existing.count += 1;
    legendStats.set(themed.label, existing);
  }

  fs.writeFileSync(localGeoJsonPath, JSON.stringify(geojson, null, 2) + "\n");

  const legend = Array.from(legendStats.values()).sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
  return {
    featureCount: features.length,
    joinedCount,
    legend
  };
}

function main() {
  requireCommand("gdalinfo");
  requireCommand("gdalsrsinfo");
  requireCommand("ogr2ogr");

  const options = parseArgs(process.argv.slice(2));
  const processedDir = resolveProcessedDir(rootDir);
  const demPath = normalizePath(options.dem || path.join(processedDir, "dem.tif"));
  if (!demPath || !fs.existsSync(demPath)) {
    throw new Error(`DEM not found: ${demPath}`);
  }

  const ssurgoRoot = locateSsurgoRoot(options.input);
  const spatialDir = path.join(ssurgoRoot, "spatial");
  const mapUnitShapefile = findMapUnitShapefile(spatialDir);
  const clipInfo = getDemClipInfo(demPath);

  const outDir = ensureDir(normalizePath(options.outdir || path.join(processedDir, "soils")));
  const reprojectedPath = path.join(outDir, "soils_reprojected.gpkg");
  const clippedPath = path.join(outDir, "soils_clipped.gpkg");
  const localGeoJsonPath = path.join(outDir, "soils_clipped_local.geojson");
  const geoJsonPath = path.join(outDir, "soils_clipped.geojson");
  const legendPath = path.join(outDir, "soil_legend.json");
  const metaPath = path.join(outDir, "soil_meta.json");
  const reprojectedLayer = "soils_reprojected";
  const clippedLayer = "soils_clipped";

  fs.rmSync(reprojectedPath, { force: true });
  fs.rmSync(clippedPath, { force: true });
  fs.rmSync(localGeoJsonPath, { force: true });
  fs.rmSync(geoJsonPath, { force: true });
  fs.rmSync(legendPath, { force: true });
  fs.rmSync(metaPath, { force: true });

  reprojectSoilMapUnits({
    mapUnitShapefile,
    outMergedPath: reprojectedPath,
    layerName: reprojectedLayer,
    targetCrs: clipInfo.crs
  });

  clipReprojectedSoils({
    mergedPath: reprojectedPath,
    mergedLayerName: reprojectedLayer,
    outGpkgPath: clippedPath,
    outLayerName: clippedLayer,
    bounds: clipInfo.bounds
  });

  exportGeoJsonOutputs({
    outGpkgPath: clippedPath,
    layerName: clippedLayer,
    outGeoJsonPath: geoJsonPath,
    outLocalGeoJsonPath: localGeoJsonPath
  });

  const mdbLookup = readMdbLookup(ssurgoRoot, outDir);
  const tabularLookup = readTabularLookup(ssurgoRoot);

  const lookup = {
    mapunit: new Map([...tabularLookup.mapunit, ...mdbLookup.mapunit]),
    muaggatt: new Map([...tabularLookup.muaggatt, ...mdbLookup.muaggatt]),
    mutext: new Map([...tabularLookup.mutext, ...mdbLookup.mutext])
  };

  const enrichment = enrichSoilGeoJson(localGeoJsonPath, lookup);
  const legendPayload = {
    generated_at: new Date().toISOString(),
    classes: enrichment.legend
  };
  writeJsonFile(legendPath, legendPayload);

  const meta = {
    generated_at: new Date().toISOString(),
    ssurgo_root: safeRelative(ssurgoRoot),
    source_mapunit_shapefile: safeRelative(mapUnitShapefile),
    source_mdb: mdbLookup.source,
    source_tabular: safeRelative(path.join(ssurgoRoot, "tabular")),
    dem: safeRelative(demPath),
    crs: clipInfo.crs,
    clip_bounds: clipInfo.bounds,
    enrichment: {
      feature_count: enrichment.featureCount,
      joined_values: enrichment.joinedCount,
      mapunit_rows: lookup.mapunit.size,
      muaggatt_rows: lookup.muaggatt.size,
      mutext_rows: lookup.mutext.size
    },
    output: {
      directory: safeRelative(outDir),
      gpkg: path.basename(clippedPath),
      geojson: path.basename(geoJsonPath),
      local_geojson: path.basename(localGeoJsonPath),
      legend: path.basename(legendPath)
    }
  };

  writeJsonFile(metaPath, meta);

  console.log(`SSURGO root: ${safeRelative(ssurgoRoot)}`);
  console.log(`Map unit shapefile: ${safeRelative(mapUnitShapefile)}`);
  console.log(`DEM: ${safeRelative(demPath)}`);
  console.log(`CRS: ${clipInfo.crs}`);
  console.log(`Output dir: ${safeRelative(outDir)}`);
  console.log(`Soil features: ${enrichment.featureCount}`);
  console.log(`Legend classes: ${enrichment.legend.length}`);
}

try {
  main();
} catch (error) {
  console.error(`\nSoils generation failed: ${error.message}`);
  process.exit(1);
}
