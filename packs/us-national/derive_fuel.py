"""Local vegetation to FBFM40 crosswalk for US LANDFIRE twins.

This module keeps LANDFIRE's 30 m FBFM40 grid as the base and replaces only
forested timber cells with a screening-grade fuel model inferred from local
tree/shrub observations plus LANDFIRE EVT/EVC/EVH context.
"""

from collections import Counter
import json
import math
import os
import re

import numpy as np


THRESHOLDS = {
    "tree_count_forest_min": 3,
    "evergreen_hardwood_max": 0.35,
    "evergreen_mixed_max": 0.60,
    "closed_cover_min_pct": 40.0,
    "small_tree_height_m": 9.0,
    "small_tree_fraction_understory_min": 0.15,
    "shrub_count_understory_min": 5,
    "pine_dominant_fraction_min": 0.50,
    "low_load_cover_max_pct": 50.0,
}

NOTE = (
    "derived from LiDAR/NAIP canopy + LANDFIRE EVT/EVC via a Scott & Burgan "
    "crosswalk; screening-grade; nonforest fuels kept from LANDFIRE"
)

CONIFER_TERMS = ("pine", "hemlock", "spruce", "fir", "conifer", "evergreen")
OTHER_CONIFER_TERMS = ("hemlock", "spruce", "fir")
HARDWOOD_TERMS = (
    "hardwood", "deciduous", "oak", "maple", "beech", "birch", "aspen"
)


def _grid_path(data_dir, name):
    return os.path.join(data_dir, "atlas", "local", name + ".grid.json")


def _load_grid(data_dir, name):
    with open(_grid_path(data_dir, name)) as fh:
        meta = json.load(fh)
    nodata = meta.get("nodata", -9999)
    rows = []
    for row in meta["values"]:
        out = []
        for v in row:
            if v is None or v == nodata or v == -9999:
                out.append(np.nan)
            else:
                out.append(float(v))
        rows.append(out)
    return np.asarray(rows, dtype=float), meta


def _sample_to_grid(src, src_meta, target_meta):
    """Nearest source cell sampled at target 30 m cell centers."""
    sminx, sminy, smaxx, smaxy = src_meta["bounds_local"]
    tminx, tminy, tmaxx, tmaxy = target_meta["bounds_local"]
    th = int(target_meta["height"])
    tw = int(target_meta["width"])
    sh, sw = src.shape

    xs = tminx + (np.arange(tw) + 0.5) * (tmaxx - tminx) / tw
    ys = tmaxy - (np.arange(th) + 0.5) * (tmaxy - tminy) / th
    col_f = ((xs - sminx) / (smaxx - sminx)) * sw
    row_f = ((smaxy - ys) / (smaxy - sminy)) * sh
    col_ok = (xs >= sminx) & (xs <= smaxx)
    row_ok = (ys >= sminy) & (ys <= smaxy)
    cols = np.clip(np.floor(col_f).astype(int), 0, sw - 1)
    rows = np.clip(np.floor(row_f).astype(int), 0, sh - 1)
    out = src[np.ix_(rows, cols)].astype(float)
    out[~row_ok, :] = np.nan
    out[:, ~col_ok] = np.nan
    return out


def _point_to_cell(meta, x, y):
    minx, miny, maxx, maxy = meta["bounds_local"]
    if not (minx <= x <= maxx and miny <= y <= maxy):
        return None
    width = int(meta["width"])
    height = int(meta["height"])
    col = int(math.floor(((x - minx) / (maxx - minx)) * width))
    row = int(math.floor(((maxy - y) / (maxy - miny)) * height))
    col = min(width - 1, max(0, col))
    row = min(height - 1, max(0, row))
    return row, col


def _safe_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return np.nan
    return out if math.isfinite(out) else np.nan


