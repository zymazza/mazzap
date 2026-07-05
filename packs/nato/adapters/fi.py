"""Finland source adapter for the NATO pack.

National source status:
  * NLS/Maanmittauslaitos documents the 2 m DEM, orthophotos, false-colour
    orthophotos, and their WCS/WMS/OGC API routes.
  * The current anonymous WCS/WMS/OGC API endpoints return HTTP 401 from this
    environment, so this adapter records those checked routes and builds the
    demo through the shared fallback stack.
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
    source_crs: str = "EPSG:3067"


class FinlandAdapter:
    alpha2 = "FI"
    alpha3 = "FIN"
    name = "Finland"
    tier = "A"
    native_crs = "EPSG:3067"
    default_resolution = 10.0

    MML_WCS = "https://avoin-karttakuva.maanmittauslaitos.fi/ortokuvat-ja-korkeusmallit/wcs/v2"
    MML_WMS = "https://avoin-karttakuva.maanmittauslaitos.fi/ortokuvat-ja-korkeusmallit/wms/v1"
    MML_OGC_API = "https://avoin-paikkatieto.maanmittauslaitos.fi/tiedostopalvelu/ogcproc/v1/"
    CHECKED_NATIONAL_ENDPOINTS = [
        "https://www.maanmittauslaitos.fi/en/maps-and-spatial-data/datasets-and-interfaces/product-descriptions/elevation-model-2-m",
        "https://www.maanmittauslaitos.fi/en/maps-and-spatial-data/datasets-and-interfaces/product-descriptions/orthophotos",
        MML_WCS + "?service=WCS&request=GetCapabilities",
        MML_WMS + "?service=WMS&request=GetCapabilities&version=1.3.0",
        MML_OGC_API,
        MML_OGC_API + "processes",
    ]
    FALLBACK_NOTE = (
        "NLS/Maanmittauslaitos 2 m DEM and orthophoto interfaces are documented "
        "as open-data channels, but anonymous WCS/WMS/OGC API calls return 401; "
        "using GLO-30 + forest-masked Meta/WRI canopy when "
        "covered, ETH fallback canopy, and Sentinel-2."
    )

    user_agent = "veil/1.0 (+packs/nato Finland adapter)"

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
        warped = os.path.join(out_dir, "fi_glo30_terrain_epsg_3067.tif")
        filled = os.path.join(out_dir, "fi_glo30_terrain_epsg_3067_filled.tif")
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
            "adapter": "packs/nato/adapters/fi.py",
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
        json.dump(meta, open(os.path.join(out_dir, "fi_elevation_fallback_fetch.json"), "w"),
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
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Finland Sentinel-2 imagery needs a built data_dir/georef")
        result = global_sources.fetch_sentinel2_imagery(
            aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
        )
        stretched = os.path.join(out_dir, "fi_sentinel2_rgbnir_visible_stretch.tif")
        result["metadata"]["adapter"] = "packs/nato/adapters/fi.py"
        result["metadata"]["country"] = self.alpha3
        result["metadata"]["national_ortho_status"] = "anonymous NLS interfaces returned 401; Sentinel-2 used"
        result["metadata"]["checked_national_endpoints"] = self.CHECKED_NATIONAL_ENDPOINTS
        result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
        result["rgbn"] = stretched
        json.dump(result["metadata"], open(os.path.join(out_dir, "fi_imagery_fetch.json"), "w"),
                  indent=2)
        return result

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/fi.py",
            "status": "fallback_pending_api_key",
            "national_sources_checked": self.CHECKED_NATIONAL_ENDPOINTS,
            "fallback": {
                "terrain": "Copernicus DEM GLO-30",
                "canopy": "Meta/WRI 1 m modeled canopy preferred; ETH 10 m fallback",
                "imagery": "Sentinel-2 L2A via Element84 Earth Search",
            },
            "note": self.FALLBACK_NOTE,
        }

    def attribution(self):
        return [
            "National sources checked but not used anonymously: © Maanmittauslaitos/NLS (Finland).",
            "Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR, open data.",
            "Imagery: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Canopy fallback attribution is recorded with the selected CHM inputs.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]


def _buffered_bounds(bbox, resolution):
    pad = max(float(resolution) * 2.0, 20.0)
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
