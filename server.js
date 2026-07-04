#!/usr/bin/env node
'use strict';

// Minimal zero-dependency static server for a VEIL digital twin.
// Serves ./public (viewer) and ./data (bundled geospatial data). No database,
// no cloud, no build step. POST /api/chat additionally proxies the viewer's
// chat panel to OpenAI with the twin MCP server's tools (see "chat API"
// below) — still no npm dependencies.

const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const fsp = fs.promises;
const os = require('os');
const path = require('path');
const { URL } = require('url');
const { spawn, spawnSync } = require('child_process');

const ROOT = __dirname;
// A twin's data dir. Defaults to ./data; set TWIN_DATA_DIR to serve an
// alternate/scratch twin from the same checkout (its /data/* requests are
// served from there, /public is unchanged) without touching ./data.
const DATA_DIR = path.resolve(ROOT, process.env.TWIN_DATA_DIR || 'data');
const PORT = Number(process.env.PORT) || 4173;
const HOST = process.env.HOST || '127.0.0.1';

// Hosted mode keeps the local-first defaults intact, but lets one public Node
// process issue anonymous session cookies and provision an isolated data dir for
// each visitor. A template may be supplied, but hosted product deployments can
// also start from an empty data dir and let the visitor build a twin via /init.
const HOSTED_MODE = process.env.VEIL_HOSTED === '1';
const HOSTED_ROOT = path.resolve(ROOT, process.env.VEIL_HOSTED_ROOT || 'hosted');
const HOSTED_TEMPLATE_DATA_DIR = process.env.VEIL_HOSTED_TEMPLATE_DATA_DIR
  ? path.resolve(ROOT, process.env.VEIL_HOSTED_TEMPLATE_DATA_DIR)
  : null;
const HOSTED_COOKIE = 'veil_session';
const HOSTED_TWIN_ID = 'default';
const HOSTED_SECRET = process.env.VEIL_SESSION_SECRET
  || (HOSTED_MODE ? crypto.randomBytes(32).toString('hex') : 'local-dev-secret');
if (HOSTED_MODE && !process.env.VEIL_SESSION_SECRET) {
  console.warn('VEIL_HOSTED=1 without VEIL_SESSION_SECRET: hosted sessions will be invalidated on restart.');
}

// ---------------------------------------------------------------- chat API
// POST /api/chat lets the viewer's chat panel ask an LLM about the land.
// The LLM's tools ARE the twin MCP server: we spawn scripts/mcp_server.py
// once, speak JSON-RPC to it over stdio, and hand its tool catalog to
// OpenAI's function calling. Still zero npm dependencies (built-in fetch +
// child_process).

// Provider: 'openai' (hosted Responses API, default) or 'ollama' (a local model
// over Ollama's native /api/chat). CHAT_PROVIDER picks it; if unset but
// OLLAMA_MODEL is given, we infer 'ollama'. Ollama needs no key and nothing
// leaves the machine.
const CHAT_PROVIDER = (process.env.CHAT_PROVIDER
  || (process.env.OLLAMA_MODEL ? 'ollama' : 'openai')).toLowerCase();

const OPENAI_MODEL = process.env.OPENAI_MODEL || 'gpt-5.5';
const OPENAI_REASONING = process.env.OPENAI_REASONING_EFFORT || 'low';
// When set, the server never spends its own key: every /api/chat request must
// carry the caller's key (X-OpenAI-Key). Use this for any public deployment so
// strangers reaching the port bring their own quota, not yours.
const OPENAI_REQUIRE_USER_KEY = process.env.OPENAI_REQUIRE_USER_KEY === '1';

const OLLAMA_HOST = (process.env.OLLAMA_HOST || 'http://127.0.0.1:11434').replace(/\/+$/, '');
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || 'qwen3.6-27b-ud-q4-k-xl-no-mmproj';
// Context window for the local model. Default to the local Qwen 3.6 27B UD Q4_K_XL
// no-mmproj Ollama model at the 90.8k-token window chosen for stable VEIL
// tool-chat runs; set OLLAMA_MODEL / OLLAMA_NUM_CTX explicitly to override.
const OLLAMA_NUM_CTX = Number(process.env.OLLAMA_NUM_CTX) || 90800;
// Sampling temperature for the local model. 0 keeps agentic tool use deterministic
// and avoids truncated/malformed tool-call JSON or empty-answer turns; raise only
// if you want more varied prose.
const OLLAMA_TEMPERATURE = process.env.OLLAMA_TEMPERATURE !== undefined
  ? Number(process.env.OLLAMA_TEMPERATURE) : 0;

// The active chat model label (shown in the panel / echoed in responses).
const CHAT_MODEL = CHAT_PROVIDER === 'ollama' ? OLLAMA_MODEL : OPENAI_MODEL;

const MAX_TOOL_ROUNDS = 8; // OpenAI batches calls per round, so 8 is plenty
// Local reasoning models often emit ONE tool call per turn after thinking, so they
// need many more rounds to work through a multi-step question. A
// site-selection question (gather facts at 3 spots + draw 3 points) can spend
// ~16-20 single-call turns, so the budget has to clear that or the model gets cut
// off before it ever writes its answer.
const MAX_TOOL_ROUNDS_OLLAMA = Number(process.env.OLLAMA_MAX_TOOL_ROUNDS) || 32;
const MAX_PARSE_NUDGES = 4;
const OLLAMA_NEAR_TOOL_CAP_WARNING = 3;
const TOOL_RESULT_CAP = 60000; // chars per tool result sent to the model

function openaiKey() {
  if (process.env.OPENAI_API_KEY) return process.env.OPENAI_API_KEY.trim();
  try {
    return fs.readFileSync(path.join(ROOT, '.openai_key'), 'utf8').trim();
  } catch (_err) {
    return null;
  }
}

// -- minimal MCP stdio client (one persistent python child per twin) -------
const mcpClients = new Map(); // dataDir -> { proc, pending, nextId, tools, init }

// The chat MCP server and hydrology simulation need the geospatial stack
// (pyproj/GDAL/numpy). A bare `python3` — especially under the Hermes/GUI
// profile that launches the server — can resolve to an interpreter without it,
// so tools or scenarios fail with "No module named ...". Resolve a Python that
// has the stack: an explicit VEIL_MCP_PYTHON wins for MCP, else the project's
// dedicated .venv-mcp, else python3. Hydrology can be overridden separately with
// VEIL_HYDRO_PYTHON but otherwise uses the same stack.
function resolveMcpPython() {
  const explicit = (process.env.VEIL_MCP_PYTHON || '').trim();
  if (explicit) return explicit;
  const venv = path.join(ROOT, '.venv-mcp', 'bin', 'python');
  try {
    fs.accessSync(venv, fs.constants.X_OK);
    return venv;
  } catch (_err) { /* fall through */ }
  return 'python3';
}

const MCP_PYTHON = resolveMcpPython();
const HYDRO_PYTHON = (process.env.VEIL_HYDRO_PYTHON || '').trim() || MCP_PYTHON;

function mcpSpawn(dataDir = DATA_DIR) {
  const proc = spawn(MCP_PYTHON, [path.join(ROOT, 'scripts', 'mcp_server.py')], {
    env: { ...process.env, TWIN_DATA_DIR: dataDir },
    cwd: ROOT,
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  const state = { proc, pending: new Map(), nextId: 1, tools: null, buf: '', dataDir };
  proc.stdout.setEncoding('utf8');
  proc.stdout.on('data', (chunk) => {
    state.buf += chunk;
    let nl;
    while ((nl = state.buf.indexOf('\n')) >= 0) {
      const line = state.buf.slice(0, nl).trim();
      state.buf = state.buf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch (_err) { continue; }
      const waiter = state.pending.get(msg.id);
      if (!waiter) continue;
      state.pending.delete(msg.id);
      if (msg.error) waiter.reject(new Error(msg.error.message || 'MCP error'));
      else waiter.resolve(msg.result);
    }
  });
  proc.on('close', () => {
    state.pending.forEach((w) => w.reject(new Error('MCP server exited')));
    state.pending.clear();
    if (mcpClients.get(dataDir) === state) mcpClients.delete(dataDir); // respawn lazily
  });
  proc.on('error', (err) => {
    // spawn failure (e.g. a bad/missing interpreter) or a runtime process
    // error arrives as an async 'error' event the request try/catch can't see;
    // fail pending waiters and drop this instance instead of letting it become
    // an uncaughtException that takes down the whole static server.
    state.pending.forEach((w) => w.reject(err instanceof Error ? err : new Error(String(err))));
    state.pending.clear();
    if (mcpClients.get(dataDir) === state) mcpClients.delete(dataDir);
  });
  // A write to a dead child's stdin emits EPIPE on the writable stream; swallow
  // it so it never crashes the process (waiters are already failed above).
  proc.stdin.on('error', () => {});
  const writeLine = (obj) => {
    try {
      proc.stdin.write(JSON.stringify(obj) + '\n');
      return true;
    } catch (_err) {
      return false;
    }
  };
  state.request = (method, params) => new Promise((resolve, reject) => {
    const id = state.nextId++;
    state.pending.set(id, { resolve, reject });
    if (!writeLine({ jsonrpc: '2.0', id, method, params })) {
      state.pending.delete(id);
      reject(new Error('MCP server unavailable'));
      return;
    }
    setTimeout(() => {
      if (state.pending.delete(id)) reject(new Error(`MCP ${method} timed out`));
    }, 120000);
  });
  state.notify = (method, params) => {
    writeLine({ jsonrpc: '2.0', method, params });
  };
  state.init = (async () => {
    await state.request('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'veil-chat', version: '1.0' },
    });
    state.notify('notifications/initialized', {});
    const listed = await state.request('tools/list', {});
    state.tools = listed.tools || [];
  })();
  return state;
}

async function mcpReady() {
  return mcpReadyFor(DATA_DIR);
}

async function mcpReadyFor(dataDir = DATA_DIR) {
  let client = mcpClients.get(dataDir);
  if (!client) {
    client = mcpSpawn(dataDir);
    mcpClients.set(dataDir, client);
  }
  await client.init;
  return client;
}

async function mcpCall(name, args, dataDir = DATA_DIR) {
  const client = await mcpReadyFor(dataDir);
  const result = await client.request('tools/call', { name, arguments: args || {} });
  const text = (result.content || [])
    .filter((c) => c.type === 'text')
    .map((c) => c.text)
    .join('\n');
  return text || JSON.stringify(result);
}

// -- scope context: what the chat panel selected in the 3D scene ----------
// Keep this as coordinate context only. The model should first decide what
// evidence the question needs, then call identify/summarize/layer tools itself.
async function scopeContext(scope) {
  if (!scope || scope.type === 'all' || !scope.type) {
    return 'Scope: the whole twin. Use tools as needed; describe_place gives lightweight location and coordinate context.';
  }
  if (scope.type === 'region') {
    const region = { polygon: scope.polygon };
    return [
      'Scope: a region the user drew in the 3D viewer.',
      `Region object for spatial tools (scene-local meters): ${JSON.stringify(region)}`,
      'When the user says "here"/"this area", they mean this region. Pass this exact',
      'region object to find_entities / aggregate_entities / canopy_change / summarize_region.',
    ].join('\n');
  }
  if (scope.type === 'point') {
    const radius = Number(scope.radius_m) || 100;
    const point = scope.point;
    const region = { within_m: radius, point };
    return [
      'Scope: a single point the user picked in the 3D viewer.',
      `Point (scene-local meters): ${JSON.stringify(point)}`,
      `Region object for spatial tools (${radius} m around it): ${JSON.stringify(region)}`,
      'When the user says "here"/"this spot", they mean this point and its surroundings.',
    ].join('\n');
  }
  throw new Error(`unknown scope type: ${scope.type}`);
}

function cap(text, n = TOOL_RESULT_CAP) {
  return text.length > n ? `${text.slice(0, n)}\n…[truncated ${text.length - n} chars]` : text;
}

// Echo a bounded, structured view of a tool result into the returned trace so the
// benchmark evaluator can credit data the twin actually surfaced (not just what the
// model retyped). Parse JSON when it's small enough to carry safely; otherwise drop
// to a truncated marker so a huge payload can never bloat the chat response.
// Large enough that real tool payloads (identify_at pretty-prints to ~14 KB,
// describe_twin larger) are echoed whole so the evaluator can read their layer
// ids / facts; the marker only guards against a pathological multi-hundred-KB
// result bloating the response. This is the RETURNED trace, separate from the
// smaller TOOL_RESULT_CAP slice fed back to the model.
const TRACE_RESULT_CAP = 200000; // chars of tool result echoed into the returned trace
function traceResult(result) {
  const text = typeof result === 'string' ? result : String(result);
  if (text.length <= TRACE_RESULT_CAP) {
    try { return JSON.parse(text); } catch (_err) { return text; }
  }
  return { truncated: true, chars: text.length, preview: text.slice(0, 800) };
}

const JSON_FINALIZE_PROMPT = [
  'Using only facts already in this conversation, output EXACTLY ONE valid JSON object and',
  'nothing else. If you do not have a value, omit that key rather than invent it.',
  'Use concise machine-checkable claim keys and real units. Do not include markdown,',
  'code fences, or explanatory prose.',
].join(' ');

function hasContractJson(text) {
  return extractContractJsonCandidates(text) !== null;
}

function extractContractJsonCandidates(text) {
  const candidates = [];
  const raw = String(text || '');
  const blockMatches = raw.match(/```(?:json)?[\s\S]*?```/gi) || [];
  for (const block of blockMatches) {
    candidates.push(block.replace(/^```(?:json)?\s*/i, '').replace(/```$/, '').trim());
  }
  candidates.push(...scanBalancedJsonCandidates(raw));

  for (let i = candidates.length - 1; i >= 0; i -= 1) {
    const candidate = candidates[i];
    try {
      const parsed = JSON.parse(candidate);
      if (
        parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        && Object.prototype.hasOwnProperty.call(parsed, 'answer')
        && Object.prototype.hasOwnProperty.call(parsed, 'claims')
      ) {
        return parsed;
      }
    } catch (_err) {
      // keep scanning
    }
  }
  return null;
}

function looksLikeJsonRequest(text) {
  return /\bjson\b/i.test(String(text || ''));
}

function scanBalancedJsonCandidates(text) {
  const raw = String(text || '');
  const out = [];
  let inString = false;
  let stringQuote = '';
  let escaped = false;
  let depth = 0;
  let start = -1;
  for (let i = 0; i < raw.length; i += 1) {
    const ch = raw[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === '\\') {
        escaped = true;
      } else if (ch === stringQuote) {
        inString = false;
        stringQuote = '';
      }
      continue;
    }
    if (ch === '"' || ch === '\'') {
      inString = true;
      stringQuote = ch;
      continue;
    }
    if (ch === '{') {
      if (depth === 0) start = i;
      depth += 1;
      continue;
    }
    if (ch === '}' && depth > 0) {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        out.push(raw.slice(start, i + 1));
        start = -1;
      }
    }
  }
  return out;
}

function openaiOutputToText(output) {
  return (output || [])
    .filter((o) => o.type === 'message')
    .flatMap((o) => o.content || [])
    .filter((c) => c.type === 'output_text')
    .map((c) => c.text || '')
    .join('\n');
}

function drawPointCoordinateKey(point) {
  if (!point || typeof point !== 'object') return null;
  const x = Number.isFinite(Number(point.x)) ? Number(point.x)
    : Number.isFinite(Number(point.lon)) ? Number(point.lon)
      : Number.isFinite(Number(point.lng)) ? Number(point.lng)
        : Number.NaN;
  const y = Number.isFinite(Number(point.y)) ? Number(point.y)
    : Number.isFinite(Number(point.lat)) ? Number(point.lat)
      : Number.NaN;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return `${x.toFixed(6)}|${y.toFixed(6)}`;
}

function drawPointResultFlags(result) {
  const lower = String(result || '').toLowerCase();
  return {
    outside: /\boutside\b.*\b(aoi|boundary|parcel|property)\b/.test(lower),
    inside: /\binside\b.*\b(aoi|boundary|parcel|property)\b/.test(lower),
  };
}

function shouldAskForPointPlacement(userText) {
  const text = `${String(userText || '')}`.toLowerCase();
  return /\b(draw|place|mark|point|recommend|site|recommendation)\b/.test(text);
}

function pointPlacementHint({ userText, drawPoints, outsideCount }) {
  if (!shouldAskForPointPlacement(userText)) return null;
  if (!drawPoints.size && !/point|recommend|site|place|draw/.test(String(userText || '').toLowerCase())) return null;
  if (outsideCount > 0) return {
    message: 'Some points appear outside parcel scope. Use only coordinates from tool results that are inside the property before finalizing.',
  };
  return {
    message: 'For placement questions, keep draw_point coordinates distinct and in-property, and avoid reusing any coordinate.',
  };
}

function isEcologyIntent(text) {
  return /\b(ecology|ecological|habitat|species|wildlife|animal|animals|hunting|hunt|deer|turkey|bear|bird|birds|mammal|reptile|amphibian|fish|pollinator|wetland|wetlands|forest|forests|vegetation|plant|plants|invasive|native|conservation|biodiversity|land\s*cover|soil|soils|geology|natural[-\s]?resource)\b/i
    .test(String(text || ''));
}

function isEcologyLayerInspectionTool(name) {
  return new Set([
    'list_layers',
    'layer_summary',
    'identify_at',
    'sample_raster',
    'summarize_region',
    'filter_layer',
    'set_layer_visibility',
  ]).has(name);
}

function needsThematicLayerPreflight(name) {
  return new Set([
    'recommend_sites',
    'draw_point',
    'draw_polygon',
    'filter_layer',
    'set_layer_visibility',
  ]).has(name);
}

const ECOLOGY_PREFLIGHT_NUDGE = [
  'This request depends on thematic/geospatial evidence. First state what spatial',
  'evidence would ideally answer it, then inspect list_layers to see what this',
  'twin actually has. Layer ids may be unexpected; choose from available labels,',
  'descriptions, themes, fields, legends, and provenance. Use layer_summary on',
  'promising candidates before recommending, revealing layers, or drawing sites.',
].join(' ');

function ecologyPreflightToolResult() {
  return JSON.stringify({
    error: 'thematic_layer_preflight_required',
    message: ECOLOGY_PREFLIGHT_NUDGE,
    next_steps: [
      'Use list_layers to inspect the available catalog, including text_metadata descriptions.',
      'Use layer_summary for promising candidate layers, not just expected layer ids.',
      'Name missing evidence and any proxy layers before making recommendations.',
      'Call recommend_sites, filter_layer, or draw_point only after that preflight.',
    ],
  });
}

function ecologyDynamicInstructions(userText) {
  if (!isEcologyIntent(userText)) return '';
  return [
    '',
    'CURRENT REQUEST THEMATIC PREFLIGHT',
    '- The latest user request appears to depend on thematic/geospatial evidence',
    '  such as ecology, wildlife, habitat, vegetation, land cover, soils, geology,',
    '  wetlands, hydrology, hazards, access, or conservation.',
    '- First decide what evidence would ideally answer the question. Then inspect',
    '  list_layers to see what is actually available; use descriptions/metadata,',
    '  labels, themes, fields, legends, and provenance rather than expecting exact',
    '  layer ids.',
    '- Use layer_summary on promising layers, state missing evidence or proxy',
    '  choices, then filter/reveal layers, recommend sites, or draw points.',
  ].join('\n');
}

