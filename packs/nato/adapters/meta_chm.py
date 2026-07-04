"""Meta/WRI 1 m canopy-height fallback for NATO global-tier twins.

The product is a modeled canopy-height surface, not a tree census and not
national LiDAR. This module keeps the data-source swap pack-side: it aligns the
Meta/WRI CHM to the existing terrain-grid outer footprint, then the existing
global canopy forest-mask logic gates it before DSM/CHM creation.
"""

import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import importlib

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

META_BUCKET = "dataforgood-fb-data"
META_PREFIX = "forests/v1/alsgedi_global_v6_float"
META_HTTP_BASE = "https://dataforgood-fb-data.s3.amazonaws.com"
META_CHM_PREFIX = META_PREFIX + "/chm"
META_INDEX_KEY = META_PREFIX + "/tiles.geojson"
META_LICENSE = "Creative Commons Attribution 4.0 International"
META_ATTRIBUTION = "Canopy height: Tolan et al. 2024 / WRI + Meta, CC-BY 4.0."
META_DSM_SOURCE = "Meta/WRI 1 m modeled CHM (predicted, MAE~2.8 m, saturates >25-30 m)"
META_MODEL_NOTE = (
    "Modeled canopy-height prediction, not measured trees or a tree census; "
    "reported MAE is about 2.8 m and tall canopy can saturate above about 25-30 m."
)
DEFAULT_RESOLUTION_M = 1.0
MAX_REASONABLE_CANOPY_M = 100.0


def prepare_meta_chm_inputs(data_dir, elevation, resolution=DEFAULT_RESOLUTION_M,
                            alpha2="nato", forest_type=None):
    """Write fine terrain/dtm.tif, terrain/dsm.tif and terrain/chm.tif.

    Returns the same shape of dict as the ETH fallback, or ``None`` when the
    Meta/WRI tiles do not cover this AOI. Network and parse failures are raised
    so the caller can log them and fall back to ETH.
    """
    del resolution
    terrain_dir = os.path.join(data_dir, "terrain")
    source_dir = os.path.dirname(elevation["terrain"])
    os.makedirs(terrain_dir, exist_ok=True)
    os.makedirs(source_dir, exist_ok=True)

    alpha = (alpha2 or "nato").lower()
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    canopy_raw = os.path.join(source_dir, "%s_meta_wri_chm_1m_grid_raw.tif" % alpha)
    canopy_masked = os.path.join(source_dir, "%s_meta_wri_chm_1m_forest_masked_grid.tif" % alpha)

    canopy = fetch_meta_chm(
        None,
        terrain_dir,
        grid,
        data_dir=data_dir,
        out_dir=source_dir,
        out_path=canopy_raw,
        alpha2=alpha,
        resolution=DEFAULT_RESOLUTION_M,
    )
    if not canopy:
        return None

    dtm_out = os.path.join(terrain_dir, "dtm.tif")
    dsm_out = os.path.join(terrain_dir, "dsm.tif")
    chm_out = os.path.join(terrain_dir, "chm.tif")
    _warp_dtm_to_template(elevation["terrain"], dtm_out, canopy_raw)

    global_sources = _global_sources()
    mask_meta = global_sources._forest_mask_canopy(  # noqa: SLF001
        data_dir,
        source_dir,
        canopy_raw,
        canopy_masked,
        forest_type=forest_type,
        alpha2=alpha,
    )
    global_sources._write_dsm_and_chm(dtm_out, canopy_masked, dsm_out, chm_out)  # noqa: SLF001

    px = _pixel_size(canopy_raw)
    status = {
        "status": "ok",
        "source": (
            "Copernicus GLO-30 DSM terrain plus forest-masked Meta/WRI 1 m "
            "modeled canopy height"
        ),
        "dsm_source": META_DSM_SOURCE,
        "dsm_source_note": META_MODEL_NOTE,
        "dsm": "terrain/dsm.tif",
        "dtm": "terrain/dtm.tif",
        "chm": "terrain/chm.tif",
        "canopy_raster": os.path.relpath(canopy_masked, data_dir),
        "raw_canopy_raster": os.path.relpath(canopy_raw, data_dir),
        "contract": (
            "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
            "global fallback writes DSM = GLO-30 + forest-masked Meta/WRI modeled CHM, "
            "DTM = GLO-30, both at the fine CHM raster shape over the exact grid outer footprint"
        ),
        "resolution_m": float(px),
        "canopy": canopy["metadata"],
        "canopy_forest_mask": mask_meta,
        "license": META_LICENSE,
        "attribution": [META_ATTRIBUTION],
    }
    for name in ("meta_chm_inputs.json", "global_chm_inputs.json"):
        json.dump(status, open(os.path.join(terrain_dir, name), "w"), indent=2)
    return {
        "dtm": dtm_out,
        "dsm": dsm_out,
        "chm": chm_out,
        "canopy": canopy_masked,
        "raw_canopy": canopy_raw,
        "layer_id": "%s_meta_chm" % alpha,
        "layer_label": "Meta Canopy Height (1 m)",
        "layer_description": (
            "Forest-masked WRI + Meta global canopy-height model at about 1 m. "
            "This is a predicted canopy surface, not measured tree inventory."
        ),
        "metadata": status,
        "attribution": [META_ATTRIBUTION],
    }


