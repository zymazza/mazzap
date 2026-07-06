#!/usr/bin/env python3
"""Core wildfire behavior and fuel-moisture engine for VEIL twins -- pure numpy.

The fire-behavior core is the M0 wildfire engine: Scott & Burgan FBFM40 fuels,
dynamic herbaceous curing, Rothermel (1972) surface spread as summarized by
Andrews (RMRS-GTR-371), Andrews' wind adjustment / wind-limit guidance, and
Byram fireline intensity / flame length.  M1 adds scenario-derived fuel
moisture helpers.  M2 adds Van Wagner / Scott-Reinhardt crown-fire screening
and static Tier-1 fields.  M3 adds anisotropic minimum-travel-time front
propagation.  It still deliberately stops before the later milestones: no
scenario CLI, and no file I/O beyond reusing ``twin_hydrology.load_grid``.

Grid convention follows ``twin_hydrology.py``.  All public speeds are SI:
wind speed in m/min, rate of spread in m/min, heat content in kJ/kg,
fireline intensity in kW/m, flame length in m, and fuel moisture as a dry
weight fraction (6 percent -> 0.06).
"""

import heapq
import math
import os
import warnings

import numpy as np

import twin_hydrology as hydro

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

_NB = hydro._NB
load_grid = hydro.load_grid
slope_radians = hydro.slope_radians

KG_M2_TO_LB_FT2 = 0.204816143622
T_AC_TO_KG_M2 = 0.224170231
M_INV_TO_FT_INV = 0.3048
M_TO_FT = 3.280839895
FT_TO_M = 0.3048
KJ_KG_TO_BTU_LB = 1.0 / 2.3259999996
BTU_LB_TO_KJ_KG = 2.3259999996
BTU_FT2_MIN_TO_KW_M2 = 0.189273025894
MPH_TO_M_MIN = 26.8224

PARTICLE_DENSITY_LB_FT3 = 32.0
TOTAL_MINERAL = 0.0555
EFFECTIVE_MINERAL = 0.01
ALMOST_ZERO = 1e-12

# Tier-1 crown-potential reference: a dry, breezy screening wind, expressed as
# a 20-ft open wind.  compute_static() converts it to midflame wind through the
# canopy-cover WAF and combines it with the local slope magnitude as an
# uphill-aligned potential, not an event forecast.
STATIC_REFERENCE_OPEN_WIND_MPH = 20.0
TI_CI_MAX_OPEN_WIND_MPH = 120.0

# Computational classes: dead 1h, dead 10h, dead 100h, live herb, live woody.
DEAD = slice(0, 3)
LIVE = slice(3, 5)


def _safe_div(num, den, default=0.0):
    num, den = np.broadcast_arrays(np.asarray(num, dtype=float),
                                   np.asarray(den, dtype=float))
    return np.divide(num, den, out=np.full(np.shape(num), default, dtype=float),
                     where=np.abs(den) > ALMOST_ZERO)


# ---------------------------------------------------------------------------
# Fuel-moisture scenario helpers (wildfire.md section 2)
# ---------------------------------------------------------------------------

_LIVE_MOISTURE_CLASSES = {
    1: (30.0, 60.0),
    2: (60.0, 90.0),
    3: (90.0, 120.0),
    4: (120.0, 150.0),
}


def emc_simard(T_F, RH_pct):
    """Simard equilibrium moisture content, in percent.

    ``RH_pct`` is clamped to 1..100 percent.  The three RH bands intentionally
    preserve the small Simard discontinuities at 10 and 50 percent RH.
    """
    t, h = np.broadcast_arrays(np.asarray(T_F, dtype=float),
                               np.asarray(RH_pct, dtype=float))
    h = np.clip(h, 1.0, 100.0)
    low = 0.03229 + 0.281073 * h - 0.000578 * h * t
    mid = 2.22749 + 0.160107 * h - 0.014784 * t
    high = 21.0606 + 0.005565 * h * h - 0.00035 * h * t - 0.483199 * h
    return np.where(h < 10.0, low, np.where(h < 50.0, mid, high))


def dead_moisture(T_F, RH_pct, days_since_rain, exposure="shaded"):
    """Scenario-derived dead fuel moisture for 1h/10h/100h fuels.

    Moistures are computed in percent using Simard EMC and the section 2.2
    time-lag dry-down, then returned as dry-weight fractions suitable for
    ``rothermel_ros`` (6 percent -> 0.06).  ``exposure`` applies a small
    screening-grade sunlight/wind correction: shaded preserves the original
    litter curve, mixed and open fuels dry slightly faster and equilibrate
    slightly lower.
    """
    label = _norm_label(exposure) or "shaded"
    if label not in ("shaded", "mixed", "open"):
        raise ValueError("unknown exposure class: %r" % exposure)
    e = emc_simard(T_F, RH_pct)
    emc_offset = {"shaded": 0.0, "mixed": -1.0, "open": -2.0}[label]
    dry_rate = {"shaded": 1.0, "mixed": 1.15, "open": 1.30}[label]
    e = np.clip(e + emc_offset, 1.0, 100.0)
    d = np.maximum(0.0, np.asarray(days_since_rain, dtype=float))
    e, d = np.broadcast_arrays(np.asarray(e, dtype=float), d)

    m1 = e + (35.0 - e) * np.exp(-24.0 * d * dry_rate / 1.0)
    m10 = (e + 2.0) + (35.0 - (e + 2.0)) * np.exp(-24.0 * d * dry_rate / 10.0)
    m100 = (e + 4.0) + (30.0 - (e + 4.0)) * np.exp(-24.0 * d * dry_rate / 100.0)

    m1 = np.clip(m1, 2.0, 35.0)
    m10 = np.clip(m10, 3.0, 35.0)
    m100 = np.clip(m100, 4.0, 40.0)
    return m1 / 100.0, m10 / 100.0, m100 / 100.0


def gsi21(tmin_C, tmax_C, rhmin_pct, daylength_s):
    """Daily Jolly/NFDRS4 Growing Season Index term.

    Accepts arrays representing a running daily history and returns daily GSI;
    callers compute the 21-day mean.  This path needs an RH history, which is
    the gridMET upgrade path because Daymet does not provide RH.
    """
    tmin, tmax, rhmin, daylen = np.broadcast_arrays(
        np.asarray(tmin_C, dtype=float),
        np.asarray(tmax_C, dtype=float),
        np.asarray(rhmin_pct, dtype=float),
        np.asarray(daylength_s, dtype=float),
    )
    rhmin = np.clip(rhmin, 0.0, 100.0)
    i_tmin = np.clip((tmin + 2.0) / 7.0, 0.0, 1.0)
    es_tmax = 610.7 * np.exp(17.38 * tmax / (239.0 + tmax))
    vpd = es_tmax * (1.0 - rhmin / 100.0)
    i_vpd = np.clip(1.0 - (vpd - 900.0) / (4100.0 - 900.0), 0.0, 1.0)
    i_daylength = np.clip((daylen - 36000.0) / (39600.0 - 36000.0), 0.0, 1.0)
    return i_tmin * i_vpd * i_daylength


def _norm_label(value):
    if value is None:
        return ""
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _live_class_from_label(value):
    label = _norm_label(value)
    compact = label.replace("_", "")
    for idx in range(1, 5):
        if compact == "l%d" % idx or compact.endswith("l%d" % idx):
            return idx

    aliases = {
        "dormant": 1,
        "cured": 1,
        "dormant_fall": 1,
        "normal_spring": 1,
        "transition": 2,
        "dry_windy_spring": 2,
        "high_spring": 2,
        "extreme_redflag": 2,
        "extreme_red_flag": 2,
        "redflag": 2,
        "red_flag": 2,
        "summer_drought": 2,
        "late_summer_drought": 2,
        "leaf_on_drought": 2,
        "drought": 2,
        "greenup": 3,
        "green_up": 3,
        "green": 3,
        "fully_green": 4,
        "leaf_on": 4,
        "leafout": 4,
        "lush": 4,
    }
    if label in aliases:
        return aliases[label]
    raise ValueError("unknown live-moisture phenology/class: %r" % value)


def live_moisture(scenario_or_gsi, phenology="auto", drought="normal"):
    """Live herbaceous and woody moisture, in percent.

    With ``phenology='gsi'``, ``scenario_or_gsi`` is a 21-day mean GSI and the
    Jolly/NFDRS4 mapping is used.  Otherwise M1 maps a scenario phenology or an
    explicit Scott & Burgan live class such as ``L2`` to the D/L table values.
    ``drought`` is accepted for scenario provenance; the chosen D/L class is the
    v1 authority and is not separately adjusted here.
    """
    del drought
    if _norm_label(phenology) == "gsi":
        gsi = np.clip(np.asarray(scenario_or_gsi, dtype=float), 0.0, 1.0)
        herb = np.where(gsi > 0.5, 440.0 * gsi - 190.0, 30.0)
        woody = np.where(gsi > 0.5, 280.0 * gsi - 80.0, 60.0)
        return herb, woody

    source = scenario_or_gsi if _norm_label(phenology) == "auto" else phenology
    if not isinstance(source, str):
        raise ValueError("numeric live moisture input requires phenology='gsi'")
    return _LIVE_MOISTURE_CLASSES[_live_class_from_label(source)]


