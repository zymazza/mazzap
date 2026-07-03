"""Netherlands source adapter for the NATO pack.

Implemented sources:
  * AHN WCS 2.0.1 DTM/DSM, 0.5 m, EPSG:28992
  * PDOK current RGB and CIR aerial orthophoto WMS, EPSG:28992

AHN DTM has canopy-shadow voids in dense forest. fetch_elevation() writes raw
rasters and filled copies, then returns the filled paths for terrain ingest and
CHM preparation.

The generic vegetation engine consumes DSM/DTM only through:
  data/terrain/dsm.tif
  data/terrain/dtm.tif

prepare_chm_inputs() warps the AHN rasters to exactly the terrain grid's outer
footprint before placing those files.
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
from dataclasses import dataclass

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
from .elevation import fill_raster_nodata  # noqa: E402

gdal.UseExceptions()


@dataclass(frozen=True)
class AoiBounds:
    bbox_28992: tuple
    source_crs: str = "EPSG:28992"


class NetherlandsAdapter:
    alpha2 = "NL"
    alpha3 = "NLD"
    name = "Netherlands"
    tier = "A"
    native_crs = "EPSG:28992"
    default_resolution = 0.5

    AHN_WCS = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
    AHN_DTM = "dtm_05m"
    AHN_DSM = "dsm_05m"

    RGB_WMS = "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0"
    CIR_WMS = "https://service.pdok.nl/hwh/luchtfotocir/wms/v1_0"
    RGB_LAYER = "Actueel_ortho25"
    CIR_LAYER = "Actueel_ortho25IR"

    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2

    user_agent = "veil/1.0 (+packs/nato Netherlands adapter)"

    def coverage(self, aoi):
        bbox = self.bbox_28992(aoi)
        return {
            "country": self.alpha3,
            "crs": self.native_crs,
            "bbox_28992": bbox,
            "area_ha": round(((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / 10000.0, 3),
            "elevation": ["AHN dtm_05m", "AHN dsm_05m"],
            "imagery": [self.RGB_LAYER, self.CIR_LAYER],
        }

    def bbox_28992(self, aoi):
        if isinstance(aoi, AoiBounds):
            return tuple(float(v) for v in aoi.bbox_28992)
        if isinstance(aoi, dict):
            bbox = aoi.get("bbox_28992") or aoi.get("bbox")
            crs = aoi.get("crs") or aoi.get("bbox_crs") or "EPSG:28992"
        else:
            bbox = aoi
            crs = "EPSG:28992"
        if bbox is None:
            raise ValueError("AOI has no bbox")
        bbox = tuple(float(v) for v in bbox)
        if crs.upper() in ("EPSG:28992", "28992"):
            return bbox
        to_rd = Transformer.from_crs(crs, self.native_crs, always_xy=True)
        xs, ys = [], []
        for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]),
                     (bbox[2], bbox[3]), (bbox[0], bbox[3])):
            rx, ry = to_rd.transform(x, y)
            xs.append(rx)
            ys.append(ry)
        return (min(xs), min(ys), max(xs), max(ys))

    def fetch_elevation(self, aoi, out_dir, resolution=0.5):
        """Fetch AHN DTM/DSM GeoTIFFs and return void-filled paths."""
        os.makedirs(out_dir, exist_ok=True)
        bbox = self.bbox_28992(aoi)
        raw_dtm = os.path.join(out_dir, "ahn_dtm_05m.tif")
        raw_dsm = os.path.join(out_dir, "ahn_dsm_05m.tif")
        self._fetch_wcs_coverage(self.AHN_DTM, bbox, raw_dtm, resolution)
        self._fetch_wcs_coverage(self.AHN_DSM, bbox, raw_dsm, resolution)
        fill = self._fill_elevation_voids(raw_dtm, raw_dsm, out_dir)
        dtm = fill["dtm"]["path"]
        dsm = fill["dsm"]["path"]
        meta = {
            "adapter": "packs/nato/adapters/nl.py",
            "country": self.alpha3,
            "bbox_28992": bbox,
            "resolution_m": resolution,
            "raw_dtm": os.path.basename(raw_dtm),
            "raw_dsm": os.path.basename(raw_dsm),
            "dtm": os.path.basename(dtm),
            "dsm": os.path.basename(dsm),
            "source": "AHN WCS 2.0.1",
            "nodata_fill": {
                "enabled": True,
                "reason": "AHN bare-earth DTM can contain forest-canopy voids",
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": _json_safe_fill(fill["dtm"]),
                "dsm": _json_safe_fill(fill["dsm"]),
            },
            "license": "CC-BY 4.0 / open data via Rijkswaterstaat and PDOK",
            "fetched_at": _utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "ahn_elevation_fetch.json"), "w"),
                  indent=2)
        return {"dtm": dtm, "dsm": dsm, "raw_dtm": raw_dtm, "raw_dsm": raw_dsm,
                "metadata": meta}

    def prepare_chm_inputs(self, data_dir, elevation, resolution=0.5):
        """Place data/terrain/dtm.tif and dsm.tif for analyze_vegetation.py."""
        terrain_dir = os.path.join(data_dir, "terrain")
        os.makedirs(terrain_dir, exist_ok=True)
        dtm_out = os.path.join(terrain_dir, "dtm.tif")
        dsm_out = os.path.join(terrain_dir, "dsm.tif")
        chm_out = os.path.join(terrain_dir, "chm.tif")
        self._align_to_grid(elevation["dtm"], data_dir, dtm_out, resolution)
        self._align_to_grid(elevation["dsm"], data_dir, dsm_out, resolution)
        self._write_chm(dsm_out, dtm_out, chm_out)
        status = {
            "status": "ok",
            "source": "AHN dsm_05m - dtm_05m",
            "dsm": "terrain/dsm.tif",
            "dtm": "terrain/dtm.tif",
            "chm": "terrain/chm.tif",
            "contract": "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif",
            "resolution_m": resolution,
        }
        json.dump(status, open(os.path.join(terrain_dir, "ahn_chm_inputs.json"), "w"),
                  indent=2)
        return {"dtm": dtm_out, "dsm": dsm_out, "chm": chm_out, "metadata": status}

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=2):
        """Fetch RGB and CIR WMS maps and assemble a VEIL RGB+NIR GeoTIFF."""
        os.makedirs(out_dir, exist_ok=True)
        bbox = tuple(float(v) for v in footprint)
        width = max(2, int(round((bbox[2] - bbox[0]) * px_per_m)))
        height = max(2, int(round((bbox[3] - bbox[1]) * px_per_m)))
        rgb_raw = os.path.join(out_dir, "pdok_rgb_ortho25.png")
        cir_raw = os.path.join(out_dir, "pdok_cir_ortho25ir.png")
        self._fetch_wms_map(self.RGB_WMS, self.RGB_LAYER, bbox, width, height, rgb_raw)
        self._fetch_wms_map(self.CIR_WMS, self.CIR_LAYER, bbox, width, height, cir_raw)

        rgb = _read_rgb(rgb_raw)
        cir = _read_rgb(cir_raw)
        if rgb.shape[:2] != cir.shape[:2]:
            cir = np.asarray(Image.fromarray(cir).resize((rgb.shape[1], rgb.shape[0])))
        rgbn = os.path.join(out_dir, "pdok_rgbn_ortho25.tif")
        _write_rgbn(rgbn, bbox, rgb, cir[:, :, 0], self.native_crs)
        meta = {
            "adapter": "packs/nato/adapters/nl.py",
            "country": self.alpha3,
            "bbox_28992": bbox,
            "width": width,
            "height": height,
            "px_per_m": px_per_m,
            "rgb_layer": self.RGB_LAYER,
            "cir_layer": self.CIR_LAYER,
            "band_order": "R,G,B,NIR (NIR copied from CIR band 1; PDOK CIR is NIR,Red,Green)",
            "fetched_at": _utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "pdok_imagery_fetch.json"), "w"),
                  indent=2)
        return {"rgbn": rgbn, "rgb_raw": rgb_raw, "cir_raw": cir_raw, "metadata": meta}

    def fetch_forest(self, aoi, out_dir, data_dir):
        """No separate Dutch forest product is required for geometry.

        fetch_nato.py registers the real AHN canopy-height model as a draped
        ecology layer after prepare_chm_inputs(). A future implementation can
        add Copernicus HRL Dominant Leaf Type or Dutch LGN here.
        """
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/nl.py",
            "elevation": {
                "service": self.AHN_WCS,
                "coverages": [self.AHN_DTM, self.AHN_DSM],
                "crs": self.native_crs,
                "resolution_m": self.default_resolution,
            },
            "imagery": {
                "rgb_wms": self.RGB_WMS,
                "rgb_layer": self.RGB_LAYER,
                "cir_wms": self.CIR_WMS,
                "cir_layer": self.CIR_LAYER,
            },
        }

    def attribution(self):
        return [
            "AHN height data: Actueel Hoogtebestand Nederland (Rijkswaterstaat/PDOK), open data / CC-BY 4.0.",
            "Aerial imagery: PDOK current aerial orthophoto RGB and CIR services.",
        ]

    def _fetch_wcs_coverage(self, coverage_id, bbox, out_path, resolution):
        width = max(1, int(math.ceil((bbox[2] - bbox[0]) / resolution)))
        height = max(1, int(math.ceil((bbox[3] - bbox[1]) / resolution)))
        if width * height <= 4_000_000:
            self._fetch_wcs_tile(coverage_id, bbox, out_path)
            _assert_raster(out_path)
            return out_path
        tile_span = max(200.0, math.sqrt(4_000_000) * resolution)
        tiles = []
        y = bbox[1]
        row = 0
        while y < bbox[3] - 1e-9:
            x = bbox[0]
            col = 0
            y1 = min(bbox[3], y + tile_span)
            while x < bbox[2] - 1e-9:
                x1 = min(bbox[2], x + tile_span)
                tile = f"{out_path}.tile-{row:03d}-{col:03d}.tif"
                self._fetch_wcs_tile(coverage_id, (x, y, x1, y1), tile)
                tiles.append(tile)
                x = x1
                col += 1
            y = y1
            row += 1
        vrt = out_path + ".vrt"
        gdal.BuildVRT(vrt, tiles)
        gdal.Translate(out_path, vrt, format="GTiff")
        for path in tiles + [vrt]:
            if os.path.exists(path):
                os.remove(path)
        _assert_raster(out_path)
        return out_path

    def _fetch_wcs_tile(self, coverage_id, bbox, out_path):
        if os.path.exists(out_path) and _raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return
        params = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("request", "GetCoverage"),
            ("coverageId", coverage_id),
            ("subset", "x(%.3f,%.3f)" % (bbox[0], bbox[2])),
            ("subset", "y(%.3f,%.3f)" % (bbox[1], bbox[3])),
            ("format", "image/tiff"),
        ]
        url = self.AHN_WCS + "?" + urllib.parse.urlencode(params, safe="(),/:")
        _download(url, out_path, self.user_agent, timeout=240)

    def _fill_elevation_voids(self, raw_dtm, raw_dsm, out_dir):
        filled_dtm = os.path.join(out_dir, "ahn_dtm_05m_filled.tif")
        filled_dsm = os.path.join(out_dir, "ahn_dsm_05m_filled.tif")
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
                "  {action} AHN {label} nodata: {bc}/{bt} ({bp:.3f}%) -> "
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

    def _fetch_wms_map(self, service, layer, bbox, width, height, out_path):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            print(f"  reuse {os.path.basename(out_path)}")
            return
        params = [
            ("service", "WMS"),
            ("version", "1.3.0"),
            ("request", "GetMap"),
            ("layers", layer),
            ("styles", ""),
            ("crs", self.native_crs),
            ("bbox", "%.3f,%.3f,%.3f,%.3f" % bbox),
            ("width", str(width)),
            ("height", str(height)),
            ("format", "image/png"),
            ("transparent", "false"),
        ]
        url = service + "?" + urllib.parse.urlencode(params, safe="(),/:")
        _download(url, out_path, self.user_agent, timeout=240)
        try:
            _read_rgb(out_path)
        except Exception as exc:  # noqa: BLE001
            preview = ""
            try:
                preview = open(out_path, "rb").read(500).decode("utf-8", "ignore")
            except OSError:
                pass
            raise RuntimeError(
                f"WMS {layer} did not return a readable image"
                + (f": {preview[:240]}" if preview else "")
            ) from exc

    def _align_to_grid(self, src_path, data_dir, out_path, resolution):
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


def _download(url, out_path, user_agent, timeout=180):
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


def _read_rgb(path):
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _write_rgbn(path, bbox, rgb, nir, crs):
    h, w = rgb.shape[:2]
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 4, gdal.GDT_Byte, options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform((bbox[0], (bbox[2] - bbox[0]) / w, 0.0,
                        bbox[3], 0.0, -((bbox[3] - bbox[1]) / h)))
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(rgb[:, :, 0])
    ds.GetRasterBand(2).WriteArray(rgb[:, :, 1])
    ds.GetRasterBand(3).WriteArray(rgb[:, :, 2])
    ds.GetRasterBand(4).WriteArray(nir.astype(np.uint8))
    ds.FlushCache()
    ds = None


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_safe_fill(fill_result):
    safe = dict(fill_result)
    safe.pop("path", None)
    return safe
