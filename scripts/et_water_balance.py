#!/usr/bin/env python3
"""Daily FAO-56-style root-zone water balance for VEIL twins.

Inputs:
  <data>/et/et0_daily.csv
  <data>/climate/daymet_daily.csv
  optional <data>/soils/tabular.json + <data>/soils/features.geojson
  optional LANDFIRE/NLCD canopy rasters

Outputs:
  <data>/et/soil_water_daily.csv
  <data>/et/summary.json
  <data>/et/local/aet_annual.png
  <data>/et/local/aet_annual.grid.json
  <data>/et/et-layers.json

This is an ungauged daily accounting model. Absolute annual AET should be framed
as +/-20-35% absent local validation; relative timing and antecedent-moisture
state are more reliable.
"""

import argparse
import csv
import io
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import analyze_hydrology as hydroviz  # noqa: E402
import twin_hydrology as hydro  # noqa: E402
import twin_store  # noqa: E402

ROOT_DEPTH_CM = 70.0
DEFAULT_TAW_MM = 120.0
DEPLETION_FRACTION = 0.5
FOREST_KCB_MIN = 0.25
FOREST_KCB_MAX = 0.95

# Temperature-index snow model (self-contained, robust to unreliable gridded SWE).
SNOW_TEMP_C = 0.5        # daily-mean temp at/below which precip falls as snow
MELT_DDF_MM_C = 4.0      # degree-day melt factor, mm per deg-C per day (forest-typical 2-6)
MELT_BASE_C = 0.0        # melt base temperature
SNOWPACK_MIN_WE_MM = 5.0  # snow water-equivalent that suppresses transpiration / enables sublimation

AET_RAMP = [(235, 246, 229, 0), (199, 233, 180, 105), (127, 205, 187, 170),
            (65, 182, 196, 220), (34, 94, 168, 245)]


def _daymet_date(year, yday):
    lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    m, d = 1, int(yday)
    for n in lengths:
        if d <= n:
            return "%04d-%02d-%02d" % (int(year), m, d)
        d -= n
        m += 1
    return "%04d-12-31" % int(year)


def _col(row, stem):
    for key, value in row.items():
        if key == stem or key.startswith(stem + " "):
            return value
    return None


def read_daymet(path):
    text = open(path).read()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("year,"))
    rows = {}
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        year, yday = int(_col(r, "year")), int(_col(r, "yday"))
        rows[_daymet_date(year, yday)] = {
            "year": year,
            "yday": yday,
            "prcp": float(_col(r, "prcp")),
            "swe": float(_col(r, "swe") or 0.0),
            "tmean": (float(_col(r, "tmax")) + float(_col(r, "tmin"))) / 2.0,
        }
    return rows


def read_et0(path, prefer="priestley_taylor_mm"):
    rows = []
    for r in csv.DictReader(open(path)):
        if prefer in r and r[prefer] not in (None, ""):
            et0 = float(r[prefer])
            method = prefer
        else:
            vals = [float(r[k]) for k in
                    ("oudin_mm", "hargreaves_samani_mm", "priestley_taylor_mm", "fao56_pm_reduced_mm")
                    if r.get(k) not in (None, "")]
            et0 = sum(vals) / len(vals)
            method = "method_mean_mm"
        rows.append({"date": r["date"], "year": int(r["year"]), "yday": int(r["yday"]),
                     "et0": et0, "et0_method": method})
    return rows


def _horizon_awc_cm(h):
    top = h.get("hzdept_r") or h.get("top_cm") or h.get("hzdept_l") or 0
    bot = h.get("hzdepb_r") or h.get("bottom_cm") or h.get("hzdepb_h") or 0
    awc = h.get("awc_cm_cm") or h.get("awc_r")
    try:
        return max(0.0, min(float(bot), ROOT_DEPTH_CM) - float(top)) * float(awc)
    except (TypeError, ValueError):
        return 0.0


