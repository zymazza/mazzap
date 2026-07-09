#!/usr/bin/env python3
"""Viewshed and horizon math for VEIL twins.

The core is a polar R2 sweep over a RingStack of scene-local rasters. It uses
GDAL's curvature/refraction convention: drop = (1-k) * d^2 / (2R).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
from typing import Any

import numpy as np


EARTH_RADIUS_M = 6_371_000.0
REFRACTION_K = {
    "optical": 1.0 / 7.0,
    "radio": 0.25,
    "radio_4_3": 0.25,
}
NODATA_I16 = -32768


def refraction_k(value: str | float | int | None = "optical") -> float:
    if value is None:
        return REFRACTION_K["optical"]
    if isinstance(value, str):
        key = value.strip().lower()
        if key in REFRACTION_K:
            return REFRACTION_K[key]
        try:
            return float(key)
        except ValueError as exc:
            raise ValueError(f"unknown refraction preset: {value!r}") from exc
    return float(value)


def curvature_drop_m(distance_m: np.ndarray | float, k: float = 1.0 / 7.0) -> np.ndarray | float:
    cc = 1.0 - float(k)
    return cc * np.asarray(distance_m) ** 2 / (2.0 * EARTH_RADIUS_M)


def _grid_steps(grid: dict[str, Any]) -> tuple[float, float]:
    xs = (float(grid["maxX"]) - float(grid["minX"])) / max(1, int(grid["width"]) - 1)
    ys = (float(grid["maxY"]) - float(grid["minY"])) / max(1, int(grid["height"]) - 1)
    return xs, ys


def merge_local_grids(base_grid: dict[str, Any], overlay_grid: dict[str, Any] | None) -> dict[str, Any]:
    """Composite overlay heights over base where the overlay is finite.

    The near field ships as two complementary grids: the apron (3DEP, valid in
    the frame around the parcel, nodata in the interior) and the parcel LiDAR
    (valid in the interior, nodata in the frame). They share bounds/shape, so
    compositing the parcel over the apron yields a fully-populated 3 m ring A;
    without this the parcel interior is nodata and the sweep falls through to
    the coarse 30 m distant ring. Mirrors the viewer's mergeApronUnderlayGrid.
    """
    if not overlay_grid:
        return base_grid
    b = base_grid.get("heights")
    o = overlay_grid.get("heights")
    if not b or not o or len(b) != len(o):
        return base_grid
    merged = dict(base_grid)
    merged["heights"] = [o[i] if o[i] is not None else b[i] for i in range(len(b))]
    return merged


def _decode_evh_vat(vat: dict[str, Any]) -> dict[int, float]:
    out: dict[int, float] = {}
    for raw, meta in vat.items():
        try:
            code = int(raw)
        except (TypeError, ValueError):
            continue
        name = str((meta or {}).get("name") or "")
        m = re.search(r"(?:Tree|Shrub|Herb(?:aceous)?)\s+Height\s*=\s*(\d+(?:\.\d+)?)\s*meter", name, re.I)
        out[code] = float(m.group(1)) if m else 0.0
    return out


def _nearest_grid_values(grid: dict[str, Any], xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    values = np.asarray(grid.get("values") or [], dtype=np.float32)
    if values.size == 0:
        return np.zeros(xs.shape, dtype=np.float32)
    minx, miny, maxx, maxy = [float(v) for v in grid["bounds_local"]]
    w = max(1e-9, maxx - minx)
    h = max(1e-9, maxy - miny)
    col = np.floor((xs - minx) / w * values.shape[1]).astype(np.int32)
    row = np.floor((maxy - ys) / h * values.shape[0]).astype(np.int32)
    inside = (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
    col = np.clip(col, 0, values.shape[1] - 1)
    row = np.clip(row, 0, values.shape[0] - 1)
    out = np.zeros(xs.shape, dtype=np.float32)
    out[inside] = values[row[inside], col[inside]]
    return out


def canopy_from_evh_grid(target_grid: dict[str, Any], evh_grid: dict[str, Any], vat: dict[str, Any]) -> np.ndarray:
    """Decode LANDFIRE EVH class codes to canopy-height metres on target cells."""
    mapping = _decode_evh_vat(vat)
    width, height = int(target_grid["width"]), int(target_grid["height"])
    xs_step, ys_step = _grid_steps(target_grid)
    cols = np.arange(width, dtype=np.float32)
    rows = np.arange(height, dtype=np.float32)
    xs = float(target_grid["minX"]) + cols[None, :] * xs_step
    ys = float(target_grid["maxY"]) - rows[:, None] * ys_step
    codes = _nearest_grid_values(evh_grid, np.broadcast_to(xs, (height, width)),
                                 np.broadcast_to(ys, (height, width))).astype(np.int32)
    canopy = np.zeros((height, width), dtype=np.float32)
    for code, metres in mapping.items():
        if metres > 0:
            canopy[codes == code] = metres
    return canopy


@dataclass
class Ring:
    name: str
    ground: np.ndarray
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    resolution_m: float
    canopy: np.ndarray | None = None
    inner_m: float = 0.0
    outer_m: float | None = None
    source: dict[str, Any] | None = None

    @property
    def height(self) -> int:
        return int(self.ground.shape[0])

    @property
    def width(self) -> int:
        return int(self.ground.shape[1])

    @property
    def x_step(self) -> float:
        return (self.max_x - self.min_x) / max(1, self.width - 1)

    @property
    def y_step(self) -> float:
        return (self.max_y - self.min_y) / max(1, self.height - 1)

    @property
    def cell_area_m2(self) -> float:
        return abs(self.x_step * self.y_step)

    def contains_xy(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return (x >= self.min_x) & (x <= self.max_x) & (y >= self.min_y) & (y <= self.max_y)

    def _bilinear(self, arr: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        x_arr = np.asarray(x, dtype=np.float32)
        y_arr = np.asarray(y, dtype=np.float32)
        out = np.full(x_arr.shape, np.nan, dtype=np.float32)
        inside = self.contains_xy(x_arr, y_arr)
        if not np.any(inside):
            return out
        xr = np.clip((x_arr[inside] - self.min_x) / max(1e-9, self.max_x - self.min_x), 0.0, 0.999999)
        yr = np.clip((y_arr[inside] - self.min_y) / max(1e-9, self.max_y - self.min_y), 0.0, 0.999999)
        xi = xr * (self.width - 1)
        yi = (1.0 - yr) * (self.height - 1)
        x0 = np.floor(xi).astype(np.int32)
        y0 = np.floor(yi).astype(np.int32)
        x1 = np.minimum(self.width - 1, x0 + 1)
        y1 = np.minimum(self.height - 1, y0 + 1)
        tx = xi - x0
        ty = yi - y0
        vals = np.stack([arr[y0, x0], arr[y0, x1], arr[y1, x0], arr[y1, x1]], axis=0)
        wgts = np.stack([(1 - tx) * (1 - ty), tx * (1 - ty), (1 - tx) * ty, tx * ty], axis=0)
        valid = np.isfinite(vals)
        denom = np.sum(np.where(valid, wgts, 0.0), axis=0)
        sampled = np.full(xi.shape, np.nan, dtype=np.float32)
        ok = denom > 0
        sampled[ok] = np.sum(np.where(valid, vals * wgts, 0.0), axis=0)[ok] / denom[ok]
        out[inside] = sampled
        return out

    def _nearest(self, arr: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        x_arr = np.asarray(x, dtype=np.float32)
        y_arr = np.asarray(y, dtype=np.float32)
        out = np.zeros(x_arr.shape, dtype=np.float32)
        inside = self.contains_xy(x_arr, y_arr)
        if not np.any(inside):
            return out
        xr = np.clip((x_arr[inside] - self.min_x) / max(1e-9, self.max_x - self.min_x), 0.0, 1.0)
        yr = np.clip((y_arr[inside] - self.min_y) / max(1e-9, self.max_y - self.min_y), 0.0, 1.0)
        col = np.rint(xr * (self.width - 1)).astype(np.int32)
        row = np.rint((1.0 - yr) * (self.height - 1)).astype(np.int32)
        out[inside] = arr[row, col]
        return out

    def sample_ground(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return self._bilinear(self.ground, x, y)

    def sample_canopy(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        if self.canopy is None:
            return np.zeros(np.asarray(x).shape, dtype=np.float32)
        return self._nearest(self.canopy, x, y)

    def rowcol_for_xy(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        x_arr = np.asarray(x)
        y_arr = np.asarray(y)
        inside = self.contains_xy(x_arr, y_arr)
        col = np.rint((x_arr - self.min_x) / max(1e-9, self.max_x - self.min_x) * (self.width - 1)).astype(np.int32)
        row = np.rint((self.max_y - y_arr) / max(1e-9, self.max_y - self.min_y) * (self.height - 1)).astype(np.int32)
        return np.clip(row, 0, self.height - 1), np.clip(col, 0, self.width - 1), inside

    def corner_distance(self, x: float, y: float) -> float:
        """Max distance from (x, y) to this ring's bounding-box corners."""
        return max(
            math.hypot(cx - float(x), cy - float(y))
            for cx in (self.min_x, self.max_x)
            for cy in (self.min_y, self.max_y)
        )

    @classmethod
    def from_grid(cls, name: str, grid: dict[str, Any], canopy: np.ndarray | None = None,
                  source: dict[str, Any] | None = None) -> "Ring":
        arr = np.asarray([np.nan if v is None else float(v) for v in grid["heights"]],
                         dtype=np.float32).reshape((int(grid["height"]), int(grid["width"])))
        xs, ys = _grid_steps(grid)
        outer = max(abs(float(grid["minX"])), abs(float(grid["maxX"])),
                    abs(float(grid["minY"])), abs(float(grid["maxY"]))) * math.sqrt(2.0)
        return cls(
            name=name,
            ground=arr,
            canopy=canopy.astype(np.float32) if canopy is not None else None,
            min_x=float(grid["minX"]),
            max_x=float(grid["maxX"]),
            min_y=float(grid["minY"]),
            max_y=float(grid["maxY"]),
            resolution_m=min(abs(xs), abs(ys)),
            outer_m=outer,
            source=source or {"ground": "data/terrain/grid.json"},
        )


