"""Generic stem detectors — the lower rungs of the vegetation capability
ladder, used by analyze_vegetation.py when no LiDAR segmentation stems exist.

Both detectors are dependency-free (numpy only, no scipy): the canopy is
divided into ~`spacing`-meter blocks and at most one stem is placed per block,
at the block's peak. Deterministic given the same inputs.

Coordinates are scene-local meters; rasters are assumed aligned to the grid's
outer cell-edge footprint (outerMinX..outerMaxX / outerMinY..outerMaxY), the
same invariant analyze_vegetation.py relies on (docs/grid-contract.md).
"""

import hashlib
import math
import random

import numpy as np


def _outer(grid):
    return grid["outerMinX"], grid["outerMaxX"], grid["outerMinY"], grid["outerMaxY"]


def _stable_unit(*parts):
    h = hashlib.blake2b(digest_size=8)
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return int.from_bytes(h.digest(), "big") / float(1 << 64)


def _jittered_pixel_xy(row, col, h, w, ox0, ox1, oy0, oy1, salt):
    """Deterministically place a stem within its native raster pixel."""
    ux = _stable_unit(salt, "x", row, col, h, w)
    uy = _stable_unit(salt, "y", row, col, h, w)
    x = ox0 + (col + ux) / w * (ox1 - ox0)
    y = oy1 - (row + uy) / h * (oy1 - oy0)
    return x, y


def _block_peaks(values, mask, grid, spacing, picker):
    """Yield (x, y, peak_value) — one per block of ~spacing meters where the
    block holds any masked (canopy) cell. picker(window)->(row,col) selects
    the in-block peak. values/mask are HxW arrays over the outer footprint."""
    h, w = values.shape
    ox0, ox1, oy0, oy1 = _outer(grid)
    px_per_m_x = w / (ox1 - ox0)
    px_per_m_y = h / (oy1 - oy0)
    bx = max(1, int(round(spacing * px_per_m_x)))
    by = max(1, int(round(spacing * px_per_m_y)))
    for r0 in range(0, h, by):
        for c0 in range(0, w, bx):
            wmask = mask[r0:r0 + by, c0:c0 + bx]
            if not wmask.any():
                continue
            wval = values[r0:r0 + by, c0:c0 + bx]
            rr, cc = picker(np.where(wmask, wval, -np.inf))
            row, col = r0 + rr, c0 + cc
            x, y = _jittered_pixel_xy(row, col, h, w, ox0, ox1, oy0, oy1,
                                      "block_peak")
            yield x, y, float(values[row, col])


def stratified_grid_candidates(grid, spacing, salt="canopy_fill"):
    """Yield one deterministic, de-latticed candidate inside each spacing cell."""
    ox0, ox1, oy0, oy1 = _outer(grid)
    gx = np.arange(ox0 + spacing / 2, ox1, spacing)
    gy = np.arange(oy0 + spacing / 2, oy1, spacing)
    half = spacing / 2
    for ix, cx in enumerate(gx):
        x0 = max(ox0, cx - half)
        x1 = min(ox1, cx + half)
        for iy, cy in enumerate(gy):
            y0 = max(oy0, cy - half)
            y1 = min(oy1, cy + half)
            ux = _stable_unit(salt, "x", ix, iy)
            uy = _stable_unit(salt, "y", ix, iy)
            yield x0 + ux * (x1 - x0), y0 + uy * (y1 - y0)


