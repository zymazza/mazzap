#!/usr/bin/env python3
"""Vegetation analysis — a capability ladder that degrades gracefully.

Stem detection uses the best signal available, in order:
  1. LiDAR segmentation stems (data/vegetation/tree_instances.lidar.json) —
     stems + measured heights.
  2. DSM + DTM (data/terrain/dsm.tif + dtm.tif) -> a canopy height model
     (DSM - DTM); stems are CHM local maxima, heights from the CHM.
  3. Red + NIR imagery -> NDVI canopy mask + local maxima (positions only,
     nominal heights).
  4. None of the above -> the vegetation layer is skipped and recorded in
     metadata; this is not an error.

Evergreen/deciduous typing and species/community naming are place-specific
knowledge supplied by the active regional pack (scripts/twin_pack.py ->
packs/<name>/vegetation.py). The engine carries none of it: when a pack
classifier and NIR imagery are both present, trees are typed and named;
otherwise type degrades to "unknown" with no species — the engine never
guesses botany.

When stems are sparse and imagery shows more canopy, the engine fills the gap
with synthetic stems (heights from nearby real stems, falling back to the
pack's community-typical height).

The result is written to the twin store (data/twin.gpkg) and the viewer
payloads re-exported from it. Both RNGs are seeded, so re-runs with unchanged
inputs are no-ops against the store.

Run:  python3 scripts/analyze_vegetation.py        # uses TWIN_PACK if set
"""

import json
import os
import random
import sys

import numpy as np
from osgeo import gdal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_viewer_payloads
import twin_pack
import twin_store
import veg_detect
from twin_store import Store

gdal.UseExceptions()
random.seed(7)
np.random.seed(7)

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
D = os.path.join(PROJECT, "data")        # reset from --data-dir / TWIN_DATA_DIR in main()
STORE_PATH = os.path.join(D, "twin.gpkg")


def _use_data_dir(data_dir):
    """Point this run at a twin's data dir. The store and its journal both live
    there, so an alternate/scratch twin never touches the default ./data."""
    global D, STORE_PATH
    D = os.path.abspath(data_dir)
    STORE_PATH = os.path.join(D, "twin.gpkg")
    twin_store.JOURNAL_DIR = os.path.join(D, "journal")

# generic densification spacing when no pack overrides it (meters)
DEFAULT_SPACING = 3.6


def load_imagery():
    """(nir, ndvi, water, Himg, Wimg) over the outer footprint, or all-None when
    the twin has no red+NIR imagery. `water` is a boolean open-water mask so no
    stem is ever planted on a lake surface (the DEM reports it as flat ground)."""
    fc = os.path.join(D, "imagery", "false_color.png")
    rgb_path = os.path.join(D, "imagery", "naip_rgb.png")
    if not (os.path.exists(fc) and os.path.exists(rgb_path)):
        return None, None, None, None, None
    nir = gdal.Open(fc).ReadAsArray().astype(float)[0]
    rgb = gdal.Open(rgb_path).ReadAsArray().astype(float)
    R = rgb[0]
    green = rgb[1]
    ndvi = (nir - R) / (nir + R + 1e-6)
    water = veg_detect.water_mask(green, nir, ndvi=ndvi)
    return nir, ndvi, water, R.shape[0], R.shape[1]


