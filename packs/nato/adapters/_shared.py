"""Shared helpers for NATO national adapters."""

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


def bbox_wgs84(aoi):
    if isinstance(aoi, dict):
        bbox = aoi.get("bbox_wgs84") or aoi.get("input_bbox") or aoi.get("bbox")
        crs = aoi.get("input_crs") or aoi.get("bbox_crs") or aoi.get("crs") or "EPSG:4326"
    else:
        bbox = aoi
        crs = "EPSG:4326"
    if bbox is None:
        raise ValueError("AOI has no bbox")
    bbox = tuple(float(v) for v in bbox)
    if crs.upper() in ("EPSG:4326", "4326", "WGS84", "WGS 84"):
        return bbox
    return transform_bounds(bbox, crs, "EPSG:4326")


def bbox_projected(aoi, target_crs):
    if isinstance(aoi, dict):
        bbox = aoi.get("bbox_native") or aoi.get("bbox")
        src = aoi.get("native_crs") if aoi.get("bbox_native") else (
            aoi.get("crs") or aoi.get("bbox_crs") or aoi.get("input_crs") or "EPSG:4326"
        )
    else:
        bbox = aoi
        src = target_crs
    if bbox is None:
        raise ValueError("AOI has no bbox")
    bbox = tuple(float(v) for v in bbox)
    if src and src.upper() == target_crs.upper():
        return bbox
    return transform_bounds(bbox, src, target_crs)


def transform_bounds(bbox, src_crs, dst_crs):
    to_dst = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    xs, ys = [], []
    for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]),
                 (bbox[2], bbox[3]), (bbox[0], bbox[3])):
        dx, dy = to_dst.transform(x, y)
        xs.append(dx)
        ys.append(dy)
    return (min(xs), min(ys), max(xs), max(ys))


def epsg_code(crs):
    return str(crs).upper().replace("EPSG:", "")


def srs(crs):
    out = osr.SpatialReference()
    out.SetFromUserInput(crs)
    out.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return out


def dataset_bounds(ds):
    gt = ds.GetGeoTransform()
    x0 = gt[0]
    y0 = gt[3]
    x1 = x0 + gt[1] * ds.RasterXSize + gt[2] * ds.RasterYSize
    y1 = y0 + gt[4] * ds.RasterXSize + gt[5] * ds.RasterYSize
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def raster_ok(path):
    try:
        ds = gdal.Open(path)
        return ds is not None and ds.RasterCount > 0 and ds.RasterXSize > 0 and ds.RasterYSize > 0
    except Exception:  # noqa: BLE001
        return False


def assert_raster(path):
    if not raster_ok(path):
        raise RuntimeError(f"{path} is not a readable raster")


def download(url, out_path, user_agent, timeout=180):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
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
            print(f"  fetch failed ({exc}); retrying in {delay}s ({attempt}/{attempts})")
            time.sleep(delay)
    raise last


def download_wcs_geotiff(url, out_path, user_agent, timeout=180):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    attempts = max(1, int(os.environ.get("VEIL_FETCH_RETRIES", "4")))
    last = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            with open(out_path, "wb") as fh:
                fh.write(extract_geotiff(body))
            return out_path
        except Exception as exc:  # noqa: BLE001
            last = exc
            transient = isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))
            if isinstance(exc, urllib.error.HTTPError):
                transient = exc.code in {429, 500, 502, 503, 504}
            if attempt >= attempts or not transient:
                raise
            delay = min(30, 2 ** attempt)
            print(f"  fetch failed ({exc}); retrying in {delay}s ({attempt}/{attempts})")
            time.sleep(delay)
    raise last


