# Mazzap

Mazzap is a local-first digital twin viewer and processing workflow for terrain + vegetation + buildings + hydrology.

It provides:
- an upload-driven workflow for source data,
- automated processing pipelines,
- a Three.js web viewer with interactive layer controls.

## What This App Does

From uploaded source data, Mazzap can generate and render:
- **DEM terrain** from LiDAR,
- **shrubs + trees** from LiDAR,
- **building footprints** from a FileGDB footprint source,
- **soil polygons (SSURGO)** clipped and themed over DEM,
- **hydrology streams** clipped to the DEM scene,
- **building meshes** (photogrammetry assets) matched to footprint names.

---

## 1) Prerequisites

## Runtime
- Node.js (recommended: Node 18+)
- npm

## External tools used by pipelines
- `pdal`
- `gdalinfo`, `gdalsrsinfo`, `ogr2ogr`, `ogrinfo`, `projinfo`
- `blender` (required only for per-building photogrammetry asset generation)

If these commands are missing, relevant pipeline steps fail with explicit errors.

---

## 2) Install and Start

From project root:

```bash
npm install
npm run start
```

Open:

```text
http://127.0.0.1:3000
```

---

## 3) Data You Need

Use the in-app **Upload Data Source** modal. Each queued item has a type dropdown.

## LiDAR
- Upload type: `LiDAR`
- Accepted source examples: `.copc.laz`, `.laz`, `.las`
- Normalized storage in app: `Raw Data Inputs/lidar_input.*`

## Footprints
- Upload type: `Footprints`
- Expected format: **Esri FileGDB** folder
- Normalized storage: `Raw Data Inputs/Footprints.gdb/`

## Hydrology
- Upload type: `Hydrology`
- Expected format: shapefile set
- Minimum practical requirement: `.shp`
- Strongly recommended together: `.shp` + `.shx` + `.dbf` + `.prj`
- Normalized storage: `Raw Data Inputs/Hydrology/`

## Soils (SSURGO)
- Upload type: `Soils (SSURGO)`
- Recommended upload: full Web Soil Survey export folder (includes `spatial/`, `tabular/`, optional `.mdb`, metadata)
- Minimum for visible overlay: map unit polygon shapefile in `spatial/`
- Normalized storage: `Raw Data Inputs/SSURGO/`

## Photogrammetry (building meshes)
- Upload type: `Photogrammetry`
- Typical files: `.glb`, `.gltf`, `.obj`, `.ply`
- Stored with original names in `Raw Data Inputs/`
- Meshes are matched to footprint names set in the Buildings editor

---

## 4) Upload Flow (Recommended)

1. Click **Upload Data Source**.
2. Drag/drop files or use file/folder pickers.
3. Set each item’s type correctly.
4. Click **Upload Selected**.
5. A progress popup appears and runs required processing steps.

### Auto-processing behavior
- LiDAR upload triggers: `DEM -> Soils/Hydrology (if present) -> Shrubs -> Trees`
- Footprints upload with LiDAR available triggers: `Generate Footprints`
- Hydrology upload triggers hydrology processing if DEM exists (or can be generated from LiDAR)
- SSURGO upload triggers soils processing if DEM exists (or can be generated from LiDAR)

The viewer refreshes layers after processing.

---

## 5) Viewer Usage

## Layers
- Show/hide shrubs, trees, building assets, building footprints, soil data, hydrology.

## Terrain
- Vertical exaggeration slider.
- Reset view.

## Density
- Independent shrub and tree density controls.

## Hydrology
- Stream width
- Stream depth (0.0m is centered in slider range)
- Flow speed animation
- **Snap Hydrology To Terrain** to re-fit streams onto current DEM terrain

## Soils
- Soil polygons are draped over DEM with thematic colors.
- A right-side **Soil Legend** is shown when Soil Data layer is visible.

## Buildings
- Click footprint outlines to select.
- Name footprints.
- Process and load matched building assets.
- Move/rotate loaded assets and save transforms.

---

## 6) Manage Data Sources

Use **Manage Data Sources** in sidebar footer to:
- list currently staged raw sources,
- delete individual sources,
- **Clear Data** (wipe raw + processed data contents and restart from scratch).

---

## 7) Manual Pipeline Commands (Optional)

If you prefer CLI/manual runs:

```bash
npm run dem
npm run vegetation
npm run trees
npm run buildings
npm run hydrology
```

Per-building mesh generation:

```bash
npm run building-asset
```

Or full batch (without hydrology):

```bash
npm run all
```

---

## 8) Output Locations

Generated outputs are under `Processed Data/`, including:
- `dem.tif`
- `vegetation/*`
- `trees/*`
- `buildings/*`
- `soils/*`
- `hydrology/*`

---

## 9) Troubleshooting

## “Hydrology uploaded but nothing renders”
- Verify hydrology output exists and is non-empty:
  - `Processed Data/hydrology/hydrology_clipped_local.geojson`
- Ensure source includes geometry (`.shp`) and preferably sidecars (`.shx`, `.dbf`, `.prj`).
- Ensure hydrology and DEM extents actually overlap.
- Ensure **Show Hydrology** is enabled.

## “SSURGO uploaded but no soil overlay appears”
- Verify soils output exists and is non-empty:
  - `Processed Data/soils/soils_clipped_local.geojson`
- Ensure SSURGO upload included the map-unit polygon data in `spatial/`.
- Ensure DEM exists (soils processing clips/reprojects to DEM extent).
- Ensure **Show Soil Data** is enabled.

## “Buildings/footprints 404 in viewer”
- Footprint pipeline likely did not run or failed.
- Verify `Raw Data Inputs/Footprints.gdb` is present and valid.

## “Failed to fetch” during upload
- Prefer the current per-file upload flow (default in UI).
- Retry with smaller batches if system resources are constrained.

## “Progress API route 404”
- Restart server after pulling/updating code:
  - `npm run start`

---

## 10) Notes

- `Assets/` is used for vegetation model templates.
- Building photogrammetry source meshes come from `Raw Data Inputs/`.
- For best reproducibility, upload through the UI so naming contracts are normalized automatically.
