# MCP server over the twin store

`scripts/mcp_server.py` exposes the twin store (`data/twin.gpkg`) and the live
telemetry side store (`data/live/telemetry.sqlite`) to LLM agents over MCP
(stdio). The durable twin store is **read-only** except for explicit scenario,
live-export, drawing, and layer-view tools.
The one viewer-facing writable surface is the map-drawing trio
(`draw_polygon` / `draw_point` / `clear_drawings`) plus the layer-view trio
(`set_layer_visibility` / `filter_layer` / `reset_layer_views`), which
together maintain `data/annotations.json` — ephemeral orange shapes and
atlas-layer overrides the 3D viewer polls and applies so an agent can point
at places (and reveal the exact map layer/region) instead of reciting
coordinates (see "Drawing on the map" below). All logic lives in
`scripts/twin_query.py` (tested by
`scripts/twin_query_test.py` against the real store); the MCP layer is thin
wrappers.

## Setup & run

```bash
pip install -r requirements.txt    # the mcp SDK (pyproj usually already present)
npm run rebuild-store              # if data/twin.gpkg doesn't exist yet
python3 scripts/mcp_server.py      # stdio server (for a client to spawn)
```

Register with Claude Code:

```bash
claude mcp add veil-twin -- python3 /ABS/PATH/TO/scripts/mcp_server.py
```

Or in a client's JSON config:

```json
{ "mcpServers": { "veil-twin": {
    "command": "python3",
    "args": ["/ABS/PATH/TO/scripts/mcp_server.py"] } } }
```

Quick sanity check without a client:

```bash
python3 scripts/twin_query.py describe_twin
# points are {x,y} scene-local meters or {lat,lon} degrees; describe_twin
# reports the extent so you can pick an in-bounds point
python3 scripts/twin_query.py identify_at '{"point":{"x":50,"y":100}}'
python3 scripts/twin_query_test.py
```

## Conventions

- **Coordinates.** Scene-local meters (x = east, y = north; EPSG:26918 minus
  `origin_utm`) — the store convention. Every point input accepts
  `{"lat","lon"}` degrees **or** `{"x","y"}` meters; every output echoes
  both. Conversion is pyproj EPSG:26918 ↔ EPSG:4269, which agrees with the
  viewer's `georef.js` to ~0.1 mm. All distances are meters.
- **Regions.** Spatial tools take one `region` object, exactly one of:
  `{"aoi": true}` · `{"bbox": [minx,miny,maxx,maxy]}` (scene-local meters) ·
  `{"within_m": r, "point": {…}}` · `{"polygon": [[lon,lat],…] or [[x,y],…]}`
  (ring auto-closed; lon/lat vertices auto-detected).
- **Provenance.** Every entity attribute returns
  `source / confidence / run_id / observed_at`; every atlas fact returns the
  layer's `acquisition` (`local_source_clip` | `api_snapshot`) and service.
- **Errors are structured** — `{"error": …}` with the valid alternatives
  (kinds, layer ids) listed, never a stack trace. Out-of-extent points get
  an `outside_extent` result with the extent.
- **Question-first layer use.** For thematic questions, an agent should first
  decide what spatial evidence would ideally answer the question, then inspect
  the available catalog with `list_layers`, then call `layer_summary` on
  promising layers. Layer ids may not match expectations; choose from the
  actual labels, natural-language metadata/descriptions, themes, fields,
  legends, status, and provenance.

## Tools