def _fbp_latn_d0(lat, lon_west, elev_m):
    lat, lon, elev = np.broadcast_arrays(np.asarray(lat, dtype=float),
                                         np.asarray(lon_west, dtype=float),
                                         np.asarray(elev_m, dtype=float))
    latn_pos = 43.0 + 33.7 * np.exp(-0.0351 * (150.0 - lon))
    latn_zero = 46.0 + 23.4 * np.exp(-0.0360 * (150.0 - lon))
    latn = np.where(elev > 0.0, latn_pos, latn_zero)
    d0_raw = np.where(elev > 0.0,
                      142.1 * (lat / latn) + 0.0172 * elev,
                      151.0 * (lat / latn))
    return latn, np.rint(d0_raw)


def fbp_fmc(lat, lon_west, elev_m, doy, drought="normal"):
    """Canadian FBP foliar moisture content, in percent.

    Longitude is positive degrees west, matching the CFFDRS/ST-X-3 formula.
    Drought nudges are multi-week class effects: normal 0, severe -5, extreme
    -10.  Output is clamped to 80..120 percent, except explicit catastrophic
    drought uses a 75 percent lower bound.
    """
    _latn, d0 = _fbp_latn_d0(lat, lon_west, elev_m)
    d0, day = np.broadcast_arrays(np.asarray(d0, dtype=float),
                                  np.asarray(doy, dtype=float))
    nd = np.abs(day - d0)
    fmc = np.where(
        nd < 30.0,
        85.0 + 0.0189 * nd * nd,
        np.where(nd < 50.0,
                 32.9 + 3.17 * nd - 0.0288 * nd * nd,
                 120.0),
    )

    drought_label = _norm_label(drought)
    catastrophic = drought_label in ("catastrophic", "explicit_catastrophic")
    nudges = {
        "normal": 0.0,
        "dry": 0.0,
        "moderate": 0.0,
        "severe": -5.0,
        "extreme": -10.0,
        "catastrophic": -10.0,
        "explicit_catastrophic": -10.0,
    }
    if drought_label not in nudges:
        raise ValueError("unknown FMC drought class: %r" % drought)
    lower = 75.0 if catastrophic else 80.0
    return np.clip(fmc + nudges[drought_label], lower, 120.0)


def _label_matches(label, names):
    parts = set(label.split("_"))
    return label in names or bool(parts & names)


def select_fmc_method(veg, region):
    """Return the region-selected foliar-moisture method label.

    The rule set encodes wildfire.md's Generalizability table: no tree canopy
    returns ``not_applicable``; Mediterranean shrub/chaparral uses LFMC
    observations; boreal/temperate North-American and temperate European
    conifer use the Canadian FBP spring dip; hardwood/deciduous and unknown
    cases use a constant low-confidence fallback.
    """
    v = _norm_label(veg)
    r = _norm_label(region)
    no_tree = {
        "nb", "nonburnable", "barren", "desert", "grass", "grassland", "gr",
        "gs", "herb", "herbaceous",
    }
    shrub = {"sh", "shrub", "shrubland", "chaparral"}
    hardwood = {"hardwood", "deciduous", "broadleaf", "broadleaf_forest"}
    conifer = {
        "conifer", "evergreen", "needleleaf", "pine", "spruce", "fir",
        "cedar",
    }
    mediterranean = {
        "med", "mediterranean", "california", "ca", "ca_mediterranean",
    }
    fbp_regions = {
        "boreal", "boreal_na", "temperate_na", "northeast_us", "adirondack",
        "alaska", "ak", "canada", "pnw", "rockies", "temperate_eu",
        "europe_temperate",
    }

    if _label_matches(v, shrub) and _label_matches(r, mediterranean):
        return "lfmc_obs"
    if _label_matches(v, no_tree) or _label_matches(v, shrub):
        return "not_applicable"
    if _label_matches(v, conifer) and _label_matches(r, fbp_regions):
        return "fbp_spring_dip"
    if _label_matches(v, hardwood):
        return "const"
    return "const"


def derive_fmc(method, veg, region, doy, drought="normal",
               lat=None, lon_west=None, elev_m=None, lfmc=None):
    """Apply the selected FMC method and return ``(FMC_pct, method_used)``."""
    method_label = _norm_label(method)
    selected = select_fmc_method(veg, region) if method_label in ("", "auto") else method_label

    if selected == "fbp_spring_dip":
        if lat is None or lon_west is None or elev_m is None:
            raise ValueError("fbp_spring_dip requires lat, lon_west, and elev_m")
        return fbp_fmc(lat, lon_west, elev_m, doy, drought=drought), selected

    if selected == "not_applicable":
        return None, selected

    if selected == "lfmc_obs":
        if lfmc is not None:
            return np.asarray(lfmc, dtype=float), selected
        warnings.warn("LFMC observation missing; falling back to constant FMC",
                      RuntimeWarning, stacklevel=2)
        selected = "const"

    if selected == "const":
        if _label_matches(_norm_label(veg), {"hardwood", "deciduous", "broadleaf"}):
            warnings.warn("Using low-confidence hardwood constant FMC=120%",
                          RuntimeWarning, stacklevel=2)
            return 120.0, selected
        warnings.warn("Using high-uncertainty fallback constant FMC=100%",
                      RuntimeWarning, stacklevel=2)
        return 100.0, selected

    raise ValueError("unknown FMC method: %r" % method)


# ---------------------------------------------------------------------------
# Fire behavior
# ---------------------------------------------------------------------------


def _nonburnable_params(code):
    return {
        "code": int(code),
        "short_name": "NB%d" % max(0, int(code) - 90),
        "group": "nonburnable",
        "burnable": False,
        "dynamic": False,
        "load_dead_1h": 0.0,
        "load_dead_10h": 0.0,
        "load_dead_100h": 0.0,
        "load_live_herb": 0.0,
        "load_live_woody": 0.0,
        "sav_dead_1h": 0.0,
        "sav_dead_10h": 0.0,
        "sav_dead_100h": 0.0,
        "sav_live_herb": 0.0,
        "sav_live_woody": 0.0,
        "depth": 0.0,
        "moisture_extinction_dead": 0.0,
        "heat": 0.0,
    }


def _param(param_table, code):
    c = int(code)
    if 91 <= c <= 99:
        return param_table.get(c, _nonburnable_params(c))
    return param_table.get(c, _nonburnable_params(c))


def _size_class(sigma_ft):
    sigma_ft = np.asarray(sigma_ft, dtype=float)
    out = np.full(sigma_ft.shape, 6, dtype=np.int8)
    out = np.where(sigma_ft >= 16.0, 5, out)
    out = np.where(sigma_ft >= 48.0, 4, out)
    out = np.where(sigma_ft >= 96.0, 3, out)
    out = np.where(sigma_ft >= 192.0, 2, out)
    out = np.where(sigma_ft >= 1200.0, 1, out)
    return out


