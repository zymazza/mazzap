#!/usr/bin/env python3
"""Branchable, non-destructive land plans for VEIL.

The twin store owns immutable plan metadata and edit snapshots.  Large terrain
and vegetation payloads follow VEIL's existing raster/model convention: they
are content-addressed files registered by hash and can be regenerated from a
pinned base plus a revision's canonical edits.

All coordinates are scene-local metres (x=east, y=north).  A revision never
mutates the baseline twin and every materialized path is namespaced by the
revision content hash.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable

import numpy as np

import twin_store
from twin_store import SHRUB_ATTRS, TREE_ATTRS, Store

try:  # Linux/macOS advisory lock; tests also run on Linux.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback remains single-process
    fcntl = None


ALLOWED_EDIT_KINDS = {
    "terrain_cut",
    "terrain_fill",
    "vegetation_remove",
    "vegetation_add",
    "vegetation_add_brush",
    "swale",
    "orchard",
    "garden",
}
MAX_EDITS = 5000
MAX_COORDINATES = 100_000
MAX_BRUSH_RADIUS_M = 500.0
MAX_EARTH_DELTA_M = 30.0
MAX_PLANTS_PER_EDIT = 10_000
MAX_TERRAIN_ACCUMULATION_STAMPS = 10_000
MAX_VEGETATION_REMOVAL_IDS = 100_000
MATERIALIZER_VERSION = 2

BASE_FILES = (
    "terrain/grid.json",
    "terrain/grid.apron.json",
    "terrain/aoi_local.geojson",
    "georef.json",
    "vegetation/tree_instances.json",
    "vegetation/shrub_points.json",
    "vegetation/metadata.json",
    "scene.json",
    "pack.txt",
)


class PlanError(RuntimeError):
    def __init__(self, message: str, *, code: str = "plan_error", **detail: Any):
        super().__init__(message)
        self.payload = {"error": code, "message": message, **detail}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_json(path: Path, value: Any, *, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=indent,
                      separators=None if indent else (",", ":"))
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return default


def _finite(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{name} must be a finite number", code="invalid_edit") from exc
    if not math.isfinite(out):
        raise PlanError(f"{name} must be a finite number", code="invalid_edit")
    return out


def _round_coord(value: Any) -> float:
    return round(_finite(value, "coordinate"), 3)


def _normalize_coordinates(value: Any, counter: list[int]) -> Any:
    if not isinstance(value, list):
        raise PlanError("geometry coordinates must be arrays", code="invalid_geometry")
    if value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value[:2]):
        if len(value) < 2:
            raise PlanError("coordinate needs x and y", code="invalid_geometry")
        counter[0] += 1
        if counter[0] > MAX_COORDINATES:
            raise PlanError("edit has too many coordinates", code="edit_too_large")
        return [_round_coord(value[0]), _round_coord(value[1])]
    return [_normalize_coordinates(item, counter) for item in value]


def normalize_geometry(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PlanError("geometry must be GeoJSON", code="invalid_geometry")
    kind = value.get("type")
    if kind not in {"Point", "MultiPoint", "LineString", "Polygon", "MultiPolygon"}:
        raise PlanError("unsupported plan geometry", code="invalid_geometry", geometry_type=kind)
    coordinates = _normalize_coordinates(value.get("coordinates"), [0])
    if kind == "Point" and (not isinstance(coordinates, list) or len(coordinates) < 2):
        raise PlanError("point geometry is empty", code="invalid_geometry")
    if kind == "LineString" and len(coordinates) < 2:
        raise PlanError("line geometry needs at least two points", code="invalid_geometry")
    if kind == "Polygon" and (not coordinates or len(coordinates[0]) < 3):
        raise PlanError("polygon geometry needs at least three vertices", code="invalid_geometry")
    return {"type": kind, "coordinates": coordinates}


def _normalize_accumulation_stamps(value: Any) -> list[list[float]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanError("terrain accumulation stamps must be an array", code="invalid_edit")
    if len(value) > MAX_TERRAIN_ACCUMULATION_STAMPS:
        raise PlanError("terrain gesture has too many accumulation stamps",
                        code="edit_too_large",
                        maximum=MAX_TERRAIN_ACCUMULATION_STAMPS)
    output: list[list[float]] = []
    for stamp in value:
        if not isinstance(stamp, list) or len(stamp) < 3:
            raise PlanError("terrain accumulation stamp needs x, y, and strength",
                            code="invalid_edit")
        x = _round_coord(stamp[0])
        y = _round_coord(stamp[1])
        strength = _finite(stamp[2], "accumulation stamp strength")
        if strength <= 0 or strength > 10:
            raise PlanError("terrain accumulation stamp strength must be between 0 and 10",
                            code="invalid_edit")
        output.append([x, y, round(strength, 4)])
    return output


def _geometry_points(geometry: dict[str, Any] | None) -> list[list[float]]:
    if not geometry:
        return []
    kind = geometry["type"]
    coords = geometry["coordinates"]
    if kind == "Point":
        return [coords]
    if kind in {"MultiPoint", "LineString"}:
        return list(coords)
    if kind == "Polygon":
        return [p for ring in coords for p in ring]
    return [p for poly in coords for ring in poly for p in ring]


def _geometry_paths(geometry: dict[str, Any] | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    kind = geometry["type"]
    coords = geometry["coordinates"]
    if kind == "Point":
        return [[coords]]
    if kind == "MultiPoint":
        return [[point] for point in coords]
    if kind == "LineString":
        return [coords]
    if kind == "Polygon":
        return coords
    return [ring for polygon in coords for ring in polygon]


def _point_in_ring_scalar(x: float, y: float, ring: list[list[float]]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = point[:2]
        xj, yj = ring[j][:2]
        if ((yi > y) != (yj > y)) and \
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _point_in_polygon_coordinates(x: float, y: float,
                                  polygon: list[list[list[float]]]) -> bool:
    return bool(polygon and _point_in_ring_scalar(x, y, polygon[0])
                and not any(_point_in_ring_scalar(x, y, hole) for hole in polygon[1:]))


def _aoi_polygons(document: Any) -> list[list[list[list[float]]]]:
    polygons: list[list[list[list[float]]]] = []
    if not isinstance(document, dict):
        return polygons
    if document.get("type") == "FeatureCollection":
        geometries = [(feature or {}).get("geometry") or {}
                      for feature in document.get("features") or []]
    elif document.get("type") == "Feature":
        geometries = [document.get("geometry") or {}]
    else:
        geometries = [document]
    for geometry in geometries:
        if geometry.get("type") == "Polygon":
            polygons.append(geometry.get("coordinates") or [])
        elif geometry.get("type") == "MultiPolygon":
            polygons.extend(geometry.get("coordinates") or [])
    return polygons


def _point_segment_distance(px: float, py: float, ax: float, ay: float,
                            bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _distance_to_path(px: float, py: float, points: list[list[float]]) -> float:
    if not points:
        return math.inf
    if len(points) == 1:
        return math.hypot(px - points[0][0], py - points[0][1])
    return min(_point_segment_distance(px, py, *a, *b) for a, b in zip(points, points[1:]))


def _hash_unit(text: str) -> float:
    return int(hashlib.sha1(text.encode()).hexdigest()[:12], 16) / float(0xFFFFFFFFFFFF)


def _brush_points(edit_id: str, geometry: dict[str, Any], params: dict[str, Any]) -> list[list[float]]:
    """Deterministic, spacing-respecting scatter for an add-vegetation brush."""
    path = _geometry_points(geometry)
    if not path:
        return []
    radius = min(MAX_BRUSH_RADIUS_M, max(0.1, _finite(params.get("radius_m", 8), "radius_m")))
    spacing = max(0.5, _finite(params.get("spacing_m", 5), "spacing_m"))
    minx = min(p[0] for p in path) - radius
    maxx = max(p[0] for p in path) + radius
    miny = min(p[1] for p in path) - radius
    maxy = max(p[1] for p in path) + radius
    origin_x = math.floor(minx / spacing) * spacing
    origin_y = math.floor(miny / spacing) * spacing
    points: list[list[float]] = []
    rows = int(math.ceil((maxy - origin_y) / spacing)) + 1
    cols = int(math.ceil((maxx - origin_x) / spacing)) + 1
    if rows * cols > MAX_PLANTS_PER_EDIT * 20:
        raise PlanError("planting brush is too large", code="edit_too_large")
    for row in range(rows):
        for col in range(cols):
            key = f"{edit_id}:{row}:{col}"
            jitter_x = (_hash_unit(key + ":x") - 0.5) * spacing * 0.5
            jitter_y = (_hash_unit(key + ":y") - 0.5) * spacing * 0.5
            x = origin_x + col * spacing + jitter_x
            y = origin_y + row * spacing + jitter_y
            if x < minx or x > maxx or y < miny or y > maxy:
                continue
            if _distance_to_path(x, y, path) <= radius:
                points.append([round(x, 3), round(y, 3)])
                if len(points) > MAX_PLANTS_PER_EDIT:
                    raise PlanError("planting brush creates too many plants", code="edit_too_large")
    return points


def _polygon_planting_points(edit_id: str, geometry: dict[str, Any],
                             params: dict[str, Any]) -> list[list[float]]:
    """Deterministically fill an orchard polygon at the requested spacing."""
    polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                else geometry["coordinates"])
    spacing = max(0.5, _finite(params.get("spacing_m", 6), "spacing_m"))
    all_points = [point for polygon in polygons for ring in polygon for point in ring]
    if not all_points:
        return []
    min_x, max_x = min(p[0] for p in all_points), max(p[0] for p in all_points)
    min_y, max_y = min(p[1] for p in all_points), max(p[1] for p in all_points)
    origin_x = math.floor(min_x / spacing) * spacing
    origin_y = math.floor(min_y / spacing) * spacing
    rows = int(math.ceil((max_y - origin_y) / spacing)) + 1
    cols = int(math.ceil((max_x - origin_x) / spacing)) + 1
    if rows * cols > MAX_PLANTS_PER_EDIT * 20:
        raise PlanError("orchard polygon is too large", code="edit_too_large")
    output: list[list[float]] = []
    for row in range(rows):
        for col in range(cols):
            key = f"{edit_id}:orchard:{row}:{col}"
            x = origin_x + col * spacing + (_hash_unit(key + ":x") - 0.5) * spacing * 0.25
            y = origin_y + row * spacing + (_hash_unit(key + ":y") - 0.5) * spacing * 0.25
            if any(_point_in_polygon_coordinates(x, y, polygon) for polygon in polygons):
                output.append([round(x, 3), round(y, 3)])
                if len(output) > MAX_PLANTS_PER_EDIT:
                    raise PlanError("orchard creates too many plants", code="edit_too_large")
    return output


def normalize_edit(raw: Any, ordinal: int = 0) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlanError("every edit must be an object", code="invalid_edit")
    kind = str(raw.get("kind") or "")
    if kind not in ALLOWED_EDIT_KINDS:
        raise PlanError("unsupported edit kind", code="invalid_edit", kind=kind,
                        valid_kinds=sorted(ALLOWED_EDIT_KINDS))
    edit_id = str(raw.get("edit_id") or ("edit_" + uuid.uuid4().hex[:16]))
    if (not edit_id.startswith("edit_") or len(edit_id) > 80
            or any(not (character.isalnum() or character in "_.:-")
                   for character in edit_id)):
        raise PlanError("invalid edit_id", code="invalid_edit")
    geometry = normalize_geometry(raw.get("geometry"))
    params = dict(raw.get("params") or {})
    if kind in {"terrain_cut", "terrain_fill", "swale"}:
        if geometry is None:
            raise PlanError("terrain edit needs geometry", code="invalid_edit")
        radius = params.get("radius_m", params.get("width_m", 8.0) / 2.0)
        params["radius_m"] = round(min(MAX_BRUSH_RADIUS_M, max(0.1, _finite(radius, "radius_m"))), 3)
        amount_key = "depth_m" if kind in {"terrain_cut", "swale"} else "height_m"
        fallback = params.get("delta_m", 0.3)
        params[amount_key] = round(min(MAX_EARTH_DELTA_M, max(0.0, abs(_finite(params.get(amount_key, fallback), amount_key)))), 3)
        falloff = str(params.get("falloff") or "smoothstep")
        params["falloff"] = falloff if falloff in {"hard", "linear", "smoothstep"} else "smoothstep"
        if "accumulation_stamps" in params:
            stamps = _normalize_accumulation_stamps(params.get("accumulation_stamps"))
            if stamps:
                if geometry.get("type") not in {"Point", "LineString"}:
                    raise PlanError("terrain accumulation requires a point or line gesture",
                                    code="invalid_edit")
                path = _geometry_points(geometry)
                maximum_offset = params["radius_m"] + 0.5
                if any(_distance_to_path(stamp[0], stamp[1], path) > maximum_offset
                       for stamp in stamps):
                    raise PlanError("terrain accumulation stamp is detached from its gesture",
                                    code="invalid_edit")
                params["accumulation_stamps"] = stamps
            else:
                params.pop("accumulation_stamps", None)
    elif kind == "garden":
        if geometry is None or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise PlanError("garden needs polygon geometry", code="invalid_edit")
        params["height_m"] = round(min(MAX_EARTH_DELTA_M, max(0.0, _finite(params.get("height_m", 0), "height_m"))), 3)
        params["edge_falloff_m"] = round(min(50.0, max(0.0, _finite(params.get("edge_falloff_m", 1), "edge_falloff_m"))), 3)
    elif kind == "vegetation_remove":
        ids = params.get("entity_ids") or []
        if not isinstance(ids, list) or len(ids) > MAX_VEGETATION_REMOVAL_IDS:
            raise PlanError("vegetation removal entity_ids must be a bounded list", code="invalid_edit")
        params["entity_ids"] = sorted({str(v) for v in ids if str(v)})
        kinds = params.get("kinds", ["tree", "shrub"])
        if not isinstance(kinds, list):
            raise PlanError("vegetation removal kinds must be an array", code="invalid_edit")
        params["kinds"] = sorted({str(v) for v in kinds
                                   if str(v) in {"tree", "shrub"}})
        if not params["kinds"]:
            raise PlanError("vegetation removal needs tree and/or shrub kinds",
                            code="invalid_edit")
        for distance_key in ("buffer_m", "distance_m"):
            if distance_key not in params:
                continue
            distance = _finite(params[distance_key], distance_key)
            if distance <= 0:
                raise PlanError(f"{distance_key} must be greater than zero",
                                code="invalid_edit")
            params[distance_key] = round(min(MAX_BRUSH_RADIUS_M, distance), 3)
    elif kind in {"vegetation_add", "vegetation_add_brush", "orchard"}:
        habit = str(params.get("habit") or params.get("kind") or "tree")
        if habit not in {"tree", "shrub"}:
            raise PlanError("planned vegetation habit must be tree or shrub", code="invalid_edit")
        species = str(params.get("species") or "").strip()
        if not species:
            raise PlanError("planned vegetation requires a species", code="invalid_edit")
        params["habit"] = habit
        params["species"] = species[:160]
        params["type"] = "deciduous" if params.get("type") == "deciduous" else "evergreen"
        params["height"] = round(max(0.2, min(80.0, _finite(params.get("height", 3 if habit == "tree" else 1.2), "height"))), 3)
        params["radius"] = round(max(0.1, min(30.0, _finite(params.get("radius", 1.5 if habit == "tree" else 0.7), "radius"))), 3)
        if kind == "orchard" and geometry is not None \
                and geometry.get("type") in {"Polygon", "MultiPolygon"}:
            params["spacing_m"] = round(max(0.5, min(100.0, _finite(
                params.get("spacing_m", 6), "spacing_m"))), 3)
            geometry = {"type": "MultiPoint",
                        "coordinates": _polygon_planting_points(edit_id, geometry, params)}
            if not geometry["coordinates"]:
                raise PlanError("orchard polygon is too small for its spacing",
                                code="invalid_edit")
        elif kind == "vegetation_add_brush" or (
                kind == "orchard" and geometry is not None
                and geometry.get("type") == "LineString"):
            if geometry is None:
                raise PlanError("planting brush needs geometry", code="invalid_edit")
            params["spacing_m"] = round(max(0.5, min(100.0, _finite(params.get("spacing_m", 5), "spacing_m"))), 3)
            params["radius_m"] = round(max(0.1, min(MAX_BRUSH_RADIUS_M, _finite(params.get("radius_m", 8), "radius_m"))), 3)
            geometry = {"type": "MultiPoint", "coordinates": _brush_points(edit_id, geometry, params)}
            if kind == "vegetation_add_brush":
                kind = "vegetation_add"
        elif geometry is None or geometry.get("type") not in {"Point", "MultiPoint"}:
            raise PlanError("planned vegetation needs point geometry", code="invalid_edit")
        if len(_geometry_points(geometry)) > MAX_PLANTS_PER_EDIT:
            raise PlanError("planned vegetation edit has too many plants", code="edit_too_large")
    return {
        "edit_id": edit_id,
        "ordinal": int(raw.get("ordinal", ordinal)),
        "kind": kind,
        "geometry": geometry,
        "params": json.loads(_canonical(params)),
        "label": None if raw.get("label") is None else str(raw.get("label"))[:200],
    }


def normalize_edits(edits: Any) -> list[dict[str, Any]]:
    if not isinstance(edits, list):
        raise PlanError("edits must be an array", code="invalid_edits")
    if len(edits) > MAX_EDITS:
        raise PlanError("plan has too many edits", code="edit_too_large", maximum=MAX_EDITS)
    normalized = [normalize_edit(edit, i) for i, edit in enumerate(edits)]
    ids = [edit["edit_id"] for edit in normalized]
    if len(set(ids)) != len(ids):
        raise PlanError("duplicate edit_id in revision", code="invalid_edits")
    normalized.sort(key=lambda item: (item["ordinal"], item["edit_id"]))
    for ordinal, item in enumerate(normalized):
        item["ordinal"] = ordinal
    return normalized


def revision_content_hash(base_id: str, edits: list[dict[str, Any]]) -> str:
    return _hash_bytes(_canonical({"base_id": base_id, "edits": edits}).encode())


def _grid_arrays(grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float]:
    width, height = int(grid["width"]), int(grid["height"])
    values = np.asarray([np.nan if v is None else float(v) for v in grid["heights"]], dtype=float)
    dem = values.reshape((height, width))
    xstep = (float(grid["maxX"]) - float(grid["minX"])) / max(1, width - 1)
    ystep = (float(grid["maxY"]) - float(grid["minY"])) / max(1, height - 1)
    xs = float(grid["minX"]) + np.arange(width) * xstep
    ys = float(grid["maxY"]) - np.arange(height) * ystep
    xx, yy = np.meshgrid(xs, ys)
    return dem, xx, yy, xstep, ystep


def _distance_field(xx: np.ndarray, yy: np.ndarray, geometry: dict[str, Any]) -> np.ndarray:
    points = _geometry_points(geometry)
    if not points:
        return np.full(xx.shape, np.inf)
    if geometry["type"] == "Point" or len(points) == 1:
        return np.hypot(xx - points[0][0], yy - points[0][1])
    out = np.full(xx.shape, np.inf)
    for a, b in zip(points, points[1:]):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        if denom <= 1e-12:
            distance = np.hypot(xx - ax, yy - ay)
        else:
            t = np.clip(((xx - ax) * dx + (yy - ay) * dy) / denom, 0.0, 1.0)
            distance = np.hypot(xx - (ax + t * dx), yy - (ay + t * dy))
        out = np.minimum(out, distance)
    return out


def _points_in_ring(xx: np.ndarray, yy: np.ndarray, ring: list[list[float]]) -> np.ndarray:
    inside = np.zeros(xx.shape, dtype=bool)
    if len(ring) < 3:
        return inside
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        crosses = ((yi > yy) != (yj > yy)) & (
            xx < (xj - xi) * (yy - yi) / ((yj - yi) or 1e-12) + xi)
        inside ^= crosses
        j = i
    return inside


def _polygon_mask(xx: np.ndarray, yy: np.ndarray, geometry: dict[str, Any]) -> np.ndarray:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    output = np.zeros(xx.shape, dtype=bool)
    for polygon in polygons:
        if not polygon:
            continue
        mask = _points_in_ring(xx, yy, polygon[0])
        for hole in polygon[1:]:
            mask &= ~_points_in_ring(xx, yy, hole)
        output |= mask
    return output


def _geometry_point_distances(xs: np.ndarray, ys: np.ndarray,
                              geometry: dict[str, Any]) -> np.ndarray:
    """Vectorized center-point distance to supported Plan geometry."""
    output = np.full(xs.shape, np.inf, dtype=float)
    if geometry["type"] in {"Polygon", "MultiPolygon"}:
        polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                    else geometry["coordinates"])
        inside = np.zeros(xs.shape, dtype=bool)
        for polygon in polygons:
            if not polygon:
                continue
            mask = _points_in_ring(xs, ys, polygon[0])
            for hole in polygon[1:]:
                mask &= ~_points_in_ring(xs, ys, hole)
            inside |= mask
        output[inside] = 0.0

    close_paths = geometry["type"] in {"Polygon", "MultiPolygon"}
    for raw_path in _geometry_paths(geometry):
        if not raw_path:
            continue
        path = list(raw_path)
        if close_paths and len(path) > 2 and path[0] != path[-1]:
            path.append(path[0])
        if len(path) == 1:
            output = np.minimum(output, np.hypot(xs - path[0][0], ys - path[0][1]))
            continue
        for start, end in zip(path, path[1:]):
            ax, ay = start
            bx, by = end
            dx, dy = bx - ax, by - ay
            denominator = dx * dx + dy * dy
            if denominator <= 1e-12:
                distance = np.hypot(xs - ax, ys - ay)
            else:
                t = np.clip(((xs - ax) * dx + (ys - ay) * dy) / denominator,
                            0.0, 1.0)
                distance = np.hypot(xs - (ax + t * dx), ys - (ay + t * dy))
            output = np.minimum(output, distance)
    return output


def _vegetation_removal_buffer(params: dict[str, Any]) -> float | None:
    value = params.get("buffer_m")
    if value is None:
        value = params.get("distance_m")
    return None if value is None else float(value)


def _falloff(distance: np.ndarray, radius: float, style: str) -> np.ndarray:
    t = np.clip(1.0 - distance / max(radius, 1e-6), 0.0, 1.0)
    if style == "hard":
        return (distance <= radius).astype(float)
    if style == "linear":
        return t
    return t * t * (3.0 - 2.0 * t)


def _accumulation_weight(xx: np.ndarray, yy: np.ndarray,
                         stamps: list[list[float]], radius: float,
                         style: str) -> np.ndarray:
    """Sum repeated brush dabs without allocating a full-grid array per dab."""
    output = np.zeros(xx.shape, dtype=float)
    if not stamps or not output.size:
        return output
    strengths: dict[tuple[float, float], float] = {}
    for x, y, strength in stamps:
        key = (float(x), float(y))
        strengths[key] = strengths.get(key, 0.0) + float(strength)
    xs = xx[0, :]
    ys = yy[:, 0]
    for (x, y), strength in strengths.items():
        columns = np.flatnonzero(np.abs(xs - x) <= radius)
        rows = np.flatnonzero(np.abs(ys - y) <= radius)
        if not columns.size or not rows.size:
            continue
        row_slice = slice(int(rows[0]), int(rows[-1]) + 1)
        column_slice = slice(int(columns[0]), int(columns[-1]) + 1)
        distance = np.hypot(xx[row_slice, column_slice] - x,
                            yy[row_slice, column_slice] - y)
        output[row_slice, column_slice] += strength * _falloff(distance, radius, style)
    return output


def _terrain_delta(edit: dict[str, Any], xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
    kind, geometry, params = edit["kind"], edit.get("geometry"), edit.get("params") or {}
    out = np.zeros(xx.shape, dtype=float)
    if kind == "garden":
        height = float(params.get("height_m") or 0.0)
        if height > 0 and geometry:
            mask = _polygon_mask(xx, yy, geometry)
            edge = float(params.get("edge_falloff_m") or 0.0)
            if edge <= 0:
                out[mask] = height
            else:
                distance = np.full(xx.shape, np.inf)
                for path in _geometry_paths(geometry):
                    if path:
                        distance = np.minimum(
                            distance,
                            _distance_field(xx, yy, {
                                "type": "LineString" if len(path) > 1 else "Point",
                                "coordinates": path if len(path) > 1 else path[0],
                            }))
                weight = np.clip(distance / edge, 0.0, 1.0)
                weight = weight * weight * (3.0 - 2.0 * weight)
                out[mask] = height * weight[mask]
        return out
    if kind not in {"terrain_cut", "terrain_fill", "swale"} or not geometry:
        return out
    radius = float(params.get("radius_m") or 1.0)
    style = str(params.get("falloff") or "smoothstep")
    distance = _distance_field(xx, yy, geometry)
    weight = _falloff(distance, radius, style)
    stamps = params.get("accumulation_stamps") or []
    if stamps:
        weight += _accumulation_weight(xx, yy, stamps, radius, style)
    if kind in {"terrain_cut", "swale"}:
        return -abs(float(params.get("depth_m") or params.get("delta_m") or 0.0)) * weight
    return abs(float(params.get("height_m") or params.get("delta_m") or 0.0)) * weight


def _vegetation_additions(edit: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if edit["kind"] not in {"vegetation_add", "orchard"}:
        return [], []
    params = edit.get("params") or {}
    habit = params.get("habit", "tree")
    trees: list[dict[str, Any]] = []
    shrubs: list[dict[str, Any]] = []
    for index, point in enumerate(_geometry_points(edit.get("geometry"))):
        identity = f"planned:{edit['edit_id']}:{index}"
        common = {
            "id": identity,
            "x": round(float(point[0]), 3),
            "y": round(float(point[1]), 3),
            "height": float(params.get("height") or (3.0 if habit == "tree" else 1.2)),
            "species": params.get("species"),
            "source": "planned",
            "confidence": 1.0,
            "plan_edit_id": edit["edit_id"],
        }
        if habit == "tree":
            trees.append({**common, "radius": float(params.get("radius") or 1.5),
                          "type": params.get("type", "evergreen"),
                          "community": params.get("community", "Planned planting")})
        else:
            shrubs.append({**common, "baseScale": float(params.get("radius") or 0.7)})
    return trees, shrubs


def _canopy_grid(grid: dict[str, Any], trees: list[dict[str, Any]], shrubs: list[dict[str, Any]]) -> dict[str, Any]:
    dem, _xx, _yy, xstep, ystep = _grid_arrays(grid)
    canopy = np.zeros(dem.shape, dtype=np.float32)
    height, width = canopy.shape
    for row in trees:
        try:
            x, y = float(row["x"]), float(row["y"])
            crown = max(0.3, float(row.get("radius") or 1.5))
            h = max(0.0, float(row.get("height") or 0.0))
        except (TypeError, ValueError, KeyError):
            continue
        col0 = int(round((x - float(grid["minX"])) / xstep))
        row0 = int(round((float(grid["maxY"]) - y) / ystep))
        dc = max(1, int(math.ceil(crown / max(xstep, 1e-6))))
        dr = max(1, int(math.ceil(crown / max(ystep, 1e-6))))
        for rr in range(max(0, row0 - dr), min(height, row0 + dr + 1)):
            for cc in range(max(0, col0 - dc), min(width, col0 + dc + 1)):
                dx = (cc - col0) * xstep
                dy = (rr - row0) * ystep
                if dx * dx + dy * dy <= crown * crown:
                    canopy[rr, cc] = max(canopy[rr, cc], h)
    for row in shrubs:
        try:
            col = int(round((float(row["x"]) - float(grid["minX"])) / xstep))
            rr = int(round((float(grid["maxY"]) - float(row["y"])) / ystep))
            if 0 <= rr < height and 0 <= col < width:
                canopy[rr, col] = max(canopy[rr, col], float(row.get("height") or row.get("baseScale") or 0.8))
        except (TypeError, ValueError, KeyError):
            continue
    values = [[None if not math.isfinite(float(dem[r, c])) else round(float(canopy[r, c]), 3)
               for c in range(width)] for r in range(height)]
    return {
        "bounds_local": [float(grid["minX"]), float(grid["minY"]),
                         float(grid["maxX"]), float(grid["maxY"])],
        "width": width,
        "height": height,
        "nodata": None,
        "value_kind": "planned_canopy_height",
        "value_unit": "m",
        "values": values,
    }


class PlanEngine:
    def __init__(self, data_dir: str | os.PathLike[str] | None = None):
        self.data_dir = Path(data_dir or os.environ.get("TWIN_DATA_DIR") or twin_store.DATA_DIR).resolve()
        self.store_path = self.data_dir / "twin.gpkg"
        self.plans_dir = self.data_dir / "plans"
        self.bases_dir = self.plans_dir / "bases"
        self.cache_dir = self.plans_dir / "cache"
        self.revisions_dir = self.plans_dir / "revisions"
        self.proposals_dir = self.plans_dir / "proposals"
        self.lock_path = self.plans_dir / ".lock"
        twin_store.DATA_DIR = str(self.data_dir)
        twin_store.STORE_PATH = str(self.store_path)
        twin_store.JOURNAL_DIR = str(self.data_dir / "journal")

    @contextlib.contextmanager
    def locked(self):
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _store(self, *, journal: bool = True) -> Store:
        return Store(str(self.store_path), journal=journal)

    def ensure_base(self) -> dict[str, Any]:
        files: dict[str, Any] = {}
        fingerprint_rows = []
        for rel in BASE_FILES:
            source = self.data_dir / rel
            if source.exists() and source.is_file():
                digest = _hash_file(source)
                files[rel] = {"sha256": digest, "bytes": source.stat().st_size}
                fingerprint_rows.append([rel, digest])
            else:
                fingerprint_rows.append([rel, None])
        fingerprint = _hash_bytes(_canonical(fingerprint_rows).encode())
        base_id = "base_" + fingerprint[:20]
        root = self.bases_dir / base_id
        manifest_path = root / "manifest.json"
        manifest = _read_json(manifest_path)
        if manifest is None:
            root.mkdir(parents=True, exist_ok=True)
            for rel, metadata in files.items():
                source = self.data_dir / rel
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or _hash_file(target) != metadata["sha256"]:
                    shutil.copy2(source, target)
            manifest = {
                "base_id": base_id,
                "fingerprint": fingerprint,
                "source_data_dir": str(self.data_dir),
                "files": files,
            }
            _atomic_json(manifest_path, manifest, indent=2)
        with self._store() as store:
            store.register_plan_base(base_id, fingerprint, manifest)
        return manifest

    @staticmethod
    def _plan_id() -> str:
        return "plan_" + uuid.uuid4().hex[:16]

    @staticmethod
    def _revision_id() -> str:
        return "rev_" + uuid.uuid4().hex[:20]

    def list_plans(self, include_archived: bool = False) -> dict[str, Any]:
        with self._store(journal=False) as store:
            rows = store.plan_rows(include_archived=include_archived)
            for row in rows:
                rev = store.plan_revision(row["head_revision_id"]) if row["head_revision_id"] else None
                row["content_hash"] = rev.get("content_hash") if rev else None
                row["edit_count"] = len(rev.get("edits") or []) if rev else 0
                row["checkpoint_name"] = rev.get("checkpoint_name") if rev else None
        return {"plans": rows}

    def get_plan(self, plan_id: str, revision_id: str | None = None,
                 materialize: bool = False) -> dict[str, Any]:
        with self._store(journal=False) as store:
            row = store.conn.execute(
                "SELECT plan_id, name, head_revision_id, forked_from_revision_id,"
                " created_at, updated_at, archived_at FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise PlanError("unknown plan", code="plan_not_found", plan_id=plan_id)
            target = revision_id or row[2]
            try:
                newest_first = store.plan_history(row[2]) if row[2] else []
            except ValueError as exc:
                raise PlanError("plan revision graph is corrupt", code="plan_graph_invalid",
                                plan_id=plan_id) from exc
            reachable = {item["revision_id"] for item in newest_first}
            if target and target not in reachable:
                raise PlanError("revision is not in this plan's history",
                                code="revision_not_found", revision_id=target,
                                plan_id=plan_id)
            revision = store.plan_revision(target) if target else None
            history = list(reversed(newest_first))
            simulation_runs = store.plan_simulation_rows(plan_id)
        out = {
            "plan": {"plan_id": row[0], "name": row[1], "head_revision_id": row[2],
                     "forked_from_revision_id": row[3], "created_at": row[4],
                     "updated_at": row[5], "archived_at": row[6]},
            "revision": revision,
            "history": history,
            "simulation_runs": simulation_runs,
        }
        if materialize and revision:
            out["materialized"] = self.materialize(revision["revision_id"])
            # A newly-created branch can share its fork revision with the
            # source plan.  The land artifact is identical, but this response
            # belongs to the branch the caller addressed.
            out["materialized"]["plan_id"] = plan_id
        return out

    def create_plan(self, name: str, *, author: str | None = None) -> dict[str, Any]:
        clean_name = str(name or "Untitled plan").strip()[:160] or "Untitled plan"
        with self.locked():
            base = self.ensure_base()
            plan_id = self._plan_id()
            revision_id = self._revision_id()
            edits: list[dict[str, Any]] = []
            content_hash = revision_content_hash(base["base_id"], edits)
            with self._store() as store:
                store.insert_plan(plan_id, clean_name)
                store.insert_plan_revision(
                    revision_id, plan_id, None, base["base_id"], content_hash,
                    edits, message="Plan created", author=author)
                if not store.update_plan_head(plan_id, revision_id, expected_revision_id=None):
                    raise PlanError("could not initialize plan head", code="plan_conflict")
        result = self.get_plan(plan_id, materialize=True)
        return result

    def commit(self, plan_id: str, expected_revision_id: str, edits: Any, *,
               message: str | None = None, checkpoint_name: str | None = None,
               author: str | None = None) -> dict[str, Any]:
        normalized = normalize_edits(edits)
        with self.locked():
            current = self.get_plan(plan_id)
            head = current["plan"]["head_revision_id"]
            if head != expected_revision_id:
                raise PlanError("plan changed since it was loaded", code="plan_conflict",
                                expected_revision_id=expected_revision_id,
                                current_revision_id=head)
            parent = current["revision"]
            base_id = parent["base_id"]
            parent_edit_ids = {str(edit.get("edit_id"))
                               for edit in parent.get("edits") or []}
            legacy_empty_removal_ids = {
                str(edit.get("edit_id"))
                for edit in parent.get("edits") or []
                if edit.get("kind") == "vegetation_remove"
                and not (edit.get("params") or {}).get("entity_ids")
            }
            normalized = self._prepare_edits_for_base(
                base_id, normalized,
                resolve_spatial_removal_ids={
                    edit["edit_id"] for edit in normalized
                    if edit["edit_id"] not in parent_edit_ids
                },
                allow_empty_removal_ids=legacy_empty_removal_ids,
            )
            content_hash = revision_content_hash(base_id, normalized)
            if (content_hash == parent["content_hash"] and not checkpoint_name
                    and not message):
                return self.get_plan(plan_id, materialize=True)
            revision_id = self._revision_id()
            with self._store() as store:
                store.insert_plan_revision(
                    revision_id, plan_id, head, base_id, content_hash, normalized,
                    message=(str(message)[:300] if message else None),
                    checkpoint_name=(str(checkpoint_name)[:160] if checkpoint_name else None),
                    author=(str(author)[:160] if author else None))
                if not store.update_plan_head(plan_id, revision_id, expected_revision_id=head):
                    raise PlanError("plan head update conflicted", code="plan_conflict")
        return self.get_plan(plan_id, materialize=True)

    def checkpoint(self, plan_id: str, expected_revision_id: str, name: str,
                   *, author: str | None = None) -> dict[str, Any]:
        current = self.get_plan(plan_id)
        return self.commit(plan_id, expected_revision_id,
                           current["revision"]["edits"],
                           message="Saved version", checkpoint_name=name,
                           author=author)

    def branch(self, source_plan_id: str, name: str, *, revision_id: str | None = None,
               author: str | None = None) -> dict[str, Any]:
        source = self.get_plan(source_plan_id, revision_id=revision_id)
        source_revision = source["revision"]
        if source_revision is None:
            raise PlanError("source plan has no revision", code="revision_not_found")
        clean_name = str(name or (source["plan"]["name"] + " branch")).strip()[:160]
        with self.locked():
            plan_id = self._plan_id()
            with self._store() as store:
                store.insert_plan(
                    plan_id, clean_name, head_revision_id=source_revision["revision_id"],
                    forked_from_revision_id=source_revision["revision_id"])
        return self.get_plan(plan_id, materialize=True)

    def update(self, plan_id: str, *, name: str | None = None,
               archived: bool | None = None) -> dict[str, Any]:
        with self.locked():
            with self._store() as store:
                kwargs: dict[str, Any] = {}
                if name is not None:
                    kwargs["name"] = str(name).strip()[:160] or "Untitled plan"
                if archived is not None:
                    kwargs.update(set_archived=True,
                                  archived_at=twin_store.utcnow() if archived else None)
                store.update_plan(plan_id, **kwargs)
        return self.get_plan(plan_id)

    @staticmethod
    def _proposal_id() -> str:
        return "proposal_" + uuid.uuid4().hex[:20]

    def _proposal_path(self, proposal_id: str) -> Path:
        proposal_id = str(proposal_id or "")
        if (not proposal_id.startswith("proposal_") or len(proposal_id) > 80
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in proposal_id)):
            raise PlanError("invalid proposal id", code="proposal_not_found")
        return self.proposals_dir / (proposal_id + ".json")

    def _proposal_summary(self, base_id: str,
                          proposed_edits: list[dict[str, Any]]) -> dict[str, Any]:
        grid = _read_json(self.bases_dir / base_id / "terrain/grid.json")
        if not isinstance(grid, dict):
            return {"edit_count": len(proposed_edits)}
        dem, xx, yy, xstep, ystep = _grid_arrays(grid)
        delta = np.zeros(dem.shape, dtype=float)
        trees = shrubs = removals = 0
        kinds: dict[str, int] = {}
        for edit in proposed_edits:
            kinds[edit["kind"]] = kinds.get(edit["kind"], 0) + 1
            delta += _terrain_delta(edit, xx, yy)
            add_trees, add_shrubs = _vegetation_additions(edit)
            trees += len(add_trees)
            shrubs += len(add_shrubs)
            if edit["kind"] == "vegetation_remove":
                removals += len(edit.get("params", {}).get("entity_ids") or [])
        delta = np.clip(delta, -MAX_EARTH_DELTA_M, MAX_EARTH_DELTA_M)
        valid = np.isfinite(dem)
        cut = np.where(valid, np.maximum(0.0, -delta), 0.0)
        fill = np.where(valid, np.maximum(0.0, delta), 0.0)
        cell_area = abs(xstep * ystep)
        return {
            "edit_count": len(proposed_edits),
            "kinds": kinds,
            "terrain": {
                "estimated_cut_m3": round(float(cut.sum() * cell_area), 2),
                "estimated_fill_m3": round(float(fill.sum() * cell_area), 2),
                "estimated_disturbed_m2": round(
                    float(np.sum(np.abs(delta) > 1e-6) * cell_area), 2),
                "analysis_cell_m": round((abs(xstep) + abs(ystep)) / 2.0, 3),
            },
            "vegetation": {
                "plants_added": trees + shrubs,
                "trees_added": trees,
                "shrubs_added": shrubs,
                "entities_removed": removals,
            },
            "note": "Preview quantities use the pinned terrain grid; run simulations after applying for modeled outcomes.",
        }

    def propose(self, plan_id: str, edits: Any, *,
                expected_revision_id: str | None = None,
                replace: bool = False, label: str | None = None,
                author: str | None = None) -> dict[str, Any]:
        """Validate a prospective edit set without changing the plan graph."""
        current = self.get_plan(plan_id)
        head = current["plan"]["head_revision_id"]
        expected = expected_revision_id or head
        if head != expected:
            raise PlanError("plan changed since the proposal was started",
                            code="plan_conflict", expected_revision_id=expected,
                            current_revision_id=head)
        incoming = normalize_edits(edits)
        if replace:
            raw = incoming
        else:
            existing = list(current["revision"]["edits"])
            next_ordinal = max(
                (int(edit.get("ordinal", index)) for index, edit in enumerate(existing)),
                default=-1,
            ) + 1
            for offset, edit in enumerate(incoming):
                edit["ordinal"] = next_ordinal + offset
            raw = existing + incoming
        prospective = normalize_edits(raw)
        incoming_ids = {edit["edit_id"] for edit in incoming}
        legacy_empty_removal_ids = {
            str(edit.get("edit_id"))
            for edit in current["revision"].get("edits") or []
            if edit.get("kind") == "vegetation_remove"
            and not (edit.get("params") or {}).get("entity_ids")
        }
        prospective = self._prepare_edits_for_base(
            current["revision"]["base_id"], prospective,
            resolve_spatial_removal_ids=incoming_ids,
            allow_empty_removal_ids=legacy_empty_removal_ids,
        )
        proposed = prospective if replace else [
            edit for edit in prospective if edit["edit_id"] in incoming_ids]
        proposal = {
            "proposal_id": self._proposal_id(),
            "plan_id": plan_id,
            "expected_revision_id": head,
            "base_id": current["revision"]["base_id"],
            "prospective_content_hash": revision_content_hash(
                current["revision"]["base_id"], prospective),
            "replace": bool(replace),
            "label": (str(label).strip()[:200] if label else "Plan proposal"),
            "author": (str(author).strip()[:160] if author else None),
            "created_at": twin_store.utcnow(),
            "status": "proposed",
            "proposed_edits": proposed,
            "edits": prospective,
            "preview": self._proposal_summary(current["revision"]["base_id"], proposed),
        }
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(self._proposal_path(proposal["proposal_id"]), proposal, indent=2)
        return proposal

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = _read_json(self._proposal_path(proposal_id))
        if not isinstance(proposal, dict):
            raise PlanError("unknown plan proposal", code="proposal_not_found",
                            proposal_id=proposal_id)
        return proposal

    def apply_proposal(self, proposal_id: str, *, confirmed: bool = False,
                       author: str | None = None) -> dict[str, Any]:
        if confirmed is not True:
            raise PlanError("explicit confirmation is required before applying a proposal",
                            code="confirmation_required", proposal_id=proposal_id)
        proposal = self.get_proposal(proposal_id)
        if proposal.get("status") == "applied" and proposal.get("applied_revision_id"):
            return self.get_plan(proposal["plan_id"], materialize=True)
        result = self.commit(
            proposal["plan_id"], proposal["expected_revision_id"], proposal["edits"],
            message=proposal.get("label") or "Applied GAIA proposal",
            author=author or proposal.get("author") or "GAIA")
        proposal["status"] = "applied"
        proposal["applied_at"] = twin_store.utcnow()
        proposal["applied_revision_id"] = result["revision"]["revision_id"]
        _atomic_json(self._proposal_path(proposal_id), proposal, indent=2)
        result["proposal"] = {
            "proposal_id": proposal_id, "status": "applied",
            "applied_revision_id": proposal["applied_revision_id"],
        }
        return result

    def _base_manifest(self, base_id: str) -> dict[str, Any]:
        with self._store(journal=False) as store:
            row = store.conn.execute(
                "SELECT manifest FROM plan_bases WHERE base_id = ?", (base_id,)
            ).fetchone()
        if row is None:
            raise PlanError("plan base is missing", code="plan_base_missing", base_id=base_id)
        return twin_store.decode_value(row[0])

    def _prepare_edits_for_base(
            self, base_id: str, edits: list[dict[str, Any]], *,
            resolve_spatial_removal_ids: set[str] | None = None,
            allow_empty_removal_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Constrain edits to the pinned AOI and reject silent no-ops/typos."""
        resolve_spatial_removal_ids = set(resolve_spatial_removal_ids or ())
        allow_empty_removal_ids = set(allow_empty_removal_ids or ())
        base_root = self.bases_dir / base_id
        grid = _read_json(base_root / "terrain/grid.json")
        if not isinstance(grid, dict):
            raise PlanError("planning baseline has no terrain grid", code="plan_base_missing")
        dem, _xx, _yy, xstep, ystep = _grid_arrays(grid)
        min_x, max_x = float(grid["minX"]), float(grid["maxX"])
        min_y, max_y = float(grid["minY"]), float(grid["maxY"])
        polygons = _aoi_polygons(_read_json(base_root / "terrain/aoi_local.geojson", {}))

        def valid_point(point: list[float]) -> bool:
            x, y = float(point[0]), float(point[1])
            if x < min_x or x > max_x or y < min_y or y > max_y:
                return False
            col = max(0, min(dem.shape[1] - 1, int(round((x - min_x) / xstep))))
            row = max(0, min(dem.shape[0] - 1, int(round((max_y - y) / ystep))))
            if not math.isfinite(float(dem[row, col])):
                return False
            return not polygons or any(
                _point_in_polygon_coordinates(x, y, polygon) for polygon in polygons)

        prepared = json.loads(_canonical(edits))
        for edit in prepared:
            geometry = edit.get("geometry")
            if edit["kind"] in {"vegetation_add", "orchard"} and geometry:
                if geometry["type"] == "Point":
                    if not valid_point(geometry["coordinates"]):
                        raise PlanError("planting point is outside the editable land",
                                        code="edit_outside_aoi", edit_id=edit["edit_id"])
                elif geometry["type"] == "MultiPoint":
                    points = geometry["coordinates"]
                    kept = [point for point in points if valid_point(point)]
                    if not kept:
                        raise PlanError("planting brush does not intersect editable land",
                                        code="edit_outside_aoi", edit_id=edit["edit_id"])
                    if len(kept) != len(points):
                        geometry["coordinates"] = kept
                        edit["params"]["clipped_outside_aoi"] = len(points) - len(kept)
            elif geometry and edit["kind"] != "vegetation_remove":
                # Sample long segments as well as vertices; a line crossing the
                # AOI is valid, while an entirely off-site gesture is rejected.
                intersects = False
                sample_step = max(0.25, min(abs(xstep), abs(ystep)) * 0.5)
                for path in _geometry_paths(geometry):
                    if any(valid_point(point) for point in path):
                        intersects = True
                        break
                    for start, end in zip(path, path[1:]):
                        distance = math.hypot(end[0] - start[0], end[1] - start[1])
                        count = min(20_000, max(1, int(math.ceil(distance / sample_step))))
                        if any(valid_point([
                            start[0] + (end[0] - start[0]) * index / count,
                            start[1] + (end[1] - start[1]) * index / count,
                        ]) for index in range(1, count)):
                            intersects = True
                            break
                    if intersects:
                        break
                if not intersects:
                    raise PlanError("edit does not intersect the editable land",
                                    code="edit_outside_aoi", edit_id=edit["edit_id"])

        vegetation: dict[str, tuple[str, dict[str, Any]]] = {}
        for rel, habit in (("vegetation/tree_instances.json", "tree"),
                           ("vegetation/shrub_points.json", "shrub")):
            for row in _read_json(base_root / rel, []) or []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                vegetation[str(row["id"])] = (habit, row)
        for edit in prepared:
            trees, shrubs = _vegetation_additions(edit)
            for row in trees:
                vegetation[str(row["id"])] = ("tree", row)
            for row in shrubs:
                vegetation[str(row["id"])] = ("shrub", row)

        removed_before: set[str] = set()
        for edit in prepared:
            if edit["kind"] != "vegetation_remove":
                continue
            params = edit.get("params") or {}
            requested = set(params.get("entity_ids") or [])
            if edit["edit_id"] in resolve_spatial_removal_ids and not requested:
                geometry = edit.get("geometry")
                buffer_m = _vegetation_removal_buffer(params)
                if geometry is None or buffer_m is None:
                    raise PlanError(
                        "vegetation removal needs entity_ids or geometry with buffer_m",
                        code="empty_vegetation_removal", edit_id=edit["edit_id"])
                points = _geometry_points(geometry)
                min_match_x = min(point[0] for point in points) - buffer_m
                max_match_x = max(point[0] for point in points) + buffer_m
                min_match_y = min(point[1] for point in points) - buffer_m
                max_match_y = max(point[1] for point in points) + buffer_m
                candidate_ids: list[str] = []
                candidate_x: list[float] = []
                candidate_y: list[float] = []
                allowed_kinds = set(params.get("kinds") or ("tree", "shrub"))
                for entity_id, (habit, row) in vegetation.items():
                    if entity_id in removed_before or habit not in allowed_kinds:
                        continue
                    try:
                        x, y = float(row["x"]), float(row["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not (min_match_x <= x <= max_match_x
                            and min_match_y <= y <= max_match_y):
                        continue
                    candidate_ids.append(entity_id)
                    candidate_x.append(x)
                    candidate_y.append(y)
                if candidate_ids:
                    distances = _geometry_point_distances(
                        np.asarray(candidate_x, dtype=float),
                        np.asarray(candidate_y, dtype=float), geometry)
                    requested = {
                        entity_id for entity_id, distance in zip(candidate_ids, distances)
                        if float(distance) <= buffer_m + 1e-9
                    }
                if not requested:
                    raise PlanError(
                        "vegetation removal found no matching entities",
                        code="empty_vegetation_removal", edit_id=edit["edit_id"],
                        buffer_m=buffer_m, kinds=sorted(allowed_kinds))
                if len(requested) > MAX_VEGETATION_REMOVAL_IDS:
                    raise PlanError(
                        "vegetation removal matches too many entities",
                        code="edit_too_large", edit_id=edit["edit_id"],
                        maximum=MAX_VEGETATION_REMOVAL_IDS,
                        matched_count=len(requested))
                params["entity_ids"] = sorted(requested)
                params["buffer_m"] = round(buffer_m, 3)
                params.pop("distance_m", None)
            if not requested and edit["edit_id"] not in allow_empty_removal_ids:
                raise PlanError(
                    "vegetation removal would not remove any entities",
                    code="empty_vegetation_removal", edit_id=edit["edit_id"])
            unknown = sorted(requested - set(vegetation))
            if unknown:
                raise PlanError("vegetation removal references unknown entities",
                                code="unknown_vegetation", edit_id=edit["edit_id"],
                                entity_ids=unknown[:50], unknown_count=len(unknown))
            removed_before.update(requested)
        return prepared

    def _populate_runtime_inputs(self, target: Path, base_root: Path) -> None:
        # Read-only source datasets are shared. Simulator output directories
        # start empty so a brush save never copies a potentially large analysis
        # workspace; a simulator seeds the few baseline inputs it needs lazily.
        readonly = ("atlas", "soils", "climate", "imagery", "buildings",
                    "roads", "parcels", "surveys", "astronomy", "groundwater")
        mutable = ("hydrology", "fire", "et", "solar", "viewshed")
        for name in readonly:
            source = self.data_dir / name
            link = target / name
            if source.exists() and not link.exists():
                try:
                    os.symlink(source, link, target_is_directory=True)
                except OSError:
                    if source.is_dir():
                        shutil.copytree(source, link, dirs_exist_ok=True)
        for name in mutable:
            output = target / name
            output.mkdir(parents=True, exist_ok=True)
        # A fresh revision has intentionally run no simulations. Explicit empty
        # payloads let the viewer distinguish that normal state from a broken
        # asset route without eagerly copying baseline output directories.
        placeholders = {
            "hydrology/simulation-layers.json": {"layers": []},
            "hydrology/summary.json": None,
            "hydrology/last-scenario.json": None,
            "fire/fire-layers.json": {"layers": []},
            "fire/summary.json": None,
            "fire/last-fire-scenario.json": None,
            "et/et-layers.json": {"layers": []},
            "et/et-scenario-layers.json": {"layers": []},
            "et/et0-summary.json": None,
            "et/summary.json": None,
            "et/last-et-scenario.json": None,
            "solar/solar-layers.json": {"layers": []},
            "solar/solar-summary.json": None,
            "viewshed/viewshed-layers.json": {"layers": []},
        }
        for rel, payload in placeholders.items():
            path = target / rel
            if not path.exists():
                _atomic_json(path, payload)
        for rel in ("georef.json", "pack.txt"):
            source = base_root / rel
            if source.exists():
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target / rel)

    @staticmethod
    def _link_or_copy(source: Path, target: Path) -> None:
        if target.exists() or target.is_symlink():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(source, target, target_is_directory=source.is_dir())
        except OSError:
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)

    def _revision_runtime_manifest(self, revision: dict[str, Any],
                                   cache_manifest: dict[str, Any]) -> dict[str, Any]:
        """Create the cheap, revision-scoped facade around immutable land.

        Land artifacts are content-addressed and safely shared by revisions.
        Simulation catalogs and effective stores are revision-scoped, avoiding
        result bleed between branches that happen to describe identical land.
        """
        revision_id = revision["revision_id"]
        root = self.revisions_dir / revision_id
        root.mkdir(parents=True, exist_ok=True)
        base_root = self.bases_dir / revision["base_id"]
        self._populate_runtime_inputs(root, base_root)
        cache_root = self.cache_dir / revision["content_hash"]
        for name in ("terrain", "vegetation"):
            self._link_or_copy(cache_root / name, root / name)
        for name in ("plan-features.geojson", "diff.json"):
            self._link_or_copy(cache_root / name, root / name)

        url_root = f"/data/plans/revisions/{revision_id}"
        scene = _read_json(cache_root / "scene.json", {}) or {}
        scene["plan"] = {
            "plan_id": revision["plan_id"],
            "revision_id": revision_id,
            "content_hash": revision["content_hash"],
            "base_id": revision["base_id"],
        }
        scene.setdefault("terrain", {})["grid_url"] = url_root + "/terrain/grid.json"
        vegetation = scene.setdefault("vegetation", {})
        vegetation["tree_instances_url"] = url_root + "/vegetation/tree_instances.json"
        vegetation["shrub_points_url"] = url_root + "/vegetation/shrub_points.json"
        vegetation["canopy_height_grid_url"] = url_root + "/vegetation/canopy_height.grid.json"
        _atomic_json(root / "scene.json", scene, indent=2)

        manifest = {
            **cache_manifest,
            "plan_id": revision["plan_id"],
            "revision_id": revision_id,
            "asset_root": url_root,
            "data_dir": str(root),
            "cache_data_dir": str(cache_root),
            "scene_url": url_root + "/scene.json",
            "terrain_grid_url": url_root + "/terrain/grid.json",
            "tree_instances_url": url_root + "/vegetation/tree_instances.json",
            "shrub_points_url": url_root + "/vegetation/shrub_points.json",
            "canopy_grid_url": url_root + "/vegetation/canopy_height.grid.json",
            "features_url": url_root + "/plan-features.geojson",
            "diff_url": url_root + "/diff.json",
        }
        _atomic_json(root / "plan-manifest.json", manifest, indent=2)
        return manifest

    def _seed_runtime_dir(self, runtime: Path, name: str) -> None:
        target = runtime / name
        marker = target / ".baseline-seeded"
        if marker.exists():
            return
        source = self.data_dir / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        marker.touch()

    def _write_effective_store(self, target: Path, trees: list[dict[str, Any]],
                               shrubs: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        source_store = self.store_path
        target_store = target / "twin.gpkg"
        if not source_store.exists():
            return
        temp_store = target / (".twin-" + uuid.uuid4().hex + ".gpkg")
        shutil.copy2(source_store, temp_store)
        try:
            store = Store(str(temp_store), journal=False)
            try:
                run = store.begin_run(
                    "plan_materialize.py", notes="effective planned vegetation")
                tree_ids, _ = store.bulk_upsert_vegetation(
                    "tree", "trees", trees, run, "member_parcel", source_default="planned")
                store.reconcile_membership(
                    "tree", "member_parcel", tree_ids, run,
                    other_member_attrs=("member_surrounding",))
                shrub_ids, _ = store.bulk_upsert_vegetation(
                    "shrub", "shrubs", shrubs, run, "member_parcel",
                    source_default="planned")
                store.reconcile_membership(
                    "shrub", "member_parcel", shrub_ids, run,
                    other_member_attrs=("member_surrounding",))
                store.set_meta("vegetation_metadata", metadata)
                store.finish_run(run, notes="effective planned vegetation")
            finally:
                store.close()
            os.replace(temp_store, target_store)
        except Exception:
            temp_store.unlink(missing_ok=True)
            raise

    def _ensure_effective_store(self, runtime: Path) -> None:
        if (runtime / "twin.gpkg").exists():
            return
        trees = _read_json(runtime / "vegetation/tree_instances.json", []) or []
        shrubs = _read_json(runtime / "vegetation/shrub_points.json", []) or []
        metadata = _read_json(runtime / "vegetation/metadata.json", {}) or {}
        self._write_effective_store(runtime, trees, shrubs, metadata)

    def materialize(self, revision_id: str, *, force: bool = False) -> dict[str, Any]:
        with self._store(journal=False) as store:
            revision = store.plan_revision(revision_id)
        if revision is None:
            raise PlanError("unknown plan revision", code="revision_not_found",
                            revision_id=revision_id)
        content_hash = revision["content_hash"]
        final_root = self.cache_dir / content_hash
        manifest_path = final_root / "plan-manifest.json"
        existing = _read_json(manifest_path)
        if (existing or {}).get("materializer_version") != MATERIALIZER_VERSION:
            existing = None
        if existing is not None and not force:
            return self._revision_runtime_manifest(revision, existing)

        with self.locked():
            existing = _read_json(manifest_path)
            if (existing or {}).get("materializer_version") != MATERIALIZER_VERSION:
                existing = None
            if existing is not None and not force:
                return self._revision_runtime_manifest(revision, existing)
            base = self._base_manifest(revision["base_id"])
            base_root = self.bases_dir / revision["base_id"]
            grid = _read_json(base_root / "terrain/grid.json")
            if not isinstance(grid, dict):
                raise PlanError("planning baseline has no terrain grid", code="plan_base_missing")
            baseline_trees = _read_json(base_root / "vegetation/tree_instances.json", []) or []
            baseline_shrubs = _read_json(base_root / "vegetation/shrub_points.json", []) or []
            scene = _read_json(base_root / "scene.json", {}) or {}
            vegetation_meta = _read_json(base_root / "vegetation/metadata.json", {}) or {}

            baseline_dem, xx, yy, xstep, ystep = _grid_arrays(grid)
            total_delta = np.zeros(baseline_dem.shape, dtype=float)
            remove_ids: set[str] = set()
            add_trees: list[dict[str, Any]] = []
            add_shrubs: list[dict[str, Any]] = []
            features: list[dict[str, Any]] = []
            for edit in revision["edits"]:
                total_delta += _terrain_delta(edit, xx, yy)
                if edit["kind"] == "vegetation_remove":
                    remove_ids.update(edit.get("params", {}).get("entity_ids") or [])
                trees, shrubs = _vegetation_additions(edit)
                add_trees.extend(trees)
                add_shrubs.extend(shrubs)
                if edit["kind"] in {"swale", "orchard", "garden"}:
                    features.append({
                        "type": "Feature",
                        "id": edit["edit_id"],
                        "geometry": edit.get("geometry"),
                        "properties": {"kind": edit["kind"], "label": edit.get("label"),
                                       **(edit.get("params") or {})},
                    })
            clipped_cells = int(np.sum(np.abs(total_delta) > MAX_EARTH_DELTA_M))
            total_delta = np.clip(total_delta, -MAX_EARTH_DELTA_M, MAX_EARTH_DELTA_M)
            valid = np.isfinite(baseline_dem)
            effective_dem = np.where(valid, baseline_dem + total_delta, np.nan)
            effective_grid = dict(grid)
            effective_grid["heights"] = [
                None if not math.isfinite(v) else round(float(v), 3)
                for v in effective_dem.ravel()
            ]
            finite = effective_dem[np.isfinite(effective_dem)]
            effective_grid["minElevation"] = round(float(np.min(finite)), 3)
            effective_grid["maxElevation"] = round(float(np.max(finite)), 3)
            effective_grid["source"] = {
                "kind": "plan_land_artifact",
                "base_id": revision["base_id"],
                "content_hash": content_hash,
            }

            trees = [row for row in baseline_trees if str(row.get("id")) not in remove_ids]
            shrubs = [row for row in baseline_shrubs if str(row.get("id")) not in remove_ids]
            trees.extend(row for row in add_trees if str(row.get("id")) not in remove_ids)
            shrubs.extend(row for row in add_shrubs if str(row.get("id")) not in remove_ids)

            cell_area = abs(xstep * ystep)
            cut = np.where(valid, np.maximum(0.0, -total_delta), 0.0)
            fill = np.where(valid, np.maximum(0.0, total_delta), 0.0)
            canopy = _canopy_grid(effective_grid, trees, shrubs)
            canopy_values = np.asarray(
                [[0.0 if v is None else float(v) for v in row] for row in canopy["values"]])
            canopy_cover = float(np.mean(canopy_values[valid] > 0.5) * 100.0) if valid.any() else 0.0
            effective_meta = {
                **vegetation_meta,
                "tree_count": len(trees),
                "shrub_count": len(shrubs),
                "canopy_cover_pct": round(canopy_cover, 1),
                "planned_content_hash": content_hash,
            }
            diff = {
                "terrain": {
                    "cut_m3": round(float(np.sum(cut) * cell_area), 2),
                    "fill_m3": round(float(np.sum(fill) * cell_area), 2),
                    "net_fill_m3": round(float((np.sum(fill) - np.sum(cut)) * cell_area), 2),
                    "disturbed_m2": round(float(np.sum(np.abs(total_delta) > 1e-6) * cell_area), 2),
                    "max_cut_m": round(float(np.max(cut)), 3),
                    "max_fill_m": round(float(np.max(fill)), 3),
                    "analysis_cell_m": round((abs(xstep) + abs(ystep)) / 2.0, 3),
                    "cumulative_delta_limit_m": MAX_EARTH_DELTA_M,
                    "clipped_cells": clipped_cells,
                },
                "vegetation": {
                    "baseline_trees": len(baseline_trees),
                    "effective_trees": len(trees),
                    "trees_added": len(add_trees),
                    "baseline_shrubs": len(baseline_shrubs),
                    "effective_shrubs": len(shrubs),
                    "shrubs_added": len(add_shrubs),
                    "entities_removed": len(remove_ids),
                    "canopy_cover_pct": round(canopy_cover, 1),
                },
            }

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temp_root = self.cache_dir / (".tmp-" + uuid.uuid4().hex)
            if temp_root.exists():
                shutil.rmtree(temp_root)
            temp_root.mkdir(parents=True)
            terrain_dir = temp_root / "terrain"
            vegetation_dir = temp_root / "vegetation"
            terrain_dir.mkdir(exist_ok=True)
            vegetation_dir.mkdir(exist_ok=True)
            distant_source = self.data_dir / "terrain/distant"
            if distant_source.is_dir():
                self._link_or_copy(distant_source, terrain_dir / "distant")
            _atomic_json(terrain_dir / "grid.json", effective_grid)
            aoi_source = base_root / "terrain/aoi_local.geojson"
            if aoi_source.exists():
                shutil.copy2(aoi_source, terrain_dir / "aoi_local.geojson")
            apron_source = base_root / "terrain/grid.apron.json"
            if apron_source.exists():
                apron = _read_json(apron_source)
                if isinstance(apron, dict):
                    apron["minElevation"] = effective_grid["minElevation"]
                    _atomic_json(terrain_dir / "grid.apron.json", apron)
            _atomic_json(vegetation_dir / "tree_instances.json", trees)
            _atomic_json(vegetation_dir / "shrub_points.json", shrubs)
            _atomic_json(vegetation_dir / "metadata.json", effective_meta, indent=2)
            _atomic_json(vegetation_dir / "canopy_height.grid.json", canopy)
            _atomic_json(temp_root / "plan-features.geojson",
                         {"type": "FeatureCollection", "features": features})
            _atomic_json(temp_root / "diff.json", diff, indent=2)

            url_root = f"/data/plans/cache/{content_hash}"
            plan_scene = json.loads(json.dumps(scene))
            plan_scene["plan"] = {
                "content_hash": content_hash,
                "base_id": revision["base_id"],
            }
            plan_scene.setdefault("terrain", {})["grid_url"] = url_root + "/terrain/grid.json"
            veg = plan_scene.setdefault("vegetation", {})
            veg.update({
                "tree_instances_url": url_root + "/vegetation/tree_instances.json",
                "shrub_points_url": url_root + "/vegetation/shrub_points.json",
                "tree_count": len(trees),
                "shrub_anchor_count": len(shrubs),
                "canopy_height_grid_url": url_root + "/vegetation/canopy_height.grid.json",
                "status": "ready",
            })
            _atomic_json(temp_root / "scene.json", plan_scene, indent=2)

            manifest = {
                "materializer_version": MATERIALIZER_VERSION,
                "content_hash": content_hash,
                "base_id": revision["base_id"],
                "base_fingerprint": base.get("fingerprint"),
                "asset_root": url_root,
                "data_dir": str(final_root),
                "scene_url": url_root + "/scene.json",
                "terrain_grid_url": url_root + "/terrain/grid.json",
                "tree_instances_url": url_root + "/vegetation/tree_instances.json",
                "shrub_points_url": url_root + "/vegetation/shrub_points.json",
                "canopy_grid_url": url_root + "/vegetation/canopy_height.grid.json",
                "features_url": url_root + "/plan-features.geojson",
                "diff_url": url_root + "/diff.json",
                "diff": diff,
            }
            _atomic_json(temp_root / "plan-manifest.json", manifest, indent=2)
            if final_root.exists():
                shutil.rmtree(final_root)
            os.replace(temp_root, final_root)
            return self._revision_runtime_manifest(revision, manifest)

    @staticmethod
    def _number(value: Any, default: float, lo: float, hi: float) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            out = default
        if not math.isfinite(out):
            out = default
        return min(hi, max(lo, out))

    def _run_json_process(self, argv: list[str], runtime: Path,
                          *, timeout: int = 300, stdin: Any = None,
                          expect_json: bool = True) -> dict[str, Any]:
        env = {**os.environ, "TWIN_DATA_DIR": str(runtime)}
        try:
            proc = subprocess.run(
                argv, cwd=twin_store.PROJECT, env=env,
                input=None if stdin is None else json.dumps(stdin),
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise PlanError("plan simulation timed out", code="simulation_timeout",
                            timeout_seconds=timeout) from exc
        lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
        payload = None
        for line in reversed(lines):
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict):
                    payload = candidate
                    break
            except ValueError:
                continue
        if proc.returncode != 0:
            message = (payload or {}).get("message") or (payload or {}).get("error") \
                or proc.stderr.strip()[-800:] or f"simulation exited {proc.returncode}"
            raise PlanError(str(message), code="simulation_failed",
                            stderr=proc.stderr.strip()[-800:])
        if payload is None and not expect_json:
            return {"ok": True, "stdout": proc.stdout[-800:]}
        if payload is None:
            raise PlanError("simulation returned no JSON result", code="simulation_failed",
                            stdout=proc.stdout[-800:], stderr=proc.stderr[-800:])
        return payload

    def _ensure_hydrology(self, runtime: Path, revision_id: str) -> None:
        marker = runtime / "hydrology" / "plan-revision.json"
        current = _read_json(marker, {}) or {}
        if current.get("revision_id") == revision_id and (runtime / "hydrology/summary.json").exists():
            return
        self._run_json_process(
            [sys.executable, str(Path(twin_store.PROJECT) / "scripts/analyze_hydrology.py"),
             "--data-dir", str(runtime)], runtime, timeout=300, expect_json=False)
        _atomic_json(marker, {"revision_id": revision_id})

    def _ensure_fuels(self, runtime: Path, revision_id: str) -> None:
        marker = runtime / "fire" / "plan-revision.json"
        current = _read_json(marker, {}) or {}
        if current.get("revision_id") == revision_id and (runtime / "fire/summary.json").exists():
            return
        self._run_json_process(
            [sys.executable, str(Path(twin_store.PROJECT) / "scripts/analyze_fuels.py"),
             "--data-dir", str(runtime), "--fuel-source", "computed"],
            runtime, timeout=300, expect_json=False)
        _atomic_json(marker, {"revision_id": revision_id, "fuel_source": "computed"})

    def _ensure_et(self, runtime: Path, content_hash: str) -> None:
        self._seed_runtime_dir(runtime, "et")
        marker = runtime / "et" / "plan-land.json"
        current = _read_json(marker, {}) or {}
        if (current.get("content_hash") == content_hash
                and (runtime / "et/summary.json").exists()):
            return
        self._run_json_process(
            [sys.executable, str(Path(twin_store.PROJECT) / "scripts/et_water_balance.py"),
             "--data-dir", str(runtime)],
            runtime, timeout=600, expect_json=False)
        _atomic_json(marker, {"content_hash": content_hash})

    def _hydrology_argv(self, params: dict[str, Any]) -> list[str]:
        mode = "rain" if params.get("mode") == "rain" else "snowmelt"
        argv = [sys.executable, str(Path(twin_store.PROJECT) / "scripts/hydro_scenario.py"),
                "--json", "--mode", mode]
        if mode == "snowmelt":
            if isinstance(params.get("swe_in"), (int, float)):
                argv += ["--swe-in", str(self._number(params["swe_in"], 7, 0, 40))]
            elif params.get("preset") in {"median", "p90", "max"}:
                argv += ["--preset", params["preset"]]
            argv += ["--melt-days", str(self._number(params.get("melt_days"), 4, 0.5, 30))]
        else:
            argv += ["--storm-hours", str(self._number(params.get("storm_hours"), 12, 0.5, 240))]
        argv += ["--rain-in", str(self._number(params.get("rain_in"), 0, 0, 15))]
        if params.get("antecedent") in {"dry", "normal", "wet"}:
            argv += ["--antecedent", params["antecedent"]]
        if params.get("frozen") is True:
            argv += ["--frozen"]
        return argv

    def _fire_argv(self, params: dict[str, Any]) -> list[str]:
        if not isinstance(params.get("ignition_x"), (int, float)) or not isinstance(params.get("ignition_y"), (int, float)):
            raise PlanError("fire simulation needs ignition_x and ignition_y", code="invalid_request")
        argv = [sys.executable, str(Path(twin_store.PROJECT) / "scripts/fire_scenario.py"),
                "--json", "--ignition-x", str(float(params["ignition_x"])),
                "--ignition-y", str(float(params["ignition_y"])),
                "--fuel-source", "computed"]
        choices = {
            "weather_class": ({"normal_spring", "high_spring", "extreme_redflag",
                               "summer_drought", "dormant_fall", "custom"}, "--weather-class"),
            "hydrology": ({"on", "off"}, "--hydrology"),
            "drought": ({"normal", "dry", "severe", "extreme"}, "--drought"),
            "exposure": ({"shaded", "mixed", "open"}, "--exposure"),
        }
        for key, (valid, flag) in choices.items():
            if params.get(key) in valid:
                argv += [flag, params[key]]
        ranges = {
            "wind_mph": ("--wind-mph", 0, 120), "wind_dir": ("--wind-dir", 0, 360),
            "temp_f": ("--temp-f", -20, 130), "rh_min": ("--rh-min", 1, 100),
            "days_since_rain": ("--days-since-rain", 0, 120),
            "duration_min": ("--duration-min", 1, 1440),
            "fmc_override": ("--fmc-override", 75, 140),
        }
        for key, (flag, lo, hi) in ranges.items():
            if isinstance(params.get(key), (int, float)):
                value = self._number(params[key], lo, lo, hi)
                if key == "wind_dir":
                    value = float(params[key]) % 360.0
                argv += [flag, str(value)]
        date = params.get("date")
        if isinstance(date, str) and len(date) == 10:
            argv += ["--date", date]
        return argv

    def _et_argv(self, params: dict[str, Any]) -> list[str]:
        argv = [sys.executable, str(Path(twin_store.PROJECT) / "scripts/et_scenario.py"), "--json"]
        if isinstance(params.get("date"), str):
            argv += ["--date", params["date"]]
        numeric = {
            "tmax_c": ("--tmax-c", -60, 60, 30),
            "tmin_c": ("--tmin-c", -60, 60, 15),
            "rh_pct": ("--rh-pct", 1, 100, 45),
            "wind_m_s": ("--wind-m-s", 0, 30, 2),
            "rain_mm": ("--rain-mm", 0, 300, 0),
            "days": ("--days", 1, 60, 1),
        }
        for key, (flag, lo, hi, default) in numeric.items():
            if key in params:
                value = self._number(params[key], default, lo, hi)
                argv += [flag, str(int(value) if key == "days" else value)]
        if params.get("sky") in {"clear", "partly", "cloudy", "overcast"}:
            argv += ["--sky", params["sky"]]
        if params.get("soil_state") in {"current", "dry", "wet", "auto"}:
            argv += ["--soil-state", params["soil_state"]]
        return argv

    def run_simulation(self, plan_id: str, revision_id: str, simulator: str,
                       parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        parameters = dict(parameters or {})
        simulator = str(simulator or "")
        valid = {"hydrology", "fire", "et", "solar", "solar_site", "viewshed"}
        if simulator not in valid:
            raise PlanError("unsupported plan simulator", code="invalid_request",
                            valid_simulators=sorted(valid))
        # get_plan performs the ancestry/reachability check. Historical
        # revisions are immutable and safe to simulate directly; branching is
        # needed only when the user wants to edit from that older state.
        self.get_plan(plan_id, revision_id=revision_id)
        manifest = self.materialize(revision_id)
        runtime = Path(manifest["data_dir"])
        plan_run_id = "planrun_" + uuid.uuid4().hex[:20]
        created_at = twin_store.utcnow()
        input_hash = _hash_bytes(_canonical({
            "content_hash": manifest["content_hash"], "simulator": simulator,
            "parameters": parameters,
        }).encode())
        with self._store() as store:
            store.upsert_plan_simulation_run(
                plan_run_id, plan_id, revision_id, simulator, "running",
                parameters, input_hash=input_hash, created_at=created_at)
        try:
            with self.locked():
                self._ensure_effective_store(runtime)
                if simulator == "hydrology":
                    self._ensure_hydrology(runtime, revision_id)
                    result = self._run_json_process(self._hydrology_argv(parameters), runtime, timeout=300)
                elif simulator == "fire":
                    if parameters.get("hydrology", "on") != "off":
                        self._ensure_hydrology(runtime, revision_id)
                    self._ensure_fuels(runtime, revision_id)
                    result = self._run_json_process(self._fire_argv(parameters), runtime, timeout=300)
                elif simulator == "et":
                    self._ensure_et(runtime, manifest["content_hash"])
                    result = self._run_json_process(self._et_argv(parameters), runtime, timeout=300)
                elif simulator == "solar":
                    argv = [sys.executable, str(Path(twin_store.PROJECT) / "scripts/analyze_solar.py"),
                            "--data-dir", str(runtime), "--json",
                            "--surface", "bare_earth" if parameters.get("surface") == "bare_earth" else "canopy",
                            "--samples", str(int(self._number(parameters.get("samples"), 220, 40, 600))),
                            "--system-kw", str(self._number(parameters.get("system_kw"), 1, 0.05, 200))]
                    result = self._run_json_process(argv, runtime, timeout=600)
                elif simulator in {"solar_site", "viewshed"}:
                    argv = [sys.executable, str(Path(twin_store.PROJECT) / "scripts/plan_site_query.py"),
                            "--data-dir", str(runtime), "--kind", simulator]
                    result = self._run_json_process(argv, runtime, timeout=300, stdin=parameters)
                else:  # pragma: no cover - valid set above is exhaustive
                    raise AssertionError(simulator)
            result = dict(result)
            result["plan"] = {
                "plan_id": plan_id, "revision_id": revision_id,
                "content_hash": manifest["content_hash"], "plan_run_id": plan_run_id,
            }
            result["plan_effects"] = {
                "hydrology": {
                    "terrain": "effective planned elevation and depressions",
                    "vegetation": "not an input to the terrain/SSURGO event solver",
                },
                "fire": {
                    "terrain": "effective planned slope/elevation",
                    "vegetation": "effective crown footprint and height; CBH/CBD for new plants are screening defaults",
                },
                "et": {
                    "terrain": "effective wetness redistribution",
                    "vegetation": "effective canopy-cover water balance",
                },
                "solar": {
                    "terrain": "effective terrain horizon",
                    "vegetation": "effective crown clearance and canopy horizon",
                },
                "solar_site": {
                    "terrain": "effective terrain horizon",
                    "vegetation": "effective crown clearance and canopy horizon",
                },
                "viewshed": {
                    "terrain": "effective ground surface",
                    "vegetation": "effective canopy blockers",
                },
            }[simulator]
            runs_dir = self.plans_dir / "runs" / plan_run_id
            runs_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(runs_dir / "result.json", result, indent=2)
            artifact_path = str(runs_dir.relative_to(self.data_dir))
            with self._store() as store:
                store.upsert_plan_simulation_run(
                    plan_run_id, plan_id, revision_id, simulator, "complete",
                    parameters, result=result, artifact_path=artifact_path,
                    input_hash=input_hash, created_at=created_at,
                    finished_at=twin_store.utcnow())
            return result
        except Exception as exc:
            error_payload = exc.payload if isinstance(exc, PlanError) else {
                "error": "simulation_failed", "message": str(exc)}
            with self._store() as store:
                store.upsert_plan_simulation_run(
                    plan_run_id, plan_id, revision_id, simulator, "failed",
                    parameters, result=error_payload, input_hash=input_hash,
                    created_at=created_at, finished_at=twin_store.utcnow())
            if isinstance(exc, PlanError):
                raise
            raise PlanError(str(exc), code="simulation_failed") from exc


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"))
    parser.add_argument("--revision")
    args = parser.parse_args()
    engine = PlanEngine(args.data_dir)
    if args.revision:
        print(json.dumps(engine.materialize(args.revision), indent=2))
    else:
        print(json.dumps(engine.list_plans(), indent=2))


if __name__ == "__main__":
    main()
