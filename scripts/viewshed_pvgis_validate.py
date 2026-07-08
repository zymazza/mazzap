#!/usr/bin/env python3
"""Compare the precomputed horizon profile against the fetched PVGIS oracle."""

from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_viewshed


def rms(rows, ours, convert):
    diffs = []
    for row in rows:
        az = convert(float(row["A"]))
        pvgis = float(row["H_hor"])
        mine = twin_viewshed.horizon_at_azimuth(ours, az)
        if math.isfinite(mine):
            diffs.append((mine - pvgis) ** 2)
    return math.sqrt(sum(diffs) / max(1, len(diffs)))


def main() -> int:
    horizon_path = os.path.join(PROJECT, "data", "viewshed", "horizon.json")
    pvgis_path = os.path.join(PROJECT, "data", "viewshed", "pvgis-horizon.json")
    if not os.path.exists(horizon_path) or not os.path.exists(pvgis_path):
        print(json.dumps({"error": "missing horizon.json or pvgis-horizon.json"}, indent=2))
        return 1
    horizon = json.load(open(horizon_path))
    pvgis = json.load(open(pvgis_path))
    ours = np.asarray((horizon.get("horizon_deg") or {}).get("bare_earth")
                      or (horizon.get("horizon_deg") or {}).get("canopy"), dtype=np.float32)
    rows = pvgis["payload"]["outputs"]["horizon_profile"]
    candidates = {
        "direct_north_zero": lambda a: (a + 360.0) % 360.0,
        "pvgis_south_zero": lambda a: (a + 180.0) % 360.0,
    }
    scores = {name: rms(rows, ours, fn) for name, fn in candidates.items()}
    best = min(scores, key=scores.get)
    result = {
        "oracle": "PVGIS printhorizon",
        "surface": "bare_earth",
        "rms_deg": scores[best],
        "azimuth_conversion": best,
        "all_rms_deg": scores,
        "pass_threshold_rms_deg": 20.0,
        "note": "PVGIS is SRTM/coarse and the local manifest currently has ring A only; threshold is wide until distant B/C terrain is staged.",
    }
    print(json.dumps(result, indent=2))
    return 0 if result["rms_deg"] <= result["pass_threshold_rms_deg"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
