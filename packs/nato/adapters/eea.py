"""Shared EEA forest leaf-type fetcher for the NATO pack.

Copernicus HRL Dominant Leaf Type (DLT) is a 10 m categorical product over
Europe. The public EEA ArcGIS ImageServer exposes raw values:

  0 = all non-tree covered areas
  1 = broadleaved trees
  2 = coniferous trees
  255 = outside area / nodata

The fetcher exports the AOI from the ImageServer in EPSG:3035, then warps it to
the twin's own projected grid footprint with nearest-neighbor resampling. The
output raster is deliberately tiny and categorical; scripts/add_layer.py turns
it into atlas/local/<iso>_leaf_type.grid.json for vegetation.py.
"""

import json
import math
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
from osgeo import gdal, osr
from pyproj import Transformer

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_DIR = os.path.dirname(HERE)
PROJECT = os.path.dirname(os.path.dirname(PACK_DIR))
SCRIPTS = os.path.join(PROJECT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import twin_georef  # noqa: E402

gdal.UseExceptions()

DLT_SERVICE = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/"
    "GioLandPublic/HRL_DominantLeafType2018/ImageServer"
)
DLT_EXPORT = DLT_SERVICE + "/exportImage"
DLT_CRS = "EPSG:3035"
DLT_RESOLUTION_M = 10
CLCPLUS_SERVICE = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/"
    "CLC_plus/CLMS_CLCplus_RASTER_2021_010m_eu/ImageServer"
)
CLCPLUS_EXPORT = CLCPLUS_SERVICE + "/exportImage"
CLCPLUS_CRS = "EPSG:3035"
CLCPLUS_RESOLUTION_M = 10
NATURA2000_QUERY = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/"
    "Natura2000/N2K_2018/MapServer/0/query"
)
NATURA2000_CRS = "EPSG:3857"

# CLMS/EEA HRLs cover the EEA/EU cooperating European domain. For NATO routing
# we treat European NATO members, including Turkiye and Iceland, as EEA-HRL
# candidates; USA is handled by packs/us-national and Canada uses the global
# fallback.
EEA_DLT_ALPHA2 = {
    "AL", "BE", "BG", "HR", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IS", "IT", "LV", "LT", "LU", "ME", "MK", "NL",
    "NO", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "TR", "GB",
}

LEGEND = {
    0: "No tree cover",
    1: "Broadleaf",
    2: "Conifer",
    255: "Outside DLT coverage",
}


def is_eea_covered(alpha2):
    return (alpha2 or "").upper() in EEA_DLT_ALPHA2


def fetch_leaf_type(aoi, out_dir, data_dir, alpha2="nato"):
    """Fetch and grid-align Copernicus HRL DLT for a built twin.

    ``aoi`` is accepted for metadata symmetry with country adapters. The actual
    request footprint is the terrain grid outer bounds from ``data_dir`` so the
    produced raster aligns exactly with vegetation sampling.
    """
    del aoi
    os.makedirs(out_dir, exist_ok=True)
    layer_id = "%s_leaf_type" % (alpha2 or "nato").lower()
    raw = os.path.join(out_dir, layer_id + "_eea_dlt_2018_3035.tif")
    aligned = os.path.join(out_dir, layer_id + "_eea_dlt_2018_grid.tif")

    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    bbox_3035 = _transform_bounds(bounds, working_crs, DLT_CRS)
    width = max(2, int(math.ceil((bbox_3035[2] - bbox_3035[0]) / DLT_RESOLUTION_M)))
    height = max(2, int(math.ceil((bbox_3035[3] - bbox_3035[1]) / DLT_RESOLUTION_M)))

    _export_dlt(bbox_3035, width, height, raw)
    _warp_to_grid(raw, aligned, grid, bounds, working_crs)
    stats = _normalize_dlt(aligned)

    metadata = {
        "status": "ok",
        "source": "Copernicus HRL Dominant Leaf Type 2018, 10 m",
        "provider": "European Environment Agency / Copernicus Land Monitoring Service",
        "service": DLT_SERVICE,
        "endpoint": DLT_EXPORT,
        "source_crs": DLT_CRS,
        "grid_crs": working_crs,
        "resolution_m": DLT_RESOLUTION_M,
        "bbox_3035": [round(v, 3) for v in bbox_3035],
        "raw": os.path.basename(raw),
        "raster": os.path.basename(aligned),
        "classes": {str(k): v for k, v in LEGEND.items()},
        "counts": stats,
        "license": "Copernicus Land Monitoring Service / EEA public HRL service",
        "fetched_at": _utcnow(),
    }
    json.dump(metadata, open(os.path.join(out_dir, layer_id + "_eea_dlt_fetch.json"), "w"),
              indent=2)
    return {
        "raster": aligned,
        "raw": raw,
        "layer_id": layer_id,
        "label": "Copernicus HRL Dominant Leaf Type",
        "description": (
            "Copernicus HRL Dominant Leaf Type 2018, 10 m. "
            "Classes: 1 broadleaf, 2 conifer, 0 no tree cover."
        ),
        "uses": "Default NATO vegetation typing and forest/non-forest QA.",
        "value_kind": "dominant leaf type class",
        "value_unit": "class",
        "value_classification": "categorical",
        "metadata": metadata,
        "attribution": [
            "Copernicus HRL Dominant Leaf Type 2018: European Environment Agency / Copernicus Land Monitoring Service."
        ],
    }