def fetch_meta_chm(aoi, terrain_dir, grid, data_dir=None, out_dir=None, out_path=None,
                   alpha2="nato", resolution=DEFAULT_RESOLUTION_M):
    """Fetch, mosaic and warp Meta/WRI CHM tiles to the grid outer footprint.

    ``aoi`` is accepted for adapter-contract symmetry, but the exact footprint
    is read from ``grid`` plus ``data_dir/georef.json``.
    """
    del aoi, terrain_dir
    if not data_dir:
        raise ValueError("fetch_meta_chm requires data_dir so georef.json can be read")
    if out_dir is None:
        out_dir = os.path.dirname(out_path) if out_path else os.path.join(data_dir, "source", "nato")
    os.makedirs(out_dir, exist_ok=True)

    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _transform_bounds(bounds, working_crs, "EPSG:4326")
    tiles = _tiles_for_bbox(wgs_bbox)
    meta_path = os.path.join(out_dir, "%s_meta_wri_chm_fetch.json" % (alpha2 or "nato").lower())
    if not tiles:
        metadata = {
            "status": "no_coverage",
            "source": "WRI + Meta global 1 m canopy height",
            "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
            "tiles": [],
            "fetched_at": _utcnow(),
        }
        json.dump(metadata, open(meta_path, "w"), indent=2)
        return None

    tile_paths = [_download_chm_tile(tile) for tile in tiles]
    width, height = _fine_shape(bounds, float(resolution))
    out_path = out_path or os.path.join(
        out_dir,
        "%s_meta_wri_chm_1m_grid_raw.tif" % (alpha2 or "nato").lower(),
    )
    if not _raster_matches(out_path, width, height, bounds, working_crs):
        if os.path.exists(out_path):
            os.remove(out_path)
        vrt = os.path.join(out_dir, "%s_meta_wri_chm_tiles.vrt" % (alpha2 or "nato").lower())
        if os.path.exists(vrt):
            os.remove(vrt)
        gdal.BuildVRT(vrt, tile_paths)
        gdal.Warp(
            out_path,
            vrt,
            dstSRS=_srs(working_crs).ExportToWkt(),
            outputBounds=bounds,
            width=width,
            height=height,
            resampleAlg="bilinear",
            outputType=gdal.GDT_Float32,
            dstNodata=-99999,
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
        )
        _sanitize_canopy(out_path)

    stats = _canopy_stats(out_path)
    metadata = {
        "status": "ok",
        "source": "WRI + Meta global 1 m canopy height, Tolan et al. 2024",
        "dsm_source": META_DSM_SOURCE,
        "dsm_source_note": META_MODEL_NOTE,
        "bucket": "s3://%s/%s" % (META_BUCKET, META_PREFIX),
        "http_base": META_HTTP_BASE,
        "index": "s3://%s/%s" % (META_BUCKET, META_INDEX_KEY),
        "tile_pattern": "forests/v1/alsgedi_global_v6_float/chm/<tileid>.tif",
        "tiles": tiles,
        "tile_paths": [os.path.relpath(p, PROJECT) for p in tile_paths],
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "grid_crs": working_crs,
        "grid_outer_bounds": [round(float(v), 3) for v in bounds],
        "shape": {"width": int(width), "height": int(height)},
        "resolution_m": float(_pixel_size(out_path)),
        "raster": os.path.basename(out_path),
        "stats": stats,
        "license": META_LICENSE,
        "attribution": [META_ATTRIBUTION],
        "fetched_at": _utcnow(),
    }
    json.dump(metadata, open(meta_path, "w"), indent=2)
    return {"path": out_path, "metadata": metadata}


