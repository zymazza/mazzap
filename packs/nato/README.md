# NATO Pack

This pack keeps VEIL region-agnostic by moving NATO-country source selection,
dataset attribution, vegetation interpretation, and layer styling into
`packs/nato/`.

Implemented now:

- Netherlands (`NL`/`NLD`) national Tier-A path: AHN terrain/CHM, PDOK RGB+CIR,
  and Copernicus HRL Dominant Leaf Type for conifer/broadleaf typing.
- Global Tier-C fallback for registered countries without a national adapter:
  Copernicus GLO-30 terrain, ETH Global Canopy Height, Sentinel-2 RGB+NIR, and
  global/continental forest typing.
- Continental EEA enrichment where available: Copernicus HRL Dominant Leaf Type,
  CLC+ land cover, and Natura 2000 context layers.

Registered but national-stubbed: all other NATO members. `USA` is Tier S
because the existing `packs/us-national` pack already covers the United States.

## Build The Dutch Demo

```bash
python3 packs/nato/fetch_nato.py \
  --country NL \
  --aoi 174732.5,474346.5,175112.5,474726.5 \
  --data-dir twins/nl-speulderbos/data \
  --resolution 0.5 \
  --name "Speulderbos, Netherlands"
```

Serve it:

```bash
TWIN_DATA_DIR=twins/nl-speulderbos/data PORT=4180 HOST=127.0.0.1 node server.js
```

## Build A Global / Continental Fallback Twin

For countries without a national adapter, `fetch_nato.py` falls back explicitly
instead of guessing a national source. Türkiye is a Tier-C example: no free
national LiDAR path is used, but the AOI is inside the EEA/CLMS domain so DLT
and optional continental context layers are added.

```bash
python3 packs/nato/fetch_nato.py \
  --country TR \
  --tier continental \
  --aoi 31.58,40.74,31.62,40.78 \
  --data-dir twins/tr-bolu/data \
  --resolution 30 \
  --name "Bolu, Türkiye Tier-C"
```

## Engine Interfaces Used

`scripts/ingest_dem.py`

- Input: any DEM GeoTIFF plus either `--bbox MINX MINY MAXX MAXY --bbox-crs EPSG:n`
  or `--aoi AOI.geojson`.
- Output:
  - `data/terrain/grid.json`
  - `data/georef.json`
  - `data/terrain/aoi_local.geojson`
  - `data/scene.json`
- Contract: `docs/grid-contract.md`. The grid stores scene-local meter bounds.
  Imagery and raster overlays must align to `outerMinX..outerMaxX` /
  `outerMinY..outerMaxY`, the cell-edge footprint.

`scripts/ingest_imagery.py`

- Input: RGB or RGB+NIR GeoTIFF in any CRS.
- Output:
  - `data/imagery/naip_rgb.png`
  - `data/imagery/drape.png`
  - `data/imagery/false_color.png` if the input has band 4.
- Invariant: output imagery covers the terrain grid outer footprint at an
  integer pixels-per-meter.
- Band contract for vegetation: `analyze_vegetation.py` reads
  `imagery/false_color.png` band 1 as NIR and `imagery/naip_rgb.png` band 1 as
  red. The Dutch adapter therefore assembles PDOK imagery as `R,G,B,NIR`, where
  NIR comes from PDOK CIR band 1 (`NIR,Red,Green`).

`scripts/analyze_vegetation.py` and `scripts/veg_detect.py`

- Stem capability ladder:
  1. `data/vegetation/tree_instances.lidar.json`
  2. `data/terrain/dsm.tif` and `data/terrain/dtm.tif`
  3. NIR/NDVI imagery
  4. skip
- CHM contract: `terrain/dsm.tif` and `terrain/dtm.tif` are float rasters over
  the exact grid outer footprint. `veg_detect.detect_from_chm()` does not
  reproject them; it assumes the arrays align to `grid.json` in scene-local
  meters. Heights are `DSM - DTM`, and tree `z` is sampled from the DTM.
- The NATO Netherlands adapter void-fills AHN `dsm_05m` and `dtm_05m`, then
  warps the filled rasters to the grid footprint and writes exactly those two
  paths before vegetation runs.
- The NATO global fallback writes `terrain/dtm.tif` from Copernicus GLO-30 and
  `terrain/dsm.tif` as GLO-30 plus ETH canopy height. Before DSM/CHM export,
  the ETH canopy is forest-masked to the leaf-type/tree-cover extent so noisy
  global canopy pixels over grass, crop, or built land do not become detected
  stems. Terrain, imagery, and draped context layers still cover the full AOI.
  This preserves the engine CHM contract without claiming a global bare-earth
  DTM exists.
- Future country adapters with DEM/DSM voids can opt into the same pack-side
  interpolation helper in `packs/nato/adapters/elevation.py`. It uses GDAL
  `FillNodata` IDW interpolation with smoothing and records before/after
  invalid-cell counts in adapter metadata.

