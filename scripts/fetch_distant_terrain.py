#!/usr/bin/env python3
"""Fetch and materialize distant terrain rings for viewshed analysis.

The output contract is intentionally the one read by ``twin_viewshed.RingStack``:
raw tiled Int16 ground decimetres and optional UInt8 EVH canopy decimetres under
``data/terrain/distant/`` plus one manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_georef
import twin_store
import twin_viewshed


AGL_MAX_M = 120.0
NODATA_I16 = twin_viewshed.NODATA_I16
TILE_SIZE = 256
RING_B_OUTER_M = 24_000.0
RING_B_INNER_M = 300.0
REGIONAL_PROBE_RADIUS_M = 250_000.0
USGS_3DEP = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation"
LANDFIRE_EVH = "https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2024/LF2024_EVH_CONUS/ImageServer/exportImage"
NAIP_PLUS = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage"
HTTP_HEADERS = {"User-Agent": "veil/1.0"}
NAIP_JPEG_QUALITY = 85


gdal.UseExceptions()
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
gdal.SetConfigOption("VSI_CACHE", "TRUE")
gdal.SetConfigOption("VSI_CACHE_SIZE", str(128 * 1024 * 1024))


@dataclass
class RingDef:
    rid: str
    product: str
    resolution_m: float
    inner_m: float
    outer_m: float
    resample: str
    canopy: bool

    @property
    def bounds_local(self) -> tuple[float, float, float, float]:
        return (-self.outer_m, -self.outer_m, self.outer_m, self.outer_m)

    @property
    def width(self) -> int:
        return int(round((self.outer_m * 2.0) / self.resolution_m)) + 1

    @property
    def height(self) -> int:
        return self.width


def radius_max_km(scene_min: float, scene_max: float, regional_max: float) -> dict[str, float]:
    h_rel = max(0.0, scene_max - scene_min) + AGL_MAX_M
    h_target = max(0.0, regional_max - scene_min)
    r = 3.86 * (math.sqrt(h_rel) + math.sqrt(h_target))
    return {"h_rel_m": h_rel, "H_rel_m": h_target, "R_max_km": r}


def read_json(path: str) -> Any:
    with open(path) as fh:
        return json.load(fh)


def ensure_evh_vat(data_dir: str) -> str:
    """Return the LANDFIRE EVH VAT sidecar, fetching it if this twin lacks one."""
    vat_dir = os.path.join(data_dir, "atlas", "vat")
    path = os.path.join(vat_dir, "landfire_evh_2024.json")
    if os.path.exists(path):
        return path

    pack_dir = os.path.join(PROJECT, "packs", "us-national")
    if pack_dir not in sys.path:
        sys.path.insert(0, pack_dir)
    try:
        import build_landfire_vat  # type: ignore
    except Exception as exc:  # noqa: BLE001 - keep the actionable context.
        raise FileNotFoundError(
            f"missing {path}; could not import packs/us-national/build_landfire_vat.py"
        ) from exc

    product = next((p for p in build_landfire_vat.LANDFIRE_PRODUCTS
                    if p.get("id") == "landfire_evh_2024"), None)
    if not product:
        raise RuntimeError("packs/us-national/build_landfire_vat.py has no landfire_evh_2024 product")
    status = build_landfire_vat.fetch_vat(product, vat_dir)
    if status.get("vat") == "ok" and os.path.exists(path):
        return path
    detail = status.get("vat_error") or status.get("vat") or "unknown error"
    raise RuntimeError(f"could not fetch LANDFIRE EVH VAT sidecar for viewshed canopy decode: {detail}")


def rel(path: str, data_dir: str) -> str:
    return os.path.relpath(path, data_dir).replace(os.sep, "/")


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def clean_ring_dir(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def local_to_projected_bbox(bounds_local: tuple[float, float, float, float],
                            origin_xy: tuple[float, float],
                            half_pixel: float = 0.0) -> tuple[float, float, float, float]:
    ox, oy = origin_xy
    minx, miny, maxx, maxy = bounds_local
    return (
        ox + minx - half_pixel,
        oy + miny - half_pixel,
        ox + maxx + half_pixel,
        oy + maxy + half_pixel,
    )


def tile_edge_bounds_local(ring: RingDef, rows: slice, cols: slice) -> tuple[float, float, float, float]:
    minx, _miny, _maxx, maxy = ring.bounds_local
    x_step = (ring.outer_m * 2.0) / max(1, ring.width - 1)
    y_step = (ring.outer_m * 2.0) / max(1, ring.height - 1)
    west = minx + int(cols.start) * x_step - x_step / 2.0
    east = minx + (int(cols.stop) - 1) * x_step + x_step / 2.0
    north = maxy - int(rows.start) * y_step + y_step / 2.0
    south = maxy - (int(rows.stop) - 1) * y_step - y_step / 2.0
    return west, south, east, north


def projected_bbox_to_lonlat_bbox(bounds_local: tuple[float, float, float, float],
                                  origin_xy: tuple[float, float],
                                  georef_path: str) -> tuple[float, float, float, float]:
    to_geo, _ = twin_georef.transformers(georef_path)
    ox, oy = origin_xy
    minx, miny, maxx, maxy = bounds_local
    pts: list[tuple[float, float]] = []
    for t in np.linspace(0.0, 1.0, 9):
        pts.extend([
            (minx + (maxx - minx) * t, miny),
            (minx + (maxx - minx) * t, maxy),
            (minx, miny + (maxy - miny) * t),
            (maxx, miny + (maxy - miny) * t),
        ])
    lonlat = [to_geo.transform(ox + x, oy + y) for x, y in pts]
    lons, lats = zip(*lonlat)
    return min(lons), min(lats), max(lons), max(lats)


def tile_code_from_edges(south_edge: int, west_edge: int) -> str:
    ns = f"n{south_edge + 1:02d}" if south_edge >= 0 else f"s{abs(south_edge):02d}"
    ew = f"w{abs(west_edge):03d}" if west_edge < 0 else f"e{west_edge:03d}"
    return ns + ew


def tile_codes_for_lonlat_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    minlon, minlat, maxlon, maxlat = bbox
    south_edges = range(math.floor(minlat), math.floor(maxlat) + 1)
    west_edges = range(math.floor(minlon), math.floor(maxlon) + 1)
    return [tile_code_from_edges(s, w) for s in south_edges for w in west_edges]


def usgs_url(product: str, tile_code: str) -> str:
    return f"/vsicurl/{USGS_3DEP}/{product}/TIFF/current/{tile_code}/USGS_{product}_{tile_code}.tif"


def available_usgs_urls(product: str, bbox: tuple[float, float, float, float]) -> list[str]:
    urls = []
    for code in tile_codes_for_lonlat_bbox(bbox):
        url = usgs_url(product, code)
        try:
            ds = gdal.OpenEx(url, gdal.OF_RASTER)
        except RuntimeError:
            ds = None
        if ds is not None:
            urls.append(url)
            ds = None
    if not urls:
        raise RuntimeError(f"no USGS 3DEP product {product!r} tiles opened for bbox {bbox}")
    return urls


def warp_urls_to_array(urls: list[str], dst_crs: str, bounds_local: tuple[float, float, float, float],
                       origin_xy: tuple[float, float], width: int, height: int,
                       resolution_m: float, resample: str) -> np.ndarray:
    vrt_path = f"/vsimem/viewshed_{sha1_text('|'.join(urls))}.vrt"
    vrt = gdal.BuildVRT(vrt_path, urls)
    if vrt is None:
        raise RuntimeError("could not build 3DEP VRT")
    bounds = local_to_projected_bbox(bounds_local, origin_xy, resolution_m / 2.0)
    opts = gdal.WarpOptions(
        format="MEM",
        dstSRS=dst_crs,
        outputBounds=bounds,
        width=width,
        height=height,
        resampleAlg=resample,
        srcNodata=-999999.0,
        dstNodata=-9999.0,
        outputType=gdal.GDT_Float32,
        multithread=True,
        warpOptions=["NUM_THREADS=ALL_CPUS"],
    )
    ds = gdal.Warp("", vrt, options=opts)
    vrt = None
    try:
        gdal.Unlink(vrt_path)
    except RuntimeError:
        pass
    if ds is None:
        raise RuntimeError("GDAL Warp returned no dataset")
    arr = ds.ReadAsArray().astype(np.float32)
    arr[arr <= -9998.0] = np.nan
    return arr


def annulus_mask(shape: tuple[int, int], bounds_local: tuple[float, float, float, float],
                 inner_m: float, outer_m: float, pad_m: float) -> np.ndarray:
    h, w = shape
    minx, miny, maxx, maxy = bounds_local
    xs = np.linspace(minx, maxx, w, dtype=np.float32)
    ys = np.linspace(maxy, miny, h, dtype=np.float32)
    dist = np.hypot(xs[None, :], ys[:, None])
    mask = dist <= outer_m + pad_m
    if inner_m > 0:
        mask &= dist >= inner_m - pad_m
    return mask


def materialize_ground(ring: RingDef, data_dir: str, georef_path: str,
                       origin_xy: tuple[float, float], dst_crs: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    bbox = projected_bbox_to_lonlat_bbox(ring.bounds_local, origin_xy, georef_path)
    urls = available_usgs_urls(ring.product, bbox)
    raw = warp_urls_to_array(
        urls, dst_crs, ring.bounds_local, origin_xy,
        ring.width, ring.height, ring.resolution_m, ring.resample,
    )
    arr = raw.copy()
    arr[~annulus_mask(arr.shape, ring.bounds_local, ring.inner_m, ring.outer_m, ring.resolution_m * 0.75)] = np.nan
    return arr, raw, urls


def write_ground_float_tif(arr: np.ndarray, ring: RingDef, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(path, ring.width, ring.height, 1, gdal.GDT_Float32,
                                              options=["TILED=YES", "COMPRESS=LZW"])
    minx, _miny, _maxx, maxy = ring.bounds_local
    ds.SetGeoTransform((minx, ring.resolution_m, 0.0, maxy, 0.0, -ring.resolution_m))
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(np.where(np.isfinite(arr), arr, -9999.0).astype(np.float32))
    ds = None


def probe_regional_max(data_dir: str, georef_path: str, origin_xy: tuple[float, float],
                       dst_crs: str) -> dict[str, Any]:
    bounds_local = (
        -REGIONAL_PROBE_RADIUS_M, -REGIONAL_PROBE_RADIUS_M,
        REGIONAL_PROBE_RADIUS_M, REGIONAL_PROBE_RADIUS_M,
    )
    resolution_m = 1000.0
    width = int(round((REGIONAL_PROBE_RADIUS_M * 2.0) / resolution_m)) + 1
    bbox = projected_bbox_to_lonlat_bbox(bounds_local, origin_xy, georef_path)
    urls = available_usgs_urls("1", bbox)
    arr = warp_urls_to_array(urls, dst_crs, bounds_local, origin_xy, width, width, resolution_m, "max")
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        raise RuntimeError("regional max probe produced no finite 3DEP cells")
    return {
        "radius_km": REGIONAL_PROBE_RADIUS_M / 1000.0,
        "resolution_m": resolution_m,
        "bbox_lonlat": [round(float(v), 6) for v in bbox],
        "tile_count": len(urls),
        "max_elevation_m": float(np.max(finite)),
    }


def fetch_landfire_evh_codes(ring: RingDef, origin_xy: tuple[float, float], epsg: int) -> np.ndarray:
    bounds = local_to_projected_bbox(ring.bounds_local, origin_xy, ring.resolution_m / 2.0)
    params = {
        "bbox": "%f,%f,%f,%f" % bounds,
        "bboxSR": str(epsg),
        "imageSR": str(epsg),
        "size": f"{ring.width},{ring.height}",
        "format": "tiff",
        "pixelType": "U16",
        "interpolation": "RSP_NearestNeighbor",
        "f": "json",
    }
    meta_url = LANDFIRE_EVH + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(meta_url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        meta = json.loads(resp.read().decode("utf-8"))
    href = meta.get("href")
    if not href:
        raise RuntimeError(f"LANDFIRE EVH exportImage returned no href: {meta!r}")
    url = "/vsicurl/" + href
    ds = gdal.Open(url)
    if ds is None:
        raise RuntimeError("LANDFIRE EVH exportImage did not return a raster")
    arr = ds.ReadAsArray()
    if arr.ndim != 2:
        raise RuntimeError(f"LANDFIRE EVH returned unexpected array shape {arr.shape}")
    return arr.astype(np.int32)


def write_image_bytes_as_jpeg(data: bytes, out_path: str, tag: str) -> bool:
    if data.startswith(b"\xff\xd8"):
        with open(out_path, "wb") as fh:
            fh.write(data)
        return True

    mem_path = f"/vsimem/naip_{tag}"
    try:
        gdal.FileFromMemBuffer(mem_path, data)
        ds = gdal.Open(mem_path)
        if ds is None or ds.RasterCount < 3:
            return False
        jpeg = gdal.GetDriverByName("JPEG")
        out = jpeg.CreateCopy(out_path, ds, options=[f"QUALITY={NAIP_JPEG_QUALITY}"])
        out = None
        ds = None
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    finally:
        try:
            gdal.Unlink(mem_path)
        except RuntimeError:
            pass


def fetch_naip_tile_jpeg(ring: RingDef, rows: slice, cols: slice,
                         origin_xy: tuple[float, float], epsg: int,
                         out_path: str) -> int:
    row_count = int(rows.stop) - int(rows.start)
    col_count = int(cols.stop) - int(cols.start)
    if row_count <= 0 or col_count <= 0:
        return 0
    bounds = local_to_projected_bbox(tile_edge_bounds_local(ring, rows, cols), origin_xy, 0.0)
    params = {
        "bbox": "%f,%f,%f,%f" % bounds,
        "bboxSR": str(epsg),
        "imageSR": str(epsg),
        "size": f"{col_count},{row_count}",
        "format": "jpg",
        "pixelType": "U8",
        "interpolation": "RSP_BilinearInterpolation",
        "compressionQuality": str(NAIP_JPEG_QUALITY),
        "f": "image",
    }
    url = NAIP_PLUS + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    tag = sha1_text(url)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
    except Exception as exc:
        print(f"warning: NAIPPlus imagery unavailable for ring {ring.rid} "
              f"tile rows {rows.start}:{rows.stop} cols {cols.start}:{cols.stop}: {exc}",
              file=sys.stderr)
        return 0
    if not data or "json" in content_type.lower() or data.lstrip().startswith(b"{"):
        print(f"warning: NAIPPlus returned no image for ring {ring.rid} "
              f"tile rows {rows.start}:{rows.stop} cols {cols.start}:{cols.stop}",
              file=sys.stderr)
        return 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        ok = write_image_bytes_as_jpeg(data, out_path, tag)
    except Exception as exc:
        print(f"warning: NAIPPlus image decode failed for ring {ring.rid} "
              f"tile rows {rows.start}:{rows.stop} cols {cols.start}:{cols.stop}: {exc}",
              file=sys.stderr)
        ok = False
    if not ok:
        print(f"warning: NAIPPlus response was not usable imagery for ring {ring.rid} "
              f"tile rows {rows.start}:{rows.stop} cols {cols.start}:{cols.stop}",
              file=sys.stderr)
        try:
            os.remove(out_path)
        except OSError:
            pass
        return 0
    return os.path.getsize(out_path)


def decode_evh_to_dm(codes: np.ndarray, vat_path: str,
                     valid_mask: np.ndarray | None = None) -> np.ndarray:
    mapping = twin_viewshed._decode_evh_vat(read_json(vat_path))
    metres = np.zeros(codes.shape, dtype=np.float32)
    for code, height_m in mapping.items():
        if height_m > 0:
            metres[codes == int(code)] = float(height_m)
    if valid_mask is not None:
        metres[~valid_mask] = 0.0
    return np.clip(np.rint(metres * 10.0), 0, 255).astype(np.uint8)


def ground_to_dm(arr: np.ndarray) -> np.ndarray:
    out = np.full(arr.shape, NODATA_I16, dtype="<i2")
    valid = np.isfinite(arr)
    clipped = np.clip(np.rint(arr[valid] * 10.0), -32767, 32767)
    out[valid] = clipped.astype("<i2")
    return out


def aoi_geometry(data_dir: str) -> dict[str, Any]:
    path = os.path.join(data_dir, "terrain", "aoi_local.geojson")
    return read_json(path)


def polygon_rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    if not geometry:
        return []
    if geometry.get("type") == "Polygon":
        return geometry.get("coordinates", [])
    if geometry.get("type") == "MultiPolygon":
        rings: list[list[list[float]]] = []
        for poly in geometry.get("coordinates", []):
            rings.extend(poly)
        return rings
    return []


def feature_rings(fc: dict[str, Any]) -> list[list[list[float]]]:
    rings: list[list[list[float]]] = []
    for feature in fc.get("features", []):
        rings.extend(polygon_rings(feature.get("geometry") or {}))
    return rings


def point_in_ring(pt: tuple[float, float], ring: list[list[float]]) -> bool:
    x, y = pt
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and x < ((xj - xi) * (y - yi)) / max(1e-12, (yj - yi)) + xi:
            inside = not inside
        j = i
    return inside


def point_in_aoi(pt: tuple[float, float], rings: list[list[list[float]]]) -> bool:
    # The AOI file uses exteriors first; holes are rare here. Treat any ring hit
    # as inside, which is conservative for placing union-sweep sample points.
    return any(point_in_ring(pt, ring) for ring in rings)


def ring_length(ring: list[list[float]]) -> float:
    total = 0.0
    for a, b in zip(ring, ring[1:]):
        total += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
    return total


def point_on_ring(ring: list[list[float]], distance_m: float) -> tuple[float, float]:
    remaining = distance_m
    for a, b in zip(ring, ring[1:]):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        seg = math.hypot(bx - ax, by - ay)
        if seg <= 0:
            continue
        if remaining <= seg:
            t = remaining / seg
            return ax + (bx - ax) * t, ay + (by - ay) * t
        remaining -= seg
    p = ring[-1]
    return float(p[0]), float(p[1])


def aoi_sample_points(data_dir: str, stack: twin_viewshed.RingStack) -> list[tuple[float, float]]:
    rings = [r for r in feature_rings(aoi_geometry(data_dir)) if len(r) >= 4]
    if not rings:
        return [twin_viewshed.nearest_valid_point(stack)]
    exterior = [r if r[0] == r[-1] else r + [r[0]] for r in rings]
    lengths = [ring_length(r) for r in exterior]
    total_len = sum(lengths)
    boundary: list[tuple[float, float]] = []
    for k in range(32):
        d = (k / 32.0) * total_len
        for ring, length in zip(exterior, lengths):
            if d <= length:
                boundary.append(point_on_ring(ring, d))
                break
            d -= length

    coords = [(float(p[0]), float(p[1])) for ring in exterior for p in ring[:-1]]
    xs, ys = zip(*coords)
    centroid = (sum(xs) / len(xs), sum(ys) / len(ys))
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    interior: list[tuple[float, float]] = []
    for fy in (0.25, 0.5, 0.75):
        for fx in (0.25, 0.5, 0.75):
            p = (minx + (maxx - minx) * fx, miny + (maxy - miny) * fy)
            for _ in range(12):
                if point_in_aoi(p, rings):
                    break
                p = ((p[0] + centroid[0]) * 0.5, (p[1] + centroid[1]) * 0.5)
            interior.append(p if point_in_aoi(p, rings) else centroid)

    out: list[tuple[float, float]] = []
    for x, y in boundary + interior:
        g = stack.sample_components(np.asarray([x]), np.asarray([y]))[0][0]
        if not np.isfinite(g):
            x, y = twin_viewshed.nearest_valid_point(stack, (x, y))
        out.append((float(x), float(y)))
    return out


def write_mask_artifacts(data_dir: str, stack: twin_viewshed.RingStack,
                         union: dict[str, np.ndarray]) -> tuple[str, dict[str, Any]]:
    out_dir = os.path.join(data_dir, "viewshed", "aoi_union_mask")
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    for ring in stack.rings:
        mask = union.get(ring.name)
        if mask is None:
            continue
        name = f"ring{ring.name}.bin"
        path = os.path.join(out_dir, name)
        mask.astype(np.uint8).tofile(path)
        valid = np.isfinite(ring.ground)
        entries.append({
            "id": ring.name,
            "width": ring.width,
            "height": ring.height,
            "bounds_local": [ring.min_x, ring.min_y, ring.max_x, ring.max_y],
            "resolution_m": ring.resolution_m,
            "path": rel(path, data_dir),
            "visible_cells": int(np.count_nonzero(mask & valid)),
            "valid_cells": int(np.count_nonzero(valid)),
        })

    manifest = {
        "version": 1,
        "kind": "viewshed_aoi_union_mask",
        "surface": "canopy",
        "observer_agl_m": AGL_MAX_M,
        "rings": entries,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)

    # Small standard drape for the local ring so the union truth is visible in
    # the existing raster-overlay system without trying to JSON-encode ring C.
    local = next((r for r in stack.rings if r.name == "A"), stack.rings[0])
    local_mask = union[local.name]
    local_dir = os.path.join(data_dir, "viewshed", "local")
    os.makedirs(local_dir, exist_ok=True)
    png_path = os.path.join(local_dir, "aoi_union_mask.png")
    grid_path = os.path.join(local_dir, "aoi_union_mask.grid.json")
    rgba = np.zeros((local_mask.shape[0], local_mask.shape[1], 4), dtype=np.uint8)
    rgba[local_mask > 0] = [255, 142, 38, 185]
    mem = gdal.GetDriverByName("MEM").Create("", local_mask.shape[1], local_mask.shape[0], 4, gdal.GDT_Byte)
    for b in range(4):
        mem.GetRasterBand(b + 1).WriteArray(rgba[:, :, b])
    gdal.GetDriverByName("PNG").CreateCopy(png_path, mem)
    aux = png_path + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)
    with open(grid_path, "w") as fh:
        json.dump({
            "bounds_local": [local.min_x, local.min_y, local.max_x, local.max_y],
            "width": local.width,
            "height": local.height,
            "nodata": None,
            "values": [[int(v) for v in row] for row in local_mask],
            "legend": {
                "0": {"name": "not visible from AOI sample set"},
                "1": {"name": "visible from at least one AOI sample", "color": [255, 142, 38]},
            },
        }, fh, separators=(",", ":"))
    return manifest_path, manifest


def write_ring_tiles(ring: RingDef, ground_dm: np.ndarray, canopy_dm: np.ndarray | None,
                     union_mask: np.ndarray, data_dir: str,
                     origin_xy: tuple[float, float], epsg: int,
                     imagery_accessed_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ring_dir = os.path.join(data_dir, "terrain", "distant", f"ring{ring.rid}")
    clean_ring_dir(ring_dir)
    tiles = []
    full_tile_count = int(math.ceil(ring.width / TILE_SIZE) * math.ceil(ring.height / TILE_SIZE))
    before_bytes = int(ring.width * ring.height * 2 + (ring.width * ring.height if canopy_dm is not None else 0))
    after_bytes = 0
    imagery_bytes = 0
    imagery_missing = 0
    for j in range(math.ceil(ring.height / TILE_SIZE)):
        for i in range(math.ceil(ring.width / TILE_SIZE)):
            rows = slice(j * TILE_SIZE, min(ring.height, (j + 1) * TILE_SIZE))
            cols = slice(i * TILE_SIZE, min(ring.width, (i + 1) * TILE_SIZE))
            valid_visible = (union_mask[rows, cols] > 0) & (ground_dm[rows, cols] != NODATA_I16)
            if not np.any(valid_visible):
                continue
            ground_path = os.path.join(ring_dir, f"tile_{i}_{j}.bin")
            ground_dm[rows, cols].astype("<i2", copy=False).tofile(ground_path)
            after_bytes += os.path.getsize(ground_path)
            canopy_rel = None
            if canopy_dm is not None:
                canopy_path = os.path.join(ring_dir, f"tile_{i}_{j}_evh.bin")
                canopy_dm[rows, cols].astype(np.uint8, copy=False).tofile(canopy_path)
                after_bytes += os.path.getsize(canopy_path)
                canopy_rel = rel(canopy_path, data_dir)
            imagery_rel = None
            imagery_path = os.path.join(ring_dir, f"tile_{i}_{j}.jpg")
            fetched = fetch_naip_tile_jpeg(ring, rows, cols, origin_xy, epsg, imagery_path)
            if fetched > 0:
                imagery_bytes += fetched
                imagery_rel = rel(imagery_path, data_dir)
            else:
                imagery_missing += 1
            tiles.append({
                "i": i,
                "j": j,
                "ground": rel(ground_path, data_dir),
                "canopy": canopy_rel,
                "imagery": imagery_rel,
            })

    entry = {
        "id": ring.rid,
        "width": ring.width,
        "height": ring.height,
        "tile_size": TILE_SIZE,
        "bounds_local": [float(v) for v in ring.bounds_local],
        "resolution_m": ring.resolution_m,
        "inner_m": ring.inner_m,
        "outer_m": ring.outer_m,
        "tiles": tiles,
        "ground_source": f"USGS 3DEP {ring.product} staged COG via /vsicurl/",
        "canopy_source": "LANDFIRE 2024 EVH" if canopy_dm is not None else None,
        "canopy_available": canopy_dm is not None,
        "imagery_source": "USGS NAIPPlus ImageServer exportImage",
        "imagery_service": NAIP_PLUS,
        "imagery_date": imagery_accessed_at,
        "imagery_resolution_m": ring.resolution_m,
        "imagery_format": f"JPEG quality {NAIP_JPEG_QUALITY}",
        "imagery_available": imagery_bytes > 0,
        "imagery_tile_count": sum(1 for tile in tiles if tile.get("imagery")),
        "imagery_missing_tile_count": imagery_missing,
        "imagery_mb": round(imagery_bytes / (1024 * 1024), 3),
    }
    stats = {
        "id": ring.rid,
        "width": ring.width,
        "height": ring.height,
        "tiles_before_clip": full_tile_count,
        "tiles_after_clip": len(tiles),
        "tiles_with_imagery": sum(1 for tile in tiles if tile.get("imagery")),
        "imagery_missing_tiles": imagery_missing,
        "mb_before_clip": round(before_bytes / (1024 * 1024), 3),
        "ground_canopy_mb_after_clip": round(after_bytes / (1024 * 1024), 3),
        "imagery_mb_after_clip": round(imagery_bytes / (1024 * 1024), 3),
        "mb_after_clip": round((after_bytes + imagery_bytes) / (1024 * 1024), 3),
    }
    return entry, stats


def local_ring_entry(data_dir: str, grid: dict[str, Any]) -> dict[str, Any]:
    apron = os.path.join(data_dir, "terrain", "grid.apron.json")
    evh = os.path.join(data_dir, "atlas", "local", "landfire_evh_2024.grid.json")
    vat = os.path.join(data_dir, "atlas", "vat", "landfire_evh_2024.json")
    has_apron = os.path.exists(apron)
    has_canopy = os.path.exists(evh) and os.path.exists(vat)
    entry = {
        "id": "A",
        "kind": "local_grid",
        "ground_grid": "terrain/grid.apron.json" if has_apron else "terrain/grid.json",
        "resolution_m": min(float(grid["xStep"]), float(grid["yStep"])),
        "bounds_local": [grid["minX"], grid["minY"], grid["maxX"], grid["maxY"]],
        "inner_m": 0,
        "outer_m": max(abs(grid["minX"]), abs(grid["maxX"]), abs(grid["minY"]), abs(grid["maxY"])) * math.sqrt(2),
        "ground_source": "local terrain grid",
        "canopy_available": has_canopy,
    }
    if has_apron:
        entry["parcel_grid"] = "terrain/grid.json"
    if has_canopy:
        entry["canopy_grid"] = "atlas/local/landfire_evh_2024.grid.json"
        entry["canopy_vat"] = "atlas/vat/landfire_evh_2024.json"
        entry["canopy_source"] = "LANDFIRE EVH 2024 atlas grid"
    return entry


def register_store(data_dir: str, manifest_path: str, summary_path: str, union_manifest_path: str) -> None:
    store_path = os.path.join(data_dir, "twin.gpkg")
    if not os.path.exists(store_path):
        return
    twin_store.DATA_DIR = data_dir
    twin_store.STORE_PATH = store_path
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")
    store = twin_store.Store(twin_store.STORE_PATH)
    run = store.begin_run("fetch_distant_terrain.py", inputs=[manifest_path, union_manifest_path],
                          notes="viewshed distant-terrain B/C materialization")
    for layer_id, path, label, kind in (
        ("viewshed_distant_manifest", manifest_path, "Viewshed distant terrain manifest", "viewshed_manifest"),
        ("viewshed_distant_summary", summary_path, "Viewshed distant terrain summary", "viewshed_summary"),
        ("viewshed_aoi_union_mask", union_manifest_path, "AOI cumulative viewshed mask", "viewshed_mask"),
    ):
        store.upsert_layer(
            layer_id,
            label=label,
            kind=kind,
            acquisition="derived",
            service=None,
            source_path=rel(path, data_dir),
            fetched_at=twin_store.utcnow(),
            feature_count=None,
            status="ok",
            content_sha1=twin_store.sha1_file(path),
        )
    manifest = read_json(manifest_path)
    fetched_at = twin_store.utcnow()
    for ring in manifest.get("rings", []):
        rid = ring.get("id")
        if not rid or ring.get("kind"):
            continue
        for tile in ring.get("tiles", []):
            imagery_rel = tile.get("imagery")
            if not imagery_rel:
                continue
            imagery_path = os.path.join(data_dir, imagery_rel)
            if not os.path.exists(imagery_path):
                continue
            store.upsert_layer(
                f"viewshed_distant_imagery_{rid}_{tile.get('i')}_{tile.get('j')}",
                label=f"Viewshed distant imagery ring {rid} tile {tile.get('i')},{tile.get('j')}",
                kind="raster_imagery",
                acquisition="USGS NAIPPlus exportImage",
                service=NAIP_PLUS,
                source_path=imagery_rel,
                fetched_at=fetched_at,
                feature_count=None,
                status="ok",
                content_sha1=twin_store.sha1_file(imagery_path),
            )
    store.finish_run(run, notes="viewshed distant terrain and AOI union mask written")
    store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--union-n-az", type=int, default=1440)
    args = ap.parse_args()

    data = os.path.abspath(args.data_dir)
    georef_path = os.path.join(data, "georef.json")
    grid_path = os.path.join(data, "terrain", "grid.json")
    grid = read_json(grid_path)
    dst_crs = twin_georef.crs(georef_path)
    epsg = twin_georef.epsg_number(georef_path)
    origin_xy = twin_georef.origin(georef_path)
    vat_path = ensure_evh_vat(data)
    imagery_accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    scene_min = float(grid["minElevation"])
    scene_max = float(grid["maxElevation"])
    print("probing regional max from real 3DEP 1 arc-second tiles...", file=sys.stderr)
    probe = probe_regional_max(data, georef_path, origin_xy, dst_crs)
    regional_max = float(probe["max_elevation_m"])
    radius = radius_max_km(scene_min, scene_max, regional_max)
    ring_c_outer = math.ceil(radius["R_max_km"] * 1000.0 / 150.0) * 150.0
    rings = [
        RingDef("B", "13", 30.0, RING_B_INNER_M, RING_B_OUTER_M, "bilinear", True),
        RingDef("C", "1", 150.0, RING_B_OUTER_M, ring_c_outer, "max", False),
    ]

    materialized: dict[str, dict[str, Any]] = {}
    ring_entries: list[dict[str, Any]] = [local_ring_entry(data, grid)]
    ring_stats: list[dict[str, Any]] = []

    for ring in rings:
        print(f"materializing ring {ring.rid} ground from USGS 3DEP product {ring.product}...", file=sys.stderr)
        ground_m, ground_raw_m, urls = materialize_ground(ring, data, georef_path, origin_xy, dst_crs)
        validation_ground = None
        if ring.rid == "B":
            validation_ground = os.path.join(data, "terrain", "distant", "ringB_ground_full.tif")
            write_ground_float_tif(ground_raw_m, ring, validation_ground)
        canopy_dm = None
        if ring.canopy:
            print(f"fetching ring {ring.rid} LANDFIRE EVH canopy...", file=sys.stderr)
            evh_codes = fetch_landfire_evh_codes(ring, origin_xy, epsg)
            canopy_dm = decode_evh_to_dm(evh_codes, vat_path, np.isfinite(ground_m))
        materialized[ring.rid] = {
            "def": ring,
            "ground_m": ground_m,
            "ground_dm": ground_to_dm(ground_m),
            "canopy_dm": canopy_dm,
            "source_tiles": urls,
            "validation_ground": validation_ground,
        }

    pre_manifest = {
        "version": 1,
        "status": "pre_clip",
        "rings": [
            ring_entries[0],
            *[
                {
                    "id": info["def"].rid,
                    "width": info["def"].width,
                    "height": info["def"].height,
                    "tile_size": TILE_SIZE,
                    "bounds_local": [float(v) for v in info["def"].bounds_local],
                    "resolution_m": info["def"].resolution_m,
                    "inner_m": info["def"].inner_m,
                    "outer_m": info["def"].outer_m,
                    "tiles": [],
                }
                for info in materialized.values()
            ],
        ],
    }
    temp_manifest = os.path.join(data, "terrain", "distant", "manifest.pre_clip.json")
    os.makedirs(os.path.dirname(temp_manifest), exist_ok=True)
    with open(temp_manifest, "w") as fh:
        json.dump(pre_manifest, fh)

    # Build an in-memory stack with all un-clipped arrays for the AOI union.
    local_stack = twin_viewshed.RingStack.from_local_files(data)
    stack_rings = [local_stack.rings[0]]
    for rid in ("B", "C"):
        info = materialized[rid]
        ring = info["def"]
        minx, miny, maxx, maxy = ring.bounds_local
        canopy = None if info["canopy_dm"] is None else info["canopy_dm"].astype(np.float32) / 10.0
        stack_rings.append(twin_viewshed.Ring(
            rid, info["ground_m"], minx, maxx, miny, maxy, ring.resolution_m,
            canopy=canopy,
            inner_m=ring.inner_m,
            outer_m=ring.outer_m,
            source={"ground_source": f"USGS 3DEP {ring.product}", "canopy_available": canopy is not None},
        ))
    stack = twin_viewshed.RingStack(stack_rings, manifest=pre_manifest)
    points = aoi_sample_points(data, stack)
    print(f"running AOI union sweep from {len(points)} points at {AGL_MAX_M:g} m AGL...", file=sys.stderr)
    union = twin_viewshed.union_sweep(stack, points, AGL_MAX_M, n_az=args.union_n_az, surface="canopy")

    for rid in ("B", "C"):
        info = materialized[rid]
        entry, stats = write_ring_tiles(
            info["def"], info["ground_dm"], info["canopy_dm"], union[rid],
            data, origin_xy, epsg, imagery_accessed_at,
        )
        entry["source_tiles"] = [u.replace("/vsicurl/", "") for u in info["source_tiles"]]
        if info.get("validation_ground"):
            entry["validation_ground_full"] = rel(info["validation_ground"], data)
        ring_entries.append(entry)
        ring_stats.append(stats)

    imagery_stats = {
        "source": "USGS NAIPPlus ImageServer exportImage",
        "service": NAIP_PLUS,
        "license": "USGS NAIP public domain, CONUS",
        "accessed_at": imagery_accessed_at,
        "source_date": "NAIPPlus mosaic acquisition dates vary by source scene; export date recorded in accessed_at.",
        "format": "JPEG",
        "jpeg_quality": NAIP_JPEG_QUALITY,
        "tile_alignment": "Each JPEG uses the kept tile's pixel-edge bounds and the same rows/cols as its ground grid.",
        "rings": {
            stats["id"]: {
                "resolution_m": next(r["imagery_resolution_m"] for r in ring_entries if r.get("id") == stats["id"]),
                "tiles_with_imagery": stats["tiles_with_imagery"],
                "missing_tiles": stats["imagery_missing_tiles"],
                "mb": stats["imagery_mb_after_clip"],
            }
            for stats in ring_stats
        },
        "tiles_with_imagery": int(sum(stats["tiles_with_imagery"] for stats in ring_stats)),
        "missing_tiles": int(sum(stats["imagery_missing_tiles"] for stats in ring_stats)),
        "mb": round(sum(stats["imagery_mb_after_clip"] for stats in ring_stats), 3),
    }

    manifest = {
        "version": 1,
        "status": "ok",
        "imagery": imagery_stats,
        "r_max_inputs": {
            "scene_min_m": scene_min,
            "scene_max_m": scene_max,
            "regional_max_m": regional_max,
            "regional_probe": probe,
            "agl_max_m": AGL_MAX_M,
            **radius,
        },
        "rings": ring_entries,
    }
    manifest_path = os.path.join(data, "terrain", "distant", "manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)

    loaded = twin_viewshed.RingStack.load(manifest_path)
    obs = twin_viewshed.nearest_valid_point(loaded)
    smoke = twin_viewshed.sweep(loaded, obs[0], obs[1], AGL_MAX_M, n_az=360, surface="canopy")
    union_manifest_path, union_manifest = write_mask_artifacts(data, loaded, union)

    summary = {
        "manifest": rel(manifest_path, data),
        "status": "ok",
        "r_max_km": radius["R_max_km"],
        "ring_c_outer_km": ring_c_outer / 1000.0,
        "regional_max_m": regional_max,
        "regional_probe": probe,
        "imagery": imagery_stats,
        "aoi_union": {
            "sample_points": len(points),
            "agl_m": AGL_MAX_M,
            "n_az": args.union_n_az,
            "mask_manifest": rel(union_manifest_path, data),
            "rings": union_manifest["rings"],
        },
        "rings": ring_stats,
        "round_trip_smoke": {
            "observer": [round(obs[0], 3), round(obs[1], 3)],
            "visible_km2": smoke["stats"]["visible_km2"],
            "analyzed_extent_km": smoke["stats"]["analyzed_extent_km"],
            "per_ring": smoke["stats"]["per_ring"],
        },
    }
    summary_path = os.path.join(data, "terrain", "distant", "summary.json")
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    try:
        os.remove(temp_manifest)
    except OSError:
        pass
    register_store(data, manifest_path, summary_path, union_manifest_path)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
