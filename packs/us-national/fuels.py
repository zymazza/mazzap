"""Scott & Burgan (2005) FBFM40 fuel-model parameters in SI units.

The source table is RMRS-GTR-153 table 7.  Published values are English:
loads in short tons/acre, SAV in 1/ft, depth in ft, and heat content in
BTU/lb.  The exposed ``FBFM40`` dictionary stores SI values:
kg/m^2, 1/m, m, moisture fractions, and kJ/kg.

This is national LANDFIRE fuel-model knowledge, not a regional pack default.
Scenario presets and regional foliar-moisture choices belong in the regional
pack; the engine consumes this table through ``fuel_params`` or
``params_for_grid``.
"""

import numpy as np

# English-to-SI conversions used for RMRS-GTR-153 table 7:
# 1 short ton/acre = 2000 lb / 4046.8564224 m^2 = 0.224170231 kg/m^2.
# 1 1/ft = 3.280839895 1/m; 1 ft = 0.3048 m.
# 1 BTU/lb = 2.3259999996 kJ/kg.
T_AC_TO_KG_M2 = 0.224170231
FT_INV_TO_M_INV = 3.280839895
FT_TO_M = 0.3048
BTU_LB_TO_KJ_KG = 2.3259999996

SAV_10HR_FT = 109.0
SAV_100HR_FT = 30.0

GROUPS = {
    "NB": "nonburnable",
    "GR": "grass",
    "GS": "grass-shrub",
    "SH": "shrub",
    "TU": "timber-understory",
    "TL": "timber-litter",
    "SB": "slash-blowdown",
}

_NAMES = {
    "GR1": "Short, Sparse Dry Climate Grass",
    "GR2": "Low Load, Dry Climate Grass",
    "GR3": "Low Load, Very Coarse, Humid Climate Grass",
    "GR4": "Moderate Load, Dry Climate Grass",
    "GR5": "Low Load, Humid Climate Grass",
    "GR6": "Moderate Load, Humid Climate Grass",
    "GR7": "High Load, Dry Climate Grass",
    "GR8": "High Load, Very Coarse, Humid Climate Grass",
    "GR9": "Very High Load, Humid Climate Grass",
    "GS1": "Low Load, Dry Climate Grass-Shrub",
    "GS2": "Moderate Load, Dry Climate Grass-Shrub",
    "GS3": "Moderate Load, Humid Climate Grass-Shrub",
    "GS4": "High Load, Humid Climate Grass-Shrub",
    "SH1": "Low Load Dry Climate Shrub",
    "SH2": "Moderate Load Dry Climate Shrub",
    "SH3": "Moderate Load, Humid Climate Shrub",
    "SH4": "Low Load, Humid Climate Timber-Shrub",
    "SH5": "High Load, Dry Climate Shrub",
    "SH6": "Low Load, Humid Climate Shrub",
    "SH7": "Very High Load, Dry Climate Shrub",
    "SH8": "High Load, Humid Climate Shrub",
    "SH9": "Very High Load, Humid Climate Shrub",
    "TU1": "Low Load Dry Climate Timber-Grass-Shrub",
    "TU2": "Moderate Load, Humid Climate Timber-Shrub",
    "TU3": "Moderate Load, Humid Climate Timber-Grass-Shrub",
    "TU4": "Dwarf Conifer With Understory",
    "TU5": "Very High Load, Dry Climate Timber-Shrub",
    "TL1": "Low Load Compact Conifer Litter",
    "TL2": "Low Load Broadleaf Litter",
    "TL3": "Moderate Load Conifer Litter",
    "TL4": "Small Downed Logs",
    "TL5": "High Load Conifer Litter",
    "TL6": "Moderate Load Broadleaf Litter",
    "TL7": "Large Downed Logs",
    "TL8": "Long-Needle Litter",
    "TL9": "Very High Load Broadleaf Litter",
    "SB1": "Low Load Activity Fuel",
    "SB2": "Moderate Load Activity Fuel or Low Load Blowdown",
    "SB3": "High Load Activity Fuel or Moderate Load Blowdown",
    "SB4": "High Load Blowdown",
}