def _load_json_list(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def _rasterize_local_vegetation(data_dir, meta):
    shape = (int(meta["height"]), int(meta["width"]))
    tree_count = np.zeros(shape, dtype=int)
    evergreen_count = np.zeros(shape, dtype=int)
    pine_count = np.zeros(shape, dtype=int)
    other_conifer_count = np.zeros(shape, dtype=int)
    small_tree_count = np.zeros(shape, dtype=int)
    height_sum = np.zeros(shape, dtype=float)
    height_count = np.zeros(shape, dtype=int)
    shrub_count = np.zeros(shape, dtype=int)

    trees = _load_json_list(os.path.join(data_dir, "vegetation", "tree_instances.json"))
    for tree in trees:
        cell = _point_to_cell(meta, _safe_float(tree.get("x")), _safe_float(tree.get("y")))
        if cell is None:
            continue
        row, col = cell
        tree_count[row, col] += 1
        species = str(tree.get("species") or "").lower()
        tree_type = str(tree.get("type") or "").lower()
        is_pine = "pine" in species
        is_other_conifer = any(term in species for term in OTHER_CONIFER_TERMS)
        is_evergreen = (
            tree_type == "evergreen"
            or is_pine
            or is_other_conifer
            or any(term in species for term in ("cedar", "conifer", "evergreen"))
        )
        if is_evergreen:
            evergreen_count[row, col] += 1
        if is_pine:
            pine_count[row, col] += 1
        if is_other_conifer:
            other_conifer_count[row, col] += 1
        height = _safe_float(tree.get("height"))
        if np.isfinite(height):
            height_sum[row, col] += height
            height_count[row, col] += 1
            if height < THRESHOLDS["small_tree_height_m"]:
                small_tree_count[row, col] += 1

    shrubs = _load_json_list(os.path.join(data_dir, "vegetation", "shrub_points.json"))
    for shrub in shrubs:
        cell = _point_to_cell(meta, _safe_float(shrub.get("x")), _safe_float(shrub.get("y")))
        if cell is not None:
            shrub_count[cell] += 1

    with np.errstate(invalid="ignore", divide="ignore"):
        evergreen_fraction = np.divide(
            evergreen_count, tree_count,
            out=np.full(shape, np.nan, dtype=float), where=tree_count > 0)
        pine_fraction = np.divide(
            pine_count, evergreen_count,
            out=np.zeros(shape, dtype=float), where=evergreen_count > 0)
        other_conifer_fraction = np.divide(
            other_conifer_count, evergreen_count,
            out=np.zeros(shape, dtype=float), where=evergreen_count > 0)
        small_tree_fraction = np.divide(
            small_tree_count, tree_count,
            out=np.zeros(shape, dtype=float), where=tree_count > 0)
        mean_height = np.divide(
            height_sum, height_count,
            out=np.full(shape, np.nan, dtype=float), where=height_count > 0)

    return {
        "tree_count": tree_count,
        "evergreen_fraction": evergreen_fraction,
        "pine_fraction": pine_fraction,
        "other_conifer_fraction": other_conifer_fraction,
        "small_tree_fraction": small_tree_fraction,
        "shrub_count": shrub_count,
        "mean_height": mean_height,
    }


def _legend_name(meta, code):
    if not np.isfinite(code):
        return ""
    return (meta.get("legend", {}).get(str(int(code))) or {}).get("name", "")


def _life_form_from_name(name):
    text = str(name or "").lower()
    if any(term in text for term in ("forest", "woodland", "treed")):
        return "tree"
    if any(term in text for term in CONIFER_TERMS + HARDWOOD_TERMS):
        return "tree"
    if "shrub" in text:
        return "shrub"
    if any(term in text for term in ("meadow", "herb", "grass", "pasture")):
        return "herb"
    return "sparse"


def _evt_defaults(name):
    text = str(name or "").lower()
    has_conifer = any(term in text for term in CONIFER_TERMS)
    has_hardwood = any(term in text for term in HARDWOOD_TERMS)
    pine = "pine" in text
    other_conifer = any(term in text for term in OTHER_CONIFER_TERMS)
    if has_conifer and has_hardwood:
        evergreen = 0.50
    elif has_conifer:
        evergreen = 1.0
    elif has_hardwood:
        evergreen = 0.0
    else:
        evergreen = np.nan
    if pine and other_conifer:
        pine_fraction = 0.50
    elif pine:
        pine_fraction = 1.0
    else:
        pine_fraction = 0.0
    return evergreen, pine_fraction, 1.0 if other_conifer else 0.0


def _names_for_grid(codes, meta):
    names = np.empty(codes.shape, dtype=object)
    life = np.empty(codes.shape, dtype=object)
    evt_evergreen = np.full(codes.shape, np.nan, dtype=float)
    evt_pine = np.zeros(codes.shape, dtype=float)
    evt_other_conifer = np.zeros(codes.shape, dtype=float)
    for idx in np.ndindex(codes.shape):
        name = _legend_name(meta, codes[idx])
        names[idx] = name
        life[idx] = _life_form_from_name(name)
        evg, pine, other = _evt_defaults(name)
        evt_evergreen[idx] = evg
        evt_pine[idx] = pine
        evt_other_conifer[idx] = other
    return names, life, evt_evergreen, evt_pine, evt_other_conifer


def _parse_first_number(text):
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(text or ""))
    return float(match.group(1)) if match else np.nan