def _spatial_index(trees, cell):
    buckets = {}
    for i, tree in enumerate(trees):
        buckets.setdefault((int(tree["x"] // cell), int(tree["y"] // cell)), []).append(i)
    return buckets


def _neighbors(buckets, x, y, radius, cell):
    out = []
    for cx in range(int((x - radius) // cell), int((x + radius) // cell) + 1):
        for cy in range(int((y - radius) // cell), int((y + radius) // cell) + 1):
            out.extend(buckets.get((cx, cy), []))
    return out


def _accept_result(result):
    if isinstance(result, tuple):
        return bool(result[0]), result[1]
    return bool(result), None


def densify_canopy(anchor_trees, grid, spacing, ndvi, to_px, valid_position,
                   community_at, accept_candidate, typical_height, add_tree,
                   elevation_for_candidate=None, cell=4.0, salt="canopy_fill"):
    """Fill canopy-confirmed spacing cells with stratified, deterministic stems.

    The spectral/community/terrain/occupancy gates are evaluated at the final
    stratified position. Heights keep the historical seeded RNG stream; only
    stem positions are de-latticed.
    """
    buckets = _spatial_index(anchor_trees, cell)
    added = 0
    for x, y in stratified_grid_candidates(grid, spacing, salt=salt):
        if not valid_position(x, y):
            continue
        px, py = to_px(x, y)
        if ndvi[py, px] < 0.15:
            continue
        phys, community = community_at(x, y)
        accepted, context = _accept_result(
            accept_candidate(x, y, phys, community)
        )
        if not accepted:
            continue
        near = _neighbors(buckets, x, y, 9.0, cell)
        if near:
            dmin = min(
                math.hypot(anchor_trees[i]["x"] - x, anchor_trees[i]["y"] - y)
                for i in near
            )
            if dmin < 2.8:
                continue
            heights = [
                anchor_trees[i]["height"] for i in near
                if math.hypot(anchor_trees[i]["x"] - x, anchor_trees[i]["y"] - y) < 12
            ]
            base = float(np.mean(heights)) if heights else typical_height(community)
        else:
            base = typical_height(community)
        height = max(3.0, base * random.uniform(0.72, 1.04))
        # Preserve the old height RNG stream while replacing positional jitter
        # with deterministic stratified placement.
        random.uniform(-1.0, 1.0)
        random.uniform(-1.0, 1.0)
        if elevation_for_candidate:
            z = elevation_for_candidate(x, y, near, anchor_trees)
        elif near:
            z = anchor_trees[near[0]]["z"]
        else:
            z = grid["minElevation"]
        add_tree(x, y, z, height, community, context)
        added += 1
    return added


def detect_from_chm(dsm, dtm, grid, spacing=6.0, min_height=2.5,
                    terrain_valid=None):
    """Canopy Height Model (DSM - DTM) local maxima. Heights come straight
    from the CHM. dsm/dtm are HxW arrays over the outer footprint."""
    chm = dsm - dtm
    chm[~np.isfinite(chm)] = 0.0
    mask = chm >= min_height
    stems = []
    argmax = lambda win: np.unravel_index(np.argmax(win), win.shape)  # noqa: E731
    for x, y, height in _block_peaks(chm, mask, grid, spacing, argmax):
        if terrain_valid and not terrain_valid(x, y):
            continue
        z = float(dtm_sample(dtm, grid, x, y))
        stems.append({"x": round(x, 3), "y": round(y, 3), "z": round(z, 2),
                      "height": round(float(height), 2), "confidence": 0.5,
                      "source": "chm"})
    return stems


def detect_from_ndvi(ndvi, grid, spacing=6.0, ndvi_min=0.2,
                     nominal_height=10.0, terrain_valid=None,
                     elevation=None):
    """NDVI canopy local maxima — the weakest rung: positions only, no real
    heights (a nominal canopy height with low confidence). ndvi is HxW over
    the outer footprint."""
    mask = ndvi >= ndvi_min
    stems = []
    argmax = lambda win: np.unravel_index(np.argmax(win), win.shape)  # noqa: E731
    for x, y, _peak in _block_peaks(ndvi, mask, grid, spacing, argmax):
        if terrain_valid and not terrain_valid(x, y):
            continue
        z = float(elevation(x, y)) if elevation else grid["minElevation"]
        stems.append({"x": round(x, 3), "y": round(y, 3), "z": round(z, 2),
                      "height": round(float(nominal_height), 2), "confidence": 0.3,
                      "source": "ndvi"})
    return stems


def dtm_sample(dtm, grid, x, y):
    """Nearest-cell DTM elevation at a scene-local point."""
    h, w = dtm.shape
    ox0, ox1, oy0, oy1 = _outer(grid)
    col = min(w - 1, max(0, int((x - ox0) / (ox1 - ox0) * w)))
    row = min(h - 1, max(0, int((oy1 - y) / (oy1 - oy0) * h)))
    v = dtm[row, col]
    return v if np.isfinite(v) else grid["minElevation"]
