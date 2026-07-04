"""NATO pack vegetation hook.

The generic engine supplies tree positions and heights from LiDAR CHM or NDVI.
This hook supplies only regional interpretation:
  * optional coarse leaf-type/land-cover grids -> forest gate and type hints,
  * NIR fallback for evergreen/deciduous typing when the grid is absent,
  * representative species names that are explicitly coarse, not a survey.

Shared NATO forest-type fetchers write atlas/local/<iso>_leaf_type.grid.json.
For EEA AOIs this is Copernicus HRL Dominant Leaf Type (0 no-tree,
1 broadleaf, 2 conifer); outside the EEA it can be a coarser global forest mask.
"""

import json
import math
import os

FOREST_PHYS = {"Conifer", "Broadleaf", "Mixed", "Forest", "Unknown"}
FALLBACK_PHYS = {None, "Mixed", "Forest", "Unknown"}
NIR_SAMPLE_STEPS = 48

CODE_MAP = {
    0: ("None", "No tree cover"),
    1: ("Broadleaf", "Broadleaf forest"),
    2: ("Conifer", "Conifer forest"),
    3: ("Mixed", "Mixed forest"),
    4: ("Forest", "Forest"),
    10: ("Broadleaf", "Broadleaf forest"),
    20: ("Conifer", "Conifer forest"),
    30: ("Mixed", "Mixed forest"),
    111: ("Broadleaf", "Broadleaf forest"),
    112: ("Conifer", "Conifer forest"),
    113: ("Mixed", "Mixed forest"),
}

TEXT_MAP = {
    "no tree": ("None", "No tree cover"),
    "non-tree": ("None", "No tree cover"),
    "broadleaf": ("Broadleaf", "Broadleaf forest"),
    "deciduous": ("Broadleaf", "Broadleaf forest"),
    "loofbos": ("Broadleaf", "Broadleaf forest"),
    "conifer": ("Conifer", "Conifer forest"),
    "evergreen": ("Conifer", "Conifer forest"),
    "naaldbos": ("Conifer", "Conifer forest"),
    "mixed": ("Mixed", "Mixed forest"),
    "gemengd": ("Mixed", "Mixed forest"),
    "forest": ("Forest", "Forest"),
    "bos": ("Forest", "Forest"),
}


def _grid_rank(name):
    if name.endswith("_leaf_type"):
        return 0
    if name.endswith("_forest_type"):
        return 1
    if name.endswith("_landcover"):
        return 2
    return 3


def _load_grid(data_dir):
    local = os.path.join(data_dir, "atlas", "local")
    priority = ("nl_leaf_type", "nato_leaf_type", "nl_landcover", "nato_landcover")
    candidates = []
    seen = set()

    def add_candidate(name):
        if name not in seen:
            candidates.append(name)
            seen.add(name)

    for name in priority:
        add_candidate(name)
    if os.path.isdir(local):
        for fname in sorted(os.listdir(local)):
            if not fname.endswith(".grid.json"):
                continue
            name = fname[:-10]
            if name.endswith(("_leaf_type", "_forest_type", "_landcover")):
                add_candidate(name)
    candidates.sort(key=lambda name: (_grid_rank(name), name))
    for name in candidates:
        path = os.path.join(data_dir, "atlas", "local", name + ".grid.json")
        if os.path.exists(path):
            grid = json.load(open(path))
            grid["_layer_name"] = name
            return grid
    return None


def _grid_lookup(grid, x, y):
    if not grid:
        return None
    b = grid.get("bounds_local")
    if not b or not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
        return None
    w, h = int(grid["width"]), int(grid["height"])
    col = min(w - 1, max(0, int((x - b[0]) / (b[2] - b[0]) * w)))
    row = min(h - 1, max(0, int((b[3] - y) / (b[3] - b[1]) * h)))
    return grid["values"][row][col]


def _coarse_leaf(value, legend=None):
    if value is None:
        return None, None
    try:
        code = int(value)
    except (TypeError, ValueError):
        code = None
    if code is not None and code in CODE_MAP:
        return CODE_MAP[code]
    label = None
    if code is not None and legend:
        entry = legend.get(str(code)) or legend.get(code)
        if isinstance(entry, dict):
            label = entry.get("name")
    label = label or str(value)
    low = label.lower()
    for key, mapped in TEXT_MAP.items():
        if key in low:
            return mapped
    return "Unknown", label


def _median(values):
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


