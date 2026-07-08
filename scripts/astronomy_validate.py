#!/usr/bin/env python3
"""Offline validation of astronomy-engine outputs against JPL Horizons."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import twin_astro  # noqa: E402


DEFAULT_REFERENCE = Path(twin_astro.HORIZONS_REFERENCE)
THRESHOLDS_ARCMIN = {"moon": 2.0}
DEFAULT_THRESHOLD_ARCMIN = 1.0


def angular_error_arcmin(az1: float, alt1: float, az2: float, alt2: float) -> float:
    a1 = math.radians(az1)
    h1 = math.radians(alt1)
    a2 = math.radians(az2)
    h2 = math.radians(alt2)
    dot = math.sin(h1) * math.sin(h2) + math.cos(h1) * math.cos(h2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot)))) * 60.0


def validate(reference_path: Path) -> int:
    ref = json.load(open(reference_path))
    site = twin_astro.Site(
        lat=float(ref["site"]["lat"]),
        lon=float(ref["site"]["lon"]),
        height_m=float(ref["site"].get("height_m", 0.0)),
    )
    failed = False
    print("body       rows   max arcmin   mean arcmin   threshold")
    print("---------  -----  ----------   -----------   ---------")
    for body, rows in ref.get("bodies", {}).items():
        errors = []
        for row in rows:
            pos = twin_astro.body_position(body, row["iso"], site)
            errors.append(angular_error_arcmin(
                float(row["azimuth_deg"]),
                float(row["altitude_deg"]),
                float(pos["azimuth_deg"]),
                float(pos["altitude_deg"]),
            ))
        max_err = max(errors) if errors else float("nan")
        mean_err = sum(errors) / len(errors) if errors else float("nan")
        threshold = THRESHOLDS_ARCMIN.get(body, DEFAULT_THRESHOLD_ARCMIN)
        ok = max_err <= threshold
        failed = failed or not ok
        print(f"{body:<9}  {len(errors):>5}  {max_err:>10.4f}   {mean_err:>11.4f}   {threshold:>9.3f} {'OK' if ok else 'FAIL'}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", default=str(DEFAULT_REFERENCE), help="Horizons reference JSON")
    args = parser.parse_args()
    path = Path(args.reference)
    if not path.exists():
        raise SystemExit(f"reference file not found: {path}; run scripts/fetch_horizons_reference.py")
    raise SystemExit(validate(path))


if __name__ == "__main__":
    main()