def _nb(code, short_name, name):
    return {
        "code": code,
        "short_name": short_name,
        "name": name,
        "group": GROUPS["NB"],
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
        # Live extinction is computed by Rothermel/Albini from dead loading
        # and current moisture; 0.0 marks "not tabulated".
        "moisture_extinction_live": 0.0,
        "heat": 0.0,
    }


def _model(code, short_name, d1, d10, d100, lh, lw,
           dynamic, sav_d1, sav_lh, sav_lw, depth, mx_dead, heat_btu_lb):
    group_key = short_name[:2]
    return {
        "code": code,
        "short_name": short_name,
        "name": _NAMES[short_name],
        "group": GROUPS[group_key],
        "burnable": True,
        "dynamic": bool(dynamic),
        "load_dead_1h": d1 * T_AC_TO_KG_M2,
        "load_dead_10h": d10 * T_AC_TO_KG_M2,
        "load_dead_100h": d100 * T_AC_TO_KG_M2,
        "load_live_herb": lh * T_AC_TO_KG_M2,
        "load_live_woody": lw * T_AC_TO_KG_M2,
        "sav_dead_1h": sav_d1 * FT_INV_TO_M_INV if d1 > 0.0 else 0.0,
        "sav_dead_10h": SAV_10HR_FT * FT_INV_TO_M_INV if d10 > 0.0 else 0.0,
        "sav_dead_100h": SAV_100HR_FT * FT_INV_TO_M_INV if d100 > 0.0 else 0.0,
        "sav_live_herb": sav_lh * FT_INV_TO_M_INV if lh > 0.0 else 0.0,
        "sav_live_woody": sav_lw * FT_INV_TO_M_INV if lw > 0.0 else 0.0,
        "depth": depth * FT_TO_M,
        "moisture_extinction_dead": mx_dead / 100.0,
        "moisture_extinction_live": 0.0,
        "heat": heat_btu_lb * BTU_LB_TO_KJ_KG,
    }


