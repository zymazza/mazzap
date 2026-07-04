# NATO Pack

This pack keeps VEIL region-agnostic by moving NATO-country source selection,
dataset attribution, vegetation interpretation, and layer styling into
`packs/nato/`.

Implemented now:

- Netherlands (`NL`/`NLD`) national Tier-A path: AHN terrain/CHM, PDOK RGB+CIR,
  and Copernicus HRL Dominant Leaf Type for conifer/broadleaf typing.
- Norway (`NO`/`NOR`) national Tier-A path: Kartverket / Geonorge Nasjonal
  hoydemodell DTM/DOM 1 m terrain/CHM, Sentinel-2 RGB+NIR imagery fallback, and
  Copernicus HRL Dominant Leaf Type for conifer/broadleaf typing.
- Spain (`ES`/`ESP`) national Tier-A path: IGN/CNIG PNOA-LiDAR MDT 5 m terrain,
  ETH Global Canopy Height CHM fallback because no open national MDS/DSM WCS was
  reachable, PNOA RGB WMS plus Sentinel-2 NIR, and Copernicus HRL Dominant Leaf
  Type for conifer/broadleaf typing.
- Belgium (`BE`/`BEL`) national Tier-A path for Flanders: Digitaal Vlaanderen
  DHMV II DTM/DSM 1 m terrain/CHM, Flanders current RGB orthophoto WMS,
  Sentinel-2 NIR, and Copernicus HRL Dominant Leaf Type typing.
- Czechia (`CZ`/`CZE`) national Tier-A path: CUZK DMR 5G terrain, DMP 1G
  surface, CUZK ORTOFOTO RGB, Sentinel-2 NIR, and Copernicus HRL Dominant Leaf
  Type typing.
- Denmark (`DK`/`DNK`) working fallback adapter pending Dataforsyningen /
  Klimadatastyrelsen DHM token access: Copernicus GLO-30 terrain,
  forest-masked ETH canopy, Sentinel-2 RGB+NIR, and Copernicus HRL Dominant
  Leaf Type typing.
- Estonia (`EE`/`EST`) working adapter: checked Maa-amet / Maa- ja Ruumiamet
  height services, uses fallback terrain/CHM because no anonymous numeric
  DTM+DSM WCS was reachable, and uses Maa-amet RGB+CIR orthophoto WMS when
  reachable.
- Finland (`FI`/`FIN`) working fallback adapter: checked NLS/Maanmittauslaitos
  2 m DEM, orthophoto/CIR WCS/WMS, and OGC API routes, but anonymous requests
  returned `401`; fallback terrain/CHM and Sentinel-2 imagery are used.
- France (`FR`/`FRA`) national Tier-A path: IGN Géoplateforme WMS-R RGE ALTI
  high-resolution MNT terrain, high-resolution MNS surface, BD ORTHO RGB,
  ORTHO IRC NIR, and Copernicus HRL Dominant Leaf Type typing.
- Latvia (`LV`/`LVA`) working fallback adapter: checked LGIA open DTM, LAS,
  RGB orthophoto, and infrared orthophoto file routes; DSM-from-LAS assembly is
  too heavy for unattended builds, so fallback terrain/CHM and Sentinel-2 are
  used.
- Luxembourg (`LU`/`LUX`) national terrain path: ACT / data.public.lu BD-L-MNT
  1 m terrain, geoportail.lu RGB and infrared orthophoto WMS, forest-masked ETH
  canopy fallback for CHM because the 2019 MNS ZIP is open but about 27 GB.
- Poland (`PL`/`POL`) national/fallback path: checks GUGiK NMT terrain and
  NMPT surface WCS in EPSG:2180 plus GUGiK ORTO WMS RGB; falls back to GLO-30
  terrain, forest-masked ETH canopy, and Sentinel-2 when WCS GetCoverage stalls
  or disconnects. NIR comes from Sentinel-2.
- Slovakia (`SK`/`SVK`) working fallback adapter: checked UGKK SR / ZBGIS DMR,
  DMP, and orthophoto WCS/WMS/ImageServer routes, but anonymous probes timed
  out from this environment; fallback terrain/CHM and Sentinel-2 are used.
