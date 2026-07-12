#!/usr/bin/env python3
"""Region-aware planting and semantic-feature catalog for Plan."""

from __future__ import annotations

import json
import os
from typing import Any

import twin_pack


GENERIC_SPECIES = [
    {
        "id": "generic_deciduous_tree",
        "common_name": "Deciduous tree",
        "habit": "tree",
        "type": "deciduous",
        "stages": {
            "sapling": {"height": 2.5, "radius": 0.8},
            "mature": {"height": 14.0, "radius": 5.0},
        },
        "default_stage": "mature",
        "default_spacing_m": 8.0,
        "asset_key": "maple",
        "note": "Generic form; choose a locally appropriate species before implementation.",
    },
    {
        "id": "generic_evergreen_tree",
        "common_name": "Evergreen tree",
        "habit": "tree",
        "type": "evergreen",
        "stages": {
            "sapling": {"height": 2.5, "radius": 0.7},
            "mature": {"height": 16.0, "radius": 4.5},
        },
        "default_stage": "mature",
        "default_spacing_m": 7.0,
        "asset_key": "pine",
        "note": "Generic form; choose a locally appropriate species before implementation.",
    },
    {
        "id": "generic_shrub",
        "common_name": "Shrub",
        "habit": "shrub",
        "type": "deciduous",
        "stages": {
            "young": {"height": 0.7, "radius": 0.5},
            "mature": {"height": 1.8, "radius": 1.2},
        },
        "default_stage": "mature",
        "default_spacing_m": 2.0,
        "asset_key": "shrub",
        "note": "Generic form; choose a locally appropriate species before implementation.",
    },
]


def _normalize_species(raw: Any) -> list[dict[str, Any]]:
    out = []
    for index, row in enumerate(raw or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("common_name") or row.get("name") or "").strip()
        if not name:
            continue
        habit = str(row.get("habit") or "tree")
        if habit not in {"tree", "shrub", "garden"}:
            continue
        stages = row.get("stages") if isinstance(row.get("stages"), dict) else {}
        out.append({
            **row,
            "id": str(row.get("id") or f"species_{index}"),
            "common_name": name,
            "habit": habit,
            "type": "deciduous" if row.get("type") == "deciduous" else "evergreen",
            "stages": stages,
            "default_stage": str(row.get("default_stage") or (next(iter(stages), "mature"))),
            "default_spacing_m": float(row.get("default_spacing_m") or (2 if habit == "shrub" else 7)),
        })
    return out


def catalog(data_dir: str) -> dict[str, Any]:
    hook = twin_pack.load_hook("planning", {"data_dir": data_dir})
    if callable(getattr(hook, "catalog", None)):
        payload = hook.catalog()
    elif isinstance(hook, dict):
        payload = hook
    else:
        payload = {}
    local = _normalize_species(payload.get("species"))
    species = local or list(GENERIC_SPECIES)
    return {
        "pack": twin_pack.active_pack_name(data_dir),
        "species": species,
        "features": payload.get("features") or {
            "swale": {"default_width_m": 6.0, "default_depth_m": 0.35,
                      "note": "Screening geometry, not construction or drainage engineering."},
            "orchard": {"default_row_spacing_m": 7.0, "default_tree_spacing_m": 6.0},
            "garden": {"default_bed_height_m": 0.25,
                       "note": "Yield, irrigation, and crop-growth simulation are not modeled."},
        },
        "limitations": [
            "Species dimensions are planning defaults, not a growth model.",
            "Suitability tags are advisory and must be checked against site evidence.",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(catalog(os.environ.get("TWIN_DATA_DIR") or "data"), indent=2))
