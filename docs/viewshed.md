# Viewshed & distant terrain — spec

> Status: **implemented in VEIL; local ring plus real 3DEP/LANDFIRE distant B/C rings can be fetched, clipped, rendered, and validated** (2026-07-08). Execution: GPT-5.5 Codex.
> Companion research dump: `RESEARCH-VIEWSHED.md` (GPT-5.5 web-research report,
> repo root) — its findings are folded into §2–§4/§10 here; read this spec, not
> it, for the build. Pattern peers: `docs/astronomy.md` (local engine + external
> oracle validation), `HYDROLOGY-RESEARCH` (pure-numpy pipeline core).
>
> **Design spine:** one radial-sweep core, two ports (numpy + browser worker),
> validated against `gdal_viewshed` + PVGIS. The MCP payoff (§8) is that
> **visibility is a region shape**, so every existing spatial tool composes with
> it — GAIA answers standalone viewshed questions *and* stacks them (visible
> forest area, "screen the build from that photo point", tower-to-lookout links,
> ridge-shaded solar siting) with no per-tool viewshed variants.
>
> Implementation note (2026-07-08): the engine code, analyzer, validation
> scripts, browser worker/UI, MCP surface, real distant-terrain fetcher, AOI
> union clip, and distant-ring renderer are implemented. The current manifest
> contains clipped ring B/C tiles sourced from USGS 3DEP staged COGs, ring-B
> LANDFIRE EVH canopy, USGS NAIPPlus JPEG imagery drapes for kept distant
> tiles, and a full ring-B validation raster for the GDAL oracle.

## 1. What this is

Three user-facing capabilities, one engine:

1. **Interactive viewshed (Simulation pane).** Drag an observer point around the
   terrain; distant terrain fades in and out live as visibility changes.
   Observer height above ground (AGL) is configurable: eye level 1.7 m,
   3-story building 10 m, towers 30 / 60 / 120 m. This is a real siting tool
   (buildings, radio towers), not a demo.
2. **POV visibility + culling.** In walk-the-land POV, see exactly what is
   visible from where you stand, and *don't render* distant terrain that
   isn't visible (GPU savings are a feature, not a side effect).
3. **Terrain vs. sky.** Per-azimuth horizon profiles feed the astronomy layer:
   when does the ridge actually block the sun (solar siting), can a dish at
   this spot see the geostationary arc, is Jupiter behind the mountain right
   now. Ties directly into `docs/astronomy.md` §8's solar-siting substrate.

The scene grid covers the near field. Everything beyond is **distant terrain**,
materialized by `scripts/fetch_distant_terrain.py` as coarser terrain rings
that share the same scene-local coordinate frame.

## 2. Research summary (what the literature actually says)

- **Algorithms.** The canonical single-observer family: R3 (exact,
  O(n³)-ish, slow), R2 (ray-march per azimuth, near-R3 accuracy, O(n²)),
  XDraw (fast, approximate, known chunk-distortion artifacts), van Kreveld
  sweep (O(n log n), exact-ish, complex). Comparisons (Franklin & Ray's
  classic work; HiXDraw, ISPRS IJGI 2019; CUDA R2, IJGIS 2014) consistently
  land on **R2 as the accuracy/speed sweet spot**; GPU implementations
  parallelize R2 rays. GDAL's `gdal_viewshed` implements Wang et al. (2000),
  a scan-line reference-plane method — solid, and importantly *available to
  us as an oracle* since GDAL is already a pipeline dependency.
- **Curvature & refraction.** Standard practice: correct target heights by
  the curvature drop with a refraction allowance,
  `drop(d) = cc · d² / (2·R_earth)`, `cc = 1 − k`. GDAL ≥3.4 defaults
  `-cc 0.85714` (= 1 − 1/7) on Earth CRSs and documents
  `Height_Corrected = H_dem − cc·d²/D_sphere`. We adopt GDAL's convention
  exactly so oracle comparisons are apples-to-apples — but **`k` is a named
  parameter, not a constant**: optical `k = 1/7` (default) and **radio
  `k = 0.25–0.33` (the 4/3-Earth model, cc ≈ 0.75–0.667)** for the tower /
  satellite use cases the user called out. The preset ships in the payload
  so an answer never silently mixes refraction models.
