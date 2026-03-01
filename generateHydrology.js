#!/usr/bin/env node
"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

const scriptPath = path.join(__dirname, "Processing Pipeline", "generateHydrology.js");
const result = spawnSync(process.execPath, [scriptPath, ...process.argv.slice(2)], { stdio: "inherit" });

if (result.error) {
  console.error(`Hydrology generation failed: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);

