"""Build viewer hydrology features from NATO atlas-local water sources.

The global/continental atlas fetchers already localize HydroLAKES,
HydroRIVERS, and JRC Global Surface Water into scene-local coordinates.  This
module combines those local products into the viewer's standard
``data/hydrology/features.geojson`` overlay contract.
"""

import argparse
import copy
import glob
import json
import os
import time

import numpy as np
from osgeo import gdal, ogr

gdal.UseExceptions()
ogr.UseExceptions()

JRC_OCCURRENCE_THRESHOLD = 50
HYDROLOGY_ATTRIBUTION = [
    "HydroLAKES v1.0 and HydroRIVERS v1.0: WWF HydroSHEDS; HydroLAKES CC-BY 4.0.",
    "JRC Global Surface Water occurrence v1.4: European Commission Joint Research Centre / Copernicus open data.",
]


def build_hydrology_features(data_dir, grid=None, alpha2="nato"):
    """Write ``data/hydrology/features.geojson`` for a NATO twin.

    Missing source files are treated as absent water sources.  The function
    always writes a FeatureCollection and returns a small count/status summary.
    """
    data_dir = os.path.abspath(data_dir)
    grid = grid or _read_json(os.path.join(data_dir, "terrain", "grid.json")) or {}
    alpha = (alpha2 or "nato").lower()
    local_dir = os.path.join(data_dir, "atlas", "local")
    output_path = os.path.join(data_dir, "hydrology", "features.geojson")

    footprint = _footprint_geom(grid)
    features = []
    warnings = []
    counts = {"lakes": 0, "rivers": 0, "jrc_polys": 0}

    lake_union = None
    lakes_path = _source_path(local_dir, alpha, "hydrolakes.geojson")
    try:
        lake_features, lake_union = _load_lake_features(lakes_path, footprint)
        features.extend(lake_features)
        counts["lakes"] = len(lake_features)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"HydroLAKES skipped: {exc}")
        lake_union = None

    rivers_path = _source_path(local_dir, alpha, "hydrorivers.geojson")
    try:
        river_features = _load_river_features(rivers_path, footprint, lake_union)
        features.extend(river_features)
        counts["rivers"] = len(river_features)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"HydroRIVERS skipped: {exc}")

    jrc_path = _source_path(local_dir, alpha, "jrc_gsw_occurrence.grid.json")
    try:
        jrc_features = _jrc_permanent_water_features(jrc_path, footprint, lake_union)
        features.extend(jrc_features)
        counts["jrc_polys"] = len(jrc_features)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"JRC GSW occurrence skipped: {exc}")

    feature_collection = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "generated_by": "packs/nato/adapters/hydrology_surface.py",
            "generated_at": _utcnow(),
            "coordinate_system": "scene-local meters",
            "sources": {
                "hydrolakes": _relpath(lakes_path, data_dir) if lakes_path else None,
                "hydrorivers": _relpath(rivers_path, data_dir) if rivers_path else None,
                "jrc_gsw_occurrence": _relpath(jrc_path, data_dir) if jrc_path else None,
            },
            "jrc_occurrence_threshold_pct": JRC_OCCURRENCE_THRESHOLD,
            "counts": counts,
            "attribution": HYDROLOGY_ATTRIBUTION,
            "warnings": warnings,
        },
    }
    _write_json(output_path, feature_collection)
    _update_scene_hydrology(data_dir, counts, warnings)
    return {
        "path": output_path,
        "features_written": len(features),
        **counts,
        "warnings": warnings,
    }


def _load_lake_features(path, footprint):
    if not path:
        return [], None
    payload = _read_json(path)
    if not payload:
        return [], None

    features = []
    union = None
    for source in payload.get("features") or []:
        geom = _geometry_from_feature(source)
        if geom is None:
            continue
        clipped = _clip_geom(geom, footprint)
        if clipped is None or clipped.IsEmpty():
            continue
        clipped = _valid_geom(clipped)
        if clipped is None or clipped.IsEmpty():
            continue
        props = _water_props(source.get("properties"), "lake", "HydroLAKES")
        name = _first_present(props, ("name", "Lake_name", "lake_name", "GNIS_Name"))
        if name not in (None, ""):
            props["name"] = name
        for part in _iter_exportable(clipped, polygon=True):
            features.append(_feature(part, props))
            union = _union_geom(union, part)
    return features, union


def _load_river_features(path, footprint, lake_union):
    if not path:
        return []
    payload = _read_json(path)
    if not payload:
        return []

    features = []
    for source in payload.get("features") or []:
        geom = _geometry_from_feature(source)
        if geom is None:
            continue
        clipped = _clip_geom(geom, footprint)
        if clipped is None or clipped.IsEmpty():
            continue
        if lake_union is not None and not lake_union.IsEmpty():
            clipped = clipped.Difference(lake_union)
            if clipped is None or clipped.IsEmpty():
                continue
        props = _water_props(source.get("properties"), "river", "HydroRIVERS")
        for part in _iter_exportable(clipped, line=True):
            if part.Length() <= 0:
                continue
            features.append(_feature(part, props))
    return features