def extract_geotiff(body):
    if body.startswith(b"II*\x00") or body.startswith(b"MM\x00*"):
        return body
    offsets = [body.find(b"II*\x00"), body.find(b"MM\x00*")]
    offsets = [v for v in offsets if v >= 0]
    if not offsets:
        preview = body[:500].decode("utf-8", "ignore")
        raise RuntimeError("service did not return a GeoTIFF" + (f": {preview}" if preview else ""))
    start = min(offsets)
    end = body.find(b"\n--", start)
    if end < 0:
        end = body.find(b"\r\n--", start)
    if end < 0:
        end = len(body)
    return body[start:end].rstrip(b"\r\n")


def fetch_wms_map(service, layer, bbox, crs, width, height, out_path, user_agent,
                  version="1.3.0", fmt="image/png", style=""):
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    params = [
        ("service", "WMS"),
        ("version", version),
        ("request", "GetMap"),
        ("layers", layer),
        ("styles", style),
        ("crs", crs),
        ("bbox", "%.3f,%.3f,%.3f,%.3f" % bbox),
        ("width", str(int(width))),
        ("height", str(int(height))),
        ("format", fmt),
        ("transparent", "false"),
    ]
    url = service + "?" + urllib.parse.urlencode(params, safe="(),/:")
    download(url, out_path, user_agent, timeout=240)
    return out_path


def read_rgb(path):
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def write_rgbn(path, bbox, rgb, nir, crs):
    h, w = rgb.shape[:2]
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 4, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE", "TILED=YES", "INTERLEAVE=PIXEL"])
    ds.SetGeoTransform((bbox[0], (bbox[2] - bbox[0]) / w, 0.0,
                        bbox[3], 0.0, -((bbox[3] - bbox[1]) / h)))
    ds.SetProjection(srs(crs).ExportToWkt())
    ds.GetRasterBand(1).WriteArray(rgb[:, :, 0])
    ds.GetRasterBand(2).WriteArray(rgb[:, :, 1])
    ds.GetRasterBand(3).WriteArray(rgb[:, :, 2])
    ds.GetRasterBand(4).WriteArray(nir.astype(np.uint8))
    ds.FlushCache()
    ds = None


