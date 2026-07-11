#!/usr/bin/env python3
"""Tier 1 of the hydrology system: terrain hydrology
layers for the Simulation window.

Runs the pure-numpy engine (scripts/twin_hydrology.py) over the twin's LiDAR
terrain grid, joins the SSURGO tabular soil hydraulics fetched by the pack
(data/soils/tabular.json), and exports viewer-ready draped layers + a summary:

  data/hydrology/local/<id>.png + <id>.grid.json   (drape image + identify grid,
                                                    same format as atlas rasters)
  data/hydrology/simulation-layers.json            (catalog the Simulation window
                                                    loads; scenario layers are
                                                    appended by hydro_scenario.py)
  data/hydrology/summary.json                      (stats consumed by the
                                                    scenario engine + UI)

Layers:
  flow_paths        log-scaled upslope contributing area (where water flows)
  wetness_index     TWI percentile (relative soil wetness)
  ponding           depression depth — where water pools, and how deep
  seep_candidates   ranked spring/seep candidacy: TWI x convergence x slope-break
                    x shallow restrictive layer (bedrock 74 cm under the
                    Berkshire-Tunbridge units, 52 cm perched table on Skerry)

Validation gates (printed, and recorded in summary.json):
  - the extracted channel network is compared against the mapped mapped channel +
    tributary lines (mean offset within a couple of cells expected);
  - seep candidates are checked for overlap with NWI wetlands / the mapped pond
    (candidates *should* cluster near them; the interesting ones are the rest).

Every layer is registered in the twin store under a pipeline run (content-hashed,
like other registered inputs/derivations). Deterministic: same inputs -> same
outputs.

Run:  python3 scripts/analyze_hydrology.py [--data-dir DIR]
"""

import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np
from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_georef
import twin_hydrology as hydro
import twin_store
from twin_store import Store

gdal.UseExceptions()

D = os.path.join(PROJECT, "data")
STORE_PATH = os.path.join(D, "twin.gpkg")


def _use_data_dir(data_dir):
    global D, STORE_PATH
    D = os.path.abspath(data_dir)
    STORE_PATH = os.path.join(D, "twin.gpkg")
    twin_store.JOURNAL_DIR = os.path.join(D, "journal")


# ------------------------------------------------------------ raster helpers

def write_png(rgba, out_path):
    h, w, _ = rgba.shape
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    for b in range(4):
        mem.GetRasterBand(b + 1).WriteArray(rgba[:, :, b])
    gdal.GetDriverByName("PNG").CreateCopy(out_path, mem)
    aux = out_path + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)


def ramp(stops, t):
    """Piecewise-linear color ramp; stops = [(r,g,b,a), ...], t in 0..1."""
    x = max(0.0, min(0.999999, float(t))) * (len(stops) - 1)
    i = int(x)
    f = x - i
    a, b = stops[i], stops[i + 1]
    return [int(a[c] + (b[c] - a[c]) * f) for c in range(4)]


def colorize(norm, stops):
    """norm: 2D array in 0..1 (NaN = transparent) -> RGBA uint8 image."""
    h, w = norm.shape
    lut = np.array([ramp(stops, i / 255.0) for i in range(256)], dtype=np.uint8)
    idx = np.clip(np.nan_to_num(norm, nan=0.0) * 255, 0, 255).astype(np.uint8)
    rgba = lut[idx]
    rgba[~np.isfinite(norm)] = [0, 0, 0, 0]
    return rgba


def grid_json(values, bounds, legend, nodata=None, decimals=2, metadata=None):
    """The identify grid format app.js sampleGrid() reads (rows of values)."""
    rows = []
    for r in range(values.shape[0]):
        row = []
        for v in values[r]:
            if isinstance(v, float) and not math.isfinite(v):
                row.append(None)
            elif isinstance(v, float):
                row.append(round(v, decimals))
            else:
                row.append(int(v))
        rows.append(row)
    out = {"bounds_local": bounds, "width": int(values.shape[1]),
           "height": int(values.shape[0]), "nodata": nodata,
           "values": rows, "legend": legend}
    if metadata:
        out.update(metadata)
    return out


