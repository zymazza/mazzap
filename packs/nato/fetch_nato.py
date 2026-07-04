#!/usr/bin/env python3
"""Build a NATO-country twin from a country adapter.

Netherlands implemented path:
  1. AHN DTM -> adapter elevation prep -> scripts/ingest_dem.py
  2. AHN DSM/DTM -> adapter elevation prep -> aligned dtm.tif/dsm.tif for CHM vegetation
  3. PDOK RGB+CIR imagery -> scripts/ingest_imagery.py
  4. Copernicus HRL DLT -> scripts/add_layer.py as the leaf-type grid
  5. AHN CHM -> scripts/add_layer.py as a draped ecology layer
  6. TWIN_PACK=nato scripts/analyze_vegetation.py -> store + viewer exports

Usage:
  python3 packs/nato/fetch_nato.py --country NL \
      --aoi 174732.5,474346.5,175112.5,474726.5 \
      --data-dir twins/nl-speulderbos/data --resolution 0.5
"""

import argparse
import inspect
import importlib
import json
import os
import subprocess
import sys
import time

import numpy as np
from osgeo import gdal
from pyproj import Transformer

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(PROJECT, "scripts")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from adapters import AdapterUnavailable, StubAdapter, get_adapter  # noqa: E402
from adapters import eea as eea_leaf  # noqa: E402
import ingest_dem  # noqa: E402
import twin_georef  # noqa: E402
import twin_store  # noqa: E402

gdal.UseExceptions()
global_leaf = importlib.import_module("adapters.global")
IMPLEMENTED_NATIONAL = {
    "NL", "NO", "ES", "BE", "CZ", "DK", "EE", "FI", "FR", "LV", "LU",
    "PL", "SK", "SE",
}


