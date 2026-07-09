#!/usr/bin/env python3
"""Astronomy math for VEIL twins.

Pure helpers over astronomy-engine plus committed sky catalogs. This module
does not read or write the twin store; callers pass a site or use data/georef.json
for the default observer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import difflib
import importlib.metadata
import json
import math
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

try:
    import astronomy
except ImportError as exc:  # pragma: no cover - exercised only on missing deps
    venv_site = os.path.join(PROJECT, ".venv-mcp", "lib")
    if os.path.isdir(venv_site):
        for name in sorted(os.listdir(venv_site)):
            candidate = os.path.join(venv_site, name, "site-packages")
            if os.path.isdir(candidate) and candidate not in sys.path:
                sys.path.insert(0, candidate)
    try:
        import astronomy
    except ImportError:
        raise RuntimeError("astronomy-engine is required; install requirements.txt") from exc


DATA_DIR = os.path.abspath(os.environ.get("TWIN_DATA_DIR")
                           or os.path.join(PROJECT, "data"))
GEOREF_PATH = os.path.join(DATA_DIR, "georef.json")
TERRAIN_GRID = os.path.join(DATA_DIR, "terrain", "grid.json")
STARS_PATH = os.path.join(PROJECT, "public", "astronomy-data", "stars.json")
CONSTELLATIONS_PATH = os.path.join(PROJECT, "public", "astronomy-data", "constellations.json")
HORIZONS_REFERENCE = os.path.join(DATA_DIR, "astronomy", "horizons-reference.json")

MIN_UTC_MS = -11676096000000  # 1600-01-01T00:00:00Z
MAX_UTC_MS = 16725225600000   # 2500-01-01T00:00:00Z
MAX_RATE = 604800.0
AU_KM = 149_597_870.7
BODY_RADII_KM = {
    "sun": 695_700.0,
    "moon": 1_737.4,
    "mercury": 2_439.7,
    "venus": 6_051.8,
    "mars": 3_389.5,
    "jupiter": 69_911.0,
    "saturn": 58_232.0,
    "uranus": 25_362.0,
    "neptune": 24_622.0,
}

BODY_ALIASES = {
    "sun": astronomy.Body.Sun,
    "moon": astronomy.Body.Moon,
    "mercury": astronomy.Body.Mercury,
    "venus": astronomy.Body.Venus,
    "mars": astronomy.Body.Mars,
    "jupiter": astronomy.Body.Jupiter,
    "saturn": astronomy.Body.Saturn,
    "uranus": astronomy.Body.Uranus,
    "neptune": astronomy.Body.Neptune,
}
PLANET_NAMES = ["mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"]
NAKED_EYE_PLANETS = ["mercury", "venus", "mars", "jupiter", "saturn"]
SUPERMOON_MAX_DISTANCE_KM = 360_000.0
MIN_OBSERVABLE_ELONGATION_DEG = 15.0


class AstronomyNameError(ValueError):
    def __init__(self, name: str, payload: dict[str, Any]):
        super().__init__(payload["error"])
        self.name = name
        self.payload = payload


@dataclass(frozen=True)
class Site:
    lat: float
    lon: float
    height_m: float = 0.0

    @property
    def observer(self):
        return astronomy.Observer(self.lat, self.lon, self.height_m)


def _engine_version() -> str:
    try:
        return importlib.metadata.version("astronomy-engine")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def provenance() -> dict[str, str]:
    return {
        "source": f"astronomy-engine {_engine_version()}",
        "validated_against": "JPL Horizons (data/astronomy/horizons-reference.json)",
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any | None) -> datetime:
    if value is None or value == "":
        return _utc_now()
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    else:
        s = str(value).strip()
        if s.lower() == "now":
            return _utc_now()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def unix_ms(dt: datetime) -> int:
    return int(round(dt.timestamp() * 1000))


def datetime_from_unix_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def clamp_unix_ms(ms: int) -> int:
    return max(MIN_UTC_MS, min(MAX_UTC_MS, int(ms)))


def clamp_rate(rate: Any) -> float:
    try:
        r = float(rate)
    except (TypeError, ValueError):
        r = 1.0
    if not math.isfinite(r):
        r = 1.0
    return max(-MAX_RATE, min(MAX_RATE, r))


def normalize_time(value: Any | None = None) -> tuple[datetime, int, Any]:
    ms = clamp_unix_ms(unix_ms(_parse_datetime(value)))
    dt = datetime_from_unix_ms(ms)
    return dt, ms, astronomy.Time(dt.isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def iso_from_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_from_time(time: Any) -> str:
    dt = time.Utc().replace(tzinfo=timezone.utc)
    return iso_from_dt(dt)


def unix_ms_from_time(time: Any) -> int:
    return unix_ms(time.Utc().replace(tzinfo=timezone.utc))


def time_payload(time: Any) -> dict[str, Any]:
    return {"iso": iso_from_time(time), "unix_ms": unix_ms_from_time(time)}


def site_from_georef(path: str = GEOREF_PATH) -> Site:
    with open(path) as fh:
        georef = json.load(fh)
    origin = georef.get("origin_wgs84") or {}
    lon = origin.get("lon")
    lat = origin.get("lat")
    if lon is None or lat is None:
        sys.path.insert(0, HERE)
        import twin_georef
        projected_to_geo, _ = twin_georef.transformers(path)
        ox, oy = georef["origin_utm"][:2]
        lon, lat = projected_to_geo.transform(float(ox), float(oy))
    height = georef.get("grid_min_elevation_m")
    if height is None and os.path.exists(TERRAIN_GRID):
        try:
            height = json.load(open(TERRAIN_GRID)).get("minElevation")
        except Exception:
            height = 0.0
    return Site(float(lat), float(lon), float(height or 0.0))


def site_from_mapping(value: dict[str, Any] | None = None) -> Site:
    if not value:
        return site_from_georef()
    return Site(
        lat=float(value["lat"]),
        lon=float(value["lon"]),
        height_m=float(value.get("height_m", value.get("heightM", value.get("height", 0.0))) or 0.0),
    )


@lru_cache(maxsize=1)
def star_catalog() -> dict[str, Any]:
    data = json.load(open(STARS_PATH))
    rows = []
    by_name = {}
    by_hip = {}
    for ra, dec, mag, bv, hip, name in data.get("stars", []):
        row = {
            "ra_deg": float(ra),
            "ra_hours": float(ra) / 15.0,
            "dec_deg": float(dec),
            "mag": float(mag),
            "bv": float(bv),
            "hip": int(hip),
            "name": str(name or ""),
        }
        rows.append(row)
        by_hip[row["hip"]] = row
        if row["name"]:
            by_name[row["name"].lower()] = row
    return {"meta": data, "rows": rows, "by_name": by_name, "by_hip": by_hip}


@lru_cache(maxsize=1)
def constellation_catalog() -> dict[str, Any]:
    data = json.load(open(CONSTELLATIONS_PATH))
    by_name = {}
    for abbr, name in data.get("names", {}).items():
        by_name[abbr.lower()] = {"abbr": abbr, "name": name}
        by_name[str(name).lower()] = {"abbr": abbr, "name": name}
    return {"meta": data, "by_name": by_name}


def valid_name_payload(name: str) -> dict[str, Any]:
    stars = sorted(star_catalog()["by_name"].keys())
    const = constellation_catalog()["by_name"]
    const_names = sorted({v["name"] for v in const.values()})
    candidates = list(BODY_ALIASES) + stars + const_names + [v["abbr"] for v in const.values()]
    suggestions = difflib.get_close_matches(str(name).lower(), candidates, n=8, cutoff=0.5)
    return {
        "error": f"unknown sky target: {name!r}",
        "valid_categories": {
            "bodies": sorted(BODY_ALIASES),
            "stars": [star_catalog()["by_name"][s]["name"] for s in stars[:200]],
            "constellations": const_names,
        },
        "suggestions": suggestions,
    }


def resolve_body(name: str):
    key = str(name or "").strip().lower()
    if key in BODY_ALIASES:
        return key, BODY_ALIASES[key]
    raise AstronomyNameError(name, valid_name_payload(name))


def resolve_target(name: str) -> dict[str, Any]:
    key = str(name or "").strip().lower()
    if key in BODY_ALIASES:
        body_name, body = resolve_body(key)
        return {"target_type": "body", "name": body_name, "body": body}
    star = star_catalog()["by_name"].get(key)
    if star:
        return {"target_type": "star", "name": star["name"], "star": star}
    const = constellation_catalog()["by_name"].get(key)
    if const:
        return {"target_type": "constellation", **const}
    raise AstronomyNameError(name, valid_name_payload(name))


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _angle_diameter_deg(name: str, dist_au: float | None, moon_diam_deg: float | None = None) -> float | None:
    if name == "moon" and moon_diam_deg is not None:
        return moon_diam_deg
    radius = BODY_RADII_KM.get(name)
    if radius is None or not dist_au:
        return None
    return math.degrees(2.0 * math.asin(radius / (float(dist_au) * AU_KM)))


def _constellation_from_j2000(ra_hours: float, dec_deg: float) -> dict[str, str]:
    info = astronomy.Constellation(float(ra_hours), float(dec_deg))
    return {"abbr": info.symbol, "name": info.name}


def _moon_phase_name(angle: float) -> str:
    a = angle % 360.0
    names = [
        (22.5, "new moon"),
        (67.5, "waxing crescent"),
        (112.5, "first quarter"),
        (157.5, "waxing gibbous"),
        (202.5, "full moon"),
        (247.5, "waning gibbous"),
        (292.5, "last quarter"),
        (337.5, "waning crescent"),
        (360.0, "new moon"),
    ]
    for limit, name in names:
        if a < limit:
            return name
    return "new moon"


def _event_payload(time: Any | None, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if time is None:
        return None
    payload = time_payload(time)
    if extra:
        payload.update(extra)
    return payload


def _rise_set(body: Any, observer: Any, time: Any, direction: Any, limit_days: float = 370.0) -> dict[str, Any] | None:
    try:
        found = astronomy.SearchRiseSet(body, observer, direction, time, limit_days)
    except Exception:
        return None
    return _event_payload(found)


def _culmination(body: Any, observer: Any, time: Any) -> dict[str, Any] | None:
    try:
        event = astronomy.SearchHourAngle(body, observer, 0.0, time)
    except Exception:
        return None
    return _event_payload(event.time, {"altitude_deg": _round(event.hor.altitude, 4)})


def _define_star(row: dict[str, Any], slot: Any = astronomy.Body.Star1) -> Any:
    astronomy.DefineStar(slot, row["ra_hours"], row["dec_deg"], 1000.0)
    return slot


def body_position(body: str, time: Any | None = None, site: Site | dict[str, Any] | None = None) -> dict[str, Any]:
    target = resolve_target(body)
    if target["target_type"] == "constellation":
        raise AstronomyNameError(body, {"error": "body_position expects a body or named star", "target": body})
    return target_position(target, time=time, site=site)


def target_position(target: dict[str, Any], time: Any | None = None, site: Site | dict[str, Any] | None = None) -> dict[str, Any]:
    dt, ms, astro_time = normalize_time(time)
    site_obj = site if isinstance(site, Site) else site_from_mapping(site)
    obs = site_obj.observer
    if target["target_type"] == "star":
        row = target["star"]
        engine_body = _define_star(row)
        name = row["name"]
        target_type = "star"
    else:
        name = target["name"]
        engine_body = target["body"]
        row = None
        target_type = "body"

    eq = astronomy.Equator(engine_body, astro_time, obs, True, True)
    if target_type == "star":
        j2000_ra = float(row["ra_hours"])
        j2000_dec = float(row["dec_deg"])
    else:
        eqj = astronomy.Equator(engine_body, astro_time, obs, False, True)
        j2000_ra = float(eqj.ra)
        j2000_dec = float(eqj.dec)
    hor = astronomy.Horizon(astro_time, obs, eq.ra, eq.dec, astronomy.Refraction.Airless)
    hor_ref = astronomy.Horizon(astro_time, obs, eq.ra, eq.dec, astronomy.Refraction.Normal)

    out: dict[str, Any] = {
        "target_type": target_type,
        "name": name,
        "time": {"iso": iso_from_dt(dt), "unix_ms": ms},
        "azimuth_deg": _round(hor.azimuth, 5),
        "altitude_deg": _round(hor.altitude, 5),
        "altitude_refracted_deg": _round(hor_ref.altitude, 5),
        "ra_ofdate_hours": _round(eq.ra, 8),
        "dec_ofdate_deg": _round(eq.dec, 8),
        "ra_j2000_hours": _round(j2000_ra, 8),
        "ra_j2000_deg": _round(j2000_ra * 15.0, 8),
        "dec_j2000_deg": _round(j2000_dec, 8),
        "constellation": _constellation_from_j2000(j2000_ra, j2000_dec),
        "next_rise": _rise_set(engine_body, obs, astro_time, astronomy.Direction.Rise),
        "next_set": _rise_set(engine_body, obs, astro_time, astronomy.Direction.Set),
        "next_culmination": _culmination(engine_body, obs, astro_time),
        "provenance": provenance(),
    }
    if target_type == "star":
        out.update({
            "hip": row["hip"],
            "magnitude": row["mag"],
            "bv": row["bv"],
        })
        return out

    out["distance_au"] = _round(eq.dist, 9)
    out["distance_km"] = _round(eq.dist * AU_KM, 1)
    try:
        illum = astronomy.Illumination(engine_body, astro_time)
        out.update({
            "magnitude": _round(illum.mag, 4),
            "phase_angle_deg": _round(illum.phase_angle, 4),
            "illuminated_fraction": _round(illum.phase_fraction, 6),
        })
    except Exception:
        illum = None
    moon_diam = None
    if name == "moon":
        phase_angle = astronomy.MoonPhase(astro_time)
        lib = astronomy.Libration(astro_time)
        moon_diam = lib.diam_deg
        out["moon_phase"] = {
            "angle_deg": _round(phase_angle, 4),
            "name": _moon_phase_name(phase_angle),
            "illuminated_fraction": _round((1.0 - math.cos(math.radians(phase_angle))) / 2.0, 6),
            "libration_lon_deg": _round(lib.elon, 4),
            "libration_lat_deg": _round(lib.elat, 4),
        }
    out["angular_diameter_deg"] = _round(_angle_diameter_deg(name, eq.dist, moon_diam), 6)
    return out


def sun_moon_planet_positions(time: Any | None = None, site: Site | dict[str, Any] | None = None) -> dict[str, Any]:
    site_obj = site if isinstance(site, Site) else site_from_mapping(site)
    return {
        name: body_position(name, time=time, site=site_obj)
        for name in ["sun", "moon", *PLANET_NAMES]
    }


def twilight_kind(sun_altitude_deg: float) -> str:
    if sun_altitude_deg >= 0:
        return "day"
    if sun_altitude_deg >= -6:
        return "civil_twilight"
    if sun_altitude_deg >= -12:
        return "nautical_twilight"
    if sun_altitude_deg >= -18:
        return "astronomical_twilight"
    return "night"


def _midnight_utc(dt: datetime) -> Any:
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return astronomy.Time(start.isoformat(timespec="seconds").replace("+00:00", "Z"))


def sky_at(time: Any | None = None, site: Site | dict[str, Any] | None = None) -> dict[str, Any]:
    dt, ms, astro_time = normalize_time(time)
    site_obj = site if isinstance(site, Site) else site_from_mapping(site)
    obs = site_obj.observer
    sun = body_position("sun", time=dt, site=site_obj)
    moon = body_position("moon", time=dt, site=site_obj)
    planets = []
    for name in PLANET_NAMES:
        pos = body_position(name, time=dt, site=site_obj)
        if pos["altitude_refracted_deg"] > 0 and (name not in {"uranus", "neptune"} or pos.get("magnitude", 99) <= 6.5):
            planets.append(pos)
    day_start = _midnight_utc(dt)
    phase = astronomy.MoonPhase(astro_time)
    return {
        "time": {"iso": iso_from_dt(dt), "unix_ms": ms},
        "site": {"lat": site_obj.lat, "lon": site_obj.lon, "height_m": site_obj.height_m},
        "sun": sun,
        "moon": moon,
        "moon_phase": {
            "angle_deg": _round(phase, 4),
            "name": _moon_phase_name(phase),
            "illuminated_fraction": _round((1.0 - math.cos(math.radians(phase))) / 2.0, 6),
        },
        "is_night": sun["altitude_deg"] < -6.0,
        "night_definition": "is_night means sun below -6 deg (end of civil twilight); "
                            "the twilight field's 'night' means below -18 deg (astronomical dark)",
        "twilight": twilight_kind(sun["altitude_deg"]),
        "visible_planets": planets,
        "events_today": {
            "sunrise": _rise_set(astronomy.Body.Sun, obs, day_start, astronomy.Direction.Rise, 2.0),
            "sunset": _rise_set(astronomy.Body.Sun, obs, day_start, astronomy.Direction.Set, 2.0),
            "moonrise": _rise_set(astronomy.Body.Moon, obs, day_start, astronomy.Direction.Rise, 2.0),
            "moonset": _rise_set(astronomy.Body.Moon, obs, day_start, astronomy.Direction.Set, 2.0),
        },
        "provenance": provenance(),
    }


def _eclipse_kind(value: Any) -> str:
    return str(value).split(".")[-1].lower()


_MAX_TIME = astronomy.Time("2500-01-01T00:00:00Z")


def _horizon_limit_ut(start_time: Any, horizon_years: float) -> float:
    years = max(0.25, min(500.0, float(horizon_years or 100.0)))
    return min(_MAX_TIME.ut, start_time.ut + years * 365.25)


def _solar_payload_from(local: Any) -> dict[str, Any]:
    # Look up the *matching* global record: a global search from the caller's
    # cursor can land on a different eclipse that never touches this site.
    global_info = astronomy.SearchGlobalSolarEclipse(local.peak.time.AddDays(-3.0))
    matches = abs(global_info.peak.ut - local.peak.time.ut) < 2.0
    return {
        "kind": "solar_eclipse",
        "local_kind": _eclipse_kind(local.kind),
        "global_kind": _eclipse_kind(global_info.kind) if matches else None,
        "obscuration": _round(local.obscuration, 6),
        "partial_begin": _event_payload(local.partial_begin.time, {"altitude_deg": _round(local.partial_begin.altitude, 4)}) if local.partial_begin else None,
        "total_begin": _event_payload(local.total_begin.time, {"altitude_deg": _round(local.total_begin.altitude, 4)}) if local.total_begin else None,
        "peak": _event_payload(local.peak.time, {"altitude_deg": _round(local.peak.altitude, 4)}),
        "total_end": _event_payload(local.total_end.time, {"altitude_deg": _round(local.total_end.altitude, 4)}) if local.total_end else None,
        "partial_end": _event_payload(local.partial_end.time, {"altitude_deg": _round(local.partial_end.altitude, 4)}) if local.partial_end else None,
        "global_peak": _event_payload(global_info.peak, {
            "latitude": _round(global_info.latitude, 5),
            "longitude": _round(global_info.longitude, 5),
            "distance_km": _round(global_info.distance, 3),
        }) if matches else None,
        "provenance": provenance(),
    }


def local_solar_eclipse_payload(start_time: Any, site: Site) -> dict[str, Any]:
    return _solar_payload_from(astronomy.SearchLocalSolarEclipse(start_time, site.observer))


def next_total_solar_eclipse(start_time: Any, site: Site, horizon_years: float = 100.0) -> dict[str, Any] | None:
    """The next eclipse whose path of totality crosses this site (local kind
    'total', not merely a deep partial). Returns None past the horizon."""
    obs = site.observer
    limit_ut = _horizon_limit_ut(start_time, horizon_years)
    local = astronomy.SearchLocalSolarEclipse(start_time, obs)
    while local.peak.time.ut <= limit_ut:
        if _eclipse_kind(local.kind) == "total":
            payload = _solar_payload_from(local)
            payload["kind"] = "total_solar_eclipse"
            return payload
        local = astronomy.NextLocalSolarEclipse(local.peak.time, obs)
    return None


def _moon_apparent_altitude(time: Any, obs: Any) -> float:
    eq = astronomy.Equator(astronomy.Body.Moon, time, obs, True, True)
    return float(astronomy.Horizon(time, obs, eq.ra, eq.dec, astronomy.Refraction.Normal).altitude)


def _lunar_payload_from(eclipse: Any, site: Site | None = None) -> dict[str, Any]:
    peak = eclipse.peak

    def bounds(sd_minutes: float) -> tuple[Any | None, Any | None]:
        if not sd_minutes or sd_minutes <= 0:
            return None, None
        return peak.AddDays(-sd_minutes / 1440.0), peak.AddDays(sd_minutes / 1440.0)

    pen_b, pen_e = bounds(eclipse.sd_penum)
    par_b, par_e = bounds(eclipse.sd_partial)
    tot_b, tot_e = bounds(eclipse.sd_total)
    payload = {
        "kind": "lunar_eclipse",
        "eclipse_kind": _eclipse_kind(eclipse.kind),
        "obscuration": _round(eclipse.obscuration, 6),
        "penumbral_begin": _event_payload(pen_b),
        "partial_begin": _event_payload(par_b),
        "total_begin": _event_payload(tot_b),
        "peak": _event_payload(peak),
        "total_end": _event_payload(tot_e),
        "partial_end": _event_payload(par_e),
        "penumbral_end": _event_payload(pen_e),
        "semi_duration_penumbral_min": _round(eclipse.sd_penum, 3),
        "semi_duration_partial_min": _round(eclipse.sd_partial, 3),
        "semi_duration_total_min": _round(eclipse.sd_total, 3),
        "provenance": provenance(),
    }
    if site is not None:
        obs = site.observer
        # Visible = the moon is apparently above the horizon at some point in
        # the deepest phase this eclipse reaches (peak alone misses eclipses
        # caught during moonrise/moonset).
        deepest = ([t for t in (tot_b, tot_e) if t is not None]
                   or [t for t in (par_b, par_e) if t is not None]
                   or [t for t in (pen_b, pen_e) if t is not None])
        peak_alt = _moon_apparent_altitude(peak, obs)
        visible = peak_alt > 0.0 or any(_moon_apparent_altitude(t, obs) > 0.0 for t in deepest)
        payload["moon_altitude_at_peak_deg"] = _round(peak_alt, 4)
        payload["visible_from_site"] = bool(visible)
        payload["visibility_note"] = (
            "moon above the horizon during the eclipse at this site" if visible
            else "moon below the horizon for the whole eclipse at this site")
    return payload


def lunar_eclipse_payload(start_time: Any, site: Site | None = None) -> dict[str, Any]:
    return _lunar_payload_from(astronomy.SearchLunarEclipse(start_time), site)


def next_total_lunar_eclipse(start_time: Any, site: Site, require_visible: bool = False,
                             horizon_years: float = 100.0) -> dict[str, Any] | None:
    """Next total lunar eclipse; with require_visible (a "blood moon" as asked
    from this site) the moon must also be up during the eclipse here."""
    limit_ut = _horizon_limit_ut(start_time, horizon_years)
    eclipse = astronomy.SearchLunarEclipse(start_time)
    while eclipse.peak.ut <= limit_ut:
        if _eclipse_kind(eclipse.kind) == "total":
            payload = _lunar_payload_from(eclipse, site)
            if not require_visible or payload["visible_from_site"]:
                payload["kind"] = "blood_moon" if require_visible else "total_lunar_eclipse"
                return payload
        eclipse = astronomy.NextLunarEclipse(eclipse.peak)
    return None


def _geo_ecliptic_longitude_deg(name: str, time: Any) -> float:
    vec = astronomy.GeoVector(BODY_ALIASES[name], time, True)
    return float(astronomy.Ecliptic(vec).elon) % 360.0


def _alignment_span_deg(time: Any, names: list[str]) -> float:
    lons = sorted(_geo_ecliptic_longitude_deg(n, time) for n in names)
    largest_gap = max((lons[(i + 1) % len(lons)] - lons[i]) % 360.0 for i in range(len(lons)))
    return 360.0 - largest_gap


def next_planetary_alignment(start_time: Any, site: Site, max_span_deg: float = 50.0,
                             horizon_years: float = 100.0) -> dict[str, Any] | None:
    """Next gathering of the five naked-eye planets within max_span_deg of
    geocentric ecliptic longitude. Reports the window (begin/peak/end), the
    tightest span, and per-planet solar elongation so callers can tell an
    observable parade from one lost in the sun's glare."""
    names = NAKED_EYE_PLANETS
    threshold = max(10.0, min(120.0, float(max_span_deg or 50.0)))
    limit_ut = _horizon_limit_ut(start_time, horizon_years)
    # Adaptive daily scan: the span changes at most ~5°/day (Mercury
    # dominates), so far-from-threshold days can be strided over safely.
    # The 10-day stride needs >60° of headroom: worst-case slew (retrograde
    # Mercury against direct Venus) approaches ~5-6°/day, and a 45° margin
    # could in principle jump a brief, deep window.
    t = start_time
    hit = None
    while t.ut <= limit_ut:
        span = _alignment_span_deg(t, names)
        if span <= threshold:
            hit = t
            break
        t = t.AddDays(10.0 if span > threshold + 60.0 else 3.0 if span > threshold + 12.0 else 1.0)
    if hit is None:
        return None
    begin = hit
    while begin.ut > start_time.ut:
        prev = begin.AddDays(-1.0)
        if prev.ut < start_time.ut or _alignment_span_deg(prev, names) > threshold:
            break
        begin = prev
    best_time, best_span = hit, _alignment_span_deg(hit, names)
    scan = begin
    while scan.ut < hit.ut:
        span = _alignment_span_deg(scan, names)
        if span < best_span:
            best_span, best_time = span, scan
        scan = scan.AddDays(1.0)
    end = hit
    while True:
        nxt = end.AddDays(1.0)
        span = _alignment_span_deg(nxt, names)
        if span > threshold:
            break
        end = nxt
        if span < best_span:
            best_span, best_time = span, nxt
    planets = []
    for name in names:
        elongation = float(astronomy.AngleFromSun(BODY_ALIASES[name], best_time))
        planets.append({
            "name": name,
            "ecliptic_longitude_deg": _round(_geo_ecliptic_longitude_deg(name, best_time), 3),
            "elongation_from_sun_deg": _round(elongation, 3),
            "observable": elongation >= MIN_OBSERVABLE_ELONGATION_DEG,
        })
    return {
        "kind": "planetary_alignment",
        "definition": f"the five naked-eye planets within {threshold:g}° of geocentric ecliptic longitude",
        "span_deg": _round(best_span, 3),
        "max_span_deg": threshold,
        "begin": _event_payload(begin),
        "peak": _event_payload(best_time, {"span_deg": _round(best_span, 3)}),
        "end": _event_payload(end),
        "planets": planets,
        "observable_planets": [p["name"] for p in planets if p["observable"]],
        "note": f"planets within {MIN_OBSERVABLE_ELONGATION_DEG:g}° of the sun sit in daylight glare and are effectively unobservable",
        "provenance": provenance(),
    }


