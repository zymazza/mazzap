#!/usr/bin/env python3
"""Tier 2 parcel-water scenario engine behind the Simulation window.

Two event modes:

  snowmelt  a snowpack (inches of SWE) melting over N days, with optional
            rain-on-snow, antecedent moisture, and frozen ground
  rain      a rainstorm: total inches over a duration in hours (presets come
            from the 45-year Daymet annual-maximum storm series)

Water is advanced in finite forcing increments over the Tier-1 D8 graph.  At
each cell, local rain/melt, upstream runon, and previously retained pond water
share one Green-Ampt/Mein-Larson infiltration calculation.  Water admitted at
the surface enters one soil state; it is either retained in that profile or
later drains below the modeled root zone.  Remaining surface water fills the
finite LiDAR depression volume before it can spill downstream.  This ordering
is load-bearing: runon can infiltrate and can also fill/saturate convergent low
ground, rather than soil-water drapes merely repeating SSURGO polygons.

SSURGO supplies profile Ksat, AWC, texture, and restriction depth. Published
Rawls/HEC texture estimates fill the wetting-front suction, porosity, field
capacity, wilting point, residual content, and pore-size-index gaps.  There is
no Curve Number loss layered on top: doing so would count infiltration and
initial abstraction twice.

Outputs:
  - JSON result on stdout (with --json): totals, peak-flow estimate at the AOI
    outlet (wide, honest uncertainty band), depression-storage filling, notes.
  - Nine drape layers replacing any previous scenario in
    data/hydrology/simulation-layers.json (group "scenario"):
      scenario_runoff             local input left on the surface (mm)
      scenario_infiltration       total local + runon infiltration (mm)
      scenario_soil_storage       final event water retained in profile (mm)
      scenario_deep_drainage      event percolation below profile (mm)
      scenario_saturation_excess  local input rejected by a full profile (mm)
      scenario_saturation         final physical pore-water saturation (%)
      scenario_runon              routed surface water arriving at cell (m^3)
      scenario_ponded_water       retained depression water depth (mm)
      scenario_flow               surface water leaving cell over event (m^3)
  - A pipeline run in the twin store with the scenario parameters as inputs,
    so scenario history is queryable like any other run.

This is an uncalibrated screening tool, not a forecast. SSURGO hydraulic
parameters can be wrong by orders of magnitude and D8 routing has no travel
time or backwater. Relative geometry and scenario comparisons are more
defensible than absolute infiltration or discharge.

Run:  python3 scripts/hydro_scenario.py --swe-in 10 --melt-days 4 --rain-in 0.5 \
          --antecedent normal [--frozen] [--json] [--data-dir DIR]
      python3 scripts/hydro_scenario.py --mode rain --rain-in 2.8 --storm-hours 12
"""

import argparse
import csv
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
ROOT_ZONE_DEPTH_CM = 70.0
DEFAULT_ROOT_ZONE_TAW_MM = 100.0
ANTECEDENT_FILL_FRACTION = {"dry": 0.2, "normal": 0.5, "wet": 0.9}
TWI_ANTECEDENT_SPREAD = 0.2  # high/low TWI shifts initial AWC fill by +/-10%
# Relative variable-source-area initialization. Cells above the percentile
# threshold begin progressively wetter, reaching physical saturation at the
# highest TWI. This antecedent water is held as a boundary condition while only
# event water is allowed to drain. These are screening assumptions, not a
# calibrated TOPMODEL contributing-area or baseflow calculation.
VSA_TWI_THRESHOLD = {"dry": 0.97, "normal": 0.85, "wet": 0.65}
FROZEN_KSAT_FACTOR = 0.03
FROZEN_PORE_SPACE_FACTOR = 0.1
MAX_ROUTING_STEP_HOURS = 1.0
MIN_ROUTING_STEPS = 6
MAX_SNOWMELT_DAYS = 30.0
MAX_STORM_HOURS = 240.0
SURFACE_SCREENING_RESOLUTION_FRACTION = 0.002
DEPRESSION_DEPTH_EPS_M = 1e-4

# Midpoints of the parameter ranges in HEC-RAS 2D User Manual table 2-5,
# derived from Rawls/Brakensiek texture estimates. Units: Ksat mm/hr, suction
# mm; water contents and pore-size index are dimensionless.
TEXTURE_HYDRAULICS = {
    "sand": (0.020, 0.033, 0.048, 0.437, 0.694, 222.8, 101.1),
    "loamy sand": (0.035, 0.055, 0.084, 0.437, 0.553, 60.45, 130.8),
    "sandy loam": (0.041, 0.095, 0.155, 0.453, 0.378, 23.85, 218.65),
    "loam": (0.027, 0.117, 0.200, 0.463, 0.252, 13.2, 245.0),
    "silt loam": (0.015, 0.133, 0.261, 0.501, 0.234, 6.8, 366.8),
    "sandy clay loam": (0.068, 0.148, 0.187, 0.398, 0.319, 3.65, 493.65),
    "clay loam": (0.075, 0.197, 0.245, 0.464, 0.242, 2.15, 427.45),
    "silty clay loam": (0.040, 0.208, 0.300, 0.471, 0.177, 1.75, 559.65),
    "sandy clay": (0.109, 0.239, 0.232, 0.430, 0.223, 1.2, 551.25),
    "silty clay": (0.056, 0.250, 0.317, 0.479, 0.150, 0.95, 612.35),
    "clay": (0.090, 0.272, 0.296, 0.475, 0.165, 0.6, 668.25),
}

HSG_KSAT_FALLBACK_MM_HR = {
    "A": 10.0, "B": 5.7, "C": 2.5, "D": 0.6,
    "A/D": 0.6, "B/D": 0.6, "C/D": 0.6,
}


def usda_texture_class(sand, silt, clay):
    """Classify complete sand/silt/clay percentages on the USDA triangle."""
    if not all(np.isfinite(v) for v in (sand, silt, clay)):
        return "sandy loam"
    total = sand + silt + clay
    if total <= 0:
        return "sandy loam"
    sand, silt, clay = (100.0 * v / total for v in (sand, silt, clay))
    if clay >= 40 and sand <= 45 and silt <= 40:
        return "clay"
    if clay >= 40 and silt > 40:
        return "silty clay"
    if clay >= 35 and sand > 45:
        return "sandy clay"
    if 27 <= clay < 40 and sand <= 20:
        return "silty clay loam"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "clay loam"
    if 20 <= clay < 35 and sand > 45 and silt < 28:
        return "sandy clay loam"
    if silt >= 50 and clay < 27 and sand <= 50:
        return "silt loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and 23 <= sand <= 52:
        return "loam"
    if clay < 20 and sand >= 43 and (silt + 2 * clay) >= 30:
        return "sandy loam"
    if sand >= 70 and (silt + 2 * clay) < 30:
        return "loamy sand" if sand < 85 else "sand"
    return "sandy loam"


