#!/usr/bin/env python3
"""Tier-2 wildfire ignition/weather scenario exporter.

This mirrors ``hydro_scenario.py``: parse a reproducible scenario, run the
pure-numpy fire engine over the DEM-grid fuelscape, write scenario drapes into
``data/fire/local/``, merge only the ``fire_scenario`` group into
``data/fire/fire-layers.json``, persist ``last-fire-scenario.json``, and
register the run/layers in the twin store.
"""

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import analyze_fuels as fuels_t1
import analyze_hydrology as t1
import hydro_fire
import twin_fire
import twin_georef
import twin_hydrology as hydro
import twin_store
from twin_store import Store

D = os.path.join(PROJECT, "data")
STORE_PATH = os.path.join(D, "twin.gpkg")

DEFAULT_DATE = "2025-05-21"
WEATHER_CLASSES = ("normal_spring", "high_spring", "extreme_redflag",
                   "summer_drought", "dormant_fall", "custom")
WEATHER_PRESETS = {
    "normal_spring": {
        "label": "Normal spring",
        "date": "2025-04-20",
        "temp_f": 60.0,
        "rh_min": 45.0,
        "wind_mph": 8.0,
        "wind_dir": 0.0,
        "days_since_rain": 5.0,
        "drought": "normal",
        "phenology": "normal_spring",
    },
    "high_spring": {
        "label": "High - dry windy spring",
        "date": "2025-05-10",
        "temp_f": 68.0,
        "rh_min": 30.0,
        "wind_mph": 15.0,
        "wind_dir": 0.0,
        "days_since_rain": 10.0,
        "drought": "dry",
        "phenology": "high_spring",
    },
    "extreme_redflag": {
        "label": "Extreme - spring Red Flag",
        "date": "2025-05-21",
        "temp_f": 78.0,
        "rh_min": 15.0,
        "wind_mph": 25.0,
        "wind_dir": 0.0,
        "days_since_rain": 21.0,
        "drought": "severe",
        "phenology": "extreme_redflag",
    },
    "summer_drought": {
        "label": "Late-summer drought",
        "date": "2025-08-15",
        "temp_f": 85.0,
        "rh_min": 25.0,
        "wind_mph": 18.0,
        "wind_dir": 0.0,
        "days_since_rain": 14.0,
        "drought": "extreme",
        "phenology": "summer_drought",
    },
    "dormant_fall": {
        "label": "Dormant fall",
        "date": "2025-10-20",
        "temp_f": 55.0,
        "rh_min": 30.0,
        "wind_mph": 12.0,
        "wind_dir": 0.0,
        "days_since_rain": 8.0,
        "drought": "normal",
        "phenology": "dormant_fall",
    },
    "custom": {
        "label": "Custom weather",
        "date": DEFAULT_DATE,
        "temp_f": 60.0,
        "rh_min": 45.0,
        "wind_mph": 8.0,
        "wind_dir": 0.0,
        "days_since_rain": 5.0,
        "drought": "normal",
        "phenology": "normal_spring",
    },
}

ARRIVAL_RAMP = [(255, 247, 188, 230), (254, 196, 79, 215),
                (236, 112, 20, 200), (189, 54, 47, 215),
                (96, 20, 55, 230)]
FLAME_RAMP = [(255, 245, 200, 0), (255, 214, 102, 115),
              (245, 126, 60, 185), (210, 54, 49, 230),
              (88, 24, 69, 250)]
INTENSITY_RAMP = [(255, 250, 205, 0), (255, 213, 96, 120),
                  (244, 126, 60, 190), (196, 40, 44, 230),
                  (72, 24, 80, 250)]
EMBER_RAMP = [(255, 247, 188, 0), (255, 184, 77, 130),
              (221, 82, 55, 210), (106, 35, 88, 235)]
CROWN_LEGEND = fuels_t1.CROWN_LEGEND
CROWN_COLORS = fuels_t1.CROWN_COLORS
BROADLEAF_LITTER_CODES = {182, 186, 189}


def _use_data_dir(data_dir):
    global D, STORE_PATH
    D = os.path.abspath(data_dir)
    STORE_PATH = os.path.join(D, "twin.gpkg")
    twin_store.JOURNAL_DIR = os.path.join(D, "journal")
    twin_georef.GEOREF_PATH = os.path.join(D, "georef.json")
    t1._use_data_dir(D)
    fuels_t1._use_data_dir(D)


def _clamp(v, lo, hi):
    return min(hi, max(lo, float(v)))


def _parse_date(value):
    dt = datetime.strptime(value, "%Y-%m-%d")
    return value, int(dt.strftime("%j"))