FBFM40 = {
    91: _nb(91, "NB1", "Urban/Developed"),
    92: _nb(92, "NB2", "Snow/Ice"),
    93: _nb(93, "NB3", "Agricultural"),
    94: _nb(94, "NB4", "Nonburnable"),
    95: _nb(95, "NB5", "Nonburnable"),
    96: _nb(96, "NB6", "Nonburnable"),
    97: _nb(97, "NB7", "Nonburnable"),
    98: _nb(98, "NB8", "Open Water"),
    99: _nb(99, "NB9", "Bare Ground"),
    101: _model(101, "GR1", 0.10, 0.00, 0.00, 0.30, 0.00, True, 2200, 2000, 9999, 0.4, 15, 8000),
    102: _model(102, "GR2", 0.10, 0.00, 0.00, 1.00, 0.00, True, 2000, 1800, 9999, 1.0, 15, 8000),
    103: _model(103, "GR3", 0.10, 0.40, 0.00, 1.50, 0.00, True, 1500, 1300, 9999, 2.0, 30, 8000),
    104: _model(104, "GR4", 0.25, 0.00, 0.00, 1.90, 0.00, True, 2000, 1800, 9999, 2.0, 15, 8000),
    105: _model(105, "GR5", 0.40, 0.00, 0.00, 2.50, 0.00, True, 1800, 1600, 9999, 1.5, 40, 8000),
    106: _model(106, "GR6", 0.10, 0.00, 0.00, 3.40, 0.00, True, 2200, 2000, 9999, 1.5, 40, 9000),
    107: _model(107, "GR7", 1.00, 0.00, 0.00, 5.40, 0.00, True, 2000, 1800, 9999, 3.0, 15, 8000),
    108: _model(108, "GR8", 0.50, 1.00, 0.00, 7.30, 0.00, True, 1500, 1300, 9999, 4.0, 30, 8000),
    109: _model(109, "GR9", 1.00, 1.00, 0.00, 9.00, 0.00, True, 1800, 1600, 9999, 5.0, 40, 8000),
    121: _model(121, "GS1", 0.20, 0.00, 0.00, 0.50, 0.65, True, 2000, 1800, 1800, 0.9, 15, 8000),
    122: _model(122, "GS2", 0.50, 0.50, 0.00, 0.60, 1.00, True, 2000, 1800, 1800, 1.5, 15, 8000),
    123: _model(123, "GS3", 0.30, 0.25, 0.00, 1.45, 1.25, True, 1800, 1600, 1600, 1.8, 40, 8000),
    124: _model(124, "GS4", 1.90, 0.30, 0.10, 3.40, 7.10, True, 1800, 1600, 1600, 2.1, 40, 8000),
    141: _model(141, "SH1", 0.25, 0.25, 0.00, 0.15, 1.30, True, 2000, 1800, 1600, 1.0, 15, 8000),
    142: _model(142, "SH2", 1.35, 2.40, 0.75, 0.00, 3.85, False, 2000, 9999, 1600, 1.0, 15, 8000),
    143: _model(143, "SH3", 0.45, 3.00, 0.00, 0.00, 6.20, False, 1600, 9999, 1400, 2.4, 40, 8000),
    144: _model(144, "SH4", 0.85, 1.15, 0.20, 0.00, 2.55, False, 2000, 1800, 1600, 3.0, 30, 8000),
    145: _model(145, "SH5", 3.60, 2.10, 0.00, 0.00, 2.90, False, 750, 9999, 1600, 6.0, 15, 8000),
    146: _model(146, "SH6", 2.90, 1.45, 0.00, 0.00, 1.40, False, 750, 9999, 1600, 2.0, 30, 8000),
    147: _model(147, "SH7", 3.50, 5.30, 2.20, 0.00, 3.40, False, 750, 9999, 1600, 6.0, 15, 8000),
    148: _model(148, "SH8", 2.05, 3.40, 0.85, 0.00, 4.35, False, 750, 9999, 1600, 3.0, 40, 8000),
    149: _model(149, "SH9", 4.50, 2.45, 0.00, 1.55, 7.00, True, 750, 1800, 1500, 4.4, 40, 8000),
    161: _model(161, "TU1", 0.20, 0.90, 1.50, 0.20, 0.90, True, 2000, 1800, 1600, 0.6, 20, 8000),
    162: _model(162, "TU2", 0.95, 1.80, 1.25, 0.00, 0.20, False, 2000, 9999, 1600, 1.0, 30, 8000),
    163: _model(163, "TU3", 1.10, 0.15, 0.25, 0.65, 1.10, True, 1800, 1600, 1400, 1.3, 30, 8000),
    164: _model(164, "TU4", 4.50, 0.00, 0.00, 0.00, 2.00, False, 2300, 9999, 2000, 0.5, 12, 8000),
    165: _model(165, "TU5", 4.00, 4.00, 3.00, 0.00, 3.00, False, 1500, 9999, 750, 1.0, 25, 8000),
    181: _model(181, "TL1", 1.00, 2.20, 3.60, 0.00, 0.00, False, 2000, 9999, 9999, 0.2, 30, 8000),
    182: _model(182, "TL2", 1.40, 2.30, 2.20, 0.00, 0.00, False, 2000, 9999, 9999, 0.2, 25, 8000),
    183: _model(183, "TL3", 0.50, 2.20, 2.80, 0.00, 0.00, False, 2000, 9999, 9999, 0.3, 20, 8000),
    184: _model(184, "TL4", 0.50, 1.50, 4.20, 0.00, 0.00, False, 2000, 9999, 9999, 0.4, 25, 8000),
    185: _model(185, "TL5", 1.15, 2.50, 4.40, 0.00, 0.00, False, 2000, 9999, 1600, 0.6, 25, 8000),
    186: _model(186, "TL6", 2.40, 1.20, 1.20, 0.00, 0.00, False, 2000, 9999, 9999, 0.3, 25, 8000),
    187: _model(187, "TL7", 0.30, 1.40, 8.10, 0.00, 0.00, False, 2000, 9999, 9999, 0.4, 25, 8000),
    188: _model(188, "TL8", 5.80, 1.40, 1.10, 0.00, 0.00, False, 1800, 9999, 9999, 0.3, 35, 8000),
    189: _model(189, "TL9", 6.65, 3.30, 4.15, 0.00, 0.00, False, 1800, 9999, 1600, 0.6, 35, 8000),
    201: _model(201, "SB1", 1.50, 3.00, 11.00, 0.00, 0.00, False, 2000, 9999, 9999, 1.0, 25, 8000),
    202: _model(202, "SB2", 4.50, 4.25, 4.00, 0.00, 0.00, False, 2000, 9999, 9999, 1.0, 25, 8000),
    203: _model(203, "SB3", 5.50, 2.75, 3.00, 0.00, 0.00, False, 2000, 9999, 9999, 1.2, 25, 8000),
    204: _model(204, "SB4", 5.25, 3.50, 5.25, 0.00, 0.00, False, 2000, 9999, 9999, 2.7, 25, 8000),
}