def percentile_norm(arr, mask=None):
    """Rank-normalize an array to 0..1 over its finite (and masked-in) cells."""
    m = np.isfinite(arr) if mask is None else (np.isfinite(arr) & mask)
    out = np.full(arr.shape, np.nan)
    vals = arr[m]
    if not vals.size:
        return out
    order = vals.argsort().argsort().astype(float)
    out[m] = order / max(1, len(vals) - 1)
    return out


# ------------------------------------------------------ soils rasterization

def rasterize_polygons(features, grid, prop):
    """Vectorized even-odd rasterization of scene-local polygons onto the
    terrain grid. Returns an object array of `prop` values (None = no polygon)."""
    h, w = grid["height"], grid["width"]
    xs = np.linspace(grid["minX"], grid["maxX"], w)
    ys = np.linspace(grid["maxY"], grid["minY"], h)  # row 0 = north
    X, Y = np.meshgrid(xs, ys)
    px = X.ravel()
    py = Y.ravel()
    out = np.full(h * w, None, dtype=object)

    def rings_of(geom):
        if geom["type"] == "Polygon":
            yield geom["coordinates"]
        elif geom["type"] == "MultiPolygon":
            for p in geom["coordinates"]:
                yield p

    for f in features:
        val = f.get("properties", {}).get(prop)
        geom = f.get("geometry")
        if not geom or val is None:
            continue
        inside = np.zeros(h * w, dtype=bool)
        for rings in rings_of(geom):
            for ring in rings:  # even-odd across all rings (holes flip parity)
                r = np.asarray(ring, dtype=float)
                x1, y1 = r[:-1, 0], r[:-1, 1]
                x2, y2 = r[1:, 0], r[1:, 1]
                for i in range(len(x1)):
                    yi, yj = y1[i], y2[i]
                    if yi == yj:
                        continue
                    crosses = ((yi > py) != (yj > py)) & (
                        px < (x2[i] - x1[i]) * (py - yi) / (yj - yi) + x1[i])
                    inside ^= crosses
        out[inside] = val
    return out.reshape(h, w)


def root_zone_taw_mm(rec, root_depth_cm=70.0):
    """Plant-available water in the modeled root zone, in millimeters.

    SSURGO AWC is a volumetric fraction (cm water / cm soil). Integrate the
    dominant-component horizons down to the shallower of ``root_depth_cm`` and
    a mapped bedrock / seasonal-water-table restriction. The map-unit AWS value
    is a coarser fallback when horizon AWC is unavailable.
    """
    depths = [float(d) for d in (
        rec.get("depth_to_bedrock_min_cm"),
        rec.get("water_table_depth_annual_min_cm"),
    ) if d is not None]
    limit_cm = min([float(root_depth_cm), *depths]) if depths else float(root_depth_cm)
    limit_cm = max(0.0, limit_cm)

    water_cm = 0.0
    saw_awc = False
    for horizon in rec.get("horizons") or []:
        top = horizon.get("top_cm")
        bottom = horizon.get("bottom_cm")
        awc = horizon.get("awc_cm_cm")
        if top is None or bottom is None or awc is None:
            continue
        thickness = max(0.0, min(float(bottom), limit_cm) - max(0.0, float(top)))
        if thickness > 0.0:
            saw_awc = True
            water_cm += thickness * max(0.0, float(awc))
    if saw_awc:
        return water_cm * 10.0

    aws_150_cm = rec.get("available_water_storage_0_150cm_cm")
    if aws_150_cm is None:
        return None
    return max(0.0, float(aws_150_cm)) * min(limit_cm, 150.0) / 150.0 * 10.0