def next_supermoon(start_time: Any, horizon_years: float = 100.0) -> dict[str, Any] | None:
    limit_ut = _horizon_limit_ut(start_time, horizon_years)
    t = astronomy.SearchMoonPhase(180.0, start_time, 40.0)
    while t is not None and t.ut <= limit_ut:
        vec = astronomy.GeoMoon(t)
        dist_km = math.sqrt(vec.x ** 2 + vec.y ** 2 + vec.z ** 2) * AU_KM
        if dist_km <= SUPERMOON_MAX_DISTANCE_KM:
            diameter = math.degrees(2.0 * math.asin(BODY_RADII_KM["moon"] / dist_km))
            return {
                "kind": "supermoon",
                "time": _event_payload(t, {
                    "distance_km": _round(dist_km, 1),
                    "angular_diameter_deg": _round(diameter, 5),
                }),
                "definition": f"full moon with geocentric distance <= {SUPERMOON_MAX_DISTANCE_KM:,.0f} km",
                "provenance": provenance(),
            }
        t = astronomy.SearchMoonPhase(180.0, t.AddDays(1.0), 40.0)
    return None


def _season_events(year: int) -> list[tuple[str, Any]]:
    seasons = astronomy.Seasons(year)
    return [
        ("equinox", seasons.mar_equinox),
        ("solstice", seasons.jun_solstice),
        ("equinox", seasons.sep_equinox),
        ("solstice", seasons.dec_solstice),
    ]


