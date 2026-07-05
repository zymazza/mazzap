"""Global fallback sources for the NATO pack.

This module keeps the coarse Tier-C path pack-side:

* Copernicus DEM GLO-30 for worldwide 30 m terrain. GLO-30 is a DSM, not
  bare-earth LiDAR; the global tier uses it as the terrain surface.
* Meta/WRI Global Canopy Height at about 1 m for canopy height when coverage is
  available. ETH Global Canopy Height 2020 at 10 m remains the last-resort
  fallback. To satisfy the existing vegetation engine contract,
  ``prepare_chm_inputs`` writes ``terrain/dtm.tif = GLO-30`` and
  ``terrain/dsm.tif = GLO-30 + canopy``. The global canopy is forest-masked
  before DSM/CHM export so noisy canopy pixels over non-forest land do not
  become detected stems.
* Sentinel-2 L2A RGB+NIR from Element84 Earth Search for imagery/NDVI.
* Copernicus CGLS-LC100 forest type as a global conifer/broadleaf fallback.
  ESA WorldCover remains the coarsest tree-mask fallback when CGLS cannot
  supply typed forest cells.
"""

import json
import importlib
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import numpy as np
from osgeo import gdal, osr
from pyproj import Transformer

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_DIR = os.path.dirname(HERE)
PROJECT = os.path.dirname(os.path.dirname(PACK_DIR))
SCRIPTS = os.path.join(PROJECT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import twin_georef  # noqa: E402

gdal.UseExceptions()

WORLD_COVER_BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
WORLD_COVER_PREFIX = "v200/2021/map"
WORLD_COVER_YEAR = 2021
WORLD_COVER_VERSION = "v200"
WORLD_COVER_TREE = 10
NATO_FOREST = 4
DLT_FOREST_CODES = (1, 2)
CGLS_FOREST_CODES = (10, 20, 30)
WORLD_COVER_FOREST_CODES = (NATO_FOREST, WORLD_COVER_TREE)
CANOPY_MASK_DILATION_PIXELS = 1

GLO30_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
ETH_SHARE_TOKEN = "cO8or7iOe5dT2Rt"
ETH_DOWNLOAD = "https://libdrive.ethz.ch/index.php/s/%s/download" % ETH_SHARE_TOKEN
ETH_TILE_DIR = "/3deg_cogs"
ETH_RESEARCH_RECORD = "https://www.research-collection.ethz.ch/handle/20.500.11850/609802"
EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
S2_COLLECTION = "sentinel-2-l2a"
S2_DEFAULT_DATETIME = "2024-06-01T00:00:00Z/2024-09-30T23:59:59Z"
S2_REFLECTANCE_BYTE_CLIP_DN = 3000.0
S2_BOA_ADD_OFFSET_DN = 1000.0
S2_BYTE_NODATA = 255
S2_CANDIDATE_LIMIT = 50
S2_MAX_CLOUD_COVER = 60.0
S2_MIN_VALID_COVERAGE = 0.85
S2_MIN_NONZERO_COVERAGE = 0.75
S2_MIN_VISIBLE_MEAN_BYTE = 14.0
S2_MIN_VISIBLE_P98_BYTE = 20.0
S2_NONZERO_BYTE = 3
S2_FALLBACK_DATETIMES = (
    S2_DEFAULT_DATETIME,
    "2023-06-01T00:00:00Z/2023-09-30T23:59:59Z",
    "2025-06-01T00:00:00Z/2025-09-30T23:59:59Z",
    "2022-06-01T00:00:00Z/2022-09-30T23:59:59Z",
    "2021-06-01T00:00:00Z/2021-09-30T23:59:59Z",
)
CGLS_LC100_RECORD = "https://zenodo.org/records/3939050"
CGLS_FOREST_URL = (
    "https://zenodo.org/api/records/3939050/files/"
    "PROBAV_LC100_global_v3.0.1_2019-nrt_Forest-Type-layer_EPSG-4326.tif/content"
)


@dataclass
class GlobalFallbackAdapter:
    """Adapter-shaped object for countries without a national implementation."""

    country: object
    requested_tier: str = "global"

    native_crs = "EPSG:4326"
    default_resolution = 30.0
    user_agent = "veil/1.0 (+packs/nato global fallback)"

    def __post_init__(self):
        self.alpha2 = getattr(self.country, "alpha2", "NATO")
        self.alpha3 = getattr(self.country, "alpha3", self.alpha2)
        self.name = "%s global fallback" % getattr(self.country, "name", self.alpha3)
        self.tier = getattr(self.country, "tier", "C")
        self._data_dir = None

    def coverage(self, aoi):
        bbox = _aoi_wgs_bbox(aoi)
        return {
            "country": self.alpha3,
            "country_name": getattr(self.country, "name", self.alpha3),
            "national_tier": self.tier,
            "fallback_tier": self.requested_tier,
            "crs": "EPSG:4326",
            "bbox_wgs84": bbox,
            "area_ha_approx": round(_bbox_area_ha_approx(bbox), 3),
            "elevation": ["Copernicus DEM GLO-30 DSM, used as terrain DEM"],
            "canopy": [
                "Meta/WRI Global Canopy Height, about 1 m, modeled",
                "ETH Global Canopy Height 2020, 10 m fallback",
            ],
            "imagery": ["Sentinel-2 L2A RGB+NIR via Element84 Earth Search"],
            "note": (
                "No national elevation adapter is used. GLO-30 is a DSM and "
                "is coarser than national LiDAR/bare-earth terrain."
            ),
        }

    def fetch_elevation(self, aoi, out_dir, resolution=30.0):
        return fetch_glo30_terrain(aoi, out_dir, resolution=resolution)

    def prepare_chm_inputs(self, data_dir, elevation, resolution=30.0, forest_type=None):
        self._data_dir = data_dir
        if not os.environ.get("VEIL_DISABLE_META_CHM"):
            try:
                from adapters import meta_chm

                meta = meta_chm.prepare_meta_chm_inputs(
                    data_dir,
                    elevation,
                    resolution=1.0,
                    alpha2=self.alpha2,
                    forest_type=forest_type,
                )
                if meta:
                    print("  source: Meta/WRI 1 m modeled canopy height for CHM")
                    return meta
                print("  Meta/WRI 1 m canopy has no tile coverage here; using ETH fallback")
            except Exception as exc:  # noqa: BLE001
                print(f"  Meta/WRI 1 m canopy unavailable ({exc}); using ETH fallback")
        return prepare_eth_chm_inputs(data_dir, elevation, resolution=resolution,
                                      alpha2=self.alpha2, forest_type=forest_type)

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=1):
        data_dir = self._data_dir or _infer_data_dir(out_dir)
        if not data_dir:
            raise RuntimeError("global Sentinel-2 imagery needs a built data_dir/georef")
        return fetch_sentinel2_imagery(aoi, out_dir, data_dir, footprint,
                                       px_per_m=px_per_m, alpha2=self.alpha2)

    def fetch_forest(self, aoi, out_dir, data_dir):
        return fetch_leaf_type(aoi, out_dir, data_dir, alpha2=self.alpha2)

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {
            "country": self.alpha3,
            "adapter": "packs/nato/adapters/global.py",
            "fallback_tier": self.requested_tier,
            "national_adapter": "unavailable or bypassed",
            "elevation": {
                "source": "Copernicus DEM GLO-30",
                "endpoint": GLO30_BASE,
                "note": "DSM used as terrain DEM; no global bare-earth DTM",
            },
            "canopy": {
                "fallback_chain": [
                    "Meta/WRI Global Canopy Height, about 1 m, modeled",
                    "ETH Global Canopy Height 2020, 10 m",
                ],
                "eth_record": ETH_RESEARCH_RECORD,
                "eth_download_share": "https://libdrive.ethz.ch/index.php/s/%s" % ETH_SHARE_TOKEN,
            },
            "imagery": {
                "source": "Sentinel-2 L2A",
                "stac": EARTH_SEARCH,
                "collection": S2_COLLECTION,
            },
            "forest_type": {
                "source": "CGLS-LC100 forest type, falling back to ESA WorldCover tree mask",
                "cgls_record": CGLS_LC100_RECORD,
                "worldcover_base": WORLD_COVER_BASE,
            },
        }

    def attribution(self):
        return [
            "Copernicus DEM GLO-30: European Space Agency / DLR, open data.",
            "Sentinel-2 imagery: modified Copernicus Sentinel data via Element84 Earth Search.",
            "Copernicus Global Land Service LC100 forest type: Copernicus Service information / VITO.",
            "ESA WorldCover 2021 v200: European Space Agency / VITO, open data.",
        ]


