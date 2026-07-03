"""Live-fetch national US base layers (terrain + imagery) for an AOI.

These are the CONUS-wide products every US twin can pull on demand, so nothing
big ships in the repo. Each is a USGS ArcGIS ImageServer exportImage call:

  * 3DEP elevation  (1 m where flown, else 1/3 arc-sec) -> a DEM GeoTIFF
  * NAIP Plus ortho (~0.6-1 m, RGB+NIR where available) -> an aerial GeoTIFF

LANDFIRE EVT (vegetation/land-cover) is fetched by the us-national pack
(packs/us-national/fetch_landfire.py). gSSURGO / NLCD / GAP follow the same
exportImage pattern and can be added here as more national sources.
"""

import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEP_ELEV = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
            "3DEPElevation/ImageServer/exportImage")
NAIP_PLUS = ("https://imagery.nationalmap.gov/arcgis/rest/services/"
             "USGSNAIPPlus/ImageServer/exportImage")
UA = {"User-Agent": "veil/1.0"}
RETRY_HTTP_STATUS = {429, 500, 502, 503, 504}


class ExportImageError(RuntimeError):
    pass


def _retry_count():
    raw = os.environ.get("VEIL_FETCH_RETRIES", "5")
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _retry_delay(attempt):
    return min(30.0, 2.0 * (2 ** max(0, attempt - 1)))


def _transient_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRY_HTTP_STATUS
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _error_summary(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    reason = getattr(exc, "reason", None)
    return str(reason or exc)


def _export(service, bbox, sr, w, h, out_path, pixel_type="F32",
            fmt="tiff", interpolation="RSP_BilinearInterpolation"):
    params = {
        "bbox": "%f,%f,%f,%f" % tuple(bbox),
        "bboxSR": str(sr), "imageSR": str(sr), "size": "%d,%d" % (w, h),
        "format": fmt, "pixelType": pixel_type,
        "interpolation": interpolation, "f": "image",
    }
    url = service + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    last_err = None
    attempts = _retry_count()
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt >= attempts or not _transient_error(exc):
                raise ExportImageError(
                    f"exportImage failed after {attempt} attempt(s): {_error_summary(exc)}"
                ) from exc
            delay = _retry_delay(attempt)
            print(
                f"  exportImage failed ({_error_summary(exc)}); retrying in {delay:.0f}s "
                f"({attempt}/{attempts})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    else:
        raise ExportImageError(f"exportImage failed: {_error_summary(last_err)}") from last_err
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return out_path


def fetch_3dep_dem(bbox, sr, out_path, resolution_m=1.0):
    """Fetch a 3DEP DEM covering bbox (in CRS `sr`) at ~resolution_m."""
    start_res = resolution_m
    last_err = None
    for scale in (1.0, 1.5, 2.5, 4.0):
        resolution_m = start_res * scale
        w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
        h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
        # cap request size so huge AOIs don't ask for gigapixel rasters
        while w * h > 4_000_000:
            resolution_m *= 1.5
            w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
            h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
        try:
            return _export(DEP_ELEV, bbox, sr, w, h, out_path, pixel_type="F32"), resolution_m
        except ExportImageError as exc:
            last_err = exc
            if scale == 4.0:
                break
            print(
                f"  3DEP elevation export still failing at {w}x{h}; trying a coarser request",
                file=sys.stderr,
                flush=True,
            )
    raise last_err


def fetch_naip(bbox, sr, out_path, resolution_m=1.0):
    """Fetch USGS NAIP Plus orthoimagery covering bbox at ~resolution_m.

    The service is NAIP plus high-resolution orthoimagery (HRO) in gaps. Where
    source imagery has NIR, ingest_imagery.py uses it for false color and NDVI.
    """
    w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
    h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
    while w * h > 16_000_000:
        resolution_m *= 1.5
        w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
        h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
    return _export(NAIP_PLUS, bbox, sr, w, h, out_path, pixel_type="U8",
                   interpolation="RSP_BilinearInterpolation")