def _sun_alt(time: Any, site: Site) -> float:
    eq = astronomy.Equator(astronomy.Body.Sun, time, site.observer, True, True)
    hor = astronomy.Horizon(time, site.observer, eq.ra, eq.dec, astronomy.Refraction.Airless)
    return float(hor.altitude)


def _search_sun_alt_cross(start: Any, site: Site, altitude: float, direction: int) -> Any | None:
    step_days = 5.0 / (24.0 * 60.0)
    prev = start
    prev_val = _sun_alt(prev, site) - altitude
    t = start
    for _ in range(int(5 / step_days)):
        t = t.AddDays(step_days)
        val = _sun_alt(t, site) - altitude
        crossed = prev_val == 0 or val == 0 or (prev_val < 0 < val) or (prev_val > 0 > val)
        if crossed and (direction == 0 or (val - prev_val) * direction > 0):
            lo, hi = prev, t
            for _ in range(32):
                mid = lo.AddDays((hi.ut - lo.ut) / 2.0)
                mv = _sun_alt(mid, site) - altitude
                if (prev_val <= 0 <= mv) or (prev_val >= 0 >= mv):
                    hi = mid
                    val = mv
                else:
                    lo = mid
                    prev_val = mv
            return hi
        prev, prev_val = t, val
    return None


