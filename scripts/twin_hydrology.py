#!/usr/bin/env python3
"""Core terrain-hydrology engine for VEIL twins — pure numpy, no heavy deps.

This is Tier 1 of the hydrology system: the
deterministic terrain analysis that everything else stands on. It works directly
on the twin's LiDAR-derived terrain grid (data/terrain/grid.json), in the scene's
local-meter coordinates, and returns gridded fields:

  - filled DEM + depression depth   (ponding / closed basins)
  - D8 flow direction
  - flow accumulation               (upslope contributing cells -> area)
  - slope                           (from the unfilled DEM)
  - TWI = ln( (a + cell) / tan(slope) )   topographic wetness index

VEIL ships zero hydrology libraries (no pysheds/whitebox/richdem/scipy), so the
depression fill is a Priority-Flood (Barnes et al. 2014) implemented on a heap,
and flow accumulation is an O(n) topological accumulation over the D8 graph. At
this twin's size (220x289 ~ 64k cells) both run in well under a second.

Grid convention (matches public/viewer/terrain.js and the atlas value grids):
row 0 = north (maxY), col 0 = west (minX); cell (row,col) is at
local (minX + col*xStep, maxY - row*yStep). heights are a flat row-major list
with None/NaN outside the footprint.

This module computes; analyze_hydrology.py renders/exports/validates and
hydro_scenario.py consumes the summary stats. No file I/O here beyond loading
the grid, so it stays easy to test and reuse (incl. a future MCP tool).
"""

import heapq
import json
import math
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

# D8 neighbor offsets (8-connected) and their step distances in cells.
_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def load_grid(data_dir, name="grid.json"):
    """Load a terrain grid into a 2D float array (NaN = no data) plus its
    geometry. cellsize is the mean of x/y step (the grid is ~square)."""
    path = os.path.join(data_dir, "terrain", name)
    g = json.load(open(path))
    h, w = g["height"], g["width"]
    flat = np.array([np.nan if v is None else v for v in g["heights"]], dtype=float)
    dem = flat.reshape(h, w)
    xstep = (g["maxX"] - g["minX"]) / (w - 1) if w > 1 else g.get("xStep", 1.0)
    ystep = (g["maxY"] - g["minY"]) / (h - 1) if h > 1 else g.get("yStep", 1.0)
    return {
        "dem": dem, "width": w, "height": h,
        "minX": g["minX"], "maxX": g["maxX"], "minY": g["minY"], "maxY": g["maxY"],
        "xstep": xstep, "ystep": ystep, "cellsize": (xstep + ystep) / 2.0,
        "bounds_local": [g["minX"], g["minY"], g["maxX"], g["maxY"]],
        "min_elevation": g["minElevation"], "max_elevation": g["maxElevation"],
        "raw": g,
    }


def fill_depressions(dem, epsilon=1e-6):
    """Priority-Flood+epsilon depression filling (Barnes, Lehman & Mulla 2014).

    Returns (filled, depth) where filled >= dem everywhere and every cell drains
    to the grid edge; depth = filled - dem is the depression (ponding) depth.
    NaN cells are treated as outside the domain and seed the flood from the edge.

    The ``epsilon`` term is the +epsilon variant: filled cells inside a depression
    are raised to *just above* the cell they spilled from, so the basin floor keeps
    a monotonic gradient toward its outlet instead of becoming a perfectly flat
    plateau. Without it, D8 (which requires a strictly lower neighbour) would mark
    every filled-flat cell as a sink (fdir=-1), severing flow accumulation and
    scenario routing at the rim of every filled basin. At 1e-6 m the cumulative
    rise across a basin is sub-millimetre and well inside float64 precision for
    these elevations, so depression storage (depth) is unaffected in practice.
    """
    h, w = dem.shape
    filled = np.full((h, w), np.inf)
    closed = ~np.isfinite(dem)  # NaN cells act as the outer boundary
    visited = np.zeros((h, w), dtype=bool)
    heap = []

    def push_edge(r, c):
        if not visited[r, c] and np.isfinite(dem[r, c]):
            visited[r, c] = True
            heapq.heappush(heap, (dem[r, c], r, c))
            filled[r, c] = dem[r, c]

    # Seed: real cells on the array border, and real cells adjacent to NaN.
    for r in range(h):
        for c in range(w):
            if not np.isfinite(dem[r, c]):
                continue
            on_border = r == 0 or c == 0 or r == h - 1 or c == w - 1
            touches_nan = False
            if not on_border:
                for dr, dc in _NB:
                    if not np.isfinite(dem[r + dr, c + dc]):
                        touches_nan = True
                        break
            if on_border or touches_nan:
                push_edge(r, c)

    while heap:
        elev, r, c = heapq.heappop(heap)
        for dr, dc in _NB:
            nr, nc = r + dr, c + dc
            if nr < 0 or nc < 0 or nr >= h or nc >= w:
                continue
            if visited[nr, nc] or not np.isfinite(dem[nr, nc]):
                continue
            visited[nr, nc] = True
            # +epsilon: raise pits to just above the spill cell so filled flats
            # retain a drainage gradient toward the outlet (real terrain higher
            # than elev+epsilon keeps its own elevation untouched).
            ne = max(dem[nr, nc], elev + epsilon)
            filled[nr, nc] = ne
            heapq.heappush(heap, (ne, nr, nc))

    filled[closed] = np.nan
    depth = np.where(np.isfinite(dem), filled - dem, np.nan)
    return filled, depth


