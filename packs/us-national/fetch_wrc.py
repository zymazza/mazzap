#!/usr/bin/env python3
"""Fetch WRC v2 national wildfire reference rasters for this twin's footprint.

The Wildfire Risk to Communities landscape-wide rasters are US-only, 30 m
reference products. They are atlas reference anchors, not ignition-sensitive
scenario outputs. This script downloads the ImageServer exportImage GeoTIFFs
for the twin terrain footprint, verifies they are readable rasters with data,
then delegates scene-local clipping/rendering/store registration to
scripts/add_layer.py.

Usage:
  python3 packs/us-national/fetch_wrc.py --data-dir ./data
"""

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))  # packs/<name>/ -> repo root
sys.path.insert(0, os.path.join(PROJECT, "scripts"))
import twin_georef  # noqa: E402
import twin_store  # noqa: E402

gdal.UseExceptions()

BASE = "https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation"
NATIVE_M = 30.0
PAD_M = 60.0
DESCRIPTION = (
    "Wildfire Risk to Communities (WRC) v2, published 2024: US-only 30 m "
    "landscape-wide reference raster. Burn probability is from FSim 270 m "
    "modeling upsampled to 30 m on LANDFIRE 2020 conditions. Reference anchor "
    "only; not ignition-sensitive."
)

PRODUCTS = [
    {
        "name": "USFS_EDW_RMRS_WRC_BurnProbability",
        "id": "wrc_burn_probability",
        "label": "WRC: Burn Probability",
    },
    {
        "name": "USFS_EDW_RMRS_WRC_ConditionalFlameLength",
        "id": "wrc_cond_flame_length",
        "label": "WRC: Conditional Flame Length",
    },
    {
        "name": "USFS_EDW_RMRS_WRC_FlameLengthExceedProb4ft",
        "id": "wrc_flep_4ft",
        "label": "WRC: Flame-Length Exceedance >4 ft",
    },
    {
        "name": "USFS_EDW_RMRS_WRC_FlameLengthExceedProb8ft",
        "id": "wrc_flep_8ft",
        "label": "WRC: Flame-Length Exceedance >8 ft",
    },
    {
        "name": "USFS_EDW_RMRS_WRC_WildfireHazardPotential",
        "id": "wrc_hazard_potential",
        "label": "WRC: Wildfire Hazard Potential",
    },
]


def _read_json(path):
    with open(path) as fh:
        return json.load(fh)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2)


def _service_url(product):
    return f"{BASE}/{product['name']}/ImageServer"


def _url_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "veil/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _fetch_metadata(service_url):
    return _url_json(service_url + "?f=json")


def _aoi(data_dir, resolution):
    georef_path = os.path.join(data_dir, "georef.json")
    grid = _read_json(os.path.join(data_dir, "terrain", "grid.json"))
    epsg = twin_georef.epsg_number(georef_path)
    ox, oy = twin_georef.origin(georef_path)
    x0 = grid["outerMinX"] + ox - PAD_M
    y0 = grid["outerMinY"] + oy - PAD_M
    x1 = grid["outerMaxX"] + ox + PAD_M
    y1 = grid["outerMaxY"] + oy + PAD_M
    w = max(2, int(math.ceil((x1 - x0) / resolution)))
    h = max(2, int(math.ceil((y1 - y0) / resolution)))
    return epsg, (x0, y0, x1, y1), (w, h)


def _export_url(service_url, bbox, epsg, size):
    params = {
        "bbox": "%f,%f,%f,%f" % bbox,
        "bboxSR": str(epsg),
        "size": "%d,%d" % size,
        "imageSR": str(epsg),
        "format": "tiff",
        "f": "image",
    }
    return service_url + "/exportImage?" + urllib.parse.urlencode(params)


