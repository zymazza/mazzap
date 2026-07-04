"""Czechia source adapter for the NATO pack.

Implemented national path:
  * Cuzk DMR 5G ImageServer terrain, 2 m, EPSG:5514
  * Cuzk DMP 1G ImageServer surface, 2 m, EPSG:5514
  * Cuzk ORTOFOTO MapServer visible RGB
  * Sentinel-2 L2A NIR via Element84 Earth Search for the fourth band
"""

import importlib
import json
import math
import os
import urllib.parse
from dataclasses import dataclass

from . import _shared as sh
from .elevation import fill_raster_nodata

global_sources = importlib.import_module(__package__ + ".global")


@dataclass(frozen=True)
class AoiBounds:
    bbox: tuple
    source_crs: str = "EPSG:5514"


class CzechiaAdapter:
    alpha2 = "CZ"
    alpha3 = "CZE"
    name = "Czechia"
    tier = "A"
    native_crs = "EPSG:5514"
    default_resolution = 2.0

    DMR5G = "https://ags.cuzk.cz/arcgis2/rest/services/dmr5g/ImageServer"
    DMP1G = "https://ags.cuzk.cz/arcgis2/rest/services/dmp1g/ImageServer"
    ORTOFOTO = "https://ags.cuzk.cz/arcgis1/rest/services/ORTOFOTO/MapServer"
    IMAGE_MAX_WIDTH = 12000
    IMAGE_MAX_HEIGHT = 4000
    ORTHO_MAX_SIZE = 4096
    NIR_FALLBACK_NOTE = (
        "No open Cuzk CIR/infrared orthophoto service was found in the public "
        "ArcGIS catalogue; Sentinel-2 L2A supplies NIR."
    )

    nodata_fill_search_distances_px = (256, 512, 1024)
    nodata_fill_smoothing_iterations = 2
    user_agent = "veil/1.0 (+packs/nato Czechia adapter)"

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
                "Cuzk DMR 5G terrain ImageServer",
                "Cuzk DMP 1G surface ImageServer",
            ],
            "imagery": [
                "Cuzk ORTOFOTO MapServer RGB",
                "Sentinel-2 L2A NIR via Element84 Earth Search",
            ],
            "imagery_note": self.NIR_FALLBACK_NOTE,
        }

    def bbox_wgs84(self, aoi):
        return sh.bbox_wgs84(aoi)

    def bbox_projected(self, aoi, crs=None):
        return sh.bbox_projected(aoi, crs or self.native_crs)

    def fetch_elevation(self, aoi, out_dir, resolution=2.0):
        os.makedirs(out_dir, exist_ok=True)
        bbox = self.bbox_projected(aoi)
        raw_dtm = os.path.join(out_dir, "cuzk_dmr5g_terrain_raw.tif")
        raw_dsm = os.path.join(out_dir, "cuzk_dmp1g_surface_raw.tif")
        try:
            self._fetch_image_server(self.DMR5G, bbox, raw_dtm, resolution)
            self._fetch_image_server(self.DMP1G, bbox, raw_dsm, resolution)
            fill = self._fill_elevation_voids(raw_dtm, raw_dsm, out_dir)
        except Exception as exc:  # noqa: BLE001
            return self._fallback_elevation(aoi, out_dir, resolution, exc)

        dtm = fill["dtm"]["path"]
        dsm = fill["dsm"]["path"]
        meta = {
            "adapter": "packs/nato/adapters/cz.py",
            "country": self.alpha3,
            "status": "national",
            "bbox_native": bbox,
            "crs": self.native_crs,
            "resolution_m": resolution,
            "native_resolution_m": 2.0,
            "raw_dtm": os.path.basename(raw_dtm),
            "raw_dsm": os.path.basename(raw_dsm),
            "dtm": os.path.basename(dtm),
            "dsm": os.path.basename(dsm),
            "source": "Cuzk DMR 5G terrain and DMP 1G surface ArcGIS ImageServer exports",
            "dtm_endpoint": self.DMR5G,
            "dsm_endpoint": self.DMP1G,
            "nodata_fill": {
                "enabled": True,
                "search_distances_px": list(self.nodata_fill_search_distances_px),
                "smoothing_iterations": self.nodata_fill_smoothing_iterations,
                "dtm": sh.json_safe_fill(fill["dtm"]),
                "dsm": sh.json_safe_fill(fill["dsm"]),
            },
            "license": "Czech Office for Surveying, Mapping and Cadastre public view/download services",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "cz_elevation_fetch.json"), "w"),
                  indent=2)
        return {"dtm": dtm, "dsm": dsm, "raw_dtm": raw_dtm, "raw_dsm": raw_dsm,
                "metadata": meta}

    def prepare_chm_inputs(self, data_dir, elevation, resolution=2.0, forest_type=None):
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
            terrain_source="Cuzk DMR 5G national terrain",
            status_filename="cz_chm_inputs.json",
            contract_note=(
                "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
                "Czechia adapter writes DSM = national DMR terrain + selected "
                "forest-masked global canopy, DTM = national DMR terrain"
            ),
        )

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        os.makedirs(out_dir, exist_ok=True)
        data_dir = self._data_dir or global_sources._infer_data_dir(out_dir)  # noqa: SLF001
        if not data_dir:
            raise RuntimeError("Czechia imagery needs a built data_dir/georef")
        bbox = tuple(float(v) for v in footprint)
        georef_path = os.path.join(data_dir, "georef.json")
        working_crs = sh.twin_georef.crs(georef_path)
        width, height = sh.wms_size_for_bbox(bbox, px_per_m, self.ORTHO_MAX_SIZE)
        try:
            rgb_raw = os.path.join(out_dir, "cz_cuzk_ortofoto_rgb.png")
            self._fetch_map_server(bbox, working_crs, width, height, rgb_raw)
            rgb = sh.read_rgb(rgb_raw)
        except Exception as exc:  # noqa: BLE001
            print(f"  Czechia ORTOFOTO unavailable ({exc}); using Sentinel-2 RGB+NIR")
            result = global_sources.fetch_sentinel2_imagery(
                aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
            )
            stretched = os.path.join(out_dir, "cz_sentinel2_rgbnir_visible_stretch.tif")
            result["metadata"]["national_ortho_status"] = "failed; Sentinel-2 used"
            result["metadata"]["national_ortho_error"] = str(exc)
            result["metadata"]["visible_rgb_stretch"] = sh.stretch_visible_rgb(result["rgbn"], stretched)
            result["rgbn"] = stretched
            return result

        sentinel = global_sources.fetch_sentinel2_imagery(
            aoi, out_dir, data_dir, footprint, px_per_m=px_per_m, alpha2=self.alpha2
        )
        nir = sh.align_sentinel_nir(
            sentinel["rgbn"], out_dir, "cz", bbox, working_crs, rgb.shape[1], rgb.shape[0]
        )
        rgbn = os.path.join(out_dir, "cz_ortofoto_rgb_sentinel2_nir.tif")
        sh.write_rgbn(rgbn, bbox, rgb, nir, working_crs)
        meta = {
            "adapter": "packs/nato/adapters/cz.py",
            "country": self.alpha3,
            "bbox": [round(v, 3) for v in bbox],
            "crs": working_crs,
            "width": int(rgb.shape[1]),
            "height": int(rgb.shape[0]),
            "px_per_m": int(px_per_m),
            "rgb_source": "Cuzk ORTOFOTO MapServer",
            "rgb_mapserver": self.ORTOFOTO,
            "nir_source": "Sentinel-2 L2A via Element84 Earth Search",
            "nir_note": self.NIR_FALLBACK_NOTE,
            "sentinel2": sentinel.get("metadata", {}),
            "rgb": os.path.basename(rgb_raw),
            "rgbn": os.path.basename(rgbn),
            "band_order": "R,G,B,NIR (visible RGB from Cuzk ORTOFOTO; NIR from Sentinel-2 band 8)",
            "fetched_at": sh.utcnow(),
        }
        json.dump(meta, open(os.path.join(out_dir, "cz_imagery_fetch.json"), "w"),
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
            "adapter": "packs/nato/adapters/cz.py",
            "elevation": {
                "dtm_imageserver": self.DMR5G,
                "dsm_imageserver": self.DMP1G,
                "crs": self.native_crs,
                "resolution_m": self.default_resolution,
                "fallback": (
                    "Copernicus GLO-30 terrain plus Meta/WRI 1 m modeled canopy "
                    "where covered; ETH 10 m canopy if Meta is unavailable"
                ),
            },
            "imagery": {
                "rgb_mapserver": self.ORTOFOTO,
                "nir_source": "Sentinel-2 L2A via Element84 Earth Search",
            },
        }

    def attribution(self):
        return [
            "Elevation: Czech Office for Surveying, Mapping and Cadastre (Cuzk) DMR 5G and DMP 1G services.",
            "Imagery RGB: Czech Office for Surveying, Mapping and Cadastre (Cuzk) ORTOFOTO service.",
            "Imagery NIR: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Fallback canopy attribution is recorded with the selected CHM inputs.",
        ]

    def _fetch_image_server(self, service, bbox, out_path, resolution):
        width = max(2, int(math.ceil((bbox[2] - bbox[0]) / resolution)))
        height = max(2, int(math.ceil((bbox[3] - bbox[1]) / resolution)))
        if width <= self.IMAGE_MAX_WIDTH and height <= self.IMAGE_MAX_HEIGHT:
            return self._fetch_image_server_tile(service, bbox, width, height, out_path)

        tile_span_y = max(200.0, self.IMAGE_MAX_HEIGHT * resolution)
        tile_span_x = max(200.0, self.IMAGE_MAX_WIDTH * resolution)
        tiles = []
        y = bbox[1]
        row = 0
        while y < bbox[3] - 1e-9:
            x = bbox[0]
            col = 0
            y1 = min(bbox[3], y + tile_span_y)
            while x < bbox[2] - 1e-9:
                x1 = min(bbox[2], x + tile_span_x)
                tw = max(2, int(math.ceil((x1 - x) / resolution)))
                th = max(2, int(math.ceil((y1 - y) / resolution)))
                tile = f"{out_path}.tile-{row:03d}-{col:03d}.tif"
                self._fetch_image_server_tile(service, (x, y, x1, y1), tw, th, tile)
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

    def _fetch_image_server_tile(self, service, bbox, width, height, out_path):
        if os.path.exists(out_path) and sh.raster_ok(out_path):
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        params = [
            ("f", "image"),
            ("bbox", "%.3f,%.3f,%.3f,%.3f" % bbox),
            ("bboxSR", "5514"),
            ("imageSR", "5514"),
            ("size", "%d,%d" % (int(width), int(height))),
            ("format", "tiff"),
            ("pixelType", "F32"),
            ("interpolation", "RSP_Bilinear"),
        ]
        url = service + "/exportImage?" + urllib.parse.urlencode(params, safe=",/:")
        sh.download(url, out_path, self.user_agent, timeout=240)
        sh.force_srs(out_path, self.native_crs)
        sh.assert_raster(out_path)
        return out_path

    def _fetch_map_server(self, bbox, crs, width, height, out_path):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            print(f"  reuse {os.path.basename(out_path)}")
            return out_path
        params = [
            ("f", "image"),
            ("bbox", "%.3f,%.3f,%.3f,%.3f" % bbox),
            ("bboxSR", sh.epsg_code(crs)),
            ("imageSR", sh.epsg_code(crs)),
            ("size", "%d,%d" % (int(width), int(height))),
            ("format", "png24"),
            ("transparent", "false"),
            ("layers", "show:0"),
        ]
        url = self.ORTOFOTO + "/export?" + urllib.parse.urlencode(params, safe=",/:")
        sh.download(url, out_path, self.user_agent, timeout=240)
        sh.read_rgb(out_path)
        return out_path

    def _fill_elevation_voids(self, raw_dtm, raw_dsm, out_dir):
        filled_dtm = os.path.join(out_dir, "cuzk_dmr5g_terrain_filled.tif")
        filled_dsm = os.path.join(out_dir, "cuzk_dmp1g_surface_filled.tif")
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
                "  {action} Cuzk {label} nodata: {bc}/{bt} ({bp:.3f}%) -> "
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
        print(f"  Czechia Cuzk elevation unavailable ({exc}); using GLO-30 + ETH fallback")
        result = global_sources.fetch_glo30_terrain(aoi, out_dir, resolution=max(float(resolution), 10.0))
        meta = {
            **result.get("metadata", {}),
            "adapter": "packs/nato/adapters/cz.py",
            "country": self.alpha3,
            "status": "fallback",
            "fallback_reason": str(exc),
            "national_dtm_checked": self.DMR5G,
            "national_dsm_checked": self.DMP1G,
            "requested_resolution_m": resolution,
        }
        json.dump(meta, open(os.path.join(out_dir, "cz_elevation_fallback_fetch.json"), "w"),
                  indent=2)
        result["metadata"] = meta
        return result