- **How far can you see?** The two-term mutual-visibility bound with
  refraction: `d_max_km ≈ 3.86·(√h_obs_m + √H_target_m)` (heights relative
  to the sightline's tangent plane; 3.57 without refraction). The "13-mile"
  figure is folklore — it's roughly the sea-level horizon for a ~10 m
  observer and says nothing about mountains. A 1,300 m-relative peak is
  visible from ~139 km + observer term. Archaeology-style cumulative
  viewsheds conventionally cap at 5–20 km, and ArcGIS's RTU viewshed caps at
  15 km (10/30 m DEM) / 50 km (90 m DEM) — those are compute-budget caps,
  not physics. **We size the fetch from physics** (§4) and let coarse rings
  keep it tractable.
- **Area visibility ("visible from anywhere on the AOI").** Cumulative /
  total-viewshed literature (Tabik et al.; GRASS `r.viewshed` + union
  practice; ViewShedR 2023) unions per-observer viewsheds. For our purpose —
  clipping the distant-terrain *shipment* to only-potentially-visible cells —
  a union over AOI boundary + interior sample points at **max supported AGL
  (120 m)** is the correct, conservative clip (higher observers see more, so
  clip at the ceiling; runtime observers at lower AGL see a subset).
- **DEM sources.** US: USGS **3DEP** seamless 1/3″ (~10 m) and 1″ (~30 m) —
  the twin already fetches 3DEP via
  `https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage`
  (pack script), plus pre-staged COGs on `prd-tnm.s3.amazonaws.com`. Global
  (veil): **Copernicus GLO-30 / GLO-90** COGs on AWS Open Data
  (`copernicus-dem-30m` / `-90m` buckets, 1°×1° tiles,
  `Copernicus_DSM_COG_10_N43_00_W075_00_DEM/…`, `tileList.txt` index, free
  license) — note it is a **DSM** (includes canopy/buildings), see gotcha
  §12.2. AWS Terrain Tiles (Terrarium PNGs) exist but are a mosaic of mixed
  vintage — prefer 3DEP/Copernicus for provenance.
- **Horizon profiles for solar/satellite.** GRASS `r.horizon` and pvlib are
  the reference consumers: pvlib applies a per-azimuth horizon elevation
  mask to kill DNI when the sun is below the local horizon and to trim
  diffuse (`pvlib.shading`, `iotools.get_pvgis_horizon`). **PVGIS serves an
  SRTM-derived horizon profile for any lat/lon** — a free external oracle
  for our profiles, same role Horizons plays for the ephemeris.
- **Web rendering.** Two schools: GPU depth-map viewsheds (render depth from
  the observer, shadow-map style — Cesium ion / ArcGIS JS client viewshed)
  vs. CPU-computed masks draped as textures. Codex's research leans GPU for
  drag preview; we deliberately don't, because the depth-map can't hand us a
  *horizon profile*, per-cell stats, or an MCP-answerable mask — and those
  three are the whole point (astronomy tie-in, solar/satellite, GAIA). A
  **radial** sweep is not a 2–6M-cell R2 over the grid; it is
  `n_az × n_samples` (~1440 × ~2000 ≈ 3M vectorized ops), ~10–30 ms in a
  worker, and yields the mask *and* the profile from one pass. **Decision:
  worker radial sweep is the source of truth; the GPU only renders it
  beautifully** (per-tile fade + in-shader hillshade). A GPU depth pass stays
  a documented fallback if near-field drag latency ever shows (§7), never the
  authoritative answer.

## 3. Locked design decisions

- **One algorithm, two ports, one oracle.** A polar **R2 radial sweep**
  (1440 azimuths × adaptive radial step, running-max elevation angle,
  GDAL-convention curvature+refraction with a named `k`, bilinear DEM
  sampling) implemented twice from one spec: `scripts/twin_viewshed.py`
  (numpy, vectorized over all rays at once) and inside `public/viewshed.js`
  (typed arrays, in a Worker). Validated against each other (parity) and
  against `gdal_viewshed` + PVGIS (oracles). The same sweep yields **both**
  the visibility mask and the 360° horizon profile — goals 1–3 share one core.
- **Intervisibility is the same core, not a new one.** "Can A see B" (towers,
  "is that peak visible") is one ray of the sweep: march A→B, compare B's
  corrected elevation angle to the running max. Point-to-point needs no mask,
  so it is cheap enough to run inline on any MCP call.
- **Physics-sized, visibility-clipped fetch** (the user's instinct, upgraded):
  fetch coarse to the physics bound, run the AOI-union viewshed at 120 m AGL,
  then keep only potentially-visible tiles. No 13-mile guess.
- **Multi-resolution rings**, not one grid: existing 3 m scene → 30 m ring →
  150 m ring. The sweep samples rings fine-to-coarse along each ray.
- **Canopy-lift is a v1 surface, not a v2 add-on** (promoted per user). The
  analysis surface is selectable: `surface="bare_earth"` (ground DEM only,
  the gdal-oracle-validated mode) or **`surface="canopy"` (ground DEM +
  per-cell vegetation height from LANDFIRE EVH)** — the honest "will the
  trees block this view" answer and the closest thing we have to *computing
  vegetation into the viewshed*. Observer z is always ground + AGL (you
  stand on the ground or on a tower, never on the canopy), so a tower whose
  AGL clears local EVH correctly sees *over* the forest while a 1.7 m
  observer in the same stand is boxed in. **Interactive UI, POV, and the
  MCP siting tools default to `bare_earth`** (what the land itself allows);
  `canopy` is selectable when tree blockers should be pessimistically included.
  The oracle-comparison and horizon-parity paths use `bare_earth`. EVH is a
  third co-registered raster per ring, sampled by the same `RingStack`.
- **No store writes at runtime.** The fetcher and `analyze_viewshed.py`
  register their runs/artifacts in the store (standard pipeline pattern);
  the interactive observer is viewer-local state; MCP `viewshed_from` writes
  files + catalog only (like scenario layers).
- **Honest accuracy claim.** "100 % accurate" means: exact R2 visibility on
  the chosen surface, GDAL-identical curvature/refraction, oracle-validated
  (bare-earth mode against gdal_viewshed; canopy mode is exact on the
  DEM+EVH surface). It does *not* mean survey-grade at 200 km — accuracy is
  bounded by ring resolution (±1 cell), DEM/EVH vintage, and the fact that
  EVH is a 30 m *class-binned* canopy height (§12.2), not per-stem. Every
  MCP payload says which rings, resolutions, and **surface** the answer
  came from.

## 4. Distant terrain data (`scripts/fetch_distant_terrain.py`, pack-hosted here; engine-level in veil)

**Radius.** `R_max = 3.86·(√(h_rel + AGL_MAX) + √H_rel) km`, where
`h_rel` = (scene max elev − scene min elev) from `grid.json`, `AGL_MAX = 120`,
and `H_rel` = (regional max elev − scene min elev) found by a coarse probe
(fetch a GLO-90/3DEP-1″ thumbnail of a 250 km box, take its max). For this
twin: h_rel≈79+120→√199≈14.1, H_rel≈1,340→√≈36.6 → **R_max ≈ 196 km → 200 km**.
Computed, logged, and stored in the output metadata — never hardcoded.

**Rings** (all resampled to scene-local meters, same origin/convention as
`grid.apron.json`). Each ring carries **two co-registered grids**: ground
(DEM) and **canopy height (EVH)**.

| ring | span | ground source / res | canopy source | grid ≈ | shipped as |
|---|---|---|---|---|---|
| A | 0 – 0.34 km | existing scene+apron grids (3 m) | LANDFIRE EVH → 3 m | in repo | unchanged + evh |
| B | 0.3 – 24 km | 3DEP 1/3″ → 30 m | LANDFIRE EVH (30 m native) | 1600² | Int16 dm ground + UInt8 dm canopy |
| C | 24 – R_max | 3DEP 1″ → 150 m | LANDFIRE EVH → 150 m (max-pool) | ~2670² | Int16 dm ground + UInt8 dm canopy |

**Canopy source:** LANDFIRE 2024 EVH, already wired in the pack
(`Landfire_LF2024/LF2024_EVH_CONUS/ImageServer`; VAT sidecar
`data/atlas/vat/landfire_evh_2024.json` already committed). Decode: codes
**101–133 are "Tree Height = N meters"** (N = code − 100), shrub/herb classes
similarly encode metres in their names — parse the VAT name for the metre
value; non-vegetation classes (water, developed, NoData) → 0 canopy. Store
canopy as **UInt8 decimetres capped at 255 (25.5 m)** — EVH tree classes top
out well under that, and it halves the canopy payload. Resample ring C by
**max-pool, not average** (a viewshed cares about the tallest blocker in a
cell, not the mean). LANDFIRE is CONUS-only; outside CONUS ring canopy is
absent and `surface="canopy"` transparently degrades to bare-earth per-ring
(recorded in the manifest, surfaced in payloads).

**Format:** `data/terrain/distant/ring{B,C}/tile_{i}_{j}.bin` (ground, Int16
decimetres, NODATA −32768) + `..._evh.bin` (canopy, UInt8 decimetres, 0 =
none) + optional `tile_{i}_{j}.jpg` (USGS NAIPPlus RGB imagery draped on the
kept tile) — raw elevation/canopy, JPEG imagery, 256×256 except edge tiles.
One `data/terrain/distant/manifest.json` (ring geometry, tile index, ground +
canopy + imagery source/date/res per ring, vertical datum, R_max inputs,
top-level imagery provenance, and a `canopy_available` flag per ring). Raw
fixed-width keeps server.js zero-dep; JPEG keeps photographic imagery small.
Budget ≈ (5 + 14) MB ground + ~½ that canopy *before* the clip, far less after,
plus a few MB of clipped NAIP JPEGs at the ring grid resolution.

**Source access (US, this twin):** prefer the **staged 3DEP COGs** on S3 over
the ImageServer for the far rings — one HTTP GET per 1°×1° tile instead of
thousands of `exportImage` calls:
`https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/nNNwWWW/USGS_13_nNNwWWW.tif`
(1/3″) and `.../Elevation/1/TIFF/current/...USGS_1_...tif` (1″); discover the
tile list with the TNM products API
(`https://tnmaccess.nationalmap.gov/api/v1/products?datasets=...&bbox=...&prodFormats=GeoTIFF`).
GDAL reads these COGs directly via `/vsis3/` or `/vsicurl/` and windows/
resamples them into scene-local rings — no full-tile download. The pack's
existing `exportImage` path stays as the fallback for gaps.

**Clip:** run the union viewshed (numpy core) from 32 AOI-boundary + 9
interior sample points at 120 m AGL over rings B+C; drop tiles with zero
potentially-visible cells; keep the union mask itself as
`data/viewshed/aoi_union_mask.bin` (the "could ever be seen from the parcel"
truth, also a nice drape). Registered in the store: one pipeline run,
`layers` rows with `content_sha1` per ring.

## 5. Engine core — `scripts/twin_viewshed.py` (~300 LOC, pure numpy)

API (mirrors `twin_hydrology.py` discipline — no file I/O in the core):

- `RingStack.load(manifest_path)` → sampler over rings A/B/C holding a ground
  grid and an optional canopy grid per ring. `sample(x, y, surface="canopy")`
  is bilinear on ground, and for `surface="canopy"` adds the (nearest, not
  bilinear — it's class-binned) canopy height where the ring has EVH, else
  ground alone. One sampler feeds the sweep, `line_of_sight`, and MCP point
  queries; `surface` threads through all of them.
- `sweep(stack, x, y, agl_m, n_az=1440, max_km=None, surface="canopy", k=1/7)` →
  `{visible: per-ring uint8 masks, horizon_deg: float32[n_az],
    stats: {visible_km2, max_visible_km, per-ring fractions}, surface, k}`.
  Core math: for each azimuth, radial samples of the **chosen surface** at
  each ring's own step; **observer z = ground(x,y) + agl_m always** (never
  canopy-lifted — you stand on the ground/tower); target angle
  `atan2(z_surface(d) − drop(d) − z_eye, d)`; visible iff angle > running
  max; `drop(d) = cc·d²/(2R)`, `cc = 1−k`, R = 6,371,000 m — byte-for-byte
  GDAL's convention; `k` from the refraction preset (optical 1/7, radio
  ~0.25–0.33). Vectorized: one (n_az × n_samples) elevation matrix,
  `np.maximum.accumulate` along the radial axis. No loops over azimuths.
- `line_of_sight(stack, x0, y0, agl0, x1, y1, agl1, k, surface="canopy")` →
  the intervisibility primitive behind `can_see`: march the single A→B ray
  over the chosen surface, return
  `{visible, obstruction: {x, y, dist_m, crest_z, sightline_z, deficit_m,
    is_canopy}, required_agl0_m}` — `is_canopy` tells the caller whether the
  blocker is trees (raise/clear it) or bedrock (move). One ray, no mask.
- `union_sweep(stack, points, agl_m)` → OR of masks (the fetch clip + the
  cumulative drape + `best_viewpoints`).
- `horizon_events(horizon_deg, sun_path)` → helper the MCP layer uses to
  turn a profile + a day's solar track into blocked/unblocked windows;
  `geo_arc_elevations(lat)` returns the GEO-belt elevation per azimuth for
  the satellite check.

## 6. Pipeline — `scripts/analyze_viewshed.py` (`npm run analyze-viewshed`, ~120 LOC)

One-time/occasional, after the fetcher: writes `data/viewshed/`:
- `horizon.json` — per-azimuth horizon profile from the **AOI centroid at
  1.7 m**, computed for **both surfaces** (`bare_earth` and `canopy` arrays;
  astronomy sky-lighting uses bare_earth ridgelines by default, solar siting
  uses canopy — a tree line to the south shades panels), the twin's default
  sky mask that astronomy + MCP consume,
- `aoi_union_visibility.{png,grid.json}` — draped cumulative-visibility layer
  ("what can be seen from the parcel, ever") in the standard raster-layer
  format + `viewshed-layers.json` catalog (mirrors
  `simulation-layers.json` exactly, so drape/identify/key are free),
- `summary.json` — R_max inputs, ring stats, notable visible summits
  (local maxima of visible ring-C cells with bearing/distance/elevation),
  oracle-validation results.
Registers a store pipeline run; layers by hash. Prints validation (§10).

## 7. Viewer

**Distant terrain rendering** (`public/viewer/terrain.js` + ~60 LOC in
app.js): rings B/C as LOD ring meshes built from the Int16 **ground** tiles
(vertex-decimated: ring B 1 vertex / 60 m, ring C 1 / 300 m — the *analysis*
stays at full ring resolution; only the mesh is decimated). The mesh is
ground, not canopy (canopy is analytical; the near-field parcel already
renders actual stems, so we never raster-render canopy geometry — a subtle
green tint on canopy-heavy distant cells in the shader is enough). Material:
single custom shader using NAIPPlus imagery as the base color where a tile has
`imagery`, with gentle dFdx/dFdy relief shading, distance-graded atmospheric
tint toward the horizon color, and the visibility fade mask. Tiles outside
NAIP coverage or fetch gaps keep the elevation-ramp hillshade fallback so no
tile renders black. Lazy: ground + canopy + imagery tiles fetch the first time
the Viewshed tool, POV, or the "Distant terrain" toggle is used, never at boot.

**`public/viewshed.js`** (~300 LOC, the feature owner):
- Worker-hosted sweep (same math as §5; typed arrays; one persistent buffer
  set, recompute ≤ 30 ms, throttled to animation frames while dragging).
- Simulation pane **"Viewshed"** tab: `Analyze views` toggle → click terrain
  to place the observer (reuses the chat "pick point" raycast pattern,
  readout click handler stands down the same way); drag to move; AGL
  `<select>`: 1.7 / 10 / 30 / 60 / 120 m; **surface `<select>`: "Bare
  ground" (default) / "Through the trees (canopy)"** (the
  worker holds both grids, so switching re-runs the sweep without refetch;
  `step="any"` gotcha does not bite a select). Readout: visible area km²,
  furthest visible cell (bearing + distance), "N of 360° azimuths open to
  the sky ≥ 2°", and how much the canopy costs ("trees hide 2.1 km² vs bare
  ground" when both are cheap to diff).
