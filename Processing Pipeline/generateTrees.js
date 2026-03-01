#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const {
  ensureDir,
  getStageByTag,
  inferLidarReaderType,
  loadJsonFile,
  removeStagesByTag,
  requireCommand,
  resolveLidarInput,
  resolveProcessedDir,
  runCommand,
  runPdalPipeline,
  substitutePlaceholders,
  writeJsonFile
} = require("./pipelineRunner");

const rootDir = path.resolve(__dirname, "..");
const pipelineDir = __dirname;
const templatePath = path.join(pipelineDir, "trees.pipeline.template.json");
const runPipelinePath = path.join(pipelineDir, "trees.pipeline.run.json");

const DEFAULT_OPTIONS = {
  minTreeHag: 2.0,
  maxTreeHag: 60.0,
  resolution: 1.0,
  voxel: 1.0,
  noOutlierFilter: false,
  excludeClasses: [6, 7, 9, 18],
  mode: "all",
  treeTopGrid: 2.0,
  nmsRadius: 2.5
};

function usageAndExit(message) {
  if (message) {
    console.error(message);
    console.error("");
  }

  console.error("Usage:");
  console.error("  node \"Processing Pipeline/generateTrees.js\" [options]");
  console.error("");
  console.error("Options:");
  console.error("  --minTreeHag <number>       Minimum tree height above ground (default: 2.0)");
  console.error("  --maxTreeHag <number>       Maximum tree height above ground (default: 60.0)");
  console.error("  --resolution <number>       Raster resolution for CHM/density (default: 1.0)");
  console.error("  --voxel <number>            Voxel size for tree candidate decimation (default: 1.0)");
  console.error("  --noOutlierFilter           Disable outlier filtering stage");
  console.error("  --excludeClasses <list>     Comma-separated classes to exclude (default: 6,7,9,18)");
  console.error("  --mode <points|rasters|all> Output mode (default: all)");
  console.error("  --help                      Show this help");
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

function parseExcludeClasses(raw) {
  if (raw === "" || raw.toLowerCase() === "none") {
    return [];
  }

  const values = raw
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => Number(item));

  if (values.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
    usageAndExit(`Invalid --excludeClasses value: ${raw}`);
  }

  return Array.from(new Set(values));
}

function parseMode(raw) {
  if (raw === "points" || raw === "rasters" || raw === "all") {
    return raw;
  }
  usageAndExit(`Invalid --mode value: ${raw}. Expected points|rasters|all.`);
}

function parseArgs(argv) {
  const options = { ...DEFAULT_OPTIONS };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const eqPos = arg.indexOf("=");
    const hasInlineValue = eqPos !== -1;
    const flag = hasInlineValue ? arg.slice(0, eqPos) : arg;
    const inlineValue = hasInlineValue ? arg.slice(eqPos + 1) : undefined;

    if (flag === "--help") {
      usageAndExit();
    } else if (flag === "--noOutlierFilter") {
      options.noOutlierFilter = true;
    } else if (flag === "--minTreeHag") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.minTreeHag = toPositiveNumber(value, "--minTreeHag");
      i = nextIndex;
    } else if (flag === "--maxTreeHag") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.maxTreeHag = toPositiveNumber(value, "--maxTreeHag");
      i = nextIndex;
    } else if (flag === "--resolution") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.resolution = toPositiveNumber(value, "--resolution");
      i = nextIndex;
    } else if (flag === "--voxel") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.voxel = toPositiveNumber(value, "--voxel");
      i = nextIndex;
    } else if (flag === "--excludeClasses") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.excludeClasses = parseExcludeClasses(value);
      i = nextIndex;
    } else if (flag === "--mode") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.mode = parseMode(value);
      i = nextIndex;
    } else {
      usageAndExit(`Unknown option: ${arg}`);
    }
  }

  if (options.maxTreeHag <= options.minTreeHag) {
    usageAndExit("--maxTreeHag must be greater than --minTreeHag");
  }

  return options;
}

function buildTreeExpression(options) {
  const parts = [
    "Classification != 2",
    `HeightAboveGround >= ${options.minTreeHag}`,
    `HeightAboveGround <= ${options.maxTreeHag}`
  ];

  for (const classId of options.excludeClasses) {
    parts.push(`Classification != ${classId}`);
  }

  return parts.join(" && ");
}

