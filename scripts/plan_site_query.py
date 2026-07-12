#!/usr/bin/env python3
"""Run point/horizon queries inside one materialized Plan data directory."""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--kind", choices=["solar_site", "viewshed"], required=True)
    args = parser.parse_args()
    data_dir = os.path.abspath(args.data_dir)
    os.environ["TWIN_DATA_DIR"] = data_dir
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)

    import twin_store
    twin_store.DATA_DIR = data_dir
    twin_store.STORE_PATH = os.path.join(data_dir, "twin.gpkg")
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")
    import twin_query
    twin_query.DATA = data_dir

    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    tq = twin_query.TwinQuery(twin_store.STORE_PATH)
    try:
        if args.kind == "solar_site":
            result = tq.solar_at(
                payload.get("point") or {},
                tilt_deg=payload.get("tilt_deg"),
                azimuth_deg=payload.get("azimuth_deg"),
                system_kw=float(payload.get("system_kw") or 1.0),
                surface="bare_earth" if payload.get("surface") == "bare_earth" else "canopy",
                objective=str(payload.get("objective") or "annual_kwh"),
            )
        else:
            result = tq.viewshed_from(
                payload.get("point") or {},
                agl_m=float(payload.get("agl_m") or 1.7),
                max_km=payload.get("max_km"),
                refraction=str(payload.get("refraction") or "optical"),
                surface="bare_earth" if payload.get("surface") == "bare_earth" else "canopy",
                demonstrate=False,
            )
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except twin_query.TwinQueryError as exc:
        print(json.dumps(exc.payload, separators=(",", ":")))
        return 2
    finally:
        tq.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
