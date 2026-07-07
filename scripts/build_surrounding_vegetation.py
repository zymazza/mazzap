#!/usr/bin/env python3
"""Build a surrounding-area vegetation layer from the same signals as the AOI.

The main tree layer is clipped by data/terrain/grid.json, while the surrounding
terrain is the apron-only cells in data/terrain/grid.apron.json. This script
leaves the parcel vegetation alone and upserts the surrounding-only tree/shrub
population into the twin store (member_surrounding), then re-exports the
viewer payloads (surrounding_*.json) from the store. Trees that exist in both
populations (LiDAR stems on the apron) are the same store entity carrying both
membership flags. The RNG is seeded, so re-runs with unchanged inputs are
no-ops against the store.

Run:  python3 scripts/build_surrounding_vegetation.py
"""

import json
import os
import random
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_viewer_payloads
import twin_pack
import twin_store
import veg_detect
from twin_store import SHRUB_ATTRS, TREE_ATTRS, Store

gdal.UseExceptions()
random.seed(1412)
np.random.seed(1412)

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
D = PROJECT / "data"          # reset from --data-dir / TWIN_DATA_DIR in main()
STORE_PATH = str(D / "twin.gpkg")

def _use_data_dir(data_dir):
    """Retarget at a twin's data dir; its store + journal stay there."""
    global D, STORE_PATH
    D = Path(data_dir).resolve()
    STORE_PATH = str(D / "twin.gpkg")
    twin_store.JOURNAL_DIR = str(D / "journal")


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def grid_params(grid):
    return {
        "width": grid["width"],
        "height": grid["height"],
        "min_x": grid["minX"],
        "max_x": grid["maxX"],
        "min_y": grid["minY"],
        "max_y": grid["maxY"],
        "x_step": (grid["maxX"] - grid["minX"]) / max(1, grid["width"] - 1),
        "y_step": (grid["maxY"] - grid["minY"]) / max(1, grid["height"] - 1),
        "heights": grid["heights"],
        "min_elevation": grid["minElevation"],
    }


def grid_index(params, x, y):
    col = int(round((x - params["min_x"]) / params["x_step"]))
    row = int(round((params["max_y"] - y) / params["y_step"]))
    if not (0 <= col < params["width"] and 0 <= row < params["height"]):
        return None
    return row * params["width"] + col


def has_valid_terrain(params, x, y):
    index = grid_index(params, x, y)
    return index is not None and params["heights"][index] is not None


def sample_elevation(params, x, y):
    index = grid_index(params, x, y)
    if index is None:
        return params["min_elevation"]
    height = params["heights"][index]
    return params["min_elevation"] if height is None else height


