"""Luxembourg source adapter for the NATO pack.

Implemented national path:
  * ACT / data.public.lu BD-L-MNT-1m national terrain JP2, EPSG:3035 source
    warped to EPSG:2169 for the twin.
  * ACT / geoportail.lu open WMS RGB orthophoto and infrared orthophoto.
  * ETH Global Canopy Height 2020 fallback for CHM. The 2019 national LiDAR
    MNS/MNT ZIP resources are numeric and open, but each is about 27 GB and
    remote range-opening the internal TIFF is too slow for unattended AOI builds.
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
    source_crs: str = "EPSG:2169"


class LuxembourgAdapter:
    alpha2 = "LU"
    alpha3 = "LUX"
    name = "Luxembourg"
    tier = "A"
    native_crs = "EPSG:2169"
    default_resolution = 2.0

    MNT_1M_JP2 = "https://download.data.public.lu/resources/bd-l-mnt-1m/20180529-134853/EL.ElevationGridCoverage.jp2"
    MNT_2019_ZIP = (
        "https://s3.eu-central-1.amazonaws.com/download.data.public.lu/resources/"
        "lidar-2019-modele-numerique-du-terrain/20200121-082330/"
        "ACT2019_MNT_EPSG2169.zip"
    )
    MNS_2019_ZIP = (
        "https://s3.eu-central-1.amazonaws.com/download.data.public.lu/resources/"
        "lidar-2019-modele-numerique-de-la-surface/20200120-105130/"
        "ACT2019_MNS_EPSG2169.zip"
    )
    WMS = "https://wms.geoportail.lu/opendata/service"
    RGB_LAYER = "ortho_latest"
    IRC_LAYER = "ortho_irc"
    WMS_VERSION = "1.3.0"
    WMS_MAX_SIZE = 4096
    DSM_FALLBACK_NOTE = (
        "ACT 2019 MNS/MNT ZIP resources are open numeric rasters but about 27 GB "
        "each; remote range-opening the internal TIFF timed out in unattended "
        "checks, so CHM uses forest-masked ETH canopy over national MNT terrain."
    )

    user_agent = "veil/1.0 (+packs/nato Luxembourg adapter)"
    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2

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
                "ACT / data.public.lu BD-L-MNT-1m national terrain",
                "ETH Global Canopy Height 2020 fallback CHM",
            ],
            "elevation_note": self.DSM_FALLBACK_NOTE,
            "imagery": [
                "geoportail.lu latest RGB orthophoto WMS",
                "geoportail.lu infrared orthophoto WMS",
            ],
        }

    def bbox_wgs84(self, aoi):
        return sh.bbox_wgs84(aoi)

    def bbox_projected(self, aoi, crs=None):
        return sh.bbox_projected(aoi, crs or self.native_crs)

    def fetch_elevation(self, aoi, out_dir, resolution=2.0):
        os.makedirs(out_dir, exist_ok=True)
        bbox = self.bbox_projected(aoi)
        fetch_bounds = _buffered_bounds(bbox, resolution)
        raw_dtm = os.path.join(out_dir, "lu_bd_l_mnt_1m_epsg2169_raw.tif")
        filled_dtm = os.path.join(out_dir, "lu_bd_l_mnt_1m_epsg2169_filled.tif")
        if not sh.raster_ok(raw_dtm):
            sh.gdal.Warp(
                raw_dtm,
                "/vsicurl/" + self.MNT_1M_JP2,
                dstSRS=sh.srs(self.native_crs).ExportToWkt(),
                outputBounds=fetch_bounds,
                xRes=float(resolution),
                yRes=float(resolution),
                resampleAlg="bilinear",
                outputType=sh.gdal.GDT_Float32,
                dstNodata=-99999,
                multithread=True,
                creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
            )
            sh.force_srs(raw_dtm, self.native_crs)
            sh.assert_raster(raw_dtm)
        fill = fill_raster_nodata(
            raw_dtm,
            filled_dtm,
            search_distances_px=self.nodata_fill_search_distances_px,
            smoothing_iterations=self.nodata_fill_smoothing_iterations,
        )
        meta = {
            "adapter": "packs/nato/adapters/lu.py",
            "country": self.alpha3,
            "status": "national_dtm_eth_canopy",
            "bbox_native": bbox,
            "fetch_bounds": [round(v, 3) for v in fetch_bounds],
            "crs": self.native_crs,
            "resolution_m": float(resolution),
            "raw_dtm": os.path.basename(raw_dtm),
            "dtm": os.path.basename(filled_dtm),
            "dsm": None,
            "source": "ACT / data.public.lu BD-L-MNT-1m national terrain JP2",
            "endpoint": self.MNT_1M_JP2,
            "source_crs": "EPSG:3035",
            "checked_2019_lidar": {
                "mnt_zip": self.MNT_2019_ZIP,
                "mns_zip": self.MNS_2019_ZIP,
                "status": "open but too large/slow for unattended AOI range-open",
            },
            "dsm_status": "fallback_to_eth_canopy",
            "dsm_note": self.DSM_FALLBACK_NOTE,
            "nodata_fill": {
                "enabled": True,
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": sh.json_safe_fill(fill),
            },
            "license": "data.public.lu CC0 / Administration du cadastre et de la topographie",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "lu_elevation_fetch.json"), "w"),
                  indent=2)
        return {"dtm": filled_dtm, "dsm": None, "raw_dtm": raw_dtm, "metadata": meta}

    def prepare_chm_inputs(self, data_dir, elevation, resolution=2.0, forest_type=None):
        self._data_dir = data_dir
        if elevation.get("metadata", {}).get("status") == "fallback":
            return global_sources.prepare_eth_chm_inputs(
                data_dir,
                elevation,
                resolution=resolution,
                alpha2=self.alpha2,
                forest_type=forest_type,
            )

        terrain_dir = os.path.join(data_dir, "terrain")
        os.makedirs(terrain_dir, exist_ok=True)
        source_dir = os.path.dirname(elevation["dtm"])
        dtm_out = os.path.join(terrain_dir, "dtm.tif")
        dsm_out = os.path.join(terrain_dir, "dsm.tif")
        chm_out = os.path.join(terrain_dir, "chm.tif")
        canopy_raw = os.path.join(source_dir, "lu_eth_canopy_height_2020_grid.tif")
        canopy_masked = os.path.join(source_dir, "lu_eth_canopy_height_2020_forest_masked_grid.tif")

        sh.align_to_grid(elevation["dtm"], data_dir, dtm_out)
        canopy_meta = global_sources.fetch_eth_canopy_to_grid(data_dir, source_dir, canopy_raw)
        mask_meta = global_sources._forest_mask_canopy(  # noqa: SLF001
            data_dir,
            source_dir,
            canopy_raw,
            canopy_masked,
            forest_type=forest_type,
            alpha2=self.alpha2,
        )
        global_sources._write_dsm_and_chm(dtm_out, canopy_masked, dsm_out, chm_out)  # noqa: SLF001
        status = {
            "status": "ok",
            "source": "ACT/data.public.lu national MNT plus forest-masked ETH Global Canopy Height 2020",
            "fallback": self.DSM_FALLBACK_NOTE,
            "dsm": "terrain/dsm.tif",
            "dtm": "terrain/dtm.tif",
            "chm": "terrain/chm.tif",
            "canopy_raster": os.path.relpath(canopy_masked, data_dir),
            "raw_canopy_raster": os.path.relpath(canopy_raw, data_dir),
            "contract": (
                "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
                "Luxembourg adapter writes DSM = national MNT + forest-masked ETH canopy, "
                "DTM = national MNT"
            ),
            "resolution_m": resolution,
            "canopy": canopy_meta,
            "canopy_forest_mask": mask_meta,
        }
        json.dump(status, open(os.path.join(terrain_dir, "lu_chm_inputs.json"), "w"),
                  indent=2)
        return {
            "dtm": dtm_out,
            "dsm": dsm_out,
            "chm": chm_out,
            "canopy": canopy_masked,
            "raw_canopy": canopy_raw,
            "layer_id": "lu_eth_chm",
            "layer_label": "ETH Canopy Height over ACT MNT",
            "layer_description": (
                "Forest-masked ETH Global Canopy Height 2020 over ACT/data.public.lu "
                "national terrain."
            ),
            "metadata": status,
        }

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        os.makedirs(out_dir, exist_ok=True)
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Luxembourg imagery needs a built data_dir/georef")
        bbox = tuple(float(v) for v in footprint)
        working_crs = sh.twin_georef.crs(os.path.join(data_dir, "georef.json"))
        width, height = sh.wms_size_for_bbox(bbox, px_per_m, self.WMS_MAX_SIZE)
        try:
            rgb_raw = os.path.join(out_dir, "lu_geoportail_ortho_latest.png")
            irc_raw = os.path.join(out_dir, "lu_geoportail_ortho_irc.png")
            sh.fetch_wms_map(
                self.WMS,
                self.RGB_LAYER,
                bbox,
                working_crs,
                width,
                height,
                rgb_raw,
                self.user_agent,
                version=self.WMS_VERSION,
            )
            sh.fetch_wms_map(
                self.WMS,
                self.IRC_LAYER,
                bbox,
                working_crs,
                width,
                height,
                irc_raw,
                self.user_agent,
                version=self.WMS_VERSION,
            )
            rgb = sh.read_rgb(rgb_raw)
            irc = sh.read_rgb(irc_raw)
            rgbn = os.path.join(out_dir, "lu_geoportail_rgbn_ortho.tif")
            sh.write_rgbn(rgbn, bbox, rgb, irc[:, :, 0], working_crs)
            meta = {
                "adapter": "packs/nato/adapters/lu.py",
                "country": self.alpha3,
                "bbox": [round(v, 3) for v in bbox],
                "crs": working_crs,
                "width": int(rgb.shape[1]),
                "height": int(rgb.shape[0]),
                "px_per_m": int(px_per_m),
                "rgb_source": "geoportail.lu open WMS latest orthophoto",
                "rgb_layer": self.RGB_LAYER,
                "nir_source": "geoportail.lu open WMS infrared orthophoto",
                "irc_layer": self.IRC_LAYER,
                "rgb": os.path.basename(rgb_raw),
                "irc": os.path.basename(irc_raw),
                "rgbn": os.path.basename(rgbn),
                "band_order": "R,G,B,NIR (NIR copied from infrared orthophoto band 1)",
                "fetched_at": sh.utcnow(),
            }
            json.dump(meta, open(os.path.join(out_dir, "lu_imagery_fetch.json"), "w"),
                      indent=2)
            return {"rgbn": rgbn, "rgb_raw": rgb_raw, "cir_raw": irc_raw,
                    "metadata": meta}
        except Exception as exc:  # noqa: BLE001
            print(f"  Luxembourg geoportail orthophoto/IRC unavailable ({exc}); using Sentinel-2")
            result = global_sources.fetch_sentinel2_imagery(
                aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
            )
            stretched = os.path.join(out_dir, "lu_sentinel2_rgbnir_visible_stretch.tif")
            result["metadata"]["national_ortho_status"] = "failed; Sentinel-2 used"
            result["metadata"]["national_ortho_error"] = str(exc)
            result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
            result["rgbn"] = stretched
            return result

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/lu.py",
            "elevation": {
                "national_dtm": self.MNT_1M_JP2,
                "checked_2019_mnt_zip": self.MNT_2019_ZIP,
                "checked_2019_mns_zip": self.MNS_2019_ZIP,
                "crs": self.native_crs,
                "dsm_fallback": self.DSM_FALLBACK_NOTE,
            },
            "imagery": {
                "wms": self.WMS,
                "rgb_layer": self.RGB_LAYER,
                "irc_layer": self.IRC_LAYER,
            },
            "canopy": {
                "source": "ETH Global Canopy Height 2020",
                "record": global_sources.ETH_RESEARCH_RECORD,
            },
        }

    def attribution(self):
        return [
            "Elevation and imagery: © ACT / Administration du cadastre et de la topographie (Luxembourg), data.public.lu / geoportail.lu.",
            "Imagery fallback: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Canopy fallback: ETH Global Canopy Height 2020, Lang, Schindler and Wegner, CC-BY 4.0.",
            "Canopy forest mask fallback: ESA WorldCover 2021 v200, European Space Agency / VITO, open data.",
        ]


def _buffered_bounds(bbox, resolution):
    pad = max(float(resolution) * 2.0, 20.0)
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