- Result → per-tile visibility fractions → each distant tile's shader gets a
  scalar `uVisible` eased over ~300 ms (the pop-in/out animation), plus a
  full-res mask texture on ring B for crisp near-field edges. Invisible
  tiles (fraction 0) are `visible = false` — not drawn at all.
- Observer marker: slim pole of AGL height + ring at base (annotation
  styling, orange), so a 120 m tower reads as a tower.

**POV integration** (~40 LOC in pov.js/viewshed.js): entering POV computes a
sweep from the camera position at eye height; distant tiles culled by
visibility (plus frustum as usual); recompute throttled to 0.5 s of
movement. A pane checkbox "Only render what's visible" (default **on** in
POV) — turning it off shows everything for comparison.

**Astronomy tie-in** (~40 LOC): `viewshed.js` exposes
`horizonAt(az_deg)` for the active observer (falls back to the precomputed
AOI-centroid `horizon.json`). `astronomy.js`: when photometric mode is on,
sun/moon light intensity gets a `smoothstep(horizon(az) − 0.5°, horizon(az) + 0.5°, alt)`
factor — the ridge actually shadows the valley before geometric sunset. The
status line and sun/moon identify cards gain "behind terrain until ~HH:MM"
when blocked. Sky picking is untouched (stars render regardless; you can
see what the mountain hides — it's the *lighting* and the *reports* that
respect terrain).

