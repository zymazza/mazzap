"""Denmark source adapter for the NATO pack.

The Danish DHM/Terraen and DHM/Overflade services are distributed through
Dataforsyningen/Klimadatastyrelsen routes that require service-specific token
access from this environment. This adapter records the checked national routes
and builds a working twin through the pack fallback stack:

  * Copernicus GLO-30 terrain
  * forest-masked ETH Global Canopy Height 2020 for DSM/CHM
  * Sentinel-2 L2A RGB+NIR via Element84 Earth Search
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
    source_crs: str = "EPSG:25832"


class DenmarkAdapter:
    alpha2 = "DK"
    alpha3 = "DNK"
    name = "Denmark"
    tier = "A"
    native_crs = "EPSG:25832"
    default_resolution = 10.0

    CHECKED_DHM_ENDPOINTS = [
        "https://api.dataforsyningen.dk/dhm?service=WCS&request=GetCapabilities",
        "https://api.dataforsyningen.dk/dhm?service=WMS&request=GetCapabilities",
        "https://services.datafordeler.dk/DHM/DHM/1.0.0/WCS?SERVICE=WCS&REQUEST=GetCapabilities",
        "https://services.datafordeler.dk/DHM/DHM/1.0.0/WMS?SERVICE=WMS&REQUEST=GetCapabilities",
    ]
    FALLBACK_NOTE = (
        "Danish DHM/Terraen and DHM/Overflade endpoints require Dataforsyningen/"
        "Klimadatastyrelsen token access from this environment; using GLO-30 + "
        "forest-masked ETH canopy + Sentinel-2."
    )

    user_agent = "veil/1.0 (+packs/nato Denmark adapter)"

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
            "national_note": self.FALLBACK_NOTE,
            "checked_national_endpoints": self.CHECKED_DHM_ENDPOINTS,
        }

    def bbox_wgs84(self, aoi):
        return sh.bbox_wgs84(aoi)

    def bbox_projected(self, aoi, crs=None):
        return sh.bbox_projected(aoi, crs or self.native_crs)

    def fetch_elevation(self, aoi, out_dir, resolution=10.0):
        os.makedirs(out_dir, exist_ok=True)
        target_resolution = max(float(resolution), 10.0)
        result = global_sources.fetch_glo30_terrain(aoi, out_dir, resolution=30.0)
        bbox = self.bbox_projected(aoi, self.native_crs)
        warped = os.path.join(out_dir, "dk_glo30_terrain_epsg_25832.tif")
        filled = os.path.join(out_dir, "dk_glo30_terrain_epsg_25832_filled.tif")
        if not sh.raster_ok(warped):
            sh.gdal.Warp(
                warped,
                result["terrain"],
                dstSRS=sh.srs(self.native_crs).ExportToWkt(),
                outputBounds=bbox,
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
            "adapter": "packs/nato/adapters/dk.py",
            "country": self.alpha3,
            "status": "fallback",
            "fallback_reason": self.FALLBACK_NOTE,
            "checked_national_endpoints": self.CHECKED_DHM_ENDPOINTS,
            "requested_resolution_m": resolution,
            "target_crs": self.native_crs,
            "target_resolution_m": target_resolution,
            "raw_glo30": result.get("terrain"),
            "terrain": os.path.basename(filled),
            "nodata_fill": sh.json_safe_fill(fill),
        }
        result["terrain"] = filled
        result["dtm"] = filled
        result["dsm"] = filled
        result["metadata"] = meta
        json.dump(meta, open(os.path.join(out_dir, "dk_elevation_fallback_fetch.json"), "w"),
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
            raise RuntimeError("Denmark Sentinel-2 imagery needs a built data_dir/georef")
        return global_sources.fetch_sentinel2_imagery(
            aoi,
            out_dir,
            data_dir,
            footprint,
            px_per_m=px_per_m,
            alpha2=self.alpha2,
        )

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/dk.py",
            "status": "fallback_pending_token",
            "national_elevation_checked": self.CHECKED_DHM_ENDPOINTS,
            "fallback": {
                "terrain": "Copernicus DEM GLO-30",
                "canopy": "ETH Global Canopy Height 2020, forest-masked",
                "imagery": "Sentinel-2 L2A via Element84 Earth Search",
            },
            "note": self.FALLBACK_NOTE,
        }

    def attribution(self):
        return [
            "National DHM checked but not used: Klimadatastyrelsen / Dataforsyningen DHM/Terraen and DHM/Overflade require token access.",
            "Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR, open data.",
            "Imagery: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner, CC-BY 4.0.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]
