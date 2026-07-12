#!/usr/bin/env python3
"""Query layer over the twin store, for the MCP server.

Everything the MCP tools answer is computed here, so the logic is testable
without the MCP runtime (scripts/twin_query_test.py runs it against the real
data/twin.gpkg). Store access goes through scripts/twin_store.py (the Store's
sqlite connection is reused for the read-only SQL the store API doesn't
cover, the same way scripts/canopy_density.py queries it).

The store is strictly read-only here. The one thing this module writes is
data/annotations.json — ephemeral map drawings (draw_polygon / draw_point /
clear_drawings) that the viewer polls and renders in orange so an LLM can
point at places instead of dictating coordinates. Annotations never touch
the store or the journal.

Conventions (the documented ones — no second convention):
  * Store/scene coordinates are scene-local meters: x = east, y = north,
    i.e. the twin's projected CRS minus origin_utm. The CRS comes from the
    store's meta table (falling back to data/georef.json) — never from a
    constant here. Geographic conversion is pyproj, projected CRS <-> its
    own geodetic CRS, so round-trips are exact and lon/lat matches the
    viewer's proj4js conversion to <1e-4 m.
  * Tool inputs accept points as {"lat","lon"} or {"x","y"}; outputs always
    echo both. Polygons accept [lon,lat] or scene-local [x,y] vertex pairs
    (auto-detected: a polygon whose every vertex falls inside the twin's
    own geographic window — extent plus a pad — is treated as lon/lat).
  * Every factual answer carries provenance: source / confidence / run_id /
    observed_at from the observations table, or acquisition / service from
    the layers table for atlas facts.

The point-identify logic (point-in-polygon, line distance, grid sampling
with legends, GAP species bitmask rows) is a direct port of the viewer's
click-to-identify in public/app.js.
"""

import json
import math
import os
import re
import struct
import sys
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import twin_store  # noqa: E402
import twin_astro  # noqa: E402
import twin_viewshed  # noqa: E402
import twin_solar  # noqa: E402

PROJECT = twin_store.PROJECT
DATA = twin_store.DATA_DIR
ATLAS_LOCAL = os.path.join(DATA, "atlas", "local")
VIEWER_LAYERS = os.path.join(ATLAS_LOCAL, "viewer-layers.json")
AOI_GEOJSON = os.path.join(DATA, "terrain", "aoi_local.geojson")
TERRAIN_GRID = os.path.join(DATA, "terrain", "grid.json")
APRON_GRID = os.path.join(DATA, "terrain", "grid.apron.json")
ANNOTATIONS_PATH = os.path.join(DATA, "annotations.json")
# Survey companion (docs/survey.md): the viewer catalog of uploaded field
# layers + the scene-local GeoJSON each references.
SURVEY_CATALOG = os.path.join(DATA, "surveys", "survey-layers.json")
# Hydrology simulation (the Simulation window): Tier-1 derived layers, the
# analysis summary, the last scenario run, and the SSURGO soils the seep
# score and the scenario CN grid are built from.
HYDRO_DIR = os.path.join(DATA, "hydrology")
HYDRO_SIM_CATALOG = os.path.join(HYDRO_DIR, "simulation-layers.json")
HYDRO_SUMMARY = os.path.join(HYDRO_DIR, "summary.json")
HYDRO_LAST_SCENARIO = os.path.join(HYDRO_DIR, "last-scenario.json")
FIRE_DIR = os.path.join(DATA, "fire")
FIRE_SIM_CATALOG = os.path.join(FIRE_DIR, "fire-layers.json")
FIRE_SUMMARY = os.path.join(FIRE_DIR, "summary.json")
FIRE_LAST_SCENARIO = os.path.join(FIRE_DIR, "last-fire-scenario.json")
SOILS_FEATURES = os.path.join(DATA, "soils", "features.geojson")
SOILS_TABULAR = os.path.join(DATA, "soils", "tabular.json")
VIEWSHED_DIR = os.path.join(DATA, "viewshed")
VIEWSHED_CATALOG = os.path.join(VIEWSHED_DIR, "viewshed-layers.json")
VIEWSHED_MANIFEST = os.path.join(DATA, "terrain", "distant", "manifest.json")
ET_DIR = os.path.join(DATA, "et")
ET0_SUMMARY = os.path.join(ET_DIR, "et0-summary.json")
ET_SUMMARY = os.path.join(ET_DIR, "summary.json")
ET_SOIL_WATER_DAILY = os.path.join(ET_DIR, "soil_water_daily.csv")
ET_LAYER_CATALOG = os.path.join(ET_DIR, "et-layers.json")
SOLAR_DIR = os.path.join(DATA, "solar")
SOLAR_LAYER_CATALOG = os.path.join(SOLAR_DIR, "solar-layers.json")
SOLAR_SUMMARY = os.path.join(SOLAR_DIR, "solar-summary.json")

# Pad (degrees) added around the twin's extent to form the geographic window
# used to auto-detect lon/lat polygon vertices. Scene-local meters never look
# like coordinates inside that window unless the polygon is a few meters
# across at one pathological spot.
GEO_WINDOW_PAD_DEG = 0.5

# Entity kind -> the gpkg spatial layer that carries its geometry.
POINT_KINDS = {"tree": "trees", "shrub": "shrubs", "live_device": "live_devices"}
VECTOR_KINDS = {
    "building": "building_footprints",
    "parcel": "parcels",
    "stream": "streams",
    "road": "roads",
}
# building_model has no spatial layer; its position is the latest "placement"
# observation (scene-local x/y written by the viewer editor).

# Same hidden-property set as the viewer's identify cards (app.js HIDE_PROPS).
HIDE_PROPS = {"__label", "OBJECTID", "Shape_Length", "Shape_Area",
              "Shape__Area", "Shape__Length", "SHAPE.AREA", "SHAPE.LEN",
              "SPATIALVER", "GlobalID"}

LINE_HIT_DISTANCE_M = 8.0  # app.js identify: line features hit within 8 m

# The richness raster the GAP per-species habitat bitmasks attach to; filtering
# it by species renders a habitat mask instead of the richness gradient.
GAP_SPECIES_LAYER = "gap_species_richness"
DRAPE_TYPES = ("raster", "polygon", "line", "point")


class TwinQueryError(Exception):
    """A structured, caller-visible error (never a stack trace)."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.payload = {"error": message}
        if details:
            self.payload.update(details)


# --------------------------------------------------------------- georef

class Georef:
    """Scene-local meters <-> lon/lat, bound to the store's projected origin
    and CRS (no module-level CRS constants — the CRS arrives from the store's
    meta / data/georef.json via the caller)."""

    def __init__(self, origin_utm, projected_crs):
        import twin_georef
        from pyproj import Transformer
        self.ox = float(origin_utm[0])
        self.oy = float(origin_utm[1])
        self.crs = projected_crs
        geographic = twin_georef.geographic_crs(projected_crs)
        self._fwd = Transformer.from_crs(projected_crs, geographic, always_xy=True)
        self._inv = Transformer.from_crs(geographic, projected_crs, always_xy=True)
        # lon/lat auto-detection window; refined to extent+pad by TwinQuery
        self._window_provider = None
        self._window = None

    def to_lonlat(self, x, y):
        lon, lat = self._fwd.transform(self.ox + x, self.oy + y)
        return lon, lat

    def to_scene(self, lon, lat):
        e, n = self._inv.transform(lon, lat)
        return e - self.ox, n - self.oy

    def set_window_provider(self, provider):
        """provider() -> (minx, miny, maxx, maxy) scene-local extent used to
        derive the lon/lat detection window."""
        self._window_provider = provider
        self._window = None

    def geo_window(self):
        """((lon_min, lon_max), (lat_min, lat_max)) — the twin's extent in
        degrees plus GEO_WINDOW_PAD_DEG, used to recognize lon/lat input."""
        if self._window is None:
            if self._window_provider is not None:
                minx, miny, maxx, maxy = self._window_provider()
            else:  # standalone Georef: a nominal 2 km box around the origin
                minx, miny, maxx, maxy = -1000, -1000, 1000, 1000
            lons, lats = [], []
            for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
                lon, lat = self.to_lonlat(x, y)
                lons.append(lon)
                lats.append(lat)
            p = GEO_WINDOW_PAD_DEG
            self._window = ((min(lons) - p, max(lons) + p),
                            (min(lats) - p, max(lats) + p))
        return self._window

    def echo(self, x, y):
        lon, lat = self.to_lonlat(x, y)
        # 9 decimals ~ 0.1 mm: returned lat/lon must round-trip within 1e-4 m
        return {"x": round(x, 3), "y": round(y, 3),
                "lat": round(lat, 9), "lon": round(lon, 9)}


def resolve_point(point, georef):
    """Accept {"lat","lon"} or {"x","y"}; return (x, y) scene-local meters."""
    if not isinstance(point, dict):
        raise TwinQueryError(
            "point must be an object with lat/lon (degrees) or x/y (scene-local meters)")
    has_geo = "lat" in point and "lon" in point
    has_scene = "x" in point and "y" in point
    if has_geo == has_scene:
        raise TwinQueryError(
            "point must carry exactly one coordinate pair: {lat, lon} in degrees "
            "or {x, y} in scene-local meters",
            got=sorted(point.keys()))
    try:
        if has_geo:
            return georef.to_scene(float(point["lon"]), float(point["lat"]))
        return float(point["x"]), float(point["y"])
    except (TypeError, ValueError):
        raise TwinQueryError("point coordinates must be numbers", got=point)


# ----------------------------------------------------- geometry helpers

def point_in_rings(rings, x, y):
    """Even-odd test across all rings (port of app.js pointInRings)."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def polygon_rings(geometry):
    """All rings of a Polygon/MultiPolygon geojson geometry."""
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    if geometry["type"] == "MultiPolygon":
        return [ring for poly in geometry["coordinates"] for ring in poly]
    return []


def line_paths(geometry):
    """Coordinate paths for line-distance tests (port of app.js eachLine:
    polygons contribute their outlines too)."""
    if not geometry:
        return []
    t = geometry["type"]
    if t == "LineString":
        return [geometry["coordinates"]]
    if t == "MultiLineString":
        return list(geometry["coordinates"])
    if t == "Polygon":
        return list(geometry["coordinates"])
    if t == "MultiPolygon":
        return [ring for poly in geometry["coordinates"] for ring in poly]
    return []


def dist_to_paths(paths, x, y):
    """Min distance (m) from a point to a set of polylines (app.js distToLine)."""
    best = math.inf
    for line in paths:
        for i in range(1, len(line)):
            x1, y1 = line[i - 1][0], line[i - 1][1]
            x2, y2 = line[i][0], line[i][1]
            dx, dy = x2 - x1, y2 - y1
            len2 = dx * dx + dy * dy or 1e-9
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / len2))
            best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def geometry_bbox(geometry):
    """Scene-local bbox for any GeoJSON geometry."""
    _centroid, bbox = geometry_centroid_and_bbox(geometry)
    return bbox


def point_geometry(x, y):
    return {"type": "Point", "coordinates": [float(x), float(y)]}


def expand_bbox(bbox, pad):
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)


