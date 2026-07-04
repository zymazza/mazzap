"""France source adapter for the NATO pack.

Implemented national path:
  * IGN Geoplateforme WMS-R RGE ALTI high-resolution MNT terrain GeoTIFF
  * IGN Geoplateforme WMS-R high-resolution MNS surface GeoTIFF
  * IGN Geoplateforme BD ORTHO RGB and ORTHO IRC WMS-R imagery
"""

import importlib
import json
import math
import os
from dataclasses import dataclass

from . import _shared as sh
from .elevation import fill_raster_nodata

global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:2154"


class FranceAdapter:
    alpha2 = "FR"
    alpha3 = "FRA"
    name = "France"
    tier = "A"
    native_crs = "EPSG:2154"
    default_resolution = 1.0

    GEOPF_WMS = "https://data.geopf.fr/wms-r/wms"
    WMS_VERSION = "1.3.0"
    DTM_LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
    DSM_LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES.MNS"
    RGB_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"
    IRC_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS.IRC"
    WMS_MAX_SIZE = 5010

    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2
    user_agent = "veil/1.0 (+packs/nato France adapter)"

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
                "IGN Geoplateforme RGE ALTI high-resolution MNT WMS-R",
                "IGN Geoplateforme high-resolution MNS WMS-R",
            ],
            "imagery": [
                "IGN Geoplateforme ORTHOIMAGERY.ORTHOPHOTOS RGB WMS-R",
                "IGN Geoplateforme ORTHOIMAGERY.ORTHOPHOTOS.IRC infrared WMS-R",
            ],
        }

    def bbox_wgs84(self, aoi):
        return sh.bbox_wgs84(aoi)

    def bbox_projected(self, aoi, crs=None):
        return sh.bbox_projected(aoi, crs or self.native_crs)

    def fetch_elevation(self, aoi, out_dir, resolution=1.0):
        os.makedirs(out_dir, exist_ok=True)
        bbox = self.bbox_projected(aoi)
        raw_dtm = os.path.join(out_dir, "ign_rgealti_highres_mnt_raw.tif")
        raw_dsm = os.path.join(out_dir, "ign_highres_mns_raw.tif")
        try:
            self._fetch_wms_float(self.DTM_LAYER, bbox, raw_dtm, resolution)
            self._fetch_wms_float(self.DSM_LAYER, bbox, raw_dsm, resolution)
            fill = self._fill_elevation_voids(raw_dtm, raw_dsm, out_dir)
        except Exception as exc:  # noqa: BLE001
            return self._fallback_elevation(aoi, out_dir, resolution, exc)

        dtm = fill["dtm"]["path"]
        dsm = fill["dsm"]["path"]
        meta = {
            "adapter": "packs/nato/adapters/fr.py",
            "country": self.alpha3,
            "status": "national",
            "bbox_native": bbox,
            "crs": self.native_crs,
            "resolution_m": resolution,
            "raw_dtm": os.path.basename(raw_dtm),
            "raw_dsm": os.path.basename(raw_dsm),
            "dtm": os.path.basename(dtm),
            "dsm": os.path.basename(dsm),
            "source": "IGN Geoplateforme WMS-R Float32 GeoTIFF elevation layers",
            "endpoint": self.GEOPF_WMS,
            "dtm_layer": self.DTM_LAYER,
            "dsm_layer": self.DSM_LAYER,
            "nodata_fill": {
                "enabled": True,
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": sh.json_safe_fill(fill["dtm"]),
                "dsm": sh.json_safe_fill(fill["dsm"]),
            },
            "license": "IGN France Geoplateforme public data, CGU cartes.gouv.fr",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "fr_elevation_fetch.json"), "w"),
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
            terrain_source="IGN Geoplateforme RGE ALTI high-resolution MNT national terrain",
            status_filename="fr_chm_inputs.json",
            contract_note=(
                "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
                "France adapter writes DSM = national MNT + selected forest-masked "
                "global canopy, DTM = national MNT"
            ),
        )

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        os.makedirs(out_dir, exist_ok=True)
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("France imagery needs a built data_dir/georef")
        bbox = tuple(float(v) for v in footprint)
        georef_path = os.path.join(data_dir, "georef.json")
        working_crs = sh.twin_georef.crs(georef_path)
        width, height = sh.wms_size_for_bbox(bbox, px_per_m, self.WMS_MAX_SIZE)

        try:
            rgb_raw = os.path.join(out_dir, "fr_ign_bdortho_rgb.png")
            irc_raw = os.path.join(out_dir, "fr_ign_bdortho_irc.png")
            sh.fetch_wms_map(
                self.GEOPF_WMS,
                self.RGB_LAYER,
                bbox,
                working_crs,
                width,
                height,
                rgb_raw,
                self.user_agent,
                version=self.WMS_VERSION,
                fmt="image/png",
                style="normal",
            )
            sh.fetch_wms_map(
                self.GEOPF_WMS,
                self.IRC_LAYER,
                bbox,
                working_crs,
                width,
                height,
                irc_raw,
                self.user_agent,
                version=self.WMS_VERSION,
                fmt="image/png",
                style="normal",
            )
            rgb = sh.read_rgb(rgb_raw)
            irc = sh.read_rgb(irc_raw)
            nir = irc[:, :, 0]
            rgbn = os.path.join(out_dir, "fr_ign_rgbn_bdortho.tif")
            sh.write_rgbn(rgbn, bbox, rgb, nir, working_crs)
            meta = {
                "adapter": "packs/nato/adapters/fr.py",
                "country": self.alpha3,
                "bbox": [round(v, 3) for v in bbox],
                "crs": working_crs,
                "width": int(rgb.shape[1]),
                "height": int(rgb.shape[0]),
                "px_per_m": int(px_per_m),
                "rgb_source": "IGN Geoplateforme BD ORTHO WMS-R",
                "rgb_layer": self.RGB_LAYER,
                "nir_source": "IGN Geoplateforme ORTHO IRC WMS-R",
                "irc_layer": self.IRC_LAYER,
                "rgb": os.path.basename(rgb_raw),
                "irc": os.path.basename(irc_raw),
                "rgbn": os.path.basename(rgbn),
                "band_order": "R,G,B,NIR (NIR copied from ORTHO IRC band 1)",
                "fetched_at": sh.utcnow(),
            }
            json.dump(meta, open(os.path.join(out_dir, "fr_imagery_fetch.json"), "w"),
                      indent=2)
            return {"rgbn": rgbn, "rgb_raw": rgb_raw, "cir_raw": irc_raw,
                    "metadata": meta}
        except Exception as exc:  # noqa: BLE001
            print(f"  France IGN ortho/IRC unavailable ({exc}); using Sentinel-2 RGB+NIR")
            result = global_sources.fetch_sentinel2_imagery(
                aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
            )
            stretched = os.path.join(out_dir, "fr_sentinel2_rgbnir_visible_stretch.tif")
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
            "adapter": "packs/nato/adapters/fr.py",
            "elevation": {
                "wms": self.GEOPF_WMS,
                "dtm_layer": self.DTM_LAYER,
                "dsm_layer": self.DSM_LAYER,
                "crs": self.native_crs,
                "fallback": (
                    "Copernicus GLO-30 terrain plus Meta/WRI 1 m modeled canopy "
                    "where covered; ETH 10 m canopy if Meta is unavailable"
                ),
            },
            "imagery": {
                "wms": self.GEOPF_WMS,
                "rgb_layer": self.RGB_LAYER,
                "irc_layer": self.IRC_LAYER,
            },
        }

    def attribution(self):
        return [
            "Elevation: IGN France Geoplateforme RGE ALTI / MNS WMS-R layers.",
            "Imagery: IGN France Geoplateforme BD ORTHO RGB and ORTHO IRC WMS-R layers.",
            "Fallback imagery: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Fallback canopy attribution is recorded with the selected CHM inputs.",
        ]

    def _fetch_wms_float(self, layer, bbox, out_path, resolution):
        width = max(2, int(math.ceil((bbox[2] - bbox[0]) / resolution)))
        height = max(2, int(math.ceil((bbox[3] - bbox[1]) / resolution)))
        if width <= self.WMS_MAX_SIZE and height <= self.WMS_MAX_SIZE:
            return self._fetch_wms_float_tile(layer, bbox, width, height, out_path)

        tile_span = max(500.0, self.WMS_MAX_SIZE * resolution)
        tiles = []
        y = bbox[1]
        row = 0
        while y < bbox[3] - 1e-9:
            x = bbox[0]
            col = 0
            y1 = min(bbox[3], y + tile_span)
            while x < bbox[2] - 1e-9:
                x1 = min(bbox[2], x + tile_span)
                tw = max(2, int(math.ceil((x1 - x) / resolution)))
                th = max(2, int(math.ceil((y1 - y) / resolution)))
                tile = f"{out_path}.tile-{row:03d}-{col:03d}.tif"
                self._fetch_wms_float_tile(layer, (x, y, x1, y1), tw, th, tile)
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
        sh.force_srs(out_path, self.native_crs)
        sh.assert_raster(out_path)
        return out_path

    def _fetch_wms_float_tile(self, layer, bbox, width, height, out_path):
        if os.path.exists(out_path) and sh.raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        sh.fetch_wms_map(
            self.GEOPF_WMS,
            layer,
            bbox,
            self.native_crs,
            width,
            height,
            out_path,
            self.user_agent,
            version=self.WMS_VERSION,
            fmt="image/geotiff",
            style="normal",
        )
        sh.force_srs(out_path, self.native_crs)
        sh.assert_raster(out_path)
        return out_path

    def _fill_elevation_voids(self, raw_dtm, raw_dsm, out_dir):
        filled_dtm = os.path.join(out_dir, "ign_rgealti_highres_mnt_filled.tif")
        filled_dsm = os.path.join(out_dir, "ign_highres_mns_filled.tif")
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
                "  {action} IGN {label} nodata: {bc}/{bt} ({bp:.3f}%) -> "
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
        print(f"  France IGN elevation unavailable ({exc}); using GLO-30 + ETH fallback")
        result = global_sources.fetch_glo30_terrain(aoi, out_dir, resolution=max(float(resolution), 10.0))
        meta = {
            **result.get("metadata", {}),
            "adapter": "packs/nato/adapters/fr.py",
            "country": self.alpha3,
            "status": "fallback",
            "fallback_reason": str(exc),
            "national_dtm_checked": self.GEOPF_WMS,
            "national_dsm_checked": self.GEOPF_WMS,
            "requested_resolution_m": resolution,
        }
        json.dump(meta, open(os.path.join(out_dir, "fr_elevation_fallback_fetch.json"), "w"),
                  indent=2)
        result["metadata"] = meta
        return result