def _precompute_fuelbed(bed):
    loads_lb = bed["loads"] * KG_M2_TO_LB_FT2
    sigma_ft = bed["sav"] * M_INV_TO_FT_INV
    depth_ft = bed["depth"] * M_TO_FT
    heat_btu = bed["heat"] * KJ_KG_TO_BTU_LB

    area = sigma_ft * loads_lb / PARTICLE_DENSITY_LB_FT3
    area_dead = np.sum(area[..., DEAD], axis=-1)
    area_live = np.sum(area[..., LIVE], axis=-1)
    area_total = area_dead + area_live

    f_ij = np.zeros_like(area)
    f_ij[..., DEAD] = _safe_div(area[..., DEAD], area_dead[..., None])
    f_ij[..., LIVE] = _safe_div(area[..., LIVE], area_live[..., None])
    f_dead = _safe_div(area_dead, area_total)
    f_live = _safe_div(area_live, area_total)

    sigma_dead = np.sum(f_ij[..., DEAD] * sigma_ft[..., DEAD], axis=-1)
    sigma_live = np.sum(f_ij[..., LIVE] * sigma_ft[..., LIVE], axis=-1)
    sigma_prime = f_dead * sigma_dead + f_live * sigma_live

    load_volume = np.sum(loads_lb / PARTICLE_DENSITY_LB_FT3, axis=-1)
    beta = _safe_div(load_volume, depth_ft)
    beta_op = np.ones_like(sigma_prime)
    sigma_mask = sigma_prime > 0.0
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        beta_op[sigma_mask] = 3.348 * np.power(sigma_prime[sigma_mask], -0.8189)
    rho_b = _safe_div(np.sum(loads_lb, axis=-1), depth_ft)

    cls = _size_class(sigma_ft)
    g_ij = np.zeros_like(f_ij)
    for j in range(5):
        same_size = cls == cls[..., j][..., None]
        same_cat = np.zeros_like(same_size, dtype=bool)
        if j < 3:
            same_cat[..., DEAD] = True
        else:
            same_cat[..., LIVE] = True
        g_ij[..., j] = np.sum(np.where(same_size & same_cat, f_ij, 0.0), axis=-1)

    rel = _safe_div(beta, beta_op)
    b = np.where(sigma_prime > 0.0, 0.02526 * np.power(sigma_prime, 0.54), 0.0)
    c = np.where(sigma_prime > 0.0,
                 7.47 * np.exp(-0.133 * np.power(sigma_prime, 0.55)), 0.0)
    e = np.where(sigma_prime > 0.0, 0.715 * np.exp(-3.59e-4 * sigma_prime), 0.0)
    f = np.where(rel > 0.0, np.power(rel, e), 0.0)

    phi_w_scalr = np.zeros_like(sigma_prime)
    mask = f > 0.0
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        phi_w_scalr[mask] = (c[mask] / f[mask]) * np.power(M_TO_FT, b[mask])

    ws_scalr = np.zeros_like(sigma_prime)
    ws_expnt = np.zeros_like(sigma_prime)
    mask = (b > 0.0) & (c > 0.0) & (f > 0.0)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        ws_scalr[mask] = FT_TO_M * np.power(f[mask] / c[mask], 1.0 / b[mask])
        ws_expnt[mask] = 1.0 / b[mask]

    phi_s_g = np.zeros_like(beta)
    beta_mask = beta > 0.0
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        phi_s_g[beta_mask] = 5.275 * np.power(beta[beta_mask], -0.3)
    residence = _safe_div(384.0, sigma_prime)

    bed.update({
        "loads_lb_ft2": loads_lb,
        "sav_ft": sigma_ft,
        "heat_btu_lb": heat_btu,
        "f_ij": f_ij,
        "f_dead": f_dead,
        "f_live": f_live,
        "g_ij": g_ij,
        "sigma_prime_ft": sigma_prime,
        "beta": beta,
        "beta_op": beta_op,
        "rho_b_lb_ft3": rho_b,
        "phi_w_scalr": phi_w_scalr,
        "phi_w_expnt": b,
        "ws_scalr": ws_scalr,
        "ws_expnt": ws_expnt,
        "phi_s_g": phi_s_g,
        "residence_time_min": residence,
    })
    return bed


def fuel_bed(fbfm_code_grid, param_table, live_herb_moisture):
    """Map FBFM40 codes to per-cell fuel arrays and Rothermel constants.

    ``live_herb_moisture`` is a fraction.  Dynamic herbaceous fuel models use
    Scott & Burgan's 30-120 percent curing window:

      cured = clip((1.20 - M_live_herb) / (1.20 - 0.30), 0, 1)

    The cured live-herb load is transferred into the dead 1-hr time-lag class
    before computing the bed.  Its SAV is preserved through an area-weighted
    1-hr SAV, matching the Behave-style dynamic fuel treatment.
    """
    codes = np.asarray(fbfm_code_grid)
    shape = codes.shape
    loads = np.zeros(shape + (5,), dtype=float)
    sav = np.zeros(shape + (5,), dtype=float)
    depth = np.zeros(shape, dtype=float)
    mx_dead = np.zeros(shape, dtype=float)
    heat = np.zeros(shape, dtype=float)
    burnable = np.zeros(shape, dtype=bool)
    dynamic = np.zeros(shape, dtype=bool)

    for raw_code in np.unique(codes.astype(int)):
        p = _param(param_table, int(raw_code))
        mask = codes == raw_code
        load_values = [
            p["load_dead_1h"], p["load_dead_10h"], p["load_dead_100h"],
            p["load_live_herb"], p["load_live_woody"],
        ]
        sav_values = [
            p["sav_dead_1h"], p["sav_dead_10h"], p["sav_dead_100h"],
            p["sav_live_herb"], p["sav_live_woody"],
        ]
        for j in range(5):
            loads[..., j] = np.where(mask, load_values[j], loads[..., j])
            sav[..., j] = np.where(mask, sav_values[j], sav[..., j])
        depth = np.where(mask, p["depth"], depth)
        mx_dead = np.where(mask, p["moisture_extinction_dead"], mx_dead)
        heat = np.where(mask, p["heat"], heat)
        burnable = np.where(mask, p["burnable"], burnable)
        dynamic = np.where(mask, p["dynamic"], dynamic)

    live_m = np.broadcast_to(np.asarray(live_herb_moisture, dtype=float), shape)
    cured = np.where(dynamic, np.clip((1.20 - live_m) / (1.20 - 0.30), 0.0, 1.0), 0.0)
    moved = loads[..., 3] * cured
    dead_1h_old = loads[..., 0].copy()
    sav_1h_old = sav[..., 0].copy()
    new_dead_1h = dead_1h_old + moved
    sav[..., 0] = np.where(new_dead_1h > 0.0,
                           _safe_div(dead_1h_old * sav_1h_old + moved * sav[..., 3],
                                     new_dead_1h),
                           sav[..., 0])
    loads[..., 0] = new_dead_1h
    loads[..., 3] = np.maximum(0.0, loads[..., 3] - moved)

    total_load = np.sum(loads, axis=-1)
    burnable = burnable & (total_load > 0.0) & (depth > 0.0)
    burnable = burnable & np.isfinite(total_load) & np.isfinite(depth)

    bed = {
        "code": codes.astype(int),
        "loads": loads,
        "sav": sav,
        "depth": depth,
        "mx_dead": mx_dead,
        "heat": heat,
        "burnable": burnable,
        "dynamic": dynamic,
        "cured_fraction": cured,
        "load_dead_1h": loads[..., 0],
        "load_dead_10h": loads[..., 1],
        "load_dead_100h": loads[..., 2],
        "load_live_herb": loads[..., 3],
        "load_live_woody": loads[..., 4],
    }
    return _precompute_fuelbed(bed)


def midflame_wind(wind_20ft_open, canopy_cover):
    """Reduce open 20-ft wind to midflame wind with an RMRS-GTR-266 WAF.

    M0 has only canopy cover in its public signature, so this uses the
    two-regime Behave/Scott convention:

      * unsheltered cells (canopy cover <= 5 percent): WAF = 0.40, the
        standard 20-ft/open-wind to midflame reduction used by Rothermel.
      * sheltered cells: Scott's canopy-cover WAF classes from Andrews
        RMRS-GTR-266 table 7 (0.30 down to 0.10 as cover increases).

    The return speed has the same units as ``wind_20ft_open`` and is clamped >= 0.
    """
    wind = np.maximum(0.0, np.asarray(wind_20ft_open, dtype=float))
    cc = np.asarray(canopy_cover, dtype=float)
    cc = np.where(cc > 1.0, cc / 100.0, cc)
    cc = np.clip(np.nan_to_num(cc, nan=0.0), 0.0, 1.0)

    waf = np.full(np.broadcast(wind, cc).shape, 0.40, dtype=float)
    cc_b = np.broadcast_to(cc, waf.shape)
    waf = np.where((cc_b > 0.05) & (cc_b <= 0.10), 0.30, waf)
    waf = np.where((cc_b > 0.10) & (cc_b <= 0.15), 0.25, waf)
    waf = np.where((cc_b > 0.15) & (cc_b <= 0.30), 0.20, waf)
    waf = np.where((cc_b > 0.30) & (cc_b <= 0.50), 0.15, waf)
    waf = np.where(cc_b > 0.50, 0.10, waf)
    return np.broadcast_to(wind, waf.shape) * waf


def _az_to_xy(azimuth_rad, magnitude):
    return magnitude * np.sin(azimuth_rad), magnitude * np.cos(azimuth_rad)