const CHAT_SYSTEM_PROMPT = [
  'You are the resident guide of a VEIL digital twin — a georeferenced 3D model of',
  'a real place with terrain, vegetation, buildings, soils, hydrology, land cover',
  'and habitat data. You answer questions about THIS land using the read-only twin',
  'tools, and you show places on the user\'s live 3D map.',
  '',
  'GROUNDING',
  '- Start by considering the nature of the user question and what spatial',
  '  evidence would ideally answer it. Do this before inspecting the layer',
  '  inventory. If you need location/coordinate context, call describe_place;',
  '  use describe_twin only when broader run history or inventory counts matter.',
  '- Every figure must come from a tool result — never invent or estimate numbers.',
  '  Keep each number\'s real unit (count, m, m², ha, acres, %); do not relabel a',
  '  percentage as an area or reuse one figure for unrelated things. If a tool did',
  '  not give you something, say so plainly instead of guessing.',
  '- Prefer aggregate_entities and summarize_region for counts and statistics; use',
  '  small limit values on find_entities. For water/runoff questions use the',
  '  hydrology tools (hydrology_at, hydrology_summary, run_scenario). Coordinates:',
  '  tools take {lat,lon} degrees or scene-local meters {x,y} (results echo both);',
  '  distances and heights are meters.',
  '',
  'QUESTION-FIRST LAYER DISCOVERY',
  '- For thematic, suitability, hazard, habitat, wetland, land-cover, soil,',
  '  geology, hydrology, access, or conservation questions, first identify the',
  '  ideal/helpful spatial evidence. Then call list_layers to inspect what this',
  '  twin actually has. Layer ids and names may be unfamiliar or incomplete;',
  '  choose from available descriptions/text_metadata, labels, themes, fields,',
  '  legend previews, provenance, and query/filter/drape flags.',
  '- After list_layers, call layer_summary for the promising candidate layers to',
  '  inspect labels, fields, legends, filterable values, and natural-language',
  '  metadata. Do not assume an expected layer id exists.',
  '- For species questions, look for any species/habitat/range layers in the',
  '  catalog. If GAP/species-habitat is present, use layer_summary or identify_at',
  '  results to find the exact species common name before filtering.',
  '- If a relevant layer is absent or empty, say that plainly and name what was',
  '  checked. If using a proxy layer, explain briefly why it is only a proxy.',
  '',
  'SHOW PLACES ON THE MAP — this is a hard rule, not optional:',
  'If the user asks for JSON, your FINAL message must be valid JSON only — no prose,',
  '  no markdown, and no fenced blocks.',
  '- STAY ON THE PROPERTY: every marker MUST fall inside the property boundary — the',
  '  parcel AOI. Scope the searches you draw from to region {"aoi": true} so you',
  '  anchor markers to features within the property, never the surrounding apron/area.',
  '  Only draw a point outside the parcel if the user EXPLICITLY asks to expand the',
  '  scope to the surrounding area.',
  '- Whenever your answer points at one or more specific places, you MUST call',
  '  draw_point (a spot) for EACH place, with a short label. Prefer draw_point',
  '  for exact spots; prefer filter_layer for mapped classes, habitat extents,',
  '  or any answer about where a condition/species/class is present.',
  '- Use REAL coordinates from a tool result (an entity\'s `position` or a feature\'s',
  '  centroid) — never invented numbers. When a place is a relevant feature (water,',
  '  an edge, a road), anchor the marker to that feature\'s coordinates.',
  '- Never write "I\'ll draw…", "I\'ll mark…", "let me plot…" or similar. Drawing is',
  '  a tool call you actually make, not a sentence you say. If you mentioned a place',
  '  but did not call a draw tool for it, you are not done.',
  '- Draw 2-3 places at most, each at a DIFFERENT location, each exactly once. Never',
  '  draw the same coordinate twice. Each marker must use a real coordinate from a',
  '  tool result and stay inside the property. Do NOT call clear_drawings (the user',
  '  clears the',
  '  map) and do not redraw or second-guess a marker you already placed.',
  '- Don\'t recite raw coordinates in prose — the marker shows the location. Just',
  '  name what you drew, then give your final written answer.',
  '',
  'REVEAL RELEVANT MAP LAYERS',
  '- When a layer directly supports your answer, show it on the map after you',
  '  have inspected the catalog/summary. Prefer',
  '  filter_layer over set_layer_visibility so the map reveals only the relevant',
  '  class, species, soil unit, geology type, wetland type, land-cover class, or',
  '  habitat cells.',
  '- For species questions, call filter_layer("gap_species_richness", [exact',
  '  species common name]) when that species is available. This is the preferred',
  '  way to show modeled habitat/range on the terrain.',
  '- For categorical raster layers, filter by legend class names from',
  '  layer_summary. For vector layers, filter by the relevant label or field',
  '  value from layer_summary.',
  '- Use draw_point only for specific recommended spots or sampled evidence',
  '  locations; use filter_layer when the answer is about an area, class, or',
  '  habitat extent.',
  '',
  'BE EFFICIENT — you have a limited number of tool turns:',
  '- Gather only the data you actually need, then commit to an answer. Do not repeat',
  '  a query you already ran or keep re-checking the same thing.',
  '- The normal arc is: a few read calls → one draw call per place → your final',
  '  written reply. End with the reply; don\'t keep calling tools after you can answer.',
  '',
  'ANSWER STYLE — the chat panel is small and renders ONLY **bold**, "- " bullet',
  'lines, and short paragraphs:',
  '- Do NOT use markdown tables, headings (#), or code blocks — they show up as raw',
  '  symbols. Use a one- or two-sentence lead answer, then at most a few "- "',
  '  bullets for the key facts, with **bold** on the essentials.',
  '- Be concise and conversational. Do not narrate your tool calls or add a "how I',
  '  got the numbers" / methodology section. Name a data source (LiDAR, gSSURGO,',
  '  LANDFIRE, GAP…) only in a few words when it genuinely matters.',
].join('\n');

// GPT-5.5 function calling lives on the Responses API (chat/completions
// rejects tools+reasoning for it). Tool rounds chain via previous_response_id
// so reasoning state carries across calls.
async function openaiResponses(payload, apiKey, signal = null) {
  const res = await fetch('https://api.openai.com/v1/responses', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify(payload),
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`OpenAI ${res.status}: ${detail.slice(0, 400)}`);
  }
  return res.json();
}

// Ollama's native /api/chat speaks the same tool/function-calling schema and,
// unlike its OpenAI-compatible endpoint, honors options.num_ctx — which we need
// to size the context window so the model fits the GPU. No auth, no network
// egress (the local daemon). Tool rounds carry state by replaying the growing
// message list (there is no previous_response_id here).
async function ollamaChat(payload, signal = null) {
  let res;
  try {
    res = await fetch(`${OLLAMA_HOST}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      ...(signal ? { signal } : {}),
    });
  } catch (err) {
    if (signal && signal.aborted) throw err; // client disconnected — propagate cleanly
    throw new Error(`cannot reach Ollama at ${OLLAMA_HOST} — is "ollama serve" running? (${err.message || err})`);
  }
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Ollama ${res.status}: ${detail.slice(0, 400)}`);
  }
  return res.json();
}

// --- provider runners: each takes the shared {history, toolDefs, instructions}
// and drives the tool loop in its provider's dialect, returning {reply, trace}.

function collectLayerRefs(value, out = new Map(), depth = 0) {
  if (!value || depth > 5 || out.size >= 12) return out;
  if (typeof value === 'string') {
    const s = value.trim();
    if (s && /^[a-z0-9][a-z0-9_.:-]{2,}$/i.test(s) && /layer|soil|land|geo|wetland|hydro|trail|habitat|cover|forest|vegetation|evt|nwi|ssurgo|gap/i.test(s)) {
      out.set(s, s);
    }
    return out;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectLayerRefs(item, out, depth + 1);
    return out;
  }
  if (typeof value !== 'object') return out;

  const id = value.layer_id || value.layerId || value.layer;
  const label = value.layer_label || value.label || value.name;
  if (typeof id === 'string' && id.trim()) {
    out.set(id.trim(), typeof label === 'string' && label.trim() ? `${id.trim()} — ${label.trim()}` : id.trim());
  }
  for (const [key, child] of Object.entries(value)) {
    if (/source_path|store_path|path|geometry|coordinates|features/i.test(key)) continue;
    collectLayerRefs(child, out, depth + 1);
    if (out.size >= 12) break;
  }
  return out;
}

function layerRefsFromToolActivity(args, result) {
  const out = collectLayerRefs(args);
  const text = typeof result === 'string' ? result : JSON.stringify(result || '');
  if (text && text.length <= TRACE_RESULT_CAP) {
    try { collectLayerRefs(JSON.parse(text), out); } catch (_err) { /* plain text result */ }
  }
  return Array.from(out.values()).slice(0, 8);
}

async function runOpenAI({ history, toolDefs, instructions, apiKey, dataDir = DATA_DIR, onProgress = null, signal = null }) {
  const tools = toolDefs.map((t) => ({
    type: 'function', name: t.name, description: t.description, parameters: t.parameters,
  }));
  const trace = [];
  let reply = null;
  let previousId = null;
  let input = history;
  const userText = history[history.length - 1] && history[history.length - 1].content || '';
  const wantsJson = looksLikeJsonRequest(userText);
  const ecologyRequest = isEcologyIntent(userText);
  let ecologyLayerChecked = false;
  let ecologyPreflightNudged = false;
  for (let round = 0; round <= MAX_TOOL_ROUNDS; round += 1) {
    if (signal?.aborted) break; // client disconnected — stop spending on the loop
    const data = await openaiResponses({
      model: OPENAI_MODEL,
      reasoning: { effort: OPENAI_REASONING },
      instructions,
      tools,
      input,
      ...(previousId ? { previous_response_id: previousId } : {}),
    }, apiKey, signal);
    previousId = data.id;
    const calls = (data.output || []).filter((o) => o.type === 'function_call');
    if (!calls.length) {
      reply = openaiOutputToText(data.output);
      break;
    }
    input = [];
    for (const call of calls) {
      let args = {};
      try { args = JSON.parse(call.arguments || '{}'); } catch (_err) { /* ignore */ }
      onProgress?.({ type: 'tool_call', tool: call.name, args, layers: layerRefsFromToolActivity(args, null) });
      let result;
      if (ecologyRequest && needsThematicLayerPreflight(call.name) && !ecologyLayerChecked && !ecologyPreflightNudged) {
        ecologyPreflightNudged = true;
        result = ecologyPreflightToolResult();
      } else {
        try {
          result = await mcpCall(call.name, args, dataDir);
        } catch (err) {
          result = JSON.stringify({ error: String(err.message || err) });
        }
        if (isEcologyLayerInspectionTool(call.name)) ecologyLayerChecked = true;
      }
      onProgress?.({ type: 'tool_result', tool: call.name, layers: layerRefsFromToolActivity(args, result) });
      trace.push({ tool: call.name, args, result: traceResult(result) });
      input.push({ type: 'function_call_output', call_id: call.call_id, output: cap(result) });
    }
  }
  if (reply === null) reply = '(stopped after too many tool calls — try a narrower question)';
  if (!signal?.aborted && wantsJson && !hasContractJson(reply)) {
    try {
      const finalizePayload = {
        model: OPENAI_MODEL,
        reasoning: { effort: OPENAI_REASONING },
        instructions: `${instructions}\n\n${JSON_FINALIZE_PROMPT}`,
        input: [...input, { type: 'message', role: 'user', content: JSON_FINALIZE_PROMPT }],
        ...(previousId ? { previous_response_id: previousId } : {}),
      };
      const finalData = await openaiResponses(finalizePayload, apiKey, signal);
      const finalText = openaiOutputToText(finalData.output).trim();
      if (finalText) {
        reply = finalText;
        trace.push({
          finalize: {
            provider: 'openai',
            tools_disabled: true,
            requested_json: wantsJson,
            reason: 'json_contract',
          },
        });
      }
    } catch (_err) {
      if (process.env.OLLAMA_DEBUG) {
        console.error('openai finalize JSON pass failed:', _err && _err.message ? _err.message : String(_err));
      }
    }
  }
  return { reply, trace };
}

async function runOllama({ history, toolDefs, instructions, dataDir = DATA_DIR, onProgress = null, signal = null }) {
  const tools = toolDefs.map((t) => ({
    type: 'function',
    function: { name: t.name, description: t.description, parameters: t.parameters },
  }));
  const messages = [{ role: 'system', content: instructions }, ...history];
  const trace = [];
  const userText = history[history.length - 1].content || '';
  const wantsJson = looksLikeJsonRequest(userText);
  const ecologyRequest = isEcologyIntent(userText);
  let ecologyLayerChecked = false;
  let ecologyPreflightNudged = false;
  const drawPoints = new Set();
  let outsideCount = 0;
  let nearCapNudged = false;
  let pointHintNudged = false;
  let reply = null;
  let lastContent = ''; // gpt-oss often writes its answer in a turn that ALSO calls a tool
  let nudges = 0;        // bounded recoveries from empty-final turns
  let parseNudges = 0;   // bounded recoveries from malformed tool-call parse errors
  for (let round = 0; round <= MAX_TOOL_ROUNDS_OLLAMA; round += 1) {
    if (signal?.aborted) break; // client disconnected — stop the tool loop
    if (!nearCapNudged && round >= MAX_TOOL_ROUNDS_OLLAMA - OLLAMA_NEAR_TOOL_CAP_WARNING) {
      nearCapNudged = true;
      const remaining = Math.max(0, MAX_TOOL_ROUNDS_OLLAMA - round);
      messages.push({
        role: 'user',
        content: `You have only about ${remaining} tool turns left. Stop gathering new evidence when ready, then output final JSON now if the user requested it.`,
      });
    }
    let data;
    try {
      data = await ollamaChat({
        model: OLLAMA_MODEL,
        messages,
        tools,
        stream: false,
        options: { num_ctx: OLLAMA_NUM_CTX, temperature: OLLAMA_TEMPERATURE },
      }, signal);
    } catch (err) {
      // gpt-oss intermittently emits invalid tool-call JSON; Ollama 500s on it.
      // Resampling at temp 0 just repeats the bad output, so instead feed back a
      // correction — that changes the context and breaks the deterministic path.
      if (/parsing tool call/i.test(String(err.message || err)) && parseNudges < MAX_PARSE_NUDGES) {
        parseNudges += 1;
        messages.push({ role: 'user', content: 'Your last tool call was not valid JSON. '
          + 'Re-issue it with complete, valid arguments — plain numbers like -86.3, never -86..275, for lat/lon or x/y and a short string label. '
          + 'If you already have what you need, stop calling tools and write your final answer now.' });
        if (process.env.OLLAMA_DEBUG) console.error(`[r${round}] tool-call parse error -> nudge ${parseNudges}`);
        continue;
      }
      if (/parsing tool call/i.test(String(err.message || err))) {
        trace.push({
          tool_call_parse_error: {
            round,
            message: String(err.message || err).slice(0, 300),
          },
        });
        if (process.env.OLLAMA_DEBUG) console.error(`[r${round}] tool-call parse error budget exhausted`);
        break;
      }
      throw err;
    }
    const msg = data.message || {};
    const calls = msg.tool_calls || [];
    if (msg.content && msg.content.trim()) lastContent = msg.content;
    if (process.env.OLLAMA_DEBUG) console.error(`[r${round}] done=${data.done_reason} content=${(msg.content||'').length} thinking=${(msg.thinking||'').length} calls=${calls.length} [${calls.map((c)=>c.function&&c.function.name).join(',')}]`);
    if (!calls.length) {
      // A turn with no tool call should carry the final answer. gpt-oss sometimes
      // ends with an empty "final" channel — nudge it once to actually write it.
      if ((msg.content && msg.content.trim()) || lastContent.trim() || nudges >= 3) {
        reply = msg.content || '';
        break;
      }
      nudges += 1;
      messages.push({ role: 'user', content: 'Now write your final answer for the user, following the answer-style rules.' });
      if (process.env.OLLAMA_DEBUG) console.error(`[r${round}] empty final -> nudge ${nudges}`);
      continue;
    }
    // Echo the assistant's tool-call turn, then answer each call with a tool msg.
    messages.push({ role: 'assistant', content: msg.content || '', tool_calls: calls });
    for (const call of calls) {
      const fn = call.function || {};
      let args = {};
      try { args = typeof fn.arguments === 'string' ? JSON.parse(fn.arguments || '{}') : (fn.arguments || {}); }
      catch (_err) { /* ignore */ }
      onProgress?.({ type: 'tool_call', tool: fn.name, args, layers: layerRefsFromToolActivity(args, null) });
      let result;
      if (ecologyRequest && needsThematicLayerPreflight(fn.name) && !ecologyLayerChecked && !ecologyPreflightNudged) {
        ecologyPreflightNudged = true;
        result = ecologyPreflightToolResult();
      } else {
        try {
          result = await mcpCall(fn.name, args, dataDir);
        } catch (err) {
          result = JSON.stringify({ error: String(err.message || err) });
        }
        if (isEcologyLayerInspectionTool(fn.name)) ecologyLayerChecked = true;
      }
      onProgress?.({ type: 'tool_result', tool: fn.name, layers: layerRefsFromToolActivity(args, result) });
      trace.push({ tool: fn.name, args, result: traceResult(result) });
      messages.push({ role: 'tool', tool_call_id: call.id, content: cap(result) });
      if (fn.name === 'draw_point') {
        const pointKey = drawPointCoordinateKey(args.point);
        if (pointKey) drawPoints.add(pointKey);
        const drawFlags = drawPointResultFlags(result);
        if (drawFlags.outside) outsideCount += 1;
      }
    }
    if (!pointHintNudged && shouldAskForPointPlacement(userText)) {
      const hint = pointPlacementHint({ userText, drawPoints, outsideCount });
      if (hint) {
        pointHintNudged = true;
        messages.push({ role: 'user', content: hint.message });
      }
    }
  }
  if (!signal?.aborted && wantsJson && !hasContractJson(reply)) {
    try {
      const finalData = await ollamaChat({
        model: OLLAMA_MODEL,
        messages: [...messages, { role: 'user', content: JSON_FINALIZE_PROMPT }],
        stream: false,
        options: { num_ctx: OLLAMA_NUM_CTX, temperature: OLLAMA_TEMPERATURE },
      }, signal);
      const finalText = String((finalData.message || {}).content || '').trim();
      if (finalText) {
        reply = finalText;
        trace.push({
          finalize: {
            provider: 'ollama',
            tools_disabled: true,
            requested_json: wantsJson,
            reason: 'json_contract',
          },
        });
      }
    } catch (err) {
      if (process.env.OLLAMA_DEBUG) console.error('[ollama] finalize JSON pass failed:', err && err.message ? err.message : String(err));
    }
  }
  // Recover the answer the model wrote alongside a tool call, or before it ran
  // out of rounds mid-drawing, rather than returning a blank bubble.
  if (reply === null) reply = '';
  if (!reply.trim()) reply = lastContent;
  if (!reply.trim()) reply = '(stopped before answering — try a narrower question)';
  return { reply, trace };
}

// Generous cap for a chat turn (long pasted context still fits) while keeping
// the body bounded so a slow/huge upload can't OOM the static server.
const CHAT_MAX_BODY = 4 * 1024 * 1024;

async function handleChat(req, res) {
  readBodyJson(req, CHAT_MAX_BODY, async (bodyErr, parsed) => {
    let keepAlive = null;
    const abort = new AbortController();
    const wantsStream = /\bapplication\/x-ndjson\b/i.test(String(req.headers.accept || ''))
      || req.headers['x-veil-stream'] === '1';
    const writeStreamEvent = (event) => {
      if (!wantsStream || res.writableEnded || res.destroyed) return;
      res.write(`${JSON.stringify(event)}\n`);
    };
    const startJsonResponse = () => {
      if (res.headersSent) return;
      res.writeHead(200, {
        'Content-Type': wantsStream ? 'application/x-ndjson; charset=utf-8' : 'application/json',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
      });
      // Long local-model tool loops can be quiet for minutes while still doing
      // useful work. A leading whitespace heartbeat keeps browsers/proxies from
      // treating the pending JSON response as a dead network request.
      keepAlive = setInterval(() => {
        if (!res.destroyed && !res.writableEnded) {
          if (wantsStream) res.write(JSON.stringify({ type: 'pulse' }) + '\n');
          else res.write(' ');
        }
      }, 15000);
      keepAlive.unref?.();
      if (wantsStream) writeStreamEvent({ type: 'status', message: 'lifting the VEIL' });
      else res.write(' ');
    };
    const finishJson = (status, payload) => {
      if (keepAlive) {
        clearInterval(keepAlive);
        keepAlive = null;
      }
      if (wantsStream && res.headersSent) {
        if (!res.writableEnded) {
          res.write(`${JSON.stringify({ type: status >= 400 ? 'error' : 'final', ...payload })}\n`);
          res.end();
        }
        return;
      }
      const json = JSON.stringify(payload);
      if (res.headersSent) {
        if (!res.writableEnded) res.end(json);
      } else {
        send(res, status, json, { 'Content-Type': 'application/json' });
      }
    };
    res.on('close', () => {
      if (keepAlive) {
        clearInterval(keepAlive);
        keepAlive = null;
      }
      // Client hung up before we finished: abort the in-flight LLM request and
      // stop the tool loop so a disconnect doesn't keep spending tokens / work.
      if (!res.writableEnded) abort.abort();
    });
    try {
      const dataDir = requestDataDir(req, res);
      if (bodyErr) {
        const tooLarge = /too large/i.test(String(bodyErr.message || ''));
        return finishJson(tooLarge ? 413 : 400, {
          error: tooLarge ? 'request body too large' : 'invalid JSON body',
        });
      }
      // OpenAI needs a key (BYOK header wins, else server key); Ollama is local
      // and keyless.
      let apiKey = null;
      if (CHAT_PROVIDER === 'openai') {
        const userKey = String(req.headers['x-openai-key'] || '').trim();
        apiKey = userKey || (OPENAI_REQUIRE_USER_KEY ? null : openaiKey());
        if (!apiKey) {
          return finishJson(400, {
            error: OPENAI_REQUIRE_USER_KEY
              ? 'This twin requires your own OpenAI key — click "Key" in the chat panel to add one.'
              : 'No OpenAI key: click "Key" in the chat panel to add yours, or set OPENAI_API_KEY / .openai_key on the server.',
          });
        }
      }
      const { messages = [], scope = { type: 'all' } } = parsed || {};
      const history = messages
        .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.content)
        .slice(-30)
        .map((m) => ({ role: m.role, content: String(m.content) }));
      if (!history.length || history[history.length - 1].role !== 'user') {
        return finishJson(400, { error: 'last message must be from the user' });
      }

      startJsonResponse();
      const client = await mcpReadyFor(dataDir);
      const toolDefs = client.tools.map((t) => ({
        name: t.name, description: t.description, parameters: t.inputSchema,
      }));
      const latestUserText = history[history.length - 1].content;
      const instructions = [
        CHAT_SYSTEM_PROMPT,
        ecologyDynamicInstructions(latestUserText),
        await scopeContext(scope),
      ].filter(Boolean).join('\n\n');

      const { reply, trace } = CHAT_PROVIDER === 'ollama'
        ? await runOllama({ history, toolDefs, instructions, dataDir, onProgress: writeStreamEvent, signal: abort.signal })
        : await runOpenAI({ history, toolDefs, instructions, apiKey, dataDir, onProgress: writeStreamEvent, signal: abort.signal });

      finishJson(200, { reply, trace, model: CHAT_MODEL, provider: CHAT_PROVIDER });
    } catch (err) {
      if (abort.signal.aborted || (err && err.name === 'AbortError')) {
        // client disconnected mid-flight — nothing to send back
        if (keepAlive) { clearInterval(keepAlive); keepAlive = null; }
        return;
      }
      console.error('chat error:', err);
      finishJson(502, { error: String(err.message || err) });
    }
  });
}