def detect_stems(grid, ndvi, terrain_valid, sample_elev):
    """The capability ladder. Returns (stems, capability_label) or (None, label)."""
    lidar_path = os.path.join(D, "vegetation", "tree_instances.lidar.json")
    if os.path.exists(lidar_path):
        return json.load(open(lidar_path)), "lidar_segmentation"

    dsm_p = os.path.join(D, "terrain", "dsm.tif")
    dtm_p = os.path.join(D, "terrain", "dtm.tif")
    if os.path.exists(dsm_p) and os.path.exists(dtm_p):
        dsm = gdal.Open(dsm_p).ReadAsArray().astype(float)
        dtm = gdal.Open(dtm_p).ReadAsArray().astype(float)
        return veg_detect.detect_from_chm(dsm, dtm, grid,
                                          terrain_valid=terrain_valid), "dsm_dtm_chm"

    if ndvi is not None:
        return veg_detect.detect_from_ndvi(ndvi, grid, terrain_valid=terrain_valid,
                                           elevation=sample_elev), "ndvi_local_maxima"

    return None, "none"


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"),
                    help="the twin's data dir (default: ./data or $TWIN_DATA_DIR) — "
                         "an alternate twin's store + journal stay in that dir")
    args = ap.parse_args()
    _use_data_dir(args.data_dir)

    pack = twin_pack.load_vegetation({"data_dir": D})
    nir, ndvi, water, Himg, Wimg = load_imagery()
    has_nir = nir is not None
    can_type = bool(pack and has_nir)  # naming evergreen/deciduous needs both

    grid = json.load(open(os.path.join(D, "terrain", "grid.json")))
    ox0, ox1 = grid["outerMinX"], grid["outerMaxX"]
    oy0, oy1 = grid["outerMinY"], grid["outerMaxY"]

    def to_px(x, y):
        return (int(round((x - ox0) / (ox1 - ox0) * (Wimg - 1))),
                int(round((oy1 - y) / (oy1 - oy0) * (Himg - 1))))

    def sample_nir_xy(x, y, w=2):
        px, py = to_px(x, y)
        x0, x1 = max(0, px - w), min(Wimg, px + w + 1)
        y0, y1 = max(0, py - w), min(Himg, py + w + 1)
        return float(nir[y0:y1, x0:x1].mean())

    # place-specific knowledge from the pack (or generic defaults)
    def community_at(x, y):
        return pack.community_at(x, y) if pack else (None, None)

    def is_forest(phys):
        return pack.is_forest(phys) if pack else True

    def typical_height(comm):
        return pack.typical_height(comm) if pack else 17

    spacing = getattr(pack, "spacing", DEFAULT_SPACING) if pack else DEFAULT_SPACING

    def crown_radius(height):
        return round(max(1.6, min(7.5, height * 0.22)), 2)

    sample_elev = make_elev_sampler(grid)

    # Open water reads as flat, valid DEM terrain, so nothing else keeps stems
    # off a lake — an explicit water mask does. Folded into terrain_valid so it
    # gates the CHM/NDVI detectors and canopy densification alike.
    is_water = veg_detect.water_at_sampler(water, grid)

    # The DEM is null outside the AOI polygon; only those cells render terrain.
    # Densified trees must land on valid terrain, or they float beyond the lot.
    def terrain_valid(x, y):
        return terrain_valid_for(grid, x, y) and not is_water(x, y)

    trees, capability = detect_stems(grid, ndvi, terrain_valid, sample_elev)
    if trees is None:
        skip_vegetation(capability, has_nir)
        return

    # LiDAR/CHM/NDVI-detected stems are planted directly (not through the
    # densifier's gates), so drop any that fall on open water here too.
    water_dropped = 0
    if water is not None:
        kept = [t for t in trees if not is_water(t["x"], t["y"])]
        water_dropped = len(trees) - len(kept)
        trees = kept

    out_trees = []
    counts = {"evergreen": 0, "deciduous": 0, "unknown": 0}
    communities = {}

    def finalize(x, y, z, height, source, conf):
        phys, comm = community_at(x, y)
        if can_type:
            typ = pack.classify_type(x, y, sample_nir_xy, phys)
        else:
            typ = "unknown"
        ev = typ == "evergreen"
        counts[typ] = counts.get(typ, 0) + 1
        if comm:
            communities[comm] = communities.get(comm, 0) + 1
        species = pack.species_for(comm, ev) if can_type else None
        out_trees.append({
            "x": round(x, 3), "y": round(y, 3), "z": round(z, 2),
            "height": round(height, 2), "radius": crown_radius(height),
            "type": typ, "community": comm, "species": species,
            "source": source, "confidence": conf,
        })

    for t in trees:
        finalize(t["x"], t["y"], t["z"], t["height"], t.get("source", "lidar"),
                 t.get("confidence", 0.72))

    # ---- canopy densification: plant the forest canopy the detected stems
    # missed, wherever imagery confirms vegetation (and, with a pack, the
    # community is forest). Height from nearby real stems, falling back to the
    # pack's community-typical height. Needs NDVI imagery; skipped without it.
    n0 = len(out_trees)
    added = 0
    if ndvi is not None:
        def accept_canopy(_x, _y, phys, _comm):
            return is_forest(phys)             # not forest -> leave field/road

        def fill_elevation(_x, _y, near, anchor_trees):
            return anchor_trees[near[0]]["z"] if near else grid["minElevation"]

        def add_fill(x, y, z, height, _comm, _context):
            finalize(x, y, z, height, "canopy_fill", 0.4)

        added = veg_detect.densify_canopy(
            trees, grid, spacing, ndvi, to_px, terrain_valid, community_at,
            accept_canopy, typical_height, add_fill,
            elevation_for_candidate=fill_elevation,
        )

    total = len(out_trees)
    top_comm = sorted(communities.items(), key=lambda kv: -kv[1])[:6]
    classified = counts["evergreen"] + counts["deciduous"]
    meta = {
        "tree_count": total,
        "detected_tree_count": n0,
        "lidar_tree_count": n0 if capability == "lidar_segmentation" else 0,
        "canopy_fill_count": added,
        "water_masked": water is not None,
        "water_dropped_stem_count": water_dropped,
        "stem_capability": capability,
        "evergreen_count": counts["evergreen"],
        "deciduous_count": counts["deciduous"],
        "unknown_count": counts.get("unknown", 0),
        "evergreen_pct": round(100 * counts["evergreen"] / classified, 1) if classified else 0,
        "deciduous_pct": round(100 * counts["deciduous"] / classified, 1) if classified else 0,
        "deciduous_evergreen_available": can_type,
        "classification_method": (
            getattr(pack, "classification_method", "pack classifier")
            if can_type else
            ("no NIR imagery — type unavailable" if pack
             else "no regional pack — type unavailable")),
        "communities": [{"name": k, "trees": v} for k, v in top_comm],
        "species_note": getattr(pack, "species_note", None) if can_type else None,
        "lidar_backed": capability == "lidar_segmentation",
        "pack": twin_pack.active_pack_name(D),
    }
    # carry over canopy cover from imagery (true canopy, not just stems)
    if ndvi is not None:
        meta["canopy_cover_pct"] = int(round(100 * float((ndvi > 0.15).mean())))

    # ---- persist to the twin store (authoritative), then re-export the
    # viewer payloads from it
    inputs = [os.path.join(D, "vegetation", "tree_instances.lidar.json"),
              os.path.join(D, "imagery", "false_color.png"),
              os.path.join(D, "imagery", "naip_rgb.png"),
              os.path.join(D, "atlas", "landfire_evt_2024.tif")]
    store = Store(STORE_PATH)
    run = store.begin_run("analyze_vegetation.py",
                          inputs=[p for p in inputs if os.path.exists(p)])
    ids, stats = store.bulk_upsert_vegetation("tree", "trees", out_trees, run,
                                              "member_parcel")
    left, retired = store.reconcile_membership(
        "tree", "member_parcel", ids, run,
        other_member_attrs=("member_surrounding",))
    store.set_meta("vegetation_metadata", meta)
    store.finish_run(run, notes="%s: %d detected + %d canopy-fill stems"
                     % (capability, n0, added))
    store.close()
    print("store run %d: %d created, %d reactivated, %d observations, "
          "%d left parcel (%d retired)" % (
              run, stats["created"], stats["reactivated"],
              stats["observations"], left, retired))

    print("trees [%s]: %d detected + %d canopy-fill = %d" % (capability, n0, added, total))
    if water is not None and water_dropped:
        print("water mask: dropped %d detected stem(s) on open water" % water_dropped)
    if can_type:
        print("evergreen %d (%.0f%%) / deciduous %d (%.0f%%)" % (
            counts["evergreen"], meta["evergreen_pct"],
            counts["deciduous"], meta["deciduous_pct"]))
    else:
        print("type: unknown (%s)" % meta["classification_method"])
    if "canopy_cover_pct" in meta:
        print("canopy cover: %d%%" % meta["canopy_cover_pct"])
    if top_comm:
        print("top communities:", ", ".join("%s (%d)" % (k.split(" Forest")[0], v)
                                             for k, v in top_comm))

    export_viewer_payloads.export_all(data_dir=D, store_path=STORE_PATH)