def _next_golden_hour(start: Any, site: Site) -> dict[str, Any] | None:
    begin = _search_sun_alt_cross(start, site, 6.0, -1)
    if begin is None:
        return None
    end = _search_sun_alt_cross(begin.AddDays(1e-5), site, -4.0, -1)
    if end is None:
        return None
    return {"kind": "golden_hour", "begin": _event_payload(begin), "end": _event_payload(end), "provenance": provenance()}


def next_sky_event(kind: str, from_time: Any | None = None, count: int = 1,
                   site: Site | dict[str, Any] | None = None,
                   max_span_deg: float = 50.0, horizon_years: float = 100.0) -> dict[str, Any]:
    event_kind = str(kind or "").strip().lower()
    n = max(1, min(20, int(count or 1)))
    _, _, start = normalize_time(from_time)
    site_obj = site if isinstance(site, Site) else site_from_mapping(site)
    obs = site_obj.observer
    horizon = max(0.25, min(500.0, float(horizon_years or 100.0)))
    events: list[dict[str, Any]] = []
    note: str | None = None
    cursor = start
    for _ in range(n):
        if event_kind == "solar_eclipse":
            payload = local_solar_eclipse_payload(cursor, site_obj)
            events.append(payload)
            cursor = astronomy.Time(payload["peak"]["iso"]).AddDays(1.0)
        elif event_kind == "total_solar_eclipse":
            payload = next_total_solar_eclipse(cursor, site_obj, horizon_years=horizon)
            if payload is None:
                note = f"no total_solar_eclipse at this site within {horizon:g} years of the start time"
                break
            events.append(payload)
            cursor = astronomy.Time(payload["peak"]["iso"]).AddDays(1.0)
        elif event_kind == "lunar_eclipse":
            payload = lunar_eclipse_payload(cursor, site_obj)
            events.append(payload)
            cursor = astronomy.Time(payload["peak"]["iso"]).AddDays(1.0)
        elif event_kind in {"total_lunar_eclipse", "blood_moon"}:
            payload = next_total_lunar_eclipse(cursor, site_obj,
                                               require_visible=event_kind == "blood_moon",
                                               horizon_years=horizon)
            if payload is None:
                note = f"no {event_kind} within {horizon:g} years of the start time"
                break
            events.append(payload)
            cursor = astronomy.Time(payload["peak"]["iso"]).AddDays(1.0)
        elif event_kind == "planetary_alignment":
            payload = next_planetary_alignment(cursor, site_obj,
                                               max_span_deg=max_span_deg,
                                               horizon_years=horizon)
            if payload is None:
                note = (f"no gathering of the five naked-eye planets tighter than "
                        f"{max(10.0, min(120.0, float(max_span_deg or 50.0))):g}° within {horizon:g} years of the start time")
                break
            events.append(payload)
            cursor = astronomy.Time(payload["end"]["iso"]).AddDays(5.0)
        elif event_kind == "supermoon":
            payload = next_supermoon(cursor, horizon_years=horizon)
            if payload is None:
                note = f"no supermoon within {horizon:g} years of the start time"
                break
            events.append(payload)
            cursor = astronomy.Time(payload["time"]["iso"]).AddDays(1.0)
        elif event_kind in {"full_moon", "new_moon"}:
            target = 180.0 if event_kind == "full_moon" else 0.0
            t = astronomy.SearchMoonPhase(target, cursor, 40.0)
            events.append({"kind": event_kind, "time": _event_payload(t), "provenance": provenance()})
            cursor = t.AddDays(1.0)
        elif event_kind in {"sunrise", "sunset", "moonrise", "moonset"}:
            body = astronomy.Body.Moon if event_kind.startswith("moon") else astronomy.Body.Sun
            direction = astronomy.Direction.Rise if event_kind.endswith("rise") else astronomy.Direction.Set
            t = astronomy.SearchRiseSet(body, obs, direction, cursor, 370.0)
            events.append({"kind": event_kind, "time": _event_payload(t), "provenance": provenance()})
            cursor = t.AddDays(0.01)
        elif event_kind in {"solstice", "equinox"}:
            year = cursor.Utc().year
            found = None
            while found is None:
                for k, t in _season_events(year):
                    if k == event_kind and t.ut > cursor.ut:
                        found = t
                        break
                year += 1
            events.append({"kind": event_kind, "time": _event_payload(found), "provenance": provenance()})
            cursor = found.AddDays(1.0)
        elif event_kind == "golden_hour":
            payload = _next_golden_hour(cursor, site_obj)
            if payload is None:
                break
            events.append(payload)
            cursor = astronomy.Time(payload["end"]["iso"]).AddDays(0.01)
        else:
            valid = ["solar_eclipse", "total_solar_eclipse", "lunar_eclipse", "total_lunar_eclipse",
                     "blood_moon", "planetary_alignment", "supermoon", "full_moon", "new_moon",
                     "sunrise", "sunset", "moonrise", "moonset", "solstice", "equinox", "golden_hour"]
            raise ValueError(f"unknown sky event kind: {kind!r}; valid kinds: {', '.join(valid)}")
    out: dict[str, Any] = {"kind": event_kind, "count": len(events), "events": events}
    if note:
        out["note"] = note
    return out


