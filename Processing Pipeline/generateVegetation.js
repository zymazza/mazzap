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
  runPdalPipeline,
  substitutePlaceholders,
  writeJsonFile
} = require("./pipelineRunner");

const rootDir = path.resolve(__dirname, "..");
const pipelineDir = __dirname;
const templatePath = path.join(pipelineDir, "vegetation.pipeline.template.json");
const runPipelinePath = path.join(pipelineDir, "vegetation.pipeline.run.json");

function usageAndExit(message) {
  if (message) {
    console.error(message);
    console.error("");
  }
  console.error("Usage:");
  console.error("  node \"Processing Pipeline/generateVegetation.js\" [options]");
  console.error("");
  console.error("Options:");
  console.error("  --minHag <number>            Minimum shrub height above ground (default: 0.2)");
  console.error("  --maxHag <number>            Maximum shrub height above ground (default: 2.0)");
  console.error("  --densityResolution <number> Raster resolution for shrubs density (default: 1.0)");
  console.error("  --voxel <number>             Voxel size for point decimation (default: 0.5)");
  console.error("  --noOutlierFilter            Disable statistical outlier filtering");
  console.error("  --excludeClasses <list>      Comma-separated class ids to exclude (default: 6,7,9,18)");
  console.error("  --help                       Show this help");
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

  const classes = raw
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => Number(item));

  if (classes.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
    usageAndExit(`Invalid --excludeClasses: ${raw}`);
  }

  return Array.from(new Set(classes));
}

function parseArgs(argv) {
  const options = {
    minHag: 0.2,
    maxHag: 2.0,
    densityResolution: 1.0,
    voxel: 0.5,
    noOutlierFilter: false,
    excludeClasses: [6, 7, 9, 18]
  };

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
    } else if (flag === "--minHag") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.minHag = toPositiveNumber(value, "--minHag");
      i = nextIndex;
    } else if (flag === "--maxHag") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.maxHag = toPositiveNumber(value, "--maxHag");
      i = nextIndex;
    } else if (flag === "--densityResolution") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.densityResolution = toPositiveNumber(value, "--densityResolution");
      i = nextIndex;
    } else if (flag === "--voxel") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.voxel = toPositiveNumber(value, "--voxel");
      i = nextIndex;
    } else if (flag === "--excludeClasses") {
      const { value, nextIndex } = readFlagValue(argv, i, inlineValue);
      options.excludeClasses = parseExcludeClasses(value);
      i = nextIndex;
    } else {
      usageAndExit(`Unknown option: ${arg}`);
    }
  }

  if (options.maxHag <= options.minHag) {
    usageAndExit("--maxHag must be greater than --minHag");
  }

  return options;
}

function buildShrubExpression(options) {
  const parts = [
    "Classification != 2",
    `HeightAboveGround >= ${options.minHag}`,
    `HeightAboveGround <= ${options.maxHag}`
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
  shrubsPointsPath,
  shrubsDensityPath,
  options,
  hagMode
}) {
  const expression = buildShrubExpression(options);
  const configured = substitutePlaceholders(template, {
    "__INPUT__": inputPath,
    "__SHRUBS_POINTS__": shrubsPointsPath,
    "__SHRUBS_DENSITY__": shrubsDensityPath,
    "__SHRUB_EXPRESSION__": expression
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

  const densityStage = getStageByTag(configured, "write_density");
  densityStage.resolution = options.densityResolution;

  if (options.noOutlierFilter) {
    removeStagesByTag(configured, ["outlier", "drop_outliers"]);
  }

  return configured;
}

function prunePipelineForMode(pipelineJson, mode) {
  if (mode === "points") {
    removeStagesByTag(pipelineJson, ["write_density"]);
    return pipelineJson;
  }

  if (mode === "density") {
    removeStagesByTag(pipelineJson, ["decimated", "write_points"]);
    return pipelineJson;
  }

  throw new Error(`Unknown vegetation pipeline mode: ${mode}`);
}

function executeModeWithFallback({
  mode,
  template,
  readerType,
  inputPath,
  shrubsPointsPath,
  shrubsDensityPath,
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
      shrubsPointsPath,
      shrubsDensityPath,
      options,
      hagMode
    });
    prunePipelineForMode(configured, mode);
    writeJsonFile(runPipelinePath, configured);
    console.log(
      `Running vegetation ${mode} pipeline (${hagMode}) via ${path.relative(rootDir, runPipelinePath)}`
    );

    try {
      runPdalPipeline(rootDir, runPipelinePath);
      return hagMode;
    } catch (error) {
      lastError = error;
      if (hagMode === "delaunay") {
        console.warn(`HAG delaunay failed for ${mode}; retrying with filters.hag_nn.\nReason: ${error.message}`);
      }
    }
  }

  throw lastError || new Error(`Vegetation ${mode} pipeline failed.`);
}

function main() {
  requireCommand("pdal");

  if (!fs.existsSync(templatePath)) {
    throw new Error(`Missing vegetation template: ${templatePath}`);
  }

  const options = parseArgs(process.argv.slice(2));
  const inputPath = resolveLidarInput(rootDir);
  const readerType = inferLidarReaderType(inputPath);
  const processedRoot = resolveProcessedDir(rootDir);
  const vegetationDir = ensureDir(path.join(processedRoot, "vegetation"));
  const shrubsPointsPath = path.join(vegetationDir, "shrubs_points.laz");
  const shrubsDensityPath = path.join(vegetationDir, "shrubs_density.tif");
  const template = loadJsonFile(templatePath);

  console.log(`Input: ${path.relative(rootDir, inputPath)}`);
  console.log(`Reader: ${readerType}`);
  console.log(`Output points: ${path.relative(rootDir, shrubsPointsPath)}`);
  console.log(`Output density: ${path.relative(rootDir, shrubsDensityPath)}`);
  console.log(
    `Options: minHag=${options.minHag}, maxHag=${options.maxHag}, ` +
      `densityResolution=${options.densityResolution}, voxel=${options.voxel}, ` +
      `outlierFilter=${options.noOutlierFilter ? "off" : "on"}, ` +
      `excludeClasses=${options.excludeClasses.length ? options.excludeClasses.join(",") : "none"}`
  );

  const pointsHagMode = executeModeWithFallback({
    mode: "points",
    template,
    readerType,
    inputPath,
    shrubsPointsPath,
    shrubsDensityPath,
    options,
    preferredHagMode: "delaunay"
  });

  const densityHagMode = executeModeWithFallback({
    mode: "density",
    template,
    readerType,
    inputPath,
    shrubsPointsPath,
    shrubsDensityPath,
    options,
    preferredHagMode: pointsHagMode
  });

  const pointsSizeMb = (fs.statSync(shrubsPointsPath).size / (1024 * 1024)).toFixed(2);
  const densitySizeMb = (fs.statSync(shrubsDensityPath).size / (1024 * 1024)).toFixed(2);
  if (pointsHagMode === "nn" || densityHagMode === "nn") {
    console.log("Vegetation pipeline completed using HAG fallback: filters.hag_nn");
  }
  console.log(`\nShrub points written: ${path.relative(rootDir, shrubsPointsPath)} (${pointsSizeMb} MB)`);
  console.log(`Shrub density written: ${path.relative(rootDir, shrubsDensityPath)} (${densitySizeMb} MB)`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`\nVegetation generation failed: ${error.message}`);
    process.exit(1);
  }
}

module.exports = { main };