def _resolve_weather(args):
    class_name = args.weather_class or "normal_spring"
    preset = dict(WEATHER_PRESETS[class_name])
    date_value = args.date or (preset["date"] if args.weather_class else DEFAULT_DATE)
    date_value, doy = _parse_date(date_value)

    temp_f = preset["temp_f"] if args.temp_f is None else args.temp_f
    rh_min = preset["rh_min"] if args.rh_min is None else args.rh_min
    wind_mph = preset["wind_mph"] if args.wind_mph is None else args.wind_mph
    wind_dir = preset["wind_dir"] if args.wind_dir is None else args.wind_dir
    days_since_rain = (preset["days_since_rain"] if args.days_since_rain is None
                       else args.days_since_rain)
    drought = preset["drought"] if args.drought is None else args.drought
    exposure = args.exposure

    return {
        "weather_class": class_name,
        "weather_label": preset["label"],
        "date": date_value,
        "doy": doy,
        "temp_f": round(_clamp(temp_f, -20.0, 130.0), 3),
        "rh_min": round(_clamp(rh_min, 1.0, 100.0), 3),
        "wind_mph": round(_clamp(wind_mph, 0.0, 120.0), 3),
        "wind_dir": round(float(wind_dir) % 360.0, 3),
        "days_since_rain": round(_clamp(days_since_rain, 0.0, 120.0), 3),
        "drought": drought,
        "exposure": exposure,
        "phenology": preset["phenology"],
        "duration_min": round(_clamp(args.duration_min, 1.0, 1440.0), 3),
    }


def _terrain_aspect_radians(dem, cellsize):
    """Downslope aspect as clockwise azimuth radians from north."""
    filled = np.where(np.isfinite(dem), dem, np.nan)
    p = np.pad(filled, 1, mode="edge")
    for _ in range(2):
        nan = ~np.isfinite(p)
        if not nan.any():
            break
        sm = np.copy(p)
        sm[nan] = 0.0
        p[nan] = (np.roll(sm, 1, 0) + np.roll(sm, -1, 0) +
                  np.roll(sm, 1, 1) + np.roll(sm, -1, 1))[nan] / 4.0
    z = p
    dzdx = (z[1:-1, 2:] - z[1:-1, :-2]) / (2.0 * cellsize)
    dz_south = (z[2:, 1:-1] - z[:-2, 1:-1]) / (2.0 * cellsize)
    aspect = np.mod(np.arctan2(-dzdx, dz_south), 2.0 * math.pi)
    aspect[~np.isfinite(dem)] = np.nan
    return aspect


def _categorical_grid_json(values, bounds, legend, nodata=None, metadata=None):
    rows = []
    arr = np.asarray(values)
    for r in range(arr.shape[0]):
        row = []
        for v in arr[r]:
            if not np.isfinite(v):
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


def _categorical_rgba(values, footprint, color_map):
    rgba = np.zeros(values.shape + (4,), dtype=np.uint8)
    finite = footprint & np.isfinite(values)
    codes = np.zeros(values.shape, dtype=int)
    codes[finite] = values[finite].astype(int)
    for code in np.unique(codes[finite]):
        color = color_map.get(int(code))
        if color:
            rgba[finite & (codes == int(code))] = color
    return rgba


def _percentile_rgba(values, mask, ramp):
    return t1.colorize(t1.percentile_norm(values, mask), ramp)


def _arrival_rgba(values, burned, duration_min):
    norm = np.where(burned, np.clip(values / max(1.0, duration_min), 0.0, 1.0), np.nan)
    return t1.colorize(norm, ARRIVAL_RAMP)


def _scene_to_cell(grid, x, y):
    col = int(round((x - grid["minX"]) / grid["xstep"]))
    row = int(round((grid["maxY"] - y) / grid["ystep"]))
    col = min(grid["width"] - 1, max(0, col))
    row = min(grid["height"] - 1, max(0, row))
    return row, col


def _cell_to_scene(grid, row, col):
    return (grid["minX"] + col * grid["xstep"],
            grid["maxY"] - row * grid["ystep"])


def _line_cells(grid, line_text):
    points = []
    for part in line_text.split(";"):
        part = part.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split(",")]
        if len(bits) != 2:
            raise ValueError("ignition-line points must be x,y pairs")
        points.append((float(bits[0]), float(bits[1])))
    if len(points) < 2:
        raise ValueError("ignition-line requires at least two points")

    cells = []
    step = max(0.5, grid["cellsize"] * 0.5)
    for a, b in zip(points[:-1], points[1:]):
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(math.ceil(dist / step)))
        for i in range(n + 1):
            f = i / n
            x = a[0] + (b[0] - a[0]) * f
            y = a[1] + (b[1] - a[1]) * f
            cells.append(_scene_to_cell(grid, x, y))
    seen = set()
    out = []
    for cell in cells:
        if cell not in seen:
            seen.add(cell)
            out.append(cell)
    return points, out


# A structure fire, burn barrel, equipment spark, or campfire on developed ground
# is a primary wildfire ignition source on a rural parcel, so an ignition on a
# nonburnable cell is ALLOWED: the fire is seeded in the nearest wildland fuel
# within this radius -- the home-ignition-zone distance over which a structure
# fire ignites adjacent vegetation. Only a spot with NO fuel within this radius
# cannot carry into the wildland and errors.
IGNITION_SNAP_MAX_M = 60.0