def _array(soils, key, shape):
    return np.array(soils.get(key, np.full(shape, np.nan)), dtype=float, copy=True)


def tied_percentile_rank(values, mask):
    """0..1 empirical percentile with equal values assigned the same midrank."""
    out = np.full(values.shape, np.nan)
    finite = mask & np.isfinite(values)
    sample = np.asarray(values[finite], dtype=float)
    if not sample.size:
        return out
    ordered = np.sort(sample)
    left = np.searchsorted(ordered, sample, side="left")
    right = np.searchsorted(ordered, sample, side="right")
    out[finite] = (left + right - 1.0) / (2.0 * max(sample.size - 1, 1))
    return out


def et_antecedent_state(as_of=None):
    """Map the latest ET/root-zone state to a continuous 0..1 wetness index.

    Returns None when the ET water-balance output is absent, preserving the
    manual dry/normal/wet path.
    """
    path = os.path.join(D, "et", "soil_water_daily.csv")
    if not os.path.exists(path):
        return None
    rows = list(csv.DictReader(open(path)))
    if not rows:
        return None
    if as_of:
        rows = [row for row in rows if row.get("date", "") <= as_of] or rows
    row = rows[-1]

    def num(key, default=0.0):
        try:
            return float(row.get(key) or default)
        except (TypeError, ValueError):
            return default

    depletion = num("root_zone_depletion_fraction", 0.5)
    wet5 = num("wetness_5d")
    wet14 = num("wetness_14d")
    wet30 = num("wetness_30d")
    root_wetness = 1.0 - np.clip(depletion, 0.0, 1.0)
    wetness = float(np.clip(
        0.45 * root_wetness + 0.25 * wet5 + 0.20 * wet14 + 0.10 * wet30,
        0.0, 1.0))
    label = "dry" if wetness < 0.33 else "wet" if wetness > 0.67 else "normal"
    return {
        "source": "et/soil_water_daily.csv",
        "date": row.get("date"),
        "as_of": as_of,
        "mode": "auto",
        "wetness_index": round(wetness, 3),
        "equivalent_manual_class": label,
        "root_zone_depletion_fraction": round(depletion, 3),
        "wetness_5d": round(wet5, 3),
        "wetness_14d": round(wet14, 3),
        "wetness_30d": round(wet30, 3),
    }


def hydraulic_state(soils, fields, antecedent, frozen=False,
                    antecedent_wetness=None):
    """Build the one per-cell soil state used by every surface-water flux."""
    footprint = np.isfinite(fields["dem"])
    shape = footprint.shape
    params = {name: np.full(shape, np.nan) for name in
              ("residual", "wilting", "field_capacity", "porosity",
               "pore_index", "texture_ksat", "suction_mm")}
    texture = np.full(shape, "sandy loam", dtype=object)
    sand = _array(soils, "sand_pct", shape)
    silt = _array(soils, "silt_pct", shape)
    clay = _array(soils, "clay_pct", shape)
    for r, c in np.argwhere(footprint):
        cls = usda_texture_class(sand[r, c], silt[r, c], clay[r, c])
        texture[r, c] = cls
        values = TEXTURE_HYDRAULICS[cls]
        for key, value in zip(params, values):
            params[key][r, c] = value

    restriction = _array(soils, "restrictive_cm", shape)
    depth_mm = np.full(shape, ROOT_ZONE_DEPTH_CM * 10.0)
    use_restriction = np.isfinite(restriction)
    depth_mm[use_restriction] = np.minimum(
        depth_mm[use_restriction], np.maximum(50.0, restriction[use_restriction] * 10.0))

    taw = _array(soils, "root_zone_taw_mm", shape)
    taw[~np.isfinite(taw)] = DEFAULT_ROOT_ZONE_TAW_MM
    awc_fraction = np.clip(taw / np.maximum(depth_mm, 1.0), 0.02, 0.30)
    field_capacity = np.minimum(
        params["porosity"] - 0.01, params["wilting"] + awc_fraction)
    awc_fraction = np.maximum(0.01, field_capacity - params["wilting"])

    if antecedent_wetness is None:
        base_fill = ANTECEDENT_FILL_FRACTION.get(antecedent, 0.5)
        vsa_threshold = VSA_TWI_THRESHOLD.get(
            antecedent, VSA_TWI_THRESHOLD["normal"])
    else:
        wetness = float(np.clip(antecedent_wetness, 0.0, 1.0))
        base_fill = float(np.interp(
            wetness, [0.0, 0.5, 1.0],
            [ANTECEDENT_FILL_FRACTION["dry"],
             ANTECEDENT_FILL_FRACTION["normal"],
             ANTECEDENT_FILL_FRACTION["wet"]]))
        vsa_threshold = float(np.interp(
            wetness, [0.0, 0.5, 1.0],
            [VSA_TWI_THRESHOLD["dry"], VSA_TWI_THRESHOLD["normal"],
             VSA_TWI_THRESHOLD["wet"]]))
    twi_rank = tied_percentile_rank(fields["twi"], footprint)
    initial_fill = np.clip(
        base_fill + TWI_ANTECEDENT_SPREAD * (np.nan_to_num(twi_rank, nan=0.5) - 0.5),
        0.02, 0.98)
    theta_base = params["wilting"] + initial_fill * awc_fraction

    vsa_fraction = np.clip(
        (np.nan_to_num(twi_rank, nan=0.0) - vsa_threshold) /
        max(1.0 - vsa_threshold, 1e-6), 0.0, 1.0)
    physical_porosity = params["porosity"].copy()
    theta_initial = theta_base + vsa_fraction * (physical_porosity - theta_base)

    profile_ksat = _array(soils, "ksat_min", shape)
    surface_ksat = _array(soils, "surface_ksat", shape)
    missing = ~np.isfinite(profile_ksat)
    profile_ksat[missing] = surface_ksat[missing]
    hsg = soils.get("hsg")
    if hsg is not None:
        for group, fallback in HSG_KSAT_FALLBACK_MM_HR.items():
            use = ~np.isfinite(profile_ksat) & (hsg == group)
            profile_ksat[use] = fallback
    profile_ksat[~np.isfinite(profile_ksat)] = HSG_KSAT_FALLBACK_MM_HR["B"]
    # Matrix Ksat is uncertain and often much larger than effective field-scale
    # intake. The texture estimate is used as a conservative upper bound.
    ksat = np.minimum(np.maximum(profile_ksat, 0.0), params["texture_ksat"])

    # Bedrock or a mapped seasonal water table inside the modeled profile is a
    # no-leak lower boundary. A known deeper restriction gets only the finite
    # drainable volume between the profile and that boundary. This avoids both
    # physically inverted drainage through a restriction and a hard free-flow
    # discontinuity immediately below the nominal profile.
    bedrock = _array(soils, "bedrock_cm", shape)
    water_table = _array(soils, "water_table_cm", shape)
    typed_restriction = np.isfinite(bedrock) | np.isfinite(water_table)
    lower_restricted = (
        (np.isfinite(bedrock) & (bedrock <= ROOT_ZONE_DEPTH_CM))
        | (np.isfinite(water_table) & (water_table <= ROOT_ZONE_DEPTH_CM))
        | (~typed_restriction & np.isfinite(restriction)
           & (restriction <= ROOT_ZONE_DEPTH_CM))
    )

    intake_limit = physical_porosity.copy()
    if frozen:
        ksat *= FROZEN_KSAT_FACTOR
        intake_limit = theta_initial + FROZEN_PORE_SPACE_FACTOR * (
            physical_porosity - theta_initial)
    lower_boundary_ksat = np.where(lower_restricted, 0.0, ksat)
    known_restriction = np.where(
        typed_restriction,
        np.fmin(
            np.where(np.isfinite(bedrock), bedrock, np.inf),
            np.where(np.isfinite(water_table), water_table, np.inf)),
        restriction)
    finite_subprofile = np.isfinite(known_restriction) & ~lower_restricted
    lower_boundary_capacity = np.full(shape, np.inf)
    lower_boundary_capacity[lower_restricted] = 0.0
    lower_boundary_capacity[finite_subprofile] = (
        np.maximum(known_restriction[finite_subprofile] - ROOT_ZONE_DEPTH_CM, 0.0)
        * 10.0
        * np.maximum(
            physical_porosity[finite_subprofile] - field_capacity[finite_subprofile],
            0.0))

    for values in (*params.values(), depth_mm, field_capacity, theta_initial, ksat,
                   initial_fill, vsa_fraction, physical_porosity, intake_limit,
                   lower_boundary_ksat, lower_boundary_capacity):
        values[~footprint] = np.nan
    return {
        "theta": theta_initial.copy(), "theta_initial": theta_initial,
        "theta_s": physical_porosity, "theta_intake_limit": intake_limit,
        "theta_r": params["residual"],
        "theta_fc": field_capacity, "pore_index": params["pore_index"],
        "ksat_mm_hr": ksat, "suction_mm": params["suction_mm"],
        "depth_mm": depth_mm, "cumulative_infiltration_mm": np.zeros(shape),
        "event_storage_mm": np.zeros(shape),
        "deep_drainage_mm": np.zeros(shape), "texture": texture,
        "lower_boundary_ksat_mm_hr": lower_boundary_ksat,
        "lower_boundary_capacity_mm": lower_boundary_capacity,
        "lower_boundary_remaining_mm": lower_boundary_capacity.copy(),
        "lower_boundary_restricted": lower_restricted,
        "lower_boundary_finite": finite_subprofile,
        "initial_fill_fraction": initial_fill, "vsa_fraction": vsa_fraction,
        "vsa_twi_threshold": vsa_threshold,
        "antecedent_fill_fraction": base_fill,
    }


