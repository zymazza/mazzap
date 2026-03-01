#!/usr/bin/env python3
"""Headless Blender pipeline for clipping/baking a building photogrammetry mesh."""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import bpy
import bmesh
from mathutils import Matrix, Vector


def log(message: str) -> None:
    print(f"[bake_and_clip] {message}")


def parse_cli_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []

    parser = argparse.ArgumentParser(description="Clip and bake a building mesh in Blender.")
    parser.add_argument("--input_mesh", required=True, help="Input OBJ/PLY/GLB path")
    parser.add_argument("--footprints", required=True, help="Footprints GeoJSON path")
    parser.add_argument("--footprint_id", default=None, help="Footprint identifier")
    parser.add_argument("--feature_index", type=int, default=None, help="Footprint feature index")
    parser.add_argument("--origin_utm", nargs=2, type=float, required=True, metavar=("X", "Y"))

    parser.add_argument("--matrix", default=None, help="Path to 4x4 matrix JSON")
    parser.add_argument("--translate", nargs=3, type=float, default=[0.0, 0.0, 0.0], metavar=("X", "Y", "Z"))
    parser.add_argument("--rotate_deg", nargs=3, type=float, default=[0.0, 0.0, 0.0], metavar=("YAW", "PITCH", "ROLL"))
    parser.add_argument("--scale", type=float, default=1.0, help="Uniform scale")

    parser.add_argument("--z_min", type=float, default=-10.0)
    parser.add_argument("--z_max", type=float, default=200.0)
    parser.add_argument("--clip_margin", type=float, default=0.25)
    parser.add_argument(
        "--clip_mode",
        choices=["none", "ground_outside"],
        default="none",
        help="Clipping strategy. Use none to preserve full mesh geometry."
    )
    parser.add_argument(
        "--target_faces",
        type=int,
        default=250000,
        help="Target face budget for decimation (0 disables decimation)."
    )

    parser.add_argument("--ground_cut_mode", choices=["none", "below_dem", "below_z"], default="none")
    parser.add_argument("--ground_z", type=float, default=0.0)
    parser.add_argument("--ground_eps", type=float, default=0.2)

    parser.add_argument("--dem", default=None, help="Optional DEM tif path")
    parser.add_argument("--tex_size", type=int, default=2048)
    parser.add_argument(
        "--texture_mode",
        choices=["preserve_multi_material", "reuse_existing", "bake_basecolor"],
        default="preserve_multi_material",
        help="Photogrammetry-safe texture strategy."
    )
    parser.add_argument("--unwrap_angle_limit", type=float, default=66.0)
    parser.add_argument("--unwrap_island_margin", type=float, default=0.02)
    parser.add_argument(
        "--export_tangents",
        action="store_true",
        help="Export tangents in GLB (disabled by default for photogrammetry-safe exports)."
    )

    # Legacy switches retained for backward compatibility.
    parser.add_argument("--bake_albedo", dest="bake_albedo", action="store_true")
    parser.add_argument("--no_bake_albedo", dest="bake_albedo", action="store_false")
    parser.set_defaults(bake_albedo=False)

    parser.add_argument("--bake_normal", dest="bake_normal", action="store_true")
    parser.add_argument("--no_bake_normal", dest="bake_normal", action="store_false")
    parser.set_defaults(bake_normal=False)

    parser.add_argument("--bake_ao", dest="bake_ao", action="store_true")
    parser.add_argument("--no_bake_ao", dest="bake_ao", action="store_false")
    parser.set_defaults(bake_ao=False)

    parser.add_argument("--out_glb", required=True, help="Output GLB path")
    parser.add_argument("--out_textures_dir", required=True, help="Output textures directory")
    parser.add_argument("--out_raw_glb", default=None, help="Optional raw baked GLB path")

    return parser.parse_args(argv)


def ensure_parent_dirs(paths: Sequence[str]) -> None:
    for p in paths:
        if not p:
            continue
        parent = os.path.dirname(os.path.abspath(p))
        if parent:
            os.makedirs(parent, exist_ok=True)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in list(bpy.data.meshes):
        if block.users == 0:
            bpy.data.meshes.remove(block)
    for block in list(bpy.data.materials):
        if block.users == 0:
            bpy.data.materials.remove(block)
    for block in list(bpy.data.images):
        if block.users == 0:
            bpy.data.images.remove(block)


def import_mesh(filepath: str) -> List[bpy.types.Object]:
    before = set(obj.name for obj in bpy.data.objects)
    lower = filepath.lower()

    if lower.endswith(".obj"):
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=filepath)
        else:
            bpy.ops.import_scene.obj(filepath=filepath)
    elif lower.endswith(".ply"):
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(filepath=filepath)
        else:
            bpy.ops.import_mesh.ply(filepath=filepath)
    elif lower.endswith(".glb") or lower.endswith(".gltf"):
        bpy.ops.import_scene.gltf(filepath=filepath)
    else:
        raise RuntimeError(f"Unsupported mesh format: {filepath}")

    imported = [obj for obj in bpy.data.objects if obj.name not in before and obj.type == "MESH"]
    if not imported:
        raise RuntimeError("No mesh objects were imported.")
    return imported


def join_meshes(meshes: List[bpy.types.Object]) -> bpy.types.Object:
    if len(meshes) == 1:
        return meshes[0]

    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def load_matrix_file(matrix_path: str) -> Matrix:
    with open(matrix_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict) and "matrix" in data:
        data = data["matrix"]

    if isinstance(data, list) and len(data) == 16:
        rows = [data[0:4], data[4:8], data[8:12], data[12:16]]
        return Matrix(rows)

    if isinstance(data, list) and len(data) == 4 and all(isinstance(row, list) and len(row) == 4 for row in data):
        return Matrix(data)

    raise RuntimeError(f"Invalid matrix JSON at {matrix_path}; expected list[16] or list[4][4].")


def apply_user_transform(obj: bpy.types.Object, args: argparse.Namespace) -> None:
    if args.matrix:
        transform = load_matrix_file(args.matrix)
        obj.matrix_world = transform @ obj.matrix_world
        return

    tx, ty, tz = args.translate
    yaw_deg, pitch_deg, roll_deg = args.rotate_deg
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)

    rotation = (
        Matrix.Rotation(yaw, 4, "Z")
        @ Matrix.Rotation(pitch, 4, "X")
        @ Matrix.Rotation(roll, 4, "Y")
    )

    scale = float(args.scale)
    scale_m = Matrix.Diagonal((scale, scale, scale, 1.0))
    translation = Matrix.Translation(Vector((tx, ty, tz)))

    obj.matrix_world = translation @ rotation @ scale_m @ obj.matrix_world


def polygon_area_2d(coords: Sequence[Sequence[float]]) -> float:
    if len(coords) < 3:
        return 0.0
    area = 0.0
    for i in range(len(coords)):
        x1, y1 = coords[i][0], coords[i][1]
        x2, y2 = coords[(i + 1) % len(coords)][0], coords[(i + 1) % len(coords)][1]
        area += x1 * y2 - x2 * y1
    return 0.5 * area