def wind_slope_factors(fuelbed, midflame_u, slope_rad, aspect_rad, wind_dir_rad):
    """Return wind/slope spread factors and max-spread direction.

    Angles are azimuth radians clockwise from north.  ``wind_dir_rad`` is the
    downwind direction; ``aspect_rad`` is downslope aspect, so the slope vector
    points toward ``aspect + pi``.  Wind and slope are combined as vectors.

    The returned ``phi_w`` and ``phi_s`` are limited components whose sum is
    the combined effective wind+slope factor used by ``rothermel_ros``.  When
    a caller supplies ``fuelbed['reaction_intensity_btu_ft2_min']``, Andrews'
    effective-wind limit U_eff <= 0.9 * I_R is applied here; ``rothermel_ros``
    repeats the same cap with the exact current-moisture I_R as a final guard.
    """
    shape = np.shape(fuelbed["depth"])
    u = np.broadcast_to(np.asarray(midflame_u, dtype=float), shape)
    u = np.maximum(0.0, np.nan_to_num(u, nan=0.0, posinf=0.0, neginf=0.0))
    slope = np.broadcast_to(np.asarray(slope_rad, dtype=float), shape)
    aspect = np.broadcast_to(np.asarray(aspect_rad, dtype=float), shape)
    wind_dir = np.broadcast_to(np.asarray(wind_dir_rad, dtype=float), shape)

    slope_ratio = np.tan(np.nan_to_num(slope, nan=0.0, posinf=0.0, neginf=0.0))
    slope_ratio = np.maximum(0.0, slope_ratio)

    with np.errstate(over="ignore", invalid="ignore"):
        phi_w_raw = fuelbed["phi_w_scalr"] * np.power(u, fuelbed["phi_w_expnt"])
    phi_w_raw = np.where(np.isfinite(phi_w_raw), phi_w_raw, 0.0)
    phi_s_raw = fuelbed["phi_s_g"] * slope_ratio * slope_ratio
    phi_s_raw = np.where(np.isfinite(phi_s_raw), phi_s_raw, 0.0)

    wx, wy = _az_to_xy(wind_dir, phi_w_raw)
    sx, sy = _az_to_xy(aspect + math.pi, phi_s_raw)
    vx = wx + sx
    vy = wy + sy
    phi_e = np.sqrt(vx * vx + vy * vy)

    eff_wind = np.zeros(shape, dtype=float)
    mask = phi_e > 0.0
    with np.errstate(over="ignore", invalid="ignore"):
        eff_wind[mask] = fuelbed["ws_scalr"][mask] * np.power(phi_e[mask], fuelbed["ws_expnt"][mask])
    eff_wind = np.where(np.isfinite(eff_wind), eff_wind, 0.0)

    if "reaction_intensity_btu_ft2_min" in fuelbed:
        max_eff = 0.9 * fuelbed["reaction_intensity_btu_ft2_min"] * FT_TO_M
        over = (eff_wind > max_eff) & (max_eff > 0.0)
        phi_limit = fuelbed["phi_w_scalr"] * np.power(max_eff, fuelbed["phi_w_expnt"])
        phi_e = np.where(over, phi_limit, phi_e)
        eff_wind = np.where(over, max_eff, eff_wind)

    raw_sum = phi_w_raw + phi_s_raw
    phi_w = phi_e * _safe_div(phi_w_raw, raw_sum)
    phi_s = phi_e * _safe_div(phi_s_raw, raw_sum)

    max_dir = np.mod(np.arctan2(vx, vy), 2.0 * math.pi)
    max_dir = np.where(phi_e > 0.0, max_dir, 0.0)
    zero = ~fuelbed["burnable"]
    return (np.where(zero, 0.0, phi_w),
            np.where(zero, 0.0, phi_s),
            np.where(zero, 0.0, eff_wind),
            np.where(zero, np.nan, max_dir))


def _moisture_arrays(moisture, shape):
    if isinstance(moisture, dict):
        aliases = [
            ("dead_1h", "m1", "dead1", "1h"),
            ("dead_10h", "m10", "dead10", "10h"),
            ("dead_100h", "m100", "dead100", "100h"),
            ("live_herb", "herb", "live_herbaceous"),
            ("live_woody", "woody", "live_woody"),
        ]
        arr = np.zeros(shape + (5,), dtype=float)
        for j, keys in enumerate(aliases):
            value = None
            for key in keys:
                if key in moisture:
                    value = moisture[key]
                    break
            if value is None:
                raise KeyError("moisture is missing %s" % keys[0])
            arr[..., j] = np.broadcast_to(np.asarray(value, dtype=float), shape)
        return arr

    vals = np.asarray(moisture, dtype=float)
    if vals.shape == ():
        return np.full(shape + (5,), float(vals), dtype=float)
    if vals.shape[-1] != 5:
        raise ValueError("moisture must be scalar, dict, or array with 5 classes")
    return np.broadcast_to(vals, shape + (5,)).astype(float, copy=False)


def _moisture_damping(m_f, m_x):
    ratio = np.minimum(1.0, _safe_div(m_f, m_x))
    eta = 1.0 - 2.59 * ratio + 5.11 * ratio * ratio - 3.52 * ratio * ratio * ratio
    eta = np.maximum(0.0, eta)
    return np.where(m_x > 0.0, eta, 0.0)


def _reaction_and_base_ros(fuelbed, moisture):
    shape = np.shape(fuelbed["depth"])
    m = np.clip(_moisture_arrays(moisture, shape), 0.0, 5.0)

    loads = fuelbed["loads_lb_ft2"]
    sigma = fuelbed["sav_ft"]
    f_ij = fuelbed["f_ij"]
    f_dead = fuelbed["f_dead"]
    f_live = fuelbed["f_live"]
    mx_dead = fuelbed["mx_dead"]

    exp_dead_mx = np.zeros_like(sigma[..., DEAD])
    exp_live_mx = np.zeros_like(sigma[..., LIVE])
    dead_sigma_ok = sigma[..., DEAD] > 0.0
    live_sigma_ok = sigma[..., LIVE] > 0.0
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        exp_dead_mx[dead_sigma_ok] = np.exp(-138.0 / sigma[..., DEAD][dead_sigma_ok])
        exp_live_mx[live_sigma_ok] = np.exp(-500.0 / sigma[..., LIVE][live_sigma_ok])
    loading_dead = loads[..., DEAD] * exp_dead_mx
    loading_live = loads[..., LIVE] * exp_live_mx
    dead_loading = np.sum(loading_dead, axis=-1)
    live_loading = np.sum(loading_live, axis=-1)
    dead_moist = _safe_div(np.sum(m[..., DEAD] * loading_dead, axis=-1), dead_loading)
    dead_live_ratio = _safe_div(dead_loading, live_loading)
    mx_live_calc = 2.9 * dead_live_ratio * (1.0 - _safe_div(dead_moist, mx_dead)) - 0.226
    mx_live = np.where(live_loading > 0.0, np.maximum(mx_dead, mx_live_calc), mx_dead)

    mf_dead = np.sum(f_ij[..., DEAD] * m[..., DEAD], axis=-1)
    mf_live = np.sum(f_ij[..., LIVE] * m[..., LIVE], axis=-1)
    eta_m_dead = _moisture_damping(mf_dead, mx_dead)
    eta_m_live = _moisture_damping(mf_live, mx_live)

    eta_s = 0.174 * math.pow(EFFECTIVE_MINERAL, -0.19)
    wn_dead = np.sum(fuelbed["g_ij"][..., DEAD] * loads[..., DEAD] *
                     (1.0 - TOTAL_MINERAL), axis=-1)
    wn_live = np.sum(fuelbed["g_ij"][..., LIVE] * loads[..., LIVE] *
                     (1.0 - TOTAL_MINERAL), axis=-1)
    heat = fuelbed["heat_btu_lb"]
    heat_area = (wn_dead * heat * eta_m_dead * eta_s +
                 wn_live * heat * eta_m_live * eta_s)

    sigma_prime = fuelbed["sigma_prime_ft"]
    beta = fuelbed["beta"]
    beta_op = fuelbed["beta_op"]
    rel = _safe_div(beta, beta_op)
    a = np.zeros_like(sigma_prime)
    sigma_mask = sigma_prime > 0.0
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        a[sigma_mask] = 133.0 * np.power(sigma_prime[sigma_mask], -0.7913)
    gamma_max = np.where(sigma_prime > 0.0,
                         np.power(sigma_prime, 1.5) /
                         (495.0 + 0.0594 * np.power(sigma_prime, 1.5)), 0.0)
    gamma = np.zeros(shape, dtype=float)
    mask = (rel > 0.0) & (sigma_prime > 0.0)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        gamma[mask] = gamma_max[mask] * np.power(rel[mask], a[mask]) * np.exp(a[mask] * (1.0 - rel[mask]))

    i_r_btu = heat_area * gamma
    xi = np.where(sigma_prime > 0.0,
                  np.exp((0.792 + 0.681 * np.sqrt(sigma_prime)) *
                         (beta + 0.1)) / (192.0 + 0.2595 * sigma_prime),
                  0.0)

    epsilon = np.zeros_like(sigma)
    sigma_ok = sigma > 0.0
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        epsilon[sigma_ok] = np.exp(-138.0 / sigma[sigma_ok])
    q_ig = 250.0 + 1116.0 * m
    eqig_dead = np.sum(f_ij[..., DEAD] * epsilon[..., DEAD] * q_ig[..., DEAD], axis=-1)
    eqig_live = np.sum(f_ij[..., LIVE] * epsilon[..., LIVE] * q_ig[..., LIVE], axis=-1)
    heat_sink = fuelbed["rho_b_lb_ft3"] * (f_dead * eqig_dead + f_live * eqig_live)
    r0_ft_min = _safe_div(i_r_btu * xi, heat_sink)

    dead_too_wet = mf_dead >= mx_dead
    ok = fuelbed["burnable"] & (heat_sink > 0.0) & (i_r_btu > 0.0)
    ok = ok & ~dead_too_wet & np.isfinite(r0_ft_min)
    return (np.where(ok, r0_ft_min, 0.0),
            np.where(ok, i_r_btu, 0.0),
            np.where(ok, i_r_btu * BTU_FT2_MIN_TO_KW_M2, 0.0))