def surface_texture_fractions(rec):
    """Return the first horizon with a complete sand/silt/clay analysis.

    The organic surface horizon is not always complete in SSURGO.  Green-Ampt
    texture parameters therefore use the shallowest mineral horizon for which
    all three fractions are present instead of silently turning missing clay
    into zero.
    """
    for horizon in rec.get("horizons") or []:
        values = tuple(horizon.get(k) for k in ("sand_pct", "silt_pct", "clay_pct"))
        if all(v is not None for v in values):
            total = sum(float(v) for v in values)
            if total > 0.0:
                return tuple(100.0 * float(v) / total for v in values)
    return (None, None, None)


def soil_fields(grid):
    """Per-cell mukey + derived hydro properties from the pack's SSURGO tabular
    fetch. Returns dict of 2D arrays (NaN/None where no soil polygon).

    When the twin carries no soil layer (no data/soils/features.geojson — e.g. a
    DEM-only or non-US AOI), this returns empty arrays of the grid shape with
    available=False, so the terrain layers (flow/wetness/ponding) and the
    terrain components of seep scoring still compute. The soil-dependent pieces
    (restrictive-layer seep weight and hydraulic parameters) simply drop out."""
    feats_path = os.path.join(D, "soils", "features.geojson")
    if not os.path.exists(feats_path):
        h, w = grid["height"], grid["width"]
        return {"mukey": np.full((h, w), None, dtype=object),
                "restrictive_cm": np.full((h, w), np.nan),
                "bedrock_cm": np.full((h, w), np.nan),
                "water_table_cm": np.full((h, w), np.nan),
                "hsg": np.full((h, w), None, dtype=object),
                "ksat_min": np.full((h, w), np.nan),
                "surface_ksat": np.full((h, w), np.nan),
                "root_zone_taw_mm": np.full((h, w), np.nan),
                "sand_pct": np.full((h, w), np.nan),
                "silt_pct": np.full((h, w), np.nan),
                "clay_pct": np.full((h, w), np.nan),
                "per_mukey": {}, "available": False}
    feats = json.load(open(feats_path))["features"]
    tab_path = os.path.join(D, "soils", "tabular.json")
    tabular = json.load(open(tab_path))["map_units"] if os.path.exists(tab_path) else {}
    mukey = rasterize_polygons(feats, grid, "mukey")

    h, w = mukey.shape
    restrictive_cm = np.full((h, w), np.nan)   # depth to bedrock / perched table
    bedrock_cm = np.full((h, w), np.nan)
    water_table_cm = np.full((h, w), np.nan)
    hsg = np.full((h, w), None, dtype=object)  # hydrologic soil group string
    ksat_min = np.full((h, w), np.nan)         # profile bottleneck Ksat, mm/hr
    surface_ksat = np.full((h, w), np.nan)     # surface Ksat, mm/hr
    root_zone_taw = np.full((h, w), np.nan)    # available water over 0-70 cm, mm
    sand_pct = np.full((h, w), np.nan)
    silt_pct = np.full((h, w), np.nan)
    clay_pct = np.full((h, w), np.nan)

    per_mukey = {}
    for mk, rec in tabular.items():
        sand, silt, clay = surface_texture_fractions(rec)
        depths = [d for d in (rec.get("depth_to_bedrock_min_cm"),
                              rec.get("water_table_depth_annual_min_cm")) if d is not None]
        per_mukey[mk] = {
            "restrictive_cm": min(depths) if depths else np.nan,
            "bedrock_cm": rec.get("depth_to_bedrock_min_cm"),
            "water_table_cm": rec.get("water_table_depth_annual_min_cm"),
            "hsg": rec.get("hydrologic_group"),
            "ksat_min": rec.get("profile_ksat_min_mm_hr"),
            "surface_ksat": rec.get("surface_ksat_mm_hr"),
            "root_zone_taw_mm": root_zone_taw_mm(rec),
            "sand_pct": sand, "silt_pct": silt, "clay_pct": clay,
            "muname": rec.get("muname"),
        }
    for r in range(h):
        for c in range(w):
            mk = mukey[r, c]
            rec = per_mukey.get(str(mk)) if mk is not None else None
            if not rec:
                continue
            restrictive_cm[r, c] = rec["restrictive_cm"]
            if rec["bedrock_cm"] is not None:
                bedrock_cm[r, c] = rec["bedrock_cm"]
            if rec["water_table_cm"] is not None:
                water_table_cm[r, c] = rec["water_table_cm"]
            hsg[r, c] = rec["hsg"]
            if rec["ksat_min"] is not None:
                ksat_min[r, c] = rec["ksat_min"]
            if rec["surface_ksat"] is not None:
                surface_ksat[r, c] = rec["surface_ksat"]
            if rec["root_zone_taw_mm"] is not None:
                root_zone_taw[r, c] = rec["root_zone_taw_mm"]
            for values, key in ((sand_pct, "sand_pct"), (silt_pct, "silt_pct"),
                                (clay_pct, "clay_pct")):
                if rec[key] is not None:
                    values[r, c] = rec[key]
    return {"mukey": mukey, "restrictive_cm": restrictive_cm,
            "bedrock_cm": bedrock_cm, "water_table_cm": water_table_cm,
            "hsg": hsg,
            "ksat_min": ksat_min, "surface_ksat": surface_ksat,
            "root_zone_taw_mm": root_zone_taw,
            "sand_pct": sand_pct, "silt_pct": silt_pct, "clay_pct": clay_pct,
            "per_mukey": per_mukey, "available": True}