// Lets the viewer tailor the chat UI to the active provider (e.g. hide the
// OpenAI "Key" button when running on a local model).
function handleChatConfig(res) {
  send(res, 200, JSON.stringify({
    provider: CHAT_PROVIDER,
    model: CHAT_MODEL,
    num_ctx: CHAT_PROVIDER === 'ollama' ? OLLAMA_NUM_CTX : null,
    needs_key: CHAT_PROVIDER === 'openai',
  }), { 'Content-Type': 'application/json' });
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.geojson': 'application/geo+json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.tif': 'image/tiff',
  '.tiff': 'image/tiff',
  '.ico': 'image/x-icon',
  '.obj': 'text/plain; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.zip': 'application/zip',
};

function send(res, status, body, headers = {}) {
  res.writeHead(status, { 'Cache-Control': 'no-cache', ...headers });
  res.end(body);
}

// -------------------------------------------------------------- hosted mode
function hmac(text) {
  return crypto.createHmac('sha256', HOSTED_SECRET).update(text).digest('base64url');
}

function parseCookies(header) {
  const out = {};
  String(header || '').split(';').forEach((part) => {
    const idx = part.indexOf('=');
    if (idx < 0) return;
    const key = part.slice(0, idx).trim();
    if (!key) return;
    out[key] = decodeURIComponent(part.slice(idx + 1).trim());
  });
  return out;
}

function signSessionId(id) {
  return `${id}.${hmac(id)}`;
}

function verifySessionCookie(value) {
  const text = String(value || '');
  const idx = text.lastIndexOf('.');
  if (idx <= 0) return null;
  const id = text.slice(0, idx);
  const sig = text.slice(idx + 1);
  if (!/^[a-zA-Z0-9_-]{24,80}$/.test(id)) return null;
  const expected = hmac(id);
  const left = Buffer.from(sig);
  const right = Buffer.from(expected);
  if (left.length !== right.length || !crypto.timingSafeEqual(left, right)) return null;
  return id;
}

function hostedCookieHeader(req, sessionId) {
  const secure = process.env.VEIL_COOKIE_SECURE === '1'
    || String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim() === 'https';
  return [
    `${HOSTED_COOKIE}=${encodeURIComponent(signSessionId(sessionId))}`,
    'Path=/',
    'HttpOnly',
    'SameSite=Lax',
    secure ? 'Secure' : '',
    'Max-Age=31536000',
  ].filter(Boolean).join('; ');
}

function hostedUserId(req, res) {
  const cookies = parseCookies(req.headers.cookie);
  let id = verifySessionCookie(cookies[HOSTED_COOKIE]);
  if (!id) {
    id = crypto.randomBytes(24).toString('base64url');
    res.setHeader('Set-Cookie', hostedCookieHeader(req, id));
  }
  return id;
}

function copyHostedTemplate(dest) {
  if (!HOSTED_TEMPLATE_DATA_DIR || !fs.existsSync(HOSTED_TEMPLATE_DATA_DIR)) return false;
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.cpSync(HOSTED_TEMPLATE_DATA_DIR, dest, {
    recursive: true,
    filter: (src) => {
      const name = path.basename(src);
      return !name.startsWith('.') && name !== 'live';
    },
  });
  return true;
}

function requestDataDir(req, res) {
  if (!HOSTED_MODE) return DATA_DIR;
  const userId = hostedUserId(req, res);
  const dir = path.join(HOSTED_ROOT, 'users', userId, 'twins', HOSTED_TWIN_ID, 'data');
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
    copyHostedTemplate(dir);
  }
  return dir;
}

function twinBuilt(dataDir) {
  return fs.existsSync(path.join(dataDir, 'georef.json'))
    && fs.existsSync(path.join(dataDir, 'terrain', 'grid.json'));
}

function hostedStatusPayload(req, res) {
  const dataDir = requestDataDir(req, res);
  return {
    hosted: HOSTED_MODE,
    twin_id: HOSTED_TWIN_ID,
    data_dir: HOSTED_MODE ? path.relative(ROOT, dataDir) : path.relative(ROOT, DATA_DIR),
    template: HOSTED_MODE && HOSTED_TEMPLATE_DATA_DIR ? path.relative(ROOT, HOSTED_TEMPLATE_DATA_DIR) : null,
    built: twinBuilt(dataDir),
  };
}

// ---------------------------------------------------------------- init API
// npm run init starts this server with VEIL_INIT=1 and serves /init.html. The
// setup page posts a drawn WGS84 polygon plus a display name here; the server
// writes the AOI into the active data dir and runs the same national builder as
// the CLI path, passing the name through to ingest_dem.py.

const INIT_MAX_BODY = 256 * 1024;
const INIT_DISK_PROBE_TIMEOUT_MS = 1500;
const INIT_TOOL_PROBE_TIMEOUT_MS = 3000;
const INIT_ESTIMATE_SAMPLE_DIR = path.join(ROOT, 'twins', 'init', 'data');
const INIT_ESTIMATE_SAMPLE_AOI = path.join(INIT_ESTIMATE_SAMPLE_DIR, 'init', 'aoi.geojson');
const INIT_ESTIMATE_FALLBACK_BYTES_PER_KM2 = 1536 * 1024 * 1024;
const INIT_ESTIMATE_BASE_BYTES = 768 * 1024 * 1024;
const INIT_ESTIMATE_MIN_FREE_BYTES = 1024 * 1024 * 1024;
const INIT_ESTIMATE_RECOMMENDED_FREE_BYTES = 2 * 1024 * 1024 * 1024;
const INIT_SYSTEM_LOW_DISK_BYTES = 10 * 1024 * 1024 * 1024;
const INIT_SYSTEM_LOW_FREE_MEMORY_BYTES = 1024 * 1024 * 1024;
const INIT_SYSTEM_LOW_TOTAL_MEMORY_BYTES = 8 * 1024 * 1024 * 1024;
const initJob = {
  status: 'idle',
  running: false,
  logs: [],
  name: null,
  started_at: null,
  finished_at: null,
  exit_code: null,
};
const hostedInitJobs = new Map();

function createInitJob() {
  return {
    status: 'idle',
    running: false,
    logs: [],
    name: null,
    started_at: null,
    finished_at: null,
    exit_code: null,
  };
}

function initJobFor(req, res) {
  if (!HOSTED_MODE) return initJob;
  const dataDir = requestDataDir(req, res);
  let job = hostedInitJobs.get(dataDir);
  if (!job) {
    job = createInitJob();
    hostedInitJobs.set(dataDir, job);
  }
  return job;
}

function initModeEnabled() {
  return HOSTED_MODE || process.env.VEIL_INIT === '1';
}

function bytesLabel(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return 'unknown';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = n;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return idx === 0 ? `${Math.round(value)} ${units[idx]}` : `${value.toFixed(1)} ${units[idx]}`;
}

function nearestExistingPath(targetPath) {
  let current = path.resolve(targetPath || ROOT);
  for (;;) {
    try {
      if (fs.existsSync(current)) return current;
    } catch (_err) { /* keep walking up */ }
    const parent = path.dirname(current);
    if (parent === current) return ROOT;
    current = parent;
  }
}

function spawnCapture(command, args, options = {}) {
  const timeoutMs = Number(options.timeoutMs) || 3000;
  const maxOutput = Number(options.maxOutput) || 16000;
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, {
        cwd: options.cwd || ROOT,
        env: options.env || process.env,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      resolve({ ok: false, error: err.message || String(err), stdout: '', stderr: '' });
      return;
    }
    let done = false;
    let stdout = '';
    let stderr = '';
    const finish = (result) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({
        stdout,
        stderr,
        ...result,
      });
    };
    const append = (which, chunk) => {
      const text = String(chunk || '');
      if (!text) return;
      if (which === 'stdout') stdout = (stdout + text).slice(-maxOutput);
      else stderr = (stderr + text).slice(-maxOutput);
    };
    const timer = setTimeout(() => {
      try { child.kill('SIGTERM'); } catch (_err) { /* ignore */ }
      finish({ ok: false, timed_out: true, error: `${path.basename(command)} timed out` });
    }, timeoutMs);
    child.stdout.on('data', (chunk) => append('stdout', chunk));
    child.stderr.on('data', (chunk) => append('stderr', chunk));
    child.on('error', (err) => finish({ ok: false, error: err.message || String(err) }));
    child.on('close', (code) => finish({
      ok: code === 0,
      code,
      error: code === 0 ? null : ((stderr || stdout).trim().split('\n')[0] || `${path.basename(command)} exited ${code}`),
    }));
  });
}

function statfsDiskInfo(targetPath) {
  const checkPath = nearestExistingPath(targetPath);
  if (typeof fs.statfsSync !== 'function') {
    return { ok: false, path: checkPath, error: 'fs.statfs is not available' };
  }
  try {
    const stats = fs.statfsSync(checkPath);
    const blockSize = Number(stats.bsize);
    const blocks = Number(stats.blocks);
    const available = Number(stats.bavail);
    if (![blockSize, blocks, available].every((n) => Number.isFinite(n) && n >= 0)) {
      throw new Error('statfs returned invalid block counts');
    }
    return {
      ok: true,
      method: 'statfs',
      path: checkPath,
      free_bytes: Math.max(0, Math.floor(available * blockSize)),
      total_bytes: Math.max(0, Math.floor(blocks * blockSize)),
    };
  } catch (err) {
    return { ok: false, path: checkPath, error: err.message || String(err) };
  }
}

async function dfDiskInfo(targetPath) {
  const checkPath = nearestExistingPath(targetPath);
  const result = await spawnCapture('df', ['-kP', checkPath], {
    timeoutMs: INIT_DISK_PROBE_TIMEOUT_MS,
    maxOutput: 4000,
  });
  if (!result.ok) {
    return {
      ok: false,
      method: 'df',
      path: checkPath,
      error: result.error || 'df failed',
    };
  }
  const lines = result.stdout.trim().split('\n').filter(Boolean);
  const parts = (lines[lines.length - 1] || '').trim().split(/\s+/);
  const totalKb = Number(parts[1]);
  const freeKb = Number(parts[3]);
  if (!Number.isFinite(totalKb) || !Number.isFinite(freeKb)) {
    return {
      ok: false,
      method: 'df',
      path: checkPath,
      error: 'df returned unparseable disk numbers',
    };
  }
  return {
    ok: true,
    method: 'df',
    path: checkPath,
    free_bytes: Math.max(0, Math.floor(freeKb * 1024)),
    total_bytes: Math.max(0, Math.floor(totalKb * 1024)),
  };
}

async function diskInfoForDataDir(dataDir) {
  const statfs = statfsDiskInfo(dataDir);
  if (statfs.ok) return statfs;
  const df = await dfDiskInfo(dataDir);
  if (df.ok) return { ...df, statfs_error: statfs.error || null };
  return {
    ok: false,
    method: 'unavailable',
    path: nearestExistingPath(dataDir),
    free_bytes: null,
    total_bytes: null,
    error: df.error || statfs.error || 'disk probe failed',
  };
}

async function pythonProbe() {
  const result = await spawnCapture(MCP_PYTHON, ['--version'], {
    timeoutMs: INIT_TOOL_PROBE_TIMEOUT_MS,
    maxOutput: 2000,
  });
  const version = (result.stdout || result.stderr || '').trim().split('\n')[0] || null;
  return {
    ok: !!result.ok,
    executable: MCP_PYTHON,
    version,
    error: result.ok ? null : (result.error || 'python probe failed'),
  };
}

async function gdalProbe() {
  const result = await spawnCapture(MCP_PYTHON, [
    '-c',
    'from osgeo import gdal; print(gdal.__version__)',
  ], {
    timeoutMs: INIT_TOOL_PROBE_TIMEOUT_MS,
    maxOutput: 2000,
  });
  const version = (result.stdout || '').trim().split('\n')[0] || null;
  return {
    ok: !!result.ok,
    version,
    error: result.ok ? null : (result.error || 'GDAL probe failed'),
  };
}

function memoryInfo() {
  return {
    ok: true,
    free_bytes: os.freemem(),
    total_bytes: os.totalmem(),
  };
}

function systemCheckWarnings(payload) {
  const warnings = [];
  const disk = payload.disk || {};
  const memory = payload.memory || {};
  const tools = payload.tools || {};
  if (!Number.isFinite(Number(disk.free_bytes))) {
    warnings.push('Disk free space could not be checked; avoid starting a large AOI build until the volume is verified.');
  } else if (disk.free_bytes < INIT_SYSTEM_LOW_DISK_BYTES) {
    warnings.push(`Only ${bytesLabel(disk.free_bytes)} is free on the twin data volume; choose a smaller AOI if the estimate is close to this.`);
  }
  if (Number.isFinite(Number(memory.total_bytes)) && memory.total_bytes < INIT_SYSTEM_LOW_TOTAL_MEMORY_BYTES) {
    warnings.push(`This machine reports ${bytesLabel(memory.total_bytes)} total RAM; large AOIs can exhaust memory, so start with a smaller AOI.`);
  } else if (Number.isFinite(Number(memory.free_bytes)) && memory.free_bytes < INIT_SYSTEM_LOW_FREE_MEMORY_BYTES) {
    warnings.push(`Only ${bytesLabel(memory.free_bytes)} RAM is currently free; close other apps or choose a smaller AOI before building.`);
  }
  if (tools.python && !tools.python.ok) {
    warnings.push(`Python probe failed for ${tools.python.executable || MCP_PYTHON}; the builder cannot run until Python is fixed.`);
  }
  if (tools.gdal && !tools.gdal.ok) {
    warnings.push('GDAL Python bindings are not available; install/fix osgeo.gdal before building a twin.');
  }
  return warnings;
}

async function initSystemCheckPayload(dataDir) {
  const [disk, python, gdal] = await Promise.all([
    diskInfoForDataDir(dataDir),
    pythonProbe(),
    gdalProbe(),
  ]);
  const payload = {
    ok: true,
    data_dir: path.relative(ROOT, dataDir) || '.',
    disk,
    memory: memoryInfo(),
    tools: {
      python,
      gdal,
      node: { ok: true, version: process.version },
    },
    warnings: [],
  };
  payload.warnings = systemCheckWarnings(payload);
  return payload;
}

function featureCollectionPolygonCoordinates(doc) {
  const feature = doc && Array.isArray(doc.features) ? doc.features[0] : null;
  const geom = feature && feature.geometry ? feature.geometry : doc && doc.type ? doc : null;
  if (!geom || geom.type !== 'Polygon' || !Array.isArray(geom.coordinates) || !Array.isArray(geom.coordinates[0])) {
    return null;
  }
  return normalizeInitCoordinates(geom.coordinates[0]);
}

function aoiAreaKm2(coordinates) {
  const ring = normalizeInitCoordinates(coordinates);
  if (ring.length < 4) return 0;
  const earthRadiusM = 6371008.8;
  let sum = 0;
  for (let i = 0; i < ring.length - 1; i += 1) {
    const lon1 = ring[i][0] * Math.PI / 180;
    const lat1 = ring[i][1] * Math.PI / 180;
    const lon2 = ring[i + 1][0] * Math.PI / 180;
    const lat2 = ring[i + 1][1] * Math.PI / 180;
    sum += (lon2 - lon1) * (2 + Math.sin(lat1) + Math.sin(lat2));
  }
  return Math.abs(sum * earthRadiusM * earthRadiusM / 2) / 1e6;
}

function directorySizeBytesSync(dirPath, budget = { entries: 0, maxEntries: 200000 }) {
  let stat;
  try {
    stat = fs.lstatSync(dirPath);
  } catch (_err) {
    return null;
  }
  budget.entries += 1;
  if (budget.entries > budget.maxEntries) return null;
  if (stat.isSymbolicLink()) return 0;
  if (stat.isFile()) return stat.size;
  if (!stat.isDirectory()) return 0;
  let total = 0;
  let entries;
  try {
    entries = fs.readdirSync(dirPath, { withFileTypes: true });
  } catch (_err) {
    return null;
  }
  for (const entry of entries) {
    const childSize = directorySizeBytesSync(path.join(dirPath, entry.name), budget);
    if (childSize === null) return null;
    total += childSize;
  }
  return total;
}

let initEstimateCalibrationCache = null;
function initEstimateCalibration() {
  if (initEstimateCalibrationCache) return initEstimateCalibrationCache;
  let measured = null;
  try {
    const doc = readJsonFile(INIT_ESTIMATE_SAMPLE_AOI, null);
    const coordinates = featureCollectionPolygonCoordinates(doc);
    const areaKm2 = coordinates ? aoiAreaKm2(coordinates) : 0;
    const bytes = directorySizeBytesSync(INIT_ESTIMATE_SAMPLE_DIR);
    if (Number.isFinite(areaKm2) && areaKm2 > 0 && Number.isFinite(bytes) && bytes > 0) {
      measured = {
        source: path.relative(ROOT, INIT_ESTIMATE_SAMPLE_DIR),
        aoi: path.relative(ROOT, INIT_ESTIMATE_SAMPLE_AOI),
        sample_area_km2: areaKm2,
        sample_bytes: bytes,
        bytes_per_km2: Math.ceil(bytes / areaKm2),
      };
    }
  } catch (_err) { /* fall back below */ }
  const bytesPerKm2 = Math.max(
    measured ? measured.bytes_per_km2 : 0,
    INIT_ESTIMATE_FALLBACK_BYTES_PER_KM2,
  );
  initEstimateCalibrationCache = {
    base_bytes: INIT_ESTIMATE_BASE_BYTES,
    bytes_per_km2: bytesPerKm2,
    fallback_bytes_per_km2: INIT_ESTIMATE_FALLBACK_BYTES_PER_KM2,
    measured,
  };
  return initEstimateCalibrationCache;
}

function estimateAdvisory({ areaKm2, estBytes, diskFreeBytes, projectedFreeBytes, blocked }) {
  if (!Number.isFinite(Number(diskFreeBytes))) {
    return 'Disk free space could not be checked. Keep the AOI small until the twin data volume is verified.';
  }
  if (blocked) {
    if (estBytes > diskFreeBytes) {
      return `This ~${areaKm2.toFixed(areaKm2 < 1 ? 3 : 1)} km^2 AOI needs about ${bytesLabel(estBytes)}, but only ${bytesLabel(diskFreeBytes)} is free. Choose a smaller AOI before building.`;
    }
    return `This ~${areaKm2.toFixed(areaKm2 < 1 ? 3 : 1)} km^2 AOI would leave less than ${bytesLabel(INIT_ESTIMATE_MIN_FREE_BYTES)} free after the build. Choose a smaller AOI before building.`;
  }
  if (projectedFreeBytes < INIT_ESTIMATE_RECOMMENDED_FREE_BYTES) {
    return `This ~${areaKm2.toFixed(areaKm2 < 1 ? 3 : 1)} km^2 AOI may leave only ${bytesLabel(projectedFreeBytes)} free after the build. A smaller AOI is recommended.`;
  }
  return null;
}

async function initEstimatePayload(coordinates, dataDir) {
  const normalized = normalizeInitCoordinates(coordinates);
  const areaKm2 = aoiAreaKm2(normalized);
  const calibration = initEstimateCalibration();
  const estBytes = Math.ceil(calibration.base_bytes + (areaKm2 * calibration.bytes_per_km2));
  const disk = await diskInfoForDataDir(dataDir);
  const diskFreeBytes = Number.isFinite(Number(disk.free_bytes)) ? Number(disk.free_bytes) : null;
  const diskTotalBytes = Number.isFinite(Number(disk.total_bytes)) ? Number(disk.total_bytes) : null;
  const projectedFreeBytes = diskFreeBytes === null ? null : diskFreeBytes - estBytes;
  const blocked = projectedFreeBytes !== null && projectedFreeBytes < INIT_ESTIMATE_MIN_FREE_BYTES;
  const advisory = estimateAdvisory({
    areaKm2,
    estBytes,
    diskFreeBytes,
    projectedFreeBytes,
    blocked,
  });
  return {
    ok: !blocked,
    blocked,
    area_km2: areaKm2,
    est_bytes: estBytes,
    disk_free_bytes: diskFreeBytes,
    disk_total_bytes: diskTotalBytes,
    projected_free_bytes: projectedFreeBytes,
    min_free_bytes: INIT_ESTIMATE_MIN_FREE_BYTES,
    recommended_free_bytes: INIT_ESTIMATE_RECOMMENDED_FREE_BYTES,
    advisory,
    disk,
    estimate: calibration,
  };
}

function initJobSnapshot(job = initJob) {
  return {
    status: job.status,
    running: job.running,
    logs: job.logs,
    name: job.name,
    started_at: job.started_at,
    finished_at: job.finished_at,
    exit_code: job.exit_code,
  };
}

function appendInitLog(job, chunk) {
  const text = String(chunk || '');
  if (!text) return;
  job.logs.push(...text.replace(/\r/g, '').split('\n').filter(Boolean));
  if (job.logs.length > 500) {
    job.logs.splice(0, job.logs.length - 500);
  }
}

function cleanTwinName(value) {
  const name = String(value || '').trim().replace(/\s+/g, ' ').slice(0, 120);
  return name || 'VEIL twin';
}