def taw_by_mukey(data_dir):
    path = os.path.join(data_dir, "soils", "tabular.json")
    try:
        tab = json.load(open(path)).get("map_units", {})
    except (OSError, ValueError):
        return {}, DEFAULT_TAW_MM, False
    out = {}
    for mukey, rec in tab.items():
        # TAW is a root-zone quantity (FAO-56): integrate AWC over the effective ROOT
        # depth. Prefer per-horizon AWC clipped to ROOT_DEPTH_CM; fall back to the
        # full-profile awc_profile_cm only when horizons are absent (it overstates the
        # root-zone store, e.g. ~235 mm profile vs ~113 mm over 0-70 cm).
        horizons = rec.get("horizons") or (
            rec.get("components", [{}])[0].get("horizons", []) if rec.get("components") else [])
        cm = sum(_horizon_awc_cm(h) for h in horizons) if horizons else None
        if not cm:
            cm = rec.get("awc_profile_cm")
        try:
            taw = max(25.0, float(cm) * 10.0) if cm is not None else DEFAULT_TAW_MM
        except (TypeError, ValueError):
            taw = DEFAULT_TAW_MM
        out[str(mukey)] = taw
    vals = list(out.values())
    return out, (sum(vals) / len(vals) if vals else DEFAULT_TAW_MM), bool(vals)


def seasonal_kcb(yday):
    # Smooth northern-forest phenology: dormant -> leaf-out -> full canopy -> senescence.
    if yday < 95 or yday > 315:
        return FOREST_KCB_MIN
    if yday < 165:
        return FOREST_KCB_MIN + (FOREST_KCB_MAX - FOREST_KCB_MIN) * (yday - 95) / 70.0
    if yday < 260:
        return FOREST_KCB_MAX
    return FOREST_KCB_MAX - (FOREST_KCB_MAX - FOREST_KCB_MIN) * (yday - 260) / 55.0


def canopy_cover_fraction(data_dir):
    # A materialized Plan carries an authoritative effective canopy summary.
    # Prefer it over the baseline LANDFIRE raster so tree removal/planting is
    # actually represented in planned ET runs.
    planned_meta = os.path.join(data_dir, "vegetation", "metadata.json")
    try:
        meta = json.load(open(planned_meta))
    except (OSError, ValueError):
        meta = {}
    if (meta.get("planned_content_hash") or meta.get("planned_revision_id")) \
            and meta.get("canopy_cover_pct") is not None:
        try:
            value = float(meta["canopy_cover_pct"]) / 100.0
            return max(0.01, min(0.99, value)), "planned effective vegetation canopy"
        except (TypeError, ValueError):
            pass
    for rel in ("atlas/local/landfire_cc_2024.grid.json", "atlas/local/landfire_evc_2024.grid.json"):
        path = os.path.join(data_dir, rel)
        try:
            g = json.load(open(path))
        except (OSError, ValueError):
            continue
        vals = [v for row in g.get("values", []) for v in row
                if isinstance(v, (int, float)) and v != g.get("nodata")]
        if vals:
            return max(0.05, min(0.95, float(np.mean(vals)) / 100.0)), rel
    store_path = os.path.join(data_dir, "twin.gpkg")
    if os.path.exists(store_path):
        try:
            store = twin_store.Store(store_path, journal=False)
            meta = store.get_meta("vegetation") or {}
            store.close()
            if "canopy_cover_pct" in meta:
                return max(0.05, min(0.95, meta["canopy_cover_pct"] / 100.0)), "store.meta vegetation.canopy_cover_pct"
        except Exception:
            pass
    return 0.8, "default_closed_forest"


def wetness_index(values, scale):
    vals = [max(0.0, float(v)) for v in values]
    if not vals:
        return 0.0
    return max(0.0, min(1.0, sum(vals) / scale))


