#!/usr/bin/env python3
"""Hydrology influence provider for wildfire scenario runs.

This module translates whatever hydrology evidence a twin carries onto the fire
grid as two additive signals:

  * hydro_barrier_mask: cells that should be forced to ROS=0.
  * moisture arrays: scenario fuel moistures lifted toward wet targets per cell.

It deliberately does not alter wildfire physics. The caller passes the returned
moisture arrays into twin_fire.rothermel_ros(), then zeroes ROS on the barrier
mask before minimum-travel-time propagation.
"""

import json
import math
import os
import re

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()


MOISTURE_KEYS = ("dead_1h", "dead_10h", "dead_100h", "live_herb", "live_woody")

DROUGHT_SCALING = {
    "normal": 1.0,
    "dry": 0.75,
    "severe": 0.45,
    "extreme": 0.20,
}

WET_TARGETS = {
    # dead fuels and live fuels are dry-weight fractions.
    1: {"label": "riparian_moist", "dead_1h": 0.09, "dead_10h": 0.10,
        "dead_100h": 0.12, "live_herb": 0.90, "live_woody": 1.20},
    2: {"label": "wet", "dead_1h": 0.10, "dead_10h": 0.12,
        "dead_100h": 0.15, "live_herb": 1.00, "live_woody": 1.30},
    3: {"label": "very_wet", "dead_1h": 0.14, "dead_10h": 0.18,
        "dead_100h": 0.22, "live_herb": 1.20, "live_woody": 1.50},
    4: {"label": "saturated", "dead_1h": 0.60, "dead_10h": 0.60,
        "dead_100h": 0.60, "live_herb": 1.50, "live_woody": 1.80},
}

NOTES = [
    "screening-grade hydrology influence, not a calibrated fire forecast",
    "wetlands are moisture dampers, not automatic hard barriers",
    "dry marsh or peat can still burn in drought; wetland damping is drought-scaled",
    "narrow stream lines without width are treated as wet corridors, not hard firebreaks",
    "static ponding depressions become hard barriers only under normal moisture; drought keeps them as dampers",
    "spotting and crown-fire lofting can cross water/wet barriers; scenario runs flag ember exposure separately",
]


def _grid_shape(grid):
    return int(grid["height"]), int(grid["width"])


def _footprint(grid):
    dem = grid.get("dem")
    if dem is None:
        return np.ones(_grid_shape(grid), dtype=bool)
    return np.isfinite(dem)


def _scenario_arrays(shape, scenario_moisture):
    arrays = {}
    for key in MOISTURE_KEYS:
        if key not in scenario_moisture:
            raise KeyError("scenario_moisture is missing %s" % key)
        arrays[key] = np.broadcast_to(
            np.asarray(scenario_moisture[key], dtype=float), shape
        ).astype(float, copy=True)
    return arrays


def _rel(data_dir, path):
    try:
        return os.path.relpath(path, data_dir)
    except ValueError:
        return path


def _as_float_grid(rows):
    return np.array(
        [[np.nan if v is None else float(v) for v in row] for row in rows],
        dtype=float,
    )


def _target_centers(grid):
    h, w = _grid_shape(grid)
    xs = grid["minX"] + np.arange(w, dtype=float) * grid["xstep"]
    ys = grid["maxY"] - np.arange(h, dtype=float) * grid["ystep"]
    return np.meshgrid(xs, ys)


def _resample_grid_json(payload, grid):
    arr = _as_float_grid(payload.get("values") or [])
    if arr.size == 0:
        return np.full(_grid_shape(grid), np.nan, dtype=float)
    h, w = _grid_shape(grid)
    if arr.shape == (h, w):
        return arr
    if arr.shape == (1, 1):
        return np.full((h, w), float(arr[0, 0]), dtype=float)

    bounds = payload.get("bounds_local") or payload.get("bounds")
    if not bounds or len(bounds) != 4:
        return np.full((h, w), np.nan, dtype=float)
    min_x, min_y, max_x, max_y = [float(v) for v in bounds]
    src_h, src_w = arr.shape
    xres = (max_x - min_x) / max(1, src_w)
    yres = (max_y - min_y) / max(1, src_h)
    if not (math.isfinite(xres) and math.isfinite(yres) and xres > 0 and yres > 0):
        return np.full((h, w), np.nan, dtype=float)

    x, y = _target_centers(grid)
    col = np.floor((x - min_x) / xres).astype(int)
    row = np.floor((max_y - y) / yres).astype(int)
    valid = (row >= 0) & (row < src_h) & (col >= 0) & (col < src_w)
    out = np.full((h, w), np.nan, dtype=float)
    out[valid] = arr[row[valid], col[valid]]
    return out