function normalizeInitCoordinates(value) {
  if (!Array.isArray(value) || value.length < 3) {
    throw new Error('draw at least 3 AOI points');
  }
  const coords = value.map((point) => {
    if (!Array.isArray(point) || point.length < 2) {
      throw new Error('AOI coordinates must be [lon, lat] pairs');
    }
    const lon = Number(point[0]);
    const lat = Number(point[1]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
      throw new Error('AOI coordinates must be finite numbers');
    }
    if (lon < -180 || lon > 180 || lat < -90 || lat > 90) {
      throw new Error('AOI coordinates must be lon/lat degrees');
    }
    return [lon, lat];
  });
  const first = coords[0];
  const last = coords[coords.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) coords.push([...first]);
  return coords;
}

function normalizeNationalLayerIds(value) {
  if (!Array.isArray(value)) return [];
  const out = [];
  const seen = new Set();
  for (const raw of value) {
    const id = String(raw || '').trim();
    if (!/^[a-z0-9_:-]{1,80}$/i.test(id) || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out.slice(0, 80);
}

function initAoiFeatureCollection(name, coordinates) {
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: { name, source: 'VEIL init UI' },
      geometry: { type: 'Polygon', coordinates: [coordinates] },
    }],
  };
}

function cleanAddressSearchQuery(value) {
  return String(value || '').trim().replace(/\s+/g, ' ').slice(0, 200);
}

function normalizeCensusAddressMatches(payload) {
  const matches = payload
    && payload.result
    && Array.isArray(payload.result.addressMatches)
    ? payload.result.addressMatches : [];
  const out = [];
  for (const match of matches) {
    const coords = match && match.coordinates ? match.coordinates : {};
    const lon = Number(coords.x);
    const lat = Number(coords.y);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    if (lon < -180 || lon > 180 || lat < -90 || lat > 90) continue;
    const components = match.addressComponents && typeof match.addressComponents === 'object'
      ? match.addressComponents : {};
    out.push({
      label: String(match.matchedAddress || '').trim() || `${lat.toFixed(6)}, ${lon.toFixed(6)}`,
      lon,
      lat,
      components: {
        from_address: String(components.fromAddress || '').trim(),
        to_address: String(components.toAddress || '').trim(),
        street: [
          components.preDirection,
          components.streetName,
          components.suffixType,
          components.suffixDirection,
        ].map((part) => String(part || '').trim()).filter(Boolean).join(' '),
        city: String(components.city || '').trim(),
        state: String(components.state || '').trim(),
        zip: String(components.zip || '').trim(),
      },
    });
    if (out.length >= 8) break;
  }
  return out;
}

async function handleInitAddressSearch(req, res, searchParams) {
  if (!initModeEnabled()) {
    return send(res, 404, JSON.stringify({ ok: false, error: 'init mode is not enabled' }),
      { 'Content-Type': 'application/json' });
  }
  const query = cleanAddressSearchQuery(searchParams.get('q'));
  if (query.length < 3) {
    return send(res, 400, JSON.stringify({ ok: false, error: 'enter at least 3 characters' }),
      { 'Content-Type': 'application/json' });
  }

  const upstream = new URL('https://geocoding.geo.census.gov/geocoder/locations/onelineaddress');
  upstream.searchParams.set('address', query);
  upstream.searchParams.set('benchmark', 'Public_AR_Current');
  upstream.searchParams.set('format', 'json');

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const upstreamRes = await fetch(upstream, {
      signal: controller.signal,
      headers: { 'User-Agent': 'VEIL init address search' },
    });
    const text = await upstreamRes.text();
    if (!upstreamRes.ok) {
      return send(res, 502, JSON.stringify({
        ok: false,
        error: `address search exited ${upstreamRes.status}`,
        detail: text.slice(0, 600),
      }), { 'Content-Type': 'application/json' });
    }
    let payload;
    try {
      payload = JSON.parse(text);
    } catch (_err) {
      return send(res, 502, JSON.stringify({
        ok: false,
        error: 'address search returned invalid JSON',
        detail: text.slice(0, 600),
      }), { 'Content-Type': 'application/json' });
    }
    return send(res, 200, JSON.stringify({
      ok: true,
      query,
      results: normalizeCensusAddressMatches(payload),
    }), { 'Content-Type': 'application/json' });
  } catch (err) {
    const aborted = err && err.name === 'AbortError';
    return send(res, 502, JSON.stringify({
      ok: false,
      error: aborted ? 'address search timed out' : `address search failed: ${err.message || err}`,
    }), { 'Content-Type': 'application/json' });
  } finally {
    clearTimeout(timer);
  }
}

function handleInitStatus(req, res) {
  send(res, 200, JSON.stringify(initJobSnapshot(initJobFor(req, res))), { 'Content-Type': 'application/json' });
}

async function handleSystemCheck(req, res) {
  if (!initModeEnabled()) {
    return send(res, 404, JSON.stringify({ ok: false, error: 'init mode is not enabled' }),
      { 'Content-Type': 'application/json' });
  }
  const dataDir = requestDataDir(req, res);
  try {
    const payload = await initSystemCheckPayload(dataDir);
    return send(res, 200, JSON.stringify(payload), { 'Content-Type': 'application/json' });
  } catch (err) {
    return send(res, 200, JSON.stringify({
      ok: false,
      data_dir: path.relative(ROOT, dataDir) || '.',
      disk: { ok: false, free_bytes: null, total_bytes: null },
      memory: memoryInfo(),
      tools: { node: { ok: true, version: process.version } },
      warnings: [`System check failed: ${err.message || err}`],
    }), { 'Content-Type': 'application/json' });
  }
}

function handleInitEstimate(req, res) {
  if (!initModeEnabled()) {
    return send(res, 404, JSON.stringify({ ok: false, error: 'init mode is not enabled' }),
      { 'Content-Type': 'application/json' });
  }
  const dataDir = requestDataDir(req, res);
  readBodyJson(req, INIT_MAX_BODY, (err, body) => {
    if (err) {
      return send(res, 400, JSON.stringify({ ok: false, error: err.message }),
        { 'Content-Type': 'application/json' });
    }
    let coordinates;
    try {
      coordinates = normalizeInitCoordinates(body.coordinates);
    } catch (e) {
      return send(res, 400, JSON.stringify({ ok: false, error: e.message }),
        { 'Content-Type': 'application/json' });
    }
    initEstimatePayload(coordinates, dataDir)
      .then((payload) => send(res, 200, JSON.stringify(payload), { 'Content-Type': 'application/json' }))
      .catch((e) => send(res, 200, JSON.stringify({
        ok: true,
        blocked: false,
        area_km2: aoiAreaKm2(coordinates),
        est_bytes: null,
        disk_free_bytes: null,
        disk_total_bytes: null,
        projected_free_bytes: null,
        advisory: `Build size could not be estimated: ${e.message || e}. Keep the AOI small.`,
      }), { 'Content-Type': 'application/json' }));
  });
}