class RingStack:
    def __init__(self, rings: list[Ring], manifest: dict[str, Any] | None = None,
                 manifest_hash: str | None = None):
        if not rings:
            raise ValueError("RingStack needs at least one ring")
        self.rings = sorted(rings, key=lambda r: r.resolution_m)
        self.manifest = manifest or {}
        self.manifest_hash = manifest_hash or self._hash_rings()

    def _hash_rings(self) -> str:
        h = hashlib.sha1()
        for ring in self.rings:
            h.update(ring.name.encode())
            h.update(str((ring.width, ring.height, ring.min_x, ring.max_x, ring.min_y, ring.max_y)).encode())
            h.update(np.nan_to_num(ring.ground, nan=NODATA_I16).astype(np.float32).tobytes())
            if ring.canopy is not None:
                h.update(ring.canopy.astype(np.float32).tobytes())
        return h.hexdigest()

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            min(r.min_x for r in self.rings), min(r.min_y for r in self.rings),
            max(r.max_x for r in self.rings), max(r.max_y for r in self.rings),
        )

    @property
    def max_distance_m(self) -> float:
        return max(float(r.outer_m or 0.0) for r in self.rings)

    @classmethod
    def from_local_files(cls, data_dir: str) -> "RingStack":
        apron_path = os.path.join(data_dir, "terrain", "grid.apron.json")
        parcel_path = os.path.join(data_dir, "terrain", "grid.json")
        terrain_path = apron_path if os.path.exists(apron_path) else parcel_path
        with open(terrain_path) as fh:
            grid = json.load(fh)
        if terrain_path == apron_path and os.path.exists(parcel_path):
            with open(parcel_path) as fh:
                grid = merge_local_grids(grid, json.load(fh))
        canopy = None
        evh_path = os.path.join(data_dir, "atlas", "local", "landfire_evh_2024.grid.json")
        vat_path = os.path.join(data_dir, "atlas", "vat", "landfire_evh_2024.json")
        if os.path.exists(evh_path) and os.path.exists(vat_path):
            with open(evh_path) as fh:
                evh = json.load(fh)
            with open(vat_path) as fh:
                vat = json.load(fh)
            canopy = canopy_from_evh_grid(grid, evh, vat)
        ring = Ring.from_grid("A", grid, canopy=canopy, source={
            "ground": os.path.relpath(terrain_path, data_dir),
            "canopy": os.path.relpath(evh_path, data_dir) if canopy is not None else None,
            "canopy_available": canopy is not None,
        })
        return cls([ring], manifest={"version": 1, "rings": [{"id": "A"}]})

    @classmethod
    def load(cls, manifest_path: str) -> "RingStack":
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        base = os.path.dirname(os.path.dirname(os.path.dirname(manifest_path)))
        rings: list[Ring] = []
        for item in manifest.get("rings", []):
            rid = item.get("id") or item.get("name")
            if item.get("kind") == "local_grid" or item.get("ground_grid"):
                ground_rel = item.get("ground_grid", "terrain/grid.apron.json")
                ground_abs = os.path.join(base, ground_rel)
                if not os.path.exists(ground_abs) and ground_rel == "terrain/grid.apron.json":
                    ground_abs = os.path.join(base, "terrain/grid.json")
                with open(ground_abs) as fh:
                    grid = json.load(fh)
                parcel_rel = item.get("parcel_grid")
                if parcel_rel is None and ground_rel == "terrain/grid.apron.json":
                    parcel_rel = "terrain/grid.json"
                parcel_abs = os.path.join(base, parcel_rel) if parcel_rel else None
                if parcel_abs and os.path.exists(parcel_abs) and os.path.abspath(parcel_abs) != os.path.abspath(ground_abs):
                    with open(parcel_abs) as fh:
                        grid = merge_local_grids(grid, json.load(fh))
                canopy = None
                if item.get("canopy_grid") and item.get("canopy_vat"):
                    canopy_grid_abs = os.path.join(base, item["canopy_grid"])
                    canopy_vat_abs = os.path.join(base, item["canopy_vat"])
                    if os.path.exists(canopy_grid_abs) and os.path.exists(canopy_vat_abs):
                        with open(canopy_grid_abs) as fh:
                            evh = json.load(fh)
                        with open(canopy_vat_abs) as fh:
                            vat = json.load(fh)
                        canopy = canopy_from_evh_grid(grid, evh, vat)
                rings.append(Ring.from_grid(rid, grid, canopy=canopy, source=item))
                continue
            width = int(item["width"])
            height = int(item["height"])
            ground = np.full((height, width), np.nan, dtype=np.float32)
            canopy = np.zeros((height, width), dtype=np.float32)
            tile_size = int(item.get("tile_size", 256))
            for tile in item.get("tiles", []):
                i, j = int(tile["i"]), int(tile["j"])
                rows = slice(j * tile_size, min(height, (j + 1) * tile_size))
                cols = slice(i * tile_size, min(width, (i + 1) * tile_size))
                shape = (rows.stop - rows.start, cols.stop - cols.start)
                gpath = os.path.join(base, tile["ground"])
                g = np.fromfile(gpath, dtype="<i2", count=shape[0] * shape[1]).reshape(shape)
                ground[rows, cols] = np.where(g == NODATA_I16, np.nan, g.astype(np.float32) / 10.0)
                if tile.get("canopy"):
                    cpath = os.path.join(base, tile["canopy"])
                    c = np.fromfile(cpath, dtype=np.uint8, count=shape[0] * shape[1]).reshape(shape)
                    canopy[rows, cols] = c.astype(np.float32) / 10.0
            minx, miny, maxx, maxy = [float(v) for v in item["bounds_local"]]
            rings.append(Ring(
                rid, ground, minx, maxx, miny, maxy,
                float(item.get("resolution_m") or (maxx - minx) / max(1, width - 1)),
                canopy=canopy if np.any(canopy > 0) else None,
                inner_m=float(item.get("inner_m") or 0.0),
                outer_m=float(item.get("outer_m") or max(abs(minx), abs(maxx), abs(miny), abs(maxy))),
                source=item,
            ))
        raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
        return cls(rings, manifest=manifest, manifest_hash=hashlib.sha1(raw).hexdigest())

    def sample(self, x: Any, y: Any, surface: str = "canopy") -> np.ndarray:
        ground, canopy = self.sample_components(x, y)
        return ground + (canopy if surface == "canopy" else 0.0)

    def sample_components(self, x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        x_arr = np.asarray(x, dtype=np.float32)
        y_arr = np.asarray(y, dtype=np.float32)
        ground = np.full(x_arr.shape, np.nan, dtype=np.float32)
        canopy = np.zeros(x_arr.shape, dtype=np.float32)
        for ring in self.rings:
            missing = ~np.isfinite(ground)
            if not np.any(missing):
                break
            g = ring.sample_ground(x_arr, y_arr)
            ok = missing & np.isfinite(g)
            if np.any(ok):
                ground[ok] = g[ok]
                canopy[ok] = ring.sample_canopy(x_arr, y_arr)[ok]
        return ground, canopy

    def radial_distances(self, max_m: float | None = None,
                         observer: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
        """Radial sample ladder measured from the observer outward.

        Ring inner/outer extents in the manifest are measured from the scene
        origin; convert them to observer-relative distances so an off-origin
        observer still samples every ring across its true annulus (and the
        reported analyzed extent is the observer's, not the origin's).
        """
        ox, oy = float(observer[0]), float(observer[1])
        r_obs = math.hypot(ox, oy)
        default_cap = max(r.corner_distance(ox, oy) for r in self.rings)
        cap = float(max_m if max_m is not None else default_cap)
        starts = []
        for ring in sorted(self.rings, key=lambda r: r.inner_m):
            inner = max(0.0, float(ring.inner_m or 0.0) - r_obs)
            outer = min(cap, ring.corner_distance(ox, oy))
            if outer <= inner:
                continue
            step = max(1.0, float(ring.resolution_m))
            starts.append(np.arange(max(step * 0.5, inner + step * 0.5), outer + step * 0.25, step, dtype=np.float32))
        if not starts:
            return np.asarray([], dtype=np.float32)
        d = np.unique(np.concatenate(starts))
        return d[d <= cap]

    def masks_from_samples(self, xs: np.ndarray, ys: np.ndarray, visible: np.ndarray) -> dict[str, np.ndarray]:
        masks = {ring.name: np.zeros((ring.height, ring.width), dtype=np.uint8) for ring in self.rings}
        flat_vis = np.asarray(visible).ravel()
        flat_x = np.asarray(xs).ravel()[flat_vis]
        flat_y = np.asarray(ys).ravel()[flat_vis]
        if flat_x.size == 0:
            return masks
        for ring in self.rings:
            row, col, inside = ring.rowcol_for_xy(flat_x, flat_y)
            if np.any(inside):
                masks[ring.name][row[inside], col[inside]] = 1
        return masks

    def mask_contains(self, masks: dict[str, np.ndarray], x: float, y: float) -> bool:
        xa = np.asarray([x], dtype=np.float32)
        ya = np.asarray([y], dtype=np.float32)
        for ring in self.rings:
            mask = masks.get(ring.name)
            if mask is None:
                continue
            row, col, inside = ring.rowcol_for_xy(xa, ya)
            if bool(inside[0]):
                return bool(mask[int(row[0]), int(col[0])])
        return False


def nearest_valid_point(stack: RingStack, prefer: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
    px, py = prefer
    best = None
    for ring in stack.rings:
        rows, cols = np.nonzero(np.isfinite(ring.ground))
        if rows.size == 0:
            continue
        xs = ring.min_x + cols * ring.x_step
        ys = ring.max_y - rows * ring.y_step
        d2 = (xs - px) ** 2 + (ys - py) ** 2
        i = int(np.argmin(d2))
        cand = (float(d2[i]), float(xs[i]), float(ys[i]))
        if best is None or cand[0] < best[0]:
            best = cand
    if best is None:
        raise ValueError("stack contains no valid terrain cells")
    return best[1], best[2]


def _normalize_surface(surface: str | None) -> str:
    s = (surface or "canopy").strip().lower()
    if s not in {"canopy", "bare_earth"}:
        raise ValueError("surface must be canopy or bare_earth")
    return s


def _classify_visible_cells(stack: RingStack, x: float, y: float, eye_z: float,
                            running: np.ndarray, distances: np.ndarray,
                            n_az: int, kval: float, target_agl_m: float) -> tuple[dict[str, np.ndarray], float]:
    """Classify every ring cell against the per-azimuth running horizon.

    Marking only ray *samples* leaves angular gaps beyond r = n_az*res/(2*pi):
    cross-ray spacing outgrows the cell size and visible cells between rays are
    silently missed, undercounting visible area and misreporting hidden_from
    regions at range. Instead, each cell is tested directly: nearest azimuth
    bin, horizon accumulated over samples strictly nearer than the cell, same
    curvature/refraction drop and epsilon as the sample test.
    """
    masks: dict[str, np.ndarray] = {}
    max_visible_m = 0.0
    az_step = 2.0 * math.pi / float(n_az)
    neg_inf = np.float32(-1e30)
    eps = np.float32(1e-7)
    for ring in stack.rings:
        mask = np.zeros((ring.height, ring.width), dtype=np.uint8)
        dx_cols = (ring.min_x + np.arange(ring.width, dtype=np.float32) * np.float32(ring.x_step)) - np.float32(x)
        cap = float(distances[-1]) + 0.5 * float(ring.resolution_m)
        step_back = np.float32(0.5 * ring.resolution_m)
        # Chunk rows so the biggest rings stay within a bounded working set.
        chunk = max(1, 4_000_000 // max(1, ring.width))
        for r0 in range(0, ring.height, chunk):
            r1 = min(ring.height, r0 + chunk)
            dy_rows = (ring.max_y - np.arange(r0, r1, dtype=np.float32) * np.float32(ring.y_step)) - np.float32(y)
            d = np.hypot(dx_cols[None, :], dy_rows[:, None])
            ground = ring.ground[r0:r1, :]
            ok = np.isfinite(ground) & (d <= cap)
            if not np.any(ok):
                continue
            az_idx = np.mod(np.rint(np.arctan2(dx_cols[None, :], dy_rows[:, None]) / az_step).astype(np.int64), n_az)
            # Last sample strictly nearer than the cell (excluding its own cell).
            di = (np.searchsorted(distances, (d - step_back).ravel(), side="right") - 1).reshape(d.shape)
            prior = np.full(d.shape, neg_inf, dtype=np.float32)
            has_prior = di >= 0
            prior[has_prior] = running[az_idx[has_prior], di[has_prior]]
            drop = curvature_drop_m(d, kval).astype(np.float32)
            with np.errstate(invalid="ignore"):
                t_angle = np.arctan2(ground + np.float32(target_agl_m) - drop - np.float32(eye_z),
                                     np.maximum(d, np.float32(0.01)))
            vis = ok & (t_angle > prior + eps)
            mask[r0:r1, :][vis] = 1
            if np.any(vis):
                max_visible_m = max(max_visible_m, float(np.max(d[vis])))
        masks[ring.name] = mask
    return masks, max_visible_m


def sweep(stack: RingStack, x: float, y: float, agl_m: float, n_az: int = 1440,
          max_km: float | None = None, surface: str = "canopy",
          k: str | float = 1.0 / 7.0, target_agl_m: float = 0.0,
          cell_classify: bool = True) -> dict[str, Any]:
    surface = _normalize_surface(surface)
    kval = refraction_k(k)
    max_m = None if max_km is None else max(0.0, float(max_km) * 1000.0)
    distances = stack.radial_distances(max_m, observer=(float(x), float(y)))
    if distances.size == 0:
        raise ValueError("no radial samples inside stack extent")

    obs_ground = stack.sample_components(np.asarray([x]), np.asarray([y]))[0][0]
    if not np.isfinite(obs_ground):
        raise ValueError("observer point is outside available terrain")
    eye_z = float(obs_ground) + float(agl_m)

    az = np.arange(int(n_az), dtype=np.float32) * (2.0 * np.pi / float(n_az))
    sin_az = np.sin(az)[:, None]
    cos_az = np.cos(az)[:, None]
    d = distances[None, :]
    xs = np.float32(x) + sin_az * d
    ys = np.float32(y) + cos_az * d
    ground, canopy = stack.sample_components(xs, ys)
    blocker_z = ground + (canopy if surface == "canopy" else 0.0)
    drop = curvature_drop_m(d, kval).astype(np.float32)
    blocker_angle = np.arctan2(blocker_z - drop - eye_z, d)
    finite_block = np.isfinite(blocker_angle)
    neg_inf = np.float32(-1e30)
    blocker_safe = np.where(finite_block, blocker_angle, neg_inf)
    running = np.maximum.accumulate(blocker_safe, axis=1)
    if cell_classify:
        masks, max_visible_m = _classify_visible_cells(
            stack, float(x), float(y), eye_z, running, distances, int(n_az), kval, float(target_agl_m))
    else:
        # Legacy fast path: mark only ray samples (undercounts at range; kept
        # for cheap relative estimates, never for area/hidden_from truth).
        target_z = ground + float(target_agl_m)
        target_angle = np.arctan2(target_z - drop - eye_z, d)
        prev = np.empty_like(running)
        prev[:, 0] = neg_inf
        prev[:, 1:] = running[:, :-1]
        visible = np.isfinite(target_angle) & (target_angle > prev + np.float32(1e-7))
        masks = stack.masks_from_samples(xs, ys, visible)
        vis_d = np.broadcast_to(distances[None, :], visible.shape)[visible]
        max_visible_m = float(np.max(vis_d)) if vis_d.size else 0.0
    horizon_rad = np.max(np.where(finite_block, blocker_angle, neg_inf), axis=1)
    horizon_rad = np.where(horizon_rad < -1e20, np.nan, horizon_rad)
    horizon_deg = np.degrees(horizon_rad).astype(np.float32)

    per_ring = {}
    total_area = 0.0
    for ring in stack.rings:
        mask = masks[ring.name]
        valid = np.isfinite(ring.ground)
        visible_cells = int(np.count_nonzero(mask & valid))
        valid_cells = int(np.count_nonzero(valid))
        area = visible_cells * ring.cell_area_m2
        total_area += area
        per_ring[ring.name] = {
            "visible_cells": visible_cells,
            "valid_cells": valid_cells,
            "fraction": 0.0 if valid_cells == 0 else visible_cells / valid_cells,
            "visible_km2": area / 1_000_000.0,
            "resolution_m": ring.resolution_m,
            "canopy_available": ring.canopy is not None,
        }
    return {
        "visible": masks,
        "horizon_deg": horizon_deg,
        "azimuth_deg": (np.arange(int(n_az), dtype=np.float32) * (360.0 / float(n_az))),
        "mask_mode": "cell_classified" if cell_classify else "ray_sampled",
        "stats": {
            "visible_km2": total_area / 1_000_000.0,
            "max_visible_km": max_visible_m / 1000.0,
            "sky_open_fraction_ge_2deg": float(np.count_nonzero(horizon_deg <= 2.0) / len(horizon_deg)),
            "per_ring": per_ring,
            "analyzed_extent_km": float(distances[-1] / 1000.0),
            "samples_per_azimuth": int(distances.size),
        },
        "surface": surface,
        "k": kval,
        "cc": 1.0 - kval,
        "observer": {"x": float(x), "y": float(y), "ground_m": float(obs_ground), "agl_m": float(agl_m)},
        "target_agl_m": float(target_agl_m),
        "manifest_hash": stack.manifest_hash,
    }


def line_of_sight(stack: RingStack, x0: float, y0: float, agl0: float,
                  x1: float, y1: float, agl1: float = 0.0,
                  k: str | float = "optical", surface: str = "canopy") -> dict[str, Any]:
    surface = _normalize_surface(surface)
    kval = refraction_k(k)
    dx, dy = float(x1) - float(x0), float(y1) - float(y0)
    total = math.hypot(dx, dy)
    if total <= 0:
        raise ValueError("line_of_sight target must differ from observer")
    if total > stack.max_distance_m + max(r.resolution_m for r in stack.rings):
        return {
            "error": "needs_fetch",
            "distance_km": total / 1000.0,
            "analyzed_extent_km": stack.max_distance_m / 1000.0,
            "message": "target lies beyond the available viewshed terrain; fetch distant terrain before answering visibility",
        }
    dists = stack.radial_distances(total, observer=(float(x0), float(y0)))
    dists = dists[(dists > 0) & (dists < total)]
    if dists.size == 0:
        dists = np.asarray([total * 0.5], dtype=np.float32)
    ux, uy = dx / total, dy / total
    xs = float(x0) + ux * dists
    ys = float(y0) + uy * dists
    g0 = stack.sample_components(np.asarray([x0]), np.asarray([y0]))[0][0]
    g1 = stack.sample_components(np.asarray([x1]), np.asarray([y1]))[0][0]
    if not np.isfinite(g0) or not np.isfinite(g1):
        return {"error": "needs_fetch", "message": "observer or target is outside available terrain"}
    eye = float(g0) + float(agl0)
    target = float(g1) + float(agl1)
    ground, canopy = stack.sample_components(xs, ys)
    blocker = ground + (canopy if surface == "canopy" else 0.0)
    drops = curvature_drop_m(dists, kval).astype(np.float32)
    target_drop = float(curvature_drop_m(total, kval))
    blocker_corr = blocker - drops
    target_corr = target - target_drop
    line_z = eye + (target_corr - eye) * (dists / total)
    deficits = blocker_corr - line_z
    valid = np.isfinite(deficits)
    if not np.any(valid):
        return {"visible": True, "distance_km": total / 1000.0, "surface": surface, "k": kval}
    worst_i = int(np.nanargmax(np.where(valid, deficits, -np.inf)))
    max_deficit = float(deficits[worst_i])
    visible = max_deficit <= 0.0
    required_raise = max(0.0, max_deficit / max(1e-6, 1.0 - float(dists[worst_i]) / total))
    obs = {
        "x": float(xs[worst_i]),
        "y": float(ys[worst_i]),
        "dist_m": float(dists[worst_i]),
        "crest_z": float(blocker[worst_i]),
        "ground_z": float(ground[worst_i]),
        "canopy_m": float(canopy[worst_i]),
        "sightline_z": float(line_z[worst_i] + drops[worst_i]),
        "deficit_m": max(0.0, max_deficit),
        "is_canopy": bool(surface == "canopy" and canopy[worst_i] > 0.1),
    }
    return {
        "visible": bool(visible),
        "distance_km": total / 1000.0,
        "bearing_deg": (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0,
        "obstruction": None if visible else obs,
        "required_agl0_m": float(agl0) + required_raise,
        "clearance_deficit_m": 0.0 if visible else max_deficit,
        "surface": surface,
        "k": kval,
        "cc": 1.0 - kval,
        "manifest_hash": stack.manifest_hash,
    }


def union_sweep(stack: RingStack, points: list[tuple[float, float]], agl_m: float,
                **kwargs) -> dict[str, np.ndarray]:
    out = {ring.name: np.zeros((ring.height, ring.width), dtype=np.uint8) for ring in stack.rings}
    for x, y in points:
        result = sweep(stack, x, y, agl_m, **kwargs)
        for key, mask in result["visible"].items():
            out[key] |= mask
    return out


def horizon_at_azimuth(horizon_deg: np.ndarray, azimuth_deg: float) -> float:
    arr = np.asarray(horizon_deg, dtype=np.float32)
    if arr.size == 0:
        return float("nan")
    pos = ((float(azimuth_deg) % 360.0) / 360.0) * arr.size
    i0 = int(math.floor(pos)) % arr.size
    i1 = (i0 + 1) % arr.size
    t = pos - math.floor(pos)
    return float(arr[i0] * (1.0 - t) + arr[i1] * t)


def horizon_events(horizon_deg: np.ndarray, sun_path: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    current = None
    for row in sun_path:
        blocked = float(row.get("altitude_deg", -90.0)) < horizon_at_azimuth(horizon_deg, row.get("azimuth_deg", 0.0))
        if current is None or current["blocked"] != blocked:
            if current is not None:
                current["end"] = row.get("time")
                events.append(current)
            current = {"blocked": blocked, "begin": row.get("time")}
    if current is not None:
        current["end"] = sun_path[-1].get("time") if sun_path else None
        events.append(current)
    return events


def geo_arc_elevations(lat_deg: float, n_az: int = 72) -> list[dict[str, float]]:
    phi = math.radians(float(lat_deg))
    r_earth = 6371.0
    r_geo = 42164.0
    bins: list[list[float]] = [[] for _ in range(n_az)]
    for dlon_deg in np.linspace(-90.0, 90.0, 721):
        dl = math.radians(float(dlon_deg))
        site = np.asarray([r_earth * math.cos(phi), 0.0, r_earth * math.sin(phi)])
        sat = np.asarray([r_geo * math.cos(dl), r_geo * math.sin(dl), 0.0])
        vec = sat - site
        east = vec[1]
        north = -math.sin(phi) * vec[0] + math.cos(phi) * vec[2]
        up = math.cos(phi) * vec[0] + math.sin(phi) * vec[2]
        az = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
        el = math.degrees(math.atan2(up, math.hypot(east, north)))
        idx = int(round(az / 360.0 * n_az)) % n_az
        bins[idx].append(el)
    out = []
    for i, vals in enumerate(bins):
        out.append({
            "azimuth_deg": i * 360.0 / n_az,
            "elevation_deg": max(vals) if vals else float("nan"),
        })
    return out


def compact_horizon(horizon_deg: np.ndarray, count: int = 72) -> list[float]:
    arr = np.asarray(horizon_deg, dtype=np.float32)
    if arr.size == count:
        return [round(float(v), 3) for v in arr]
    idx = np.linspace(0, arr.size, count, endpoint=False)
    return [round(horizon_at_azimuth(arr, float(i) * 360.0 / count), 3) for i in range(count)]