def fetch_continental_layers(aoi, out_dir, data_dir, alpha2="nato"):
    """Fetch optional EEA/CLMS context layers for the built terrain footprint."""
    del aoi
    os.makedirs(out_dir, exist_ok=True)
    layers = []
    for fetcher in (_fetch_clcplus_landcover, _fetch_natura2000):
        try:
            layer = fetcher(out_dir, data_dir, alpha2=alpha2)
            if layer:
                layers.append(layer)
        except Exception as exc:  # noqa: BLE001
            print(f"  optional EEA layer skipped: {exc}")
    return layers


def _fetch_clcplus_landcover(out_dir, data_dir, alpha2="nato"):
    layer_id = "%s_clcplus_landcover" % (alpha2 or "nato").lower()
    raw = os.path.join(out_dir, layer_id + "_2021_3035.tif")
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    bbox_3035 = _transform_bounds(bounds, working_crs, CLCPLUS_CRS)
    width = max(2, int(math.ceil((bbox_3035[2] - bbox_3035[0]) / CLCPLUS_RESOLUTION_M)))
    height = max(2, int(math.ceil((bbox_3035[3] - bbox_3035[1]) / CLCPLUS_RESOLUTION_M)))
    _export_image(
        CLCPLUS_EXPORT,
        bbox_3035,
        width,
        height,
        raw,
        bbox_sr=3035,
        image_sr=3035,
        pixel_type="U8",
        nodata=0,
    )
    metadata = {
        "status": "ok",
        "source": "CLC+ Backbone raster 2021, 10 m",
        "provider": "European Environment Agency / Copernicus Land Monitoring Service",
        "service": CLCPLUS_SERVICE,
        "endpoint": CLCPLUS_EXPORT,
        "source_crs": CLCPLUS_CRS,
        "bbox_3035": [round(v, 3) for v in bbox_3035],
        "raw": os.path.basename(raw),
        "resolution_m": CLCPLUS_RESOLUTION_M,
        "classes": {
            "1": "sealed",
            "2": "woody needle-leaved trees",
            "3": "woody broadleaved deciduous trees",
            "4": "woody broadleaved evergreen trees",
            "5": "low-growing woody plants",
            "6": "permanent herbaceous",
            "7": "periodically herbaceous",
            "8": "lichens and mosses",
            "9": "non- and sparsely-vegetated",
            "10": "water",
            "11": "snow and ice",
        },
        "license": "Copernicus Land Monitoring Service / EEA public service",
        "fetched_at": _utcnow(),
    }
    json.dump(metadata, open(os.path.join(out_dir, layer_id + "_fetch.json"), "w"),
              indent=2)
    return {
        "path": raw,
        "layer_id": layer_id,
        "label": "CLC+ Land Cover",
        "description": "Copernicus CLC+ Backbone raster 2021, 10 m land-cover classes.",
        "uses": "Continental land-cover context for EEA/CLMS-domain NATO AOIs.",
        "value_kind": "land-cover class",
        "value_unit": "class",
        "value_classification": "categorical",
        "metadata": metadata,
        "attribution": [
            "CLC+ Backbone 2021: European Environment Agency / Copernicus Land Monitoring Service."
        ],
    }


def _fetch_natura2000(out_dir, data_dir, alpha2="nato"):
    layer_id = "%s_natura2000" % (alpha2 or "nato").lower()
    out = os.path.join(out_dir, layer_id + "_2018.geojson")
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    bbox_3857 = _transform_bounds(bounds, working_crs, NATURA2000_CRS)
    params = [
        ("f", "geojson"),
        ("where", "1=1"),
        ("geometry", "%.3f,%.3f,%.3f,%.3f" % bbox_3857),
        ("geometryType", "esriGeometryEnvelope"),
        ("inSR", "3857"),
        ("spatialRel", "esriSpatialRelIntersects"),
        ("outFields", "*"),
        ("returnGeometry", "true"),
        ("outSR", "4326"),
    ]
    url = NATURA2000_QUERY + "?" + urllib.parse.urlencode(params, safe=",=*")
    payload = _read_json(url)
    if payload.get("error"):
        raise RuntimeError("Natura 2000 query returned %r" % payload["error"])
    payload.setdefault("type", "FeatureCollection")
    payload.setdefault("features", [])
    json.dump(payload, open(out, "w"), indent=2)
    metadata = {
        "status": "ok",
        "source": "Natura 2000 / N2K 2018 vector service",
        "provider": "European Environment Agency",
        "endpoint": NATURA2000_QUERY,
        "bbox_3857": [round(v, 3) for v in bbox_3857],
        "feature_count": len(payload.get("features", [])),
        "geojson": os.path.basename(out),
        "fetched_at": _utcnow(),
    }
    json.dump(metadata, open(os.path.join(out_dir, layer_id + "_fetch.json"), "w"),
              indent=2)
    return {
        "path": out,
        "layer_id": layer_id,
        "label": "Natura 2000",
        "description": "Natura 2000/N2K 2018 protected-site context from the EEA service.",
        "uses": "Protected-area context for EEA/CLMS-domain NATO AOIs.",
        "value_kind": "protected site polygon",
        "value_unit": "feature",
        "value_classification": "categorical",
        "metadata": metadata,
        "attribution": [
            "Natura 2000/N2K 2018: European Environment Agency."
        ],
    }