function handleInitLayerScan(req, res) {
  if (!initModeEnabled()) {
    return send(res, 404, JSON.stringify({ ok: false, error: 'init mode is not enabled' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, INIT_MAX_BODY, (err, body) => {
    if (err) {
      return send(res, 400, JSON.stringify({ ok: false, error: err.message }),
        { 'Content-Type': 'application/json' });
    }
    let coordinates;
    try {
      coordinates = normalizeInitCoordinates(body.coordinates);
    } catch (e) {
      return send(res, 400, JSON.stringify({ ok: false, error: e.message }),
        { 'Content-Type': 'application/json' });
    }
    const dataDir = requestDataDir(req, res);
    const scanDir = path.join(dataDir, 'init');
    fs.mkdirSync(scanDir, { recursive: true });
    const scanPath = path.join(scanDir, `scan-aoi-${Date.now()}.geojson`);
    writeJsonFileAtomic(scanPath, initAoiFeatureCollection('Layer scan AOI', coordinates));

    const child = spawn(MCP_PYTHON, [
      path.join(ROOT, 'scripts', 'fetch_national_layers.py'),
      'probe',
      '--aoi', scanPath,
    ], {
      cwd: ROOT,
      env: { ...process.env, TWIN_DATA_DIR: dataDir, TWIN_PACK: 'us-national' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let done = false;
    const timer = setTimeout(() => {
      if (!done) child.kill('SIGTERM');
    }, 5 * 60 * 1000);
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', (e) => {
      done = true;
      clearTimeout(timer);
      send(res, 500, JSON.stringify({ ok: false, error: e.message }),
        { 'Content-Type': 'application/json' });
    });
    child.on('close', (code) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      fs.rm(scanPath, { force: true }, () => {});
      if (code !== 0) {
        return send(res, 502, JSON.stringify({
          ok: false,
          error: `layer scan exited ${code}`,
          detail: stderr.slice(0, 2000) || stdout.slice(0, 2000),
        }), { 'Content-Type': 'application/json' });
      }
      try {
        const payload = JSON.parse(stdout);
        send(res, 200, JSON.stringify(payload), { 'Content-Type': 'application/json' });
      } catch (_err) {
        send(res, 502, JSON.stringify({
          ok: false,
          error: 'layer scan returned invalid JSON',
          detail: stdout.slice(0, 2000) || stderr.slice(0, 2000),
        }), { 'Content-Type': 'application/json' });
      }
    });
  });
}

// Streaming sibling of handleInitLayerScan: spawns the probe with --progress and
// pipes its per-layer NDJSON events straight to the client (Transfer-Encoding:
// chunked) so the setup UI can show each national layer being checked as it
// resolves. The buffered endpoint above stays as the fallback.
function handleInitLayerScanStream(req, res) {
  if (!initModeEnabled()) {
    return send(res, 404, JSON.stringify({ ok: false, error: 'init mode is not enabled' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, INIT_MAX_BODY, (err, body) => {
    if (err) {
      return send(res, 400, JSON.stringify({ ok: false, error: err.message }),
        { 'Content-Type': 'application/json' });
    }
    let coordinates;
    try {
      coordinates = normalizeInitCoordinates(body.coordinates);
    } catch (e) {
      return send(res, 400, JSON.stringify({ ok: false, error: e.message }),
        { 'Content-Type': 'application/json' });
    }
    const dataDir = requestDataDir(req, res);
    const scanDir = path.join(dataDir, 'init');
    fs.mkdirSync(scanDir, { recursive: true });
    const scanPath = path.join(scanDir, `scan-aoi-${Date.now()}.geojson`);
    writeJsonFileAtomic(scanPath, initAoiFeatureCollection('Layer scan AOI', coordinates));

    const child = spawn(MCP_PYTHON, [
      path.join(ROOT, 'scripts', 'fetch_national_layers.py'),
      'probe',
      '--aoi', scanPath,
      '--progress',
    ], {
      cwd: ROOT,
      env: { ...process.env, TWIN_DATA_DIR: dataDir, TWIN_PACK: 'us-national' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    res.writeHead(200, {
      'Content-Type': 'application/x-ndjson',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
    });

    let ended = false;
    let stderr = '';
    const end = () => { if (!ended) { ended = true; res.end(); } };
    const timer = setTimeout(() => child.kill('SIGTERM'), 5 * 60 * 1000);

    child.stdout.on('data', (chunk) => { if (!ended) res.write(chunk); });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', (e) => {
      clearTimeout(timer);
      if (!ended) res.write(`${JSON.stringify({ event: 'error', error: e.message })}\n`);
      end();
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      fs.rm(scanPath, { force: true }, () => {});
      if (!ended && code !== 0) {
        res.write(`${JSON.stringify({
          event: 'error',
          error: `layer scan exited ${code}`,
          detail: stderr.slice(0, 600),
        })}\n`);
      }
      end();
    });
    // If the browser navigates away or aborts mid-stream, stop the probe. Guard
    // on `ended` so the normal end() path (which fires res 'close' too) and the
    // request-body 'end' don't kill a still-producing child.
    res.on('close', () => {
      clearTimeout(timer);
      if (!ended) { ended = true; child.kill('SIGTERM'); }
    });
  });
}

function handleInitAoi(req, res) {
  if (!initModeEnabled()) {
    return send(res, 404, JSON.stringify({ ok: false, error: 'init mode is not enabled' }),
      { 'Content-Type': 'application/json' });
  }
  const job = initJobFor(req, res);
  const dataDir = requestDataDir(req, res);
  if (job.running) {
    return send(res, 409, JSON.stringify({ ok: false, error: 'an init build is already running', job: initJobSnapshot(job) }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, INIT_MAX_BODY, (err, body) => {
    if (err) {
      return send(res, 400, JSON.stringify({ ok: false, error: err.message }),
        { 'Content-Type': 'application/json' });
    }
    let coordinates;
    try {
      coordinates = normalizeInitCoordinates(body.coordinates);
    } catch (e) {
      return send(res, 400, JSON.stringify({ ok: false, error: e.message }),
        { 'Content-Type': 'application/json' });
    }
    const name = cleanTwinName(body.name);
    const nationalLayers = normalizeNationalLayerIds(body.national_layers);
    initEstimatePayload(coordinates, dataDir).then((estimate) => {
      if (estimate.blocked && body.override !== true) {
        return send(res, 507, JSON.stringify({
          ok: false,
          error: 'Insufficient disk space for this AOI; choose a smaller AOI before building.',
          advisory: estimate.advisory || 'Choose a smaller AOI before building.',
          area_km2: estimate.area_km2,
          est_bytes: estimate.est_bytes,
          disk_free_bytes: estimate.disk_free_bytes,
          disk_total_bytes: estimate.disk_total_bytes,
          projected_free_bytes: estimate.projected_free_bytes,
          min_free_bytes: estimate.min_free_bytes,
        }), { 'Content-Type': 'application/json' });
      }

      const initDir = path.join(dataDir, 'init');
      const aoiPath = path.join(initDir, 'aoi.geojson');
      writeJsonFileAtomic(aoiPath, initAoiFeatureCollection(name, coordinates));

      Object.assign(job, {
        status: 'running',
        running: true,
        logs: [`Starting ${name}`].concat(
          estimate.advisory ? [`Disk-space advisory: ${estimate.advisory}`] : [],
          nationalLayers.length ? [`Selected optional layers: ${nationalLayers.join(', ')}`] : []),
        name,
        started_at: new Date().toISOString(),
        finished_at: null,
        exit_code: null,
      });

      const args = [
        path.join(ROOT, 'scripts', 'build_from_aoi.py'),
        '--aoi', aoiPath,
        '--data-dir', dataDir,
        '--name', name,
        '--force',
      ];
      if (nationalLayers.length) {
        args.push('--national-layers', nationalLayers.join(','));
      }
      const child = spawn(MCP_PYTHON, args, {
        cwd: ROOT,
        env: { ...process.env, TWIN_DATA_DIR: dataDir, TWIN_PACK: 'us-national' },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      child.stdout.on('data', (chunk) => appendInitLog(job, chunk));
      child.stderr.on('data', (chunk) => appendInitLog(job, chunk));
      child.on('error', (e) => {
        job.status = 'error';
        job.running = false;
        job.finished_at = new Date().toISOString();
        appendInitLog(job, `could not start build: ${e.message}`);
      });
      child.on('close', (code) => {
        job.exit_code = code;
        job.running = false;
        job.finished_at = new Date().toISOString();
        job.status = code === 0 ? 'done' : 'error';
        appendInitLog(job, code === 0 ? 'Build complete' : `Build exited ${code}`);
      });

      return send(res, 202, JSON.stringify({ ok: true, job: initJobSnapshot(job), estimate }),
        { 'Content-Type': 'application/json' });
    }).catch((e) => send(res, 500, JSON.stringify({
      ok: false,
      error: `could not check disk space: ${e.message || e}`,
    }), { 'Content-Type': 'application/json' }));
  });
}

// --------------------------------------------------------------- live inputs
// Live telemetry is source-neutral at the HTTP boundary. Bridges for LoRA,
// BLE, serial, TCP, cameras, or edge models normalize into the same event
// envelope and post here. The server keeps current state in memory for the
// viewer, writes raw append-only logs under the active twin data directory, and
// mirrors events into data/live/telemetry.sqlite through a tiny Python helper so
// Node keeps its zero-dependency posture.

const LIVE_DIR = path.join(DATA_DIR, 'live');
const LIVE_EVENTS_PATH = path.join(LIVE_DIR, 'events.jsonl');
const LIVE_REGISTRY_PATH = path.join(LIVE_DIR, 'registry.json');
const LIVE_COMMAND_DIR = path.join(LIVE_DIR, 'commands');
const LIVE_MAX_BODY = 1024 * 1024;
const LIVE_KINDS = new Set(['position', 'message', 'data', 'status', 'media', 'command']);
const LIVE_TRANSPORTS = new Set(['serial', 'internet', 'bluetooth', 'tcp', 'udp', 'websocket', 'replay', 'manual', 'lora', 'mesh']);
const liveLatest = new Map();
const liveStreams = new Set();
const liveBridgeProcesses = new Map();

function liveDiscoveryEnabled(query) {
  const value = String(query?.get?.('discovery') || query?.get?.('include_discovered') || '').toLowerCase();
  return ['1', 'true', 'yes', 'all'].includes(value);
}

const LIVE_POSITION_ACTIVE_MS = Number(process.env.VEIL_LIVE_POSITION_ACTIVE_MS || 2 * 60 * 1000);
const LIVE_POSITION_STALE_MS = Number(process.env.VEIL_LIVE_POSITION_STALE_MS || 15 * 60 * 1000);
const LIVE_BRIDGE_RETRY_BASE_MS = Number(process.env.VEIL_LIVE_BRIDGE_RETRY_BASE_MS || 3000);
const LIVE_BRIDGE_RETRY_MAX_MS = Number(process.env.VEIL_LIVE_BRIDGE_RETRY_MAX_MS || 60 * 1000);
const LIVE_GATEWAY_AUTOSTART = process.env.VEIL_LIVE_GATEWAY_AUTOSTART !== '0';

function positiveIntEnv(name, fallback) {
  const value = Number(process.env[name]);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

// Live JSONL logs are intentionally recent replay/debug windows. SQLite remains
// the durable telemetry replay store; JSONL readers below are bounded by lines
// and bytes so startup/history requests do not scale with old file size.
const LIVE_JSONL_MAX_BYTES = positiveIntEnv('VEIL_LIVE_JSONL_MAX_BYTES', 16 * 1024 * 1024);
const LIVE_JSONL_GENERATIONS = positiveIntEnv('VEIL_LIVE_JSONL_GENERATIONS', 3);
const LIVE_COMMAND_JSONL_MAX_BYTES = positiveIntEnv('VEIL_LIVE_COMMAND_JSONL_MAX_BYTES', 1024 * 1024);
const LIVE_COMMAND_JSONL_GENERATIONS = positiveIntEnv('VEIL_LIVE_COMMAND_JSONL_GENERATIONS', 3);
const LIVE_JSONL_TAIL_MAX_BYTES = positiveIntEnv('VEIL_LIVE_JSONL_TAIL_MAX_BYTES', 2 * 1024 * 1024);
const LIVE_LATEST_MAX_LINES = positiveIntEnv('VEIL_LIVE_LATEST_MAX_LINES', 2000);
const LIVE_LATEST_TAIL_MAX_BYTES = positiveIntEnv('VEIL_LIVE_LATEST_TAIL_MAX_BYTES', LIVE_JSONL_TAIL_MAX_BYTES);
const LIVE_HISTORY_MAX_LINES = positiveIntEnv('VEIL_LIVE_HISTORY_MAX_LINES', 5000);
const LIVE_HISTORY_TAIL_MAX_BYTES = positiveIntEnv('VEIL_LIVE_HISTORY_TAIL_MAX_BYTES', LIVE_JSONL_TAIL_MAX_BYTES);
const LIVE_BRIDGE_LOG_SNAPSHOT_DEBOUNCE_MS = positiveIntEnv('VEIL_LIVE_BRIDGE_LOG_SNAPSHOT_DEBOUNCE_MS', 250);

function resolveLivePython() {
  const explicit = (process.env.VEIL_LIVE_PYTHON || '').trim();
  if (explicit) return explicit;
  const venv = path.join(ROOT, '.venv-live', 'bin', 'python');
  try {
    fs.accessSync(venv, fs.constants.X_OK);
    return venv;
  } catch (_err) {
    return MCP_PYTHON;
  }
}

const LIVE_PYTHON = resolveLivePython();

const LIVE_STORE_IMPORT_CHECK = 'import pyproj, osgeo';

function liveStorePythonHasGeoDeps(python) {
  if (!python) return false;
  const result = spawnSync(python, ['-c', LIVE_STORE_IMPORT_CHECK], {
    cwd: ROOT,
    encoding: 'utf8',
    timeout: 5000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  return !result.error && result.status === 0;
}

function uniqueLiveStorePythonCandidates(candidates) {
  const seen = new Set();
  const unique = [];
  candidates.forEach((candidate) => {
    const value = String(candidate || '').trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    unique.push(value);
  });
  return unique;
}

function resolveLiveStorePython({
  env = process.env,
  root = ROOT,
  mcpPython = MCP_PYTHON,
  livePython = LIVE_PYTHON,
  canImportGeoDeps = liveStorePythonHasGeoDeps,
  warn = console.warn,
} = {}) {
  const explicit = (env.VEIL_LIVE_STORE_PYTHON || '').trim();
  if (explicit) {
    if (!canImportGeoDeps(explicit)) {
      warn(`VEIL_LIVE_STORE_PYTHON (${explicit}) cannot import live-store export dependencies (${LIVE_STORE_IMPORT_CHECK}); export may fail.`);
    }
    return explicit;
  }

  const candidates = uniqueLiveStorePythonCandidates([
    mcpPython,
    path.join(root, '.venv-mcp', 'bin', 'python'),
    'python3',
    livePython,
  ]);
  for (const candidate of candidates) {
    if (canImportGeoDeps(candidate)) return candidate;
  }
  const fallback = mcpPython || 'python3';
  warn(`No Python interpreter could import live-store export dependencies (${LIVE_STORE_IMPORT_CHECK}); falling back to ${fallback}. Set VEIL_LIVE_STORE_PYTHON to a Python with pyproj and GDAL/osgeo.`);
  return fallback;
}

const LIVE_STORE_PYTHON = resolveLiveStorePython();

function safeDeviceKey(id) {
  return String(id || '').replace(/[^a-zA-Z0-9_.:!@-]+/g, '_').slice(0, 120);
}

function cleanLiveColor(value) {
  const text = String(value || '').trim();
  return /^#[0-9a-fA-F]{6}$/.test(text) ? text.toLowerCase() : null;
}

function canonicalMeshtasticNodeId(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  if (Number.isInteger(value) && value >= 0 && value <= 0xffffffff) {
    return `!${value.toString(16).padStart(8, '0')}`;
  }
  const text = String(value).trim();
  let match = text.match(/^!([0-9a-fA-F]{1,8})$/) || text.match(/^0x([0-9a-fA-F]{1,8})$/);
  if (match) return `!${Number.parseInt(match[1], 16).toString(16).padStart(8, '0')}`;
  if (/^\d+$/.test(text)) {
    const num = Number.parseInt(text, 10);
    if (Number.isSafeInteger(num) && num >= 0 && num <= 0xffffffff) {
      return `!${num.toString(16).padStart(8, '0')}`;
    }
  }
  match = text.match(/^[0-9a-fA-F]{1,8}$/);
  if (match && /[a-fA-F]/.test(text)) {
    return `!${Number.parseInt(text, 16).toString(16).padStart(8, '0')}`;
  }
  return null;
}

function liveGatewayNodeIds(reg = liveRegistry()) {
  const ids = new Set();
  (reg.gateways || []).forEach((gateway) => {
    if (!gateway.node_id) return;
    const canonical = canonicalMeshtasticNodeId(gateway.node_id);
    if (canonical) ids.add(canonical);
    ids.add(safeDeviceKey(gateway.node_id));
  });
  return ids;
}

function isGatewaySelfEvent(event, reg = liveRegistry(), gatewayNodeIds = liveGatewayNodeIds(reg)) {
  if (!event?.device_id) return false;
  if (gatewayNodeIds.has(event.device_id)) return true;
  const canonicalDeviceId = canonicalMeshtasticNodeId(event.device_id);
  if (canonicalDeviceId && gatewayNodeIds.has(canonicalDeviceId)) return true;
  if (!event.link?.gateway_node_id) return false;
  if (event.device_id === event.link.gateway_node_id) return true;
  return !!canonicalDeviceId && canonicalDeviceId === canonicalMeshtasticNodeId(event.link.gateway_node_id);
}

function liveEventGatewayId(event, pref = {}) {
  return safeDeviceKey(pref.gateway_id || event?.link?.gateway_id || event?.gateway_id || '');
}

function defaultMeshtasticLabel(deviceId) {
  const canonical = canonicalMeshtasticNodeId(deviceId);
  if (!canonical) return null;
  return `Meshtastic ${canonical.slice(-4)}`;
}

function isDefaultMeshtasticLabel(deviceId, label) {
  const text = String(label || '').trim();
  const fallback = defaultMeshtasticLabel(deviceId);
  return !!fallback && text.toLowerCase() === fallback.toLowerCase();
}

function isConfiguredLiveDevice(deviceId, pref = {}, event = {}) {
  if (!pref || typeof pref !== 'object') return false;
  if (pref.configured === true) return true;
  if (pref.configured === false) return false;
  const label = pref.label || event.label;
  if (label && label !== deviceId && !isDefaultMeshtasticLabel(deviceId, label)) return true;
  if (pref.visible === false && label && !isDefaultMeshtasticLabel(deviceId, label)) return true;
  return false;
}

function timestampMs(value) {
  const ms = new Date(value || '').valueOf();
  return Number.isFinite(ms) ? ms : null;
}

function canonicalIsoSeconds(date) {
  const d = new Date(date.valueOf());
  d.setUTCMilliseconds(0);
  return d.toISOString().replace('.000Z', 'Z');
}

function normalizeLiveTimestamp(value, fieldName, fallbackDate) {
  if (value === undefined || value === null || value === '') {
    return canonicalIsoSeconds(fallbackDate || new Date());
  }
  if (typeof value !== 'string') throw new Error(`${fieldName} must be an ISO timestamp string`);
  const text = value.trim();
  if (!/(Z|[+-]\d{2}:\d{2})$/.test(text)) {
    throw new Error(`${fieldName} must include an explicit timezone`);
  }
  const parsed = new Date(text);
  if (!Number.isFinite(parsed.valueOf())) throw new Error(`${fieldName} must be an ISO timestamp`);
  return canonicalIsoSeconds(parsed);
}

function liveBridgeConnected(gatewayId) {
  if (!gatewayId) return true;
  return liveBridgeStatus(gatewayId).state === 'running';
}

function utcDay(ts) {
  const d = new Date(ts);
  if (!Number.isFinite(d.valueOf())) throw new Error('live event timestamp must be valid before partitioning');
  return d.toISOString().slice(0, 10);
}

function liveToken() {
  const envToken = (process.env.VEIL_LIVE_TOKEN || '').trim();
  if (envToken) return envToken;
  for (const p of [path.join(DATA_DIR, '.live_token'), path.join(ROOT, '.live_token')]) {
    try {
      const token = fs.readFileSync(p, 'utf8').trim();
      if (token) return token;
    } catch (_err) { /* no token file */ }
  }
  return null;
}

let warnedLiveUnauthenticated = false;

function headerText(value) {
  if (Array.isArray(value)) return value[0] || '';
  return typeof value === 'string' ? value : '';
}

function timingSafeTokenEqual(a, b) {
  if (!a || !b) return false;
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function liveRequestTokens(req, query) {
  const headers = req?.headers || {};
  const tokens = [];
  const liveHeader = headerText(headers['x-veil-live-token']).trim();
  if (liveHeader) tokens.push(liveHeader);
  const bearer = headerText(headers.authorization).match(/^Bearer\s+(.+)$/i);
  if (bearer?.[1]?.trim()) tokens.push(bearer[1].trim());
  const queryToken = query?.get?.('token')?.trim();
  if (queryToken) tokens.push(queryToken);
  return tokens;
}

function isLoopbackLiveRequest(req) {
  const remote = req?.socket?.remoteAddress || req?.connection?.remoteAddress || '';
  return remote === '127.0.0.1' || remote === '::1' || remote === '::ffff:127.0.0.1';
}

// Manage the live token from the UI. Gated to loopback only: you set it up on
// the machine running VEIL (before publishing), and a remote visitor on an
// open twin can't seize or clear it. Writes data/.live_token (gitignored).
function handleLiveTokenStatus(req, res) {
  send(res, 200, JSON.stringify({
    ok: true,
    protected: !!liveToken(),
    env_locked: !!(process.env.VEIL_LIVE_TOKEN || '').trim(),
    can_manage: isLoopbackLiveRequest(req),
  }), { 'Content-Type': 'application/json' });
}

function handleLiveTokenSet(req, res) {
  if (!isLoopbackLiveRequest(req)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'set the live token from the machine running VEIL (localhost)' }), { 'Content-Type': 'application/json' });
  }
  if ((process.env.VEIL_LIVE_TOKEN || '').trim()) {
    return send(res, 409, JSON.stringify({ ok: false, error: 'VEIL_LIVE_TOKEN is set in the environment and overrides the file; unset it to manage the token here' }), { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const token = body.generate ? crypto.randomBytes(16).toString('hex') : String(body.token || '').trim();
    const file = path.join(DATA_DIR, '.live_token');
    try {
      if (token) fs.writeFileSync(file, `${token}\n`, { mode: 0o600 });
      else fs.rmSync(file, { force: true });
    } catch (e) {
      return send(res, 500, JSON.stringify({ ok: false, error: e.message }), { 'Content-Type': 'application/json' });
    }
    send(res, 200, JSON.stringify({ ok: true, protected: !!token, token: token || null }), { 'Content-Type': 'application/json' });
  });
}

function liveAuthorized(req, query, options = {}) {
  const token = Object.prototype.hasOwnProperty.call(options, 'token') ? options.token : liveToken();
  if (token) {
    return liveRequestTokens(req, query).some((candidate) => timingSafeTokenEqual(candidate, token));
  }
  // No token configured: VEIL is local-first, so the live API stays open with
  // zero setup — telemetry "just works" for someone running it on their own
  // machine. Warn once so anyone who exposes the server to the web knows to set
  // a token (data/.live_token or VEIL_LIVE_TOKEN) to lock the live API down.
  if (!warnedLiveUnauthenticated) {
    const warn = options.warn || console.warn;
    warn('Live telemetry API has no token set, so it is open to anyone who can reach this server. That is fine for local use. If you publish this VEIL twin to the web, set data/.live_token (or VEIL_LIVE_TOKEN) to require a token.');
    warnedLiveUnauthenticated = true;
  }
  return true;
}

function readJsonFile(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (_err) {
    return fallback;
  }
}

function writeJsonFileAtomic(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
  fs.renameSync(tmp, filePath);
}

async function writeJsonTextAtomicAsync(filePath, text) {
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  await fsp.writeFile(tmp, text);
  await fsp.rename(tmp, filePath);
}

function normalizeLiveRegistryDoc(doc) {
  if (doc && typeof doc === 'object') {
    doc.gateways = Array.isArray(doc.gateways) ? doc.gateways : [];
    doc.devices = doc.devices && typeof doc.devices === 'object' ? doc.devices : {};
    return doc;
  }
  return { version: 1, gateways: [], devices: {} };
}

let liveRegistryCache = normalizeLiveRegistryDoc(readJsonFile(LIVE_REGISTRY_PATH, null));
let liveRegistrySaveScheduled = false;
let liveRegistryDirty = false;
let liveRegistryPersistQueue = Promise.resolve();

function liveRegistry() {
  return liveRegistryCache;
}

function saveLiveRegistry(doc) {
  liveRegistryCache = normalizeLiveRegistryDoc(doc);
  liveRegistryCache.version = 1;
  liveRegistryCache.updated_at = new Date().toISOString();
  liveRegistryDirty = false;
  writeJsonFileAtomic(LIVE_REGISTRY_PATH, liveRegistryCache);
}

function queueLiveRegistrySave() {
  liveRegistryDirty = true;
  if (liveRegistrySaveScheduled) return;
  liveRegistrySaveScheduled = true;
  setImmediate(flushQueuedLiveRegistrySave);
}

function flushQueuedLiveRegistrySave() {
  liveRegistrySaveScheduled = false;
  if (!liveRegistryDirty) return;
  liveRegistryDirty = false;
  liveRegistryCache.version = 1;
  liveRegistryCache.updated_at = new Date().toISOString();
  const text = JSON.stringify(liveRegistryCache, null, 2);
  liveRegistryPersistQueue = liveRegistryPersistQueue
    .then(() => writeJsonTextAtomicAsync(LIVE_REGISTRY_PATH, text))
    .catch((err) => console.error('live registry save failed:', err.message))
    .then(() => {
      if (liveRegistryDirty && !liveRegistrySaveScheduled) {
        liveRegistrySaveScheduled = true;
        setImmediate(flushQueuedLiveRegistrySave);
      }
    });
}

function readBodyJson(req, maxBytes, callback) {
  let body = '';
  let tooLarge = false;
  req.on('data', (chunk) => {
    body += chunk;
    if (body.length > maxBytes) {
      tooLarge = true;
      req.destroy();
    }
  });
  req.on('error', () => callback(new Error(tooLarge ? 'body too large' : 'request interrupted')));
  req.on('end', () => {
    if (tooLarge) return callback(new Error('body too large'));
    try {
      callback(null, JSON.parse(body || '{}'));
    } catch (_err) {
      callback(new Error('invalid JSON body'));
    }
  });
}

function readBodyBuffer(req, maxBytes, callback) {
  const chunks = [];
  let bytes = 0;
  let tooLarge = false;
  req.on('data', (chunk) => {
    bytes += chunk.length;
    if (bytes > maxBytes) {
      tooLarge = true;
      req.destroy();
      return;
    }
    chunks.push(chunk);
  });
  req.on('error', () => callback(new Error(tooLarge ? 'body too large' : 'request interrupted')));
  req.on('end', () => {
    if (tooLarge) return callback(new Error('body too large'));
    callback(null, Buffer.concat(chunks));
  });
}

function parseMultipart(req, maxBytes, callback) {
  const type = String(req.headers['content-type'] || '');
  const match = type.match(/boundary=(?:"([^"]+)"|([^;]+))/i);
  if (!match) return callback(new Error('expected multipart/form-data'));
  const boundary = match[1] || match[2];
  readBodyBuffer(req, maxBytes, (err, body) => {
    if (err) return callback(err);
    const delimiter = Buffer.from(`--${boundary}`);
    const fields = {};
    const files = [];
    let offset = 0;
    while (offset < body.length) {
      const start = body.indexOf(delimiter, offset);
      if (start < 0) break;
      let partStart = start + delimiter.length;
      if (body.slice(partStart, partStart + 2).toString() === '--') break;
      if (body.slice(partStart, partStart + 2).toString() === '\r\n') partStart += 2;
      const headerEnd = body.indexOf(Buffer.from('\r\n\r\n'), partStart);
      if (headerEnd < 0) break;
      const headers = body.slice(partStart, headerEnd).toString('utf8');
      let dataEnd = body.indexOf(delimiter, headerEnd + 4);
      if (dataEnd < 0) dataEnd = body.length;
      let data = body.slice(headerEnd + 4, dataEnd);
      if (data.length >= 2 && data.slice(data.length - 2).toString() === '\r\n') {
        data = data.slice(0, data.length - 2);
      }
      const disp = headers.match(/content-disposition:\s*form-data;([^\r\n]+)/i);
      const name = disp && (disp[1].match(/name="([^"]+)"/i) || [])[1];
      const filename = disp && (disp[1].match(/filename="([^"]*)"/i) || [])[1];
      if (name && filename) {
        files.push({ field: name, filename, data });
      } else if (name) {
        fields[name] = data.toString('utf8');
      }
      offset = dataEnd;
    }
    callback(null, { fields, files });
  });
}

function finiteNumber(value, min, max) {
  return typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;
}

function cleanOptionalObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

function normalizeLiveEvent(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new Error('event must be an object');
  }
  const kind = String(input.kind || 'position').toLowerCase();
  if (!LIVE_KINDS.has(kind)) throw new Error(`unsupported kind: ${kind}`);
  const deviceId = safeDeviceKey(input.device_id || input.node_id || input.id);
  if (!deviceId) throw new Error('device_id is required');

  const receivedAtDate = new Date();
  const event = {
    schema: 'veil.live.v1',
    kind,
    device_id: deviceId,
    label: typeof input.label === 'string' ? input.label.slice(0, 120) : undefined,
    observed_at: normalizeLiveTimestamp(input.observed_at, 'observed_at', receivedAtDate),
    received_at: normalizeLiveTimestamp(input.received_at, 'received_at', receivedAtDate),
  };

  if (input.position !== undefined) {
    const p = cleanOptionalObject(input.position);
    if (!p || !finiteNumber(p.lat, -90, 90) || !finiteNumber(p.lon, -180, 180)) {
      throw new Error('position.lat and position.lon must be valid numbers');
    }
    event.position = {
      lat: p.lat,
      lon: p.lon,
      alt_m: typeof p.alt_m === 'number' && Number.isFinite(p.alt_m) ? p.alt_m : null,
      accuracy_m: typeof p.accuracy_m === 'number' && Number.isFinite(p.accuracy_m) ? p.accuracy_m : null,
    };
  }
  if (kind === 'position' && !event.position) throw new Error('position events require position');

  for (const key of ['motion', 'link', 'source', 'metadata', 'media']) {
    const obj = cleanOptionalObject(input[key]);
    if (obj) event[key] = obj;
  }
  if (event.source) {
    event.source.protocol = String(event.source.protocol || 'unknown').slice(0, 60);
    event.source.transport = String(event.source.transport || 'unknown').slice(0, 40);
    if (event.source.transport !== 'unknown' && !LIVE_TRANSPORTS.has(event.source.transport)) {
      event.source.transport = 'internet';
    }
  }
  if (typeof input.message === 'string') event.message = input.message.slice(0, 20000);
  if (Object.prototype.hasOwnProperty.call(input, 'data')) event.data = input.data;
  if (Object.prototype.hasOwnProperty.call(input, 'payload')) event.payload = input.payload;
  return event;
}

const LIVE_DB_APPEND_TIMEOUT_MS = Number(process.env.VEIL_LIVE_DB_APPEND_TIMEOUT_MS) || 30000;
const LIVE_DB_APPEND_QUEUE_MAX = Number(process.env.VEIL_LIVE_DB_APPEND_QUEUE_MAX) || 5000;
let liveDbAppendDropped = 0;

function runLiveDbAppend(event) {
  return new Promise((resolve) => {
    const py = spawn(LIVE_STORE_PYTHON, [path.join(ROOT, 'scripts', 'live', 'live_store.py'), 'append'], {
      cwd: ROOT,
      env: { ...process.env, TWIN_DATA_DIR: DATA_DIR },
      stdio: ['pipe', 'ignore', 'pipe'],
    });
    let settled = false;
    let timer = null;
    const finish = () => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve();
    };
    // Watchdog: a wedged interpreter or a stuck WAL lock would otherwise keep
    // liveDbAppendActive=true forever, stalling the serial drain while
    // /api/live/events keeps pushing onto the queue until the server OOMs.
    // SIGKILL the child and move on (SQLite WAL rolls back its open txn cleanly).
    timer = setTimeout(() => {
      if (settled) return;
      console.error(`live db append timed out after ${LIVE_DB_APPEND_TIMEOUT_MS}ms; killing child`);
      try { py.kill('SIGKILL'); } catch (_err) { /* already gone */ }
      finish();
    }, LIVE_DB_APPEND_TIMEOUT_MS);
    timer.unref?.();
    py.stderr.on('data', (c) => console.error('live db:', String(c).trim()));
    py.stdin.on('error', () => {}); // swallow EPIPE if the child died before write
    py.on('error', (err) => {
      console.error('live db append failed:', err.message);
      finish();
    });
    py.on('close', () => finish());
    try {
      py.stdin.end(JSON.stringify(event));
    } catch (_err) {
      finish();
    }
  });
}

let liveDbAppendRunner = runLiveDbAppend;
const liveDbAppendQueue = [];
let liveDbAppendActive = false;
let liveDbIdleResolvers = [];

function notifyLiveDbIdle() {
  if (liveDbAppendActive || liveDbAppendQueue.length) return;
  const resolvers = liveDbIdleResolvers;
  liveDbIdleResolvers = [];
  resolvers.forEach((resolve) => resolve());
}

function drainLiveDbAppendQueue() {
  if (liveDbAppendActive) return;
  const event = liveDbAppendQueue.shift();
  if (!event) {
    notifyLiveDbIdle();
    return;
  }
  liveDbAppendActive = true;
  Promise.resolve()
    .then(() => liveDbAppendRunner(event))
    .catch((err) => console.error('live db append failed:', err.message))
    .finally(() => {
      liveDbAppendActive = false;
      setImmediate(drainLiveDbAppendQueue);
    });
}

function queueLiveDbAppend(event) {
  if (liveDbAppendQueue.length >= LIVE_DB_APPEND_QUEUE_MAX) {
    // Persistence is falling behind (or wedged): drop the oldest queued event so
    // the in-memory queue can't grow without bound. Live telemetry is best-effort
    // and the journal is rebuildable; the newest events matter most.
    liveDbAppendQueue.shift();
    liveDbAppendDropped += 1;
    if (liveDbAppendDropped === 1 || liveDbAppendDropped % 100 === 0) {
      console.error(`live db append queue at cap (${LIVE_DB_APPEND_QUEUE_MAX}); dropped ${liveDbAppendDropped} oldest event(s)`);
    }
  }
  liveDbAppendQueue.push(event);
  drainLiveDbAppendQueue();
}

function liveDbQueueIdle() {
  if (!liveDbAppendActive && !liveDbAppendQueue.length) return Promise.resolve();
  return new Promise((resolve) => liveDbIdleResolvers.push(resolve));
}

function rotatedJsonlPath(filePath, generation) {
  return generation > 0 ? `${filePath}.${generation}` : filePath;
}

function jsonlPathsOldestFirst(filePath, generations) {
  const paths = [];
  for (let i = generations; i >= 1; i -= 1) paths.push(rotatedJsonlPath(filePath, i));
  paths.push(filePath);
  return paths;
}

function nonEmptyTailLines(text, maxLines, dropFirstPartial) {
  const lines = text.split('\n');
  if (dropFirstPartial && lines.length) lines.shift();
  return lines
    .filter((line) => line.trim())
    .slice(-maxLines);
}

function readLastNonEmptyLinesSync(filePath, maxLines, maxBytes = LIVE_JSONL_TAIL_MAX_BYTES) {
  if (maxLines <= 0 || maxBytes <= 0) return [];
  let fd;
  try {
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size <= 0) return [];
    const bytesToRead = Math.min(stat.size, maxBytes);
    const start = stat.size - bytesToRead;
    const buffer = Buffer.alloc(bytesToRead);
    fd = fs.openSync(filePath, 'r');
    const bytesRead = fs.readSync(fd, buffer, 0, bytesToRead, start);
    return nonEmptyTailLines(buffer.subarray(0, bytesRead).toString('utf8'), maxLines, start > 0);
  } catch (_err) {
    return [];
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch (_err) { /* already closed */ }
    }
  }
}

async function readLastNonEmptyLines(filePath, maxLines, maxBytes = LIVE_JSONL_TAIL_MAX_BYTES) {
  if (maxLines <= 0 || maxBytes <= 0) return [];
  let fh;
  try {
    fh = await fsp.open(filePath, 'r');
    const stat = await fh.stat();
    if (!stat.isFile() || stat.size <= 0) return [];
    const bytesToRead = Math.min(stat.size, maxBytes);
    const start = stat.size - bytesToRead;
    const buffer = Buffer.alloc(bytesToRead);
    const { bytesRead } = await fh.read(buffer, 0, bytesToRead, start);
    return nonEmptyTailLines(buffer.subarray(0, bytesRead).toString('utf8'), maxLines, start > 0);
  } catch (_err) {
    return [];
  } finally {
    if (fh) {
      try {
        await fh.close();
      } catch (_err) { /* already closed */ }
    }
  }
}

function readRecentJsonlLinesSync(filePath, {
  maxLines = LIVE_LATEST_MAX_LINES,
  maxBytes = LIVE_JSONL_TAIL_MAX_BYTES,
  generations = LIVE_JSONL_GENERATIONS,
} = {}) {
  const lines = [];
  jsonlPathsOldestFirst(filePath, generations).forEach((candidate) => {
    lines.push(...readLastNonEmptyLinesSync(candidate, maxLines, maxBytes));
  });
  return lines.slice(-maxLines);
}

async function readRecentJsonlLines(filePath, {
  maxLines = LIVE_HISTORY_MAX_LINES,
  maxBytes = LIVE_JSONL_TAIL_MAX_BYTES,
  generations = LIVE_JSONL_GENERATIONS,
} = {}) {
  const lines = [];
  for (const candidate of jsonlPathsOldestFirst(filePath, generations)) {
    lines.push(...await readLastNonEmptyLines(candidate, maxLines, maxBytes));
  }
  return lines.slice(-maxLines);
}

async function renameIfExists(from, to) {
  try {
    await fsp.rename(from, to);
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
  }
}

async function unlinkIfExists(filePath) {
  try {
    await fsp.unlink(filePath);
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
  }
}

async function rotateJsonlFileForAppend(filePath, lineBytes, {
  maxBytes = LIVE_JSONL_MAX_BYTES,
  generations = LIVE_JSONL_GENERATIONS,
} = {}) {
  if (maxBytes <= 0 || generations <= 0) return;
  let stat;
  try {
    stat = await fsp.stat(filePath);
  } catch (err) {
    if (err.code === 'ENOENT') return;
    throw err;
  }
  if (!stat.isFile() || stat.size === 0 || stat.size + lineBytes <= maxBytes) return;
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  await unlinkIfExists(rotatedJsonlPath(filePath, generations));
  for (let i = generations - 1; i >= 1; i -= 1) {
    await renameIfExists(rotatedJsonlPath(filePath, i), rotatedJsonlPath(filePath, i + 1));
  }
  await renameIfExists(filePath, rotatedJsonlPath(filePath, 1));
}

async function appendJsonlRotating(filePath, line, options = {}) {
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  await rotateJsonlFileForAppend(filePath, Buffer.byteLength(line), options);
  await fsp.appendFile(filePath, line);
}

let liveEventFileQueue = Promise.resolve();
const liveCommandFileQueues = new Map();

function queueLiveEventFiles(event) {
  const line = JSON.stringify(event) + '\n';
  const dailyPath = path.join(LIVE_DIR, 'daily', `${utcDay(event.observed_at)}.jsonl`);
  liveEventFileQueue = liveEventFileQueue
    .then(async () => {
      await appendJsonlRotating(LIVE_EVENTS_PATH, line, {
        maxBytes: LIVE_JSONL_MAX_BYTES,
        generations: LIVE_JSONL_GENERATIONS,
      });
      await appendJsonlRotating(dailyPath, line, {
        maxBytes: LIVE_JSONL_MAX_BYTES,
        generations: LIVE_JSONL_GENERATIONS,
      });
    })
    .catch((err) => console.error('live event file append failed:', err.message));
  return liveEventFileQueue;
}

function queueJsonlAppendByPath(queueMap, filePath, line, options, label) {
  const previous = queueMap.get(filePath) || Promise.resolve();
  const next = previous
    .catch(() => {})
    .then(() => appendJsonlRotating(filePath, line, options))
    .catch((err) => console.error(`${label} append failed:`, err.message))
    .finally(() => {
      if (queueMap.get(filePath) === next) queueMap.delete(filePath);
    });
  queueMap.set(filePath, next);
  return next;
}

function queueLiveCommandFile(gatewayId, cmd) {
  const filePath = path.join(LIVE_COMMAND_DIR, `${safeDeviceKey(gatewayId)}.jsonl`);
  return queueJsonlAppendByPath(
    liveCommandFileQueues,
    filePath,
    `${JSON.stringify(cmd)}\n`,
    {
      maxBytes: LIVE_COMMAND_JSONL_MAX_BYTES,
      generations: LIVE_COMMAND_JSONL_GENERATIONS,
    },
    'live command file',
  );
}

function liveCommandQueuesIdle() {
  return Promise.all(Array.from(liveCommandFileQueues.values()));
}

function setLiveDbAppendRunnerForTest(runner) {
  liveDbAppendRunner = runner || runLiveDbAppend;
}

function drainLivePersistenceForTest() {
  if (liveRegistrySaveScheduled) flushQueuedLiveRegistrySave();
  return Promise.all([
    liveEventFileQueue,
    liveCommandQueuesIdle(),
    liveRegistryPersistQueue,
    liveDbQueueIdle(),
  ]);
}

function mergeLiveLatestEvent(previous, event, pref = {}) {
  const merged = {
    ...previous,
    ...event,
    position: event.position || previous.position,
    motion: event.motion || previous.motion,
    label: pref.label || event.label || previous.label || event.device_id,
    color: pref.color || previous.color || event.color,
    visible: pref.visible !== false,
    last_event_observed_at: event.observed_at,
    last_event_received_at: event.received_at,
  };
  if (event.position) {
    merged.position_observed_at = event.observed_at;
    merged.position_received_at = event.received_at;
  } else {
    merged.position_observed_at = previous.position_observed_at || previous.observed_at;
    merged.position_received_at = previous.position_received_at || previous.received_at;
  }
  return merged;
}

function rememberLiveEvent(event) {
  const reg = liveRegistry();
  if (isGatewaySelfEvent(event, reg)) {
    if (reg.devices[event.device_id]) {
      delete reg.devices[event.device_id];
      queueLiveRegistrySave();
    }
    liveLatest.delete(event.device_id);
    return;
  }
  const pref = reg.devices[event.device_id] || {};
  const configured = isConfiguredLiveDevice(event.device_id, pref, event);
  if (configured && event.label && event.label !== event.device_id && (!pref.label || pref.label === event.device_id)) {
    pref.label = event.label;
  }
  if (configured) {
    if (pref.visible === undefined) pref.visible = true;
    const color = cleanLiveColor(pref.color);
    if (color) pref.color = color;
    pref.last_seen_at = event.received_at;
    if (event.link?.gateway_id) pref.gateway_id = safeDeviceKey(event.link.gateway_id);
    reg.devices[event.device_id] = pref;
    queueLiveRegistrySave();
  }

  const previous = liveLatest.get(event.device_id) || {};
  const merged = mergeLiveLatestEvent(previous, event, pref);
  liveLatest.set(event.device_id, merged);
  queueLiveEventFiles(event);
  queueLiveDbAppend(event);
  liveStreams.forEach((client) => {
    const options = client.__liveOptions || {};
    if (!options.includeDiscovered && !configured) return;
    const payload = `event: live\ndata: ${JSON.stringify({
      ...merged,
      label: pref.label || merged.label || event.device_id,
      color: cleanLiveColor(pref.color) || merged.color,
      visible: pref.visible !== false,
      configured,
      freshness: liveDeviceFreshness(merged, pref, reg),
    })}\n\n`;
    client.write(payload);
  });
}

function loadLiveLatest() {
  try {
    const lines = readRecentJsonlLinesSync(LIVE_EVENTS_PATH, {
      maxLines: LIVE_LATEST_MAX_LINES,
      maxBytes: LIVE_LATEST_TAIL_MAX_BYTES,
      generations: LIVE_JSONL_GENERATIONS,
    });
    const reg = liveRegistry();
    lines.forEach((line) => {
      try {
        const event = JSON.parse(line);
        if (!event.device_id) return;
        const pref = reg.devices[event.device_id] || {};
        const previous = liveLatest.get(event.device_id) || {};
        liveLatest.set(event.device_id, mergeLiveLatestEvent(previous, event, {
          ...pref,
          color: cleanLiveColor(pref.color) || pref.color,
        }));
      } catch (_err) { /* skip corrupt line */ }
    });
  } catch (err) {
    console.error('live latest load failed:', err.message);
  }
}
loadLiveLatest();

function liveDeviceFreshness(event, pref, reg, nowMs = Date.now()) {
  const gatewayId = liveEventGatewayId(event, pref);
  const bridge = gatewayId ? liveBridgeStatus(gatewayId) : { state: 'unknown' };
  const positionAt = timestampMs(event.position_observed_at || event.observed_at);
  const lastEventReceivedAt = timestampMs(event.last_event_received_at || event.received_at);
  const ageMs = positionAt === null ? null : Math.max(0, nowMs - positionAt);
  let state = 'active';
  let reason = 'location is current';
  if (!event.position) {
    state = 'no_location';
    reason = 'no location packet has been received';
  } else if (gatewayId && !liveBridgeConnected(gatewayId)) {
    state = 'offline';
    reason = `gateway bridge is ${bridge.state || 'stopped'}`;
  } else if (ageMs === null || ageMs > LIVE_POSITION_STALE_MS) {
    state = 'offline';
    reason = 'location is too old';
  } else if (ageMs > LIVE_POSITION_ACTIVE_MS) {
    state = 'stale';
    reason = 'location has not updated recently';
  }
  return {
    state,
    active: state === 'active',
    stale: state !== 'active',
    reason,
    gateway_id: gatewayId || null,
    gateway_state: gatewayId ? bridge.state : null,
    position_observed_at: event.position_observed_at || null,
    position_received_at: event.position_received_at || null,
    age_seconds: ageMs === null ? null : Math.round(ageMs / 1000),
    active_after_seconds: Math.round(LIVE_POSITION_ACTIVE_MS / 1000),
    offline_after_seconds: Math.round(LIVE_POSITION_STALE_MS / 1000),
    last_event_observed_at: event.last_event_observed_at || event.observed_at || null,
    last_event_received_at: event.last_event_received_at || event.received_at || null,
    last_packet_age_seconds: lastEventReceivedAt === null ? null : Math.round(Math.max(0, nowMs - lastEventReceivedAt) / 1000),
  };
}

function liveSnapshot({
  reg = liveRegistry(),
  computeGatewayNodeIds = liveGatewayNodeIds,
  gatewayNodeIds = null,
  includeDiscovered = false,
} = {}) {
  const gatewaySelfNodeIds = gatewayNodeIds || computeGatewayNodeIds(reg);
  const nowMs = Date.now();
  const registeredGatewayIds = new Set((reg.gateways || []).map((g) => safeDeviceKey(g.id)));
  let cleaned = false;
  Object.keys(reg.devices || {}).forEach((deviceId) => {
    if (gatewaySelfNodeIds.has(deviceId)) {
      delete reg.devices[deviceId];
      liveLatest.delete(deviceId);
      cleaned = true;
    }
  });
  if (cleaned && reg === liveRegistryCache) queueLiveRegistrySave();
  return {
    schema: 'veil.live.snapshot.v1',
    updated_at: new Date().toISOString(),
    gateways: reg.gateways.map((gateway) => ({
      ...gateway,
      bridge: liveBridgeStatus(gateway.id),
    })),
    devices: [...liveLatest.values()]
      .filter((event) => !isGatewaySelfEvent(event, reg, gatewaySelfNodeIds))
      .filter((event) => {
        const pref = reg.devices[event.device_id] || {};
        const gatewayId = liveEventGatewayId(event, pref);
        return !gatewayId || registeredGatewayIds.has(gatewayId);
      })
      .filter((event) => {
        const pref = reg.devices[event.device_id] || {};
        return includeDiscovered || isConfiguredLiveDevice(event.device_id, pref, event);
      })
      .map((event) => {
        const pref = reg.devices[event.device_id] || {};
        const configured = isConfiguredLiveDevice(event.device_id, pref, event);
        return {
          ...event,
          label: pref.label || event.label || event.device_id,
          color: cleanLiveColor(pref.color) || event.color,
          visible: pref.visible !== false,
          configured,
          freshness: liveDeviceFreshness(event, pref, reg, nowMs),
        };
      }),
    preferences: reg.devices,
  };
}

function liveBridgeStatus(id) {
  const bridge = liveBridgeProcesses.get(id);
  if (!bridge) return { state: 'stopped' };
  return {
    state: bridge.state,
    pid: bridge.proc?.pid || null,
    started_at: bridge.started_at,
    stopped_at: bridge.stopped_at || null,
    exit_code: bridge.exit_code ?? null,
    last_line: bridge.last_line || null,
    error: bridge.error || null,
    desired: bridge.desired !== false,
    retry_attempt: bridge.retry_attempt || 0,
    next_retry_at: bridge.next_retry_at || null,
  };
}

function clearLiveBridgeRetry(bridge) {
  if (bridge?.retry_timer) {
    clearTimeout(bridge.retry_timer);
    bridge.retry_timer = null;
  }
  if (bridge) bridge.next_retry_at = null;
}

function scheduleLiveBridgeRetry(gateway, bridge, reason) {
  if (!bridge || bridge.desired === false) return;
  clearLiveBridgeRetry(bridge);
  bridge.retry_attempt = (bridge.retry_attempt || 0) + 1;
  const delay = Math.min(
    LIVE_BRIDGE_RETRY_MAX_MS,
    LIVE_BRIDGE_RETRY_BASE_MS * (2 ** Math.min(bridge.retry_attempt - 1, 6))
  );
  bridge.state = 'retrying';
  bridge.error = reason || bridge.error || null;
  bridge.next_retry_at = new Date(Date.now() + delay).toISOString();
  bridge.retry_timer = setTimeout(() => {
    bridge.retry_timer = null;
    try {
      startLiveBridge(gateway, { resetBackoff: false });
    } catch (err) {
      bridge.error = err.message;
      scheduleLiveBridgeRetry(gateway, bridge, err.message);
      broadcastLiveSnapshot();
    }
  }, delay);
  bridge.retry_timer.unref?.();
  broadcastLiveSnapshot();
}

function terminateLiveBridgeProcess(proc) {
  if (!proc || proc.killed) return;
  try {
    proc.kill('SIGTERM');
  } catch (_err) { /* already gone */ }
  const pid = proc.pid;
  setTimeout(() => {
    if (!pid) return;
    try {
      process.kill(pid, 0);
      process.kill(pid, 'SIGKILL');
    } catch (_err) { /* already gone */ }
  }, 2500).unref();
}

function sameLiveGatewayTarget(a = {}, b = {}) {
  const transportA = a.transport === 'internet' ? 'internet' : a.transport;
  const transportB = b.transport === 'internet' ? 'internet' : b.transport;
  return transportA
    && transportA === transportB
    && String(a.address || '').trim().toLowerCase() === String(b.address || '').trim().toLowerCase()
    && String(a.address || '').trim() !== '';
}

function localLiveUrl() {
  if (process.env.VEIL_SELF_URL) return process.env.VEIL_SELF_URL.replace(/\/+$/, '');
  return `http://127.0.0.1:${PORT}`;
}

function liveBridgeArgs(gateway) {
  const transport = gateway.transport === 'internet' ? 'internet' : gateway.transport;
  const args = [
    path.join(ROOT, 'scripts', 'live', 'meshtastic_serial_bridge.py'),
    '--veil', localLiveUrl(),
    '--transport', transport,
    '--gateway-id', gateway.id,
    '--gateway-name', gateway.name || gateway.id,
    '--command-file', path.join(LIVE_COMMAND_DIR, `${safeDeviceKey(gateway.id)}.jsonl`),
  ];
  // The token is injected via the child env (VEIL_LIVE_TOKEN) in startLiveBridge,
  // NOT argv: /proc/<pid>/cmdline is world-readable while /proc/<pid>/environ is
  // owner-only, so argv would leak the live-API secret to any local user.
  if (transport === 'serial') {
    if (gateway.address) args.push('--port', gateway.address);
  } else if (transport === 'bluetooth') {
    if (gateway.address) args.push('--address', gateway.address);
  } else if (transport === 'internet') {
    if (gateway.address) {
      let host = gateway.address;
      try {
        host = new URL(host.includes('://') ? host : `tcp://${host}`).hostname;
      } catch (_err) { /* use raw address */ }
      args.push('--host', host);
    }
  }
  return args;
}

function captureLiveBridgeOutput(bridge, gateway, chunk, isError, log = console.log) {
  const lines = String(chunk).split('\n').filter(Boolean);
  lines.forEach((line) => {
    bridge.last_line = line.slice(-500);
    if (isError) bridge.error = bridge.last_line;
    else bridge.error = null;
    log(`live bridge ${gateway.id}: ${line}`);
  });
  if (lines.length) scheduleLiveBridgeLogSnapshot(bridge);
}

function startLiveBridge(gateway, options = {}) {
  const existing = liveBridgeProcesses.get(gateway.id);
  if (existing?.proc && existing.state === 'running') {
    existing.desired = false;
    clearLiveBridgeRetry(existing);
    existing.state = 'stopping';
    terminateLiveBridgeProcess(existing.proc);
  } else if (existing) {
    clearLiveBridgeRetry(existing);
  }
  for (const [id, bridge] of liveBridgeProcesses.entries()) {
    if (id === gateway.id || !sameLiveGatewayTarget(bridge.gateway, gateway)) continue;
    bridge.desired = false;
    clearLiveBridgeRetry(bridge);
    if (bridge.proc && bridge.state === 'running') {
      bridge.state = 'stopping';
      terminateLiveBridgeProcess(bridge.proc);
    }
    liveBridgeProcesses.delete(id);
  }
  if (['bluetooth', 'serial'].includes(gateway.transport) && !gateway.address) {
    throw new Error(`${gateway.transport} gateways need a selected ${gateway.transport === 'serial' ? 'port' : 'device address'}; use Scan first`);
  }
  if (gateway.transport === 'internet' && !gateway.address) {
    throw new Error('internet gateways need a host or URL');
  }
  const py = LIVE_PYTHON;
  const liveBridgeEnv = { ...process.env, TWIN_DATA_DIR: DATA_DIR, PYTHONUNBUFFERED: '1' };
  const token = liveToken();
  if (token) liveBridgeEnv.VEIL_LIVE_TOKEN = token; // owner-only env, not world-readable argv
  const proc = spawn(py, liveBridgeArgs(gateway), {
    cwd: ROOT,
    env: liveBridgeEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const bridge = {
    proc,
    gateway: { ...gateway },
    state: 'running',
    started_at: new Date().toISOString(),
    last_line: null,
    error: null,
    desired: true,
    retry_attempt: options.resetBackoff === false ? (existing?.retry_attempt || 0) : 0,
  };
  liveBridgeProcesses.set(gateway.id, bridge);
  proc.stdout.on('data', (c) => captureLiveBridgeOutput(bridge, gateway, c, false));
  proc.stderr.on('data', (c) => captureLiveBridgeOutput(bridge, gateway, c, true));
  proc.on('error', (err) => {
    bridge.error = err.message;
    bridge.stopped_at = new Date().toISOString();
    if (bridge.desired !== false) {
      scheduleLiveBridgeRetry(gateway, bridge, err.message);
    } else {
      bridge.state = 'error';
      cancelLiveBridgeLogSnapshot(bridge);
      broadcastLiveSnapshot();
    }
  });
  proc.on('close', (code) => {
    const shouldRetry = bridge.desired !== false;
    bridge.state = shouldRetry ? 'retrying' : (code === 0 ? 'stopped' : 'error');
    bridge.exit_code = code;
    bridge.stopped_at = new Date().toISOString();
    cancelLiveBridgeLogSnapshot(bridge);
    broadcastLiveSnapshot();
    if (shouldRetry && !bridge.retry_timer) {
      scheduleLiveBridgeRetry(gateway, bridge, code === 0 ? 'bridge exited; reconnecting' : `bridge exited ${code}; reconnecting`);
    }
  });
  broadcastLiveSnapshot();
  return liveBridgeStatus(gateway.id);
}

function handleLiveEvent(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) {
      const status = err.message === 'body too large' ? 413 : 400;
      return send(res, status, JSON.stringify({ ok: false, error: err.message }),
        { 'Content-Type': 'application/json' });
    }
    let event;
    try {
      event = normalizeLiveEvent(body);
      rememberLiveEvent(event);
    } catch (e) {
      return send(res, 400, JSON.stringify({ ok: false, error: e.message }),
        { 'Content-Type': 'application/json' });
    }
    send(res, 200, JSON.stringify({ ok: true, event }),
      { 'Content-Type': 'application/json' });
  });
}

function handleLiveLatest(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  send(res, 200, JSON.stringify(liveSnapshot({ includeDiscovered: liveDiscoveryEnabled(query) })),
    { 'Content-Type': 'application/json' });
}

function handleLiveDiscover(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  const transport = String(query.get('transport') || 'serial').toLowerCase();
  if (!['serial', 'bluetooth'].includes(transport)) {
    return send(res, 400, JSON.stringify({ ok: false, error: 'transport must be serial or bluetooth' }),
      { 'Content-Type': 'application/json' });
  }
  const timeout = transport === 'bluetooth' ? '8' : '1';
  const py = spawn(LIVE_PYTHON, [
    path.join(ROOT, 'scripts', 'live', 'discover_devices.py'),
    '--transport', transport,
    '--timeout', timeout,
  ], {
    cwd: ROOT,
    env: { ...process.env, TWIN_DATA_DIR: DATA_DIR },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  let done = false;
  const finish = (status, payload) => {
    if (done) return;
    done = true;
    send(res, status, JSON.stringify(payload), { 'Content-Type': 'application/json' });
  };
  py.stdout.on('data', (c) => { stdout += c; });
  py.stderr.on('data', (c) => { stderr += c; });
  py.on('error', (err) => finish(500, { ok: false, error: err.message }));
  py.on('close', (code) => {
    if (code !== 0) {
      return finish(500, { ok: false, error: stderr.slice(-1000).trim() || `discovery exited ${code}` });
    }
    try {
      finish(200, { ok: true, ...JSON.parse(stdout.trim() || '{}') });
    } catch (e) {
      finish(500, { ok: false, error: `unparseable discovery output: ${e.message}` });
    }
  });
  setTimeout(() => {
    if (!done) {
      py.kill();
      finish(504, { ok: false, error: 'discovery timed out' });
    }
  }, transport === 'bluetooth' ? 15000 : 5000);
}

function handleLiveStream(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
  });
  res.__liveOptions = { includeDiscovered: liveDiscoveryEnabled(query) };
  res.write(`event: snapshot\ndata: ${JSON.stringify(liveSnapshot(res.__liveOptions))}\n\n`);
  liveStreams.add(res);
  req.on('close', () => liveStreams.delete(res));
}

function handleLiveGateway(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const transport = String(body.transport || 'bluetooth').toLowerCase();
    const protocol = String(body.protocol || 'meshtastic').toLowerCase();
    if (!LIVE_TRANSPORTS.has(transport)) {
      return send(res, 400, JSON.stringify({ ok: false, error: `unsupported transport: ${transport}` }),
        { 'Content-Type': 'application/json' });
    }
    const now = new Date().toISOString();
    const reg = liveRegistry();
    const explicitId = safeDeviceKey(body.gateway_id || body.device_id || '');
    const address = String(body.address || '').slice(0, 200);
    const candidate = { transport, address };
    const sameTarget = explicitId ? null : reg.gateways.find((g) => sameLiveGatewayTarget(g, candidate));
    const id = safeDeviceKey(explicitId || sameTarget?.id || body.name || `gateway-${Date.now()}`);
    const gateway = {
      id,
      name: String(body.name || id).slice(0, 120),
      protocol,
      transport,
      address,
      node_id: body.node_id ? safeDeviceKey(body.node_id) : undefined,
      created_at: body.created_at || now,
      updated_at: now,
    };
    const idx = reg.gateways.findIndex((g) => g.id === id);
    if (idx >= 0) reg.gateways[idx] = { ...reg.gateways[idx], ...gateway, created_at: reg.gateways[idx].created_at || gateway.created_at };
    else reg.gateways.push(gateway);
    const savedGateway = reg.gateways.find((g) => g.id === id) || gateway;
    saveLiveRegistry(reg);
    let bridge = liveBridgeStatus(id);
    if (body.connect === true) {
      try {
        bridge = startLiveBridge(savedGateway);
      } catch (e) {
        return send(res, 400, JSON.stringify({ ok: false, gateway: savedGateway, registry: reg, error: e.message }),
          { 'Content-Type': 'application/json' });
      }
    }
    send(res, 200, JSON.stringify({ ok: true, gateway: { ...savedGateway, bridge }, registry: reg }),
      { 'Content-Type': 'application/json' });
  });
}

function writeLiveSnapshotToStreams() {
  if (!liveStreams.size) return;
  liveStreams.forEach((client) => {
    const payload = `event: snapshot\ndata: ${JSON.stringify(liveSnapshot(client.__liveOptions || {}))}\n\n`;
    client.write(payload);
  });
}

function broadcastLiveSnapshot() {
  writeLiveSnapshotToStreams();
}

function cancelLiveBridgeLogSnapshot(bridge) {
  if (!bridge?.log_snapshot_timer) return;
  clearTimeout(bridge.log_snapshot_timer);
  bridge.log_snapshot_timer = null;
}

function scheduleLiveBridgeLogSnapshot(bridge, delayMs = LIVE_BRIDGE_LOG_SNAPSHOT_DEBOUNCE_MS) {
  if (!liveStreams.size || bridge?.log_snapshot_timer) return;
  bridge.log_snapshot_timer = setTimeout(() => {
    bridge.log_snapshot_timer = null;
    broadcastLiveSnapshot();
  }, delayMs);
  bridge.log_snapshot_timer.unref?.();
}

function stopLiveBridge(id) {
  const bridge = liveBridgeProcesses.get(id);
  if (bridge) {
    bridge.desired = false;
    clearLiveBridgeRetry(bridge);
    cancelLiveBridgeLogSnapshot(bridge);
  }
  if (bridge?.proc && bridge.state === 'running') {
    bridge.state = 'stopping';
    terminateLiveBridgeProcess(bridge.proc);
  }
}

function handleLiveGatewayRestart(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const id = safeDeviceKey(body.gateway_id || body.id);
    if (!id) return send(res, 400, JSON.stringify({ ok: false, error: 'gateway_id is required' }), { 'Content-Type': 'application/json' });
    const reg = liveRegistry();
    const gateway = reg.gateways.find((g) => safeDeviceKey(g.id) === id);
    if (!gateway) return send(res, 404, JSON.stringify({ ok: false, error: 'gateway is not registered' }), { 'Content-Type': 'application/json' });
    try {
      const bridge = startLiveBridge(gateway);
      return send(res, 200, JSON.stringify({ ok: true, gateway: { ...gateway, bridge } }), { 'Content-Type': 'application/json' });
    } catch (e) {
      return send(res, 400, JSON.stringify({ ok: false, gateway, error: e.message }), { 'Content-Type': 'application/json' });
    }
  });
}

function handleLiveGatewayStop(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const id = safeDeviceKey(body.gateway_id || body.id);
    if (!id) return send(res, 400, JSON.stringify({ ok: false, error: 'gateway_id is required' }), { 'Content-Type': 'application/json' });
    const reg = liveRegistry();
    const gateway = reg.gateways.find((g) => safeDeviceKey(g.id) === id);
    if (!gateway) return send(res, 404, JSON.stringify({ ok: false, error: 'gateway is not registered' }), { 'Content-Type': 'application/json' });
    stopLiveBridge(id);
    broadcastLiveSnapshot();
    send(res, 200, JSON.stringify({ ok: true, gateway: { ...gateway, bridge: liveBridgeStatus(id) } }),
      { 'Content-Type': 'application/json' });
  });
}

function handleLiveGatewayRemove(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const id = safeDeviceKey(body.gateway_id || body.id);
    if (!id) return send(res, 400, JSON.stringify({ ok: false, error: 'gateway_id is required' }), { 'Content-Type': 'application/json' });
    stopLiveBridge(id);
    liveBridgeProcesses.delete(id);
    const reg = liveRegistry();
    const before = reg.gateways.length;
    reg.gateways = reg.gateways.filter((g) => g.id !== id);
    let removedDevices = 0;
    for (const [deviceId, pref] of Object.entries(reg.devices)) {
      const current = liveLatest.get(deviceId);
      if (pref.gateway_id === id || current?.link?.gateway_id === id) {
        delete reg.devices[deviceId];
        liveLatest.delete(deviceId);
        removedDevices += 1;
      }
    }
    saveLiveRegistry(reg);
    broadcastLiveSnapshot();
    send(res, 200, JSON.stringify({ ok: true, removed: before - reg.gateways.length, removed_devices: removedDevices }),
      { 'Content-Type': 'application/json' });
  });
}