- Sweden (`SE`/`SWE`) working fallback adapter: checked Lantmateriet Min karta
  orthophoto and height-model WMS routes; no anonymous numeric DEM/DSM route
  was found, so fallback terrain/CHM are used. Lantmateriet orthophoto WMS
  supplies visible RGB when reachable; NIR comes from Sentinel-2.
- Global Tier-C fallback for registered countries without a national adapter:
  Copernicus GLO-30 terrain, Meta/WRI 1 m modeled canopy height where covered,
  ETH Global Canopy Height as the last-resort canopy fallback, Sentinel-2
  RGB+NIR, and global/continental forest typing.
- Continental EEA enrichment where available: Copernicus HRL Dominant Leaf Type,
  CLC+ land cover, and Natura 2000 context layers.
- Global atlas enrichment for every NATO twin build: ISRIC SoilGrids 250 m v2.0
  topsoil pH, organic carbon, clay, and sand layers; WWF HydroSHEDS
  HydroRIVERS and HydroLAKES vectors; JRC/EC Global Surface Water occurrence;
  GBIF observation-density tiles; and a GBIF species-richness grid based on
  distinct `speciesKey` occurrence-search facets, filtered to CC0/CC-BY records
  with coordinates where supported.
  These layers are optional and graceful: failed fetches are logged and skipped.
  Large HydroSHEDS source archives, GBIF tiles, and Meta/WRI CHM tiles are
  cached under `packs/nato/cache/`, which is gitignored.
- Continental EEA protected-species enrichment for covered European NATO twins:
  Habitats Directive Article 17 species distributions (2013-2018, 10 km grid)
  are cached once from the EEA SDI download and rasterized as distinct
  protected-species richness per terrain cell.

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

## Add Global Atlas Layers To An Existing Twin

The global atlas fetcher can be run independently after a twin already exists:

```bash
python3 packs/nato/adapters/atlas_global.py \
  --data-dir twins/ca-algonquin/data \
  --alpha2 ca \
  --theme all \
  --register
```

Use `--theme soil`, `--theme hydrology`, or `--theme species` to refresh one
theme. Registration goes through `scripts/add_layer.py`, so the viewer receives
the same draped PNG/value-grid or localized GeoJSON entries as the existing
DLT, CLC+, and Natura 2000 layers.

Global atlas attribution/provenance:

- ISRIC SoilGrids 250 m v2.0: ISRIC - World Soil Information, CC-BY 4.0.
- HydroRIVERS v1.0 and HydroLAKES v1.0: WWF HydroSHEDS; HydroLAKES is CC-BY
  4.0.
- JRC Global Surface Water occurrence v1.4: European Commission Joint Research
  Centre / Copernicus open data.
- GBIF occurrence density tiles: GBIF Maps API, filtered to CC0/CC-BY where the
  API accepts license filters. Raster values are visualization intensity, not a
  raw occurrence count.
- GBIF species richness: GBIF occurrence search API `speciesKey` facets,
  filtered to records with coordinates and CC0/CC-BY licenses. Raster values are
  distinct species-key counts per coarse query cell, warped to the twin grid.
- EEA Article 17 protected-species richness: European Environment Agency,
  Habitats Directive Article 17 species distribution 2013-2018 10 km grid,
  source record `https://sdi.eea.europa.eu/data/9f71b3e3-f8ec-442b-a2d5-c3c190605ac4`.

## Build The Norwegian Demo

Nordmarka north of Oslo is in Kartverket NHM EPSG:25832 coverage.

```bash
python3 packs/nato/fetch_nato.py \
  --country NO \
  --aoi 10.676,60.018,10.684,60.022 \
  --data-dir twins/no-nordmarka/data \
  --resolution 1 \
  --name "Nordmarka, Norway"
```

Serve it:

```bash
TWIN_DATA_DIR=twins/no-nordmarka/data PORT=4193 HOST=127.0.0.1 node server.js
```

## Build The Spanish Demo

The Valsain / Sierra de Guadarrama demo uses a mixed pine-and-oak AOI in
EPSG:25830 coverage.

```bash
python3 packs/nato/fetch_nato.py \
  --country ES \
  --aoi -4.023,40.868,-4.017,40.872 \
  --data-dir twins/es-valsain/data \
  --resolution 5 \
  --name "Valsain, Sierra de Guadarrama, Spain" \
  --force
```

Serve it:

```bash
TWIN_DATA_DIR=twins/es-valsain/data PORT=4194 HOST=127.0.0.1 node server.js
```

## Build The Belgium Demo

The Sonian Forest / Zoniënwoud demo uses the Flanders DHMV II EPSG:31370
coverage southeast of Brussels.

```bash
python3 packs/nato/fetch_nato.py \
  --country BE \
  --aoi 4.418,50.767,4.423,50.771 \
  --data-dir twins/be-sonian/data \
  --resolution 1 \
  --name "Sonian Forest, Belgium" \
  --force
```

## Build The Czechia Demo

The Šumava demo uses mixed spruce forest in CUZK EPSG:5514 coverage.

```bash
python3 packs/nato/fetch_nato.py \
  --country CZ \
  --aoi 13.520,49.010,13.526,49.014 \
  --data-dir twins/cz-sumava-spruce/data \
  --resolution 2 \
  --name "Sumava National Park, Czechia" \
  --force
```

## Build The Denmark Demo

The Gribskov demo currently uses the fallback stack because anonymous Danish
DHM WCS/WMS access was not reachable without Dataforsyningen credentials.

```bash
python3 packs/nato/fetch_nato.py \
  --country DK \
  --aoi 12.295,56.000,12.303,56.004 \
  --data-dir twins/dk-gribskov/data \
  --resolution 10 \
  --name "Gribskov, Denmark" \
  --force
```

## Build The France Demo

The Fontainebleau demo uses IGN Géoplateforme EPSG:2154 coverage.

```bash
python3 packs/nato/fetch_nato.py \
  --country FR \
  --aoi 2.666,48.398,2.672,48.402 \
  --data-dir twins/fr-fontainebleau/data \
  --resolution 1 \
  --name "Fontainebleau Forest, France" \
  --force
```

## Build The Estonia Demo

The Järvselja demo uses a forested AOI in EPSG:3301 coverage.

```bash
python3 packs/nato/fetch_nato.py \
  --country EE \
  --aoi 27.306,58.276,27.314,58.280 \
  --data-dir twins/ee-jarvselja/data \
  --resolution 10 \
  --name "Jarvselja, Estonia" \
  --force
```

## Build The Finland Demo

The Nuuksio demo uses a conifer-dominant boreal forest AOI.

```bash
python3 packs/nato/fetch_nato.py \
  --country FI \
  --aoi 24.506,60.303,24.515,60.307 \
  --data-dir twins/fi-nuuksio/data \
  --resolution 10 \
  --name "Nuuksio, Finland" \
  --force
```

## Build The Latvia Demo

The Gauja demo uses a forested AOI in EPSG:3059 coverage.

```bash
python3 packs/nato/fetch_nato.py \
  --country LV \
  --aoi 24.920,57.280,24.928,57.284 \
  --data-dir twins/lv-gauja/data \
  --resolution 10 \
  --name "Gauja Forest, Latvia" \
  --force
```

## Build The Luxembourg Demo

The Gréngewald demo uses ACT/data.public.lu national terrain in EPSG:2169.

```bash
python3 packs/nato/fetch_nato.py \
  --country LU \
  --aoi 6.147,49.670,6.154,49.674 \
  --data-dir twins/lu-grengewald/data \
  --resolution 2 \
  --name "Grengewald, Luxembourg" \
  --force
```

## Build The Poland Demo

The Bialowieza demo uses a forested old-growth AOI in EPSG:2180 coverage.
Attribution: © GUGiK (Poland) when national NMT/NMPT/ORTO services are used;
fallbacks are Copernicus GLO-30, forest-masked ETH canopy, modified Copernicus
Sentinel data, and Copernicus HRL DLT / EEA.

```bash
python3 packs/nato/fetch_nato.py \
  --country PL \
  --aoi 23.848,52.719,23.854,52.722 \
  --data-dir twins/pl-bialowieza/data \
  --resolution 10 \
  --name "Bialowieza Forest, Poland" \
  --force
```

## Build The Slovakia Demo

The High Tatras demo uses a conifer-dominant forest AOI. The adapter records
UGKK SR / ZBGIS routes, but this environment uses the fallback stack after
anonymous probes timed out. Attribution: © UGKK SR (Slovakia) for checked
national sources; fallback data are Copernicus GLO-30, forest-masked ETH
canopy, modified Copernicus Sentinel data, and Copernicus HRL DLT / EEA.