def _snap_to_burnable(grid, row, col, footprint, burnable):
    """Nearest (row, col, distance_m) burnable in-footprint cell to seed the fire.

    (row, col, 0.0) if the clicked cell is itself burnable; None if the nearest
    wildland fuel is farther than IGNITION_SNAP_MAX_M (a fire there cannot reach
    vegetation to become a wildfire).
    """
    ok = footprint & burnable
    if ok[row, col]:
        return int(row), int(col), 0.0
    rr, cc = np.where(ok)
    if rr.size == 0:
        return None
    d2 = (rr - row) ** 2 + (cc - col) ** 2
    i = int(np.argmin(d2))
    dist_m = float(np.sqrt(d2[i])) * grid["cellsize"]
    if dist_m > IGNITION_SNAP_MAX_M:
        return None
    return int(rr[i]), int(cc[i]), dist_m


def _validate_ignition(args, grid, footprint, burnable):
    bounds_error = ("ignition is outside the terrain grid bounds "
                    "[%.2f, %.2f] x [%.2f, %.2f]" %
                    (grid["minX"], grid["maxX"], grid["minY"], grid["maxY"]))

    if args.ignition_line:
        points, cells = _line_cells(grid, args.ignition_line)
        for x, y in points:
            if not (grid["minX"] <= x <= grid["maxX"] and
                    grid["minY"] <= y <= grid["maxY"]):
                raise ValueError(bounds_error)
        for r, c in cells:
            if not footprint[r, c]:
                raise ValueError("ignition line crosses outside the DEM footprint")
            if not burnable[r, c]:
                raise ValueError("ignition line crosses a nonburnable fuel cell")
        mask = np.zeros(footprint.shape, dtype=bool)
        for r, c in cells:
            mask[r, c] = True
        rep_row, rep_col = cells[0]
        rep_x, rep_y = points[0]
        return mask, {
            "type": "line",
            "line": [{"x": round(float(x), 3), "y": round(float(y), 3)}
                     for x, y in points],
            "cell_count": int(mask.sum()),
            "row": int(rep_row),
            "col": int(rep_col),
            "x": round(float(rep_x), 3),
            "y": round(float(rep_y), 3),
        }

    if args.ignition_x is None or args.ignition_y is None:
        raise ValueError("supply --ignition-x/--ignition-y or --ignition-line")
    x = float(args.ignition_x)
    y = float(args.ignition_y)
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError("ignition coordinates must be finite numbers")
    if not (grid["minX"] <= x <= grid["maxX"] and grid["minY"] <= y <= grid["maxY"]):
        raise ValueError(bounds_error)
    row, col = _scene_to_cell(grid, x, y)
    snap = _snap_to_burnable(grid, row, col, footprint, burnable)
    if snap is None:
        raise ValueError(
            "no wildland fuel within %.0f m of the ignition - a fire here "
            "(developed ground, open water, or bare rock) has no vegetation "
            "close enough to carry into a wildfire" % IGNITION_SNAP_MAX_M)
    srow, scol, snap_m = snap
    mask = np.zeros(footprint.shape, dtype=bool)
    mask[srow, scol] = True
    cx, cy = _cell_to_scene(grid, srow, scol)
    ignition = {
        "type": "point",
        "x": round(x, 3),
        "y": round(y, 3),
        "cell_center_x": round(float(cx), 3),
        "cell_center_y": round(float(cy), 3),
        "row": int(srow),
        "col": int(scol),
    }
    if snap_m > 0.0:
        # Ignition on developed/nonburnable ground: a structure/equipment fire
        # that carries into the nearest wildland fuel and spreads from there.
        ignition["source_on_nonburnable"] = True
        ignition["fuel_seed_distance_m"] = round(snap_m, 1)
        ignition["source_note"] = (
            "ignition source on developed ground (e.g., a structure or cleared "
            "yard); the fire reaches wildland fuel ~%.0f m away and spreads "
            "from there" % snap_m)
    return mask, ignition


def _projected_lonlat(x, y):
    georef_path = twin_georef.GEOREF_PATH
    origin_x, origin_y = twin_georef.origin(path=georef_path)
    to_geo, _from_geo = twin_georef.transformers(path=georef_path)
    lon, lat = to_geo.transform(origin_x + x, origin_y + y)
    return float(lon), float(lat)


def _crown_fractions(crown, mask):
    total = int(mask.sum())
    out = {}
    for cls, name in [(0, "surface"), (1, "passive"), (2, "active")]:
        count = int((mask & (crown == cls)).sum())
        out[name] = {"count": count,
                     "fraction": round(count / max(1, total), 4)}
    return out


def _crown_applicable_mask(footprint, canopy, fbfm_codes):
    canopy_ok = (footprint & (canopy["cbh_m"] > 0.0) & (canopy["cbd_kg_m3"] > 0.0) &
                 np.isfinite(canopy["cbh_m"]) & np.isfinite(canopy["cbd_kg_m3"]))
    broadleaf_litter = np.isin(np.asarray(fbfm_codes).astype(int),
                               list(BROADLEAF_LITTER_CODES))
    return canopy_ok & ~broadleaf_litter