def _export_image(endpoint, bbox, width, height, out_path, bbox_sr, image_sr,
                  pixel_type="U8", nodata=255):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    params = [
        ("f", "json"),
        ("bbox", "%.3f,%.3f,%.3f,%.3f" % bbox),
        ("bboxSR", str(bbox_sr)),
        ("imageSR", str(image_sr)),
        ("size", "%d,%d" % (width, height)),
        ("format", "tiff"),
        ("pixelType", pixel_type),
        ("noData", str(nodata)),
        ("interpolation", "RSP_NearestNeighbor"),
    ]
    url = endpoint + "?" + urllib.parse.urlencode(params, safe=",")
    payload = _read_json(url)
    href = payload.get("href")
    if not href:
        raise RuntimeError("%s exportImage did not return a GeoTIFF href: %r" %
                           (endpoint, payload))
    _download(href, out_path)
    if not _raster_ok(out_path):
        raise RuntimeError("%s is not a readable EEA raster" % out_path)
    return out_path


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


def _export_dlt(bbox, width, height, out_path):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    params = [
        ("f", "json"),
        ("bbox", "%.3f,%.3f,%.3f,%.3f" % bbox),
        ("bboxSR", "3035"),
        ("imageSR", "3035"),
        ("size", "%d,%d" % (width, height)),
        ("format", "tiff"),
        ("pixelType", "U8"),
        ("noData", "255"),
        ("interpolation", "RSP_NearestNeighbor"),
    ]
    url = DLT_EXPORT + "?" + urllib.parse.urlencode(params, safe=",")
    payload = _read_json(url)
    href = payload.get("href")
    if not href:
        raise RuntimeError("EEA DLT exportImage did not return a GeoTIFF href: %r" % payload)
    _download(href, out_path)
    if not _raster_ok(out_path):
        raise RuntimeError("%s is not a readable EEA DLT raster" % out_path)
    return out_path


def _warp_to_grid(src_path, out_path, grid, bounds, working_crs):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    srs = _srs(working_crs)
    gdal.Warp(
        out_path,
        src_path,
        dstSRS=srs.ExportToWkt(),
        outputBounds=bounds,
        width=int(grid["width"]),
        height=int(grid["height"]),
        resampleAlg="near",
        outputType=gdal.GDT_Byte,
        srcNodata=255,
        dstNodata=255,
        creationOptions=["COMPRESS=DEFLATE"],
    )
    return out_path


def _normalize_dlt(path):
    ds = gdal.Open(path, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.uint8)
    arr[~np.isin(arr, [0, 1, 2, 255])] = 0
    band.WriteArray(arr)
    band.SetNoDataValue(255)
    ds.FlushCache()
    ds = None
    return {str(code): int((arr == code).sum()) for code in (0, 1, 2, 255)}


def _srs(crs):
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _read_json(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "veil/1.0 (+packs/nato EEA DLT)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _download(url, out_path, timeout=240):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "veil/1.0 (+packs/nato EEA DLT)"})
    attempts = max(1, int(os.environ.get("VEIL_FETCH_RETRIES", "4")))
    last = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(out_path, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            return out_path
        except Exception as exc:  # noqa: BLE001
            last = exc
            transient = isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))
            if isinstance(exc, urllib.error.HTTPError):
                transient = exc.code in {429, 500, 502, 503, 504}
            if attempt >= attempts or not transient:
                raise
            delay = min(30, 2 ** attempt)
            print(f"  EEA DLT fetch failed ({exc}); retrying in {delay}s ({attempt}/{attempts})")
            time.sleep(delay)
    raise last


def _raster_ok(path):
    try:
        ds = gdal.Open(path)
        return ds is not None and ds.RasterCount > 0 and ds.RasterXSize > 0 and ds.RasterYSize > 0
    except Exception:  # noqa: BLE001
        return False


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