def fetch_glo30_terrain(aoi, out_dir, resolution=30.0):
    """Fetch and project Copernicus GLO-30 DSM tiles covering the AOI."""
    os.makedirs(out_dir, exist_ok=True)
    bbox = _aoi_wgs_bbox(aoi)
    target_crs = _utm_crs_for_bbox(bbox)
    sources = _glo30_sources(bbox)
    if not sources:
        raise RuntimeError("No Copernicus GLO-30 tiles found for bbox %r" % (bbox,))
    out = os.path.join(out_dir, "copernicus_glo30_dsm_terrain_%s.tif" %
                       target_crs.lower().replace(":", "_"))
    bounds = _transform_bounds(bbox, "EPSG:4326", target_crs)
    if not _raster_ok(out):
        gdal.Warp(
            out,
            sources,
            dstSRS=_srs(target_crs).ExportToWkt(),
            outputBounds=bounds,
            xRes=float(resolution),
            yRes=float(resolution),
            resampleAlg="bilinear",
            outputType=gdal.GDT_Float32,
            dstNodata=-99999,
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
        )
    meta = {
        "adapter": "packs/nato/adapters/global.py",
        "source": "Copernicus DEM GLO-30 DSM",
        "endpoint": GLO30_BASE,
        "bbox_wgs84": [round(v, 8) for v in bbox],
        "target_crs": target_crs,
        "resolution_m": float(resolution),
        "raw_tiles": sources,
        "terrain": os.path.basename(out),
        "note": "GLO-30 is a DSM. The global NATO tier uses it as coarse terrain.",
        "license": "Copernicus DEM GLO-30 open data, ESA / DLR",
        "fetched_at": _utcnow(),
    }
    json.dump(meta, open(os.path.join(out_dir, "copernicus_glo30_fetch.json"), "w"),
              indent=2)
    return {"terrain": out, "dtm": out, "dsm": out, "metadata": meta}


def prepare_eth_chm_inputs(data_dir, elevation, resolution=30.0, alpha2="nato",
                           forest_type=None):
    """Write terrain/dtm.tif, terrain/dsm.tif and terrain/chm.tif for CHM detection."""
    terrain_dir = os.path.join(data_dir, "terrain")
    os.makedirs(terrain_dir, exist_ok=True)
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    source_dir = os.path.dirname(elevation["terrain"])
    dtm_out = os.path.join(terrain_dir, "dtm.tif")
    dsm_out = os.path.join(terrain_dir, "dsm.tif")
    chm_out = os.path.join(terrain_dir, "chm.tif")
    canopy_raw = os.path.join(source_dir, "%s_eth_canopy_height_2020_grid.tif" %
                              (alpha2 or "nato").lower())
    canopy_masked = os.path.join(
        source_dir,
        "%s_eth_canopy_height_2020_forest_masked_grid.tif" % (alpha2 or "nato").lower(),
    )

    _warp_float_to_grid(elevation["terrain"], dtm_out, grid, bounds, working_crs)
    canopy_meta = fetch_eth_canopy_to_grid(data_dir, source_dir, canopy_raw)
    mask_meta = _forest_mask_canopy(data_dir, source_dir, canopy_raw, canopy_masked,
                                    forest_type=forest_type, alpha2=alpha2)
    _write_dsm_and_chm(dtm_out, canopy_masked, dsm_out, chm_out)
    status = {
        "status": "ok",
        "source": (
            "Copernicus GLO-30 DSM terrain plus forest-masked ETH Global "
            "Canopy Height 2020"
        ),
        "dsm": "terrain/dsm.tif",
        "dtm": "terrain/dtm.tif",
        "chm": "terrain/chm.tif",
        "canopy_raster": os.path.relpath(canopy_masked, data_dir),
        "raw_canopy_raster": os.path.relpath(canopy_raw, data_dir),
        "contract": (
            "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
            "global fallback writes DSM = GLO-30 + forest-masked ETH canopy, "
            "DTM = GLO-30"
        ),
        "resolution_m": float(resolution),
        "canopy": canopy_meta,
        "canopy_forest_mask": mask_meta,
        "dsm_source": "ETH Global Canopy Height 2020, 10 m modeled CHM",
        "dsm_source_note": "Global modeled canopy-height fallback, not measured trees or a tree census.",
        "attribution": [
            "ETH Global Canopy Height 2020: Lang, Schindler and Wegner, CC-BY 4.0.",
        ],
    }
    json.dump(status, open(os.path.join(terrain_dir, "global_chm_inputs.json"), "w"),
              indent=2)
    return {"dtm": dtm_out, "dsm": dsm_out, "chm": chm_out, "canopy": canopy_masked,
            "raw_canopy": canopy_raw,
            "layer_id": "%s_eth_chm" % (alpha2 or "nato").lower(),
            "layer_label": "Forest-Masked ETH Canopy Height",
            "metadata": status,
            "attribution": status["attribution"]}


def prepare_best_chm_inputs(data_dir, elevation, resolution=30.0, alpha2="nato",
                            forest_type=None, terrain_source=None,
                            status_filename="global_chm_inputs.json",
                            contract_note=None):
    """Write DSM/CHM from terrain plus the best available global canopy model.

    Meta/WRI 1 m modeled canopy is preferred. ETH Global Canopy Height remains
    the last-resort fallback when Meta has no coverage or cannot be fetched.
    """
    terrain_path = elevation.get("dtm") or elevation.get("terrain")
    if not terrain_path:
        raise ValueError("prepare_best_chm_inputs requires elevation['dtm'] or ['terrain']")
    metadata = elevation.get("metadata") or {}
    label = terrain_source or metadata.get("source") or "terrain"
    return _prepare_synthetic_chm_inputs(
        data_dir,
        terrain_path,
        os.path.dirname(terrain_path),
        resolution=resolution,
        alpha2=alpha2,
        forest_type=forest_type,
        terrain_source=label,
        status_filename=status_filename,
        contract_note=contract_note,
    )


def _prepare_synthetic_chm_inputs(data_dir, terrain_path, source_dir, resolution=30.0,
                                  alpha2="nato", forest_type=None,
                                  terrain_source="terrain",
                                  status_filename="global_chm_inputs.json",
                                  contract_note=None):
    terrain_dir = os.path.join(data_dir, "terrain")
    os.makedirs(terrain_dir, exist_ok=True)
    os.makedirs(source_dir, exist_ok=True)
    dtm_out = os.path.join(terrain_dir, "dtm.tif")
    dsm_out = os.path.join(terrain_dir, "dsm.tif")
    chm_out = os.path.join(terrain_dir, "chm.tif")

    canopy = fetch_best_canopy_to_grid(
        data_dir,
        source_dir,
        forest_type=forest_type,
        alpha2=alpha2,
    )
    _warp_float_to_template(terrain_path, dtm_out, canopy["raw_canopy"])
    _write_dsm_and_chm(dtm_out, canopy["canopy"], dsm_out, chm_out)

    canopy_label = canopy["source_label"]
    status = {
        "status": "ok",
        "source": "%s plus %s" % (terrain_source, canopy_label),
        "canopy_source": canopy["source_key"],
        "canopy_source_label": canopy_label,
        "dsm": "terrain/dsm.tif",
        "dtm": "terrain/dtm.tif",
        "chm": "terrain/chm.tif",
        "canopy_raster": os.path.relpath(canopy["canopy"], data_dir),
        "raw_canopy_raster": os.path.relpath(canopy["raw_canopy"], data_dir),
        "contract": contract_note or (
            "scripts/analyze_vegetation.py reads terrain/dsm.tif and terrain/dtm.tif; "
            "adapter writes DSM = terrain + selected forest-masked global canopy, "
            "DTM = terrain"
        ),
        "resolution_m": float(canopy["resolution_m"]),
        "requested_resolution_m": float(resolution),
        "terrain_source": terrain_source,
        "canopy": canopy["metadata"],
        "canopy_forest_mask": canopy["canopy_forest_mask"],
        "canopy_selection": canopy["selection"],
        "dsm_source": canopy["dsm_source"] % terrain_source
        if "%s" in canopy["dsm_source"] else canopy["dsm_source"],
        "dsm_source_note": canopy["dsm_source_note"],
        "attribution": canopy["attribution"],
    }
    json.dump(status, open(os.path.join(terrain_dir, status_filename), "w"), indent=2)
    return {
        "dtm": dtm_out,
        "dsm": dsm_out,
        "chm": chm_out,
        "canopy": canopy["canopy"],
        "raw_canopy": canopy["raw_canopy"],
        "layer_id": canopy["layer_id"],
        "layer_label": canopy["layer_label"],
        "layer_description": canopy["layer_description"],
        "metadata": status,
        "attribution": canopy["attribution"],
    }