def step_day(state, day, taw_mm, canopy_frac):
    raw = DEPLETION_FRACTION * taw_mm
    dr = state.get("dr", raw)
    snow_we = state.get("snow_we", 0.0)
    recent_eff = state.setdefault("recent_eff", [])
    tmean = day["tmean"]
    # Temperature-index snow partition. Gridded SWE (e.g. Daymet) is unreliable at
    # montane cells (it can carry phantom summer snowpack), so we accumulate our own
    # snowpack from precipitation + daily-mean temperature (both reliable) and let
    # only a genuine cold-season snowpack suppress transpiration.
    if tmean <= SNOW_TEMP_C:
        snow_we += day["prcp"]
        liquid_p, melt = 0.0, 0.0
    else:
        liquid_p = day["prcp"]
        melt = min(snow_we, MELT_DDF_MM_C * max(0.0, tmean - MELT_BASE_C))
        snow_we -= melt
    p_eff = liquid_p + melt
    snowpack = snow_we > SNOWPACK_MIN_WE_MM
    kcb = 0.0 if snowpack else seasonal_kcb(day["yday"]) * (0.65 + 0.35 * canopy_frac)
    ks = 1.0 if dr <= raw else max(0.0, min(1.0, (taw_mm - dr) / max(1e-6, taw_mm - raw)))
    wetted = min(1.0, p_eff / 8.0)
    ke = 0.0 if snowpack else min(0.25, (1.0 - canopy_frac) * 0.6 * wetted)
    interception_capacity = 0.6 + 1.6 * canopy_frac
    interception = min(day["prcp"], interception_capacity) if day["prcp"] > 0 else 0.0
    interception_loss = min(interception, day["et0"] * (0.10 + 0.15 * canopy_frac))
    sublimation = min(snow_we, min(max(0.0, day["et0"] * 0.12), 0.6)) if snowpack else 0.0
    snow_we = max(0.0, snow_we - sublimation)
    transp_soil = (kcb * ks + ke) * day["et0"]
    aet = transp_soil + interception_loss + sublimation
    runoff = max(0.0, 0.04 * max(0.0, p_eff - 8.0) + 0.18 * max(0.0, p_eff - 35.0))
    dr0 = dr
    dr = max(0.0, min(taw_mm, dr + transp_soil - max(0.0, p_eff - runoff)))
    delta_dr = dr - dr0
    # Deep percolation below the root zone = infiltration the soil column could not
    # hold (the depletion clamp overflow). Interception and sublimation are
    # atmospheric losses, not soil percolation, so they are excluded here. This is
    # non-negative by construction; using total AET or the wrong storage sign makes
    # it spuriously negative on dry days.
    recharge = max(0.0, p_eff - runoff - transp_soil + delta_dr)
    recent_eff.append(p_eff)
    if len(recent_eff) > 30:
        recent_eff.pop(0)
    state["dr"] = dr
    state["snow_we"] = snow_we
    rec = {
        "date": day["date"], "year": day["year"], "yday": day["yday"],
        "et0_mm": day["et0"], "aet_mm": max(0.0, aet),
        "prcp_mm": day["prcp"], "snowmelt_mm": melt, "p_eff_mm": p_eff,
        "runoff_mm": runoff, "interception_mm": interception_loss,
        "sublimation_mm": sublimation, "snow_we_mm": snow_we,
        "Dr_mm": dr, "TAW_mm": taw_mm,
        "root_zone_depletion_fraction": dr / taw_mm if taw_mm else None,
        "Ks": ks, "Kcb": kcb, "Ke": ke,
        "wetness_5d": wetness_index(recent_eff[-5:], 25.0),
        "wetness_14d": wetness_index(recent_eff[-14:], 60.0),
        "wetness_30d": wetness_index(recent_eff[-30:], 110.0),
        "recharge_residual_mm": recharge,
        "snowpack": snowpack,
    }
    return rec, state


def run_balance(et0_rows, climate, taw_mm, canopy_frac):
    raw = DEPLETION_FRACTION * taw_mm
    state = {"dr": raw, "snow_we": 0.0, "recent_eff": []}
    rows = []
    annual = {}
    for e in et0_rows:
        c = climate.get(e["date"], {"prcp": 0.0, "swe": 0.0, "tmean": 0.0})
        rec, state = step_day(state, {**e, **c}, taw_mm, canopy_frac)
        rows.append(rec)
        a = annual.setdefault(str(e["year"]), {"P": 0.0, "ET0": 0.0, "AET": 0.0, "runoff": 0.0, "recharge": 0.0})
        a["P"] += c["prcp"]
        a["ET0"] += e["et0"]
        a["AET"] += rec["aet_mm"]
        a["runoff"] += rec["runoff_mm"]
        a["recharge"] += rec["recharge_residual_mm"]
    return rows, annual