function handleLiveDeviceRemove(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const id = safeDeviceKey(body.device_id || body.id);
    if (!id) return send(res, 400, JSON.stringify({ ok: false, error: 'device_id is required' }), { 'Content-Type': 'application/json' });
    const reg = liveRegistry();
    const existed = !!(reg.devices[id] || liveLatest.has(id));
    delete reg.devices[id];
    liveLatest.delete(id);
    saveLiveRegistry(reg);
    broadcastLiveSnapshot();
    send(res, 200, JSON.stringify({ ok: true, removed: existed ? 1 : 0 }),
      { 'Content-Type': 'application/json' });
  });
}

function handleLiveDevicePrefs(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const id = safeDeviceKey(body.device_id);
    if (!id) return send(res, 400, JSON.stringify({ ok: false, error: 'device_id is required' }), { 'Content-Type': 'application/json' });
    const reg = liveRegistry();
    const pref = reg.devices[id] || {};
    if (typeof body.label === 'string') {
      pref.label = body.label.slice(0, 120);
      pref.configured = true;
    }
    if (typeof body.visible === 'boolean') {
      pref.visible = body.visible;
      pref.configured = true;
    }
    if (typeof body.color === 'string') {
      const color = cleanLiveColor(body.color);
      if (!color) return send(res, 400, JSON.stringify({ ok: false, error: 'color must be #rrggbb' }), { 'Content-Type': 'application/json' });
      pref.color = color;
      pref.configured = true;
    }
    if (typeof body.gateway_id === 'string') {
      pref.gateway_id = safeDeviceKey(body.gateway_id);
      pref.configured = true;
    }
    if (typeof body.configured === 'boolean') pref.configured = body.configured;
    reg.devices[id] = pref;
    saveLiveRegistry(reg);
    const current = liveLatest.get(id);
    if (current) liveLatest.set(id, {
      ...current,
      label: pref.label || current.label,
      color: cleanLiveColor(pref.color) || current.color,
      visible: pref.visible !== false,
    });
    broadcastLiveSnapshot();
    send(res, 200, JSON.stringify({ ok: true, device: { device_id: id, ...pref } }),
      { 'Content-Type': 'application/json' });
  });
}