def make_elev_sampler(grid):
    gw, gh = grid["width"], grid["height"]
    gminx, gmaxx, gminy, gmaxy = grid["minX"], grid["maxX"], grid["minY"], grid["maxY"]
    gxstep = (gmaxx - gminx) / max(1, gw - 1)
    gystep = (gmaxy - gminy) / max(1, gh - 1)
    heights = grid["heights"]

    def elev(x, y):
        col = int(round((x - gminx) / gxstep))
        row = int(round((gmaxy - y) / gystep))
        if 0 <= col < gw and 0 <= row < gh and heights[row * gw + col] is not None:
            return heights[row * gw + col]
        return grid["minElevation"]
    return elev


def terrain_valid_for(grid, x, y):
    """True only where the DEM has a real elevation (the rendered terrain).
    Densified stems must land here or they float beyond the parcel."""
    gw, gh = grid["width"], grid["height"]
    gminx, gmaxx, gminy, gmaxy = grid["minX"], grid["maxX"], grid["minY"], grid["maxY"]
    gxstep = (gmaxx - gminx) / max(1, gw - 1)
    gystep = (gmaxy - gminy) / max(1, gh - 1)
    col = int(round((x - gminx) / gxstep))
    row = int(round((gmaxy - y) / gystep))
    if not (0 <= col < gw and 0 <= row < gh):
        return False
    return grid["heights"][row * gw + col] is not None


def skip_vegetation(capability, has_nir):
    """No stem signal at all: record it in metadata (not an error) and stop."""
    meta = {
        "tree_count": 0, "stem_capability": capability,
        "deciduous_evergreen_available": False,
        "status": "skipped",
        "reason": "no stem source (LiDAR / DSM+DTM / NDVI imagery) available",
        "pack": twin_pack.active_pack_name(D),
    }
    store = Store(STORE_PATH)
    run = store.begin_run("analyze_vegetation.py")
    store.set_meta("vegetation_metadata", meta)
    store.finish_run(run, notes="vegetation skipped: no stem source")
    store.close()
    print("vegetation skipped — no stem source (LiDAR / DSM+DTM / NDVI imagery)")
    export_viewer_payloads.export_all(data_dir=D, store_path=STORE_PATH)


if __name__ == "__main__":
    main()
