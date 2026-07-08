#!/usr/bin/env python3
"""Build precomputed viewshed layers and horizon profiles."""

from __future__ import annotations

import argparse
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

import twin_store
import twin_viewshed as viewshed
from twin_query import _aoi_rings, shoelace_area

gdal.UseExceptions()


def write_png(rgba: np.ndarray, path: str) -> None:
    h, w, _ = rgba.shape
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    for band in range(4):
        mem.GetRasterBand(band + 1).WriteArray(rgba[:, :, band])
    gdal.GetDriverByName("PNG").CreateCopy(path, mem)
    aux = path + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)


def mask_png(mask: np.ndarray) -> np.ndarray:
    rgba = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
    rgba[mask > 0] = [255, 142, 38, 185]
    return rgba


def grid_json(mask: np.ndarray, ring: viewshed.Ring, metadata: dict | None = None) -> dict:
    out = {
        "bounds_local": [ring.min_x, ring.min_y, ring.max_x, ring.max_y],
        "width": int(mask.shape[1]),
        "height": int(mask.shape[0]),
        "nodata": None,
        "legend": {
            "0": {"name": "not visible from AOI sample set"},
            "1": {"name": "visible from at least one AOI sample"},
        },
        "values": [[int(v) for v in row] for row in mask],
    }
    if metadata:
        out.update(metadata)
    return out


def aoi_sample_points(stack: viewshed.RingStack) -> list[tuple[float, float]]:
    rings = _aoi_rings()
    pts = []
    for ring in rings:
        if len(ring) < 3:
            continue
        # Eight boundary samples per ring plus centroid-like vertex mean.
        open_ring = ring[:-1] if ring[0] == ring[-1] else ring
        step = max(1, len(open_ring) // 8)
        pts.extend((float(x), float(y)) for x, y in open_ring[::step][:8])
        pts.append((sum(p[0] for p in open_ring) / len(open_ring),
                    sum(p[1] for p in open_ring) / len(open_ring)))
    # Keep only terrain-valid points, snapping invalid samples to nearest valid.
    out = []
    seen = set()
    for x, y in pts:
        g = stack.sample_components(np.asarray([x]), np.asarray([y]))[0][0]
        if not np.isfinite(g):
            x, y = viewshed.nearest_valid_point(stack, (x, y))
        key = (round(x, 1), round(y, 1))
        if key not in seen:
            seen.add(key)
            out.append((x, y))
    return out[:41]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--n-az", type=int, default=720)
    args = ap.parse_args()
    data = os.path.abspath(args.data_dir)
    manifest = os.path.join(data, "terrain", "distant", "manifest.json")
    stack = viewshed.RingStack.load(manifest) if os.path.exists(manifest) else viewshed.RingStack.from_local_files(data)
    out_dir = os.path.join(data, "viewshed")
    local_dir = os.path.join(out_dir, "local")
    os.makedirs(local_dir, exist_ok=True)

    obs = viewshed.nearest_valid_point(stack)
    horizon = {}
    for surface in ("bare_earth", "canopy"):
        r = viewshed.sweep(stack, obs[0], obs[1], 1.7, n_az=args.n_az, surface=surface)
        horizon[surface] = [round(float(v), 4) for v in r["horizon_deg"]]
    horizon_doc = {
        "observer": {"x": round(obs[0], 3), "y": round(obs[1], 3), "agl_m": 1.7},
        "azimuth_deg": [round(i * 360.0 / args.n_az, 4) for i in range(args.n_az)],
        "horizon_deg": horizon,
        "surface_default": "bare_earth",
        "k": viewshed.REFRACTION_K["optical"],
        "manifest_hash": stack.manifest_hash,
    }
    horizon_path = os.path.join(out_dir, "horizon.json")
    with open(horizon_path, "w") as fh:
        json.dump(horizon_doc, fh, indent=2)

    points = aoi_sample_points(stack)
    union = viewshed.union_sweep(stack, points, 120.0, n_az=args.n_az, surface="canopy")
    ring = stack.rings[0]
    mask = union[ring.name]
    png_rel = "viewshed/local/aoi_union_visibility.png"
    grid_rel = "viewshed/local/aoi_union_visibility.grid.json"
    write_png(mask_png(mask), os.path.join(data, png_rel))
    layer_grid = grid_json(mask, ring, {"manifest_hash": stack.manifest_hash, "surface": "canopy"})
    with open(os.path.join(data, grid_rel), "w") as fh:
        json.dump(layer_grid, fh, separators=(",", ":"))
    layer = {
        "id": "viewshed_aoi_union",
        "label": "AOI cumulative viewshed",
        "type": "raster",
        "group": "viewshed",
        "image": png_rel,
        "grid": grid_rel,
        "bounds_local": layer_grid["bounds_local"],
        "description": "Cells visible from the AOI boundary/interior sample set at 120 m AGL on the canopy surface.",
    }
    catalog_path = os.path.join(out_dir, "viewshed-layers.json")
    with open(catalog_path, "w") as fh:
        json.dump({"version": 1, "layers": [layer]}, fh, indent=2)

    visible_cells = int(np.count_nonzero(mask))
    valid_cells = int(np.count_nonzero(np.isfinite(ring.ground)))
    summary = {
        "observer": horizon_doc["observer"],
        "sample_points": len(points),
        "visible_cells": visible_cells,
        "valid_cells": valid_cells,
        "visible_fraction": 0.0 if valid_cells == 0 else visible_cells / valid_cells,
        "analyzed_extent_km": stack.max_distance_m / 1000.0,
        "manifest_hash": stack.manifest_hash,
        "surfaces": ["bare_earth", "canopy"],
        "notes": [
            "Canopy surface uses DTM + decoded LANDFIRE EVH as blockers; observer z is ground + AGL.",
            "If distant ring B/C tiles are absent, results are truthful only inside ring A.",
        ],
    }
    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)

    if os.path.exists(os.path.join(data, "twin.gpkg")):
        twin_store.DATA_DIR = data
        twin_store.STORE_PATH = os.path.join(data, "twin.gpkg")
        twin_store.JOURNAL_DIR = os.path.join(data, "journal")
        store = twin_store.Store(twin_store.STORE_PATH)
        run = store.begin_run("analyze_viewshed.py", inputs=[horizon_path, os.path.join(data, grid_rel)],
                              notes="viewshed horizon and AOI cumulative drape")
        for layer_id, rel, label in (
            ("viewshed_horizon", "viewshed/horizon.json", "Viewshed horizon profile"),
            ("viewshed_aoi_union", grid_rel, "AOI cumulative viewshed"),
        ):
            store.upsert_layer(layer_id, label=label, kind="viewshed",
                               acquisition="derived", service=None, source_path=rel,
                               fetched_at=twin_store.utcnow(), feature_count=None,
                               status="ok", content_sha1=twin_store.sha1_file(os.path.join(data, rel)))
        store.finish_run(run, notes="viewshed layers + horizon profile")
        store.close()
    print(json.dumps({"summary": summary_path, "horizon": horizon_path,
                      "layers": catalog_path, "visible_fraction": summary["visible_fraction"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
