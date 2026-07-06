#!/usr/bin/env python3
"""One-day-forward actual-ET scenario runner for VEIL twins.

Given scenario weather and the twin's current root-zone state, this computes
screening-grade daily actual evapotranspiration with the same FAO-56 dual crop
coefficient balance used by et_water_balance.py. It is a what-if: it writes only
et/last-et-scenario.json for viewer restore and does not register a store run.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from derive_et0_daily import (  # noqa: E402
    METHODS,
    compute_et0,
    extraterrestrial_radiation_mj,
    lat_elev,
    saturation_vapor_pressure_kpa,
)
import et_water_balance as ewb  # noqa: E402
from et_water_balance import (  # noqa: E402
    DEPLETION_FRACTION,
    canopy_cover_fraction,
    step_day,
    taw_by_mukey,
)
import twin_hydrology as hydro  # noqa: E402

DEFAULT_DATE = "2024-07-15"
SKY_FRACTIONS = {"clear": 1.0, "partly": 0.72, "cloudy": 0.45, "overcast": 0.25}
UNCERTAINTY_NOTE = (
    "Screening-grade daily actual ET (~±20-40%, worse on individual days); "
    "not a flux-tower measurement."
)


def _day(s):
    try:
        d = dt.date.fromisoformat(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc
    if d.isoformat() != s:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    return d


def nonleap_yday(d):
    return max(1, min(365, int(d.timetuple().tm_yday)))


def solar_daylength_seconds(lat_deg, yday):
    phi = math.radians(lat_deg)
    dec = 0.409 * math.sin(2.0 * math.pi * int(yday) / 365.0 - 1.39)
    arg = -math.tan(phi) * math.tan(dec)
    ws = math.acos(max(-1.0, min(1.0, arg)))
    return max(1.0, (24.0 * 3600.0 / math.pi) * ws)


def scenario_srad(lat_deg, elev_m, yday, dayl_s, sky, explicit_srad):
    if explicit_srad is not None:
        return max(0.0, float(explicit_srad))
    ra = extraterrestrial_radiation_mj(lat_deg, yday)
    rso = max(0.0, (0.75 + 2e-5 * elev_m) * ra)
    rs_mj = SKY_FRACTIONS[sky] * rso
    return rs_mj * 1e6 / dayl_s


def actual_vp_pa(tmax_c, tmin_c, rh_pct=None, dewpoint_c=None):
    if dewpoint_c is not None:
        return saturation_vapor_pressure_kpa(float(dewpoint_c)) * 1000.0, (
            "dew point %.1f C" % float(dewpoint_c)
        )
    rh = 45.0 if rh_pct is None else max(1.0, min(100.0, float(rh_pct)))
    tmean = (float(tmax_c) + float(tmin_c)) / 2.0
    ea = (rh / 100.0) * saturation_vapor_pressure_kpa(tmean)
    return ea * 1000.0, "RH %.0f%%" % rh


def scenario_row(args, lat_deg, elev_m):
    d = _day(args.date or DEFAULT_DATE)
    yday = nonleap_yday(d)
    dayl = solar_daylength_seconds(lat_deg, yday)
    srad = scenario_srad(lat_deg, elev_m, yday, dayl, args.sky, args.srad_w_m2)
    vp, humidity_note = actual_vp_pa(args.tmax_c, args.tmin_c, args.rh_pct, args.dewpoint_c)
    return {
        "year": d.year,
        "yday": yday,
        "date": d.isoformat(),
        "prcp": max(0.0, float(args.rain_mm)),
        "tmax": float(args.tmax_c),
        "tmin": float(args.tmin_c),
        "swe": 0.0,
        "srad": srad,
        "dayl": dayl,
        "vp": vp,
    }, humidity_note


def _num(row, key, default=0.0):
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def seed_state(data_dir, soil_state, taw_mm):
    mode = soil_state if soil_state != "auto" else "current"
    if mode == "dry":
        # A stressed-but-not-dead root zone (~80% depleted -> Ks~0.4), not the
        # wilting-point floor (Dr=TAW, Ks=0, AET=0). This shows a meaningful
        # water-limited reduction; the true floor is reachable via a multi-day
        # dry run, which walks Dr up to TAW on its own.
        dr = 0.8 * taw_mm
        return {"dr": dr, "snow_we": 0.0, "recent_eff": []}, {
            "source": "manual dry (stressed) root zone",
            "Dr_mm": round(dr, 2),
            "depletion_pct": round(100.0 * dr / taw_mm) if taw_mm else None,
            "TAW_mm": round(taw_mm, 2),
        }
    if mode == "wet":
        dr = 0.0
        return {"dr": dr, "snow_we": 0.0, "recent_eff": []}, {
            "source": "manual wet root zone",
            "Dr_mm": round(dr, 2),
            "depletion_pct": 0,
            "TAW_mm": round(taw_mm, 2),
        }

    path = os.path.join(data_dir, "et", "soil_water_daily.csv")
    try:
        rows = list(csv.DictReader(open(path)))
    except OSError:
        rows = []
    if rows:
        latest = rows[-1]
        dr = max(0.0, min(taw_mm, _num(latest, "Dr_mm", DEPLETION_FRACTION * taw_mm)))
        snow_we = max(0.0, _num(latest, "snow_we_mm", 0.0))
        recent = [max(0.0, _num(r, "p_eff_mm", 0.0)) for r in rows[-30:]]
        return {"dr": dr, "snow_we": snow_we, "recent_eff": recent}, {
            "source": "latest record %s" % (latest.get("date") or "unknown"),
            "Dr_mm": round(dr, 2),
            "depletion_pct": round(100.0 * dr / taw_mm) if taw_mm else None,
            "TAW_mm": round(taw_mm, 2),
        }

    dr = DEPLETION_FRACTION * taw_mm
    return {"dr": dr, "snow_we": 0.0, "recent_eff": []}, {
        "source": "default stress point (no et/soil_water_daily.csv)",
        "Dr_mm": round(dr, 2),
        "depletion_pct": round(100.0 * dr / taw_mm) if taw_mm else None,
        "TAW_mm": round(taw_mm, 2),
    }


def ring_area(ring):
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][:2]
        x2, y2 = ring[i + 1][:2]
        area += x1 * y2 - x2 * y1
    return 0.5 * area


def polygon_area(coords):
    if not coords:
        return 0.0
    area = abs(ring_area(coords[0]))
    for hole in coords[1:]:
        area -= abs(ring_area(hole))
    return max(0.0, area)


def aoi_area_m2(data_dir):
    path = os.path.join(data_dir, "terrain", "aoi_local.geojson")
    try:
        gj = json.load(open(path))
        area = 0.0
        for feat in gj.get("features", []):
            geom = feat.get("geometry") or {}
            if geom.get("type") == "Polygon":
                area += polygon_area(geom.get("coordinates") or [])
            elif geom.get("type") == "MultiPolygon":
                area += sum(polygon_area(poly) for poly in geom.get("coordinates") or [])
        if area > 0:
            return area
    except (OSError, ValueError, TypeError):
        pass
    grid = hydro.load_grid(data_dir)
    dem = grid["dem"]
    return float((~np.isnan(dem)).sum()) * grid["cellsize"] ** 2


def scenario_label(row, args):
    sky = "custom sun" if args.srad_w_m2 is not None else args.sky
    label = "%.0f/%.0f C %s, %s soil" % (
        row["tmax"], row["tmin"], sky, args.soil_state
    )
    if args.days > 1:
        label += ", %d d" % args.days
    return label


def round_mm(value):
    return round(float(value), 2)


def grid_values_array(grid):
    rows = grid.get("values") or []
    height = int(grid.get("height") or len(rows))
    width = int(grid.get("width") or (max((len(r) for r in rows), default=0)))
    nodata = grid.get("nodata")
    arr = np.full((height, width), np.nan, dtype=float)
    for r, row in enumerate(rows[:height]):
        if not isinstance(row, list):
            continue
        for c, value in enumerate(row[:width]):
            if value is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if nodata is not None:
                try:
                    if v == float(nodata):
                        continue
                except (TypeError, ValueError):
                    if str(value) == str(nodata):
                        continue
            if math.isfinite(v):
                arr[r, c] = v
    return arr


def write_scenario_aet_drape(data_dir, result):
    src = os.path.join(data_dir, "et", "local", "aet_annual.grid.json")
    try:
        annual = json.load(open(src))
    except (OSError, ValueError, TypeError):
        return None

    values = grid_values_array(annual)
    finite = np.isfinite(values)
    if not finite.any():
        return None
    mean_annual = float(np.nanmean(values))
    if not math.isfinite(mean_annual) or mean_annual <= 0:
        return None

    scenario = result.get("scenario") or {}
    headline = result.get("aet", {}).get("mm")
    try:
        headline = float(headline)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(headline):
        return None

    scale = headline / mean_annual
    scenario_values = np.where(finite, values * scale, np.nan)
    finite_out = np.isfinite(scenario_values)
    if not finite_out.any():
        return None
    vmax = float(np.nanmax(scenario_values))
    denom = vmax if math.isfinite(vmax) and vmax > 0 else 1.0
    bounds = annual.get("bounds_local")
    if not (isinstance(bounds, list) and len(bounds) >= 4):
        return None
    bounds = [float(v) for v in bounds[:4]]

    out_dir = os.path.join(data_dir, "et", "local")
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, "scenario_aet.png")
    gj = os.path.join(out_dir, "scenario_aet.grid.json")
    cat_path = os.path.join(data_dir, "et", "et-scenario-layers.json")

    ewb.hydroviz.write_png(
        ewb.hydroviz.colorize(np.clip(scenario_values / denom, 0, 1), ewb.AET_RAMP),
        png,
    )
    legend = {
        "min": {"name": "low simulated ET", "color": list(ewb.AET_RAMP[1][:3])},
        "max": {"name": "%.1f mm/day" % vmax, "color": list(ewb.AET_RAMP[4][:3])},
    }
    metadata = {
        "value_kind": "simulated_actual_evapotranspiration",
        "value_unit": "mm/day",
        "value_classification": "continuous",
        "scenario_label": scenario.get("label"),
        "scenario_date": scenario.get("date"),
        "soil_state": scenario.get("soil_state"),
    }
    with open(gj, "w") as fh:
        json.dump(ewb.hydroviz.grid_json(
            scenario_values, bounds, legend,
            nodata=annual.get("nodata"), decimals=3, metadata=metadata,
        ), fh)

    label = scenario.get("label") or "ET scenario"
    layer = {
        "id": "scenario_aet",
        "label": "Simulated ET · %s" % label,
        "type": "raster",
        "image": "et/local/scenario_aet.png",
        "grid": "et/local/scenario_aet.grid.json",
        "bounds_local": bounds,
        "acquisition": "derived",
        "group": "et_scenario",
        "description": (
            "Simulated daily actual ET for the scenario, distributed by terrain wetness "
            "& soil. Click for mm/day."
        ),
        "value_kind": "simulated_actual_evapotranspiration",
        "value_unit": "mm/day",
        "scenario_label": scenario.get("label"),
        "scenario_date": scenario.get("date"),
    }
    with open(cat_path, "w") as fh:
        json.dump({"generated_by": "et_scenario.py", "layers": [layer]}, fh, indent=2)
    return {"layer_id": "scenario_aet", "grid": "et/local/scenario_aet.grid.json", "label": label}


def run_scenario(args, data_dir):
    lat, elev = lat_elev(data_dir)
    row, humidity_note = scenario_row(args, lat, elev)
    et0_rec = compute_et0([row], lat, elev, u2=max(0.0, float(args.wind_m_s)),
                          wind_clim=None)[0]
    method_vals = [et0_rec[k] for k in METHODS]
    et0 = et0_rec["fao56_pm_reduced_mm"]
    _taw_map, taw_mm, _soil_available = taw_by_mukey(data_dir)
    canopy_frac, _canopy_source = canopy_cover_fraction(data_dir)
    state, seed = seed_state(data_dir, args.soil_state, taw_mm)
    weather_day = {
        "date": row["date"], "year": row["year"], "yday": row["yday"],
        "et0": et0,
        "prcp": row["prcp"], "swe": 0.0,
        "tmean": (row["tmax"] + row["tmin"]) / 2.0,
    }

    recs = []
    for _i in range(max(1, int(args.days))):
        rec, state = step_day(state, weather_day, taw_mm, canopy_frac)
        recs.append(rec)
    first = recs[0]
    last = recs[-1]
    area_m2 = aoi_area_m2(data_dir)
    transpiration = first["Kcb"] * first["Ks"] * et0
    soil_evap = first["Ke"] * et0
    limiting = "snow" if first["snowpack"] else "water" if first["Ks"] < 0.9 else "energy"
    rh_out = None if args.dewpoint_c is not None else (
        45.0 if args.rh_pct is None else max(1.0, min(100.0, float(args.rh_pct)))
    )
    weather = {
        "tmax_c": round(row["tmax"], 2),
        "tmin_c": round(row["tmin"], 2),
        "sky": args.sky,
        "srad_w_m2": round(row["srad"], 1),
    }
    if args.dewpoint_c is not None:
        weather["dewpoint_c"] = round(float(args.dewpoint_c), 2)
    else:
        weather["rh_pct"] = round(rh_out, 1)
    weather["wind_m_s"] = round(max(0.0, float(args.wind_m_s)), 2)
    weather["rain_mm"] = round(row["prcp"], 2)
    result = {
        "scenario": {
            "label": scenario_label(row, args),
            "date": row["date"],
            "yday": row["yday"],
            "weather": weather,
            "soil_state": args.soil_state,
            "days": max(1, int(args.days)),
        },
        "et0": {
            "pm_mm": round_mm(et0),
            "ensemble_mean_mm": round_mm(sum(method_vals) / len(method_vals)),
            "range_mm": [round_mm(min(method_vals)), round_mm(max(method_vals))],
        },
        "aet": {
            "mm": round_mm(first["aet_mm"]),
            "l_per_m2": round_mm(first["aet_mm"]),
            "m3_over_aoi": int(round(first["aet_mm"] / 1000.0 * area_m2)),
            "aoi_area_ha": round(area_m2 / 10000.0, 2),
        },
        "decomposition": {
            "Kcb": round(first["Kcb"], 3),
            "Ke": round(first["Ke"], 3),
            "Ks": round(first["Ks"], 3),
            "transpiration_mm": round_mm(transpiration),
            "soil_evap_mm": round_mm(soil_evap),
            "interception_mm": round_mm(first["interception_mm"]),
            "snowpack": bool(first["snowpack"]),
        },
        "limiting_factor": limiting,
        "seed_state": seed,
        "end_state": {
            "Dr_mm": round(last["Dr_mm"], 2),
            "depletion_pct": round(100.0 * last["Dr_mm"] / taw_mm) if taw_mm else None,
        },
        "series": [
            {"day": i + 1, "aet_mm": round_mm(r["aet_mm"]),
             "Ks": round(r["Ks"], 3), "Dr_mm": round(r["Dr_mm"], 2)}
            for i, r in enumerate(recs)
        ],
        "uncertainty_note": UNCERTAINTY_NOTE,
        "provenance": {
            "et0": "FAO-56 Penman-Monteith",
            "wind": "scenario %.1f m/s" % max(0.0, float(args.wind_m_s)),
            "humidity": humidity_note,
        },
    }
    drape = write_scenario_aet_drape(data_dir, result)
    if drape:
        result["drape"] = drape
    return result


def persist_last(data_dir, result):
    out_dir = os.path.join(data_dir, "et")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "last-et-scenario.json"), "w") as fh:
        json.dump(result, fh, indent=2)


def _test_et0(tmax, tmin, sky, rh, wind):
    args = argparse.Namespace(
        date="2024-07-15", tmax_c=tmax, tmin_c=tmin, sky=sky, srad_w_m2=None,
        rh_pct=rh, dewpoint_c=None, wind_m_s=wind, rain_mm=0.0,
        soil_state="wet", days=1,
    )
    row, _hum = scenario_row(args, 40.0, 1750.0)
    return compute_et0([row], 40.0, 1750.0, u2=wind, wind_clim=None)[0]["fao56_pm_reduced_mm"]


def self_test():
    taw = 66.7
    canopy = 0.25
    et0 = _test_et0(32.0, 15.0, "clear", 25.0, 4.0)
    day = {"date": "2024-07-15", "year": 2024, "yday": 197,
           "et0": et0, "prcp": 0.0, "swe": 0.0, "tmean": 23.5}
    wet, _ = step_day({"dr": 0.0, "snow_we": 0.0, "recent_eff": []}, day, taw, canopy)
    dry, _ = step_day({"dr": taw, "snow_we": 0.0, "recent_eff": []}, day, taw, canopy)
    assert dry["aet_mm"] < wet["aet_mm"], (dry["aet_mm"], wet["aet_mm"])
    hot = _test_et0(34.0, 16.0, "clear", 20.0, 5.0)
    cool = _test_et0(16.0, 8.0, "cloudy", 90.0, 0.5)
    assert hot > cool * 1.5, (hot, cool)
    snow_day = {"date": "2024-01-15", "year": 2024, "yday": 15,
                "et0": 0.8, "prcp": 12.0, "swe": 0.0, "tmean": -4.0}
    snow, _ = step_day({"dr": 0.0, "snow_we": 0.0, "recent_eff": []}, snow_day, taw, canopy)
    assert snow["snowpack"] and snow["Kcb"] == 0.0, snow
    assert snow["Kcb"] * snow["Ks"] * snow_day["et0"] == 0.0
    state = {"dr": 0.0, "snow_we": 0.0, "recent_eff": []}
    series = []
    for _i in range(24):
        rec, state = step_day(state, day, taw, canopy)
        series.append(rec)
    assert series[-1]["Dr_mm"] > series[0]["Dr_mm"], (series[0], series[-1])
    assert series[-1]["aet_mm"] < series[0]["aet_mm"], (series[0], series[-1])
    with tempfile.TemporaryDirectory(prefix="et_scenario_self_test_") as tmp:
        local = os.path.join(tmp, "et", "local")
        os.makedirs(local, exist_ok=True)
        annual = np.array([[1.0, 2.0], [3.0, np.nan]], dtype=float)
        bounds = [0.0, 0.0, 2.0, 2.0]
        with open(os.path.join(local, "aet_annual.grid.json"), "w") as fh:
            json.dump(ewb.hydroviz.grid_json(
                annual, bounds, {"min": {"name": "low"}, "max": {"name": "high"}},
                decimals=1, metadata={"value_unit": "mm/yr"},
            ), fh)
        result = {
            "scenario": {"label": "self-test", "date": "2024-07-15", "soil_state": "wet"},
            "aet": {"mm": 5.0},
        }
        drape = write_scenario_aet_drape(tmp, result)
        assert drape and drape["layer_id"] == "scenario_aet", drape
        written = json.load(open(os.path.join(local, "scenario_aet.grid.json")))
        vals = [float(v) for row in written["values"] for v in row if isinstance(v, (int, float))]
        assert abs((sum(vals) / len(vals)) - result["aet"]["mm"]) <= 0.001, vals
    print("et_scenario.py self-test ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=DEFAULT_DATE, help="scenario date, YYYY-MM-DD")
    ap.add_argument("--tmax-c", type=float, default=30.0)
    ap.add_argument("--tmin-c", type=float, default=15.0)
    ap.add_argument("--sky", choices=sorted(SKY_FRACTIONS), default="clear")
    ap.add_argument("--srad-w-m2", type=float, default=None)
    hum = ap.add_mutually_exclusive_group()
    hum.add_argument("--rh-pct", type=float, default=45.0)
    hum.add_argument("--dewpoint-c", type=float, default=None)
    ap.add_argument("--wind-m-s", type=float, default=2.0)
    ap.add_argument("--rain-mm", type=float, default=0.0)
    ap.add_argument("--soil-state", choices=["current", "dry", "wet", "auto"], default="current")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return
    try:
        args.date = _day(args.date).isoformat()
    except argparse.ArgumentTypeError as exc:
        raise SystemExit(str(exc)) from exc
    args.days = max(1, int(args.days))
    args.rain_mm = max(0.0, float(args.rain_mm))
    args.wind_m_s = max(0.0, float(args.wind_m_s))
    data_dir = os.path.abspath(args.data_dir)
    result = run_scenario(args, data_dir)
    persist_last(data_dir, result)
    if args.json:
        print(json.dumps(result))
    else:
        print("%s: %.2f mm AET (ET0 %.2f mm, Ks %.2f)" % (
            result["scenario"]["label"], result["aet"]["mm"],
            result["et0"]["pm_mm"], result["decomposition"]["Ks"]))


if __name__ == "__main__":
    main()