_ARRAY_KEYS = [
    "load_dead_1h", "load_dead_10h", "load_dead_100h",
    "load_live_herb", "load_live_woody",
    "sav_dead_1h", "sav_dead_10h", "sav_dead_100h",
    "sav_live_herb", "sav_live_woody",
    "depth", "moisture_extinction_dead", "moisture_extinction_live", "heat",
]


def fuel_params(code):
    """Return the FBFM40 parameter dict for ``code``.

    LANDFIRE nonburnable codes 91-99 return a zero-load sentinel. Unknown
    codes are treated as nonburnable so downstream spread is zero rather than
    NaN or an exception.
    """
    c = int(code)
    if 91 <= c <= 99:
        return FBFM40.get(c, _nb(c, "NB%d" % (c - 90), "Nonburnable"))
    return FBFM40.get(c, _nb(c, "NB0", "Unknown or unsupported fuel model"))


def params_for_grid(code_grid, table=FBFM40):
    """Vectorize fuel parameters over a scalar or numpy code grid.

    Returns numeric arrays keyed by parameter name plus ``burnable``,
    ``dynamic``, ``code``, and ``short_name`` object arrays.  The engine can
    consume this helper directly, but it also accepts the table itself.
    """
    codes = np.asarray(code_grid)
    out = {
        "code": codes.astype(int, copy=True),
        "burnable": np.zeros(codes.shape, dtype=bool),
        "dynamic": np.zeros(codes.shape, dtype=bool),
        "short_name": np.empty(codes.shape, dtype=object),
        "group": np.empty(codes.shape, dtype=object),
    }
    for key in _ARRAY_KEYS:
        out[key] = np.zeros(codes.shape, dtype=float)

    out["short_name"][...] = "NB0"
    out["group"][...] = GROUPS["NB"]

    for raw_code in np.unique(codes.astype(int)):
        params = table.get(int(raw_code), fuel_params(int(raw_code)))
        mask = codes == raw_code
        out["burnable"] = np.where(mask, params["burnable"], out["burnable"])
        out["dynamic"] = np.where(mask, params["dynamic"], out["dynamic"])
        out["short_name"][mask] = params["short_name"]
        out["group"][mask] = params["group"]
        for key in _ARRAY_KEYS:
            out[key] = np.where(mask, params[key], out[key])
    return out