def d8_flowdir(filled, cellsize):
    """D8 steepest-descent flow direction over the filled DEM.

    Returns an int array: index 0..7 into _NB for the downslope neighbor, -1 for
    cells with no lower neighbor (edge/sink outlet), and -2 for NaN. Ties broken
    by steepest drop/distance.
    """
    h, w = filled.shape
    fdir = np.full((h, w), -1, dtype=np.int8)
    diag = math.sqrt(2.0) * cellsize
    dist = [diag, cellsize, diag, cellsize, cellsize, diag, cellsize, diag]
    for r in range(h):
        for c in range(w):
            e = filled[r, c]
            if not np.isfinite(e):
                fdir[r, c] = -2
                continue
            best_slope = 0.0
            best = -1
            for k, (dr, dc) in enumerate(_NB):
                nr, nc = r + dr, c + dc
                if nr < 0 or nc < 0 or nr >= h or nc >= w:
                    continue
                ne = filled[nr, nc]
                if not np.isfinite(ne):
                    continue
                s = (e - ne) / dist[k]
                if s > best_slope:
                    best_slope = s
                    best = k
            fdir[r, c] = best
    return fdir


def flow_accumulation(fdir):
    """Number of upslope cells draining through each cell (incl. itself), via
    topological accumulation over the D8 graph (Kahn's algorithm). O(n)."""
    h, w = fdir.shape
    receiver = np.full((h, w), -1, dtype=np.int64)  # flat index of downstream cell
    indeg = np.zeros((h, w), dtype=np.int64)
    valid = fdir >= -1

    for r in range(h):
        for c in range(w):
            k = fdir[r, c]
            if k < 0:
                continue
            dr, dc = _NB[k]
            nr, nc = r + dr, c + dc
            receiver[r, c] = nr * w + nc
            indeg[nr, nc] += 1

    acc = np.where(valid, 1.0, 0.0)
    # process cells with no upstream contributors first
    stack = [(r, c) for r in range(h) for c in range(w)
             if valid[r, c] and indeg[r, c] == 0]
    while stack:
        r, c = stack.pop()
        rec = receiver[r, c]
        if rec < 0:
            continue
        nr, nc = divmod(int(rec), w)
        acc[nr, nc] += acc[r, c]
        indeg[nr, nc] -= 1
        if indeg[nr, nc] == 0:
            stack.append((nr, nc))
    acc[~valid] = np.nan
    return acc


def slope_radians(dem, cellsize):
    """Slope (radians) via Horn's 3x3 finite difference; NaN-aware with a small
    floor so TWI's tan(slope) never divides by zero."""
    h, w = dem.shape
    filled = np.where(np.isfinite(dem), dem, np.nan)
    # pad by edge replication
    p = np.pad(filled, 1, mode="edge")
    # fill interior NaNs with local mean so gradients are defined near holes
    for _ in range(2):
        nan = ~np.isfinite(p)
        if not nan.any():
            break
        sm = np.copy(p)
        sm[nan] = 0
        cnt = np.isfinite(p).astype(float)
        kern = np.zeros_like(p)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                kern += np.roll(np.roll(np.nan_to_num(p), dr, 0), dc, 1)
        # cheap local fill (good enough at the footprint edge)
        p[nan] = (np.roll(sm, 1, 0) + np.roll(sm, -1, 0) +
                  np.roll(sm, 1, 1) + np.roll(sm, -1, 1))[nan] / 4.0
    z = p
    dzdx = ((z[1:-1, 2:] - z[1:-1, :-2]) / (2 * cellsize))
    dzdy = ((z[2:, 1:-1] - z[:-2, 1:-1]) / (2 * cellsize))
    slope = np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2))
    slope[~np.isfinite(dem)] = np.nan
    return slope


def twi(acc_cells, slope_rad, cellsize):
    """Topographic Wetness Index = ln( a / tan(beta) ), a = specific catchment
    area (upslope area per unit contour width ~ acc*cellarea/cellsize)."""
    a = (acc_cells * cellsize * cellsize) / cellsize  # = acc * cellsize
    tanb = np.tan(np.maximum(slope_rad, math.radians(0.5)))  # floor ~0.5deg
    out = np.log(np.maximum(a, cellsize) / tanb)
    out[~np.isfinite(slope_rad)] = np.nan
    return out


def compute_all(grid):
    """Run the full Tier-1 chain on a loaded grid; returns a dict of fields."""
    dem = grid["dem"]
    cs = grid["cellsize"]
    filled, depth = fill_depressions(dem)
    fdir = d8_flowdir(filled, cs)
    acc = flow_accumulation(fdir)
    slope = slope_radians(dem, cs)
    wet = twi(acc, slope, cs)
    return {
        "dem": dem, "filled": filled, "depression_depth": depth,
        "flowdir": fdir, "flow_accum_cells": acc,
        "slope_rad": slope, "twi": wet,
        "cell_area_m2": cs * cs,
    }


if __name__ == "__main__":  # smoke test against the default twin
    import sys
    dd = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT, "data")
    g = load_grid(dd)
    f = compute_all(g)
    acc = f["flow_accum_cells"]
    print("grid %dx%d  cell %.2f m  area %.1f ha" % (
        g["width"], g["height"], g["cellsize"],
        np.isfinite(g["dem"]).sum() * f["cell_area_m2"] / 1e4))
    print("max flow accumulation: %.0f cells (%.1f ha)" % (
        np.nanmax(acc), np.nanmax(acc) * f["cell_area_m2"] / 1e4))
    print("TWI range: %.1f .. %.1f" % (np.nanmin(f["twi"]), np.nanmax(f["twi"])))
    print("ponding cells > 0.1 m: %d" % int((f["depression_depth"] > 0.1).sum()))
