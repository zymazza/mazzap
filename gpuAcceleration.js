"use strict";

const { spawnSync } = require("child_process");

const BLENDER_BACKENDS_SUPPORTED = ["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"];
const PROFILE_CACHE = new Map();

function hasText(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function normalizeName(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\[[^\]]+\]/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function parseInteger(value) {
  if (!hasText(value)) {
    return null;
  }
  const parsed = Number(String(value).trim());
  if (!Number.isInteger(parsed)) {
    return null;
  }
  return parsed;
}

function toVendor(rawVendor) {
  const value = String(rawVendor || "").toLowerCase().trim();
  if (value === "nvidia") {
    return "nvidia";
  }
  if (value === "amd" || value === "ati" || value === "radeon") {
    return "amd";
  }
  if (value === "intel") {
    return "intel";
  }
  return "any";
}

function parseAccelerationMode(rawMode) {
  const value = String(rawMode || "").toLowerCase().trim();
  if (value === "cpu" || value === "off" || value === "disabled" || value === "false" || value === "0") {
    return "cpu";
  }
  return "auto";
}

function parseBlenderBackends(rawBackends) {
  if (!hasText(rawBackends)) {
    return [];
  }

  const tokens = String(rawBackends)
    .split(",")
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);

  const unique = [];
  const seen = new Set();
  for (const token of tokens) {
    if (!BLENDER_BACKENDS_SUPPORTED.includes(token) || seen.has(token)) {
      continue;
    }
    seen.add(token);
    unique.push(token);
  }

  return unique;
}

function runProbe(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    maxBuffer: options.maxBuffer || 8 * 1024 * 1024,
    env: options.env || process.env
  });

  if (result.error && result.error.code === "ENOENT") {
    return {
      ok: false,
      missing: true,
      stdout: "",
      stderr: "",
      status: null
    };
  }

  if (result.error && (result.status === null || result.status === undefined)) {
    return {
      ok: false,
      missing: false,
      stdout: result.stdout || "",
      stderr: String(result.error.message || ""),
      status: null
    };
  }

  if (result.status !== 0) {
    return {
      ok: false,
      missing: false,
      stdout: result.stdout || "",
      stderr: result.stderr || "",
      status: result.status
    };
  }

  return {
    ok: true,
    missing: false,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    status: result.status
  };
}

function isLikelyIntegratedGpuName(name, vendor) {
  if (vendor === "intel") {
    return true;
  }

  const text = String(name || "").toLowerCase();
  return (
    text.includes("integrated") ||
    text.includes(" igpu") ||
    text.includes("uhd graphics") ||
    text.includes("iris xe") ||
    text.includes("radeon graphics") ||
    text.includes("apu")
  );
}

