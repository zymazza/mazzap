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
        if name.endswith("_hydrorivers"):
            if props.get("ORD_STRA") not in (None, ""):
                props.setdefault("__label", "Strahler %s" % props.get("ORD_STRA"))
            props.setdefault("source_pack", "nato")
        if name.endswith("_hydrolakes"):
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
        if name.endswith("_hydrorivers"):
            return ("Rivers (HydroRIVERS)", "rgba(0,0,0,0)", "#2377b9", "line")
        if name.endswith("_hydrolakes"):
            return ("Lakes & reservoirs (HydroLAKES)", "rgba(42,128,185,0.30)", "#2a80b9", "polygon")
        return styles.get(name)

    def raster_label(self, name):
        if name.endswith("_leaf_type"):
            return "Dominant Leaf Type"
        if name.endswith("_eth_chm"):
            return "ETH Canopy Height"
        if name.endswith("_clcplus_landcover"):
            return "CLC+ Land Cover"
        if name.endswith("_soil_phh2o_0_5cm"):
            return "Soil pH (0-5cm)"
        if name.endswith("_soil_soc_0_5cm"):
            return "Soil organic carbon (0-5cm)"
        if name.endswith("_soil_clay_0_5cm"):
            return "Clay % (0-5cm)"
        if name.endswith("_soil_sand_0_5cm"):
            return "Sand % (0-5cm)"
        if name.endswith("_jrc_gsw_occurrence"):
            return "Surface water occurrence (JRC GSW)"
        if name.endswith("_gbif_density"):
            return "GBIF observation density"
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
        if name.endswith("_soil_phh2o_0_5cm"):
            return _render_continuous(arr, nodata, [
                (4.0, [191, 74, 64, 150], "acidic"),
                (6.5, [226, 202, 94, 170], "near neutral"),
                (8.5, [75, 145, 109, 185], "alkaline"),
            ], unit="pH")
        if name.endswith("_soil_soc_0_5cm"):
            return _render_continuous(arr, nodata, [
                (0.0, [218, 205, 168, 95], "low"),
                (50.0, [126, 150, 91, 165], "moderate"),
                (150.0, [67, 89, 70, 215], "high"),
            ], unit="g/kg")
        if name.endswith("_soil_clay_0_5cm"):
            return _render_continuous(arr, nodata, [
                (0.0, [232, 218, 176, 90], "low"),
                (35.0, [169, 139, 119, 165], "moderate"),
                (70.0, [101, 88, 128, 210], "high"),
            ], unit="%")
        if name.endswith("_soil_sand_0_5cm"):
            return _render_continuous(arr, nodata, [
                (0.0, [110, 132, 120, 85], "low"),
                (50.0, [207, 185, 118, 160], "moderate"),
                (95.0, [224, 210, 151, 205], "high"),
            ], unit="%")
        if name.endswith("_jrc_gsw_occurrence"):
            return _render_continuous(arr, nodata, [
                (1.0, [126, 196, 218, 80], "rare"),
                (50.0, [49, 128, 190, 170], "seasonal"),
                (100.0, [18, 74, 135, 225], "persistent"),
            ], unit="%", transparent_zero=True)
        if name.endswith("_gbif_density"):
            return _render_continuous(arr, nodata, [
                (1.0, [112, 92, 151, 80], "low"),
                (90.0, [209, 121, 83, 160], "moderate"),
                (220.0, [244, 202, 99, 225], "high"),
            ], unit="intensity", transparent_zero=True)
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


def _render_continuous(arr, nodata, stops, unit="", transparent_zero=False):
    arr = arr.astype(float)
    mask = _valid_mask(arr, nodata)
    if transparent_zero:
        mask &= arr > 0
    vals = arr[mask]
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    if vals.size == 0:
        return rgba, {}
    stop_vals = np.array([s[0] for s in stops], dtype=float)
    colors = np.array([s[1] for s in stops], dtype=float)
    safe = np.nan_to_num(arr, nan=stop_vals[0], posinf=stop_vals[-1], neginf=stop_vals[0])
    idx = np.searchsorted(stop_vals, safe, side="right") - 1
    idx = np.clip(idx, 0, len(stops) - 2)
    lo = stop_vals[idx]
    hi = stop_vals[idx + 1]
    span = np.where((hi - lo) == 0, 1.0, hi - lo)
    frac = np.clip((safe - lo) / span, 0, 1)
    out = colors[idx] * (1 - frac[..., None]) + colors[idx + 1] * frac[..., None]
    rgba = out.astype(np.uint8)
    rgba[~mask] = [0, 0, 0, 0]

    def label(v, text):
        if unit == "pH":
            return "%.1f pH %s" % (v, text)
        if unit == "%":
            return "%.0f%% %s" % (v, text)
        if unit == "g/kg":
            return "%.0f g/kg %s" % (v, text)
        return "%s %s" % (int(v), text)

    legend = {}
    for value, color, text in stops:
        legend[str(value)] = {"name": label(float(value), text), "color": color[:3]}
    return rgba, legend


def load(context):
    return NatoLayers()