`scripts/add_layer.py`

- Input: any GeoTIFF/GeoJSON/etc. in any CRS.
- Output:
  - `data/atlas/local/<id>.png`
  - `data/atlas/local/<id>.grid.json`
  - `data/atlas/local/viewer-layers.json`
- The Dutch build adds `nl_ahn_chm` from the real AHN canopy-height raster as a
  draped ecology/forest-structure layer.
- The NATO build also adds `<iso>_leaf_type` before vegetation analysis. For
  Netherlands this is `nl_leaf_type`, a draped categorical raster whose
  `grid.json` values are sampled by `packs/nato/vegetation.py`. When multiple
  categorical grids exist, vegetation typing prefers `*_leaf_type` over
  `*_forest_type` over coarse `*_landcover`; land cover remains a last-resort
  mask for AOIs without a real leaf/forest type layer.

Store/export flow:

- `scripts/analyze_vegetation.py` writes tree entities into
  `data/twin.gpkg`, then calls `scripts/export_viewer_payloads.py`.
- The viewer reads exported payloads such as
  `data/vegetation/tree_instances.json` and `data/scene.json`.
- `fetch_nato.py` seeds minimal store metadata (`origin_utm`, CRS,
  `scene_template`, source manifest) before `add_layer.py` and vegetation.

## Netherlands Sources

Elevation:

- AHN WCS: `https://service.pdok.nl/rws/ahn/wcs/v1_0`
- Coverages: `dtm_05m` and `dsm_05m`
- CRS: `EPSG:28992`, meters
- Terrain uses AHN DTM. The Netherlands adapter keeps raw AHN rasters in
  `source/nato/nl/`, fills nodata voids in the DTM and DSM with GDAL
  `FillNodata`, and uses the filled DTM for both terrain ingest and CHM
  subtraction. CHM uses filled AHN DSM minus filled AHN DTM.

Imagery:

- PDOK RGB WMS: `https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0`
- RGB layer: `Actueel_ortho25`
- PDOK CIR WMS: `https://service.pdok.nl/hwh/luchtfotocir/wms/v1_0`
- CIR layer: `Actueel_ortho25IR`
- CIR band order: `NIR,Red,Green`

Forest leaf type:

- Source: Copernicus HRL Dominant Leaf Type 2018, 10 m, via EEA Discomap.
- ArcGIS ImageServer:
  `https://image.discomap.eea.europa.eu/arcgis/rest/services/GioLandPublic/HRL_DominantLeafType2018/ImageServer`
- CRS: `EPSG:3035` (ETRS89 / LAEA Europe)
- Classes used by the pack:
  - `0`: no tree cover
  - `1`: broadleaved trees
  - `2`: coniferous trees
  - `255`: outside DLT coverage / nodata
- `fetch_nato.py` exports the AOI from the ImageServer, warps it with nearest
  neighbor to the twin's terrain grid, adds it through `scripts/add_layer.py`,
  and vegetation samples `atlas/local/nl_leaf_type.grid.json`.

Attribution:

- AHN height data: Actueel Hoogtebestand Nederland
  (Rijkswaterstaat/PDOK), open data / CC-BY 4.0.
- Aerial imagery: PDOK current aerial orthophoto RGB and CIR services.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Shared Forest Typing Sources

`packs/nato/adapters/eea.py` is the continental default for European NATO AOIs
inside the EEA/CLMS HRL domain. It uses the public EEA Discomap ImageServer
above and provides real broadleaf/conifer/no-tree categories at 10 m.

`packs/nato/adapters/global.py` is the non-EEA fallback. It first uses the
Copernicus Global Land Service LC100 forest-type layer (100 m) from Zenodo and
maps ENF/DNF to conifer, EBF/DBF to broadleaf, and mixed forest to mixed. If
CGLS cannot provide typed forest cells for the AOI, it falls back to ESA
WorldCover 2021 v200 COG tiles from `s3://esa-worldcover/v200/2021/map` and
maps WorldCover class `10` (Tree cover) to the NATO generic forest code `4`.
WorldCover is a tree mask, not a leaf-type product, so that final fallback is
coarse by design.

For global-tier CHM generation, the ETH canopy raster is masked before
`terrain/dsm.tif` and `terrain/chm.tif` are written. The mask precedence is:
Copernicus HRL Dominant Leaf Type where available (`1`/`2` are forest, `0` is
non-forest), otherwise ESA WorldCover tree cover, otherwise CGLS-LC100 forest
classes. The binary mask is dilated by one source-product pixel before
nearest-neighbor alignment to the CHM grid, avoiding a coarse terrain-grid
expansion. For DLT masks, aligned DLT `0` remains a hard no-tree exclusion after
that buffer. CLC+ land cover is kept as context/QA rather than as an additional
canopy-retention rule.