def green_ampt_capacity_increment(cumulative_mm, ksat_mm_hr, suction_mm,
                                  moisture_deficit, duration_hours):
    """Ponded Green-Ampt capacity increment, solved by vector bisection.

    Integrating dF/dt = Ks(1 + psi*dtheta/F) from F0 to F1 gives
    Ks*dt = dF - A*ln(1 + dF/(F0+A)), A=psi*dtheta.  Actual infiltration is
    capped later by available surface water and remaining pore volume.
    """
    f0 = np.maximum(np.asarray(cumulative_mm, dtype=float), 0.0)
    ks = np.maximum(np.asarray(ksat_mm_hr, dtype=float), 0.0)
    a = np.maximum(np.asarray(suction_mm, dtype=float) *
                   np.maximum(np.asarray(moisture_deficit, dtype=float), 0.0), 0.0)
    target = ks * max(float(duration_hours), 0.0)
    lo = target.copy()
    hi = target + a + 2.0 * np.sqrt(np.maximum(a * target, 0.0)) + 1e-12
    denom = np.maximum(f0 + a, 1e-12)
    for _ in range(32):
        mid = (lo + hi) * 0.5
        residual = mid - a * np.log1p(mid / denom) - target
        hi = np.where(residual >= 0.0, mid, hi)
        lo = np.where(residual < 0.0, mid, lo)
    out = (lo + hi) * 0.5
    out[(ks <= 0.0) | ~np.isfinite(out)] = 0.0
    return out


def drain_profile(state, duration_hours, footprint):
    """Analytic Brooks-Corey drainage of event water above field capacity.

    The separable ODE for effective saturation is integrated over the requested
    duration instead of taking an explicit K(theta)*dt step. Antecedent VSA
    water is a fixed initial boundary condition: only traced event storage may
    percolate, so it cannot create unaccounted baseflow in the event budget.
    """
    if duration_hours <= 0.0:
        return np.zeros_like(state["theta"])
    theta = state["theta"]
    excess_mm = np.maximum(theta - state["theta_fc"], 0.0) * state["depth_mm"]
    denom = np.maximum(state["theta_s"] - state["theta_r"], 1e-6)
    effective_saturation = np.clip((theta - state["theta_r"]) / denom, 0.0, 1.0)
    exponent = 3.0 + 2.0 / np.maximum(state["pore_index"], 0.05)
    lower_ksat = np.minimum(
        state["ksat_mm_hr"], state["lower_boundary_ksat_mm_hr"])
    coefficient = lower_ksat / np.maximum(state["depth_mm"] * denom, 1e-9)
    final_effective_saturation = effective_saturation.copy()
    active = footprint & (lower_ksat > 0.0) & (effective_saturation > 0.0)
    n = exponent[active]
    se0 = effective_saturation[active]
    base = se0 ** (1.0 - n) + (n - 1.0) * coefficient[active] * duration_hours
    final_effective_saturation[active] = base ** (-1.0 / (n - 1.0))
    potential = np.maximum(
        0.0, (effective_saturation - final_effective_saturation)
        * denom * state["depth_mm"])
    drainable_event_water = np.minimum.reduce((
        excess_mm, state["event_storage_mm"],
        state["lower_boundary_remaining_mm"]))
    drained = np.minimum(drainable_event_water, potential)
    drained[~footprint] = 0.0
    theta[footprint] -= drained[footprint] / state["depth_mm"][footprint]
    state["event_storage_mm"] = np.maximum(
        0.0, state["event_storage_mm"] - drained)
    finite_boundary = np.isfinite(state["lower_boundary_remaining_mm"])
    state["lower_boundary_remaining_mm"][finite_boundary] = np.maximum(
        0.0,
        state["lower_boundary_remaining_mm"][finite_boundary] - drained[finite_boundary])
    state["deep_drainage_mm"] += drained
    return drained