function handleLiveDeviceCommand(req, res, query) {
  if (!liveAuthorized(req, query)) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }),
      { 'Content-Type': 'application/json' });
  }
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const deviceId = safeDeviceKey(body.device_id || body.id);
    const gatewayId = safeDeviceKey(body.gateway_id);
    const command = String(body.command || 'request_position').toLowerCase();
    if (!deviceId) return send(res, 400, JSON.stringify({ ok: false, error: 'device_id is required' }), { 'Content-Type': 'application/json' });
    if (!gatewayId) return send(res, 400, JSON.stringify({ ok: false, error: 'gateway_id is required' }), { 'Content-Type': 'application/json' });
    if (!['request_position', 'traceroute'].includes(command)) {
      return send(res, 400, JSON.stringify({ ok: false, error: `unsupported command: ${command}` }), { 'Content-Type': 'application/json' });
    }
    const reg = liveRegistry();
    const gateway = reg.gateways.find((g) => g.id === gatewayId);
    if (!gateway) return send(res, 404, JSON.stringify({ ok: false, error: 'gateway is not registered' }), { 'Content-Type': 'application/json' });
    const bridge = liveBridgeStatus(gatewayId);
    if (bridge.state !== 'running') {
      return send(res, 409, JSON.stringify({ ok: false, error: 'gateway bridge is not running' }), { 'Content-Type': 'application/json' });
    }
    const cmd = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
      command,
      device_id: deviceId,
      gateway_id: gatewayId,
      channel_index: Number.isInteger(body.channel_index) ? body.channel_index : 0,
      hop_limit: Number.isInteger(body.hop_limit) ? body.hop_limit : null,
      queued_at: new Date().toISOString(),
    };
    queueLiveCommandFile(gatewayId, cmd);
    send(res, 202, JSON.stringify({ ok: true, queued: cmd }), { 'Content-Type': 'application/json' });
  });
}

function handleLiveDays(req, res, query) {
  if (!liveAuthorized(req, query)) return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }), { 'Content-Type': 'application/json' });
  const dir = path.join(LIVE_DIR, 'daily');
  let days = [];
  try {
    days = fs.readdirSync(dir)
      .map((f) => (/^(\d{4}-\d{2}-\d{2})\.jsonl$/.exec(f) || [])[1])
      .filter(Boolean)
      .sort();
  } catch (_err) { /* none yet */ }
  send(res, 200, JSON.stringify({ days }), { 'Content-Type': 'application/json' });
}

async function loadLiveHistoryEvents(day, wanted, {
  maxLines = LIVE_HISTORY_MAX_LINES,
  maxBytes = LIVE_HISTORY_TAIL_MAX_BYTES,
  generations = LIVE_JSONL_GENERATIONS,
} = {}) {
  const filePath = path.join(LIVE_DIR, 'daily', `${day}.jsonl`);
  const events = [];
  const lines = await readRecentJsonlLines(filePath, { maxLines, maxBytes, generations });
  lines.forEach((line) => {
    try {
      const event = JSON.parse(line);
      if (!wanted.size || wanted.has(event.device_id)) events.push(event);
    } catch (_err) { /* skip corrupt line */ }
  });
  events.sort((a, b) => {
    const ams = timestampMs(a.observed_at || a.received_at) ?? Number.MAX_SAFE_INTEGER;
    const bms = timestampMs(b.observed_at || b.received_at) ?? Number.MAX_SAFE_INTEGER;
    return ams - bms;
  });
  return events;
}

function handleLiveHistory(req, res, query) {
  if (!liveAuthorized(req, query)) return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }), { 'Content-Type': 'application/json' });
  const day = query.get('date');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day || '')) {
    return send(res, 400, JSON.stringify({ error: 'date=YYYY-MM-DD is required' }),
      { 'Content-Type': 'application/json' });
  }
  const wanted = new Set((query.get('device_id') || '').split(',').map((s) => safeDeviceKey(s)).filter(Boolean));
  loadLiveHistoryEvents(day, wanted)
    .then((events) => send(res, 200, JSON.stringify({ date: day, events }), { 'Content-Type': 'application/json' }))
    .catch((err) => {
      console.error('live history load failed:', err.message);
      send(res, 500, JSON.stringify({ ok: false, error: 'live history load failed' }), { 'Content-Type': 'application/json' });
    });
}

function handleLiveExport(req, res, query) {
  if (!liveAuthorized(req, query)) return send(res, 403, JSON.stringify({ ok: false, error: 'bad live token' }), { 'Content-Type': 'application/json' });
  readBodyJson(req, LIVE_MAX_BODY, (err, body) => {
    if (err) return send(res, 400, JSON.stringify({ ok: false, error: err.message }), { 'Content-Type': 'application/json' });
    const py = spawn(LIVE_STORE_PYTHON, [path.join(ROOT, 'scripts', 'live', 'live_store.py'), 'export'], {
      cwd: ROOT,
      env: { ...process.env, TWIN_DATA_DIR: DATA_DIR },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    py.stdout.on('data', (c) => { stdout += c; });
    py.stderr.on('data', (c) => { stderr += c; });
    py.on('error', (e) => send(res, 500, JSON.stringify({ ok: false, error: e.message }), { 'Content-Type': 'application/json' }));
    py.on('close', (code) => {
      if (code !== 0) {
        return send(res, 500, JSON.stringify({ ok: false, error: stderr.slice(-800).trim() || `export exited ${code}` }),
          { 'Content-Type': 'application/json' });
      }
      try {
        const lines = stdout.trim().split('\n').filter(Boolean);
        const payload = lines.length ? JSON.parse(lines[lines.length - 1]) : { ok: true };
        send(res, 200, JSON.stringify(payload), { 'Content-Type': 'application/json' });
      } catch (e) {
        console.error('live export unparseable output:', stdout.trim(), e.message);
        send(res, 200, JSON.stringify({ ok: true }), { 'Content-Type': 'application/json' });
      }
    });
    py.stdin.end(JSON.stringify(body || {}));
  });
}

// ------------------------------------------------------------- layer upload
// Hosted users can add their own GeoTIFF/vector data from the browser. All
// dropped files are saved into one upload directory so shapefile sidecars
// (.shx/.dbf/.prj) remain adjacent, then scripts/add_layer.py performs the
// existing GDAL/OGR reprojection, AOI/terrain-footprint clipping, drape asset
// generation, and viewer catalog registration.

const LAYER_UPLOAD_MAX_BYTES = Number(process.env.VEIL_LAYER_UPLOAD_MAX_BYTES || 512 * 1024 * 1024);
const LAYER_PRIMARY_EXTS = new Set([
  '.gpkg', '.geojson', '.json', '.tif', '.tiff', '.shp', '.kml', '.kmz',
  '.gpx', '.csv', '.zip',
]);

function layerSlug(value, fallback = 'layer') {
  const text = String(value || '').toLowerCase()
    .replace(/\.[a-z0-9]+$/i, '')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64);
  return text || fallback;
}

function safeUploadFileName(name) {
  const clean = path.basename(String(name || 'upload')).replace(/[^a-zA-Z0-9._ -]+/g, '_').slice(0, 160);
  return clean && clean !== '.' && clean !== '..' ? clean : `upload-${Date.now()}`;
}

function chooseLayerPrimary(files) {
  const candidates = files
    .map((file) => ({ ...file, ext: path.extname(file.path).toLowerCase() }))
    .filter((file) => LAYER_PRIMARY_EXTS.has(file.ext));
  if (!candidates.length) return null;
  return candidates.find((file) => file.ext !== '.json') || candidates[0];
}

function readAtlasCatalog(dataDir) {
  const catalogPath = path.join(dataDir, 'atlas', 'local', 'viewer-layers.json');
  try {
    return JSON.parse(fs.readFileSync(catalogPath, 'utf8'));
  } catch (_err) {
    return { layers: [] };
  }
}

function handleLayerUpload(req, res, dataDir) {
  parseMultipart(req, LAYER_UPLOAD_MAX_BYTES, (err, form) => {
    if (err) {
      const tooLarge = /too large/i.test(String(err.message || ''));
      return send(res, tooLarge ? 413 : 400,
        JSON.stringify({ ok: false, error: err.message }),
        { 'Content-Type': 'application/json' });
    }
    const rawFiles = (form.files || []).filter((file) => file.field === 'files' && file.data?.length);
    if (!rawFiles.length) {
      return send(res, 400, JSON.stringify({ ok: false, error: 'drop at least one geospatial file' }),
        { 'Content-Type': 'application/json' });
    }
    const uploadId = `${new Date().toISOString().replace(/[:.]/g, '-')}-${crypto.randomBytes(4).toString('hex')}`;
    const uploadDir = path.join(dataDir, 'uploads', 'layers', uploadId);
    fs.mkdirSync(uploadDir, { recursive: true });
    const saved = rawFiles.map((file) => {
      const filename = safeUploadFileName(file.filename);
      const filePath = path.join(uploadDir, filename);
      fs.writeFileSync(filePath, file.data);
      return { filename, path: filePath };
    });
    const primary = chooseLayerPrimary(saved);
    if (!primary) {
      return send(res, 400,
        JSON.stringify({ ok: false, error: 'no supported geospatial file found; use GeoTIFF, GeoJSON, GeoPackage, Shapefile, KML, GPX, CSV, or ZIP' }),
        { 'Content-Type': 'application/json' });
    }
    const label = String(form.fields.label || '').trim()
      || path.basename(primary.filename, path.extname(primary.filename)).replace(/[_-]+/g, ' ');
    const baseId = layerSlug(form.fields.id || label || primary.filename);
    const layerId = `${baseId}_${crypto.randomBytes(3).toString('hex')}`;
    const argv = [
      path.join(ROOT, 'scripts', 'add_layer.py'),
      primary.path,
      '--id', layerId,
      '--label', label,
      '--data-dir', dataDir,
      '--description', `User-uploaded layer from ${primary.filename}`,
    ];
    if (form.fields.src_crs) argv.push('--src-crs', String(form.fields.src_crs).trim());
    if (form.fields.layer_name) argv.push('--layer', String(form.fields.layer_name).trim());
    if (form.fields.label_field) argv.push('--label-field', String(form.fields.label_field).trim());

    const py = spawn(MCP_PYTHON, argv, {
      cwd: ROOT,
      env: { ...process.env, TWIN_DATA_DIR: dataDir },
    });
    let stdout = '';
    let stderr = '';
    let finished = false;
    const finish = (status, payload) => {
      if (finished) return;
      finished = true;
      send(res, status, JSON.stringify(payload), { 'Content-Type': 'application/json' });
    };
    py.stdout.on('data', (c) => { stdout += c; });
    py.stderr.on('data', (c) => { stderr += c; });
    py.on('error', (e) => finish(500, { ok: false, error: e.message }));
    py.on('close', (code) => {
      if (code !== 0) {
        return finish(400, {
          ok: false,
          error: stderr.trim().slice(-800) || stdout.trim().slice(-800) || `add_layer exited ${code}`,
        });
      }
      finish(200, {
        ok: true,
        layer_id: layerId,
        label,
        primary: primary.filename,
        files: saved.map((f) => f.filename),
        output: stdout.trim().slice(-1200),
        atlas: readAtlasCatalog(dataDir),
      });
    });
  });
}

// ------------------------------------------------------------ survey upload
// POST /api/survey-upload?name=... — the Survey companion write path
// (docs/survey.md). The raw request body is a zipped QField project folder.
// Durable-drop first: the zip is streamed to data/surveys/incoming/ and a
// line appended to uploads.log.jsonl BEFORE any processing (the same
// Node -> Python handoff as the placements log; the server never touches the
// gpkg). Then scripts/ingest_survey.py --pending is spawned for the
// synchronous attempt; if the Python side is sick the upload is already safe
// on disk and the next `npm run export` ingests it via the log cursor.
// Auth: if a gitignored .survey_token file exists at the repo root, the
// X-Survey-Token header must match. Without the file the route is open —
// meant for the localhost/Tailscale posture, not a public bind.

const SURVEY_MAX_BYTES = 512 * 1024 * 1024;
const SURVEY_INGEST_TIMEOUT_MS = 5 * 60 * 1000;

function surveyToken() {
  try {
    return fs.readFileSync(path.join(ROOT, '.survey_token'), 'utf8').trim();
  } catch (_err) {
    return null;
  }
}

function runSurveyIngest(callback) {
  const py = spawn('python3',
    [path.join(ROOT, 'scripts', 'ingest_survey.py'), '--pending', '--json'],
    { cwd: ROOT, env: { ...process.env, TWIN_DATA_DIR: DATA_DIR } });
  let stdout = '';
  let stderr = '';
  let done = false;
  const finish = (err, summary) => {
    if (done) return;
    done = true;
    callback(err, summary);
  };
  py.stdout.on('data', (c) => { stdout += c; });
  py.stderr.on('data', (c) => { stderr += c; });
  py.on('error', (err) => finish(err.message));
  py.on('close', (code) => {
    if (code !== 0) {
      return finish(`ingest exited ${code}: ${stderr.slice(-400).trim()}`);
    }
    // the JSON summary is the last stdout line (the journal note precedes it)
    const lines = stdout.trim().split('\n').filter(Boolean);
    try {
      finish(null, JSON.parse(lines[lines.length - 1]));
    } catch (_err) {
      finish(`unparseable ingest output: ${stdout.slice(-400).trim()}`);
    }
  });
  setTimeout(() => {
    if (!done) { py.kill(); finish('ingest timed out'); }
  }, SURVEY_INGEST_TIMEOUT_MS);
}

function handleSurveyUpload(req, res, query) {
  const token = surveyToken();
  if (token && req.headers['x-survey-token'] !== token) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad survey token' }),
      { 'Content-Type': 'application/json' });
  }
  const name = (query.get('name') || 'survey').slice(0, 80);
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'survey';
  const fileName = `${new Date().toISOString().replace(/[:.]/g, '-')}-${slug}.zip`;
  const incomingDir = path.join(DATA_DIR, 'surveys', 'incoming');
  fs.mkdirSync(incomingDir, { recursive: true });
  const filePath = path.join(incomingDir, fileName);
  const out = fs.createWriteStream(filePath);
  let bytes = 0;
  let failed = false;
  const fail = (status, error) => {
    if (failed) return;
    failed = true;
    out.destroy();
    fs.unlink(filePath, () => {});
    send(res, status, JSON.stringify({ ok: false, error }),
      { 'Content-Type': 'application/json' });
    req.destroy();
  };
  req.on('data', (chunk) => {
    bytes += chunk.length;
    if (bytes > SURVEY_MAX_BYTES) fail(413, 'upload exceeds 512 MB');
  });
  req.on('error', () => fail(400, 'upload interrupted'));
  out.on('error', (err) => fail(500, `could not store upload: ${err.message}`));
  out.on('finish', () => {
    if (failed) return;
    const logLine = JSON.stringify(
      { ts: new Date().toISOString(), file: fileName, name, bytes });
    fs.appendFileSync(path.join(DATA_DIR, 'surveys', 'uploads.log.jsonl'),
      logLine + '\n');
    console.log(`survey upload saved: ${fileName} (${bytes} bytes)`);
    runSurveyIngest((err, summary) => {
      if (err) {
        console.error(`survey ingest failed (upload kept for next export): ${err}`);
        return send(res, 200,
          JSON.stringify({ ok: false, saved: true, file: fileName, error: err }),
          { 'Content-Type': 'application/json' });
      }
      send(res, 200, JSON.stringify({ ok: true, file: fileName, ingest: summary }),
        { 'Content-Type': 'application/json' });
    });
  });
  req.pipe(out);
}