def _cover_from_evc(evc, evc_meta):
    out = np.full(evc.shape, np.nan, dtype=float)
    for idx in np.ndindex(evc.shape):
        name = _legend_name(evc_meta, evc[idx])
        if "cover" in name.lower():
            out[idx] = _parse_first_number(name)
    return out


def _height_from_evh(evh, evh_meta):
    out = np.full(evh.shape, np.nan, dtype=float)
    for idx in np.ndindex(evh.shape):
        name = _legend_name(evh_meta, evh[idx])
        if "tree height" in name.lower():
            out[idx] = _parse_first_number(name)
    return out


def _short_name(code):
    c = int(code)
    if 91 <= c <= 99:
        return "NB%d" % (c - 90)
    if 101 <= c <= 109:
        return "GR%d" % (c - 100)
    if 121 <= c <= 124:
        return "GS%d" % (c - 120)
    if 141 <= c <= 149:
        return "SH%d" % (c - 140)
    if 161 <= c <= 165:
        return "TU%d" % (c - 160)
    if 181 <= c <= 189:
        return "TL%d" % (c - 180)
    if 201 <= c <= 204:
        return "SB%d" % (c - 200)
    return str(c)


def _counts(arr):
    valid = np.asarray(arr)
    valid = valid[np.isfinite(valid)]
    counter = Counter(int(v) for v in valid)
    return {
        str(code): {"short_name": _short_name(code), "count": int(count)}
        for code, count in sorted(counter.items())
    }


def _transition_summary(before, after):
    valid = np.isfinite(before) & np.isfinite(after)
    changed = valid & (before.astype(int) != after.astype(int))
    counter = Counter()
    for src, dst in zip(before[changed].astype(int).ravel(), after[changed].astype(int).ravel()):
        counter[(int(src), int(dst))] += 1
    transitions = []
    for (src, dst), count in counter.most_common():
        transitions.append({
            "from_code": src,
            "from": _short_name(src),
            "to_code": dst,
            "to": _short_name(dst),
            "transition": "%s->%s" % (_short_name(src), _short_name(dst)),
            "count": int(count),
        })
    total = int(valid.sum())
    return {
        "basis": "30 m LANDFIRE FBFM40 cells",
        "total_cells": total,
        "changed_cells": int(changed.sum()),
        "changed_fraction": round(float(changed.sum()) / max(1, total), 4),
        "top_transitions": transitions[:12],
    }