def run(cmd, env=None):
    def show(c):
        return os.path.relpath(c, PROJECT) if c.startswith("/") and os.path.exists(c) else c

    print("  $", " ".join(show(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, env=env)


def _parse_bbox(text):
    cleaned = text.replace(",", " ").split()
    if len(cleaned) != 4:
        return None
    try:
        return tuple(float(v) for v in cleaned)
    except ValueError:
        return None


def _bbox_crs_for_values(bbox, explicit):
    if explicit:
        return explicit
    if -180 <= bbox[0] <= 180 and -180 <= bbox[2] <= 180 \
            and -90 <= bbox[1] <= 90 and -90 <= bbox[3] <= 90:
        return "EPSG:4326"
    return "EPSG:28992"


def _ring_from_bbox(bbox):
    x0, y0, x1, y1 = bbox
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def _write_geojson(path, ring, crs=None, properties=None):
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": properties or {},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }],
    }
    if crs:
        payload["crs"] = {"type": "name", "properties": {"name": crs}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(payload, open(path, "w"), indent=2)
    return path


def parse_aoi(aoi_arg, adapter, source_dir, aoi_crs=None):
    native_crs = getattr(adapter, "native_crs", "EPSG:4326")
    bbox = _parse_bbox(aoi_arg)
    if bbox is not None:
        src_crs = _bbox_crs_for_values(bbox, aoi_crs)
        if hasattr(adapter, "native_crs_for_aoi"):
            native_crs = adapter.native_crs_for_aoi({"bbox": bbox, "crs": src_crs})
        native_tag = native_crs.lower().replace(":", "")
        to_native = Transformer.from_crs(src_crs, native_crs, always_xy=True)
        native_pts = [to_native.transform(x, y) for x, y in _ring_from_bbox(bbox)]
        xs, ys = zip(*native_pts)
        bbox_native = (min(xs), min(ys), max(xs), max(ys))
        to_wgs = Transformer.from_crs(native_crs, "EPSG:4326", always_xy=True)
        wgs_ring = [list(to_wgs.transform(x, y)) for x, y in native_pts]
        native_ring = [[float(x), float(y)] for x, y in native_pts]
        native_path = _write_geojson(
            os.path.join(source_dir, f"aoi_{native_tag}.geojson"),
            native_ring,
            crs=native_crs,
            properties={"source": "fetch_nato.py --aoi bbox"},
        )
        wgs_path = _write_geojson(
            os.path.join(source_dir, "aoi_wgs84.geojson"),
            wgs_ring,
            properties={"source": "fetch_nato.py --aoi bbox"},
        )
        out = {
            "kind": "bbox",
            "input": aoi_arg,
            "input_bbox": bbox,
            "input_crs": src_crs,
            "bbox_native": bbox_native,
            "native_crs": native_crs,
            "aoi_native": native_path,
            "aoi_wgs84": wgs_path,
            "bbox_wgs84": (min(p[0] for p in wgs_ring), min(p[1] for p in wgs_ring),
                           max(p[0] for p in wgs_ring), max(p[1] for p in wgs_ring)),
        }
        if native_crs.upper() in ("EPSG:28992", "28992"):
            out["bbox_28992"] = bbox_native
            out["aoi_28992"] = native_path
        return out

    if not os.path.exists(aoi_arg):
        raise SystemExit(
            "--aoi must be either 'minx,miny,maxx,maxy' or a GeoJSON/Shapefile/GPKG path"
        )
    ring, src_crs = ingest_dem.ring_from_aoi(aoi_arg, aoi_crs or "EPSG:4326")
    xs0, ys0 = zip(*ring)
    if hasattr(adapter, "native_crs_for_aoi"):
        native_crs = adapter.native_crs_for_aoi({
            "bbox": (min(xs0), min(ys0), max(xs0), max(ys0)),
            "crs": src_crs,
        })
    native_tag = native_crs.lower().replace(":", "")
    to_native = Transformer.from_crs(src_crs, native_crs, always_xy=True)
    native_ring = [list(to_native.transform(x, y)) for x, y in ring]
    xs, ys = zip(*native_ring)
    bbox_native = (min(xs), min(ys), max(xs), max(ys))
    to_wgs = Transformer.from_crs(native_crs, "EPSG:4326", always_xy=True)
    wgs_ring = [list(to_wgs.transform(x, y)) for x, y in native_ring]
    native_path = _write_geojson(
        os.path.join(source_dir, f"aoi_{native_tag}.geojson"),
        native_ring,
        crs=native_crs,
        properties={"source": os.path.abspath(aoi_arg)},
    )
    wgs_path = _write_geojson(
        os.path.join(source_dir, "aoi_wgs84.geojson"),
        wgs_ring,
        properties={"source": os.path.abspath(aoi_arg)},
    )
    out = {
        "kind": "file",
        "input": os.path.abspath(aoi_arg),
        "input_crs": src_crs,
        "bbox_native": bbox_native,
        "native_crs": native_crs,
        "aoi_native": native_path,
        "aoi_wgs84": wgs_path,
        "bbox_wgs84": (min(p[0] for p in wgs_ring), min(p[1] for p in wgs_ring),
                       max(p[0] for p in wgs_ring), max(p[1] for p in wgs_ring)),
    }
    if native_crs.upper() in ("EPSG:28992", "28992"):
        out["bbox_28992"] = bbox_native
        out["aoi_28992"] = native_path
    return out


def _footprint_abs(data_dir):
    georef_path = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    ox, oy = twin_georef.origin(georef_path)
    return (
        grid["outerMinX"] + ox,
        grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox,
        grid["outerMaxY"] + oy,
    )


def _seed_store(data_dir, adapter, source_manifest=None):
    georef = json.load(open(os.path.join(data_dir, "georef.json")))
    scene_path = os.path.join(data_dir, "scene.json")
    scene = json.load(open(scene_path)) if os.path.exists(scene_path) else {}
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")
    store = twin_store.Store(os.path.join(data_dir, "twin.gpkg"))
    try:
        store.set_meta("schema_version", twin_store.SCHEMA_VERSION)
        store.set_meta("origin_utm", georef["origin_utm"])
        store.set_meta("crs", {
            "analysis_crs": georef["analysis_crs"],
            "convention": "store coordinates are scene-local meters: x=easting-origin, y=northing-origin",
        })
        store.set_meta("scene_template", scene)
        store.set_meta("source_manifest", source_manifest or {})
        run_id = store.begin_run("packs/nato/fetch_nato.py")
        for layer_id, rel, kind, label in [
            ("terrain_grid", "terrain/grid.json", "terrain_grid", "Terrain grid"),
            ("terrain_dtm", "terrain/dtm.tif", "elevation", "Terrain DTM/terrain surface aligned to grid"),
            ("terrain_dsm", "terrain/dsm.tif", "elevation", "DSM/canopy surface aligned to grid"),
            ("terrain_chm", "terrain/chm.tif", "derived", "Canopy height model"),
            ("imagery_drape", "imagery/drape.png", "imagery", "RGB imagery drape"),
            ("imagery_false_color", "imagery/false_color.png", "imagery", "NIR false-color imagery"),
            ("imagery_naip_rgb", "imagery/naip_rgb.png", "imagery", "RGB imagery"),
        ]:
            path = os.path.join(data_dir, rel)
            if os.path.exists(path):
                store.upsert_layer(
                    layer_id,
                    label=label,
                    kind=kind,
                    acquisition="nato_adapter",
                    service=adapter.name,
                    source_path="data/" + rel,
                    fetched_at=_file_mtime_utc(path),
                    content_sha1=twin_store.sha1_file(path),
                    status="ok",
                )
        store.finish_run(run_id, notes="seeded store metadata and NATO source layers")
    finally:
        store.close()


def _file_mtime_utc(path):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))