// ------------------------------------------------------------- simulation
// POST /api/simulate — the viewer's Simulation window runs a hydrology
// scenario (scripts/hydro_scenario.py). Parameters are validated here and
// passed as argv (never shell-interpolated); the script writes the scenario
// drape layers + catalog into the data bundle and prints a JSON result on
// stdout, which is relayed to the panel. Same spawn pattern as survey ingest.

const SIMULATE_TIMEOUT_MS = 2 * 60 * 1000;

function handleSimulate(req, res, dataDir = DATA_DIR) {
  readBodyJson(req, LIVE_MAX_BODY, (err, params) => {
    if (err) {
      const tooLarge = /too large/i.test(String(err.message || ''));
      return send(res, tooLarge ? 413 : 400,
        JSON.stringify({ error: tooLarge ? 'request body too large' : 'invalid JSON body' }),
        { 'Content-Type': 'application/json' });
    }
    const argv = [path.join(ROOT, 'scripts', 'hydro_scenario.py'), '--json'];
    const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
    const mode = params.mode === 'rain' ? 'rain' : 'snowmelt';
    argv.push('--mode', mode);
    if (mode === 'snowmelt') {
      if (num(params.swe_in) !== null) {
        argv.push('--swe-in', String(Math.min(40, Math.max(0, params.swe_in))));
      } else if (['median', 'p90', 'max'].includes(params.preset)) {
        argv.push('--preset', params.preset);
      }
      if (num(params.melt_days) !== null) {
        argv.push('--melt-days', String(Math.min(30, Math.max(0.5, params.melt_days))));
      }
    } else if (num(params.storm_hours) !== null) {
      argv.push('--storm-hours', String(Math.min(240, Math.max(0.5, params.storm_hours))));
    }
    if (num(params.rain_in) !== null) {
      argv.push('--rain-in', String(Math.min(15, Math.max(0, params.rain_in))));
    }
    if (['dry', 'normal', 'wet'].includes(params.antecedent)) {
      argv.push('--antecedent', params.antecedent);
    }
    if (params.frozen === true) argv.push('--frozen');

    const py = spawn(HYDRO_PYTHON, argv,
      { cwd: ROOT, env: { ...process.env, TWIN_DATA_DIR: dataDir } });
    let stdout = '';
    let stderr = '';
    let done = false;
    const finish = (status, payload) => {
      if (done) return;
      done = true;
      send(res, status, JSON.stringify(payload), { 'Content-Type': 'application/json' });
    };
    py.stdout.on('data', (c) => { stdout += c; });
    py.stderr.on('data', (c) => { stderr += c; });
    py.on('error', (err) => finish(500, { error: `could not run scenario: ${err.message}` }));
    py.on('close', (code) => {
      if (code !== 0) {
        return finish(500, { error: `scenario exited ${code}: ${stderr.slice(-400).trim()}` });
      }
      // result JSON is the last stdout line (store journal note precedes it)
      const lines = stdout.trim().split('\n').filter(Boolean);
      try {
        finish(200, JSON.parse(lines[lines.length - 1]));
      } catch (_err) {
        finish(500, { error: `unparseable scenario output: ${stdout.slice(-400).trim()}` });
      }
    });
    setTimeout(() => {
      if (!done) { py.kill(); finish(504, { error: 'scenario timed out' }); }
    }, SIMULATE_TIMEOUT_MS);
  });
}

// Persist building-model placements tuned with the in-viewer editor
// (public/viewer/building-editor.js) back into the manifest, and append every
// save to placements.log.jsonl — the handoff that scripts/ingest_placements.py
// turns into twin-store observations (the server itself never touches the gpkg).
// The editor (public/viewer/building-editor.js) only ever writes these flat
// numeric fields; everything else is rejected so attacker-controlled JSON can't
// reach manifest.json or the append-only placements journal.
const PLACEMENT_FIELDS = ['x', 'y', 'rot_x_deg', 'yaw_deg', 'rot_z_deg', 'scale', 'z_offset'];

function sanitizePlacement(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const out = {};
  for (const k of PLACEMENT_FIELDS) {
    if (k in value) {
      const n = value[k];
      if (typeof n !== 'number' || !Number.isFinite(n)) return null;
      out[k] = n;
    }
  }
  return Object.keys(out).length ? out : null;
}

function saveBuildingPlacements(req, res) {
  readBodyJson(req, LIVE_MAX_BODY, (err, placements) => {
    if (err) {
      const tooLarge = /too large/i.test(String(err.message || ''));
      return send(res, tooLarge ? 413 : 400, tooLarge ? 'request body too large' : 'invalid JSON body');
    }
    if (!placements || typeof placements !== 'object' || Array.isArray(placements)) {
      return send(res, 400, 'invalid placements: expected an object');
    }
    const modelsDir = path.join(DATA_DIR, 'buildings', 'models');
    const manifestPath = path.join(modelsDir, 'manifest.json');
    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      // Only apply/log placements for buildings that actually exist, each
      // validated to the known numeric fields — never raw, never unknown ids.
      const clean = {};
      for (const b of manifest.buildings) {
        if (Object.prototype.hasOwnProperty.call(placements, b.id)) {
          const sp = sanitizePlacement(placements[b.id]);
          if (!sp) return send(res, 400, `invalid placement for ${b.id}`);
          clean[b.id] = sp;
          b.placement = sp;
        }
      }
      if (!Object.keys(clean).length) {
        return send(res, 400, 'no valid placements for known buildings');
      }
      fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
      const logLine = JSON.stringify({ ts: new Date().toISOString(), placements: clean });
      fs.appendFileSync(path.join(modelsDir, 'placements.log.jsonl'), logLine + '\n');
      console.log('saved building placements:', JSON.stringify(clean));
      send(res, 200, 'ok');
    } catch (err2) {
      send(res, 400, 'invalid placements: ' + err2.message);
    }
  });
}

// Empty out the LLM map drawings (data/annotations.json — written by the
// MCP server's draw_polygon/draw_point tools, polled and rendered orange by
// public/annotations.js). The viewer's "Clear drawings" button posts here;
// writing an empty document (rather than unlinking) keeps the file's shape
// stable for whichever MCP process writes next. Layer-view overrides in the
// same file (set_layer_visibility/filter_layer) are cleared too, so the one
// button also hides any atlas layers the agent revealed — the client's
// follow-up poll applies the now-empty layer_views and resets the drape.
function clearAnnotations(res, dataDir = DATA_DIR) {
  const annPath = path.join(dataDir, 'annotations.json');
  try {
    fs.writeFileSync(annPath, JSON.stringify({
      version: 1, updated_at: new Date().toISOString(),
      annotations: [], layer_views: [],
    }, null, 1));
    send(res, 200, JSON.stringify({ ok: true }), { 'Content-Type': 'application/json' });
  } catch (err) {
    send(res, 500, JSON.stringify({ ok: false, error: err.message }),
      { 'Content-Type': 'application/json' });
  }
}

// CSRF / drive-by protection for state-changing routes. Browsers always attach
// an Origin (and usually Referer) on cross-site state-changing fetch/form POSTs,
// so requiring it to match Host blocks a page the operator merely visits from
// silently driving this browser-reachable server (corrupting the manifest/journal,
// spawning the hydrology/bridge processes, wiping annotations, spending the chat
// key). Non-browser clients (curl, the MCP server, the live telemetry bridge)
// send no Origin/Referer and are allowed through; token-gated routes keep their
// token check on top of this.
const CSRF_PROTECTED = new Set([
  '/api/building-placements',
  '/api/simulate',
  '/api/annotations/clear',
  '/api/chat',
  '/api/layers/upload',
  '/api/live/events',
  '/api/live/gateways', '/api/live/gateways/remove',
  '/api/live/gateways/restart', '/api/live/gateways/stop',
  '/api/live/devices', '/api/live/devices/remove', '/api/live/devices/command',
  '/api/live/export', '/api/live/token',
  '/api/survey-upload',
]);

function hostedUnsupported(pathname) {
  if (!HOSTED_MODE) return false;
  return pathname.startsWith('/api/live/')
    || pathname === '/api/building-placements'
    || pathname === '/api/survey-upload';
}

function sameOriginOk(req) {
  const host = req.headers.host;
  if (!host) return false;
  const matches = (value) => {
    if (!value) return null;
    try { return new URL(value).host === host; } catch (_err) { return false; }
  };
  const byOrigin = matches(req.headers.origin);
  if (byOrigin !== null) return byOrigin;     // Origin present -> must match Host
  const byReferer = matches(req.headers.referer);
  if (byReferer !== null) return byReferer;   // else fall back to Referer
  return true;  // no Origin/Referer -> non-browser client, not a CSRF vector
}

const server = http.createServer((req, res) => {
  let pathname;
  let requestUrl;
  try {
    requestUrl = new URL(req.url, `http://${req.headers.host}`);
    pathname = decodeURIComponent(requestUrl.pathname);
  } catch (_err) {
    return send(res, 400, 'Bad request');
  }

  if (req.method === 'POST' && CSRF_PROTECTED.has(pathname) && !sameOriginOk(req)) {
    return send(res, 403, 'Forbidden: cross-origin request rejected');
  }
  if (hostedUnsupported(pathname)) {
    return send(res, 403, JSON.stringify({
      ok: false,
      error: 'this local hardware/import route is disabled in hosted mode',
    }), { 'Content-Type': 'application/json' });
  }

  if (req.method === 'POST' && pathname === '/api/building-placements') {
    return saveBuildingPlacements(req, res);
  }

  if (req.method === 'GET' && pathname === '/api/hosted/status') {
    return send(res, 200, JSON.stringify(hostedStatusPayload(req, res)),
      { 'Content-Type': 'application/json' });
  }

  if (req.method === 'GET' && pathname === '/api/system-check') {
    return handleSystemCheck(req, res);
  }

  if (req.method === 'GET' && pathname === '/api/init-status') {
    return handleInitStatus(req, res);
  }
  if (req.method === 'GET' && pathname === '/api/init-address-search') {
    return handleInitAddressSearch(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/init-estimate') {
    return handleInitEstimate(req, res);
  }

  if (req.method === 'POST' && pathname === '/api/init-layer-scan') {
    return handleInitLayerScan(req, res);
  }
  if (req.method === 'POST' && pathname === '/api/init-layer-scan-stream') {
    return handleInitLayerScanStream(req, res);
  }

  if (req.method === 'POST' && pathname === '/api/init-aoi') {
    return handleInitAoi(req, res);
  }

  if (req.method === 'POST' && pathname === '/api/live/events') {
    return handleLiveEvent(req, res, requestUrl.searchParams);
  }

  if (req.method === 'GET' && pathname === '/api/live/latest') {
    return handleLiveLatest(req, res, requestUrl.searchParams);
  }

  if (req.method === 'GET' && pathname === '/api/live/discover') {
    return handleLiveDiscover(req, res, requestUrl.searchParams);
  }

  if (req.method === 'GET' && pathname === '/api/live/stream') {
    return handleLiveStream(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/gateways/remove') {
    return handleLiveGatewayRemove(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/gateways/restart') {
    return handleLiveGatewayRestart(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/gateways/stop') {
    return handleLiveGatewayStop(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/gateways') {
    return handleLiveGateway(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/devices/remove') {
    return handleLiveDeviceRemove(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/devices/command') {
    return handleLiveDeviceCommand(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/devices') {
    return handleLiveDevicePrefs(req, res, requestUrl.searchParams);
  }

  if (req.method === 'GET' && pathname === '/api/live/days') {
    return handleLiveDays(req, res, requestUrl.searchParams);
  }

  if (req.method === 'GET' && pathname === '/api/live/history') {
    return handleLiveHistory(req, res, requestUrl.searchParams);
  }

  if (req.method === 'POST' && pathname === '/api/live/export') {
    return handleLiveExport(req, res, requestUrl.searchParams);
  }

  if (pathname === '/api/live/token') {
    if (req.method === 'GET') return handleLiveTokenStatus(req, res);
    if (req.method === 'POST') return handleLiveTokenSet(req, res);
  }

  if (req.method === 'POST' && pathname === '/api/survey-upload') {
    return handleSurveyUpload(req, res, requestUrl.searchParams);
  }

  if (req.method === 'GET' && pathname === '/api/chat/config') {
    return handleChatConfig(res);
  }

  if (req.method === 'POST' && pathname === '/api/chat') {
    return handleChat(req, res);
  }

  if (req.method === 'POST' && pathname === '/api/layers/upload') {
    return handleLayerUpload(req, res, requestDataDir(req, res));
  }

  if (req.method === 'POST' && pathname === '/api/simulate') {
    return handleSimulate(req, res, requestDataDir(req, res));
  }

  if (req.method === 'POST' && pathname === '/api/annotations/clear') {
    return clearAnnotations(res, requestDataDir(req, res));
  }

  if (pathname === '/') {
    if (HOSTED_MODE && !twinBuilt(requestDataDir(req, res))) pathname = '/init.html';
    else pathname = '/index.html';
  }
  // directory request (any trailing-slash path) -> its index.html
  else if (pathname.endsWith('/')) pathname += 'index.html';

  // Map URL -> file. /data/* serves the twin's data bundle (DATA_DIR, which may
  // be an alternate twin via TWIN_DATA_DIR); everything else serves from /public.
  let filePath;
  let baseDir;
  if (pathname.startsWith('/data/')) {
    baseDir = requestDataDir(req, res);
    filePath = path.normalize(path.join(baseDir, pathname.slice('/data/'.length)));
  } else {
    baseDir = path.join(ROOT, 'public');
    filePath = path.normalize(path.join(ROOT, path.posix.join('/public', pathname)));
  }

  // Prevent path traversal outside the served base directory. Require a path
  // separator immediately after baseDir (or an exact match) so a prefix-sharing
  // sibling directory (e.g. data-secret / data.bak next to data) can't be
  // reached via an encoded "../" — a bare startsWith(baseDir) would accept it.
  if (filePath !== baseDir && !filePath.startsWith(baseDir + path.sep)) {
    return send(res, 403, 'Forbidden');
  }

  // Never serve dotfiles or dot-directories. A twin's optional .live_token
  // secret lives inside DATA_DIR (which is web-served at /data/), so without
  // this guard a GET /data/.live_token would hand out the live-API credential;
  // the same rule protects any future secret dropped into the served bundle.
  if (path.relative(baseDir, filePath).split(path.sep).some((seg) => seg.startsWith('.'))) {
    return send(res, 404, `Not found: ${pathname}`);
  }

  fs.stat(filePath, (err, stat) => {
    if (err || !stat.isFile()) {
      return send(res, 404, `Not found: ${pathname}`);
    }
    const ext = path.extname(filePath).toLowerCase();
    const type = MIME[ext] || 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': type, 'Content-Length': stat.size, 'Cache-Control': 'no-cache' });
    fs.createReadStream(filePath).pipe(res);
  });
});

// VEIL chat can legitimately spend many minutes in local LLM/tool rounds. Keep
// Node from closing those quiet-but-active HTTP requests before the final JSON
// answer is ready.
server.requestTimeout = 20 * 60 * 1000;
server.headersTimeout = 21 * 60 * 1000;
server.keepAliveTimeout = 75 * 1000;

function autostartLiveGateways() {
  if (!LIVE_GATEWAY_AUTOSTART) return;
  const reg = liveRegistry();
  (reg.gateways || []).forEach((gateway) => {
    if (!gateway?.id) return;
    if (['bluetooth', 'serial', 'internet'].includes(gateway.transport) && !gateway.address) return;
    try {
      startLiveBridge(gateway);
    } catch (err) {
      console.error(`live bridge ${gateway.id} autostart failed: ${err.message}`);
    }
  });
}

if (require.main === module) {
  server.listen(PORT, HOST, () => {
    console.log(`\n  VEIL digital twin`);
    console.log(`  → http://${HOST}:${PORT}`);
    console.log(`  chat: ${CHAT_PROVIDER}/${CHAT_MODEL}${CHAT_PROVIDER === 'ollama' ? ` ctx=${OLLAMA_NUM_CTX}` : ''} · MCP python: ${MCP_PYTHON} · hydro python: ${HYDRO_PYTHON} · live python: ${LIVE_PYTHON} · live-store python: ${LIVE_STORE_PYTHON}\n`);
    autostartLiveGateways();
  });
} else {
  module.exports = {
    _test: {
      normalizeLiveEvent,
      rememberLiveEvent,
      liveSnapshot,
      liveGatewayNodeIds,
      isConfiguredLiveDevice,
      liveDiscoveryEnabled,
      liveRegistry,
      liveLatest,
      liveStreams,
      liveBridgeProcesses,
      captureLiveBridgeOutput,
      drainLivePersistenceForTest,
      setLiveDbAppendRunnerForTest,
      resolveLiveStorePython,
      liveAuthorized,
      liveRequestTokens,
      isLoopbackLiveRequest,
      handleLiveTokenStatus,
      handleLiveTokenSet,
      liveToken,
      readLastNonEmptyLinesSync,
      readLastNonEmptyLines,
      readRecentJsonlLines,
      appendJsonlRotating,
      queueLiveCommandFile,
      loadLiveHistoryEvents,
      LIVE_STORE_IMPORT_CHECK,
      LIVE_COMMAND_DIR,
    },
  };
}
