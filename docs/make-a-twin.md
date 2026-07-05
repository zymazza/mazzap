# Make your own twin

VEIL is region-agnostic: give it an area of interest and it builds a
browser-viewable 3D twin of that ground. The repo ships **no geospatial data** —
you either let it **fetch national data live** for your AOI, or **drop your own
files** in a directory. One checkout can host **many** twins; each lives in its
own data directory, so a new twin never disturbs the default `./data` twin.

## The fastest path (US): one AOI, fetch the rest

Anywhere in the continental US, hand it a small AOI polygon (a shapefile,
GeoJSON, GeoPackage…) and it queries everything else — 3DEP elevation, NAIP
imagery, LANDFIRE land cover/vegetation — and builds a complete twin:

```bash
npm run build-from-aoi -- --aoi my_area.shp --data-dir ./twins/mine/data --name "My Place"
TWIN_DATA_DIR=./twins/mine/data PORT=4174 npm start    # -> http://127.0.0.1:4174
```

The bundled **demo** is exactly this, driven by one 600-byte committed
shapefile (`packs/us-national/demo/flatirons_aoi.shp`):

```bash
npm run demo        # AOI shapefile -> terrain + imagery + LANDFIRE + typed trees
npm run serve-demo  # -> http://127.0.0.1:4174
```

Outside the US (or to use your own data), do the steps below by hand.

## 0. What you need (manual / bring-your-own-data path)

- The engine running locally (`npm start` works).
- Python with GDAL (`osgeo`), numpy, pyproj — the same deps the build scripts use.
- A **DEM** GeoTIFF for your area (1 m LiDAR DTM, USGS 3DEP, a national grid…).
  USGS 3DEP is free and covers the US; you can pull a tile from
  `https://elevation.nationalmap.gov/.../3DEPElevation/ImageServer/exportImage`.
- Optionally: an **aerial image** (GeoTIFF, RGB or RGB+NIR — NAIP is great), and
  any **map layers** you have (soils, wetlands, zoning, trails, habitat…).

## 1. Terrain + georeferencing — `ingest-dem`

Pick a data directory for the new twin and give it a DEM plus an area of
interest (a bbox, or a polygon GeoJSON). The working CRS is chosen for you (the
DEM's own CRS if it's projected, else the right UTM zone for your AOI) and
written to `georef.json`; everything else reads it from there.

```bash
DATA=./twins/mine/data

# bbox in the DEM's CRS, or pass --bbox-crs:
npm run ingest-dem -- mydem.tif --bbox -105.300 39.970 -105.270 39.990 \
    --bbox-crs EPSG:4326 --name "My Place" --data-dir $DATA

# or an AOI polygon (masks the terrain to the shape):
npm run ingest-dem -- mydem.tif --aoi boundary.geojson --data-dir $DATA
```

This writes `$DATA/terrain/grid.json`, `$DATA/georef.json`,
`$DATA/terrain/aoi_local.geojson`, and a minimal `$DATA/scene.json`. (The
terrain grid follows the frozen [grid contract](grid-contract.md);
`ingest-dem --validate $DATA/terrain/grid.json` checks any grid against it.)

## 2. Aerial imagery — `ingest-imagery` (optional)

```bash
npm run ingest-imagery -- myaerial.tif --data-dir $DATA
```

The image is reprojected and resampled to **exactly** the terrain footprint so
it drapes correctly. A 4-band (RGB+NIR) image also yields a false-color view; a
3-band one just gives the aerial drape. No imagery is fine too — the twin shows
elevation-shaded terrain.

## 3. View it — `TWIN_DATA_DIR`

Serve the new twin on its own port; your default `./data` twin is untouched.

```bash
TWIN_DATA_DIR=$DATA PORT=4174 npm start    # -> http://127.0.0.1:4174
```

Open it: you get the 3D terrain (with imagery if you added it), the surface-mode
buttons, and the click-to-identify GPS readout reading true lat/lon for *your*
twin's CRS. The title bar shows your `--name`.

## 4. Add map layers — `add-layer`

Drop in **any** geospatial file GDAL/OGR can read — GeoJSON, Shapefile
(`.shp`), GeoPackage (`.gpkg`), KML/KMZ, GPX, CSV with coordinates, File
Geodatabase (`.gdb`), GeoTIFF and other rasters — in **any** coordinate system.
It's reprojected to the scene, clipped to your terrain footprint, auto-styled
(categorical → stable colors, continuous rasters → a ramp), auto-labelled, and
made clickable.

```bash
npm run add-layer -- soils.shp     --id soils     --label "Soils"     --data-dir $DATA
npm run add-layer -- wetlands.gpkg --id wetlands   --data-dir $DATA --layer NWI_Wetlands
npm run add-layer -- landcover.tif --id landcover  --data-dir $DATA
npm run add-layer -- trails.gpx    --id trails     --label "Trails"   --data-dir $DATA
```

Useful flags:
- `--layer NAME` — pick one layer from a multi-layer `.gpkg`/`.gdb` (the tool
  lists the available names if you omit it).
- `--src-crs EPSG:n` — only needed for formats that carry no CRS (e.g. a bare
  CSV); otherwise the file's own CRS is used.
- `--label-field FIELD` — which attribute to show as each feature's label
  (auto-detected otherwise).

Reload the viewer; the new layer appears in the Atlas toggles and answers
click-to-identify. Layers persist in `$DATA/atlas/local/viewer-layers.json`.

## 5. Vegetation — `build-vegetation`

Derive trees from the best signal your twin has — LiDAR segmentation stems, a
DSM−DTM canopy-height model, or (as here) an **NDVI canopy** from RGB+NIR
imagery.

Anywhere in the **continental US**, fetch LANDFIRE first and use the
`us-national` pack — then the trees come back *typed* (evergreen/deciduous) with
real community names and representative species, with no per-place setup:

```bash
# 1. fetch LANDFIRE EVT for this twin's footprint (CONUS; one-time, needs net)
python3 packs/us-national/fetch_landfire.py --data-dir $DATA

# 2. build vegetation with the national pack (store + journal stay in $DATA)
TWIN_PACK=us-national TWIN_DATA_DIR=$DATA npm run build-vegetation
```

Reload the viewer; the canopy renders and the vegetation panel shows the
evergreen/deciduous split and the dominant community. (The Flatirons demo this
guide builds comes out ~94% evergreen — Ponderosa Pine, Pinyon, mixed conifer —
which is right for the Colorado foothills.)

Without LANDFIRE / without a pack, `build-vegetation` still works but emits
`type:"unknown"` with no species — the engine never guesses botany. For richer,
locally-correct species, write a region pack (`packs/<name>/vegetation.py` —
`packs/us-national/vegetation.py` shows the hook interface).

> Note: `build-vegetation` writes to a twin **store** (`$DATA/twin.gpkg`); it
> creates one if the twin doesn't have it yet. The store and its journal live
> entirely inside `$DATA`, so running it against a scratch twin never touches
> your default `./data`.

## 6. (Optional) deeper features
- **The twin store + "Ask G.A.I.A." chat**: `npm run migrate` seeds the store
  for a twin; the chat panel and MCP query tools then work against it. See
  [mcp.md](mcp.md).
- **A regional pack**: to get place-specific styling, attribute enrichment, and
  species knowledge, add a `packs/<name>/` (`packs/us-national/` shows the
  pack.json + hook-module shape) and select it with `TWIN_PACK=<name>` or a
  `$DATA/pack.txt`.

## Privacy

A twin's data dir holds real coordinates and attributes for a specific place;
it is **gitignored** (see `.gitignore`). Share the engine and your packs; keep
your ground to yourself.