## 8. MCP + chat — GAIA answers & composes viewshed questions (`twin_query.py` + `mcp_server.py`, ~230 LOC)

This is the feature's payoff surface. GAIA must answer standalone viewshed
questions *and* stack visibility with every other tool the twin has. The
design that makes stacking nearly free: **visibility is a region shape**, so
the whole existing spatial toolset composes with it without new per-tool
variants.

### 8.1 The unlock — `visible_from` as a fifth `resolve_region` shape

`twin_query.resolve_region` today accepts `aoi | bbox | within_m | polygon`
and returns a `Region` (a `contains(x, y)` predicate + scene-local bounds +
area). Add two shapes that return a `Region` backed by a cached viewshed mask:

```
{"visible_from": {"point": {lat,lon}|{x,y}, "agl_m": 1.7, "max_km": null,
                  "refraction": "optical"|"radio", "target_agl_m": 0,
                  "surface": "canopy"|"bare_earth"}}   # default canopy
{"hidden_from":  { …same… }}          # complement, for "screen it from view"
```

Resolution: run `twin_viewshed.sweep` once, build a mask; `contains(x,y)` =
"the cell at (x,y) clears the observer's corrected horizon at its azimuth and
is within max_km" (`target_agl_m` lets you ask "where could a 10 m antenna be
seen from here"; `surface` decides whether trees block — default `canopy`).
**Memoize** the resolved mask keyed by
(point@0.1 m, agl, target_agl, refraction, max_km) so a stacked query that
reuses the same `visible_from` in three tool calls sweeps once (§12 gotcha).