```bash
python3 packs/nato/fetch_nato.py \
  --country SK \
  --aoi 20.100,49.150,20.106,49.153 \
  --data-dir twins/sk-high-tatras/data \
  --resolution 10 \
  --name "High Tatras, Slovakia" \
  --force
```

## Build The Sweden Demo

The Tyresta demo uses a boreal forest AOI in EPSG:3006 coverage. Attribution:
© Lantmateriet (Sweden) for orthophoto RGB when used and for checked national
height-model visualization routes; fallback data are Copernicus GLO-30,
forest-masked ETH canopy, modified Copernicus Sentinel data, and Copernicus HRL
DLT / EEA.

```bash
python3 packs/nato/fetch_nato.py \
  --country SE \
  --aoi 18.226,59.178,18.234,59.182 \
  --data-dir twins/se-tyresta/data \
  --resolution 10 \
  --name "Tyresta National Park, Sweden" \
  --force
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
  `terrain/dsm.tif` as GLO-30 plus the selected global canopy-height model.
  Meta/WRI 1 m modeled CHM is tried first and ETH 10 m canopy is the last
  resort. Before DSM/CHM export, the canopy raster is forest-masked to the
  leaf-type/tree-cover extent so noisy global canopy pixels over grass, crop,
  or built land do not become detected stems. Terrain, imagery, and draped
  context layers still cover the full AOI. This preserves the engine CHM
  contract without claiming a global bare-earth DTM exists.
- The NATO Spain adapter writes `terrain/dtm.tif` from void-filled IGN/CNIG
  PNOA-LiDAR MDT and `terrain/dsm.tif` as MDT plus forest-masked ETH Global
  Canopy Height. This keeps national bare-earth terrain while dropping canopy
  structure to the global 10 m ETH fallback for AOIs where no open national
  MDS/DSM WCS is available.
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

## Norway Sources

Elevation:

- NHM DTM WCS, EPSG:25832:
  `https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25832`
- Coverage: `nhm_dtm_topo_25832`
- NHM DOM WCS, EPSG:25832:
  `https://wcs.geonorge.no/skwms1/wcs.hoyde-dom-nhm-25832`
- Coverage: `nhm_dom_topo_25832`
- NHM DTM WCS, EPSG:25833:
  `https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833`
- Coverage: `nhm_dtm_topo_25833`
- NHM DOM WCS, EPSG:25833:
  `https://wcs.geonorge.no/skwms1/wcs.hoyde-dom-nhm-25833`
- Coverage: `nhm_dom_topo_25833`
- WCS version used by the adapter: `1.1.2`, `GetCoverage` with
  `identifier=<coverage>` and a projected `boundingbox=...,urn:ogc:def:crs:EPSG::<zone>`.
- Native resolution: 1 m. The adapter chooses EPSG:25832 west of 12 E and
  EPSG:25833 east of that split for mainland Norway demos.
- Terrain uses NHM DTM. The Norway adapter keeps raw NHM rasters in
  `source/nato/no/`, fills nodata voids in DTM and DOM with GDAL `FillNodata`,
  and uses the filled DTM for terrain ingest. CHM uses filled NHM DOM minus
  filled NHM DTM.

Imagery:

- Checked national Norge i bilder WMS:
  `https://services.norgeibilder.no/wms/ortofoto?service=WMS&request=GetCapabilities`
- Checked national Norge i bilder WMTS:
  `https://tilecache.norgeibilder.no/wmts/utm32_euref89?SERVICE=WMTS&REQUEST=GetCapabilities`
- Both national orthophoto endpoints require token/Norway Digital access from
  this environment, and the national WMS/WMTS catalog entries are marked
  restricted. The Norway adapter therefore uses Sentinel-2 L2A RGB+NIR via
  Element84 Earth Search for both visible drape and the NIR band required by
  `false_color.png`.

Forest leaf type:

- Source: Copernicus HRL Dominant Leaf Type 2018, 10 m, via EEA Discomap.
- Norway is in `EEA_DLT_ALPHA2`, so `fetch_nato.py` automatically adds
  `no_leaf_type` before vegetation analysis. Boreal AOIs should normally type
  mostly as conifer/evergreen.

Attribution:

- Elevation: Kartverket / Geonorge Nasjonal hoydemodell DTM/DOM open data.
- Imagery: modified Copernicus Sentinel data via Element84 Earth Search.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Spain Sources

Elevation:

- IGN/CNIG MDT WCS:
  `https://servicios.idee.es/wcs-inspire/mdt`
- Coverage used for the mainland demo: `Elevacion25830_5`
- WCS version used by the adapter: `2.0.1`, `GetCoverage` with
  `coverageId=Elevacion25830_5`, `subset=x(...)`, `subset=y(...)`, and
  `format=image/tiff`.
- CRS for the Valsain demo: `EPSG:25830` (ETRS89 / UTM zone 30N)
- Native resolution: 5 m. The adapter keeps raw MDT rasters in
  `source/nato/es/`, fills MDT nodata voids with GDAL `FillNodata`, and uses the
  filled MDT for terrain ingest.

Canopy / CHM:

- Checked open MDS/DSM WCS candidates:
  `https://servicios.idee.es/wcs-inspire/mds`,
  `https://servicios.idee.es/wcs-inspire/dsm`, and
  `https://servicios.idee.es/wcs-inspire/mdt-mds`.
- The reachable `mdt-mds`/`mdtmds` paths returned the same MDT coverage list as
  `mdt`; separate open MDS/DSM coverages were not available from this
  environment. The Spain adapter therefore uses ETH Global Canopy Height 2020
  over the national MDT and writes `DSM = MDT + forest-masked ETH canopy`.
- ETH canopy record:
  `https://www.research-collection.ethz.ch/handle/20.500.11850/609802`
- Public Libdrive tile download share:
  `https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt`
- This fallback drops canopy structure to the global 10 m ETH product while
  preserving national LiDAR terrain.

Imagery:

- IGN/CNIG PNOA maxima actualidad WMS:
  `https://www.ign.es/wms-inspire/pnoa-ma`
- RGB layer: `OI.OrthoimageCoverage`
- The open WMS exposes current visible PNOA orthophoto RGB. No open IGN/CNIG
  PNOA CIR/4-band NIR WMS was reachable, so the adapter fetches Sentinel-2 L2A
  NIR via Element84 Earth Search and combines it as `R,G,B,NIR` with the PNOA
  visible drape.
- Element84 Earth Search STAC:
  `https://earth-search.aws.element84.com/v1`
- Collection: `sentinel-2-l2a`

Forest leaf type:

- Source: Copernicus HRL Dominant Leaf Type 2018, 10 m, via EEA Discomap.
- Spain is in `EEA_DLT_ALPHA2`, so `fetch_nato.py` automatically adds
  `es_leaf_type` before vegetation analysis. The Valsain demo AOI was selected
  to include both DLT class `1` broadleaf and class `2` conifer.

Attribution:

- Elevation: Instituto Geografico Nacional (IGN) / CNIG PNOA-LiDAR MDT,
  CC BY 4.0 scne.es.
- Imagery RGB: Instituto Geografico Nacional (IGN) / CNIG PNOA orthophoto WMS,
  CC BY 4.0 scne.es.
- Imagery NIR: modified Copernicus Sentinel data via Element84 Earth Search.
- Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner,
  CC-BY 4.0.
- Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency
  / VITO, open data.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Belgium Sources

Elevation:

- Flanders DHMV WCS: `https://geo.api.vlaanderen.be/DHMV/wcs`
- Coverages: `DHMVII_DTM_1m` and `DHMVII_DSM_1m`
- CRS: `EPSG:31370` (Belgian Lambert 72)
- WCS version: `2.0.1`, `GetCoverage` with `coverageId=<coverage>`,
  `subset=x(...)`, `subset=y(...)`, and `format=image/tiff`.
- Terrain uses void-filled DHMV II DTM. CHM uses filled DHMV II DSM minus DTM.

Imagery:

- Flanders current RGB orthophoto WMS:
  `https://geo.api.vlaanderen.be/OMWRGBMRVL/wms`
- RGB layer: `Ortho`
- No current open Flanders CIR/infrared endpoint was found in the public
  service probes, so Sentinel-2 L2A supplies NIR.

Attribution:

- Elevation: Digitaal Vlaanderen / Agentschap Informatie Vlaanderen DHMV II.
- Imagery RGB: Digitaal Vlaanderen Flanders orthophoto service.
- Imagery NIR: modified Copernicus Sentinel data via Element84 Earth Search.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Czechia Sources

Elevation:

- CUZK DMR 5G terrain ImageServer:
  `https://ags.cuzk.cz/arcgis2/rest/services/dmr5g/ImageServer`
- CUZK DMP 1G surface ImageServer:
  `https://ags.cuzk.cz/arcgis2/rest/services/dmp1g/ImageServer`
- CRS: `EPSG:5514` (S-JTSK / Krovak East North)
- Native pixel size exposed by the services: 2 m.
- Terrain uses void-filled DMR 5G. CHM uses filled DMP 1G minus DMR 5G.

Imagery:

- CUZK ORTOFOTO MapServer:
  `https://ags.cuzk.cz/arcgis1/rest/services/ORTOFOTO/MapServer`
- No open CUZK CIR/infrared service was found in the public ArcGIS catalog, so
  Sentinel-2 L2A supplies NIR.

Attribution:

- Elevation: Czech Office for Surveying, Mapping and Cadastre (CUZK) DMR 5G
  and DMP 1G services.
- Imagery RGB: Czech Office for Surveying, Mapping and Cadastre (CUZK)
  ORTOFOTO service.
- Imagery NIR: modified Copernicus Sentinel data via Element84 Earth Search.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Denmark Sources

National status:

- Checked DHM routes:
  `https://api.dataforsyningen.dk/dhm?service=WCS&request=GetCapabilities`,
  `https://api.dataforsyningen.dk/dhm?service=WMS&request=GetCapabilities`,
  `https://services.datafordeler.dk/DHM/DHM/1.0.0/WCS?SERVICE=WCS&REQUEST=GetCapabilities`,
  and `https://services.datafordeler.dk/DHM/DHM/1.0.0/WMS?SERVICE=WMS&REQUEST=GetCapabilities`.
- These did not expose anonymous DHM/Terræn or DHM/Overflade coverage from this
  environment, so Denmark is currently fallback pending a Dataforsyningen /
  Klimadatastyrelsen token.

Fallback used:

- Terrain: Copernicus DEM GLO-30.
- Canopy / CHM: forest-masked ETH Global Canopy Height 2020.
- Imagery: Sentinel-2 L2A RGB+NIR.
- Forest leaf type: Copernicus HRL Dominant Leaf Type 2018 via EEA Discomap.

Attribution:

- National DHM checked but not used: Klimadatastyrelsen / Dataforsyningen.
- Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR.
- Imagery: modified Copernicus Sentinel data via Element84 Earth Search.
- Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner,
  CC-BY 4.0.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## France Sources

Elevation:

- IGN Géoplateforme WMS-R:
  `https://data.geopf.fr/wms-r/wms`
- DTM layer: `ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES`
- DSM/MNS layer: `ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES.MNS`
- CRS: `EPSG:2154` (Lambert-93)
- The WMS-R service advertises GeoTIFF output and returns Float32 elevation
  rasters. Terrain uses void-filled MNT; CHM uses filled MNS minus MNT.

Imagery:

- RGB layer: `ORTHOIMAGERY.ORTHOPHOTOS`
- NIR/IRC layer: `ORTHOIMAGERY.ORTHOPHOTOS.IRC`
- Band order assembled by the adapter: `R,G,B,NIR`, with NIR copied from the
  first ORTHO IRC band.

Attribution:

- Elevation: IGN France Géoplateforme RGE ALTI / MNS WMS-R layers.
- Imagery: IGN France Géoplateforme BD ORTHO RGB and ORTHO IRC WMS-R layers.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Estonia Sources

National status:

- Maa-amet / Maa- ja Ruumiamet WMS/WFS/WCS service catalog:
  `https://geoportaal.maaamet.ee/est/teenused/wms-wfs-wcs-teenused-p65.html`
- Orthophoto/CIR WMS:
  `https://kaart.maaamet.ee/wms/alus`
- RGB layer: `of10000`
- CIR-NGR layer: `cir_ngr`
- Height display WMS:
  `https://kaart.maaamet.ee/wms/fotokaart`
- Checked DTM/DSM WCS routes did not expose an anonymous numeric terrain and
  surface coverage from this environment. Terrain/CHM therefore use GLO-30 plus
  forest-masked ETH canopy.

