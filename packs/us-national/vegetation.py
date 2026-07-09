"""National (CONUS) vegetation typing from LANDFIRE EVT.

Unlike a single-region pack, this knowledge applies anywhere in the
continental US: LANDFIRE 2024 Existing Vegetation Type covers all of CONUS,
its physiognomy field (Conifer / Hardwood / …) maps cleanly to
evergreen / deciduous, and its community names are nationally defined. So any
US twin that has fetched a LANDFIRE EVT grid (see fetch_landfire.py) gets typed
trees with community names — no hand-built regional pack required.

What's national here (and lives in this pack):
  * EVT physiognomy -> evergreen / deciduous (Conifer -> evergreen,
    Hardwood -> deciduous, mixed/unknown -> NIR spectral fallback),
  * which physiognomies count as "forest" for canopy densification,
  * a coarse representative species pulled from the community name's genus
    keywords (Ponderosa Pine, Douglas-fir, Pinyon, Juniper, Aspen, Oak, …).

Exact local species composition is still genuine regional knowledge; this pack
gives a defensible national estimate, not a field survey. A region-specific
pack can do better for its own area.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from landfire_vat import load_vat  # noqa: E402

# EVT physiognomies that are forest/woodland (trees get planted; densified).
FOREST_PHYS = {"Conifer", "Hardwood", "Conifer-Hardwood", "Riparian"}

# NIR threshold for the spectral fallback (mixed/unknown physiognomy), same
# midpoint convention as the regional packs.
NIR_SPLIT = 162.0

# genus/EVT-name keyword -> representative species + leaf habit. Ordered: first
# hit wins. Evergreen (conifer) genera first, then broadleaf.
SPECIES_KEYWORDS = [
    ("ponderosa", "Ponderosa Pine", True),
    ("lodgepole", "Lodgepole Pine", True),
    ("pinyon", "Two-needle Pinyon", True),
    ("pinon", "Two-needle Pinyon", True),
    ("juniper", "Rocky Mountain Juniper", True),
    ("douglas-fir", "Douglas-fir", True),
    ("douglas fir", "Douglas-fir", True),
    ("white fir", "White Fir", True),
    ("subalpine fir", "Subalpine Fir", True),
    ("grand fir", "Grand Fir", True),
    ("spruce-fir", "Engelmann Spruce", True),
    ("spruce", "Engelmann Spruce", True),
    ("hemlock", "Western Hemlock", True),
    ("redwood", "Coast Redwood", True),
    ("red cedar", "Western Redcedar", True),
    ("redcedar", "Western Redcedar", True),
    ("cypress", "Bald Cypress", False),
    ("white pine", "Western White Pine", True),
    ("red pine", "Red Pine", True),
    ("jack pine", "Jack Pine", True),
    ("loblolly", "Loblolly Pine", True),
    ("longleaf", "Longleaf Pine", True),
    ("shortleaf", "Shortleaf Pine", True),
    ("pine", "Pine", True),
    ("aspen", "Quaking Aspen", False),
    ("cottonwood", "Plains Cottonwood", False),
    ("oak", "Gambel Oak", False),
    ("maple", "Red Maple", False),
    ("birch", "Paper Birch", False),
    ("beech", "American Beech", False),
    ("hickory", "Shagbark Hickory", False),
    ("willow", "Black Willow", False),
    ("alder", "Thinleaf Alder", False),
    ("mesquite", "Honey Mesquite", False),
    ("hardwood", "Mixed Hardwood", False),
]

# coarse community-typical canopy height (m) when no measured stem is nearby
HEIGHT_BY_PHYS = {"Conifer": 18, "Conifer-Hardwood": 18, "Hardwood": 17,
                  "Riparian": 14}


def _load_grid(data_dir):
    """at(x, y) -> (EVT_PHYS, EVT_NAME) over scene-local meters, from the
    fetched LANDFIRE grid; None when the twin has no LANDFIRE."""
    grid_path = os.path.join(data_dir, "atlas", "local", "landfire_evt_2024.grid.json")
    if not os.path.exists(grid_path):
        return None
    return json.load(open(grid_path))


class NationalVegetation:
    spacing = 3.6
    classification_method = ("LANDFIRE 2024 EVT physiognomy "
                             "(+ NAIP color-infrared NDVI/NIR where mixed)")
    species_note = ("Species are a coarse national estimate from the LANDFIRE "
                    "community's dominant genus — not a field survey; a "
                    "region-specific pack can refine them.")

    def __init__(self, context):
        self.vat = load_vat()
        self._phys_by_name = {name.lower(): phys for name, phys in self.vat.values() if name}
        grid = _load_grid(context["data_dir"])
        if grid is None:
            raise SystemExit(
                "us-national vegetation needs a LANDFIRE EVT grid — run "
                "`python3 packs/us-national/fetch_landfire.py --data-dir <data>` first")
        self._grid = grid
        b = grid["bounds_local"]
        ew, eh = grid["width"], grid["height"]
        values = grid["values"]

        def code_at(x, y):
            if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
                return None
            c = min(ew - 1, int((x - b[0]) / (b[2] - b[0]) * ew))
            r = min(eh - 1, int((b[3] - y) / (b[3] - b[1]) * eh))
            v = values[r][c]
            return None if v is None else int(v)
        self._code_at = code_at

    # -- community / forest gate ------------------------------------------
    def community_at(self, x, y):
        code = self._code_at(x, y)
        if code is None:
            return None, None
        name, phys = self.vat.get(code, (None, None))
        return phys, name

    def is_forest(self, phys):
        return phys in FOREST_PHYS

    def is_water(self, phys):
        # EVT physiognomy for lakes/rivers — a stem can't stand in open water.
        # Complements the engine's imagery NDWI mask, which needs a trustworthy
        # NIR band (some snapshots ship a bogus 4th band that fools it).
        return (phys or "").strip().lower() in ("open water", "water")

    # -- type classification ----------------------------------------------
    def classify_type(self, x, y, sample_nir, phys=None):
        if phys is None:
            phys, _ = self.community_at(x, y)
        if phys == "Conifer":
            ev = True
        elif phys in ("Hardwood", "Riparian", "Exotic Tree-Shrub", "Exotic Herbaceous"):
            ev = False
        else:  # Conifer-Hardwood / None / non-forest -> spectral fallback
            ev = sample_nir(x, y) < NIR_SPLIT
        nirv = sample_nir(x, y)
        if ev and nirv > NIR_SPLIT + 22:
            ev = False
        elif not ev and nirv < NIR_SPLIT - 22:
            ev = True
        return "evergreen" if ev else "deciduous"

    # -- species / height -------------------------------------------------
    def species_for(self, community, is_evergreen):
        c = (community or "").lower()
        for kw, species, _ev in SPECIES_KEYWORDS:
            if kw in c:
                return species
        return "Evergreen tree" if is_evergreen else "Broadleaf tree"

    def typical_height(self, community):
        phys = self._phys_by_name.get((community or "").lower())
        return HEIGHT_BY_PHYS.get(phys, 16)


def load(context):
    return NationalVegetation(context)