def _global_sources():
    return importlib.import_module("adapters.global")


def _cache_root():
    return os.environ.get("VEIL_META_CHM_CACHE") or os.path.join(PACK_DIR, "cache", "meta_chm")


def _download_chm_tile(tile):
    safe = str(tile).strip()
    if not safe or any(ch not in "0123456789" for ch in safe):
        raise ValueError("unexpected Meta CHM tile id %r" % tile)
    key = "%s/%s.tif" % (META_CHM_PREFIX, safe)
    out = os.path.join(_cache_root(), "chm", "%s.tif" % safe)
    return _download_s3(key, out)


def _tile_index_path():
    return _download_s3(META_INDEX_KEY, os.path.join(_cache_root(), "tiles.geojson"))


def _download_s3(key, out_path):
    if _raster_or_file_ok(out_path):
        return out_path
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    s3_uri = "s3://%s/%s" % (META_BUCKET, key)
    cmd = ["aws", "s3", "cp", s3_uri, tmp, "--no-sign-request", "--only-show-errors"]
    try:
        subprocess.run(cmd, check=True)
    except Exception:  # noqa: BLE001
        if os.path.exists(tmp):
            os.remove(tmp)
        url = "%s/%s" % (META_HTTP_BASE, key)
        req = urllib.request.Request(url, headers={"User-Agent": "veil/1.0 (+packs/nato Meta CHM)"})
        with urllib.request.urlopen(req, timeout=240) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    os.replace(tmp, out_path)
    if not _raster_or_file_ok(out_path):
        raise RuntimeError("downloaded Meta CHM object is not readable: %s" % out_path)
    return out_path


def _raster_or_file_ok(path):
    if not os.path.exists(path) or os.path.getsize(path) <= 1024:
        return False
    if path.lower().endswith((".tif", ".tiff")):
        try:
            ds = gdal.Open(path)
            return ds is not None and ds.RasterXSize > 0 and ds.RasterYSize > 0
        except Exception:  # noqa: BLE001
            return False
    return True


def _tiles_for_bbox(wgs_bbox):
    with open(_tile_index_path()) as fh:
        index = json.load(fh)
    hits = []
    for feature in index.get("features", []):
        fb = _feature_bounds(feature)
        if fb and _intersects(fb, wgs_bbox):
            tile = (feature.get("properties") or {}).get("tile")
            if tile:
                hits.append(str(tile))
    return sorted(set(hits))


def _feature_bounds(feature):
    coords = (feature.get("geometry") or {}).get("coordinates")
    if coords is None:
        return None
    pts = []

    def collect(obj):
        if isinstance(obj, (list, tuple)) and len(obj) >= 2 and _is_num(obj[0]) and _is_num(obj[1]):
            pts.append((float(obj[0]), float(obj[1])))
            return
        if isinstance(obj, (list, tuple)):
            for child in obj:
                collect(child)

    collect(coords)
    if not pts:
        return None
    xs, ys = zip(*pts)
    return (min(xs), min(ys), max(xs), max(ys))


def _is_num(value):
    return isinstance(value, (int, float)) and np.isfinite(value)


def _intersects(a, b):
    return a[2] >= b[0] and a[0] <= b[2] and a[3] >= b[1] and a[1] <= b[3]


def _grid(data_dir):
    return json.load(open(os.path.join(data_dir, "terrain", "grid.json")))


