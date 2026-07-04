"""Spain source adapter for the NATO pack.

Implemented sources:
  * IGN/CNIG MDT WCS 2.0.1, PNOA-LiDAR bare-earth terrain, 5 m
  * Meta/WRI 1 m modeled canopy-height fallback for CHM where no open national
    PNOA-LiDAR MDS/DSM WCS is reachable from this environment, with ETH 10 m
    canopy as the last resort
  * IGN/CNIG PNOA maxima actualidad RGB WMS for visible imagery
  * Sentinel-2 L2A NIR via Element84 Earth Search for the fourth NIR band

The public IGN MDT WCS currently exposes 5 m MDT coverages such as
Elevacion25830_5 and Elevacion4258_5. The checked MDS/DSM WCS candidates were
not separate surface-model services, so this adapter keeps terrain national and
uses the pack's Meta-then-ETH canopy fallback to synthesize DSM = MDT + canopy.
"""

import importlib
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
global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:25830"


class SpainAdapter:
    alpha2 = "ES"
    alpha3 = "ESP"
    name = "Spain"
    tier = "A"
    native_crs = "EPSG:25830"
    default_resolution = 5.0

    MDT_WCS = "https://servicios.idee.es/wcs-inspire/mdt"
    MDT_WCS_VERSION = "2.0.1"
    MDT_FORMAT = "image/tiff"
    MDT_RESOLUTION_M = 5.0
    MDT_MAINLAND_CRS = "EPSG:25830"
    MDT_CANARY_CRS = "EPSG:4083"

    PNOA_WMS = "https://www.ign.es/wms-inspire/pnoa-ma"
    PNOA_LAYER = "OI.OrthoimageCoverage"
    PNOA_WMS_VERSION = "1.3.0"
    PNOA_MAX_SIZE = 4096

    DSM_FALLBACK_NOTE = (
        "No separate open IGN/CNIG PNOA-LiDAR MDS/DSM WCS was reachable; "
        "CHM uses Meta/WRI 1 m modeled canopy over national MDT terrain when "
        "covered, with ETH Global Canopy Height 2020 as the last resort."
    )
    NIR_FALLBACK_NOTE = (
        "No open IGN/CNIG 4-band/CIR PNOA WMS was reachable; Sentinel-2 L2A "
        "supplies the NIR band while PNOA WMS supplies visible RGB."
    )

    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2

    user_agent = "veil/1.0 (+packs/nato Spain adapter)"

    def __init__(self):
        self._data_dir = None

    def native_crs_for_aoi(self, aoi):
        bbox = self.bbox_wgs84(aoi)
        lon = (bbox[0] + bbox[2]) / 2.0
        lat = (bbox[1] + bbox[3]) / 2.0
        if lon < -10.0 or lat < 32.0:
            return self.MDT_CANARY_CRS
        zone = int((lon + 180.0) // 6.0) + 1
        if 29 <= zone <= 31:
            return f"EPSG:{25800 + zone}"
        return self.MDT_MAINLAND_CRS

    def coverage(self, aoi):
        crs = self.native_crs_for_aoi(aoi)
        bbox = self.bbox_projected(aoi, crs)
        service_crs = self.mdt_service_crs_for_aoi(aoi)
        service_bbox = self.bbox_projected(aoi, service_crs)
        return {
            "country": self.alpha3,
            "crs": crs,
            "bbox_native": bbox,
            "bbox_wgs84": self.bbox_wgs84(aoi),
            "area_ha": round(((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / 10000.0, 3),
            "elevation": [
                "IGN/CNIG PNOA-LiDAR MDT 5 m (%s)" % service_crs,
                "Meta/WRI 1 m modeled canopy fallback CHM",
                "ETH Global Canopy Height 2020 last-resort fallback CHM",
            ],
            "elevation_coverages": [self.mdt_coverage_id(service_crs)],
            "elevation_note": self.DSM_FALLBACK_NOTE,
            "imagery": [
                "IGN/CNIG PNOA maxima actualidad RGB WMS",
                "Sentinel-2 L2A NIR via Element84 Earth Search",
            ],
            "imagery_note": self.NIR_FALLBACK_NOTE,
            "service_crs": service_crs,
            "bbox_service": service_bbox,
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

    def mdt_service_crs_for_aoi(self, aoi):
        target = self.native_crs_for_aoi(aoi)
        if target.upper() == self.MDT_CANARY_CRS:
            return self.MDT_CANARY_CRS
        return self.MDT_MAINLAND_CRS

    def mdt_coverage_id(self, crs):
        epsg = _epsg_code(crs)
        if epsg not in {"25830", "4083"}:
            epsg = "25830"
        return f"Elevacion{epsg}_5"

    def fetch_elevation(self, aoi, out_dir, resolution=5.0):
        """Fetch IGN MDT GeoTIFF and return a void-filled terrain path."""
        os.makedirs(out_dir, exist_ok=True)
        target_crs = self.native_crs_for_aoi(aoi)
        target_bbox = self.bbox_projected(aoi, target_crs)
        service_crs = self.mdt_service_crs_for_aoi(aoi)
        service_bbox = self.bbox_projected(aoi, service_crs)
        service_epsg = _epsg_code(service_crs)
        target_epsg = _epsg_code(target_crs)

        raw_service = os.path.join(out_dir, f"ign_mdt_5m_{service_epsg}_raw.tif")
        self._fetch_mdt_coverage(service_bbox, service_crs, raw_service)
        raw_dtm = raw_service
        if target_crs.upper() != service_crs.upper():
            raw_dtm = os.path.join(out_dir, f"ign_mdt_5m_{target_epsg}_raw.tif")
            self._warp_to_target(raw_service, raw_dtm, target_bbox, target_crs, resolution)

        fill = self._fill_elevation_voids(raw_dtm, out_dir, target_epsg)
        dtm = fill["dtm"]["path"]
        meta = {
            "adapter": "packs/nato/adapters/es.py",
            "country": self.alpha3,
            "bbox_native": target_bbox,
            "bbox_service": service_bbox,
            "crs": target_crs,
            "service_crs": service_crs,
            "resolution_m": resolution,
            "native_resolution_m": self.MDT_RESOLUTION_M,
            "raw_dtm": os.path.basename(raw_dtm),
            "raw_service_dtm": os.path.basename(raw_service),
            "dtm": os.path.basename(dtm),
            "dsm": None,
            "source": "IGN/CNIG PNOA-LiDAR MDT WCS 2.0.1, 5 m",
            "endpoint": self.MDT_WCS,
            "coverage": self.mdt_coverage_id(service_crs),
            "dsm_status": "fallback_to_best_global_canopy",
            "dsm_note": self.DSM_FALLBACK_NOTE,
            "mds_wcs_checked": [
                "https://servicios.idee.es/wcs-inspire/mds",
                "https://servicios.idee.es/wcs-inspire/dsm",
                "https://servicios.idee.es/wcs-inspire/mdt-mds",
            ],
            "nodata_fill": {
                "enabled": True,
                "reason": "PNOA-LiDAR bare-earth MDT can contain voids under canopy/shadow or source gaps",
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": _json_safe_fill(fill["dtm"]),
            },
            "license": "CC BY 4.0 scne.es / Instituto Geografico Nacional and CNIG",
            "fetched_at": _utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "ign_mdt_elevation_fetch.json"), "w"),
                  indent=2)
        return {
            "dtm": dtm,
            "dsm": None,
            "raw_dtm": raw_dtm,
            "raw_service_dtm": raw_service,
            "metadata": meta,
        }

    def prepare_chm_inputs(self, data_dir, elevation, resolution=5.0, forest_type=None):
        """Place grid-aligned DTM/DSM using national MDT plus best global canopy."""
        self._data_dir = data_dir
        return global_sources.prepare_best_chm_inputs(
            data_dir,
            elevation,
            resolution=resolution,
            alpha2=self.alpha2,
            forest_type=forest_type,
            terrain_source="IGN/CNIG PNOA-LiDAR MDT 5 m national terrain",
            status_filename="es_chm_inputs.json",
            contract_note=(
                "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
                "Spain adapter writes DSM = national MDT + selected forest-masked global "
                "canopy, DTM = national MDT"
            ),
        )

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        """Fetch PNOA RGB and Sentinel-2 NIR, then assemble VEIL RGB+NIR."""
        os.makedirs(out_dir, exist_ok=True)
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Spain imagery needs a built data_dir/georef")
        georef_path = os.path.join(data_dir, "georef.json")
        working_crs = twin_georef.crs(georef_path)
        bbox = tuple(float(v) for v in footprint)
        width = max(2, int(round((bbox[2] - bbox[0]) * px_per_m)))
        height = max(2, int(round((bbox[3] - bbox[1]) * px_per_m)))
        if width > self.PNOA_MAX_SIZE or height > self.PNOA_MAX_SIZE:
            raise RuntimeError(
                "PNOA WMS request is %dx%d, above service max %d; lower --imagery-px-per-m"
                % (width, height, self.PNOA_MAX_SIZE)
            )

        pnoa_rgb = os.path.join(out_dir, "ign_pnoa_ma_rgb.png")
        self._fetch_wms_map(
            self.PNOA_WMS,
            self.PNOA_LAYER,
            bbox,
            working_crs,
            width,
            height,
            pnoa_rgb,
        )
        rgb = _read_rgb(pnoa_rgb)

        sentinel = global_sources.fetch_sentinel2_imagery(
            aoi,
            out_dir,
            data_dir,
            footprint,
            px_per_m=px_per_m,
            alpha2=self.alpha2,
        )
        nir = self._align_sentinel_nir(sentinel["rgbn"], out_dir, bbox, working_crs, width, height)
        if rgb.shape[:2] != nir.shape[:2]:
            nir = np.asarray(Image.fromarray(nir).resize((rgb.shape[1], rgb.shape[0])))

        rgbn = os.path.join(out_dir, "ign_pnoa_rgb_sentinel2_nir.tif")
        _write_rgbn(rgbn, bbox, rgb, nir, working_crs)
        meta = {
            "adapter": "packs/nato/adapters/es.py",
            "country": self.alpha3,
            "bbox": [round(v, 3) for v in bbox],
            "crs": working_crs,
            "width": width,
            "height": height,
            "px_per_m": int(px_per_m),
            "rgb_source": "IGN/CNIG PNOA maxima actualidad WMS",
            "rgb_wms": self.PNOA_WMS,
            "rgb_layer": self.PNOA_LAYER,
            "nir_source": "Sentinel-2 L2A via Element84 Earth Search",
            "nir_note": self.NIR_FALLBACK_NOTE,
            "sentinel2": sentinel.get("metadata", {}),
            "rgb": os.path.basename(pnoa_rgb),
            "rgbn": os.path.basename(rgbn),
            "band_order": "R,G,B,NIR (visible RGB from PNOA WMS; NIR from Sentinel-2 band 8)",
            "fetched_at": _utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "es_imagery_fetch.json"), "w"),
                  indent=2)
        return {
            "rgbn": rgbn,
            "rgb_raw": pnoa_rgb,
            "sentinel_rgbn": sentinel["rgbn"],
            "sentinel_bands": sentinel.get("bands", []),
            "metadata": meta,
        }

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/es.py",
            "elevation": {
                "service": self.MDT_WCS,
                "coverages": [self.mdt_coverage_id(self.MDT_MAINLAND_CRS)],
                "crs": self.MDT_MAINLAND_CRS,
                "resolution_m": self.MDT_RESOLUTION_M,
                "dsm_fallback": self.DSM_FALLBACK_NOTE,
            },
            "canopy": {
                "fallback_chain": [
                    "Meta/WRI Global Canopy Height, about 1 m, modeled",
                    "ETH Global Canopy Height 2020, 10 m",
                ],
                "record": global_sources.ETH_RESEARCH_RECORD,
                "download_share": "https://libdrive.ethz.ch/index.php/s/%s" %
                global_sources.ETH_SHARE_TOKEN,
            },
            "imagery": {
                "rgb_wms": self.PNOA_WMS,
                "rgb_layer": self.PNOA_LAYER,
                "nir_source": "Sentinel-2 L2A via Element84 Earth Search",
                "nir_stac": global_sources.EARTH_SEARCH,
                "nir_collection": global_sources.S2_COLLECTION,
            },
            "forest_type": {
                "source": "Copernicus HRL Dominant Leaf Type 2018 via EEA Discomap",
            },
        }

    def attribution(self):
        return [
            "Elevation: Instituto Geografico Nacional (IGN) / CNIG PNOA-LiDAR MDT, CC BY 4.0 scne.es.",
            "Imagery RGB: Instituto Geografico Nacional (IGN) / CNIG PNOA orthophoto WMS, CC BY 4.0 scne.es.",
            "Imagery NIR: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Canopy fallback attribution is recorded with the selected CHM inputs.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]

    def _fetch_mdt_coverage(self, bbox, crs, out_path):
        resolution = self.MDT_RESOLUTION_M
        width = max(1, int(math.ceil((bbox[2] - bbox[0]) / resolution)))
        height = max(1, int(math.ceil((bbox[3] - bbox[1]) / resolution)))
        if width * height <= 4_000_000:
            self._fetch_mdt_tile(bbox, crs, out_path)
            _assert_raster(out_path)
            return out_path

        tile_span = max(500.0, math.sqrt(4_000_000) * resolution)
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
                self._fetch_mdt_tile((x, y, x1, y1), crs, tile)
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

    def _fetch_mdt_tile(self, bbox, crs, out_path):
        if os.path.exists(out_path) and _raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return
        params = [
            ("service", "WCS"),
            ("version", self.MDT_WCS_VERSION),
            ("request", "GetCoverage"),
            ("coverageId", self.mdt_coverage_id(crs)),
            ("subset", "x(%.3f,%.3f)" % (bbox[0], bbox[2])),
            ("subset", "y(%.3f,%.3f)" % (bbox[1], bbox[3])),
            ("format", self.MDT_FORMAT),
        ]
        url = self.MDT_WCS + "?" + urllib.parse.urlencode(params, safe="(),/:")
        _download(url, out_path, self.user_agent, timeout=240)
        _assert_raster(out_path)

    def _fill_elevation_voids(self, raw_dtm, out_dir, epsg):
        filled_dtm = os.path.join(out_dir, f"ign_mdt_5m_{epsg}_filled.tif")
        result = {
            "dtm": fill_raster_nodata(
                raw_dtm,
                filled_dtm,
                search_distances_px=self.nodata_fill_search_distances_px,
                smoothing_iterations=self.nodata_fill_smoothing_iterations,
            )
        }
        before = result["dtm"]["before"]
        after = result["dtm"]["after"]
        action = "reuse" if result["dtm"].get("reused") else "fill"
        print(
            "  {action} IGN MDT nodata: {bc}/{bt} ({bp:.3f}%) -> "
            "{ac}/{at} ({ap:.3f}%)".format(
                action=action,
                bc=before["nodata_count"],
                bt=before["total_count"],
                bp=before["nodata_pct"],
                ac=after["nodata_count"],
                at=after["total_count"],
                ap=after["nodata_pct"],
            )
        )
        return result

    def _fetch_wms_map(self, service, layer, bbox, crs, width, height, out_path):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            print(f"  reuse {os.path.basename(out_path)}")
            return
        params = [
            ("service", "WMS"),
            ("version", self.PNOA_WMS_VERSION),
            ("request", "GetMap"),
            ("layers", layer),
            ("styles", ""),
            ("crs", crs),
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

    def _align_sentinel_nir(self, sentinel_rgbn, out_dir, bbox, crs, width, height):
        nir_src = os.path.join(out_dir, "es_sentinel2_nir_byte.tif")
        nir_aligned = os.path.join(out_dir, "es_sentinel2_nir_to_pnoa_grid.tif")
        if not _raster_ok(nir_src):
            gdal.Translate(nir_src, sentinel_rgbn, format="GTiff", bandList=[4],
                           creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
        if not _raster_ok(nir_aligned):
            gdal.Warp(
                nir_aligned,
                nir_src,
                dstSRS=_srs(crs).ExportToWkt(),
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

    def _warp_to_target(self, src_path, out_path, bbox, crs, resolution):
        if os.path.exists(out_path) and _raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        gdal.Warp(
            out_path,
            src_path,
            dstSRS=_srs(crs).ExportToWkt(),
            outputBounds=bbox,
            xRes=float(resolution),
            yRes=float(resolution),
            resampleAlg="bilinear",
            outputType=gdal.GDT_Float32,
            dstNodata=-99999,
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
        )
        _assert_raster(out_path)
        return out_path

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
        gdal.Warp(
            out_path,
            src_path,
            dstSRS=_srs(twin_georef.crs(georef_path)).ExportToWkt(),
            outputBounds=bounds,
            width=int(grid["width"]),
            height=int(grid["height"]),
            resampleAlg="bilinear",
            outputType=gdal.GDT_Float32,
            dstNodata=float("nan"),
        )
        _clean_float_raster(out_path)


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


def _read_rgb(path):
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _write_rgbn(path, bbox, rgb, nir, crs):
    h, w = rgb.shape[:2]
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 4, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE", "TILED=YES", "INTERLEAVE=PIXEL"])
    ds.SetGeoTransform((bbox[0], (bbox[2] - bbox[0]) / w, 0.0,
                        bbox[3], 0.0, -((bbox[3] - bbox[1]) / h)))
    ds.SetProjection(_srs(crs).ExportToWkt())
    ds.GetRasterBand(1).WriteArray(rgb[:, :, 0])
    ds.GetRasterBand(2).WriteArray(rgb[:, :, 1])
    ds.GetRasterBand(3).WriteArray(rgb[:, :, 2])
    ds.GetRasterBand(4).WriteArray(nir.astype(np.uint8))
    ds.FlushCache()
    ds = None


def _srs(crs):
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_safe_fill(fill_result):
    safe = dict(fill_result)
    safe.pop("path", None)
    return safe
