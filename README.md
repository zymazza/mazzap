[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/zymazza-mazzap-badge.png)](https://mseep.ai/app/zymazza-mazzap)

# Mazzap v2 — a georeferenced 3D digital-twin engine



https://github.com/user-attachments/assets/deb9e860-abf8-4e34-ab9b-88b3c7a2643f



Mazzap v2 turns a patch of real ground into a standalone, fully **georeferenced** 3D
digital twin you can open in a browser, click to read true GPS coordinates, drape
your own map layers onto the terrain, and ask questions about in natural language.

No database, no cloud, no build step at view time: one tiny zero-dependency Node
static server serves a Three.js viewer over a self-contained bundle of geospatial
data. Nothing is fetched from the network when you view it.

```bash
# A fresh clone ships the engine, not a place — build a twin first:
npm run demo       # build the bundled Flatirons demo twin (needs internet + GDAL)
npm run serve-demo # -> http://127.0.0.1:4174

# ...or build your own area interactively, then serve it:
npm run init       # guided setup: draw an AOI, fetch data, build the twin
npm start          # -> http://127.0.0.1:4173
```

(`npm start` with no twin built yet just tells you to run one of the above.)

Requires Node ≥ 18 (the server uses the built-in `fetch`); the data pipeline
scripts need Python 3 with GDAL (`osgeo`), numpy, pyproj, and Pillow. The
MCP/chat path also needs the Python `mcp` SDK from `requirements.txt`. If you'd
rather not assemble that toolchain yourself, **[run it in a container](#run-with-docker)** —
GDAL, numpy, Node, and the rest come pinned and pre-built.

Point it at **your own** DEM and imagery (see "Build your own twin" below) — the
engine is region-agnostic. The coordinate system, the vegetation knowledge, the
map-layer styling, and any source-acquisition scripts all live in data and in an
optional **regional pack**, never hardcoded in the engine.

## What you get

- **3D terrain** from any DEM (a LiDAR DTM, USGS 3DEP, a national survey grid),
  rendered as a lit mesh with selectable surface modes (aerial / false-color /
  hillshade / elevation).
- **Aerial imagery** draped on the terrain, aligned to the grid so it conforms
  to the topography instead of floating.
- **Click-to-identify + GPS readout** — click anywhere to pin the real-world
  lat/lon (validated against proj4js), elevation, and every map feature true at
  that spot.
- **Atlas layers** — import any vector or raster geospatial file and it is
  reprojected to the scene, clipped to the terrain footprint, auto-styled, and
  made clickable. Bring soils, wetlands, zoning, habitat, hydrology, land cover —
  whatever you have.
- **Vegetation** — a capability ladder that derives trees from the best signal
  available (LiDAR segmentation stems, a canopy-height model, or an NDVI canopy),
  rendered as low-LOD 3D forms. Anywhere in the continental US, the bundled
  **`us-national`** pack types them evergreen/deciduous and names their
  community from LANDFIRE — no regional setup (see "Vegetation" under
  [docs/make-a-twin.md](docs/make-a-twin.md)).
- **G.A.I.A. (Geospatial Agroecological Intelligent Agent)** — an in-viewer chat
  panel that talks to an LLM wired to the twin's read-only query tools (the MCP
  server), scoped to the whole twin, a polygon you draw, or a point you pick.
  See [docs/mcp.md](docs/mcp.md).
- **QField survey companion** — generate a QField project from the active twin,
  collect trails / stream centerlines / photo points / observations in the
  field, then upload the zipped project folder back through the viewer. Uploads
  become journaled `survey_*` entities, rendered as survey layers and queryable
  through MCP. See [docs/survey.md](docs/survey.md).
- **Live telemetry** — register Meshtastic/LoRa gateways from the viewer,
  render live tracker positions, replay recorded days with realtime/speed
  controls and tracker POV, then append selected samples into the curated twin
  store. See [docs/live-inputs.md](docs/live-inputs.md).

## Run with Docker

The static server has **zero npm dependencies**, but the data pipeline needs
Python with GDAL, numpy, pyproj, and Pillow — fiddly to assemble by hand because
GDAL's Python (`osgeo`) bindings must match the system GDAL. The container does
it for you: one image with Node and the whole Python pipeline, all versions
pinned (GDAL via `GDAL_VERSION`, the pip deps in `requirements.txt`, Node via
`NODE_MAJOR`). It's built on the official [OSGeo GDAL](https://github.com/OSGeo/gdal)
image, so GDAL + bindings + numpy already line up.

```bash
docker compose up --build                     # build + serve ./data at http://127.0.0.1:4173
docker compose --profile live-hardware up veil-live --build
                                               # serve with Bluetooth/USB serial access for Meshtastic

# the same image runs every pipeline step — twins persist to ./twins on the host:
docker compose run --rm veil npm run demo     # build the Flatirons demo twin
TWIN_DATA_DIR=/app/twins/demo/data docker compose up   # serve it

# build your own twin (see "Build your own twin" below for the commands):
docker compose run --rm -e TWIN_DATA_DIR=/app/twins/mine/data \
  veil npm run build-from-aoi -- --aoi /app/twins/mine/my_area.shp --name "My Place"
```

`./data` and `./twins` are bind-mounted, so anything the pipeline builds inside
the container lands on the host and stays private/gitignored. Those files are
written as `root` by default; to own them as your host user, prefix the commands
with `VEIL_UID=$(id -u) VEIL_GID=$(id -g)`. The image includes
the live Meshtastic bridge dependencies in `/opt/veil-live`; the
`live-hardware` profile additionally mounts host D-Bus and `/dev` with elevated
device access so Bluetooth and USB serial gateways can be discovered. Use the
plain `veil` service when you only need the viewer, data pipeline, chat, or
network/TCP telemetry. PDAL is optional and is not installed by this image; if
the `pdal` command is absent, LiDAR-derived DSM/DTM generation is skipped as
described in `requirements.txt`. For the **G.A.I.A.** chat panel, pass
`OPENAI_API_KEY` (env var or a `.env` file) — see the chat section below. For an
editable dev environment with the same pinned toolchain, open the repo in a
**Dev Container** (`.devcontainer/`) in VS Code or any devcontainer-aware
editor.

Prefer a local toolchain? Everything below works the same with `npm` and
`python3` directly.

## Hosted mode

For a public website where each visitor gets an isolated session twin and brings
their own OpenAI key, run with `VEIL_HOSTED=1` and
`OPENAI_REQUIRE_USER_KEY=1`. Hosted mode scopes `/data`, chat/MCP tools,
annotations, simulations, and sidebar geospatial uploads to a signed browser
session. Setup details and production caveats are in [docs/hosted.md](docs/hosted.md).

## Build your own twin

**The repo ships only code** (plus one ~600-byte demo AOI). You either fetch
national data live for your area, or bring your own files. One checkout hosts
many twins — point `--data-dir` / `TWIN_DATA_DIR` at a folder and your other
twins are untouched. Full walkthrough: [docs/make-a-twin.md](docs/make-a-twin.md).

### Quick start

Three ways in, easiest first — each writes a self-contained twin you then view
with `npm start`:

```bash
# 1. From a map, in the browser: draw an area, press "Set area & scan layers".
npm run init                                            # -> http://127.0.0.1:4173/init.html

# 2. From an AOI polygon (US): one command fetches terrain, imagery,
#    vegetation, and soils for that footprint.
npm run build-from-aoi -- --aoi my_area.shp --data-dir ./twins/mine/data --name "My Place"

# 3. Anywhere on Earth, from global open data (see "Build a twin anywhere" below):
python3 packs/nato/fetch_nato.py --country FR --aoi 2.6,48.4,2.7,48.5 \
  --aoi-crs EPSG:4326 --data-dir ./twins/mine/data --name "My Place"

# then view it:
TWIN_DATA_DIR=./twins/mine/data npm start               # -> http://127.0.0.1:4173
```

Everything below is the detail behind those three paths.

**First-time setup from a map** — launch the initiation page, optionally search
for a U.S. street address to jump the map to that location, draw an AOI on the
basemap, then press **Set area & scan layers**:

```bash
npm run init
```

By default this builds into `./twins/init/data` and opens
`http://127.0.0.1:4173/init.html`. Set `TWIN_DATA_DIR` first to build somewhere
else:

```bash
TWIN_DATA_DIR=./twins/mine/data npm run init
```

The init page writes the drawn polygon to `<data>/init/aoi.geojson`, then runs
the same national AOI builder as the CLI path below. The nationally available
layers currently implemented in this repo are 3DEP elevation, 3DEP LiDAR Point
Cloud tiles where USGS has coverage, NAIP Plus orthoimagery, LANDFIRE EVT, and
additional LANDFIRE forest/fire ecology rasters, and gSSURGO soils, plus
`us-national` vegetation typing from LANDFIRE. LiDAR, the extra LANDFIRE rasters,
and gSSURGO are best-effort: if a source is unavailable, the twin still builds
from the remaining national sources. When the build finishes, open the viewer
link shown on the setup page.

**US, fetch everything from one AOI** — hand it a small AOI polygon (shapefile,
GeoJSON, GeoPackage…) and it queries 3DEP elevation, 3DEP LiDAR Point Cloud
tiles where available, NAIP Plus orthoimagery, LANDFIRE land cover/vegetation,
additional LANDFIRE forest/fire ecology rasters, and gSSURGO soils for that
footprint and builds a complete, typed twin:

```bash
npm run build-from-aoi -- --aoi my_area.shp --data-dir ./twins/mine/data --name "My Place"
TWIN_DATA_DIR=./twins/mine/data PORT=4174 npm start    # -> http://127.0.0.1:4174

# the bundled demo is exactly this, from one committed shapefile:
npm run demo && npm run serve-demo
```

**Build a twin anywhere (global open data)** — the paths above are US-only. The
bundled `nato` pack (`packs/nato/`) extends VEIL to **any of the 32 NATO member
nations**, assembling a twin from worldwide open datasets with no national
account or portal needed. Give it an ISO country code and a lon/lat AOI:

```bash
python3 packs/nato/fetch_nato.py --country FR --aoi 2.6,48.4,2.7,48.5 \
  --aoi-crs EPSG:4326 --data-dir ./twins/mine/data --name "My Place"
TWIN_DATA_DIR=./twins/mine/data npm start
```

Coverage is **tiered, and the pack is honest about which tier you get**. 14
members ship a national adapter, but the tier they deliver varies. Seven pull
their own higher-resolution national terrain today (Tier A) — **BE, CZ, ES, FR,
LU, NL, NO**; the other seven adapters (**DK, EE, FI, LV, PL, SE, SK**) are
wired up and probe their national services, but currently fall back to the
**global 30 m stack** where an anonymous/tokenless high-res route wasn't
reachable (per-country specifics in [`packs/nato/README.md`](packs/nato/README.md)).
Every remaining member also resolves through that global stack, which works for
any land AOI on Earth. So every member resolves to a working fetch path — but
*builds a twin* is not the same as *builds a great twin*: where a member relies
on the global fallback, the terrain (30 m) and the canopy are **modeled, not
surveyed**, and the quality of the free aerial imagery varies by location and date (see the
note below). The US is served by its own richer `us-national` pack (3DEP LiDAR +
NAIP + LANDFIRE), not this one. What the pack pulls, all as clickable atlas
layers:

- **Terrain** — Copernicus GLO-30, or national LiDAR (e.g. Netherlands AHN,
  Spain PNOA) where a country publishes it.
- **Canopy height → trees** — **Meta / WRI Global 1 m Canopy Height** (Tolan et
  al. 2024, CC-BY 4.0), falling back to ETH Global Canopy Height (10 m). Trees
  are detected as local maxima of this canopy surface.
- **Soil** — ISRIC SoilGrids 250 m: pH, organic carbon, clay %, sand %.
- **Hydrology** — HydroRIVERS + HydroLAKES (WWF) and JRC Global Surface Water.
  Lakes and rivers render as real **water surfaces**, not just flat overlays.
- **Species** — GBIF occurrence density and a GBIF **species-richness** grid (an
  open-data analogue of a GAP richness layer), plus EEA Article 17
  protected-species distributions across the EU.
- **Land cover / leaf type** — Copernicus HRL Dominant Leaf Type and CLC+ across
  Europe, CGLS-LC100 globally, used to type vegetation and mask the canopy to
  forest.

> **The Meta canopy fallback is imperfect — treat it as modeled, not measured.**
> Meta/WRI 1 m canopy height is a *model prediction from satellite imagery*, not
> a survey: ~2.8 m mean absolute error, it saturates (under-estimates) tall
> canopy above ~25–30 m, and carries a US/NEON training-domain bias. It gives
> realistic per-tree structure where no LiDAR exists, but a twin built on a
> country's real national LiDAR is materially more accurate. Likewise, where only
> a hazy or low-sun free Sentinel-2 scene is available for an area, the draped
> aerial imagery can come out dark or tinted. See `packs/nato/README.md` for the
> per-country data tiers and full source list.

During `npm run init`, VEIL also probes an optional national-layer catalog
against the drawn AOI before the build starts. The setup dialog lists only
interactive sources that report an AOI intersection, explains what each layer is
useful for, and passes the checked layer IDs into the build. Those selected
layers are fetched after terrain/georeferencing exists and are registered as
ordinary clickable atlas layers. The catalog includes live fetchers for sources
such as NWI, NHDPlus/WBD, FEMA NFHL, USFWS critical habitat, EPA ecoregions, BLM
surface management and grazing allotments, USFS/NPS boundaries/access layers,
current/historic WFIGS fire perimeters, NCED easements, National Inventory of
Dams, USDA CDL, NRCS MLRA/LRR, PAD-US, current U.S. Drought Monitor polygons,
USDA plant hardiness zones, USFS Wildfire Hazard Potential, and LCMS landscape
change layers, plus selected USGS RCMAP WCS rasters. Some sources use direct
archive downloads instead of service-side AOI clipping: VEIL checks the
advertised file size, requires 20 GB of local free headroom, refuses sources
over 1 GB, downloads the source archive, clips it locally, and discards the raw
archive. Direct downloads are tagged in the setup menu as light under 100 MB,
medium from 100 MB to 1 GB, and heavy at 1 GB or more. Moderate downloads such
as PAD-US are offered as normal guarded downloads, not manual work.
Multi-purpose sources such as PAD-US, LCMS, and RCMAP show child choices in the
menu with their own explanations, so users can choose the interpretation they
need without unnecessary duplicate downloads. The menu keeps a live total of
selected download bytes and the estimated clipped output size for sources that
can report it, including GAP species masks.
The catalog also records self-service download sources such as NLCD, GAP species
habitat species beyond the starter menu, TreeMap, PRISM/gridMET, POLARIS/SOLUS,
MTBS/RAVG, and others. Those are not one-click AOI services yet, but the catalog
includes official download links and notes so users can fetch a product, drop it
into `manual_layers/`, and let VEIL clip/register it locally.

CLI equivalents:

```bash
npm run --silent fetch-national-layers -- catalog
npm run --silent fetch-national-layers -- probe --aoi my_area.geojson
npm run --silent fetch-national-layers -- fetch --aoi my_area.geojson \
  --data-dir ./twins/mine/data --layers nwi_wetlands,nhdplus_flowlines
```

Show the full national catalog, including download/manual links:

```bash
npm run --silent fetch-national-layers -- catalog
```

Check the direct-download endpoints and local disk headroom without downloading:

```bash
npm run --silent fetch-national-layers -- check-downloads
```

**Bring your own data** — anywhere, any format. Drop files in and ingest them:

```bash
# terrain + georeferencing from any DEM (the CRS is the DEM's, or the AOI's UTM zone)
npm run ingest-dem -- mydem.tif --aoi boundary.geojson --data-dir ./twins/mine/data
# aerial imagery, aligned to the terrain footprint
npm run ingest-imagery -- myaerial.tif --data-dir ./twins/mine/data
# any vector/raster layer, any format, any CRS
npm run add-layer -- soils.shp     --id soils    --label "Soils"   --data-dir ./twins/mine/data
npm run add-layer -- wetlands.gpkg --id wetlands --layer NWI       --data-dir ./twins/mine/data
npm run add-layer -- landcover.tif --id landcover                  --data-dir ./twins/mine/data
```

`add_layer` accepts anything GDAL/OGR can read — GeoJSON, Shapefile (`.shp`),
GeoPackage (`.gpkg`), KML/KMZ, GPX, CSV with coordinates, File Geodatabase,
GeoTIFF and other rasters — in any coordinate system. Multi-layer sources take a
`--layer NAME` selector. National raster layers such as LANDFIRE fetch straight
into a twin and register themselves as draped, clickable atlas layers. 3DEP LPC
LiDAR is handled separately by `scripts/fetch_lidar.py`, which queries TNMAccess
for LAZ tiles and derives `terrain/dsm.tif` + `terrain/dtm.tif` for canopy-height
vegetation detection; it does not become an atlas drape. The extra LANDFIRE
forest/fire layers are handled by `scripts/fetch_forest_ecology.py` and include
existing vegetation cover/height, biophysical settings, succession class,
vegetation condition/departure, fire behavior fuel models, canopy cover/height,
canopy base height, canopy bulk density, fuel disturbance, and fire regime group.

### Downloadable and Manual Layers

Many public products are available, but not as small AOI clipping services.
For those, use the drop-directory workflow:

1. Download the source product you need. Prefer one AOI, state, year, metric,
   species, or fire event instead of whole national archives when possible.
2. Put the downloaded file or extracted dataset in `manual_layers/`.
3. Ingest everything GDAL/OGR can read:

   ```bash
   npm run ingest-manual-layers -- --data-dir ./twins/mine/data
   ```

4. If a GeoPackage, FileGDB, or zip has multiple vector layers, ingest each layer
   with derived ids:

   ```bash
   npm run ingest-manual-layers -- --data-dir ./twins/mine/data --all-layers
   ```

Supported inputs include GeoTIFF, GeoJSON, Shapefile, GeoPackage, KML/KMZ, GPX,
CSV with coordinates, File Geodatabase directories, and many zip archives. VEIL
clips each layer to the twin terrain footprint, reprojects it, styles it, and
registers it in the viewer.

Common download/manual sources:

| Layer | Link | Note |
| --- | --- | --- |
| NLCD / Annual NLCD | <https://www.mrlc.gov/data> | Pick product and year. |
| PAD-US | <https://www.usgs.gov/programs/gap-analysis-project/science/pad-us-data-download> | Direct download path offers protection status, public access, manager/owner, and designation type choices. |
| GAP species habitat | <https://www.usgs.gov/programs/gap-analysis-project/science/species-data-download> | Starter species are automated as selectable child downloads; additional species can be added from ScienceBase metadata. |
| USFS TreeMap | <https://data.fs.usda.gov/geodata/rastergateway/treemap/index.php> | Pick year and attribute raster. |
| USFS LCMS | <https://data.fs.usda.gov/geodata/rastergateway/LCMS/index.php> | Fetched automatically for default land-cover/change/loss/gain views. |
| USFS FIA DataMart | <https://apps.fs.usda.gov/fia/datamart/> | Tabular/statistical; not a direct atlas layer yet. |
| USFS Aerial Detection Survey | <https://www.fs.usda.gov/science-technology/data-tools-products/fhp-mapping-reporting/detection-surveys> | Download region/year geospatial data. |
| Wildfire Hazard Potential | <https://research.fs.usda.gov/firelab/products/dataandtools/wildfire-hazard-potential> | Fetched automatically from the 2023 classified image service. |
| MTBS burn severity | <https://www.mtbs.gov/direct-download> | Pick fire/state/national products. |
| RAVG post-fire condition | <https://data.fs.usda.gov/geodata/rastergateway/ravg/index.php> | Pick fire and product raster. |
| gNATSGO | <https://www.nrcs.usda.gov/resources/data-and-reports/gridded-national-soil-survey-geographic-database-gnatsgo> | Bulk soils source; base builds already fetch SDA/gSSURGO. |
| POLARIS soils | <http://hydrology.cee.duke.edu/POLARIS/> | Pick property, depth, and statistic raster. |
| SOLUS100 soils | <https://www.nrcs.usda.gov/resources/data-and-reports/soil-landscapes-of-the-united-states-solus> | Pick property/depth raster. |
| NRCS MLRA/LRR | <https://www.nrcs.usda.gov/resources/data-and-reports/major-land-resource-area-mlra> | Fetched automatically from the hosted feature service; downloadable database also available. |
| RAP rangelands | <https://rangelands.app/> | Export AOI/year/metric outputs. |
| USGS RCMAP | <https://www.mrlc.gov/data/type/rcmap-time-series-trends> | Fetched automatically from MRLC WCS for selected 2025 component views. |
| LANID irrigation | <https://zenodo.org/records/5548555> | Pick annual irrigation or frequency raster. |
| MIrAD-US irrigation | <https://data.usgs.gov/datacatalog/data/USGS:5db08e84e4b0b0c58b56e04f> | Pick year/resolution raster. |
| OpenET | <https://etdata.org/api/> | API/date/product workflow; export before ingest. |
| PRISM | <https://prism.oregonstate.edu/> | Pick variable and period. |
| gridMET | <https://www.climatologylab.org/gridmet.html> | Pick variable/year or AOI subset. |
| SNODAS | <https://nsidc.org/data/g02158/versions/1> | Convert flat-binary products to GeoTIFF first. |
| U.S. Drought Monitor | <https://droughtmonitor.unl.edu/DmData/GISData.aspx> | Direct download path fetches the current weekly shapefile automatically. |
| USGS geology / NGMDB | <https://ngmdb.usgs.gov/mapview/> | Pick map/product and download geospatial files if offered. |
| EPA ATTAINS | <https://www.epa.gov/waterdata/get-data-access-public-attains-data> | API/table/geospatial service; adapter still needed. |
| Water Quality Portal | <https://www.waterqualitydata.us/> | Download stations/results; rich observation adapter still needed. |
| NOAA C-CAP | <https://coast.noaa.gov/digitalcoast/data/ccapregional.html> | Pick regional/high-resolution product and year. |
| NOAA Essential Fish Habitat | <https://www.habitat.noaa.gov/application/efhinventory/> | Pick council/species/life-stage GIS data. |
| USDA plant hardiness zones | <https://prism.oregonstate.edu/phzm/> | Direct download path fetches the 2023 CONUS shapefile automatically. |
| NatureServe biodiversity | <https://www.natureserve.org/access-data> | Download open GIS layers. |
| TNC Resilient and Connected Network | <https://crcs.tnc.org/pages/data-terrestrial-resilience> | Download state/network component data. |

The terrain grid that `ingest-dem` writes conforms to a frozen interface — see
[docs/grid-contract.md](docs/grid-contract.md). It is the one contract every
terrain consumer depends on; don't change it silently.

## QField survey companion

VEIL has a built-in field loop for QField: the app generates the survey package
from the current twin, QField records edits against that package, and the viewer
uploads the finished package back into the twin store.

Build a package for the twin you are serving:

```bash
npm run build-survey-package -- --data-dir ./twins/mine/data --name "My Place"
TWIN_DATA_DIR=./twins/mine/data npm start
```

Then open the viewer's **Survey companion** panel and download
`survey-package.zip`. Unzip or sideload the `survey/` project folder into
QField and open `project.qgs`. The generated package contains:

- `survey.gpkg` with four editable layers: `trails`, `stream_centerlines`,
  `photo_points`, and `observations`.
- `project.qgs`, with forms wired for stable UUIDs, active / retired / removed
  status, capture time, GPS accuracy, notes, and camera attachments.
- `basemap.tif` when the twin has georeferenced imagery.

After fieldwork, zip the whole QField project folder, including its `DCIM/`
photos, and upload it from the same **Survey companion** panel. The server saves
the raw zip under `<data>/surveys/incoming/`, logs it in
`<data>/surveys/uploads.log.jsonl`, runs `scripts/ingest_survey.py --pending`,
and refreshes the viewer's survey layers. If Python ingest is unavailable, the
upload is kept and `npm run export` will process pending uploads later.

Survey features are stored as ordinary twin-store entities with stable IDs like
`survey_trails:<uuid>`. Re-uploading the same project is safe: unchanged
features are skipped, moved features keep identity, and retirement is explicit
through the `status` field rather than inferred from a missing feature. To gate
uploads on a shared LAN, create a gitignored `.survey_token`; the viewer prompts
for the token and sends it as `X-Survey-Token`.

Full details: [docs/survey.md](docs/survey.md).

## MCP server and app chat

`scripts/mcp_server.py` is the MCP surface over the twin store. It exposes
the same facts the viewer uses — terrain, entities, atlas layers, survey
layers, provenance, and region summaries — as structured tools for LLM
agents. The store itself is read-only; the only write tools are
`draw_polygon` / `draw_point` / `clear_drawings`, which let the LLM put
labeled orange shapes on the live 3D map (a flat `annotations.json` the
viewer polls — never the store). It reads the active twin from
`TWIN_DATA_DIR` or `./data`.

Install the Python side once:

```bash
pip install -r requirements.txt
TWIN_DATA_DIR=./twins/mine/data npm run rebuild-store      # only if twin.gpkg is missing/stale
TWIN_DATA_DIR=./twins/mine/data python3 scripts/twin_query.py describe_twin
```

You do **not** start the MCP server separately for the browser app. Open **Ask
G.A.I.A.**, and the Node server lazily spawns `scripts/mcp_server.py` on the
first chat request. Provide an OpenAI key either way:

```bash
# Bring-your-own-key (recommended, esp. for a shared/public twin): start with no
# key, then click "Key" in the chat panel and paste yours. It is stored only in
# your browser (localStorage) and sent per request as X-OpenAI-Key — never on the
# server or in the repo.
TWIN_DATA_DIR=./twins/mine/data npm start

# Server-side key (single-user/local convenience): the server uses this for any
# request that doesn't bring its own.
OPENAI_API_KEY=sk-... TWIN_DATA_DIR=./twins/mine/data npm start
# or put the key in a gitignored .openai_key file

# Public deployment: forbid the server-key fallback so every request must BYOK.
OPENAI_REQUIRE_USER_KEY=1 TWIN_DATA_DIR=./twins/mine/data npm start

# Fully local, no key, nothing leaves the machine: point the chat at an Ollama
# model. `npm run start:local` is the shortcut (CHAT_PROVIDER=ollama,
# OLLAMA_MODEL=gpt-oss:20b) and also opens the viewer in an integrated-GPU Firefox
# profile so CUDA VRAM stays free for the model; use `npm run start:local:server`
# (or VEIL_OPEN_BROWSER=0) for server-only. CHAT_PROVIDER=ollama is also inferred
# whenever OLLAMA_MODEL is set. Needs `ollama serve` running with a tool-calling
# model pulled. gpt-oss:20b is a good 24 GB-GPU default (~16 GB on the GPU at the
# 96k default context — its KV cache is cheap; even the full 131072 fits ~17 GB).
TWIN_DATA_DIR=./twins/mine/data npm run start:local
# tune with OLLAMA_HOST (default http://127.0.0.1:11434), OLLAMA_NUM_CTX (default
# 98304), OLLAMA_TEMPERATURE (default 0), OLLAMA_MAX_TOOL_ROUNDS (default 24)
```

In the app, questions can target:

- **Whole land** — the default scope.
- **Drawn region** — click **Draw region**, place 3+ terrain points, then ask
  about "this area".
- **Picked point** — click **Pick point**, choose a terrain point, then ask
  about "here" with nearby context preloaded.

The chat transcript shows the MCP tools the model called, so answers can be
checked against store data and layer provenance.

For an external MCP client, register the stdio server directly:

```bash
claude mcp add veil-twin -- env TWIN_DATA_DIR=/ABS/PATH/TO/twins/mine/data \
  python3 /ABS/PATH/TO/veil/scripts/mcp_server.py
```

Useful direct sanity checks:

```bash
TWIN_DATA_DIR=./twins/mine/data python3 scripts/twin_query.py describe_twin
TWIN_DATA_DIR=./twins/mine/data python3 scripts/twin_query.py identify_at '{"point":{"x":50,"y":100}}'
npm test
```

Core MCP tools include `describe_twin`, `find_entities`, `get_entity`,
`entity_history`, `identify_at`, `sample_raster`, `list_layers`,
`layer_summary`, `summarize_region`, `aggregate_entities`,
`canopy_change`, and `list_survey_layers`, plus the map-drawing trio
`draw_polygon` / `draw_point` / `clear_drawings` — answers can point at places
with labeled orange shapes in the viewer (built-in chat and external MCP
clients alike; the chat panel's **Clear drawings** button removes them).
Survey uploads appear automatically as `survey_*` kinds for `find_entities`,
`aggregate_entities`, and `summarize_region`, are catalogued by
`list_survey_layers`, and are now included in point `identify_at` (with photo
and status). Full tool semantics and examples: [docs/mcp.md](docs/mcp.md).

## Architecture: engine vs. regional pack

VEIL is a **region-agnostic engine** (`scripts/`, `public/`, `server.js`) plus an
optional **regional content pack** (`packs/<name>/`). Nothing in the engine names
a CRS, a layer, or a species.

- **Coordinates are data.** `<data>/georef.json` carries the projected CRS (EPSG +
  a proj4 string), the geographic CRS, and the scene origin. Python reads it
  through `scripts/twin_georef.py`; the viewer through vendored **proj4js**
  (`public/vendor/proj4.js`) in `public/viewer/georef.js`. Scene coordinates are
  local meters (x = east, y = north) offset from the origin; the store keeps the
  same convention.
- **Packs** load via `scripts/twin_pack.py` (chosen by `TWIN_PACK`, else
  `<data>/pack.txt`, else none). A pack is a folder with `pack.json` and optional
  `load(context)` hook modules: `vegetation.py` (species/community/type knowledge)
  and `layers.py` (atlas styles, attribute enrichment, named raster renderings),
  plus its own source-acquisition scripts. Without a pack the engine auto-styles
  every layer and emits trees with `type:"unknown"` — it never guesses local
  botany.
- **`packs/us-national`** is the region-agnostic exception: it encodes no single
  place, just CONUS-wide LANDFIRE EVT. Any US twin can use it
  (`TWIN_PACK=us-national`) to type vegetation evergreen/deciduous and name
  communities, after fetching LANDFIRE for the twin's footprint
  (`packs/us-national/fetch_landfire.py --data-dir <data>`). A single-region
  pack adds what no national dataset has: curated local species, your own atlas
  styling, regional attribute enrichment. National datasets
  (3DEP/NAIP/NLCD/LANDFIRE/GAP/gSSURGO) belong in `us-national`-style packs,
  distinct from both the engine core and any one region.

```
server.js                 zero-dependency static server (+ /api/chat, /api/* )
public/
  index.html  app.js  chat.js        UI, boot, click-to-identify, chat panel
  viewer/  scene.js terrain.js vegetation.js overlays.js buildings3d.js
           georef.js                 scene-local meters <-> lon/lat (proj4js)
  vendor/  three.min.js  OrbitControls.js  proj4.js
scripts/                  the region-agnostic engine
  twin_georef.py  twin_pack.py        read georef.json / load the active pack
  ingest_dem.py  ingest_imagery.py    genesis: DEM + imagery -> a twin
  add_layer.py                        import any geospatial file as a layer
  analyze_vegetation.py  veg_detect.py   capability-gated vegetation
  build_viewer_layers.py              generic atlas localization + styling
  twin_store.py  migrate_to_store.py  rebuild_store.py   the twin store
  mcp_server.py  twin_query.py        read-only query tools for the chat/agent
packs/<name>/             optional regional content pack
  pack.json  vegetation.py  layers.py   knowledge + styling hooks
  *.py                                  its own source-acquisition scripts
data/  (or any --data-dir)             one twin instance: georef, terrain,
                                       imagery, vegetation, atlas, store
```

## The twin store

The system of record is the **twin store** — an append-only write journal in
`<data>/journal/`, materialized as a GeoPackage at `<data>/twin.gpkg`. The
journal lives inside the twin's data dir (private, gitignored along with the
rest of it); `npm run rebuild-store` reconstructs the GeoPackage from it
exactly. The flat JSON the viewer loads is an *export* of the store. Origin
and CRS live in the store's `meta`; coordinates are scene-local meters. See
`CLAUDE.md` and `docs/mcp.md` for the store model and the query/agent layer.

## Tests

```bash
npm test            # offline: the committed fixture twin
npm run test:demo   # real data: the Flatirons demo twin
```

`npm test` runs `scripts/twin_query_test.py` against a tiny **committed fixture
twin** (`tests/fixtures/mini-twin/data`) — a synthetic, network-free twin built
by `scripts/build_test_fixture.py`. It needs no internet and no GDAL (just
Python + `pyproj`; Node for the proj4js cross-check), so the full query suite
runs offline anywhere — in CI or on a fresh clone. Every expectation is derived
from the twin under test, never hardcoded to a place.

`npm run test:demo` runs the same suite against the **Flatirons demo twin**
(`twins/demo/data`), building it first from live national services if it isn't
there (needs internet + GDAL once) — the real-data check. Point `TWIN_DATA_DIR`
at any other twin to run the suite against it. Regenerate the fixture with
`npm run build-test-fixture` after a store schema change, then commit it.

## Privacy

A twin's `data/` (and any `--data-dir`) holds real coordinates, parcel/owner
attributes, building footprints, and imagery for a specific place — it is **not**
tracked by git in this repo (see `.gitignore`). Share the engine and your pack;
keep your ground to yourself. If you previously committed a twin and want it gone
from history (not just future commits), rewrite history with `git filter-repo` —
or, safer, start a fresh repo from the current tree, since a rewritten history
can still leave reachable objects in clones and forks.

## Security posture

The server is meant for localhost or a trusted LAN/Tailscale bind, not the
open internet:

- **`POST /api/chat` spends on an OpenAI key.** Each request uses the caller's
  own key if the viewer supplies one (the chat panel's **"Key"** button stores it
  in the browser's `localStorage` and sends it as the `X-OpenAI-Key` header — it
  never touches the repo or the server's disk); otherwise it falls back to the
  server's `OPENAI_API_KEY` / `.openai_key`. That fallback is **unauthenticated
  spend**: anyone who can reach the port can call OpenAI on the server's key. For
  a public deployment set **`OPENAI_REQUIRE_USER_KEY=1`** so the server never uses
  its own key and every request must bring its own.
- Survey uploads can be token-gated (a `.survey_token` file at the repo root
  enforces an `X-Survey-Token` header); without the file the route is open.
- **The live-telemetry API (`/api/live/*`) is unauthenticated by default and can
  spawn local processes.** Unless you set a `.live_token` (or `VEIL_LIVE_TOKEN`),
  anyone who can reach the port can register/start Meshtastic bridge processes
  and trigger serial/Bluetooth device scans. It is disabled entirely in hosted
  mode; set a token before exposing the port on a LAN.
- There is no TLS, no rate limiting, and no auth on the building-placement
  endpoint — by design, for a single-user posture.
- State-changing routes require the request's `Origin`/`Referer` to match `Host`
  (blocking ordinary cross-site drive-by POSTs). That check alone does **not**
  stop DNS rebinding; on any exposed deployment set **`VEIL_ALLOWED_HOSTS`**
  (comma-separated hostnames) to pin the acceptable `Host` values.