# ------------------------------------------------------------- seep scoring

def seep_scores(fields, soils, grid):
    """0-100 spring/seep candidacy per cell.

    Components: topographic wetness, flow
    convergence, slope-break (toe-of-slope flattening), and a shallow
    restrictive layer forcing water laterally (bedrock at 74 cm under
    Berkshire-Tunbridge, the 52 cm perched table + 3 mm/hr fragipan on Skerry).
    """
    footprint = np.isfinite(fields["dem"])
    twi_pct = percentile_norm(fields["twi"], footprint)
    acc_pct = percentile_norm(np.log1p(fields["flow_accum_cells"]), footprint)

    # slope-break: neighborhood (5x5) mean slope minus own slope, positive where
    # the ground flattens below steeper terrain (classic seep position)
    s = np.where(np.isfinite(fields["slope_rad"]), fields["slope_rad"], 0.0)
    k = 2
    pad = np.pad(s, k, mode="edge")
    neigh = np.zeros_like(s)
    for dr in range(-k, k + 1):
        for dc in range(-k, k + 1):
            neigh += pad[k + dr: k + dr + s.shape[0], k + dc: k + dc + s.shape[1]]
    neigh /= (2 * k + 1) ** 2
    brk = np.maximum(neigh - s, 0.0)
    brk_pct = percentile_norm(brk, footprint)

    # shallow restrictive layer: 1 at the surface -> 0 at >= 150 cm; soils with
    # a slow bottleneck (Ksat_min < 5 mm/hr) but unknown depth count as 60 cm
    restr = soils["restrictive_cm"].copy()
    unknown_slow = ~np.isfinite(restr) & (soils["ksat_min"] < 5.0)
    restr[unknown_slow] = 60.0
    restr_factor = np.clip(1.0 - restr / 150.0, 0.0, 1.0)
    restr_factor[~np.isfinite(restr)] = 0.0

    score = (40.0 * np.nan_to_num(twi_pct) + 20.0 * np.nan_to_num(acc_pct) +
             15.0 * np.nan_to_num(brk_pct) + 25.0 * restr_factor)
    score[~footprint] = np.nan
    return score


