#!/usr/bin/env python3
"""Tier 1 wildfire fuelscape exporter for the Simulation window.

Mirrors ``scripts/analyze_hydrology.py``: load the terrain grid, resample the
LANDFIRE 30 m fuels/canopy rasters to the LiDAR DEM, run the pure-numpy fire
engine, and export viewer-ready draped layers plus a summary/catalog/store run.

Run:  python3 scripts/analyze_fuels.py [--data-dir DIR]
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import analyze_hydrology as t1
import twin_fire
import twin_georef
import twin_hydrology as hydro
from twin_store import Store

D = os.path.join(PROJECT, "data")
STORE_PATH = os.path.join(D, "twin.gpkg")

REFERENCE_MOISTURE = {
    "dead_1h": 0.06,
    "dead_10h": 0.07,
    "dead_100h": 0.08,
    "live_herb": 0.60,
    "live_woody": 0.90,
}
REFERENCE_DATE = {"label": "May 21", "doy": 141}

ROS_RAMP = [(255, 247, 188, 0), (254, 196, 79, 95), (236, 112, 20, 170),
            (189, 54, 47, 225), (96, 20, 55, 245)]
THRESHOLD_RAMP = [(178, 24, 43, 238), (239, 104, 45, 222),
                  (254, 224, 139, 198), (102, 189, 99, 190),
                  (49, 130, 189, 210)]
THRESHOLD_NOT_REACHED = (71, 91, 99, 225)
CROWN_COLORS = {
    0: (92, 92, 82, 85),
    1: (245, 157, 51, 220),
    2: (188, 34, 34, 240),
}
CROWN_LEGEND = {
    "0": {"name": "surface / crown not applicable", "color": [92, 92, 82]},
    "1": {"name": "passive / torching", "color": [245, 157, 51]},
    "2": {"name": "active crown", "color": [188, 34, 34]},
}
BROADLEAF_LITTER_CODES = {182, 186, 189}


def _use_data_dir(data_dir):
    global D, STORE_PATH
    t1._use_data_dir(data_dir)
    D = t1.D
    STORE_PATH = t1.STORE_PATH


def _load_us_fuels():
    path = os.path.join(PROJECT, "packs", "us-national", "fuels.py")
    spec = importlib.util.spec_from_file_location("us_national_fuels", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_us_derive_fuel():
    path = os.path.join(PROJECT, "packs", "us-national", "derive_fuel.py")
    spec = importlib.util.spec_from_file_location("us_national_derive_fuel", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _grid_values(path):
    g = json.load(open(path))
    rows = []
    for row in g["values"]:
        rows.append([np.nan if v is None or v == -9999 else v for v in row])
    return np.asarray(rows, dtype=float), g


def _upsample_nearest(src, src_grid, target_grid):
    """Nearest source cell by scene-local coordinates, matching app.js sampling."""
    minx, miny, maxx, maxy = src_grid["bounds_local"]
    src_h, src_w = src.shape
    xs = target_grid["minX"] + np.arange(target_grid["width"]) * target_grid["xstep"]
    ys = target_grid["maxY"] - np.arange(target_grid["height"]) * target_grid["ystep"]

    col_f = ((xs - minx) / (maxx - minx)) * src_w
    row_f = ((maxy - ys) / (maxy - miny)) * src_h
    col_ok = (xs >= minx) & (xs <= maxx)
    row_ok = (ys >= miny) & (ys <= maxy)
    cols = np.clip(np.floor(col_f).astype(int), 0, src_w - 1)
    rows = np.clip(np.floor(row_f).astype(int), 0, src_h - 1)

    out = src[np.ix_(rows, cols)].astype(float)
    out[~row_ok, :] = np.nan
    out[:, ~col_ok] = np.nan
    return out


def load_fuelscape(data_dir, grid=None, fuel_source="landfire", return_provenance=False):
    if grid is None:
        grid = data_dir
        data_dir = D
    if fuel_source not in ("landfire", "computed"):
        raise ValueError("fuel_source must be 'landfire' or 'computed'")
    layers = {}
    provenance = {
        "method": "landfire_fbfm40_2024",
        "source": "landfire",
        "note": "LANDFIRE 2024 FBFM40 + canopy grids, nearest-resampled to DEM.",
        "fuel_model_shift": {
            "basis": "30 m LANDFIRE FBFM40 cells",
            "total_cells": 0,
            "changed_cells": 0,
            "changed_fraction": 0.0,
            "top_transitions": [],
        },
    }
    names = ("landfire_fbfm40_2024", "landfire_cc_2024", "landfire_ch_2024",
             "landfire_cbh_2024", "landfire_cbd_2024")
    for name in names:
        arr, meta = _grid_values(os.path.join(data_dir, "atlas", "local",
                                              name + ".grid.json"))
        if name == "landfire_fbfm40_2024" and fuel_source == "computed":
            arr, provenance = _load_us_derive_fuel().derive_fbfm40(data_dir, grid)
        elif name == "landfire_fbfm40_2024":
            provenance["fuel_model_shift"]["total_cells"] = int(np.isfinite(arr).sum())
        layers[name] = _upsample_nearest(arr, meta, grid)

    fbfm = layers["landfire_fbfm40_2024"]
    canopy = {
        "cc_pct": layers["landfire_cc_2024"],
        "ch_m": layers["landfire_ch_2024"] / 10.0,
        "cbh_m": layers["landfire_cbh_2024"] / 10.0,
        "cbd_kg_m3": layers["landfire_cbd_2024"] / 100.0,
    }
    if return_provenance:
        return fbfm, canopy, provenance
    return fbfm, canopy


def _terrain_reference_fmc(grid):
    geo = json.load(open(os.path.join(D, "georef.json")))
    lat = float(geo["origin_wgs84"]["lat"])
    lon_west = abs(float(geo["origin_wgs84"]["lon"]))
    elev = float(np.nanmean(grid["dem"]))
    fmc = float(np.asarray(twin_fire.fbp_fmc(
        lat, lon_west, elev, REFERENCE_DATE["doy"], drought="normal")))
    return fmc, {"lat": lat, "lon_west": lon_west, "elevation_m": round(elev, 1)}


def _categorical_grid_json(values, bounds, legend, nodata=None, metadata=None):
    rows = []
    arr = np.asarray(values)
    for r in range(arr.shape[0]):
        row = []
        for v in arr[r]:
            if isinstance(v, float) and not math.isfinite(v):
                row.append(None)
            elif np.issubdtype(type(v), np.floating) and not math.isfinite(float(v)):
                row.append(None)
            else:
                row.append(int(v))
        rows.append(row)
    out = {"bounds_local": bounds, "width": int(arr.shape[1]),
           "height": int(arr.shape[0]), "nodata": nodata,
           "values": rows, "legend": legend}
    if metadata:
        out.update(metadata)
    return out


def _categorical_rgba(values, footprint, color_map, default=(0, 0, 0, 0)):
    rgba = np.zeros(values.shape + (4,), dtype=np.uint8)
    rgba[:, :] = default
    finite = np.isfinite(values) & footprint
    codes = np.zeros(values.shape, dtype=int)
    codes[finite] = values[finite].astype(int)
    for raw in np.unique(values[finite].astype(int)):
        color = color_map.get(int(raw))
        if color is None:
            continue
        rgba[finite & (codes == int(raw))] = color
    return rgba


def _vat_legend(vat, codes):
    out = {}
    for code in sorted(int(c) for c in codes):
        rec = vat.get(str(code))
        if not rec:
            continue
        out[str(code)] = {"name": rec["name"], "color": rec["color"]}
    return out


def _fuel_rgba(values, footprint, vat):
    cmap = {}
    for key, rec in vat.items():
        if key == "-9999":
            continue
        rgb = rec.get("color", [0, 0, 0])
        cmap[int(key)] = (int(rgb[0]), int(rgb[1]), int(rgb[2]), 230)
    return _categorical_rgba(values, footprint, cmap)


def _crown_rgba(values, footprint):
    return _categorical_rgba(values.astype(float), footprint, CROWN_COLORS)


def _ros_rgba(values, footprint):
    mask = footprint & np.isfinite(values) & (values > 0.0)
    norm = t1.percentile_norm(values, mask)
    return t1.colorize(norm, ROS_RAMP)


def _threshold_display_values(values, not_reached_value):
    arr = np.asarray(values, dtype=float)
    return np.where(np.isinf(arr), float(not_reached_value), arr)


def _threshold_rgba(values, footprint, applicable, cap_mph):
    arr = np.asarray(values, dtype=float)
    app = footprint & np.asarray(applicable, dtype=bool)
    norm = np.full(arr.shape, np.nan, dtype=float)
    finite = app & np.isfinite(arr)
    norm[finite] = np.clip(arr[finite] / max(1.0, float(cap_mph)), 0.0, 1.0)
    rgba = t1.colorize(norm, THRESHOLD_RAMP)
    rgba[app & np.isinf(arr)] = THRESHOLD_NOT_REACHED
    return rgba


def _threshold_legend(name, values, applicable, cap_mph, not_reached_value):
    arr = np.asarray(values, dtype=float)
    app = np.asarray(applicable, dtype=bool)
    finite = app & np.isfinite(arr)
    hi = float(np.nanmax(arr[finite])) if finite.any() else 0.0
    return {
        "low": {"name": "low %s: crown-prone" % name.lower(),
                "color": list(THRESHOLD_RAMP[0][:3])},
        "high": {"name": "%.0f mph: crown-resistant" % min(hi, float(cap_mph)),
                 "color": list(THRESHOLD_RAMP[-1][:3])},
        str(int(not_reached_value)): {
            "name": "not reached by %.0f mph" % float(cap_mph),
            "color": list(THRESHOLD_NOT_REACHED[:3]),
        },
    }


def _range_stats(arr, mask, decimals=3):
    vals = np.asarray(arr, dtype=float)[mask & np.isfinite(arr)]
    if not vals.size:
        return {"min": None, "max": None, "mean": None}
    return {
        "min": round(float(vals.min()), decimals),
        "max": round(float(vals.max()), decimals),
        "mean": round(float(vals.mean()), decimals),
    }


def _threshold_stats(arr, applicable, domain=None):
    a = np.asarray(arr, dtype=float)
    domain_mask = np.ones(a.shape, dtype=bool) if domain is None else np.asarray(domain, dtype=bool)
    app = domain_mask & np.asarray(applicable, dtype=bool)
    finite = app & np.isfinite(a)
    nonapp = domain_mask & ~app
    vals = a[finite]
    out = {
        "unit": "mph_20ft_open",
        "applicable_cells": int(app.sum()),
        "not_applicable_cells": int(nonapp.sum()),
        "infinite_fraction": round(float((app & np.isinf(a)).sum()) / max(1, int(app.sum())), 3),
    }
    if vals.size:
        out.update({
            "min": round(float(vals.min()), 2),
            "p50": round(float(np.percentile(vals, 50)), 2),
            "p90": round(float(np.percentile(vals, 90)), 2),
            "max": round(float(vals.max()), 2),
        })
    else:
        out.update({"min": None, "p50": None, "p90": None, "max": None})
    return out


def _crown_applicable_mask(footprint, canopy, fbfm_codes):
    canopy_ok = (footprint & (canopy["cbh_m"] > 0.0) & (canopy["cbd_kg_m3"] > 0.0) &
                 np.isfinite(canopy["cbh_m"]) & np.isfinite(canopy["cbd_kg_m3"]))
    broadleaf_litter = np.isin(np.asarray(fbfm_codes).astype(int),
                               list(BROADLEAF_LITTER_CODES))
    return canopy_ok & ~broadleaf_litter


def _fuel_breakdown(codes, footprint, fuels_mod, vat):
    valid = footprint & np.isfinite(codes)
    total = int(valid.sum())
    out = []
    for code in sorted(np.unique(codes[valid].astype(int))):
        count = int((valid & (codes.astype(int) == code)).sum())
        params = fuels_mod.FBFM40.get(int(code), fuels_mod.fuel_params(int(code)))
        vat_name = (vat.get(str(code)) or {}).get("name", params["short_name"])
        out.append({
            "code": int(code),
            "short_name": params["short_name"],
            "name": params.get("name", vat_name),
            "count": count,
            "fraction": round(count / max(1, total), 4),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"))
    ap.add_argument("--fuel-source", choices=("landfire", "computed"),
                    default="landfire")
    args = ap.parse_args()
    if args.data_dir:
        _use_data_dir(args.data_dir)
    twin_georef.GEOREF_PATH = os.path.join(D, "georef.json")

    fire_dir = os.path.join(D, "fire")
    out_dir = os.path.join(fire_dir, "local")
    os.makedirs(out_dir, exist_ok=True)

    print("Wildfire fuels (Tier 1) —", D)
    print("  fuel source:", args.fuel_source)
    grid = hydro.load_grid(D)
    slope = hydro.slope_radians(grid["dem"], grid["cellsize"])
    footprint = np.isfinite(grid["dem"])
    cs = grid["cellsize"]
    half = cs / 2.0
    bounds = [round(grid["minX"] - half, 2), round(grid["minY"] - half, 2),
              round(grid["maxX"] + half, 2), round(grid["maxY"] + half, 2)]

    fuels_mod = _load_us_fuels()
    vat = json.load(open(os.path.join(D, "atlas", "vat", "landfire_fbfm40_2024.json")))
    fbfm_raw, canopy, fuel_provenance = load_fuelscape(
        D, grid, fuel_source=args.fuel_source, return_provenance=True)
    fbfm_codes = np.where(np.isfinite(fbfm_raw), fbfm_raw, 91).astype(int)

    fmc, fmc_ref = _terrain_reference_fmc(grid)
    crown_applicable_input = _crown_applicable_mask(footprint, canopy, fbfm_codes)
    canopy["fmc_pct"] = np.where(crown_applicable_input, fmc, np.nan)
    fuelbed = twin_fire.fuel_bed(fbfm_codes, fuels_mod.FBFM40,
                                 REFERENCE_MOISTURE["live_herb"])
    fields = twin_fire.compute_static(grid, fuelbed, canopy, REFERENCE_MOISTURE)

    nonforested = footprint & ~crown_applicable_input
    nonforest_surface_ok = bool(np.all(fields["crown_potential"][nonforested] == 0))

    layers = []

    def export(layer_id, label, rgba, values, legend, description, decimals=2,
               metadata=None, categorical=False):
        png = os.path.join(out_dir, layer_id + ".png")
        gj = os.path.join(out_dir, layer_id + ".grid.json")
        t1.write_png(rgba, png)
        grid_payload = (_categorical_grid_json(values, bounds, legend, metadata=metadata)
                        if categorical else
                        t1.grid_json(values, bounds, legend, decimals=decimals,
                                     metadata=metadata))
        with open(gj, "w") as fh:
            json.dump(grid_payload, fh)
        layer = {
            "id": layer_id, "label": label, "type": "raster",
            "image": "fire/local/%s.png" % layer_id,
            "grid": "fire/local/%s.grid.json" % layer_id,
            "bounds_local": bounds, "acquisition": "derived",
            "group": "fire", "description": description,
        }
        if metadata:
            layer.update(metadata)
        layers.append(layer)
        print("  [layer] %-18s %s" % (layer_id, label))

    fuel_values = np.where(footprint & np.isfinite(fbfm_raw), fbfm_codes, np.nan)
    present_codes = np.unique(fbfm_codes[footprint & np.isfinite(fbfm_raw)])
    fuel_description = (
        "LANDFIRE 2024 Scott & Burgan FBFM40 fuel model, nearest-resampled "
        "from the 30 m fuelscape to the LiDAR terrain grid."
        if args.fuel_source == "landfire" else
        "Computed local Scott & Burgan FBFM40 fuel model: LANDFIRE nonforest "
        "cells kept, forest timber cells reclassified from LiDAR/NAIP tree "
        "and shrub observations plus LANDFIRE EVT/EVC. Screening-grade."
    )
    source_label = (
        "LANDFIRE 2024 FBFM40 + canopy grids, nearest-resampled to DEM"
        if args.fuel_source == "landfire" else
        "Computed local FBFM40 from LiDAR/NAIP vegetation + LANDFIRE EVT/EVC; "
        "canopy grids from LANDFIRE, nearest-resampled to DEM"
    )
    source_note = fuel_provenance.get("note", "")

    export("fuel_model", "Fuel model", _fuel_rgba(fuel_values, footprint, vat),
           fuel_values, _vat_legend(vat, present_codes),
           fuel_description,
           metadata={"value_kind": "fbfm40_fuel_model", "value_unit": "code",
                     "fuel_source": args.fuel_source,
                     "cell_area_m2": round(float(fields["cell_area_m2"]), 4)},
           categorical=True)

    for layer_id, label, key, desc in [
            ("base_ros", "Base ROS", "base_ros",
             "Surface rate of spread with no wind and no slope under the "
             "reference D2L2-ish moisture scenario."),
            ("slope_hazard", "Slope hazard", "slope_hazard",
             "Surface rate of spread with no wind, driven only by each cell's "
             "slope magnitude under the reference moisture scenario."),
    ]:
        values = np.where(footprint, fields[key], np.nan)
        finite = values[np.isfinite(values) & (values > 0.0)]
        hi = float(np.max(finite)) if finite.size else 0.0
        export(layer_id, label, _ros_rgba(values, footprint), values,
               {"min": {"name": "low spread", "color": list(ROS_RAMP[1][:3])},
                "max": {"name": "%.2f m/min" % hi, "color": list(ROS_RAMP[4][:3])}},
               (desc if args.fuel_source == "landfire"
                else desc + " Fuel source: computed local crosswalk, screening-grade."),
               decimals=3, metadata={
                   "value_kind": "surface_rate_of_spread",
                   "value_unit": "m/min",
                   "fuel_source": args.fuel_source,
                   "cell_area_m2": round(float(fields["cell_area_m2"]), 4),
               })

    crown_counts = {}
    crown_total = int(footprint.sum())
    for cls, name in [(0, "surface"), (1, "passive"), (2, "active")]:
        count = int((footprint & (fields["crown_potential"] == cls)).sum())
        crown_counts[name] = {"count": count, "fraction": round(count / max(1, crown_total), 4)}

    applicable_crown = crown_applicable_input
    threshold_cap = float(twin_fire.TI_CI_MAX_OPEN_WIND_MPH)
    not_reached_value = int(math.ceil(threshold_cap)) + 1
    fuel_source_sentence = (
        "Fuel source: LANDFIRE FBFM40, selected by --fuel-source."
        if args.fuel_source == "landfire" else
        "Fuel source: computed local FBFM40 crosswalk selected by --fuel-source; "
        "screening-grade."
    )
    for layer_id, label, key, index_name, action in [
            ("torching_index", "Torching Index", "TI", "Torching Index",
             "begins to torch"),
            ("crowning_index", "Crowning Index", "CI", "Crowning Index",
             "can actively crown"),
    ]:
        values = np.where(footprint, fields[key], np.nan)
        export(
            layer_id, label,
            _threshold_rgba(values, footprint, applicable_crown, threshold_cap),
            _threshold_display_values(values, not_reached_value),
            _threshold_legend(index_name, values, applicable_crown,
                              threshold_cap, not_reached_value),
            ("20-ft open-wind speed at which this conifer-compatible stand %s "
             "(%s). Lower = more crown-prone; 'not reached' = crown-resistant "
             "under this fuel. %s" %
             (action, index_name, fuel_source_sentence)),
            decimals=1,
            metadata={
                "value_kind": layer_id,
                "value_unit": "mph_20ft_open",
                "fuel_source": args.fuel_source,
                "cell_area_m2": round(float(fields["cell_area_m2"]), 4),
                "threshold_cap_mph": threshold_cap,
                "not_reached_value": not_reached_value,
                "not_reached_label": "not reached by %.0f mph" % threshold_cap,
            },
        )

    summary = {
        "engine": "twin_fire.py (Rothermel surface + Van Wagner/Scott-Reinhardt crown screen; active crown ROS uses original FM10 at 0.40 WAF)",
        "fuel_source": source_label,
        "fuel_source_key": args.fuel_source,
        "fuel_model_shift": fuel_provenance.get("fuel_model_shift"),
        "fuel_model_provenance_note": source_note,
        "cell_size_m": round(float(cs), 2),
        "footprint_ha": round(float(footprint.sum()) * fields["cell_area_m2"] / 1e4, 2),
        "reference_moisture": {
            "dead_1h_pct": 6,
            "dead_10h_pct": 7,
            "dead_100h_pct": 8,
            "live_herb_pct": 60,
            "live_woody_pct": 90,
            "label": "D2L2-ish moderate day",
        },
        "reference_fmc": {
            "method": "fbp_spring_dip",
            "date": REFERENCE_DATE["label"],
            "doy": REFERENCE_DATE["doy"],
            "fmc_pct": round(float(fmc), 2),
            **fmc_ref,
        },
        "reference_wind": {
            "open_wind_20ft_mph": fields["reference_wind_20ft_open_mph"],
            "note": fields["reference_wind_note"],
        },
        "fuel_model_breakdown": _fuel_breakdown(fbfm_codes, footprint, fuels_mod, vat),
        "mean_slope": {
            "radians": round(float(np.nanmean(slope[footprint])), 4),
            "degrees": round(float(np.nanmean(np.degrees(slope[footprint]))), 2),
        },
        "canopy_stats": {
            "cc_pct": _range_stats(canopy["cc_pct"], footprint, 2),
            "ch_m": _range_stats(canopy["ch_m"], footprint, 2),
            "cbh_m": _range_stats(canopy["cbh_m"], footprint, 2),
            "cbd_kg_m3": _range_stats(canopy["cbd_kg_m3"], footprint, 3),
        },
        "crown_potential_fractions": crown_counts,
        "nonforested_hardwood_crown_surface_ok": nonforest_surface_ok,
        "nonforested_hardwood_or_missing_canopy_cells": int(nonforested.sum()),
        "TI_baseline": _threshold_stats(fields["TI"], applicable_crown, footprint),
        "CI_baseline": _threshold_stats(fields["CI"], applicable_crown, footprint),
        "note": ("Screening-grade static fuelscape: fuel/canopy classes are 30 m "
                 "LANDFIRE cells draped onto a 3 m DEM. Absolute ROS, TI/CI, and "
                 "crown classes are approximate and dominated by fuel/canopy and "
                 "reference-weather uncertainty; event runs must state their "
                 "weather and moisture scenario."),
    }
    with open(os.path.join(fire_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)

    catalog = {
        "generated_by": "analyze_fuels.py",
        "note": "Derived Tier-1 wildfire fuelscape layers. Scenario layers are "
                "appended by fire_scenario.py.",
        "layers": layers,
    }
    cat_path = os.path.join(fire_dir, "fire-layers.json")
    if os.path.exists(cat_path):
        try:
            old = json.load(open(cat_path))
            catalog["layers"] += [l for l in old.get("layers", [])
                                  if l.get("group") == "fire_scenario"]
        except Exception:  # noqa: BLE001
            pass
    with open(cat_path, "w") as fh:
        json.dump(catalog, fh, indent=2)

    try:
        store = Store(STORE_PATH)
        run = store.begin_run("analyze_fuels.py",
                              inputs={"grid": grid["raw"]["heights"][:100],
                                      "fuels": "atlas/local/landfire_fbfm40_2024.grid.json",
                                      "canopy": "atlas/local/landfire_c*.grid.json",
                                      "layers": [l["id"] for l in layers]})
        for l in layers:
            png_path = os.path.join(D, l["image"])
            sha = hashlib.sha1(open(png_path, "rb").read()).hexdigest()
            store.upsert_layer("fire_" + l["id"], label=l["label"], kind="raster",
                               acquisition="derived", source_path=l["image"],
                               feature_count=None, status="ok", content_sha1=sha)
        if store.conn.execute(
                "SELECT 1 FROM layers WHERE layer_id = ?",
                ("fire_crown_potential",)).fetchone():
            store.upsert_layer(
                "fire_crown_potential",
                label="Crown potential (replaced by TI/CI)",
                kind="raster", acquisition="derived", source_path=None,
                feature_count=None, status="replaced_by_ti_ci",
                content_sha1=None)
        store.finish_run(run, notes="wildfire Tier-1 fuelscape layers + summary")
        store.close()
        print("  [store] registered %d layers (run %d)" % (len(layers), run))
    except Exception as e:  # noqa: BLE001
        print("  [store] WARNING: registration skipped: %s" % e)

    print("\nSummary:")
    print("  canopy cc %.0f..%.0f%%, ch %.1f..%.1f m, cbh %.1f..%.1f m, cbd %.2f..%.2f kg/m^3" % (
        summary["canopy_stats"]["cc_pct"]["min"], summary["canopy_stats"]["cc_pct"]["max"],
        summary["canopy_stats"]["ch_m"]["min"], summary["canopy_stats"]["ch_m"]["max"],
        summary["canopy_stats"]["cbh_m"]["min"], summary["canopy_stats"]["cbh_m"]["max"],
        summary["canopy_stats"]["cbd_kg_m3"]["min"], summary["canopy_stats"]["cbd_kg_m3"]["max"]))
    print("  reference crown class fractions:", ", ".join(
        "%s %.1f%%" % (k, v["fraction"] * 100.0)
        for k, v in summary["crown_potential_fractions"].items()))
    print("  nonforested crown gate:", "ok" if nonforest_surface_ok else "FAILED")
    print("  reference FMC %.1f%% on %s; reference wind %.1f mph 20-ft open" % (
        fmc, REFERENCE_DATE["label"], fields["reference_wind_20ft_open_mph"]))


if __name__ == "__main__":
    main()