def fetch_best_canopy_to_grid(data_dir, out_dir, forest_type=None, alpha2="nato"):
    """Return the selected forest-masked canopy raster and provenance.

    The returned canopy has already been clipped by the forest mask. The raw
    raster is kept so callers can align terrain to the selected canopy grid.
    """
    os.makedirs(out_dir, exist_ok=True)
    alpha = (alpha2 or "nato").lower()
    attempts = []
    if not os.environ.get("VEIL_DISABLE_META_CHM"):
        try:
            meta_chm = importlib.import_module("adapters.meta_chm")
            raw = os.path.join(out_dir, "%s_meta_wri_chm_1m_grid_raw.tif" % alpha)
            masked = os.path.join(out_dir, "%s_meta_wri_chm_1m_forest_masked_grid.tif" % alpha)
            canopy = meta_chm.fetch_meta_chm(
                None,
                os.path.join(data_dir, "terrain"),
                _grid(data_dir),
                data_dir=data_dir,
                out_dir=out_dir,
                out_path=raw,
                alpha2=alpha,
                resolution=meta_chm.DEFAULT_RESOLUTION_M,
            )
            if canopy:
                mask_meta = _forest_mask_canopy(
                    data_dir,
                    out_dir,
                    raw,
                    masked,
                    forest_type=forest_type,
                    alpha2=alpha,
                )
                return {
                    "source_key": "meta_wri_1m",
                    "source_label": "forest-masked Meta/WRI 1 m modeled canopy height",
                    "raw_canopy": raw,
                    "canopy": masked,
                    "metadata": canopy["metadata"],
                    "canopy_forest_mask": mask_meta,
                    "selection": {
                        "winner": "Meta/WRI Global Canopy Height",
                        "fallback_chain": [
                            "Meta/WRI Global Canopy Height, about 1 m, modeled",
                            "ETH Global Canopy Height 2020, 10 m",
                        ],
                        "attempts": attempts + [{"source": "Meta/WRI", "status": "ok"}],
                    },
                    "dsm_source": "Meta/WRI 1 m modeled canopy over %s",
                    "dsm_source_note": meta_chm.META_MODEL_NOTE,
                    "resolution_m": _pixel_size(raw),
                    "layer_id": "%s_meta_chm" % alpha,
                    "layer_label": "Meta Canopy Height (1 m)",
                    "layer_description": (
                        "Forest-masked WRI + Meta global canopy-height model at about 1 m. "
                        "This is a predicted canopy surface, not measured tree inventory."
                    ),
                    "attribution": [meta_chm.META_ATTRIBUTION],
                }
            attempts.append({"source": "Meta/WRI", "status": "no_coverage"})
            print("  Meta/WRI 1 m canopy has no tile coverage here; using ETH fallback")
        except Exception as exc:  # noqa: BLE001
            attempts.append({"source": "Meta/WRI", "status": "unavailable", "error": str(exc)})
            print(f"  Meta/WRI 1 m canopy unavailable ({exc}); using ETH fallback")
    else:
        attempts.append({"source": "Meta/WRI", "status": "disabled_by_VEIL_DISABLE_META_CHM"})

    raw = os.path.join(out_dir, "%s_eth_canopy_height_2020_grid.tif" % alpha)
    masked = os.path.join(out_dir, "%s_eth_canopy_height_2020_forest_masked_grid.tif" % alpha)
    canopy_meta = fetch_eth_canopy_to_grid(data_dir, out_dir, raw)
    mask_meta = _forest_mask_canopy(
        data_dir,
        out_dir,
        raw,
        masked,
        forest_type=forest_type,
        alpha2=alpha,
    )
    return {
        "source_key": "eth_10m",
        "source_label": "forest-masked ETH Global Canopy Height 2020",
        "raw_canopy": raw,
        "canopy": masked,
        "metadata": canopy_meta,
        "canopy_forest_mask": mask_meta,
        "selection": {
            "winner": "ETH Global Canopy Height 2020",
            "fallback_chain": [
                "Meta/WRI Global Canopy Height, about 1 m, modeled",
                "ETH Global Canopy Height 2020, 10 m",
            ],
            "attempts": attempts + [{"source": "ETH", "status": "ok"}],
        },
        "dsm_source": "ETH Global Canopy Height 2020, 10 m modeled CHM",
        "dsm_source_note": "Global modeled canopy-height fallback, not measured trees or a tree census.",
        "resolution_m": _pixel_size(raw),
        "layer_id": "%s_eth_chm" % alpha,
        "layer_label": "Forest-Masked ETH Canopy Height",
        "layer_description": "Forest-masked ETH Global Canopy Height 2020.",
        "attribution": [
            "ETH Global Canopy Height 2020: Lang, Schindler and Wegner, CC-BY 4.0.",
        ],
    }


def fetch_eth_canopy_to_grid(data_dir, out_dir, out_path=None):
    """Clip ETH Global Canopy Height tiles to the built terrain grid."""
    os.makedirs(out_dir, exist_ok=True)
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _transform_bounds(bounds, working_crs, "EPSG:4326")
    sources = _eth_sources(wgs_bbox)
    out_path = out_path or os.path.join(out_dir, "eth_canopy_height_2020_grid.tif")
    if not sources:
        _write_constant(out_path, data_dir, grid, bounds, working_crs, value=0, nodata=255)
    elif not _raster_ok(out_path):
        gdal.Warp(
            out_path,
            sources,
            dstSRS=_srs(working_crs).ExportToWkt(),
            outputBounds=bounds,
            width=int(grid["width"]),
            height=int(grid["height"]),
            resampleAlg="bilinear",
            outputType=gdal.GDT_Byte,
            srcNodata=255,
            dstNodata=255,
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
        )
    stats = _canopy_stats(out_path)
    meta = {
        "source": "ETH Global Canopy Height 2020, 10 m",
        "record": ETH_RESEARCH_RECORD,
        "download_share": "https://libdrive.ethz.ch/index.php/s/%s" % ETH_SHARE_TOKEN,
        "tile_naming": "3deg_cogs/ETH_GlobalCanopyHeight_10m_2020_<N|S><lat><E|W><lon>_Map.tif",
        "raw_tiles": sources,
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "grid_crs": working_crs,
        "raster": os.path.basename(out_path),
        "license": "Creative Commons Attribution 4.0 International",
        "stats": stats,
        "fetched_at": _utcnow(),
    }
    json.dump(meta, open(os.path.join(out_dir, "eth_canopy_height_2020_fetch.json"), "w"),
              indent=2)
    return meta