Because it is just a `Region`, these compose immediately and with **zero new
code** in the consumers:

- `find_entities("tree", region={visible_from:{point,agl_m:60}})` — trees a
  60 m tower would see (bare-earth by default; pass `surface="canopy"` to
  include tree blockers).
- `aggregate_entities("tree","count", group_by="type", region={visible_from:…})`
  — how much forest vs. field is in view. (Note the neat self-consistency:
  the same EVH that *hides* far cells in canopy mode is what these tools
  report as "forest in view" — vegetation is genuinely computed into the
  viewshed, per the user's ask.)
- `summarize_region({visible_from:…})` — full land-cover/soil/hydrology
  breakdown of the visible area (this is the big one — it runs every atlas
  layer against the viewshed).
- `find_entities("survey_photo_points", region={hidden_from:{point:proposed_build}})`
  — survey shots that the new house would *not* overlook.
- `recommend_sites(objective="overlook", region={visible_from:…})` — siting
  inside a visibility constraint.

### 8.2 Dedicated viewshed tools

- **`viewshed_from(point, agl_m=1.7, max_km=None, refraction="optical", surface="canopy", demonstrate=False)`**
  — the sweep as a first-class answer: writes a `group:"viewshed"` drape
  layer (replacing the previous one, scenario-layer pattern; viewer
  auto-enables via the survey/scenario refetch hook) and returns
  `visible_area_km2`, `sky_open_fraction`, notable **visible summits**
  (bearing/distance/elevation/name where an atlas peak layer exists) and
  notable **blocking ridges**, plus provenance (rings, resolutions, DEM
  vintage, `k`, `surface`, and `canopy_hidden_km2` = how much the trees cost
  vs. bare ground). `demonstrate=True` also drops the observer marker (pole
  of AGL height) and animates the distant terrain — the astronomy
  `demonstrate` pattern: one call answers *and* shows it.
- **`can_see(from_point, to_point, from_agl_m=1.7, to_agl_m=0, refraction="optical", surface="canopy")`**
  — intervisibility along a single ray (the tower/"is that peak visible"
  workhorse). Returns `visible: bool`, and when blocked the **controlling
  obstruction** (bearing, distance, its crest elevation, the sightline
  elevation there, the **clearance deficit** in metres — "raise the antenna
  14 m or move 40 m north to clear" — and `is_canopy`: whether a treeline or
  the ground is the blocker), the required `from_agl` to
  just clear, and for `refraction="radio"` a first-Fresnel-zone radius note
  at a caller-supplied `freq_mhz` (stub: radius only, `r = 17.3·√(d1·d2/(f·d))`,
  flagged approximate). This is what makes GAIA useful for radio links and
  building-view questions ("will the barn block the house's view of the
  pond" = `can_see(house, pond)` with the barn in the DEM… or, honestly, a
  proposed-massing variant is future work — v1 answers against current terrain).
- **`horizon_at(point, agl_m=1.7, date=None, surface="canopy")`** — 360°
  profile (compact: 72 samples + min/max), **sun blocked/unblocked windows**
  for the date (uses `twin_astro` sun track — "the ridge blocks the sun
  until 09:12 and after 16:40 in late December"), and **`geo_arc`**:
  elevation of the geostationary belt across southern azimuths at this
  latitude vs. the local horizon → per-azimuth clear/blocked, using
  `E = atan2(cos ψ − R/r_geo, √(1 − cos²ψ))`, `cos ψ = cos(lat)·cos(Δlon)`,
  `r_geo ≈ 42164 km` (the "can a dish here see the satellite arc" answer).
  `geo_arc`/satellite use passes `refraction="radio"`; a solar-siting caller
  gets the real answer with `surface="canopy"` (a treeline to the south is
  exactly what shades a panel).
- **`best_viewpoints(region=None, agl_m=1.7, objective="area"|"sees_target",
  target=None, surface="canopy", count=3)`** — coarse cumulative pass:
  sample candidate cells over `region` (default AOI), sweep each, rank by
  visible area *or* by whether/how well they see `target`. Returns ranked
  points with stats and writes an optional `demonstrate` overlay. This is
  `recommend_sites` for views specifically, and stacks with it (a caller can
  post-filter the ranked points through `summarize_region`/hydrology to
  avoid wet or wooded spots).

### 8.3 Astronomy & irradiance become terrain-aware

- `body_position` / `sky_at` gain `terrain: {blocked: bool,
  horizon_deg_at_azimuth}` — uses the observer-point horizon when a `point`
  is passed, else the precomputed AOI-centroid profile. Lets GAIA answer
  "can I watch the eclipse from the north field, or is the sun behind Cat
  Mountain at 14:20?" by stacking `next_sky_event` → `horizon_at`/`terrain`.
- `solar_irradiance` gains a horizon adjustment: DNI zeroed when the sun is
  below the local horizon at its azimuth; diffuse trimmed by the sky-view
  fraction (pvlib convention, **not** a hard zero); payload flagged
  `horizon_adjusted: true` with the profile source. This is the honest
  solar-siting number.

### 8.4 What GAIA can now do (worked stacks — put these in `docs/mcp.md`)

- "What can I see from the top of the hill if I build a 3-storey house?" →
  `viewshed_from(point, agl_m=10, demonstrate=true)`.
- "How much of what I'd see is forest vs. open field?" →
  `aggregate_entities("tree","count", region={visible_from:{point,agl_m:10}})`
  + `summarize_region({visible_from:…})`.
- "Is Snowy Mountain visible from my parcel, and if not why?" →
  `can_see(parcel_high_point, snowy_mtn)` → blocked, controlling ridge at
  8.2 km, deficit 60 m.
- "Where should a 60 m radio tower go to reach the fire lookout?" →
  `best_viewpoints(objective="sees_target", target=lookout, agl_m=60)` then
  `can_see(...,refraction="radio")` on the winner for the Fresnel check.
- "Can the lookout see where the fire scenario spreads?" →
  `run_fire_scenario` → its arrival layer, intersected with
  `{visible_from: lookout}` via `summarize_region`/a masked overlay.
- "Best spot for solar that neither the ridge *nor the treeline* shades in
  winter?" → `best_viewpoints(surface="canopy")` ∘
  `horizon_at(date="2026-12-21", surface="canopy")` ∘ `solar_irradiance` —
  the canopy surface is what makes this answer real instead of optimistic.
- "How much would clearing the trees open up the view from here?" →
  `viewshed_from(point, surface="canopy")` vs `surface="bare_earth"`; the
  diff (`canopy_hidden_km2`) is the answer, and the drape shows exactly
  which distant cells the trees hide.
- "Screen the new build from the neighbour's survey photo point." →
  `find_entities("survey_photo_points", region={hidden_from:{point:build}})`.

### 8.5 Writers & instructions

New store-safe writers (viewshed layer files + annotations marker only,
never the store at query time): `viewshed_from`, `best_viewpoints` (when
`demonstrate`), and the `visible_from`/`hidden_from` region resolution when
it caches a drape. `can_see` / `horizon_at` are pure reads. Update the MCP
server instructions block and `docs/mcp.md` to enumerate them and to
advertise the `visible_from`/`hidden_from` region shapes on every
region-taking tool (that advertisement is what makes GAIA *reach for*
composition instead of reciting coordinates).

## 9. LOC budget (keep Codex honest)

| piece | ~LOC |
|---|---|
| `twin_viewshed.py` core (sweep + line_of_sight + union + horizon/geo + canopy surface) | 330 |
| `fetch_distant_terrain.py` (ground + EVH rings) | 240 |
| `analyze_viewshed.py` | 120 |
| `public/viewshed.js` (incl. worker code, surface toggle) | 320 |
| distant-ring mesh + shader (terrain.js/app.js) | 140 |
| pov.js + astronomy.js integration | 80 |
| `twin_query.py` region shapes + tools + `mcp_server.py` | 230 |
| index.html/shell.css UI | 60 |
| tests (twin_query_test + oracle scripts) | 190 |
| **total** | **≈ 1,710** |

The MCP tools are cheap because the `visible_from`/`hidden_from` region
shapes reuse every existing region-taking consumer (§8.1) — no per-tool
viewshed variants. Anything drifting materially past this budget needs a
reason in the PR/commit message. No new npm deps, no new Python deps
(numpy/GDAL already present).

## 10. Validation & acceptance (the astronomy pattern)

1. **GDAL oracle** (`scripts/viewshed_validate.py`): run `gdal_viewshed`
   (default `-cc 0.85714`, same observer/AGL/max-distance) against ring B's
   **ground** grid as a GeoTIFF, in `surface="bare_earth"` mode; agreement
   ≥ 98 % of in-range cells for 5 observer/AGL combos (exclude the 1-cell
   edge band; R2-vs-scanline discretization differs there — that's expected
   and documented, not a failure). Canopy mode is validated by the invariant
   in test 5, not the oracle (gdal has no EVH surface).
2. **PVGIS horizon oracle** (`scripts/fetch_pvgis_horizon.py`, one-time
   online → `data/viewshed/pvgis-horizon.json`): our AOI-centroid profile
   within ~3° RMS of PVGIS's SRTM-90 profile (coarse oracle, wide tolerance;
   catches sign/azimuth-convention bugs cold).
3. **JS↔Python parity** (`scripts/viewshed_parity_test.js`): same observer →
   identical horizon profile within 0.05°, visible-fraction within 0.5 %.
4. **`can_see` oracle**: point-to-point against `gdal_viewshed` single-cell
   results for a dozen A→B pairs (both clear and blocked, both refraction
   presets); blocked pairs must name an obstruction whose deficit sign is
   correct (raising `from_agl` by the reported deficit flips it to visible).
5. **twin_query tests**: sweep sanity (valley observer sees < ridge observer;
   AGL 120 ⊇ AGL 1.7 as a strict superset of the mask), **canopy invariant
   (canopy mask ⊆ bare_earth mask for the same observer/AGL — trees only ever
   hide, never reveal), and the see-over-canopy check (a tower with AGL above
   local EVH over a forested valley sees strictly more than a 1.7 m observer
   there)**, horizon_at blocks the sun at a known low-sun time, geo_arc shape,
   `viewshed_from` writes/replaces its layer and preserves the annotations
   doc, provenance blocks present (incl. `surface`/`canopy_hidden_km2`),
   **and the composition path**: `find_entities`/`aggregate_entities`/
   `summarize_region` with a `visible_from` region return a strict subset of
   the same query with no region, and a second call with the identical region
   hits the memo (assert the sweep ran once).
6. **Visual acceptance**: screenshot plates (fixed ASTRO_TIME) — observer at
   the house at 1.7 m vs 120 m: distant ridgeline pops in; POV plate with
   culling on/off shows identical near-field pixels; a `demonstrate` viewshed
   plate shows the observer pole + drape.

## 11. Implementation order

1. **Core + data**: `twin_viewshed.py` → `fetch_distant_terrain.py` (run it,
   commit manifest + summary, data stays gitignored per twin policy) →
   `analyze_viewshed.py` → oracle scripts green.
2. **Viewer**: distant ring meshes + lazy load → `viewshed.js` worker + Explore
   UI + fades → POV culling.
3. **Astronomy + MCP**: horizon into lighting/status/cards → `viewshed_from` /
   `horizon_at` / irradiance-horizon → chat demonstrations work end-to-end.
4. **Validation + docs**: parity/oracle tests in the suite, spec status flip,
   CLAUDE.md + docs/mcp.md sections, then the veil port (§13).

## 12. Gotchas (pin these; they will bite otherwise)

1. **Planar UTM at 200 km**: scale distortion reaches ~0.1–0.4 % off the
   central meridian. Compute the curvature drop on true planar distance and
   accept the distortion (sub-cell at ring C res); document it in the
   accuracy note. Do NOT try to go geodesic — not worth the LOC.
2. **DSM vs DTM vs DTM+EVH**: 3DEP is bare-earth (DTM). `surface="bare_earth"`
   is honestly optimistic over forest (you "see" through 20 m of canopy);
   `surface="canopy"` = DTM + LANDFIRE EVH is the realistic one and is the
   default (v1, per user). Two EVH pitfalls: (a) **it is class-binned at
   30 m** — a cell is "Tree Height = 12 m", not a smoothed surface, so
   nearest-sample it (don't bilinear-interpolate canopy) and never claim
   sub-cell canopy precision; (b) **decode via the committed VAT**
   (`data/atlas/vat/landfire_evh_2024.json`): tree/shrub/herb classes carry
   the metre value in their name, everything else (water, developed, snow,
   NoData) is 0 canopy — a wrong decode silently plants 100 m "forests".
   Copernicus (veil global path) is itself a *DSM* — it *already* includes
   canopy/buildings, so **do not add EVH on top of a DSM ring** (double
   count); the manifest's per-ring `surface_native` flag tells the sampler
   whether canopy is already baked in. Never mix sources within a ring;
   record ground source, canopy source, and whether canopy is native-DSM or
   added-EVH per ring.
3. **Vertical datum seams**: 3DEP is NAVD88; scene grid came from the same
   family so ring A/B/C are consistent here. Veil's Copernicus path is
   EGM2008 — apply a constant offset fitted on ring-overlap samples, and
   assert the seam residual < 2 m in the fetcher.
4. **Ring seams in the sweep**: sample fine-to-coarse with a one-step blend
   at ring boundaries or the horizon profile gets a sawtooth at 24 km.
5. **gdal_viewshed edge semantics**: out-of-range and nodata cells differ
   from ours; compare only the common in-range footprint.
6. **Worker hygiene**: persistent transferables; zero allocations in the
   drag loop; kill in-flight sweeps on new input (a 120 m AGL sweep reaches
   further → more samples → slower; don't queue stale ones).
7. **Lazy-load discipline**: nothing about this feature may slow boot. Tiles
   load on first use; `scene.json` untouched.
8. **POV fog vs 200 km**: FogExp2 at current density will eat ring C
   entirely; the distant-ring shader takes its own fog curve (atmospheric
   tint), and POV's classic fog only applies to ring A. Check both POV
   gating states (`photometricOn`) — don't regress astronomy fix 1.4.
9. **Server**: `.bin` needs a MIME entry (`application/octet-stream`) in
   server.js; stream-error path already handled.
10. **Store discipline**: fetcher/analyzer runs + hashes in the store;
    interactive sweeps never touch it. Journal-visible ops only from the two
    pipeline scripts (twin-store-write-conventions memory applies).
11. **Region-shape sweep cost (the stacking trap)**: a `visible_from` region
    resolves by running a full sweep. A stacked GAIA turn can pass the *same*
    region to three tools in a row — without the (point,agl,target_agl,
    refraction,max_km) memo (§8.1) that's three sweeps for one logical
    question. Memoize per `TwinQuery` instance; round the point to 0.1 m so
    trivially-different coordinates still hit. Also: the memo must key on the
    loaded ring manifest hash, so a re-fetch invalidates it.
12. **`visible_from` beyond fetched extent**: if distant terrain isn't
    fetched, the sweep runs on whatever rings exist and the region is only
    truthful within that extent. Every `visible_from`-scoped result must
    carry `analyzed_extent_km` and a note when `max_km` exceeds it — GAIA
    must not imply "not visible" when the honest answer is "not analyzed
    that far". `can_see` to a target past the fetched extent returns a
    structured `needs_fetch` error, not a false `visible: false`.
13. **Radio refraction is not optical**: never answer a tower/satellite
    question with the optical `k=1/7`. `can_see`/`viewshed_from` default
    optical, but the tower and `geo_arc`/satellite paths must pass
    `refraction="radio"`; the preset used is echoed in every payload so a
    stacked answer can't silently cross models.

## 13. VEIL portability notes

- Fetcher becomes engine-level (`scripts/fetch_distant_terrain.py` proper):
  source picker = 3DEP staged COGs (`prd-tnm.s3.amazonaws.com`, via
  `/vsis3/` + `national_fetch.py` helpers) when the twin is CONUS, else
  Copernicus GLO-30 (ring B) / GLO-90 (ring C) COGs from the AWS Open Data
  buckets (`copernicus-dem-30m`/`-90m`, 1° tiles, `tileList.txt` index;
  no-sign `/vsis3/` or HTTPS). Same manifest format; the manifest records
  which source + datum each ring used (the §12.2/12.3 DSM-vs-DTM and
  EGM2008-vs-NAVD88 corrections are per-source).
- **Canopy source per region**: CONUS twins get LANDFIRE EVH. Global twins use
  a global canopy-height raster — ETH GlobalCanopyHeight
  (Lang et al. 2022/2023, 10 m, on AWS/GEE) or Meta/WRI 1 m canopy height —
  added onto the *3DEP/SRTM* ground rings; but where the ground ring is
  itself Copernicus (a DSM), skip the add (§12.2). The synthetic test fixture
  ships a small EVH grid so the canopy invariant test runs offline.
- `TWIN_DATA_DIR`-aware paths throughout (twin_astro pattern); `VEIL*`
  globals; `npm test` fixture gets a tiny synthetic ring manifest (16×16
  tiles around the mini-twin, ground + canopy) so sweep/horizon/canopy/MCP
  tests run without any fetch; site-dependent assertions use explicit
  `site=` dicts like the astronomy tests.
- `build_from_aoi.py` gains an optional `--distant-terrain` step; init shell
  gets a checkbox, default off (it's a multi-minute fetch).

## 14. Future (out of scope now)

Solar-siting layer (annual insolation with horizon shading per cell — the
irradiance+horizon pieces will already exist), **per-stem near-field
visibility** (use the parcel's actual tree instances instead of the 30 m EVH
raster for the innermost ring — the raster canopy-lift is v1, per-stem is the
refinement), proposed-massing intervisibility (add a not-yet-built structure
to the surface before the sweep — "will the barn I'm about to build block the
house's view"), full Fresnel-zone RF links (needs frequency + link budget),
satellite pass predictions against the horizon (TLE ingestion; the GEO arc is
v1).