def rothermel_ros(fuelbed, moisture, phi_w, phi_s):
    """Head-fire surface rate of spread in m/min plus reaction intensity.

    Implements Rothermel's
    ``ROS = I_R * xi * (1 + Phi_w + Phi_s) / (rho_b * epsilon * Q_ig)``.
    The returned reaction intensity is SI ``kW/m^2``.  Nonburnable cells,
    zero-load cells, NaNs, and cells whose characteristic dead moisture is at
    or above the dead moisture of extinction return zero ROS and zero I_R.

    Andrews' effective-wind-speed limit is enforced here with the current
    moisture-dependent reaction intensity: U_eff,max = 0.9 * I_R in ft/min
    (converted to m/min), then inverted through the Rothermel wind function.
    """
    shape = np.shape(fuelbed["depth"])
    r0_ft_min, i_r_btu, i_r_kw_m2 = _reaction_and_base_ros(fuelbed, moisture)

    phi_total = np.broadcast_to(np.asarray(phi_w, dtype=float), shape)
    phi_total = phi_total + np.broadcast_to(np.asarray(phi_s, dtype=float), shape)
    phi_total = np.maximum(0.0, np.nan_to_num(phi_total, nan=0.0, posinf=0.0, neginf=0.0))

    eff_wind = np.zeros(shape, dtype=float)
    mask = phi_total > 0.0
    with np.errstate(over="ignore", invalid="ignore"):
        eff_wind[mask] = fuelbed["ws_scalr"][mask] * np.power(phi_total[mask], fuelbed["ws_expnt"][mask])
    max_eff = 0.9 * i_r_btu * FT_TO_M
    over = (eff_wind > max_eff) & (max_eff > 0.0)
    with np.errstate(over="ignore", invalid="ignore"):
        phi_limit = fuelbed["phi_w_scalr"] * np.power(max_eff, fuelbed["phi_w_expnt"])
    phi_total = np.where(over, phi_limit, phi_total)

    ros = r0_ft_min * FT_TO_M * (1.0 + phi_total)
    ros = np.where(fuelbed["burnable"] & np.isfinite(ros), ros, 0.0)
    return ros, np.where(ros > 0.0, i_r_kw_m2, 0.0)


def byram_intensity(ros_m_min, fuel_consumed_kg_m2, heat_kJ_kg):
    """Byram fireline intensity, ``I = H * w * R``, in kW/m.

    ``ros_m_min`` is converted to m/s.  ``fuel_consumed_kg_m2`` should be the
    flaming-front fuel consumed, not necessarily the full fuelbed loading.
    """
    ros = np.maximum(0.0, np.asarray(ros_m_min, dtype=float)) / 60.0
    consumed = np.maximum(0.0, np.asarray(fuel_consumed_kg_m2, dtype=float))
    heat = np.maximum(0.0, np.asarray(heat_kJ_kg, dtype=float))
    out = heat * consumed * ros
    return np.where(np.isfinite(out), out, 0.0)


def flame_length(intensity_kW_m, crown_mask=None):
    """Flame length in m from Byram surface-fire intensity.

    Surface fire uses Byram's ``L = 0.0775 * I^0.46``.  If a crown-fire caller
    passes ``crown_mask``, those cells use Thomas'
    ``L = 0.0266 * I^0.667`` relation.
    """
    intensity = np.maximum(0.0, np.asarray(intensity_kW_m, dtype=float))
    surface = 0.0775 * np.power(intensity, 0.46)
    if crown_mask is None:
        return np.where(np.isfinite(surface), surface, 0.0)
    crown = 0.0266 * np.power(intensity, 0.667)
    mask = np.asarray(crown_mask, dtype=bool)
    out = np.where(mask, crown, surface)
    return np.where(np.isfinite(out), out, 0.0)


def _anderson_fm10_params():
    """Original Anderson fuel model 10, used by Rothermel crown ROS."""
    return {
        "code": 10,
        "short_name": "FM10",
        "group": "timber-litter",
        "burnable": True,
        "dynamic": False,
        "load_dead_1h": 3.01 * T_AC_TO_KG_M2,
        "load_dead_10h": 2.00 * T_AC_TO_KG_M2,
        "load_dead_100h": 5.01 * T_AC_TO_KG_M2,
        "load_live_herb": 0.0,
        "load_live_woody": 2.00 * T_AC_TO_KG_M2,
        "sav_dead_1h": 2000.0 * M_TO_FT,
        "sav_dead_10h": 109.0 * M_TO_FT,
        "sav_dead_100h": 30.0 * M_TO_FT,
        "sav_live_herb": 0.0,
        "sav_live_woody": 1500.0 * M_TO_FT,
        "depth": 1.0 * FT_TO_M,
        "moisture_extinction_dead": 0.25,
        "heat": 8000.0 * BTU_LB_TO_KJ_KG,
    }


def _anderson_fm10_fuelbed(shape):
    return fuel_bed(np.full(shape, 10, dtype=int), {10: _anderson_fm10_params()}, 0.0)


def active_crown_ros(open_wind_mph, slope_rad, moisture):
    """Rothermel/Scott-Reinhardt active crown ROS estimate, in m/min.

    Active crown spread follows Rothermel's 1991 correlation used by
    Scott-Reinhardt: 3.34 times the Rothermel surface ROS for original fuel
    model 10 with a fixed 0.40 open-wind-to-midflame wind adjustment factor.
    Slope and fuel moisture remain scenario/local inputs.
    """
    open_wind = np.asarray(open_wind_mph, dtype=float)
    slope = np.asarray(slope_rad, dtype=float)
    shape = np.broadcast(open_wind, slope).shape
    bed = _anderson_fm10_fuelbed(shape)
    open_m_min = np.broadcast_to(open_wind, shape) * MPH_TO_M_MIN
    u = np.maximum(0.0, np.nan_to_num(open_m_min * 0.40,
                                      nan=0.0, posinf=0.0, neginf=0.0))
    with np.errstate(over="ignore", invalid="ignore"):
        phi_w = bed["phi_w_scalr"] * np.power(u, bed["phi_w_expnt"])
    phi_w = np.where(np.isfinite(phi_w), phi_w, 0.0)

    slope_b = np.broadcast_to(slope, shape)
    slope_ratio = np.tan(np.nan_to_num(slope_b, nan=0.0, posinf=0.0, neginf=0.0))
    phi_s = bed["phi_s_g"] * np.maximum(0.0, slope_ratio) ** 2
    phi_s = np.where(np.isfinite(phi_s), phi_s, 0.0)

    ros, _reaction = rothermel_ros(bed, moisture, phi_w, phi_s)
    return np.where(np.isfinite(ros), 3.34 * ros, 0.0)


def crown_class(surf_intensity_kw_m, active_crown_ros_m_min,
                cbh_m, cbd_kg_m3, fmc_pct):
    """Scott & Reinhardt crown-fire class: 0 surface, 1 passive, 2 active.

    Van Wagner crown initiation uses
    ``I0 = (0.010 * CBH * (460 + 25.9 * FMC)) ** 1.5`` in kW/m, with CBH in
    meters and FMC in percent.  Active crowning uses the critical spread rate
    ``R_active = 3.0 / CBD`` in m/min.  The supplied
    ``active_crown_ros_m_min`` must be the Scott-Reinhardt/Rothermel active
    crown ROS estimate, not the cell's ordinary surface ROS.

    Validity gate: cells with ``CBD <= 0`` or ``CBH <= 0`` are non-forested /
    no applicable conifer canopy and are forced to class 0.
    """
    intensity, crown_ros, cbh, cbd, fmc = np.broadcast_arrays(
        np.asarray(surf_intensity_kw_m, dtype=float),
        np.asarray(active_crown_ros_m_min, dtype=float),
        np.asarray(cbh_m, dtype=float),
        np.asarray(cbd_kg_m3, dtype=float),
        np.asarray(fmc_pct, dtype=float),
    )
    out = np.zeros(intensity.shape, dtype=np.int8)
    valid = ((cbh > 0.0) & (cbd > 0.0) & np.isfinite(cbh) &
             np.isfinite(cbd) & np.isfinite(fmc))
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        i0 = np.power(0.010 * cbh * (460.0 + 25.9 * fmc), 1.5)
        r_active = 3.0 / cbd
    torching = valid & np.isfinite(intensity) & np.isfinite(i0) & (intensity >= i0)
    active = (torching & np.isfinite(crown_ros) & np.isfinite(r_active) &
              (crown_ros >= r_active))
    out = np.where(torching, 1, out)
    out = np.where(active, 2, out)
    return out