class NatoVegetation:
    spacing = 3.6
    classification_method = (
        "NATO pack leaf-type/forest grid where available "
        "(Copernicus HRL DLT in the EEA), with scene-relative NIR fallback"
    )
    species_note = (
        "Species are coarse representative labels for visualization and are "
        "not a field survey."
    )

    def __init__(self, context):
        self.data_dir = context["data_dir"]
        self.grid = _load_grid(self.data_dir)
        self.legend = (self.grid or {}).get("legend") or {}
        self.spacing = self._spacing_for_grid()
        self._terrain_bounds = self._load_terrain_bounds()
        self._nir_split = None
        self._nir_split_ready = False

    def _spacing_for_grid(self):
        path = os.path.join(self.data_dir, "terrain", "grid.json")
        try:
            terrain = json.load(open(path))
            step = max(float(terrain.get("xStep") or 0), float(terrain.get("yStep") or 0))
        except Exception:  # noqa: BLE001
            step = 0
        return max(type(self).spacing, step if step >= 10 else 0)

    def _load_terrain_bounds(self):
        path = os.path.join(self.data_dir, "terrain", "grid.json")
        try:
            terrain = json.load(open(path))
            bounds = (
                float(terrain["outerMinX"]),
                float(terrain["outerMinY"]),
                float(terrain["outerMaxX"]),
                float(terrain["outerMaxY"]),
            )
        except Exception:  # noqa: BLE001
            bounds = tuple((self.grid or {}).get("bounds_local") or ())
        if len(bounds) != 4:
            return None
        x0, y0, x1, y1 = bounds
        if not (x0 < x1 and y0 < y1):
            return None
        return bounds

    def _nir_probe_points(self):
        if not self._terrain_bounds:
            return
        x0, y0, x1, y1 = self._terrain_bounds
        steps = NIR_SAMPLE_STEPS
        for row in range(steps):
            y = y0 + (row + 0.5) / steps * (y1 - y0)
            for col in range(steps):
                x = x0 + (col + 0.5) / steps * (x1 - x0)
                yield x, y

    def _fallback_nir_split(self, sample_nir):
        if self._nir_split_ready:
            return self._nir_split
        preferred = []
        sampled = []
        for x, y in self._nir_probe_points() or ():
            try:
                nir = float(sample_nir(x, y))
            except Exception:  # noqa: BLE001
                continue
            if not math.isfinite(nir):
                continue
            sampled.append(nir)
            phys, _comm = self.community_at(x, y)
            if phys in FALLBACK_PHYS:
                preferred.append(nir)
        # The fallback is only for mixed/unknown forest cells. Use their scene
        # median when present, otherwise the whole scene median, so byte-scale
        # brightness changes do not flip the classifier.
        values = preferred if preferred else sampled
        self._nir_split = _median(values) if values else None
        self._nir_split_ready = True
        return self._nir_split

    def community_at(self, x, y):
        phys, comm = _coarse_leaf(_grid_lookup(self.grid, x, y), self.legend)
        if phys is None:
            return None, "Generic NATO forest"
        return phys, comm

    def is_forest(self, phys):
        return phys is None or phys in FOREST_PHYS

    def classify_type(self, x, y, sample_nir, phys=None):
        if phys is None:
            phys, _comm = self.community_at(x, y)
        if phys == "None":
            return "unknown"
        if phys == "Conifer":
            return "evergreen"
        if phys == "Broadleaf":
            return "deciduous"
        if phys not in (None, "Mixed", "Forest", "Unknown"):
            return "unknown"
        try:
            nir = sample_nir(x, y)
        except Exception:  # noqa: BLE001
            return "unknown"
        split = self._fallback_nir_split(sample_nir)
        if split is None:
            return "unknown"
        return "evergreen" if nir < split else "deciduous"

    def species_for(self, community, is_evergreen):
        name = (community or "").lower()
        if "beech" in name or "beuk" in name:
            return "European beech"
        if "oak" in name or "eik" in name:
            return "Pedunculate oak"
        if "birch" in name or "berk" in name:
            return "Silver birch"
        if "pine" in name or "den" in name or "conifer" in name:
            return "Scots pine"
        if "spruce" in name or "spar" in name:
            return "Norway spruce"
        return "Scots pine" if is_evergreen else "European beech"

    def typical_height(self, community):
        low = (community or "").lower()
        if "conifer" in low or "pine" in low:
            return 20
        if "broadleaf" in low or "beech" in low:
            return 22
        return 19


def load(context):
    return NatoVegetation(context)