def _jrc_permanent_water_features(path, footprint, lake_union):
    if not path:
        return []
    grid = _read_json(path)
    if not grid:
        return []
    arr = _grid_values_array(grid)
    if arr is None:
        return []

    nodata = grid.get("nodata")
    mask = _jrc_mask(arr, nodata)
    if lake_union is not None and not lake_union.IsEmpty() and mask.any():
        _clear_lake_cells(mask, grid, lake_union)
    if not mask.any():
        return []

    geom = _polygonize_mask(mask, grid)
    if geom is None or geom.IsEmpty():
        return []
    geom = _clip_geom(geom, footprint)
    if geom is None or geom.IsEmpty():
        return []
    if lake_union is not None and not lake_union.IsEmpty():
        geom = geom.Difference(lake_union)
        if geom is None or geom.IsEmpty():
            return []

    cell = _grid_cell_size(grid)
    tolerance = max(0.25, min(cell) * 0.12)
    simplified = geom.SimplifyPreserveTopology(tolerance)
    if simplified is not None and not simplified.IsEmpty():
        geom = simplified

    props = {
        "water": "permanent_water",
        "source": "JRC Global Surface Water occurrence",
        "occurrence_threshold_pct": JRC_OCCURRENCE_THRESHOLD,
    }
    return [_feature(part, props) for part in _iter_exportable(geom, polygon=True)]


def _source_path(local_dir, alpha, suffix):
    preferred = os.path.join(local_dir, f"{alpha}_{suffix}")
    if os.path.exists(preferred):
        return preferred
    matches = sorted(glob.glob(os.path.join(local_dir, f"*_{suffix}")))
    return matches[0] if matches else None


def _read_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def _relpath(path, root):
    if not path:
        return None
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _footprint_geom(grid):
    bounds = (
        float(grid.get("outerMinX", grid.get("minX", -0.5))),
        float(grid.get("outerMinY", grid.get("minY", -0.5))),
        float(grid.get("outerMaxX", grid.get("maxX", 0.5))),
        float(grid.get("outerMaxY", grid.get("maxY", 0.5))),
    )
    return _rect_geom(bounds)


def _rect_geom(bounds):
    minx, miny, maxx, maxy = bounds
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)):
        ring.AddPoint_2D(float(x), float(y))
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def _geometry_from_feature(feature):
    raw = (feature or {}).get("geometry")
    if not raw:
        return None
    geom = ogr.CreateGeometryFromJson(json.dumps(raw))
    return _valid_geom(geom)


def _valid_geom(geom):
    if geom is None:
        return None
    try:
        if not geom.IsValid() and hasattr(geom, "MakeValid"):
            geom = geom.MakeValid()
    except Exception:  # noqa: BLE001
        pass
    return geom


def _clip_geom(geom, footprint):
    if geom is None or footprint is None:
        return None
    if not geom.Intersects(footprint):
        return None
    clipped = geom.Intersection(footprint)
    return _valid_geom(clipped)


def _union_geom(left, right):
    if right is None or right.IsEmpty():
        return left
    if left is None or left.IsEmpty():
        return right.Clone()
    merged = left.Union(right)
    return _valid_geom(merged)


def _iter_exportable(geom, polygon=False, line=False):
    if geom is None or geom.IsEmpty():
        return
    flat = ogr.GT_Flatten(geom.GetGeometryType())
    if polygon and flat == ogr.wkbPolygon:
        yield geom
    elif line and flat == ogr.wkbLineString:
        if geom.GetPointCount() >= 2:
            yield geom
    elif polygon and flat == ogr.wkbMultiPolygon:
        for idx in range(geom.GetGeometryCount()):
            part = geom.GetGeometryRef(idx)
            if part is not None and not part.IsEmpty():
                yield part.Clone()
    elif line and flat == ogr.wkbMultiLineString:
        for idx in range(geom.GetGeometryCount()):
            part = geom.GetGeometryRef(idx)
            if part is not None and not part.IsEmpty() and part.GetPointCount() >= 2:
                yield part.Clone()
    elif flat == ogr.wkbGeometryCollection:
        for idx in range(geom.GetGeometryCount()):
            part = geom.GetGeometryRef(idx)
            if part is not None:
                yield from _iter_exportable(part, polygon=polygon, line=line)


def _feature(geom, props):
    raw = json.loads(geom.ExportToJson())
    raw = _round_geometry(raw)
    return {
        "type": "Feature",
        "properties": copy.deepcopy(props),
        "geometry": raw,
    }