def _surface_for_open_wind(fuelbed, open_wind_mph, slope_rad, moisture):
    """Surface ROS/intensity for a 20-ft open wind speed in mph.

    If ``fuelbed`` carries ``canopy_cover`` as a percent/fraction array, the
    standard midflame WAF from ``midflame_wind()`` is used.  Otherwise the
    unsheltered 20-ft open-wind WAF of 0.40 applies.
    """
    shape = np.shape(fuelbed["depth"])
    open_m_min = np.asarray(open_wind_mph, dtype=float) * MPH_TO_M_MIN
    if "canopy_cover" in fuelbed:
        midflame = midflame_wind(open_m_min, fuelbed["canopy_cover"])
    else:
        midflame = np.broadcast_to(open_m_min, shape) * 0.40

    u = np.maximum(0.0, np.nan_to_num(midflame, nan=0.0, posinf=0.0, neginf=0.0))
    with np.errstate(over="ignore", invalid="ignore"):
        phi_w = fuelbed["phi_w_scalr"] * np.power(u, fuelbed["phi_w_expnt"])
    phi_w = np.where(np.isfinite(phi_w), phi_w, 0.0)

    slope = np.broadcast_to(np.asarray(slope_rad, dtype=float), shape)
    slope_ratio = np.tan(np.nan_to_num(slope, nan=0.0, posinf=0.0, neginf=0.0))
    slope_ratio = np.maximum(0.0, slope_ratio)
    phi_s = fuelbed["phi_s_g"] * slope_ratio * slope_ratio
    phi_s = np.where(np.isfinite(phi_s), phi_s, 0.0)

    ros, reaction = rothermel_ros(fuelbed, moisture, phi_w, phi_s)
    consumed = _effective_consumed_for_fireline(fuelbed, reaction)
    intensity = byram_intensity(ros, consumed, fuelbed["heat"])
    return ros, intensity


def _wind_threshold_mph(fuelbed, slope_rad, moisture, target, metric,
                        applicable, max_open_mph=TI_CI_MAX_OPEN_WIND_MPH):
    """Bisection inversion for a monotonic ROS/intensity threshold."""
    shape = np.shape(fuelbed["depth"])
    def values(open_mph):
        ros, intensity = _surface_for_open_wind(fuelbed, open_mph, slope_rad, moisture)
        return intensity if metric == "intensity" else ros
    return _wind_threshold_from_values(
        shape, values, target, applicable & fuelbed["burnable"],
        max_open_mph=max_open_mph)


def _wind_threshold_from_values(shape, values, target, applicable,
                                max_open_mph=TI_CI_MAX_OPEN_WIND_MPH):
    """Bisection inversion for any monotonic 20-ft-open-wind response."""
    target = np.broadcast_to(np.asarray(target, dtype=float), shape)
    applicable = np.broadcast_to(np.asarray(applicable, dtype=bool), shape)
    valid = applicable & np.isfinite(target) & (target > 0.0)

    lo_val = values(0.0)
    hi_val = values(max_open_mph)
    out = np.full(shape, np.nan, dtype=float)
    reached_at_zero = valid & np.isfinite(lo_val) & (lo_val >= target)
    out[reached_at_zero] = 0.0

    bracketed = (valid & ~reached_at_zero & np.isfinite(hi_val) &
                 (hi_val >= target))
    out[valid & ~reached_at_zero & ~bracketed] = np.inf
    if not bracketed.any():
        return out

    lo = np.zeros(shape, dtype=float)
    hi = np.full(shape, float(max_open_mph), dtype=float)
    for _ in range(34):
        mid = (lo + hi) * 0.5
        mid_val = values(mid)
        ge = np.isfinite(mid_val) & (mid_val >= target)
        hi = np.where(bracketed & ge, mid, hi)
        lo = np.where(bracketed & ~ge, mid, lo)
    out[bracketed] = hi[bracketed]
    return out


def torching_crowning_index(fuelbed, cbh_m, cbd_kg_m3, fmc_pct, slope_rad, moisture):
    """Return ``(TI, CI)`` as 20-ft open-wind speeds in miles per hour.

    TI is the wind speed where surface fireline intensity reaches Van Wagner's
    crown-initiation threshold.  CI is the wind speed where active crowning can
    begin, meaning both initiation and ``ROS >= 3 / CBD`` are satisfied.  The
    bisection searches 0..120 mph; forested burnable cells whose threshold is
    not reached in that range return ``inf``.  Cells with ``CBD <= 0`` or
    ``CBH <= 0`` return ``NaN`` because the crown module is not applicable.
    """
    shape = np.shape(fuelbed["depth"])
    cbh, cbd, fmc = np.broadcast_arrays(
        np.broadcast_to(np.asarray(cbh_m, dtype=float), shape),
        np.broadcast_to(np.asarray(cbd_kg_m3, dtype=float), shape),
        np.broadcast_to(np.asarray(fmc_pct, dtype=float), shape),
    )
    applicable = ((cbh > 0.0) & (cbd > 0.0) & np.isfinite(cbh) &
                  np.isfinite(cbd) & np.isfinite(fmc))
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        i0 = np.power(0.010 * cbh * (460.0 + 25.9 * fmc), 1.5)
        r_active = 3.0 / cbd

    ti = _wind_threshold_mph(fuelbed, slope_rad, moisture, i0, "intensity", applicable)
    ros_ci = _wind_threshold_from_values(
        shape,
        lambda open_mph: active_crown_ros(open_mph, slope_rad, moisture),
        r_active,
        applicable,
    )
    ci = np.where(applicable, np.maximum(ti, ros_ci), np.nan)
    ci = np.where(np.isinf(ti) | np.isinf(ros_ci), np.inf, ci)
    return ti, ci


def ellipse_lw(eff_wind_mph):
    """Anderson/Finney fire-ellipse length-to-breadth ratio, capped at 8.

    ``eff_wind_mph`` is the effective wind speed in miles per hour.  Zero wind
    returns 1.0, making the ellipse a circle.
    """
    u = np.maximum(0.0, np.nan_to_num(np.asarray(eff_wind_mph, dtype=float),
                                      nan=0.0, posinf=0.0, neginf=0.0))
    with np.errstate(over="ignore", invalid="ignore"):
        lw = (0.936 * np.exp(0.2566 * u) +
              0.461 * np.exp(-0.1548 * u) - 0.397)
    lw = np.where(np.isfinite(lw), lw, 8.0)
    return np.minimum(8.0, np.maximum(1.0, lw))


def _ignition_mask(ignition_cells, shape):
    if ignition_cells is None:
        return np.zeros(shape, dtype=bool)

    arr = np.asarray(ignition_cells)
    if arr.dtype == bool:
        if arr.shape != shape:
            raise ValueError("boolean ignition_cells mask must match ros_field shape")
        return arr.copy()

    mask = np.zeros(shape, dtype=bool)
    if arr.size == 0:
        return mask
    if arr.ndim == 1:
        if arr.size != 2:
            raise ValueError("ignition_cells must be a mask or (row, col) pairs")
        pairs = arr.reshape(1, 2)
    else:
        if arr.shape[-1] != 2:
            raise ValueError("ignition_cells must be a mask or (row, col) pairs")
        pairs = arr.reshape(-1, 2)

    h, w = shape
    for r, c in pairs:
        ri = int(r)
        ci = int(c)
        if ri < 0 or ci < 0 or ri >= h or ci >= w:
            raise ValueError("ignition cell out of bounds: (%d, %d)" % (ri, ci))
        mask[ri, ci] = True
    return mask


