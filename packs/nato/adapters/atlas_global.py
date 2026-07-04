"""Global atlas-layer fetchers for the NATO pack.

The functions in this module mirror the EEA atlas fetcher contract: each
fetcher clips/warps source data to the built twin's terrain grid and returns a
layer dictionary that ``fetch_nato.py`` passes to ``scripts/add_layer.py``.
The module is also runnable so a theme can be added to an existing twin without
rebuilding the whole pack.
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import numpy as np
from osgeo import gdal, ogr, osr
from PIL import Image
from pyproj import Transformer

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_DIR = os.path.dirname(HERE)
PROJECT = os.path.dirname(os.path.dirname(PACK_DIR))
SCRIPTS = os.path.join(PROJECT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import twin_georef  # noqa: E402

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

CACHE_DIR = os.path.abspath(os.environ.get(
    "VEIL_NATO_CACHE", os.path.join(PACK_DIR, "cache")
))
USER_AGENT = "veil/1.0 (+packs/nato global atlas)"
NODATA_FLOAT = -9999.0

SOIL_SPECS = [
    {
        "property": "phh2o",
        "coverage": "phh2o_0-5cm_mean",
        "layer_suffix": "soil_phh2o_0_5cm",
        "label": "Soil pH (0-5cm)",
        "description": "ISRIC SoilGrids v2.0 pH in H2O at 0-5 cm depth.",
        "value_kind": "soil pH",
        "value_unit": "pH",
        "scale": 0.1,
        "valid_range": (0.0, 14.0),
    },
    {
        "property": "soc",
        "coverage": "soc_0-5cm_mean",
        "layer_suffix": "soil_soc_0_5cm",
        "label": "Soil organic carbon (0-5cm)",
        "description": "ISRIC SoilGrids v2.0 soil organic carbon at 0-5 cm depth.",
        "value_kind": "soil organic carbon",
        "value_unit": "g/kg",
        "scale": 0.1,
        "valid_range": (0.0, 1000.0),
    },
    {
        "property": "clay",
        "coverage": "clay_0-5cm_mean",
        "layer_suffix": "soil_clay_0_5cm",
        "label": "Clay % (0-5cm)",
        "description": "ISRIC SoilGrids v2.0 clay fraction at 0-5 cm depth.",
        "value_kind": "clay fraction",
        "value_unit": "%",
        "scale": 0.1,
        "valid_range": (0.0, 100.0),
    },
    {
        "property": "sand",
        "coverage": "sand_0-5cm_mean",
        "layer_suffix": "soil_sand_0_5cm",
        "label": "Sand % (0-5cm)",
        "description": "ISRIC SoilGrids v2.0 sand fraction at 0-5 cm depth.",
        "value_kind": "sand fraction",
        "value_unit": "%",
        "scale": 0.1,
        "valid_range": (0.0, 100.0),
    },
]

HYDRORIVERS = {
    "eu": "https://data.hydrosheds.org/file/HydroRIVERS/HydroRIVERS_v10_eu_shp.zip",
    "na": "https://data.hydrosheds.org/file/HydroRIVERS/HydroRIVERS_v10_na_shp.zip",
}
HYDROLAKES = "https://data.hydrosheds.org/file/hydrolakes/HydroLAKES_polys_v10_shp.zip"
JRC_GSW = (
    "https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/"
    "occurrence_{lon}_{lat}v1_4_2021.tif"
)
GBIF_DENSITY = (
    "https://api.gbif.org/v2/map/occurrence/density/"
    "{z}/{x}/{y}@1x.png?srs=EPSG:4326&bin=hex&license=CC0_1_0&license=CC_BY_4_0"
)


def fetch_global_atlas_layers(aoi, out_dir, data_dir, alpha2="nato"):
    """Fetch all optional global atlas themes, logging and skipping failures."""
    del aoi
    layers = []
    for theme, fetcher in (
        ("soil", fetch_soil_layers),
        ("hydrology", fetch_hydrology_layers),
        ("species", fetch_species_layers),
    ):
        try:
            layers.extend(fetcher(out_dir, data_dir, alpha2=alpha2))
        except Exception as exc:  # noqa: BLE001
            print(f"  optional global atlas theme skipped ({theme}): {exc}")
    return layers


def fetch_soil_layers(out_dir, data_dir, alpha2="nato"):
    os.makedirs(out_dir, exist_ok=True)
    layers = []
    for spec in SOIL_SPECS:
        try:
            layer = _fetch_soil_property(spec, out_dir, data_dir, alpha2)
            if layer:
                layers.append(layer)
        except Exception as exc:  # noqa: BLE001
            print(f"  optional SoilGrids layer skipped ({spec['label']}): {exc}")
    return layers


def fetch_hydrology_layers(out_dir, data_dir, alpha2="nato"):
    os.makedirs(out_dir, exist_ok=True)
    layers = []
    for label, fetcher in (
        ("HydroRIVERS", _fetch_hydrorivers),
        ("HydroLAKES", _fetch_hydrolakes),
        ("JRC Global Surface Water", _fetch_jrc_gsw_occurrence),
    ):
        try:
            layer = fetcher(out_dir, data_dir, alpha2)
            if layer:
                layers.append(layer)
        except Exception as exc:  # noqa: BLE001
            print(f"  optional hydrology layer skipped ({label}): {exc}")
    return layers


def fetch_species_layers(out_dir, data_dir, alpha2="nato"):
    os.makedirs(out_dir, exist_ok=True)
    layers = []
    try:
        layer = _fetch_gbif_density(out_dir, data_dir, alpha2)
        if layer:
            layers.append(layer)
    except Exception as exc:  # noqa: BLE001
        print(f"  optional GBIF density layer skipped: {exc}")
    return layers


def _fetch_soil_property(spec, out_dir, data_dir, alpha2):
    layer_id = "%s_%s" % ((alpha2 or "nato").lower(), spec["layer_suffix"])
    raw = os.path.join(out_dir, layer_id + "_soilgrids_wcs_4326_raw.tif")
    warped_raw = os.path.join(out_dir, layer_id + "_raw_grid.tif")
    aligned = os.path.join(out_dir, layer_id + ".tif")

    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _pad_bbox(_transform_bounds(bounds, working_crs, "EPSG:4326"), 0.01)
    url = _soilgrids_wcs_url(spec["property"], spec["coverage"], wgs_bbox)
    _download_raster(url, raw, service_name="SoilGrids WCS")
    _warp_raster_to_grid(
        raw, warped_raw, grid, bounds, working_crs,
        resample_alg="bilinear", output_type=gdal.GDT_Float32,
        src_nodata=-32768, dst_nodata=NODATA_FLOAT,
    )
    stats = _scale_float_raster(
        warped_raw, aligned, spec["scale"], spec.get("valid_range"), NODATA_FLOAT
    )
    metadata = {
        "status": "ok",
        "theme": "soil",
        "source": "ISRIC SoilGrids 250m v2.0",
        "provider": "ISRIC - World Soil Information",
        "service": "https://maps.isric.org/mapserv",
        "endpoint": url,
        "coverage_id": spec["coverage"],
        "license": "CC-BY 4.0",
        "source_crs": "EPSG:4326 WCS subset/output",
        "grid_crs": working_crs,
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "raw": os.path.basename(raw),
        "raster": os.path.basename(aligned),
        "scale_applied": spec["scale"],
        "statistics": stats,
        "fetched_at": _utcnow(),
    }
    _write_json(os.path.join(out_dir, layer_id + "_fetch.json"), metadata)
    return {
        "path": aligned,
        "layer_id": layer_id,
        "label": spec["label"],
        "description": spec["description"],
        "uses": "Topsoil context for ecology, hydrology, and vegetation interpretation.",
        "value_kind": spec["value_kind"],
        "value_unit": spec["value_unit"],
        "value_classification": "continuous",
        "metadata": metadata,
        "attribution": [
            "ISRIC SoilGrids 250m v2.0: ISRIC - World Soil Information, CC-BY 4.0."
        ],
    }


def _soilgrids_wcs_url(prop, coverage, wgs_bbox):
    endpoint = "https://maps.isric.org/mapserv?map=/map/%s.map" % prop
    params = [
        ("SERVICE", "WCS"),
        ("VERSION", "2.0.1"),
        ("REQUEST", "GetCoverage"),
        ("COVERAGEID", coverage),
        ("FORMAT", "image/tiff"),
        ("SUBSET", "long(%.8f,%.8f)" % (wgs_bbox[0], wgs_bbox[2])),
        ("SUBSET", "lat(%.8f,%.8f)" % (wgs_bbox[1], wgs_bbox[3])),
        ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
        ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
    ]
    return endpoint + "&" + urllib.parse.urlencode(params, safe="(),/:")


def _fetch_hydrorivers(out_dir, data_dir, alpha2):
    layer_id = "%s_hydrorivers" % (alpha2 or "nato").lower()
    out = os.path.join(out_dir, layer_id + ".geojson")
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _pad_bbox(_transform_bounds(bounds, working_crs, "EPSG:4326"), 0.002)
    sources = [_hydrorivers_shp(c) for c in _hydro_continents(wgs_bbox)]
    feature_count = _clip_merge_vectors(sources, out, wgs_bbox)
    metadata = {
        "status": "ok",
        "theme": "hydrology",
        "source": "HydroRIVERS v1.0",
        "provider": "WWF HydroSHEDS",
        "license": "HydroSHEDS free/open data license",
        "source_files": [os.path.basename(s) for s in sources],
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "feature_count": feature_count,
        "geojson": os.path.basename(out),
        "fetched_at": _utcnow(),
    }
    _write_json(os.path.join(out_dir, layer_id + "_fetch.json"), metadata)
    return {
        "path": out,
        "layer_id": layer_id,
        "label": "Rivers (HydroRIVERS)",
        "description": "HydroRIVERS v1.0 river network clipped to the twin AOI.",
        "uses": "River context, Strahler order, and long-term discharge reference.",
        "value_kind": "river feature",
        "value_unit": "feature",
        "value_classification": "categorical",
        "metadata": metadata,
        "attribution": ["HydroRIVERS v1.0: WWF HydroSHEDS."],
    }


def _fetch_hydrolakes(out_dir, data_dir, alpha2):
    layer_id = "%s_hydrolakes" % (alpha2 or "nato").lower()
    out = os.path.join(out_dir, layer_id + ".geojson")
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _pad_bbox(_transform_bounds(bounds, working_crs, "EPSG:4326"), 0.002)
    source = _hydrolakes_shp()
    feature_count = _clip_merge_vectors([source], out, wgs_bbox)
    metadata = {
        "status": "ok",
        "theme": "hydrology",
        "source": "HydroLAKES v1.0",
        "provider": "WWF HydroSHEDS",
        "license": "CC-BY 4.0",
        "source_file": os.path.basename(source),
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "feature_count": feature_count,
        "geojson": os.path.basename(out),
        "fetched_at": _utcnow(),
    }
    _write_json(os.path.join(out_dir, layer_id + "_fetch.json"), metadata)
    return {
        "path": out,
        "layer_id": layer_id,
        "label": "Lakes & reservoirs (HydroLAKES)",
        "description": "HydroLAKES v1.0 lake and reservoir polygons clipped to the twin AOI.",
        "uses": "Lake, reservoir, and shoreline context for water/terrain interpretation.",
        "value_kind": "lake or reservoir polygon",
        "value_unit": "feature",
        "value_classification": "categorical",
        "metadata": metadata,
        "attribution": ["HydroLAKES v1.0: WWF HydroSHEDS, CC-BY 4.0."],
    }


def _fetch_jrc_gsw_occurrence(out_dir, data_dir, alpha2):
    layer_id = "%s_jrc_gsw_occurrence" % (alpha2 or "nato").lower()
    out = os.path.join(out_dir, layer_id + ".tif")
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _pad_bbox(_transform_bounds(bounds, working_crs, "EPSG:4326"), 0.002)
    urls = _jrc_occurrence_urls(wgs_bbox)
    if not urls:
        raise RuntimeError("no JRC GSW occurrence tiles found for AOI")
    if _raster_ok(out) and _raster_stats(out, positive=True).get("valid_px", 0) == 0:
        os.remove(out)
    sources = ["/vsicurl/" + u for u in urls]
    _warp_raster_to_grid(
        sources, out, grid, bounds, working_crs,
        resample_alg="bilinear", output_type=gdal.GDT_Byte,
        src_nodata=255, dst_nodata=255,
    )
    stats = _raster_stats(out, positive=True)
    if stats.get("valid_px", 0) == 0:
        raise RuntimeError("GBIF density tile mosaic is empty over the AOI")
    metadata = {
        "status": "ok",
        "theme": "hydrology",
        "source": "JRC Global Surface Water occurrence v1.4 (2021)",
        "provider": "European Commission Joint Research Centre / Copernicus",
        "license": "Copernicus open data",
        "endpoint": "https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/",
        "source_tiles": [os.path.basename(u) for u in urls],
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "raster": os.path.basename(out),
        "statistics": stats,
        "fetched_at": _utcnow(),
    }
    _write_json(os.path.join(out_dir, layer_id + "_fetch.json"), metadata)
    return {
        "path": out,
        "layer_id": layer_id,
        "label": "Surface water occurrence (JRC GSW)",
        "description": "JRC Global Surface Water occurrence, 1984-2021, clipped to the twin AOI.",
        "uses": "Persistent and seasonal surface-water context for hydrology QA.",
        "value_kind": "surface water occurrence",
        "value_unit": "%",
        "value_classification": "continuous",
        "metadata": metadata,
        "attribution": [
            "JRC Global Surface Water: European Commission Joint Research Centre / Copernicus."
        ],
    }


def _fetch_gbif_density(out_dir, data_dir, alpha2):
    layer_id = "%s_gbif_density" % (alpha2 or "nato").lower()
    out = os.path.join(out_dir, layer_id + ".tif")
    tile_dir = os.path.join(out_dir, layer_id + "_tiles")
    os.makedirs(tile_dir, exist_ok=True)

    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _pad_bbox(_transform_bounds(bounds, working_crs, "EPSG:4326"), 0.01)
    zoom = _gbif_zoom(wgs_bbox)
    tiles = _gbif_tiles(wgs_bbox, zoom)
    geotiffs = []
    for z, x, y, tbounds in tiles:
        png = _gbif_tile_png(z, x, y)
        tif = os.path.join(tile_dir, "gbif_%d_%d_%d_density.tif" % (z, x, y))
        _gbif_png_to_density_tif(png, tif, tbounds)
        geotiffs.append(tif)
    if not geotiffs:
        raise RuntimeError("no GBIF density tiles fetched")
    _warp_raster_to_grid(
        geotiffs, out, grid, bounds, working_crs,
        resample_alg="bilinear", output_type=gdal.GDT_Byte,
        src_nodata=0, dst_nodata=0,
    )
    stats = _raster_stats(out, positive=True)
    metadata = {
        "status": "ok",
        "theme": "species",
        "source": "GBIF v2 Maps API occurrence density tiles",
        "provider": "GBIF",
        "license_filter": ["CC0_1_0", "CC_BY_4_0"],
        "endpoint": "https://api.gbif.org/v2/map/occurrence/density/",
        "zoom": zoom,
        "tile_count": len(tiles),
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "raster": os.path.basename(out),
        "statistics": stats,
        "notes": (
            "Raster values are 0-255 visualization intensity derived from the "
            "licensed GBIF density PNG alpha channel, not raw occurrence counts."
        ),
        "fetched_at": _utcnow(),
    }
    _write_json(os.path.join(out_dir, layer_id + "_fetch.json"), metadata)
    return {
        "path": out,
        "layer_id": layer_id,
        "label": "GBIF observation density",
        "description": "GBIF occurrence density tile mosaic for CC0 and CC-BY observations.",
        "uses": "Biodiversity observation-density context; intensity is relative, not a raw count.",
        "value_kind": "observation density intensity",
        "value_unit": "0-255 intensity",
        "value_classification": "continuous",
        "metadata": metadata,
        "attribution": ["GBIF occurrence density tiles, filtered to CC0/CC-BY where supported."],
    }


def _hydro_continents(wgs_bbox):
    lon0, lat0, lon1, lat1 = wgs_bbox
    continents = []
    if lon0 <= -20 and lon1 >= -170 and lat1 >= 5 and lat0 <= 85:
        continents.append("na")
    if lon1 >= -30 and lon0 <= 60 and lat1 >= 30 and lat0 <= 85:
        continents.append("eu")
    return continents or ["na", "eu"]


def _hydrorivers_shp(continent):
    url = HYDRORIVERS[continent]
    return _cached_zip_shp(url, os.path.join(CACHE_DIR, "hydrosheds"))


def _hydrolakes_shp():
    return _cached_zip_shp(HYDROLAKES, os.path.join(CACHE_DIR, "hydrosheds"))


def _cached_zip_shp(url, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    filename = os.path.basename(urllib.parse.urlparse(url).path)
    zip_path = os.path.join(cache_dir, filename)
    _download_file(url, zip_path)
    extract_dir = os.path.join(cache_dir, os.path.splitext(filename)[0])
    shp = _find_shapefile(extract_dir)
    if shp:
        return shp
    os.makedirs(extract_dir, exist_ok=True)
    marker = os.path.join(extract_dir, ".unpacked")
    if not os.path.exists(marker):
        print(f"  unpack {filename}")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        _write_text(marker, _utcnow() + "\n")
    shp = _find_shapefile(extract_dir)
    if not shp:
        raise RuntimeError("no shapefile found in %s" % zip_path)
    return shp


def _find_shapefile(root):
    if not os.path.isdir(root):
        return None
    matches = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(".shp") and not name.startswith("._"):
                matches.append(os.path.join(dirpath, name))
    if not matches:
        return None
    matches.sort(key=lambda p: (0 if os.path.basename(p).lower().startswith("hydro") else 1, p))
    return matches[0]


def _clip_merge_vectors(sources, out_path, wgs_bbox):
    if _geojson_ok(out_path):
        return _feature_count(out_path)
    all_features = []
    for idx, source in enumerate(sources):
        tmp = out_path + ".part%d.geojson" % idx
        _clip_vector(source, tmp, wgs_bbox)
        if os.path.exists(tmp):
            try:
                payload = json.load(open(tmp))
                all_features.extend(payload.get("features") or [])
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    payload = {"type": "FeatureCollection", "features": all_features}
    _write_json(out_path, payload)
    return len(all_features)


def _clip_vector(source, out_path, wgs_bbox):
    if os.path.exists(out_path):
        os.remove(out_path)
    opts = gdal.VectorTranslateOptions(
        format="GeoJSON",
        dstSRS="EPSG:4326",
        spatFilter=[wgs_bbox[0], wgs_bbox[1], wgs_bbox[2], wgs_bbox[3]],
        clipSrc=[wgs_bbox[0], wgs_bbox[1], wgs_bbox[2], wgs_bbox[3]],
    )
    gdal.VectorTranslate(out_path, source, options=opts)
    if not os.path.exists(out_path):
        _write_json(out_path, {"type": "FeatureCollection", "features": []})
    return out_path


def _jrc_occurrence_urls(wgs_bbox):
    lon0, lat0, lon1, lat1 = wgs_bbox
    lon_start = int(math.floor(max(-180.0, lon0) / 10.0) * 10)
    lon_end = int(math.floor((min(179.999999, lon1)) / 10.0) * 10)
    # JRC tile names use the tile's north edge for latitude:
    # occurrence_80W_40N has a geotransform top at 40 and covers 30..40N.
    lat_start = int(math.ceil(max(-60.0, lat0) / 10.0) * 10)
    lat_end = int(math.ceil(min(89.999999, lat1) / 10.0) * 10)
    urls = []
    for lon in range(lon_start, lon_end + 1, 10):
        for lat in range(lat_start, lat_end + 1, 10):
            url = JRC_GSW.format(lon=_axis_label(lon, "E", "W"),
                                 lat=_axis_label(lat, "N", "S"))
            if _url_exists(url):
                urls.append(url)
    return urls


def _axis_label(value, positive, negative):
    return "%d%s" % (abs(int(value)), positive if value >= 0 else negative)


def _gbif_zoom(wgs_bbox):
    override = os.environ.get("VEIL_GBIF_ZOOM")
    if override:
        return int(override)
    lon_span = max(0.000001, wgs_bbox[2] - wgs_bbox[0])
    lat_span = max(0.000001, wgs_bbox[3] - wgs_bbox[1])
    span = max(lon_span, lat_span)
    if span <= 0.08:
        return 8
    if span <= 0.5:
        return 7
    if span <= 2:
        return 6
    return 5


def _gbif_tiles(wgs_bbox, zoom):
    n = 2 ** zoom
    lon0, lat0, lon1, lat1 = wgs_bbox
    x0 = _clamp(int(math.floor((lon0 + 180.0) / 360.0 * n)), 0, n - 1)
    x1 = _clamp(int(math.floor((lon1 + 180.0) / 360.0 * n)), 0, n - 1)
    y0 = _clamp(int(math.floor((90.0 - lat1) / 180.0 * n)), 0, n - 1)
    y1 = _clamp(int(math.floor((90.0 - lat0) / 180.0 * n)), 0, n - 1)
    tiles = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            west = x / n * 360.0 - 180.0
            east = (x + 1) / n * 360.0 - 180.0
            north = 90.0 - y / n * 180.0
            south = 90.0 - (y + 1) / n * 180.0
            tiles.append((zoom, x, y, (west, south, east, north)))
    while len(tiles) > 16 and zoom > 4:
        zoom -= 1
        tiles = _gbif_tiles(wgs_bbox, zoom)
    return tiles


def _gbif_tile_png(z, x, y):
    path = os.path.join(CACHE_DIR, "gbif", str(z), str(x), "%d.png" % y)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    url = GBIF_DENSITY.format(z=z, x=x, y=y)
    _download_file(url, path)
    return path


def _gbif_png_to_density_tif(png_path, tif_path, bounds):
    if _raster_ok(tif_path):
        return tif_path
    im = Image.open(png_path).convert("RGBA")
    rgba = np.array(im)
    alpha = rgba[:, :, 3].astype(np.uint8)
    # The density tiles are rendered images. The alpha channel is the most
    # stable intensity signal across styles and preserves transparent no-data.
    arr = alpha
    west, south, east, north = bounds
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(tif_path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Byte,
                       options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform((west, (east - west) / arr.shape[1], 0, north, 0,
                        -(north - south) / arr.shape[0]))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.WriteArray(arr)
    band.SetNoDataValue(0)
    ds.FlushCache()
    ds = None
    return tif_path


def _warp_raster_to_grid(src, out_path, grid, bounds, working_crs,
                         resample_alg="near", output_type=gdal.GDT_Float32,
                         src_nodata=None, dst_nodata=None):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    kwargs = {
        "dstSRS": _srs(working_crs).ExportToWkt(),
        "outputBounds": bounds,
        "width": int(grid["width"]),
        "height": int(grid["height"]),
        "resampleAlg": resample_alg,
        "outputType": output_type,
        "multithread": True,
        "creationOptions": ["COMPRESS=DEFLATE", "TILED=YES"],
    }
    if src_nodata is not None:
        kwargs["srcNodata"] = src_nodata
    if dst_nodata is not None:
        kwargs["dstNodata"] = dst_nodata
    ds = gdal.Warp(out_path, src, **kwargs)
    if ds is None:
        raise RuntimeError("gdalwarp failed for %s" % out_path)
    ds = None
    return out_path


def _scale_float_raster(src_path, out_path, scale, valid_range, nodata):
    if _raster_ok(out_path):
        return _raster_stats(out_path)
    src = gdal.Open(src_path)
    band = src.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)
    src_nodata = band.GetNoDataValue()
    valid = np.isfinite(arr)
    if src_nodata is not None and np.isfinite(src_nodata):
        valid &= arr != float(src_nodata)
    valid &= arr > -30000
    out = np.full(arr.shape, nodata, dtype=np.float32)
    out[valid] = arr[valid] * float(scale)
    if valid_range:
        lo, hi = valid_range
        good = (out >= lo) & (out <= hi)
        out[~good] = nodata
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(out_path, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32,
                       options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(src.GetGeoTransform())
    ds.SetProjection(src.GetProjection())
    out_band = ds.GetRasterBand(1)
    out_band.WriteArray(out)
    out_band.SetNoDataValue(nodata)
    ds.FlushCache()
    src = None
    ds = None
    return _raster_stats(out_path)


def _raster_stats(path, positive=False):
    ds = gdal.Open(path)
    if ds is None:
        return {"valid_px": 0}
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(float)
    nodata = band.GetNoDataValue()
    mask = np.isfinite(arr)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != float(nodata)
    valid_px = int(mask.sum())
    stats = {"valid_px": valid_px}
    if valid_px:
        vals = arr[mask]
        stats.update({
            "min": round(float(vals.min()), 4),
            "max": round(float(vals.max()), 4),
            "mean": round(float(vals.mean()), 4),
        })
        if positive:
            stats["positive_px"] = int((vals > 0).sum())
    return stats


def _download_raster(url, out_path, service_name="remote raster"):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    _download_file(url, out_path)
    if not _raster_ok(out_path):
        snippet = ""
        try:
            snippet = open(out_path, "rb").read(500).decode("utf-8", "replace")
        except OSError:
            pass
        raise RuntimeError("%s did not return a readable raster: %s" %
                           (service_name, snippet[:300]))
    return out_path


def _download_file(url, out_path, timeout=300):
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    attempts = max(1, int(os.environ.get("VEIL_FETCH_RETRIES", "4")))
    last = None
    for attempt in range(1, attempts + 1):
        try:
            print(f"  download {os.path.basename(out_path)}")
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            os.replace(tmp, out_path)
            return out_path
        except Exception as exc:  # noqa: BLE001
            last = exc
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            transient = isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))
            if isinstance(exc, urllib.error.HTTPError):
                transient = exc.code in {429, 500, 502, 503, 504}
            if attempt >= attempts or not transient:
                raise
            delay = min(30, 2 ** attempt)
            print(f"  fetch failed ({exc}); retrying in {delay}s ({attempt}/{attempts})")
            time.sleep(delay)
    raise last


def _url_exists(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def _grid(data_dir):
    return json.load(open(os.path.join(data_dir, "terrain", "grid.json")))


def _grid_bounds_abs(data_dir, grid):
    georef = os.path.join(data_dir, "georef.json")
    ox, oy = twin_georef.origin(georef)
    return (
        grid["outerMinX"] + ox,
        grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox,
        grid["outerMaxY"] + oy,
    ), twin_georef.crs(georef)


def _transform_bounds(bounds, src_crs, dst_crs):
    to_dst = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    x0, y0, x1, y1 = bounds
    pts = [to_dst.transform(x, y) for x, y in
           ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    xs, ys = zip(*pts)
    return (min(xs), min(ys), max(xs), max(ys))


def _pad_bbox(bbox, pad):
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)


def _srs(crs):
    s = osr.SpatialReference()
    s.SetFromUserInput(crs)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def _raster_ok(path):
    try:
        ds = gdal.Open(path)
        return ds is not None and ds.RasterCount > 0 and ds.RasterXSize > 0 and ds.RasterYSize > 0
    except Exception:  # noqa: BLE001
        return False


def _geojson_ok(path):
    if not os.path.exists(path):
        return False
    try:
        payload = json.load(open(path))
        return payload.get("type") == "FeatureCollection"
    except Exception:  # noqa: BLE001
        return False


def _feature_count(path):
    try:
        return len((json.load(open(path)).get("features") or []))
    except Exception:  # noqa: BLE001
        return 0


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _add_layer(layer, data_dir):
    path = layer.get("path") or layer.get("raster")
    if not path:
        return
    env = dict(os.environ, TWIN_DATA_DIR=data_dir, TWIN_PACK="nato")
    cmd = [
        sys.executable, os.path.join(SCRIPTS, "add_layer.py"), path,
        "--id", layer["layer_id"],
        "--label", layer["label"],
        "--description", layer["description"],
        "--uses", layer["uses"],
        "--value-kind", layer["value_kind"],
        "--value-unit", layer["value_unit"],
        "--value-classification", layer["value_classification"],
        "--data-dir", data_dir,
    ]
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch NATO global atlas layers for an existing twin")
    ap.add_argument("--data-dir", required=True, help="existing twin data directory")
    ap.add_argument("--out-dir", help="source output directory; defaults under data/source/nato/atlas")
    ap.add_argument("--alpha2", default="nato", help="layer id prefix, e.g. ca, nl, us")
    ap.add_argument("--theme", choices=("all", "soil", "hydrology", "species"), default="all")
    ap.add_argument("--register", action="store_true",
                    help="add fetched layers to the twin through scripts/add_layer.py")
    args = ap.parse_args(argv)

    data_dir = os.path.abspath(args.data_dir)
    out_dir = os.path.abspath(args.out_dir or os.path.join(
        data_dir, "source", "nato", "atlas_global"
    ))
    if args.theme == "soil":
        layers = fetch_soil_layers(out_dir, data_dir, alpha2=args.alpha2)
    elif args.theme == "hydrology":
        layers = fetch_hydrology_layers(out_dir, data_dir, alpha2=args.alpha2)
    elif args.theme == "species":
        layers = fetch_species_layers(out_dir, data_dir, alpha2=args.alpha2)
    else:
        layers = fetch_global_atlas_layers(None, out_dir, data_dir, alpha2=args.alpha2)
    if args.register:
        for layer in layers:
            _add_layer(layer, data_dir)
    print(json.dumps({
        "theme": args.theme,
        "data_dir": data_dir,
        "out_dir": out_dir,
        "layers": [layer["layer_id"] for layer in layers],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