def _ros_ellipse_at(row, col, ros, eff_wind, max_dir):
    head = float(ros[row, col])
    eff_mph = float(eff_wind[row, col] / twin_fire.MPH_TO_M_MIN)
    lw = float(np.asarray(twin_fire.ellipse_lw(eff_mph)))
    ecc = math.sqrt(max(0.0, 1.0 - 1.0 / (lw * lw)))
    flank = head * math.sqrt(max(0.0, (1.0 - ecc) / max(1e-12, 1.0 + ecc)))
    back = head * (1.0 - ecc) / max(1e-12, 1.0 + ecc)
    return {
        "head_m_min": round(head, 4),
        "flank_m_min": round(float(flank), 4),
        "back_m_min": round(float(back), 4),
        "effective_wind_mph": round(eff_mph, 3),
        "length_to_breadth": round(lw, 3),
        "max_spread_dir_deg": round(math.degrees(float(max_dir[row, col])) % 360.0, 2),
    }


def _result_error(args, message, **extra):
    result = {"ok": False, "error": message}
    result.update(extra)
    if args.json:
        print(json.dumps(result))
        return 0
    print("fire scenario error: %s" % message, file=sys.stderr)
    return 2


def _metadata(value_kind, value_unit, cell_area_m2, scenario):
    return {
        "value_kind": value_kind,
        "value_unit": value_unit,
        "cell_area_m2": round(float(cell_area_m2), 4),
        "scenario": scenario["weather_label"],
        "weather_class": scenario["weather_class"],
    }


def _fuel_shift_summary(provenance):
    shift = dict(provenance.get("fuel_model_shift") or {})
    if not shift:
        return {
            "basis": "30 m LANDFIRE FBFM40 cells",
            "total_cells": 0,
            "changed_cells": 0,
            "changed_fraction": 0.0,
            "top_transitions": [],
        }
    shift["top_transitions"] = list(shift.get("top_transitions") or [])[:12]
    return shift


def _hydrology_summary(enabled, provenance):
    if not enabled:
        return {
            "on": False,
            "sources_used": [],
            "barrier_cells": 0,
            "wet_cells": 0,
            "drought_scaling": None,
            "note": "hydrology influence disabled for this comparison run",
        }
    prov = dict(provenance or {})
    notes = list(prov.get("notes") or [])
    source_ids = []
    for source in prov.get("sources_used", []):
        sid = source.get("id")
        if sid and sid not in source_ids:
            source_ids.append(sid)
    return {
        "on": True,
        "sources_used": source_ids,
        "source_details": prov.get("sources_used", []),
        "sources_found": prov.get("sources_found", []),
        "barrier_cells": int(prov.get("barrier_cells") or 0),
        "wet_cells": int(prov.get("wet_cells") or 0),
        "wet_cells_by_class": prov.get("wet_cells_by_class", {}),
        "barrier_cells_by_source": prov.get("barrier_cells_by_source", {}),
        "wet_cells_by_source": prov.get("wet_cells_by_source", {}),
        "drought": prov.get("drought"),
        "drought_scaling": prov.get("drought_scaling"),
        "screening_grade": bool(prov.get("screening_grade", True)),
        "note": "; ".join(notes[:5]),
        "notes": notes,
    }