def budyko_fu(ai, omega=2.6):
    if ai <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 + ai - (1.0 + ai ** omega) ** (1.0 / omega)))


def summarize(rows, annual, taw_mm, canopy_frac, canopy_source, soil_available, et0_method):
    annual_out = {}
    for year, a in sorted(annual.items()):
        ai = a["ET0"] / a["P"] if a["P"] else None
        expected = budyko_fu(ai) if ai is not None else None
        actual = a["AET"] / a["P"] if a["P"] else None
        annual_out[year] = {
            "precip_mm": round(a["P"], 1),
            "et0_mm": round(a["ET0"], 1),
            "aet_mm": round(a["AET"], 1),
            "aet_over_p": round(actual, 3) if actual is not None else None,
            "deficit_mm": round(max(0.0, a["ET0"] - a["AET"]), 1),
            "modeled_runoff_mm": round(a["runoff"], 1),
            "recharge_residual_mm": round(a["recharge"], 1),
            "budyko_aridity_index": round(ai, 3) if ai is not None else None,
            "budyko_expected_aet_over_p": round(expected, 3) if expected is not None else None,
            "budyko_position": (
                "above_expected" if actual is not None and expected is not None and actual > expected + 0.08
                else "below_expected" if actual is not None and expected is not None and actual < expected - 0.08
                else "near_expected" if actual is not None and expected is not None else None
            ),
        }
    latest = rows[-1] if rows else {}
    return {
        "source": "et_water_balance.py",
        "et0_method": et0_method,
        "soil_available": soil_available,
        "TAW_mm": round(taw_mm, 1),
        "RAW_mm": round(DEPLETION_FRACTION * taw_mm, 1),
        "canopy_cover_fraction": round(canopy_frac, 3),
        "canopy_source": canopy_source,
        "annual": annual_out,
        "latest_antecedent": {k: latest.get(k) for k in
                              ("date", "Dr_mm", "TAW_mm", "root_zone_depletion_fraction",
                               "Ks", "wetness_5d", "wetness_14d", "wetness_30d")},
        "uncertainty_note": (
            "Annual AET is +/-20-35% absent local validation. Root depth, forest "
            "Kcb, interception and snow sublimation are conservative defaults; "
            "relative timing and antecedent wetness are more reliable than the "
            "absolute annual flux."
        ),
    }


def write_daily(rows, path):
    fields = ["date", "year", "yday", "et0_mm", "aet_mm", "prcp_mm", "snowmelt_mm",
              "p_eff_mm", "runoff_mm", "interception_mm", "sublimation_mm", "snow_we_mm",
              "Dr_mm", "TAW_mm", "root_zone_depletion_fraction", "Ks", "Kcb", "Ke",
              "wetness_5d", "wetness_14d", "wetness_30d", "recharge_residual_mm",
              "snowpack"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: round(r[k], 4) if isinstance(r.get(k), float) else r.get(k) for k in fields})