Attribution:

- National imagery/elevation services checked: © Maa-amet (Estonia).
- Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR.
- Imagery fallback: modified Copernicus Sentinel data via Element84 Earth
  Search.
- Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner,
  CC-BY 4.0.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Finland Sources

National status:

- NLS 2 m DEM product page:
  `https://www.maanmittauslaitos.fi/en/maps-and-spatial-data/datasets-and-interfaces/product-descriptions/elevation-model-2-m`
- NLS orthophoto product page:
  `https://www.maanmittauslaitos.fi/en/maps-and-spatial-data/datasets-and-interfaces/product-descriptions/orthophotos`
- Checked WCS:
  `https://avoin-karttakuva.maanmittauslaitos.fi/ortokuvat-ja-korkeusmallit/wcs/v2?service=WCS&request=GetCapabilities`
- Checked WMS:
  `https://avoin-karttakuva.maanmittauslaitos.fi/ortokuvat-ja-korkeusmallit/wms/v1?service=WMS&request=GetCapabilities&version=1.3.0`
- Checked OGC API Processes file service:
  `https://avoin-paikkatieto.maanmittauslaitos.fi/tiedostopalvelu/ogcproc/v1/`
- Anonymous requests to those interfaces returned `401` here. Terrain/CHM use
  GLO-30 plus forest-masked ETH canopy, and imagery uses Sentinel-2 RGB+NIR.

Attribution:

- National sources checked: © Maanmittauslaitos/NLS (Finland).
- Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR.
- Imagery: modified Copernicus Sentinel data via Element84 Earth Search.
- Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner,
  CC-BY 4.0.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Latvia Sources

National status:

- LGIA open-data catalog:
  `https://www.lgia.gov.lv/lv/atvertie-dati`
- 20 m DTM:
  `https://s3.storage.pub.lvdc.gov.lv/lgia-opendata/citi/dtm/DTM_Latvija_20m.7z`
- Classified LAS tile list:
  `https://s3.storage.pub.lvdc.gov.lv/lgia-opendata/las/LGIA_OpenData_las_saites.txt`
- RGB orthophoto tile list:
  `http://s3.storage.pub.lvdc.gov.lv/lgia-opendata/ortofoto_rgb_v6/LGIA_OpenData_Ortofoto_rgb_v6_saites.txt`
- Infrared orthophoto tile list:
  `http://s3.storage.pub.lvdc.gov.lv/lgia-opendata/ortofoto_ir_v6/LGIA_OpenData_Ortofoto_ir_v6_saites.txt`
- No anonymous national DTM+DSM WCS was found. DSM-from-LAS assembly is possible
  in principle but too heavy for unattended demo builds, so terrain/CHM use
  GLO-30 plus forest-masked ETH canopy and imagery uses Sentinel-2 RGB+NIR.

Attribution:

- National sources checked: © Latvijas Geotelpiskas informacijas agentura
  (LGIA).
- Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR.
- Imagery: modified Copernicus Sentinel data via Element84 Earth Search.
- Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner,
  CC-BY 4.0.
- Dominant Leaf Type: Copernicus HRL Dominant Leaf Type 2018
  (European Environment Agency / Copernicus Land Monitoring Service).

## Luxembourg Sources

Elevation:

- ACT/data.public.lu BD-L-MNT-1m terrain JP2:
  `https://download.data.public.lu/resources/bd-l-mnt-1m/20180529-134853/EL.ElevationGridCoverage.jp2`
- Checked 2019 LiDAR MNT ZIP:
  `https://s3.eu-central-1.amazonaws.com/download.data.public.lu/resources/lidar-2019-modele-numerique-du-terrain/20200121-082330/ACT2019_MNT_EPSG2169.zip`
- Checked 2019 LiDAR MNS ZIP:
  `https://s3.eu-central-1.amazonaws.com/download.data.public.lu/resources/lidar-2019-modele-numerique-de-la-surface/20200120-105130/ACT2019_MNS_EPSG2169.zip`
- CRS: `EPSG:2169` for the adapter output.
- The 2019 MNT/MNS ZIPs are numeric and open, but about 27 GB each. Remote
  range-opening the internal TIFF timed out in unattended checks, so CHM uses
  forest-masked ETH canopy over national terrain.

