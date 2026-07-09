#!/usr/bin/env python3
"""Build planning-grade solar/PV siting layers for the Simulation window.

Outputs:
  <data>/solar/local/*.png + *.grid.json
  <data>/solar/solar-layers.json
  <data>/solar/solar-summary.json

The analyzer samples a bounded lattice over the AOI, computes a local horizon
for each valid point, estimates Daymet-normalized plane-of-array irradiance and
PVWatts-style kWh/kWdc, then writes low-resolution heatmaps and ranked sites.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import analyze_hydrology as viz  # noqa: E402
import twin_solar  # noqa: E402
import twin_viewshed  # noqa: E402


SOLAR_RAMPS = {
    "pv": [(33, 53, 64, 0), (47, 97, 121, 120), (89, 154, 139, 185),
           (234, 184, 85, 225), (244, 112, 67, 245)],
    "winter": [(30, 46, 68, 0), (62, 102, 140, 130), (119, 172, 169, 195),
               (231, 210, 128, 230), (247, 151, 78, 245)],
    "shade": [(28, 58, 54, 0), (79, 132, 101, 130), (226, 187, 86, 205),
              (203, 92, 71, 235), (112, 42, 58, 245)],
    "cloud": [(42, 57, 71, 0), (71, 113, 143, 140), (112, 155, 176, 200),
              (184, 196, 163, 225), (231, 190, 114, 240)],
    "vegetation": [(132, 42, 58, 240), (203, 92, 71, 230), (226, 187, 86, 205),
                   (89, 154, 139, 210), (79, 132, 101, 230)],
}


def finite_minmax(arr: np.ndarray) -> tuple[float | None, float | None]:
    vals = arr[np.isfinite(arr)]
    if vals.size == 0:
        return None, None
    return float(np.min(vals)), float(np.max(vals))


def norm_array(arr: np.ndarray, reverse: bool = False) -> np.ndarray:
    out = np.full(arr.shape, np.nan, dtype=np.float32)
    vals = arr[np.isfinite(arr)]
    if vals.size == 0:
        return out
    lo = float(np.percentile(vals, 2))
    hi = float(np.percentile(vals, 98))
    if hi <= lo:
        hi = float(np.max(vals))
        lo = float(np.min(vals))
    if hi <= lo:
        out[np.isfinite(arr)] = 0.5
        return out
    out[np.isfinite(arr)] = np.clip((arr[np.isfinite(arr)] - lo) / (hi - lo), 0.0, 1.0)
    return 1.0 - out if reverse else out


def grid_json(values: np.ndarray, bounds: list[float], metadata: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for r in range(values.shape[0]):
        row = []
        for raw in values[r]:
            v = float(raw)
            row.append(None if not math.isfinite(v) else round(v, 3))
        rows.append(row)
    return {
        "bounds_local": [round(float(v), 3) for v in bounds],
        "width": int(values.shape[1]),
        "height": int(values.shape[0]),
        "nodata": None,
        "values": rows,
        "legend": {},
        **metadata,
    }


def lattice_shape(bounds: tuple[float, float, float, float], samples: int) -> tuple[int, int]:
    minx, miny, maxx, maxy = bounds
    width = max(1.0, maxx - minx)
    height = max(1.0, maxy - miny)
    n = max(16, int(samples))
    nx = max(4, int(round(math.sqrt(n * width / height))))
    ny = max(4, int(math.ceil(n / nx)))
    return nx, ny


def point_site(tq: Any, x: float, y: float, stack: twin_viewshed.RingStack) -> twin_solar.SolarSite | None:
    ground = stack.sample_components(np.asarray([x], dtype=np.float32), np.asarray([y], dtype=np.float32))[0][0]
    if not math.isfinite(float(ground)):
        return None
    echo = tq.georef.echo(x, y)
    return twin_solar.SolarSite(float(echo["lat"]), float(echo["lon"]), float(ground))


def horizon_for(stack: twin_viewshed.RingStack, x: float, y: float, surface: str, n_az: int) -> np.ndarray | None:
    try:
        result = twin_viewshed.sweep(stack, x, y, 1.7, n_az=n_az, surface=surface, k="optical")
        return result["horizon_deg"]
    except Exception as exc:
        # A failed sweep means "no shading data", not "no shade" -- say so
        # instead of silently treating the point as fully open.
        print(f"warning: horizon sweep failed at ({x:.1f},{y:.1f}) [{surface}]: {exc}", file=sys.stderr)
        return None


def ranked_sites(tq: Any, rows: list[dict[str, Any]], data_dir: str,
                 system_kw: float, objective: str = "annual_kwh",
                 limit: int = 10) -> list[dict[str, Any]]:
    metric = "winter_poa_kwh_m2" if "winter" in str(objective).lower() else "pv_kwh_per_kwdc"
    rows = sorted(rows, key=lambda r: (-float(r["score"]), r["x"], r["y"]))
    refined = []
    for row in rows[:max(limit * 3, limit)]:
        opt = twin_solar.analyze_site(row["site"], data_dir=data_dir,
                                      horizon_deg=row["horizon"],
                                      system_kw=system_kw, objective=objective)
        refined.append({
            "surface": row.get("surface"),
            "point": tq.georef.echo(row["x"], row["y"]),
            "tilt_deg": opt["tilt_deg"],
            "azimuth_deg": opt["azimuth_deg"],
            "annual": opt["annual"],
            "vegetation": row.get("vegetation"),
            "score": opt["annual"].get(metric),
        })
    refined.sort(key=lambda r: (-(float(r.get("score") or 0.0)), r["point"]["x"], r["point"]["y"]))
    ranked = []
    for rank, row in enumerate(refined[:limit], start=1):
        ranked.append({"rank": rank, **row})
    return ranked


def write_layer(data_dir: str, layer_id: str, label: str, values: np.ndarray,
                bounds: list[float], ramp_key: str, unit: str,
                description: str) -> dict[str, Any]:
    local = os.path.join(data_dir, "solar", "local")
    os.makedirs(local, exist_ok=True)
    png_rel = f"solar/local/{layer_id}.png"
    grid_rel = f"solar/local/{layer_id}.grid.json"
    rgba = viz.colorize(norm_array(values, reverse=False), SOLAR_RAMPS[ramp_key])
    viz.write_png(rgba, os.path.join(data_dir, png_rel))
    lo, hi = finite_minmax(values)
    grid = grid_json(values, bounds, {
        "value_unit": unit,
        "range": [lo, hi],
        "description": description,
    })
    with open(os.path.join(data_dir, grid_rel), "w", encoding="utf-8") as fh:
        json.dump(grid, fh, separators=(",", ":"))
    return {
        "id": layer_id,
        "label": label,
        "type": "raster",
        "group": "solar",
        "image": png_rel,
        "grid": grid_rel,
        "bounds_local": grid["bounds_local"],
        "value_unit": unit,
        "description": description,
    }


def register_outputs(data_dir: str, layers: list[dict[str, Any]], summary_path: str) -> int | None:
    store_path = os.path.join(data_dir, "twin.gpkg")
    if not os.path.exists(store_path):
        return None
    import twin_store
    twin_store.DATA_DIR = data_dir
    twin_store.STORE_PATH = store_path
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")
    store = twin_store.Store(store_path)
    inputs = [os.path.join(data_dir, layer["grid"]) for layer in layers] + [summary_path]
    run = store.begin_run("analyze_solar.py", inputs=inputs, notes="solar resource and PV siting layers")
    for layer in layers:
        rel = layer["grid"]
        store.upsert_layer(layer["id"], label=layer["label"], kind="solar",
                           acquisition="derived", source_path=rel,
                           fetched_at=twin_store.utcnow(), status="ok",
                           content_sha1=twin_store.sha1_file(os.path.join(data_dir, rel)))
    store.upsert_layer("solar_summary", label="Solar siting summary", kind="solar",
                       acquisition="derived", source_path=os.path.relpath(summary_path, data_dir),
                       fetched_at=twin_store.utcnow(), status="ok",
                       content_sha1=twin_store.sha1_file(summary_path))
    store.finish_run(run, notes="solar layers + ranked panel sites")
    store.close()
    return run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--surface", choices=["bare_earth", "canopy"], default="canopy")
    ap.add_argument("--samples", type=int, default=220)
    ap.add_argument("--n-az", type=int, default=180)
    ap.add_argument("--system-kw", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    os.environ["TWIN_DATA_DIR"] = data_dir
    import twin_query
    import twin_store
    twin_store.DATA_DIR = data_dir
    twin_store.STORE_PATH = os.path.join(data_dir, "twin.gpkg")
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")

    tq = twin_query.TwinQuery(os.path.join(data_dir, "twin.gpkg"))
    region = tq._resolve_region({"aoi": True})
    stack = twin_viewshed.RingStack.load(os.path.join(data_dir, "terrain", "distant", "manifest.json")) \
        if os.path.exists(os.path.join(data_dir, "terrain", "distant", "manifest.json")) \
        else twin_viewshed.RingStack.from_local_files(data_dir)

    nx, ny = lattice_shape(region.bounds, args.samples)
    minx, miny, maxx, maxy = region.bounds
    bounds = [minx, miny, maxx, maxy]
    pv = np.full((ny, nx), np.nan, dtype=np.float32)
    poa = np.full((ny, nx), np.nan, dtype=np.float32)
    winter = np.full((ny, nx), np.nan, dtype=np.float32)
    shade = np.full((ny, nx), np.nan, dtype=np.float32)
    cloud = np.full((ny, nx), np.nan, dtype=np.float32)
    vegetation_clearance = np.full((ny, nx), np.nan, dtype=np.float32)
    vegetation_index = twin_solar.SolarVegetationIndex.from_data_dir(data_dir)
    vegetation_aware_candidates = []
    bare_earth_candidates = []
    valid_points = 0
    vegetation_excluded = 0
    vegetation_unknown = 0
    horizon_failures = 0
    first_site = None

    for row in range(ny):
        y = maxy - ((row + 0.5) / ny) * (maxy - miny)
        for col in range(nx):
            x = minx + ((col + 0.5) / nx) * (maxx - minx)
            if not region.contains(x, y):
                continue
            site = point_site(tq, x, y, stack)
            if site is None:
                continue
            valid_points += 1
            if first_site is None:
                first_site = site
            veg = vegetation_index.clearance_at(x, y, system_kw=args.system_kw)
            if veg.get("nearest_crown_clearance_m") is not None:
                vegetation_clearance[row, col] = float(veg["nearest_crown_clearance_m"])
            if veg.get("installable") is False:
                vegetation_excluded += 1
            if veg.get("installable") is None:
                vegetation_unknown += 1
            bare_horizon = horizon_for(stack, x, y, "bare_earth", args.n_az)
            canopy_horizon = horizon_for(stack, x, y, "canopy", args.n_az)
            if bare_horizon is None or canopy_horizon is None:
                horizon_failures += 1
            if canopy_horizon is not None and vegetation_index.available:
                # Per-stem crown lift on top of the 30 m EVH canopy horizon
                # (combined by max; see twin_solar.vegetation_horizon_lift).
                canopy_horizon = np.asarray(
                    twin_solar.vegetation_horizon_lift(vegetation_index, x, y, canopy_horizon)["horizon_deg"],
                    dtype=np.float32)
            horizon = canopy_horizon if args.surface == "canopy" else bare_horizon
            default_tilt = max(5.0, min(60.0, abs(site.lat)))
            default_az = 180.0 if site.lat >= 0 else 0.0
            result = twin_solar.analyze_site(site, data_dir=data_dir, horizon_deg=horizon,
                                             tilt_deg=default_tilt, azimuth_deg=default_az,
                                             system_kw=args.system_kw)
            bare_result = result if args.surface == "bare_earth" else twin_solar.analyze_site(
                site, data_dir=data_dir, horizon_deg=bare_horizon,
                tilt_deg=default_tilt, azimuth_deg=default_az,
                system_kw=args.system_kw)
            canopy_result = result if args.surface == "canopy" else twin_solar.analyze_site(
                site, data_dir=data_dir, horizon_deg=canopy_horizon,
                tilt_deg=default_tilt, azimuth_deg=default_az,
                system_kw=args.system_kw)
            annual = result["annual"]
            pv[row, col] = float(annual["pv_kwh_per_kwdc"])
            poa[row, col] = float(annual["poa_kwh_m2"])
            winter[row, col] = float(annual["winter_poa_kwh_m2"])
            shade[row, col] = float(annual["shade_loss_pct"])
            cloud[row, col] = float(annual["cloud_loss_pct"])
            bare_earth_candidates.append({
                "x": x, "y": y, "site": site, "horizon": bare_horizon,
                "surface": "bare_earth",
                "score": float(bare_result["annual"]["pv_kwh_per_kwdc"]),
                "default": bare_result,
                "vegetation": veg,
            })
            if veg.get("installable") is not False:
                vegetation_aware_candidates.append({
                    "x": x, "y": y, "site": site, "horizon": canopy_horizon,
                    "surface": "canopy",
                    "score": float(canopy_result["annual"]["pv_kwh_per_kwdc"]),
                    "default": canopy_result,
                    "vegetation": veg,
                })

    vegetation_aware_sites = ranked_sites(
        tq, vegetation_aware_candidates, data_dir, args.system_kw, objective="annual_kwh")
    bare_earth_sites = ranked_sites(
        tq, bare_earth_candidates, data_dir, args.system_kw, objective="annual_kwh")
    ranked = vegetation_aware_sites

    layers = [
        write_layer(data_dir, "solar_pv_annual", "Solar PV yield", pv, bounds, "pv",
                    "kWh/kWdc/yr", "Estimated annual PV yield per installed kWdc at the default fixed angle (latitude tilt, equator-facing); ranked sites optimize angles."),
        write_layer(data_dir, "solar_poa_annual", "Solar panel radiation", poa, bounds, "pv",
                    "kWh/m2/yr", "Estimated annual plane-of-array solar radiation at the default fixed angle (latitude tilt, equator-facing)."),
        write_layer(data_dir, "solar_winter_poa", "Winter panel radiation", winter, bounds, "winter",
                    "kWh/m2", "November-February plane-of-array solar radiation at the default fixed angle."),
        write_layer(data_dir, "solar_shade_loss", "Solar shade loss", shade, bounds, "shade",
                    "%", "Annual plane-of-array loss from terrain/canopy horizon shading."),
        write_layer(data_dir, "solar_cloud_loss", "Solar cloud loss", cloud, bounds, "cloud",
                    "%", "Annual clear-sky loss inferred from local Daymet all-sky radiation."),
        write_layer(data_dir, "solar_vegetation_clearance", "Solar vegetation clearance",
                    vegetation_clearance, bounds, "vegetation", "m",
                    "Nearest vegetation crown clearance around the assumed panel footprint; negative values require clearing."),
    ]
    catalog_path = os.path.join(data_dir, "solar", "solar-layers.json")
    os.makedirs(os.path.dirname(catalog_path), exist_ok=True)
    with open(catalog_path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "layers": layers}, fh, indent=2)

    center_site = point_site(tq, (minx + maxx) / 2.0, (miny + maxy) / 2.0, stack) or (
        vegetation_aware_candidates[0]["site"] if vegetation_aware_candidates
        else bare_earth_candidates[0]["site"] if bare_earth_candidates
        else first_site or twin_solar.SolarSite(0.0, 0.0, 0.0))
    summary = {
        "surface": args.surface,
        "sample_grid": {
            "width": nx,
            "height": ny,
            "valid_points": valid_points,
            "installable_points": len(vegetation_aware_candidates),
            "vegetation_excluded_points": vegetation_excluded,
            "vegetation_unknown_points": vegetation_unknown,
            "horizon_failures": horizon_failures,
        },
        "vegetation_policy": {
            "source": vegetation_index.source,
            "available": vegetation_index.available,
            "tree_count": len(vegetation_index.records),
            "best_sites_require_installable_footprint": vegetation_index.available,
            "clearance_radius_m": twin_solar.required_vegetation_clearance_radius_m(args.system_kw),
            "note": "Recommended sites exclude vegetation-crown conflicts when a vegetation inventory is available.",
        },
        "layers": [layer["id"] for layer in layers],
        "recommended_sites": ranked,
        "vegetation_aware_sites": vegetation_aware_sites,
        "bare_earth_sites": bare_earth_sites,
        **twin_solar.summary_payload(data_dir, center_site.lat, center_site.elevation_m),
        "notes": [
            "Fixed-panel PVWatts-style planning estimate, not a bankable solar assessment.",
            "Terrain/canopy horizon is modeled locally; recommended sites also require an open vegetation footprint when inventory data exists.",
            "Cloud loss comes from Daymet all-sky shortwave climatology when available.",
        ],
    }
    summary_path = os.path.join(data_dir, "solar", "solar-summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    run = register_outputs(data_dir, layers, summary_path)
    payload = {
        "summary": os.path.relpath(summary_path, data_dir),
        "layers": [layer["id"] for layer in layers],
        "recommended_sites": ranked[:5],
        "vegetation_aware_sites": vegetation_aware_sites[:3],
        "bare_earth_sites": bare_earth_sites[:3],
        "valid_points": valid_points,
        "installable_points": len(vegetation_aware_candidates),
        "vegetation_excluded_points": vegetation_excluded,
        "run": run,
    }
    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