function configurePipeline({
  template,
  readerType,
  inputPath,
  treePointsLasPath,
  treeChmPath,
  treeDensityPath,
  options,
  hagMode
}) {
  const expression = buildTreeExpression(options);
  const configured = substitutePlaceholders(template, {
    "__INPUT__": inputPath,
    "__TREE_POINTS_LAS__": treePointsLasPath,
    "__TREE_CHM__": treeChmPath,
    "__TREE_DENSITY__": treeDensityPath,
    "__TREE_EXPRESSION__": expression
  });

  const readerStage = getStageByTag(configured, "reader");
  readerStage.type = readerType;

  const hagStage = getStageByTag(configured, "hag");
  if (hagMode === "nn") {
    hagStage.type = "filters.hag_nn";
    hagStage.count = 10;
    hagStage.max_distance = 2.0;
    delete hagStage.allow_extrapolation;
  } else {
    hagStage.type = "filters.hag_delaunay";
    hagStage.allow_extrapolation = true;
    delete hagStage.count;
    delete hagStage.max_distance;
  }

  const voxelStage = getStageByTag(configured, "decimated");
  voxelStage.cell = options.voxel;

  const chmStage = getStageByTag(configured, "write_chm");
  chmStage.resolution = options.resolution;

  const densityStage = getStageByTag(configured, "write_density");
  densityStage.resolution = options.resolution;

  if (options.noOutlierFilter) {
    removeStagesByTag(configured, ["outlier", "drop_outliers"]);
  }

  return configured;
}

function prunePipelineForMode(pipelineJson, mode) {
  if (mode === "points") {
    removeStagesByTag(pipelineJson, ["write_chm", "write_density"]);
    return pipelineJson;
  }

  if (mode === "chm") {
    removeStagesByTag(pipelineJson, ["decimated", "write_points", "write_density"]);
    return pipelineJson;
  }

  if (mode === "density") {
    removeStagesByTag(pipelineJson, ["decimated", "write_points", "write_chm"]);
    return pipelineJson;
  }

  throw new Error(`Unknown trees pipeline mode: ${mode}`);
}

