"""Norway source adapter for the NATO pack.

Implemented sources:
  * Kartverket/Geonorge Nasjonal hoydemodell WCS DTM/DOM, 1 m,
    EPSG:25832 or EPSG:25833 depending on AOI longitude
  * Sentinel-2 L2A RGB+NIR via Element84 Earth Search for imagery

The public Norge i bilder national WMS/WMTS services require Norway Digital
authorization or a token (no anonymous route), including the orthophoto/CIR
routes. This adapter therefore uses the open national height model for terrain
and canopy structure, and the existing open Sentinel-2 path for RGB+NIR.
"""

import importlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

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
from .elevation import fill_raster_nodata  # noqa: E402

gdal.UseExceptions()
global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:25832"


class NorwayAdapter:
    alpha2 = "NO"
    alpha3 = "NOR"
    name = "Norway"
    tier = "A"
    native_crs = "EPSG:25832"
    default_resolution = 1.0

    WCS_TEMPLATE = "https://wcs.geonorge.no/skwms1/wcs.hoyde-{kind}-nhm-{epsg}"
    WCS_VERSION = "1.1.2"
    WCS_FORMAT = "image/GeoTIFF"
    WCS_MAX_ROWS = 2160
    WCS_MAX_COLS = 3840

    NIB_WMS = "https://services.norgeibilder.no/wms/ortofoto"
    NIB_WMTS_UTM32 = "https://tilecache.norgeibilder.no/wmts/utm32_euref89"
    NIB_RESTRICTION = (
        "Norge i bilder national WMS/WMTS requires Norway Digital authorization "
        "or token access (no anonymous route); Sentinel-2 supplies RGB+NIR."
    )

    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2

    user_agent = "veil/1.0 (+packs/nato Norway adapter)"

    def __init__(self):
        self._data_dir = None

    def native_crs_for_aoi(self, aoi):
        bbox = self.bbox_wgs84(aoi)
        lon = (bbox[0] + bbox[2]) / 2.0
        # Kartverket publishes the NHM WCS in multiple ETRS89 / UTM zones.
        # The Oslo/Nordmarka demo is in zone 32; central/eastern Norway uses 33.
        return "EPSG:25832" if lon < 12.0 else "EPSG:25833"

    def coverage(self, aoi):
        crs = self.native_crs_for_aoi(aoi)
        bbox = self.bbox_projected(aoi, crs)
        epsg = _epsg_code(crs)
        return {
            "country": self.alpha3,
            "crs": crs,
            "bbox_native": bbox,
            "bbox_wgs84": self.bbox_wgs84(aoi),
            "area_ha": round(((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / 10000.0, 3),
            "elevation": [
                "Nasjonal hoydemodell DTM 1 m (%s)" % crs,
                "Nasjonal hoydemodell DOM 1 m (%s)" % crs,
            ],
            "elevation_coverages": [
                self.coverage_id("dtm", epsg),
                self.coverage_id("dom", epsg),
            ],
            "imagery": ["Sentinel-2 L2A RGB+NIR via Element84 Earth Search"],
            "imagery_note": self.NIB_RESTRICTION,
        }

    def bbox_wgs84(self, aoi):
        if isinstance(aoi, dict):
            bbox = aoi.get("bbox_wgs84") or aoi.get("input_bbox") or aoi.get("bbox")
            crs = aoi.get("input_crs") or aoi.get("bbox_crs") or aoi.get("crs") or "EPSG:4326"
        elif isinstance(aoi, AoiBounds):
            bbox = aoi.bbox
            crs = aoi.source_crs
        else:
            bbox = aoi
            crs = "EPSG:4326"
        if bbox is None:
            raise ValueError("AOI has no bbox")
        bbox = tuple(float(v) for v in bbox)
        if crs.upper() in ("EPSG:4326", "4326", "WGS84", "WGS 84"):
            return bbox
        return _transform_bounds(bbox, crs, "EPSG:4326")

    def bbox_projected(self, aoi, crs=None):
        target = crs or self.native_crs_for_aoi(aoi)
        if isinstance(aoi, dict):
            bbox = aoi.get("bbox_native") or aoi.get("bbox")
            src = aoi.get("native_crs") if aoi.get("bbox_native") else (
                aoi.get("crs") or aoi.get("bbox_crs") or "EPSG:4326"
            )
        elif isinstance(aoi, AoiBounds):
            bbox = aoi.bbox
            src = aoi.source_crs
        else:
            bbox = aoi
            src = target
        if bbox is None:
            raise ValueError("AOI has no bbox")
        bbox = tuple(float(v) for v in bbox)
        if src and src.upper() == target.upper():
            return bbox
        return _transform_bounds(bbox, src, target)

    def fetch_elevation(self, aoi, out_dir, resolution=1.0):
        """Fetch NHM DTM/DOM GeoTIFFs and return void-filled paths."""
        os.makedirs(out_dir, exist_ok=True)
        crs = self.native_crs_for_aoi(aoi)
        bbox = self.bbox_projected(aoi, crs)
        epsg = _epsg_code(crs)
        raw_dtm = os.path.join(out_dir, f"nhm_dtm_1m_{epsg}.tif")
        raw_dom = os.path.join(out_dir, f"nhm_dom_1m_{epsg}.tif")
        self._fetch_wcs_coverage("dtm", bbox, crs, raw_dtm)
        self._fetch_wcs_coverage("dom", bbox, crs, raw_dom)
        fill = self._fill_elevation_voids(raw_dtm, raw_dom, out_dir, epsg)
        dtm = fill["dtm"]["path"]
        dsm = fill["dsm"]["path"]
        meta = {
            "adapter": "packs/nato/adapters/no.py",
            "country": self.alpha3,
            "bbox_native": bbox,
            "crs": crs,
            "resolution_m": resolution,
            "native_resolution_m": 1.0,
            "raw_dtm": os.path.basename(raw_dtm),
            "raw_dom": os.path.basename(raw_dom),
            "dtm": os.path.basename(dtm),
            "dsm": os.path.basename(dsm),
            "source": "Kartverket Nasjonal hoydemodell WCS (DTM/DOM 1 m)",
            "dtm_endpoint": self.wcs_endpoint("dtm", epsg),
            "dom_endpoint": self.wcs_endpoint("dom", epsg),
            "dtm_coverage": self.coverage_id("dtm", epsg),
            "dom_coverage": self.coverage_id("dom", epsg),
            "nodata_fill": {
                "enabled": True,
                "reason": "NHM DTM/DOM can contain source voids under occlusion or coverage edges",
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": _json_safe_fill(fill["dtm"]),
                "dsm": _json_safe_fill(fill["dsm"]),
            },
            "license": "Kartverket / Geonorge open data, no restrictions",
            "fetched_at": _utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "nhm_elevation_fetch.json"), "w"),
                  indent=2)
        return {"dtm": dtm, "dsm": dsm, "raw_dtm": raw_dtm, "raw_dsm": raw_dom,
                "metadata": meta}

    def prepare_chm_inputs(self, data_dir, elevation, resolution=1.0):
        """Place data/terrain/dtm.tif and dsm.tif for analyze_vegetation.py."""
        self._data_dir = data_dir
        terrain_dir = os.path.join(data_dir, "terrain")
        os.makedirs(terrain_dir, exist_ok=True)
        dtm_out = os.path.join(terrain_dir, "dtm.tif")
        dsm_out = os.path.join(terrain_dir, "dsm.tif")
        chm_out = os.path.join(terrain_dir, "chm.tif")
        self._align_to_grid(elevation["dtm"], data_dir, dtm_out)
        self._align_to_grid(elevation["dsm"], data_dir, dsm_out)
        self._write_chm(dsm_out, dtm_out, chm_out)
        status = {
            "status": "ok",
            "source": "Kartverket NHM DOM 1 m - DTM 1 m",
            "dsm": "terrain/dsm.tif",
            "dtm": "terrain/dtm.tif",
            "chm": "terrain/chm.tif",
            "contract": "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif",
            "resolution_m": resolution,
        }
        json.dump(status, open(os.path.join(terrain_dir, "nhm_chm_inputs.json"), "w"),
                  indent=2)
        return {
            "dtm": dtm_out,
            "dsm": dsm_out,
            "chm": chm_out,
            "layer_id": "no_nhm_chm",
            "layer_label": "Kartverket NHM Canopy Height",
            "layer_description": "Canopy height model derived from NHM DOM minus DTM.",
            "metadata": status,
        }

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        """Fetch open RGB+NIR imagery.

        Norway's national orthophoto WMS/WMTS is not openly reachable from this
        environment. Reuse the pack's Sentinel-2 L2A RGB+NIR path so VEIL still
        receives a fourth NIR band for false_color.png and NDVI.
        """
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)
        if not data_dir:
            raise RuntimeError("Norway Sentinel-2 imagery needs a built data_dir/georef")
        result = global_sources.fetch_sentinel2_imagery(
            aoi,
            out_dir,
            data_dir,
            footprint,
            px_per_m=px_per_m,
            alpha2=self.alpha2,
        )
        raw_rgbn = result["rgbn"]
        stretched_rgbn = os.path.join(out_dir, "no_sentinel2_rgbnir_visible_stretch.tif")
        stretch_meta = _stretch_visible_rgb(raw_rgbn, stretched_rgbn)
        result["rgbn"] = stretched_rgbn
        metadata = {
            **result.get("metadata", {}),
            "adapter": "packs/nato/adapters/no.py",
            "country": self.alpha3,
            "rgbn_unstretched": os.path.basename(raw_rgbn),
            "rgbn": os.path.basename(stretched_rgbn),
            "visible_rgb_stretch": stretch_meta,
            "national_ortho_wms_checked": self.NIB_WMS,
            "national_ortho_wmts_checked": self.NIB_WMTS_UTM32,
            "national_ortho_status": "restricted/token-required; not used",
            "national_ortho_note": self.NIB_RESTRICTION,
        }
        json.dump(metadata, open(os.path.join(out_dir, "no_imagery_fetch.json"), "w"),
                  indent=2)
        result["metadata"] = metadata
        return result

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/no.py",
            "elevation": {
                "services": {
                    "dtm_25832": self.wcs_endpoint("dtm", "25832"),
                    "dom_25832": self.wcs_endpoint("dom", "25832"),
                    "dtm_25833": self.wcs_endpoint("dtm", "25833"),
                    "dom_25833": self.wcs_endpoint("dom", "25833"),
                },
                "coverages": {
                    "dtm_25832": self.coverage_id("dtm", "25832"),
                    "dom_25832": self.coverage_id("dom", "25832"),
                    "dtm_25833": self.coverage_id("dtm", "25833"),
                    "dom_25833": self.coverage_id("dom", "25833"),
                },
                "resolution_m": 1.0,
            },
            "imagery": {
                "source": "Sentinel-2 L2A via Element84 Earth Search",
                "national_ortho": self.NIB_RESTRICTION,
            },
        }

    def attribution(self):
        return [
            "Elevation: Kartverket / Geonorge Nasjonal hoydemodell DTM/DOM open data.",
            "Imagery: modified Copernicus Sentinel data via Element84 Earth Search.",
        ]

    def wcs_endpoint(self, kind, epsg):
        return self.WCS_TEMPLATE.format(kind=kind, epsg=str(epsg))

    def coverage_id(self, kind, epsg):
        model = "dtm" if kind == "dtm" else "dom"
        return f"nhm_{model}_topo_{epsg}"

    def _fetch_wcs_coverage(self, kind, bbox, crs, out_path):
        width = max(1, int(math.ceil(bbox[2] - bbox[0])))
        height = max(1, int(math.ceil(bbox[3] - bbox[1])))
        if width <= self.WCS_MAX_COLS and height <= self.WCS_MAX_ROWS:
            self._fetch_wcs_tile(kind, bbox, crs, out_path)
            _assert_raster(out_path)
            return out_path

        tile_span_x = min(self.WCS_MAX_COLS - 16, 1800)
        tile_span_y = min(self.WCS_MAX_ROWS - 16, 1800)
        tiles = []
        y = bbox[1]
        row = 0
        while y < bbox[3] - 1e-9:
            x = bbox[0]
            col = 0
            y1 = min(bbox[3], y + tile_span_y)
            while x < bbox[2] - 1e-9:
                x1 = min(bbox[2], x + tile_span_x)
                tile = f"{out_path}.tile-{row:03d}-{col:03d}.tif"
                self._fetch_wcs_tile(kind, (x, y, x1, y1), crs, tile)
                tiles.append(tile)
                x = x1
                col += 1
            y = y1
            row += 1

        vrt = out_path + ".vrt"
        gdal.BuildVRT(vrt, tiles)
        gdal.Translate(out_path, vrt, format="GTiff", creationOptions=["COMPRESS=DEFLATE"])
        for path in tiles + [vrt]:
            if os.path.exists(path):
                os.remove(path)
        _assert_raster(out_path)
        return out_path

    def _fetch_wcs_tile(self, kind, bbox, crs, out_path):
        if os.path.exists(out_path) and _raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return
        epsg = _epsg_code(crs)
        params = [
            ("service", "WCS"),
            ("version", self.WCS_VERSION),
            ("request", "GetCoverage"),
            ("identifier", self.coverage_id(kind, epsg)),
            ("boundingbox", "%.3f,%.3f,%.3f,%.3f,urn:ogc:def:crs:EPSG::%s" %
             (bbox[0], bbox[1], bbox[2], bbox[3], epsg)),
            ("format", self.WCS_FORMAT),
        ]
        url = self.wcs_endpoint(kind, epsg) + "?" + urllib.parse.urlencode(params, safe=",/:")
        _download_wcs_geotiff(url, out_path, self.user_agent, timeout=240)
        _assert_raster(out_path)

    def _fill_elevation_voids(self, raw_dtm, raw_dsm, out_dir, epsg):
        filled_dtm = os.path.join(out_dir, f"nhm_dtm_1m_{epsg}_filled.tif")
        filled_dsm = os.path.join(out_dir, f"nhm_dom_1m_{epsg}_filled.tif")
        result = {
            "dtm": fill_raster_nodata(
                raw_dtm,
                filled_dtm,
                search_distances_px=self.nodata_fill_search_distances_px,
                smoothing_iterations=self.nodata_fill_smoothing_iterations,
            ),
            "dsm": fill_raster_nodata(
                raw_dsm,
                filled_dsm,
                search_distances_px=self.nodata_fill_search_distances_px,
                smoothing_iterations=self.nodata_fill_smoothing_iterations,
            ),
        }
        for label, stats in result.items():
            before = stats["before"]
            after = stats["after"]
            action = "reuse" if stats.get("reused") else "fill"
            print(
                "  {action} NHM {label} nodata: {bc}/{bt} ({bp:.3f}%) -> "
                "{ac}/{at} ({ap:.3f}%)".format(
                    action=action,
                    label=label.upper(),
                    bc=before["nodata_count"],
                    bt=before["total_count"],
                    bp=before["nodata_pct"],
                    ac=after["nodata_count"],
                    at=after["total_count"],
                    ap=after["nodata_pct"],
                )
            )
        return result

    def _align_to_grid(self, src_path, data_dir, out_path):
        georef_path = os.path.join(data_dir, "georef.json")
        grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
        ox, oy = twin_georef.origin(georef_path)
        bounds = (
            grid["outerMinX"] + ox,
            grid["outerMinY"] + oy,
            grid["outerMaxX"] + ox,
            grid["outerMaxY"] + oy,
        )
        srs = osr.SpatialReference()
        srs.SetFromUserInput(twin_georef.crs(georef_path))
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        gdal.Warp(
            out_path,
            src_path,
            dstSRS=srs.ExportToWkt(),
            outputBounds=bounds,
            width=int(grid["width"]),
            height=int(grid["height"]),
            resampleAlg="bilinear",
            outputType=gdal.GDT_Float32,
            dstNodata=float("nan"),
        )
        _clean_float_raster(out_path)

    def _write_chm(self, dsm_path, dtm_path, out_path):
        dsm_ds = gdal.Open(dsm_path)
        dtm_ds = gdal.Open(dtm_path)
        dsm = dsm_ds.ReadAsArray().astype("float32")
        dtm = dtm_ds.ReadAsArray().astype("float32")
        chm = dsm - dtm
        chm[~np.isfinite(chm)] = np.nan
        chm[chm < 0] = 0.0
        chm[chm > 80] = np.nan
        drv = gdal.GetDriverByName("GTiff")
        out = drv.Create(out_path, dsm_ds.RasterXSize, dsm_ds.RasterYSize, 1, gdal.GDT_Float32)
        out.SetGeoTransform(dsm_ds.GetGeoTransform())
        out.SetProjection(dsm_ds.GetProjection())
        out.GetRasterBand(1).WriteArray(chm)
        out.GetRasterBand(1).SetNoDataValue(float("nan"))
        out.FlushCache()
        out = None