def fetch_sentinel2_imagery(aoi, out_dir, data_dir, footprint, px_per_m=1, alpha2="nato"):
    """Fetch one low-cloud Sentinel-2 L2A scene and assemble R,G,B,NIR Byte GeoTIFF."""
    os.makedirs(out_dir, exist_ok=True)
    bbox = _aoi_wgs_bbox(aoi)
    georef = os.path.join(data_dir, "georef.json")
    working_crs = twin_georef.crs(georef)
    datetime_range = os.environ.get("VEIL_SENTINEL2_DATETIME", S2_DEFAULT_DATETIME)
    band_keys = [("red", "red"), ("green", "green"), ("blue", "blue"), ("nir", "nir")]
    rejected = []
    searched_ranges = []
    seen = set()
    for query_range in _sentinel2_datetime_ranges(datetime_range):
        searched_ranges.append(query_range)
        items = _sentinel2_candidate_items(bbox, query_range)
        for item in items:
            item_id = item["id"]
            if item_id in seen:
                continue
            seen.add(item_id)
            assets = item.get("assets", {})
            safe_id = item_id.replace("/", "_")
            band_paths = []
            try:
                for key, label in band_keys:
                    href = assets[key]["href"]
                    out_band = os.path.join(out_dir, "sentinel2_%s_%s_u16.tif" %
                                            (safe_id, label))
                    _warp_sentinel_band("/vsicurl/" + href, out_band, footprint, working_crs)
                    band_paths.append(out_band)
                quality = _sentinel2_scene_quality(band_paths)
            except Exception as exc:  # noqa: BLE001
                rejected.append(_sentinel2_rejection(item, "warp/read failed: %s" % exc))
                continue
            ok, reason = _sentinel2_scene_passes(quality, item)
            if not ok:
                rejected.append(_sentinel2_rejection(item, reason, quality))
                print(
                    "  reject Sentinel-2 %s: %s (valid %.1f%%, mean %.1f, p98 %.1f)" %
                    (
                        item_id,
                        reason,
                        quality.get("valid_coverage_pct", 0.0),
                        quality.get("visible_mean_byte_avg", 0.0),
                        quality.get("visible_p98_byte_avg", 0.0),
                    ),
                    flush=True,
                )
                continue

            rgbn = os.path.join(out_dir, "sentinel2_%s_rgbnir_byte.tif" % safe_id)
            _write_rgbn_byte(rgbn, band_paths)
            meta = {
                "adapter": "packs/nato/adapters/global.py",
                "source": "Sentinel-2 L2A via Element84 Earth Search",
                "stac": EARTH_SEARCH,
                "collection": S2_COLLECTION,
                "datetime_query": datetime_range,
                "datetime_ranges_searched": searched_ranges,
                "item_id": item_id,
                "datetime": item.get("properties", {}).get("datetime"),
                "eo_cloud_cover": item.get("properties", {}).get("eo:cloud_cover"),
                "scene_quality": quality,
                "rejected_candidates": rejected,
                "bbox_wgs84": [round(v, 8) for v in bbox],
                "target_crs": working_crs,
                "footprint": [round(float(v), 3) for v in footprint],
                "px_per_m": int(px_per_m),
                "reflectance_scaling": (
                    "uniform Sentinel-2 stretch for R,G,B,NIR: optional BOA offset "
                    "correction, 0..3000 DN -> Byte 0..254, 255 nodata; display "
                    "brightening happens after vegetation analysis in packs/nato/display.py"
                ),
                "band_order": "R,G,B,NIR",
                "assets": {key: assets[key]["href"] for key, _label in band_keys},
                "rgbn": os.path.basename(rgbn),
                "fetched_at": _utcnow(),
            }
            json.dump(meta, open(os.path.join(out_dir, "sentinel2_imagery_fetch.json"), "w"),
                      indent=2)
            return {"rgbn": rgbn, "bands": band_paths, "metadata": meta}
    raise RuntimeError(
        "No Sentinel-2 L2A candidate passed AOI quality checks for %r; rejected %d scenes"
        % (bbox, len(rejected))
    )


def fetch_leaf_type(aoi, out_dir, data_dir, alpha2="nato"):
    """Fetch global forest typing and align it to the twin grid.

    CGLS-LC100 forest type is preferred because it carries evergreen/
    deciduous and needleleaf/broadleaf information. ESA WorldCover remains the
    coarsest fallback when CGLS cannot produce typed cells for the AOI.
    """
    del aoi
    os.makedirs(out_dir, exist_ok=True)
    layer_id = "%s_leaf_type" % (alpha2 or "nato").lower()
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _transform_bounds(bounds, working_crs, "EPSG:4326")
    try:
        cgls = _fetch_cgls_leaf_type(layer_id, out_dir, grid, bounds, working_crs, wgs_bbox)
        typed = cgls["metadata"]["counts"].get("10", 0) \
            + cgls["metadata"]["counts"].get("20", 0) \
            + cgls["metadata"]["counts"].get("30", 0)
        if typed > 0:
            return cgls
        print("  CGLS-LC100 forest type has no typed cells here; using WorldCover mask")
    except Exception as exc:  # noqa: BLE001
        print(f"  CGLS-LC100 forest type unavailable ({exc}); using WorldCover mask")
    return _fetch_worldcover_mask(layer_id, out_dir, grid, bounds, working_crs, wgs_bbox)


def _fetch_cgls_leaf_type(layer_id, out_dir, grid, bounds, working_crs, wgs_bbox):
    raw = os.path.join(out_dir, layer_id + "_cgls_lc100_forest_type_2019_raw.tif")
    forest = os.path.join(out_dir, layer_id + "_cgls_lc100_forest_type_grid.tif")
    _warp_to_grid("/vsicurl/" + CGLS_FOREST_URL, raw, grid, bounds, working_crs)
    stats = _write_cgls_forest_type(raw, forest)
    metadata = {
        "status": "ok",
        "source": "Copernicus Global Land Service LC100 forest type, 2019, 100 m",
        "provider": "Copernicus Global Land Service / VITO",
        "record": CGLS_LC100_RECORD,
        "endpoint": CGLS_FOREST_URL,
        "raw": os.path.basename(raw),
        "raster": os.path.basename(forest),
        "grid_crs": working_crs,
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "raw_classes": {
            "0": "unknown / no typed forest",
            "1": "evergreen needleleaf forest",
            "2": "evergreen broadleaf forest",
            "3": "deciduous needleleaf forest",
            "4": "deciduous broadleaf forest",
            "5": "mixed forest",
            "255": "nodata",
        },
        "classes": {
            "0": "not typed as forest",
            "10": "broadleaf forest fallback (CGLS EBF/DBF)",
            "20": "conifer forest fallback (CGLS ENF/DNF)",
            "30": "mixed forest fallback (CGLS mixed)",
            "255": "nodata",
        },
        "counts": stats,
        "license": "Copernicus Service information 2020",
        "fetched_at": _utcnow(),
    }
    json.dump(metadata, open(os.path.join(out_dir, layer_id + "_cgls_lc100_fetch.json"), "w"),
              indent=2)
    return {
        "raster": forest,
        "raw": raw,
        "layer_id": layer_id,
        "label": "CGLS-LC100 Forest Type",
        "description": (
            "Copernicus Global Land Service LC100 forest type, 2019, 100 m. "
            "Mapped to NATO broadleaf/conifer/mixed fallback classes."
        ),
        "uses": (
            "Global NATO forest typing fallback where continental DLT is unavailable; "
            "coarser than EEA 10 m Dominant Leaf Type."
        ),
        "value_kind": "forest type class",
        "value_unit": "class",
        "value_classification": "categorical",
        "metadata": metadata,
        "attribution": [
            "Copernicus Global Land Service LC100 2019 forest type: Copernicus Service information / VITO."
        ],
    }