def _load_grid_layer(data_dir, rel_path, grid):
    path = os.path.join(data_dir, rel_path)
    if not os.path.exists(path):
        return None, None
    with open(path) as fh:
        payload = json.load(fh)
    return _resample_grid_json(payload, grid), path


def _srs_epsg(code):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(code))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _analysis_srs(georef):
    label = str(georef.get("analysis_crs") or "")
    if label.upper().startswith("EPSG:"):
        return _srs_epsg(label.split(":", 1)[1])
    srs = osr.SpatialReference()
    if georef.get("proj4"):
        srs.ImportFromProj4(georef["proj4"])
    else:
        srs.ImportFromEPSG(3857)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _source_srs(path):
    try:
        ds = ogr.Open(path)
        if ds is None:
            return None
        layer = ds.GetLayer(0)
        srs = layer.GetSpatialRef()
        if srs is not None:
            srs = srs.Clone()
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return srs
    except Exception:
        return None


def _coord_extent(geojson):
    xs = []
    ys = []

    def walk(value):
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
                xs.append(float(value[0]))
                ys.append(float(value[1]))
                return
            for child in value:
                walk(child)

    for feature in geojson.get("features") or []:
        geom = feature.get("geometry") or {}
        if geom.get("type") == "GeometryCollection":
            for part in geom.get("geometries") or []:
                walk(part.get("coordinates"))
        else:
            walk(geom.get("coordinates"))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _looks_geographic(extent):
    if extent is None:
        return False
    min_x, min_y, max_x, max_y = extent
    return (-180.0 <= min_x <= 180.0 and -180.0 <= max_x <= 180.0 and
            -90.0 <= min_y <= 90.0 and -90.0 <= max_y <= 90.0)


def _expanded_grid_extent(grid, pad_m):
    return (grid["minX"] - pad_m, grid["minY"] - pad_m,
            grid["maxX"] + pad_m, grid["maxY"] + pad_m)