function detectNvidiaGpus(env) {
  const probe = runProbe(
    "nvidia-smi",
    ["--query-gpu=index,name,memory.total,pci.bus_id", "--format=csv,noheader,nounits"],
    { env }
  );

  if (!probe.ok) {
    return {
      available: false,
      gpus: []
    };
  }

  const gpus = [];
  const lines = String(probe.stdout || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  for (const line of lines) {
    const parts = line.split(",").map((item) => item.trim());
    if (parts.length < 2) {
      continue;
    }

    const index = parseInteger(parts[0]);
    const name = parts[1];
    const memoryMb = Number(parts[2]);
    const busId = parts.slice(3).join(",").trim() || null;

    gpus.push({
      vendor: "nvidia",
      name,
      index,
      memoryMb: Number.isFinite(memoryMb) ? memoryMb : null,
      busId,
      integrated: false,
      source: "nvidia-smi"
    });
  }

  return {
    available: gpus.length > 0,
    gpus
  };
}

function vendorFromLspciLine(line) {
  const text = String(line || "").toLowerCase();
  if (text.includes("nvidia")) {
    return "nvidia";
  }
  if (text.includes("advanced micro devices") || text.includes(" amd") || text.includes("radeon")) {
    return "amd";
  }
  if (text.includes("intel")) {
    return "intel";
  }
  return "other";
}

function detectLspciGpus(env) {
  const probe = runProbe("lspci", ["-nn"], { env });
  if (!probe.ok) {
    return [];
  }

  const gpus = [];
  const lines = String(probe.stdout || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  for (const line of lines) {
    if (!/(vga compatible controller|3d controller|display controller)/i.test(line)) {
      continue;
    }

    const vendor = vendorFromLspciLine(line);
    const name = line.includes(":") ? line.slice(line.indexOf(":") + 1).trim() : line;

    gpus.push({
      vendor,
      name,
      index: null,
      memoryMb: null,
      busId: null,
      integrated: isLikelyIntegratedGpuName(name, vendor),
      source: "lspci"
    });
  }

  return gpus;
}

function dedupeGpus(gpus) {
  const unique = [];
  const seen = new Set();

  for (const gpu of gpus) {
    const key = `${gpu.vendor}|${normalizeName(gpu.name)}|${gpu.index === null ? "na" : gpu.index}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    unique.push(gpu);
  }

  return unique;
}

function scoreGpu(gpu, preferredVendor) {
  const vendorScore = {
    nvidia: 120,
    amd: 80,
    intel: 40,
    other: 20
  };

  let score = vendorScore[gpu.vendor] || vendorScore.other;
  if (preferredVendor !== "any" && gpu.vendor === preferredVendor) {
    score += 40;
  }
  if (!gpu.integrated) {
    score += 25;
  }
  if (Number.isFinite(gpu.memoryMb) && gpu.memoryMb > 0) {
    score += Math.min(gpu.memoryMb / 1024, 32);
  }
  if (gpu.source === "nvidia-smi") {
    score += 5;
  }

  return score;
}

function pickPreferredGpu(gpus, preferredVendor, requestedIndex) {
  if (gpus.length === 0) {
    return null;
  }

  if (Number.isInteger(requestedIndex)) {
    const direct = gpus.find((gpu) => gpu.vendor === "nvidia" && gpu.index === requestedIndex);
    if (direct) {
      return direct;
    }
  }

  let selected = gpus[0];
  let bestScore = scoreGpu(selected, preferredVendor);

  for (const gpu of gpus.slice(1)) {
    const score = scoreGpu(gpu, preferredVendor);
    if (score > bestScore) {
      selected = gpu;
      bestScore = score;
    }
  }

  return selected;
}

function defaultBlenderBackendsForVendor(vendor) {
  if (vendor === "nvidia") {
    return ["OPTIX", "CUDA"];
  }
  if (vendor === "amd") {
    return ["HIP"];
  }
  if (vendor === "intel") {
    return ["ONEAPI"];
  }
  return ["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"];
}

function cacheKeyFromEnv(env) {
  const values = [
    env.MAZZAP_ACCELERATION_MODE,
    env.MAZZAP_GPU_VENDOR_PREFERENCE,
    env.MAZZAP_GPU_INDEX,
    env.MAZZAP_BLENDER_BACKENDS
  ];
  return JSON.stringify(values.map((value) => String(value || "")));
}

function detectAccelerationProfile(env = process.env) {
  const key = cacheKeyFromEnv(env);
  if (PROFILE_CACHE.has(key)) {
    return PROFILE_CACHE.get(key);
  }

  const mode = parseAccelerationMode(env.MAZZAP_ACCELERATION_MODE);
  const preferredVendor = toVendor(env.MAZZAP_GPU_VENDOR_PREFERENCE || "nvidia");
  const requestedIndex = parseInteger(env.MAZZAP_GPU_INDEX);

  const nvidia = detectNvidiaGpus(env);
  const lspciGpus = detectLspciGpus(env);

  const merged = dedupeGpus([
    ...nvidia.gpus,
    ...lspciGpus.filter((gpu) => gpu.vendor !== "nvidia" || !nvidia.available)
  ]);

  let profile;
  if (mode === "cpu") {
    profile = {
      mode: "cpu",
      reason: "Forced by MAZZAP_ACCELERATION_MODE",
      availableGpus: merged,
      preferredGpu: null,
      blender: {
        device: "CPU",
        backends: []
      }
    };
  } else {
    const preferredGpu = pickPreferredGpu(merged, preferredVendor, requestedIndex);
    if (!preferredGpu) {
      profile = {
        mode: "cpu",
        reason: "No supported GPU detected",
        availableGpus: merged,
        preferredGpu: null,
        blender: {
          device: "CPU",
          backends: []
        }
      };
    } else {
      const backendOverride = parseBlenderBackends(env.MAZZAP_BLENDER_BACKENDS);
      const backends = backendOverride.length > 0
        ? backendOverride
        : defaultBlenderBackendsForVendor(preferredGpu.vendor);

      profile = {
        mode: "gpu",
        reason: "Detected available GPU",
        availableGpus: merged,
        preferredGpu,
        blender: {
          device: "GPU",
          backends,
          gpuNameHint: preferredGpu.name,
          gpuIndex: Number.isInteger(preferredGpu.index) ? preferredGpu.index : null
        }
      };
    }
  }

  PROFILE_CACHE.set(key, profile);
  return profile;
}

function hasExistingEnvValue(env, key) {
  return Object.prototype.hasOwnProperty.call(env, key) && hasText(env[key]);
}

function buildAccelerationEnv(baseEnv = process.env) {
  const env = { ...baseEnv };
  const profile = detectAccelerationProfile(baseEnv);

  env.MAZZAP_ACCELERATION_MODE = profile.mode;
  env.MAZZAP_BLENDER_DEVICE = profile.blender.device;
  env.MAZZAP_BLENDER_BACKENDS = (profile.blender.backends || []).join(",");

  if (profile.preferredGpu) {
    env.MAZZAP_GPU_VENDOR = profile.preferredGpu.vendor;
    env.MAZZAP_GPU_NAME = profile.preferredGpu.name;
    if (Number.isInteger(profile.preferredGpu.index)) {
      env.MAZZAP_GPU_INDEX = String(profile.preferredGpu.index);
      env.MAZZAP_BLENDER_GPU_INDEX = String(profile.preferredGpu.index);
    }
    env.MAZZAP_BLENDER_GPU_NAME_HINT = profile.preferredGpu.name;
  }

  if (profile.mode === "gpu" && profile.preferredGpu && profile.preferredGpu.vendor === "nvidia") {
    const gpuIndex = Number.isInteger(profile.preferredGpu.index) ? String(profile.preferredGpu.index) : null;
    if (gpuIndex) {
      if (!hasExistingEnvValue(baseEnv, "CUDA_VISIBLE_DEVICES")) {
        env.CUDA_VISIBLE_DEVICES = gpuIndex;
      }
      if (!hasExistingEnvValue(baseEnv, "NVIDIA_VISIBLE_DEVICES")) {
        env.NVIDIA_VISIBLE_DEVICES = gpuIndex;
      }
      if (!hasExistingEnvValue(baseEnv, "GPU_DEVICE_ORDINAL")) {
        env.GPU_DEVICE_ORDINAL = gpuIndex;
      }
    }
    if (!hasExistingEnvValue(baseEnv, "__NV_PRIME_RENDER_OFFLOAD")) {
      env.__NV_PRIME_RENDER_OFFLOAD = "1";
    }
    if (!hasExistingEnvValue(baseEnv, "__GLX_VENDOR_LIBRARY_NAME")) {
      env.__GLX_VENDOR_LIBRARY_NAME = "nvidia";
    }
  }

  if (profile.mode === "gpu" && profile.preferredGpu && !profile.preferredGpu.integrated) {
    if (!hasExistingEnvValue(baseEnv, "DRI_PRIME")) {
      env.DRI_PRIME = "1";
    }
  }

  return env;
}

function formatAccelerationSummary(profile) {
  if (!profile || profile.mode !== "gpu" || !profile.preferredGpu) {
    const reason = profile && profile.reason ? profile.reason : "CPU fallback";
    return `mode=cpu (${reason})`;
  }

  const gpu = profile.preferredGpu;
  const index = Number.isInteger(gpu.index) ? ` index=${gpu.index}` : "";
  const memory = Number.isFinite(gpu.memoryMb) ? ` vram=${Math.round(gpu.memoryMb / 1024)}GiB` : "";
  const backends = (profile.blender && profile.blender.backends && profile.blender.backends.length > 0)
    ? ` blender=${profile.blender.backends.join("/")}`
    : "";

  return `mode=gpu vendor=${gpu.vendor}${index}${memory} name=${gpu.name}${backends}`;
}

module.exports = {
  buildAccelerationEnv,
  detectAccelerationProfile,
  formatAccelerationSummary
};