def _fetch_worldcover_mask(layer_id, out_dir, grid, bounds, working_crs, wgs_bbox):
    aligned_worldcover = os.path.join(out_dir, layer_id + "_worldcover_2021_grid_raw.tif")
    forest = os.path.join(out_dir, layer_id + "_worldcover_2021_tree_mask_grid.tif")

    sources = _worldcover_sources(wgs_bbox)
    if not sources:
        _write_constant(forest, None, grid, bounds, working_crs, value=0, nodata=255)
        stats = {"0": int(grid["width"]) * int(grid["height"]), "4": 0, "255": 0}
    else:
        vrt = os.path.join(out_dir, layer_id + "_worldcover_2021.vrt")
        gdal.BuildVRT(vrt, sources)
        _warp_to_grid(vrt, aligned_worldcover, grid, bounds, working_crs)
        stats = _write_worldcover_mask(aligned_worldcover, forest)

    metadata = {
        "status": "ok",
        "source": "ESA WorldCover 2021 v200, 10 m",
        "provider": "ESA / VITO",
        "bucket": "s3://esa-worldcover/%s" % WORLD_COVER_PREFIX,
        "http_base": WORLD_COVER_BASE,
        "grid": "3x3 degree WorldCover tiles in EPSG:4326",
        "raw_tiles": sources,
        "grid_crs": working_crs,
        "bbox_wgs84": [round(v, 8) for v in wgs_bbox],
        "raster": os.path.basename(forest),
        "classes": {
            "0": "not tree cover",
            "4": "tree cover / forest mask; global fallback does not distinguish leaf type",
        },
        "counts": stats,
        "license": "ESA WorldCover free and open data",
        "fetched_at": _utcnow(),
    }
    json.dump(metadata, open(os.path.join(out_dir, layer_id + "_worldcover_fetch.json"), "w"),
              indent=2)
    return {
        "raster": forest,
        "raw": aligned_worldcover if sources else None,
        "layer_id": layer_id,
        "label": "ESA WorldCover Tree Cover Mask",
        "description": (
            "ESA WorldCover 2021 v200 tree-cover mask, 10 m. "
            "This global fallback marks forest only; it is not real leaf-type data."
        ),
        "uses": (
            "Global NATO forest mask fallback. Leaf typing is coarse and relies on NIR "
            "fallback when available."
        ),
        "value_kind": "tree cover mask",
        "value_unit": "class",
        "value_classification": "categorical",
        "metadata": metadata,
        "attribution": [
            "ESA WorldCover 2021 v200: European Space Agency / VITO, open data."
        ],
    }


def _aoi_wgs_bbox(aoi):
    if isinstance(aoi, dict):
        bbox = aoi.get("bbox_wgs84") or aoi.get("input_bbox") or aoi.get("bbox")
        crs = aoi.get("bbox_crs") or aoi.get("input_crs") or "EPSG:4326"
    else:
        bbox = aoi
        crs = "EPSG:4326"
    if bbox is None:
        raise ValueError("AOI has no WGS84 bbox")
    bbox = tuple(float(v) for v in bbox)
    if crs and crs.upper() not in ("EPSG:4326", "4326", "WGS84", "WGS 84"):
        return _transform_bounds(bbox, crs, "EPSG:4326")
    return bbox


def _bbox_area_ha_approx(bbox):
    lon0, lat0, lon1, lat1 = bbox
    mid = math.radians((lat0 + lat1) / 2.0)
    width = abs(lon1 - lon0) * 111_320.0 * max(0.1, math.cos(mid))
    height = abs(lat1 - lat0) * 110_574.0
    return width * height / 10_000.0


def _infer_data_dir(out_dir):
    marker = os.path.join("source", "nato")
    parts = os.path.abspath(out_dir).split(os.sep)
    for idx in range(len(parts) - 1):
        if parts[idx] == "source" and idx + 1 < len(parts) and parts[idx + 1] == "nato":
            return os.sep.join(parts[:idx]) or os.sep
    if marker in os.path.abspath(out_dir):
        return os.path.abspath(out_dir).split(marker)[0].rstrip(os.sep)
    return None