function executePipelineModeWithFallback({
  mode,
  template,
  readerType,
  inputPath,
  treePointsLasPath,
  treeChmPath,
  treeDensityPath,
  options,
  preferredHagMode
}) {
  const hagCandidates = preferredHagMode === "nn" ? ["nn"] : ["delaunay", "nn"];
  let lastError = null;

  for (const hagMode of hagCandidates) {
    const configured = configurePipeline({
      template,
      readerType,
      inputPath,
      treePointsLasPath,
      treeChmPath,
      treeDensityPath,
      options,
      hagMode
    });
    prunePipelineForMode(configured, mode);
    writeJsonFile(runPipelinePath, configured);
    console.log(`Running trees ${mode} pipeline (${hagMode}) via ${path.relative(rootDir, runPipelinePath)}`);

    try {
      runPdalPipeline(rootDir, runPipelinePath);
      return hagMode;
    } catch (error) {
      lastError = error;
      if (hagMode === "delaunay") {
        console.warn(`HAG delaunay failed for mode=${mode}; retrying with filters.hag_nn.\nReason: ${error.message}`);
      }
    }
  }

  throw lastError || new Error(`Trees ${mode} pipeline failed.`);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function validatePointsOutput(pointsPath) {
  if (!fs.existsSync(pointsPath)) {
    throw new Error(`Expected points output not found: ${pointsPath}`);
  }

  const summaryJson = runCommand("pdal", ["info", "--summary", pointsPath], { cwd: rootDir });
  let parsed = null;
  try {
    parsed = JSON.parse(summaryJson);
  } catch {
    throw new Error(`Unable to parse pdal summary JSON for ${pointsPath}`);
  }

  const dims = ((parsed.summary && parsed.summary.dimensions) || "").toString();
  if (!dims.includes("HeightAboveGround")) {
    throw new Error(`Points output missing HeightAboveGround dimension: ${pointsPath}`);
  }

  const bounds = parsed.summary && parsed.summary.bounds;
  const numPoints = parsed.summary && parsed.summary.num_points;
  console.log(
    `Validated points output: ${path.relative(rootDir, pointsPath)} ` +
      `(num_points=${numPoints || "unknown"}, bounds=${bounds ? "ok" : "unknown"})`
  );
}

function validateRasterOutput(rasterPath, label) {
  if (!fs.existsSync(rasterPath)) {
    throw new Error(`Expected ${label} raster not found: ${rasterPath}`);
  }

  const info = runCommand("gdalinfo", ["-stats", rasterPath], { cwd: rootDir });
  const keyLines = info
    .split(/\r?\n/)
    .filter((line) =>
      /Description =|Minimum=|Maximum=|NoData Value|STATISTICS_VALID_PERCENT/.test(line)
    )
    .slice(0, 8);
  console.log(`Validated ${label} raster: ${path.relative(rootDir, rasterPath)}`);
  for (const line of keyLines) {
    console.log(`  ${line.trim()}`);
  }
}

function writeOptionalLaz(pointsLasPath, pointsLazPath) {
  try {
    runCommand(
      "pdal",
      [
        "translate",
        pointsLasPath,
        pointsLazPath,
        "--writers.las.compression=laszip",
        "--writers.las.minor_version=4",
        "--writers.las.dataformat_id=6",
        "--writers.las.extra_dims=HeightAboveGround=float32",
        "--writers.las.forward=all"
      ],
      { cwd: rootDir }
    );
    validatePointsOutput(pointsLazPath);
    console.log(`Optional LAZ written: ${path.relative(rootDir, pointsLazPath)}`);
  } catch (error) {
    console.warn(`Optional LAZ export skipped due to validation failure: ${error.message}`);
    fs.rmSync(pointsLazPath, { force: true });
  }
}

function exportCandidatePointsCsv(pointsLasPath, csvPath) {
  runCommand(
    "pdal",
    [
      "translate",
      pointsLasPath,
      csvPath,
      "--writers.text.format=csv",
      "--writers.text.order=X,Y,Z,HeightAboveGround",
      "--writers.text.keep_unspecified=false"
    ],
    { cwd: rootDir }
  );
}

function parseCandidateCsv(csvText) {
  const lines = csvText
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  const points = [];
  for (const line of lines) {
    const parts = line.split(",");
    if (parts.length < 4) {
      continue;
    }
    const x = Number(parts[0].replace(/"/g, ""));
    const y = Number(parts[1].replace(/"/g, ""));
    const z = Number(parts[2].replace(/"/g, ""));
    const height = Number(parts[3].replace(/"/g, ""));
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z) || !Number.isFinite(height)) {
      continue;
    }
    points.push({ x, y, z, height });
  }
  return points;
}

function extractTreeTopCandidates(points, gridCell) {
  const cells = new Map();

  for (const point of points) {
    const cx = Math.floor(point.x / gridCell);
    const cy = Math.floor(point.y / gridCell);
    const key = `${cx}:${cy}`;

    if (!cells.has(key)) {
      cells.set(key, { count: 0, top: point });
    }

    const cell = cells.get(key);
    cell.count += 1;
    if (point.height > cell.top.height) {
      cell.top = point;
    }
  }

  const candidates = [];
  for (const cell of cells.values()) {
    candidates.push({
      ...cell.top,
      localDensity: cell.count
    });
  }

  return candidates;
}

function applyNonMaximumSuppression(candidates, radius) {
  const sorted = [...candidates].sort((a, b) => b.height - a.height);
  const kept = [];
  const r2 = radius * radius;

  for (const candidate of sorted) {
    let suppressed = false;
    for (const existing of kept) {
      const dx = candidate.x - existing.x;
      const dy = candidate.y - existing.y;
      if (dx * dx + dy * dy <= r2) {
        suppressed = true;
        break;
      }
    }
    if (!suppressed) {
      kept.push(candidate);
    }
  }

  return kept;
}

// Placeholder for future NAIP/LiDAR fused spectral sampling.
function sampleSpectralPlaceholder() {
  return {
    r: null,
    g: null,
    b: null,
    nir: null
  };
}

// Placeholder for future species classification stage.
function classifyTreeSpeciesPlaceholder() {
  return {
    label: null,
    confidence: null
  };
}

function buildTreeInstancesFromCandidates(points, options) {
  if (points.length === 0) {
    return [];
  }

  const tops = extractTreeTopCandidates(points, options.treeTopGrid);
  const kept = applyNonMaximumSuppression(tops, options.nmsRadius);

  const maxLocalDensity = tops.reduce((acc, p) => Math.max(acc, p.localDensity), 1);
  const heightRange = Math.max(options.maxTreeHag - options.minTreeHag, 1e-6);

  return kept.map((candidate) => {
    const groundZ = candidate.z - candidate.height;
    const radius = clamp(candidate.height * 0.15, 1.0, 6.0);
    const densityScore = clamp(candidate.localDensity / maxLocalDensity, 0, 1);
    const heightScore = clamp((candidate.height - options.minTreeHag) / heightRange, 0, 1);
    const confidence = clamp(0.55 * heightScore + 0.45 * densityScore, 0, 1);

    return {
      x: candidate.x,
      y: candidate.y,
      z: groundZ,
      height: candidate.height,
      radius,
      confidence: Number(confidence.toFixed(3)),
      spectral: sampleSpectralPlaceholder(),
      species: classifyTreeSpeciesPlaceholder(),
      naipTileId: null,
      naipSample: null
    };
  });
}

function generateTreeInstancesJson(pointsLasPath, outputJsonPath, options) {
  if (!fs.existsSync(pointsLasPath)) {
    throw new Error(`Cannot generate tree_instances.json; missing ${pointsLasPath}`);
  }

  const tmpCsvPath = path.join("/tmp", `mazzap-tree-candidates-${process.pid}-${Date.now()}.csv`);
  try {
    exportCandidatePointsCsv(pointsLasPath, tmpCsvPath);
    const csvText = fs.readFileSync(tmpCsvPath, "utf8");
    const points = parseCandidateCsv(csvText);
    const instances = buildTreeInstancesFromCandidates(points, options);
    fs.writeFileSync(outputJsonPath, JSON.stringify(instances, null, 2) + "\n");
    console.log(
      `Tree instances written: ${path.relative(rootDir, outputJsonPath)} ` +
        `(${instances.length} instances from ${points.length} candidate points)`
    );
  } finally {
    fs.rmSync(tmpCsvPath, { force: true });
  }
}

function main() {
  requireCommand("pdal");
  requireCommand("gdalinfo");

  if (!fs.existsSync(templatePath)) {
    throw new Error(`Missing trees template: ${templatePath}`);
  }

  const options = parseArgs(process.argv.slice(2));
  const inputPath = resolveLidarInput(rootDir);
  const readerType = inferLidarReaderType(inputPath);
  const processedRoot = resolveProcessedDir(rootDir);
  const treesDir = ensureDir(path.join(processedRoot, "trees"));

  const treePointsLasPath = path.join(treesDir, "tree_candidates_points.las");
  const treePointsLazPath = path.join(treesDir, "tree_candidates_points.laz");
  const treeChmPath = path.join(treesDir, "tree_canopy_height.tif");
  const treeDensityPath = path.join(treesDir, "tree_density.tif");
  const treeInstancesPath = path.join(treesDir, "tree_instances.json");

  const template = loadJsonFile(templatePath);

  console.log(`Input: ${path.relative(rootDir, inputPath)}`);
  console.log(`Reader: ${readerType}`);
  console.log(`Trees output dir: ${path.relative(rootDir, treesDir)}`);
  console.log(
    `Options: minTreeHag=${options.minTreeHag}, maxTreeHag=${options.maxTreeHag}, ` +
      `resolution=${options.resolution}, voxel=${options.voxel}, mode=${options.mode}, ` +
      `outlierFilter=${options.noOutlierFilter ? "off" : "on"}, ` +
      `excludeClasses=${options.excludeClasses.length ? options.excludeClasses.join(",") : "none"}`
  );

  const runPoints = options.mode === "points" || options.mode === "all";
  const runRasters = options.mode === "rasters" || options.mode === "all";

  let hagPreference = "delaunay";
  if (runPoints) {
    hagPreference = executePipelineModeWithFallback({
      mode: "points",
      template,
      readerType,
      inputPath,
      treePointsLasPath,
      treeChmPath,
      treeDensityPath,
      options,
      preferredHagMode: hagPreference
    });
    validatePointsOutput(treePointsLasPath);
    writeOptionalLaz(treePointsLasPath, treePointsLazPath);
    generateTreeInstancesJson(treePointsLasPath, treeInstancesPath, options);
  }

  if (runRasters) {
    hagPreference = executePipelineModeWithFallback({
      mode: "chm",
      template,
      readerType,
      inputPath,
      treePointsLasPath,
      treeChmPath,
      treeDensityPath,
      options,
      preferredHagMode: hagPreference
    });

    hagPreference = executePipelineModeWithFallback({
      mode: "density",
      template,
      readerType,
      inputPath,
      treePointsLasPath,
      treeChmPath,
      treeDensityPath,
      options,
      preferredHagMode: hagPreference
    });

    validateRasterOutput(treeChmPath, "tree CHM");
    validateRasterOutput(treeDensityPath, "tree density");
  }

  if (!runPoints) {
    if (fs.existsSync(treePointsLasPath)) {
      validatePointsOutput(treePointsLasPath);
      generateTreeInstancesJson(treePointsLasPath, treeInstancesPath, options);
    } else {
      console.warn(
        "tree_instances.json not updated because points mode was skipped and no prior tree_candidates_points.las was found."
      );
    }
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`\nTree generation failed: ${error.message}`);
    process.exit(1);
  }
}

module.exports = { main };