| Tool | Question it answers |
|---|---|
| `describe_place()` | Lightweight location/coordinate context — name/id, CRS, extent, AOI bounds/area |
| `describe_twin()` | "What is this place?" — origin, CRS, extent, entity counts, run history |
| `find_entities(kind, near?, within_m?, region?, attr_filters?, limit?)` | "Trees over 20 m within 50 m of the barn" |
| `get_entity(entity_id)` | Full current state + geometry of one entity |
| `entity_history(entity_id, attr?)` | One entity's observation timeline across runs |
| `identify_at(point)` | Everything true at a point (the viewer's click-to-identify) — atlas layers, GAP species habitat, containing parcel/building, **and** survey features (`survey_*`, photo + status). For water at a point use `hydrology_at` |
| `sample_raster(layer_id, point)` | One raster value + legend name at a point |
| `list_layers(kind?)` | The atlas/input catalog with provenance, text metadata/descriptions when present, themes, flags, and compact field/label/legend previews |
| `layer_summary(layer_id)` | One candidate layer in depth: metadata/description, fields/labels of a vector; legend + class shares of a raster |
| `summarize_region(region)` | "What's happening inside this shape?" — the headline call |
| `aggregate_entities(kind, metric, group_by?, where?, region?)` | Counts, mean height, crown area, splits |
| `canopy_change(region?, member?)` | "When did canopy density change here?" — per-run history |
| `list_survey_layers()` | The field-survey catalog (`survey_*` kinds, counts, fields, photos) |
| `live_telemetry_snapshot(include_hidden?, prefer_live_api?)` | Current live gateways/devices, bridge status, latest positions/messages, and freshness |
| `live_telemetry_history(date?, dates?, device_ids?, kind?, since?, until?, limit?)` | Raw replay events from the temporary telemetry SQLite store |
| `live_telemetry_store_summary()` | Recorded live days, event/device counts, recent devices, and export history |
| `export_live_telemetry_to_twin(mode?, date?, dates?, device_ids?, at?)` | Materialize selected live telemetry into `twin.gpkg` as `live_device` entities (**writes**) |
| `discover_live_connections(transport?, timeout?)` | Discover serial/Bluetooth gateway connection targets |
| `manage_live_gateway(action, gateway_id?, name?, protocol?, transport?, address?, node_id?)` | Register/connect/start/restart/stop/remove live gateway connections |
| `manage_live_device(action, device_id, gateway_id?, label?, visible?, color?, command?, channel_index?, hop_limit?)` | Update/remove tracked devices or queue position/traceroute commands |
| `hydrology_at(point)` | Water at a point: flow/wetness/ponding/seep (+ live scenario) + soil + a plain-language reading |
| `hydrology_summary()` | Property-wide water: outlet, depression storage, top seeps, validation + the last scenario |
| `run_scenario(mode, swe_in?/preset?, melt_days?, rain_in?, storm_hours?, antecedent?, frozen?)` | "What if it …" — run a snowmelt/rain event (**writes** scenario layers + a store run) |
| `draw_polygon(polygon, label?)` | Draw a labeled orange polygon on the user's live 3D map |
| `draw_point(point, label?)` | Drop a labeled orange marker on the user's live 3D map |
| `clear_drawings()` | Remove every drawn shape from the map |
| `set_layer_visibility(layer_id, visible?)` | Show/hide one atlas map layer on the user's terrain |
| `filter_layer(layer_id, values, field?)` | Reveal ONLY the selected regions of a layer (turns it on) |
| `reset_layer_views()` | Drop every agent layer override (user toggles back in control) |

## Drawing on the map

When an answer points at a place — "the densest stand is here", "put the
trail along this line of parcels", "the wettest corner is this one" — the
agent should **draw it** with `draw_polygon` / `draw_point` (vertices in
`[lon,lat]` or scene-local `[x,y]`, auto-detected like region polygons; a
short `label` shows on the map) rather than listing coordinates in text.

Mechanics: the tools append to `data/annotations.json` (scene-local
meters, atomic rewrite) and the viewer (`public/annotations.js`) polls that
file every few seconds, rendering terrain-hugging orange outlines, orange
point markers, and label sprites. This works identically for the built-in
chat panel and for any external MCP client pointed at the same twin — the
viewer doesn't care who wrote the file. Drawings are presentation-only:
they never enter the store or the journal, and they persist until cleared.
The user clears them with the chat panel's **Clear drawings** button
(`POST /api/annotations/clear`); the agent can also call `clear_drawings`,
e.g. before drawing a fresh set for a new question.

## Controlling map layers

Beyond drawing its own shapes, the agent can drive the twin's *own* atlas
layers. `set_layer_visibility(layer_id, visible)` toggles a layer's drape on
the terrain — bring up the layer the answer is about (soils, geology, land
cover, wetlands, GAP species richness — whatever the twin holds) instead of
describing it. `filter_layer(layer_id, values, field?)` goes further: it
turns the layer on but reveals **only** the selected regions, hiding the
rest. The `values` are:

- **raster categorical layers** (e.g. NLCD / LANDFIRE land cover): legend
  class names from `layer_summary(layer_id).classes[].name` — the drape
  re-renders from the value grid keeping only those classes, in their legend
  colors;
- **the GAP species-richness grid**: species common-names (default
  `field="species"`) from `layer_summary(...).filterable_species` or
  `identify_at` — the viewer paints an orange habitat mask over the cells
  where any chosen species has modeled habitat. *"Where could I find wild
  turkey?"* → `filter_layer("gap_species_richness", ["Wild Turkey"])` and the
  map lights up exactly that range;
- **vector layers** (soils, wetlands, geology…): the distinct values of
  `field` (default the feature label `__label`) from
  `layer_summary(layer_id).labels` / `.attribute_fields`.

Matching is case-insensitive; the result reports `matched_values` and any
`unmatched_values` so the agent can correct a name. Mechanics mirror the
drawings: directives are written into the same `data/annotations.json`
(under `layer_views`), polled by `public/annotations.js`, and applied by
`public/app.js` (which owns the drape) — moving the layer toggles and drape
filters to match. They are **edge-triggered**: between directive changes the
user's own manual toggles win, and a manual toggle reclaims a layer from the
agent. `reset_layer_views` drops every override (the **Clear drawings**
button leaves layer views untouched — they have their own reset). Layer
control, like drawings, never touches the store or the journal.

## Live telemetry

Live inputs are deliberately split from the durable twin store. The web server
writes every normalized packet to `data/live/events.jsonl`,
`data/live/daily/YYYY-MM-DD.jsonl`, and `data/live/telemetry.sqlite`; the JSONL
files are bounded rotating recent windows, while SQLite is the replay store.
Gateway and device preferences live in `data/live/registry.json`. The current live UI
state is available through `live_telemetry_snapshot`, which uses the running
VEIL HTTP server when available so bridge process state is included. If the
server is not reachable, it reconstructs latest device state from the SQLite
store and registry.

Use `live_telemetry_history` and `live_telemetry_store_summary` for temporary
replay data that has not been materialized. Use
`export_live_telemetry_to_twin` only when a day or snapshot should become
durable `live_device` entities queryable through `find_entities`,
`get_entity`, and `entity_history`.

Gateway/device management mirrors the Telemetry panel.
`discover_live_connections` finds serial or Bluetooth targets;
`manage_live_gateway(action="connect", ...)` registers and starts a bridge;
`action="stop"` halts the bridge while keeping the registration; and
`action="remove"` also removes current child-device registry entries.
`manage_live_device(action="request_position"|"traceroute", ...)` queues
commands through the selected gateway bridge.

## Example session

Natural-language questions and the tool calls they become:

0. **"Where would be good wild turkey trail-camera spots?"**
   First reason that useful evidence would include species/range habitat,
   land cover/forest edge, terrain, water, access, and the parcel boundary.
   Then `list_layers()` to inspect what this twin actually has, using labels,
   descriptions, themes, previews, and provenance; `layer_summary(...)` for
   promising habitat/land-cover/water/access layers; then `filter_layer(...)`
   and/or `recommend_sites(...)` using the available evidence. If no
   turkey-specific layer exists, say what was checked and name any proxy layer.

1. **"How tall are the trees right around the barn?"**
   `find_entities(kind="building_model", attr_filters=["name = Barn"])` →
   position of `building_model:B-4`, then
   `find_entities(kind="tree", near={"entity_id":"building_model:B-4"}, within_m=50, attr_filters=["height > 20"])`
   → 59 trees, nearest a 20.7 m Eastern Hemlock at 17 m (lidar, confidence 0.72).

2. **"What's the soil and habitat at this spot?"** (a point in scene-local
   meters, or `{lat,lon}`)
   `identify_at(point={"x":50,"y":100})` → the soil map unit (drainage,
   hydrologic group, slope, farmland class), surficial geology, LANDFIRE
   community, NLCD land cover, and the GAP species with modeled habitat there.

3. **"Summarize this stand."** (a polygon from the viewer's draw tool, in
   scene-local meters or a lon/lat ring)
   `summarize_region(region={"polygon":[[-50,22],[113,22],[113,190],[-50,190]]})`
   → region area, tree count + evergreen/deciduous split + mean/max height,
   dominant LANDFIRE community and its share, parcels covering it, soils,
   wetland overlap, species-richness range — each fact with provenance.

4. **"When did canopy density change in that stand?"**
   `canopy_change(region={"polygon": …})` → tree count + crown area per
   pipeline run, showing the clearance pass around the placed buildings
   (-43 trees at run 7) and any later rebuilds.

5. **"Is any of the parcel wet or protected?"**
   `summarize_region(region={"aoi": true})` → wetland and
   protected-species-area overlap shares (DEC/NWI layers, `api_snapshot`
   acquisition), plus `list_layers(kind="wetlands")` for what was checked.

6. **"Where are the springs, and what happens in a big snowmelt?"**
   `hydrology_summary()` → the drainage outlet, depression storage, and the
   top spring/seep candidates (lat/lon, 0–100 score); then
   `run_scenario(mode="snowmelt", preset="p90", rain_in=1.5)` runs the event
   and returns the runoff/infiltration partition and outlet discharge.
   `hydrology_at(point=…)` reads flow/wetness/ponding/seep + soil at any spot.

7. **"What did the field survey find here?"**
   `list_survey_layers()` → the uploaded layers and their `survey_*` kinds,
   then `find_entities(kind="survey_observations", region={…})` or
   `identify_at(point=…)` for the features (with photos) at a spot.

## Hydrology simulation

The Simulation window's engine is on the MCP server:

- `hydrology_at(point)` samples every Tier-1 derived layer (upslope
  contributing area, TWI wetness percentile, ponding depth, the spring/seep
  score) plus the live scenario's runoff/routed-flow, joins the SSURGO soil
  at the point, and returns the **same plain-language reading** the viewer
  shows (a direct port of `public/simulation.js interpretAt`).
- `hydrology_summary()` is the property-wide read: outlet, depression/pond
  storage, hydrologic soil-group fractions, the top seep candidates, the
  stream/wetland validation, and the last scenario's water budget.
- `run_scenario(…)` runs `scripts/hydro_scenario.py` with the **same clamps**
  as the viewer's `POST /api/simulate`, then returns the result. It **writes**:
  it rewrites the `scenario_runoff` / `scenario_flow` drape layers and records
  a `scenario` pipeline run (so scenario history stays queryable). Honest
  framing carried in every payload: geometry — where water concentrates — is
  reliable; discharge magnitude is ±50%-class, not a forecast.

A scenario the agent runs persists to disk immediately, but the viewer's
Simulation window only repaints on its next refresh (reload or re-toggle the
scenario layers).

## Survey companion

Field uploads (QField, docs/survey.md) land as `survey_*` store entities and
were already queryable via `find_entities` / `summarize_region` /
`aggregate_entities`. Two additions complete the exposure:

- `list_survey_layers()` is the discovery call — the uploaded layers, their
  store kinds, geometry types, feature counts, attribute fields, and whether
  photos are attached. Empty (with a note) until something is uploaded.
- `identify_at(point)` now includes survey features (polygons by containment,
  lines and points within 8 m), with each feature's photo and status — the
  click-to-identify gap is closed.

## The viewer chat panel

The viewer ships an "Ask G.A.I.A." panel (above the coordinate readout) that
exposes this MCP server through an LLM: `POST /api/chat` in `server.js`
spawns `scripts/mcp_server.py` once, speaks MCP over stdio, and hands the
tool catalog to OpenAI `gpt-5.5` (Responses API function calling; override
with `OPENAI_MODEL` / `OPENAI_REASONING_EFFORT`). The key comes from
`OPENAI_API_KEY` or the gitignored `.openai_key` file.

Three question scopes:

- **Whole land** (default) — the model calls tools as it pleases.
- **Drawn region** — "Draw region", click 3+ points on the terrain, finish;
  the polygon goes to the model with a pre-loaded `summarize_region`, and
  spatial tool calls are scoped to it. "Clear" removes it.
- **Picked point** — "Pick point", click the terrain (same raycast as the
  GPS readout); the model gets `identify_at` plus `summarize_region` within
  100 m as pre-loaded context.

The transcript shows each tool call the model made (⚙ lines), so every
answer is auditable against the store. When the model draws on the map
mid-answer (draw_polygon / draw_point), the shapes appear in orange as soon
as the reply lands; **Clear drawings** removes them.

## Phase boundaries

The store stays read-only except for two deliberate writers: `run_scenario`
(a `scenario` pipeline run + the scenario drape layers, exactly what the
viewer's Simulation window writes) and the viewer-directive tools — the
`draw_*` tools and the layer-view tools (`set_layer_visibility` /
`filter_layer` / `reset_layer_views`) — which touch only `annotations.json`,
never the store. No sensors/actuators, no HTTP
transport, no auth — later phases. The MCP server reads `data/twin.gpkg`
(plus the atlas/hydrology/soils/survey files under `data/`) directly via
`scripts/twin_store.py`; `run_scenario` shells out to `hydro_scenario.py`
(its own store connection), and the Node viewer server is involved only in
serving/clearing the annotations file for the browser.