def _write_pack_marker(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "pack.txt"), "w") as fh:
        fh.write("nato\n")


def _copy_attribution(data_dir, adapter, forest_type=None, extra_layers=None):
    path = os.path.join(data_dir, "attribution.json")
    attribution = list(adapter.attribution())
    if forest_type:
        attribution.extend(forest_type.get("attribution") or [])
    for layer in extra_layers or []:
        attribution.extend(layer.get("attribution") or [])
    payload = {
        "pack": "nato",
        "country": adapter.alpha3,
        "attribution": attribution,
        "provenance": {
            **adapter.provenance(),
            "forest_type": (forest_type or {}).get("metadata", {}),
            "context_layers": [(layer or {}).get("metadata", {}) for layer in extra_layers or []],
        },
    }
    json.dump(payload, open(path, "w"), indent=2)
    return path


def _chm_stats(chm_path):
    if not os.path.exists(chm_path):
        return {}
    arr = gdal.Open(chm_path).ReadAsArray().astype(float)
    vals = arr[np.isfinite(arr)]
    if vals.size == 0:
        return {}
    return {
        "mean": round(float(vals.mean()), 2),
        "p90": round(float(np.percentile(vals, 90)), 2),
        "max": round(float(vals.max()), 2),
        "canopy_cover_gt3_pct": round(100.0 * float((vals > 3.0).mean()), 1),
    }


def _summary(data_dir):
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    heights = grid.get("heights", [])
    null_count = sum(1 for v in heights if v is None or not np.isfinite(v))
    height_count = len(heights)
    trees_path = os.path.join(data_dir, "vegetation", "tree_instances.json")
    trees = json.load(open(trees_path)) if os.path.exists(trees_path) else []
    type_dist = {}
    for tree in trees:
        typ = tree.get("type") or "unknown"
        type_dist[typ] = type_dist.get(typ, 0) + 1
    meta_path = os.path.join(data_dir, "vegetation", "metadata.json")
    veg_meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    return {
        "grid": {"width": grid["width"], "height": grid["height"],
                 "xStep": grid.get("xStep"), "yStep": grid.get("yStep"),
                 "null_height_count": null_count, "height_count": height_count,
                 "null_height_pct": round(100.0 * null_count / height_count, 6)
                 if height_count else 0.0},
        "tree_count": len(trees),
        "tree_type_distribution": dict(sorted(type_dist.items())),
        "vegetation_metadata": veg_meta,
        "chm": _chm_stats(os.path.join(data_dir, "terrain", "chm.tif")),
    }


