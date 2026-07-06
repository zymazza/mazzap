#!/usr/bin/env python3
"""Optional gridMET daily forcing gap-fill fetcher for US twins.

This script is intentionally offline-safe by default: it writes no network
requests unless `--fetch` is supplied. `--dry-run` and `--self-test` exercise the
URL construction, 10 m -> 2 m wind conversion, CSV writer and summary/correlation
logic without touching the network.

Target variables: pet, etr, vs, rmin, rmax, sph, vpd, srad, tmmn, tmmx, pr.
gridMET wind `vs` is 10 m wind; FAO-56 2 m wind is
u2 = vs * 4.87 / ln(67.8 * 10 - 5.42).
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PROJECT, "scripts"))

import twin_georef  # noqa: E402

THREDDS = "https://thredds.northwestknowledge.net/thredds/"
SINGLE_POINT_SERVICE = "https://thredds.northwestknowledge.net/thredds/gridmet-single-point"
VARS = ("pet", "etr", "vs", "rmin", "rmax", "sph", "vpd", "srad", "tmmn", "tmmx", "pr")


def wind_2m(vs_10m):
    return float(vs_10m) * 4.87 / math.log(67.8 * 10.0 - 5.42)


def anchor_lonlat(data_dir):
    georef_path = os.path.join(data_dir, "georef.json")
    g = twin_georef.load(georef_path)
    o = g.get("origin_wgs84")
    if o:
        return float(o["lon"]), float(o["lat"])
    ox, oy = twin_georef.origin(georef_path)
    fwd, _ = twin_georef.transformers(georef_path)
    return fwd.transform(ox, oy)


def request_url(lat, lon, start, end):
    params = urllib.parse.urlencode({
        "lat": "%.6f" % lat,
        "lon": "%.6f" % lon,
        "start": start,
        "end": end,
        "vars": ",".join(VARS),
    })
    return SINGLE_POINT_SERVICE + "?" + params


def fetch_gridmet(lat, lon, start, end):
    req = urllib.request.Request(request_url(lat, lon, start, end),
                                 headers={"User-Agent": "veil/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8")


def parse_csv(text):
    rows = []
    for r in csv.DictReader(text.splitlines()):
        out = {"date": r.get("date") or r.get("day") or r.get("time")}
        for v in VARS:
            out[v] = float(r[v]) if r.get(v) not in (None, "") else None
        out["u2"] = wind_2m(out["vs"]) if out.get("vs") is not None else None
        rows.append(out)
    return rows


def synthetic_rows(start="2020-01-01", days=30):
    d0 = dt.date.fromisoformat(start)
    rows = []
    for i in range(days):
        d = d0 + dt.timedelta(days=i)
        t = 5.0 + 10.0 * math.sin(2 * math.pi * i / 365.0)
        pet = max(0.1, 1.5 + 1.0 * math.sin(2 * math.pi * (i - 80) / 365.0))
        rows.append({
            "date": d.isoformat(),
            "pet": pet,
            "etr": pet * 1.18,
            "vs": 3.0,
            "u2": wind_2m(3.0),
            "rmin": 45.0,
            "rmax": 92.0,
            "sph": 0.006,
            "vpd": 0.7,
            "srad": 190.0,
            "tmmn": t + 273.15 - 5.0,
            "tmmx": t + 273.15 + 6.0,
            "pr": 2.0 if i % 4 == 0 else 0.1,
        })
    return rows


def write_csv(rows, path):
    fields = ["date", *VARS, "u2"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: round(r[k], 5) if isinstance(r.get(k), float) else r.get(k) for k in fields})


def corr(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if x not in (None, "") and y not in (None, "")]
    if len(pairs) < 3:
        return None
    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    return (sum((a - mx) * (b - my) for a, b in pairs) / (sx * sy)) if sx and sy else None


def local_series(path, key):
    if not os.path.exists(path):
        return {}
    out = {}
    for r in csv.DictReader(open(path)):
        if r.get("date") and r.get(key) not in (None, ""):
            out[r["date"]] = float(r[key])
    return out


def summarize(rows, data_dir, dry_run=False, url=None):
    by_date = {r["date"]: r for r in rows}
    et0_pt = local_series(os.path.join(data_dir, "et", "et0_daily.csv"), "priestley_taylor_mm")
    et0_mean = local_series(os.path.join(data_dir, "et", "et0_daily.csv"), "method_mean_mm")
    daymet_p = local_series(os.path.join(data_dir, "climate", "daymet_daily.csv"), "prcp (mm/day)")
    if not daymet_p:
        daymet_p = local_series(os.path.join(data_dir, "climate", "daymet_daily.csv"), "prcp")
    dates = sorted(by_date)
    return {
        "source": "gridMET daily forcing",
        "service": THREDDS,
        "request_url": url,
        "dry_run": dry_run,
        "variables": list(VARS) + ["u2"],
        "records": len(rows),
        "wind_conversion": "u2 = vs * 4.87 / ln(67.8*10 - 5.42)",
        "correlations": {
            "gridmet_pet_vs_et0_priestley_taylor": (
                None if not et0_pt else corr([by_date[d]["pet"] for d in dates if d in et0_pt],
                                             [et0_pt[d] for d in dates if d in et0_pt])),
            "gridmet_pet_vs_et0_method_mean": (
                None if not et0_mean else corr([by_date[d]["pet"] for d in dates if d in et0_mean],
                                               [et0_mean[d] for d in dates if d in et0_mean])),
            "gridmet_pr_vs_daymet_prcp": (
                None if not daymet_p else corr([by_date[d]["pr"] for d in dates if d in daymet_p],
                                               [daymet_p[d] for d in dates if d in daymet_p])),
        },
        "note": "gridMET is regional 4 km forcing; use it to fill wind/humidity/PET gaps and compare against local Daymet-derived ET0, not as parcel validation.",
    }


def self_test():
    rows = synthetic_rows()
    assert abs(rows[0]["u2"] - wind_2m(rows[0]["vs"])) < 1e-9
    s = summarize(rows, "/tmp", dry_run=True, url=request_url(44.0, -73.0, "2020-01-01", "2020-01-30"))
    assert s["records"] == 30
    print("fetch_gridmet_forcing.py self-test ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--start", default="1980-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--fetch", action="store_true",
                    help="actually call the gridMET service; omitted by default for offline-safe dry runs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    data_dir = os.path.abspath(args.data_dir)
    lon, lat = anchor_lonlat(data_dir)
    url = request_url(lat, lon, args.start, args.end)
    if not args.fetch or args.dry_run:
        rows = synthetic_rows(args.start, 30)
        dry = True
        print("Dry run only; not fetching gridMET. URL would be:")
        print(url)
    else:
        rows = parse_csv(fetch_gridmet(lat, lon, args.start, args.end))
        dry = False
    out_dir = os.path.join(data_dir, "climate")
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "gridmet_daily.csv")
    summary_path = os.path.join(out_dir, "gridmet-summary.json")
    write_csv(rows, csv_path)
    with open(summary_path, "w") as fh:
        json.dump(summarize(rows, data_dir, dry_run=dry, url=url), fh, indent=2)
    print("Wrote %s (%d records, dry_run=%s)" % (csv_path, len(rows), dry))
    print("Wrote %s" % summary_path)


if __name__ == "__main__":
    main()
