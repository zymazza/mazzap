"""Belgium source adapter for the NATO pack.

Implemented national path:
  * Flanders / Digitaal Vlaanderen DHMV II WCS DTM/DSM 1 m, EPSG:31370
  * Flanders current winter RGB orthophoto WMS for visible imagery
  * Sentinel-2 L2A NIR via Element84 Earth Search for the fourth band

If the Flanders national height service is unreachable for an AOI, the adapter
falls back internally to Copernicus GLO-30 terrain plus forest-masked Meta/WRI
modeled canopy where covered, with ETH Global Canopy Height as the last resort.
"""

import importlib
import json
import math
import os
import urllib.parse
from dataclasses import dataclass

import numpy as np

from . import _shared as sh
from .elevation import fill_raster_nodata

global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:31370"


class BelgiumAdapter:
    alpha2 = "BE"
    alpha3 = "BEL"
    name = "Belgium"
    tier = "A"
    native_crs = "EPSG:31370"
    default_resolution = 1.0

    DHMV_WCS = "https://geo.api.vlaanderen.be/DHMV/wcs"
    DHMV_WCS_VERSION = "2.0.1"
    DHMV_DTM = "DHMVII_DTM_1m"
    DHMV_DSM = "DHMVII_DSM_1m"
    DHMV_FORMAT = "image/tiff"

    RGB_WMS = "https://geo.api.vlaanderen.be/OMWRGBMRVL/wms"
    RGB_LAYER = "Ortho"
    WMS_VERSION = "1.3.0"
    WMS_MAX_SIZE = 4096

    NIR_FALLBACK_NOTE = (
        "No current open Flanders CIR/infrared orthophoto endpoint was found; "
        "Sentinel-2 L2A supplies NIR."
    )

    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2
    user_agent = "veil/1.0 (+packs/nato Belgium adapter)"

    def __init__(self):
        self._data_dir = None

    def coverage(self, aoi):
        bbox = self.bbox_projected(aoi)
        return {
            "country": self.alpha3,
            "region": "Flanders",
            "crs": self.native_crs,
            "bbox_native": bbox,
            "bbox_wgs84": self.bbox_wgs84(aoi),
            "area_ha": round(((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / 10000.0, 3),
            "elevation": [
                "DHMV II DTM 1 m (%s)" % self.DHMV_DTM,
                "DHMV II DSM 1 m (%s)" % self.DHMV_DSM,
            ],
            "imagery": [
                "Digitaal Vlaanderen current Flanders RGB orthophoto WMS",
                "Sentinel-2 L2A NIR via Element84 Earth Search",
            ],
            "imagery_note": self.NIR_FALLBACK_NOTE,
        }

    def bbox_wgs84(self, aoi):
        return sh.bbox_wgs84(aoi)

    def bbox_projected(self, aoi, crs=None):
        return sh.bbox_projected(aoi, crs or self.native_crs)

    def fetch_elevation(self, aoi, out_dir, resolution=1.0):
        os.makedirs(out_dir, exist_ok=True)
        bbox = self.bbox_projected(aoi)
        raw_dtm = os.path.join(out_dir, "dhmvii_dtm_1m_raw.tif")
        raw_dsm = os.path.join(out_dir, "dhmvii_dsm_1m_raw.tif")
        try:
            self._fetch_wcs_coverage(self.DHMV_DTM, bbox, raw_dtm, resolution)
            self._fetch_wcs_coverage(self.DHMV_DSM, bbox, raw_dsm, resolution)
            fill = self._fill_elevation_voids(raw_dtm, raw_dsm, out_dir)
        except Exception as exc:  # noqa: BLE001
            return self._fallback_elevation(aoi, out_dir, resolution, exc)

        dtm = fill["dtm"]["path"]
        dsm = fill["dsm"]["path"]
        meta = {
            "adapter": "packs/nato/adapters/be.py",
            "country": self.alpha3,
            "region": "Flanders",
            "status": "national",
            "bbox_native": bbox,
            "crs": self.native_crs,
            "resolution_m": resolution,
            "native_resolution_m": 1.0,
            "raw_dtm": os.path.basename(raw_dtm),
            "raw_dsm": os.path.basename(raw_dsm),
            "dtm": os.path.basename(dtm),
            "dsm": os.path.basename(dsm),
            "source": "Digitaal Vlaanderen DHMV II WCS DTM/DSM 1 m",
            "endpoint": self.DHMV_WCS,
            "coverages": [self.DHMV_DTM, self.DHMV_DSM],
            "nodata_fill": {
                "enabled": True,
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": sh.json_safe_fill(fill["dtm"]),
                "dsm": sh.json_safe_fill(fill["dsm"]),
            },
            "license": "Digitaal Vlaanderen / Agentschap Informatie Vlaanderen open geodata",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "be_elevation_fetch.json"), "w"),
                  indent=2)
        return {"dtm": dtm, "dsm": dsm, "raw_dtm": raw_dtm, "raw_dsm": raw_dsm,
                "metadata": meta}

    def prepare_chm_inputs(self, data_dir, elevation, resolution=1.0, forest_type=None):
        self._data_dir = data_dir
        if elevation.get("metadata", {}).get("status") == "fallback":
            return global_sources.prepare_best_chm_inputs(
                data_dir,
                elevation,
                resolution=resolution,
                alpha2=self.alpha2,
                forest_type=forest_type,
            )

        return global_sources.prepare_best_chm_inputs(
            data_dir,
            elevation,
            resolution=resolution,
            alpha2=self.alpha2,
            forest_type=forest_type,
            terrain_source="Digitaal Vlaanderen DHMV II DTM 1 m national terrain",
            status_filename="be_chm_inputs.json",
            contract_note=(
                "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
                "Belgium adapter writes DSM = national DTM + selected forest-masked "
                "global canopy, DTM = national DTM"
            ),
        )

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        os.makedirs(out_dir, exist_ok=True)
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Belgium imagery needs a built data_dir/georef")
        bbox = tuple(float(v) for v in footprint)
        georef_path = os.path.join(data_dir, "georef.json")
        working_crs = sh.twin_georef.crs(georef_path)
        width, height = sh.wms_size_for_bbox(bbox, px_per_m, self.WMS_MAX_SIZE)

        try:
            rgb_raw = os.path.join(out_dir, "be_digitaal_vlaanderen_rgb.png")
            sh.fetch_wms_map(
                self.RGB_WMS,
                self.RGB_LAYER,
                bbox,
                working_crs,
                width,
                height,
                rgb_raw,
                self.user_agent,
                version=self.WMS_VERSION,
            )
            rgb = sh.read_rgb(rgb_raw)
        except Exception as exc:  # noqa: BLE001
            print(f"  Belgium RGB orthophoto unavailable ({exc}); using Sentinel-2 RGB+NIR")
            result = global_sources.fetch_sentinel2_imagery(
                aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
            )
            stretched = os.path.join(out_dir, "be_sentinel2_rgbnir_visible_stretch.tif")
            result["metadata"]["national_ortho_status"] = "failed; Sentinel-2 used"
            result["metadata"]["national_ortho_error"] = str(exc)
            result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
            result["rgbn"] = stretched
            return result

        sentinel = global_sources.fetch_sentinel2_imagery(
            aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
        )
        nir = sh.align_sentinel_nir(
            sentinel["rgbn"], out_dir, "be", bbox, working_crs, rgb.shape[1], rgb.shape[0]
        )
        rgbn = os.path.join(out_dir, "be_rgb_sentinel2_nir.tif")
        sh.write_rgbn(rgbn, bbox, rgb, nir, working_crs)
        meta = {
            "adapter": "packs/nato/adapters/be.py",
            "country": self.alpha3,
            "rgb_source": "Digitaal Vlaanderen Flanders current RGB orthophoto WMS",
            "rgb_wms": self.RGB_WMS,
            "rgb_layer": self.RGB_LAYER,
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
            "band_order": "R,G,B,NIR (visible RGB from Flanders WMS; NIR from Sentinel-2 band 8)",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "be_imagery_fetch.json"), "w"),
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
            "adapter": "packs/nato/adapters/be.py",
            "elevation": {
                "service": self.DHMV_WCS,
                "coverages": [self.DHMV_DTM, self.DHMV_DSM],
                "crs": self.native_crs,
                "resolution_m": self.default_resolution,
                "fallback": (
                    "Copernicus GLO-30 terrain plus Meta/WRI 1 m modeled canopy "
                    "where covered; ETH 10 m canopy if Meta is unavailable"
                ),
            },
            "imagery": {
                "rgb_wms": self.RGB_WMS,
                "rgb_layer": self.RGB_LAYER,
                "nir_source": "Sentinel-2 L2A via Element84 Earth Search",
            },
        }

    def attribution(self):
        return [
            "Elevation: Digitaal Vlaanderen / Agentschap Informatie Vlaanderen DHMV II height model.",
            "Imagery RGB: Digitaal Vlaanderen Flanders orthophoto WMS.",
            "Imagery NIR: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Fallback canopy attribution is recorded with the selected CHM inputs.",
        ]

    def _fetch_wcs_coverage(self, coverage_id, bbox, out_path, resolution):
        width = max(1, int(math.ceil((bbox[2] - bbox[0]) / resolution)))
        height = max(1, int(math.ceil((bbox[3] - bbox[1]) / resolution)))
        if width * height <= 4_000_000:
            return self._fetch_wcs_tile(coverage_id, bbox, out_path)

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
                self._fetch_wcs_tile(coverage_id, (x, y, x1, y1), tile)
                tiles.append(tile)
                x = x1
                col += 1
            y = y1
            row += 1

        vrt = out_path + ".vrt"
        sh.gdal.BuildVRT(vrt, tiles)
        sh.gdal.Translate(out_path, vrt, format="GTiff", creationOptions=["COMPRESS=DEFLATE"])
        for path in tiles + [vrt]:
            if os.path.exists(path):
                os.remove(path)
        sh.assert_raster(out_path)
        return out_path

    def _fetch_wcs_tile(self, coverage_id, bbox, out_path):
        if os.path.exists(out_path) and sh.raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        params = [
            ("service", "WCS"),
            ("version", self.DHMV_WCS_VERSION),
            ("request", "GetCoverage"),
            ("coverageId", coverage_id),
            ("subset", "x(%.3f,%.3f)" % (bbox[0], bbox[2])),
            ("subset", "y(%.3f,%.3f)" % (bbox[1], bbox[3])),
            ("format", self.DHMV_FORMAT),
        ]
        url = self.DHMV_WCS + "?" + urllib.parse.urlencode(params, safe="(),/:")
        sh.download_wcs_geotiff(url, out_path, self.user_agent, timeout=240)
        sh.force_srs(out_path, self.native_crs)
        sh.assert_raster(out_path)
        return out_path

    def _fill_elevation_voids(self, raw_dtm, raw_dsm, out_dir):
        filled_dtm = os.path.join(out_dir, "dhmvii_dtm_1m_filled.tif")
        filled_dsm = os.path.join(out_dir, "dhmvii_dsm_1m_filled.tif")
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
                "  {action} DHMV II {label} nodata: {bc}/{bt} ({bp:.3f}%) -> "
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
        print(f"  Belgium DHMV II unavailable ({exc}); using GLO-30 + ETH fallback")
        result = global_sources.fetch_glo30_terrain(aoi, out_dir, resolution=max(float(resolution), 10.0))
        meta = {
            **result.get("metadata", {}),
            "adapter": "packs/nato/adapters/be.py",
            "country": self.alpha3,
            "status": "fallback",
            "fallback_reason": str(exc),
            "national_dtm_checked": self.DHMV_WCS,
            "national_dsm_checked": self.DHMV_WCS,
            "requested_resolution_m": resolution,
        }
        json.dump(meta, open(os.path.join(out_dir, "be_elevation_fallback_fetch.json"), "w"),
                  indent=2)
        result["metadata"] = meta
        return result