def _download(url, out_path):
    req = urllib.request.Request(url, headers={"User-Agent": "veil/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read()
    if body[:2] not in (b"II", b"MM"):
        preview = body[:500].decode("utf-8", errors="replace")
        raise RuntimeError(f"export did not return TIFF bytes: {preview}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(body)


def _valid_data_array(ds):
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    mask = np.isfinite(arr)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != nodata
    vals = arr[mask]
    if vals.size == 0:
        raise RuntimeError("GeoTIFF has no finite non-nodata pixels")
    return arr, vals, nodata


def _verify_tiff(tif_path):
    ds = gdal.Open(tif_path)
    if ds is None or ds.RasterCount < 1:
        raise RuntimeError("GDAL could not open GeoTIFF")
    _arr, vals, nodata = _valid_data_array(ds)
    return {
        "width": int(ds.RasterXSize),
        "height": int(ds.RasterYSize),
        "dtype": gdal.GetDataTypeName(ds.GetRasterBand(1).DataType),
        "nodata": None if nodata is None else float(nodata),
        "valid_pixels": int(vals.size),
        "min": float(np.min(vals)),
        "max": float(np.max(vals)),
        "mean": float(np.mean(vals)),
        "std": float(np.std(vals)),
        "unique_count": int(np.unique(vals).size),
    }


def _grid_stats(data_dir, layer_id):
    grid_path = os.path.join(data_dir, "atlas", "local", layer_id + ".grid.json")
    grid = _read_json(grid_path)
    vals = np.array([
        v for row in grid.get("values", [])
        for v in row
        if v is not None and np.isfinite(v)
    ], dtype=float)
    if vals.size == 0:
        raise RuntimeError(f"localized grid has no valid cells: {grid_path}")
    return {
        "width": int(grid.get("width", 0)),
        "height": int(grid.get("height", 0)),
        "valid_pixels": int(vals.size),
        "min": float(np.min(vals)),
        "max": float(np.max(vals)),
        "mean": float(np.mean(vals)),
        "std": float(np.std(vals)),
        "unique_count": int(np.unique(vals).size),
    }


def _run_add_layer(data_dir, tif_path, product):
    cmd = [
        sys.executable,
        os.path.join(PROJECT, "scripts", "add_layer.py"),
        tif_path,
        "--id",
        product["id"],
        "--label",
        product["label"],
        "--src-crs",
        "EPSG:26918",
        "--data-dir",
        data_dir,
    ]
    return subprocess.run(cmd, cwd=PROJECT, text=True, capture_output=True, check=True)


def _patch_viewer_entry(data_dir, product, service_url, metadata):
    path = os.path.join(data_dir, "atlas", "local", "viewer-layers.json")
    catalog = _read_json(path)
    changed = False
    for layer in catalog.get("layers", []):
        if layer.get("id") != product["id"]:
            continue
        layer["acquisition"] = "api_snapshot"
        layer["service"] = service_url
        layer["description"] = DESCRIPTION
        layer["source"] = "USFS RMRS Wildfire Risk to Communities v2"
        layer["fetch_note"] = (
            "ArcGIS ImageServer exportImage snapshot in the twin analysis CRS; "
            "raw WRC values preserved."
        )
        if metadata.get("pixelType"):
            layer["pixel_type"] = metadata["pixelType"]
        changed = True
    if not changed:
        raise RuntimeError(f"{product['id']} missing from viewer-layers.json after ingest")
    _write_json(path, catalog)


def _patch_store(data_dir, product, service_url, tif_path):
    store_path = os.path.join(data_dir, "twin.gpkg")
    if not os.path.exists(store_path):
        print(f"  [warn] store missing, skipped provenance patch: {store_path}")
        return
    store = twin_store.Store(store_path)
    store.upsert_layer(
        product["id"],
        label=product["label"],
        kind="raster",
        acquisition="api_snapshot",
        service=service_url,
        source_path=os.path.abspath(tif_path),
        fetched_at=twin_store.utcnow(),
        feature_count=None,
        status="ok",
        content_sha1=twin_store.sha1_file(tif_path),
    )
    store.close()


def _png_nonempty(data_dir, layer_id):
    path = os.path.join(data_dir, "atlas", "local", layer_id + ".png")
    if not os.path.exists(path) or os.path.getsize(path) <= 0:
        raise RuntimeError(f"missing or empty drape PNG: {path}")
    return path


def _fetch_one(data_dir, bbox, epsg, size, product):
    layer_id = product["id"]
    service_url = _service_url(product)
    work_dir = os.path.join(data_dir, "atlas", "wrc")
    tif_path = os.path.join(work_dir, layer_id + ".tif")
    meta_path = os.path.join(work_dir, layer_id + ".metadata.json")

    print(f"\n[{layer_id}] metadata: {service_url}?f=json")
    metadata = _fetch_metadata(service_url)
    if "error" in metadata:
        raise RuntimeError(f"metadata error: {metadata['error']}")
    _write_json(meta_path, metadata)
    ranges = {
        "pixelType": metadata.get("pixelType"),
        "minValues": metadata.get("minValues"),
        "maxValues": metadata.get("maxValues"),
        "rasterFunctionInfos": [
            r.get("name") for r in metadata.get("rasterFunctionInfos", [])[:12]
            if isinstance(r, dict)
        ],
    }
    print("  encoding: " + json.dumps(ranges, separators=(",", ":")))

    url = _export_url(service_url, bbox, epsg, size)
    print(f"  download: {size[0]}x{size[1]} EPSG:{epsg}")
    _download(url, tif_path)
    source_stats = _verify_tiff(tif_path)
    print("  source range: min={min:.8g} max={max:.8g} std={std:.8g}".format(**source_stats))

    print("  ingest: scripts/add_layer.py")
    res = _run_add_layer(data_dir, tif_path, product)
    if res.stdout.strip():
        print("  " + res.stdout.strip().replace("\n", "\n  "))
    if res.stderr.strip():
        print("  [add_layer stderr] " + res.stderr.strip().replace("\n", "\n  "))

    _patch_viewer_entry(data_dir, product, service_url, metadata)
    _patch_store(data_dir, product, service_url, tif_path)
    _png_nonempty(data_dir, layer_id)
    localized_stats = _grid_stats(data_dir, layer_id)
    return {
        "id": layer_id,
        "label": product["label"],
        "service": service_url,
        "metadata": ranges,
        "source": source_stats,
        "localized": localized_stats,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--resolution", type=float, default=NATIVE_M,
                    help="export sample spacing in meters (default: 30 = WRC native)")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    epsg, bbox, size = _aoi(data_dir, args.resolution)
    print("fetching WRC v2 rasters for twin footprint "
          f"({size[0]}x{size[1]} @ {args.resolution:g} m, EPSG:{epsg})")
    print("bbox: %.3f,%.3f,%.3f,%.3f" % bbox)

    results = []
    failures = []
    for product in PRODUCTS:
        try:
            results.append(_fetch_one(data_dir, bbox, epsg, size, product))
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError,
                subprocess.CalledProcessError, OSError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"
            if isinstance(e, subprocess.CalledProcessError):
                msg += "\n" + (e.stderr or e.stdout or "")[-1200:]
            print(f"  [skip] {product['id']}: {msg}")
            failures.append({"id": product["id"], "error": msg})

    summary_path = os.path.join(data_dir, "atlas", "wrc", "fetch-summary.json")
    _write_json(summary_path, {
        "generated_by": "packs/us-national/fetch_wrc.py",
        "acquisition": "api_snapshot",
        "description": DESCRIPTION,
        "bbox_epsg": epsg,
        "bbox": bbox,
        "size": size,
        "layers": results,
        "failures": failures,
    })

    print("\nWRC fetch summary:")
    for r in results:
        s = r["localized"]
        uniform = " uniform" if s["unique_count"] <= 1 or abs(s["max"] - s["min"]) < 1e-12 else ""
        near_zero = " near-zero" if max(abs(s["min"]), abs(s["max"])) < 1e-6 else ""
        print("  {id}: localized min={min:.8g} max={max:.8g} "
              "std={std:.8g} valid={valid_pixels}{uniform}{near_zero}".format(
                  **r, **s, uniform=uniform, near_zero=near_zero))
    if failures:
        print("  skipped: " + ", ".join(f["id"] for f in failures))
    print(f"summary -> {summary_path}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
