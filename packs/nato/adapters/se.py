"""Sweden source adapter for the NATO pack.

National source status:
  * Lantmateriet Min karta orthophoto and height-model visualization WMS
    capabilities are reachable anonymously.
  * No anonymous numeric Markhojdmodell DEM WCS/download route was found from
    those checked services, and DSM from national point clouds is too heavy for
    unattended small-AOI builds.

The adapter therefore builds terrain/CHM through the shared fallback stack and
uses the anonymous Lantmateriet orthophoto WMS for visible RGB when reachable,
with Sentinel-2 supplying NIR or the whole RGB+NIR stack as needed.
"""

import importlib
import json
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
    source_crs: str = "EPSG:3006"


class SwedenAdapter:
    alpha2 = "SE"
    alpha3 = "SWE"
    name = "Sweden"
    tier = "A"
    native_crs = "EPSG:3006"
    default_resolution = 10.0

    ORTHO_WMS = "https://minkarta.lantmateriet.se/map/ortofoto"
    ORTHO_LAYER = "Ortofoto_0.25"
    ORTHO_MAX_SIZE = 4096
    CHECKED_NATIONAL_ENDPOINTS = [
        "https://minkarta.lantmateriet.se/map/ortofoto?SERVICE=WMS&REQUEST=GetCapabilities",
        "https://minkarta.lantmateriet.se/map/hojdmodell?SERVICE=WMS&REQUEST=GetCapabilities",
        "https://minkarta.lantmateriet.se/map/hojdmodell?SERVICE=WCS&REQUEST=GetCapabilities",
        "https://maps.lantmateriet.se/hojdmodell/wcs/v1?SERVICE=WCS&REQUEST=GetCapabilities",
        "https://api.lantmateriet.se/open/topowebb-ccby/v1/wmts/token/none/?SERVICE=WMTS&REQUEST=GetCapabilities",
    ]
    FALLBACK_NOTE = (
        "Anonymous Lantmateriet Min karta height-model routes provide WMS "
        "visualization, not a numeric DEM/DSM coverage; using GLO-30 + "
        "forest-masked Meta/WRI canopy when covered, with ETH canopy as fallback."
    )
    NIR_FALLBACK_NOTE = (
        "No open Lantmateriet CIR/infrared orthophoto route was found in the "
        "anonymous services checked here; Sentinel-2 L2A supplies NIR."
    )

    user_agent = "veil/1.0 (+packs/nato Sweden adapter)"

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
            "elevation": ["Copernicus GLO-30 terrain fallback"],
            "canopy": [
                "Meta/WRI Global Canopy Height, about 1 m, modeled",
                "ETH Global Canopy Height 2020, 10 m fallback",
            ],
            "imagery": [
                "Lantmateriet Min karta orthophoto WMS visible RGB",
                "Sentinel-2 L2A NIR via Element84 Earth Search",
            ],
            "checked_national_endpoints": self.CHECKED_NATIONAL_ENDPOINTS,
            "national_note": self.FALLBACK_NOTE,
            "imagery_note": self.NIR_FALLBACK_NOTE,
        }

    def bbox_wgs84(self, aoi):
        return sh.bbox_wgs84(aoi)

    def bbox_projected(self, aoi, crs=None):
        return sh.bbox_projected(aoi, crs or self.native_crs)

    def fetch_elevation(self, aoi, out_dir, resolution=10.0):
        os.makedirs(out_dir, exist_ok=True)
        target_resolution = max(float(resolution), 10.0)
        result = global_sources.fetch_glo30_terrain(aoi, out_dir, resolution=30.0)
        bbox = self.bbox_projected(aoi)
        fetch_bounds = _buffered_bounds(bbox, target_resolution)
        warped = os.path.join(out_dir, "se_glo30_terrain_epsg_3006.tif")
        filled = os.path.join(out_dir, "se_glo30_terrain_epsg_3006_filled.tif")
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
            "adapter": "packs/nato/adapters/se.py",
            "country": self.alpha3,
            "status": "fallback",
            "fallback_reason": self.FALLBACK_NOTE,
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
        json.dump(meta, open(os.path.join(out_dir, "se_elevation_fallback_fetch.json"), "w"),
                  indent=2)
        return result

    def prepare_chm_inputs(self, data_dir, elevation, resolution=10.0, forest_type=None):
        self._data_dir = data_dir
        return global_sources.prepare_best_chm_inputs(
            data_dir,
            elevation,
            resolution=max(float(resolution), 10.0),
            alpha2=self.alpha2,
            forest_type=forest_type,
        )

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        os.makedirs(out_dir, exist_ok=True)
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Sweden imagery needs a built data_dir/georef")
        bbox = tuple(float(v) for v in footprint)
        georef_path = os.path.join(data_dir, "georef.json")
        working_crs = sh.twin_georef.crs(georef_path)
        width, height = sh.wms_size_for_bbox(bbox, px_per_m, self.ORTHO_MAX_SIZE)

        try:
            rgb_raw = os.path.join(out_dir, "se_lantmateriet_ortho_rgb.png")
            self._fetch_ortho_wms(bbox, working_crs, width, height, rgb_raw)
            rgb = sh.read_rgb(rgb_raw)
            if _blank_rgb(rgb):
                raise RuntimeError("Lantmateriet orthophoto WMS returned a blank image for the AOI")
        except Exception as exc:  # noqa: BLE001
            print(f"  Sweden orthophoto unavailable ({exc}); using Sentinel-2 RGB+NIR")
            result = global_sources.fetch_sentinel2_imagery(
                aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
            )
            stretched = os.path.join(out_dir, "se_sentinel2_rgbnir_visible_stretch.tif")
            result["metadata"]["adapter"] = "packs/nato/adapters/se.py"
            result["metadata"]["country"] = self.alpha3
            result["metadata"]["national_ortho_status"] = "failed; Sentinel-2 used"
            result["metadata"]["national_ortho_error"] = str(exc)
            result["metadata"]["checked_national_endpoints"] = self.CHECKED_NATIONAL_ENDPOINTS
            result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
            result["rgbn"] = stretched
            json.dump(result["metadata"], open(os.path.join(out_dir, "se_imagery_fetch.json"), "w"),
                      indent=2)
            return result

        sentinel = global_sources.fetch_sentinel2_imagery(
            aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
        )
        nir = sh.align_sentinel_nir(
            sentinel["rgbn"], out_dir, "se", bbox, working_crs, rgb.shape[1], rgb.shape[0]
        )
        rgbn = os.path.join(out_dir, "se_ortho_rgb_sentinel2_nir.tif")
        sh.write_rgbn(rgbn, bbox, rgb, nir, working_crs)
        meta = {
            "adapter": "packs/nato/adapters/se.py",
            "country": self.alpha3,
            "rgb_source": "Lantmateriet Min karta orthophoto WMS",
            "rgb_wms": self.ORTHO_WMS,
            "rgb_layer": self.ORTHO_LAYER,
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
            "band_order": "R,G,B,NIR (visible RGB from Lantmateriet orthophoto; NIR from Sentinel-2 band 8)",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "se_imagery_fetch.json"), "w"),
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
            "adapter": "packs/nato/adapters/se.py",
            "status": "fallback_no_anonymous_numeric_dem_or_dsm",
            "national_sources_checked": self.CHECKED_NATIONAL_ENDPOINTS,
            "fallback": {
                "terrain": "Copernicus DEM GLO-30",
                "canopy": "Meta/WRI 1 m modeled canopy preferred; ETH 10 m fallback",
                "imagery_nir": "Sentinel-2 L2A via Element84 Earth Search",
            },
            "imagery": {
                "rgb_wms": self.ORTHO_WMS,
                "rgb_layer": self.ORTHO_LAYER,
                "fallback": "Sentinel-2 RGB+NIR if Lantmateriet orthophoto is unavailable",
            },
            "note": self.FALLBACK_NOTE,
        }

    def attribution(self):
        return [
            "RGB orthophoto when used: © Lantmateriet (Sweden) Min karta orthophoto WMS.",
            "National height-model visualization checked but not used as numeric terrain: © Lantmateriet (Sweden).",
            "Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR, open data.",
            "Imagery NIR or fallback RGB+NIR: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Forest typing: Copernicus HRL Dominant Leaf Type / EEA.",
            "Canopy fallback attribution is recorded with the selected CHM inputs.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]

    def _fetch_ortho_wms(self, bbox, crs, width, height, out_path):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        params = [
            ("SERVICE", "WMS"),
            ("VERSION", "1.1.1"),
            ("REQUEST", "GetMap"),
            ("LAYERS", self.ORTHO_LAYER),
            ("STYLES", ""),
            ("SRS", crs.lower()),
            ("BBOX", "%.3f,%.3f,%.3f,%.3f" % bbox),
            ("WIDTH", str(int(width))),
            ("HEIGHT", str(int(height))),
            ("FORMAT", "image/png"),
        ]
        url = self.ORTHO_WMS + "?" + urllib.parse.urlencode(params, safe="(),/:")
        sh.download(url, out_path, self.user_agent, timeout=120)
        sh.read_rgb(out_path)
        return out_path


def _blank_rgb(rgb):
    if rgb.size == 0:
        return True
    flat = rgb.reshape(-1, 3)
    sample = flat[: min(flat.shape[0], 10000)]
    return len(np.unique(sample, axis=0)) <= 2 and float(flat.mean()) > 245.0


def _buffered_bounds(bbox, resolution):
    pad = max(float(resolution) * 2.0, 20.0)
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