def _classify_cells(base, cover, stats, evt_life, evt_evergreen, evt_pine,
                    evt_other_conifer):
    derived = np.where(np.isfinite(base), base, 91).astype(int)
    tree_count = stats["tree_count"]
    shrub_count = stats["shrub_count"]
    small_tree_fraction = stats["small_tree_fraction"]
    mean_height = stats["mean_height"]

    evergreen = np.where(
        np.isfinite(stats["evergreen_fraction"]),
        stats["evergreen_fraction"],
        evt_evergreen)
    evergreen = np.where(np.isfinite(evergreen), evergreen, 0.0)
    pine_fraction = np.where(
        stats["tree_count"] > 0,
        stats["pine_fraction"],
        evt_pine)
    other_conifer_fraction = np.where(
        stats["tree_count"] > 0,
        stats["other_conifer_fraction"],
        evt_other_conifer)

    is_nonforest_fbfm = (
        ((base >= 91) & (base <= 99))
        | ((base >= 101) & (base <= 149))
    )
    forested = (
        ((evt_life == "tree") | (tree_count >= THRESHOLDS["tree_count_forest_min"]))
        & np.isfinite(base)
        & ~is_nonforest_fbfm
    )
    understory = (
        (shrub_count >= THRESHOLDS["shrub_count_understory_min"])
        | (small_tree_fraction >= THRESHOLDS["small_tree_fraction_understory_min"])
    )
    low_load = (
        (cover < THRESHOLDS["low_load_cover_max_pct"])
        | (np.isfinite(mean_height) & (mean_height < THRESHOLDS["small_tree_height_m"]))
    )

    hardwood = forested & (evergreen < THRESHOLDS["evergreen_hardwood_max"])
    derived[hardwood & (cover >= THRESHOLDS["closed_cover_min_pct"])] = 186
    derived[hardwood & (cover < THRESHOLDS["closed_cover_min_pct"])] = 182

    mixed = (
        forested
        & (evergreen >= THRESHOLDS["evergreen_hardwood_max"])
        & (evergreen < THRESHOLDS["evergreen_mixed_max"])
    )
    derived[mixed & understory] = 162
    derived[mixed & ~understory] = 185

    conifer = forested & (evergreen >= THRESHOLDS["evergreen_mixed_max"])
    pine_dominant = pine_fraction >= THRESHOLDS["pine_dominant_fraction_min"]
    other_conifer_dominant = other_conifer_fraction >= pine_fraction
    derived[conifer & understory] = 162
    derived[conifer & ~understory & pine_dominant] = 188
    derived[conifer & ~understory & ~pine_dominant & other_conifer_dominant & low_load] = 183
    derived[conifer & ~understory & ~pine_dominant & other_conifer_dominant & ~low_load] = 185
    derived[conifer & ~understory & ~pine_dominant & ~other_conifer_dominant] = 185

    return derived, {
        "forested_cells": int(forested.sum()),
        "understory_cells": int((forested & understory).sum()),
        "protected_nonforest_cells": int((np.isfinite(base) & is_nonforest_fbfm).sum()),
        "life_form_counts": {
            life: int((evt_life == life).sum())
            for life in ("tree", "shrub", "herb", "sparse")
        },
    }


def derive_fbfm40(data_dir, grid=None):
    """Return a derived 30 m FBFM40 grid and screening-grade provenance."""
    base, base_meta = _load_grid(data_dir, "landfire_fbfm40_2024")
    evc, evc_meta = _load_grid(data_dir, "landfire_evc_2024")
    evh, evh_meta = _load_grid(data_dir, "landfire_evh_2024")
    cc, cc_meta = _load_grid(data_dir, "landfire_cc_2024")
    evt, evt_meta = _load_grid(data_dir, "landfire_evt_2024")

    if evc.shape != base.shape or evc_meta.get("bounds_local") != base_meta.get("bounds_local"):
        evc = _sample_to_grid(evc, evc_meta, base_meta)
    if evh.shape != base.shape or evh_meta.get("bounds_local") != base_meta.get("bounds_local"):
        evh = _sample_to_grid(evh, evh_meta, base_meta)
    if cc.shape != base.shape or cc_meta.get("bounds_local") != base_meta.get("bounds_local"):
        cc = _sample_to_grid(cc, cc_meta, base_meta)
    evt_on_fuel = _sample_to_grid(evt, evt_meta, base_meta)

    evt_names, evt_life, evt_evergreen, evt_pine, evt_other_conifer = _names_for_grid(
        evt_on_fuel, evt_meta)
    stats = _rasterize_local_vegetation(data_dir, base_meta)
    cover_evc = _cover_from_evc(evc, evc_meta)
    cover = np.where(np.isfinite(cover_evc), cover_evc, cc)
    cover = np.where(np.isfinite(cover), cover, 0.0)
    evh_height = _height_from_evh(evh, evh_meta)
    stats["mean_height"] = np.where(
        np.isfinite(stats["mean_height"]), stats["mean_height"], evh_height)

    derived, screen = _classify_cells(
        base, cover, stats, evt_life, evt_evergreen, evt_pine, evt_other_conifer)
    derived = np.where(np.isfinite(base), derived, np.nan)
    shift = _transition_summary(base, derived)

    provenance = {
        "method": "local_vegetation_fbfm40_crosswalk_v1",
        "source": "computed",
        "base_fuel_source": "LANDFIRE 2024 FBFM40",
        "geometry": {
            "source": "landfire_fbfm40_2024.grid.json",
            "width": int(base_meta["width"]),
            "height": int(base_meta["height"]),
            "bounds_local": base_meta["bounds_local"],
        },
        "thresholds": dict(THRESHOLDS),
        "model_counts_before": _counts(base),
        "model_counts_after": _counts(derived),
        "fuel_model_shift": shift,
        "screen": screen,
        "note": NOTE,
    }
    return derived, provenance