def _download_wcs_geotiff(url, out_path, user_agent, timeout=180):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    attempts = max(1, int(os.environ.get("VEIL_FETCH_RETRIES", "4")))
    last = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            geotiff = _extract_geotiff(body)
            with open(out_path, "wb") as fh:
                fh.write(geotiff)
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


def _extract_geotiff(body):
    if body.startswith(b"II*\x00") or body.startswith(b"MM\x00*"):
        return body
    offsets = [body.find(b"II*\x00"), body.find(b"MM\x00*")]
    offsets = [v for v in offsets if v >= 0]
    if not offsets:
        preview = body[:500].decode("utf-8", "ignore")
        raise RuntimeError("WCS did not return a GeoTIFF" + (f": {preview}" if preview else ""))
    start = min(offsets)
    end = body.find(b"\n--wcs", start)
    if end < 0:
        end = body.find(b"\r\n--wcs", start)
    if end < 0:
        end = len(body)
    return body[start:end].rstrip(b"\r\n")


def _transform_bounds(bbox, src_crs, dst_crs):
    to_dst = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    xs, ys = [], []
    for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]),
                 (bbox[2], bbox[3]), (bbox[0], bbox[3])):
        dx, dy = to_dst.transform(x, y)
        xs.append(dx)
        ys.append(dy)
    return (min(xs), min(ys), max(xs), max(ys))


