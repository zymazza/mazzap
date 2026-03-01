# Purpose

Mazzap is a platform for ingesting and visualizing spatial data towards the end of land management. Mazap ingests LiDAR, NAIP & Ortho Imagery, photogrammetry meshes, Esri File Geodatabases, Shapefiles, GeoPackages, and realtime georefrenced sensor data to create an interactive map for property owners to manage their land and plan for the future.

Mazzap handles data processing for the user, such that property owners can simply upload available data on their property and generate a high-quality three dimensional map quickly and easily. Integration of realtime sensor data is facilitated through an easy-to-use API. Sensors can be manually placed on the map with a point and click system, or, if GPS metadata is available, they can be placed on the map automatically, which is especially useful for vehicles, drones, robots, and other mobile assets. 

# Plan (So Far)

## Recommended pipeline (open, scriptable)

### 0) Standardize projection + tiling strategy

Pick one working CRS for preprocessing (often **UTM** for your zone) and a **fixed tile size** (e.g., 256m or 512m). Then you can export for three.js with **local-origin tiles** (the tile corner becomes local (0,0,0) to avoid floating precision problems).

Output structure example:

data/  
  lidar/raw/  
  lidar/ept/  
  rasters/  
    dem/  
    dsm/  
    chm/  
  vectors/  
    tree_points.gpkg  
    tree_crowns.gpkg  
  imagery/  
    naip_cog/  
    tiles/  
  web/  
    terrain_tiles/  
    imagery_tiles/  
    tree_instances/

### 1) Build an Entwine EPT for the LiDAR

EPT is the sweet spot for “big point cloud, fast random access, reproducible.”

- If USGS delivers COPC LAZ, you _can_ read directly, but EPT still helps downstream.
    
- Command-line (common approach): **entwine build** (open source) → produces EPT.
    

Then PDAL can query subsets efficiently from EPT.

### 2) Ground classification (if needed)

USGS 3DEP data sometimes comes pre-classified, sometimes not. If it’s not reliably classified, PDAL can do ground classification with SMRF/PMF.

PDAL pipeline sketch:

- `readers.ept` or `readers.las`
    
- `filters.outlier`
    
- `filters.smrf` (or `filters.pmf`)
    
- write classified LAZ (optional)
    

### 3) Produce DEM (DTM) and DSM

From classified points:

- **DTM/DEM** from **ground-class** points
    
- **DSM** from **highest** returns (first/maximum)
    

PDAL can rasterize via `writers.gdal`:

- DEM: `output_type="min"` (or `mean`) on ground points
    
- DSM: `output_type="max"` on all points (or first returns)
    

### 4) CHM (canopy height model) = DSM − DEM

Use GDAL/rasterio:

- `chm = max(dsm - dem, 0)`
    
- optionally smooth (small gaussian/median) to reduce speckle
    

This CHM becomes the basis for:

- canopy masks
    
- tree tops
    
- crown segmentation (tree extents)
    

### 5) Canopy mask + tree points + tree extents

**Open-source paths**:

#### Option A (all-Python + raster tools)

- Canopy mask: `chm > height_threshold` (e.g., 2m)
    
- Tree tops: local maxima filter on CHM
    
- Tree crowns: watershed segmentation on inverted CHM or distance transform within canopy mask
    
- Output: points (tree location + height), polygons (crown extents)
    

This is very repeatable and works well for “good enough” instancing in a digital twin.

#### Option B (best quality): R `lidR`

If you want results closer to “forestry-grade” individual tree delineation, `lidR` is excellent:

- normalize heights using DEM
    
- locate tree tops
    
- segment crowns
    
- export points/polygons
    

It’s still scriptable and open source—just adds an R step.

### 6) NAIP imagery: COG → tiles

- Download NAIP
    
- Build **Cloud Optimized GeoTIFFs** (COGs)
    
- Generate web tiles (XYZ) for your viewer
    

GDAL does this cleanly:

- warp/reproject/resample once into your working CRS
    
- translate to COG
    
- tile with `gdal2tiles.py` or a modern tiler
    

### 7) Export for three.js

For each tile:

- **terrain mesh**: from DEM tile (or decimated TIN)
    
- **texture**: NAIP tile
    
- **instances**: tree points in that tile → JSON (x,y,z,height,crown_radius,species? later)
    

Critically: store a per-tile metadata file:

{  
  "tile_id": "UTM_18N_512m_0123_0456",  
  "crs": "EPSG:32618",  
  "origin_utm": [597000, 4769000, 0],  
  "bounds_utm": [597000, 4769000, 597512, 4769512]  
}

Then in three.js you render tile-local coordinates (stable floats) and keep “real world” only in metadata.

## Agent Scratchpad (Working Notes)

Date: 2026-02-27

Objective for this step:
- Create the first runnable LiDAR processing pipeline so `node generateDEM.js` produces a DEM raster in `Processed Data`.

What I changed:
1. Added `Processing Pipeline/dem.pipeline.template.json`.
Why:
- This gives us a simple, explicit PDAL pipeline definition we can reuse and evolve.
- It currently reads LAS/LAZ, filters to ground points (`Classification[2:2]`), and writes a 1m GeoTIFF DEM (`output_type=min`).

2. Added `generateDEM.js` in project root.
Why:
- You asked for a direct test command: `node generateDEM.js`.
- The script picks input automatically from `Raw Data Inputs` (prefers `lidar_input.copc.laz`, then `lidar_input.laz`), writes a run-specific PDAL file at `Processing Pipeline/dem.pipeline.run.json`, and outputs `Processed Data/dem.tif`.

3. Ran the pipeline and validated output creation.
Why:
- Confirmed end-to-end behavior before handing off to you for QGIS inspection.
- Current generated file: `Processed Data/dem.tif` (~2.26 MB).

Notes:
- This is intentionally minimal and stable for first-step testing.
- Next iterations can add DSM/CHM, reprojection/tiling, nodata filling, and metadata outputs.

### Follow-up adjustments (viewer + structure)

Date: 2026-02-27

What I changed:
1. Moved core DEM generator into `Processing Pipeline/generateDEM.js`.
Why:
- Keeps processing logic inside the processing folder as intended.
- Root `generateDEM.js` is now only a tiny wrapper so `node generateDEM.js` still works.

2. Updated server path handling for processed output folders.
Why:
- Server now supports both `Processed Data` and `Processed_Data` and resolves whichever exists.
- This prevents breakage from folder naming differences.

3. Reworked DEM viewer loading path and terrain source.
Why:
- Viewer now requests `/data/dem-grid.json` (server-generated from `dem.tif`) for stable browser parsing.
- Removed fragile `OrbitControls` module import path; camera controls are built in directly (drag rotate + wheel zoom).

### DEM artifact reduction pass

Date: 2026-02-27

Requested adjustments:
- Replace GDAL rasterization `output_type=min` with `mean`.
- Increase interpolation neighborhood for sparse cells.
- Add outlier filtering (`filters.elm`).
- Ensure ground is classified with SMRF before gridding; grid only ground.

Implemented in `Processing Pipeline/dem.pipeline.template.json`:
- `filters.elm`
- `filters.range` with `Classification![7:7]` (drop ELM outliers)
- `filters.assign` with `Classification = 1` (reset classes before SMRF)
- `filters.smrf` (ground classification)
- `filters.range` with `Classification[2:2]` (ground-only DEM rasterization)
- `writers.gdal` with:
  - `output_type: "mean"`
  - `window_size: 8`
  - `resolution: 1.0`

Validation:
- Re-ran `node generateDEM.js` successfully.
- Output written to `Processed Data/dem.tif`.

### Detailed pipeline reference (processing + visualization)

Date: 2026-02-27

This section documents the current operational pipeline as implemented in this workspace.

#### 1) LiDAR input selection and folder contract

Raw inputs:
- `Raw Data Inputs/lidar_input.copc.laz` (preferred when present)
- `Raw Data Inputs/lidar_input.laz` (fallback)

Processed outputs:
- `Processed Data/dem.tif`
- `Processed Data/vegetation/shrubs_points.laz`
- `Processed Data/vegetation/shrubs_density.tif`
- `Processed Data/trees/tree_candidates_points.las`
- `Processed Data/trees/tree_candidates_points.laz` (optional convenience export)
- `Processed Data/trees/tree_canopy_height.tif`
- `Processed Data/trees/tree_density.tif`
- `Processed Data/trees/tree_instances.json`

Pipeline definitions:
- `Processing Pipeline/dem.pipeline.template.json`
- `Processing Pipeline/vegetation.pipeline.template.json`
- `Processing Pipeline/trees.pipeline.template.json`