def bboxes_intersect(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def line_geometry_intersects_region(geometry, region):
    """Test a line against a Region's exact point predicate, not its centroid."""
    bbox = geometry_bbox(geometry)
    if bbox is None or not bboxes_intersect(bbox, region.bounds):
        return False
    width = max(1e-6, region.bounds[2] - region.bounds[0])
    height = max(1e-6, region.bounds[3] - region.bounds[1])
    sample_step = max(0.25, min(10.0, min(width, height) / 64.0))
    for path in line_paths(geometry):
        if any(region.contains(float(point[0]), float(point[1])) for point in path):
            return True
        for start, end in zip(path, path[1:]):
            ax, ay = float(start[0]), float(start[1])
            bx, by = float(end[0]), float(end[1])
            distance = math.hypot(bx - ax, by - ay)
            samples = min(20_000, max(1, int(math.ceil(distance / sample_step))))
            if any(region.contains(
                    ax + (bx - ax) * index / samples,
                    ay + (by - ay) * index / samples)
                    for index in range(1, samples)):
                return True
    return False


def geometry_distance_m(a, b):
    """True planar distance in scene-local meters between two GeoJSON geometries.

    This intentionally uses the geometry itself, not the display centroid. It is
    what proximity queries need for long streams and large parcels.
    """
    from osgeo import ogr
    ga = ogr.CreateGeometryFromJson(json.dumps(a))
    gb = ogr.CreateGeometryFromJson(json.dumps(b))
    if ga is None or gb is None:
        return math.inf
    return float(ga.Distance(gb))


def shoelace_area(ring):
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i - 1][0], ring[i - 1][1]
        x2, y2 = ring[i][0], ring[i][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def sample_grid(grid, bounds, x, y):
    """Nearest-cell raster sample (port of app.js sampleGrid).
    Returns (row, col, value) or None when outside the bounds."""
    minx, miny, maxx, maxy = bounds
    if x < minx or x > maxx or y < miny or y > maxy:
        return None
    col = min(grid["width"] - 1, int((x - minx) / (maxx - minx) * grid["width"]))
    row = min(grid["height"] - 1, int((maxy - y) / (maxy - miny) * grid["height"]))
    return row, col, grid["values"][row][col]


def sample_terrain_elevation(grid, x, y):
    """Bilinear DEM sample, absolute meters (port of viewer/terrain.js
    sampleTerrainHeightAtLocal, without the minElevation offset).
    Returns None outside the grid or over nodata."""
    if not (grid["minX"] <= x <= grid["maxX"] and grid["minY"] <= y <= grid["maxY"]):
        return None
    w = max(1e-9, grid["maxX"] - grid["minX"])
    h = max(1e-9, grid["maxY"] - grid["minY"])
    xr = min(max((x - grid["minX"]) / w, 0.0), 0.999999)
    yr = min(max((y - grid["minY"]) / h, 0.0), 0.999999)
    xi = xr * (grid["width"] - 1)
    yi = (1 - yr) * (grid["height"] - 1)
    x0, y0 = int(xi), int(yi)
    x1 = min(grid["width"] - 1, x0 + 1)
    y1 = min(grid["height"] - 1, y0 + 1)
    tx, ty = xi - x0, yi - y0
    heights = grid["heights"]
    cells = [
        (heights[y0 * grid["width"] + x0], (1 - tx) * (1 - ty)),
        (heights[y0 * grid["width"] + x1], tx * (1 - ty)),
        (heights[y1 * grid["width"] + x0], (1 - tx) * ty),
        (heights[y1 * grid["width"] + x1], tx * ty),
    ]
    valid = [(v, wgt) for v, wgt in cells if isinstance(v, (int, float))]
    if not valid:
        return None
    total = sum(wgt for _, wgt in valid)
    if total <= 0:
        return valid[0][0]
    return sum(v * wgt for v, wgt in valid) / total


def write_rgba_png(path, rgba):
    """Small RGBA PNG writer for MCP-generated drape layers."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[row].astype(np.uint8).tobytes() for row in range(h))
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(png)
    os.replace(tmp, path)


_SITE_OBJECTIVE_ALIASES = {
    "overlook": ("overlook", "over-look", "lookout", "view", "viewpoint", "vantage", "ridge"),
    "trailcam": ("trailcam", "trail-cam", "trail cam", "camera", "camerapoint", "cam"),
    "well": ("well", "spring", "seep", "water source", "water"),
    "garden": ("garden", "clearing", "forest garden"),
    "structure": ("structure", "shelter", "platform", "platforms", "pad"),
}

# Words that legitimately follow "for ..." in a generic site request and must
# NOT be mistaken for an unresolved (e.g. species) target. Anything purely made
# of these is a generic objective phrase, not a constraint we failed to resolve.
_GENERIC_TARGET_WORDS = frozenset({
    "a", "an", "the", "some", "my", "our", "your", "this", "that", "best",
    "good", "great", "new", "site", "sites", "spot", "spots", "place", "places",
    "location", "locations", "point", "points", "area", "areas", "view", "views",
    "viewpoint", "lookout", "overlook", "vantage", "sunset", "sunrise", "camp",
    "camping", "campsite", "tent", "shelter", "structure", "cabin", "platform",
    "pad", "garden", "clearing", "well", "spring", "water", "trail", "camera",
    "wildlife", "animals", "game", "scenery", "privacy", "fishing", "hunting",
    "and", "or", "of", "in", "on", "near", "with", "to",
})


def _normalize_site_objective(text):
    """Map free-form prompts to a small stable set of recommendation profiles."""
    if not text:
        return "overlook"
    value = str(text).strip().lower()
    for objective, aliases in _SITE_OBJECTIVE_ALIASES.items():
        for alias in aliases:
            if alias in value:
                return objective
    return "overlook"


def parse_gpkg_geometry(blob):
    """GeoPackage geometry blob -> geojson dict (scene-local coords).
    Header: 'GP', version, flags (bit 1-3 = envelope size code), srs_id."""
    from osgeo import ogr
    flags = blob[3]
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
    geom = ogr.CreateGeometryFromWkb(bytes(blob[8 + envelope_bytes:]))
    if geom is None:
        return None
    return json.loads(geom.ExportToJson())


def geometry_centroid_and_bbox(geometry):
    """Centroid (vertex average is enough for locating entities) and bbox."""
    xs, ys = [], []

    def collect(coords):
        if coords and isinstance(coords[0], (int, float)):
            xs.append(coords[0])
            ys.append(coords[1])
        else:
            for c in coords:
                collect(c)

    collect(geometry["coordinates"])
    if not xs:
        return None, None
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    return (cx, cy), (min(xs), min(ys), max(xs), max(ys))


# ----------------------------------------------------------------- region

class Region:
    """One region abstraction, including viewshed-backed visibility shapes.
    `contains(x, y)` takes scene-local meters; `bounds` is the scene-local
    bounding box used to prefilter before the exact test."""

    def __init__(self, shape, bounds, contains, area_m2, description, metadata=None):
        self.shape = shape
        self.bounds = bounds
        self.contains = contains
        self.area_m2 = area_m2
        self.description = description
        self.metadata = metadata or {}

    def describe(self):
        out = {"shape": self.shape, "bounds_scene_m": [round(v, 3) for v in self.bounds],
               "area_m2": round(self.area_m2, 1) if self.area_m2 else None,
               "description": self.description}
        if self.metadata:
            out.update(self.metadata)
        return out


def _looks_geographic(pairs, georef):
    (lon0, lon1), (lat0, lat1) = georef.geo_window()
    return all(lon0 <= p[0] <= lon1 and lat0 <= p[1] <= lat1 for p in pairs)


def _rings_region(shape, rings, description):
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    area = sum(shoelace_area(r) for r in rings if shoelace_area(r) > 0)
    # even-odd handles holes; approximate area as outer-minus-holes per polygon
    # is not derivable from a flat ring list, so report the even-odd area by
    # summing signed contributions: outer rings dominate in this dataset.
    return Region(shape, bounds, lambda x, y: point_in_rings(rings, x, y),
                  area, description)


def _aoi_rings():
    with open(AOI_GEOJSON) as fh:
        gj = json.load(fh)
    features = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
    rings = []
    for f in features:
        rings.extend(polygon_rings(f.get("geometry") or f))
    if not rings:
        raise TwinQueryError("AOI boundary has no polygon rings", file=AOI_GEOJSON)
    return rings


def resolve_region(region, georef, viewshed_resolver=None):
    """The single region resolver every spatial tool uses (decision 6).
    Accepts exactly one of:
      {"aoi": true}
      {"bbox": [minx, miny, maxx, maxy]}            (scene-local meters)
      {"within_m": r, "point": {lat,lon} | {x,y}}   (radius in meters)
      {"polygon": [[lon,lat], ...] | [[x,y], ...]}  (ring auto-closed)
      {"visible_from": {...}} / {"hidden_from": {...}} (TwinQuery-backed)
    Returns a Region, or None when region is None (no spatial filter).
    """
    if region is None:
        return None
    if not isinstance(region, dict):
        raise TwinQueryError("region must be an object", got=region)
    shapes = [k for k in ("aoi", "bbox", "within_m", "polygon", "visible_from", "hidden_from") if k in region]
    if len(shapes) != 1:
        raise TwinQueryError(
            "region must carry exactly one of: aoi, bbox, within_m (+point), polygon, visible_from, hidden_from",
            got=sorted(region.keys()))
    extra = set(region) - {shapes[0], "point"}
    if extra or ("point" in region and shapes[0] != "within_m"):
        raise TwinQueryError("unexpected region keys", got=sorted(region.keys()))
    shape = shapes[0]

    if shape in {"visible_from", "hidden_from"}:
        if viewshed_resolver is None:
            raise TwinQueryError("visible_from/hidden_from regions need a TwinQuery viewshed resolver")
        return viewshed_resolver(shape, region[shape])

    if shape == "aoi":
        if region["aoi"] is not True:
            raise TwinQueryError('the aoi region is {"aoi": true}', got=region)
        return _rings_region("aoi", _aoi_rings(), "parcel AOI boundary")

    if shape == "bbox":
        b = region["bbox"]
        if (not isinstance(b, (list, tuple)) or len(b) != 4
                or not all(isinstance(v, (int, float)) for v in b)):
            raise TwinQueryError(
                "bbox must be [minx, miny, maxx, maxy] in scene-local meters", got=b)
        minx, miny, maxx, maxy = map(float, b)
        if minx >= maxx or miny >= maxy:
            raise TwinQueryError("bbox min must be < max on both axes", got=b)
        return Region(
            "bbox", (minx, miny, maxx, maxy),
            lambda x, y: minx <= x <= maxx and miny <= y <= maxy,
            (maxx - minx) * (maxy - miny),
            f"bbox ({minx:g},{miny:g})..({maxx:g},{maxy:g}) scene-local m")

    if shape == "within_m":
        if "point" not in region:
            raise TwinQueryError('within_m region needs a center: {"within_m": r, "point": {...}}')
        r = region["within_m"]
        if not isinstance(r, (int, float)) or r <= 0:
            raise TwinQueryError("within_m must be a positive number of meters", got=r)
        cx, cy = resolve_point(region["point"], georef)
        r = float(r)
        r2 = r * r
        return Region(
            "within_m", (cx - r, cy - r, cx + r, cy + r),
            lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r2,
            math.pi * r2,
            f"within {r:g} m of ({cx:.1f},{cy:.1f}) scene-local m")

    # polygon
    poly = region["polygon"]
    if (not isinstance(poly, (list, tuple)) or len(poly) < 3
            or not all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in poly)):
        raise TwinQueryError(
            "polygon must be a list of at least 3 [lon,lat] or [x,y] vertex pairs", got=poly)
    pts = [(float(p[0]), float(p[1])) for p in poly]
    geographic = _looks_geographic(pts, georef)
    if geographic:
        pts = [georef.to_scene(lon, lat) for lon, lat in pts]
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]  # auto-close
    coords = "lon/lat" if geographic else "scene-local m"
    return _rings_region("polygon", [pts], f"polygon with {len(pts) - 1} vertices ({coords})")


# --------------------------------------------------- map drawings (viewer)
# LLM-drawn polygons/points the viewer renders in orange. They live in one
# flat JSON file inside the twin's data dir (so the static server serves it
# and any process pointed at the same twin shares it) — never in the store.
# Scene-local meters only, matching every other viewer payload.

ANNOTATION_LABEL_MAX = 80


def _utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_view_doc():
    """The viewer-directive document: drawings the agent placed (`annotations`)
    layer-view overrides it set (`layer_views`), sky highlights (`sky_views`),
    and an optional viewer clock directive (`view_time`). One file the viewer
    polls; callers save the whole dict so a write to one key never drops the
    others."""
    try:
        with open(ANNOTATIONS_PATH) as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        doc = {}
    anns = doc.get("annotations")
    views = doc.get("layer_views")
    sky_views = doc.get("sky_views")
    view_time = doc.get("view_time")
    plan_view = doc.get("plan_view")
    return {
        "version": 1,
        "updated_at": doc.get("updated_at") or _utc_now(),
        "annotations": anns if isinstance(anns, list) else [],
        "layer_views": views if isinstance(views, list) else [],
        "sky_views": sky_views if isinstance(sky_views, list) else [],
        "view_time": view_time if isinstance(view_time, dict) else None,
        "plan_view": plan_view if isinstance(plan_view, dict) else None,
    }


def _save_view_doc(doc):
    clean = {
        "version": 1,
        "updated_at": _utc_now(),
        "annotations": doc.get("annotations") if isinstance(doc.get("annotations"), list) else [],
        "layer_views": doc.get("layer_views") if isinstance(doc.get("layer_views"), list) else [],
        "sky_views": doc.get("sky_views") if isinstance(doc.get("sky_views"), list) else [],
        "view_time": doc.get("view_time") if isinstance(doc.get("view_time"), dict) else None,
        "plan_view": doc.get("plan_view") if isinstance(doc.get("plan_view"), dict) else None,
    }
    tmp = ANNOTATIONS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(clean, fh, indent=1)
    os.replace(tmp, ANNOTATIONS_PATH)


def _load_annotations():
    return _load_view_doc()["annotations"]


def _next_annotation_id(annotations):
    high = 0
    for a in annotations:
        m = re.fullmatch(r"drawing:(\d+)", str(a.get("id", "")))
        if m:
            high = max(high, int(m.group(1)))
    return f"drawing:{high + 1:04d}"


def _clean_label(label):
    if label is None:
        return None
    label = str(label).strip()
    return label[:ANNOTATION_LABEL_MAX] or None


_DRAWN_NOTE = ("now visible on the user's 3D map in orange; refer to it by its "
               "label/color instead of reciting coordinates. The user can remove "
               "drawings with the viewer's \"Clear drawings\" button, or call "
               "clear_drawings.")

_LAYER_NOTE = ("The drape conforms to the terrain so the user sees exactly "
               "which ground it covers. Overrides take effect within a few "
               "seconds and persist until you change them; call "
               "reset_layer_views to hand layer control back to the user.")


# -------------------------------------------------------------- the store

class TwinQuery:
    """All query functions, over one Store connection, with per-process
    caches invalidated when data/twin.gpkg changes on disk."""

    def __init__(self, store_path=twin_store.STORE_PATH):
        if not os.path.exists(store_path):
            raise TwinQueryError(
                "twin store not found — run `npm run rebuild-store` first",
                path=store_path)
        self.store = twin_store.Store(store_path, journal=False)
        self.conn = self.store.conn
        origin = self.store.get_meta("origin_utm")
        if not origin:
            raise TwinQueryError("store has no origin_utm in meta; not a twin store?")
        import twin_georef
        crs_meta = self.store.get_meta("crs") or {}
        projected = crs_meta.get("analysis_crs") or twin_georef.crs()
        self.georef = Georef(origin, projected)
        self.georef.set_window_provider(self._extent)
        self._store_path = store_path
        self._cache_stamp = None
        self._caches = {}

    # -- caching -----------------------------------------------------------

    def _cache(self, key, build):
        stamp = os.path.getmtime(self._store_path)
        if stamp != self._cache_stamp:
            self._caches = {}
            self._cache_stamp = stamp
        if key not in self._caches:
            self._caches[key] = build()
        return self._caches[key]

    def _resolve_region(self, region):
        return resolve_region(region, self.georef, self._resolve_viewshed_region)

    def _viewshed_stack(self):
        def build():
            if os.path.exists(VIEWSHED_MANIFEST):
                return twin_viewshed.RingStack.load(VIEWSHED_MANIFEST)
            return twin_viewshed.RingStack.from_local_files(DATA)
        return self._cache("viewshed_stack", build)

    def _viewshed_key(self, x, y, agl, target_agl, refraction, max_km, surface, n_az=720):
        stack = self._viewshed_stack()
        return (
            "viewshed_sweep",
            round(float(x), 1), round(float(y), 1),
            round(float(agl), 2), round(float(target_agl), 2),
            str(refraction or "optical").lower(),
            None if max_km is None else round(float(max_km), 3),
            str(surface or "canopy").lower(),
            int(n_az),
            stack.manifest_hash,
        )

    def _viewshed_sweep_cached(self, point, agl_m=1.7, target_agl_m=0.0,
                               refraction="optical", max_km=None, surface="bare_earth",
                               n_az=720):
        x, y = resolve_point(point, self.georef)
        key = self._viewshed_key(x, y, agl_m, target_agl_m, refraction, max_km, surface, n_az=n_az)
        def build():
            stack = self._viewshed_stack()
            return twin_viewshed.sweep(
                stack, x, y, float(agl_m), n_az=n_az, max_km=max_km,
                surface=surface or "canopy", k=refraction or "optical",
                target_agl_m=float(target_agl_m or 0.0))
        result = self._cache(key, build)
        return self._viewshed_stack(), result, x, y, key

    def _resolve_viewshed_region(self, shape, spec):
        if not isinstance(spec, dict) or "point" not in spec:
            raise TwinQueryError(f"{shape} region needs a point and optional agl_m/max_km/refraction/surface",
                                 got=spec)
        agl = float(spec.get("agl_m", 1.7))
        target_agl = float(spec.get("target_agl_m", 0.0))
        max_km = spec.get("max_km")
        max_km = None if max_km is None else float(max_km)
        refraction = spec.get("refraction", "optical")
        surface = spec.get("surface", "bare_earth")
        stack, result, x, y, key = self._viewshed_sweep_cached(
            spec["point"], agl_m=agl, target_agl_m=target_agl,
            refraction=refraction, max_km=max_km, surface=surface)
        minx, miny, maxx, maxy = stack.bounds
        masks = result["visible"]
        negate = shape == "hidden_from"
        analyzed = result["stats"]["analyzed_extent_km"]
        note = None
        if max_km is not None and max_km > analyzed + 1e-6:
            note = "needs_fetch: requested max_km exceeds analyzed terrain; absence outside this range is unknown"
        visible_area = result["stats"]["visible_km2"] * 1_000_000.0
        total_area = sum(np.count_nonzero(np.isfinite(r.ground)) * r.cell_area_m2
                         for r in stack.rings)
        area = (total_area - visible_area) if negate else visible_area
        def contains(px, py):
            val = stack.mask_contains(masks, px, py)
            if not negate:
                return val
            if val:
                return False
            # hidden_from means "analyzed and not visible" -- terrain outside
            # the loaded rings is unknown, never claimed hidden.
            ground = stack.sample_components(np.asarray([px]), np.asarray([py]))[0][0]
            return bool(np.isfinite(ground))
        return Region(
            shape,
            (minx, miny, maxx, maxy),
            contains,
            area,
            f"{shape.replace('_', ' ')} ({x:.1f},{y:.1f}) agl={agl:g}m surface={surface}",
            metadata={
                "viewshed": {
                    "observer": self.georef.echo(x, y),
                    "agl_m": agl,
                    "target_agl_m": target_agl,
                    "surface": surface,
                    "refraction": refraction,
                    "k": result["k"],
                    "analyzed_extent_km": analyzed,
                    "manifest_hash": stack.manifest_hash,
                    "memo_key": str(key),
                    "note": note,
                }
            })

    # -- low-level reads ----------------------------------------------------

    def kinds(self):
        return self._cache("kinds", lambda: [
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT kind FROM entities ORDER BY kind")])

    def _require_kind(self, kind):
        if kind not in self.kinds():
            raise TwinQueryError(f"unknown entity kind: {kind!r}", valid_kinds=self.kinds())

    def _runs_by_id(self):
        return self._cache("runs", lambda: {
            r[0]: {"run_id": r[0], "script": r[1], "started_at": r[2],
                   "finished_at": r[3], "inputs_hash": r[4], "notes": r[5]}
            for r in self.conn.execute(
                "SELECT run_id, script, started_at, finished_at, inputs_hash, notes"
                " FROM pipeline_runs")})

    def _alive_ids(self, kind):
        return self._cache(("alive", kind), lambda: set(self.store.alive_entities(kind)))

    def _latest_full(self, kind):
        """{entity_id: {attr: (encoded_value, observed_at, run_id, source,
        confidence)}} — latest observation per (entity, attr), one ordered
        scan (no N+1)."""
        def build():
            out = {}
            for eid, attr, value, at, run_id, source, conf in self.conn.execute(
                    "SELECT o.entity_id, o.attr, o.value, o.observed_at, o.run_id,"
                    " o.source, o.confidence"
                    " FROM observations o JOIN entities e ON e.entity_id = o.entity_id"
                    " WHERE e.kind = ? ORDER BY o.obs_id", (kind,)):
                out.setdefault(eid, {})[attr] = (value, at, run_id, source, conf)
            return out
        return self._cache(("latest", kind), build)

    def _vector_table(self, kind):
        """The spatial table carrying a kind's geometry: the static map for
        the base kinds, plus survey kinds (docs/survey.md), whose table name
        is the kind itself (survey_trails etc.)."""
        table = VECTOR_KINDS.get(kind)
        if table is None and kind.startswith("survey_"):
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (kind,)).fetchone()
            table = kind if row else None
        return table

    def _positions(self, kind):
        """{entity_id: (x, y)} — point layers directly; vector layers by
        centroid; building_model by its latest placement observation."""
        def build():
            if kind in POINT_KINDS:
                return {eid: (x, y) for eid, (x, y, _s)
                        in self.store.points(POINT_KINDS[kind]).items()}
            if self._vector_table(kind):
                out = {}
                for eid, blob in self.conn.execute(
                        f"SELECT entity_id, geom FROM {self._vector_table(kind)}"):
                    gj = parse_gpkg_geometry(blob)
                    if gj:
                        centroid, _bbox = geometry_centroid_and_bbox(gj)
                        if centroid:
                            out[eid] = centroid
                return out
            if kind == "building_model":
                out = {}
                for eid, attrs in self._latest_full(kind).items():
                    if "placement" in attrs:
                        p = twin_store.decode_value(attrs["placement"][0])
                        out[eid] = (p["x"], p["y"])
                return out
            return {}
        return self._cache(("positions", kind), build)

    def _geometries(self, kind):
        """{entity_id: GeoJSON geometry} for kinds with real vector geometry.

        _positions() keeps centroids for display and entity anchoring. Proximity
        filters must use these geometries instead.
        """
        def build():
            table = self._vector_table(kind)
            if not table:
                return {}
            out = {}
            for eid, blob in self.conn.execute(f"SELECT entity_id, geom FROM {table}"):
                gj = parse_gpkg_geometry(blob)
                if gj:
                    out[eid] = gj
            return out
        return self._cache(("geometries", kind), build)

    def _entity_geometry_or_point(self, eid):
        """GeoJSON geometry for an entity, falling back to its point position."""
        kind = self._entity_row(eid)[1]
        geom = self._geometries(kind).get(eid)
        if geom:
            return kind, geom
        _kind, (x, y) = self._entity_position(eid)
        return kind, point_geometry(x, y)

    def _entity_row(self, eid):
        row = self.conn.execute(
            "SELECT entity_id, kind, created_run_id, created_at, retired_run_id,"
            " retired_at FROM entities WHERE entity_id = ?", (eid,)).fetchone()
        if row is None:
            raise TwinQueryError(f"unknown entity_id: {eid!r}",
                                 hint="find_entities(kind=...) lists valid IDs",
                                 valid_kinds=self.kinds())
        return row

    def _attrs_with_provenance(self, kind, eid, only=None):
        runs = self._runs_by_id()
        out = {}
        for attr, (value, at, run_id, source, conf) in self._latest_full(kind).get(eid, {}).items():
            if attr == "id" or (only is not None and attr not in only):
                continue  # "id" duplicates entity_id
            out[attr] = {
                "value": twin_store.decode_value(value),
                "observed_at": at,
                "run_id": run_id,
                "run_script": runs.get(run_id, {}).get("script"),
                "source": source,
                "confidence": conf,
            }
        return out

    def _entity_position(self, eid):
        kind = self._entity_row(eid)[1]
        pos = self._positions(kind).get(eid)
        if pos is None:
            raise TwinQueryError(f"entity {eid} has no position/geometry")
        return kind, pos

    # -- atlas data ----------------------------------------------------------

    def _atlas_catalog(self):
        """Viewer-ready atlas layers (the ones with local data files), merged
        with their provenance row from the store's layers table."""
        def build():
            try:
                with open(VIEWER_LAYERS) as fh:
                    viewer = json.load(fh)
            except OSError:
                raise TwinQueryError("atlas catalog missing — run `npm run build-atlas`",
                                     path=VIEWER_LAYERS)
            table = self._layers_table()
            catalog = {}
            for layer in viewer.get("layers", []):
                merged = dict(layer)
                # the viewer entry wins (friendly labels); the table row only
                # contributes what the viewer file lacks (acquisition etc.)
                for k, v in table.get(layer["id"], {}).items():
                    if v is not None and merged.get(k) in (None, ""):
                        merged[k] = v
                catalog[layer["id"]] = merged
            catalog["__species_grids__"] = viewer.get("gap_species_grids")
            return catalog
        return self._cache("atlas", build)

    def _atlas_manifest(self):
        """Raw acquisition manifest rows keyed by layer id/name, when present."""
        def build():
            path = os.path.join(DATA, "atlas", "atlas-manifest.json")
            try:
                with open(path) as fh:
                    manifest = json.load(fh)
            except (OSError, ValueError):
                return {}
            out = {}
            for row in manifest.get("layers", []):
                lid = row.get("name") or row.get("id") or row.get("layer_id")
                if lid:
                    out[lid] = row
            return out
        return self._cache("atlas_manifest", build)

    def _layers_table(self):
        return self._cache("layers_table", lambda: {
            r[0]: {"layer_id": r[0], "label": r[1], "kind": r[2], "acquisition": r[3],
                   "service": r[4], "source_path": r[5], "fetched_at": r[6],
                   "feature_count": r[7], "status": r[8], "content_sha1": r[9]}
            for r in self.conn.execute(
                "SELECT layer_id, label, kind, acquisition, service, source_path,"
                " fetched_at, feature_count, status, content_sha1 FROM layers")})

    def _atlas_layers(self):
        return [v for k, v in self._atlas_catalog().items() if k != "__species_grids__"]

    def _layer_data(self, layer):
        """Lazily loaded layer payload: geojson features (scene-local) for
        vectors, the value grid for rasters."""
        def build():
            if layer["type"] == "raster":
                with open(os.path.join(DATA, layer["grid"])) as fh:
                    return {"grid": json.load(fh)}
            with open(os.path.join(DATA, layer["file"])) as fh:
                return json.load(fh)
        return self._cache(("layer_data", layer["id"]), build)

    def _species_grids(self):
        def build():
            rel = self._atlas_catalog().get("__species_grids__")
            if not rel:
                return None
            with open(os.path.join(DATA, rel)) as fh:
                return json.load(fh)
        return self._cache("species_grids", build)

    def _terrain_grids(self):
        def build():
            grids = []
            for path in (TERRAIN_GRID, APRON_GRID):
                try:
                    with open(path) as fh:
                        grids.append(json.load(fh))
                except OSError:
                    pass
            return grids
        return self._cache("terrain_grids", build)

    def _extent(self):
        """The twin's queryable extent: union of the raster atlas bounds and
        the terrain grids (scene-local meters)."""
        def build():
            boxes = [l["bounds_local"] for l in self._atlas_layers()
                     if l["type"] == "raster" and l.get("bounds_local")]
            for g in self._terrain_grids():
                boxes.append([g.get("outerMinX", g["minX"]), g.get("outerMinY", g["minY"]),
                              g.get("outerMaxX", g["maxX"]), g.get("outerMaxY", g["maxY"])])
            return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
        return self._cache("extent", build)

    def _layer_provenance(self, layer):
        return {k: layer.get(k) for k in
                ("layer_id", "label", "acquisition", "service", "source_path", "fetched_at")
                if layer.get(k) is not None}

    @staticmethod
    def _layer_text_metadata(*sources):
        """Natural-language metadata carried by manifests/catalogs."""
        keys = (
            "description", "abstract", "summary", "purpose", "metadata",
            "service_title", "service_description", "source_description",
            "license_note", "notes",
        )
        out = {}
        for src in sources:
            if not isinstance(src, dict):
                continue
            for k in keys:
                v = src.get(k)
                if not isinstance(v, str) or not v.strip() or k in out:
                    continue
                text = v.strip()
                # Some manifests use "summary" for a sidecar JSON path; keep
                # text_metadata for human-readable descriptions only.
                if re.search(r"\.(json|geojson|gpkg|shp|tif|tiff|png|jpg|jpeg)$", text, re.I):
                    continue
                out[k] = text
        return out

    @staticmethod
    def _layer_themes(entry):
        text = " ".join(str(entry.get(k) or "") for k in
                        ("layer_id", "id", "label", "kind", "source_path",
                         "service", "group", "description")).lower()
        rules = {
            "soil": ("soil", "ssurgo", "mukey", "mapunit"),
            "ecology": ("ecoregion", "habitat", "species", "gap", "wildlife",
                        "vegetation", "landfire"),
            "water": ("wetland", "stream", "hydro", "water", "flow", "pond",
                      "seep", "watershed"),
            "land_cover": ("land cover", "nlcd", "landfire", "forest",
                           "developed", "cover"),
            "geology": ("geolog", "surficial", "bedrock"),
            "hazard_or_protection": ("hazard", "protected", "rare", "padus",
                                     "conservation", "designation"),
            "access": ("road", "trail", "access"),
            "imagery": ("imagery", "aerial", "orthophoto"),
            "hydrology": ("hydrology", "wetness", "runoff", "scenario"),
            "survey": ("survey", "qfield", "photo"),
        }
        return [theme for theme, needles in rules.items()
                if any(n in text for n in needles)]

    def _layer_preview(self, layer):
        """Small catalog preview so agents can choose unfamiliar layers."""
        if not layer:
            return {}
        preview = {}
        try:
            data = self._layer_data(layer)
        except Exception:
            return preview
        if layer.get("type") == "raster":
            grid = data.get("grid") or {}
            legend = grid.get("legend") or {}
            names = []
            for key in sorted(legend, key=lambda v: str(v))[:12]:
                name = (legend.get(str(key)) or {}).get("name")
                if name:
                    names.append(name)
            if names:
                preview["legend_preview"] = names
            if layer.get("bounds_local"):
                preview["bounds_scene_m"] = layer["bounds_local"]
        else:
            features = data.get("features", [])
            geom_types = []
            fields = set()
            labels = []
            for f in features[:200]:
                gtype = (f.get("geometry") or {}).get("type")
                if gtype and gtype not in geom_types:
                    geom_types.append(gtype)
                props = f.get("properties") or {}
                fields.update(k for k in props if k not in HIDE_PROPS)
                lbl = props.get("__label")
                if lbl and lbl not in labels:
                    labels.append(lbl)
                if len(labels) >= 8 and len(fields) >= 12:
                    break
            if geom_types:
                preview["geometry_types"] = geom_types
            if fields:
                preview["field_preview"] = sorted(fields)[:12]
            if labels:
                preview["label_preview"] = labels[:8]
        return preview

    @staticmethod
    def _raster_value_name(grid, layer, value):
        legend = (grid.get("legend") or {}).get(str(value))
        if legend and legend.get("name"):
            return legend["name"]
        unit = grid.get("value_unit") or layer.get("value_unit")
        if unit and unit != "year":
            return f"{value} {unit}"
        return str(value)

    # -- survey companion (docs/survey.md) ------------------------------------

    def _survey_catalog(self):
        """The survey-layers.json catalog (one entry per uploaded survey
        layer), or [] when nothing has been surveyed yet."""
        def build():
            try:
                with open(SURVEY_CATALOG) as fh:
                    return json.load(fh).get("layers", [])
            except (OSError, ValueError):
                return []
        return self._cache("survey_catalog", build)

    def _survey_features(self, layer):
        """The scene-local GeoJSON features for one survey layer."""
        def build():
            try:
                with open(os.path.join(DATA, layer["file"])) as fh:
                    return json.load(fh).get("features", [])
            except (OSError, ValueError):
                return []
        return self._cache(("survey_features", layer["id"]), build)

    # -- hydrology simulation (the Simulation window) -------------------------

    def _hydro_catalog(self):
        """simulation-layers.json entries (Tier-1 derived + any scenario
        layers), or [] when `npm run analyze-hydrology` hasn't run."""
        def build():
            try:
                with open(HYDRO_SIM_CATALOG) as fh:
                    return json.load(fh).get("layers", [])
            except (OSError, ValueError):
                return []
        return self._cache("hydro_catalog", build)

    def _hydro_grid(self, layer):
        def build():
            with open(os.path.join(DATA, layer["grid"])) as fh:
                return json.load(fh)
        return self._cache(("hydro_grid", layer["id"]), build)

    def _fire_catalog(self):
        """fire-layers.json entries (Tier-1 fuels + any scenario layers), or
        [] when `npm run analyze-fuels` hasn't run."""
        def build():
            try:
                with open(FIRE_SIM_CATALOG) as fh:
                    return json.load(fh).get("layers", [])
            except (OSError, ValueError):
                return []
        return self._cache("fire_catalog", build)

    def _fire_grid(self, layer):
        def build():
            with open(os.path.join(DATA, layer["grid"])) as fh:
                return json.load(fh)
        return self._cache(("fire_grid", layer["id"]), build)

    def _et_catalog(self):
        def build():
            try:
                with open(ET_LAYER_CATALOG) as fh:
                    return json.load(fh).get("layers", [])
            except (OSError, ValueError):
                return []
        return self._cache("et_catalog", build)

    def _et_grid(self, layer):
        def build():
            with open(os.path.join(DATA, layer["grid"])) as fh:
                return json.load(fh)
        return self._cache(("et_grid", layer["id"]), build)

    def _soil_water_daily(self):
        def build():
            import csv
            try:
                return list(csv.DictReader(open(ET_SOIL_WATER_DAILY)))
            except OSError:
                return []
        return self._cache("soil_water_daily", build)

    @staticmethod
    def _read_json(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _soils(self):
        """(scene-local soil polygons, {mukey: tabular attrs}) — the SSURGO
        join behind the seep score and per-cell scenario hydraulic state."""
        def build():
            feats = (self._read_json(SOILS_FEATURES) or {}).get("features", [])
            tab = (self._read_json(SOILS_TABULAR) or {}).get("map_units", {})
            return feats, tab
        return self._cache("soils", build)

    def _soil_at(self, x, y):
        feats, tab = self._soils()
        for f in feats:
            rings = polygon_rings(f.get("geometry") or {})
            if rings and point_in_rings(rings, x, y):
                mukey = str((f.get("properties") or {}).get("mukey", ""))
                info = dict(tab.get(mukey, {}))
                info["mukey"] = mukey
                return info
        return None

    # -- attr filters ---------------------------------------------------------

    _FILTER_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(>=|<=|!=|=|>|<)\s*(.+?)\s*$")

    def _parse_filters(self, attr_filters):
        if attr_filters is None:
            return []
        if isinstance(attr_filters, str):
            attr_filters = [attr_filters]
        parsed = []
        for f in attr_filters:
            m = self._FILTER_RE.match(f) if isinstance(f, str) else None
            if not m:
                raise TwinQueryError(
                    'attr_filters entries look like "height > 20" or "type = evergreen"'
                    " (ops: = != > >= < <=)", got=f)
            attr, op, raw = m.groups()
            raw = raw.strip("'\"")
            try:
                value = float(raw)
            except ValueError:
                value = {"true": True, "false": False}.get(raw.lower(), raw)
            parsed.append((attr, op, value))
        return parsed

    @staticmethod
    def _filter_match(actual, op, expected):
        if actual is None:
            return False
        if isinstance(expected, float):
            try:
                a = float(actual)
            except (TypeError, ValueError):
                return False
            return {"=": a == expected, "!=": a != expected, ">": a > expected,
                    ">=": a >= expected, "<": a < expected, "<=": a <= expected}[op]
        if op not in ("=", "!="):
            raise TwinQueryError(
                f"ordering comparison needs a numeric value, got {expected!r}")
        equal = (str(actual).lower() == str(expected).lower()
                 if isinstance(expected, str) else actual == expected)
        return equal if op == "=" else not equal

    # ======================================================== public queries

    def describe_place(self):
        """Lightweight place/coordinate orientation without layer inventory."""
        crs = self.store.get_meta("crs")
        minx, miny, maxx, maxy = self._extent()
        aoi = _rings_region("aoi", _aoi_rings(), "aoi")
        ax0, ay0, ax1, ay1 = aoi.bounds
        return {
            "twin_id": self.store.get_meta("twin_id") or os.path.basename(os.path.dirname(self._store_path)),
            "name": self.store.get_meta("twin_name") or "VEIL digital twin",
            "crs": crs,
            "origin_utm": self.store.get_meta("origin_utm"),
            "coordinate_convention": (
                f"scene-local meters: x = east, y = north ({self.georef.crs} minus "
                "origin_utm). Tools accept {lat,lon} degrees or {x,y} meters; "
                "results echo both."),
            "extent_scene_m": [round(v, 1) for v in (minx, miny, maxx, maxy)],
            "extent_corners": {
                "southwest": self.georef.echo(minx, miny),
                "northeast": self.georef.echo(maxx, maxy)},
            "aoi": {
                "area_m2": round(aoi.area_m2, 1),
                "bounds_scene_m": [round(v, 1) for v in aoi.bounds],
                "southwest": self.georef.echo(ax0, ay0),
                "northeast": self.georef.echo(ax1, ay1)},
        }

    def describe_twin(self):
        """Origin, CRS, extent, entity-kind counts, run history — orientation."""
        crs = self.store.get_meta("crs")
        counts = {kind: {"alive": 0, "total": 0} for kind in self.kinds()}
        for kind, retired, n in self.conn.execute(
                "SELECT kind, retired_run_id IS NOT NULL, COUNT(*)"
                " FROM entities GROUP BY 1, 2"):
            counts[kind]["total"] += n
            if not retired:
                counts[kind]["alive"] += n
        minx, miny, maxx, maxy = self._extent()
        aoi = _rings_region("aoi", _aoi_rings(), "aoi")
        ax0, ay0, ax1, ay1 = aoi.bounds
        layer_rows = list(self._layers_table().values())
        return {
            "twin_id": self.store.get_meta("twin_id") or os.path.basename(os.path.dirname(self._store_path)),
            "name": self.store.get_meta("twin_name") or "VEIL digital twin",
            "crs": crs,
            "origin_utm": self.store.get_meta("origin_utm"),
            "store_path": self._store_path,
            "data_dir": os.path.dirname(self._store_path),
            "schema_version": self.store.get_meta("schema_version"),
            "coordinate_convention": (
                f"scene-local meters: x = east, y = north ({self.georef.crs} minus "
                "origin_utm). Tools accept {lat,lon} degrees or {x,y} meters; "
                "results echo both."),
            "extent_scene_m": [round(v, 1) for v in (minx, miny, maxx, maxy)],
            "extent_corners": {
                "southwest": self.georef.echo(minx, miny),
                "northeast": self.georef.echo(maxx, maxy)},
            "aoi": {
                "area_m2": round(aoi.area_m2, 1),
                "bounds_scene_m": [round(v, 1) for v in aoi.bounds],
                "southwest": self.georef.echo(ax0, ay0),
                "northeast": self.georef.echo(ax1, ay1)},
            "entity_counts": counts,
            "pipeline_runs": sorted(self._runs_by_id().values(),
                                    key=lambda r: r["run_id"]),
            "layers": {
                "total": len(layer_rows),
                "with_data": sum(1 for r in layer_rows if r["status"] == "ok"),
                "empty_for_parcel": sum(1 for r in layer_rows if r["status"] == "empty"),
                "viewer_ready": len(self._atlas_layers())},
            "vegetation_metadata": self.store.get_meta("vegetation_metadata"),
        }

    def find_entities(self, kind, near=None, within_m=None, region=None,
                      attr_filters=None, limit=50):
        """Spatially + attribute-filtered entity search. `near`+`within_m` is
        sugar for the within_m region shape; `near` may also be
        {"entity_id": ...} to center on another entity."""
        self._require_kind(kind)
        near_geometry = None
        near_bounds = None
        near_radius = None
        region_description = None
        if near is not None:
            if region is not None:
                raise TwinQueryError("pass either near+within_m or region, not both")
            if within_m is None:
                raise TwinQueryError("near needs within_m (meters)")
            if isinstance(near, dict) and "entity_id" in near:
                _target_kind, near_geometry = self._entity_geometry_or_point(near["entity_id"])
                try:
                    near_radius = float(within_m)
                except (TypeError, ValueError):
                    raise TwinQueryError("within_m must be a positive number of meters",
                                         got=within_m)
                if near_radius <= 0:
                    raise TwinQueryError("within_m must be a positive number of meters",
                                         got=within_m)
                near_bounds = expand_bbox(geometry_bbox(near_geometry), near_radius)
                region_description = {
                    "shape": "within_m",
                    "bounds_scene_m": [round(v, 3) for v in near_bounds],
                    "area_m2": None,
                    "description": f"within {near_radius:g} m of {near['entity_id']}",
                }
            else:
                nx, ny = resolve_point(near, self.georef)
                near_geometry = point_geometry(nx, ny)
                region = {"within_m": within_m, "point": {"x": nx, "y": ny}}
        reg = self._resolve_region(region)
        if reg is not None and reg.shape == "within_m":
            b = reg.bounds
            nx, ny = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            near_geometry = point_geometry(nx, ny)
            near_bounds = reg.bounds
            near_radius = (b[2] - b[0]) / 2
            region_description = reg.describe()
        elif reg is not None:
            region_description = reg.describe()
        filters = self._parse_filters(attr_filters)
        limit = max(1, min(int(limit or 50), 1000))

        positions = self._positions(kind)
        geometries = self._geometries(kind)
        alive = self._alive_ids(kind)
        latest = self._latest_full(kind)

        matches = []
        for eid, (x, y) in positions.items():
            if eid not in alive:
                continue
            candidate_geometry = geometries.get(eid) or point_geometry(x, y)
            distance_m = None
            if near_geometry is not None:
                candidate_bbox = geometry_bbox(candidate_geometry) or (x, y, x, y)
                if not bboxes_intersect(candidate_bbox, near_bounds):
                    continue
                distance_m = geometry_distance_m(candidate_geometry, near_geometry)
                if distance_m > near_radius:
                    continue
            elif reg is not None:
                if candidate_geometry.get("type") in {"LineString", "MultiLineString"}:
                    if not line_geometry_intersects_region(candidate_geometry, reg):
                        continue
                else:
                    bx0, by0, bx1, by1 = reg.bounds
                    if not (bx0 <= x <= bx1 and by0 <= y <= by1):
                        continue
                    if not reg.contains(x, y):
                        continue
            if filters:
                attrs = latest.get(eid, {})
                ok = True
                for attr, op, expected in filters:
                    actual = (twin_store.decode_value(attrs[attr][0])
                              if attr in attrs else None)
                    if not self._filter_match(actual, op, expected):
                        ok = False
                        break
                if not ok:
                    continue
            matches.append((eid, x, y, distance_m))

        if near_geometry is not None:
            matches.sort(key=lambda m: (m[3], m[0]))
        else:
            matches.sort(key=lambda m: m[0])

        entities = []
        for eid, x, y, distance_m in matches[:limit]:
            entry = {
                "entity_id": eid,
                "kind": kind,
                "position": self.georef.echo(x, y),
                "attrs": self._attrs_with_provenance(kind, eid),
            }
            if kind not in POINT_KINDS and kind != "building_model":
                entry["position_is"] = "centroid"
            if distance_m is not None:
                entry["distance_m"] = round(distance_m, 2)
            entities.append(entry)
        return {
            "kind": kind,
            "region": region_description,
            "attr_filters": attr_filters,
            "total_matched": len(matches),
            "returned": len(entities),
            "entities": entities,
        }

    def get_entity(self, entity_id):
        """Full current state of one entity: latest attrs with provenance,
        geometry, created/retired runs."""
        eid, kind, created_run, created_at, retired_run, retired_at = \
            self._entity_row(entity_id)
        runs = self._runs_by_id()
        out = {
            "entity_id": eid,
            "kind": kind,
            "created": {"run": runs.get(created_run), "at": created_at},
            "retired": ({"run": runs.get(retired_run), "at": retired_at}
                        if retired_run is not None else None),
            "attrs": self._attrs_with_provenance(kind, eid),
        }
        pos = self._positions(kind).get(eid)
        if pos:
            out["position"] = self.georef.echo(*pos)
        if self._vector_table(kind):
            row = self.conn.execute(
                f"SELECT geom FROM {self._vector_table(kind)} WHERE entity_id = ?",
                (eid,)).fetchone()
            if row:
                out["geometry_scene_m"] = parse_gpkg_geometry(row[0])
                out["position_is"] = "centroid"
        return out

    def entity_history(self, entity_id, attr=None):
        """The observation timeline for one entity, oldest first."""
        self._entity_row(entity_id)
        runs = self._runs_by_id()
        rows = self.store.history(entity_id, attr)
        for r in rows:
            r["run_script"] = runs.get(r["run_id"], {}).get("script")
        return {"entity_id": entity_id, "attr": attr,
                "observations": rows, "count": len(rows)}

    # -- site selection ---------------------------------------------------

    def _terrain_elevation(self, x, y):
        """Elevation sample with twin-to-twin fallback across available terrain grids.
        Returns None when no grid provides a numeric value (outside the DEM or no
        data for this location)."""
        for grid in self._terrain_grids():
            value = sample_terrain_elevation(grid, x, y)
            if value is not None:
                return value
        return None

    def _slope_deg(self, x, y, step=2.0):
        """Simple central-difference slope estimate in degrees at a single point.
        Returns None when neighboring samples are unavailable."""
        h = self._terrain_elevation(x, y)
        if h is None:
            return None
        hx1 = self._terrain_elevation(x + step, y)
        hx2 = self._terrain_elevation(x - step, y)
        hy1 = self._terrain_elevation(x, y + step)
        hy2 = self._terrain_elevation(x, y - step)
        if hx1 is None or hx2 is None or hy1 is None or hy2 is None:
            return None
        dzdx = (hx1 - hx2) / (2 * step)
        dzdy = (hy1 - hy2) / (2 * step)
        return math.degrees(math.atan(math.hypot(dzdx, dzdy)))

    def _prominence_and_openness(self, x, y, radius=100.0, ring_points=24):
        """Local terrain context around one point:
        - prominence: center elevation minus ring mean
        - openness proxy: 1 - normalized local standard deviation (higher is more open/flat)
        """
        center = self._terrain_elevation(x, y)
        if center is None:
            return None, None, None
        ring_values = []
        ring_samples = []
        step = max(1.0, radius / 8.0)
        for i in range(ring_points):
            a = (2 * math.pi * i / ring_points)
            ring_samples.append((x + radius * math.cos(a), y + radius * math.sin(a)))
            # Also sample the immediate orthogonal ring for openness.
            ring_samples.append((x + step * math.cos(a), y + step * math.sin(a)))
        for sx, sy in ring_samples:
            value = self._terrain_elevation(sx, sy)
            if value is not None:
                ring_values.append(value)
        if not ring_values:
            return center, None, None
        mean = sum(ring_values) / len(ring_values)
        prominence = center - mean
        if len(ring_values) >= 3:
            var = sum((v - mean) ** 2 for v in ring_values) / len(ring_values)
            stdev = math.sqrt(var)
        else:
            stdev = 0.0
        openness = max(0.0, 1.0 - min(1.0, stdev / 20.0))
        return prominence, openness, stdev

    def _sample_hydro_features(self, x, y):
        """Fast point sample of derived hydrology grids, normalized to 0..1 where possible."""
        out = {"wetness": 0.0, "seep": 0.0, "ponding": 0.0, "flow": 0.0}
        for layer in self._hydro_catalog():
            lid = layer.get("id")
            if lid not in {"wetness_index", "seep_candidates", "ponding", "flow_paths"}:
                continue
            try:
                grid = self._hydro_grid(layer)
                s = sample_grid(grid, layer["bounds_local"], x, y)
            except Exception:
                continue
            v = s[2] if s else None
            if v is None or v == grid.get("nodata"):
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if lid == "wetness_index":
                out["wetness"] = max(0.0, min(1.0, fv / 100.0))
            elif lid == "seep_candidates":
                out["seep"] = max(0.0, min(1.0, fv / 100.0))
            elif lid == "ponding":
                out["ponding"] = max(0.0, min(1.0, fv / 0.5))
            elif lid == "flow_paths":
                out["flow"] = max(0.0, min(1.0, math.log1p(max(0.0, fv)) / math.log1p(50000.0)))
        return out

    def _sample_raster_name(self, layer_id, x, y):
        layer = self._atlas_catalog().get(layer_id)
        if not layer or layer.get("type") != "raster":
            return None
        try:
            grid = self._layer_data(layer)["grid"]
            s = sample_grid(grid, layer["bounds_local"], x, y)
        except Exception:
            return None
        if s is None or s[2] is None or s[2] == grid.get("nodata"):
            return None
        return self._raster_value_name(grid, layer, s[2])

    @staticmethod
    def _landcover_scores(*names):
        text = " ".join(str(n or "").lower() for n in names)
        forest = 1.0 if any(t in text for t in ("forest", "wood", "hardwood", "timber")) else 0.0
        wetland = 1.0 if any(t in text for t in ("wetland", "swamp", "marsh", "bog", "fen")) else 0.0
        openish = 1.0 if any(t in text for t in ("open", "grass", "pasture", "meadow", "shrub", "scrub", "barren", "developed")) else 0.0
        return {"forest_cover": forest, "wetland_cover": wetland, "open_cover": openish}

    @staticmethod
    def _soil_scores(soil):
        if not soil:
            return {"soil_drainage": 0.5}
        group = str(soil.get("hydrologic_group") or "").upper()
        hsg = {"A": 1.0, "B": 0.75, "C": 0.45, "D": 0.2}.get(group[:1], 0.5)
        drainage = str(soil.get("drainage_class") or "").lower()
        if "well" in drainage or "excessive" in drainage:
            dscore = 1.0
        elif "moderate" in drainage:
            dscore = 0.75
        elif "somewhat poor" in drainage:
            dscore = 0.35
        elif "poor" in drainage:
            dscore = 0.15
        else:
            dscore = hsg
        return {"soil_drainage": max(0.0, min(1.0, (hsg + dscore) / 2.0))}

    def _regular_lattice_points(self, reg, target=1500):
        """Deterministic lattice over reg.bounds (scene-local).
        Returns (point list, spacing_used_m)."""
        minx, miny, maxx, maxy = reg.bounds
        if maxx <= minx or maxy <= miny:
            return [], 0.0
        area = max(1.0, (maxx - minx) * (maxy - miny))
        spacing = max(1.0, math.sqrt(area / max(1, target)))
        nx = max(1, int((maxx - minx) / spacing))
        ny = max(1, int((maxy - miny) / spacing))
        if nx <= 0 or ny <= 0:
            return [], spacing
        step_x = (maxx - minx) / nx
        step_y = (maxy - miny) / ny
        pts = []
        for iy in range(ny):
            y = miny + (iy + 0.5) * step_y
            for ix in range(nx):
                x = minx + (ix + 0.5) * step_x
                if reg.contains(x, y):
                    pts.append((x, y))
        return pts, min(step_x, step_y)

    @staticmethod
    def _terrain_cell_xy(grid, row, col):
        width = max(1, int(grid["width"]))
        height = max(1, int(grid["height"]))
        xden = max(1, width - 1)
        yden = max(1, height - 1)
        x = float(grid["minX"]) + (float(col) / xden) * (float(grid["maxX"]) - float(grid["minX"]))
        y = float(grid["maxY"]) - (float(row) / yden) * (float(grid["maxY"]) - float(grid["minY"]))
        return x, y

    def _refine_to_local_dem_max(self, x, y, reg, radius_m):
        """Snap to the highest DEM cell near a seed while staying in region."""
        best = None
        radius_m = max(0.0, float(radius_m))
        for grid in self._terrain_grids():
            heights = grid.get("heights") or []
            width = int(grid.get("width") or 0)
            height = int(grid.get("height") or 0)
            if width <= 0 or height <= 0 or not heights:
                continue
            x_span = max(1e-9, float(grid["maxX"]) - float(grid["minX"]))
            y_span = max(1e-9, float(grid["maxY"]) - float(grid["minY"]))
            cell_x = x_span / max(1, width - 1)
            cell_y = y_span / max(1, height - 1)
            c0 = max(0, int((x - radius_m - float(grid["minX"])) / cell_x) - 1)
            c1 = min(width - 1, int((x + radius_m - float(grid["minX"])) / cell_x) + 1)
            r0 = max(0, int((float(grid["maxY"]) - (y + radius_m)) / cell_y) - 1)
            r1 = min(height - 1, int((float(grid["maxY"]) - (y - radius_m)) / cell_y) + 1)
            for row in range(r0, r1 + 1):
                base = row * width
                for col in range(c0, c1 + 1):
                    elev = heights[base + col]
                    if not isinstance(elev, (int, float)):
                        continue
                    cx, cy = self._terrain_cell_xy(grid, row, col)
                    if math.hypot(cx - x, cy - y) > radius_m:
                        continue
                    if not reg.contains(cx, cy):
                        continue
                    if best is None or float(elev) > best[2]:
                        best = (cx, cy, float(elev))
        return best

    # -- generalized site constraints -----------------------------------------

    def _gap_species_vocab(self):
        """Lowercased common name -> canonical common name for every GAP
        modeled-habitat species this twin carries ({} when none)."""
        def build():
            sg = self._species_grids()
            if not sg:
                return {}
            return {s["common_name"].lower(): s["common_name"]
                    for s in sg.get("species", {}).values()
                    if s.get("common_name")}
        return self._cache("gap_species_vocab", build)

    def _gap_species_at(self, x, y):
        """Sorted GAP modeled-habitat species present at a scene-local point.
        Returns None when there is no GAP grid or the point is outside it (so
        identify_at can distinguish "no data" from "no species here"); a list
        (possibly empty) when the point is inside the grid. This is the exact
        sampler identify_at and click-to-identify use."""
        sg = self._species_grids()
        if not sg:
            return None
        bx0, by0, bx1, by1 = sg["bounds_local"]
        if not (bx0 <= x <= bx1 and by0 <= y <= by1):
            return None
        col = min(sg["width"] - 1, int((x - bx0) / (bx1 - bx0) * sg["width"]))
        row = min(sg["height"] - 1, int((by1 - y) / (by1 - by0) * sg["height"]))
        return sorted(
            s["common_name"] for s in sg["species"].values()
            if row < len(s["rows"]) and col < len(s["rows"][row])
            and s["rows"][row][col] == "1")

    @staticmethod
    def _normalize_constraint(c):
        """Accept a constraint dict in a few spellings and return the canonical
        {signal, op, value, layer_id} form. `signal`/`type`/`field` and
        `value`/`values` are interchangeable; gap_species defaults to `includes`,
        everything else to `==`."""
        if not isinstance(c, dict):
            raise TwinQueryError(
                "each constraint must be an object like "
                '{"signal": "terrain.slope_deg", "op": "<=", "value": 12}',
                got=c)
        signal = c.get("signal") or c.get("type") or c.get("field")
        if not signal:
            raise TwinQueryError("constraint needs a 'signal'", got=c)
        signal = str(signal)
        op = c.get("op") or c.get("operator")
        if not op:
            op = "includes" if signal == "gap_species" else "=="
        value = c.get("value", c.get("values"))
        return {"signal": signal, "op": str(op), "value": value,
                "layer_id": c.get("layer_id")}

    # Signals the evaluator understands today. Vector / entity / survey signals
    # are a deliberate future extension: the dict schema already carries them,
    # they simply aren't sampled yet (see recommend_sites provenance notes).
    _SUPPORTED_SIGNALS = (
        "gap_species", "terrain.slope_deg",
        "hydrology.wetness", "hydrology.seep", "hydrology.flow", "hydrology.ponding",
        "raster_class", "soil_drainage",
        "soil.hydrologic_group", "soil.drainage_class",
    )

    def _signal_actual(self, signal, x, y, layer_id=None):
        """Freshly sample one constraint signal at a scene-local point. Fresh
        sampling (not cached row values) is what makes the final pre-draw
        re-check independent of scoring."""
        if signal == "gap_species":
            return self._gap_species_at(x, y) or []
        if signal == "terrain.slope_deg":
            return self._slope_deg(x, y)
        if signal.startswith("hydrology."):
            return self._sample_hydro_features(x, y).get(signal.split(".", 1)[1])
        if signal == "soil_drainage":
            return self._soil_scores(self._soil_at(x, y)).get("soil_drainage")
        if signal in ("soil.hydrologic_group", "soil_hydrologic_group"):
            return (self._soil_at(x, y) or {}).get("hydrologic_group")
        if signal in ("soil.drainage_class", "soil_drainage_class"):
            return (self._soil_at(x, y) or {}).get("drainage_class")
        if signal == "raster_class":
            return self._sample_raster_name(layer_id, x, y)
        return None

    @staticmethod
    def _apply_constraint_op(actual, op, value):
        """Boolean test of one sampled value against a constraint operator.
        Set / membership ops for categorical signals; numeric comparisons with a
        string-equality fallback for everything else. Missing data fails."""
        if op in ("includes", "present", "contains"):
            present = {str(n).lower() for n in (actual or [])}
            wanted = value if isinstance(value, (list, tuple)) else [value]
            wanted = [str(w).lower() for w in wanted if w is not None]
            if not wanted:
                return False
            if op == "present":
                return any(w in present for w in wanted)
            return all(w in present for w in wanted)
        if op in ("in", "not_in"):
            if actual is None:
                return False
            vals = value if isinstance(value, (list, tuple)) else [value]
            member = str(actual).lower() in {str(v).lower() for v in vals}
            return member if op == "in" else not member
        num_ops = {"<", "<=", ">", ">=", "==", "=", "eq", "!=", "ne"}
        if op in num_ops:
            try:
                a = float(actual)
                v = float(value)
            except (TypeError, ValueError):
                if actual is None:
                    return False
                equal = str(actual).lower() == str(value).lower()
                if op in ("==", "=", "eq"):
                    return equal
                if op in ("!=", "ne"):
                    return not equal
                return False
            return {"<": a < v, "<=": a <= v, ">": a > v, ">=": a >= v,
                    "==": a == v, "=": a == v, "eq": a == v,
                    "!=": a != v, "ne": a != v}[op]
        return False

    def _eval_constraint(self, norm, x, y):
        """Evaluate one normalized constraint at a point -> result dict."""
        actual = self._signal_actual(norm["signal"], x, y, norm.get("layer_id"))
        passed = self._apply_constraint_op(actual, norm["op"], norm["value"])
        reported = actual
        if isinstance(actual, float):
            reported = round(actual, 4)
        return {"signal": norm["signal"], "op": norm["op"], "value": norm["value"],
                "layer_id": norm.get("layer_id"), "actual": reported,
                "passed": bool(passed)}

    def _eval_constraints(self, norms, x, y):
        """Evaluate a list of normalized constraints -> (all_passed, results)."""
        results = [self._eval_constraint(n, x, y) for n in norms]
        return (all(r["passed"] for r in results), results)

    def _validate_site_constraints(self, norms):
        """Fail fast on unsupported or underspecified hard-filter constraints.

        A bad signal/layer must not turn into "no data" and accidentally pass an
        exclusion predicate, nor silently return zero candidates without telling
        the caller what was invalid.
        """
        supported = set(self._SUPPORTED_SIGNALS)
        raster_ids = sorted(
            lid for lid, layer in self._atlas_catalog().items()
            if isinstance(layer, dict) and layer.get("type") == "raster"
        )
        for norm in norms:
            signal = norm.get("signal")
            if signal not in supported:
                raise TwinQueryError(
                    "unsupported_recommend_sites_constraint",
                    signal=signal,
                    supported_signals=sorted(supported),
                )
            if signal == "raster_class":
                layer_id = norm.get("layer_id")
                if layer_id not in raster_ids:
                    raise TwinQueryError(
                        "invalid_raster_constraint_layer",
                        layer_id=layer_id,
                        valid_raster_layer_ids=raster_ids,
                    )

    def _interpret_site_request(self, intent_text, hard_filters):
        """Bind a free-form site request to a structured intent without silently
        discarding target terms. Returns (normalized_objective, applied_filters,
        interpretation, unresolved_terms).

        - Natural-language species targets ("... for Gray Fox") become a
          gap_species `includes` hard filter when the name is in the GAP
          vocabulary; otherwise they are surfaced as unresolved terms.
        - Explicit hard_filters are normalized and merged; gap_species filters
          naming species outside the vocabulary contribute unresolved terms.
        """
        text = str(intent_text or "")
        low = text.lower()
        vocab = self._gap_species_vocab()

        detected_species = sorted({
            canon for lname, canon in vocab.items()
            if re.search(r"\b" + re.escape(lname) + r"\b", low)
        })

        unresolved = []
        # "for <phrase>" targets that look like a proper noun (a named species,
        # place, etc.) but resolve to nothing known are unresolved — generic
        # phrases ("for the view") are ignored so legacy calls keep working.
        for phrase in re.findall(
                r"\bfor\s+(?:a\s+|an\s+|the\s+|some\s+)?([A-Za-z][A-Za-z '\-]*)", text):
            phrase = phrase.strip()
            if not phrase:
                continue
            pl = phrase.lower()
            if any(re.search(r"\b" + re.escape(l) + r"\b", pl) for l in vocab):
                continue  # a known species is embedded — resolved above
            words = pl.split()
            if all(w in _GENERIC_TARGET_WORDS for w in words):
                continue  # purely generic objective phrasing
            if any(w not in _GENERIC_TARGET_WORDS for w in words):
                unresolved.append(phrase)

        applied = []
        sources = []
        if detected_species:
            applied.append({"signal": "gap_species", "op": "includes",
                            "value": detected_species, "source": "intent"})
            sources.append("intent")

        for raw in (hard_filters or []):
            norm = self._normalize_constraint(raw)
            if norm["signal"] == "gap_species":
                wanted = norm["value"] if isinstance(norm["value"], (list, tuple)) \
                    else [norm["value"]]
                resolved = []
                for w in wanted:
                    if w is None:
                        continue
                    canon = vocab.get(str(w).lower())
                    if canon is None:
                        unresolved.append(str(w))
                    else:
                        resolved.append(canon)
                norm["value"] = resolved or wanted
            norm["source"] = raw.get("source", "explicit") if isinstance(raw, dict) else "explicit"
            applied.append(norm)
            sources.append("explicit")

        # de-dup unresolved terms preserving order
        seen = set()
        unresolved = [t for t in unresolved
                      if not (t.lower() in seen or seen.add(t.lower()))]

        obj = _normalize_site_objective(text)
        interpretation = {
            "raw_intent": text,
            "normalized_objective": obj,
            "detected_species": detected_species,
            "filter_sources": sorted(set(sources)),
            "supported_signals": list(self._SUPPORTED_SIGNALS),
            "note": ("vector/entity/survey constraints are a documented future "
                     "extension of this schema; not yet sampled."),
        }
        return obj, applied, interpretation, unresolved

    def recommend_sites(self, objective="overlook", region=None, count=3,
                       min_separation_m=120.0, draw=True,
                       label_prefix=None, purpose=None, hard_filters=None,
                       preferences=None, avoid=None, strict=False, validate=True):
        """Generate and rank candidate sites inside a region with objective-specific
        terrain, hydrology, soil, and land-cover features.

        The helper is a general site-ranking baseline:
        - deterministic lattice candidates clipped to the requested region
        - DEM elevation, local prominence, slope/roughness/openness
        - derived hydrology wetness, seep, flow, and ponding grids
        - SSURGO hydrologic group / drainage-class signal
        - NLCD/LANDFIRE land-cover signals
        - objective-specific weighted scoring plus greedy separation/NMS

        Returned candidates include both the scored feature bundle and local
        identify_at evidence for reproducibility and field-check planning.
        """
        raw_intent = str(purpose if purpose is not None else objective or "overlook")
        if re.search(r"\b(solar|pv|panel|photovoltaic|sun|insolation|irradiance)\b", raw_intent, re.I):
            solar_objective = "winter_kwh" if re.search(r"\bwinter\b", raw_intent, re.I) else "annual_kwh"
            return self.recommend_solar_sites(
                region=region,
                objective=solar_objective,
                count=count,
                surface=("bare_earth" if re.search(r"\b(bare|cleared|no[- ]?tree|remove trees?)\b", raw_intent, re.I)
                         else "canopy"),
                system_kw=1.0,
                demonstrate=draw,
            )
        if preferences:
            raise TwinQueryError(
                "recommend_sites_preferences_not_implemented",
                detail="Non-empty preferences are not implemented yet; use hard_filters for this phase.",
            )
        if avoid:
            raise TwinQueryError(
                "recommend_sites_avoid_not_implemented",
                detail="Non-empty avoid constraints are not implemented yet; use hard_filters for this phase.",
            )
        obj, applied_filters, interpretation, unresolved_terms = \
            self._interpret_site_request(raw_intent, hard_filters)
        normalized_filters = [self._normalize_constraint(f) for f in applied_filters]
        self._validate_site_constraints(normalized_filters)
        if region is None:
            region = {"aoi": True}
        reg = self._resolve_region(region)
        if reg is None:
            raise TwinQueryError("recommend_sites needs a region", region=region)

        count = int(count)
        if count <= 0:
            raise TwinQueryError("count must be a positive integer", got=count)
        min_sep = float(min_separation_m)
        if min_sep < 0:
            raise TwinQueryError("min_separation_m must be >= 0", got=min_sep)
        if strict and unresolved_terms:
            raise TwinQueryError(
                "unresolved_recommendation_terms",
                raw_intent=raw_intent,
                purpose=obj,
                objective=obj,
                unresolved_terms=unresolved_terms,
                applied_filters=applied_filters,
                region=reg.describe(),
                draw_count=0,
            )
        if unresolved_terms:
            return {
                "objective": obj,
                "purpose": obj,
                "raw_intent": raw_intent,
                "interpretation": interpretation,
                "applied_filters": applied_filters,
                "unresolved_terms": unresolved_terms,
                "region": reg.describe(),
                "requested_count": count,
                "returned_count": 0,
                "step_m": 0.0,
                "note": "Unresolved recommendation terms; no points were generated or drawn.",
                "provenance": {
                    "tool": "recommend_sites",
                    "draw": False,
                    "min_separation_m": round(min_sep, 3),
                },
                "candidates": [],
                "draw_count": 0,
            }
        if not label_prefix:
            label_prefix = f"{obj} site"

        lattice, spacing = self._regular_lattice_points(reg, target=1500)
        lattice_strategy = "dense_regular_lattice_inside_region"
        candidates = []
        for x, y in lattice:
            elevation = self._terrain_elevation(x, y)
            if elevation is None:
                continue
            prominence, openness, stdev = self._prominence_and_openness(x, y, radius=100.0)
            if prominence is None:
                continue
            slope = self._slope_deg(x, y)
            hydro = self._sample_hydro_features(x, y)
            soil = self._soil_at(x, y)
            soil_scores = self._soil_scores(soil)
            nlcd_name = self._sample_raster_name("nlcd_2019_landcover", x, y)
            landfire_name = self._sample_raster_name("landfire_evt_2024", x, y)
            cover_scores = self._landcover_scores(nlcd_name, landfire_name)
            candidates.append({
                "x": x, "y": y,
                "elevation_m": elevation,
                "prominence_m": prominence,
                "openness": openness if openness is not None else 0.0,
                "roughness": stdev if stdev is not None else 0.0,
                "slope_deg": slope if slope is not None else 90.0,
                "wetness": hydro["wetness"],
                "seep": hydro["seep"],
                "ponding": hydro["ponding"],
                "flow": hydro["flow"],
                "soil_drainage": soil_scores["soil_drainage"],
                "forest_cover": cover_scores["forest_cover"],
                "wetland_cover": cover_scores["wetland_cover"],
                "open_cover": cover_scores["open_cover"],
                "soil": soil,
                "nlcd_name": nlcd_name,
                "landfire_name": landfire_name,
            })

        candidates_generated = len(candidates)
        if normalized_filters:
            filtered = []
            rejected_by_filter = {f.get("signal"): 0 for f in normalized_filters}
            for row in candidates:
                passed, results = self._eval_constraints(normalized_filters, row["x"], row["y"])
                row["constraint_results"] = results
                row["all_hard_passed"] = passed
                if passed:
                    filtered.append(row)
                else:
                    for r in results:
                        if not r.get("passed"):
                            rejected_by_filter[r.get("signal")] = rejected_by_filter.get(r.get("signal"), 0) + 1
                            break
            candidates = filtered
        else:
            rejected_by_filter = {}

        if not candidates:
            return {
                "objective": obj,
                "purpose": obj,
                "raw_intent": raw_intent,
                "interpretation": interpretation,
                "applied_filters": applied_filters,
                "unresolved_terms": unresolved_terms,
                "region": reg.describe(),
                "requested_count": count,
                "returned_count": 0,
                "step_m": round(spacing, 3),
                "note": "No candidate points satisfied the requested region/data and hard filters.",
                "provenance": {
                    "tool": "recommend_sites",
                    "candidates_considered": candidates_generated,
                    "seed_grid_step_m": round(spacing, 3),
                    "draw": False,
                    "min_separation_m": round(min_sep, 3),
                    "rejected_by_filter": rejected_by_filter,
                },
                "candidates": [],
                "draw_count": 0,
            }

        elev_vals = [c["elevation_m"] for c in candidates]
        prom_vals = [c["prominence_m"] for c in candidates]
        opn_vals = [c["openness"] for c in candidates]
        def rng(values):
            return min(values), max(values)
        elev_min, elev_max = rng(elev_vals)
        prom_min, prom_max = rng(prom_vals)
        opn_min, opn_max = rng(opn_vals)

        def normalize(v, lo, hi):
            if hi == lo:
                return 0.5
            return max(0.0, min(1.0, (v - lo) / (hi - lo)))

        profiles = {
            "overlook": {
                "elevation": 0.30, "prominence": 0.30, "openness": 0.15,
                "low_slope": 0.15, "dryness": 0.10,
            },
            "trailcam": {
                "forest_cover": 0.25, "flow": 0.15, "wetness": 0.15,
                "prominence": 0.15, "low_slope": 0.15, "open_cover": 0.10,
                "elevation": 0.05,
            },
            "well": {
                "seep": 0.35, "wetness": 0.25, "flow": 0.15,
                "low_slope": 0.10, "low_ponding": 0.10, "prominence": 0.05,
            },
            "garden": {
                "open_cover": 0.25, "soil_drainage": 0.25, "low_slope": 0.20,
                "wetness": 0.10, "low_ponding": 0.10, "openness": 0.10,
            },
            "structure": {
                "low_slope": 0.35, "soil_drainage": 0.25, "low_ponding": 0.20,
                "open_cover": 0.10, "elevation": 0.10,
            },
        }
        weights = profiles.get(obj, profiles["overlook"])

        def score_row(row):
            features = {
                "elevation": normalize(row["elevation_m"], elev_min, elev_max),
                "prominence": normalize(row["prominence_m"], prom_min, prom_max),
                "openness": normalize(row["openness"], opn_min, opn_max),
                "low_slope": 1.0 - min(1.0, max(0.0, row["slope_deg"]) / 35.0),
                "wetness": row["wetness"],
                "dryness": 1.0 - row["wetness"],
                "seep": row["seep"],
                "ponding": row["ponding"],
                "low_ponding": 1.0 - row["ponding"],
                "flow": row["flow"],
                "soil_drainage": row["soil_drainage"],
                "forest_cover": row["forest_cover"],
                "wetland_cover": row["wetland_cover"],
                "open_cover": row["open_cover"],
            }
            total_w = sum(abs(w) for w in weights.values()) or 1.0
            score = sum(w * features.get(name, 0.0) for name, w in weights.items()) / total_w
            row["features"] = features
            row["score"] = min(1.0, max(0.0, score))

        for row in candidates:
            score_row(row)

        if obj == "overlook":
            original_candidates = candidates
            seed_count = min(len(candidates), max(50, count * 25))
            seeds = sorted(candidates, key=lambda c: (
                -c["score"], -round(c["elevation_m"], 3), -round(c["prominence_m"], 3),
                round(c["x"], 3), round(c["y"], 3)
            ))[:seed_count]
            refined = []
            seen = set()
            refine_radius = min(180.0, max(40.0, spacing * 3.0, min_sep))
            for row in seeds:
                peak = self._refine_to_local_dem_max(row["x"], row["y"], reg, refine_radius)
                if peak is None:
                    refined.append(row)
                    continue
                px, py, pelev = peak
                key = (round(px, 3), round(py, 3))
                if key in seen:
                    continue
                seen.add(key)
                moved = math.hypot(px - row["x"], py - row["y"])
                new_row = dict(row)
                new_row["seed_x"] = row["x"]
                new_row["seed_y"] = row["y"]
                new_row["peak_refinement_m"] = moved
                new_row["x"] = px
                new_row["y"] = py
                new_row["elevation_m"] = pelev
                prominence, openness, stdev = self._prominence_and_openness(px, py, radius=100.0)
                if prominence is not None:
                    new_row["prominence_m"] = prominence
                if openness is not None:
                    new_row["openness"] = openness
                if stdev is not None:
                    new_row["roughness"] = stdev
                slope = self._slope_deg(px, py)
                if slope is not None:
                    new_row["slope_deg"] = slope
                hydro = self._sample_hydro_features(px, py)
                soil = self._soil_at(px, py)
                soil_scores = self._soil_scores(soil)
                nlcd_name = self._sample_raster_name("nlcd_2019_landcover", px, py)
                landfire_name = self._sample_raster_name("landfire_evt_2024", px, py)
                cover_scores = self._landcover_scores(nlcd_name, landfire_name)
                new_row.update({
                    "wetness": hydro["wetness"],
                    "seep": hydro["seep"],
                    "ponding": hydro["ponding"],
                    "flow": hydro["flow"],
                    "soil_drainage": soil_scores["soil_drainage"],
                    "forest_cover": cover_scores["forest_cover"],
                    "wetland_cover": cover_scores["wetland_cover"],
                    "open_cover": cover_scores["open_cover"],
                    "soil": soil,
                    "nlcd_name": nlcd_name,
                    "landfire_name": landfire_name,
                })
                score_row(new_row)
                refined.append(new_row)
            if refined:
                for row in original_candidates:
                    key = (round(row["x"], 3), round(row["y"], 3))
                    if key not in seen:
                        refined.append(row)
                candidates = refined
                lattice_strategy = f"{lattice_strategy}+top_seed_dem_peak_refinement"

        candidates.sort(key=lambda c: (
            -c["score"], -round(c["elevation_m"], 3), -round(c["prominence_m"], 3),
            round(c["x"], 3), round(c["y"], 3)
        ))

        selected = []
        for row in candidates:
            px, py = row["x"], row["y"]
            if normalized_filters:
                passed, results = self._eval_constraints(normalized_filters, px, py)
                row["constraint_results"] = results
                row["all_hard_passed"] = passed
                if not passed:
                    continue
            else:
                row.setdefault("constraint_results", [])
                row.setdefault("all_hard_passed", True)
            too_close = False
            for keep in selected:
                if math.hypot(px - keep["x"], py - keep["y"]) < min_sep:
                    too_close = True
                    break
            if too_close:
                continue
            selected.append(row)
            if len(selected) >= count:
                break

        ranked = []
        evidence_calls = []
        for idx, row in enumerate(selected, start=1):
            position = self.georef.echo(row["x"], row["y"])
            rec = {
                "rank": idx,
                "x": position["x"], "y": position["y"],
                "lat": position["lat"], "lon": position["lon"],
                "score": round(row["score"], 4),
                "constraint_results": row.get("constraint_results", []),
                "constraint_report": {
                    "all_hard_passed": bool(row.get("all_hard_passed", True)),
                    "failed": [r for r in row.get("constraint_results", []) if not r.get("passed")],
                },
                "evidence": {
                    "elevation_m": round(row["elevation_m"], 2),
                    "prominence_m": round(row["prominence_m"], 2),
                    "slope_deg": None if row["slope_deg"] is None else round(row["slope_deg"], 3),
                    "openness": round(row["openness"], 4),
                    "roughness_m": None if row["roughness"] is None else round(row["roughness"], 3),
                    "hydrology": {
                        "wetness": round(row["wetness"], 4),
                        "seep": round(row["seep"], 4),
                        "flow": round(row["flow"], 4),
                        "ponding": round(row["ponding"], 4),
                    },
                    "soil_drainage_score": round(row["soil_drainage"], 4),
                    "landcover_scores": {
                        "forest_cover": round(row["forest_cover"], 4),
                        "open_cover": round(row["open_cover"], 4),
                        "wetland_cover": round(row["wetland_cover"], 4),
                    },
                    "normalized_scoring_features": {k: round(v, 4) for k, v in row.get("features", {}).items()},
                    "nlcd_name": row.get("nlcd_name"),
                    "landfire_community": row.get("landfire_name"),
                    "peak_refinement_m": round(row.get("peak_refinement_m", 0.0), 3),
                },
                "provenance": {
                    "tool": "recommend_sites",
                    "objective": obj,
                    "scoring": {
                        "weights": weights,
                        "elevation_normalized_range": [round(elev_min, 3), round(elev_max, 3)],
                        "prominence_range": [round(prom_min, 3), round(prom_max, 3)],
                        "feature_inputs": ["DEM/terrain derivatives", "hydrology grids",
                                           "SSURGO soil drainage",
                                           "NLCD/LANDFIRE land-cover classes",
                                           "objective weights and NMS spacing"],
                    },
                    "notes": [
                        "site ranking uses general terrain, hydrology, soil, and land-cover features with objective-specific weights plus NMS spacing.",
                        f"lattice_strategy={lattice_strategy}",
                        f"sample_lattice_step_m={round(spacing, 2)}",
                        f"requested_min_separation_m={round(min_sep, 2)}",
                    ],
                },
            }
            try:
                detail = self.identify_at({"x": row["x"], "y": row["y"]})
                soil = next((r for r in detail.get("atlas", [])
                             if r.get("layer_id") == "gssurgo_soils"), None)
                if soil:
                    rec["evidence"]["soil_name"] = soil.get("properties", {}).get("soil_name")
                landfire = next((r for r in detail.get("atlas", [])
                                if r.get("layer_id") == "landfire_evt_2024"), None)
                if landfire:
                    rec["evidence"]["landfire_community"] = landfire.get("name")
                nlcd = next((r for r in detail.get("atlas", [])
                             if r.get("layer_id") == "nlcd_2019_landcover"), None)
                if nlcd:
                    rec["evidence"]["nlcd_name"] = nlcd.get("name")
                rec["evidence"]["entity_count_here"] = len(detail.get("entities_here", []))
            except TwinQueryError:
                # Non-critical: ranking still useful without atlas evidence.
                pass
            if draw:
                drawn = self.draw_point({"x": row["x"], "y": row["y"]},
                                       label=f"{label_prefix} #{idx}")
                rec["drawn"] = drawn.get("drawn", drawn)
                evidence_calls.append(drawn)
            ranked.append(rec)

        return {
            "objective": obj,
            "purpose": obj,
            "raw_intent": raw_intent,
            "interpretation": interpretation,
            "applied_filters": applied_filters,
            "unresolved_terms": unresolved_terms,
            "region": reg.describe(),
            "requested_count": count,
            "returned_count": len(ranked),
            "step_m": round(spacing, 3),
            "provenance": {
                "tool": "recommend_sites",
                "candidates_considered": len(candidates),
                "seed_grid_step_m": round(spacing, 3),
                "lattice_strategy": lattice_strategy,
                "draw": draw,
                "min_separation_m": round(min_sep, 3),
                "rejected_by_filter": rejected_by_filter,
                "final_validation": bool(normalized_filters),
            },
            "candidates": ranked,
            "draw_count": len(evidence_calls) if draw else 0,
        }

    # -- point identify --------------------------------------------------------

    def identify_at(self, point):
        """Everything true at one point, across all atlas + entity layers —
        the server-side port of the viewer's click-to-identify."""
        x, y = resolve_point(point, self.georef)
        minx, miny, maxx, maxy = self._extent()
        echo = self.georef.echo(x, y)
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return {
                "point": echo,
                "outside_extent": True,
                "message": "point is outside the twin extent — no data here",
                "extent_scene_m": [round(v, 1) for v in (minx, miny, maxx, maxy)],
                "extent_corners": {
                    "southwest": self.georef.echo(minx, miny),
                    "northeast": self.georef.echo(maxx, maxy)},
            }

        results = []
        for layer in self._atlas_layers():
            data = self._layer_data(layer)
            if layer["type"] == "raster":
                grid = data["grid"]
                s = sample_grid(grid, layer["bounds_local"], x, y)
                if s is None or s[2] is None or s[2] == grid.get("nodata"):
                    continue
                results.append({
                    "layer_id": layer["id"], "layer_label": layer["label"],
                    "value": s[2],
                    "name": self._raster_value_name(grid, layer, s[2]),
                    "provenance": self._layer_provenance(layer),
                })
                continue
            for f in data.get("features", []):
                g = f.get("geometry")
                if not g:
                    continue
                if layer["type"] == "polygon":
                    hit = point_in_rings(polygon_rings(g), x, y)
                else:
                    hit = dist_to_paths(line_paths(g), x, y) < LINE_HIT_DISTANCE_M
                if hit:
                    props = {k: v for k, v in (f.get("properties") or {}).items()
                             if k not in HIDE_PROPS and v not in (None, "", " ")}
                    results.append({
                        "layer_id": layer["id"], "layer_label": layer["label"],
                        "name": (f.get("properties") or {}).get("__label") or layer["label"],
                        "properties": props,
                        "provenance": self._layer_provenance(layer),
                    })

        species = None
        if self._species_grids():
            names = self._gap_species_at(x, y)
            if names is not None:
                gap_row = self._layers_table().get("gap_species_richness", {})
                species = {"count": len(names), "common_names": names,
                           "provenance": {k: gap_row.get(k) for k in
                                          ("layer_id", "acquisition", "service")}}

        containing = []
        for kind in ("parcel", "building"):
            table = VECTOR_KINDS[kind]
            for eid, blob in self.conn.execute(
                    f"SELECT entity_id, geom FROM {table}"):
                gj = parse_gpkg_geometry(blob)
                if gj and gj["type"].endswith("Polygon") \
                        and point_in_rings(polygon_rings(gj), x, y):
                    containing.append({
                        "entity_id": eid, "kind": kind,
                        "attrs": self._attrs_with_provenance(kind, eid)})

        survey = self._survey_hits(x, y)

        elevation = None
        for grid in self._terrain_grids():
            elevation = sample_terrain_elevation(grid, x, y)
            if elevation is not None:
                break

        return {
            "point": echo,
            "elevation_m": round(elevation, 2) if elevation is not None else None,
            "atlas": results,
            "species_habitat": species,
            "survey": survey,
            "entities_here": containing,
        }

    def _survey_hits(self, x, y):
        """Survey-companion features at a point (docs/survey.md): polygons by
        containment, lines within 8 m, points within 8 m — the click-to-identify
        coverage atlas layers get, now extended to field uploads (photo and
        status included). [] when nothing has been surveyed here."""
        hits = []
        for layer in self._survey_catalog():
            for f in self._survey_features(layer):
                g = f.get("geometry") or {}
                gtype = g.get("type", "")
                if gtype.endswith("Polygon"):
                    hit = point_in_rings(polygon_rings(g), x, y)
                elif "Line" in gtype:
                    hit = dist_to_paths(line_paths(g), x, y) < LINE_HIT_DISTANCE_M
                elif gtype in ("Point", "MultiPoint"):
                    coords = [g["coordinates"]] if gtype == "Point" else g["coordinates"]
                    hit = any(math.hypot(c[0] - x, c[1] - y) < LINE_HIT_DISTANCE_M
                              for c in coords)
                else:
                    hit = False
                if hit:
                    props = {k: v for k, v in (f.get("properties") or {}).items()
                             if k not in HIDE_PROPS and v not in (None, "", " ")}
                    hits.append({
                        "kind": layer["id"], "layer_label": layer.get("label"),
                        "name": (f.get("properties") or {}).get("__label")
                        or layer.get("label"),
                        "properties": props,
                        "provenance": {"acquisition": layer.get("acquisition",
                                                                 "qfield_survey")},
                    })
        return hits

    def sample_raster(self, layer_id, point):
        """One raster layer's value + legend entry at a point."""
        layer = self._atlas_catalog().get(layer_id)
        rasters = [l["id"] for l in self._atlas_layers() if l["type"] == "raster"]
        if not layer or layer.get("type") != "raster":
            raise TwinQueryError(f"unknown raster layer: {layer_id!r}",
                                 valid_raster_layers=rasters)
        x, y = resolve_point(point, self.georef)
        grid = self._layer_data(layer)["grid"]
        s = sample_grid(grid, layer["bounds_local"], x, y)
        echo = self.georef.echo(x, y)
        if s is None:
            return {"layer_id": layer_id, "point": echo, "value": None,
                    "message": "point is outside this layer's bounds",
                    "bounds_scene_m": layer["bounds_local"]}
        legend = (grid.get("legend") or {}).get(str(s[2]))
        return {
            "layer_id": layer_id, "layer_label": layer["label"],
            "point": echo,
            "value": s[2],
            "name": legend["name"] if legend else self._raster_value_name(grid, layer, s[2]),
            "nodata": s[2] == grid.get("nodata"),
            "provenance": self._layer_provenance(layer),
        }

    # -- catalog ---------------------------------------------------------------

    def list_layers(self, kind=None):
        """The layer catalog (atlas layers and registered inputs) with
        acquisition provenance. Layers with status 'empty' legitimately have
        no features on this parcel. Entries include any natural-language
        description/metadata present plus compact field/legend previews so
        agents can choose from what actually exists."""
        rows = sorted(self._layers_table().values(), key=lambda r: r["layer_id"])
        valid = sorted({r["kind"] for r in rows if r["kind"]})
        if kind is not None:
            if kind not in valid:
                raise TwinQueryError(f"unknown layer kind: {kind!r}", valid_kinds=valid)
            rows = [r for r in rows if r["kind"] == kind]
        queryable = {l["id"]: l["type"] for l in self._atlas_layers()}
        catalog = self._atlas_catalog()
        manifest = self._atlas_manifest()
        out = []
        for r in rows:
            entry = dict(r)
            entry.pop("content_sha1", None)
            layer = catalog.get(r["layer_id"])
            manifest_row = manifest.get(r["layer_id"], {})
            if layer:
                entry["label"] = layer.get("label") or entry.get("label")
                entry["viewer_type"] = layer.get("type")
                entry["drapeable"] = bool(layer.get("image") or layer.get("file"))
                entry["themes"] = self._layer_themes({**entry, **layer})
                text_meta = self._layer_text_metadata(manifest_row, layer, entry)
                if text_meta:
                    entry["text_metadata"] = text_meta
                preview = self._layer_preview(layer)
                if preview:
                    entry["preview"] = preview
            else:
                entry["themes"] = self._layer_themes(entry)
                text_meta = self._layer_text_metadata(manifest_row, entry)
                if text_meta:
                    entry["text_metadata"] = text_meta
            if r["layer_id"] in queryable:
                entry["queryable_as"] = queryable[r["layer_id"]]
                entry["filterable"] = True
                entry["summarizable"] = True
            if manifest_row:
                entry["manifest"] = {k: manifest_row.get(k) for k in
                                     ("source", "layer", "kind", "status")
                                     if manifest_row.get(k) not in (None, "")}
            out.append(entry)
        return {"count": len(out), "kinds": valid, "layers": out}

    def layer_summary(self, layer_id):
        """One layer in depth: fields and labels for vectors, the legend and
        per-class cell breakdown for categorical rasters."""
        table_row = self._layers_table().get(layer_id)
        layer = self._atlas_catalog().get(layer_id)
        if table_row is None and layer is None:
            raise TwinQueryError(f"unknown layer_id: {layer_id!r}",
                                 valid_layer_ids=sorted(self._layers_table().keys()))
        manifest_row = self._atlas_manifest().get(layer_id, {})
        out = {"layer_id": layer_id, "provenance": table_row or self._layer_provenance(layer)}
        text_meta = self._layer_text_metadata(manifest_row, layer or {}, table_row or {})
        if text_meta:
            out["text_metadata"] = text_meta
        if manifest_row:
            out["manifest"] = {k: manifest_row.get(k) for k in
                               ("source", "layer", "kind", "status")
                               if manifest_row.get(k) not in (None, "")}
        if layer is None:
            out["note"] = ("registered in the store but not viewer-queryable "
                           "(input file, imagery, or empty for this parcel)")
            return out
        out["type"] = layer["type"]
        out["label"] = layer.get("label")
        out["themes"] = self._layer_themes({**(table_row or {}), **layer})
        data = self._layer_data(layer)
        if layer["type"] == "raster":
            grid = data["grid"]
            values = []
            counts = {}
            for row in grid["values"]:
                for v in row:
                    if v is not None and v != grid.get("nodata"):
                        values.append(v)
                        counts[v] = counts.get(v, 0) + 1
            b = layer["bounds_local"]
            out.update({
                "width": grid["width"], "height": grid["height"],
                "bounds_scene_m": b,
                "bounds_corners": {"southwest": self.georef.echo(b[0], b[1]),
                                   "northeast": self.georef.echo(b[2], b[3])},
            })
            for key in ("description", "uses", "value_kind", "value_unit", "value_classification"):
                v = grid.get(key) or layer.get(key)
                if v not in (None, ""):
                    out[key] = v
            if (grid.get("value_classification") or layer.get("value_classification")) == "continuous":
                if values:
                    out["value_stats"] = {
                        "min": min(values),
                        "max": max(values),
                        "mean": round(sum(values) / len(values), 3),
                        "cells": len(values),
                    }
                out["legend"] = grid.get("legend") or {}
            else:
                total = sum(counts.values()) or 1
                classes = [{
                    "value": v,
                    "name": self._raster_value_name(grid, layer, v),
                    "cells": n,
                    "share": round(n / total, 4),
                } for v, n in sorted(counts.items(), key=lambda kv: -kv[1])]
                out["classes"] = classes
            # the GAP richness grid carries per-species habitat masks: list the
            # species so the agent knows what filter_layer(..., field="species")
            # can reveal.
            sg = self._species_grids() if layer_id == GAP_SPECIES_LAYER else None
            if sg:
                out["filterable_species"] = sorted(
                    {s.get("common_name") for s in sg["species"].values()
                     if s.get("common_name")})
        else:
            features = data.get("features", [])
            geom_types = {}
            prop_keys = set()
            labels = []
            for f in features:
                g = f.get("geometry") or {}
                geom_types[g.get("type")] = geom_types.get(g.get("type"), 0) + 1
                props = f.get("properties") or {}
                prop_keys.update(k for k in props if k not in HIDE_PROPS)
                lbl = props.get("__label")
                if lbl and lbl not in labels:
                    labels.append(lbl)
            out.update({
                "feature_count": len(features),
                "geometry_types": geom_types,
                "attribute_fields": sorted(prop_keys),
                "labels": labels[:25],
            })
        return out

    # -- region summary -----------------------------------------------------------

    def _region_samples(self, reg, target=3000):
        """Evenly spaced sample points inside a region (used to estimate
        raster class shares and polygon-layer overlap)."""
        minx, miny, maxx, maxy = reg.bounds
        w, h = maxx - minx, maxy - miny
        step = max(1.0, math.sqrt(max(w * h, 1.0) / target))
        pts = []
        ny = max(1, int(h / step))
        nx = max(1, int(w / step))
        for iy in range(ny):
            yv = miny + (iy + 0.5) * (h / ny)
            for ix in range(nx):
                xv = minx + (ix + 0.5) * (w / nx)
                if reg.contains(xv, yv):
                    pts.append((xv, yv))
        if not pts:
            cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
            if reg.contains(cx, cy):
                pts = [(cx, cy)]
        return pts, step

    def _raster_breakdown(self, layer_id, samples):
        layer = self._atlas_catalog().get(layer_id)
        if not layer or layer.get("type") != "raster":
            return None
        grid = self._layer_data(layer)["grid"]
        counts = {}
        values = []
        hit = 0
        for x, y in samples:
            s = sample_grid(grid, layer["bounds_local"], x, y)
            if s is None or s[2] is None or s[2] == grid.get("nodata"):
                continue
            hit += 1
            values.append(s[2])
            counts[s[2]] = counts.get(s[2], 0) + 1
        if not hit:
            return None
        if (grid.get("value_classification") or layer.get("value_classification")) == "continuous":
            return {
                "value_stats": {
                    "min": min(values),
                    "max": max(values),
                    "mean": round(sum(values) / len(values), 3),
                    "samples": len(values),
                },
                "value_kind": grid.get("value_kind") or layer.get("value_kind"),
                "value_unit": grid.get("value_unit") or layer.get("value_unit"),
                "provenance": self._layer_provenance(layer),
            }
        classes = [{
            "value": v,
            "name": self._raster_value_name(grid, layer, v),
            "share": round(n / hit, 4),
        } for v, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        return {"classes": classes, "dominant": classes[0],
                "provenance": self._layer_provenance(layer)}

    def _polygon_overlap(self, layer_id, samples, name_props=("__label",)):
        """Which features of a polygon atlas layer cover the region, with the
        sampled share of the region they cover."""
        layer = self._atlas_catalog().get(layer_id)
        if not layer or layer.get("type") not in ("polygon", "line"):
            return None
        data = self._layer_data(layer)
        found = {}
        for f in data.get("features", []):
            rings = polygon_rings(f.get("geometry"))
            if not rings:
                continue
            inside = sum(1 for x, y in samples if point_in_rings(rings, x, y))
            if not inside:
                continue
            props = f.get("properties") or {}
            name = next((props[p] for p in name_props if props.get(p)), layer["label"])
            entry = found.setdefault(name, {"name": name, "samples_inside": 0,
                                            "properties": {k: v for k, v in props.items()
                                                           if k not in HIDE_PROPS}})
            entry["samples_inside"] += inside
        if not found:
            return None
        total = len(samples) or 1
        features = sorted(found.values(), key=lambda e: -e["samples_inside"])
        for e in features:
            e["share_of_region"] = round(e["samples_inside"] / total, 4)
            del e["samples_inside"]
        return {"features": features, "provenance": self._layer_provenance(layer)}

    def summarize_region(self, region):
        """The headline call: everything happening inside a region, with
        provenance per fact — shaped for an LLM to narrate directly."""
        reg = self._resolve_region(region)
        if reg is None:
            raise TwinQueryError(
                "summarize_region needs a region "
                '({"aoi":true} | {"bbox":[...]} | {"within_m":r,"point":{...}} | {"polygon":[...]})')
        samples, spacing = self._region_samples(reg)
        if not samples:
            return {"region": reg.describe(),
                    "message": "region contains no sampleable area inside the twin extent"}

        runs = self._runs_by_id()

        def veg_stats(kind):
            positions = self._positions(kind)
            alive = self._alive_ids(kind)
            latest = self._latest_full(kind)
            bx0, by0, bx1, by1 = reg.bounds
            stats = {"count": 0}
            heights, crown, types, species, sources, run_ids = [], 0.0, {}, {}, {}, set()
            for eid, (x, y) in positions.items():
                if eid not in alive or not (bx0 <= x <= bx1 and by0 <= y <= by1):
                    continue
                if not reg.contains(x, y):
                    continue
                stats["count"] += 1
                attrs = latest.get(eid, {})
                for name, bucket in (("type", types), ("species", species),
                                     ("source", sources)):
                    if name in attrs:
                        v = twin_store.decode_value(attrs[name][0])
                        bucket[v] = bucket.get(v, 0) + 1
                if "height" in attrs:
                    heights.append(float(twin_store.decode_value(attrs["height"][0])))
                if "radius" in attrs:
                    r = float(twin_store.decode_value(attrs["radius"][0]))
                    crown += math.pi * r * r
                for rec in attrs.values():
                    run_ids.add(rec[2])
            if heights:
                stats["mean_height_m"] = round(sum(heights) / len(heights), 2)
                stats["max_height_m"] = round(max(heights), 2)
            if crown:
                stats["crown_area_m2"] = round(crown, 1)
            if types:
                stats["type_split"] = types
            if species:
                stats["top_species"] = dict(sorted(species.items(),
                                                   key=lambda kv: -kv[1])[:8])
            if sources:
                stats["sources"] = sources
            if run_ids:
                stats["provenance"] = {
                    "store": "latest observations per entity",
                    "runs": sorted({runs[r]["script"] for r in run_ids if r in runs}),
                }
            return stats

        entity_counts = {}
        for kind in self.kinds():
            positions = self._positions(kind)
            alive = self._alive_ids(kind)
            bx0, by0, bx1, by1 = reg.bounds
            n = sum(1 for eid, (x, y) in positions.items()
                    if eid in alive and bx0 <= x <= bx1 and by0 <= y <= by1
                    and reg.contains(x, y))
            if n:
                entity_counts[kind] = n

        richness = None
        layer = self._atlas_catalog().get("gap_species_richness")
        if layer:
            grid = self._layer_data(layer)["grid"]
            vals = []
            for x, y in samples:
                s = sample_grid(grid, layer["bounds_local"], x, y)
                if s and s[2] is not None:
                    vals.append(s[2])
            if vals:
                richness = {"min": min(vals), "max": max(vals),
                            "mean": round(sum(vals) / len(vals), 1),
                            "provenance": self._layer_provenance(layer)}

        # parcel entities live in the store, not the atlas: report which
        # parcel polygons cover the region's samples (subsampled — coverage,
        # not share, is the question here).
        parcel_hits = {}
        for eid, blob in self.conn.execute("SELECT entity_id, geom FROM parcels"):
            gj = parse_gpkg_geometry(blob)
            rings = polygon_rings(gj) if gj else []
            if rings and any(point_in_rings(rings, x, y) for x, y in samples[::7] or samples):
                props = self._attrs_with_provenance("parcel", eid).get("properties", {})
                p = props.get("value") or {}
                parcel_hits[eid] = {"entity_id": eid, "owner": p.get("owner"),
                                    "parcel_address": p.get("parcel_address"),
                                    "calc_acres": p.get("calc_acres")}

        return {
            "region": reg.describe(),
            "sampling": {"points": len(samples), "spacing_m": round(spacing, 1)},
            "entity_counts": entity_counts,
            "trees": veg_stats("tree"),
            "shrubs": {"count": entity_counts.get("shrub", 0)},
            "parcels": list(parcel_hits.values()),
            "landfire_community": self._raster_breakdown("landfire_evt_2024", samples),
            "nlcd_landcover": self._raster_breakdown("nlcd_2019_landcover", samples),
            "soils": self._polygon_overlap("gssurgo_soils", samples,
                                           name_props=("soil_name", "__label")),
            "wetlands": (self._polygon_overlap("nwi_wetlands_uh", samples,
                                               name_props=("USGS_NAME", "__label"))
                         or self._polygon_overlap(
                             "dec_informational_freshwater_wetlands", samples)),
            "protected_species_areas": self._polygon_overlap(
                "dec_rare_plants_animals", samples),
            "gap_species_richness": richness,
        }

    # -- aggregates / temporal ------------------------------------------------------

    def aggregate_entities(self, kind, metric, group_by=None, where=None, region=None):
        """Aggregate latest-state values over entities of one kind.
        metric: "count", "crown_area" (sum of pi*radius^2), or
        "<sum|mean|min|max>:<numeric attr>" e.g. "mean:height"."""
        self._require_kind(kind)
        reg = self._resolve_region(region)
        filters = self._parse_filters(where)

        m = re.match(r"^(count|crown_area|(sum|mean|min|max):([A-Za-z_]\w*))$",
                     str(metric))
        if not m:
            raise TwinQueryError(
                'metric must be "count", "crown_area", or "<sum|mean|min|max>:<attr>"',
                got=metric)
        agg, attr = (m.group(2), m.group(3)) if m.group(2) else (m.group(1), None)
        if agg == "crown_area":
            agg, attr = "crown_area", "radius"

        positions = self._positions(kind)
        alive = self._alive_ids(kind)
        latest = self._latest_full(kind)
        runs = self._runs_by_id()

        groups = {}
        for eid, (x, y) in positions.items():
            if eid not in alive:
                continue
            if reg is not None:
                bx0, by0, bx1, by1 = reg.bounds
                if not (bx0 <= x <= bx1 and by0 <= y <= by1) or not reg.contains(x, y):
                    continue
            attrs = latest.get(eid, {})
            if filters:
                skip = False
                for fattr, op, expected in filters:
                    actual = (twin_store.decode_value(attrs[fattr][0])
                              if fattr in attrs else None)
                    if not self._filter_match(actual, op, expected):
                        skip = True
                        break
                if skip:
                    continue
            key = "all"
            if group_by:
                key = (twin_store.decode_value(attrs[group_by][0])
                       if group_by in attrs else None)
            g = groups.setdefault(key, {"n": 0, "values": [], "sources": {},
                                        "run_ids": set()})
            g["n"] += 1
            if attr and attr in attrs:
                try:
                    g["values"].append(float(twin_store.decode_value(attrs[attr][0])))
                except (TypeError, ValueError):
                    pass
                g["run_ids"].add(attrs[attr][2])
                src = attrs[attr][3]
                g["sources"][src] = g["sources"].get(src, 0) + 1
            elif "source" in attrs:
                src = twin_store.decode_value(attrs["source"][0])
                g["sources"][src] = g["sources"].get(src, 0) + 1

        def finish(g):
            vals = g["values"]
            if agg == "count":
                value = g["n"]
            elif agg == "crown_area":
                value = round(sum(math.pi * v * v for v in vals), 1)
            elif not vals:
                value = None
            elif agg == "sum":
                value = round(sum(vals), 3)
            elif agg == "mean":
                value = round(sum(vals) / len(vals), 3)
            elif agg == "min":
                value = round(min(vals), 3)
            else:
                value = round(max(vals), 3)
            out = {"value": value, "entity_count": g["n"]}
            prov = {"sources": g["sources"]} if g["sources"] else {}
            if g["run_ids"]:
                prov["runs"] = sorted({runs[r]["script"] for r in g["run_ids"]
                                       if r in runs})
            if prov:
                out["provenance"] = prov
            return out

        return {
            "kind": kind, "metric": metric, "group_by": group_by,
            "where": where, "region": reg.describe() if reg else None,
            "groups": {str(k): finish(g) for k, g in
                       sorted(groups.items(), key=lambda kv: -kv[1]["n"])},
        }

    def canopy_change(self, region=None, member="member_parcel"):
        """Tree count + summed crown area as of each pipeline run, in time
        order — "when did canopy density change here". member: which
        population ('member_parcel', 'member_surrounding', or 'any')."""
        if member not in ("member_parcel", "member_surrounding", "any"):
            raise TwinQueryError("member must be member_parcel, member_surrounding, or any",
                                 got=member)
        reg = self._resolve_region(region)
        params = {"minx": -1e9, "miny": -1e9, "maxx": 1e9, "maxy": 1e9}
        id_join = ""
        if reg is not None:
            bx0, by0, bx1, by1 = reg.bounds
            params.update(minx=bx0, miny=by0, maxx=bx1, maxy=by1)
            if reg.shape != "bbox":
                # exact predicate -> temp table of candidate ids (no N+1)
                cand = [eid for eid, (x, y) in self._positions("tree").items()
                        if bx0 <= x <= bx1 and by0 <= y <= by1 and reg.contains(x, y)]
                self.conn.execute("DROP TABLE IF EXISTS temp.region_trees")
                self.conn.execute("CREATE TEMP TABLE region_trees (entity_id TEXT PRIMARY KEY)")
                self.conn.executemany("INSERT INTO temp.region_trees VALUES (?)",
                                      [(c,) for c in cand])
                id_join = "JOIN temp.region_trees ri ON ri.entity_id = e.entity_id"

        def member_subselect(attr):
            return (f"(SELECT o.value FROM observations o"
                    f" WHERE o.entity_id = e.entity_id AND o.attr = '{attr}'"
                    f" AND o.run_id <= r.run_id ORDER BY o.obs_id DESC LIMIT 1)")

        if member == "any":
            member_cols = (f"{member_subselect('member_parcel')} AS mp,"
                           f" {member_subselect('member_surrounding')} AS ms")
            member_where = "(s.mp = 'true' OR s.ms = 'true')"
        else:
            member_cols = f"{member_subselect(member)} AS mp"
            member_where = "s.mp = 'true'"

        sql = f"""
        WITH runs AS (SELECT run_id, script, started_at FROM pipeline_runs),
        state AS (
          SELECT r.run_id, e.entity_id, {member_cols},
            (SELECT CAST(o.value AS REAL) FROM observations o
              WHERE o.entity_id = e.entity_id AND o.attr = 'radius'
                AND o.run_id <= r.run_id
              ORDER BY o.obs_id DESC LIMIT 1) AS radius
          FROM runs r
          JOIN entities e ON e.kind = 'tree'
            AND e.created_run_id <= r.run_id
            AND (e.retired_run_id IS NULL OR e.retired_run_id > r.run_id)
          JOIN trees t ON t.entity_id = e.entity_id
          {id_join}
          WHERE t.x BETWEEN :minx AND :maxx AND t.y BETWEEN :miny AND :maxy
        )
        SELECT r.run_id, r.script, r.started_at,
               COUNT(*) AS tree_count,
               CAST(ROUND(SUM(3.14159265 * radius * radius), 0) AS INTEGER)
        FROM state s JOIN runs r USING (run_id)
        WHERE {member_where}
        GROUP BY r.run_id
        ORDER BY r.started_at
        """
        rows = []
        prev = None
        for run_id, script, started, count, area in self.conn.execute(sql, params):
            rows.append({
                "run_id": run_id, "script": script, "started_at": started,
                "tree_count": count, "crown_area_m2": area,
                "tree_delta": None if prev is None else count - prev,
            })
            prev = count
        return {
            "member": member,
            "region": reg.describe() if reg else None,
            "runs": rows,
            "provenance": {
                "store": "per-run liveness over entities + latest-attr-as-of-run "
                         "over observations, joined to pipeline_runs "
                         "(same query shape as scripts/canopy_density.py)"},
        }

    # -- survey companion query surface (docs/survey.md) ----------------------

    def list_survey_layers(self):
        """The field-survey catalog: one entry per uploaded QField layer
        (trails, stream_centerlines, photo_points, observations), each with
        its store kind (survey_<layer> — queryable via find_entities /
        summarize_region / aggregate_entities / identify_at), geometry type,
        live feature count, the attribute fields present, and whether any
        feature carries a photo. Empty list (with a note) when no survey has
        been uploaded yet."""
        layers = []
        for layer in self._survey_catalog():
            feats = self._survey_features(layer)
            fields = sorted({k for f in feats
                             for k in (f.get("properties") or {})
                             if k not in HIDE_PROPS and k != "__label"})
            layers.append({
                "kind": layer["id"],
                "label": layer.get("label"),
                "geometry_type": layer.get("type"),
                "feature_count": layer.get("feature_count", len(feats)),
                "fields": fields,
                "has_photos": any((f.get("properties") or {}).get("photo")
                                  for f in feats),
                "acquisition": layer.get("acquisition", "qfield_survey"),
            })
        out = {"count": len(layers), "layers": layers}
        if not layers:
            out["note"] = ("no field surveys uploaded yet — the Survey companion "
                           "write path (docs/survey.md) is empty for this twin")
        return out

    # -- hydrology simulation (the Simulation window) -------------------------

    def hydrology_at(self, point):
        """The terrain-hydrology read at one point — the server-side voice of
        the Simulation window's click-to-identify. Samples every derived layer
        (upslope contributing area, TWI wetness percentile, ponding depth, the
        spring/seep score, and the live scenario's runoff, absorption, soil
        storage, percolation, local saturation excess, profile saturation,
        arriving runon, retained pond water, and surface throughflow if a
        scenario has been run), reports the SSURGO soil at the point, and
        synthesizes the same plain-language reading the viewer shows. Raises
        if `npm run analyze-hydrology` hasn't produced the layers yet."""
        cat = self._hydro_catalog()
        if not cat:
            raise TwinQueryError(
                "no hydrology layers — run `npm run analyze-hydrology` first",
                path=HYDRO_SIM_CATALOG)
        x, y = resolve_point(point, self.georef)
        echo = self.georef.echo(x, y)
        layers = {}
        any_value = False
        for layer in cat:
            grid = self._hydro_grid(layer)
            s = sample_grid(grid, layer["bounds_local"], x, y)
            v = s[2] if s else None
            if v is not None and v == grid.get("nodata"):
                v = None
            if v is not None:
                any_value = True
            layers[layer["id"]] = {
                "value": round(v, 3) if isinstance(v, (int, float)) else v,
                "label": layer.get("label"),
                "group": layer.get("group"),
                "description": layer.get("description"),
                "value_kind": grid.get("value_kind") or layer.get("value_kind"),
                "value_unit": grid.get("value_unit") or layer.get("value_unit"),
                "cell_area_m2": grid.get("cell_area_m2") or layer.get("cell_area_m2"),
            }
        if not any_value:
            return {"point": echo, "soil": self._soil_at(x, y), "layers": layers,
                    "summary": ["No hydrology data at this point — it is outside "
                                "the analyzed terrain footprint."],
                    "provenance": self._hydro_provenance()}
        soil = self._soil_at(x, y)
        return {
            "point": echo,
            "soil": soil,
            "layers": layers,
            "summary": self._hydrology_sentences(layers, soil),
            "provenance": self._hydro_provenance(),
        }

    def hydrology_summary(self):
        """The headline hydrology read for the whole property: the Tier-1
        analysis summary (drainage outlet, depression/pond storage, hydrologic
        soil-group fractions, soil map units, the top spring/seep candidates
        with lat/lon, and the stream/wetland validation) plus the last scenario
        that was run (water input, terminal surface water, infiltrated water,
        profile water gain, percolation, local saturation excess, outlet flow,
        finite depression retention, and the closed event water budget with its
        uncertainty band, ponding). Raises until
        `npm run analyze-hydrology` has run."""
        summ = self._read_json(HYDRO_SUMMARY)
        if not summ:
            raise TwinQueryError(
                "no hydrology summary — run `npm run analyze-hydrology` first",
                path=HYDRO_SUMMARY)
        return {
            "summary": summ,
            "last_scenario": self._read_json(HYDRO_LAST_SCENARIO),
            "provenance": self._hydro_provenance(),
        }

    def run_scenario(self, mode="snowmelt", swe_in=None, preset=None,
                     melt_days=None, rain_in=None, storm_hours=None,
                     antecedent=None, frozen=False, as_of=None, dry_run=False):
        """Run a snowmelt or rainstorm scenario (scripts/hydro_scenario.py) and
        return the result. This WRITES: it rewrites the viewer's scenario drape
        layers and records a `scenario` pipeline run in the store (history stays
        queryable). Parameters are clamped exactly like the viewer's Simulation
        window. mode: "snowmelt" (swe_in inches 0-40 or preset
        median|p90|max; melt_days 0.5-30) or "rain" (storm_hours 0.5-240).
        rain_in: rain-on-snow / storm rain inches 0-15. antecedent:
        dry|normal|wet|auto. auto uses ET water-balance antecedent state when
        present. frozen: restricted frozen-ground screening state. dry_run
        returns the argv that would run, without executing."""
        if not os.path.isdir(HYDRO_DIR):
            raise TwinQueryError(
                "hydrology not initialized — run `npm run analyze-hydrology` first",
                path=HYDRO_DIR)
        argv = self._scenario_argv(mode, swe_in, preset, melt_days, rain_in,
                                   storm_hours, antecedent, frozen, as_of)
        if dry_run:
            return {"would_run": ["hydro_scenario.py"] + argv}
        import subprocess
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, "hydro_scenario.py")] + argv,
                cwd=PROJECT, capture_output=True, text=True, timeout=180,
                env={**os.environ, "TWIN_DATA_DIR": DATA})
        except subprocess.TimeoutExpired:
            raise TwinQueryError("scenario timed out after 180 s")
        if proc.returncode != 0:
            raise TwinQueryError("scenario run failed",
                                 detail=proc.stderr.strip()[-400:])
        lines = [ln for ln in proc.stdout.strip().split("\n") if ln]
        try:
            result = json.loads(lines[-1])  # JSON result is the last stdout line
        except (ValueError, IndexError):
            raise TwinQueryError("scenario produced no parseable result",
                                 stdout=proc.stdout[-400:])
        result["note"] = (
            "scenario written to the store and the viewer's local-surface-excess, "
            "infiltration, profile-water, percolation, saturation, runon, retained-"
            "pond-water, and surface-throughflow drapes; the Simulation window "
            "repaints on its "
            "next refresh (reload or re-toggle). Past scenarios stay queryable "
            "as pipeline runs.")
        return result

    @staticmethod
    def _scenario_argv(mode, swe_in, preset, melt_days, rain_in, storm_hours,
                       antecedent, frozen, as_of=None):
        """Validate + clamp scenario params into hydro_scenario.py argv, byte
        for byte the same ranges server.js applies to /api/simulate."""
        def clamp(v, lo, hi):
            return f"{min(hi, max(lo, float(v)))}"
        mode = "rain" if mode == "rain" else "snowmelt"
        argv = ["--json", "--mode", mode]
        if mode == "snowmelt":
            if isinstance(swe_in, (int, float)):
                argv += ["--swe-in", clamp(swe_in, 0, 40)]
            elif preset in ("median", "p90", "max"):
                argv += ["--preset", preset]
            if isinstance(melt_days, (int, float)):
                argv += ["--melt-days", clamp(melt_days, 0.5, 30)]
        elif isinstance(storm_hours, (int, float)):
            argv += ["--storm-hours", clamp(storm_hours, 0.5, 240)]
        if isinstance(rain_in, (int, float)):
            argv += ["--rain-in", clamp(rain_in, 0, 15)]
        if antecedent in ("dry", "normal", "wet", "auto"):
            argv += ["--antecedent", antecedent]
        if as_of:
            argv += ["--as-of", str(as_of)]
        if frozen is True:
            argv += ["--frozen"]
        return argv

    # -- wildfire simulation (the Fire pane) ---------------------------------

    def fire_at(self, point):
        """The wildfire read at one point: Tier-1 fuelscape plus latest
        scenario layers, with the same plain-language style as the viewer."""
        cat = self._fire_catalog()
        if not cat:
            raise TwinQueryError(
                "no fire layers — run `npm run analyze-fuels` first",
                path=FIRE_SIM_CATALOG)
        x, y = resolve_point(point, self.georef)
        echo = self.georef.echo(x, y)
        layers = {}
        sampled_values = {}
        any_value = False
        for layer in cat:
            grid = self._fire_grid(layer)
            s = sample_grid(grid, layer["bounds_local"], x, y)
            row = col = None
            value = None
            if s:
                row, col, value = s
                if value is not None and value == grid.get("nodata"):
                    value = None
            if value is not None:
                any_value = True
            shown = round(value, 3) if isinstance(value, (int, float)) else value
            legend = None
            if value is not None:
                key = str(int(round(value))) if isinstance(value, (int, float)) else str(value)
                legend = (grid.get("legend") or {}).get(key)
            layers[layer["id"]] = {
                "value": shown,
                "row": row,
                "col": col,
                "label": layer.get("label"),
                "group": layer.get("group"),
                "description": layer.get("description"),
                "value_kind": grid.get("value_kind") or layer.get("value_kind"),
                "value_unit": grid.get("value_unit") or layer.get("value_unit"),
                "cell_area_m2": grid.get("cell_area_m2") or layer.get("cell_area_m2"),
                "legend": legend,
                "acquisition": layer.get("acquisition"),
            }
            sampled_values[layer["id"]] = shown
        provenance = self._fire_provenance()
        if not any_value:
            return {
                "error": "no fire data at this point — it is outside the analyzed terrain footprint",
                "point": echo,
                "layers": layers,
                "sampled_values": sampled_values,
                "provenance": provenance,
            }
        last = self._read_json(FIRE_LAST_SCENARIO)
        return {
            "point": echo,
            "layers": layers,
            "sampled_values": sampled_values,
            "summary": self._fire_sentences(layers, last),
            "last_scenario": self._fire_last_scenario_brief(last),
            "provenance": provenance,
        }

    def fire_summary(self):
        """The headline wildfire read for the whole property."""
        summ = self._read_json(FIRE_SUMMARY)
        if not summ:
            raise TwinQueryError(
                "no fire summary — run `npm run analyze-fuels` first",
                path=FIRE_SUMMARY)
        last = self._read_json(FIRE_LAST_SCENARIO)
        return {
            "summary": summ,
            "fuel_model_breakdown": summ.get("fuel_model_breakdown"),
            "canopy_stats": summ.get("canopy_stats"),
            "crown_potential_fractions": summ.get("crown_potential_fractions"),
            "TI_baseline": summ.get("TI_baseline"),
            "CI_baseline": summ.get("CI_baseline"),
            "last_scenario": last,
            "provenance": self._fire_provenance(),
        }

    def run_fire_scenario(self, ignition_x, ignition_y, weather_class=None,
                          temp_f=None, rh_min=None, wind_mph=None,
                          wind_dir=None, days_since_rain=None, drought=None,
                          exposure=None, date=None, duration_min=None,
                          fmc_override=None, fuel_source=None, hydrology=None):
        """Run scripts/fire_scenario.py with the same clamping as the viewer."""
        if not os.path.isdir(FIRE_DIR):
            return {"error": "fire not initialized — run `npm run analyze-fuels` first",
                    "path": FIRE_DIR}
        try:
            argv = self._fire_scenario_argv(
                ignition_x, ignition_y, weather_class=weather_class,
                temp_f=temp_f, rh_min=rh_min, wind_mph=wind_mph,
                wind_dir=wind_dir, days_since_rain=days_since_rain,
                drought=drought, exposure=exposure, date=date,
                duration_min=duration_min, fmc_override=fmc_override,
                fuel_source=fuel_source, hydrology=hydrology)
        except TwinQueryError as e:
            return e.payload
        try:
            self.conn.commit()
        except Exception:
            pass
        import subprocess
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(PROJECT, "scripts", "fire_scenario.py"),
                 "--json"] + argv,
                cwd=PROJECT, env={**os.environ, "TWIN_DATA_DIR": DATA},
                timeout=180, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return {"error": "fire scenario timed out after 180 s"}
        if proc.returncode != 0:
            return {"error": "fire scenario run failed",
                    "detail": proc.stderr.strip()[-400:]}
        lines = [ln for ln in proc.stdout.strip().split("\n") if ln]
        try:
            result = json.loads(lines[-1])
        except (ValueError, IndexError):
            return {"error": "fire scenario produced no parseable result",
                    "stdout": proc.stdout[-400:]}
        self._caches = {}
        return result

    @staticmethod
    def _fire_scenario_argv(ignition_x, ignition_y, weather_class=None,
                            temp_f=None, rh_min=None, wind_mph=None,
                            wind_dir=None, days_since_rain=None, drought=None,
                            exposure=None, date=None, duration_min=None,
                            fmc_override=None, fuel_source=None, hydrology=None):
        """Validate + clamp fire scenario params into fire_scenario.py argv.

        wind_dir is the downwind / maximum-spread azimuth in degrees clockwise
        from north, not the meteorological wind-from bearing.
        """
        def num(v):
            return (float(v) if type(v) in (int, float) and math.isfinite(float(v))
                    else None)
        def js_number(v):
            v = float(v)
            if v == 0:
                return "0"
            if v.is_integer():
                return str(int(v))
            return repr(v)
        def clamp(v, lo, hi):
            return js_number(min(hi, max(lo, float(v))))

        ix = num(ignition_x)
        iy = num(ignition_y)
        if ix is None or iy is None:
            raise TwinQueryError("ignition_x and ignition_y must be finite numbers")
        try:
            with open(TERRAIN_GRID) as fh:
                grid = json.load(fh)
            bounds = {
                "minX": float(grid["minX"]),
                "maxX": float(grid["maxX"]),
                "minY": float(grid["minY"]),
                "maxY": float(grid["maxY"]),
            }
        except (OSError, ValueError, KeyError) as e:
            raise TwinQueryError(f"could not read terrain grid bounds: {e}")
        if (ix < bounds["minX"] or ix > bounds["maxX"]
                or iy < bounds["minY"] or iy > bounds["maxY"]):
            raise TwinQueryError(
                "ignition outside grid bounds "
                f"[{js_number(bounds['minX'])}, {js_number(bounds['maxX'])}] x "
                f"[{js_number(bounds['minY'])}, {js_number(bounds['maxY'])}]")
        argv = ["--ignition-x", js_number(ix), "--ignition-y", js_number(iy)]

        weather_classes = {
            "normal_spring", "high_spring", "extreme_redflag",
            "summer_drought", "dormant_fall", "custom",
        }
        droughts = {"normal", "dry", "severe", "extreme"}
        exposures = {"shaded", "mixed", "open"}
        fuel_sources = {"landfire", "computed"}
        hydrology_modes = {"on", "off"}
        if weather_class is not None:
            if weather_class not in weather_classes:
                raise TwinQueryError("invalid weather_class")
            argv += ["--weather-class", weather_class]
        if drought is not None:
            if drought not in droughts:
                raise TwinQueryError("invalid drought")
            argv += ["--drought", drought]
        if exposure is not None:
            if exposure not in exposures:
                raise TwinQueryError("invalid exposure")
            argv += ["--exposure", exposure]
        if fuel_source is not None:
            if fuel_source not in fuel_sources:
                raise TwinQueryError("invalid fuel_source")
            argv += ["--fuel-source", fuel_source]
        if hydrology is not None:
            if hydrology not in hydrology_modes:
                raise TwinQueryError("invalid hydrology")
            argv += ["--hydrology", hydrology]

        if TwinQuery._valid_iso_date(date):
            argv += ["--date", date]
        if num(wind_mph) is not None:
            argv += ["--wind-mph", clamp(wind_mph, 0, 120)]
        if num(wind_dir) is not None:
            argv += ["--wind-dir", js_number(((float(wind_dir) % 360) + 360) % 360)]
        if num(temp_f) is not None:
            argv += ["--temp-f", clamp(temp_f, -20, 130)]
        if num(rh_min) is not None:
            argv += ["--rh-min", clamp(rh_min, 1, 100)]
        if num(days_since_rain) is not None:
            argv += ["--days-since-rain", clamp(days_since_rain, 0, 120)]
        if num(duration_min) is not None:
            argv += ["--duration-min", clamp(duration_min, 1, 1440)]
        if num(fmc_override) is not None:
            argv += ["--fmc-override", clamp(fmc_override, 75, 140)]
        return argv

    @staticmethod
    def _valid_iso_date(value):
        if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return False
        import datetime
        try:
            dt = datetime.datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return False
        return dt.strftime("%Y-%m-%d") == value

    def _fire_provenance(self):
        last = self._read_json(FIRE_LAST_SCENARIO) or {}
        run = self._runs_by_id().get(last.get("run_id"))
        if run is None:
            run = max((r for r in self._runs_by_id().values()
                       if r.get("script") in ("analyze_fuels.py", "fire_scenario.py")),
                      key=lambda r: r.get("started_at") or "", default=None)
        layers = []
        for layer in self._fire_catalog():
            layers.append({
                "id": layer.get("id"),
                "group": layer.get("group"),
                "acquisition": layer.get("acquisition"),
                "grid": layer.get("grid"),
            })
        return {
            "source": "twin_fire.py (Rothermel surface + Van Wagner/Scott-Reinhardt crown screen)",
            "acquisition": "derived",
            "run_id": last.get("run_id") or (run.get("run_id") if run else None),
            "observed_at": (run.get("finished_at") or run.get("started_at")) if run else None,
            "scenario_file": FIRE_LAST_SCENARIO if last else None,
            "layers": layers,
            "caveat": "scenario-grade spread screen, not a forecast; wind, fuels, and moisture dominate uncertainty",
        }

    def _fire_sentences(self, layers, last):
        def rec(lid):
            return layers.get(lid) or {}
        def val(lid):
            v = rec(lid).get("value")
            return v if isinstance(v, (int, float)) else None
        def fmt_measure(value, unit):
            n = float(value)
            rounded = f"{n:.1f}" if abs(n) < 10 else f"{round(n):.0f}"
            return f"{rounded} {unit}"
        def fmt_arrival(value):
            minutes = float(value)
            return f"{round(minutes)} min" if minutes < 90 else f"{minutes / 60:.1f} hr"
        def crown_sentence(cls, context):
            suffix = ("under this scenario" if context == "scenario"
                      else "under the reference worst-case day")
            if cls == 0:
                return "Modeled as a surface fire here " + suffix + " — the canopy is not predicted to ignite."
            if cls == 1:
                return "Passive crown fire (torching) is modeled here " + suffix + " — individual trees or clumps candle."
            if cls == 2:
                return "Active crown fire is modeled here " + suffix + " — fire carries through the canopy."
            return None

        out = []
        fuel = val("fuel_model")
        if fuel is not None:
            key = str(int(round(fuel)))
            legend = rec("fuel_model").get("legend") or {}
            short = legend.get("short_name") or legend.get("name") or key
            out.append(f"Fuel here: {short}.")

        base = val("base_ros")
        slope = val("slope_hazard")
        if base is not None:
            slope_part = (f", ~{fmt_measure(slope, 'm/min')} with this slope"
                          if slope is not None else "")
            out.append("On a moderate day this fuel carries fire at "
                       f"~{fmt_measure(base, 'm/min')} on flat ground{slope_part}.")
        elif slope is not None:
            out.append("On a moderate day this slope-adjusted fuel carries fire "
                       f"at ~{fmt_measure(slope, 'm/min')}.")

        arrival = val("fire_arrival")
        duration = (((last or {}).get("scenario") or {}).get("duration_min")
                    if isinstance(last, dict) else None)
        if arrival is not None:
            if isinstance(duration, (int, float)) and arrival > duration:
                out.append("The fire never reaches this spot in this scenario "
                           f"(within its {round(duration)}-minute window).")
            else:
                out.append(f"Fire reaches this spot ~{fmt_arrival(arrival)} "
                           "after ignition (+/- class; one wind guess).")
        elif rec("fire_arrival"):
            suffix = (f" within the {round(duration)}-minute window"
                      if isinstance(duration, (int, float)) else "")
            out.append(f"The fire does not reach this spot in the last scenario{suffix}.")

        flame = val("flame_length")
        intensity = val("fireline_intensity")
        if flame is not None:
            intensity_part = (f" (~{fmt_measure(intensity, 'kW/m')})"
                              if intensity is not None else "")
            out.append(f"~{fmt_measure(flame, 'm')} flames here{intensity_part}.")
        elif intensity is not None:
            out.append(f"Fireline intensity is ~{fmt_measure(intensity, 'kW/m')} here.")

        crown = val("crown_class")
        if crown is not None:
            s = crown_sentence(int(round(crown)), "scenario")
            if s:
                out.append(s)
        else:
            crown = val("crown_potential")
            if crown is not None:
                s = crown_sentence(int(round(crown)), "reference")
                if s:
                    out.append(s)

        if val("ember_exposure") is not None:
            out.append("This spot is in the downwind ember-exposure band; firebrands can cross water, wetlands, roads, and cleared gaps.")
        recap = self._fire_scenario_recap_sentence(last)
        if recap:
            out.append(recap)
        return out

    @staticmethod
    def _fire_scenario_recap_sentence(last):
        if not isinstance(last, dict):
            return None
        scenario = last.get("scenario") if isinstance(last.get("scenario"), dict) else {}
        moist = (last.get("derived_moistures")
                 if isinstance(last.get("derived_moistures"), dict) else {})
        label = str(scenario.get("weather_label") or scenario.get("label")
                    or scenario.get("weather_class") or "Scenario")
        label = re.sub(r"\s+-\s+", " — ", label)
        facts = []
        date = TwinQuery._month_day(scenario.get("date"))
        if date:
            facts.append(date)
        if isinstance(scenario.get("rh_min"), (int, float)):
            facts.append(f"RH {float(scenario['rh_min']):.0f}%")
        if isinstance(scenario.get("wind_mph"), (int, float)):
            facts.append(f"{float(scenario['wind_mph']):.0f} mph wind")
        moisture_bits = []
        if isinstance(moist.get("dead_1h_pct"), (int, float)):
            moisture_bits.append(f"1h {float(moist['dead_1h_pct']):.1f}%")
        if isinstance(moist.get("fmc_pct"), (int, float)):
            moisture_bits.append(f"FMC {float(moist['fmc_pct']):.0f}%")
        fact_text = f" ({', '.join(facts)})" if facts else ""
        moist_text = f" - moistures {' / '.join(moisture_bits)}" if moisture_bits else ""
        return f"Scenario: {label}{fact_text}{moist_text}."

    @staticmethod
    def _month_day(date_text):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(date_text or ""))
        if not m:
            return ""
        names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        idx = int(m.group(2)) - 1
        month = names[idx] if 0 <= idx < len(names) else m.group(2)
        return f"{month} {int(m.group(3))}"

    @staticmethod
    def _fire_last_scenario_brief(last):
        if not isinstance(last, dict):
            return None
        return {
            "run_id": last.get("run_id"),
            "scenario": last.get("scenario"),
            "derived_moistures": last.get("derived_moistures"),
            "ros_at_ignition": last.get("ros_at_ignition"),
            "burned_area": last.get("burned_area"),
        }

    def _hydro_provenance(self):
        runs = [r for r in self._runs_by_id().values()
                if r.get("script") in ("analyze_hydrology.py", "hydro_scenario.py")]
        latest = max(runs, key=lambda r: r.get("started_at") or "", default=None)
        return {
            "source": "twin_hydrology.py (priority-flood + D8 flow, pure numpy)",
            "acquisition": "derived",
            "soils": "gSSURGO tabular + polygons (seep score, scenario CN)",
            "run_id": latest["run_id"] if latest else None,
            "caveat": "geometry (where water concentrates) is reliable; "
                      "discharge magnitude is scenario-grade, not a forecast",
        }

    # -- evapotranspiration / water balance ----------------------------------

    def et_summary(self):
        """Annual/monthly ET0 and AET with uncertainty and Budyko sanity check."""
        et0 = self._read_json(ET0_SUMMARY)
        wb = self._read_json(ET_SUMMARY)
        if not et0 and not wb:
            raise TwinQueryError(
                "no ET outputs — run `npm run derive-et0` and `npm run et-water-balance` first",
                paths=[ET0_SUMMARY, ET_SUMMARY])
        return {
            "et0": et0,
            "water_balance": wb,
            "provenance": self._et_provenance(),
            "uncertainty": (
                "Reduced-data ET0 uses modeled Daymet humidity when available and "
                "assumed u2=2 m/s for FAO-56 PM. Annual AET is +/-20-35% absent "
                "local validation; timing/relative wetness are more reliable."
            ),
        }

    def et_at(self, point):
        """Sample annual AET raster plus latest root-zone state at a point."""
        cat = self._et_catalog()
        if not cat:
            raise TwinQueryError(
                "no ET layers — run `npm run et-water-balance` first",
                path=ET_LAYER_CATALOG)
        x, y = resolve_point(point, self.georef)
        echo = self.georef.echo(x, y)
        layers = {}
        for layer in cat:
            grid = self._et_grid(layer)
            s = sample_grid(grid, layer["bounds_local"], x, y)
            v = s[2] if s else None
            if v is not None and v == grid.get("nodata"):
                v = None
            layers[layer["id"]] = {
                "value": round(v, 3) if isinstance(v, (int, float)) else v,
                "label": layer.get("label"),
                "group": layer.get("group"),
                "description": layer.get("description"),
                "value_kind": grid.get("value_kind") or layer.get("value_kind"),
                "value_unit": grid.get("value_unit") or layer.get("value_unit"),
            }
        daily = self._soil_water_daily()
        latest = daily[-1] if daily else None

        def f(row, key):
            try:
                return float(row.get(key)) if row and row.get(key) not in (None, "") else None
            except ValueError:
                return None

        return {
            "point": echo,
            "layers": layers,
            "latest_soil_water": None if latest is None else {
                "date": latest.get("date"),
                "aet_mm_day": f(latest, "aet_mm"),
                "deficit_proxy_mm_day": (
                    round(max(0.0, f(latest, "et0_mm") - f(latest, "aet_mm")), 3)
                    if f(latest, "et0_mm") is not None and f(latest, "aet_mm") is not None else None),
                "root_zone_depletion_fraction": f(latest, "root_zone_depletion_fraction"),
                "Ks": f(latest, "Ks"),
                "wetness_5d": f(latest, "wetness_5d"),
                "wetness_14d": f(latest, "wetness_14d"),
                "wetness_30d": f(latest, "wetness_30d"),
                "recharge_residual_mm_day": f(latest, "recharge_residual_mm"),
            },
            "soil": self._soil_at(x, y),
            "provenance": self._et_provenance(),
        }

    def water_balance(self, region=None):
        """Aggregate P, ET, runoff, storage-change and recharge over a region."""
        summ = self._read_json(ET_SUMMARY)
        if not summ:
            raise TwinQueryError(
                "no ET water-balance summary — run `npm run et-water-balance` first",
                path=ET_SUMMARY)
        reg = resolve_region(region or {"aoi": True}, self.georef)
        samples, spacing = self._region_samples(reg, target=2500)
        layer = next((l for l in self._et_catalog() if l.get("id") == "aet_annual"), None)
        aet_vals = []
        if layer:
            grid = self._et_grid(layer)
            for x, y in samples:
                s = sample_grid(grid, layer["bounds_local"], x, y)
                if s and isinstance(s[2], (int, float)):
                    aet_vals.append(float(s[2]))
        years = summ.get("annual") or {}
        latest_year = sorted(years)[-1] if years else None
        a = years.get(latest_year, {}) if latest_year else {}
        area_m2 = reg.area_m2
        mm_to_m3 = area_m2 / 1000.0
        aet_mm = (sum(aet_vals) / len(aet_vals)) if aet_vals else a.get("aet_mm")
        return {
            "region": {"shape": reg.shape, "description": reg.description,
                       "area_m2": round(area_m2, 1), "sample_count": len(samples),
                       "sample_spacing_m": round(spacing, 2)},
            "year": latest_year,
            "annual_mm": {
                "precip": a.get("precip_mm"),
                "et0": a.get("et0_mm"),
                "aet": round(aet_mm, 1) if aet_mm is not None else None,
                "modeled_runoff": a.get("modeled_runoff_mm"),
                "delta_storage_proxy": 0.0,
                "recharge_residual": a.get("recharge_residual_mm"),
            },
            "annual_m3": {
                "precip": round(a.get("precip_mm", 0.0) * mm_to_m3, 1) if a else None,
                "aet": round(aet_mm * mm_to_m3, 1) if aet_mm is not None else None,
                "modeled_runoff": round(a.get("modeled_runoff_mm", 0.0) * mm_to_m3, 1) if a else None,
                "recharge_residual": round(a.get("recharge_residual_mm", 0.0) * mm_to_m3, 1) if a else None,
            },
            "checks": {
                "aet_over_p": a.get("aet_over_p"),
                "budyko_aridity_index": a.get("budyko_aridity_index"),
                "budyko_expected_aet_over_p": a.get("budyko_expected_aet_over_p"),
                "budyko_position": a.get("budyko_position"),
            },
            "provenance": self._et_provenance(),
            "uncertainty": "Annual AET +/-20-35% absent local validation; regional aggregation samples the modeled AET raster.",
        }

    def _et_provenance(self):
        runs = [r for r in self._runs_by_id().values()
                if r.get("script") in ("derive_et0_daily.py", "et_water_balance.py")]
        latest = max(runs, key=lambda r: r.get("started_at") or "", default=None)
        return {
            "source": "derive_et0_daily.py + et_water_balance.py",
            "acquisition": "derived",
            "run_id": latest["run_id"] if latest else None,
            "files": {
                "et0_summary": os.path.relpath(ET0_SUMMARY, DATA),
                "water_balance_summary": os.path.relpath(ET_SUMMARY, DATA),
                "soil_water_daily": os.path.relpath(ET_SOIL_WATER_DAILY, DATA),
                "layers": os.path.relpath(ET_LAYER_CATALOG, DATA),
            },
        }

    def _hydrology_sentences(self, layers, soil):
        """Port of public/simulation.js interpretAt — the same plain-language
        reading from the same thresholds, so MCP and the viewer agree."""
        def val(lid):
            v = layers.get(lid, {}).get("value")
            return v if isinstance(v, (int, float)) else None
        def flow_ha():
            rec = layers.get("flow_paths") or {}
            v = rec.get("value")
            if not isinstance(v, (int, float)):
                return None
            unit = str(rec.get("value_unit") or rec.get("flow_unit") or
                       rec.get("units") or "").strip().lower().replace("²", "2")
            unit = unit.replace("-", "_").replace(" ", "_")
            if unit in ("ha", "hectare", "hectares"):
                return float(v)
            if unit in ("m2", "sq_m", "sqm", "square_meter", "square_meters"):
                return float(v) / 10000.0
            if unit in ("cell", "cells", "grid_cell", "grid_cells"):
                area = rec.get("cell_area_m2")
                if isinstance(area, (int, float)) and area > 0:
                    return float(v) * float(area) / 10000.0
            return None
        out = []
        summ = self._read_json(HYDRO_SUMMARY) or {}
        max_ha = summ.get("max_contributing_ha")

        if val("flow_paths") is not None:
            ha = flow_ha()
            if ha is None:
                out.append("Flow-path accumulation is available here, but its "
                           "unit metadata is missing or unknown, so the "
                           "drainage area is not displayed.")
            else:
                m2 = ha * 10000
                pct = round(ha / max_ha * 100) if max_ha else None
                if ha < 0.05:
                    out.append(f"Only local water passes here — about {m2:.0f} m² "
                               "drains through this spot.")
                elif ha < 0.5:
                    out.append(f"A defined flow path: water from about {m2:.0f} m² "
                               f"({ha:.2f} ha) upslope funnels through here when it runs.")
                elif ha < 2:
                    out.append(f"A significant drainage line — roughly {ha:.1f} ha "
                               f"drains through this point"
                               + (f" ({pct}% of the property's largest drainage)."
                                  if pct else "."))
                else:
                    out.append(f"A main channel: about {ha:.1f} ha"
                               + (f" — {pct}% of " if pct else " — ")
                               + "the property's biggest drainage — passes through "
                               "here. Expect real flow in any melt or storm.")

        twi = val("wetness_index")
        if twi is not None:
            if twi >= 90:
                out.append(f"Among the wettest ground on the property (wetter than "
                           f"{twi:.0f}% of it) — expect soft, saturated soil much "
                           "of the year.")
            elif twi >= 70:
                out.append(f"Wetter than {twi:.0f}% of the property — likely damp "
                           "after rain and in spring.")
            elif twi >= 30:
                out.append(f"Middling wetness for this land ({twi:.0f}th percentile).")
            else:
                out.append(f"Dry ground by this property's standards "
                           f"({twi:.0f}th percentile) — water sheds away rather "
                           "than collecting.")

        pond = val("ponding")
        if pond is not None and pond > 0:
            cm = pond * 100
            if cm < 8:
                out.append(f"A shallow pool forms here (~{cm:.0f} cm) before water "
                           "finds its way out.")
            else:
                depth = f"{cm:.0f} cm" if cm < 100 else f"{pond:.1f} m"
                out.append(f"Water pools here up to ~{depth} deep before spilling — "
                           "a real depression in the LiDAR surface.")

        seep = val("seep_candidates")
        if seep is not None:
            if soil and soil.get("depth_to_bedrock_min_cm"):
                geo = (f"bedrock as shallow as "
                       f"{round(soil['depth_to_bedrock_min_cm'])} cm here")
            elif soil and soil.get("water_table_depth_annual_min_cm") is not None:
                geo = (f"a seasonal water table at ~"
                       f"{round(soil['water_table_depth_annual_min_cm'])} cm")
            else:
                geo = "the soil profile"
            if seep >= 75:
                out.append(f"Strong spring/seep candidate ({seep:.0f}/100): "
                           f"converging water, a slope break, and {geo} all line "
                           "up. Worth a field check.")
            elif seep >= 60:
                out.append(f"Moderate spring/seep candidate ({seep:.0f}/100) — "
                           "conditions partly favor groundwater surfacing near here.")
            elif seep >= 45:
                out.append(f"Weak seep signal ({seep:.0f}/100); damp ground is "
                           "plausible, a flowing spring unlikely.")
            else:
                out.append(f"Little to suggest a spring here (score {seep:.0f}/100).")

        if soil and soil.get("muname"):
            bits = []
            if soil.get("hydrologic_group"):
                bits.append(f"hydrologic group {soil['hydrologic_group']}")
            if soil.get("drainage_class"):
                bits.append(str(soil["drainage_class"]).lower())
            if soil.get("surface_ksat_mm_hr"):
                bits.append(f"soaks ~{round(soil['surface_ksat_mm_hr'])} mm/hr at "
                            "the surface")
            tail = f" — {', '.join(bits)}" if bits else ""
            out.append(f"Soil: {str(soil['muname']).replace(chr(34), '')}{tail}.")

        scen = self._read_json(HYDRO_LAST_SCENARIO)
        ro, flow = val("scenario_runoff"), val("scenario_flow")
        absorbed = val("scenario_infiltration")
        stored = val("scenario_soil_storage")
        drained = val("scenario_deep_drainage")
        saturation_excess = val("scenario_saturation_excess")
        saturation_pct = val("scenario_saturation")
        runon = val("scenario_runon")
        ponded = val("scenario_ponded_water")
        scenario_values = (ro, absorbed, stored, drained, saturation_excess,
                           saturation_pct, runon, ponded, flow)
        if any(v is not None for v in scenario_values) and scen:
            label = (scen.get("scenario") or {}).get("label")
            parts = []
            total = (scen.get("water_input") or {}).get("total_mm")
            if ro is not None:
                pct = round(ro / total * 100) if total else None
                parts.append(f"~{ro:.0f} mm of the local {round(total)} mm input "
                             f"stays on the surface here"
                             f"{f' ({pct}% locally unabsorbed)' if pct is not None else ''} "
                             "before routing"
                             if total else f"~{ro:.0f} mm of local input stays on "
                             "the surface here before routing")
            if absorbed is not None:
                runon_note = ", including upstream runon" if total and absorbed > total + 0.5 else ""
                parts.append(f"~{absorbed:.0f} mm enters the soil{runon_note}")
            if stored is not None:
                parts.append(f"~{stored:.0f} mm remains as modeled profile water gain")
            if drained is not None:
                parts.append(f"~{drained:.0f} mm percolates below the modeled profile")
            if saturation_excess is not None:
                parts.append(
                    f"~{saturation_excess:.0f} mm of local input becomes saturation "
                    "excess here and may re-infiltrate downslope"
                    if saturation_excess >= 0.05
                    else "no local saturation excess is generated here")
            if saturation_pct is not None:
                parts.append(f"the modeled profile ends ~{saturation_pct:.0f}% water-filled")
            if runon is not None:
                parts.append(f"~{runon:.1f} m³ of upstream surface water arrives here "
                             "before infiltration" if runon >= 0.05 else
                             "almost no upstream surface runon arrives here")
            if ponded is not None and ponded >= 0.05:
                parts.append(f"~{ponded:.1f} mm remains ponded at event end")
            if flow is not None:
                outlet = (scen.get("outlet") or {}).get("event_volume_m3")
                if flow >= 1:
                    pct = round(flow / outlet * 100) if outlet else None
                    parts.append(f"about {flow:.0f} m³ of surface water leaves this cell over the "
                                 "event"
                                 + (f" — {pct}% of the combined outlet volume"
                                    if pct else ""))
                else:
                    parts.append("almost no surface throughflow leaves this exact spot")
            if parts:
                tag = f'“{label}”' if label else "scenario"
                out.append(f"In the simulated {tag}: " + "; ".join(parts) + ".")
        return out

    # -- viewshed -------------------------------------------------------------

    def _viewshed_layer_catalog(self, layer):
        os.makedirs(VIEWSHED_DIR, exist_ok=True)
        catalog = {"version": 1, "layers": []}
        if os.path.exists(VIEWSHED_CATALOG):
            try:
                catalog = json.load(open(VIEWSHED_CATALOG))
            except Exception:
                catalog = {"version": 1, "layers": []}
        catalog["layers"] = [l for l in catalog.get("layers", []) if l.get("id") != layer["id"]] + [layer]
        tmp = VIEWSHED_CATALOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(catalog, fh, indent=2)
        os.replace(tmp, VIEWSHED_CATALOG)
        return catalog

    def _write_viewshed_layer(self, result, layer_id="viewshed_current", label="Current viewshed"):
        stack = self._viewshed_stack()
        ring = stack.rings[0]
        mask = result["visible"][ring.name]
        os.makedirs(os.path.join(VIEWSHED_DIR, "local"), exist_ok=True)
        image_rel = f"viewshed/local/{layer_id}.png"
        grid_rel = f"viewshed/local/{layer_id}.grid.json"
        rgba = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
        rgba[mask > 0] = [255, 142, 26, 190]
        write_rgba_png(os.path.join(DATA, image_rel), rgba)
        grid = {
            "bounds_local": [ring.min_x, ring.min_y, ring.max_x, ring.max_y],
            "width": int(mask.shape[1]),
            "height": int(mask.shape[0]),
            "nodata": None,
            "legend": {"0": {"name": "not visible"}, "1": {"name": "visible"}},
            "values": [[int(v) for v in row] for row in mask],
            "surface": result.get("surface"),
            "k": result.get("k"),
            "manifest_hash": stack.manifest_hash,
        }
        with open(os.path.join(DATA, grid_rel), "w") as fh:
            json.dump(grid, fh, separators=(",", ":"))
        layer = {
            "id": layer_id,
            "label": label,
            "type": "raster",
            "group": "viewshed",
            "image": image_rel,
            "grid": grid_rel,
            "bounds_local": grid["bounds_local"],
            "description": f"Viewshed from {result['observer']['x']:.1f},{result['observer']['y']:.1f}; surface={result['surface']}, k={result['k']:.5f}.",
        }
        self._viewshed_layer_catalog(layer)
        doc = _load_view_doc()
        doc["layer_views"] = [v for v in doc["layer_views"] if v.get("layer_id") != layer_id]
        doc["layer_views"].append({"layer_id": layer_id, "visible": True, "created_at": _utc_now()})
        _save_view_doc(doc)
        return {"layer": layer, "catalog": os.path.relpath(VIEWSHED_CATALOG, DATA)}

    def _viewshed_provenance(self, result):
        stack = self._viewshed_stack()
        return {
            "tool": "twin_viewshed radial R2 sweep",
            "surface": result.get("surface"),
            "k": result.get("k"),
            "cc": result.get("cc"),
            "rings": [{
                "id": r.name,
                "resolution_m": r.resolution_m,
                "canopy_available": r.canopy is not None,
                "bounds_scene_m": [round(r.min_x, 2), round(r.min_y, 2), round(r.max_x, 2), round(r.max_y, 2)],
            } for r in stack.rings],
            "manifest_hash": stack.manifest_hash,
            "accuracy_note": "Exact radial sweep on the loaded ring surface; no claim beyond analyzed_extent_km.",
        }

    def viewshed_from(self, point, agl_m=1.7, max_km=None, refraction="optical",
                      surface="bare_earth", demonstrate=False):
        stack, result, x, y, _key = self._viewshed_sweep_cached(
            point, agl_m=agl_m, refraction=refraction, max_km=max_km,
            surface=surface, n_az=1440)
        canopy_hidden = None
        if surface == "canopy":
            bare = twin_viewshed.sweep(stack, x, y, float(agl_m), n_az=720,
                                       max_km=max_km, surface="bare_earth",
                                       k=refraction or "optical")
            canopy_hidden = max(0.0, bare["stats"]["visible_km2"] - result["stats"]["visible_km2"])
        payload = {
            "observer": self.georef.echo(x, y),
            "agl_m": float(agl_m),
            "visible_area_km2": round(result["stats"]["visible_km2"], 6),
            "max_visible_km": round(result["stats"]["max_visible_km"], 3),
            "sky_open_fraction": round(result["stats"]["sky_open_fraction_ge_2deg"], 4),
            "surface": result["surface"],
            "refraction": refraction,
            "k": result["k"],
            "canopy_hidden_km2": None if canopy_hidden is None else round(canopy_hidden, 6),
            "analyzed_extent_km": round(result["stats"]["analyzed_extent_km"], 3),
            "needs_fetch": bool(max_km is not None and float(max_km) > result["stats"]["analyzed_extent_km"] + 1e-6),
            "provenance": self._viewshed_provenance(result),
        }
        if demonstrate:
            payload["demonstration"] = self._write_viewshed_layer(result)
            payload["observer_marker"] = self.draw_point({"x": x, "y": y}, label=f"Viewshed {float(agl_m):g} m AGL").get("drawn")
        return payload

    def can_see(self, from_point, to_point, from_agl_m=1.7, to_agl_m=0.0,
                refraction="optical", surface="bare_earth", freq_mhz=None):
        x0, y0 = resolve_point(from_point, self.georef)
        x1, y1 = resolve_point(to_point, self.georef)
        result = twin_viewshed.line_of_sight(
            self._viewshed_stack(), x0, y0, float(from_agl_m), x1, y1,
            float(to_agl_m), k=refraction or "optical", surface=surface or "canopy")
        if result.get("error"):
            return result
        obs = result.get("obstruction")
        if obs:
            obs = {**obs, "position": self.georef.echo(obs["x"], obs["y"])}
            obs.pop("x", None)
            obs.pop("y", None)
        fresnel = None
        if freq_mhz and refraction and str(refraction).startswith("radio"):
            d_km = result["distance_km"]
            fresnel = {"note": "first Fresnel radius at midpoint, approximate",
                       "radius_m": round(17.3 * math.sqrt((d_km / 2) * (d_km / 2) / (float(freq_mhz) * d_km)), 3)}
        return {
            "from": self.georef.echo(x0, y0),
            "to": self.georef.echo(x1, y1),
            "visible": result["visible"],
            "bearing_deg": round(result["bearing_deg"], 3),
            "distance_km": round(result["distance_km"], 3),
            "obstruction": obs,
            "required_from_agl_m": round(result["required_agl0_m"], 3),
            "clearance_deficit_m": round(result["clearance_deficit_m"], 3),
            "surface": result["surface"],
            "refraction": refraction,
            "k": result["k"],
            "fresnel": fresnel,
            "provenance": self._viewshed_provenance({"surface": result["surface"], "k": result["k"], "cc": result["cc"]}),
        }

    def _sun_block_windows(self, horizon_deg, date=None):
        base = date or twin_astro.iso_from_dt(twin_astro._utc_now())
        day = str(base)[:10]
        rows = []
        for minute in range(0, 24 * 60, 10):
            hh, mm = divmod(minute, 60)
            iso = f"{day}T{hh:02d}:{mm:02d}:00Z"
            try:
                sun = twin_astro.body_position("sun", time=iso, site=self._astronomy_site())
            except Exception:
                continue
            hz = twin_viewshed.horizon_at_azimuth(np.asarray(horizon_deg), sun["azimuth_deg"])
            rows.append({"time": iso, "blocked": sun["altitude_deg"] < hz,
                         "sun_altitude_deg": sun["altitude_deg"], "horizon_deg": hz})
        windows = []
        cur = None
        for row in rows:
            if cur is None or cur["blocked"] != row["blocked"]:
                if cur is not None:
                    cur["end"] = row["time"]
                    windows.append(cur)
                cur = {"blocked": row["blocked"], "begin": row["time"]}
        if cur is not None:
            cur["end"] = rows[-1]["time"] if rows else None
            windows.append(cur)
        return windows

    def horizon_at(self, point, agl_m=1.7, date=None, surface="bare_earth"):
        stack, result, x, y, _key = self._viewshed_sweep_cached(
            point, agl_m=agl_m, refraction="optical", surface=surface, n_az=720)
        horizon = result["horizon_deg"]
        compact = twin_viewshed.compact_horizon(horizon, 72)
        geo = twin_viewshed.geo_arc_elevations(self._astronomy_site().lat, n_az=72)
        for row, hz in zip(geo, compact):
            row["horizon_deg"] = hz
            row["clear"] = bool(math.isfinite(row["elevation_deg"]) and row["elevation_deg"] > hz)
        return {
            "observer": self.georef.echo(x, y),
            "agl_m": float(agl_m),
            "surface": surface,
            "horizon_72_deg": compact,
            "min_horizon_deg": round(float(np.nanmin(horizon)), 3),
            "max_horizon_deg": round(float(np.nanmax(horizon)), 3),
            "sun_windows": self._sun_block_windows(horizon, date=date),
            "geo_arc": {"refraction": "none",
                        "note": "geometric spherical-earth elevation of the GEO belt; no atmospheric refraction applied",
                        "samples": geo},
            "provenance": self._viewshed_provenance(result),
        }

    def best_viewpoints(self, region=None, agl_m=1.7, objective="area", target=None,
                        surface="bare_earth", count=3, demonstrate=False):
        reg = self._resolve_region(region or {"aoi": True})
        pts, spacing = self._regular_lattice_points(reg, target=80)
        ranked = []
        for x, y in pts:
            if self._terrain_elevation(x, y) is None:
                continue
            if objective == "sees_target" and target:
                tx, ty = resolve_point(target, self.georef)
                los = twin_viewshed.line_of_sight(self._viewshed_stack(), x, y, float(agl_m), tx, ty,
                                                  0.0, k="radio", surface=surface)
                score = 1.0 if los.get("visible") else 0.0
                area = 0.0
            else:
                r = twin_viewshed.sweep(self._viewshed_stack(), x, y, float(agl_m), n_az=180,
                                        surface=surface)
                area = r["stats"]["visible_km2"]
                score = area
            ranked.append({"x": x, "y": y, "score": score, "visible_area_km2": area})
        ranked.sort(key=lambda r: (-r["score"], r["x"], r["y"]))
        out = []
        for i, row in enumerate(ranked[:max(1, min(int(count), 10))], start=1):
            rec = {"rank": i, "point": self.georef.echo(row["x"], row["y"]),
                   "score": round(row["score"], 6),
                   "visible_area_km2": round(row["visible_area_km2"], 6)}
            if demonstrate:
                rec["drawn"] = self.draw_point({"x": row["x"], "y": row["y"]},
                                               label=f"Viewpoint #{i}").get("drawn")
            out.append(rec)
        return {"region": reg.describe(), "objective": objective, "agl_m": float(agl_m),
                "surface": surface, "candidate_spacing_m": round(spacing, 3),
                "viewpoints": out, "provenance": {"tool": "best_viewpoints", "refraction_for_target": "radio"}}

    # -- astronomy -----------------------------------------------------------

    def _astronomy_site(self):
        site = getattr(self, "_cached_astronomy_site", None)
        if site is None:
            site = twin_astro.site_from_georef()
            self._cached_astronomy_site = site
        return site

    def _astronomy_error(self, error):
        if isinstance(error, twin_astro.AstronomyNameError):
            payload = dict(error.payload)
            message = payload.pop("error", str(error))
            raise TwinQueryError(message, **payload)
        raise TwinQueryError(str(error))

    def _precomputed_horizon(self, surface="bare_earth"):
        path = os.path.join(VIEWSHED_DIR, "horizon.json")
        if not os.path.exists(path):
            return None
        try:
            doc = json.load(open(path))
            values = (doc.get("horizon_deg") or {}).get(surface) or (doc.get("horizon_deg") or {}).get("bare_earth")
            return np.asarray(values, dtype=np.float32) if values else None
        except Exception:
            return None

    def _terrain_block_for_altaz(self, altitude, azimuth, point=None, surface="bare_earth"):
        if altitude is None or azimuth is None:
            return None
        horizon = None
        source = "precomputed_aoi_centroid"
        if point:
            try:
                _stack, result, _x, _y, _key = self._viewshed_sweep_cached(
                    point, agl_m=1.7, refraction="optical", surface=surface, n_az=720)
                horizon = result["horizon_deg"]
                source = "observer_point_sweep"
            except Exception:
                horizon = None
        if horizon is None:
            horizon = self._precomputed_horizon(surface)
        if horizon is None:
            return {"available": False, "blocked": None, "source": "missing data/viewshed/horizon.json"}
        hz = twin_viewshed.horizon_at_azimuth(horizon, azimuth)
        return {"available": True, "blocked": bool(float(altitude) < hz),
                "horizon_deg_at_azimuth": round(float(hz), 3),
                "source": source, "surface": surface}

    def sky_at(self, time=None, point=None, surface="bare_earth"):
        """Sun, moon, visible planets, twilight state, and rise/set events at
        the twin's observer site for a UTC time (default: now)."""
        try:
            result = twin_astro.sky_at(time=time, site=self._astronomy_site())
        except (twin_astro.AstronomyNameError, ValueError) as error:
            self._astronomy_error(error)
        sun = result.get("sun") or {}
        result["terrain"] = {"sun": self._terrain_block_for_altaz(
            sun.get("altitude_deg"), sun.get("azimuth_deg"), point=point, surface=surface)}
        return result

    def body_position(self, body, time=None, point=None, surface="bare_earth"):
        """Topocentric position, phase/size where available, constellation,
        and next rise/set/culmination for a body or named star."""
        try:
            result = twin_astro.body_position(body, time=time, site=self._astronomy_site())
        except (twin_astro.AstronomyNameError, ValueError) as error:
            self._astronomy_error(error)
        result["terrain"] = self._terrain_block_for_altaz(
            result.get("altitude_deg"), result.get("azimuth_deg"), point=point, surface=surface)
        return result

    def next_sky_event(self, kind, from_time=None, count=1, max_span_deg=50.0,
                       horizon_years=100.0, demonstrate=False):
        """Find upcoming sky events at the twin site: eclipses (including the
        next path-of-totality pass over the site and blood moons visible from
        here), planetary alignments, supermoons, moon phases, rise/set events,
        solstices/equinoxes, or golden hour windows. demonstrate=True also
        scrubs the live viewer's astronomy clock to the first event and
        highlights the bodies involved (annotations.json only, never the
        store)."""
        try:
            result = twin_astro.next_sky_event(kind, from_time=from_time, count=count,
                                               site=self._astronomy_site(),
                                               max_span_deg=max_span_deg,
                                               horizon_years=horizon_years)
        except (twin_astro.AstronomyNameError, ValueError) as error:
            self._astronomy_error(error)
        if demonstrate:
            if result.get("events"):
                result["demonstration"] = self._demonstrate_sky_event(
                    result["kind"], result["events"][0])
            else:
                result["demonstration"] = {"note": "no event found; nothing written to the viewer"}
        return result

    def _demonstrate_sky_event(self, kind, event):
        def iso_of(key):
            block = event.get(key)
            return block.get("iso") if isinstance(block, dict) else None

        rate = 1.0
        lead_ms = 0
        if kind in {"solar_eclipse", "total_solar_eclipse"}:
            iso = iso_of("total_begin") or iso_of("peak")
            rate, lead_ms, targets = 60.0, 600_000, ["sun"]
        elif kind in {"lunar_eclipse", "total_lunar_eclipse", "blood_moon"}:
            iso = iso_of("total_begin") or iso_of("partial_begin") or iso_of("peak")
            rate, lead_ms, targets = 60.0, 600_000, ["moon"]
        elif kind == "planetary_alignment":
            iso = iso_of("peak")
            targets = [p["name"] for p in event.get("planets", [])]
        elif kind in {"full_moon", "new_moon", "supermoon", "moonrise", "moonset"}:
            iso = iso_of("time")
            targets = ["moon"]
        else:
            iso = iso_of("time") or iso_of("begin")
            targets = ["sun"]
        if not iso:
            return {"note": "first event carries no usable time; nothing written to the viewer"}
        _, ms, _ = twin_astro.normalize_time(iso)
        start_iso = twin_astro.iso_from_dt(
            twin_astro.datetime_from_unix_ms(twin_astro.clamp_unix_ms(ms - lead_ms)))
        label = kind.replace("_", " ")
        label = label[:1].upper() + label[1:]
        highlighted = []
        for name in targets:
            try:
                target = twin_astro.resolve_target(name)
            except twin_astro.AstronomyNameError:
                continue
            self._upsert_sky_view({
                "target_type": target["target_type"],
                "name": target.get("name") or target.get("abbr") or str(name),
                "label": label,
                "created_at": _utc_now(),
            })
            highlighted.append(target.get("name") or str(name))
        view = self.set_view_time(start_iso, rate=rate)
        return {
            "view_time": view.get("view_time"),
            "highlighted": highlighted,
            "note": "viewer clock scrubbed to the event and target(s) highlighted; the browser applies it within a few seconds",
        }

    def solar_irradiance(self, time=None, point=None, surface="bare_earth"):
        """Clear-sky GHI/DNI/DHI at the twin site for a UTC time. This is a
        radiometric data result with optional terrain-horizon adjustment."""
        try:
            result = twin_astro.solar_irradiance(time=time, site=self._astronomy_site())
            sun = twin_astro.body_position("sun", time=time, site=self._astronomy_site())
        except (twin_astro.AstronomyNameError, ValueError) as error:
            self._astronomy_error(error)
        terrain = self._terrain_block_for_altaz(
            result.get("sun_altitude_deg"), sun.get("azimuth_deg"), point=point, surface=surface)
        if terrain and terrain.get("available"):
            horizon = self._precomputed_horizon(surface)
            sky_fraction = 0.5 if horizon is None else float(np.count_nonzero(horizon <= 2.0) / len(horizon))
            if terrain.get("blocked"):
                result["dni_wm2_unadjusted"] = result.get("dni_wm2")
                result["ghi_wm2_unadjusted"] = result.get("ghi_wm2")
                result["dni_wm2"] = 0.0
                result["dhi_wm2"] = round(float(result.get("dhi_wm2") or 0.0) * sky_fraction, 2)
                result["ghi_wm2"] = result["dhi_wm2"]
            result["horizon_adjusted"] = True
            result["terrain"] = terrain
            result["sky_view_fraction"] = round(sky_fraction, 4)
        else:
            result["horizon_adjusted"] = False
            result["terrain"] = terrain
        return result

    # -- solar siting --------------------------------------------------------

    def _solar_site_for_point(self, point):
        x, y = resolve_point(point, self.georef)
        elev = self._terrain_elevation(x, y)
        if elev is None:
            raise TwinQueryError("solar point is outside available terrain", point=self.georef.echo(x, y))
        echo = self.georef.echo(x, y)
        return x, y, twin_solar.SolarSite(float(echo["lat"]), float(echo["lon"]), float(elev))

    def _solar_horizon(self, point, surface="bare_earth", n_az=360):
        try:
            _stack, result, x, y, _key = self._viewshed_sweep_cached(
                point, agl_m=1.7, refraction="optical", surface=surface, n_az=n_az)
            horizon = result["horizon_deg"]
            meta = {
                "available": True,
                "surface": result.get("surface", surface),
                "source": "observer_point_viewshed",
                "analyzed_extent_km": result.get("stats", {}).get("analyzed_extent_km"),
                "manifest_hash": result.get("manifest_hash"),
            }
            if surface == "canopy":
                # The viewshed canopy horizon is 30 m class-binned EVH; lift it
                # with the per-stem inventory so a single tree just outside the
                # clearance radius still shades the panel. Combined by max, so
                # EVH canopy is never double-counted.
                lift = twin_solar.vegetation_horizon_lift(
                    self._solar_vegetation_index(), x, y, horizon)
                horizon = np.asarray(lift["horizon_deg"], dtype=np.float32)
                meta["vegetation_lift"] = {
                    "applied": lift["applied"],
                    "stems_in_range": lift["stems_in_range"],
                    "stems_lifting": lift["stems_lifting"],
                }
            meta["sky_view_fraction"] = twin_solar.sky_view_fraction(horizon)
            return horizon, meta
        except Exception as exc:
            return None, {
                "available": False,
                "surface": surface,
                "source": "none",
                "message": str(exc),
            }

    @staticmethod
    def _solar_default_tilt(site):
        return max(5.0, min(60.0, abs(float(site.lat))))

    @staticmethod
    def _solar_default_azimuth(site):
        return 180.0 if float(site.lat) >= 0.0 else 0.0

    def _solar_store_vegetation_records(self):
        records = []
        for kind in ("tree", "shrub"):
            if kind not in self.kinds():
                continue
            latest = self._latest_full(kind)
            for eid, (x, y) in self._positions(kind).items():
                attrs = latest.get(eid, {})
                def attr_value(*names):
                    for name in names:
                        if name in attrs:
                            return twin_store.decode_value(attrs[name][0])
                    return None
                height = attr_value("height", "height_m")
                if height is None:
                    # Unknown height: assume a canonical blocking stem rather
                    # than 0 m, which would silently fall below
                    # min_blocker_height_m and vanish from clearance checks.
                    height = 8.0 if kind == "tree" else 1.0
                radius = attr_value("radius", "radius_m", "crown_radius", "crown_radius_m")
                ref = attrs.get("height") or attrs.get("radius")
                records.append({
                    "id": eid,
                    "kind": kind,
                    "x": x,
                    "y": y,
                    "height": height,
                    "radius": radius,
                    "type": attr_value("type"),
                    "species": attr_value("species"),
                    "source": ref[3] if ref else "twin_store",
                    "confidence": ref[4] if ref else None,
                })
        return records

    def _solar_vegetation_index(self):
        def build():
            index = twin_solar.SolarVegetationIndex.from_data_dir(DATA)
            if index.available:
                return index
            return twin_solar.SolarVegetationIndex.from_records(
                self._solar_store_vegetation_records(),
                source="twin_store tree/shrub entities",
            )
        return self._cache("solar_vegetation_index", build)

    def _solar_vegetation_clearance(self, x, y, system_kw=1.0):
        return self._solar_vegetation_index().clearance_at(x, y, system_kw=system_kw)

    def solar_at(self, point, tilt_deg=None, azimuth_deg=None, system_kw=1.0,
                 surface="canopy", objective="annual_kwh"):
        """Solar resource and fixed-panel PV estimate at a proposed site.

        If both tilt and azimuth are omitted, VEIL optimizes a fixed panel for
        the objective. If only one is supplied, the missing angle falls back to
        the local latitude-facing default so users can ask partial questions.
        """
        x, y, site = self._solar_site_for_point(point)
        horizon, horizon_meta = self._solar_horizon({"x": x, "y": y}, surface=surface, n_az=360)
        if tilt_deg is not None and azimuth_deg is None:
            azimuth_deg = self._solar_default_azimuth(site)
        if azimuth_deg is not None and tilt_deg is None:
            tilt_deg = self._solar_default_tilt(site)
        try:
            result = twin_solar.analyze_site(
                site, data_dir=DATA, horizon_deg=horizon,
                tilt_deg=None if tilt_deg is None else float(tilt_deg),
                azimuth_deg=None if azimuth_deg is None else float(azimuth_deg),
                system_kw=float(system_kw or 0.0),
                objective=objective or "annual_kwh")
        except Exception as exc:
            raise TwinQueryError(str(exc))
        result["point"] = self.georef.echo(x, y)
        result["surface"] = surface
        result["horizon"] = horizon_meta
        vegetation = self._solar_vegetation_clearance(x, y, system_kw=system_kw)
        result["vegetation"] = vegetation
        result["recommendation"] = {
            "tilt_deg": result["tilt_deg"],
            "azimuth_deg": result["azimuth_deg"],
            "azimuth_note": "degrees clockwise from true north; 180 is true south, 0 is true north",
            "why": ("optimized fixed-panel angle for the requested objective"
                    if result.get("optimized") else "user-specified fixed-panel angle"),
            "vegetation": vegetation.get("recommendation"),
        }
        return result

    def solar_profile(self, point, surface="canopy", system_kw=1.0):
        """Monthly/seasonal solar profile at a proposed panel site."""
        result = self.solar_at(point, system_kw=system_kw, surface=surface)
        return {
            "point": result["point"],
            "surface": result["surface"],
            "tilt_deg": result["tilt_deg"],
            "azimuth_deg": result["azimuth_deg"],
            "annual": result["annual"],
            "monthly": result["monthly"],
            "climate": result["climate"],
            "horizon": result["horizon"],
            "vegetation": result["vegetation"],
            "model": result["model"],
        }

    def compare_solar_sites(self, points, surface="canopy", system_kw=1.0,
                            objective="annual_kwh"):
        if not isinstance(points, list) or not points:
            raise TwinQueryError("compare_solar_sites needs a non-empty points list")
        rows = []
        for i, point in enumerate(points[:20], start=1):
            result = self.solar_at(point, system_kw=system_kw, surface=surface,
                                   objective=objective)
            rows.append({
                "rank_input": i,
                "point": result["point"],
                "tilt_deg": result["tilt_deg"],
                "azimuth_deg": result["azimuth_deg"],
                "annual": result["annual"],
                "climate": result["climate"],
                "horizon": result["horizon"],
                "vegetation": result["vegetation"],
            })
        metric = "winter_poa_kwh_m2" if "winter" in str(objective).lower() else "pv_kwh_per_kwdc"
        rows.sort(key=lambda r: (-(r["annual"].get(metric) or 0.0), r["rank_input"]))
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return {"objective": objective, "surface": surface, "sites": rows}

    def recommend_solar_sites(self, region=None, objective="annual_kwh", count=5,
                              surface="canopy", system_kw=1.0, demonstrate=True):
        reg = self._resolve_region(region or {"aoi": True})
        if reg is None:
            raise TwinQueryError("recommend_solar_sites needs a region")
        count = max(1, min(10, int(count or 5)))
        pts, spacing = self._regular_lattice_points(reg, target=max(40, count * 24))
        prelim = []
        vegetation_excluded = 0
        vegetation_unknown = 0
        for x, y in pts:
            elev = self._terrain_elevation(x, y)
            if elev is None:
                continue
            vegetation = self._solar_vegetation_clearance(x, y, system_kw=system_kw)
            if vegetation.get("installable") is False:
                vegetation_excluded += 1
                continue
            if vegetation.get("installable") is None:
                vegetation_unknown += 1
            echo = self.georef.echo(x, y)
            site = twin_solar.SolarSite(float(echo["lat"]), float(echo["lon"]), float(elev))
            horizon, horizon_meta = self._solar_horizon({"x": x, "y": y}, surface=surface, n_az=180)
            fixed = twin_solar.analyze_site(
                site, data_dir=DATA, horizon_deg=horizon,
                tilt_deg=self._solar_default_tilt(site),
                azimuth_deg=self._solar_default_azimuth(site),
                system_kw=float(system_kw or 0.0),
                objective=objective or "annual_kwh")
            metric = "winter_poa_kwh_m2" if "winter" in str(objective).lower() else "pv_kwh_per_kwdc"
            prelim.append({
                "x": x, "y": y, "site": site, "horizon": horizon,
                "horizon_meta": horizon_meta,
                "vegetation": vegetation,
                "score": float(fixed["annual"].get(metric) or 0.0),
            })
        prelim.sort(key=lambda r: (-r["score"], r["x"], r["y"]))
        selected = []
        min_sep = max(10.0, spacing * 0.8)
        for row in prelim:
            if any(math.hypot(row["x"] - s["x"], row["y"] - s["y"]) < min_sep for s in selected):
                continue
            selected.append(row)
            if len(selected) >= count:
                break
        sites = []
        for rank, row in enumerate(selected, start=1):
            result = twin_solar.analyze_site(
                row["site"], data_dir=DATA, horizon_deg=row["horizon"],
                system_kw=float(system_kw or 0.0), objective=objective or "annual_kwh")
            rec = {
                "rank": rank,
                "point": self.georef.echo(row["x"], row["y"]),
                "tilt_deg": result["tilt_deg"],
                "azimuth_deg": result["azimuth_deg"],
                "annual": result["annual"],
                "climate": result["climate"],
                "horizon": row["horizon_meta"],
                "vegetation": row["vegetation"],
            }
            if demonstrate:
                rec["drawn"] = self.draw_point({"x": row["x"], "y": row["y"]},
                                               label=f"Solar #{rank}").get("drawn")
            sites.append(rec)
        return {
            "objective": objective,
            "surface": surface,
            "system_kw": float(system_kw or 0.0),
            "region": reg.describe(),
            "candidate_spacing_m": round(spacing, 3),
            "vegetation_policy": {
                "source": self._solar_vegetation_index().source,
                "available": self._solar_vegetation_index().available,
                "tree_count": len(self._solar_vegetation_index().records),
                "excluded_candidates": vegetation_excluded,
                "unknown_candidates": vegetation_unknown,
                "best_sites_require_installable_footprint": self._solar_vegetation_index().available,
                "clearance_radius_m": twin_solar.required_vegetation_clearance_radius_m(system_kw),
            },
            "recommended_sites": sites,
            "notes": [
                "PV yield is planning-grade, fixed-panel, PVWatts-style; not a bankable production guarantee.",
                "Recommended sites exclude vegetation-crown footprint conflicts when a vegetation inventory is available.",
                "Use surface='bare_earth' only for cleared/no-tree scenario planning; default canopy is the as-is conservative mode.",
            ],
        }

    def set_view_time(self, time, rate=1.0):
        """Set the viewer's shared astronomy clock through annotations.json.
        time='now' clears the directive so the browser returns to realtime."""
        try:
            payload = twin_astro.set_view_time_payload(time, rate=rate)
        except ValueError as error:
            self._astronomy_error(error)
        doc = _load_view_doc()
        if payload is None:
            doc["view_time"] = None
            _save_view_doc(doc)
            return {
                "view_time": None,
                "mode": "realtime",
                "note": "viewer astronomy clock returned to browser realtime",
                "provenance": twin_astro.provenance(),
            }
        payload = {**payload, "created_at": _utc_now()}
        doc["view_time"] = payload
        _save_view_doc(doc)
        return {
            "view_time": payload,
            "mode": "manual",
            "note": "viewer astronomy clock directive written; the browser poll applies it within a few seconds",
            "provenance": twin_astro.provenance(),
        }

    def _upsert_sky_view(self, directive):
        doc = _load_view_doc()
        views = [
            v for v in doc["sky_views"]
            if not (
                str(v.get("target_type", "")).lower() == directive["target_type"]
                and str(v.get("name", "")).lower() == directive["name"].lower()
            )
        ]
        views.append(directive)
        doc["sky_views"] = views
        _save_view_doc(doc)
        return views

    def highlight_sky(self, name, label=None):
        """Highlight one sky target in the live viewer: body, named star, or
        constellation. The highlight is presentation-only and never touches the
        twin store."""
        try:
            target = twin_astro.resolve_target(name)
        except twin_astro.AstronomyNameError as error:
            self._astronomy_error(error)
        target_type = target["target_type"]
        resolved_name = target.get("name") or target.get("abbr") or str(name)
        directive = {
            "target_type": target_type,
            "name": resolved_name,
            "label": _clean_label(label),
            "created_at": _utc_now(),
        }
        views = self._upsert_sky_view(directive)
        result = {
            "sky_view": directive,
            "sky_views_total": len(views),
            "resolved": {k: v for k, v in target.items() if k not in {"body", "star"}},
            "note": "sky highlight written; the browser poll applies it within a few seconds",
            "provenance": twin_astro.provenance(),
        }
        if target_type != "constellation":
            try:
                result["current_position"] = twin_astro.target_position(
                    target, time=None, site=self._astronomy_site())
            except (twin_astro.AstronomyNameError, ValueError) as error:
                self._astronomy_error(error)
        return result

    def clear_sky_highlights(self):
        """Clear only sky highlights; map drawings and layer-view overrides
        are left untouched."""
        doc = _load_view_doc()
        cleared = len(doc["sky_views"])
        doc["sky_views"] = []
        _save_view_doc(doc)
        return {
            "cleared": cleared,
            "note": "all sky highlights removed; drawings and layer views left untouched",
            "provenance": twin_astro.provenance(),
        }

    # -- map drawings (viewer annotations) -----------------------------------

    def _within_extent(self, xs, ys):
        try:
            minx, miny, maxx, maxy = self._extent()
        except Exception:
            return None
        return all(minx <= x <= maxx and miny <= y <= maxy
                   for x, y in zip(xs, ys))

    def draw_polygon(self, polygon, label=None):
        if (not isinstance(polygon, (list, tuple)) or len(polygon) < 3
                or not all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in polygon)):
            raise TwinQueryError(
                "polygon must be a list of at least 3 [lon,lat] or [x,y] vertex pairs",
                got=polygon)
        try:
            pts = [(float(p[0]), float(p[1])) for p in polygon]
        except (TypeError, ValueError):
            raise TwinQueryError("polygon vertices must be numbers", got=polygon)
        geographic = _looks_geographic(pts, self.georef)
        if geographic:
            pts = [self.georef.to_scene(lon, lat) for lon, lat in pts]
        if pts[0] == pts[-1]:
            pts = pts[:-1]  # store the open ring; the viewer closes it
        if len(pts) < 3:
            raise TwinQueryError("polygon needs at least 3 distinct vertices", got=polygon)
        pts = [(round(x, 2), round(y, 2)) for x, y in pts]

        doc = _load_view_doc()
        annotations = doc["annotations"]
        ann = {"id": _next_annotation_id(annotations), "type": "polygon",
               "label": _clean_label(label), "vertices": [[x, y] for x, y in pts],
               "created_at": _utc_now()}
        annotations.append(ann)
        _save_view_doc(doc)

        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        result = {
            "drawn": {"id": ann["id"], "type": "polygon", "label": ann["label"],
                      "vertex_count": len(pts),
                      "area_m2": round(shoelace_area(pts), 1),
                      "centroid": self.georef.echo(cx, cy),
                      "vertices_scene_m": ann["vertices"]},
            "annotations_total": len(annotations),
            "note": f"Polygon {_DRAWN_NOTE}",
        }
        inside = self._within_extent([p[0] for p in pts], [p[1] for p in pts])
        if inside is False:
            result["warning"] = "some vertices fall outside the twin's extent"
        return result

    def draw_point(self, point, label=None):
        x, y = resolve_point(point, self.georef)
        x, y = round(x, 2), round(y, 2)
        doc = _load_view_doc()
        annotations = doc["annotations"]
        ann = {"id": _next_annotation_id(annotations), "type": "point",
               "label": _clean_label(label), "x": x, "y": y,
               "created_at": _utc_now()}
        annotations.append(ann)
        _save_view_doc(doc)
        result = {
            "drawn": {"id": ann["id"], "type": "point", "label": ann["label"],
                      "position": self.georef.echo(x, y)},
            "annotations_total": len(annotations),
            "note": f"Point marker {_DRAWN_NOTE}",
        }
        if self._within_extent([x], [y]) is False:
            result["warning"] = "the point falls outside the twin's extent"
        return result

    def clear_drawings(self):
        doc = _load_view_doc()
        annotations = doc["annotations"]
        doc["annotations"] = []
        _save_view_doc(doc)
        return {"cleared": len(annotations),
                "note": "all drawings removed from the user's 3D map "
                        "(layer views and sky highlights left untouched — use "
                        "reset_layer_views / clear_sky_highlights for those)"}

    # -- layer views (atlas map-layer control) -------------------------------

    def _drape_layer(self, layer_id):
        """The atlas layer the agent can show/filter, or a structured error
        listing the drape-able ids."""
        layer = self._atlas_catalog().get(layer_id)
        if layer is None or layer.get("type") not in DRAPE_TYPES:
            raise TwinQueryError(
                f"unknown or non-drape-able layer_id: {layer_id!r}",
                valid_layer_ids=sorted(
                    l["id"] for l in self._atlas_layers()
                    if l.get("type") in DRAPE_TYPES))
        return layer

    def _filter_options(self, layer):
        """How a layer can be filtered: its drape `kind` and the values a
        filter may select (legend class names for rasters, modeled-habitat
        species for the GAP grid, per-attribute distinct values for vectors)."""
        lid = layer["id"]
        sg = self._species_grids()
        if lid == GAP_SPECIES_LAYER and sg:
            names = sorted({s.get("common_name") for s in sg["species"].values()
                            if s.get("common_name")})
            return {"kind": "species", "field": "species",
                    "fields": {"species": names}}
        data = self._layer_data(layer)
        if layer["type"] == "raster":
            legend = (data.get("grid") or {}).get("legend") or {}
            names = []
            for meta in legend.values():
                nm = (meta or {}).get("name")
                if nm and nm not in names:
                    names.append(nm)
            return {"kind": "raster", "field": "class", "fields": {"class": names}}
        fields = {}
        labels = []
        for f in data.get("features", []):
            props = f.get("properties") or {}
            # __label is the feature's friendly name and the primary filter
            # target; it lives in HIDE_PROPS (hidden from identify cards) so it
            # must be collected explicitly, not through the property loop below.
            lbl = props.get("__label")
            if lbl not in (None, "", " ") and str(lbl) not in labels:
                labels.append(str(lbl))
            for k, v in props.items():
                if k in HIDE_PROPS or v in (None, "", " "):
                    continue
                vals = fields.setdefault(k, [])
                if str(v) not in vals:
                    vals.append(str(v))
        fields["__label"] = labels
        return {"kind": "vector", "field": "__label", "fields": fields}

    def _upsert_layer_view(self, directive):
        doc = _load_view_doc()
        views = doc["layer_views"]
        views = [v for v in views if v.get("layer_id") != directive["layer_id"]]
        views.append(directive)
        doc["layer_views"] = views
        _save_view_doc(doc)
        return views

    def set_layer_visibility(self, layer_id, visible=True):
        """Show or hide one atlas map layer on the user's live 3D terrain,
        without filtering it."""
        layer = self._drape_layer(layer_id)
        directive = {"layer_id": layer_id, "visible": bool(visible),
                     "filter": None, "created_at": _utc_now()}
        views = self._upsert_layer_view(directive)
        verb = "shown on" if visible else "hidden from"
        return {
            "layer": {"id": layer_id, "label": layer.get("label"),
                      "type": layer.get("type")},
            "visible": bool(visible),
            "layer_views_total": len(views),
            "note": f"{layer.get('label', layer_id)} is now {verb} the user's "
                    "3D map. " + _LAYER_NOTE,
        }

    def filter_layer(self, layer_id, values, field=None):
        """Reveal only the selected features/regions of an atlas layer (and turn
        the layer on). values are legend class names for rasters, modeled-habitat
        species common-names for the GAP species grid, or — for vector layers —
        the distinct values of `field` (default the feature label). Everything
        else in the layer is hidden until the filter is cleared."""
        layer = self._drape_layer(layer_id)
        if not isinstance(values, (list, tuple)) or not values:
            raise TwinQueryError(
                "values must be a non-empty list of names/classes to reveal",
                got=values)
        values = [str(v) for v in values]
        opts = self._filter_options(layer)
        kind = opts["kind"]
        field = field or opts["field"]
        available = opts["fields"].get(field)
        if available is None:
            raise TwinQueryError(
                f"layer {layer_id!r} cannot be filtered on field {field!r}",
                filterable_fields=sorted(opts["fields"].keys()))
        by_lower = {a.lower(): a for a in available}
        matched, unmatched = [], []
        for v in values:
            hit = by_lower.get(v.lower())
            (matched if hit else unmatched).append(hit or v)
        if not matched:
            raise TwinQueryError(
                f"none of the requested values exist in {layer_id!r} "
                f"(field {field!r})",
                requested=values, available_values=available[:60])
        flt = {"field": field, "values": matched}
        directive = {"layer_id": layer_id, "visible": True, "filter": flt,
                     "kind": kind, "created_at": _utc_now()}
        views = self._upsert_layer_view(directive)
        result = {
            "layer": {"id": layer_id, "label": layer.get("label"),
                      "type": layer.get("type"), "filter_kind": kind},
            "filter": flt,
            "matched_values": matched,
            "layer_views_total": len(views),
            "note": f"{layer.get('label', layer_id)} now reveals only "
                    f"{', '.join(matched)} on the user's 3D map; everything else "
                    "in the layer is hidden. " + _LAYER_NOTE,
        }
        if unmatched:
            result["unmatched_values"] = unmatched
            result["warning"] = ("these values matched nothing in the layer and "
                                 "were ignored — see layer_summary for the valid "
                                 "names")
        return result

    def reset_layer_views(self):
        """Drop every agent layer override, returning the user's manual layer
        toggles to control. Leaves drawn polygons/points in place."""
        doc = _load_view_doc()
        views = doc["layer_views"]
        doc["layer_views"] = []
        _save_view_doc(doc)
        return {"cleared": len(views),
                "note": "all agent layer overrides removed; the user's manual "
                        "layer toggles are back in control"}

    # -------------------------------------------------------------- Plan / GAIA

    def _plan_engine(self):
        import plan_engine
        return plan_engine.PlanEngine(os.path.dirname(os.path.abspath(self._store_path)))

    @staticmethod
    def _run_plan_call(callable_):
        import plan_engine
        try:
            return callable_()
        except plan_engine.PlanError as exc:
            detail = dict(exc.payload)
            message = detail.pop("message", str(exc))
            code = detail.pop("error", "plan_error")
            raise TwinQueryError(message, code=code, **detail) from exc

    def _scene_coordinate_list(self, points, *, minimum, label):
        if not isinstance(points, list) or len(points) < minimum:
            raise TwinQueryError(f"{label} needs at least {minimum} points")
        if all(isinstance(point, dict) for point in points):
            return [[round(x, 3), round(y, 3)]
                    for x, y in (resolve_point(point, self.georef) for point in points)]
        if not all(isinstance(point, (list, tuple)) and len(point) >= 2 for point in points):
            raise TwinQueryError(
                f"{label} points must all be point objects or [x,y]/[lon,lat] pairs")
        try:
            pairs = [(float(point[0]), float(point[1])) for point in points]
        except (TypeError, ValueError) as exc:
            raise TwinQueryError(f"{label} coordinates must be numbers") from exc
        if _looks_geographic(pairs, self.georef):
            pairs = [self.georef.to_scene(lon, lat) for lon, lat in pairs]
        return [[round(x, 3), round(y, 3)] for x, y in pairs]

    def list_plans(self, include_archived=False):
        """List saved, branchable land plans and their current revisions."""
        return self._run_plan_call(
            lambda: self._plan_engine().list_plans(bool(include_archived)))

    def get_plan(self, plan_id, revision_id=None, materialize=False):
        """Inspect a plan, one reachable revision, its ancestry, edits and diff."""
        return self._run_plan_call(lambda: self._plan_engine().get_plan(
            plan_id, revision_id=revision_id, materialize=bool(materialize)))

    def planning_catalog(self):
        """Species/stage dimensions and swale/orchard/garden defaults."""
        import plan_catalog
        return plan_catalog.catalog(os.path.dirname(os.path.abspath(self._store_path)))

    def create_plan(self, name, author="GAIA"):
        """Create a new empty plan pinned to the current baseline twin."""
        return self._run_plan_call(
            lambda: self._plan_engine().create_plan(name, author=author))

    def branch_plan(self, source_plan_id, name, revision_id=None, author="GAIA"):
        """Create a new plan branch from a reachable immutable revision."""
        return self._run_plan_call(lambda: self._plan_engine().branch(
            source_plan_id, name, revision_id=revision_id, author=author))

    def save_plan_version(self, plan_id, expected_revision_id, name, author="GAIA"):
        """Create a named immutable checkpoint without changing the land."""
        return self._run_plan_call(lambda: self._plan_engine().checkpoint(
            plan_id, expected_revision_id, name, author=author))

    def propose_plan_edits(self, plan_id, edits, expected_revision_id=None,
                           replace=False, label=None, demonstrate=True,
                           author="GAIA"):
        """Validate edits and create a non-applied proposal for user review."""
        proposal = self._run_plan_call(lambda: self._plan_engine().propose(
            plan_id, edits, expected_revision_id=expected_revision_id,
            replace=bool(replace), label=label, author=author))
        if demonstrate:
            proposal["visualization"] = self.visualize_plan(
                plan_id, proposal_id=proposal["proposal_id"], view="difference")
        proposal["next_step"] = (
            "Show the preview and ask the user to confirm. Only then call "
            "apply_plan_proposal with confirmed=true.")
        return proposal

    def propose_vegetation_clearance(
            self, plan_id, target_entity_id, buffer_m=10.0, kinds=None,
            expected_revision_id=None, label=None, demonstrate=True,
            author="GAIA"):
        """Propose clearing effective vegetation around one mapped feature."""
        target_kind, geometry = self._entity_geometry_or_point(target_entity_id)
        if target_kind in {"tree", "shrub", "live_device"}:
            raise TwinQueryError(
                "vegetation clearance target must be a mapped land feature",
                target_entity_id=target_entity_id, target_kind=target_kind)
        try:
            buffer_m = float(buffer_m)
        except (TypeError, ValueError) as exc:
            raise TwinQueryError("buffer_m must be a positive number") from exc
        if not math.isfinite(buffer_m) or buffer_m <= 0:
            raise TwinQueryError("buffer_m must be a positive number",
                                 buffer_m=buffer_m)
        requested_kinds = ["tree"] if kinds is None else kinds
        if not isinstance(requested_kinds, (list, tuple)):
            raise TwinQueryError("kinds must be an array containing tree and/or shrub")
        requested_kinds = sorted({str(kind) for kind in requested_kinds
                                  if str(kind) in {"tree", "shrub"}})
        if not requested_kinds:
            raise TwinQueryError("kinds must contain tree and/or shrub")
        clean_label = (str(label).strip()[:200] if label
                       else f"Vegetation clearance near {target_kind}")
        proposal = self.propose_plan_edits(
            plan_id, [{
                "kind": "vegetation_remove",
                "geometry": geometry,
                "params": {
                    "buffer_m": min(500.0, buffer_m),
                    "kinds": requested_kinds,
                    "entity_ids": [],
                    "target_entity_id": str(target_entity_id),
                },
                "label": clean_label,
            }], expected_revision_id=expected_revision_id,
            label=clean_label, demonstrate=demonstrate, author=author)
        proposal["clearance_target"] = {
            "entity_id": str(target_entity_id), "kind": target_kind,
            "buffer_m": min(500.0, buffer_m),
            "vegetation_kinds": requested_kinds,
        }
        return proposal

    def propose_swale(self, plan_id, centerline, width_m=6.0, depth_m=0.35,
                      expected_revision_id=None, label="Swale",
                      demonstrate=True):
        """Propose a broad terrain depression along a scene/geographic line."""
        points = self._scene_coordinate_list(
            centerline, minimum=2, label="swale centerline")
        try:
            width = max(0.2, min(1000.0, float(width_m)))
            depth = max(0.0, min(30.0, float(depth_m)))
        except (TypeError, ValueError) as exc:
            raise TwinQueryError("swale width_m and depth_m must be numbers") from exc
        edit = {
            "kind": "swale",
            "geometry": {"type": "LineString", "coordinates": points},
            "params": {"width_m": width, "radius_m": width / 2.0,
                       "depth_m": depth, "falloff": "smoothstep"},
            "label": label,
        }
        return self.propose_plan_edits(
            plan_id, [edit], expected_revision_id=expected_revision_id,
            label=label, demonstrate=demonstrate)

    def _planning_species(self, species, stage=None, habit="tree"):
        catalog = self.planning_catalog()
        needle = str(species or "").strip().lower()
        rows = [item for item in catalog.get("species", [])
                if item.get("habit") == habit]
        if needle:
            row = next((item for item in rows
                        if str(item.get("id", "")).lower() == needle
                        or str(item.get("common_name", "")).lower() == needle), None)
        else:
            row = next((item for item in rows if "orchard" in (item.get("tags") or [])),
                       rows[0] if rows else None)
        if row is None:
            raise TwinQueryError("unknown planning species", species=species,
                                 valid_species=[item.get("id") for item in catalog.get("species", [])])
        if row.get("habit") != habit:
            raise TwinQueryError(f"{row.get('common_name')} is not cataloged as {habit}")
        stage_id = stage or row.get("default_stage")
        dimensions = (row.get("stages") or {}).get(stage_id)
        if not isinstance(dimensions, dict):
            raise TwinQueryError("unknown species stage", stage=stage_id,
                                 valid_stages=sorted((row.get("stages") or {}).keys()))
        return row, stage_id, dimensions

    def propose_orchard(self, plan_id, polygon, species=None,
                        spacing_m=None, stage=None, expected_revision_id=None,
                        label=None, demonstrate=True):
        """Propose deterministic tree placement throughout an orchard polygon."""
        ring = self._scene_coordinate_list(polygon, minimum=3, label="orchard polygon")
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        row, stage_id, dimensions = self._planning_species(species, stage, "tree")
        spacing = row.get("default_spacing_m", 6.0) if spacing_m is None else spacing_m
        edit = {
            "kind": "orchard",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "params": {
                "habit": "tree", "species": row["common_name"],
                "type": row.get("type", "deciduous"),
                "height": dimensions.get("height", 5),
                "radius": dimensions.get("radius", 2.5),
                "spacing_m": spacing, "stage": stage_id,
                "asset_key": row.get("asset_key"),
            },
            "label": label or f"{row['common_name']} orchard",
        }
        return self.propose_plan_edits(
            plan_id, [edit], expected_revision_id=expected_revision_id,
            label=edit["label"], demonstrate=demonstrate)

    def propose_garden(self, plan_id, polygon, height_m=0.25,
                       expected_revision_id=None, label="Garden",
                       demonstrate=True):
        """Propose a filled/raised garden footprint; crop yield is not modeled."""
        ring = self._scene_coordinate_list(polygon, minimum=3, label="garden polygon")
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        edit = {
            "kind": "garden",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "params": {"height_m": height_m, "edge_falloff_m": 1.0},
            "label": label,
        }
        return self.propose_plan_edits(
            plan_id, [edit], expected_revision_id=expected_revision_id,
            label=label, demonstrate=demonstrate)

    def apply_plan_proposal(self, proposal_id, confirmed=False, author="GAIA"):
        """Apply a reviewed proposal as a new immutable revision."""
        result = self._run_plan_call(lambda: self._plan_engine().apply_proposal(
            proposal_id, confirmed=confirmed, author=author))
        result["visualization"] = self.visualize_plan(
            result["plan"]["plan_id"],
            revision_id=result["revision"]["revision_id"], view="difference")
        return result

    def visualize_plan(self, plan_id, revision_id=None, proposal_id=None,
                       view="difference"):
        """Open the live viewer's Plan pane at a revision or proposal preview."""
        if view not in {"baseline", "planned", "difference"}:
            raise TwinQueryError("plan view must be baseline, planned, or difference")
        engine = self._plan_engine()
        proposal = None
        if proposal_id:
            proposal = self._run_plan_call(lambda: engine.get_proposal(proposal_id))
            if proposal.get("plan_id") != plan_id:
                raise TwinQueryError("proposal belongs to a different plan",
                                     proposal_id=proposal_id, plan_id=plan_id)
            if revision_id and revision_id != proposal.get("expected_revision_id"):
                raise TwinQueryError("proposal preview must use its expected base revision",
                                     proposal_id=proposal_id,
                                     expected_revision_id=proposal.get("expected_revision_id"))
            revision_id = proposal.get("expected_revision_id")
        selected = self._run_plan_call(
            lambda: engine.get_plan(plan_id, revision_id=revision_id))
        directive = {
            "plan_id": plan_id,
            "revision_id": selected["revision"]["revision_id"],
            "proposal_id": proposal_id,
            "preview_edits": proposal.get("proposed_edits", []) if proposal else [],
            "label": proposal.get("label") if proposal else selected["plan"]["name"],
            "view": "difference" if proposal else view,
            "created_at": _utc_now(),
        }
        doc = _load_view_doc()
        doc["plan_view"] = directive
        _save_view_doc(doc)
        return {
            "plan_id": plan_id,
            "revision_id": directive["revision_id"],
            "proposal_id": proposal_id,
            "view": directive["view"],
            "note": "The live 3D viewer is opening Plan and showing this land/proposal.",
        }

    def clear_plan_visualization(self):
        """Clear GAIA's Plan-pane directive without changing any saved plan."""
        doc = _load_view_doc()
        had_directive = bool(doc.get("plan_view"))
        doc["plan_view"] = None
        _save_view_doc(doc)
        return {"cleared": had_directive}

    def run_plan_simulation(self, plan_id, revision_id, simulator,
                            parameters=None):
        """Run a supported simulator against one plan's current immutable land."""
        return self._run_plan_call(lambda: self._plan_engine().run_simulation(
            plan_id, revision_id, simulator, parameters or {}))


# ----------------------------------------------------------- CLI for demos

def main(argv):
    """python3 scripts/twin_query.py <function> ['<json kwargs>'] — run one
    query function directly (used for demos; the MCP server is the product)."""
    if len(argv) < 2:
        names = [n for n in dir(TwinQuery) if not n.startswith("_")]
        print(f"usage: twin_query.py <function> ['<json kwargs>']\nfunctions: {names}")
        return 2
    fn_name = argv[1]
    kwargs = json.loads(argv[2]) if len(argv) > 2 else {}
    tq = TwinQuery()
    fn = getattr(tq, fn_name, None)
    if fn is None or fn_name.startswith("_"):
        print(f"unknown function: {fn_name}")
        return 2
    try:
        print(json.dumps(fn(**kwargs), indent=1, default=str))
    except TwinQueryError as e:
        print(json.dumps(e.payload, indent=1))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