def d8_graph(fdir, receiver_override=None):
    """Return flattened receiver and a checked upstream-to-downstream order."""
    h, w = fdir.shape
    valid = (fdir >= -1).ravel()
    receiver = np.full(h * w, -1, dtype=np.int64)
    for i, k in enumerate(fdir.ravel()):
        if k < 0:
            continue
        dr, dc = hydro._NB[int(k)]
        r, c = divmod(i, w)
        receiver[i] = (r + dr) * w + c + dc
    if receiver_override:
        for src, dst in receiver_override.items():
            receiver[int(src)] = int(dst)
    indegree = np.zeros(h * w, dtype=np.int64)
    for i in np.flatnonzero(valid):
        if receiver[i] >= 0:
            indegree[receiver[i]] += 1
    stack = [int(i) for i in np.flatnonzero(valid & (indegree == 0))]
    order = []
    while stack:
        i = stack.pop()
        order.append(i)
        rec = receiver[i]
        if rec >= 0:
            indegree[rec] -= 1
            if indegree[rec] == 0:
                stack.append(int(rec))
    if len(order) != int(valid.sum()):
        raise ValueError("D8 routing graph contains a cycle")
    return receiver, np.asarray(order, dtype=np.int64), valid


def depression_model(fields):
    """Build finite shared reservoirs for connected Priority-Flood depressions.

    This is not a nested Fill-Spill-Merge hierarchy. It is a conservative
    component-scale approximation: each connected depression shares its full
    LiDAR volume and spills through its lowest D8 exit only after that volume
    is occupied.
    """
    depth = fields["depression_depth"]
    footprint = np.isfinite(fields["dem"])
    mask = footprint & (depth > DEPRESSION_DEPTH_EPS_M)
    h, w = depth.shape
    labels = np.full((h, w), -1, dtype=np.int32)
    cells = []
    for r, c in np.argwhere(mask):
        if labels[r, c] >= 0:
            continue
        label = len(cells)
        stack = [(int(r), int(c))]
        labels[r, c] = label
        component = []
        while stack:
            cr, cc = stack.pop()
            component.append(cr * w + cc)
            for dr, dc in hydro._NB:
                nr, nc = cr + dr, cc + dc
                if (0 <= nr < h and 0 <= nc < w and mask[nr, nc]
                        and labels[nr, nc] < 0):
                    labels[nr, nc] = label
                    stack.append((nr, nc))
        cells.append(np.asarray(component, dtype=np.int64))

    receiver, _, valid = d8_graph(fields["flowdir"])
    override = {}
    exit_component = np.full(h * w, -1, dtype=np.int32)
    capacities = np.zeros(len(cells), dtype=float)
    cell_area = float(fields["cell_area_m2"])
    filled_flat = fields["filled"].ravel()
    for label, component in enumerate(cells):
        capacities[label] = float(np.nansum(depth.ravel()[component]) * cell_area)
        in_component = np.zeros(h * w, dtype=bool)
        in_component[component] = True
        exits = [int(i) for i in component
                 if receiver[i] < 0 or not in_component[receiver[i]]]
        if not exits:
            exits = [int(component[np.nanargmax(depth.ravel()[component])])]
        def spill_key(i):
            rec = receiver[i]
            return (filled_flat[rec] if rec >= 0 and np.isfinite(filled_flat[rec])
                    else filled_flat[i])
        canonical_exit = min(exits, key=spill_key)
        canonical_receiver = int(receiver[canonical_exit])
        for i in exits:
            override[i] = canonical_receiver
            exit_component[i] = label
    receiver, order, valid = d8_graph(fields["flowdir"], override)
    return {
        "labels": labels, "cells": cells, "capacity_m3": capacities,
        "exit_component": exit_component, "receiver": receiver,
        "order": order, "valid": valid,
    }


def allocate_depression_depth_m(model, stored_m3, fields):
    """Spread each shared reservoir volume to a level water surface."""
    out = np.zeros(fields["dem"].size, dtype=float)
    dem = fields["dem"].ravel()
    capacity_depth = fields["depression_depth"].ravel()
    area = float(fields["cell_area_m2"])
    for label, component in enumerate(model["cells"]):
        volume = min(max(float(stored_m3[label]), 0.0), model["capacity_m3"][label])
        if volume <= 0.0:
            continue
        low = float(np.nanmin(dem[component]))
        high = float(np.nanmax(dem[component] + capacity_depth[component]))
        for _ in range(32):
            level = 0.5 * (low + high)
            trial = np.minimum(np.maximum(level - dem[component], 0.0),
                               capacity_depth[component])
            if float(trial.sum() * area) < volume:
                low = level
            else:
                high = level
        out[component] = np.minimum(np.maximum(0.5 * (low + high) - dem[component], 0.0),
                                    capacity_depth[component])
    return out.reshape(fields["dem"].shape)


def routing_step_count(event_hours):
    return max(MIN_ROUTING_STEPS,
               int(math.ceil(event_hours / MAX_ROUTING_STEP_HOURS)))