Runtime-expanded pipeline files:
- `Processing Pipeline/dem.pipeline.run.json`
- `Processing Pipeline/vegetation.pipeline.run.json`
- `Processing Pipeline/trees.pipeline.run.json`

#### 2) DEM processing pipeline

Entry points:
- `generateDEM.js` (root wrapper for convenience)
- `Processing Pipeline/generateDEM.js` (actual implementation)

Current DEM logic (PDAL):
1. Read LAZ/COPC input.
2. `filters.elm` to identify low outliers.
3. `filters.range` to remove ELM outlier class.
4. `filters.assign` to normalize classification before terrain classification.
5. `filters.smrf` to classify ground.
6. `filters.range` keep only ground (`Classification == 2`).
7. `writers.gdal` produce DEM with:
   - `output_type = mean`
   - larger `window_size` for sparse interpolation
   - `resolution = 1.0`

Goal:
- Produce a ground-only DEM with fewer spike artifacts and better fill behavior in sparse areas.

#### 3) Shrub/low-vegetation pipeline

Entry point:
- `Processing Pipeline/generateVegetation.js`

Current shrub logic (PDAL):
1. Read LAZ/COPC input.
2. Optional statistical outlier filtering.
3. Ground classification with `filters.smrf`.
4. Height normalization (`filters.hag_delaunay` with fallback pattern where needed).
5. Candidate filtering:
   - not ground
   - height-above-ground range (configurable, default low vegetation band)
   - optional excluded classes
6. Decimation with `filters.voxelcenternearestneighbor`.
7. Write point output: `shrubs_points.laz` with HAG retained.
8. Write density raster: `shrubs_density.tif` via `writers.gdal` count mode.

CLI controls include:
- min/max HAG
- density raster resolution
- voxel size
- outlier filter toggle

#### 4) Tree pipeline

Entry point:
- `Processing Pipeline/generateTrees.js`

Current tree logic (PDAL + Node post-process):
1. Read LAZ/COPC input.
2. Optional outlier filtering.
3. Ground classification with SMRF.
4. Compute HeightAboveGround (delaunay preferred, NN fallback).
5. Filter tree candidates by HAG band (default `2m` to `60m`), non-ground, optional class exclusions.
6. Voxel downsample for instancing-scale density.
7. Write `tree_candidates_points.las` with LAS 1.4 settings and `HeightAboveGround` in `extra_dims`.
8. Optional LAZ translation to `tree_candidates_points.laz`.
9. Write CHM-like raster:
   - `tree_canopy_height.tif`
   - `writers.gdal`, `dimension=HeightAboveGround`, `output_type=max`, float nodata.
10. Write density raster:
   - `tree_density.tif`
   - `writers.gdal`, `output_type=count`, integer nodata.

Post-processing to instances:
1. Export candidate points to text for lightweight Node-side analysis.
2. Bin into XY grid; keep local max HeightAboveGround per cell.
3. Apply non-maximum suppression in XY neighborhood.
4. Build instance records:
   - `x, y, z, height, radius, confidence`
   - placeholders for later NAIP/species enrichment:
     - `spectral`
     - `species`
     - `naipTileId`
     - `naipSample`

Output:
- `Processed Data/trees/tree_instances.json`

#### 5) Reusable pipeline runner abstraction

Shared helper:
- `Processing Pipeline/pipelineRunner.js`

Responsibilities:
- input path resolution
- processed directory creation
- template token substitution into runtime pipeline json
- PDAL command execution and standardized error output

Design goal:
- future pipelines (for example NAIP ingest/warp/tiling) can reuse the same scaffolding.

#### 6) Viewer server data bridge

Server entry point:
- `index.js`

Responsibilities:
1. Serve static web viewer files.
2. Serve Assets OBJ/MTL files.
3. Resolve processed folder naming (`Processed Data` or `Processed_Data`).
4. Provide computed/json endpoints:
   - `/data/dem-grid.json` from `dem.tif` via `gdal_translate -of XYZ`
   - `/data/vegetation/shrubs-points.json` from shrub LAZ via `pdal translate` to CSV
   - `/data/vegetation/shrub-assets.json` (auto-discovered shrub OBJ variants)
   - `/data/trees/tree-assets.json` (auto-discovered tree OBJ variants)
   - `/data/trees/tree_instances.json` (direct static JSON under `/data/*`)

#### 7) Web visualization pipeline (Three.js)