def clean_float_raster(path):
    ds = gdal.Open(path, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype("float32")
    nodata = band.GetNoDataValue()
    if nodata is not None and np.isfinite(nodata):
        arr[arr == nodata] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    arr[np.abs(arr) > 10000] = np.nan
    band.WriteArray(arr)
    band.SetNoDataValue(float("nan"))
    ds.FlushCache()
    ds = None


def force_srs(path, crs):
    ds = gdal.Open(path, gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"{path} is not a readable raster")
    ds.SetProjection(srs(crs).ExportToWkt())
    ds.FlushCache()
    ds = None


def align_to_grid(src_path, data_dir, out_path):
    georef_path = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    ox, oy = twin_georef.origin(georef_path)
    bounds = (
        grid["outerMinX"] + ox,
        grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox,
        grid["outerMaxY"] + oy,
    )
    gdal.Warp(
        out_path,
        src_path,
        dstSRS=srs(twin_georef.crs(georef_path)).ExportToWkt(),
        outputBounds=bounds,
        width=int(grid["width"]),
        height=int(grid["height"]),
        resampleAlg="bilinear",
        outputType=gdal.GDT_Float32,
        dstNodata=float("nan"),
        multithread=True,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    clean_float_raster(out_path)


def write_chm(dsm_path, dtm_path, out_path):
    dsm_ds = gdal.Open(dsm_path)
    dtm_ds = gdal.Open(dtm_path)
    dsm = dsm_ds.ReadAsArray().astype("float32")
    dtm = dtm_ds.ReadAsArray().astype("float32")
    chm = dsm - dtm
    chm[~np.isfinite(chm)] = np.nan
    chm[chm < 0] = 0.0
    chm[chm > 80] = np.nan
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(out_path, dsm_ds.RasterXSize, dsm_ds.RasterYSize, 1, gdal.GDT_Float32,
                     options=["COMPRESS=DEFLATE", "TILED=YES"])
    out.SetGeoTransform(dsm_ds.GetGeoTransform())
    out.SetProjection(dsm_ds.GetProjection())
    out.GetRasterBand(1).WriteArray(chm)
    out.GetRasterBand(1).SetNoDataValue(float("nan"))
    out.FlushCache()
    out = None


def align_sentinel_nir(sentinel_rgbn, out_dir, prefix, bbox, crs, width, height):
    nir_src = os.path.join(out_dir, f"{prefix}_sentinel2_nir_byte.tif")
    nir_aligned = os.path.join(out_dir, f"{prefix}_sentinel2_nir_to_ortho_grid.tif")
    if not raster_ok(nir_src):
        gdal.Translate(nir_src, sentinel_rgbn, format="GTiff", bandList=[4],
                       creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    if not raster_ok(nir_aligned):
        gdal.Warp(
            nir_aligned,
            nir_src,
            dstSRS=srs(crs).ExportToWkt(),
            outputBounds=bbox,
            width=int(width),
            height=int(height),
            resampleAlg="bilinear",
            outputType=gdal.GDT_Byte,
            srcNodata=255,
            dstNodata=255,
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
        )
    ds = gdal.Open(nir_aligned)
    return ds.GetRasterBand(1).ReadAsArray().astype(np.uint8)


def stretch_visible_rgb(src_path, out_path):
    src = gdal.Open(src_path)
    if src is None:
        raise RuntimeError(f"{src_path} is not a readable raster")
    if src.RasterCount < 4:
        raise RuntimeError("imagery stretch expects R,G,B,NIR input")
    arrays = [src.GetRasterBand(i).ReadAsArray().astype(np.float32) for i in range(1, 5)]
    nodata = src.GetRasterBand(1).GetNoDataValue()
    valid = np.ones(arrays[0].shape, dtype=bool)
    if nodata is not None and np.isfinite(nodata):
        for arr in arrays[:3]:
            valid &= arr != nodata
    valid &= np.all(np.isfinite(np.stack(arrays[:3], axis=0)), axis=0)

    stretched = []
    stats = []
    for arr in arrays[:3]:
        vals = arr[valid]
        if vals.size:
            lo = float(np.percentile(vals, 2))
            hi = float(np.percentile(vals, 98))
        else:
            lo, hi = 0.0, 254.0
        if hi <= lo:
            hi = lo + 1.0
        byte = np.clip((arr - lo) * (235.0 / (hi - lo)) + 10.0, 0, 254).astype(np.uint8)
        if nodata is not None and np.isfinite(nodata):
            byte[arr == nodata] = int(nodata)
        stretched.append(byte)
        stats.append({"p2": round(lo, 2), "p98": round(hi, 2)})
    nir = np.clip(arrays[3], 0, 255).astype(np.uint8)

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, src.RasterXSize, src.RasterYSize, 4, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE", "TILED=YES", "INTERLEAVE=PIXEL"])
    ds.SetGeoTransform(src.GetGeoTransform())
    ds.SetProjection(src.GetProjection())
    for idx, arr in enumerate(stretched + [nir], start=1):
        ds.GetRasterBand(idx).WriteArray(arr)
        if nodata is not None and np.isfinite(nodata):
            ds.GetRasterBand(idx).SetNoDataValue(int(nodata))
    ds.FlushCache()
    ds = None
    return {"visible_percentiles": stats}


def json_safe_fill(fill_result):
    safe = dict(fill_result)
    safe.pop("path", None)
    return safe


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def wms_size_for_bbox(bbox, px_per_m, max_size):
    width = max(2, int(round((bbox[2] - bbox[0]) * px_per_m)))
    height = max(2, int(round((bbox[3] - bbox[1]) * px_per_m)))
    if width > max_size or height > max_size:
        scale = min(max_size / float(width), max_size / float(height))
        width = max(2, int(math.floor(width * scale)))
        height = max(2, int(math.floor(height * scale)))
    return width, height
