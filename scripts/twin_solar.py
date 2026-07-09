#!/usr/bin/env python3
"""Solar resource and fixed-panel PV helpers for VEIL twins.

The model is deliberately local-first:
  * clear-sky geometry/irradiance is computed offline from latitude/elevation;
  * Daymet all-sky shortwave (`srad * dayl`) supplies climatological cloud loss;
  * a caller-provided horizon profile supplies terrain/canopy direct-beam shade.

It is a planning-grade PVWatts-style estimate, not a bankable production model.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import csv
import datetime as dt
import io
import json
import math
import os
from typing import Any


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DATA_DIR = os.path.abspath(os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))

MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
MONTH_MID_YDAY = [15, 46, 74, 105, 135, 166, 196, 227, 258, 288, 319, 349]
SOLAR_CONSTANT_WM2 = 1367.0
DEFAULT_LOSSES = 0.14
TEMP_COEFF_PER_C = -0.004
GROUND_ALBEDO = 0.20


@dataclass(frozen=True)
class SolarSite:
    lat: float
    lon: float
    elevation_m: float = 0.0


@dataclass(frozen=True)
class VegetationRecord:
    id: str
    x: float
    y: float
    height_m: float
    radius_m: float
    kind: str = "tree"
    type: str | None = None
    species: str | None = None
    source: str | None = None
    confidence: float | None = None


def panel_footprint_radius_m(system_kw: float = 1.0) -> float:
    """Approximate fixed-panel footprint radius for vegetation clearance.

    This is not structural design. It gives the siting engine a conservative
    local exclusion radius scaled from panel area plus maintenance access.
    """
    kw = max(0.05, float(system_kw or 1.0))
    panel_area_m2 = kw * 6.0
    return math.sqrt(panel_area_m2 / math.pi)


def required_vegetation_clearance_radius_m(system_kw: float = 1.0,
                                           service_clearance_m: float = 1.5) -> float:
    return max(2.5, panel_footprint_radius_m(system_kw) + max(0.0, float(service_clearance_m)))


def _finite_number(value: Any, default: float | None = None) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _record_from_raw(raw: dict[str, Any], kind: str = "tree") -> VegetationRecord | None:
    if not isinstance(raw, dict):
        return None
    x = _finite_number(raw.get("x"))
    y = _finite_number(raw.get("y"))
    if (x is None or y is None) and isinstance(raw.get("geometry"), dict):
        coords = raw["geometry"].get("coordinates") or []
        if raw["geometry"].get("type") == "Point" and len(coords) >= 2:
            x = _finite_number(coords[0])
            y = _finite_number(coords[1])
    if x is None or y is None:
        return None
    height = _finite_number(raw.get("height"), _finite_number(raw.get("height_m"), 0.0)) or 0.0
    radius = _finite_number(raw.get("radius"), _finite_number(raw.get("radius_m")))
    if radius is None:
        radius = max(0.75, min(7.5, height * 0.22)) if height > 0 else 1.5
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else raw
    conf = _finite_number(props.get("confidence"))
    return VegetationRecord(
        id=str(props.get("id") or raw.get("id") or f"{kind}:{x:.2f},{y:.2f}"),
        x=float(x),
        y=float(y),
        height_m=max(0.0, float(height)),
        radius_m=max(0.0, float(radius)),
        kind=str(props.get("kind") or kind),
        type=props.get("type"),
        species=props.get("species"),
        source=props.get("source"),
        confidence=conf,
    )


def load_vegetation_records(data_dir: str) -> list[VegetationRecord]:
    """Load local vegetation instances emitted by analyze_vegetation.py."""
    veg_dir = os.path.join(os.path.abspath(data_dir), "vegetation")
    records: list[VegetationRecord] = []
    for filename, kind in (("tree_instances.json", "tree"), ("shrub_points.json", "shrub")):
        path = os.path.join(veg_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, list):
            rows = doc
        elif isinstance(doc, dict):
            rows = doc.get("features") or doc.get("trees") or doc.get("shrubs") or []
        else:
            rows = []
        for raw in rows:
            rec = _record_from_raw(raw, kind=kind)
            if rec is not None:
                records.append(rec)
    return records


class SolarVegetationIndex:
    """Small spatial hash over vegetation crowns for solar footprint checks."""

    def __init__(self, records: list[VegetationRecord] | None = None,
                 source: str = "none", cell_size_m: float = 25.0):
        self.records = list(records or [])
        self.source = source
        self.cell_size_m = max(5.0, float(cell_size_m))
        self.max_radius_m = max([r.radius_m for r in self.records] or [0.0])
        self._buckets: dict[tuple[int, int], list[VegetationRecord]] = {}
        for rec in self.records:
            self._buckets.setdefault(self._cell(rec.x, rec.y), []).append(rec)

    @classmethod
    def from_data_dir(cls, data_dir: str) -> "SolarVegetationIndex":
        records = load_vegetation_records(data_dir)
        if records:
            return cls(records, source="vegetation/tree_instances.json + shrub_points.json")
        return cls([], source="none")

    @classmethod
    def from_records(cls, records: list[dict[str, Any] | VegetationRecord],
                     source: str = "store tree/shrub entities") -> "SolarVegetationIndex":
        normalized: list[VegetationRecord] = []
        for raw in records:
            if isinstance(raw, VegetationRecord):
                normalized.append(raw)
            else:
                rec = _record_from_raw(raw, kind=str(raw.get("kind") or "tree"))
                if rec is not None:
                    normalized.append(rec)
        return cls(normalized, source=source if normalized else "none")

    @property
    def available(self) -> bool:
        return bool(self.records)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return (math.floor(float(x) / self.cell_size_m),
                math.floor(float(y) / self.cell_size_m))

    def _nearby(self, x: float, y: float, radius_m: float) -> list[VegetationRecord]:
        if not self.records:
            return []
        reach = max(0.0, float(radius_m)) + self.max_radius_m
        cx, cy = self._cell(x, y)
        d = int(math.ceil(reach / self.cell_size_m))
        rows: list[VegetationRecord] = []
        for ix in range(cx - d, cx + d + 1):
            for iy in range(cy - d, cy + d + 1):
                rows.extend(self._buckets.get((ix, iy), []))
        return rows

    def clearance_at(self, x: float, y: float, system_kw: float = 1.0,
                     service_clearance_m: float = 1.5,
                     min_blocker_height_m: float = 1.5) -> dict[str, Any]:
        required = required_vegetation_clearance_radius_m(system_kw, service_clearance_m)
        panel_radius = panel_footprint_radius_m(system_kw)
        base = {
            "available": self.available,
            "source": self.source,
            "installable": None,
            "status": "unknown",
            "tree_count": len(self.records),
            "clearance_radius_m": _round(required, 2),
            "panel_footprint_radius_m": _round(panel_radius, 2),
            "service_clearance_m": _round(service_clearance_m, 2),
            "min_blocker_height_m": _round(min_blocker_height_m, 2),
            "intersecting_crowns_count": 0,
            "intersecting_crowns": [],
            "clearing_required": False,
            "recommendation": "Vegetation inventory unavailable; field-check the panel footprint.",
        }
        if not self.records:
            return base
        blockers = []
        nearest = None
        nearest_clearance = None
        for rec in self._nearby(x, y, required):
            if rec.height_m < min_blocker_height_m:
                continue
            center_dist = math.hypot(float(x) - rec.x, float(y) - rec.y)
            crown_clearance = center_dist - rec.radius_m - required
            if nearest_clearance is None or crown_clearance < nearest_clearance:
                nearest_clearance = crown_clearance
                nearest = rec, center_dist
            if crown_clearance < 0:
                blockers.append((crown_clearance, center_dist, rec))
        blockers.sort(key=lambda row: (row[0], row[1], row[2].id))
        installable = len(blockers) == 0
        def rec_payload(rec: VegetationRecord, center_dist: float, clearance: float) -> dict[str, Any]:
            return {
                "id": rec.id,
                "kind": rec.kind,
                "type": rec.type,
                "species": rec.species,
                "source": rec.source,
                "confidence": _round(rec.confidence, 3),
                "height_m": _round(rec.height_m, 2),
                "radius_m": _round(rec.radius_m, 2),
                "center_distance_m": _round(center_dist, 2),
                "crown_clearance_m": _round(clearance, 2),
            }
        out = dict(base)
        out.update({
            "installable": installable,
            "status": "open" if installable else "tree_conflict",
            "intersecting_crowns_count": len(blockers),
            "intersecting_crowns": [
                rec_payload(rec, center_dist, clearance)
                for clearance, center_dist, rec in blockers[:8]
            ],
            "clearing_required": not installable,
            "nearest_crown_clearance_m": _round(nearest_clearance, 2),
            "recommendation": (
                "Open footprint in the current vegetation inventory."
                if installable else
                "Do not recommend as-is: panel footprint intersects vegetation crowns."
            ),
        })
        if nearest is not None:
            rec, center_dist = nearest
            out["nearest_vegetation"] = rec_payload(rec, center_dist, nearest_clearance or 0.0)
        return out


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _col(row: dict[str, str], stem: str) -> str | None:
    for key, value in row.items():
        if key == stem or key.startswith(stem + " "):
            return value
    return None


def _daymet_date(year: int, yday: int) -> str:
    m, d = 1, int(yday)
    for n in MONTH_DAYS:
        if d <= n:
            return "%04d-%02d-%02d" % (int(year), m, d)
        d -= n
        m += 1
    return "%04d-12-31" % int(year)


def read_daymet_csv(path: str) -> list[dict[str, Any]]:
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("year,"))
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        year = int(_col(r, "year"))
        yday = int(_col(r, "yday"))
        tmax = float(_col(r, "tmax"))
        tmin = float(_col(r, "tmin"))
        srad = float(_col(r, "srad"))
        dayl = float(_col(r, "dayl"))
        rows.append({
            "date": _daymet_date(year, yday),
            "year": year,
            "month": int(_daymet_date(year, yday)[5:7]),
            "yday": yday,
            "tmean_c": (tmax + tmin) / 2.0,
            "srad_w_m2": srad,
            "dayl_s": dayl,
            "rs_mj_m2_d": max(0.0, srad * dayl / 1_000_000.0),
        })
    return rows


def solar_position(lat_deg: float, yday: int, solar_hour: float) -> dict[str, float]:
    """Approximate apparent solar position from local solar time.

    Azimuth is degrees clockwise from north, matching the viewshed horizon.
    """
    lat = math.radians(float(lat_deg))
    j = int(yday)
    decl = 0.409 * math.sin(2.0 * math.pi * j / 365.0 - 1.39)
    h = math.radians(15.0 * (float(solar_hour) - 12.0))
    sin_alt = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(h)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)
    cos_alt = max(1e-9, math.cos(alt))
    sin_az = -math.cos(decl) * math.sin(h) / cos_alt
    cos_az = (math.sin(decl) - math.sin(alt) * math.sin(lat)) / max(1e-9, cos_alt * math.cos(lat))
    az = (math.degrees(math.atan2(sin_az, cos_az)) + 360.0) % 360.0
    return {
        "altitude_deg": math.degrees(alt),
        "azimuth_deg": az,
        "zenith_deg": 90.0 - math.degrees(alt),
        "cos_zenith": max(0.0, math.sin(alt)),
    }


def clear_sky_components(lat_deg: float, elev_m: float, yday: int, solar_hour: float) -> dict[str, float]:
    pos = solar_position(lat_deg, yday, solar_hour)
    cosz = pos["cos_zenith"]
    if cosz <= 0.0:
        return {**pos, "ghi_wm2": 0.0, "dni_wm2": 0.0, "dhi_wm2": 0.0, "e0h_wm2": 0.0}
    i0 = SOLAR_CONSTANT_WM2 * (1.0 + 0.033 * math.cos(2.0 * math.pi * int(yday) / 365.0))
    zen = 90.0 - pos["altitude_deg"]
    pressure_ratio = math.exp(-max(0.0, float(elev_m)) / 8434.5)
    m_rel = 1.0 / (cosz + 0.50572 * max(1e-6, 96.07995 - zen) ** -1.6364)
    m = m_rel * pressure_ratio
    # Same compact Bird-Hulstrom-style constants used by twin_astro.solar_irradiance.
    ozone_cm = 0.30
    water_cm = 1.50
    aerosol_tau = 0.10
    t_rayleigh = math.exp(-0.0903 * (m ** 0.84) * (1.0 + m - (m ** 1.01)))
    uo = ozone_cm * m
    t_ozone = 1.0 - 0.1611 * uo * (1.0 + 139.48 * uo) ** -0.3035 - 0.002715 * uo / (1.0 + 0.044 * uo + 0.0003 * uo * uo)
    t_gas = math.exp(-0.0127 * (m ** 0.26))
    uw = water_cm * m
    t_water = 1.0 - 2.4959 * uw / ((1.0 + 79.034 * uw) ** 0.6828 + 6.385 * uw)
    t_aerosol = math.exp(-(aerosol_tau ** 0.873) * (1.0 + aerosol_tau - aerosol_tau ** 0.7088) * (m ** 0.9108))
    dni = max(0.0, 0.9662 * i0 * t_rayleigh * t_ozone * t_gas * t_water * t_aerosol)
    diffuse_rayleigh = i0 * cosz * t_ozone * t_gas * t_water * (1.0 - t_rayleigh) * 0.5
    diffuse_aerosol = i0 * cosz * t_ozone * t_gas * t_water * t_rayleigh * (1.0 - t_aerosol) * 0.75
    dhi = max(0.0, diffuse_rayleigh + diffuse_aerosol)
    ghi = max(0.0, dni * cosz + dhi)
    return {**pos, "ghi_wm2": ghi, "dni_wm2": dni, "dhi_wm2": dhi, "e0h_wm2": i0 * cosz}


def _daily_clear_ghi_mj(lat_deg: float, elev_m: float, yday: int, step_h: float = 1.0) -> float:
    total_wh = 0.0
    steps = int(round(24.0 / step_h))
    for i in range(steps):
        hour = (i + 0.5) * step_h
        total_wh += clear_sky_components(lat_deg, elev_m, yday, hour)["ghi_wm2"] * step_h
    return total_wh * 0.0036


@lru_cache(maxsize=16)
def climate_normals(data_dir: str, lat_deg: float, elev_m: float) -> dict[str, Any]:
    data_dir = os.path.abspath(data_dir)
    daymet_path = os.path.join(data_dir, "climate", "daymet_daily.csv")
    months = []
    if not os.path.exists(daymet_path):
        for m, yday in enumerate(MONTH_MID_YDAY, start=1):
            clear = _daily_clear_ghi_mj(lat_deg, elev_m, yday)
            months.append({
                "month": m,
                "yday": yday,
                "days": MONTH_DAYS[m - 1],
                "tmean_c": 15.0,
                "all_sky_rs_mj_m2_d": clear,
                "clear_sky_rs_mj_m2_d": clear,
                "clearness_index": 1.0,
                "records": 0,
            })
        return {
            "available": False,
            "source": "clear-sky fallback; missing data/climate/daymet_daily.csv",
            "months": months,
            "cloud_loss_pct": 0.0,
        }
    rows = read_daymet_csv(daymet_path)
    annual_all = 0.0
    annual_clear = 0.0
    for m, yday in enumerate(MONTH_MID_YDAY, start=1):
        recs = [r for r in rows if r["month"] == m]
        all_sky = sum(r["rs_mj_m2_d"] for r in recs) / len(recs) if recs else 0.0
        tmean = sum(r["tmean_c"] for r in recs) / len(recs) if recs else 15.0
        clear = _daily_clear_ghi_mj(lat_deg, elev_m, yday)
        kt = max(0.15, min(1.10, all_sky / clear)) if clear > 0 else 0.0
        annual_all += all_sky * MONTH_DAYS[m - 1]
        annual_clear += clear * MONTH_DAYS[m - 1]
        months.append({
            "month": m,
            "yday": yday,
            "days": MONTH_DAYS[m - 1],
            "tmean_c": tmean,
            "all_sky_rs_mj_m2_d": all_sky,
            "clear_sky_rs_mj_m2_d": clear,
            "clearness_index": kt,
            "records": len(recs),
        })
    cloud_loss = 0.0 if annual_clear <= 0 else max(0.0, min(100.0, 100.0 * (1.0 - annual_all / annual_clear)))
    return {
        "available": True,
        "source": "Daymet daily all-sky shortwave climatology",
        "daymet_daily_csv": os.path.relpath(daymet_path, data_dir),
        "records": len(rows),
        "months": months,
        "annual_all_sky_rs_kwh_m2": annual_all / 3.6,
        "annual_clear_sky_rs_kwh_m2": annual_clear / 3.6,
        "cloud_loss_pct": cloud_loss,
    }


def erbs_diffuse_fraction(kt: float) -> float:
    kt = max(0.0, min(2.0, float(kt)))
    if kt <= 0.22:
        return max(0.0, min(1.0, 1.0 - 0.09 * kt))
    if kt <= 0.80:
        kd = 0.9511 - 0.1604 * kt + 4.388 * kt ** 2 - 16.638 * kt ** 3 + 12.336 * kt ** 4
        return max(0.0, min(1.0, kd))
    return 0.165


def horizon_at_azimuth(horizon_deg: list[float] | tuple[float, ...] | Any, azimuth_deg: float) -> float:
    if horizon_deg is None:
        return -90.0
    arr = list(horizon_deg)
    if not arr:
        return -90.0
    pos = ((float(azimuth_deg) % 360.0) / 360.0) * len(arr)
    i0 = int(math.floor(pos)) % len(arr)
    i1 = (i0 + 1) % len(arr)
    t = pos - math.floor(pos)
    a = float(arr[i0])
    b = float(arr[i1])
    if not math.isfinite(a):
        a = -90.0
    if not math.isfinite(b):
        b = -90.0
    return a * (1.0 - t) + b * t


def sky_view_fraction(horizon_deg: Any) -> float:
    if horizon_deg is None:
        return 1.0
    vals = [float(v) for v in list(horizon_deg) if v is not None and math.isfinite(float(v))]
    if not vals:
        return 1.0
    # A compact diffuse-sky proxy: fully open at/below horizon, linearly closed by 45 deg.
    factors = [max(0.0, min(1.0, 1.0 - max(0.0, v) / 45.0)) for v in vals]
    return sum(factors) / len(factors)


def cos_incidence(altitude_deg: float, azimuth_deg: float, tilt_deg: float, panel_azimuth_deg: float) -> float:
    alt = math.radians(float(altitude_deg))
    beta = math.radians(float(tilt_deg))
    daz = math.radians(float(azimuth_deg) - float(panel_azimuth_deg))
    return max(0.0, math.sin(alt) * math.cos(beta) + math.cos(alt) * math.sin(beta) * math.cos(daz))


def evaluate_fixed(
    site: SolarSite,
    normals: dict[str, Any],
    horizon_deg: Any = None,
    tilt_deg: float | None = None,
    azimuth_deg: float | None = None,
    system_kw: float = 1.0,
    step_h: float = 1.0,
    losses: float = DEFAULT_LOSSES,
) -> dict[str, Any]:
    tilt = float(tilt_deg if tilt_deg is not None else max(5.0, min(60.0, abs(site.lat))))
    panel_az = float(azimuth_deg if azimuth_deg is not None else (180.0 if site.lat >= 0 else 0.0))
    sky_view = sky_view_fraction(horizon_deg)
    system_kw = max(0.0, float(system_kw or 0.0))
    monthly = []
    annual_poa = annual_poa_no_shade = annual_ghi = annual_clear_ghi = annual_pv_per_kw = 0.0
    annual_winter_poa = annual_summer_poa = 0.0
    steps = int(round(24.0 / step_h))
    for month in normals["months"]:
        poa_wh = poa_no_shade_wh = ghi_wh = clear_ghi_wh = pv_wh_per_kw = 0.0
        for i in range(steps):
            hour = (i + 0.5) * step_h
            clear = clear_sky_components(site.lat, site.elevation_m, month["yday"], hour)
            if clear["ghi_wm2"] <= 0.0:
                continue
            clear_ghi_wh += clear["ghi_wm2"] * step_h
            clearness = float(month.get("clearness_index", 1.0))
            ghi = max(0.0, clear["ghi_wm2"] * clearness)
            e0h = max(1e-6, clear["e0h_wm2"])
            kt = max(0.0, min(1.2, ghi / e0h))
            kd = erbs_diffuse_fraction(kt)
            dhi = min(ghi, ghi * kd)
            dni = max(0.0, (ghi - dhi) / max(1e-6, clear["cos_zenith"]))
            ci = cos_incidence(clear["altitude_deg"], clear["azimuth_deg"], tilt, panel_az)
            beam_no_shade = dni * ci
            diffuse_no_shade = dhi * (1.0 + math.cos(math.radians(tilt))) / 2.0
            ground = ghi * GROUND_ALBEDO * (1.0 - math.cos(math.radians(tilt))) / 2.0
            no_shade = max(0.0, beam_no_shade + diffuse_no_shade + ground)
            blocked = clear["altitude_deg"] <= horizon_at_azimuth(horizon_deg, clear["azimuth_deg"])
            beam = 0.0 if blocked else beam_no_shade
            diffuse = diffuse_no_shade * sky_view
            poa = max(0.0, beam + diffuse + ground)
            cell_temp = float(month.get("tmean_c", 15.0)) + (poa / 800.0) * 20.0
            temp_factor = max(0.75, min(1.08, 1.0 + TEMP_COEFF_PER_C * (cell_temp - 25.0)))
            pv_wh_per_kw += poa * step_h * temp_factor * max(0.0, 1.0 - losses)
            poa_wh += poa * step_h
            poa_no_shade_wh += no_shade * step_h
            ghi_wh += ghi * step_h
        days = int(month["days"])
        poa_kwh = poa_wh * days / 1000.0
        poa_no_shade_kwh = poa_no_shade_wh * days / 1000.0
        ghi_kwh = ghi_wh * days / 1000.0
        clear_ghi_kwh = clear_ghi_wh * days / 1000.0
        pv_kwh_per_kw = pv_wh_per_kw * days / 1000.0
        shade_loss = 0.0 if poa_no_shade_kwh <= 0 else max(0.0, 100.0 * (1.0 - poa_kwh / poa_no_shade_kwh))
        row = {
            "month": int(month["month"]),
            "poa_kwh_m2": _round(poa_kwh, 3),
            "pv_kwh_per_kwdc": _round(pv_kwh_per_kw, 3),
            "pv_kwh": _round(pv_kwh_per_kw * system_kw, 3),
            "ghi_kwh_m2": _round(ghi_kwh, 3),
            "clear_ghi_kwh_m2": _round(clear_ghi_kwh, 3),
            "shade_loss_pct": _round(shade_loss, 2),
            "clearness_index": _round(month.get("clearness_index", 1.0), 3),
        }
        monthly.append(row)
        annual_poa += poa_kwh
        annual_poa_no_shade += poa_no_shade_kwh
        annual_ghi += ghi_kwh
        annual_clear_ghi += clear_ghi_kwh
        annual_pv_per_kw += pv_kwh_per_kw
        if int(month["month"]) in {11, 12, 1, 2}:
            annual_winter_poa += poa_kwh
        if int(month["month"]) in {5, 6, 7, 8}:
            annual_summer_poa += poa_kwh
    shade_loss = 0.0 if annual_poa_no_shade <= 0 else max(0.0, 100.0 * (1.0 - annual_poa / annual_poa_no_shade))
    cloud_loss = float(normals.get("cloud_loss_pct", 0.0) or 0.0)
    return {
        "tilt_deg": _round(tilt, 2),
        "azimuth_deg": _round(panel_az % 360.0, 2),
        "system_kw": _round(system_kw, 3),
        "annual": {
            "poa_kwh_m2": _round(annual_poa, 2),
            "pv_kwh_per_kwdc": _round(annual_pv_per_kw, 2),
            "pv_kwh": _round(annual_pv_per_kw * system_kw, 2),
            "ghi_kwh_m2": _round(annual_ghi, 2),
            "clear_sky_ghi_kwh_m2": _round(annual_clear_ghi, 2),
            "winter_poa_kwh_m2": _round(annual_winter_poa, 2),
            "summer_poa_kwh_m2": _round(annual_summer_poa, 2),
            "shade_loss_pct": _round(shade_loss, 2),
            "cloud_loss_pct": _round(cloud_loss, 2),
            "sky_view_fraction": _round(sky_view, 4),
        },
        "monthly": monthly,
    }


def optimize_fixed(
    site: SolarSite,
    normals: dict[str, Any],
    horizon_deg: Any = None,
    objective: str = "annual_kwh",
    system_kw: float = 1.0,
) -> dict[str, Any]:
    obj = str(objective or "annual_kwh").lower()
    best = None
    base_az = 180 if site.lat >= 0 else 0
    azimuths = list(range(max(0, base_az - 90), min(360, base_az + 91), 10))
    tilts = list(range(0, 71, 5))
    for tilt in tilts:
        for az in azimuths:
            result = evaluate_fixed(site, normals, horizon_deg, tilt, az, system_kw=system_kw)
            annual = result["annual"]
            if "winter" in obj:
                score = annual["winter_poa_kwh_m2"]
            elif "summer" in obj:
                score = annual["summer_poa_kwh_m2"]
            else:
                score = annual["pv_kwh_per_kwdc"]
            row = (float(score or 0.0), tilt, az, result)
            if best is None or row[0] > best[0]:
                best = row
    assert best is not None
    # One-degree local refinement around the coarse best.
    _score, tilt0, az0, _result = best
    for tilt in range(max(0, tilt0 - 6), min(75, tilt0 + 7)):
        for az in range(max(0, az0 - 6), min(360, az0 + 7)):
            result = evaluate_fixed(site, normals, horizon_deg, tilt, az, system_kw=system_kw)
            annual = result["annual"]
            if "winter" in obj:
                score = annual["winter_poa_kwh_m2"]
            elif "summer" in obj:
                score = annual["summer_poa_kwh_m2"]
            else:
                score = annual["pv_kwh_per_kwdc"]
            if score > best[0]:
                best = (float(score), tilt, az, result)
    return best[3]


def analyze_site(
    site: SolarSite,
    data_dir: str = DATA_DIR,
    horizon_deg: Any = None,
    tilt_deg: float | None = None,
    azimuth_deg: float | None = None,
    system_kw: float = 1.0,
    objective: str = "annual_kwh",
) -> dict[str, Any]:
    normals = climate_normals(os.path.abspath(data_dir), round(site.lat, 6), round(site.elevation_m, 1))
    if tilt_deg is None or azimuth_deg is None:
        result = optimize_fixed(site, normals, horizon_deg, objective=objective, system_kw=system_kw)
        optimized = True
    else:
        result = evaluate_fixed(site, normals, horizon_deg, tilt_deg, azimuth_deg, system_kw=system_kw)
        optimized = False
    result.update({
        "optimized": optimized,
        "objective": objective,
        "climate": {
            "available": bool(normals.get("available")),
            "source": normals.get("source"),
            "cloud_loss_pct": _round(normals.get("cloud_loss_pct", 0.0), 2),
        },
        "model": {
            "irradiance": "clear-sky Bird-Hulstrom-style shape scaled to Daymet all-sky monthly normals",
            "decomposition": "Erbs diffuse fraction",
            "transposition": "isotropic sky diffuse + direct beam + ground-reflected POA",
            "pv": "PVWatts-style fixed panel, 14% default system losses, -0.4%/C module temperature coefficient",
            "bankability": "planning-grade; validate with NSRDB/PVGIS or site measurements before investment",
        },
    })
    return result


def summary_payload(data_dir: str, lat: float, elev: float) -> dict[str, Any]:
    normals = climate_normals(os.path.abspath(data_dir), round(lat, 6), round(elev, 1))
    return {
        "climate": {
            "available": bool(normals.get("available")),
            "source": normals.get("source"),
            "records": normals.get("records", 0),
            "annual_all_sky_rs_kwh_m2": _round(normals.get("annual_all_sky_rs_kwh_m2"), 2),
            "annual_clear_sky_rs_kwh_m2": _round(normals.get("annual_clear_sky_rs_kwh_m2"), 2),
            "cloud_loss_pct": _round(normals.get("cloud_loss_pct", 0.0), 2),
            "monthly": [
                {
                    "month": m["month"],
                    "clearness_index": _round(m.get("clearness_index"), 3),
                    "all_sky_rs_mj_m2_d": _round(m.get("all_sky_rs_mj_m2_d"), 3),
                    "clear_sky_rs_mj_m2_d": _round(m.get("clear_sky_rs_mj_m2_d"), 3),
                    "tmean_c": _round(m.get("tmean_c"), 2),
                }
                for m in normals.get("months", [])
            ],
        },
        "provenance": {
            "tool": "twin_solar.py",
            "date": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    }