def crown_radius(height):
    return round(max(1.6, min(7.5, height * 0.22)), 2)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or str(PROJECT / "data"),
                    help="the twin's data dir (default: ./data or $TWIN_DATA_DIR)")
    args = ap.parse_args()
    _use_data_dir(args.data_dir)

    pack = twin_pack.load_vegetation({"data_dir": str(D)})
    if pack is None:
        # The surrounding apron mirrors the parcel population's typing; with no
        # pack there is no evergreen/deciduous knowledge to mirror. Re-run with
        # TWIN_PACK set, or skip the surrounding layer.
        print("no regional pack (TWIN_PACK unset) — surrounding vegetation needs "
              "the pack's typing knowledge; skipping")
        return
    if not (D / "terrain" / "grid.apron.json").exists():
        print("no surrounding terrain apron (grid.apron.json) — skipping "
              "surrounding vegetation")
        return
    spacing = getattr(pack, "spacing", 3.6)
    nir = gdal.Open(str(D / "imagery" / "false_color.png")).ReadAsArray().astype(float)[0]
    rgb = gdal.Open(str(D / "imagery" / "naip_rgb.png")).ReadAsArray().astype(float)
    red = rgb[0]
    green = rgb[1]
    ndvi = (nir - red) / (nir + red + 1e-6)
    water = veg_detect.water_mask(green, nir, ndvi=ndvi)
    image_height, image_width = red.shape
    typical_height = pack.typical_height

    grid = read_json(D / "terrain" / "grid.json")
    apron_grid = read_json(D / "terrain" / "grid.apron.json")
    grid_p = grid_params(grid)
    apron_p = grid_params(apron_grid)

    ox0, ox1 = grid["outerMinX"], grid["outerMaxX"]
    oy0, oy1 = grid["outerMinY"], grid["outerMaxY"]

    def to_px(x, y):
        px = int(round((x - ox0) / (ox1 - ox0) * (image_width - 1)))
        py = int(round((oy1 - y) / (oy1 - oy0) * (image_height - 1)))
        return (
            min(image_width - 1, max(0, px)),
            min(image_height - 1, max(0, py)),
        )

    def sample_nir_xy(x, y, width=2):
        px, py = to_px(x, y)
        x0, x1 = max(0, px - width), min(image_width, px + width + 1)
        y0, y1 = max(0, py - width), min(image_height, py + width + 1)
        return float(nir[y0:y1, x0:x1].mean())

    def sample_canopy_xy(x, y, width=3):
        px, py = to_px(x, y)
        x0, x1 = max(0, px - width), min(image_width, px + width + 1)
        y0, y1 = max(0, py - width), min(image_height, py + width + 1)
        rgb_patch = rgb[:, y0:y1, x0:x1]
        ndvi_patch = ndvi[y0:y1, x0:x1]
        green_excess = rgb_patch[1] - np.maximum(rgb_patch[0], rgb_patch[2])
        return {
            "ndvi": float(ndvi_patch.mean()),
            "brightness": float(rgb_patch.mean()),
            "texture": float(rgb_patch.reshape(3, -1).std(axis=1).mean()),
            "green_excess": float(green_excess.mean()),
        }

    def has_spectral_canopy(x, y):
        signal = sample_canopy_xy(x, y)
        return (
            signal["ndvi"] >= 0.22
            and signal["green_excess"] >= 4.0
            and (signal["texture"] >= 9.0 or signal["brightness"] <= 118.0)
        )

    # Open water reads as flat, valid apron terrain; an explicit water mask is
    # the only thing that keeps stems off a lake. Folded into is_surrounding_cell
    # so it gates the carried-over stems, canopy densification and shrubs alike.
    is_water = veg_detect.water_at_sampler(water, grid)

    def is_surrounding_cell(x, y):
        return (has_valid_terrain(apron_p, x, y)
                and not has_valid_terrain(grid_p, x, y)
                and not is_water(x, y))

    evt_at = pack.community_at
    # Read the parcel populations from the store (the authoritative source),
    # not the exported JSON. Ordered by entity_id, so RNG consumption — and
    # therefore the generated stems — are deterministic across rebuilds.
    store = Store(STORE_PATH)
    all_trees = store.instances("tree", "trees", "member_parcel", TREE_ATTRS,
                                include_id=False)
    shrubs = store.instances("shrub", "shrubs", "member_parcel", SHRUB_ATTRS,
                             include_id=False)

    surrounding_trees = []
    counts = {
        "existing": 0,
        "canopy_fill": 0,
        "spectral_canopy_fill": 0,
        "evergreen": 0,
        "deciduous": 0,
    }
    communities = {}

    def add_tree(x, y, z, height, source, confidence, species=None, tree_type=None, community=None):
        if tree_type is None:
            phys, community = evt_at(x, y)
            tree_type = pack.classify_type(x, y, sample_nir_xy, phys)
            evergreen = tree_type == "evergreen"
            species = pack.species_for(community, evergreen)
        typ = tree_type if tree_type == "deciduous" else "evergreen"
        counts[typ] += 1
        if community:
            communities[community] = communities.get(community, 0) + 1
        surrounding_trees.append({
            "x": round(x, 3),
            "y": round(y, 3),
            "z": round(z, 2),
            "height": round(height, 2),
            "radius": crown_radius(height),
            "type": typ,
            "community": community,
            "species": species,
            "source": source,
            "confidence": confidence,
        })

    for tree in all_trees:
        x = float(tree.get("x", 0))
        y = float(tree.get("y", 0))
        if not is_surrounding_cell(x, y):
            continue
        add_tree(
            x,
            y,
            float(tree.get("z", sample_elevation(apron_p, x, y))),
            float(tree.get("height", 8)),
            tree.get("source", "lidar"),
            tree.get("confidence", 0.72),
            tree.get("species"),
            tree.get("type"),
            tree.get("community"),
        )
        counts["existing"] += 1

    def accept_canopy(x, y, phys, _community):
        forest = pack.is_forest(phys)
        if forest:
            return True, {"spectral": False}
        return has_spectral_canopy(x, y), {"spectral": True}

    def fill_elevation(x, y, _near, _anchor_trees):
        return sample_elevation(apron_p, x, y)

    def add_fill(x, y, z, height, _community, context):
        add_tree(x, y, z, height, "canopy_fill", 0.4)
        counts["canopy_fill"] += 1
        if context and context.get("spectral"):
            counts["spectral_canopy_fill"] += 1

    veg_detect.densify_canopy(
        all_trees, grid, spacing, ndvi, to_px, is_surrounding_cell,
        evt_at, accept_canopy, typical_height, add_fill,
        elevation_for_candidate=fill_elevation,
    )

    surrounding_shrubs = [
        shrub for shrub in shrubs
        if is_surrounding_cell(float(shrub.get("x", 0)), float(shrub.get("y", 0)))
    ]

    total = len(surrounding_trees)
    metadata = {
        "tree_count": total,
        "existing_tree_count": counts["existing"],
        "canopy_fill_count": counts["canopy_fill"],
        "water_masked": bool(water is not None),
        "spectral_canopy_fill_count": counts["spectral_canopy_fill"],
        "shrub_anchor_count": len(surrounding_shrubs),
        "evergreen_count": counts["evergreen"],
        "deciduous_count": counts["deciduous"],
        "evergreen_pct": round(100 * counts["evergreen"] / total, 1) if total else 0,
        "deciduous_pct": round(100 * counts["deciduous"] / total, 1) if total else 0,
        "classification_method": getattr(pack, "classification_method", "pack classifier"),
        "species_note": getattr(pack, "species_note", None),
        "communities": [
            {"name": name, "trees": count}
            for name, count in sorted(communities.items(), key=lambda item: -item[1])[:6]
        ],
        "source": ("grid.apron.json cells outside grid.json; canopy-fill uses "
                   "LANDFIRE forest classes plus high-confidence NAIP NDVI/RGB "
                   "canopy fallback"),
    }

    # ---- persist to the twin store (authoritative), then re-export the
    # viewer payloads from it
    run = store.begin_run("build_surrounding_vegetation.py", inputs=[
        str(D / "vegetation" / "tree_instances.lidar.json"),
        str(D / "terrain" / "grid.json"),
        str(D / "terrain" / "grid.apron.json"),
        str(D / "imagery" / "false_color.png"),
        str(D / "imagery" / "naip_rgb.png"),
    ])
    tree_ids, tree_stats = store.bulk_upsert_vegetation(
        "tree", "trees", surrounding_trees, run, "member_surrounding")
    tleft, tretired = store.reconcile_membership(
        "tree", "member_surrounding", tree_ids, run,
        other_member_attrs=("member_parcel",))
    shrub_ids, shrub_stats = store.bulk_upsert_vegetation(
        "shrub", "shrubs", surrounding_shrubs, run, "member_surrounding")
    sleft, sretired = store.reconcile_membership(
        "shrub", "member_surrounding", shrub_ids, run,
        other_member_attrs=("member_parcel",))
    store.set_meta("surrounding_vegetation_metadata", metadata)
    store.finish_run(run, notes="%d existing + %d canopy-fill stems"
                     % (counts["existing"], counts["canopy_fill"]))
    store.close()
    print("store run %d: trees %d created / %d obs (%d left, %d retired); "
          "shrubs %d created / %d obs (%d left, %d retired)" % (
              run, tree_stats["created"], tree_stats["observations"], tleft, tretired,
              shrub_stats["created"], shrub_stats["observations"], sleft, sretired))

    print(
        "surrounding trees: %d existing + %d canopy-fill = %d"
        % (counts["existing"], counts["canopy_fill"], total)
    )
    print("surrounding shrubs: %d" % len(surrounding_shrubs))
    if total:
        print(
            "evergreen %d (%.1f%%) / deciduous %d (%.1f%%)"
            % (counts["evergreen"], metadata["evergreen_pct"], counts["deciduous"], metadata["deciduous_pct"])
        )

    export_viewer_payloads.export_all(data_dir=str(D), store_path=STORE_PATH)


if __name__ == "__main__":
    main()
