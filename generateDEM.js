#!/usr/bin/env node
"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

const scriptPath = path.join(__dirname, "Processing Pipeline", "generateDEM.js");
const result = spawnSync(process.execPath, [scriptPath], { stdio: "inherit" });

if (result.error) {
  console.error(`DEM generation failed: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);
