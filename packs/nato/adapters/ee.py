"""Estonia source adapter for the NATO pack.

Checked national sources:
  * Maa-amet / Maa- ja Ruumiamet WMS services at kaart.maaamet.ee.
  * Public `fotokaart` WMS exposes rendered nDSM/DSM-style products, but no
    anonymous numeric DTM WCS was reachable from this environment.
  * Public `alus` WMS exposes RGB orthophoto (`of10000`) and CIR-NGR
    orthophoto (`cir_ngr`).

The adapter therefore builds terrain/CHM through the pack fallback stack while
using national orthophoto/CIR imagery when the WMS responds for the AOI.
"""

import importlib
import json
import os
import urllib.parse
from dataclasses import dataclass

from . import _shared as sh
from .elevation import fill_raster_nodata

global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:3301"


class EstoniaAdapter:
    alpha2 = "EE"
    alpha3 = "EST"
    name = "Estonia"
    tier = "A"
    native_crs = "EPSG:3301"
    default_resolution = 10.0

    ALUS_WMS = "https://kaart.maaamet.ee/wms/alus"
    FOTOKAART_WMS = "https://kaart.maaamet.ee/wms/fotokaart"
    RGB_LAYER = "of10000"
    CIR_LAYER = "cir_ngr"
    NDSM_LAYER = "nDSM"
    WMS_VERSION = "1.1.1"
    WMS_MAX_SIZE = 4096

    CHECKED_ELEVATION_ENDPOINTS = [
        "https://geoportaal.maaamet.ee/est/teenused/wms-wfs-wcs-teenused-p65.html",
        "https://kaart.maaamet.ee/wms/fotokaart?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0",
        "https://kaart.maaamet.ee/wcs?SERVICE=WCS&REQUEST=GetCapabilities",
        "https://kaart.maaamet.ee/wcs/fotokaart?SERVICE=WCS&REQUEST=GetCapabilities",
        "https://teenus.maaamet.ee/ows/elevation?SERVICE=WCS&REQUEST=GetCapabilities",
    ]
    FALLBACK_NOTE = (
        "Maa-amet public services expose orthophoto/CIR WMS and rendered nDSM, "
        "but no anonymous numeric DTM+DSM WCS was reachable; using GLO-30 + "
        "forest-masked Meta/WRI canopy when covered, with ETH canopy as fallback."
    )

    user_agent = "veil/1.0 (+packs/nato Estonia adapter)"

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
                "Maa-amet RGB orthophoto WMS",
                "Maa-amet CIR-NGR orthophoto WMS for NIR",
            ],
            "checked_national_endpoints": self.CHECKED_ELEVATION_ENDPOINTS,
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
        warped = os.path.join(out_dir, "ee_glo30_terrain_epsg_3301.tif")
        filled = os.path.join(out_dir, "ee_glo30_terrain_epsg_3301_filled.tif")
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
            "adapter": "packs/nato/adapters/ee.py",
            "country": self.alpha3,
            "status": "fallback",
            "fallback_reason": self.FALLBACK_NOTE,
            "checked_national_endpoints": self.CHECKED_ELEVATION_ENDPOINTS,
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
        json.dump(meta, open(os.path.join(out_dir, "ee_elevation_fallback_fetch.json"), "w"),
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
            raise RuntimeError("Estonia imagery needs a built data_dir/georef")
        bbox = tuple(float(v) for v in footprint)
        working_crs = sh.twin_georef.crs(os.path.join(data_dir, "georef.json"))
        width, height = sh.wms_size_for_bbox(bbox, px_per_m, self.WMS_MAX_SIZE)
        try:
            rgb_raw = os.path.join(out_dir, "ee_maaamet_rgb_ortho.png")
            cir_raw = os.path.join(out_dir, "ee_maaamet_cir_ngr.png")
            self._fetch_wms_111(self.ALUS_WMS, self.RGB_LAYER, bbox, working_crs,
                                width, height, rgb_raw)
            self._fetch_wms_111(self.ALUS_WMS, self.CIR_LAYER, bbox, working_crs,
                                width, height, cir_raw)
            rgb = sh.read_rgb(rgb_raw)
            cir = sh.read_rgb(cir_raw)
            _assert_not_blank(rgb, self.RGB_LAYER)
            _assert_not_blank(cir, self.CIR_LAYER)
            rgbn = os.path.join(out_dir, "ee_maaamet_rgbn_ortho.tif")
            sh.write_rgbn(rgbn, bbox, rgb, cir[:, :, 0], working_crs)
            meta = {
                "adapter": "packs/nato/adapters/ee.py",
                "country": self.alpha3,
                "bbox": [round(v, 3) for v in bbox],
                "crs": working_crs,
                "width": int(rgb.shape[1]),
                "height": int(rgb.shape[0]),
                "px_per_m": int(px_per_m),
                "rgb_source": "Maa-amet / Maa- ja Ruumiamet orthophoto WMS",
                "rgb_wms": self.ALUS_WMS,
                "rgb_layer": self.RGB_LAYER,
                "nir_source": "Maa-amet CIR-NGR orthophoto WMS",
                "cir_layer": self.CIR_LAYER,
                "rgb": os.path.basename(rgb_raw),
                "cir": os.path.basename(cir_raw),
                "rgbn": os.path.basename(rgbn),
                "band_order": "R,G,B,NIR (NIR copied from CIR-NGR band 1)",
                "fetched_at": sh.utcnow(),
            }
            json.dump(meta, open(os.path.join(out_dir, "ee_imagery_fetch.json"), "w"),
                      indent=2)
            return {"rgbn": rgbn, "rgb_raw": rgb_raw, "cir_raw": cir_raw,
                    "metadata": meta}
        except Exception as exc:  # noqa: BLE001
            print(f"  Estonia Maa-amet orthophoto/CIR unavailable ({exc}); using Sentinel-2")
            result = global_sources.fetch_sentinel2_imagery(
                aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
            )
            stretched = os.path.join(out_dir, "ee_sentinel2_rgbnir_visible_stretch.tif")
            result["metadata"]["national_ortho_status"] = "failed; Sentinel-2 used"
            result["metadata"]["national_ortho_error"] = str(exc)
            result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
            result["rgbn"] = stretched
            return result

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def _fetch_wms_111(self, service, layer, bbox, crs, width, height, out_path):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        params = [
            ("service", "WMS"),
            ("version", self.WMS_VERSION),
            ("request", "GetMap"),
            ("layers", layer),
            ("styles", ""),
            ("srs", crs),
            ("bbox", "%.3f,%.3f,%.3f,%.3f" % bbox),
            ("width", str(int(width))),
            ("height", str(int(height))),
            ("format", "image/png"),
            ("transparent", "false"),
        ]
        url = service + "?" + urllib.parse.urlencode(params, safe="(),/:")
        sh.download(url, out_path, self.user_agent, timeout=240)
        sh.read_rgb(out_path)
        return out_path

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/ee.py",
            "status": "fallback_national_elevation_unavailable",
            "national_elevation_checked": self.CHECKED_ELEVATION_ENDPOINTS,
            "imagery": {
                "wms": self.ALUS_WMS,
                "rgb_layer": self.RGB_LAYER,
                "cir_layer": self.CIR_LAYER,
            },
            "fallback": {
                "terrain": "Copernicus DEM GLO-30",
                "canopy": "Meta/WRI 1 m modeled canopy preferred; ETH 10 m fallback",
                "imagery": "Sentinel-2 L2A if Maa-amet WMS fails",
            },
            "note": self.FALLBACK_NOTE,
        }

    def attribution(self):
        return [
            "National imagery/elevation services checked: © Maa-amet (Estonia) / Maa- ja Ruumiamet.",
            "Terrain fallback: Copernicus DEM GLO-30, European Space Agency / DLR, open data.",
            "Imagery fallback: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Canopy fallback attribution is recorded with the selected CHM inputs.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]


def _buffered_bounds(bbox, resolution):
    pad = max(float(resolution) * 2.0, 20.0)
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)


def _assert_not_blank(rgb, layer):
    mean = rgb.reshape(-1, 3).mean(axis=0)
    std = rgb.reshape(-1, 3).std(axis=0)
    if float(mean.mean()) > 245.0 and float(std.mean()) < 20.0:
        raise RuntimeError(f"Maa-amet WMS layer {layer} returned a near-blank tile")