def _grid_bounds_abs(data_dir, grid):
    georef = os.path.join(data_dir, "georef.json")
    ox, oy = twin_georef.origin(georef)
    return (
        float(grid["outerMinX"]) + ox,
        float(grid["outerMinY"]) + oy,
        float(grid["outerMaxX"]) + ox,
        float(grid["outerMaxY"]) + oy,
    ), twin_georef.crs(georef)


def _transform_bounds(bounds, src_crs, dst_crs):
    to_dst = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    x0, y0, x1, y1 = bounds
    pts = [to_dst.transform(x, y) for x, y in
           ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    xs, ys = zip(*pts)
    return (min(xs), min(ys), max(xs), max(ys))


def _srs(crs):
    s = osr.SpatialReference()
    s.SetFromUserInput(crs)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def _fine_shape(bounds, resolution):
    width_m = float(bounds[2] - bounds[0])
    height_m = float(bounds[3] - bounds[1])
    res = max(0.25, float(resolution or DEFAULT_RESOLUTION_M))
    width = max(1, int(round(width_m / res)))
    height = max(1, int(round(height_m / res)))
    return width, height


def _raster_matches(path, width, height, bounds, crs):
    if not os.path.exists(path):
        return False
    try:
        ds = gdal.Open(path)
        if ds is None or ds.RasterXSize != int(width) or ds.RasterYSize != int(height):
            return False
        if not _same_projection(ds.GetProjection(), crs):
            return False
        got = _dataset_bounds(ds)
        return all(abs(float(a) - float(b)) <= 0.05 for a, b in zip(got, bounds))
    except Exception:  # noqa: BLE001
        return False


def _same_projection(wkt, crs):
    src = osr.SpatialReference()
    src.ImportFromWkt(wkt)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return bool(src.IsSame(_srs(crs)))


def _dataset_bounds(ds):
    gt = ds.GetGeoTransform()
    pts = []
    for px, py in ((0, 0), (ds.RasterXSize, 0), (ds.RasterXSize, ds.RasterYSize),
                   (0, ds.RasterYSize)):
        pts.append((gt[0] + px * gt[1] + py * gt[2],
                    gt[3] + px * gt[4] + py * gt[5]))
    xs, ys = zip(*pts)
    return (min(xs), min(ys), max(xs), max(ys))


def _sanitize_canopy(path):
    ds = gdal.Open(path, gdal.GA_Update)
    if ds is None:
        raise RuntimeError("cannot read Meta CHM output %r" % path)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)
    nodata = band.GetNoDataValue()
    bad = ~np.isfinite(arr) | (arr < 0.0) | (arr > MAX_REASONABLE_CANOPY_M)
    if nodata is not None and np.isfinite(nodata):
        bad |= arr == float(nodata)
    arr[bad] = 0.0
    band.WriteArray(arr)
    band.SetNoDataValue(-99999)
    ds.FlushCache()
    ds = None


def _warp_dtm_to_template(src_path, out_path, template_path):
    template = gdal.Open(template_path)
    if template is None:
        raise RuntimeError("cannot align DTM to Meta CHM template %r" % template_path)
    if _raster_matches(
        out_path,
        template.RasterXSize,
        template.RasterYSize,
        _dataset_bounds(template),
        template.GetProjection(),
    ):
        print("  reuse %s" % os.path.basename(out_path))
        return out_path
    if os.path.exists(out_path):
        os.remove(out_path)
    gdal.Warp(
        out_path,
        src_path,
        dstSRS=template.GetProjection(),
        outputBounds=_dataset_bounds(template),
        width=template.RasterXSize,
        height=template.RasterYSize,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Float32,
        dstNodata=-99999,
        multithread=True,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
    )
    return out_path


def _pixel_size(path):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    return round(float((abs(gt[1]) + abs(gt[5])) / 2.0), 3)


def _canopy_stats(path):
    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(float)
    nodata = band.GetNoDataValue()
    mask = np.isfinite(arr)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != nodata
    vals = arr[mask]
    if vals.size == 0:
        return {}
    return {
        "mean": round(float(vals.mean()), 2),
        "p90": round(float(np.percentile(vals, 90)), 2),
        "max": round(float(vals.max()), 2),
        "canopy_cover_gt5_pct": round(100.0 * float((vals > 5.0).mean()), 1),
    }


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