def simulate_coupled_event(p_mm, event_hours, fields, soils, antecedent="normal",
                           frozen=False, steps=None, antecedent_wetness=None):
    """Run the coupled runon/infiltration/storage event and close its budget."""
    footprint = np.isfinite(fields["dem"])
    shape = footprint.shape
    area = float(fields["cell_area_m2"])
    forcing = np.asarray(p_mm, dtype=float)
    if forcing.ndim == 0:
        forcing = np.full(shape, float(forcing))
    if forcing.shape != shape:
        raise ValueError("forcing depth must be scalar or match the terrain grid")
    forcing = np.where(footprint, np.maximum(forcing, 0.0), 0.0)
    event_hours = max(float(event_hours), 1e-9)
    steps = int(steps or routing_step_count(event_hours))
    dt = event_hours / steps

    state = hydraulic_state(
        soils, fields, antecedent, frozen=frozen,
        antecedent_wetness=antecedent_wetness)
    depressions = depression_model(fields)
    flat_n = forcing.size
    stored = np.zeros(len(depressions["cells"]), dtype=float)
    infiltration_m3 = np.zeros(flat_n)
    local_infiltration_m3 = np.zeros(flat_n)
    local_excess_m3 = np.zeros(flat_n)
    saturation_excess_m3 = np.zeros(flat_n)
    runon_m3 = np.zeros(flat_n)
    throughflow_m3 = np.zeros(flat_n)
    boundary_outflow_m3 = 0.0
    local_step_m3 = forcing.ravel() / 1000.0 * area / steps

    for step in range(steps):
        if step:
            drain_profile(state, 0.5 * dt, footprint)

        moisture_deficit = np.maximum(
            state["theta_intake_limit"] - state["theta"], 0.0)
        ga_capacity = green_ampt_capacity_increment(
            state["cumulative_infiltration_mm"], state["ksat_mm_hr"],
            state["suction_mm"], moisture_deficit, dt)
        storage_room = moisture_deficit * state["depth_mm"]

        # Standing depression water gets first access to this step's intake
        # capacity; it remains in the reservoir rather than being re-routed.
        if stored.size and np.any(stored > 0.0):
            pond_depth_m = allocate_depression_depth_m(depressions, stored, fields)
            pond_mm = pond_depth_m * 1000.0
            pond_inf = np.minimum.reduce((pond_mm, ga_capacity, storage_room))
            pond_inf[~footprint] = 0.0
            pond_inf_m3 = pond_inf / 1000.0 * area
            for label, component in enumerate(depressions["cells"]):
                taken = float(pond_inf_m3.ravel()[component].sum())
                stored[label] = max(0.0, stored[label] - taken)
            state["theta"][footprint] += pond_inf[footprint] / state["depth_mm"][footprint]
            state["event_storage_mm"] += pond_inf
            state["cumulative_infiltration_mm"] += pond_inf
            infiltration_m3 += pond_inf_m3.ravel()
            ga_capacity = np.maximum(0.0, ga_capacity - pond_inf)
            storage_room = np.maximum(0.0, storage_room - pond_inf)

        routed = np.zeros(flat_n)
        theta_flat = state["theta"].ravel()
        event_storage_flat = state["event_storage_mm"].ravel()
        depth_flat = state["depth_mm"].ravel()
        cumulative_flat = state["cumulative_infiltration_mm"].ravel()
        ga_flat = ga_capacity.ravel()
        room_flat = storage_room.ravel()
        for i in depressions["order"]:
            local = local_step_m3[i]
            incoming = routed[i]
            available = local + incoming
            if incoming > 0.0:
                runon_m3[i] += incoming
            supply_mm = available / area * 1000.0
            accepted_mm = min(supply_mm, ga_flat[i], room_flat[i])
            accepted_m3 = accepted_mm / 1000.0 * area
            if accepted_m3 > 0.0:
                infiltration_m3[i] += accepted_m3
                local_share = local / available if available > 0.0 else 0.0
                local_infiltration_m3[i] += accepted_m3 * local_share
                theta_flat[i] += accepted_mm / depth_flat[i]
                event_storage_flat[i] += accepted_mm
                cumulative_flat[i] += accepted_mm
            local_unabsorbed = local * (1.0 - accepted_m3 / available) if available else 0.0
            local_excess_m3[i] += local_unabsorbed
            profile_limited = (room_flat[i] <= ga_flat[i] + 1e-9
                               and supply_mm > room_flat[i] + 1e-9)
            if profile_limited:
                saturation_excess_m3[i] += local_unabsorbed

            outflow = max(0.0, available - accepted_m3)
            component = depressions["exit_component"][i]
            if component >= 0 and outflow > 0.0:
                free = max(0.0, depressions["capacity_m3"][component] - stored[component])
                retained = min(outflow, free)
                stored[component] += retained
                outflow -= retained
            throughflow_m3[i] += outflow
            rec = depressions["receiver"][i]
            if rec >= 0:
                routed[rec] += outflow
            else:
                boundary_outflow_m3 += outflow

        drain_profile(state, 0.5 * dt, footprint)

    ponded_depth_m = allocate_depression_depth_m(depressions, stored, fields)
    storage_gain_mm = state["event_storage_mm"].copy()
    infiltration_mm = infiltration_m3.reshape(shape) / area * 1000.0
    local_infiltration_mm = local_infiltration_m3.reshape(shape) / area * 1000.0
    local_excess_mm = local_excess_m3.reshape(shape) / area * 1000.0
    saturation_excess_mm = saturation_excess_m3.reshape(shape) / area * 1000.0
    saturation_pct = 100.0 * np.clip(
        state["theta"] / np.maximum(state["theta_s"], 1e-6), 0.0, 1.0)
    initial_saturation_pct = 100.0 * np.clip(
        state["theta_initial"] / np.maximum(state["theta_s"], 1e-6), 0.0, 1.0)
    saturation_change_pct = saturation_pct - initial_saturation_pct

    input_m3 = float(forcing.sum() / 1000.0 * area)
    root_storage_m3 = float(np.sum(storage_gain_mm[footprint]) / 1000.0 * area)
    deep_drainage_m3 = float(np.sum(state["deep_drainage_mm"][footprint]) / 1000.0 * area)
    ponded_m3 = float(stored.sum())
    accounted_m3 = root_storage_m3 + deep_drainage_m3 + ponded_m3 + boundary_outflow_m3
    residual_m3 = input_m3 - accounted_m3
    relative_error = abs(residual_m3) / max(input_m3, 1e-12)
    if relative_error > 1e-8:
        raise RuntimeError("coupled hydrology mass balance failed: %.9g m3" % residual_m3)

    outputs = {
        "local_runoff_mm": local_excess_mm,
        "infiltration_mm": infiltration_mm,
        "local_infiltration_mm": local_infiltration_mm,
        "runon_infiltration_mm": np.maximum(0.0, infiltration_mm - local_infiltration_mm),
        "root_zone_storage_mm": storage_gain_mm,
        "deep_drainage_mm": state["deep_drainage_mm"].copy(),
        "saturation_excess_mm": saturation_excess_mm,
        "saturation_pct": saturation_pct,
        "initial_saturation_pct": initial_saturation_pct,
        "saturation_change_pct": saturation_change_pct,
        "runon_m3": runon_m3.reshape(shape),
        "flow_m3": throughflow_m3.reshape(shape),
        "ponded_water_mm": ponded_depth_m * 1000.0,
    }
    for values in outputs.values():
        values[~footprint] = np.nan
    return {
        **outputs,
        "state": state,
        "steps": steps, "step_hours": dt,
        "input_m3": input_m3,
        "boundary_outflow_m3": boundary_outflow_m3,
        "depression_storage_m3": ponded_m3,
        "depression_capacity_m3": float(depressions["capacity_m3"].sum()),
        "root_zone_storage_m3": root_storage_m3,
        "deep_drainage_m3": deep_drainage_m3,
        "mass_balance_residual_m3": residual_m3,
        "mass_balance_relative_error": relative_error,
        "depression_count": len(depressions["cells"]),
        "lower_boundary_restricted_cells": int(
            np.sum(state["lower_boundary_restricted"] & footprint)),
        "lower_boundary_finite_cells": int(
            np.sum(state["lower_boundary_finite"] & footprint)),
    }


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
INFILTRATION_RAMP = [(238, 249, 244, 0), (183, 228, 199, 105), (102, 194, 164, 170),
                     (44, 127, 134, 220), (17, 73, 86, 250)]