Viewer files:
- `Frontend Web Viewer/index.html`
- `Frontend Web Viewer/styles.css`
- `Frontend Web Viewer/viewer.js`

Current render flow:
1. Load DEM grid json, build colored terrain mesh (Z-up scene).
2. Load shrub asset templates (OBJ/MTL), normalize orientation/scale, place on DEM using shrub anchors.
3. Load tree asset templates (OBJ/MTL), classify templates by naming:
   - names containing `tall` => tall pool
   - names containing `small` or `ground` => short pool
   - remaining tree assets => mid pool
4. Load `tree_instances.json`, build tree anchors, select template by tree height bucket, place on DEM.

Density model:
- Sliders run `0..100%`.
- `50%` is baseline density (current default visual baseline).
- `100%` scales toward ~2x via deterministic jittered duplication.
- Separate independent controls for shrubs and trees.

Vertical exaggeration:
- Terrain Z and vegetation Z scale update together from one vertical scale slider.

#### 8) Current viewer UI structure

Top navigation:
- thin top bar
- hamburger icon on left
- search input on right (placeholder for future plumbing)

Collapsible left menu:
- layer toggles:
  - show/hide shrubs
  - show/hide trees
- density sliders:
  - shrubs
  - trees
- terrain controls:
  - vertical scale
  - reset view
- status panel:
  - DEM + shrubs + trees load/render status text

### Building asset runtime diagnosis + resolution (latest)

Date: 2026-02-28

Context:
- Building artifacts were confirmed to look correct in Blender at all export stages.
- Visual corruption remained only in the Three.js runtime, indicating viewer-side processing as root cause.

What we changed:
1. Added strict runtime A/B diagnostics in viewer loading.
Why:
- To isolate whether corruption came from exported GLB files or viewer-side sanitation logic.
- We logged per-load diagnostics for comparability:
  - mesh count
  - primitive/material counts
  - material names
  - texture names
  - whether material replacement occurred
  - whether UV modification occurred

2. Verified raw path fixed rendering issues.
Why:
- The "raw GLB" runtime path (no UV/material/texture mutation) rendered correctly.
- This confirmed the sanitation layer was damaging valid assets.

3. Removed runtime toggle from UI and made raw behavior the default.
Current behavior:
- Building GLBs now load and render in viewer as faithful raw GLTF content by default.
- No building-specific UV normalization or material replacement is applied in active path.
- Placement/transform/orientation logic remains in place for scene integration.
- Diagnostics logging remains active and reports `renderPath: raw-gltf`.

### Building photogrammetry source contract update

Date: 2026-02-28

Requested change:
- Building photogrammetry inputs should be read from `Raw Data Inputs/` instead of `Assets/`.

Implemented:
1. Server footprint-name mesh lookup (`index.js`) now resolves from:
   - `Raw Data Inputs/` (primary)
   - `Raw_Data_Inputs/` (fallback)

2. Building API processing route now reports missing-name matches against `Raw Data Inputs/`.

3. Building pipeline mesh resolution (`Processing Pipeline/generateBuildingAsset.js`) now:
   - accepts explicit full/relative mesh paths as before,
   - and resolves basename-style mesh names against `Raw Data Inputs/` by default.

Operational implication:
- Named building footprints should correspond to mesh files placed in `Raw Data Inputs/` (for example `B-3.glb`, `Barn_Main_A.obj`, etc.).
- `Assets/` remains in use for vegetation OBJ/MTL templates, not building photogrammetry source lookup.

### Viewer data-source upload workflow (latest)

Date: 2026-02-28

What was added:
1. Sidebar action button:
   - `Upload Data Source` at the bottom of the left sidebar.

2. Upload modal with drag/drop + picker controls:
   - drag-and-drop area
   - `Select Files`
   - `Select Folder`
   - queue list with per-item type dropdown

3. Per-upload type selection options:
   - `LiDAR`
   - `Footprints`
   - `Photogrammetry`

4. Backend upload API:
   - `POST /api/data-sources/upload`
   - receives multipart uploads and writes into `Raw Data Inputs/`

Naming/placement rules now enforced at upload time:
- LiDAR:
  - renamed to `lidar_input.*` in `Raw Data Inputs/`
  - preserves extension style (for example `.copc.laz`, `.laz`, `.las`)
- Footprints:
  - uploaded footprint dataset is written under `Raw Data Inputs/Footprints.gdb/`
  - this standardizes prior names (for example `Saratoga_Building_Footprints.gdb`) to the universal expected name `Footprints.gdb`