def _fetch_forest_type(adapter, aoi, source_dir, data_dir):
    alpha2 = getattr(adapter, "alpha2", "nato")
    if eea_leaf.is_eea_covered(alpha2):
        print("  source: Copernicus HRL Dominant Leaf Type 2018 (EEA, 10 m)")
        return eea_leaf.fetch_leaf_type(aoi, source_dir, data_dir, alpha2=alpha2)
    print("  source: CGLS-LC100 forest type, with ESA WorldCover mask fallback")
    return global_leaf.fetch_leaf_type(aoi, source_dir, data_dir, alpha2=alpha2)


def _fetch_context_layers(adapter, aoi, source_dir, data_dir, tier):
    alpha2 = getattr(adapter, "alpha2", "nato")
    layers = []
    if tier in ("auto", "continental", "global") and eea_leaf.is_eea_covered(alpha2):
        print("  source: EEA/CLMS continental context layers (CLC+ and Natura 2000, optional)")
        layers.extend(eea_leaf.fetch_continental_layers(aoi, source_dir, data_dir, alpha2=alpha2))
    return layers


def _prepare_chm_inputs(adapter, data_dir, elevation, resolution, forest_type=None):
    kwargs = {"resolution": resolution}
    if "forest_type" in inspect.signature(adapter.prepare_chm_inputs).parameters:
        kwargs["forest_type"] = forest_type
    return adapter.prepare_chm_inputs(data_dir, elevation, **kwargs)


def _add_source_layer(layer, data_dir):
    if not layer:
        return
    path = layer.get("path") or layer.get("raster")
    if not path:
        return
    env = dict(os.environ, TWIN_DATA_DIR=data_dir, TWIN_PACK="nato")
    run([
        sys.executable, os.path.join(SCRIPTS, "add_layer.py"), path,
        "--id", layer["layer_id"],
        "--label", layer["label"],
        "--description", layer["description"],
        "--uses", layer["uses"],
        "--value-kind", layer["value_kind"],
        "--value-unit", layer["value_unit"],
        "--value-classification", layer["value_classification"],
        "--data-dir", data_dir,
    ], env=env)