def normalize_ring(ring: Sequence[Sequence[float]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for point in ring:
        if not isinstance(point, list) or len(point) < 2:
            continue
        x = float(point[0])
        y = float(point[1])
        out.append((x, y))

    if len(out) >= 2 and out[0] == out[-1]:
        out = out[:-1]
    return out


def feature_candidate_ids(feature: dict, index: int) -> List[str]:
    props = feature.get("properties") or {}
    base_values = [
        props.get("BuildingID"),
        props.get("BUILDINGID"),
        props.get("BLDG_ID"),
        props.get("OBJECTID"),
        props.get("ObjectID"),
        props.get("FID"),
        props.get("id"),
        feature.get("id"),
    ]

    ids: List[str] = []
    for value in base_values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        ids.append(text)
        ids.append(f"B-{text}")

    ids.append(f"B-{index + 1}")
    ids.append(str(index))
    ids.append(str(index + 1))
    return ids


def select_footprint_feature(geojson: dict, footprint_id: Optional[str], feature_index: Optional[int]) -> Tuple[dict, int]:
    features = geojson.get("features") or []
    if not isinstance(features, list) or not features:
        raise RuntimeError("No features found in footprints GeoJSON.")

    if feature_index is not None:
        if feature_index < 0 or feature_index >= len(features):
            raise RuntimeError(f"feature_index {feature_index} is out of range (0..{len(features)-1}).")
        return features[feature_index], feature_index

    if footprint_id:
        lookup = str(footprint_id).strip().lower()
        for idx, feature in enumerate(features):
            for candidate in feature_candidate_ids(feature, idx):
                if candidate.lower() == lookup:
                    return feature, idx

        match = re.match(r"^b-(\d+)$", lookup)
        if match:
            inferred_idx = int(match.group(1)) - 1
            if 0 <= inferred_idx < len(features):
                return features[inferred_idx], inferred_idx

        raise RuntimeError(f"Unable to locate footprint_id '{footprint_id}' in GeoJSON.")

    return features[0], 0


def extract_polygons_from_feature(feature: dict) -> List[List[List[Tuple[float, float]]]]:
    geometry = feature.get("geometry") or {}
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    polygons: List[List[List[Tuple[float, float]]]] = []
    if gtype == "Polygon":
        if isinstance(coords, list):
            rings = [normalize_ring(ring) for ring in coords if isinstance(ring, list)]
            rings = [ring for ring in rings if len(ring) >= 3]
            if rings:
                polygons.append(rings)
    elif gtype == "MultiPolygon":
        if isinstance(coords, list):
            for poly in coords:
                if not isinstance(poly, list):
                    continue
                rings = [normalize_ring(ring) for ring in poly if isinstance(ring, list)]
                rings = [ring for ring in rings if len(ring) >= 3]
                if rings:
                    polygons.append(rings)
    else:
        raise RuntimeError(f"Unsupported footprint geometry type: {gtype}")

    if not polygons:
        raise RuntimeError("Selected footprint has no valid polygon rings.")

    return polygons


def choose_largest_polygon(polygons: List[List[List[Tuple[float, float]]]]) -> List[List[Tuple[float, float]]]:
    largest = None
    largest_area = -1.0
    for rings in polygons:
        outer = rings[0]
        area = abs(polygon_area_2d(outer))
        if area > largest_area:
            largest = rings
            largest_area = area
    if largest is None:
        raise RuntimeError("Could not choose a polygon from footprint geometry.")
    return largest


def utm_ring_to_blender_xy(ring: List[Tuple[float, float]], origin_x: float, origin_y: float) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for x, y in ring:
        local_x = x - origin_x
        local_y = y - origin_y
        out.append((local_x, -local_y))
    return out


def ring_centroid_xy(ring_xy: List[Tuple[float, float]]) -> Tuple[float, float]:
    if not ring_xy:
        return (0.0, 0.0)
    sx = sum(p[0] for p in ring_xy)
    sy = sum(p[1] for p in ring_xy)
    n = float(len(ring_xy))
    return (sx / n, sy / n)


def ring_span_xy(ring_xy: List[Tuple[float, float]]) -> float:
    if not ring_xy:
        return 0.0
    min_x = min(p[0] for p in ring_xy)
    max_x = max(p[0] for p in ring_xy)
    min_y = min(p[1] for p in ring_xy)
    max_y = max(p[1] for p in ring_xy)
    return max(max_x - min_x, max_y - min_y)


def mesh_world_bbox_center_xy(obj: bpy.types.Object) -> Tuple[float, float]:
    world_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    cx = sum(v.x for v in world_corners) / len(world_corners)
    cy = sum(v.y for v in world_corners) / len(world_corners)
    return (cx, cy)


def mesh_world_bbox_span_xy(obj: bpy.types.Object) -> float:
    world_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    min_x = min(v.x for v in world_corners)
    max_x = max(v.x for v in world_corners)
    min_y = min(v.y for v in world_corners)
    max_y = max(v.y for v in world_corners)
    return max(max_x - min_x, max_y - min_y)


def shifted_ring(ring_xy: List[Tuple[float, float]], dx: float, dy: float) -> List[Tuple[float, float]]:
    return [(p[0] + dx, p[1] + dy) for p in ring_xy]


def scale_ring_about_centroid(ring_xy: List[Tuple[float, float]], factor: float) -> List[Tuple[float, float]]:
    cx, cy = ring_centroid_xy(ring_xy)
    return [((x - cx) * factor + cx, (y - cy) * factor + cy) for x, y in ring_xy]


def maybe_scale_ring_to_mesh_span(ring_xy: List[Tuple[float, float]], mesh_obj: bpy.types.Object) -> List[Tuple[float, float]]:
    ring_span = ring_span_xy(ring_xy)
    mesh_span = mesh_world_bbox_span_xy(mesh_obj)
    if ring_span <= 1e-9 or mesh_span <= 1e-9:
        return ring_xy

    ratio = mesh_span / ring_span
    if 0.2 <= ratio <= 5.0:
        return ring_xy

    scaled = scale_ring_about_centroid(ring_xy, ratio)
    log(
        "Footprint and mesh scale appear mismatched; scaling footprint ring by "
        f"{ratio:.4f} (ring span {ring_span:.3f}m, mesh span {mesh_span:.3f} units)"
    )
    return scaled


def pca_orientation_and_spans(points_xy: List[Tuple[float, float]]) -> Tuple[Tuple[float, float], float, float, float]:
    if not points_xy:
        return ((0.0, 0.0), 0.0, 1.0, 1.0)

    cx = sum(p[0] for p in points_xy) / len(points_xy)
    cy = sum(p[1] for p in points_xy) / len(points_xy)

    cxx = 0.0
    cyy = 0.0
    cxy = 0.0
    for x, y in points_xy:
        dx = x - cx
        dy = y - cy
        cxx += dx * dx
        cyy += dy * dy
        cxy += dx * dy

    n = float(max(1, len(points_xy)))
    cxx /= n
    cyy /= n
    cxy /= n

    angle = 0.5 * math.atan2(2.0 * cxy, cxx - cyy)
    ux = math.cos(angle)
    uy = math.sin(angle)
    vx = -uy
    vy = ux

    min_u = float("inf")
    max_u = float("-inf")
    min_v = float("inf")
    max_v = float("-inf")

    for x, y in points_xy:
        dx = x - cx
        dy = y - cy
        pu = dx * ux + dy * uy
        pv = dx * vx + dy * vy
        min_u = min(min_u, pu)
        max_u = max(max_u, pu)
        min_v = min(min_v, pv)
        max_v = max(max_v, pv)

    span_u = max(max_u - min_u, 1e-9)
    span_v = max(max_v - min_v, 1e-9)
    return ((cx, cy), angle, span_u, span_v)


def convex_hull_2d(points_xy: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    unique = sorted(set(points_xy))
    if len(unique) <= 3:
        return unique

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: List[Tuple[float, float]] = []
    for p in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def convex_hull_area(points_xy: List[Tuple[float, float]]) -> float:
    if len(points_xy) < 3:
        return 0.0
    hull = convex_hull_2d(points_xy)
    if len(hull) < 3:
        return 0.0
    return abs(polygon_area_2d(hull))


def bbox_area_xy(points_xy: List[Tuple[float, float]]) -> float:
    if len(points_xy) < 2:
        return 0.0
    min_x = min(p[0] for p in points_xy)
    max_x = max(p[0] for p in points_xy)
    min_y = min(p[1] for p in points_xy)
    max_y = max(p[1] for p in points_xy)
    return max(0.0, (max_x - min_x) * (max_y - min_y))


def bottom_support_ratio(world_vertices: List[Vector]) -> Tuple[float, int]:
    if not world_vertices:
        return (0.0, 0)

    all_xy = [(v.x, v.y) for v in world_vertices]
    total_area = bbox_area_xy(all_xy)
    if total_area <= 1e-9:
        return (0.0, 0)

    z_values = [v.z for v in world_vertices]
    z_min = min(z_values)
    z_max = max(z_values)
    z_span = max(z_max - z_min, 1e-9)

    thresholds = [0.02, 0.04, 0.06, 0.10]
    bottom_xy: List[Tuple[float, float]] = []
    for frac in thresholds:
        z_lim = z_min + z_span * frac
        bottom_xy = [(v.x, v.y) for v in world_vertices if v.z <= z_lim]
        if len(bottom_xy) >= 30:
            break

    if len(bottom_xy) < 10:
        return (0.0, len(bottom_xy))

    bottom_area = bbox_area_xy(bottom_xy)
    ratio = max(0.0, min(1.0, bottom_area / total_area))
    return (ratio, len(bottom_xy))


def estimate_bottom_tilt_from_faces(
    mesh_obj: bpy.types.Object,
    matrix_world: Matrix,
    max_polys: int = 180000
) -> Tuple[float, int]:
    polygons = mesh_obj.data.polygons
    poly_count = len(polygons)
    if poly_count == 0:
        return (90.0, 0)

    step = max(1, int(math.ceil(poly_count / float(max_polys))))
    centers: List[Vector] = []
    sampled_indices: List[int] = []
    for idx in range(0, poly_count, step):
        p = polygons[idx]
        c = matrix_world @ p.center
        centers.append(c)
        sampled_indices.append(idx)

    if not centers:
        return (90.0, 0)

    z_values = [c.z for c in centers]
    z_min = min(z_values)
    z_max = max(z_values)
    z_span = max(z_max - z_min, 1e-9)
    z_limit = z_min + 0.15 * z_span

    mw3 = matrix_world.to_3x3()
    accum = Vector((0.0, 0.0, 0.0))
    weight_sum = 0.0
    used = 0
    up = Vector((0.0, 0.0, 1.0))

    for list_idx, poly_idx in enumerate(sampled_indices):
        if centers[list_idx].z > z_limit:
            continue
        poly = polygons[poly_idx]
        n = (mw3 @ poly.normal).normalized()
        if n.z < 0:
            n = -n
        w = max(poly.area, 1e-9)
        accum += n * w
        weight_sum += w
        used += 1

    if weight_sum <= 1e-9 or accum.length <= 1e-9:
        return (90.0, used)

    bottom_n = accum.normalized()
    dot = max(-1.0, min(1.0, bottom_n.dot(up)))
    tilt_deg = math.degrees(math.acos(dot))
    return (tilt_deg, used)


def sample_world_vertices(
    mesh_obj: bpy.types.Object,
    matrix_world: Optional[Matrix] = None,
    max_points: int = 120000
) -> List[Vector]:
    vertices = mesh_obj.data.vertices
    count = len(vertices)
    if count == 0:
        return []

    mw = matrix_world if matrix_world is not None else mesh_obj.matrix_world
    step = max(1, int(math.ceil(count / float(max_points))))

    sampled: List[Vector] = []
    for idx in range(0, count, step):
        sampled.append(mw @ vertices[idx].co)
    if step > 1:
        sampled.append(mw @ vertices[-1].co)
    return sampled


def mesh_points_xy_for_alignment(world_vertices: List[Vector]) -> List[Tuple[float, float]]:
    if not world_vertices:
        return []

    z_values = [v.z for v in world_vertices]
    bands = [
        (0.45, 0.65, 160),
        (0.35, 0.75, 120),
        (0.25, 0.80, 80),
    ]

    selected: List[Vector] = []
    for lo_q, hi_q, min_count in bands:
        z_lo = percentile(z_values, lo_q)
        z_hi = percentile(z_values, hi_q)
        selected = [v for v in world_vertices if z_lo <= v.z <= z_hi]
        if len(selected) >= min_count:
            break

    if len(selected) < 30:
        selected = world_vertices

    points_xy = [(v.x, v.y) for v in selected]
    if len(points_xy) < 3:
        return points_xy
    hull = convex_hull_2d(points_xy)
    return hull if len(hull) >= 3 else points_xy


def normalize_angle_pi(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def transform_points_xy(points: List[Tuple[float, float]], matrix: Matrix) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for x, y in points:
        p = matrix @ Vector((x, y, 0.0))
        out.append((p.x, p.y))
    return out


def alignment_fit_score(
    transformed_points: List[Tuple[float, float]],
    ring_xy: List[Tuple[float, float]],
    margin: float
) -> Tuple[float, float, float]:
    if not transformed_points or not ring_xy:
        return (-1e9, 0.0, 1e9)

    inside_count = 0
    outside_dist_sum = 0.0
    outside_count = 0
    for px, py in transformed_points:
        inside = point_in_ring_2d(px, py, ring_xy)
        if not inside and margin > 0:
            if distance_point_to_ring_2d(px, py, ring_xy) <= margin:
                inside = True
        if inside:
            inside_count += 1
        else:
            outside_count += 1
            outside_dist_sum += distance_point_to_ring_2d(px, py, ring_xy)

    inside_ratio = inside_count / float(len(transformed_points))
    avg_outside = (outside_dist_sum / float(max(1, outside_count)))
    ring_span = max(ring_span_xy(ring_xy), 1e-6)

    score = inside_ratio - 0.20 * (avg_outside / ring_span)
    return (score, inside_ratio, avg_outside)


def auto_orient_and_align_mesh_to_footprint(mesh_obj: bpy.types.Object, ring_xy: List[Tuple[float, float]]) -> None:
    if len(ring_xy) < 3:
        return

    (dst_cx, dst_cy), dst_angle, dst_u, dst_v = pca_orientation_and_spans(ring_xy)
    dst_area = max(dst_u * dst_v, 1e-9)
    dst_span = max(dst_u, dst_v, 1e-6)

    orientation_candidates = [
        ("none", Matrix.Identity(4)),
        ("x+90", Matrix.Rotation(math.radians(90.0), 4, "X")),
        ("x-90", Matrix.Rotation(math.radians(-90.0), 4, "X")),
        ("y+90", Matrix.Rotation(math.radians(90.0), 4, "Y")),
        ("y-90", Matrix.Rotation(math.radians(-90.0), 4, "Y")),
    ]

    best = None
    for orient_name, orient_rot in orientation_candidates:
        orient_world = orient_rot @ mesh_obj.matrix_world
        sampled_vertices = sample_world_vertices(mesh_obj, orient_world, max_points=120000)
        source_points = mesh_points_xy_for_alignment(sampled_vertices)
        if len(source_points) < 3:
            continue

        (src_cx, src_cy), src_angle, src_u, src_v = pca_orientation_and_spans(source_points)
        src_area = max(src_u * src_v, 1e-9)
        base_scale = math.sqrt(dst_area / src_area)
        base_scale = max(0.005, min(500.0, base_scale))

        yaw_base = normalize_angle_pi(dst_angle - src_angle)
        yaw_options = [
            yaw_base,
            normalize_angle_pi(yaw_base + math.pi),
            normalize_angle_pi(yaw_base + math.pi / 2.0),
            normalize_angle_pi(yaw_base - math.pi / 2.0),
        ]

        z_values = [v.z for v in sampled_vertices]
        z_span = max(max(z_values) - min(z_values), 1e-9)
        src_planar_span = max(src_u, src_v, 1e-9)
        upright_ratio = z_span / src_planar_span
        upright_penalty = 0.0
        if upright_ratio < 0.08:
            upright_penalty += 0.35
        elif upright_ratio < 0.15:
            upright_penalty += 0.15
        elif upright_ratio > 4.0:
            upright_penalty += 0.20

        for yaw in yaw_options:
            for scale_mult in (0.88, 1.0, 1.12):
                scale = base_scale * scale_mult
                transform = (
                    Matrix.Translation(Vector((dst_cx, dst_cy, 0.0)))
                    @ Matrix.Rotation(yaw, 4, "Z")
                    @ Matrix.Diagonal((scale, scale, scale, 1.0))
                    @ Matrix.Translation(Vector((-src_cx, -src_cy, 0.0)))
                )

                transformed = transform_points_xy(source_points, transform)
                fit_score, inside_ratio, avg_outside = alignment_fit_score(
                    transformed,
                    ring_xy,
                    margin=max(0.25, dst_span * 0.01),
                )

                transformed_vertices = [transform @ v for v in sampled_vertices]
                support_ratio, support_count = bottom_support_ratio(transformed_vertices)
                support_bonus = 0.10 * max(0.0, 0.08 - support_ratio)
                support_penalty = 0.35 * support_ratio
                if support_count < 30:
                    support_penalty += 0.08

                scale_penalty = 0.025 * abs(math.log(max(scale, 1e-9)))
                score = (
                    fit_score
                    + support_bonus
                    - support_penalty
                    - upright_penalty
                    - scale_penalty
                )

                if best is None or score > best["score"]:
                    best = {
                        "score": score,
                        "orient_name": orient_name,
                        "orient_rot": orient_rot,
                        "yaw": yaw,
                        "scale": scale,
                        "inside_ratio": inside_ratio,
                        "avg_outside": avg_outside,
                        "support_ratio": support_ratio,
                        "support_count": support_count,
                        "src_span": (src_u, src_v),
                        "transform": transform,
                    }

    if best is None:
        return

    final_transform = best["transform"] @ best["orient_rot"]
    mesh_obj.matrix_world = final_transform @ mesh_obj.matrix_world

    src_u, src_v = best["src_span"]
    (dst_cx, dst_cy), dst_angle, dst_u, dst_v = pca_orientation_and_spans(ring_xy)
    log(
        "Auto-orient+aligned mesh to footprint using mesh footprint fit "
        f"(orientation {best['orient_name']}, yaw {math.degrees(best['yaw']):.2f}deg, "
        f"uniform scale {best['scale']:.4f}, inside={best['inside_ratio']:.3f}, "
        f"avg_out={best['avg_outside']:.3f}, support={best['support_ratio']:.3f}/{best['support_count']}, "
        f"src_span=({src_u:.3f},{src_v:.3f}), "
        f"dst_span=({dst_u:.3f},{dst_v:.3f}))."
    )


def maybe_align_ring_to_mesh(ring_xy: List[Tuple[float, float]], mesh_obj: bpy.types.Object) -> List[Tuple[float, float]]:
    ring_cx, ring_cy = ring_centroid_xy(ring_xy)
    mesh_cx, mesh_cy = mesh_world_bbox_center_xy(mesh_obj)
    distance = math.hypot(mesh_cx - ring_cx, mesh_cy - ring_cy)
    threshold = max(20.0, ring_span_xy(ring_xy) * 2.5, mesh_world_bbox_span_xy(mesh_obj) * 2.5)

    if distance <= threshold:
        return ring_xy

    dx = mesh_cx - ring_cx
    dy = mesh_cy - ring_cy
    log(
        "Footprint appears misaligned with mesh; auto-aligning cutter by "
        f"dx={dx:.3f}, dy={dy:.3f} (distance {distance:.3f}m)"
    )
    return shifted_ring(ring_xy, dx, dy)


def duplicate_mesh_object(obj: bpy.types.Object, name_suffix: str) -> bpy.types.Object:
    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = f"{obj.name}_{name_suffix}"
    bpy.context.scene.collection.objects.link(dup)
    return dup


def make_prism_from_ring(name: str, ring_xy: List[Tuple[float, float]], z_min: float, z_max: float) -> bpy.types.Object:
    if len(ring_xy) < 3:
        raise RuntimeError("Ring has fewer than 3 points; cannot create cutter prism.")

    bm = bmesh.new()

    bottom_verts = [bm.verts.new((p[0], p[1], z_min)) for p in ring_xy]
    top_verts = [bm.verts.new((p[0], p[1], z_max)) for p in ring_xy]
    bm.verts.ensure_lookup_table()

    for i in range(len(ring_xy)):
        i2 = (i + 1) % len(ring_xy)
        try:
            bm.faces.new((bottom_verts[i], bottom_verts[i2], top_verts[i2], top_verts[i]))
        except ValueError:
            pass

    try:
        bm.faces.new(tuple(bottom_verts[::-1]))
    except ValueError:
        pass

    try:
        bm.faces.new(tuple(top_verts))
    except ValueError:
        pass

    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def apply_boolean_intersect(target_obj: bpy.types.Object, cutter_obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    target_obj.select_set(True)
    bpy.context.view_layer.objects.active = target_obj

    modifier = target_obj.modifiers.new(name="FootprintClip", type="BOOLEAN")
    modifier.operation = "INTERSECT"
    modifier.solver = "EXACT"
    modifier.object = cutter_obj
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def delete_faces_all_vertices_below_z(obj: bpy.types.Object, threshold_z: float) -> int:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")

    bm = bmesh.from_edit_mesh(obj.data)
    faces_to_delete = []
    for face in bm.faces:
        world_verts = [obj.matrix_world @ v.co for v in face.verts]
        if world_verts and all(v.z < threshold_z for v in world_verts):
            faces_to_delete.append(face)

    count = len(faces_to_delete)
    if faces_to_delete:
        bmesh.ops.delete(bm, geom=faces_to_delete, context="FACES")
        bmesh.update_edit_mesh(obj.data)

    bpy.ops.object.mode_set(mode="OBJECT")
    return count


def point_in_ring_2d(x: float, y: float, ring_xy: List[Tuple[float, float]]) -> bool:
    inside = False
    j = len(ring_xy) - 1
    for i in range(len(ring_xy)):
        xi, yi = ring_xy[i]
        xj, yj = ring_xy[j]
        denom = (yj - yi) if abs(yj - yi) > 1e-12 else 1e-12
        intersects = ((yi > y) != (yj > y)) and (x < ((xj - xi) * (y - yi) / denom + xi))
        if intersects:
            inside = not inside
        j = i
    return inside


def distance_point_to_segment_2d(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    vv = vx * vx + vy * vy
    if vv <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
    cx = ax + t * vx
    cy = ay + t * vy
    return math.hypot(px - cx, py - cy)


def distance_point_to_ring_2d(px: float, py: float, ring_xy: List[Tuple[float, float]]) -> float:
    if len(ring_xy) < 2:
        return float("inf")
    best = float("inf")
    for i in range(len(ring_xy)):
        ax, ay = ring_xy[i]
        bx, by = ring_xy[(i + 1) % len(ring_xy)]
        d = distance_point_to_segment_2d(px, py, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def trim_mesh_outside_ring_xy(obj: bpy.types.Object, ring_xy: List[Tuple[float, float]], margin: float) -> int:
    if len(ring_xy) < 3 or not obj.data or len(obj.data.polygons) == 0:
        return 0

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    faces_to_delete = []

    for face in bm.faces:
        world_center = obj.matrix_world @ face.calc_center_median()
        px = world_center.x
        py = world_center.y
        inside = point_in_ring_2d(px, py, ring_xy)
        if not inside and margin > 0:
            dist = distance_point_to_ring_2d(px, py, ring_xy)
            if dist <= margin:
                inside = True
        if not inside:
            faces_to_delete.append(face)

    removed = len(faces_to_delete)
    if faces_to_delete:
        bmesh.ops.delete(bm, geom=faces_to_delete, context="FACES")

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return removed


def percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    q = max(0.0, min(1.0, q))
    sorted_vals = sorted(values)
    index = q * (len(sorted_vals) - 1)
    lo = int(math.floor(index))
    hi = int(math.ceil(index))
    if lo == hi:
        return float(sorted_vals[lo])
    t = index - lo
    return float(sorted_vals[lo] * (1 - t) + sorted_vals[hi] * t)


def remove_ground_outside_footprint(
    obj: bpy.types.Object,
    ring_xy: List[Tuple[float, float]],
    margin: float
) -> Tuple[int, float, float]:
    if len(ring_xy) < 3 or not obj.data or len(obj.data.polygons) == 0:
        return (0, 0.0, 0.0)

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    z_samples = []
    for face in bm.faces:
        center = obj.matrix_world @ face.calc_center_median()
        z_samples.append(center.z)

    if not z_samples:
        bm.free()
        return (0, 0.0, 0.0)

    z05 = percentile(z_samples, 0.05)
    z95 = percentile(z_samples, 0.95)
    span = max(z95 - z05, 1e-6)
    ground_z_limit = z05 + span * 0.32

    faces_to_delete = []
    for face in bm.faces:
        center = obj.matrix_world @ face.calc_center_median()
        px = center.x
        py = center.y
        outside = not point_in_ring_2d(px, py, ring_xy)
        if outside and margin > 0:
            d = distance_point_to_ring_2d(px, py, ring_xy)
            if d <= margin:
                outside = False
        if not outside:
            continue

        near_ground = center.z <= ground_z_limit
        if near_ground:
            faces_to_delete.append(face)

    removed = len(faces_to_delete)
    if faces_to_delete:
        bmesh.ops.delete(bm, geom=faces_to_delete, context="FACES")

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return (removed, z05, ground_z_limit)


def sample_dem_value(dem_path: str, x: float, y: float) -> Optional[float]:
    if not dem_path or not os.path.exists(dem_path):
        return None

    commands = [
        ["gdallocationinfo", "-valonly", "-geoloc", dem_path, str(x), str(y)],
        ["gdallocationinfo", "-valonly", dem_path, str(x), str(y)],
    ]

    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return None

        if proc.returncode != 0:
            continue

        text = f"{proc.stdout}\n{proc.stderr}".strip()
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
        if not match:
            continue

        value = float(match.group(0))
        if not math.isfinite(value):
            continue
        if abs(value + 9999.0) < 1e-6:
            continue
        return value

    return None


def select_active_object(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def ensure_mesh_has_faces(obj: bpy.types.Object) -> None:
    mesh = obj.data
    if not mesh or len(mesh.vertices) == 0 or len(mesh.polygons) == 0:
        raise RuntimeError("Mesh has no faces after processing; cannot continue.")


def has_uv_map(obj: bpy.types.Object) -> bool:
    return bool(obj.data and obj.data.uv_layers and len(obj.data.uv_layers) > 0)


def smart_uv_project(obj: bpy.types.Object, angle_limit_deg: float, island_margin: float) -> None:
    ensure_mesh_has_faces(obj)
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    mesh = obj.data
    if not mesh.uv_layers:
        mesh.uv_layers.new(name="UVMap")

    select_active_object(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")

    angle_limit_deg = float(max(1.0, min(89.0, angle_limit_deg)))
    island_margin = float(max(0.0, min(1.0, island_margin)))

    smart_ok = False
    smart_error = None
    try:
        bpy.ops.uv.smart_project(
            angle_limit=angle_limit_deg,
            island_margin=island_margin,
        )
        smart_ok = True
    except Exception as exc:
        smart_error = exc

    if not smart_ok:
        try:
            bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=island_margin)
            log(
                "Smart UV Project failed; used unwrap fallback "
                f"(angle_limit={angle_limit_deg:.2f}, island_margin={island_margin:.4f})."
            )
        except Exception as unwrap_exc:
            bpy.ops.object.mode_set(mode="OBJECT")
            raise RuntimeError(
                "UV unwrap failed in headless mode. "
                f"smart_project={smart_error}; unwrap={unwrap_exc}"
            )

    bpy.ops.object.mode_set(mode="OBJECT")
    mesh.update()


def ensure_material_slots(obj: bpy.types.Object) -> None:
    if not obj.data.materials:
        mat = bpy.data.materials.new(name="SourceMaterial")
        mat.use_nodes = True
        obj.data.materials.append(mat)
        return

    for mat in obj.data.materials:
        if mat and not mat.use_nodes:
            mat.use_nodes = True


def attach_bake_image_nodes(obj: bpy.types.Object, image: bpy.types.Image):
    touched = []
    for mat in obj.data.materials:
        if not mat or not mat.use_nodes or not mat.node_tree:
            continue
        nodes = mat.node_tree.nodes
        node = nodes.new(type="ShaderNodeTexImage")
        node.image = image
        node.select = True
        nodes.active = node
        touched.append((nodes, node))
    return touched


def detach_bake_image_nodes(touched):
    for nodes, node in touched:
        try:
            nodes.remove(node)
        except Exception:
            pass


def find_base_color_image_in_material(mat: bpy.types.Material) -> Optional[bpy.types.Image]:
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None

    nodes = mat.node_tree.nodes
    for node in nodes:
        if node.type != "BSDF_PRINCIPLED":
            continue
        base_input = node.inputs.get("Base Color")
        if not base_input or not base_input.is_linked:
            continue
        for link in base_input.links:
            source = link.from_node
            if source and source.type == "TEX_IMAGE" and getattr(source, "image", None):
                return source.image

    for node in nodes:
        if node.type == "TEX_IMAGE" and getattr(node, "image", None):
            return node.image
    return None


def find_first_base_color_image(obj: bpy.types.Object) -> Optional[bpy.types.Image]:
    if not obj.data:
        return None
    for mat in obj.data.materials:
        image = find_base_color_image_in_material(mat)
        if image:
            return image
    return None


def bake_basecolor_atlas(obj: bpy.types.Object, tex_size: int, out_dir: str, map_name: str = "albedo") -> str:
    image = bpy.data.images.new(name=f"Bake_{map_name}", width=tex_size, height=tex_size, alpha=False)
    image.generated_color = (0.5, 0.5, 0.5, 1.0)

    touched = attach_bake_image_nodes(obj, image)

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 32
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.render.bake.margin = 4
    scene.render.bake.use_clear = True
    scene.render.bake.use_pass_direct = False
    scene.render.bake.use_pass_indirect = False
    scene.render.bake.use_pass_color = True

    select_active_object(obj)
    bpy.ops.object.bake(type="DIFFUSE")

    out_path = os.path.join(out_dir, f"{map_name}.png")
    image.filepath_raw = out_path
    image.file_format = "PNG"
    image.save()

    detach_bake_image_nodes(touched)
    return out_path


def build_simple_matte_material(
    obj: bpy.types.Object,
    base_color_image: Optional[bpy.types.Image] = None,
    base_color_path: Optional[str] = None
) -> None:
    image = base_color_image
    if image is None and base_color_path and os.path.exists(base_color_path):
        image = bpy.data.images.load(base_color_path, check_existing=True)

    mat = bpy.data.materials.new(name="PhotogrammetryMatte")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    for node in list(nodes):
        nodes.remove(node)

    out_node = nodes.new(type="ShaderNodeOutputMaterial")
    bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")
    links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
    bsdf.inputs["Metallic"].default_value = 0.0
    bsdf.inputs["Roughness"].default_value = 1.0

    if image is not None:
        try:
            image.colorspace_settings.name = "sRGB"
        except Exception:
            pass
        albedo_node = nodes.new(type="ShaderNodeTexImage")
        albedo_node.image = image
        albedo_node.location = (-420, 120)
        links.new(albedo_node.outputs["Color"], bsdf.inputs["Base Color"])

    obj.data.materials.clear()
    obj.data.materials.append(mat)


def get_base_color_texture_names(obj: bpy.types.Object) -> List[str]:
    names: List[str] = []
    if not obj.data:
        return names
    for mat in obj.data.materials:
        image = find_base_color_image_in_material(mat)
        if image:
            names.append(str(image.name))
    return sorted(set(names))


def mesh_stage_stats(obj: bpy.types.Object) -> Dict[str, object]:
    mesh = obj.data
    if not mesh:
        return {
            "faces": 0,
            "has_uv": False,
            "uv_range": None,
            "material_count": 0,
            "basecolor_textures": []
        }

    face_count = len(mesh.polygons)
    has_uv = bool(mesh.uv_layers and mesh.uv_layers.active and len(mesh.uv_layers.active.data) > 0)
    uv_range = None
    if has_uv:
        uv_data = mesh.uv_layers.active.data
        min_u = float("inf")
        max_u = float("-inf")
        min_v = float("inf")
        max_v = float("-inf")
        for item in uv_data:
            u = float(item.uv.x)
            v = float(item.uv.y)
            if not math.isfinite(u) or not math.isfinite(v):
                continue
            min_u = min(min_u, u)
            max_u = max(max_u, u)
            min_v = min(min_v, v)
            max_v = max(max_v, v)
        if math.isfinite(min_u) and math.isfinite(max_u) and math.isfinite(min_v) and math.isfinite(max_v):
            uv_range = {
                "min_u": min_u,
                "max_u": max_u,
                "min_v": min_v,
                "max_v": max_v,
                "span_u": max_u - min_u,
                "span_v": max_v - min_v,
            }

    return {
        "faces": face_count,
        "has_uv": has_uv,
        "uv_range": uv_range,
        "material_count": len(mesh.materials),
        "basecolor_textures": get_base_color_texture_names(obj)
    }


def log_stage_summary(
    stage_name: str,
    obj: bpy.types.Object,
    output_path: str,
    uv_action: str = "none",
    texture_action: str = "none",
    atlas_generated: bool = False
) -> None:
    stats = mesh_stage_stats(obj)
    uv_range = stats["uv_range"]
    if uv_range:
        uv_text = (
            f"u[{uv_range['min_u']:.4f},{uv_range['max_u']:.4f}] "
            f"v[{uv_range['min_v']:.4f},{uv_range['max_v']:.4f}] "
            f"span=({uv_range['span_u']:.4f},{uv_range['span_v']:.4f})"
        )
    else:
        uv_text = "none"

    textures = stats["basecolor_textures"]
    tex_text = ", ".join(textures) if textures else "none"

    log(
        f"[{stage_name}] faces={stats['faces']} "
        f"has_uv={stats['has_uv']} uv={uv_text} "
        f"materials={stats['material_count']} "
        f"baseColorTextures={tex_text} "
        f"atlas_generated={atlas_generated} "
        f"uv_action='{uv_action}' texture_action='{texture_action}' "
        f"output={output_path}"
    )


def export_stage_snapshot(
    stage_name: str,
    obj: bpy.types.Object,
    output_path: str,
    export_tangents: bool,
    uv_action: str = "none",
    texture_action: str = "none",
    atlas_generated: bool = False
) -> None:
    ensure_parent_dirs([output_path])
    select_active_object(obj)
    export_glb(output_path, selected_only=True, export_tangents=export_tangents)
    log_stage_summary(
        stage_name,
        obj,
        output_path=output_path,
        uv_action=uv_action,
        texture_action=texture_action,
        atlas_generated=atlas_generated
    )


def export_glb(filepath: str, selected_only: bool = True, export_tangents: bool = False) -> None:
    bpy.ops.export_scene.gltf(
        filepath=filepath,
        export_format="GLB",
        use_selection=selected_only,
        export_apply=True,
        export_texcoords=True,
        export_normals=True,
        export_tangents=bool(export_tangents),
        export_materials="EXPORT",
        export_yup=True,
    )


def world_min_z(obj: bpy.types.Object) -> float:
    if not obj.data.vertices:
        return 0.0
    return min((obj.matrix_world @ v.co).z for v in obj.data.vertices)


def world_z_bounds(obj: bpy.types.Object) -> Tuple[float, float]:
    if not obj.data.vertices:
        return (0.0, 0.0)
    values = [(obj.matrix_world @ v.co).z for v in obj.data.vertices]
    return (min(values), max(values))


def remove_loose_geometry(obj: bpy.types.Object) -> int:
    if not obj.data:
        return 0

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    loose_verts = [v for v in bm.verts if len(v.link_faces) == 0]
    loose_edges = [e for e in bm.edges if len(e.link_faces) == 0]
    removed = len(loose_verts) + len(loose_edges)

    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return removed


def weld_and_cleanup(obj: bpy.types.Object, merge_dist: float = 0.0002) -> None:
    if not obj.data:
        return

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    if bm.verts:
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=max(0.0, merge_dist))
    if bm.edges:
        bmesh.ops.dissolve_degenerate(bm, dist=1e-7, edges=bm.edges)

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def apply_decimate_to_target_faces(obj: bpy.types.Object, target_faces: int) -> Tuple[int, int]:
    if target_faces <= 0 or not obj.data:
        face_count = len(obj.data.polygons) if obj.data else 0
        return (face_count, face_count)

    before = len(obj.data.polygons)
    if before <= target_faces:
        return (before, before)

    ratio = target_faces / float(max(before, 1))
    ratio = max(0.01, min(1.0, ratio))

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    modifier = obj.modifiers.new(name="CleanupDecimate", type="DECIMATE")
    modifier.decimate_type = "COLLAPSE"
    modifier.ratio = ratio
    modifier.use_collapse_triangulate = True
    bpy.ops.object.modifier_apply(modifier=modifier.name)

    after = len(obj.data.polygons)
    return (before, after)


def recalculate_outward_normals(obj: bpy.types.Object) -> None:
    if not obj.data or len(obj.data.polygons) == 0:
        return

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    if bm.faces:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.normal_update()
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def place_mesh_base_at_local_zero(obj: bpy.types.Object) -> float:
    min_z = world_min_z(obj)
    offset = -min_z
    if abs(offset) > 1e-9:
        obj.location.z += offset
    return offset


def apply_object_transforms(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def robust_axis_center(values: List[float], trim_fraction: float = 0.04) -> float:
    if not values:
        return 0.0
    if len(values) < 20:
        return 0.5 * (min(values) + max(values))
    lo = percentile(values, trim_fraction)
    hi = percentile(values, 1.0 - trim_fraction)
    return 0.5 * (lo + hi)


def recenter_mesh_data_at_origin(obj: bpy.types.Object) -> Tuple[float, float, float]:
    mesh = obj.data
    if not mesh or not mesh.vertices:
        return (0.0, 0.0, 0.0)

    xs = [v.co.x for v in mesh.vertices]
    ys = [v.co.y for v in mesh.vertices]
    zs = [v.co.z for v in mesh.vertices]

    # Use a trimmed midpoint to avoid tiny outlier fragments skewing the pivot.
    center_x = robust_axis_center(xs)
    center_y = robust_axis_center(ys)
    center_z = robust_axis_center(zs)
    offset = Vector((-center_x, -center_y, -center_z))

    for vertex in mesh.vertices:
        vertex.co += offset
    mesh.update()
    return (offset.x, offset.y, offset.z)


def main() -> None:
    args = parse_cli_args()
    texture_mode = str(args.texture_mode or "preserve_multi_material").strip().lower()
    if args.bake_albedo and texture_mode == "preserve_multi_material":
        texture_mode = "bake_basecolor"
        log("Legacy --bake_albedo detected; switching texture_mode to bake_basecolor.")
    elif args.bake_albedo and texture_mode == "reuse_existing":
        texture_mode = "bake_basecolor"
        log("Legacy --bake_albedo detected; switching texture_mode to bake_basecolor.")
    if args.bake_normal or args.bake_ao:
        log("Ignoring legacy normal/AO bake flags for photogrammetry-safe export.")

    input_mesh = os.path.abspath(args.input_mesh)
    if not os.path.exists(input_mesh):
        raise RuntimeError(f"Input mesh not found: {input_mesh}")

    footprints_path = os.path.abspath(args.footprints)
    if not os.path.exists(footprints_path):
        raise RuntimeError(f"Footprints GeoJSON not found: {footprints_path}")

    out_glb = os.path.abspath(args.out_glb)
    out_raw_glb = os.path.abspath(args.out_raw_glb) if args.out_raw_glb else None
    textures_dir = os.path.abspath(args.out_textures_dir)
    os.makedirs(textures_dir, exist_ok=True)
    ensure_parent_dirs([out_glb, out_raw_glb])
    stage_dir = os.path.dirname(out_glb)
    stage_00_source = os.path.join(stage_dir, "stage_00_source.glb")
    stage_10_cleanup = os.path.join(stage_dir, "stage_10_cleanup.glb")
    stage_20_decimated = os.path.join(stage_dir, "stage_20_decimated.glb")
    stage_30_uv = os.path.join(stage_dir, "stage_30_uv.glb")
    stage_40_baked = os.path.join(stage_dir, "stage_40_baked.glb")
    stage_50_final = os.path.join(stage_dir, "stage_50_final.glb")

    origin_x, origin_y = float(args.origin_utm[0]), float(args.origin_utm[1])

    log("Clearing scene...")
    clear_scene()

    log(f"Importing mesh: {input_mesh}")
    imported = import_mesh(input_mesh)
    mesh_obj = join_meshes(imported)
    mesh_obj.name = "BuildingMesh"

    apply_user_transform(mesh_obj, args)
    export_stage_snapshot(
        "stage_00_source",
        mesh_obj,
        stage_00_source,
        export_tangents=args.export_tangents,
        uv_action="reused original UVs",
        texture_action="reused source materials/textures",
        atlas_generated=False
    )

    with open(footprints_path, "r", encoding="utf-8") as handle:
        geojson = json.load(handle)

    feature, resolved_index = select_footprint_feature(geojson, args.footprint_id, args.feature_index)
    polygons = extract_polygons_from_feature(feature)
    polygon = choose_largest_polygon(polygons)
    outer_ring_utm = polygon[0]

    if len(outer_ring_utm) < 3:
        raise RuntimeError("Selected footprint polygon is invalid after normalization.")

    ring_blender_xy = utm_ring_to_blender_xy(outer_ring_utm, origin_x, origin_y)
    auto_orient_and_align_mesh_to_footprint(mesh_obj, ring_blender_xy)

    loose_removed = remove_loose_geometry(mesh_obj)
    weld_and_cleanup(mesh_obj)
    log(f"Cleanup complete: loose removed={loose_removed}.")

    decimate_before, decimate_after = apply_decimate_to_target_faces(mesh_obj, int(max(0, args.target_faces)))
    recalculate_outward_normals(mesh_obj)
    log(f"Decimation complete: faces before={decimate_before}, after={decimate_after}; normals recalculated.")
    export_stage_snapshot(
        "stage_20_decimated",
        mesh_obj,
        stage_20_decimated,
        export_tangents=args.export_tangents,
        uv_action="reused original UVs",
        texture_action="reused source materials/textures",
        atlas_generated=False
    )

    if args.clip_mode == "ground_outside":
        removed_ground, z05, ground_limit = remove_ground_outside_footprint(
            mesh_obj,
            ring_blender_xy,
            max(0.0, args.clip_margin),
        )
        remaining_after_ground = len(mesh_obj.data.polygons)
        log(
            f"Removed {removed_ground} outside ground-like faces "
            f"(z05={z05:.3f}, ground_limit={ground_limit:.3f}, remaining faces={remaining_after_ground})."
        )
        if remaining_after_ground == 0:
            raise RuntimeError("Ground cleanup removed all faces; mesh alignment/scale is invalid.")
    else:
        log("Clip mode is 'none'; preserving mesh geometry (no clipping).")

    centroid_x = sum(p[0] for p in outer_ring_utm) / len(outer_ring_utm)
    centroid_y = sum(p[1] for p in outer_ring_utm) / len(outer_ring_utm)

    base_offset = place_mesh_base_at_local_zero(mesh_obj)
    log(f"Placed mesh base at local Z=0 (offset {base_offset:.3f}m).")
    apply_object_transforms(mesh_obj)
    log("Applied object transforms to mesh data for stable GLB export.")

    dem_for_ops: Optional[float] = None
    if args.dem:
        dem_for_ops = sample_dem_value(os.path.abspath(args.dem), centroid_x, centroid_y)
        if dem_for_ops is None:
            log("DEM sample unavailable at footprint centroid; DEM-based cut/snap disabled.")
        else:
            z_min_now, z_max_now = world_z_bounds(mesh_obj)
            if dem_for_ops < z_min_now - 10.0 or dem_for_ops > z_max_now + 10.0:
                log(
                    "DEM elevation appears incompatible with mesh Z range "
                    f"(dem={dem_for_ops:.3f}, mesh_z=[{z_min_now:.3f},{z_max_now:.3f}]); "
                    "skipping DEM-based cut/snap."
                )
                dem_for_ops = None

    if args.ground_cut_mode == "below_dem":
        if not args.dem:
            raise RuntimeError("ground_cut_mode=below_dem requires --dem.")
        if dem_for_ops is None:
            log("DEM sample unavailable for ground cutting; skipping below_dem face deletion.")
        else:
            threshold = dem_for_ops + args.ground_eps
            removed = delete_faces_all_vertices_below_z(mesh_obj, threshold)
            remaining = len(mesh_obj.data.polygons)
            log(f"Removed {removed} faces below DEM threshold {threshold:.3f}m (remaining faces: {remaining})")
    elif args.ground_cut_mode == "below_z":
        removed = delete_faces_all_vertices_below_z(mesh_obj, args.ground_z)
        log(f"Removed {removed} faces below Z {args.ground_z:.3f}")

    if args.dem and dem_for_ops is not None:
        min_z = world_min_z(mesh_obj)
        target = dem_for_ops + args.ground_eps
        offset = target - min_z
        if abs(offset) > 50.0:
            log(
                f"Skipping DEM snap due to large offset ({offset:.3f}m). "
                "Likely local (non-georeferenced) mesh elevations."
            )
        else:
            min_z = world_min_z(mesh_obj)
            mesh_obj.location.z += (target - min_z)
            log(f"Applied DEM snap offset: {target - min_z:.3f}m")

    # Final normalization pass so GLB pivot/origin is centered on the building.
    apply_object_transforms(mesh_obj)
    recenter_dx, recenter_dy, recenter_dz = recenter_mesh_data_at_origin(mesh_obj)
    z_min_final, z_max_final = world_z_bounds(mesh_obj)
    log(
        "Final recenter to origin "
        f"(dx={recenter_dx:.3f}, dy={recenter_dy:.3f}, dz={recenter_dz:.3f}, "
        f"z_bounds=[{z_min_final:.3f},{z_max_final:.3f}])."
    )
    recalculate_outward_normals(mesh_obj)
    export_stage_snapshot(
        "stage_10_cleanup",
        mesh_obj,
        stage_10_cleanup,
        export_tangents=args.export_tangents,
        uv_action="reused original UVs",
        texture_action="reused source materials/textures",
        atlas_generated=False
    )

    ensure_material_slots(mesh_obj)
    uv_action = "reused original UVs"
    texture_action = "reused source materials/textures"
    atlas_generated = False
    atlas_path: Optional[str] = None
    stage_40_bake_skipped = False

    if texture_mode == "bake_basecolor":
        log("Texture mode: bake_basecolor (Smart UV + single basecolor atlas).")
        smart_uv_project(mesh_obj, args.unwrap_angle_limit, args.unwrap_island_margin)
        uv_action = "generated Smart UV Project unwrap"
        export_stage_snapshot(
            "stage_30_uv",
            mesh_obj,
            stage_30_uv,
            export_tangents=args.export_tangents,
            uv_action=uv_action,
            texture_action="reused source textures pre-bake",
            atlas_generated=False
        )
        atlas_path = bake_basecolor_atlas(mesh_obj, args.tex_size, textures_dir, map_name="stage_40_basecolor_atlas")
        build_simple_matte_material(mesh_obj, base_color_path=atlas_path)
        texture_action = f"baked atlas from source to {os.path.basename(atlas_path)} and reassigned base color"
        atlas_generated = True
    elif texture_mode == "reuse_existing":
        if not has_uv_map(mesh_obj):
            log("Texture mode: reuse_existing (no UVs found, running Smart UV Project).")
            smart_uv_project(mesh_obj, args.unwrap_angle_limit, args.unwrap_island_margin)
            uv_action = "generated Smart UV Project unwrap"
        else:
            log("Texture mode: reuse_existing (keeping imported UVs).")
            uv_action = "reused original UVs"
        existing_base = find_first_base_color_image(mesh_obj)
        if existing_base:
            log(f"Reusing imported base color texture: {existing_base.name}")
            texture_action = f"reassigned base color texture to {existing_base.name}"
        else:
            log("No imported base color texture found; using matte fallback material color.")
            texture_action = "no base color image found; using matte fallback color"
        export_stage_snapshot(
            "stage_30_uv",
            mesh_obj,
            stage_30_uv,
            export_tangents=args.export_tangents,
            uv_action=uv_action,
            texture_action="no texture reassignment yet",
            atlas_generated=False
        )
        build_simple_matte_material(mesh_obj, base_color_image=existing_base)
    elif texture_mode == "preserve_multi_material":
        if not has_uv_map(mesh_obj):
            log("Texture mode: preserve_multi_material (no UVs found, running Smart UV Project).")
            smart_uv_project(mesh_obj, args.unwrap_angle_limit, args.unwrap_island_margin)
            uv_action = "generated Smart UV Project unwrap (missing source UVs)"
        else:
            log("Texture mode: preserve_multi_material (preserving original UVs and material slots).")
            uv_action = "reused original UVs"

        export_stage_snapshot(
            "stage_30_uv",
            mesh_obj,
            stage_30_uv,
            export_tangents=args.export_tangents,
            uv_action=uv_action,
            texture_action="preserved original materials/textures",
            atlas_generated=False
        )
        texture_action = "preserved material slots and baseColor textures; skipped single-atlas collapse"
        stage_40_bake_skipped = True
        log("Stage 40 bake/material-collapse skipped (preserve_multi_material mode).")
    else:
        raise RuntimeError(f"Unsupported texture_mode '{texture_mode}'.")

    select_active_object(mesh_obj)
    export_stage_snapshot(
        "stage_40_baked",
        mesh_obj,
        stage_40_baked,
        export_tangents=args.export_tangents,
        uv_action=uv_action,
        texture_action=texture_action,
        atlas_generated=atlas_generated
    )
    if stage_40_bake_skipped:
        log("[stage_40_baked] stage_40 bake skipped=true")
    if atlas_path:
        log(f"[stage_40_baked] atlas_output={atlas_path}")

    before_final_stats = mesh_stage_stats(mesh_obj)

    if out_raw_glb:
        log(f"Exporting raw GLB: {out_raw_glb}")
        export_glb(out_raw_glb, selected_only=True, export_tangents=args.export_tangents)

    log(f"Exporting final GLB: {out_glb}")
    export_glb(out_glb, selected_only=True, export_tangents=args.export_tangents)
    after_final_stats = mesh_stage_stats(mesh_obj)
    log(
        "Final export material slots/baseColor textures "
        f"before={before_final_stats['material_count']} "
        f"after={after_final_stats['material_count']} "
        f"base_before={before_final_stats['basecolor_textures']} "
        f"base_after={after_final_stats['basecolor_textures']} "
        f"stage_40_bake_skipped={stage_40_bake_skipped}"
    )
    export_stage_snapshot(
        "stage_50_final",
        mesh_obj,
        stage_50_final,
        export_tangents=args.export_tangents,
        uv_action=uv_action,
        texture_action=texture_action,
        atlas_generated=atlas_generated
    )

    log("Done")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        raise