- Photogrammetry:
  - keeps incoming file names
  - stored directly in `Raw Data Inputs/`

Default pipeline contract updated:
- Building-footprint processing now expects default footprints source at:
  - `Raw Data Inputs/Footprints.gdb`

### Upload robustness + automatic pipeline triggers (latest)

Date: 2026-02-28

Issue observed during clean-slate test:
- Uploading full datasets in one large multipart request caused client-side `Failed to fetch` / `ERR_ACCESS_DENIED` behavior and blocked end-to-end bootstrap.

Fix implemented:
1. Switched viewer upload workflow to per-file streaming uploads:
   - endpoint: `POST /api/data-sources/upload-item`
   - each file uploads independently with source type + relative-path metadata
   - avoids giant single-request multipart payloads

2. Added server-side processing trigger endpoint:
   - endpoint: `POST /api/data-sources/process`
   - runs pipelines based on available inputs and uploaded types

3. Added auto-run behavior after upload in viewer:
   - if `LiDAR` uploaded: auto-runs DEM, shrubs, trees pipelines
   - if `Footprints` uploaded and LiDAR exists: auto-runs building-footprint pipeline
   - viewer then refreshes terrain/buildings/shrubs/trees data products

Flow now expected from upload alone:
- LiDAR upload -> DEM + shrubs + trees generated
- Footprints upload (with LiDAR present) -> building footprints generated
- Photogrammetry upload -> source meshes staged for per-footprint building asset processing

UI progress updates:
- Upload action now hides upload modal and shows a dedicated progress popup.
- Popup includes:
  - progress bar
  - percent complete
  - current stage title
  - human-readable explanation of the active step
  - detail line (for file or step index)

Processing progress endpoints:
- `POST /api/data-sources/process-plan` returns planned auto steps.
- `POST /api/data-sources/process-step` runs one step at a time.
- Viewer executes steps sequentially to provide real stage-by-stage progress feedback.

### Hydrology pipeline + viewer layer (latest)

Date: 2026-02-28

What was added:
1. New upload type:
   - `Hydrology` in data-source upload type dropdown.
   - Intended for shapefile component uploads (for example `.shp`, `.dbf`, `.prj`).

2. Hydrology source staging:
   - Hydrology uploads are stored under `Raw Data Inputs/Hydrology/`.

3. New hydrology processing pipeline:
   - `Processing Pipeline/generateHydrology.js`
   - Clips hydrology vectors to DEM scene bounds.
   - Reprojects to DEM CRS.
   - Outputs:
     - `Processed Data/hydrology/hydrology_clipped.gpkg`
     - `Processed Data/hydrology/hydrology_clipped.geojson`
     - `Processed Data/hydrology/hydrology_clipped_local.geojson`
     - `Processed Data/hydrology/hydrology_meta.json`

4. Auto-processing integration:
   - If hydrology uploads are present (and LiDAR/DEM context exists), auto-process planning can include a `Generate Hydrology` step.

5. Viewer hydrology layer:
   - New `Show Hydrology` layer toggle.
   - Terrain-aligned stream rendering from processed hydrology GeoJSON.
   - Lightweight flow animation on stream surface.
   - User controls:
     - stream width
     - stream depth
     - flow speed
     - `Snap Hydrology To Terrain` button

Vegetation separation rule:
- Shrubs and trees now enforce strict separation from streams in placement pass.
- Required distance uses: `stream_half_width + 1.0m + vegetation_radius`.
- Vegetation is iteratively pushed away from hydrology centerlines similar to building-edge avoidance.

Stability follow-ups applied:
- Hydrology load now checks `/api/hydrology/status` before requesting GeoJSON outputs to avoid noisy 404s when no hydrology outputs exist yet.
- Hydrology pipeline CRS detection now avoids invalid EPSG unit-code picks (for example `EPSG:9001`) by preferring `gdalsrsinfo -o epsg` and safer fallback parsing.
- Hydrology clipping now passes `--config SHAPE_RESTORE_SHX YES` to `ogr2ogr` to recover missing `.shx` sidecars where possible.

### SSURGO / Soils pipeline + viewer layer (latest)

Date: 2026-03-01

What was added:
1. New upload type:
   - `Soils (SSURGO)` in the data-source upload dropdown.

2. SSURGO source staging:
   - Soils uploads are normalized under `Raw Data Inputs/SSURGO/`.
   - Intended contract is full Web Soil Survey export folder content (not a single shapefile only).