def top_candidates(score, grid, n=8, min_score=55.0, min_sep_m=30.0):
    """Local best seep candidates: greedy pick of highest-scoring cells with a
    minimum separation, reported in scene + geographic coordinates."""
    fwd, _ = twin_georef.transformers()
    ox, oy = twin_georef.origin()
    flat = np.nan_to_num(score, nan=-1).ravel()
    order = np.argsort(flat)[::-1]
    h, w = score.shape
    chosen = []
    for idx in order:
        if flat[idx] < min_score or len(chosen) >= n:
            break
        r, c = divmod(int(idx), w)
        x = grid["minX"] + c * grid["xstep"]
        y = grid["maxY"] - r * grid["ystep"]
        if any(math.hypot(x - p["x"], y - p["y"]) < min_sep_m for p in chosen):
            continue
        lon, lat = fwd.transform(x + ox, y + oy)
        chosen.append({"x": round(x, 1), "y": round(y, 1),
                       "lat": round(lat, 6), "lon": round(lon, 6),
                       "score": round(float(flat[idx]), 1)})
    return chosen


# -------------------------------------------------------------- validation

def stream_validation(fields, grid, channel_area_m2=5000.0):
    """Mean/median offset between extracted channel cells and the mapped
    perennial lines (mapped channel + trib) inside the grid footprint."""
    try:
        feats = json.load(open(os.path.join(D, "hydrology", "features.geojson")))["features"]
    except OSError:
        return None
    segs = []
    for f in feats:
        g = f.get("geometry") or {}
        if g["type"] == "LineString":
            segs.append(np.asarray(g["coordinates"], dtype=float))
        elif g["type"] == "MultiLineString":
            segs.extend(np.asarray(l, dtype=float) for l in g["coordinates"])
    if not segs:
        return None

    acc_area = fields["flow_accum_cells"] * fields["cell_area_m2"]
    ch = np.argwhere(acc_area >= channel_area_m2)
    if not ch.size:
        return None
    cx = grid["minX"] + ch[:, 1] * grid["xstep"]
    cy = grid["maxY"] - ch[:, 0] * grid["ystep"]

    def dist_to_segs(x, y):
        best = np.inf
        for s in segs:
            x1, y1 = s[:-1, 0], s[:-1, 1]
            x2, y2 = s[1:, 0], s[1:, 1]
            dx, dy = x2 - x1, y2 - y1
            ln = dx * dx + dy * dy
            ln[ln == 0] = 1e-9
            t = np.clip(((x - x1) * dx + (y - y1) * dy) / ln, 0, 1)
            d = np.hypot(x - (x1 + t * dx), y - (y1 + t * dy))
            best = min(best, float(d.min()))
        return best

    # only score channel cells where a mapped line actually passes nearby-ish
    # (the mapped lines run beyond the grid; cap at 60 m so an unmapped gully
    # doesn't poison the metric, it just reports separately)
    dists = np.array([dist_to_segs(x, y) for x, y in zip(cx, cy)])
    near = dists[dists < 60.0]
    return {
        "channel_threshold_m2": channel_area_m2,
        "channel_cells": int(len(dists)),
        "matched_cells": int(len(near)),
        "mean_offset_m": round(float(near.mean()), 1) if near.size else None,
        "median_offset_m": round(float(np.median(near)), 1) if near.size else None,
    }