def _epsg_code(crs):
    return str(crs).upper().replace("EPSG:", "")


def _raster_ok(path):
    try:
        ds = gdal.Open(path)
        return ds is not None and ds.RasterCount > 0 and ds.RasterXSize > 0 and ds.RasterYSize > 0
    except Exception:  # noqa: BLE001
        return False


def _assert_raster(path):
    if not _raster_ok(path):
        raise RuntimeError(f"{path} is not a readable raster")


def _clean_float_raster(path):
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


def _stretch_visible_rgb(src_path, out_path):
    src = gdal.Open(src_path)
    if src is None:
        raise RuntimeError(f"{src_path} is not a readable raster")
    if src.RasterCount < 4:
        raise RuntimeError("Norway imagery stretch expects R,G,B,NIR input")
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
        band = ds.GetRasterBand(idx)
        band.WriteArray(arr)
        if nodata is not None:
            band.SetNoDataValue(nodata)
    ds.FlushCache()
    ds = None
    means = [round(float(arr[valid].mean()), 2) if valid.any() else None for arr in stretched]
    return {
        "method": "per-band visible RGB p2..p98 stretch to byte range 10..245; NIR band unchanged",
        "source": os.path.basename(src_path),
        "output": os.path.basename(out_path),
        "rgb_percentiles": stats,
        "rgb_means_after": means,
    }


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_safe_fill(fill_result):
    safe = dict(fill_result)
    safe.pop("path", None)
    return safe