def export_aet_layer(data_dir, annual_mean_aet, taw_by_key, mean_taw):
    grid = hydro.load_grid(data_dir)
    fields = hydro.compute_all(grid)
    footprint = np.isfinite(fields["dem"])
    hydroviz._use_data_dir(data_dir)
    hydroviz.twin_georef.GEOREF_PATH = os.path.join(data_dir, "georef.json")
    soils = hydroviz.soil_fields(grid)
    taw = np.full(fields["dem"].shape, mean_taw, dtype=float)
    if soils.get("available"):
        for r in range(taw.shape[0]):
            for c in range(taw.shape[1]):
                mk = soils["mukey"][r, c]
                if mk is not None:
                    taw[r, c] = taw_by_key.get(str(mk), mean_taw)
    twi_norm = hydroviz.percentile_norm(fields["twi"], footprint)
    modifier = 0.85 + 0.25 * np.nan_to_num(twi_norm) + 0.15 * np.clip((taw - mean_taw) / max(mean_taw, 1.0), -0.5, 0.8)
    modifier = np.clip(modifier, 0.65, 1.25)
    values = np.where(footprint, annual_mean_aet * modifier / np.nanmean(modifier[footprint]), np.nan)
    out_dir = os.path.join(data_dir, "et", "local")
    os.makedirs(out_dir, exist_ok=True)
    half = grid["cellsize"] / 2.0
    bounds = [round(grid["minX"] - half, 2), round(grid["minY"] - half, 2),
              round(grid["maxX"] + half, 2), round(grid["maxY"] + half, 2)]
    vmax = float(np.nanmax(values)) if np.isfinite(values).any() else max(1.0, annual_mean_aet)
    png = os.path.join(out_dir, "aet_annual.png")
    gj = os.path.join(out_dir, "aet_annual.grid.json")
    hydroviz.write_png(hydroviz.colorize(np.clip(values / vmax, 0, 1), AET_RAMP), png)
    with open(gj, "w") as fh:
        json.dump(hydroviz.grid_json(
            values, bounds,
            {"min": {"name": "low annual AET", "color": list(AET_RAMP[1][:3])},
             "max": {"name": "%.0f mm/yr" % vmax, "color": list(AET_RAMP[4][:3])}},
            decimals=1,
            metadata={"value_kind": "annual_actual_evapotranspiration",
                      "value_unit": "mm/yr", "value_classification": "continuous"}),
            fh)
    layer = {
        "id": "aet_annual", "label": "Annual actual ET", "type": "raster",
        "image": "et/local/aet_annual.png",
        "grid": "et/local/aet_annual.grid.json",
        "bounds_local": bounds, "acquisition": "derived",
        "group": "water_balance",
        "description": "FAO-56-style root-zone water-balance AET, distributed by terrain wetness and soil available water. Click for annual mm.",
        "value_kind": "annual_actual_evapotranspiration",
        "value_unit": "mm/yr",
    }
    cat = {"generated_by": "et_water_balance.py",
           "note": "Derived ET/water-balance layers. Annual AET carries +/-20-35% uncertainty absent local validation.",
           "layers": [layer]}
    cat_path = os.path.join(data_dir, "et", "et-layers.json")
    with open(cat_path, "w") as fh:
        json.dump(cat, fh, indent=2)
    return [png, gj, cat_path], layer


def register_outputs(data_dir, paths, layer, summary):
    store_path = os.path.join(data_dir, "twin.gpkg")
    if not os.path.exists(store_path):
        return None
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")
    store = twin_store.Store(store_path)
    run = store.begin_run("et_water_balance.py", inputs=paths,
                          notes="daily root-zone soil-water balance")
    eid = "et_dataset:soil_water_daily"
    store.upsert_entity(eid, "et_dataset", run)
    store.observe(eid, "summary", summary.get("annual"), run, source="et_water_balance.py")
    for path in paths:
        rel = os.path.relpath(path, data_dir)
        ext = os.path.splitext(path)[1].lower()
        store.upsert_layer("et_" + os.path.splitext(os.path.basename(path))[0],
                           label=os.path.basename(path),
                           kind="raster" if ext == ".png" else "table" if ext == ".csv" else "json",
                           acquisition="derived", source_path=rel,
                           status="ok", content_sha1=twin_store.sha1_file(path))
    store.upsert_layer("et_" + layer["id"], label=layer["label"], kind="raster",
                       acquisition="derived", source_path=layer["image"],
                       status="ok", content_sha1=twin_store.sha1_file(os.path.join(data_dir, layer["image"])))
    store.finish_run(run, notes="wrote ET soil-water balance and AET layer")
    store.close()
    return run


