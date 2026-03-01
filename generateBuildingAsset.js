#!/usr/bin/env node
"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

const scriptPath = path.join(__dirname, "Processing Pipeline", "generateBuildingAsset.js");
const result = spawnSync(process.execPath, [scriptPath, ...process.argv.slice(2)], { stdio: "inherit" });

if (result.error) {
  console.error(`Building asset generation failed: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);