3. New soils processing pipeline:
   - `Processing Pipeline/generateSoils.js`
   - Wrapper: `generateSoils.js`
   - Script command: `npm run soils`

4. Pipeline behavior:
   - Locates SSURGO `spatial/` map unit polygon shapefile (`soilmu_a*`/`smu_a*` preference).
   - Reprojects geometry to DEM CRS and clips to DEM extent.
   - Reads SSURGO tabular side (and attempts `.mdb` lookup when available) to enrich map unit records by `mukey`.
   - Derives a simplified thematic class per polygon (hydrologic group/drainage/mapunit fallback) plus display color.

5. Soils outputs:
   - `Processed Data/soils/soils_clipped.gpkg`
   - `Processed Data/soils/soils_clipped.geojson`
   - `Processed Data/soils/soils_clipped_local.geojson`
   - `Processed Data/soils/soil_legend.json`
   - `Processed Data/soils/soil_meta.json`

6. Soils API availability endpoint:
   - `GET /api/soils/status`

7. Viewer integration:
   - New `Show Soil Data` layer toggle.
   - Terrain-draped soil polygon overlay rendered over DEM.
   - Right-side soil legend panel driven by generated legend classes/colors.

SSURGO upload requirements (current):
- Recommended: upload full SSURGO export folder contents including:
  - `spatial/`
  - `tabular/`
  - optional `soildb_US_2003.mdb`
  - metadata files
- Minimum geometry requirement for visible soil overlay:
  - map unit polygon shapefile in `spatial/`.

### Current pipeline contracts (authoritative)

Date: 2026-03-01

This section supersedes older notes above when there is any mismatch.

#### A) Required runtime tools

Installed command dependencies used by current pipelines:
- DEM / vegetation / trees: `pdal`
- building footprints: `pdal`, `ogrinfo`, `ogr2ogr`, `gdalsrsinfo`, `projinfo`
- hydrology: `gdalinfo`, `ogr2ogr`, `gdalsrsinfo`
- per-building photogrammetry asset generation: `blender`

#### B) Canonical input staging (Raw Data Inputs)

All user uploads are normalized into `Raw Data Inputs/`.

1. LiDAR:
- Stored as `lidar_input.copc.laz` or `lidar_input.laz` or `lidar_input.las`
- Upload type: `LiDAR`

2. Footprints:
- Stored as folder `Footprints.gdb/`
- Upload type: `Footprints`
- Expected to be an Esri FileGDB dataset

3. Hydrology:
- Stored under `Hydrology/`
- Upload type: `Hydrology`
- Minimum practical shapefile requirement: `.shp` geometry file
- Recommended sidecars for reliable processing: `.shx`, `.dbf`, `.prj`

4. Photogrammetry (building source meshes):
- Stored directly in `Raw Data Inputs/` with original names
- Upload type: `Photogrammetry`
- Names should match footprint names used in viewer for auto-match

#### C) Upload + processing API surface

Current primary upload/processing flow:
- `POST /api/data-sources/upload-item` (per-file streaming upload)
- `POST /api/data-sources/process-plan` (returns ordered steps)
- `POST /api/data-sources/process-step` (runs one step)

Compatibility / legacy path still available:
- `POST /api/data-sources/process`
- `POST /api/data-sources/upload` (legacy multipart mode)

Data management APIs:
- `GET /api/data-sources/list`
- `POST /api/data-sources/delete`
- `POST /api/data-sources/clear`

Hydrology availability check:
- `GET /api/hydrology/status`

#### D) Auto-process step ordering

Planned steps are selected from currently available inputs:
1. `Generate DEM`
2. `Generate Hydrology` (if hydrology input exists and DEM exists or will be generated)
3. `Generate Shrubs`
4. `Generate Trees`
5. `Generate Footprints`

Behavior details:
- LiDAR upload triggers DEM + shrubs + trees.
- Footprints upload with LiDAR available triggers footprint generation.
- Hydrology upload triggers hydrology clipping/render prep if DEM exists (or LiDAR is present so DEM can run first).

#### E) Pipeline scripts, inputs, outputs

1. DEM
- Script: `generateDEM.js` (wrapper) -> `Processing Pipeline/generateDEM.js`
- Inputs: `Raw Data Inputs/lidar_input.copc.laz` or `Raw Data Inputs/lidar_input.laz`
- Output: `Processed Data/dem.tif`