def _extent_intersects(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _vector_coord_mode(path, geojson, grid, georef):
    extent = _coord_extent(geojson)
    if extent is None:
        return "local"

    # Localized atlas files in this repo may still advertise WGS84 in the
    # GeoJSON CRS metadata. Trust the coordinate values first.
    max_abs = max(abs(v) for v in extent)
    if max_abs < 100000.0:
        if not _looks_geographic(extent):
            return "local"
        if _extent_intersects(extent, _expanded_grid_extent(grid, 20000.0)):
            return "local"
        return "geographic"

    origin = georef.get("origin_utm") or [0.0, 0.0]
    ox, oy = float(origin[0]), float(origin[1])
    projected_window = (ox - 50000.0, oy - 50000.0, ox + 50000.0, oy + 50000.0)
    if _extent_intersects(extent, projected_window):
        return "analysis_projected"

    srs = _source_srs(path)
    if srs is not None:
        if srs.IsGeographic():
            return "geographic"
        return "projected"
    return "local"


def _transformer_for_mode(path, mode, georef):
    origin = georef.get("origin_utm") or [0.0, 0.0]
    ox, oy = float(origin[0]), float(origin[1])
    if mode == "local":
        return lambda x, y: (float(x), float(y))

    analysis = _analysis_srs(georef)
    if mode == "geographic":
        source = _source_srs(path) or _srs_epsg(4326)
    elif mode == "analysis_projected":
        source = analysis
    else:
        source = _source_srs(path) or analysis
    transform = osr.CoordinateTransformation(source, analysis)

    def convert(x, y):
        px, py, _pz = transform.TransformPoint(float(x), float(y))
        return px - ox, py - oy

    return convert


def _map_coords(coords, fn):
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and all(isinstance(v, (int, float)) for v in coords[:2]):
            x, y = fn(coords[0], coords[1])
            return [x, y]
        return [_map_coords(child, fn) for child in coords]
    return coords


def _map_geometry(geom, fn):
    if not geom:
        return None
    gtype = geom.get("type")
    if gtype == "GeometryCollection":
        return {
            "type": "GeometryCollection",
            "geometries": [
                mapped for mapped in (_map_geometry(g, fn) for g in geom.get("geometries") or [])
                if mapped
            ],
        }
    return {"type": gtype, "coordinates": _map_coords(geom.get("coordinates"), fn)}


def _candidate_paths(data_dir, rel_path):
    dirname, basename = os.path.split(rel_path)
    paths = []
    if dirname == "atlas":
        paths.append(os.path.join(data_dir, "atlas", "local", basename))
    paths.append(os.path.join(data_dir, rel_path))
    seen = set()
    out = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _load_geojson_features(data_dir, rel_path, grid):
    path = next((p for p in _candidate_paths(data_dir, rel_path) if os.path.exists(p)), None)
    if path is None:
        return [], None
    with open(path) as fh:
        geojson = json.load(fh)
    georef_path = os.path.join(data_dir, "georef.json")
    georef = json.load(open(georef_path)) if os.path.exists(georef_path) else {}
    mode = _vector_coord_mode(path, geojson, grid, georef)
    convert = _transformer_for_mode(path, mode, georef)

    features = []
    for feature in geojson.get("features") or []:
        geom = _map_geometry(feature.get("geometry"), convert)
        if not geom:
            continue
        props = dict(feature.get("properties") or {})
        features.append({"geometry": geom, "properties": props})
    return features, {"path": path, "coord_mode": mode, "feature_count": len(features)}


def _ogr_geom(geom_json):
    if not geom_json:
        return None
    geom = ogr.CreateGeometryFromJson(json.dumps(geom_json))
    if geom is None or geom.IsEmpty():
        return None
    return geom


def _memory_layer(geometries):
    driver = ogr.GetDriverByName("MEM")
    ds = driver.CreateDataSource("")
    layer = ds.CreateLayer("features", None, ogr.wkbUnknown)
    defn = layer.GetLayerDefn()
    for geom_json in geometries:
        geom = _ogr_geom(geom_json)
        if geom is None:
            continue
        feat = ogr.Feature(defn)
        feat.SetGeometry(geom)
        layer.CreateFeature(feat)
        feat = None
    return ds, layer


def _raster(grid, factor=1, dtype=gdal.GDT_Byte):
    h, w = _grid_shape(grid)
    factor = max(1, int(factor))
    ds = gdal.GetDriverByName("MEM").Create("", w * factor, h * factor, 1, dtype)
    ds.SetGeoTransform((
        grid["minX"] - grid["xstep"] / 2.0,
        grid["xstep"] / factor,
        0.0,
        grid["maxY"] + grid["ystep"] / 2.0,
        0.0,
        -grid["ystep"] / factor,
    ))
    return ds


def _rasterize_mask(geometries, grid, all_touched=False, factor=1):
    h, w = _grid_shape(grid)
    if not geometries:
        return np.zeros((h, w), dtype=bool)
    _ds, layer = _memory_layer(geometries)
    raster = _raster(grid, factor=factor)
    options = ["ALL_TOUCHED=TRUE"] if all_touched else []
    gdal.RasterizeLayer(raster, [1], layer, burn_values=[1], options=options)
    arr = raster.ReadAsArray()
    if factor > 1:
        arr = arr.reshape(h, factor, w, factor)
        return arr.mean(axis=(1, 3))
    return arr > 0


def _polygon_mask_and_coverage(geometries, grid):
    if not geometries:
        h, w = _grid_shape(grid)
        return np.zeros((h, w), dtype=bool), np.zeros((h, w), dtype=float)
    center = _rasterize_mask(geometries, grid, all_touched=False, factor=1)
    coverage = _rasterize_mask(geometries, grid, all_touched=False, factor=4)
    return center, coverage


def _buffered_geometries(features, radius_m):
    out = []
    for feature in features:
        geom = _ogr_geom(feature.get("geometry"))
        if geom is None:
            continue
        try:
            buf = geom.Buffer(float(radius_m), 6)
        except Exception:
            continue
        if buf is None or buf.IsEmpty():
            continue
        out.append(json.loads(buf.ExportToJson()))
    return out


def _line_geometries(features):
    return [f["geometry"] for f in features if f.get("geometry")]


def _prop_text(props, names):
    values = []
    for name in names:
        value = props.get(name)
        if value is not None:
            values.append(str(value))
    return " ".join(values).upper()


def _is_nwi_open_water(props):
    label = _prop_text(props, ("NWILABEL", "ATTRIBUTE", "WETLAND_TYPE", "CLASS1", "CLASS2"))
    tokens = re.findall(r"[A-Z0-9]+", label)
    for token in tokens:
        if (token == "OW" or token.startswith("POW") or
                token.startswith("L1OW") or token.startswith("L2OW") or
                (token.startswith("R") and "OW" in token)):
            return True
    return False


def _is_nwi_wetland(props):
    if _is_nwi_open_water(props):
        return False
    system = str(props.get("SYSTEM") or props.get("SYSTEM1") or "").upper()
    label = str(props.get("NWILABEL") or props.get("ATTRIBUTE") or "").upper()
    if system.startswith("U") or label.startswith("U"):
        return False
    return bool(system or label)


def _stream_confidence(props):
    text = _prop_text(props, (
        "FCode_Description", "STANDARD", "CLASSIFICA", "label", "GNIS_Name",
        "__label", "FType", "FCode",
    ))
    return ("PERENNIAL" in text or "C(T)" in text or "PROTECTED" in text or
            "STREAM/RIVER" in text)


def _stream_width_m(props):
    names = (
        "WETTEDWIDTH", "WETTED_WIDTH", "WIDTH", "WIDTH_M", "BANKFULL_W",
        "BANKFULL_WIDTH", "AVG_WIDTH", "MEAN_WIDTH",
    )
    lower = {str(k).upper(): v for k, v in props.items()}
    for name in names:
        value = lower.get(name)
        if value is None:
            continue
        try:
            width = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(width) and width > 0.0:
            return width
    return None


def _soil_water_table_surface(data_dir, grid):
    feat_path = os.path.join(data_dir, "soils", "features.geojson")
    tab_path = os.path.join(data_dir, "soils", "tabular.json")
    if not (os.path.exists(feat_path) and os.path.exists(tab_path)):
        return None, None
    tabular = json.load(open(tab_path)).get("map_units") or {}
    with open(feat_path) as fh:
        geojson = json.load(fh)
    georef_path = os.path.join(data_dir, "georef.json")
    georef = json.load(open(georef_path)) if os.path.exists(georef_path) else {}
    mode = _vector_coord_mode(feat_path, geojson, grid, georef)
    convert = _transformer_for_mode(feat_path, mode, georef)
    geoms = []
    for feature in geojson.get("features") or []:
        props = feature.get("properties") or {}
        mukey = str(props.get("mukey") or "")
        rec = tabular.get(mukey) or {}
        depths = [
            rec.get("water_table_depth_annual_min_cm"),
            rec.get("water_table_depth_apr_jun_min_cm"),
        ]
        saturated = False
        for depth in depths:
            try:
                if depth is not None and float(depth) <= 0.0:
                    saturated = True
            except (TypeError, ValueError):
                pass
        if not saturated:
            continue
        geom = _map_geometry(feature.get("geometry"), convert)
        if geom:
            geoms.append(geom)
    if not geoms:
        return np.zeros(_grid_shape(grid), dtype=bool), {"path": feat_path, "coord_mode": mode}
    center, coverage = _polygon_mask_and_coverage(geoms, grid)
    return center | (coverage >= 0.5), {"path": feat_path, "coord_mode": mode}


def _record_found(provenance, layer_id, path, coord_mode=None):
    item = {"id": layer_id, "path": _rel(provenance["data_dir"], path)}
    if coord_mode:
        item["coord_mode"] = coord_mode
    provenance["sources_found"].append(item)


def _record_used(provenance, layer_id, path, cells, effect, coord_mode=None):
    if int(cells) <= 0:
        return
    item = {
        "id": layer_id,
        "path": _rel(provenance["data_dir"], path),
        "cells": int(cells),
        "effect": effect,
    }
    if coord_mode:
        item["coord_mode"] = coord_mode
    provenance["sources_used"].append(item)


def _apply_signal(wet_level, wet_score, mask, level, score):
    if mask is None:
        return
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return
    score_arr = np.broadcast_to(np.asarray(score, dtype=float), wet_score.shape)
    score_arr = np.clip(score_arr, 0.0, 1.0)
    stronger = m & ((int(level) > wet_level) |
                    ((int(level) == wet_level) & (score_arr > wet_score)))
    wet_level[stronger] = int(level)
    wet_score[stronger] = score_arr[stronger]


def _add_barrier(barrier, mask):
    if mask is None:
        return np.zeros_like(barrier, dtype=bool)
    m = np.asarray(mask, dtype=bool)
    new = m & ~barrier
    barrier |= m
    return new


def _blend_moisture(base_arrays, wet_level, wet_score, drought_scale):
    scaled = np.clip(wet_score * drought_scale, 0.0, 1.0)
    out = {}
    for key in MOISTURE_KEYS:
        base = base_arrays[key]
        target = base.copy()
        for level, values in WET_TARGETS.items():
            target = np.where(wet_level == level, values[key], target)
        blended = base + scaled * (target - base)
        out[key] = np.maximum(base, blended)
    return out, scaled


def _count(mask):
    return int(np.asarray(mask, dtype=bool).sum())


def hydro_fire_influence(data_dir, grid, drought, scenario_moisture):
    """Return (barrier_mask, moisture_arrays, provenance) for the fire grid.

    ``scenario_moisture`` is the scalar scenario moisture dict used by the fire
    scenario. Returned moisture arrays have the same keys but are 2D arrays in
    dry-weight fraction units.
    """
    data_dir = os.path.abspath(data_dir)
    shape = _grid_shape(grid)
    footprint = _footprint(grid)
    base_arrays = _scenario_arrays(shape, scenario_moisture)
    barrier = np.zeros(shape, dtype=bool)
    wet_level = np.zeros(shape, dtype=np.uint8)
    wet_score = np.zeros(shape, dtype=float)
    drought_label = str(drought or "normal").strip().lower()
    drought_scale = DROUGHT_SCALING.get(drought_label, DROUGHT_SCALING["normal"])

    provenance = {
        "data_dir": data_dir,
        "sources_found": [],
        "sources_used": [],
        "barrier_cells_by_source": {},
        "wet_cells_by_source": {},
        "drought": drought_label,
        "drought_scaling": drought_scale,
        "notes": list(NOTES),
    }

    def add_barrier(layer_id, path, mask, effect, coord_mode=None):
        masked = np.asarray(mask, dtype=bool) & footprint
        new = _add_barrier(barrier, masked)
        cells = _count(masked)
        if cells:
            provenance["barrier_cells_by_source"][layer_id] = (
                provenance["barrier_cells_by_source"].get(layer_id, 0) + cells)
        _record_used(provenance, layer_id, path, cells, effect, coord_mode)
        return new

    def add_wet(layer_id, path, mask, level, score, effect, coord_mode=None):
        masked = np.asarray(mask, dtype=bool) & footprint
        _apply_signal(wet_level, wet_score, masked, level, score)
        cells = _count(masked)
        if cells:
            provenance["wet_cells_by_source"][layer_id] = (
                provenance["wet_cells_by_source"].get(layer_id, 0) + cells)
        _record_used(provenance, layer_id, path, cells, effect, coord_mode)

    # 1) Already-rasterized terrain hydrology.
    wetness, path = _load_grid_layer(data_dir, "hydrology/local/wetness_index.grid.json", grid)
    if path:
        _record_found(provenance, "wetness_index", path)
        finite = np.isfinite(wetness)
        rip = finite & (wetness >= 75.0) & (wetness < 90.0)
        wet = finite & (wetness >= 90.0) & (wetness < 95.0)
        very = finite & (wetness >= 95.0)
        add_wet("wetness_index", path, rip, 1, 0.55, "TWI 75-90 percentile riparian moisture")
        add_wet("wetness_index", path, wet, 2, 0.80, "TWI 90-95 percentile wet moisture")
        add_wet("wetness_index", path, very, 3, 1.00, "TWI >95 percentile very-wet moisture")

    ponding, path = _load_grid_layer(data_dir, "hydrology/local/ponding.grid.json", grid)
    if path:
        _record_found(provenance, "ponding", path)
        finite = np.isfinite(ponding)
        shallow = finite & (ponding > 0.0) & (ponding < 0.05)
        saturated = finite & (ponding >= 0.05)
        add_wet("ponding", path, shallow, 3, 0.85, "shallow ponding very-wet moisture")
        add_wet("ponding", path, saturated, 4, 1.00, "ponding >=0.05 m saturated moisture")
        if drought_label == "normal":
            add_barrier("ponding", path, saturated,
                        "ponding >=0.05 m hard barrier under normal moisture")

    seep, path = _load_grid_layer(data_dir, "hydrology/local/seep_candidates.grid.json", grid)
    if path:
        _record_found(provenance, "seep_candidates", path)
        finite = np.isfinite(seep)
        wet = finite & (seep >= 60.0) & (seep < 80.0)
        very = finite & (seep >= 80.0)
        add_wet("seep_candidates", path, wet, 2, 0.80, "seep score 60-80 wet moisture")
        add_wet("seep_candidates", path, very, 3, 1.00, "seep score >80 very-wet moisture")

    flow, path = _load_grid_layer(data_dir, "hydrology/local/flow_paths.grid.json", grid)
    if path:
        _record_found(provenance, "flow_paths", path)
        finite = np.isfinite(flow)
        corridor = finite & (flow >= 0.03)
        strong = finite & (flow >= 0.10)
        add_wet("flow_paths", path, corridor, 1, 0.45, "contributing-area flow path riparian moisture")
        add_wet("flow_paths", path, strong, 1, 0.65, "strong contributing-area flow path riparian moisture")

    # 2) Vector water, wetlands, and streams.
    water_features, info = _load_geojson_features(data_dir, "atlas/nys_hydrography_waterbodies.geojson", grid)
    if info:
        _record_found(provenance, "nys_hydrography_waterbodies", info["path"], info["coord_mode"])
        geoms = [f["geometry"] for f in water_features]
        center, coverage = _polygon_mask_and_coverage(geoms, grid)
        add_barrier(
            "nys_hydrography_waterbodies", info["path"],
            center | (coverage >= 0.5),
            "waterbody polygon center/50pct coverage hard barrier",
            info["coord_mode"],
        )

    nwi_features, info = _load_geojson_features(data_dir, "atlas/nwi_wetlands_uh.geojson", grid)
    if info:
        _record_found(provenance, "nwi_wetlands_uh", info["path"], info["coord_mode"])
        open_water = [f["geometry"] for f in nwi_features if _is_nwi_open_water(f["properties"])]
        wetlands = [f for f in nwi_features if _is_nwi_wetland(f["properties"])]
        center, coverage = _polygon_mask_and_coverage(open_water, grid)
        add_barrier(
            "nwi_open_water", info["path"], center | (coverage >= 0.5),
            "NWI open-water class center/50pct coverage hard barrier",
            info["coord_mode"],
        )
        wet_geoms = [f["geometry"] for f in wetlands]
        center, coverage = _polygon_mask_and_coverage(wet_geoms, grid)
        wet_mask = center | (coverage > 0.0)
        add_wet("nwi_wetlands_uh", info["path"], wet_mask, 2, 1.00,
                "NWI wetland polygon wet moisture", info["coord_mode"])
        edge = _rasterize_mask(_buffered_geometries(wetlands, grid["cellsize"] * 1.5),
                               grid, all_touched=False) & ~wet_mask
        add_wet("nwi_wetland_edge", info["path"], edge, 1, 0.65,
                "NWI wetland edge riparian moisture", info["coord_mode"])

    dec_wet_features, info = _load_geojson_features(
        data_dir, "atlas/dec_informational_freshwater_wetlands.geojson", grid)
    if info:
        _record_found(provenance, "dec_informational_freshwater_wetlands",
                      info["path"], info["coord_mode"])
        geoms = [f["geometry"] for f in dec_wet_features]
        center, coverage = _polygon_mask_and_coverage(geoms, grid)
        wet_mask = center | (coverage > 0.0)
        add_wet("dec_informational_freshwater_wetlands", info["path"], wet_mask, 2, 1.00,
                "DEC freshwater wetland polygon wet moisture", info["coord_mode"])
        edge = _rasterize_mask(
            _buffered_geometries(dec_wet_features, grid["cellsize"] * 1.5),
            grid, all_touched=False) & ~wet_mask
        add_wet("dec_wetland_edge", info["path"], edge, 1, 0.65,
                "DEC wetland edge riparian moisture", info["coord_mode"])

    stream_layers = [
        ("nys_hydrography_flowlines", "atlas/nys_hydrography_flowlines.geojson"),
        ("dec_stream_classifications", "atlas/dec_stream_classifications.geojson"),
    ]
    for layer_id, rel_path in stream_layers:
        features, info = _load_geojson_features(data_dir, rel_path, grid)
        if not info:
            continue
        _record_found(provenance, layer_id, info["path"], info["coord_mode"])
        if not features:
            continue
        corridor_radius = max(grid["cellsize"], 3.0)
        corridor = _rasterize_mask(
            _buffered_geometries(features, corridor_radius),
            grid, all_touched=False)
        confidence = any(_stream_confidence(f["properties"]) for f in features)
        add_wet(layer_id, info["path"], corridor, 1, 0.90 if confidence else 0.70,
                "mapped stream wet corridor moisture", info["coord_mode"])

        wide_geoms = []
        no_width_features = []
        for feature in features:
            width = _stream_width_m(feature["properties"])
            if width is None:
                no_width_features.append(feature)
            elif width >= grid["cellsize"]:
                geom = _ogr_geom(feature["geometry"])
                if geom is not None:
                    buf = geom.Buffer(width / 2.0, 6)
                    if buf is not None and not buf.IsEmpty():
                        wide_geoms.append(json.loads(buf.ExportToJson()))
        if wide_geoms:
            center, coverage = _polygon_mask_and_coverage(wide_geoms, grid)
            add_barrier(layer_id + "_wide", info["path"], center | (coverage >= 0.5),
                        "stream width >= one fire cell hard barrier", info["coord_mode"])
        if no_width_features and drought_label in ("normal", "dry"):
            centerline = _rasterize_mask(_line_geometries(no_width_features), grid,
                                         all_touched=True)
            add_wet(layer_id + "_centerline", info["path"], centerline, 1,
                    0.90 if confidence else 0.70,
                    "no-width stream centerline wet-corridor moisture, not a hard barrier",
                    info["coord_mode"])

    # 3) Soil/snow saturation evidence if present.
    wt_surface, info = _soil_water_table_surface(data_dir, grid)
    if info:
        _record_found(provenance, "soil_water_table_surface", info["path"], info["coord_mode"])
        add_wet("soil_water_table_surface", info["path"], wt_surface, 4, 1.00,
                "soil water table at surface saturated moisture", info["coord_mode"])
        add_barrier("soil_water_table_surface", info["path"], wt_surface,
                    "soil water table at surface hard barrier", info["coord_mode"])

    for layer_id, rel_path, threshold in (
        ("snodas_swe_current_noaa_service", "atlas/local/snodas_swe_current_noaa_service.grid.json", 0.005),
        ("snodas_snow_depth_current_noaa_service", "atlas/local/snodas_snow_depth_current_noaa_service.grid.json", 0.02),
    ):
        arr, path = _load_grid_layer(data_dir, rel_path, grid)
        if path:
            _record_found(provenance, layer_id, path)
            snow = np.isfinite(arr) & (arr > threshold)
            add_wet(layer_id, path, snow, 4, 1.00, "snow/SWE saturated moisture")
            add_barrier(layer_id, path, snow, "snow/SWE hard barrier")

    barrier &= footprint
    moisture_arrays, scaled_score = _blend_moisture(base_arrays, wet_level, wet_score, drought_scale)

    wet_effect = (wet_level > 0) & (scaled_score > 0.0) & footprint & ~barrier
    provenance["barrier_cells"] = _count(barrier)
    provenance["wet_cells"] = _count(wet_effect)
    provenance["wet_cells_by_class"] = {}
    for level, values in WET_TARGETS.items():
        provenance["wet_cells_by_class"][values["label"]] = _count(wet_effect & (wet_level == level))
    provenance["moisture_cells_including_barriers"] = _count((wet_level > 0) & footprint)
    if wet_effect.any():
        provenance["mean_scaled_wetness_score"] = round(float(np.mean(scaled_score[wet_effect])), 4)
        provenance["max_scaled_wetness_score"] = round(float(np.max(scaled_score[wet_effect])), 4)
    else:
        provenance["mean_scaled_wetness_score"] = 0.0
        provenance["max_scaled_wetness_score"] = 0.0
    provenance["screening_grade"] = True

    if not provenance["sources_used"]:
        provenance["notes"].append("no usable hydrology influence layers found; scenario moisture unchanged")

    provenance.pop("data_dir", None)
    return barrier, moisture_arrays, provenance
