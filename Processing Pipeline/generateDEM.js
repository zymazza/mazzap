#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const rootDir = path.resolve(__dirname, "..");
const rawDataDir = path.join(rootDir, "Raw Data Inputs");
const pipelineDir = __dirname;
const processedDirCandidates = [
  path.join(rootDir, "Processed_Data"),
  path.join(rootDir, "Processed Data")
];

const templatePath = path.join(pipelineDir, "dem.pipeline.template.json");
const pipelineRunPath = path.join(pipelineDir, "dem.pipeline.run.json");

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: rootDir,
    encoding: "utf8",
    stdio: options.stdio || "pipe"
  });

  if (result.error) {
    throw result.error;
  }

  if (result.status !== 0) {
    const stderr = (result.stderr || "").trim();
    const stdout = (result.stdout || "").trim();
    const details = [stdout, stderr].filter(Boolean).join("\n");
    throw new Error(`Command failed: ${command} ${args.join(" ")}\n${details}`);
  }

  return result.stdout || "";
}

function requireCommand(commandName) {
  const probe = spawnSync(commandName, ["--version"], { encoding: "utf8" });
  if (probe.error && probe.error.code === "ENOENT") {
    throw new Error(`Missing required command: ${commandName}`);
  }
}

function resolveInputFile() {
  const candidates = [
    path.join(rawDataDir, "lidar_input.copc.laz"),
    path.join(rawDataDir, "lidar_input.laz")
  ];

  for (const filePath of candidates) {
    if (fs.existsSync(filePath)) {
      return filePath;
    }
  }

  throw new Error(
    "No LiDAR input found. Expected lidar_input.copc.laz or lidar_input.laz in Raw Data Inputs."
  );
}

function resolveProcessedDir() {
  for (const candidate of processedDirCandidates) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return candidate;
    }
  }

  return processedDirCandidates[0];
}

function inferReaderType(inputPath) {
  return inputPath.toLowerCase().endsWith(".copc.laz") ? "readers.copc" : "readers.las";
}

function main() {
  requireCommand("pdal");

  if (!fs.existsSync(templatePath)) {
    throw new Error(`Missing PDAL template: ${templatePath}`);
  }

  const processedDataDir = resolveProcessedDir();
  fs.mkdirSync(processedDataDir, { recursive: true });
  const outputDemPath = path.join(processedDataDir, "dem.tif");

  const inputFilePath = resolveInputFile();
  const readerType = inferReaderType(inputFilePath);
  const template = JSON.parse(fs.readFileSync(templatePath, "utf8"));

  template.pipeline[0].type = readerType;
  template.pipeline[0].filename = inputFilePath;
  template.pipeline[template.pipeline.length - 1].filename = outputDemPath;

  fs.writeFileSync(pipelineRunPath, JSON.stringify(template, null, 2) + "\n");

  console.log(`Input: ${path.relative(rootDir, inputFilePath)}`);
  console.log(`Reader: ${readerType}`);
  console.log(`Running PDAL pipeline: ${path.relative(rootDir, pipelineRunPath)}`);

  run("pdal", ["pipeline", pipelineRunPath], { stdio: "inherit" });
  const outputStats = fs.statSync(outputDemPath);
  const sizeMb = (outputStats.size / (1024 * 1024)).toFixed(2);
  console.log(`\nDEM written to: ${path.relative(rootDir, outputDemPath)} (${sizeMb} MB)`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`\nDEM generation failed: ${error.message}`);
    process.exit(1);
  }
}

module.exports = { main };