2. Shrubs / low vegetation
- Script: `Processing Pipeline/generateVegetation.js`
- Inputs: LiDAR (`lidar_input.*`)
- Outputs:
  - `Processed Data/vegetation/shrubs_points.laz`
  - `Processed Data/vegetation/shrubs_density.tif`

3. Trees
- Script: `Processing Pipeline/generateTrees.js`
- Inputs: LiDAR (`lidar_input.*`)
- Outputs:
  - `Processed Data/trees/tree_candidates_points.las`
  - `Processed Data/trees/tree_candidates_points.laz` (optional conversion)
  - `Processed Data/trees/tree_canopy_height.tif`
  - `Processed Data/trees/tree_density.tif`
  - `Processed Data/trees/tree_instances.json`

4. Building footprints
- Script: `generateBuildings.js` (wrapper) -> `Processing Pipeline/generateBuildings.js`
- Inputs:
  - LiDAR (`lidar_input.*`) for extent/CRS reference
  - `Raw Data Inputs/Footprints.gdb`
- Outputs (under `Processed Data/buildings/`):
  - `buildings_meta.json`
  - `footprints_clipped.geojson`
  - `footprints_clipped_local.geojson`
  - supporting GPKG outputs

5. Hydrology
- Script: `generateHydrology.js` (wrapper) -> `Processing Pipeline/generateHydrology.js`
- Inputs:
  - hydrology shapefile source(s) under `Raw Data Inputs/Hydrology`
  - DEM (`Processed Data/dem.tif`) for clipping extent + CRS
- Outputs (under `Processed Data/hydrology/`):
  - `hydrology_clipped.gpkg`
  - `hydrology_clipped.geojson`
  - `hydrology_clipped_local.geojson`
  - `hydrology_meta.json`

6. Per-footprint building assets (manual per building)
- Script: `generateBuildingAsset.js` (wrapper) -> `Processing Pipeline/generateBuildingAsset.js`
- Inputs:
  - photogrammetry mesh from `Raw Data Inputs/` (basename or explicit path)
  - processed footprints (`Processed Data/buildings/footprints_clipped*.geojson`)
- Outputs:
  - `Processed Data/buildings/assets/<footprint_id>/...` (`lod0.glb`, `asset_meta.json`, etc.)

#### F) Viewer rendering requirements by layer

1. DEM visible:
- requires `Processed Data/dem.tif` -> `/data/dem-grid.json`

2. Shrubs visible:
- requires shrubs outputs + shrub assets in `Assets/`

3. Trees visible:
- requires tree outputs + tree assets in `Assets/`

4. Building footprint overlays / assets visible:
- overlays require building footprint outputs
- per-building meshes require generated building assets

5. Hydrology visible:
- requires non-empty hydrology output GeoJSON (`hydrology_clipped_local.geojson` preferred)
- requires DEM loaded (for terrain alignment)
- layer toggle `Show Hydrology` must be enabled

Important hydrology note:
- Uploading only `.dbf`/`.prj` without `.shp` is insufficient to render streams (no geometry source).

### Startup data-directory bootstrap (latest)

Date: 2026-03-01

Why this was added:
- `Raw Data Inputs/` and `Processed Data/` are now git-ignored.
- Fresh clones/new environments need these folders recreated automatically on startup.

Implemented in `index.js`:
1. Added `ensureProjectDataDirectories()` and execute it at server startup (before `server.listen`).

2. Startup now guarantees these roots exist:
- `Raw Data Inputs/`
- `Processed Data/`

3. Startup now guarantees these raw-input subfolders exist:
- `Raw Data Inputs/Hydrology/`
- `Raw Data Inputs/SSURGO/`
- `Raw Data Inputs/SSURGO/spatial/`
- `Raw Data Inputs/SSURGO/tabular/`
- `Raw Data Inputs/SSURGO/thematic/`

4. Startup now guarantees these processed-output subfolders exist:
- `Processed Data/vegetation/`
- `Processed Data/trees/`
- `Processed Data/buildings/`
- `Processed Data/buildings/assets/`
- `Processed Data/hydrology/`
- `Processed Data/soils/`

Notes:
- We intentionally do **not** pre-create `Raw Data Inputs/Footprints.gdb` so footprint auto-detection does not return a false positive on empty projects.
- Server startup logs now print resolved raw + processed root paths for quick verification.
