#!/usr/bin/env python3
"""Fetch a PVGIS SRTM-derived horizon profile for this twin's site."""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_astro


def main() -> int:
    site = twin_astro.site_from_georef()
    params = urllib.parse.urlencode({
        "lat": site.lat,
        "lon": site.lon,
        "outputformat": "json",
    })
    url = f"https://re.jrc.ec.europa.eu/api/printhorizon?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out_dir = os.path.join(PROJECT, "data", "viewshed")
    os.makedirs(out_dir, exist_ok=True)
    out = {
        "source": "PVGIS printhorizon SRTM horizon oracle",
        "url": url,
        "site": {"lat": site.lat, "lon": site.lon, "height_m": site.height_m},
        "payload": payload,
    }
    path = os.path.join(out_dir, "pvgis-horizon.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps({"path": path, "source": out["source"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
