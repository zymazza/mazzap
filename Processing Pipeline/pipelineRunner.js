#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

function runCommand(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    encoding: "utf8",
    stdio: options.stdio || "pipe",
    maxBuffer: options.maxBuffer || 32 * 1024 * 1024,
    input: options.input
  });

  if (result.error && (result.status === null || result.status === undefined)) {
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

function resolveLidarInput(rootDir) {
  const rawDataDir = path.join(rootDir, "Raw Data Inputs");
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

function inferLidarReaderType(inputPath) {
  return inputPath.toLowerCase().endsWith(".copc.laz") ? "readers.copc" : "readers.las";
}

function resolveProcessedDir(rootDir) {
  const candidates = [
    path.join(rootDir, "Processed_Data"),
    path.join(rootDir, "Processed Data")
  ];

  for (const candidate of candidates) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return candidate;
    }
  }

  return candidates[0];
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
  return dirPath;
}

function loadJsonFile(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJsonFile(filePath, data) {
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + "\n");
}

function substitutePlaceholders(value, replacements) {
  if (typeof value === "string") {
    let result = value;
    for (const [key, replacement] of Object.entries(replacements)) {
      result = result.split(key).join(String(replacement));
    }
    return result;
  }

  if (Array.isArray(value)) {
    return value.map((item) => substitutePlaceholders(item, replacements));
  }

  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = substitutePlaceholders(item, replacements);
    }
    return out;
  }

  return value;
}

function getStageByTag(pipelineJson, tag) {
  return pipelineJson.pipeline.find((stage) => stage.tag === tag);
}

function removeStagesByTag(pipelineJson, tags) {
  const tagSet = new Set(tags);
  pipelineJson.pipeline = pipelineJson.pipeline.filter((stage) => !tagSet.has(stage.tag));
  return pipelineJson;
}

function runPdalPipeline(rootDir, runPipelinePath) {
  return runCommand("pdal", ["pipeline", runPipelinePath], {
    cwd: rootDir,
    stdio: "inherit"
  });
}

module.exports = {
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
};
