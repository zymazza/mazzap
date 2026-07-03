"""NATO pack atlas styling hook."""

import numpy as np


class NatoLayers:
    label_keys = (
        "naam", "name", "Name", "label", "omschrijving", "description",
        "klasse", "class", "type", "id",
    )

    def enrich(self, name, props):
        if name.startswith("nl_") or name.endswith(("_natura2000", "_clcplus_landcover")):
            props.setdefault("source_pack", "nato")

    def vector_style(self, name):
        styles = {
            "nl_forest": ("Dutch Forest", "rgba(45,105,67,0.32)", "#2d6943", "polygon"),
            "nl_landcover": ("Dutch Land Cover", "rgba(120,143,72,0.34)", "#697f3f", "polygon", True),
            "nl_protected_area": ("Protected Area", "rgba(54,113,162,0.22)", "#3671a2", "polygon"),
            "nl_trails": ("Trails", "rgba(0,0,0,0)", "#d38b2c", "line"),
            "nl_water": ("Water", "rgba(54,147,190,0.32)", "#3693be", "polygon"),
        }
        if name.endswith("_natura2000"):
            return ("Natura 2000", "rgba(54,113,162,0.20)", "#3671a2", "polygon")
        return styles.get(name)

    def raster_label(self, name):
        if name.endswith("_leaf_type"):
            return "Dominant Leaf Type"
        if name.endswith("_eth_chm"):
            return "ETH Canopy Height"
        if name.endswith("_clcplus_landcover"):
            return "CLC+ Land Cover"
        return {
            "nl_ahn_chm": "AHN Canopy Height",
            "nl_landcover": "Dutch Land Cover",
        }.get(name)

    def render_raster(self, name, arr, nodata, helpers):
        if name == "nl_ahn_chm":
            return _render_chm(arr, nodata)
        if name.endswith("_eth_chm"):
            return _render_chm(arr, nodata)
        if name.endswith("_leaf_type") or name.endswith("_forest_type"):
            return _render_leaf_type(arr, nodata)
        if name.endswith("_clcplus_landcover"):
            return _render_clcplus(arr, nodata)
        return None


def _valid_mask(arr, nodata):
    mask = np.isfinite(arr)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != nodata
    return mask


def _render_chm(arr, nodata):
    arr = arr.astype(float)
    mask = _valid_mask(arr, nodata) & (arr > 0.5)
    vals = arr[mask]
    hi = float(np.percentile(vals, 98)) if vals.size else 30.0
    hi = max(8.0, min(45.0, hi))
    stops = np.array([
        [236, 232, 203, 0],
        [164, 185, 109, 130],
        [80, 141, 82, 185],
        [33, 92, 73, 220],
        [29, 55, 67, 235],
    ], dtype=float)
    safe = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    scaled = np.clip(safe / hi, 0, 0.999) * (len(stops) - 1)
    idx = scaled.astype(int)
    frac = scaled - idx
    rgba = (stops[idx] * (1 - frac[..., None]) + stops[idx + 1] * frac[..., None]).astype(np.uint8)
    rgba[~mask] = [0, 0, 0, 0]
    legend = {
        "min": {"name": "0 m", "color": [164, 185, 109]},
        "max": {"name": "%.1f m" % hi, "color": [29, 55, 67]},
    }
    return rgba, legend


def _render_leaf_type(arr, nodata):
    colors = {
        0: ([160, 160, 150, 35], "No tree cover"),
        1: ([112, 154, 83, 210], "Broadleaf"),
        2: ([45, 92, 70, 220], "Conifer"),
        3: ([88, 132, 89, 215], "Mixed"),
        4: ([92, 118, 76, 200], "Forest"),
        10: ([112, 154, 83, 210], "Broadleaf"),
        20: ([45, 92, 70, 220], "Conifer"),
        30: ([88, 132, 89, 215], "Mixed"),
        111: ([112, 154, 83, 210], "Broadleaf"),
        112: ([45, 92, 70, 220], "Conifer"),
        113: ([88, 132, 89, 215], "Mixed"),
    }
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    legend = {}
    for code, (color, label) in colors.items():
        if nodata is not None and np.isfinite(nodata) and code == int(nodata):
            continue
        m = arr == code
        if m.any():
            rgba[m] = color
            legend[code] = {"name": label, "color": color[:3]}
    return rgba, legend


def _render_clcplus(arr, nodata):
    colors = {
        1: ([192, 70, 70, 155], "Sealed"),
        2: ([45, 92, 70, 210], "Needleleaf trees"),
        3: ([112, 154, 83, 210], "Broadleaf deciduous trees"),
        4: ([74, 128, 81, 210], "Broadleaf evergreen trees"),
        5: ([131, 150, 83, 190], "Low-growing woody plants"),
        6: ([156, 190, 92, 175], "Permanent herbaceous"),
        7: ([205, 199, 91, 170], "Periodic herbaceous"),
        8: ([165, 172, 142, 160], "Lichens and mosses"),
        9: ([190, 180, 160, 140], "Sparse vegetation"),
        10: ([54, 147, 190, 175], "Water"),
        11: ([230, 238, 242, 190], "Snow and ice"),
    }
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    legend = {}
    for code, (color, label) in colors.items():
        if nodata is not None and np.isfinite(nodata) and code == int(nodata):
            continue
        m = arr == code
        if m.any():
            rgba[m] = color
            legend[code] = {"name": label, "color": color[:3]}
    return rgba, legend


def load(context):
    return NatoLayers()