def arrival_time(ros_field, eff_wind, max_dir, ignition_cells, cellsize):
    """Anisotropic minimum-travel-time fire arrival, in minutes.

    ``ros_field`` is each cell's head-fire ROS in m/min.  ``eff_wind`` is the
    effective wind speed in m/min and is converted to mph for ``ellipse_lw``.
    ``max_dir`` is the azimuth of maximum spread, radians clockwise from north.
    The returned array uses ``np.inf`` for unreachable and nonburnable cells
    except ignitions, which are set to 0 even if they are on a barrier.
    """
    ros = np.asarray(ros_field, dtype=float)
    if ros.ndim != 2:
        raise ValueError("ros_field must be a 2D array")
    h, w = ros.shape

    cs = float(np.asarray(cellsize, dtype=float))
    if not np.isfinite(cs) or cs <= 0.0:
        raise ValueError("cellsize must be a positive scalar")

    eff = np.broadcast_to(np.asarray(eff_wind, dtype=float), ros.shape)
    direction = np.broadcast_to(np.asarray(max_dir, dtype=float), ros.shape)
    head_ros = np.where(np.isfinite(ros) & (ros > 0.0), ros, 0.0)
    burnable = head_ros > 0.0

    lw = ellipse_lw(np.maximum(0.0, np.nan_to_num(eff, nan=0.0,
                                                  posinf=0.0, neginf=0.0)) /
                    MPH_TO_M_MIN)
    with np.errstate(invalid="ignore", divide="ignore"):
        ecc = np.sqrt(np.maximum(0.0, 1.0 - 1.0 / (lw * lw)))
    ecc = np.where(burnable & np.isfinite(ecc), ecc, 0.0)
    direction = np.mod(np.nan_to_num(direction, nan=0.0,
                                     posinf=0.0, neginf=0.0), 2.0 * math.pi)

    bearings = np.array([math.atan2(dc, -dr) for dr, dc in _NB], dtype=float)
    distances = np.array([
        math.hypot(float(dc), float(dr)) * cs for dr, dc in _NB
    ], dtype=float)

    dir_ros = []
    numerator = head_ros * (1.0 - ecc)
    for bearing in bearings:
        denom = 1.0 - ecc * np.cos(bearing - direction)
        speed = np.divide(numerator, denom,
                          out=np.zeros_like(head_ros),
                          where=denom > ALMOST_ZERO)
        speed = np.where(burnable & np.isfinite(speed) & (speed > 0.0),
                         speed, 0.0)
        dir_ros.append(speed.ravel())

    ign = _ignition_mask(ignition_cells, ros.shape)
    t = np.full(ros.shape, np.inf, dtype=float)
    settled = np.zeros(ros.shape, dtype=bool)
    t[ign] = 0.0

    t_flat = t.ravel()
    settled_flat = settled.ravel()
    burnable_flat = burnable.ravel()
    heap = [(0.0, int(idx)) for idx in np.flatnonzero(ign)]
    heapq.heapify(heap)

    while heap:
        cur_t, idx = heapq.heappop(heap)
        if settled_flat[idx]:
            continue
        if cur_t != t_flat[idx]:
            continue
        settled_flat[idx] = True
        if not burnable_flat[idx]:
            continue

        r = idx // w
        c = idx - r * w
        for k, (dr, dc) in enumerate(_NB):
            nr = r + dr
            nc = c + dc
            if nr < 0 or nc < 0 or nr >= h or nc >= w:
                continue
            nidx = nr * w + nc
            if settled_flat[nidx] or not burnable_flat[nidx]:
                continue
            speed = dir_ros[k][idx]
            if speed <= 0.0:
                continue
            nt = cur_t + distances[k] / speed
            if nt < t_flat[nidx]:
                t_flat[nidx] = nt
                heapq.heappush(heap, (nt, nidx))

    return t


def _canopy_field(canopy, names, default=None):
    for name in names:
        if name in canopy:
            return canopy[name]
    if default is not None:
        return default
    raise KeyError("canopy is missing %s" % names[0])


def compute_static(grid, fuelbed, canopy, moisture_scenario):
    """Static Tier-1 wildfire fields for the terrain grid.

    ``base_ros`` is no-wind/no-slope surface ROS.  ``slope_hazard`` is no-wind
    ROS at each cell's slope magnitude.  ``crown_potential`` uses a reference
    20 mph 20-ft open wind, converted to midflame wind from canopy cover, and
    combines it with the local slope magnitude as an uphill-aligned potential.
    This is a screening layer, not an event forecast.
    """
    shape = np.shape(fuelbed["depth"])
    slope = slope_radians(grid["dem"], grid["cellsize"])
    footprint = np.isfinite(grid["dem"])

    zeros = np.zeros(shape, dtype=float)
    base_ros, base_reaction = rothermel_ros(fuelbed, moisture_scenario, zeros, zeros)
    del base_reaction

    slope_ratio = np.tan(np.nan_to_num(slope, nan=0.0, posinf=0.0, neginf=0.0))
    slope_phi = fuelbed["phi_s_g"] * np.maximum(0.0, slope_ratio) ** 2
    slope_phi = np.where(np.isfinite(slope_phi), slope_phi, 0.0)
    slope_ros, _slope_reaction = rothermel_ros(fuelbed, moisture_scenario, zeros, slope_phi)

    cc = _canopy_field(canopy, ("cc_pct", "cc", "canopy_cover"), np.zeros(shape))
    cbh = _canopy_field(canopy, ("cbh_m", "cbh"))
    cbd = _canopy_field(canopy, ("cbd_kg_m3", "cbd"))
    fmc = _canopy_field(canopy, ("fmc_pct", "fmc"))

    bed_with_canopy = dict(fuelbed)
    bed_with_canopy["canopy_cover"] = cc
    ref_ros, ref_intensity = _surface_for_open_wind(
        bed_with_canopy,
        STATIC_REFERENCE_OPEN_WIND_MPH,
        slope,
        moisture_scenario,
    )
    del ref_ros
    ref_crown_ros = active_crown_ros(
        STATIC_REFERENCE_OPEN_WIND_MPH,
        slope,
        moisture_scenario,
    )
    crown = crown_class(ref_intensity, ref_crown_ros, cbh, cbd, fmc)
    ti, ci = torching_crowning_index(bed_with_canopy, cbh, cbd, fmc, slope, moisture_scenario)

    base_ros = np.where(footprint, base_ros, np.nan)
    slope_ros = np.where(footprint, slope_ros, np.nan)
    crown = np.where(footprint, crown, 0).astype(np.int8)
    ti = np.where(footprint, ti, np.nan)
    ci = np.where(footprint, ci, np.nan)

    return {
        "base_ros": base_ros,
        "slope_hazard": slope_ros,
        "crown_potential": crown,
        "TI": ti,
        "CI": ci,
        "cell_area_m2": grid["cellsize"] * grid["cellsize"],
        "reference_wind_20ft_open_mph": STATIC_REFERENCE_OPEN_WIND_MPH,
        "reference_wind_note": (
            "Crown potential uses a 20 mph 20-ft open wind, canopy-cover WAF, "
            "uphill-aligned slope magnitude, and Scott-Reinhardt active crown "
            "ROS from original FM10 at 0.40 WAF."
        ),
    }


def _effective_consumed_for_fireline(fuelbed, reaction_intensity_kW_m2):
    """Effective flaming-front consumption that makes Byram equal Rothermel."""
    return _safe_div(60.0 * reaction_intensity_kW_m2 *
                     fuelbed["residence_time_min"], fuelbed["heat"])


def _smoke_param_table():
    tc = 0.224170231
    sf = 3.280839895
    df = 0.3048
    hf = 2.3259999996

    def model(code, d1, d10, d100, lh, lw, dyn, sd1, slh, slw, depth, mx, heat):
        return {
            "code": code,
            "burnable": True,
            "dynamic": dyn,
            "load_dead_1h": d1 * tc,
            "load_dead_10h": d10 * tc,
            "load_dead_100h": d100 * tc,
            "load_live_herb": lh * tc,
            "load_live_woody": lw * tc,
            "sav_dead_1h": sd1 * sf if d1 > 0 else 0.0,
            "sav_dead_10h": 109.0 * sf if d10 > 0 else 0.0,
            "sav_dead_100h": 30.0 * sf if d100 > 0 else 0.0,
            "sav_live_herb": slh * sf if lh > 0 else 0.0,
            "sav_live_woody": slw * sf if lw > 0 else 0.0,
            "depth": depth * df,
            "moisture_extinction_dead": mx / 100.0,
            "heat": heat * hf,
        }

    return {
        102: model(102, 0.10, 0.00, 0.00, 1.00, 0.00, True, 2000, 1800, 9999, 1.0, 15, 8000),
        145: model(145, 3.60, 2.10, 0.00, 0.00, 2.90, False, 750, 9999, 1600, 6.0, 15, 8000),
        165: model(165, 4.00, 4.00, 3.00, 0.00, 3.00, False, 1500, 9999, 750, 1.0, 25, 8000),
        185: model(185, 1.15, 2.50, 4.40, 0.00, 0.00, False, 2000, 9999, 1600, 0.6, 25, 8000),
    }


def _mph_to_m_min(mph):
    return mph * MPH_TO_M_MIN