def wetland_overlap(score, grid):
    """Fraction of strong seep-candidate cells that fall inside mapped NWI
    wetlands / waterbodies (a sanity check, not a target — field-verifiable
    NEW candidates are the interesting output)."""
    path = os.path.join(D, "atlas", "nwi_wetlands_uh.geojson")
    if not os.path.exists(path):
        return None
    fc = json.load(open(path))
    to_utm = twin_georef.from_wgs84_transformer()
    ox, oy = twin_georef.origin()

    def localize(coords):
        out = []
        for ring in coords:
            r = np.asarray(ring, dtype=float)
            e, n = to_utm.transform(r[:, 0], r[:, 1])
            out.append(np.column_stack([e - ox, n - oy]).tolist())
        return out

    feats = []
    for f in fc["features"]:
        g = f.get("geometry") or {}
        if g["type"] == "Polygon":
            feats.append({"properties": {"v": 1},
                          "geometry": {"type": "Polygon", "coordinates": localize(g["coordinates"])}})
        elif g["type"] == "MultiPolygon":
            feats.append({"properties": {"v": 1},
                          "geometry": {"type": "MultiPolygon",
                                       "coordinates": [localize(p) for p in g["coordinates"]]}})
    if not feats:
        return None
    wet = rasterize_polygons(feats, grid, "v")
    strong = np.nan_to_num(score) >= 70.0
    n_strong = int(strong.sum())
    if not n_strong:
        return {"strong_cells": 0, "in_mapped_wetland_pct": None}
    inside = sum(1 for r, c in np.argwhere(strong) if wet[r, c] is not None)
    return {"strong_cells": n_strong,
            "in_mapped_wetland_pct": round(100.0 * inside / n_strong, 1)}


# ------------------------------------------------------------------- layers

# dry sand -> deep water blue
WET_RAMP = [(240, 228, 190, 0), (214, 209, 165, 60), (130, 180, 190, 140),
            (62, 124, 177, 200), (21, 66, 137, 235)]
FLOW_RAMP = [(120, 170, 200, 0), (100, 160, 205, 90), (62, 124, 177, 170),
             (30, 80, 160, 230), (200, 235, 255, 255)]
POND_RAMP = [(100, 180, 220, 0), (80, 150, 210, 120), (40, 100, 190, 200),
             (15, 50, 140, 240), (5, 25, 90, 250)]
