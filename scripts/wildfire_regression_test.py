#!/usr/bin/env python3
"""Focused regressions for wildfire audit fixes."""

import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fire_scenario  # noqa: E402
import hydro_fire  # noqa: E402
import twin_fire  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("%s failed%s" % (name, ": " + detail if detail else ""))
    print("PASS", name)


def main():
    # Van Wagner initiation alone is passive; active requires the
    # Scott-Reinhardt crown ROS, not the surface ROS.
    cbh = np.array([[1.0]])
    cbd = np.array([[0.10]])
    fmc = np.array([[85.0]])
    intensity = np.array([[10000.0]])
    passive = twin_fire.crown_class(intensity, np.array([[10.0]]), cbh, cbd, fmc)
    active = twin_fire.crown_class(intensity, np.array([[40.0]]), cbh, cbd, fmc)
    check("crown class passive below Ractive", int(passive[0, 0]) == 1)
    check("crown class active uses crown ROS", int(active[0, 0]) == 2)

    moisture = {
        "dead_1h": 0.06,
        "dead_10h": 0.07,
        "dead_100h": 0.08,
        "live_herb": 0.60,
        "live_woody": 0.90,
    }
    crown_ros = twin_fire.active_crown_ros(25.0, np.array([[0.0]]), moisture)
    check("active crown ROS is positive", float(crown_ros[0, 0]) > 0.0)

    # The reported flank spread is the ellipse semi-minor spread rate, not the
    # polar radius at 90 degrees.
    target_lw = 3.0
    lo, hi = 0.0, 30.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if float(np.asarray(twin_fire.ellipse_lw(mid))) < target_lw:
            lo = mid
        else:
            hi = mid
    lw = float(np.asarray(twin_fire.ellipse_lw(hi)))
    ecc = math.sqrt(max(0.0, 1.0 - 1.0 / (lw * lw)))
    ros = np.array([[10.0]])
    eff = np.array([[hi * twin_fire.MPH_TO_M_MIN]])
    max_dir = np.array([[math.pi / 2.0]])
    reported = fire_scenario._ros_ellipse_at(0, 0, ros, eff, max_dir)
    expected_flank = 10.0 * math.sqrt((1.0 - ecc) / (1.0 + ecc))
    check("reported flank ROS", abs(reported["flank_m_min"] - expected_flank) < 1e-3)

    capped = fire_scenario._capped_effective_wind(
        np.array([[1000.0]]), np.array([[1.0]]))
    check("ellipse wind cap", float(capped[0, 0]) < 1000.0)

    shaded = twin_fire.dead_moisture(80.0, 30.0, 3.0, exposure="shaded")[0]
    open_ = twin_fire.dead_moisture(80.0, 30.0, 3.0, exposure="open")[0]
    check("exposure affects dead moisture", float(open_) < float(shaded))

    check("NWI OW token true", hydro_fire._is_nwi_open_water({"NWILABEL": "POW"}))
    check("NWI meadow not open water",
          not hydro_fire._is_nwi_open_water({"NWILABEL": "PEM1E MEADOW"}))

    print("wildfire regression PASS")


if __name__ == "__main__":
    main()