if __name__ == "__main__":
    import time

    # Reference values were generated from Pyretechnics 2026.5.15's
    # Behave/Rothermel-compatible implementation, cross-checked against
    # firebehavioR 0.1.2 source equations and RMRS-GTR-153 D2L2 moisture:
    # dead 1/10/100 h = 6/7/8 percent, live herb/woody = 60/90 percent.
    table = _smoke_param_table()
    moisture = {
        "dead_1h": 0.06,
        "dead_10h": 0.07,
        "dead_100h": 0.08,
        "live_herb": 0.60,
        "live_woody": 0.90,
    }
    cases = [
        ("GR2 flat 5 mph", 102, 5.0, 0.0, 0.0, 0.0, 11.6582, 534.9295, 1.3936),
        ("SH5 flat 5 mph", 145, 5.0, 0.0, 0.0, 0.0, 19.2066, 5734.5880, 4.1500),
        ("TL5 flat 3 mph", 185, 3.0, 0.0, 0.0, 0.0, 0.8110, 53.4230, 0.4829),
        ("TU5 flat 5 mph", 165, 5.0, 0.0, 0.0, 0.0, 3.1063, 1509.2639, 2.2458),
        ("TU5 20deg uphill", 165, 5.0, 20.0, math.pi, 0.0, 4.0207, 1953.5448, 2.5288),
    ]
    tol = 0.10
    print("case                 ROS exp/act   I exp/act      L exp/act    status")
    failures = []
    for name, code, wind_mph, slope_deg, aspect, wind_dir, exp_ros, exp_i, exp_l in cases:
        bed = fuel_bed(np.array(code), table, moisture["live_herb"])
        phi_w, phi_s, _eff, _direction = wind_slope_factors(
            bed,
            _mph_to_m_min(wind_mph),
            math.radians(slope_deg),
            aspect,
            wind_dir,
        )
        ros, reaction = rothermel_ros(bed, moisture, phi_w, phi_s)
        consumed = _effective_consumed_for_fireline(bed, reaction)
        intensity = byram_intensity(ros, consumed, bed["heat"])
        flame = flame_length(intensity)
        ros_v = float(np.asarray(ros))
        i_v = float(np.asarray(intensity))
        l_v = float(np.asarray(flame))
        rels = [
            abs(ros_v - exp_ros) / max(exp_ros, 1e-9),
            abs(i_v - exp_i) / max(exp_i, 1e-9),
            abs(l_v - exp_l) / max(exp_l, 1e-9),
        ]
        ok = all(r <= tol for r in rels)
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures.append(name)
        print("%-20s %6.3f/%6.3f %8.1f/%8.1f %5.2f/%5.2f  %s" % (
            name, exp_ros, ros_v, exp_i, i_v, exp_l, l_v, status))
    if failures:
        raise SystemExit("wildfire smoke failed: " + ", ".join(failures))

    print("\nMOISTURE")
    twin_lat = 43.280
    twin_lon_west = 74.062
    twin_elev = 320.0
    latn, d0 = _fbp_latn_d0(twin_lat, twin_lon_west, twin_elev)
    latn_v = float(np.asarray(latn))
    d0_v = int(np.asarray(d0))
    assert abs(latn_v - 45.3445) < 0.01
    assert d0_v == 141
    print("FBP spring dip: LAT_n=%.3f D0=%d" % (latn_v, d0_v))

    # Reference values from CFFDRS foliar_moisture_content.r / Forestry Canada
    # ST-X-3 D0/ND curve for LAT=43.280, LONG=74.062 W, ELV=320 m.
    fmc_refs = {
        60: 120.0,
        110: 103.4932,
        141: 85.0,
        172: 103.4932,
        196: 120.0,
        288: 120.0,
        300: 120.0,
    }
    for doy, expected in fmc_refs.items():
        actual = float(np.asarray(fbp_fmc(twin_lat, twin_lon_west, twin_elev, doy)))
        assert abs(actual - expected) < 1e-4
    assert float(np.asarray(fbp_fmc(twin_lat, twin_lon_west, twin_elev, 141))) == 85.0
    assert float(np.asarray(fbp_fmc(twin_lat, twin_lon_west, twin_elev, 60))) == 120.0
    assert float(np.asarray(fbp_fmc(twin_lat, twin_lon_west, twin_elev, 300))) == 120.0

    print("date       doy  ND   FMC%")
    for label, doy in [
            ("Apr 20", 110),
            ("May 21", 141),
            ("Jun 21", 172),
            ("Jul 15", 196),
            ("Oct 15", 288),
    ]:
        nd = abs(doy - d0_v)
        fmc = float(np.asarray(fbp_fmc(twin_lat, twin_lon_west, twin_elev, doy)))
        print("%-8s  %3d %3d  %5.1f" % (label, doy, nd, fmc))

    emc_t = np.array([70.0, 80.0, 60.0])
    emc_rh = np.array([20.0, 60.0, 90.0])
    emc_expected = np.array([4.39475, 10.42266, 20.75919])
    emc_actual = emc_simard(emc_t, emc_rh)
    assert np.allclose(emc_actual, emc_expected, atol=1e-5)
    print("Simard EMC checks:")
    for t, rh, expected, actual in zip(emc_t, emc_rh, emc_expected, emc_actual):
        print("  T=%2.0f F RH=%2.0f%% -> %.5f%% (ref %.5f%%)" %
              (t, rh, actual, expected))

    fresh = [float(np.asarray(v)) for v in dead_moisture(60.0, 45.0, 0.0)]
    assert fresh[0] > 0.34
    dry = [float(np.asarray(v)) for v in dead_moisture(80.0, 20.0, 30.0)]
    dry_emc = float(np.asarray(emc_simard(80.0, 20.0))) / 100.0
    assert abs(dry[0] - dry_emc) < 1e-5
    assert dry[0] < 0.06
    assert dry[0] <= dry[1] <= dry[2]
    print("dead moisture fresh rain 1/10/100h: %.3f %.3f %.3f" % tuple(fresh))
    print("dead moisture 30 dry days 1/10/100h: %.3f %.3f %.3f" % tuple(dry))

    dispatch_cases = [
        ("conifer", "temperate_na", "fbp_spring_dip"),
        ("grass", "*", "not_applicable"),
        ("shrub", "mediterranean", "lfmc_obs"),
        ("hardwood", "*", "const"),
    ]
    print("FMC selector checks:")
    for veg, region, expected in dispatch_cases:
        actual = select_fmc_method(veg, region)
        assert actual == expected
        print("  (%s, %s) -> %s" % (veg, region, actual))

    print("moisture smoke PASS")

    print("\nPROPAGATION")
    prop_failures = []

    def prop_line(name, ok, detail):
        status = "PASS" if ok else "FAIL"
        if not ok:
            prop_failures.append(name)
        print("%-12s %s  %s" % (name, status, detail))

    n = 121
    center = n // 2
    cell = 1.0
    head = 10.0
    radius_cells = 40
    uniform_ros = np.full((n, n), head, dtype=float)
    zero_wind = np.zeros_like(uniform_ros)
    north = np.zeros_like(uniform_ros)

    iso_t = arrival_time(uniform_ros, zero_wind, north, [(center, center)], cell)
    rows, cols = np.indices(uniform_ros.shape)
    dy = rows - center
    dx = cols - center
    ring = (dx * dx + dy * dy) == radius_cells * radius_cells
    ring_t = iso_t[ring]
    expected_ring_t = radius_cells * cell / head
    iso_anis = float(np.max(ring_t) / np.min(ring_t) - 1.0)
    iso_high_bias = float(np.max(ring_t) / expected_ring_t - 1.0)
    iso_ok = (ring_t.size > 0 and np.all(np.isfinite(ring_t)) and
              iso_anis < 0.05 and iso_high_bias <= 0.08)
    prop_line(
        "ISOTROPY",
        iso_ok,
        "R=%dm max/min-1=%.3f, max high bias vs R/ROS=%.3f (tol 0.05/0.08)" %
        (radius_cells, iso_anis, iso_high_bias),
    )

    target_lw = 3.0
    lo = 0.0
    hi = 20.0
    for _ in range(50):
        mid = (lo + hi) * 0.5
        if float(np.asarray(ellipse_lw(mid))) < target_lw:
            lo = mid
        else:
            hi = mid
    wind_mph = hi
    lw = float(np.asarray(ellipse_lw(wind_mph)))
    ecc = math.sqrt(max(0.0, 1.0 - 1.0 / (lw * lw)))
    expected_hb = (1.0 + ecc) / (1.0 - ecc)
    wind_field = np.full_like(uniform_ros, wind_mph * MPH_TO_M_MIN)
    east_dir = np.full_like(uniform_ros, math.pi / 2.0)
    aniso_t = arrival_time(uniform_ros, wind_field, east_dir,
                           [(center, center)], cell)
    east_t = float(aniso_t[center, center + radius_cells])
    west_t = float(aniso_t[center, center - radius_cells])
    actual_hb = west_t / east_t
    aniso_ok = (east_t < west_t and
                abs(actual_hb / expected_hb - 1.0) <= 0.10)
    prop_line(
        "ANISOTROPY",
        aniso_ok,
        "L/W=%.3f e=%.3f T_west/T_east=%.2f expected=%.2f (tol 10%%)" %
        (lw, ecc, actual_hb, expected_hb),
    )

    big_shape = (220, 289)
    big_ros = np.full(big_shape, head, dtype=float)
    big_zero = np.zeros(big_shape, dtype=float)
    start = time.perf_counter()
    big_t = arrival_time(big_ros, big_zero, big_zero,
                         [(big_shape[0] // 2, big_shape[1] // 2)], cell)
    elapsed = time.perf_counter() - start
    timing_ok = elapsed < 2.0 and np.isfinite(big_t[0, 0])
    prop_line("TIMING", timing_ok, "%.3fs on %dx%d grid (tol 2.0s)" %
              (elapsed, big_shape[0], big_shape[1]))

    if prop_failures:
        raise SystemExit("propagation smoke failed: " + ", ".join(prop_failures))