SEEP_RAMP = [(255, 255, 200, 0), (254, 217, 118, 90), (253, 141, 60, 170),
             (227, 26, 28, 220), (145, 0, 63, 245)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"))
    args = ap.parse_args()
    if args.data_dir:
        _use_data_dir(args.data_dir)
    twin_georef.GEOREF_PATH = os.path.join(D, "georef.json")

    out_dir = os.path.join(D, "hydrology", "local")
    os.makedirs(out_dir, exist_ok=True)

    print("Terrain hydrology (Tier 1) —", D)
    grid = hydro.load_grid(D)
    fields = hydro.compute_all(grid)
    soils = soil_fields(grid)
    if not soils.get("available"):
        print("  no soil layer (data/soils/features.geojson) — terrain-only: "
              "flow/wetness/ponding compute; seep scoring drops its "
              "restrictive-layer weight; no soil map units in the summary.")
    footprint = np.isfinite(fields["dem"])

    cs = grid["cellsize"]
    half = cs / 2.0
    bounds = [round(grid["minX"] - half, 2), round(grid["minY"] - half, 2),
              round(grid["maxX"] + half, 2), round(grid["maxY"] + half, 2)]

    layers = []

    def export(layer_id, label, rgba, values, legend, description, decimals=2,
               metadata=None):
        png = os.path.join(out_dir, layer_id + ".png")
        gj = os.path.join(out_dir, layer_id + ".grid.json")
        write_png(rgba, png)
        with open(gj, "w") as fh:
            json.dump(grid_json(values, bounds, legend, decimals=decimals,
                                metadata=metadata), fh)
        layer = {
            "id": layer_id, "label": label, "type": "raster",
            "image": "hydrology/local/%s.png" % layer_id,
            "grid": "hydrology/local/%s.grid.json" % layer_id,
            "bounds_local": bounds, "acquisition": "derived",
            "group": "hydrology", "description": description,
        }
        if metadata:
            layer.update(metadata)
        layers.append(layer)
        print("  [layer] %-18s %s" % (layer_id, label))

    # -- flow paths: log-scaled contributing area (identify value in hectares)
    acc_ha = fields["flow_accum_cells"] * fields["cell_area_m2"] / 1e4
    log_acc = np.log10(np.maximum(acc_ha, 1e-4))
    lo, hi = math.log10(0.01), math.log10(max(0.5, float(np.nanmax(acc_ha))))
    norm = np.clip((log_acc - lo) / (hi - lo), 0, 1)
    norm[acc_ha < 0.01] = np.nan  # < 100 m^2 upslope: not a flow path
    norm[~footprint] = np.nan
    export("flow_paths", "Flow paths", colorize(norm, FLOW_RAMP),
           np.where(footprint, acc_ha, np.nan),
           {"min": {"name": "0.01 ha upslope", "color": list(FLOW_RAMP[1][:3])},
            "max": {"name": "%.1f ha upslope" % np.nanmax(acc_ha), "color": list(FLOW_RAMP[4][:3])}},
           "Upslope contributing area per cell (D8 over the LiDAR DEM). "
           "Click a cell for the area in hectares draining through it.",
           decimals=3, metadata={
               "value_kind": "upslope_contributing_area",
               "value_unit": "ha",
               "cell_area_m2": round(float(fields["cell_area_m2"]), 4),
           })

    # -- wetness index (TWI percentile)
    twi_pct = percentile_norm(fields["twi"], footprint)
    export("wetness_index", "Wetness index (TWI)", colorize(twi_pct, WET_RAMP),
           np.where(footprint, np.round(twi_pct * 100.0, 1), np.nan),
           {"min": {"name": "driest (0th pct)", "color": list(WET_RAMP[1][:3])},
            "max": {"name": "wettest (100th pct)", "color": list(WET_RAMP[4][:3])}},
           "Topographic Wetness Index percentile — relative tendency to be wet "
           "(contributing area over slope). Click for the percentile.", decimals=1)

    # -- ponding (depression depth)
    depth = fields["depression_depth"]
    pond = np.where(depth > 0.03, depth, np.nan)
    pond_max = float(np.nanmax(pond)) if np.isfinite(pond).any() else 0.3
    export("ponding", "Ponding depth", colorize(np.clip(pond / pond_max, 0, 1), POND_RAMP),
           np.where(np.isfinite(pond) & footprint, pond, np.nan),
           {"min": {"name": "shallow ponding", "color": list(POND_RAMP[1][:3])},
            "max": {"name": "%.2f m deep" % pond_max, "color": list(POND_RAMP[4][:3])}},
           "Closed depressions in the LiDAR surface: where water pools before "
           "spilling, and how deep it can get. Click for depth in meters.")

    # -- seep / spring candidates
    score = seep_scores(fields, soils, grid)
    norm = np.where(score >= 40.0, (score - 40.0) / 60.0, np.nan)
    export("seep_candidates", "Spring & seep candidates", colorize(norm, SEEP_RAMP),
           np.where(footprint, np.round(score, 0), np.nan),
           {"min": {"name": "score 40 (weak)", "color": list(SEEP_RAMP[1][:3])},
            "max": {"name": "score 100 (strong)", "color": list(SEEP_RAMP[4][:3])}},
           "Where springs/seeps should form: wetness x flow convergence x "
           "slope-break x shallow bedrock/fragipan (SSURGO). Click for the "
           "0-100 score; go field-check the strong spots.", decimals=0)

    # ---------------------------------------------------------------- summary
    candidates = top_candidates(score, grid)
    validation = stream_validation(fields, grid)
    overlap = wetland_overlap(score, grid)

    pond_cells = np.isfinite(pond) & footprint
    pond_volume = float(np.nansum(np.where(pond_cells, depth, 0.0)) * fields["cell_area_m2"])
    hsg_counts = {}
    for v in soils["hsg"][footprint].ravel():
        if v:
            hsg_counts[v] = hsg_counts.get(v, 0) + 1

    # outlet: the boundary cell with max accumulation = where the AOI drains
    acc = fields["flow_accum_cells"]
    edge = footprint & (fields["flowdir"] == -1)
    outlet = None
    if edge.any():
        masked = np.where(edge, acc, np.nan)
        r, c = np.unravel_index(int(np.nanargmax(masked)), acc.shape)
        outlet = {"x": round(grid["minX"] + c * grid["xstep"], 1),
                  "y": round(grid["maxY"] - r * grid["ystep"], 1),
                  "contributing_ha": round(float(acc[r, c]) * fields["cell_area_m2"] / 1e4, 2)}

    summary = {
        "engine": "twin_hydrology.py (priority-flood + D8, pure numpy)",
        "soil_available": bool(soils.get("available")),
        "cell_size_m": round(cs, 2),
        "footprint_ha": round(float(footprint.sum()) * fields["cell_area_m2"] / 1e4, 2),
        "max_contributing_ha": round(float(np.nanmax(acc_ha)), 2),
        "outlet": outlet,
        "depression_storage_m3": round(pond_volume, 1),
        "depression_cells": int(pond_cells.sum()),
        "hsg_cell_fractions": {k: round(v / float(footprint.sum()), 3)
                               for k, v in sorted(hsg_counts.items())},
        "soil_map_units": {mk: rec["muname"] for mk, rec in soils["per_mukey"].items()
                           if rec.get("muname")},
        "seep_candidates": candidates,
        "validation": {"streams": validation, "wetland_overlap": overlap},
    }
    with open(os.path.join(D, "hydrology", "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)

    catalog = {
        "generated_by": "analyze_hydrology.py",
        "note": "Derived terrain-hydrology layers for the Simulation window. "
                "Scenario layers are appended by hydro_scenario.py.",
        "layers": layers,
    }
    cat_path = os.path.join(D, "hydrology", "simulation-layers.json")
    # keep any scenario layers a previous hydro_scenario.py appended
    if os.path.exists(cat_path):
        try:
            old = json.load(open(cat_path))
            catalog["layers"] += [l for l in old.get("layers", [])
                                  if l.get("group") == "scenario"]
        except Exception:  # noqa: BLE001
            pass
    with open(cat_path, "w") as fh:
        json.dump(catalog, fh, indent=2)

    # ------------------------------------------------------ store registration
    try:
        store = Store(STORE_PATH)
        run = store.begin_run("analyze_hydrology.py",
                              inputs={"grid": grid["raw"]["heights"][:100],
                                      "cell": cs, "layers": [l["id"] for l in layers]})
        for l in layers:
            png_path = os.path.join(D, l["image"])
            sha = hashlib.sha1(open(png_path, "rb").read()).hexdigest()
            store.upsert_layer("hydro_" + l["id"], label=l["label"], kind="raster",
                               acquisition="derived", source_path=l["image"],
                               feature_count=None, status="ok", content_sha1=sha)
        store.finish_run(run, notes="terrain hydrology layers + summary")
        store.close()
        print("  [store] registered %d layers (run %d)" % (len(layers), run))
    except Exception as e:  # noqa: BLE001
        print("  [store] WARNING: registration skipped: %s" % e)

    # -------------------------------------------------------------- report
    print("\nSummary:")
    print("  footprint %.1f ha, outlet drains %.1f ha at (%.0f, %.0f)" % (
        summary["footprint_ha"], outlet["contributing_ha"] if outlet else 0,
        outlet["x"] if outlet else 0, outlet["y"] if outlet else 0))
    print("  depression storage %.0f m^3 across %d cells" % (
        pond_volume, summary["depression_cells"]))
    if validation:
        print("  stream check: %d/%d channel cells within 60 m of mapped lines, "
              "median offset %s m" % (validation["matched_cells"],
                                      validation["channel_cells"],
                                      validation["median_offset_m"]))
    if overlap:
        print("  seep check: %s%% of strong candidates inside mapped NWI wetlands"
              % overlap["in_mapped_wetland_pct"])
    print("  top seep candidates:")
    for cand in candidates:
        print("    score %5.1f  at %.6f, %.6f  (scene %.0f, %.0f)" % (
            cand["score"], cand["lat"], cand["lon"], cand["x"], cand["y"]))


if __name__ == "__main__":
    main()