def _add_canopy_layer(chm_inputs, adapter, data_dir):
    env = dict(os.environ, TWIN_DATA_DIR=data_dir, TWIN_PACK="nato")
    alpha2 = getattr(adapter, "alpha2", "nato").lower()
    is_global = "ETH Global Canopy" in (chm_inputs.get("metadata", {}).get("source") or "")
    layer_id = chm_inputs.get("layer_id") or (
        f"{alpha2}_eth_chm" if is_global else f"{alpha2}_ahn_chm"
    )
    label = chm_inputs.get("layer_label") or (
        "ETH Canopy Height" if is_global else "AHN Canopy Height"
    )
    description = chm_inputs.get("layer_description") or (
        "Canopy height model from ETH Global Canopy Height 2020."
        if is_global else
        "Canopy height model derived from AHN DSM minus DTM."
    )
    uses = "Forest structure, canopy inspection, and vegetation QA."
    run([
        sys.executable, os.path.join(SCRIPTS, "add_layer.py"), chm_inputs["chm"],
        "--id", layer_id,
        "--label", label,
        "--description", description,
        "--uses", uses,
        "--value-kind", "canopy height",
        "--value-unit", "m",
        "--value-classification", "continuous",
        "--data-dir", data_dir,
    ], env=env)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--country", required=True, help="NATO country code, e.g. NL or NLD")
    ap.add_argument("--aoi", required=True, help="bbox 'minx,miny,maxx,maxy' or AOI file")
    ap.add_argument("--aoi-crs", help="CRS for bbox AOIs or AOI files without CRS")
    ap.add_argument("--data-dir", required=True, help="output twin data dir")
    ap.add_argument("--name", default="NATO twin", help="twin display name")
    ap.add_argument("--resolution", type=float, default=None, help="terrain/CHM resolution in meters")
    ap.add_argument("--tier", choices=("auto", "national", "continental", "global"),
                    default="auto",
                    help="source tier: national when implemented, or global/continental fallback")
    ap.add_argument("--imagery-px-per-m", type=int, default=None,
                    help="integer drape pixels per meter (default: round(1/resolution))")
    ap.add_argument("--no-imagery", action="store_true", help="skip imagery")
    ap.add_argument("--no-layer", action="store_true", help="skip canopy-height atlas layer")
    ap.add_argument("--no-vegetation", action="store_true", help="skip vegetation analysis")
    ap.add_argument("--force", action="store_true", help="overwrite existing terrain genesis outputs")
    args = ap.parse_args()

    try:
        country_adapter = get_adapter(args.country)
    except (KeyError, AdapterUnavailable) as exc:
        raise SystemExit(str(exc)) from exc

    force_fallback = args.tier in ("continental", "global")
    use_global = force_fallback or isinstance(country_adapter, StubAdapter)
    if args.tier == "national" and isinstance(country_adapter, StubAdapter):
        raise SystemExit(
            f"{args.country} has no implemented national adapter in packs/nato."
        )
    if use_global:
        fallback_tier = args.tier if args.tier != "auto" else (
            "continental" if eea_leaf.is_eea_covered(country_adapter.alpha2) else "global"
        )
        adapter = global_leaf.GlobalFallbackAdapter(country_adapter, requested_tier=fallback_tier)
        print(
            f"fallback: {country_adapter.name} ({country_adapter.alpha3}) has no national "
            f"build in this pack or fallback was requested; using {fallback_tier} sources",
            flush=True,
        )
    else:
        adapter = country_adapter
        if getattr(adapter, "alpha2", None) not in IMPLEMENTED_NATIONAL:
            raise SystemExit(
                f"{args.country} is registered, but only {', '.join(sorted(IMPLEMENTED_NATIONAL))} national adapters "
                "are implemented now. Use --tier global or --tier continental for fallback."
            )

    resolution = args.resolution or getattr(adapter, "default_resolution", 30.0)

    data_dir = os.path.abspath(args.data_dir)
    source_dir = os.path.join(data_dir, "source", "nato", adapter.alpha2.lower())
    os.makedirs(source_dir, exist_ok=True)
    aoi = parse_aoi(args.aoi, adapter, source_dir, aoi_crs=args.aoi_crs)
    coverage = adapter.coverage(aoi)
    if "bbox_28992" in coverage:
        print("AOI:", tuple(round(v, 3) for v in coverage["bbox_28992"]),
              f"{coverage['area_ha']} ha in {adapter.native_crs}", flush=True)
    elif "bbox_native" in coverage and "area_ha" in coverage:
        print("AOI:", tuple(round(v, 3) for v in coverage["bbox_native"]),
              f"{coverage['area_ha']} ha in {coverage.get('crs', aoi['native_crs'])}",
              flush=True)
    else:
        print("AOI:", tuple(round(v, 6) for v in coverage["bbox_wgs84"]),
              f"{coverage.get('area_ha_approx')} ha approx in EPSG:4326", flush=True)

    total_steps = 7 - (1 if args.no_imagery else 0) - (1 if args.no_layer else 0) \
        - (1 if args.no_vegetation else 0)
    step = 1

    print(f"\n[{step}/{total_steps}] fetching terrain source...", flush=True)
    elevation = adapter.fetch_elevation(aoi, source_dir, resolution=resolution)
    step += 1

    print(f"\n[{step}/{total_steps}] ingesting terrain DEM...", flush=True)
    cmd = [
        sys.executable, os.path.join(SCRIPTS, "ingest_dem.py"),
        elevation.get("dtm") or elevation.get("terrain"),
        "--name", args.name,
        "--data-dir", data_dir,
        "--resolution", str(resolution),
    ]
    if aoi["kind"] == "bbox":
        cmd += ["--bbox"] + [str(v) for v in aoi["bbox_native"]] + [
            "--bbox-crs", aoi.get("native_crs", adapter.native_crs)
        ]
    else:
        cmd += ["--aoi", aoi["aoi_native"], "--aoi-crs",
                aoi.get("native_crs", adapter.native_crs)]
    if args.force:
        cmd.append("--force")
    run(cmd)
    _write_pack_marker(data_dir)
    step += 1

    forest_type = None
    context_layers = []
    forest_layers_ready = False
    if isinstance(adapter, global_leaf.GlobalFallbackAdapter):
        print(f"\n[{step}/{total_steps}] fetching forest leaf-type/context layers...", flush=True)
        forest_type = _fetch_forest_type(adapter, aoi, source_dir, data_dir)
        context_layers = _fetch_context_layers(adapter, aoi, source_dir, data_dir,
                                               getattr(adapter, "requested_tier", args.tier))
        forest_layers_ready = True
        step += 1

    print(f"\n[{step}/{total_steps}] placing DSM/DTM for CHM vegetation...", flush=True)
    chm_inputs = _prepare_chm_inputs(adapter, data_dir, elevation, resolution,
                                     forest_type=forest_type)
    step += 1

    imagery = None
    if not args.no_imagery:
        print(f"\n[{step}/{total_steps}] fetching RGB+NIR imagery...", flush=True)
        ppm = args.imagery_px_per_m or max(1, int(round(1.0 / resolution)))
        imagery = adapter.fetch_imagery(aoi, source_dir, _footprint_abs(data_dir), px_per_m=ppm)
        run([
            sys.executable, os.path.join(SCRIPTS, "ingest_imagery.py"), imagery["rgbn"],
            "--data-dir", data_dir,
            "--px-per-m", str(ppm),
        ])
        step += 1

    if not forest_layers_ready:
        print(f"\n[{step}/{total_steps}] fetching forest leaf-type/context layers...", flush=True)
        forest_type = _fetch_forest_type(adapter, aoi, source_dir, data_dir)
        context_layers = _fetch_context_layers(adapter, aoi, source_dir, data_dir,
                                               getattr(adapter, "requested_tier", args.tier))
        step += 1

    manifest = {
        "pack": "nato",
        "country": adapter.alpha3,
        "source_tier": getattr(adapter, "requested_tier", "national"),
        "national_adapter_used": not use_global,
        "coverage": coverage,
        "aoi": aoi,
        "elevation": elevation.get("metadata", {}),
        "chm_inputs": chm_inputs.get("metadata", {}),
        "imagery": (imagery or {}).get("metadata", {}),
        "forest_type": (forest_type or {}).get("metadata", {}),
        "context_layers": [(layer or {}).get("metadata", {}) for layer in context_layers],
        "attribution": (
            adapter.attribution()
            + ((forest_type or {}).get("attribution") or [])
            + [a for layer in context_layers for a in (layer.get("attribution") or [])]
        ),
    }
    json.dump(manifest, open(os.path.join(source_dir, "source_manifest.json"), "w"), indent=2)
    _copy_attribution(data_dir, adapter, forest_type=forest_type, extra_layers=context_layers)
    _seed_store(data_dir, adapter, source_manifest=manifest)
    _add_source_layer(forest_type, data_dir)
    for layer in context_layers:
        _add_source_layer(layer, data_dir)

    if not args.no_layer:
        print(f"\n[{step}/{total_steps}] adding canopy-height atlas layer...", flush=True)
        _add_canopy_layer(chm_inputs, adapter, data_dir)
        step += 1

    if not args.no_vegetation:
        print(f"\n[{step}/{total_steps}] building vegetation (NATO typing)...", flush=True)
        env = dict(os.environ, TWIN_PACK="nato", TWIN_DATA_DIR=data_dir)
        run([sys.executable, os.path.join(SCRIPTS, "analyze_vegetation.py"),
             "--data-dir", data_dir], env=env)

    summary = _summary(data_dir)
    json.dump(summary, open(os.path.join(data_dir, "build_summary.json"), "w"), indent=2)
    rel_data = os.path.relpath(data_dir, PROJECT)
    print("\nDone. Summary:")
    print("  grid: {width}x{height} @ {xStep:g} m".format(**summary["grid"]))
    print("  grid null heights: {null_height_count}/{height_count} ({null_height_pct:.3f}%)"
          .format(**summary["grid"]))
    print(f"  trees: {summary['tree_count']}")
    print(f"  tree types: {summary['tree_type_distribution']}")
    if summary["chm"]:
        print("  CHM: mean {mean} m, p90 {p90} m, max {max} m, cover>3m {canopy_cover_gt3_pct}%"
              .format(**summary["chm"]))
    print(f"  data: {rel_data}")
    print(f"  serve: TWIN_DATA_DIR={rel_data} PORT=4180 HOST=127.0.0.1 node server.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