def self_test():
    climate = {}
    et0 = []
    for d in range(1, 366):
        date = _daymet_date(2020, d)
        et = max(0.2, 2.2 + 2.0 * math.sin(2 * math.pi * (d - 100) / 365))
        et0.append({"date": date, "year": 2020, "yday": d, "et0": et, "et0_method": "synthetic"})
        # seasonal temperature so winter (< SNOW_TEMP_C) accumulates snow that melts in spring
        tmean = 8.0 + 14.0 * math.sin(2 * math.pi * (d - 105) / 365)  # ~ -6 .. 22 C
        # a bogus perennial gridded SWE (summer included) must NOT suppress summer transpiration
        climate[date] = {"prcp": 4.0 if d % 2 == 0 else 0.3, "swe": 200.0, "tmean": tmean}
    rows, annual = run_balance(et0, climate, 120.0, 0.8)
    assert len(rows) == 365
    assert 250.0 < annual["2020"]["AET"] < 950.0, ("AET", annual["2020"]["AET"])
    assert all(0.0 <= r["Ks"] <= 1.0 for r in rows)
    # Temperature-index snow must form in winter and melt, and must NOT persist through
    # summer despite the bogus perennial gridded SWE input.
    assert any(r["snowpack"] for r in rows), "winter snowpack should form"
    assert any(r["snowmelt_mm"] > 0 for r in rows), "snow should melt"
    summer = [r for r in rows if 152 <= r["yday"] <= 244]
    assert not any(r["snowpack"] for r in summer), "no summer snowpack from bad SWE"
    assert sum(r["aet_mm"] for r in summer) > 150.0, "summer transpiration must proceed"
    # Recharge is deep percolation and must be non-negative; a storage-sign error makes
    # it strongly negative on dry days.
    assert all(r["recharge_residual_mm"] >= -1e-6 for r in rows), "recharge sign/closure"
    assert annual["2020"]["recharge"] >= -1.0, ("annual recharge", annual["2020"]["recharge"])
    print("et_water_balance.py self-test ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--et0-method", default="priestley_taylor_mm",
                    help="ET0 column to use; falls back to method mean")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    data_dir = os.path.abspath(args.data_dir)
    et0_path = os.path.join(data_dir, "et", "et0_daily.csv")
    daymet_path = os.path.join(data_dir, "climate", "daymet_daily.csv")
    if not os.path.exists(et0_path):
        raise SystemExit("missing %s; run derive_et0_daily.py first" % et0_path)
    if not os.path.exists(daymet_path):
        raise SystemExit("missing %s; climate forcing is required" % daymet_path)
    et0 = read_et0(et0_path, args.et0_method)
    climate = read_daymet(daymet_path)
    taw_map, mean_taw, soil_available = taw_by_mukey(data_dir)
    canopy_frac, canopy_source = canopy_cover_fraction(data_dir)
    rows, annual = run_balance(et0, climate, mean_taw, canopy_frac)
    et_dir = os.path.join(data_dir, "et")
    os.makedirs(et_dir, exist_ok=True)
    daily_path = os.path.join(et_dir, "soil_water_daily.csv")
    write_daily(rows, daily_path)
    summary = summarize(rows, annual, mean_taw, canopy_frac, canopy_source,
                        soil_available, et0[0]["et0_method"] if et0 else args.et0_method)
    summary_path = os.path.join(et_dir, "summary.json")
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    annual_mean_aet = np.mean([v["aet_mm"] for v in summary["annual"].values()]) if summary["annual"] else 0.0
    layer_paths, layer = export_aet_layer(data_dir, annual_mean_aet, taw_map, mean_taw)
    run = register_outputs(data_dir, [daily_path, summary_path, *layer_paths], layer, summary)
    print("Wrote %s (%d records)" % (os.path.relpath(daily_path, data_dir), len(rows)))
    print("Wrote %s" % os.path.relpath(summary_path, data_dir))
    print("Wrote %s and %s" % (layer["image"], layer["grid"]))
    if run:
        print("Registered ET water-balance outputs in store (run %d)" % run)


if __name__ == "__main__":
    main()