def solar_irradiance(time: Any | None = None, site: Site | dict[str, Any] | None = None) -> dict[str, Any]:
    dt, ms, _ = normalize_time(time)
    site_obj = site if isinstance(site, Site) else site_from_mapping(site)
    sun = body_position("sun", time=dt, site=site_obj)
    alt = float(sun["altitude_deg"])
    if alt <= 0:
        return {
            "time": {"iso": iso_from_dt(dt), "unix_ms": ms},
            "ghi_wm2": 0.0,
            "dni_wm2": 0.0,
            "dhi_wm2": 0.0,
            "sun_altitude_deg": _round(alt, 5),
            "airmass": None,
            "model": "Bird-Hulstrom clear-sky approximation; no clouds or terrain shading",
            "provenance": provenance(),
        }

    zen = 90.0 - alt
    cosz = max(0.0, math.cos(math.radians(zen)))
    pressure_ratio = math.exp(-max(0.0, site_obj.height_m) / 8434.5)
    m_rel = 1.0 / (cosz + 0.50572 * (96.07995 - zen) ** -1.6364)
    m = m_rel * pressure_ratio
    doy = dt.timetuple().tm_yday
    i0 = 1367.0 * (1.0 + 0.033 * math.cos(2.0 * math.pi * doy / 365.0))

    ozone_cm = 0.30
    water_cm = 1.50
    aerosol_tau = 0.10
    surface_albedo = 0.20
    t_rayleigh = math.exp(-0.0903 * (m ** 0.84) * (1.0 + m - (m ** 1.01)))
    uo = ozone_cm * m
    t_ozone = 1.0 - 0.1611 * uo * (1.0 + 139.48 * uo) ** -0.3035 - 0.002715 * uo / (1.0 + 0.044 * uo + 0.0003 * uo * uo)
    t_gas = math.exp(-0.0127 * (m ** 0.26))
    uw = water_cm * m
    t_water = 1.0 - 2.4959 * uw / ((1.0 + 79.034 * uw) ** 0.6828 + 6.385 * uw)
    t_aerosol = math.exp(-(aerosol_tau ** 0.873) * (1.0 + aerosol_tau - aerosol_tau ** 0.7088) * (m ** 0.9108))
    dni = max(0.0, 0.9662 * i0 * t_rayleigh * t_ozone * t_gas * t_water * t_aerosol)
    direct_horizontal = dni * cosz
    # Bird's diffuse split is compact but empirical; keep the terms explicit
    # so future solar-siting work can replace the constants without changing API.
    diffuse_rayleigh = i0 * cosz * t_ozone * t_gas * t_water * (1.0 - t_rayleigh) * 0.5
    diffuse_aerosol = i0 * cosz * t_ozone * t_gas * t_water * t_rayleigh * (1.0 - t_aerosol) * 0.75
    dhi = max(0.0, diffuse_rayleigh + diffuse_aerosol)
    base_global = max(0.0, direct_horizontal + dhi)
    sky_albedo = 0.0685 + (1.0 - 0.84) * (1.0 - t_aerosol)
    ground_reflectance = base_global * surface_albedo * sky_albedo / max(1e-6, 1.0 - surface_albedo * sky_albedo)
    dhi = max(0.0, dhi + ground_reflectance)
    ghi = max(0.0, direct_horizontal + dhi)
    return {
        "time": {"iso": iso_from_dt(dt), "unix_ms": ms},
        "ghi_wm2": _round(ghi, 2),
        "dni_wm2": _round(dni, 2),
        "dhi_wm2": _round(dhi, 2),
        "sun_altitude_deg": _round(alt, 5),
        "airmass": _round(m, 4),
        "model": "Bird-Hulstrom clear-sky approximation; no clouds or terrain shading",
        "parameters": {
            "ozone_cm": ozone_cm,
            "precipitable_water_cm": water_cm,
            "aerosol_optical_depth_500nm": aerosol_tau,
            "surface_albedo": surface_albedo,
            "sky_albedo": _round(sky_albedo, 5),
        },
        "provenance": provenance(),
    }


def set_view_time_payload(time: Any, rate: Any = 1.0) -> dict[str, Any] | None:
    if str(time).strip().lower() == "now":
        return None
    dt, ms, _ = normalize_time(time)
    return {"iso": iso_from_dt(dt), "unix_ms": ms, "rate": clamp_rate(rate)}
