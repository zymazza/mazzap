#!/usr/bin/env python3
"""Validate bare-earth viewshed math against GDAL gdal_viewshed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import numpy as np
from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_viewshed as viewshed

gdal.UseExceptions()


def write_gtiff(ring: viewshed.Ring, path: str) -> None:
    arr = np.where(np.isfinite(ring.ground), ring.ground, -9999.0).astype(np.float32)
    ds = gdal.GetDriverByName("GTiff").Create(path, ring.width, ring.height, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((ring.min_x, ring.x_step, 0.0, ring.max_y, 0.0, -ring.y_step))
    b = ds.GetRasterBand(1)
    b.WriteArray(arr)
    b.SetNoDataValue(-9999.0)
    ds = None


def observer_points(ring: viewshed.Ring) -> list[tuple[float, float]]:
    rows, cols = np.nonzero(np.isfinite(ring.ground))
    picks = []
    span = min(abs(ring.min_x), abs(ring.max_x), abs(ring.min_y), abs(ring.max_y))
    targets = [
        (max(300.0, span * 0.05), 0),
        (-span * 0.18, span * 0.12),
        (span * 0.25, -span * 0.18),
        (-span * 0.42, -span * 0.35),
        (span * 0.45, span * 0.30),
    ]
    for tx, ty in targets:
        xs = ring.min_x + cols * ring.x_step
        ys = ring.max_y - rows * ring.y_step
        i = int(np.argmin((xs - tx) ** 2 + (ys - ty) ** 2))
        pt = (float(xs[i]), float(ys[i]))
        if pt not in picks:
            picks.append(pt)
    return picks


def aligned_gdal_visibility(path: str, ring: viewshed.Ring) -> np.ndarray:
    ds = gdal.Open(path)
    arr = ds.ReadAsArray()
    gt = ds.GetGeoTransform()
    col0 = int(round((gt[0] - ring.min_x) / ring.x_step))
    row0 = int(round((ring.max_y - gt[3]) / ring.y_step))
    out = np.zeros((ring.height, ring.width), dtype=bool)
    r0 = max(0, row0)
    c0 = max(0, col0)
    r1 = min(ring.height, row0 + arr.shape[0])
    c1 = min(ring.width, col0 + arr.shape[1])
    if r1 > r0 and c1 > c0:
        ar0 = r0 - row0
        ac0 = c0 - col0
        out[r0:r1, c0:c1] = arr[ar0:ar0 + (r1 - r0), ac0:ac0 + (c1 - c0)] > 0
    return out


def validation_ring_from_manifest(data: str, stack: viewshed.RingStack) -> viewshed.Ring:
    ring = next((r for r in stack.rings if r.name == "B"), stack.rings[0])
    if ring.name != "B":
        return ring
    item = next((r for r in stack.manifest.get("rings", []) if r.get("id") == "B"), None)
    rel = item.get("validation_ground_full") if item else None
    if not rel:
        return ring
    path = os.path.join(data, rel)
    if not os.path.exists(path):
        return ring
    arr = gdal.Open(path).ReadAsArray().astype(np.float32)
    arr[arr <= -9998.0] = np.nan
    minx, miny, maxx, maxy = [float(v) for v in item["bounds_local"]]
    return viewshed.Ring(
        "B", arr, minx, maxx, miny, maxy, float(item["resolution_m"]),
        canopy=None, inner_m=float(item.get("inner_m") or 0.0),
        outer_m=float(item.get("outer_m") or 0.0), source=item,
    )


def main() -> int:
    data = os.path.join(PROJECT, "data")
    manifest = os.path.join(data, "terrain", "distant", "manifest.json")
    stack = viewshed.RingStack.load(manifest) if os.path.exists(manifest) else viewshed.RingStack.from_local_files(data)
    ring = validation_ring_from_manifest(data, stack)
    if ring.name == "B":
        local = next((r for r in stack.rings if r.name == "A"), None)
        stack = viewshed.RingStack([r for r in (local, ring) if r is not None], manifest=stack.manifest)
    is_distant = ring.name == "B"
    threshold = 0.98 if is_distant else 0.90
    agls = [1.7, 10.0, 30.0, 60.0, 120.0]
    rows = []
    with tempfile.TemporaryDirectory() as td:
        tif = os.path.join(td, "ground.tif")
        write_gtiff(ring, tif)
        for idx, ((x, y), agl) in enumerate(zip(observer_points(ring), agls), start=1):
            out = os.path.join(td, f"viewshed_{idx}.tif")
            maxdist = min(float(ring.outer_m or stack.max_distance_m), stack.max_distance_m)
            subprocess.run([
                "gdal_viewshed", "-q",
                "-ox", str(x), "-oy", str(y), "-oz", str(agl), "-tz", "0",
                "-md", str(maxdist), "-cc", "0.8571428571428572",
                tif, out,
            ], check=True)
            gd_vis = aligned_gdal_visibility(out, ring)
            ours = viewshed.sweep(stack, x, y, agl, n_az=1440, max_km=maxdist / 1000.0,
                                  surface="bare_earth", k="optical")["visible"][ring.name].astype(bool)
            valid = np.isfinite(ring.ground)
            valid[[0, -1], :] = False
            valid[:, [0, -1]] = False
            common = valid
            agreement = float(np.count_nonzero(gd_vis[common] == ours[common]) / max(1, np.count_nonzero(common)))
            rows.append({"observer": [round(x, 3), round(y, 3)], "agl_m": agl,
                         "agreement": agreement,
                         "cells": int(np.count_nonzero(common))})
    worst = min(r["agreement"] for r in rows) if rows else 0.0
    result = {
        "oracle": "gdal_viewshed",
        "ring": ring.name,
        "surface": "bare_earth",
        "k": viewshed.REFRACTION_K["optical"],
        "cc": 1.0 - viewshed.REFRACTION_K["optical"],
        "rows": rows,
        "worst_agreement": worst,
        "pass_threshold": threshold,
        "note": "Distant ring-B validation excludes the one-cell edge band and compares the common finite footprint.",
    }
    print(json.dumps(result, indent=2))
    return 0 if worst >= result["pass_threshold"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