STORAGE_RAMP = [(255, 251, 218, 0), (217, 240, 163, 105), (145, 207, 96, 170),
                (59, 153, 92, 220), (20, 90, 80, 250)]
SATURATION_RAMP = [(239, 248, 255, 0), (186, 228, 255, 110), (94, 184, 232, 180),
                   (42, 112, 178, 225), (24, 48, 112, 250)]
DRAINAGE_RAMP = [(250, 246, 255, 0), (220, 206, 239, 105), (174, 151, 211, 175),
                 (119, 92, 168, 220), (65, 42, 105, 250)]
SATURATION_STATE_RAMP = [(250, 250, 250, 0), (207, 230, 240, 95), (103, 178, 200, 165),
                         (35, 112, 150, 220), (8, 48, 82, 250)]
PONDED_RAMP = [(240, 249, 255, 0), (160, 218, 255, 110), (67, 160, 221, 180),
               (20, 91, 160, 225), (7, 39, 92, 250)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["snowmelt", "rain"], default="snowmelt")
    ap.add_argument("--swe-in", type=float, default=None,
                    help="snowmelt: snow water equivalent, inches (water, not snow depth)")
    ap.add_argument("--preset", choices=["median", "p90", "max"], default=None,
                    help="snowmelt: take SWE from the twin's Daymet climatology")
    ap.add_argument("--melt-days", type=float, default=4.0)
    ap.add_argument("--rain-in", type=float, default=None,
                    help="snowmelt: rain-on-snow; rain: the storm total")
    ap.add_argument("--storm-hours", type=float, default=12.0,
                    help="rain: storm duration in hours")
    ap.add_argument("--antecedent", choices=["dry", "normal", "wet", "auto"],
                    default="normal")
    ap.add_argument("--as-of", default=None,
                    help="with --antecedent auto, use ET state on or before YYYY-MM-DD")
    ap.add_argument("--frozen", action="store_true",
                    help="restricted frozen-ground screening state (reduced Ksat and pore space)")
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
        rain_in = (args.rain_in if args.rain_in is not None
                   else (presets or {}).get("storm_1day_median_in") or 2.0)
        storm_hours = min(MAX_STORM_HOURS, max(0.5, args.storm_hours))
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
        rain_in = args.rain_in or 0.0
        melt_days = min(MAX_SNOWMELT_DAYS, max(0.5, args.melt_days))
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

    # -------------------------------------- coupled routing + soil-water state
    auto_state = et_antecedent_state(args.as_of) if args.antecedent == "auto" else None
    antecedent = (auto_state["equivalent_manual_class"] if auto_state else
                  "normal" if args.antecedent == "auto" else args.antecedent)
    antecedent_wetness = auto_state["wetness_index"] if auto_state else None
    water = simulate_coupled_event(
        p_mm, event_seconds / 3600.0, fields, soils, antecedent,
        frozen=args.frozen, antecedent_wetness=antecedent_wetness)
    runoff_mm = water["local_runoff_mm"]
    infil_mm = water["infiltration_mm"]
    root_storage_mm = water["root_zone_storage_mm"]
    deep_drainage_mm = water["deep_drainage_mm"]
    saturation_excess_mm = water["saturation_excess_mm"]
    saturation_pct = water["saturation_pct"]
    initial_saturation_pct = water["initial_saturation_pct"]
    saturation_change_pct = water["saturation_change_pct"]
    runon_m3_grid = water["runon_m3"]
    ponded_water_mm = water["ponded_water_mm"]
    flow_m3 = water["flow_m3"]

    cells = float(footprint.sum())
    area_m2 = cells * cell_m2

    def depth_metrics(values):
        mean_mm = float(np.nanmean(values))
        return mean_mm, mean_mm / 1000.0 * area_m2

    surface_remaining_m3 = (water["boundary_outflow_m3"] +
                            water["depression_storage_m3"])
    mean_runoff = surface_remaining_m3 / area_m2 * 1000.0
    runoff_m3 = surface_remaining_m3
    mean_infil, infil_m3 = depth_metrics(infil_mm)
    mean_root_storage, root_storage_m3 = depth_metrics(root_storage_mm)
    mean_deep_drainage, deep_drainage_m3 = depth_metrics(deep_drainage_mm)
    mean_saturation_excess, saturation_excess_m3 = depth_metrics(saturation_excess_mm)
    mean_saturation = float(np.nanmean(saturation_pct))
    mean_initial_saturation = float(np.nanmean(initial_saturation_pct))
    mean_saturation_change = float(np.nanmean(saturation_change_pct))

    # Peak discharge at all AOI outlets: event outflow over the event window with
    # a mode-appropriate peaking factor (snowmelt: diurnal ~2x; rain: tiered by
    # storm duration — short bursts are peakier). No travel-time routing is
    # present, so this remains a wide screening estimate.
    out_m3 = float(water["boundary_outflow_m3"])
    mean_q = out_m3 / event_seconds
    peak_q = mean_q * peaking
    cfs = 35.3147
    surface_partition_fraction = float(
        surface_remaining_m3 / max(water["input_m3"], 1e-12))
    below_surface_resolution = bool(
        surface_partition_fraction < SURFACE_SCREENING_RESOLUTION_FRACTION)

    storage_m3 = water["depression_capacity_m3"]

    # --------------------------------------------------------------- layers
    out_dir = os.path.join(D, "hydrology", "local")
    os.makedirs(out_dir, exist_ok=True)
    half = grid["cellsize"] / 2.0
    bounds = [round(grid["minX"] - half, 2), round(grid["minY"] - half, 2),
              round(grid["maxX"] + half, 2), round(grid["maxY"] + half, 2)]

    new_layers = []

    def export(layer_id, label, rgba, values, legend, description, decimals=2,
               value_kind=None, value_unit=None, swatch=None):
        png = os.path.join(out_dir, layer_id + ".png")
        metadata = {
            "value_kind": value_kind,
            "value_unit": value_unit,
            "value_classification": "continuous",
            "cell_area_m2": round(cell_m2, 6),
        }
        with open(os.path.join(out_dir, layer_id + ".grid.json"), "w") as fh:
            json.dump(t1.grid_json(values, bounds, legend, decimals=decimals,
                                   metadata=metadata), fh)
        t1.write_png(rgba, png)
        new_layers.append({
            "id": layer_id, "label": label, "type": "raster",
            "image": "hydrology/local/%s.png" % layer_id,
            "grid": "hydrology/local/%s.grid.json" % layer_id,
            "bounds_local": bounds, "acquisition": "derived",
            "group": "scenario", "scenario": scenario_label,
            "description": description,
            "value_kind": value_kind, "value_unit": value_unit,
            "cell_area_m2": round(cell_m2, 6),
            "swatch": swatch,
        })

    def export_depth(layer_id, label, values, ramp, value_kind, description,
                     color_percentile=99.0):
        finite = values[np.isfinite(values)]
        vmax = float(np.percentile(finite, color_percentile)) if finite.size else 0.0
        actual_max = float(np.max(finite)) if finite.size else 0.0
        norm = np.where(footprint, np.clip(values / max(vmax, 1.0), 0.0, 1.0), np.nan)
        export(
            layer_id, label, t1.colorize(norm, ramp), values,
            {"min": {"name": "0 mm", "color": list(ramp[1][:3])},
             "max": {"name": "%.0f mm%s" % (vmax, "+" if actual_max > vmax else ""),
                     "color": list(ramp[4][:3])}},
            description, decimals=1, value_kind=value_kind, value_unit="mm",
            swatch="#%02x%02x%02x" % tuple(ramp[3][:3]))

    export_depth(
        "scenario_runoff", "Scenario: local surface excess", runoff_mm, RUNOFF_RAMP,
        "event_local_surface_excess",
        "Rain or melt falling on this cell that was not absorbed here. It can "
        "still infiltrate farther downslope; this is local generation, not outlet "
        "runoff. Coupled Green-Ampt screening result; click for mm.")
    export_depth(
        "scenario_infiltration", "Scenario: infiltrated water", infil_mm,
        INFILTRATION_RAMP, "event_surface_infiltration",
        "Total event water entering the soil at this cell, including upstream "
        "runon. Values may exceed local rain or melt. SSURGO Green-Ampt screening "
        "result; click for equivalent mm over this cell.")
    export_depth(
        "scenario_soil_storage", "Scenario: profile water gain", root_storage_mm,
        STORAGE_RAMP, "event_root_zone_storage_gain",
        "Net event water remaining in the modeled soil profile after gravity "
        "drainage. Profile depth is capped by mapped bedrock or seasonal water "
        "table. Screening estimate; click for mm.")
    export_depth(
        "scenario_deep_drainage", "Scenario: profile percolation", deep_drainage_mm,
        DRAINAGE_RAMP, "event_deep_drainage",
        "Water already admitted to the soil that drains below the modeled profile "
        "through an analytic Brooks-Corey conductivity estimate. Mapped shallow "
        "boundaries have zero event leakage; a known deeper restriction has finite "
        "sub-profile storage. This is not calibrated groundwater recharge; click "
        "for mm.")
    export_depth(
        "scenario_saturation_excess", "Scenario: local saturation excess",
        saturation_excess_mm, SATURATION_RAMP, "event_saturation_excess",
        "Local rain or melt left on the surface specifically because incoming "
        "water had filled this cell's modeled soil pore space. Runon itself is "
        "not counted again here; click for mm.")

    saturation_norm = np.where(footprint, np.clip(saturation_pct / 100.0, 0.0, 1.0), np.nan)
    export(
        "scenario_saturation", "Scenario: profile saturation",
        t1.colorize(saturation_norm, SATURATION_STATE_RAMP), saturation_pct,
        {"min": {"name": "0%", "color": list(SATURATION_STATE_RAMP[1][:3])},
         "max": {"name": "100%", "color": list(SATURATION_STATE_RAMP[4][:3])}},
        "Final physical soil pore space occupied by water. It includes a disclosed "
        "relative-TWI antecedent wetness prior, but no TWI-reduced denominator; "
        "it is not an observed water table. Click for percent.", decimals=1,
        value_kind="estimated_profile_saturation", value_unit="pct",
        swatch="#%02x%02x%02x" % tuple(SATURATION_STATE_RAMP[3][:3]))

    export_depth(
        "scenario_ponded_water", "Scenario: retained pond water", ponded_water_mm,
        PONDED_RAMP, "event_retained_surface_water",
        "Water still retained in finite LiDAR depression storage at event end. "
        "Depressions spill only after their connected storage volume fills. "
        "Component-scale screening approximation; click for water depth in mm.")

    runon_log = np.log10(np.maximum(runon_m3_grid, 0.01))
    runon_hi = math.log10(max(1.0, float(np.nanpercentile(runon_m3_grid, 99))))
    runon_norm = np.clip((runon_log + 2.0) / max(runon_hi + 2.0, 1e-6), 0, 1)
    runon_norm[runon_m3_grid < 0.05] = np.nan
    runon_norm[~footprint] = np.nan
    export(
        "scenario_runon", "Scenario: arriving runon",
        t1.colorize(runon_norm, t1.FLOW_RAMP), runon_m3_grid,
        {"min": {"name": "0.05 m³", "color": list(t1.FLOW_RAMP[1][:3])},
         "max": {"name": "%.0f m³+" % (10 ** runon_hi),
                 "color": list(t1.FLOW_RAMP[4][:3])}},
        "Cumulative upstream surface water delivered to this cell before local "
        "infiltration. This is the routed input that was missing from the old "
        "soil-water model. Click for m³ over the event.", decimals=2,
        value_kind="routed_event_runon_volume", value_unit="m3",
        swatch="#%02x%02x%02x" % tuple(t1.FLOW_RAMP[2][:3]))

    logf = np.log10(np.maximum(flow_m3, 0.01))
    lo = -2.0
    hi = math.log10(max(1.0, float(np.nanpercentile(flow_m3, 99))))
    norm = np.clip((logf - lo) / (hi - lo), 0, 1)
    norm[flow_m3 < 0.05] = np.nan
    norm[~footprint] = np.nan
    export("scenario_flow", "Scenario: surface throughflow",
           t1.colorize(norm, t1.FLOW_RAMP), np.where(footprint, flow_m3, np.nan),
           {"min": {"name": "0.05 m³ over event", "color": list(t1.FLOW_RAMP[1][:3])},
            "max": {"name": "%.0f m³+ over event" % (10 ** hi),
                    "color": list(t1.FLOW_RAMP[4][:3])}},
           "Surface water leaving this cell after local infiltration and finite "
           "depression retention. D8 has no travel-time or backwater solution; "
           "click for m³ over the event.", decimals=2,
           value_kind="routed_event_flow_volume", value_unit="m3",
           swatch="#%02x%02x%02x" % tuple(t1.FLOW_RAMP[3][:3]))

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
        "antecedent_effective": antecedent,
        "label": scenario_label,
    }
    if args.as_of:
        scenario_params["as_of"] = args.as_of
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
            "infiltration_pct": round(100.0 * mean_infil / p_mm, 1) if p_mm else 0.0,
            "runoff_m3": round(runoff_m3, 0),
            "infiltration_m3": round(infil_m3, 0),
            "root_zone_storage_mm_mean": round(mean_root_storage, 1),
            "root_zone_storage_m3": round(root_storage_m3, 0),
            "deep_drainage_mm_mean": round(mean_deep_drainage, 1),
            "deep_drainage_m3": round(deep_drainage_m3, 0),
            "saturation_excess_mm_mean": round(mean_saturation_excess, 1),
            "saturation_excess_m3": round(saturation_excess_m3, 0),
            "profile_saturation_pct_mean": round(mean_saturation, 1),
            "initial_profile_saturation_pct_mean": round(mean_initial_saturation, 1),
            "profile_saturation_change_pct_points_mean": round(
                mean_saturation_change, 1),
            "boundary_outflow_m3": round(water["boundary_outflow_m3"], 3),
            "retained_surface_water_m3": round(water["depression_storage_m3"], 3),
            "mass_balance_residual_m3": round(water["mass_balance_residual_m3"], 9),
            "mass_balance_relative_error": water["mass_balance_relative_error"],
        },
        "soil_water_model": {
            "method": "runon-aware Green-Ampt / Mein-Larson with analytic Brooks-Corey drainage",
            "routing": "incremental, mass-conservative D8 transmission-loss routing",
            "depressions": "finite connected-component storage before D8 spill",
            "root_zone_depth_cm": ROOT_ZONE_DEPTH_CM,
            "time_steps": water["steps"],
            "step_hours": round(water["step_hours"], 4),
            "antecedent_awc_fill_fraction": water["state"]["antecedent_fill_fraction"],
            "twi_antecedent_adjustment": "+/-%.0f%% of AWC" %
                                         (TWI_ANTECEDENT_SPREAD * 50.0),
            "relative_vsa_twi_percentile": water["state"]["vsa_twi_threshold"],
            "relative_vsa_policy": (
                "TWI raises antecedent physical water content; antecedent water is "
                "held fixed and only traced event water may percolate"),
            "lower_boundary_policy": (
                "zero event leakage where mapped bedrock or seasonal water table "
                "falls inside the profile; finite drainable sub-profile storage "
                "where a deeper restriction is known; free drainage otherwise"),
            "lower_boundary_restricted_cells": water["lower_boundary_restricted_cells"],
            "lower_boundary_finite_storage_cells": water["lower_boundary_finite_cells"],
            "green_ampt_drainage_policy": (
                "cumulative wetting-front infiltration is not restored by profile "
                "drainage, conservatively reducing late-event suction capacity"),
            "frozen_ksat_factor": FROZEN_KSAT_FACTOR if args.frozen else 1.0,
            "frozen_available_pore_factor": FROZEN_PORE_SPACE_FACTOR if args.frozen else 1.0,
            "soil_inputs": ["SSURGO horizon texture and available water capacity",
                            "profile bottleneck / surface Ksat",
                            "bedrock / seasonal water-table depth",
                            "Rawls-Brakensiek texture hydraulic estimates"],
            "uncertainty": "uncalibrated screening; SSURGO matrix Ksat does not resolve macropores",
        },
        "outlet": {
            "event_volume_m3": round(out_m3, 0),
            "mean_discharge_m3s": None if below_surface_resolution else round(mean_q, 4),
            "peak_discharge_m3s_est": (
                None if below_surface_resolution else round(peak_q, 4)),
            "peak_discharge_cfs_est": (
                None if below_surface_resolution else round(peak_q * cfs, 2)),
            "below_screening_resolution": below_surface_resolution,
            "surface_partition_pct": round(100.0 * surface_partition_fraction, 3),
            "screening_resolution": "0.2% of event input",
            "uncertainty": (
                "below screening resolution (<0.2% of event input); numeric peak suppressed"
                if below_surface_resolution else
                "+/-50%% class or wider (ungauged; no travel-time routing, "
                "%.1fx empirical peaking factor)" % peaking),
        },
        "ponding": {
            "depression_storage_m3": storage_m3,
            "retained_water_m3": round(water["depression_storage_m3"], 3),
            "storage_fill_pct": (round(100.0 * water["depression_storage_m3"] / storage_m3, 1)
                                 if storage_m3 else None),
            "storage_filled": (bool(water["depression_storage_m3"] >= storage_m3 - 1e-6)
                               if storage_m3 else None),
            "depression_count": water["depression_count"],
            "note": "Finite connected-depression storage is applied before spill; "
                    "nested fill-spill-merge behavior is approximated at component scale."
                    if storage_m3 else None,
        },
        "layers": [l["id"] for l in new_layers],
        "default_visible_layers": ["scenario_saturation"],
        "antecedent_state": auto_state or (
            {"mode": "auto", "error": (
                "et/soil_water_daily.csv absent; used normal antecedent state")}
            if args.antecedent == "auto" else
            {"mode": "manual", "class": antecedent}),
        "notes": [
            "Screening-level and uncalibrated: relative runon, saturation, and "
            "ponding patterns are more defensible than absolute fluxes.",
            "Mapped shallow bedrock and water-table boundaries block event-scale "
            "vertical leakage; a known deeper restriction has finite sub-profile "
            "storage, and only traced event water drains.",
            "Runon is coupled into infiltration and the parcel-wide water budget "
            "closes across soil storage, percolation, retained pond water, and outlets.",
            "D8 routing is instantaneous and has no backwater or hydrograph; "
            "SSURGO matrix Ksat can differ greatly from field intake rates.",
            "Outlet discharge is this AOI's own contribution; any watercourse "
            "crossing it drains a basin far beyond the twin's footprint.",
        ] + ([] if soils.get("available") else [
            "No soil data for this twin — sandy-loam / HSG-B screening defaults "
            "replace spatial SSURGO hydraulic properties.",
        ]) + ([] if presets is not None else [
            "No climate forcing for this twin — snowmelt/storm presets are "
            "unavailable; supply event depths explicitly.",
        ]) + ([] if args.antecedent != "auto" or auto_state else [
            "ET-derived antecedent moisture was requested, but no ET water-balance "
            "table was found; the scenario used the normal antecedent state.",
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
        if below_surface_resolution:
            print("  outlet       %.0f m^3 event volume; peak below screening resolution" %
                  out_m3)
        else:
            print("  outlet       %.0f m^3 event volume, peak ~%.2f cfs (+/-50%%)" %
                  (out_m3, peak_q * cfs))


if __name__ == "__main__":
    main()