def _capped_effective_wind(eff_wind, reaction_kw_m2):
    """Effective wind for ellipse shape after Andrews' reaction-intensity cap."""
    eff = np.asarray(eff_wind, dtype=float)
    reaction = np.asarray(reaction_kw_m2, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        reaction_btu = reaction / twin_fire.BTU_FT2_MIN_TO_KW_M2
        max_eff = 0.9 * reaction_btu * twin_fire.FT_TO_M
    capped = np.where((eff > max_eff) & (max_eff > 0.0), max_eff, eff)
    return np.where(np.isfinite(capped), capped, 0.0)


def _angle_delta(a, b):
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def _ember_exposure(grid, footprint, burned, flame, intensity, crown, scenario):
    """Downwind ember-exposure band for barriers/gaps; not an ignition solver."""
    source = (burned & footprint &
              ((crown >= 1) | (flame >= 1.2) | (intensity >= 500.0)))
    wind_mph = float(scenario["wind_mph"])
    if not source.any() or wind_mph < 1.0:
        return np.zeros(footprint.shape, dtype=bool), {
            "mode": "screening_exposure_band",
            "source_cells": int(source.sum()),
            "exposed_cells": 0,
            "max_downwind_distance_m": 0.0,
            "note": "no ember exposure band: no strong burned source cells or no wind",
        }

    max_source_flame = float(np.nanmax(np.where(source, flame, np.nan)))
    active = bool((source & (crown >= 2)).any())
    passive = bool((source & (crown == 1)).any())
    base_m = 60.0
    if max_source_flame >= 2.4:
        base_m = 120.0
    if passive:
        base_m = max(base_m, 180.0)
    if active:
        base_m = max(base_m, 320.0)
    wind_factor = np.clip(wind_mph / 20.0, 0.5, 2.5)
    max_m = min(800.0, base_m * float(wind_factor))
    half_angle = math.radians(30.0 if wind_mph < 10.0 else 22.5)
    wind_dir = math.radians(float(scenario["wind_dir"]))

    h, w = footprint.shape
    cs = float(grid["cellsize"])
    max_cells = int(math.ceil(max_m / max(cs, 1e-9)))
    exposed = np.zeros((h, w), dtype=bool)
    for dr in range(-max_cells, max_cells + 1):
        for dc in range(-max_cells, max_cells + 1):
            if dr == 0 and dc == 0:
                continue
            dist = math.hypot(float(dr), float(dc)) * cs
            if dist <= 0.0 or dist > max_m:
                continue
            bearing = math.atan2(float(dc), float(-dr))
            if abs(_angle_delta(bearing, wind_dir)) > half_angle:
                continue
            src_r0 = max(0, -dr)
            src_r1 = min(h, h - dr)
            src_c0 = max(0, -dc)
            src_c1 = min(w, w - dc)
            if src_r0 >= src_r1 or src_c0 >= src_c1:
                continue
            tgt_r0 = src_r0 + dr
            tgt_r1 = src_r1 + dr
            tgt_c0 = src_c0 + dc
            tgt_c1 = src_c1 + dc
            exposed[tgt_r0:tgt_r1, tgt_c0:tgt_c1] |= source[src_r0:src_r1, src_c0:src_c1]
    exposed &= footprint & ~burned
    return exposed, {
        "mode": "screening_exposure_band",
        "source_cells": int(source.sum()),
        "exposed_cells": int(exposed.sum()),
        "max_downwind_distance_m": round(float(max_m), 1),
        "cone_half_angle_deg": round(math.degrees(half_angle), 1),
        "note": (
            "Downwind ember exposure band only; not a stochastic Albini/BehavePlus "
            "spot-fire ignition or recursive spread solver. Treat exposed cells "
            "behind barriers as potentially vulnerable to firebrands."),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ignition-x", type=float, default=None)
    ap.add_argument("--ignition-y", type=float, default=None)
    ap.add_argument("--ignition-line", default=None,
                    help='scene-local polyline "x1,y1;x2,y2;..."')
    ap.add_argument("--date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--weather-class", choices=WEATHER_CLASSES, default=None)
    ap.add_argument("--temp-f", type=float, default=None)
    ap.add_argument("--rh-min", type=float, default=None)
    ap.add_argument("--wind-mph", type=float, default=None)
    ap.add_argument("--wind-dir", type=float, default=None,
                    help="downwind / maximum-spread azimuth, degrees clockwise from north")
    ap.add_argument("--days-since-rain", type=float, default=None)
    ap.add_argument("--drought", choices=["normal", "dry", "severe", "extreme"],
                    default=None)
    ap.add_argument("--exposure", choices=["shaded", "mixed", "open"],
                    default="shaded")
    ap.add_argument("--fmc-override", type=float, default=None)
    ap.add_argument("--duration-min", type=float, default=240.0)
    ap.add_argument("--fuel-source", choices=("landfire", "computed"),
                    default="landfire")
    ap.add_argument("--hydrology", choices=("on", "off"), default="on",
                    help="apply twin hydrology as water barriers and wet-fuel moisture uplift")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dump-presets", action="store_true",
                    help="print WEATHER_PRESETS as JSON and exit")
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"))
    args = ap.parse_args()

    if args.dump_presets:
        print(json.dumps(WEATHER_PRESETS))
        return 0

    if args.data_dir:
        _use_data_dir(args.data_dir)
    else:
        twin_georef.GEOREF_PATH = os.path.join(D, "georef.json")

    try:
        scenario = _resolve_weather(args)
    except Exception as e:  # noqa: BLE001
        return _result_error(args, str(e))

    fire_dir = os.path.join(D, "fire")
    out_dir = os.path.join(fire_dir, "local")
    os.makedirs(out_dir, exist_ok=True)

    grid = hydro.load_grid(D)
    footprint = np.isfinite(grid["dem"])
    slope = hydro.slope_radians(grid["dem"], grid["cellsize"])
    aspect = _terrain_aspect_radians(grid["dem"], grid["cellsize"])
    fuels_mod = fuels_t1._load_us_fuels()
    fbfm_raw, canopy, fuel_provenance = fuels_t1.load_fuelscape(
        D, grid, fuel_source=args.fuel_source, return_provenance=True)
    fbfm_codes = np.where(np.isfinite(fbfm_raw), fbfm_raw, 91).astype(int)

    dead_1h, dead_10h, dead_100h = twin_fire.dead_moisture(
        scenario["temp_f"], scenario["rh_min"], scenario["days_since_rain"],
        scenario["exposure"])
    live_herb_pct, live_woody_pct = twin_fire.live_moisture(
        scenario["phenology"], phenology="auto", drought=scenario["drought"])
    live_herb_pct = float(np.asarray(live_herb_pct))
    live_woody_pct = float(np.asarray(live_woody_pct))

    geo = twin_georef.load(twin_georef.GEOREF_PATH)
    lat = float(geo["origin_wgs84"]["lat"])
    lon_west = abs(float(geo["origin_wgs84"]["lon"]))
    elev_m = float(np.nanmean(grid["dem"]))
    selected_fmc = twin_fire.select_fmc_method("conifer", "temperate_na")
    if args.fmc_override is not None:
        fmc_pct = _clamp(args.fmc_override, 75.0, 140.0)
        fmc_method = "override"
    else:
        fmc_value, fmc_method = twin_fire.derive_fmc(
            selected_fmc, "conifer", "temperate_na", scenario["doy"],
            drought=scenario["drought"], lat=lat, lon_west=lon_west,
            elev_m=elev_m)
        fmc_pct = float(np.asarray(fmc_value))

    moisture = {
        "dead_1h": float(np.asarray(dead_1h)),
        "dead_10h": float(np.asarray(dead_10h)),
        "dead_100h": float(np.asarray(dead_100h)),
        "live_herb": live_herb_pct / 100.0,
        "live_woody": live_woody_pct / 100.0,
    }
    hydro_enabled = args.hydrology == "on"
    hydro_barrier_mask = np.zeros(footprint.shape, dtype=bool)
    moisture_for_ros = moisture
    hydro_provenance = {}
    if hydro_enabled:
        try:
            hydro_barrier_mask, moisture_for_ros, hydro_provenance = (
                hydro_fire.hydro_fire_influence(
                    D, grid, scenario["drought"], moisture))
            hydro_barrier_mask = np.asarray(hydro_barrier_mask, dtype=bool) & footprint
        except Exception as e:  # noqa: BLE001
            hydro_barrier_mask = np.zeros(footprint.shape, dtype=bool)
            moisture_for_ros = moisture
            hydro_provenance = {
                "sources_used": [],
                "sources_found": [],
                "barrier_cells": 0,
                "wet_cells": 0,
                "drought": scenario["drought"],
                "drought_scaling": None,
                "screening_grade": True,
                "notes": [
                    "hydrology influence failed to load and was skipped: %s" % str(e),
                    "scenario moisture is unchanged for this run",
                ],
            }
    derived = {
        "dead_1h_pct": round(moisture["dead_1h"] * 100.0, 2),
        "dead_10h_pct": round(moisture["dead_10h"] * 100.0, 2),
        "dead_100h_pct": round(moisture["dead_100h"] * 100.0, 2),
        "live_herb_pct": round(live_herb_pct, 2),
        "live_woody_pct": round(live_woody_pct, 2),
        "fmc_pct": round(float(fmc_pct), 2),
    }

    applicable_crown = _crown_applicable_mask(footprint, canopy, fbfm_codes)
    canopy["fmc_pct"] = np.where(applicable_crown, fmc_pct, np.nan)
    fuelbed = twin_fire.fuel_bed(
        fbfm_codes, fuels_mod.FBFM40, moisture_for_ros["live_herb"])
    burnable = footprint & fuelbed["burnable"]
    try:
        ignition_mask, ignition = _validate_ignition(args, grid, footprint, burnable)
    except Exception as e:  # noqa: BLE001
        return _result_error(args, str(e), scenario=scenario)

    wind_m_min = scenario["wind_mph"] * twin_fire.MPH_TO_M_MIN
    midflame = twin_fire.midflame_wind(wind_m_min, canopy["cc_pct"])
    phi_w, phi_s, eff_wind, max_dir = twin_fire.wind_slope_factors(
        fuelbed, midflame, slope, aspect, math.radians(scenario["wind_dir"]))
    ros, reaction = twin_fire.rothermel_ros(fuelbed, moisture_for_ros, phi_w, phi_s)
    if hydro_enabled and hydro_barrier_mask.any():
        ros = np.where(hydro_barrier_mask, 0.0, ros)
        reaction = np.where(hydro_barrier_mask, 0.0, reaction)
    eff_wind_for_ellipse = _capped_effective_wind(eff_wind, reaction)
    consumed = twin_fire._effective_consumed_for_fireline(fuelbed, reaction)
    surface_intensity = twin_fire.byram_intensity(ros, consumed, fuelbed["heat"])
    crown_ros = twin_fire.active_crown_ros(
        scenario["wind_mph"], slope, moisture_for_ros)
    if hydro_enabled and hydro_barrier_mask.any():
        crown_ros = np.where(hydro_barrier_mask, 0.0, crown_ros)
    crown = twin_fire.crown_class(
        surface_intensity, crown_ros, canopy["cbh_m"], canopy["cbd_kg_m3"], fmc_pct)
    spread_ros = np.where(crown >= 2, np.maximum(ros, crown_ros), ros)
    intensity = twin_fire.byram_intensity(spread_ros, consumed, fuelbed["heat"])
    flame = twin_fire.flame_length(intensity, crown_mask=(crown >= 2))

    arrival = twin_fire.arrival_time(
        spread_ros, eff_wind_for_ellipse, max_dir, ignition_mask, grid["cellsize"])
    burned = footprint & np.isfinite(arrival) & (arrival <= scenario["duration_min"])
    cell_area_m2 = grid["cellsize"] * grid["cellsize"]
    half = grid["cellsize"] / 2.0
    bounds = [round(grid["minX"] - half, 2), round(grid["minY"] - half, 2),
              round(grid["maxX"] + half, 2), round(grid["maxY"] + half, 2)]
    ember_exposed, spotting_summary = _ember_exposure(
        grid, footprint, burned, flame, intensity, crown, scenario)

    new_layers = []

    def export(layer_id, label, rgba, values, legend, description, decimals=2,
               metadata=None, categorical=False):
        png = os.path.join(out_dir, layer_id + ".png")
        gj = os.path.join(out_dir, layer_id + ".grid.json")
        t1.write_png(rgba, png)
        payload = (_categorical_grid_json(values, bounds, legend, metadata=metadata)
                   if categorical else
                   t1.grid_json(values, bounds, legend, decimals=decimals,
                                metadata=metadata))
        with open(gj, "w") as fh:
            json.dump(payload, fh)
        layer = {
            "id": layer_id,
            "label": label,
            "type": "raster",
            "image": "fire/local/%s.png" % layer_id,
            "grid": "fire/local/%s.grid.json" % layer_id,
            "bounds_local": bounds,
            "acquisition": "derived",
            "group": "fire_scenario",
            "description": description,
        }
        if metadata:
            layer.update(metadata)
        new_layers.append(layer)

    arrival_values = np.where(burned, arrival, np.nan)
    export(
        "fire_arrival", "Fire scenario: arrival time",
        _arrival_rgba(arrival_values, burned, scenario["duration_min"]),
        arrival_values,
        {"min": {"name": "ignition", "color": list(ARRIVAL_RAMP[0][:3])},
         "max": {"name": "%.0f min" % scenario["duration_min"],
                 "color": list(ARRIVAL_RAMP[-1][:3])}},
        "Minimum travel-time fire arrival for the ignition and weather scenario. "
        "Cells outside the duration window are transparent.",
        decimals=1,
        metadata=_metadata("fire_arrival_time", "min", cell_area_m2, scenario))

    flame_values = np.where(burned, flame, np.nan)
    flame_max = float(np.nanmax(flame_values)) if np.isfinite(flame_values).any() else 0.0
    export(
        "flame_length", "Fire scenario: flame length",
        _percentile_rgba(flame_values, burned & (flame_values > 0.0), FLAME_RAMP),
        flame_values,
        {"min": {"name": "low flame", "color": list(FLAME_RAMP[1][:3])},
         "max": {"name": "%.1f m" % flame_max, "color": list(FLAME_RAMP[-1][:3])}},
        "Flame length for cells reached within the scenario duration: Byram for "
        "surface/passive cells and Thomas-style crown flame length for active "
        "crown cells.",
        decimals=2,
        metadata=_metadata("flame_length", "m", cell_area_m2, scenario))

    intensity_values = np.where(burned, intensity, np.nan)
    intensity_max = (float(np.nanmax(intensity_values))
                     if np.isfinite(intensity_values).any() else 0.0)
    export(
        "fireline_intensity", "Fire scenario: fireline intensity",
        _percentile_rgba(intensity_values, burned & (intensity_values > 0.0),
                         INTENSITY_RAMP),
        intensity_values,
        {"min": {"name": "low intensity", "color": list(INTENSITY_RAMP[1][:3])},
         "max": {"name": "%.0f kW/m" % intensity_max,
                 "color": list(INTENSITY_RAMP[-1][:3])}},
        "Byram fireline intensity for cells reached within the scenario duration.",
        decimals=1,
        metadata=_metadata("fireline_intensity", "kW/m", cell_area_m2, scenario))

    crown_values = np.where(burned, crown.astype(float), np.nan)
    export(
        "crown_class", "Fire scenario: crown class",
        _categorical_rgba(crown_values, footprint, CROWN_COLORS),
        crown_values, CROWN_LEGEND,
        "Scott-Reinhardt crown-fire class for reached cells; cells without a "
        "valid conifer-compatible canopy are forced to surface class by the "
        "crown validity guard.",
        metadata=_metadata("crown_fire_class", "class", cell_area_m2, scenario),
        categorical=True)

    ember_values = np.where(ember_exposed, 1.0, np.nan)
    export(
        "ember_exposure", "Fire scenario: ember exposure",
        _categorical_rgba(ember_values, footprint, {
            1: (221, 82, 55, 210),
        }),
        ember_values,
        {"1": {"name": "downwind ember exposure", "color": list(EMBER_RAMP[2][:3])}},
        "Screening downwind ember-exposure band from high-intensity, torching, "
        "or active-crown source cells. It flags cells that may be vulnerable "
        "behind water, wetlands, roads, or cleared gaps; it is not a stochastic "
        "spot-fire ignition solver.",
        metadata=_metadata("ember_exposure", "class", cell_area_m2, scenario),
        categorical=True)

    cat_path = os.path.join(fire_dir, "fire-layers.json")
    catalog = {"generated_by": "fire_scenario.py", "layers": []}
    if os.path.exists(cat_path):
        try:
            catalog = json.load(open(cat_path))
        except Exception:  # noqa: BLE001
            pass
    catalog["layers"] = [l for l in catalog.get("layers", [])
                         if l.get("group") != "fire_scenario"] + new_layers
    catalog["scenario_generated_by"] = "fire_scenario.py"
    catalog["note"] = catalog.get(
        "note",
        "Derived Tier-1 wildfire fuelscape layers plus latest fire scenario.")
    with open(cat_path, "w") as fh:
        json.dump(catalog, fh, indent=2)

    row = int(ignition["row"])
    col = int(ignition["col"])
    lon, lat_ign = _projected_lonlat(float(ignition["x"]), float(ignition["y"]))
    burned_cells = int(burned.sum())
    result_notes = [
        "Unsuppressed potential spread screen, not a forecast; wind, fuels, and "
        "moisture dominate the magnitude uncertainty.",
        "Wind is spatially uniform and held constant for the whole scenario.",
        ("LANDFIRE 30 m fuels and canopy are nearest-resampled to the LiDAR "
         "DEM grid, so fine-scale fuel boundaries are approximate."
         if args.fuel_source == "landfire" else
         "Computed local fuels keep LANDFIRE nonforest classes and reclassify "
         "forested timber cells from LiDAR/NAIP vegetation plus LANDFIRE "
         "EVT/EVC; screening-grade, not a calibrated fuel inventory."),
        "wind_dir is interpreted as the downwind / maximum-spread azimuth "
        "in degrees clockwise from north, not the meteorological wind-from bearing.",
        "Crown class is only applicable where CBH/CBD and the fuel model indicate "
        "a valid conifer-compatible canopy; other cells are forced to surface class.",
        "Active crown cells use Scott-Reinhardt/Rothermel active crown ROS "
        "for spread; passive crown cells remain a torching overlay.",
        "Hydrology influence is screening-grade: open water, snow, surface "
        "water-table, and normal-moisture ponding cells are barriers; wetlands, "
        "wet ground, dry-weather ponding, and no-width streams damp fuels.",
        "Ember spotting is shown as a screening downwind exposure band, not "
        "propagated as stochastic spot-fire ignitions in arrival time; embers "
        "can cross water, wetlands, roads, and cleared gaps.",
    ]
    if scenario["duration_min"] > 360.0:
        result_notes.append(
            "Long-run caution: this holds one peak weather state for more than "
            "six hours, so it can overstate all-day spread without diurnal wind "
            "and humidity changes.")

    result = {
        "ok": True,
        "scenario": scenario,
        "ignition": {
            **ignition,
            "lat": round(lat_ign, 7),
            "lon": round(lon, 7),
        },
        "derived_moistures": derived,
        "fmc_method": fmc_method,
        "fmc_method_selected": selected_fmc,
        "fuel_source": args.fuel_source,
        "fuel_data_tier": ("landfire" if args.fuel_source == "landfire"
                           else "local_crosswalk_screening"),
        "fuel_model_shift": _fuel_shift_summary(fuel_provenance),
        "fuel_model_provenance_note": fuel_provenance.get("note"),
        "hydrology": _hydrology_summary(hydro_enabled, hydro_provenance),
        "spotting": spotting_summary,
        "crown_model_validity": {
            "model": "Van Wagner / Scott-Reinhardt, conifer-compatible canopy only",
            "valid_cells": int(applicable_crown.sum()),
            "valid_fraction_of_footprint": round(
                int(applicable_crown.sum()) / max(1, int(footprint.sum())), 4),
            "nonforested_hardwood_or_missing_canopy_forced_surface_cells": int(
                (footprint & ~applicable_crown).sum()),
        },
        "ros_at_ignition": _ros_ellipse_at(
            row, col, spread_ros, eff_wind_for_ellipse, max_dir),
        "max_flame_length_m": round(flame_max, 3),
        "max_fireline_intensity_kw_m": round(intensity_max, 1),
        "crown_fractions_burned_area": _crown_fractions(crown, burned),
        "burned_area": {
            "cells": burned_cells,
            "ha": round(burned_cells * cell_area_m2 / 10000.0, 3),
            "duration_min": scenario["duration_min"],
        },
        "terrain_reference": {
            "lat": round(lat, 7),
            "lon_west": round(lon_west, 7),
            "mean_elevation_m": round(elev_m, 1),
        },
        "layers": [l["id"] for l in new_layers],
        "notes": result_notes,
    }

    try:
        store = Store(STORE_PATH)
        run = store.begin_run("fire_scenario.py", inputs=result["scenario"],
                              notes="fire scenario: " + scenario["weather_label"])
        for l in new_layers:
            png_path = os.path.join(D, l["image"])
            sha = hashlib.sha1(open(png_path, "rb").read()).hexdigest()
            store.upsert_layer("fire_" + l["id"], label=l["label"], kind="raster",
                               acquisition="derived", source_path=l["image"],
                               status="ok", content_sha1=sha)
        store.finish_run(run, notes=json.dumps({
            "burned_area_ha": result["burned_area"]["ha"],
            "head_ros_m_min": result["ros_at_ignition"]["head_m_min"],
            "max_flame_length_m": result["max_flame_length_m"],
        }))
        store.close()
        result["run_id"] = run
    except Exception as e:  # noqa: BLE001
        result["store_warning"] = str(e)

    with open(os.path.join(fire_dir, "last-fire-scenario.json"), "w") as fh:
        json.dump(result, fh, indent=2)

    if args.json:
        print(json.dumps(result))
    else:
        print("Fire scenario: %s" % scenario["weather_label"])
        print("  ignition     %.1f, %.1f" %
              (float(ignition["x"]), float(ignition["y"])))
        print("  ROS          head %.2f m/min, flank %.2f, back %.2f" %
              (result["ros_at_ignition"]["head_m_min"],
               result["ros_at_ignition"]["flank_m_min"],
               result["ros_at_ignition"]["back_m_min"]))
        print("  burned       %.2f ha in %.0f min, max flame %.1f m" %
              (result["burned_area"]["ha"], scenario["duration_min"],
               result["max_flame_length_m"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
