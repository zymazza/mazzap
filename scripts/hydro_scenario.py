#!/usr/bin/env python3
"""Tier 2 of the hydrology system: the **scenario
engine** behind the viewer's Simulation window. Two event modes:

  snowmelt  a snowpack (inches of SWE) melting over N days, with optional
            rain-on-snow, antecedent moisture, and frozen ground
  rain      a rainstorm: total inches over a duration in hours (presets come
            from the 45-year Daymet annual-maximum storm series)

Either way it computes where the water goes on the property:

  melt water + rain  --SCS curve number per soil cell-->  runoff vs infiltration
  per-cell runoff    --routed down the Tier-1 D8 graph-->  scenario flow volumes

Soil response uses the hydrologic soil groups and restrictive layers from the
pack's SSURGO tabular fetch (woods-in-good-condition curve numbers; dual-group
soils take their undrained class; AMC I/III conversions for dry/wet antecedent;
frozen ground pushes CN toward saturated response). Snowpack presets come from
the 45-year Daymet climatology (data/climate/forcing-summary.json).

Outputs:
  - JSON result on stdout (with --json): totals, peak-flow estimate at the AOI
    outlet (wide, honest uncertainty band), depression-storage filling, notes.
  - Two drape layers replacing any previous scenario in
    data/hydrology/simulation-layers.json (group "scenario"):
      scenario_runoff   per-cell runoff generation (mm) — the soil contrasts
      scenario_flow     routed event flow volume (m^3) — where the water goes
  - A pipeline run in the twin store with the scenario parameters as inputs,
    so scenario history is queryable like any other run.

This is a scenario tool, not a forecast: discharge carries +/-50%-class
uncertainty (ungauged), geometry (where water concentrates) is the reliable part.

Run:  python3 scripts/hydro_scenario.py --swe-in 10 --melt-days 4 --rain-in 0.5 \
          --antecedent normal [--frozen] [--json] [--data-dir DIR]
      python3 scripts/hydro_scenario.py --mode rain --rain-in 2.8 --storm-hours 12
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

import analyze_hydrology as t1  # reuse raster/ramp/soils helpers (same conventions)

gdal.UseExceptions()

D = os.path.join(PROJECT, "data")
STORE_PATH = os.path.join(D, "twin.gpkg")

IN_TO_MM = 25.4

# SCS curve numbers, cover type "woods, good hydrologic condition" (TR-55).
# Dual-group (undrained) soils respond as group D when wet — the conservative
# and, for B/D Skerry with its 52 cm perched table, the realistic choice.
CN_WOODS = {"A": 30.0, "B": 55.0, "C": 70.0, "D": 77.0,
            "A/D": 77.0, "B/D": 77.0, "C/D": 77.0}
CN_DEFAULT = 60.0  # footprint cells with no soil polygon (roads, water edge)


def cn_amc_adjust(cn2, antecedent):
    """AMC I (dry) / III (wet) conversions (Chow, Maidment & Mays)."""
    if antecedent == "dry":
        return cn2 / (2.281 - 0.01281 * cn2)
    if antecedent == "wet":
        return cn2 / (0.427 + 0.00573 * cn2)
    return cn2


def scs_runoff_mm(p_mm, cn):
    """SCS-CN event runoff. p_mm scalar, cn array -> runoff array (mm)."""
    s = 25400.0 / np.clip(cn, 1.0, 99.9) - 254.0
    ia = 0.2 * s
    q = np.where(p_mm > ia, (p_mm - ia) ** 2 / (p_mm + 0.8 * s), 0.0)
    return q


def route_volumes(fdir, vol):
    """Accumulate per-cell volumes (m^3) down the D8 graph: total event volume
    passing through each cell. Same topological pass as flow_accumulation."""
    h, w = fdir.shape
    receiver = np.full(h * w, -1, dtype=np.int64)
    indeg = np.zeros(h * w, dtype=np.int64)
    valid = (fdir >= -1).ravel()
    f = fdir.ravel()
    for i in range(h * w):
        k = f[i]
        if k < 0:
            continue
        dr, dc = hydro._NB[k]
        r, c = divmod(i, w)
        receiver[i] = (r + dr) * w + (c + dc)
        indeg[receiver[i]] += 1
    out = np.where(valid, vol.ravel(), 0.0).copy()
    stack = [i for i in range(h * w) if valid[i] and indeg[i] == 0]
    while stack:
        i = stack.pop()
        rec = receiver[i]
        if rec < 0:
            continue
        out[rec] += out[i]
        indeg[rec] -= 1
        if indeg[rec] == 0:
            stack.append(int(rec))
    out[~valid] = np.nan
    return out.reshape(h, w)


def climatology_presets():
    """Snowpack + storm presets from the Daymet fetch (inches), if present."""
    path = os.path.join(D, "climate", "forcing-summary.json")
    if not os.path.exists(path):
        return None
    c = json.load(open(path))
    def inches(key):
        v = c.get(key)
        return round(v / IN_TO_MM, 1) if v is not None else None
    return {
        "median_in": inches("peak_swe_kg_m2_median"),
        "p90_in": inches("peak_swe_kg_m2_p90"),
        "max_in": inches("peak_swe_kg_m2_max"),
        "storm_1day_median_in": inches("storm_1day_mm_median"),
        "storm_1day_p90_in": inches("storm_1day_mm_p90"),
        "storm_1day_max_in": inches("storm_1day_mm_max"),
        "storm_3day_max_in": inches("storm_3day_mm_max"),
        "n_water_years": c.get("n_full_water_years"),
    }


def rain_peaking_factor(hours):
    """Ratio of peak to mean discharge for a storm of this duration: short
    convective bursts are far peakier than long soakers. Coarse, honest tiers."""
    if hours <= 3:
        return 3.5
    if hours <= 12:
        return 2.5
    if hours <= 24:
        return 2.0
    return 1.5


RUNOFF_RAMP = [(255, 245, 200, 0), (252, 197, 96, 110), (245, 126, 60, 180),
               (200, 50, 40, 230), (120, 10, 40, 250)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["snowmelt", "rain"], default="snowmelt")
    ap.add_argument("--swe-in", type=float, default=None,
                    help="snowmelt: snow water equivalent, inches (water, not snow depth)")
    ap.add_argument("--preset", choices=["median", "p90", "max"], default=None,
                    help="snowmelt: take SWE from the twin's Daymet climatology")
    ap.add_argument("--melt-days", type=float, default=4.0)
    ap.add_argument("--rain-in", type=float, default=0.0,
                    help="snowmelt: rain-on-snow; rain: the storm total")
    ap.add_argument("--storm-hours", type=float, default=12.0,
                    help="rain: storm duration in hours")
    ap.add_argument("--antecedent", choices=["dry", "normal", "wet"], default="normal")
    ap.add_argument("--frozen", action="store_true",
                    help="frozen/concrete ground: soils respond as wet AMC III + floor CN 80")
    ap.add_argument("--json", action="store_true", help="print result JSON on stdout")
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"))
    args = ap.parse_args()

    global D, STORE_PATH
    if args.data_dir:
        D = os.path.abspath(args.data_dir)
        STORE_PATH = os.path.join(D, "twin.gpkg")
        twin_store.JOURNAL_DIR = os.path.join(D, "journal")
        t1._use_data_dir(D)
    twin_georef.GEOREF_PATH = os.path.join(D, "georef.json")

    presets = climatology_presets()
    if args.mode == "rain":
        swe_in = 0.0
        rain_in = args.rain_in or (presets or {}).get("storm_1day_median_in") or 2.0
        storm_hours = max(0.5, args.storm_hours)
        event_seconds = storm_hours * 3600.0
        peaking = rain_peaking_factor(storm_hours)
        p_mm = rain_in * IN_TO_MM
        scenario_label = "%.1f″ rain / %s storm%s" % (
            rain_in,
            ("%.0f h" % storm_hours) if storm_hours < 48 else ("%.0f d" % (storm_hours / 24)),
            ", frozen" if args.frozen else "")
    else:
        swe_in = args.swe_in
        if swe_in is None and args.preset and presets:
            swe_in = presets["%s_in" % args.preset]
        if swe_in is None:
            swe_in = presets["median_in"] if presets else 7.0
        rain_in = args.rain_in
        melt_days = max(0.5, args.melt_days)
        event_seconds = melt_days * 86400.0
        peaking = 2.0  # melt concentrates in the warm afternoon
        p_mm = swe_in * IN_TO_MM + rain_in * IN_TO_MM
        scenario_label = "%.1f″ SWE / %.0f d melt%s%s" % (
            swe_in, melt_days,
            " + %.1f″ rain" % rain_in if rain_in else "",
            ", frozen" if args.frozen else "")

    # ---------------------------------------------------------------- terrain
    grid = hydro.load_grid(D)
    fields = hydro.compute_all(grid)
    soils = t1.soil_fields(grid)
    footprint = np.isfinite(fields["dem"])
    cell_m2 = fields["cell_area_m2"]

    # ------------------------------------------------------------- soils -> CN
    h, w = fields["dem"].shape
    cn = np.full((h, w), CN_DEFAULT)
    for r in range(h):
        for c in range(w):
            g = soils["hsg"][r, c]
            if g:
                cn[r, c] = CN_WOODS.get(g, CN_DEFAULT)
    antecedent = "wet" if args.frozen else args.antecedent
    cn = cn_amc_adjust(cn, antecedent)
    if args.frozen:
        cn = np.maximum(cn, 80.0)  # concrete frost floor
    cn[~footprint] = np.nan

    # ------------------------------------------------------------ partitioning
    runoff_mm = scs_runoff_mm(p_mm, cn)
    runoff_mm[~footprint] = np.nan
    infil_mm = np.where(footprint, p_mm - runoff_mm, np.nan)

    cells = float(footprint.sum())
    area_m2 = cells * cell_m2
    mean_runoff = float(np.nanmean(runoff_mm))
    mean_infil = float(np.nanmean(infil_mm))
    runoff_m3 = mean_runoff / 1000.0 * area_m2
    infil_m3 = mean_infil / 1000.0 * area_m2

    # ------------------------------------------------------------- routing
    vol = np.where(footprint, runoff_mm / 1000.0 * cell_m2, 0.0)
    flow_m3 = route_volumes(fields["flowdir"], vol)

    # peak discharge at the AOI outlet: event runoff over the event window with
    # a mode-appropriate peaking factor (snowmelt: diurnal ~2x; rain: tiered by
    # storm duration — short bursts are peakier).
    out_m3 = float(np.nanmax(flow_m3))
    mean_q = out_m3 / event_seconds
    peak_q = mean_q * peaking
    cfs = 35.3147

    # depression storage filling
    summary1 = json.load(open(os.path.join(D, "hydrology", "summary.json"))) \
        if os.path.exists(os.path.join(D, "hydrology", "summary.json")) else {}
    storage_m3 = summary1.get("depression_storage_m3")

    # --------------------------------------------------------------- layers
    out_dir = os.path.join(D, "hydrology", "local")
    os.makedirs(out_dir, exist_ok=True)
    half = grid["cellsize"] / 2.0
    bounds = [round(grid["minX"] - half, 2), round(grid["minY"] - half, 2),
              round(grid["maxX"] + half, 2), round(grid["maxY"] + half, 2)]

    new_layers = []

    def export(layer_id, label, rgba, values, legend, description, decimals=2):
        png = os.path.join(out_dir, layer_id + ".png")
        with open(os.path.join(out_dir, layer_id + ".grid.json"), "w") as fh:
            json.dump(t1.grid_json(values, bounds, legend, decimals=decimals), fh)
        t1.write_png(rgba, png)
        new_layers.append({
            "id": layer_id, "label": label, "type": "raster",
            "image": "hydrology/local/%s.png" % layer_id,
            "grid": "hydrology/local/%s.grid.json" % layer_id,
            "bounds_local": bounds, "acquisition": "derived",
            "group": "scenario", "scenario": scenario_label,
            "description": description,
        })

    rmax = float(np.nanmax(runoff_mm)) or 1.0
    export("scenario_runoff", "Scenario: runoff generated",
           t1.colorize(np.where(footprint, runoff_mm / rmax, np.nan), RUNOFF_RAMP),
           runoff_mm,
           {"min": {"name": "0 mm runoff", "color": list(RUNOFF_RAMP[1][:3])},
            "max": {"name": "%.0f mm runoff" % rmax, "color": list(RUNOFF_RAMP[4][:3])}},
           "Runoff each cell sheds in this scenario (SCS-CN on the SSURGO "
           "soils) — the rest soaks in. Click for mm.", decimals=1)

    logf = np.log10(np.maximum(flow_m3, 0.01))
    lo, hi = -1.0, math.log10(max(10.0, out_m3))
    norm = np.clip((logf - lo) / (hi - lo), 0, 1)
    norm[flow_m3 < 0.5] = np.nan
    norm[~footprint] = np.nan
    export("scenario_flow", "Scenario: routed flow",
           t1.colorize(norm, t1.FLOW_RAMP), np.where(footprint, flow_m3, np.nan),
           {"min": {"name": "0.5 m³ over event", "color": list(t1.FLOW_RAMP[1][:3])},
            "max": {"name": "%.0f m³ over event" % out_m3, "color": list(t1.FLOW_RAMP[4][:3])}},
           "Total scenario runoff routed down the LiDAR drainage — the event "
           "volume passing each cell. Click for m³.", decimals=1)

    # merge into the simulation catalog (replace previous scenario layers)
    cat_path = os.path.join(D, "hydrology", "simulation-layers.json")
    catalog = {"generated_by": "analyze_hydrology.py", "layers": []}
    if os.path.exists(cat_path):
        try:
            catalog = json.load(open(cat_path))
        except Exception:  # noqa: BLE001
            pass
    catalog["layers"] = [l for l in catalog.get("layers", [])
                         if l.get("group") != "scenario"] + new_layers
    with open(cat_path, "w") as fh:
        json.dump(catalog, fh, indent=2)

    # ----------------------------------------------------------------- result
    scenario_params = {
        "mode": args.mode,
        "rain_in": rain_in,
        "antecedent": args.antecedent, "frozen_ground": bool(args.frozen),
        "label": scenario_label,
    }
    if args.mode == "rain":
        scenario_params["storm_hours"] = storm_hours
    else:
        scenario_params.update({"swe_in": round(swe_in, 1),
                                "swe_mm": round(swe_in * IN_TO_MM, 1),
                                "melt_days": melt_days})
    result = {
        "scenario": scenario_params,
        "climatology": presets,
        "soil_available": bool(soils.get("available")),
        "climate_available": presets is not None,
        "water_input": {
            "total_mm": round(p_mm, 1),
            "total_m3_on_aoi": round(p_mm / 1000.0 * area_m2, 0),
        },
        "partition": {
            "runoff_mm_mean": round(mean_runoff, 1),
            "infiltration_mm_mean": round(mean_infil, 1),
            "runoff_pct": round(100.0 * mean_runoff / p_mm, 1) if p_mm else 0.0,
            "runoff_m3": round(runoff_m3, 0),
            "infiltration_m3": round(infil_m3, 0),
        },
        "outlet": {
            "event_volume_m3": round(out_m3, 0),
            "mean_discharge_m3s": round(mean_q, 4),
            "peak_discharge_m3s_est": round(peak_q, 4),
            "peak_discharge_cfs_est": round(peak_q * cfs, 2),
            "uncertainty": "+/-50%% class (ungauged; SCS-CN event method, "
                           "%.1fx peaking factor)" % peaking,
        },
        "ponding": {
            "depression_storage_m3": storage_m3,
            "storage_filled": (bool(runoff_m3 > storage_m3 * 3)
                               if storage_m3 else None),
            "note": "Routed flow vastly exceeds depression storage in any "
                    "runoff-producing scenario — expect every mapped pond/pool "
                    "to fill." if storage_m3 else None,
        },
        "layers": [l["id"] for l in new_layers],
        "notes": [
            "Geometry (where water concentrates) is the reliable output; "
            "discharge magnitude is scenario-grade, not a forecast.",
            "Outlet discharge is this AOI's own contribution; any watercourse "
            "crossing it drains a basin far beyond the twin's footprint.",
        ] + ([] if soils.get("available") else [
            "No soil data for this twin — runoff uses a uniform woods curve "
            "number (no per-cell hydrologic soil groups); partitioning is "
            "coarser than a soil-resolved run.",
        ]) + ([] if presets is not None else [
            "No climate forcing for this twin — snowmelt/storm presets are "
            "unavailable; supply event depths explicitly.",
        ]),
    }

    # ------------------------------------------------------ store registration
    try:
        store = Store(STORE_PATH)
        run = store.begin_run("hydro_scenario.py", inputs=result["scenario"],
                              notes="snowmelt scenario: " + scenario_label)
        for l in new_layers:
            sha = hashlib.sha1(open(os.path.join(D, l["image"]), "rb").read()).hexdigest()
            store.upsert_layer("hydro_" + l["id"], label=l["label"], kind="raster",
                               acquisition="derived", source_path=l["image"],
                               status="ok", content_sha1=sha)
        store.finish_run(run, notes=json.dumps(result["partition"]))
        store.close()
        result["run_id"] = run
    except Exception as e:  # noqa: BLE001
        result["store_warning"] = str(e)

    # persist for the Simulation window (restores results across page reloads,
    # and feeds the natural-language identify card with scenario context)
    with open(os.path.join(D, "hydrology", "last-scenario.json"), "w") as fh:
        json.dump(result, fh, indent=2)

    if args.json:
        print(json.dumps(result))
    else:
        print("Scenario: %s" % scenario_label)
        print("  water input  %.0f mm  (%.0f m^3 on the AOI)" %
              (p_mm, result["water_input"]["total_m3_on_aoi"]))
        print("  runoff       %.0f mm (%.0f%%)   infiltration %.0f mm" %
              (mean_runoff, result["partition"]["runoff_pct"], mean_infil))
        print("  outlet       %.0f m^3 event volume, peak ~%.2f cfs (+/-50%%)" %
              (out_m3, peak_q * cfs))


if __name__ == "__main__":
    main()