def _round_geometry(geometry, ndigits=2):
    out = copy.deepcopy(geometry)
    if "coordinates" in out:
        out["coordinates"] = _round_coords(out["coordinates"], ndigits)
    if out.get("type") == "GeometryCollection":
        out["geometries"] = [_round_geometry(g, ndigits) for g in out.get("geometries", [])]
    return out


def _round_coords(value, ndigits):
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
            rounded = [round(float(value[0]), ndigits), round(float(value[1]), ndigits)]
            if len(value) > 2:
                rounded.extend(value[2:])
            return rounded
        return [_round_coords(v, ndigits) for v in value]
    return value


def _water_props(properties, water, source):
    props = dict(properties or {})
    props["water"] = water
    props["source"] = source
    return props


def _first_present(props, names):
    for name in names:
        value = props.get(name)
        if value not in (None, ""):
            return value
    return None


def _grid_values_array(grid):
    values = grid.get("values")
    width = int(grid.get("width") or 0)
    height = int(grid.get("height") or 0)
    if not values or width <= 0 or height <= 0:
        return None
    arr = np.asarray(values)
    if arr.ndim == 1:
        if arr.size != width * height:
            return None
        arr = arr.reshape((height, width))
    if arr.ndim != 2:
        return None
    if arr.shape != (height, width):
        return None
    return arr


def _jrc_mask(arr, nodata):
    numeric = np.asarray(arr, dtype=float)
    mask = numeric >= JRC_OCCURRENCE_THRESHOLD
    if nodata is not None:
        mask &= numeric != float(nodata)
    mask &= np.isfinite(numeric)
    return mask.astype(np.uint8)


def _grid_bounds(grid):
    bounds = grid.get("bounds_local")
    if bounds and len(bounds) == 4:
        return tuple(float(v) for v in bounds)
    return (
        float(grid.get("minX", grid.get("outerMinX", 0.0))),
        float(grid.get("minY", grid.get("outerMinY", 0.0))),
        float(grid.get("maxX", grid.get("outerMaxX", grid.get("width", 1)))),
        float(grid.get("maxY", grid.get("outerMaxY", grid.get("height", 1)))),
    )


def _grid_cell_size(grid):
    minx, miny, maxx, maxy = _grid_bounds(grid)
    width = float(grid.get("width") or 1)
    height = float(grid.get("height") or 1)
    return ((maxx - minx) / width, (maxy - miny) / height)


def _cell_center(grid, row, col):
    minx, _miny, _maxx, maxy = _grid_bounds(grid)
    cellx, celly = _grid_cell_size(grid)
    return minx + (col + 0.5) * cellx, maxy - (row + 0.5) * celly


def _clear_lake_cells(mask, grid, lake_union):
    rows, cols = mask.shape
    point = ogr.Geometry(ogr.wkbPoint)
    for row in range(rows):
        for col in range(cols):
            if not mask[row, col]:
                continue
            x, y = _cell_center(grid, row, col)
            point.Empty()
            point.AddPoint_2D(float(x), float(y))
            if lake_union.Contains(point):
                mask[row, col] = 0


def _polygonize_mask(mask, grid):
    height, width = mask.shape
    minx, _miny, _maxx, maxy = _grid_bounds(grid)
    cellx, celly = _grid_cell_size(grid)

    raster = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
    raster.SetGeoTransform((minx, cellx, 0.0, maxy, 0.0, -celly))
    band = raster.GetRasterBand(1)
    band.WriteArray(mask)
    band.SetNoDataValue(0)

    vector = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = vector.CreateLayer("permanent_water", geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))
    gdal.Polygonize(band, None, layer, 0, [], callback=None)

    union = None
    for feat in layer:
        if int(feat.GetField("value") or 0) != 1:
            continue
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            continue
        union = _union_geom(union, geom)
    return union


def _update_scene_hydrology(data_dir, counts, warnings):
    scene_path = os.path.join(data_dir, "scene.json")
    scene = _read_json(scene_path)
    if not scene:
        return
    scene["hydrology"] = {
        "status": "ready",
        "features_url": "/data/hydrology/features.geojson",
        "source": "packs/nato/adapters/hydrology_surface.py",
        "feature_count": int(sum(counts.values())),
        "counts": counts,
    }
    if warnings:
        scene["hydrology"]["warnings"] = warnings
    _write_json(scene_path, scene)


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build NATO hydrology features for an existing twin")
    ap.add_argument("--data-dir", required=True, help="Twin data directory")
    ap.add_argument("--alpha2", default="nato", help="ISO alpha-2 prefix used by atlas/local files")
    args = ap.parse_args(argv)
    result = build_hydrology_features(args.data_dir, alpha2=args.alpha2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
