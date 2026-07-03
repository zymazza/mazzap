"""Elevation raster utilities shared by NATO country adapters."""

import os

import numpy as np
from osgeo import gdal


FILL_NODATA_VALUE = -99999.0
INVALID_ABS_LIMIT = 10000.0


def raster_nodata_stats(path):
    """Return first-band invalid-cell counts using nodata, NaN, and bad sentinels."""
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError(f"{path} is not a readable raster")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()
    invalid = _invalid_mask(arr, nodata)
    total = int(arr.size)
    count = int(invalid.sum())
    stats = {
        "path": os.path.basename(path),
        "width": int(ds.RasterXSize),
        "height": int(ds.RasterYSize),
        "total_count": total,
        "nodata_count": count,
        "nodata_pct": round(100.0 * count / total, 6) if total else 0.0,
        "nodata_value": _json_number(nodata),
    }
    ds = None
    return stats


def fill_raster_nodata(src_path, out_path, search_distances_px=(256, 512, 1024),
                       smoothing_iterations=2, creation_options=None):
    """Fill voids in a DEM/DSM using GDAL's IDW FillNodata interpolation."""
    before = raster_nodata_stats(src_path)
    if _reusable_filled_copy(src_path, out_path):
        after = raster_nodata_stats(out_path)
        if after["nodata_count"] == 0:
            return _result(src_path, out_path, before, after, [], smoothing_iterations, reused=True)

    _write_clean_copy(src_path, out_path, creation_options=creation_options)
    attempts = []
    ds = gdal.Open(out_path, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    for distance in search_distances_px:
        arr = band.ReadAsArray().astype("float64")
        invalid = _invalid_mask(arr, FILL_NODATA_VALUE)
        remaining = int(invalid.sum())
        attempts.append({"max_search_dist_px": int(distance), "before_count": remaining})
        if remaining == 0:
            attempts[-1]["after_count"] = 0
            break

        mask_ds = gdal.GetDriverByName("MEM").Create(
            "", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte
        )
        mask_ds.GetRasterBand(1).WriteArray((~invalid).astype("uint8"))
        rc = gdal.FillNodata(
            band,
            mask_ds.GetRasterBand(1),
            float(distance),
            int(smoothing_iterations),
        )
        if rc != gdal.CE_None:
            raise RuntimeError(f"GDAL FillNodata failed for {out_path}")
        band.FlushCache()

        arr = band.ReadAsArray().astype("float64")
        attempts[-1]["after_count"] = int(_invalid_mask(arr, FILL_NODATA_VALUE).sum())
        if attempts[-1]["after_count"] == 0:
            break

    ds.FlushCache()
    ds = None
    after = raster_nodata_stats(out_path)
    if after["nodata_count"] != 0:
        raise RuntimeError(
            f"nodata fill left {after['nodata_count']} / {after['total_count']} "
            f"invalid cells in {out_path}"
        )
    return _result(src_path, out_path, before, after, attempts, smoothing_iterations, reused=False)


def _result(src_path, out_path, before, after, attempts, smoothing_iterations, reused):
    return {
        "method": "gdal.FillNodata IDW from void edges",
        "source": os.path.basename(src_path),
        "filled": os.path.basename(out_path),
        "path": out_path,
        "smoothing_iterations": int(smoothing_iterations),
        "reused": bool(reused),
        "attempts": attempts,
        "before": before,
        "after": after,
    }


def _write_clean_copy(src_path, out_path, creation_options=None):
    src = gdal.Open(src_path)
    if src is None:
        raise RuntimeError(f"{src_path} is not a readable raster")
    band = src.GetRasterBand(1)
    arr = band.ReadAsArray().astype("float32")
    arr[_invalid_mask(arr.astype("float64"), band.GetNoDataValue())] = FILL_NODATA_VALUE

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    options = creation_options or ["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(out_path, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32, options)
    out.SetGeoTransform(src.GetGeoTransform())
    out.SetProjection(src.GetProjection())
    out.GetRasterBand(1).WriteArray(arr)
    out.GetRasterBand(1).SetNoDataValue(FILL_NODATA_VALUE)
    out.FlushCache()
    out = None
    src = None


def _reusable_filled_copy(src_path, out_path):
    if not os.path.exists(out_path):
        return False
    if os.path.getmtime(out_path) < os.path.getmtime(src_path):
        return False
    try:
        return raster_nodata_stats(out_path)["nodata_count"] == 0
    except Exception:  # noqa: BLE001
        return False


def _invalid_mask(arr, nodata):
    invalid = ~np.isfinite(arr) | (np.abs(arr) > INVALID_ABS_LIMIT)
    if nodata is not None:
        if np.isfinite(nodata):
            invalid |= arr == nodata
        else:
            invalid |= ~np.isfinite(arr)
    return invalid


def _json_number(value):
    if value is None:
        return None
    if not np.isfinite(value):
        return str(value)
    return float(value)