Imagery:

- geoportail.lu open WMS:
  `https://wms.geoportail.lu/opendata/service`
- RGB layer: `ortho_latest`
- Infrared layer: `ortho_irc`
- Band order assembled by the adapter: `R,G,B,NIR`, with NIR copied from the
  infrared orthophoto first band.

Attribution:

- Elevation and imagery: © ACT / Administration du cadastre et de la
  topographie (Luxembourg), data.public.lu / geoportail.lu.
- Imagery fallback: modified Copernicus Sentinel data via Element84 Earth
  Search.
- Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner,
  CC-BY 4.0.
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

For global-tier CHM generation, the selected canopy raster is masked before
`terrain/dsm.tif` and `terrain/chm.tif` are written. Meta/WRI 1 m modeled CHM is
preferred; ETH 10 m canopy is used only when Meta/WRI coverage or fetching
fails. The mask precedence is: Copernicus HRL Dominant Leaf Type where
available (`1`/`2` are forest, `0` is non-forest), otherwise ESA WorldCover tree
cover, otherwise CGLS-LC100 forest classes. The binary mask is dilated by one
source-product pixel before nearest-neighbor alignment to the CHM grid,
avoiding a coarse terrain-grid expansion. For DLT masks, aligned DLT `0`
remains a hard no-tree exclusion after that buffer. CLC+ land cover is kept as
context/QA rather than as an additional canopy-retention rule.

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

- WRI + Meta Global Canopy Height, Tolan et al. 2024:
  `s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/`
- Anonymous HTTPS/S3 paths:
  `https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float/tiles.geojson`
  and `forests/v1/alsgedi_global_v6_float/chm/<tileid>.tif`
- License: CC-BY 4.0. Attribution: "Canopy height: Tolan et al. 2024 / WRI +
  Meta, CC-BY 4.0."
- Important limitation: this is a modeled CHM prediction, not measured trees,
  national LiDAR, or a tree census. Build metadata records it as
  "Meta/WRI 1 m modeled CHM (predicted, MAE~2.8 m, saturates >25-30 m)".

- ETH Global Canopy Height 2020 record:
  `https://www.research-collection.ethz.ch/handle/20.500.11850/609802`
- Public Libdrive tile download share:
  `https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt`
- Tile pattern:
  `3deg_cogs/ETH_GlobalCanopyHeight_10m_2020_N39E030_Map.tif`
- License: CC-BY 4.0. ETH is the last-resort global canopy fallback when the
  Meta/WRI path cannot provide coverage.

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
- Norway Kartverket NHM DTM/DOM terrain and CHM.
- Norway Sentinel-2 RGB+NIR imagery fallback because national Norge i bilder
  WMS/WMTS access is restricted/tokened from this environment.
- Spain IGN/CNIG PNOA-LiDAR MDT terrain, with ETH Global Canopy Height CHM
  fallback because no open national MDS/DSM WCS was reachable.
- Spain PNOA RGB imagery plus Sentinel-2 NIR fallback for `false_color.png`.
- Estonia Maa-amet RGB+CIR imagery when reachable, with fallback terrain/CHM.
- Finland fallback terrain/CHM and Sentinel-2 imagery after NLS anonymous
  endpoints returned `401`.
- Latvia fallback terrain/CHM and Sentinel-2 imagery after checking LGIA open
  file routes.
- Luxembourg ACT/data.public.lu national terrain, geoportail.lu RGB/infrared
  imagery, and ETH canopy fallback for CHM.
- Copernicus HRL Dominant Leaf Type over the EEA/European HRL domain for real
  broadleaf/conifer/no-tree typing.
- ESA WorldCover global tree-mask fallback for non-EEA AOIs. This is coarse and
  not a real leaf-type layer.
- Meta/WRI 1 m modeled canopy height for global-tier CHM, with ETH 10 m canopy
  retained as a graceful last-resort fallback.
- AHN CHM draped atlas layer.
- Generic NATO vegetation interpretation with default leaf-type grid support.
  Mixed/unknown cells use a scene-relative median NIR fallback rather than a
  fixed byte threshold, so imagery brightness changes do not silently flip
  evergreen/deciduous typing.

Stubbed:

- Other NATO countries' national source adapters.

No engine-core changes are required.
