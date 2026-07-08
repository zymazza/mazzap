#!/usr/bin/env python3
"""Fetch and normalize committed astronomy viewer assets.

This is a one-time/occasional online snapshot script. The viewer consumes the
outputs offline from public/astronomy-data/.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "public" / "astronomy-data"

D3_BASE = "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data"
STARS_URL = f"{D3_BASE}/stars.6.json"
STAR_NAMES_URL = f"{D3_BASE}/starnames.json"
CONSTELLATION_LINES_URL = f"{D3_BASE}/constellations.lines.json"
CONSTELLATION_NAMES_URL = f"{D3_BASE}/constellations.json"
MOON_URL = "https://svs.gsfc.nasa.gov/vis/a000000/a004700/a004720/lroc_color_poles_1k.jpg"


def fetch_json(url: str) -> Any:
    with urlopen(url, timeout=60) as r:
        return json.load(r)


def fetch_bytes(url: str) -> bytes:
    with urlopen(url, timeout=120) as r:
        return r.read()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        f.write(text)
        tmp = Path(f.name)
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as f:
        f.write(data)
        tmp = Path(f.name)
    os.replace(tmp, path)


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default


def norm_ra(ra_deg: float) -> float:
    return ra_deg % 360.0


def angular_sep_deg(a_ra: float, a_dec: float, b_ra: float, b_dec: float) -> float:
    ar = math.radians(a_ra)
    ad = math.radians(a_dec)
    br = math.radians(b_ra)
    bd = math.radians(b_dec)
    dot = math.sin(ad) * math.sin(bd) + math.cos(ad) * math.cos(bd) * math.cos(ar - br)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def build_stars() -> tuple[dict[str, Any], dict[int, tuple[float, float]]]:
    src = fetch_json(STARS_URL)
    names = fetch_json(STAR_NAMES_URL)
    rows: list[list[Any]] = []
    positions: dict[int, tuple[float, float]] = {}
    for feature in src.get("features", []):
        hip = int(feature.get("id") or 0)
        coords = feature.get("geometry", {}).get("coordinates") or [0, 0]
        ra = round(norm_ra(as_float(coords[0])), 6)
        dec = round(as_float(coords[1]), 6)
        props = feature.get("properties", {})
        mag = round(as_float(props.get("mag")), 3)
        bv = round(as_float(props.get("bv")), 3)
        name = str((names.get(str(hip)) or {}).get("name") or "")
        rows.append([ra, dec, mag, bv, hip, name])
        positions[hip] = (ra, dec)
    rows.sort(key=lambda r: (r[0], r[1], r[4]))
    return {
        "version": 1,
        "source": {
            "stars": STARS_URL,
            "names": STAR_NAMES_URL,
            "note": "Hipparcos-derived d3-celestial stars.6.json; RA/Dec are J2000 degrees. Proper motion is ignored.",
        },
        "license": "BSD-3-Clause",
        "count": len(rows),
        "stars": rows,
    }, positions


def nearest_hip(
    ra: float,
    dec: float,
    by_coord: dict[tuple[float, float], int],
    positions: dict[int, tuple[float, float]],
) -> int | None:
    key = (round(norm_ra(ra), 4), round(dec, 4))
    if key in by_coord:
        return by_coord[key]
    best_hip = None
    best_sep = 1e9
    for hip, (sra, sdec) in positions.items():
        sep = angular_sep_deg(norm_ra(ra), dec, sra, sdec)
        if sep < best_sep:
            best_sep = sep
            best_hip = hip
    return best_hip if best_sep <= 0.05 else None


def build_constellations(positions: dict[int, tuple[float, float]]) -> dict[str, Any]:
    names_src = fetch_json(CONSTELLATION_NAMES_URL)
    lines_src = fetch_json(CONSTELLATION_LINES_URL)
    names: dict[str, str] = {}
    for feature in names_src.get("features", []):
        props = feature.get("properties", {})
        abbr = str(props.get("desig") or feature.get("id") or "").strip()
        if abbr:
            names[abbr] = str(props.get("name") or props.get("en") or abbr)

    by_coord = {
        (round(ra, 4), round(dec, 4)): hip
        for hip, (ra, dec) in positions.items()
    }
    lines: dict[str, list[list[int]]] = {}
    dropped = 0
    for feature in lines_src.get("features", []):
        abbr = str(feature.get("id") or "").strip()
        if not abbr:
            continue
        seen: set[tuple[int, int]] = set()
        segments: list[list[int]] = []
        for chain in feature.get("geometry", {}).get("coordinates") or []:
            hips: list[int | None] = [
                nearest_hip(as_float(pt[0]), as_float(pt[1]), by_coord, positions)
                for pt in chain
            ]
            for a, b in zip(hips, hips[1:]):
                if not a or not b or a == b:
                    dropped += 1
                    continue
                key = (a, b) if a < b else (b, a)
                if key not in seen:
                    seen.add(key)
                    segments.append([a, b])
        lines[abbr] = segments
    return {
        "version": 1,
        "source": {
            "lines": CONSTELLATION_LINES_URL,
            "names": CONSTELLATION_NAMES_URL,
            "note": "Line coordinates resolved to HIP ids in stars.json; missing bright-catalog endpoints are dropped.",
        },
        "license": "BSD-3-Clause",
        "names": dict(sorted(names.items())),
        "lines": dict(sorted(lines.items())),
        "dropped_segments": dropped,
    }


def write_moon() -> None:
    data = fetch_bytes(MOON_URL)
    if not data.startswith(b"\xff\xd8"):
        raise RuntimeError(f"{MOON_URL} did not return a JPEG")
    atomic_write_bytes(OUT_DIR / "moon_1k.jpg", data)


def main() -> None:
    stars, positions = build_stars()
    constellations = build_constellations(positions)
    atomic_write_text(OUT_DIR / "stars.json", json.dumps(stars, ensure_ascii=False, indent=1) + "\n")
    atomic_write_text(OUT_DIR / "constellations.json", json.dumps(constellations, ensure_ascii=False, indent=1) + "\n")
    write_moon()
    print(f"wrote {OUT_DIR / 'stars.json'} ({stars['count']} stars)")
    print(f"wrote {OUT_DIR / 'constellations.json'} ({sum(len(v) for v in constellations['lines'].values())} segments, {constellations['dropped_segments']} dropped)")
    print(f"wrote {OUT_DIR / 'moon_1k.jpg'}")


if __name__ == "__main__":
    main()
