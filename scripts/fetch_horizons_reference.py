#!/usr/bin/env python3
"""Fetch one per-twin JPL Horizons astronomy validation fixture.

The viewer and MCP tools never call Horizons at runtime. This online script
writes data/astronomy/horizons-reference.json for offline validation only.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlencode
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import twin_astro  # noqa: E402


OUT = Path(twin_astro.HORIZONS_REFERENCE)
HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
BODY_COMMANDS = {
    "sun": "10",
    "moon": "301",
    "mercury": "199",
    "venus": "299",
    "mars": "499",
    "jupiter": "599",
    "saturn": "699",
    "uranus": "799",
    "neptune": "899",
}


def jd_from_datetime(dt: datetime) -> float:
    return dt.timestamp() / 86400.0 + 2440587.5


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sample_times(now: datetime, count: int = 48) -> list[datetime]:
    start = now - timedelta(days=365.25 * 5)
    end = now + timedelta(days=365.25 * 5)
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]


def parse_result(result: str) -> list[dict[str, float | str]]:
    try:
        table = result.split("$$SOE", 1)[1].split("$$EOE", 1)[0]
    except IndexError as exc:
        raise RuntimeError("Horizons response did not contain $$SOE/$$EOE table") from exc
    rows = []
    for row in csv.reader(line for line in table.splitlines() if line.strip()):
        # CSV_FORMAT + QUANTITIES='2,4' + ANG_FORMAT=DEG:
        # date, solar-presence flag, lunar-presence flag, RA deg, Dec deg, Az deg, Elev deg, ...
        if len(row) < 7:
            continue
        rows.append({
            "time_horizons": row[0].strip(),
            "ra_deg": float(row[3]),
            "dec_deg": float(row[4]),
            "azimuth_deg": float(row[5]),
            "altitude_deg": float(row[6]),
        })
    return rows


def fetch_body(command: str, site: twin_astro.Site, times: list[datetime]) -> list[dict[str, float | str]]:
    tlist = ",".join(f"{jd_from_datetime(t):.9f}" for t in times)
    params = {
        "format": "json",
        "EPHEM_TYPE": "OBSERVER",
        "COMMAND": command,
        "CENTER": "coord@399",
        "COORD_TYPE": "GEODETIC",
        "SITE_COORD": f"'{site.lon},{site.lat},{site.height_m / 1000.0}'",
        "QUANTITIES": "'2,4'",
        "APPARENT": "AIRLESS",
        "TLIST": tlist,
        "CSV_FORMAT": "YES",
        "OBJ_DATA": "NO",
        "ANG_FORMAT": "DEG",
    }
    with urlopen(HORIZONS_URL + "?" + urlencode(params), timeout=120) as r:
        data = json.load(r)
    if data.get("error"):
        raise RuntimeError(data["error"])
    rows = parse_result(data["result"])
    if len(rows) != len(times):
        raise RuntimeError(f"Horizons returned {len(rows)} rows for {len(times)} requested times")
    for row, dt in zip(rows, times):
        row["iso"] = iso(dt)
        row["jd"] = round(jd_from_datetime(dt), 9)
    return rows


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(payload, f, indent=1)
        f.write("\n")
        tmp = Path(f.name)
    os.replace(tmp, path)


def main() -> None:
    site = twin_astro.site_from_georef()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    times = sample_times(now)
    bodies = {}
    for name, command in BODY_COMMANDS.items():
        print(f"fetching {name} ({command})...")
        bodies[name] = fetch_body(command, site, times)
    payload = {
        "version": 1,
        "generated_at": iso(now),
        "source": {
            "name": "JPL Horizons",
            "url": HORIZONS_URL,
            "ephem_type": "OBSERVER",
            "quantities": "2,4",
            "apparent": "AIRLESS",
        },
        "site": {"lat": site.lat, "lon": site.lon, "height_m": site.height_m},
        "times": [iso(t) for t in times],
        "bodies": bodies,
    }
    atomic_write(OUT, payload)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