def _utm_crs_for_bbox(bbox):
    lon = (bbox[0] + bbox[2]) / 2.0
    lat = (bbox[1] + bbox[3]) / 2.0
    zone = int((lon + 180.0) // 6.0) + 1
    return "EPSG:%d" % ((32600 if lat >= 0 else 32700) + zone)


def _glo30_sources(wgs_bbox):
    lon0, lat0, lon1, lat1 = wgs_bbox
    lon_min = int(math.floor(lon0))
    lon_max = int(math.floor(lon1))
    lat_min = int(math.floor(lat0))
    lat_max = int(math.floor(lat1))
    sources = []
    for lat in range(lat_min, lat_max + 1):
        for lon in range(lon_min, lon_max + 1):
            url = _glo30_tile_url(lat, lon)
            if _url_exists(url):
                sources.append("/vsicurl/" + url)
    return sources


def _glo30_tile_url(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    stem = "Copernicus_DSM_COG_10_%s%02d_00_%s%03d_00_DEM" % (
        ns, abs(int(lat)), ew, abs(int(lon)))
    return "%s/%s/%s.tif" % (GLO30_BASE, stem, stem)


def _eth_sources(wgs_bbox):
    lon0, lat0, lon1, lat1 = wgs_bbox
    lon_min = int(math.floor(lon0 / 3.0) * 3)
    lon_max = int(math.floor(lon1 / 3.0) * 3)
    lat_min = int(math.floor(lat0 / 3.0) * 3)
    lat_max = int(math.floor(lat1 / 3.0) * 3)
    sources = []
    for lat in range(lat_min, lat_max + 1, 3):
        for lon in range(lon_min, lon_max + 1, 3):
            url = _eth_tile_url(lat, lon)
            if _url_exists(url):
                sources.append("/vsicurl/" + url)
    return sources


def _eth_tile_url(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    fname = "ETH_GlobalCanopyHeight_10m_2020_%s%02d%s%03d_Map.tif" % (
        ns, abs(int(lat)), ew, abs(int(lon)))
    return "%s?%s" % (ETH_DOWNLOAD, urllib.parse.urlencode({
        "path": ETH_TILE_DIR,
        "files": fname,
    }))


def _warp_float_to_grid(src_path, out_path, grid, bounds, working_crs):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    gdal.Warp(
        out_path,
        src_path,
        dstSRS=_srs(working_crs).ExportToWkt(),
        outputBounds=bounds,
        width=int(grid["width"]),
        height=int(grid["height"]),
        resampleAlg="bilinear",
        outputType=gdal.GDT_Float32,
        dstNodata=-99999,
        multithread=True,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    return out_path


def _warp_float_to_template(src_path, out_path, template_path):
    template = gdal.Open(template_path)
    if template is None:
        raise RuntimeError("cannot align terrain to canopy template %r" % template_path)
    if _raster_matches_template(out_path, template):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    if os.path.exists(out_path):
        os.remove(out_path)
    gdal.Warp(
        out_path,
        src_path,
        dstSRS=template.GetProjection(),
        outputBounds=_dataset_bounds(template),
        width=template.RasterXSize,
        height=template.RasterYSize,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Float32,
        dstNodata=-99999,
        multithread=True,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
    )
    return out_path


def _raster_matches_template(path, template):
    if not os.path.exists(path):
        return False
    try:
        ds = gdal.Open(path)
        if ds is None:
            return False
        if ds.RasterXSize != template.RasterXSize or ds.RasterYSize != template.RasterYSize:
            return False
        if ds.GetProjection() != template.GetProjection():
            return False
        return all(
            abs(float(a) - float(b)) <= 0.05
            for a, b in zip(_dataset_bounds(ds), _dataset_bounds(template))
        )
    except Exception:  # noqa: BLE001
        return False


def _forest_mask_canopy(data_dir, out_dir, canopy_raw, canopy_masked, forest_type=None,
                        alpha2="nato"):
    alpha = (alpha2 or "nato").lower()
    mask_path = os.path.join(out_dir, "%s_canopy_forest_mask_grid.tif" % alpha)
    source_mask = os.path.join(out_dir, "%s_canopy_forest_mask_source_binary.tif" % alpha)
    source = _select_canopy_mask_source(data_dir, out_dir, forest_type, alpha)
    source_mask_meta = _write_source_binary_forest_mask(
        source["raster"],
        source_mask,
        source["forest_codes"],
        dilation_pixels=CANOPY_MASK_DILATION_PIXELS,
    )
    _warp_byte_to_template(source_mask, mask_path, canopy_raw)
    strict_clip_meta = None
    if source.get("strict_clip_raster"):
        strict_clip = os.path.join(out_dir, "%s_canopy_forest_mask_strict_clip_grid.tif" % alpha)
        _warp_byte_to_template(source["strict_clip_raster"], strict_clip, canopy_raw)
        strict_clip_meta = _apply_strict_forest_clip(
            mask_path,
            strict_clip,
            source["strict_clip_codes"],
        )
    mask_meta = _binary_mask_stats(mask_path)
    if strict_clip_meta:
        mask_meta["strict_clip"] = strict_clip_meta
    canopy_meta = _write_masked_canopy(canopy_raw, mask_path, canopy_masked)
    return {
        "source": source["source"],
        "source_raster": os.path.relpath(source["raster"], data_dir),
        "source_precedence": source["precedence"],
        "forest_codes": [int(v) for v in source["forest_codes"]],
        "dilation_stage": "source_raster_before_canopy_grid_alignment",
        "dilation_pixels": CANOPY_MASK_DILATION_PIXELS,
        "source_binary_mask": os.path.relpath(source_mask, data_dir),
        "mask_raster": os.path.relpath(mask_path, data_dir),
        "masked_canopy_raster": os.path.relpath(canopy_masked, data_dir),
        "source_mask": source_mask_meta,
        "mask": mask_meta,
        "canopy": canopy_meta,
    }


def _select_canopy_mask_source(data_dir, out_dir, forest_type, alpha):
    if forest_type and _is_dlt_layer(forest_type):
        return {
            "source": "Copernicus HRL Dominant Leaf Type 2018",
            "raster": forest_type.get("raw") or forest_type["raster"],
            "forest_codes": DLT_FOREST_CODES,
            "strict_clip_raster": forest_type["raster"],
            "strict_clip_codes": DLT_FOREST_CODES,
            "precedence": "dlt",
        }

    worldcover = _worldcover_canopy_mask_layer(data_dir, out_dir, alpha)
    if worldcover:
        return {
            "source": "ESA WorldCover 2021 v200 tree cover",
            "raster": worldcover["raster"],
            "forest_codes": WORLD_COVER_FOREST_CODES,
            "precedence": "worldcover",
        }

    if forest_type and _is_cgls_layer(forest_type):
        return {
            "source": "Copernicus Global Land Service LC100 forest type 2019",
            "raster": forest_type["raster"],
            "forest_codes": CGLS_FOREST_CODES,
            "precedence": "cgls",
        }

    if forest_type and forest_type.get("raster"):
        return {
            "source": (forest_type.get("metadata") or {}).get("source") or forest_type.get("label"),
            "raster": forest_type["raster"],
            "forest_codes": DLT_FOREST_CODES + CGLS_FOREST_CODES + (NATO_FOREST,),
            "precedence": "generic_leaf_type",
        }

    raise RuntimeError("global canopy masking requires DLT, WorldCover, or CGLS forest coverage")


def _worldcover_canopy_mask_layer(data_dir, out_dir, alpha):
    grid = _grid(data_dir)
    bounds, working_crs = _grid_bounds_abs(data_dir, grid)
    wgs_bbox = _transform_bounds(bounds, working_crs, "EPSG:4326")
    if not _worldcover_sources(wgs_bbox):
        return None
    return _fetch_worldcover_mask(
        "%s_canopy_mask_worldcover" % alpha,
        out_dir,
        grid,
        bounds,
        working_crs,
        wgs_bbox,
    )


def _is_dlt_layer(layer):
    text = _layer_text(layer)
    return "dominant leaf type" in text or "_eea_dlt_" in text


def _is_cgls_layer(layer):
    text = _layer_text(layer)
    return "cgls" in text or "lc100" in text or "forest-type-layer" in text


def _layer_text(layer):
    if not layer:
        return ""
    metadata = layer.get("metadata") or {}
    parts = [
        layer.get("label"),
        layer.get("layer_id"),
        layer.get("raster"),
        metadata.get("source"),
        metadata.get("provider"),
        metadata.get("endpoint"),
    ]
    return " ".join(str(v).lower() for v in parts if v)


def _warp_byte_to_template(src_path, out_path, template_path):
    template = gdal.Open(template_path)
    src = gdal.Open(src_path)
    if template is None or src is None:
        raise RuntimeError("cannot align forest mask source %r to canopy grid" % src_path)
    options = {
        "dstSRS": template.GetProjection(),
        "outputBounds": _dataset_bounds(template),
        "width": template.RasterXSize,
        "height": template.RasterYSize,
        "resampleAlg": "near",
        "outputType": gdal.GDT_Byte,
        "dstNodata": 255,
        "multithread": True,
        "creationOptions": ["COMPRESS=DEFLATE", "TILED=YES"],
    }
    nodata = src.GetRasterBand(1).GetNoDataValue()
    if nodata is not None and np.isfinite(nodata):
        options["srcNodata"] = nodata
    if os.path.exists(out_path):
        os.remove(out_path)
    gdal.Warp(out_path, src_path, **options)
    return out_path


def _write_source_binary_forest_mask(src_path, out_path, forest_codes, dilation_pixels=1):
    src = gdal.Open(src_path)
    if src is None:
        raise RuntimeError("cannot build canopy forest mask from %r" % src_path)
    band = src.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.uint16)
    nodata = band.GetNoDataValue()
    valid = np.ones(arr.shape, dtype=bool)
    if nodata is not None and np.isfinite(nodata):
        valid &= arr != int(nodata)
    forest = np.isin(arr, list(forest_codes)) & valid
    dilated = _dilate_binary(forest, dilation_pixels)
    if nodata is not None and np.isfinite(nodata):
        dilated &= valid
    mask = dilated.astype(np.uint8)

    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(out_path):
        os.remove(out_path)
    ds = drv.Create(out_path, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(src.GetGeoTransform())
    ds.SetProjection(src.GetProjection())
    ds.GetRasterBand(1).WriteArray(mask)
    ds.GetRasterBand(1).SetNoDataValue(255)
    ds.FlushCache()
    ds = None

    values, counts = np.unique(arr, return_counts=True)
    source_counts = {str(int(v)): int(c) for v, c in zip(values, counts)}
    return {
        "source_counts": source_counts,
        "forest_cells_before_dilation": int(forest.sum()),
        "forest_cells_after_dilation": int(mask.sum()),
        "nonforest_cells_after_dilation": int(mask.size - mask.sum()),
    }


def _binary_mask_stats(path):
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError("cannot read canopy forest mask %r" % path)
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    valid = arr != 255
    forest = arr == 1
    return {
        "forest_cells": int(forest.sum()),
        "nonforest_cells": int((valid & ~forest).sum()),
        "nodata_cells": int((~valid).sum()),
    }


def _apply_strict_forest_clip(mask_path, clip_path, forest_codes):
    mask_ds = gdal.Open(mask_path, gdal.GA_Update)
    clip_ds = gdal.Open(clip_path)
    if mask_ds is None or clip_ds is None:
        raise RuntimeError("cannot apply strict forest clip to %r" % mask_path)
    mask_band = mask_ds.GetRasterBand(1)
    mask = mask_band.ReadAsArray().astype(np.uint8)
    clip_band = clip_ds.GetRasterBand(1)
    clip = clip_band.ReadAsArray().astype(np.uint16)
    nodata = clip_band.GetNoDataValue()
    valid = np.ones(clip.shape, dtype=bool)
    if nodata is not None and np.isfinite(nodata):
        valid &= clip != int(nodata)
    strict_forest = np.isin(clip, list(forest_codes)) & valid
    before = int((mask == 1).sum())
    mask = np.where((mask == 1) & strict_forest, 1, 0).astype(np.uint8)
    mask_band.WriteArray(mask)
    mask_band.SetNoDataValue(255)
    mask_ds.FlushCache()
    mask_ds = None
    return {
        "forest_cells_before_clip": before,
        "forest_cells_after_clip": int((mask == 1).sum()),
        "removed_cells": int(before - (mask == 1).sum()),
        "rule": "DLT 0 remains authoritative no-tree cover after dilation",
    }


def _dilate_binary(mask, pixels):
    out = mask.astype(bool)
    for _ in range(max(0, int(pixels))):
        padded = np.pad(out, 1, mode="constant", constant_values=False)
        expanded = np.zeros_like(out, dtype=bool)
        for dy in range(3):
            for dx in range(3):
                expanded |= padded[dy:dy + out.shape[0], dx:dx + out.shape[1]]
        out = expanded
    return out


def _write_masked_canopy(canopy_path, mask_path, out_path):
    canopy_ds = gdal.Open(canopy_path)
    mask_ds = gdal.Open(mask_path)
    if canopy_ds is None or mask_ds is None:
        raise RuntimeError("cannot apply forest mask to canopy %r" % canopy_path)
    canopy_band = canopy_ds.GetRasterBand(1)
    canopy = canopy_band.ReadAsArray().astype(np.float32)
    nodata = canopy_band.GetNoDataValue()
    bad = ~np.isfinite(canopy)
    if nodata is not None and np.isfinite(nodata):
        bad |= canopy == nodata
    canopy[bad] = 0.0
    canopy = np.clip(canopy, 0.0, 100.0).astype(np.float32)
    forest = mask_ds.GetRasterBand(1).ReadAsArray().astype(np.uint8) == 1
    masked = np.where(forest, canopy, 0.0).astype(np.float32)

    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(out_path):
        os.remove(out_path)
    ds = drv.Create(out_path, canopy_ds.RasterXSize, canopy_ds.RasterYSize, 1, gdal.GDT_Float32,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(canopy_ds.GetGeoTransform())
    ds.SetProjection(canopy_ds.GetProjection())
    ds.GetRasterBand(1).WriteArray(masked)
    ds.GetRasterBand(1).SetNoDataValue(-99999)
    ds.FlushCache()
    ds = None

    positive = canopy > 0.0
    return {
        "raw_positive_cells": int(positive.sum()),
        "masked_positive_cells": int((masked > 0.0).sum()),
        "zeroed_positive_cells": int((positive & ~forest).sum()),
        "raw_stats": _array_stats(canopy),
        "masked_stats": _array_stats(masked),
    }


def _array_stats(arr):
    vals = arr[np.isfinite(arr)]
    if vals.size == 0:
        return {}
    return {
        "mean": round(float(vals.mean()), 2),
        "p90": round(float(np.percentile(vals, 90)), 2),
        "max": round(float(vals.max()), 2),
        "canopy_cover_gt5_pct": round(100.0 * float((vals > 5.0).mean()), 1),
    }


def _dataset_bounds(ds):
    gt = ds.GetGeoTransform()
    pts = []
    for px, py in ((0, 0), (ds.RasterXSize, 0), (ds.RasterXSize, ds.RasterYSize),
                   (0, ds.RasterYSize)):
        pts.append((gt[0] + px * gt[1] + py * gt[2],
                    gt[3] + px * gt[4] + py * gt[5]))
    xs, ys = zip(*pts)
    return (min(xs), min(ys), max(xs), max(ys))


def _write_dsm_and_chm(dtm_path, canopy_path, dsm_path, chm_path):
    dtm_ds = gdal.Open(dtm_path)
    canopy_ds = gdal.Open(canopy_path)
    dtm = dtm_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    canopy = canopy_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    nodata = canopy_ds.GetRasterBand(1).GetNoDataValue()
    bad = ~np.isfinite(canopy)
    if nodata is not None and np.isfinite(nodata):
        bad |= canopy == nodata
    canopy[bad] = 0.0
    canopy = np.clip(canopy, 0.0, 100.0).astype(np.float32)
    dsm = (dtm + canopy).astype(np.float32)

    drv = gdal.GetDriverByName("GTiff")
    for path, arr in ((chm_path, canopy), (dsm_path, dsm)):
        ds = drv.Create(path, dtm_ds.RasterXSize, dtm_ds.RasterYSize, 1, gdal.GDT_Float32,
                        options=["COMPRESS=DEFLATE", "TILED=YES"])
        ds.SetGeoTransform(dtm_ds.GetGeoTransform())
        ds.SetProjection(dtm_ds.GetProjection())
        ds.GetRasterBand(1).WriteArray(arr)
        ds.GetRasterBand(1).SetNoDataValue(-99999)
        ds.FlushCache()
        ds = None


def _canopy_stats(path):
    ds = gdal.Open(path)
    arr = ds.GetRasterBand(1).ReadAsArray().astype(float)
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    mask = np.isfinite(arr)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != nodata
    vals = arr[mask]
    if vals.size == 0:
        return {}
    return {
        "mean": round(float(vals.mean()), 2),
        "p90": round(float(np.percentile(vals, 90)), 2),
        "max": round(float(vals.max()), 2),
        "canopy_cover_gt5_pct": round(100.0 * float((vals > 5.0).mean()), 1),
    }


def _pixel_size(path):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    return round(float((abs(gt[1]) + abs(gt[5])) / 2.0), 3)


def _sentinel2_datetime_ranges(primary):
    ranges = []
    for candidate in (primary,) + S2_FALLBACK_DATETIMES:
        if candidate and candidate not in ranges:
            ranges.append(candidate)
    return ranges


def _sentinel2_candidate_items(bbox, datetime_range):
    body = {
        "collections": [S2_COLLECTION],
        "bbox": [float(v) for v in bbox],
        "datetime": datetime_range,
        "limit": S2_CANDIDATE_LIMIT,
        "query": {"eo:cloud_cover": {"lt": S2_MAX_CLOUD_COVER}},
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    url = EARTH_SEARCH.rstrip("/") + "/search"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "veil/1.0 (+packs/nato Sentinel-2)",
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        items = json.loads(resp.read().decode("utf-8")).get("features", [])
    needed = {"red", "green", "blue", "nir"}
    usable = [it for it in items if needed.issubset(set(it.get("assets", {})))]
    usable.sort(key=lambda it: (
        float(it.get("properties", {}).get("eo:cloud_cover") or 999),
        it.get("properties", {}).get("datetime") or "",
    ))
    return usable


def _sentinel2_scene_quality(band_paths):
    arrays = []
    masks = []
    for path in band_paths:
        ds = gdal.Open(path)
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray().astype(np.float32)
        nodata = band.GetNoDataValue()
        valid = np.isfinite(arr)
        if nodata is not None and np.isfinite(nodata):
            valid &= arr != nodata
        arrays.append(arr)
        masks.append(valid)
    if not arrays:
        return {}
    valid = np.logical_and.reduce(masks)
    total = int(valid.size)
    valid_count = int(valid.sum())
    if valid_count == 0:
        return {
            "valid_coverage_pct": 0.0,
            "nonzero_coverage_pct": 0.0,
            "visible_mean_byte": [0.0, 0.0, 0.0],
            "visible_p98_byte": [0.0, 0.0, 0.0],
            "visible_mean_byte_avg": 0.0,
            "visible_p98_byte_avg": 0.0,
        }
    apply_boa_offset = _sentinel2_should_apply_boa_offset(arrays, masks)
    visible = np.stack(arrays[:3], axis=2)
    if apply_boa_offset:
        visible = np.maximum(visible - S2_BOA_ADD_OFFSET_DN, 0.0)
    visible_byte = np.clip(
        np.rint(visible * (254.0 / S2_REFLECTANCE_BYTE_CLIP_DN)),
        0,
        S2_BYTE_NODATA - 1,
    ).astype(np.uint8)
    vals = visible_byte[valid].astype(np.float32)
    nonzero = valid & (np.max(visible_byte, axis=2) > S2_NONZERO_BYTE)
    mean = vals.mean(axis=0)
    p98 = np.percentile(vals, 98, axis=0)
    return {
        "valid_coverage_pct": round(100.0 * valid_count / total, 3) if total else 0.0,
        "nonzero_coverage_pct": round(100.0 * int(nonzero.sum()) / total, 3) if total else 0.0,
        "valid_pixels": valid_count,
        "total_pixels": total,
        "boa_offset_correction": bool(apply_boa_offset),
        "visible_mean_byte": [round(float(v), 2) for v in mean],
        "visible_p98_byte": [round(float(v), 2) for v in p98],
        "visible_mean_byte_avg": round(float(mean.mean()), 2),
        "visible_p98_byte_avg": round(float(p98.mean()), 2),
    }


def _sentinel2_should_apply_boa_offset(arrays, masks):
    visible_p2 = []
    visible_p50 = []
    for arr, valid in zip(arrays[:3], masks[:3]):
        vals = arr[valid]
        if vals.size:
            visible_p2.append(float(np.percentile(vals, 2)))
            visible_p50.append(float(np.percentile(vals, 50)))
    return (
        len(visible_p2) == 3 and
        min(visible_p2) > 700.0 and
        min(visible_p50) > 1300.0
    )


def _sentinel2_scene_passes(quality, item):
    cloud = item.get("properties", {}).get("eo:cloud_cover")
    if cloud is None or float(cloud) > S2_MAX_CLOUD_COVER:
        return False, "cloud cover is above low-cloud threshold"
    if quality.get("valid_coverage_pct", 0.0) < 100.0 * S2_MIN_VALID_COVERAGE:
        return False, "insufficient valid AOI coverage"
    if quality.get("nonzero_coverage_pct", 0.0) < 100.0 * S2_MIN_NONZERO_COVERAGE:
        return False, "insufficient non-near-zero AOI coverage"
    if quality.get("visible_mean_byte_avg", 0.0) < S2_MIN_VISIBLE_MEAN_BYTE:
        return False, "visible AOI mean is near-black"
    if quality.get("visible_p98_byte_avg", 0.0) < S2_MIN_VISIBLE_P98_BYTE:
        return False, "visible AOI p98 is near-black"
    return True, "ok"


def _sentinel2_rejection(item, reason, quality=None):
    props = item.get("properties", {})
    return {
        "item_id": item.get("id"),
        "datetime": props.get("datetime"),
        "eo_cloud_cover": props.get("eo:cloud_cover"),
        "reason": reason,
        "quality": quality or {},
    }


def _warp_sentinel_band(src_path, out_path, bounds, working_crs):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    gdal.Warp(
        out_path,
        src_path,
        dstSRS=_srs(working_crs).ExportToWkt(),
        outputBounds=tuple(float(v) for v in bounds),
        xRes=10.0,
        yRes=10.0,
        resampleAlg="bilinear",
        outputType=gdal.GDT_UInt16,
        srcNodata=0,
        dstNodata=0,
        multithread=True,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    return out_path


def _write_rgbn_byte(out_path, band_paths):
    src = gdal.Open(band_paths[0])
    band_arrays = []
    for path in band_paths:
        band_ds = gdal.Open(path)
        band = band_ds.GetRasterBand(1)
        arr = band.ReadAsArray().astype(np.float32)
        nodata = band.GetNoDataValue()
        valid = np.isfinite(arr)
        if nodata is not None and np.isfinite(nodata):
            valid &= arr != nodata
        band_arrays.append((arr, valid))

    visible_p2 = []
    visible_p50 = []
    for arr, valid in band_arrays[:3]:
        vals = arr[valid]
        if vals.size:
            visible_p2.append(float(np.percentile(vals, 2)))
            visible_p50.append(float(np.percentile(vals, 50)))
    apply_boa_offset = (
        len(visible_p2) == 3 and
        min(visible_p2) > 700.0 and
        min(visible_p50) > 1300.0
    )

    arrays = []
    for arr, valid in band_arrays:
        corrected = arr
        if apply_boa_offset:
            corrected = np.maximum(corrected - S2_BOA_ADD_OFFSET_DN, 0.0)
        byte = np.clip(
            np.rint(corrected * (254.0 / S2_REFLECTANCE_BYTE_CLIP_DN)),
            0,
            S2_BYTE_NODATA - 1,
        ).astype(np.uint8)
        byte[~valid] = S2_BYTE_NODATA
        arrays.append(byte)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, src.RasterXSize, src.RasterYSize, 4, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE", "TILED=YES", "INTERLEAVE=PIXEL"])
    ds.SetGeoTransform(src.GetGeoTransform())
    ds.SetProjection(src.GetProjection())
    for idx, arr in enumerate(arrays, start=1):
        band = ds.GetRasterBand(idx)
        band.WriteArray(arr)
        band.SetNoDataValue(S2_BYTE_NODATA)
    ds.FlushCache()
    ds = None
    return out_path


def _write_cgls_forest_type(src_path, out_path):
    src = gdal.Open(src_path)
    arr = src.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    out = np.zeros_like(arr, dtype=np.uint8)
    out[np.isin(arr, [2, 4])] = 10
    out[np.isin(arr, [1, 3])] = 20
    out[arr == 5] = 30
    out[arr == 255] = 255
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(src.GetGeoTransform())
    ds.SetProjection(src.GetProjection())
    ds.GetRasterBand(1).WriteArray(out)
    ds.GetRasterBand(1).SetNoDataValue(255)
    ds.FlushCache()
    ds = None
    return {str(code): int((out == code).sum()) for code in (0, 10, 20, 30, 255)}


def _grid(data_dir):
    return json.load(open(os.path.join(data_dir, "terrain", "grid.json")))


def _grid_bounds_abs(data_dir, grid):
    georef = os.path.join(data_dir, "georef.json")
    ox, oy = twin_georef.origin(georef)
    return (
        grid["outerMinX"] + ox,
        grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox,
        grid["outerMaxY"] + oy,
    ), twin_georef.crs(georef)


def _transform_bounds(bounds, src_crs, dst_crs):
    to_dst = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    x0, y0, x1, y1 = bounds
    pts = [to_dst.transform(x, y) for x, y in
           ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    xs, ys = zip(*pts)
    return (min(xs), min(ys), max(xs), max(ys))


def _worldcover_sources(wgs_bbox):
    lon0, lat0, lon1, lat1 = wgs_bbox
    lon_min = int(math.floor(lon0 / 3.0) * 3)
    lon_max = int(math.floor(lon1 / 3.0) * 3)
    lat_min = int(math.floor(lat0 / 3.0) * 3)
    lat_max = int(math.floor(lat1 / 3.0) * 3)
    sources = []
    for lat in range(lat_min, lat_max + 1, 3):
        for lon in range(lon_min, lon_max + 1, 3):
            url = _tile_url(lat, lon)
            if _url_exists(url):
                sources.append("/vsicurl/" + url)
    return sources


def _tile_url(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    tile = "%s%02d%s%03d" % (ns, abs(lat), ew, abs(lon))
    return (
        "%s/%s/ESA_WorldCover_10m_%d_%s_%s_Map.tif"
        % (WORLD_COVER_BASE, WORLD_COVER_PREFIX, WORLD_COVER_YEAR,
           WORLD_COVER_VERSION, tile)
    )


def _url_exists(url):
    req = urllib.request.Request(url, method="HEAD", headers={
        "User-Agent": "veil/1.0 (+packs/nato WorldCover)",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def _warp_to_grid(src_path, out_path, grid, bounds, working_crs):
    if _raster_ok(out_path):
        print(f"  reuse {os.path.basename(out_path)}")
        return out_path
    gdal.Warp(
        out_path,
        src_path,
        dstSRS=_srs(working_crs).ExportToWkt(),
        outputBounds=bounds,
        width=int(grid["width"]),
        height=int(grid["height"]),
        resampleAlg="near",
        outputType=gdal.GDT_Byte,
        dstNodata=0,
        creationOptions=["COMPRESS=DEFLATE"],
    )
    return out_path


def _write_worldcover_mask(src_path, out_path):
    src = gdal.Open(src_path)
    arr = src.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    mask = np.where(arr == WORLD_COVER_TREE, NATO_FOREST, 0).astype(np.uint8)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(src.GetGeoTransform())
    ds.SetProjection(src.GetProjection())
    ds.GetRasterBand(1).WriteArray(mask)
    ds.GetRasterBand(1).SetNoDataValue(255)
    ds.FlushCache()
    ds = None
    return {
        "0": int((mask == 0).sum()),
        "4": int((mask == NATO_FOREST).sum()),
        "255": 0,
    }


def _write_constant(out_path, data_dir, grid, bounds, working_crs, value=0, nodata=255):
    del data_dir
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, int(grid["width"]), int(grid["height"]), 1, gdal.GDT_Byte,
                    options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform((bounds[0], (bounds[2] - bounds[0]) / int(grid["width"]), 0.0,
                        bounds[3], 0.0, -((bounds[3] - bounds[1]) / int(grid["height"]))))
    ds.SetProjection(_srs(working_crs).ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((int(grid["height"]), int(grid["width"])),
                                           value, dtype=np.uint8))
    ds.GetRasterBand(1).SetNoDataValue(nodata)
    ds.FlushCache()
    ds = None


def _srs(crs):
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _raster_ok(path):
    try:
        ds = gdal.Open(path)
        return ds is not None and ds.RasterCount > 0 and ds.RasterXSize > 0 and ds.RasterYSize > 0
    except Exception:  # noqa: BLE001
        return False


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
