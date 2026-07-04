"""Latvia source adapter for the NATO pack.

LGIA publishes open-data pages for orthophotos, infrared orthophotos, a 20 m
DTM, and classified LAS point-cloud base data. The available national DSM path
requires assembling LAS tiles, which is too heavy for unattended small AOI
builds here, and no anonymous national DTM/DSM WCS was found. This adapter
therefore records the checked LGIA routes and builds through the shared
GLO-30/ETH/Sentinel fallback stack.
"""

import importlib
import json
import os
from dataclasses import dataclass

from . import _shared as sh
from .elevation import fill_raster_nodata

global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:3059"


class LatviaAdapter:
    alpha2 = "LV"
    alpha3 = "LVA"
    name = "Latvia"
    tier = "A"
    native_crs = "EPSG:3059"
    default_resolution = 10.0

    CHECKED_NATIONAL_ENDPOINTS = [
        "https://www.lgia.gov.lv/lv/atvertie-dati",
        "https://www.lgia.gov.lv/lv/Digit%C4%81lais%20reljefa%20modelis",
        "https://s3.storage.pub.lvdc.gov.lv/lgia-opendata/citi/dtm/DTM_Latvija_20m.7z",
        "https://www.lgia.gov.lv/lv/Digit%C4%81lais%20virsmas%20modelis",
        "https://s3.storage.pub.lvdc.gov.lv/lgia-opendata/las/LGIA_OpenData_las_saites.txt",
        "http://s3.storage.pub.lvdc.gov.lv/lgia-opendata/ortofoto_rgb_v6/LGIA_OpenData_Ortofoto_rgb_v6_saites.txt",
        "http://s3.storage.pub.lvdc.gov.lv/lgia-opendata/ortofoto_ir_v6/LGIA_OpenData_Ortofoto_ir_v6_saites.txt",
    ]
    FALLBACK_NOTE = (
        "LGIA exposes open file downloads for DTM, LAS point clouds, RGB "
        "orthophoto, and infrared orthophoto, but no anonymous DTM+DSM WCS was "
        "found and DSM-from-LAS assembly is too heavy for unattended demo builds; "
        "using GLO-30 + forest-masked ETH canopy + Sentinel-2."
    )

    user_agent = "veil/1.0 (+packs/nato Latvia adapter)"

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
            "canopy": ["ETH Global Canopy Height 2020, forest-masked"],
            "imagery": ["Sentinel-2 L2A RGB+NIR via Element84 Earth Search"],
            "checked_national_endpoints": self.CHECKED_NATIONAL_ENDPOINTS,
            "national_note": self.FALLBACK_NOTE,
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
        warped = os.path.join(out_dir, "lv_glo30_terrain_epsg_3059.tif")
        filled = os.path.join(out_dir, "lv_glo30_terrain_epsg_3059_filled.tif")
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
            "adapter": "packs/nato/adapters/lv.py",
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
        json.dump(meta, open(os.path.join(out_dir, "lv_elevation_fallback_fetch.json"), "w"),
                  indent=2)
        return result

    def prepare_chm_inputs(self, data_dir, elevation, resolution=10.0, forest_type=None):
        self._data_dir = data_dir
        return global_sources.prepare_eth_chm_inputs(
            data_dir,
            elevation,
            resolution=max(float(resolution), 10.0),
            alpha2=self.alpha2,
            forest_type=forest_type,
        )

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Latvia Sentinel-2 imagery needs a built data_dir/georef")
        result = global_sources.fetch_sentinel2_imagery(
            aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
        )
        stretched = os.path.join(out_dir, "lv_sentinel2_rgbnir_visible_stretch.tif")
        result["metadata"]["adapter"] = "packs/nato/adapters/lv.py"
        result["metadata"]["country"] = self.alpha3
        result["metadata"]["national_ortho_status"] = "LGIA file-tile route checked; Sentinel-2 used for unattended demo"
        result["metadata"]["checked_national_endpoints"] = self.CHECKED_NATIONAL_ENDPOINTS
        result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
        result["rgbn"] = stretched
        json.dump(result["metadata"], open(os.path.join(out_dir, "lv_imagery_fetch.json"), "w"),
                  indent=2)
        return result

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/lv.py",
            "status": "fallback_national_las_too_heavy",
            "national_sources_checked": self.CHECKED_NATIONAL_ENDPOINTS,
            "fallback": {
                "terrain": "Copernicus DEM GLO-30",
                "canopy": "ETH Global Canopy Height 2020, forest-masked",
                "imagery": "Sentinel-2 L2A via Element84 Earth Search",
            },
            "note": self.FALLBACK_NOTE,
        }

    def attribution(self):
        return [
            "National sources checked but not used for the unattended build: © Latvijas Geotelpiskas informacijas agentura (LGIA).",
            "Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR, open data.",
            "Imagery: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner, CC-BY 4.0.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]


def _buffered_bounds(bbox, resolution):
    pad = max(float(resolution) * 2.0, 20.0)
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
