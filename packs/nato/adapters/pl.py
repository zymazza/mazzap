"""Poland source adapter for the NATO pack.

Implemented path where the national services complete:
  * GUGiK NMT terrain WCS, EPSG:2180
  * GUGiK NMPT surface WCS, EPSG:2180
  * GUGiK ORTO standard-resolution WMS visible RGB
  * Sentinel-2 L2A NIR via Element84 Earth Search

The GUGiK WCS advertises numeric Arc/Info ASCII Grid coverages, but GetCoverage
can disconnect or stall from this environment. The adapter records the checked
national endpoints and falls back to shared GLO-30 terrain, forest-masked
Meta/WRI canopy when covered, ETH fallback canopy, and Sentinel-2 when a
national request does not finish promptly.
"""

import importlib
import json
import math
import os
import subprocess
import urllib.parse
from dataclasses import dataclass

import numpy as np

from . import _shared as sh
from .elevation import fill_raster_nodata

global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:2180"


class PolandAdapter:
    alpha2 = "PL"
    alpha3 = "POL"
    name = "Poland"
    tier = "A"
    native_crs = "EPSG:2180"
    default_resolution = 10.0

    NMT_WCS = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModel"
    NMPT_WCS = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMPT/GRID1/WCS/DigitalSurfaceModel"
    NMT_COVERAGE = "DTM_PL-EVRF2007-NH"
    NMPT_COVERAGE = "DSM_PL-EVRF2007-NH"
    ORTO_WMS = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution"
    ORTO_LAYER = "Raster"
    ORTO_MAX_SIZE = 4096
    WCS_TIMEOUT_SECONDS = 45

    CHECKED_NATIONAL_ENDPOINTS = [
        NMT_WCS + "?SERVICE=WCS&REQUEST=GetCapabilities",
        NMPT_WCS + "?SERVICE=WCS&REQUEST=GetCapabilities",
        ORTO_WMS + "?SERVICE=WMS&REQUEST=GetCapabilities",
        "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/HighResolution?SERVICE=WMS&REQUEST=GetCapabilities",
    ]
    NIR_FALLBACK_NOTE = (
        "No open GUGiK CIR/infrared orthophoto WMS was found in the public "
        "Geoportal services checked here; Sentinel-2 L2A supplies NIR."
    )
    FALLBACK_NOTE = (
        "GUGiK NMT/NMPT WCS is open and was checked, but GetCoverage can "
        "disconnect or exceed the unattended timeout from this environment; "
        "using GLO-30 + forest-masked Meta/WRI canopy when covered, with ETH "
        "canopy as fallback when that occurs."
    )

    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2
    user_agent = "veil/1.0 (+packs/nato Poland adapter)"

    def __init__(self):
        self._data_dir = None

    def coverage(self, aoi):
        bbox = self.bbox_projected(aoi)
        return {
            "country": self.alpha3,
            "crs": self.native_crs,
            "bbox_native": bbox,
            "bbox_wgs84": self.bbox_wgs84(aoi),
            "area_ha": round(((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / 10000.0, 3),
            "elevation": [
                "GUGiK NMT terrain WCS (%s)" % self.NMT_COVERAGE,
                "GUGiK NMPT surface WCS (%s)" % self.NMPT_COVERAGE,
            ],
            "imagery": [
                "GUGiK ORTO WMS visible RGB",
                "Sentinel-2 L2A NIR via Element84 Earth Search",
            ],
            "imagery_note": self.NIR_FALLBACK_NOTE,
            "fallback_note": self.FALLBACK_NOTE,
        }

    def bbox_wgs84(self, aoi):
        return sh.bbox_wgs84(aoi)

    def bbox_projected(self, aoi, crs=None):
        return sh.bbox_projected(aoi, crs or self.native_crs)

    def fetch_elevation(self, aoi, out_dir, resolution=10.0):
        os.makedirs(out_dir, exist_ok=True)
        bbox = self.bbox_projected(aoi)
        raw_dtm = os.path.join(out_dir, "gugik_nmt_evrf2007_raw.tif")
        raw_dsm = os.path.join(out_dir, "gugik_nmpt_evrf2007_raw.tif")
        try:
            self._fetch_wcs_aaigrid(self.NMT_WCS, self.NMT_COVERAGE, bbox, raw_dtm)
            self._fetch_wcs_aaigrid(self.NMPT_WCS, self.NMPT_COVERAGE, bbox, raw_dsm)
            fill = self._fill_elevation_voids(raw_dtm, raw_dsm, out_dir)
        except Exception as exc:  # noqa: BLE001
            return self._fallback_elevation(aoi, out_dir, resolution, exc)

        dtm = fill["dtm"]["path"]
        dsm = fill["dsm"]["path"]
        meta = {
            "adapter": "packs/nato/adapters/pl.py",
            "country": self.alpha3,
            "status": "national",
            "bbox_native": bbox,
            "crs": self.native_crs,
            "resolution_m": resolution,
            "raw_dtm": os.path.basename(raw_dtm),
            "raw_dsm": os.path.basename(raw_dsm),
            "dtm": os.path.basename(dtm),
            "dsm": os.path.basename(dsm),
            "source": "GUGiK NMT terrain and NMPT surface WCS Arc/Info ASCII Grid coverages",
            "dtm_endpoint": self.NMT_WCS,
            "dsm_endpoint": self.NMPT_WCS,
            "coverages": [self.NMT_COVERAGE, self.NMPT_COVERAGE],
            "nodata_fill": {
                "enabled": True,
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": sh.json_safe_fill(fill["dtm"]),
                "dsm": sh.json_safe_fill(fill["dsm"]),
            },
            "license": "GUGiK public Geoportal services",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "pl_elevation_fetch.json"), "w"),
                  indent=2)
        return {"dtm": dtm, "dsm": dsm, "raw_dtm": raw_dtm, "raw_dsm": raw_dsm,
                "metadata": meta}

    def prepare_chm_inputs(self, data_dir, elevation, resolution=10.0, forest_type=None):
        self._data_dir = data_dir
        if elevation.get("metadata", {}).get("status") == "fallback":
            return global_sources.prepare_best_chm_inputs(
                data_dir,
                elevation,
                resolution=max(float(resolution), 10.0),
                alpha2=self.alpha2,
                forest_type=forest_type,
            )

        return global_sources.prepare_best_chm_inputs(
            data_dir,
            elevation,
            resolution=max(float(resolution), 10.0),
            alpha2=self.alpha2,
            forest_type=forest_type,
            terrain_source="GUGiK NMT national terrain",
            status_filename="pl_chm_inputs.json",
            contract_note=(
                "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
                "Poland adapter writes DSM = national NMT + selected forest-masked "
                "global canopy, DTM = national NMT"
            ),
        )

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        os.makedirs(out_dir, exist_ok=True)
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Poland imagery needs a built data_dir/georef")
        bbox = tuple(float(v) for v in footprint)
        georef_path = os.path.join(data_dir, "georef.json")
        working_crs = sh.twin_georef.crs(georef_path)
        width, height = sh.wms_size_for_bbox(bbox, px_per_m, self.ORTO_MAX_SIZE)

        try:
            rgb_raw = os.path.join(out_dir, "pl_gugik_orto_rgb.png")
            self._fetch_orto_wms(bbox, working_crs, width, height, rgb_raw)
            rgb = sh.read_rgb(rgb_raw)
            if _blank_rgb(rgb):
                raise RuntimeError("GUGiK ORTO WMS returned a blank image for the AOI")
        except Exception as exc:  # noqa: BLE001
            print(f"  Poland ORTO unavailable ({exc}); using Sentinel-2 RGB+NIR")
            result = global_sources.fetch_sentinel2_imagery(
                aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
            )
            stretched = os.path.join(out_dir, "pl_sentinel2_rgbnir_visible_stretch.tif")
            result["metadata"]["adapter"] = "packs/nato/adapters/pl.py"
            result["metadata"]["country"] = self.alpha3
            result["metadata"]["national_ortho_status"] = "failed; Sentinel-2 used"
            result["metadata"]["national_ortho_error"] = str(exc)
            result["metadata"]["checked_national_endpoints"] = self.CHECKED_NATIONAL_ENDPOINTS
            result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
            result["rgbn"] = stretched
            json.dump(result["metadata"], open(os.path.join(out_dir, "pl_imagery_fetch.json"), "w"),
                      indent=2)
            return result

        sentinel = global_sources.fetch_sentinel2_imagery(
            aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
        )
        nir = sh.align_sentinel_nir(
            sentinel["rgbn"], out_dir, "pl", bbox, working_crs, rgb.shape[1], rgb.shape[0]
        )
        rgbn = os.path.join(out_dir, "pl_orto_rgb_sentinel2_nir.tif")
        sh.write_rgbn(rgbn, bbox, rgb, nir, working_crs)
        meta = {
            "adapter": "packs/nato/adapters/pl.py",
            "country": self.alpha3,
            "rgb_source": "GUGiK ORTO WMS StandardResolution",
            "rgb_wms": self.ORTO_WMS,
            "rgb_layer": self.ORTO_LAYER,
            "nir_source": "Sentinel-2 L2A via Element84 Earth Search",
            "nir_note": self.NIR_FALLBACK_NOTE,
            "sentinel2": sentinel.get("metadata", {}),
            "bbox": [round(v, 3) for v in bbox],
            "crs": working_crs,
            "width": int(rgb.shape[1]),
            "height": int(rgb.shape[0]),
            "px_per_m": int(px_per_m),
            "rgb": os.path.basename(rgb_raw),
            "rgbn": os.path.basename(rgbn),
            "band_order": "R,G,B,NIR (visible RGB from GUGiK ORTO; NIR from Sentinel-2 band 8)",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "pl_imagery_fetch.json"), "w"),
                  indent=2)
        return {"rgbn": rgbn, "rgb_raw": rgb_raw, "sentinel_rgbn": sentinel["rgbn"],
                "metadata": meta}

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/pl.py",
            "elevation": {
                "dtm_wcs": self.NMT_WCS,
                "dsm_wcs": self.NMPT_WCS,
                "coverages": [self.NMT_COVERAGE, self.NMPT_COVERAGE],
                "crs": self.native_crs,
                "fallback": (
                    "Copernicus GLO-30 terrain plus Meta/WRI 1 m modeled canopy "
                    "where covered; ETH 10 m canopy if Meta is unavailable"
                ),
            },
            "imagery": {
                "rgb_wms": self.ORTO_WMS,
                "rgb_layer": self.ORTO_LAYER,
                "nir_source": "Sentinel-2 L2A via Element84 Earth Search",
            },
            "checked_national_endpoints": self.CHECKED_NATIONAL_ENDPOINTS,
        }

    def attribution(self):
        return [
            "Elevation and RGB orthophoto when used: © GUGiK (Poland) Geoportal services.",
            "Imagery NIR: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Forest typing: Copernicus HRL Dominant Leaf Type / EEA.",
            "Fallback terrain: Copernicus DEM GLO-30, European Space Agency / DLR, open data.",
            "Fallback canopy attribution is recorded with the selected CHM inputs.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]

    def _fetch_wcs_aaigrid(self, service, coverage_id, bbox, out_path):
        if os.path.exists(out_path) and sh.raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        params = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("request", "GetCoverage"),
            ("coverageId", coverage_id),
            ("subset", "y(%.3f,%.3f)" % (bbox[1], bbox[3])),
            ("subset", "x(%.3f,%.3f)" % (bbox[0], bbox[2])),
            ("format", "image/x-aaigrid"),
        ]
        url = service + "?" + urllib.parse.urlencode(params, safe="(),/:")
        multipart = out_path + ".multipart"
        asc = out_path + ".asc"
        cmd = [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--max-time",
            str(int(self.WCS_TIMEOUT_SECONDS)),
            "--retry",
            "1",
            "--retry-delay",
            "2",
            "--user-agent",
            self.user_agent,
            "--output",
            multipart,
            url,
        ]
        subprocess.run(cmd, check=True, timeout=self.WCS_TIMEOUT_SECONDS + 10)
        _extract_aaigrid(multipart, asc)
        sh.gdal.Translate(
            out_path,
            asc,
            format="GTiff",
            outputType=sh.gdal.GDT_Float32,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
        )
        sh.force_srs(out_path, self.native_crs)
        sh.clean_float_raster(out_path)
        sh.assert_raster(out_path)
        return out_path

    def _fetch_orto_wms(self, bbox, crs, width, height, out_path):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        request_bbox = bbox
        if crs.upper() in ("EPSG:2180", "2180"):
            request_bbox = (bbox[1], bbox[0], bbox[3], bbox[2])
        params = [
            ("service", "WMS"),
            ("version", "1.3.0"),
            ("request", "GetMap"),
            ("layers", self.ORTO_LAYER),
            ("styles", ""),
            ("crs", crs),
            ("bbox", "%.3f,%.3f,%.3f,%.3f" % request_bbox),
            ("width", str(int(width))),
            ("height", str(int(height))),
            ("format", "image/png"),
            ("transparent", "false"),
        ]
        url = self.ORTO_WMS + "?" + urllib.parse.urlencode(params, safe="(),/:")
        sh.download(url, out_path, self.user_agent, timeout=120)
        sh.read_rgb(out_path)
        return out_path

    def _fill_elevation_voids(self, raw_dtm, raw_dsm, out_dir):
        filled_dtm = os.path.join(out_dir, "gugik_nmt_evrf2007_filled.tif")
        filled_dsm = os.path.join(out_dir, "gugik_nmpt_evrf2007_filled.tif")
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
                "  {action} GUGiK {label} nodata: {bc}/{bt} ({bp:.3f}%) -> "
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

    def _fallback_elevation(self, aoi, out_dir, resolution, exc):
        print(f"  Poland GUGiK NMT/NMPT unavailable ({exc}); using GLO-30 + ETH fallback")
        target_resolution = max(float(resolution), 10.0)
        result = global_sources.fetch_glo30_terrain(aoi, out_dir, resolution=30.0)
        bbox = self.bbox_projected(aoi)
        fetch_bounds = _buffered_bounds(bbox, target_resolution)
        warped = os.path.join(out_dir, "pl_glo30_terrain_epsg_2180.tif")
        filled = os.path.join(out_dir, "pl_glo30_terrain_epsg_2180_filled.tif")
        if not sh.raster_ok(warped):
            sh.gdal.Warp(
                warped,
                result["terrain"],
                dstSRS=sh.srs(self.native_crs).ExportToWkt(),
                outputBounds=fetch_bounds,
                xRes=target_resolution,
                yRes=target_resolution,
                resampleAlg="bilinear",
                outputType=sh.gdal.GDT_Float32,
                dstNodata=-99999,
                multithread=True,
                creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
            )
        fill = fill_raster_nodata(
            warped,
            filled,
            search_distances_px=(64, 128, 256),
            smoothing_iterations=1,
        )
        meta = {
            **result.get("metadata", {}),
            "adapter": "packs/nato/adapters/pl.py",
            "country": self.alpha3,
            "status": "fallback",
            "fallback_reason": str(exc),
            "fallback_note": self.FALLBACK_NOTE,
            "checked_national_endpoints": self.CHECKED_NATIONAL_ENDPOINTS,
            "requested_resolution_m": resolution,
            "target_crs": self.native_crs,
            "target_resolution_m": target_resolution,
            "fetch_bounds": [round(v, 3) for v in fetch_bounds],
            "raw_glo30": result.get("terrain"),
            "terrain": os.path.basename(filled),
            "nodata_fill": sh.json_safe_fill(fill),
        }
        result["terrain"] = filled
        result["dtm"] = filled
        result["dsm"] = filled
        result["metadata"] = meta
        json.dump(meta, open(os.path.join(out_dir, "pl_elevation_fallback_fetch.json"), "w"),
                  indent=2)
        return result


def _extract_aaigrid(multipart_path, asc_path):
    body = open(multipart_path, "rb").read()
    start = body.find(b"ncols")
    if start < 0:
        preview = body[:500].decode("utf-8", "ignore")
        raise RuntimeError("GUGiK WCS did not return Arc/Info ASCII Grid" +
                           (f": {preview}" if preview else ""))
    end = body.find(b"\r\n--", start)
    if end < 0:
        end = body.find(b"\n--", start)
    if end < 0:
        end = len(body)
    with open(asc_path, "wb") as fh:
        fh.write(body[start:end].rstrip(b"\r\n"))


def _blank_rgb(rgb):
    if rgb.size == 0:
        return True
    flat = rgb.reshape(-1, 3)
    return len(np.unique(flat[: min(flat.shape[0], 10000)], axis=0)) <= 2 and float(flat.mean()) > 245.0


def _buffered_bounds(bbox, resolution):
    pad = max(float(resolution) * 2.0, 20.0)
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