## Global Sources

Terrain:

- Copernicus DEM GLO-30 public COG bucket:
  `https://copernicus-dem-30m.s3.amazonaws.com/`
- Tile pattern:
  `Copernicus_DSM_COG_10_N40_00_E031_00_DEM/Copernicus_DSM_COG_10_N40_00_E031_00_DEM.tif`
- Important limitation: GLO-30 is a 30 m DSM. The global tier uses it as
  terrain because no worldwide bare-earth DTM exists in this open, keyless path.
  It is much coarser than national 0.5 m LiDAR such as AHN.

Canopy:

- ETH Global Canopy Height 2020 record:
  `https://www.research-collection.ethz.ch/handle/20.500.11850/609802`
- Public Libdrive tile download share:
  `https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt`
- Tile pattern:
  `3deg_cogs/ETH_GlobalCanopyHeight_10m_2020_N39E030_Map.tif`
- License: CC-BY 4.0.

Imagery:

- Element84 Earth Search STAC:
  `https://earth-search.aws.element84.com/v1`
- Collection: `sentinel-2-l2a`
- Bands used: red, green, blue, nir. Sentinel-2 reflectance is converted with
  one uniform true-color stretch for all four bands (`0..3000 DN` to Byte,
  with 255 reserved for nodata) before `scripts/ingest_imagery.py`. The global
  adapter only subtracts the Sentinel-2 BOA +1000 DN offset when the source COGs
  still show that offset, so cached harmonized scenes are not double-corrected.

Continental context:

- CLC+ Backbone raster 2021 ImageServer:
  `https://image.discomap.eea.europa.eu/arcgis/rest/services/CLC_plus/CLMS_CLCplus_RASTER_2021_010m_eu/ImageServer`
- Natura 2000/N2K 2018 MapServer query:
  `https://image.discomap.eea.europa.eu/arcgis/rest/services/Natura2000/N2K_2018/MapServer/0/query`

## Files Written By The Dutch Build

Under the twin data directory:

- `source/nato/nl/aoi_28992.geojson`
- `source/nato/nl/aoi_wgs84.geojson`
- `source/nato/nl/ahn_dtm_05m.tif`
- `source/nato/nl/ahn_dsm_05m.tif`
- `source/nato/nl/ahn_dtm_05m_filled.tif`
- `source/nato/nl/ahn_dsm_05m_filled.tif`
- `source/nato/nl/pdok_rgbn_ortho25.tif`
- `source/nato/nl/nl_leaf_type_eea_dlt_2018_3035.tif`
- `source/nato/nl/nl_leaf_type_eea_dlt_2018_grid.tif`
- `source/nato/nl/source_manifest.json`
- `terrain/grid.json`
- `terrain/dtm.tif`
- `terrain/dsm.tif`
- `terrain/chm.tif`
- `imagery/drape.png`
- `atlas/local/nl_ahn_chm.*`
- `atlas/local/nl_leaf_type.*`
- `twin.gpkg`
- `vegetation/tree_instances.json`

## Adapter Contract

The registry is `packs/nato/adapters/__init__.py`.

An adapter should expose:

- `coverage(aoi) -> dict`
- `fetch_elevation(aoi, out_dir, resolution) -> {"dtm": path, "dsm": path}`
- `prepare_chm_inputs(data_dir, elevation, resolution[, forest_type]) -> {"dtm": path, "dsm": path, "chm": path}`
- `fetch_imagery(aoi, out_dir, footprint, px_per_m) -> {"rgbn": path, ...}`
- `fetch_forest(aoi, out_dir, data_dir) -> optional dict`
- `fetch_landcover(aoi, out_dir, data_dir) -> optional dict`
- `provenance() -> dict`
- `attribution() -> list[str]`

For another country, add `packs/nato/adapters/<country>.py`, register it in
`adapters/__init__.py`, add its reference to `pack.json`, and keep all
country-specific endpoints and interpretation in the pack.

## Real Versus Stubbed

Real in this pack:

- Netherlands AHN DTM/DSM terrain and CHM.
- Netherlands PDOK RGB+CIR imagery.
- Copernicus HRL Dominant Leaf Type over the EEA/European HRL domain for real
  broadleaf/conifer/no-tree typing.
- ESA WorldCover global tree-mask fallback for non-EEA AOIs. This is coarse and
  not a real leaf-type layer.
- AHN CHM draped atlas layer.
- Generic NATO vegetation interpretation with default leaf-type grid support.
  Mixed/unknown cells use a scene-relative median NIR fallback rather than a
  fixed byte threshold, so imagery brightness changes do not silently flip
  evergreen/deciduous typing.

Stubbed:

- Other NATO countries' national source adapters.

No engine-core changes are required.
