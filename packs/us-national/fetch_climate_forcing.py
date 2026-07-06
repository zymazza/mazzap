#!/usr/bin/env python3
"""One-time acquisition of the daily **climate forcing** record for a twin.

A hydrology simulation needs water in (precip, snowmelt) to produce water out
(runoff, recharge, stream flow). This fetches a multi-decade daily record ONCE
from Daymet [1] (1 km gridded daily surface weather for North America) at the
twin's anchor point and **saves it locally**, so the bundle stays
view-time-offline like every other VEIL input.

Daymet is the high-value single fetch here: one no-auth REST call returns daily
precipitation, min/max temperature, modeled snow-water-equivalent (SWE), shortwave
radiation, daylength and vapor pressure — i.e. both the precip/temp forcing the
degree-day snowmelt step needs AND a ~45-year SWE climatology to calibrate its
melt factor against, plus the radiation/daylength/humidity an energy-balance
upgrade uses.

Region-agnostic: the anchor comes from the twin's own georef.json, and the
outputs go under the twin's --data-dir, so this works for any North American
twin. Coverage is **North America only** (Daymet's domain); twins outside it get
no climate forcing and the Simulation window degrades to terrain-only geometry
with explicit event depths.

Writes (under <data-dir>/climate/):
  daymet_daily.csv     — the raw daily record, as returned (compact, the
                         canonical forcing the pipeline reads)
  forcing-summary.json — per-water-year climatology: peak SWE and its date,
                         annual precip, melt-season window, simple degree-day
                         melt stats — the scenario-engine calibration targets

Idempotent: re-running re-queries Daymet and overwrites the same files.

SNODAS upgrade path: for operational (assimilated) daily SWE + snowmelt-runoff
grids 2003-present, NOHRSC's SNODAS [2] is the next step — gridded 1 km, but
distributed as masked flat-binary over FTP (heavier ingest than this single-pixel
REST pull). Daymet SWE is modeled, not assimilated; it is the right multi-decade
calibration baseline, and SNODAS is the operational refinement when needed.

[1] https://daymet.ornl.gov/  — Single Pixel Extraction REST API
[2] https://nsidc.org/data/g02158/versions/1  — SNODAS at NSIDC

Run:  python3 packs/us-national/fetch_climate_forcing.py [--data-dir DIR]
      (or set TWIN_DATA_DIR; --resummarize re-derives the summary, no fetch)
"""

import argparse
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))  # packs/<name>/ -> repo root
sys.path.insert(0, os.path.join(PROJECT, "scripts"))
import twin_georef  # noqa: E402

DAYMET_API = "https://daymet.ornl.gov/single-pixel/api/data"
VARS = "prcp,tmax,tmin,swe,srad,dayl,vp"
START, END = "1980-01-01", "2024-12-31"  # Daymet returns the available subset


def anchor_lonlat(georef_path):
    """The twin's anchor in lon/lat — origin_wgs84 from georef.json, else the
    origin_utm reprojected. The property's real coordinates live only in the
    gitignored data/, never in this tracked script."""
    g = twin_georef.load(georef_path)
    o = g.get("origin_wgs84")
    if o:
        return float(o["lon"]), float(o["lat"])
    ox, oy = twin_georef.origin(georef_path)
    fwd, _ = twin_georef.transformers(georef_path)
    return fwd.transform(ox, oy)


def fetch_daymet(lat, lon):
    params = urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "vars": VARS, "start": START, "end": END})
    req = urllib.request.Request(
        DAYMET_API + "?" + params, headers={"User-Agent": "veil/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8")


def parse_records(csv_text):
    """Split the Daymet response into its metadata-header lines and the daily
    data rows (the data table starts at the line beginning 'year,')."""
    lines = csv_text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("year,"))
    header_block = lines[:start]
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows = []
    for r in reader:
        rows.append({
            "year": int(r["year"]),
            "yday": int(r["yday"]),
            "prcp": float(r["prcp (mm/day)"]),
            "tmax": float(r["tmax (deg c)"]),
            "tmin": float(r["tmin (deg c)"]),
            "swe": float(r["swe (kg/m^2)"]),
            "srad": float(r["srad (W/m^2)"]),
            "dayl": float(r["dayl (s)"]),
            "vp": float(r["vp (Pa)"]) if r.get("vp (Pa)") not in (None, "") else None,
        })
    return header_block, rows


def water_year(year, yday):
    """US water year: Oct 1 (yday ~274) starts the next labelled year. Daymet
    uses a fixed 365-day year, so this day-of-year cutoff is stable."""
    return year + 1 if yday >= 274 else year


def summarize(rows):
    """Per-water-year climatology aimed at snowmelt-model calibration, plus
    storm statistics (annual max 1-day / 3-day rainfall) for rain scenarios."""
    by_wy = {}
    for r in rows:
        wy = water_year(r["year"], r["yday"])
        by_wy.setdefault(wy, []).append(r)

    years = []
    for wy, recs in sorted(by_wy.items()):
        if len(recs) < 300:  # partial (first/last) water year — skip from stats
            continue
        peak = max(recs, key=lambda r: r["swe"])
        annual_precip = round(sum(r["prcp"] for r in recs), 1)
        # storm stats: largest 1-day and largest 3-day-total precipitation
        prcps = [r["prcp"] for r in recs]
        max_1day = max(prcps)
        max_3day = max(sum(prcps[i:i + 3]) for i in range(len(prcps) - 2)) \
            if len(prcps) >= 3 else max_1day
        # melt season: first to last day SWE exceeds 10 kg/m^2, and the largest
        # single-day SWE drop within it (a crude peak-melt-rate indicator)
        snow_days = [r for r in recs if r["swe"] > 10]
        max_daily_melt = 0.0
        for a, b in zip(recs, recs[1:]):
            drop = a["swe"] - b["swe"]
            if drop > max_daily_melt:
                max_daily_melt = drop
        # positive-degree-day sum over Mar-May (the snowmelt window) using mean temp
        pdd_spring = round(sum(
            max(0.0, (r["tmax"] + r["tmin"]) / 2.0)
            for r in recs if 60 <= r["yday"] <= 151), 1)
        years.append({
            "water_year": wy,
            "peak_swe_kg_m2": round(peak["swe"], 1),
            "peak_swe_yday": peak["yday"],
            "peak_swe_calendar_year": peak["year"],
            "annual_precip_mm": annual_precip,
            "snow_cover_days": len(snow_days),
            "max_daily_swe_loss_kg_m2": round(max_daily_melt, 1),
            "spring_positive_degree_days_c": pdd_spring,
            "max_1day_prcp_mm": round(max_1day, 1),
            "max_3day_prcp_mm": round(max_3day, 1),
        })

    if not years:
        return {"water_years": []}
    peaks = sorted(y["peak_swe_kg_m2"] for y in years)
    n = len(peaks)

    def pct(p):
        return round(peaks[min(n - 1, int(p * n))], 1)

    storms1 = sorted(y["max_1day_prcp_mm"] for y in years)
    storms3 = sorted(y["max_3day_prcp_mm"] for y in years)

    def spct(vals, p):
        return round(vals[min(n - 1, int(p * n))], 1)

    return {
        "water_years": years,
        "n_full_water_years": n,
        "peak_swe_kg_m2_median": pct(0.5),
        "peak_swe_kg_m2_p10": pct(0.10),
        "peak_swe_kg_m2_p90": pct(0.90),
        "peak_swe_kg_m2_max": peaks[-1],
        "mean_annual_precip_mm": round(sum(y["annual_precip_mm"] for y in years) / n, 1),
        # annual-maximum-series storm stats (drive the rain-scenario presets)
        "storm_1day_mm_median": spct(storms1, 0.5),
        "storm_1day_mm_p90": spct(storms1, 0.90),
        "storm_1day_mm_max": storms1[-1],
        "storm_3day_mm_median": spct(storms3, 0.5),
        "storm_3day_mm_p90": spct(storms3, 0.90),
        "storm_3day_mm_max": storms3[-1],
        "note": "SWE in kg/m^2 == mm of water; 25.4 kg/m^2 ~= 1 inch SWE. Peak "
                "SWE distribution feeds snowmelt scenarios; spring PDD calibrates "
                "the degree-day melt factor. storm_* fields are annual-maximum "
                "series of 1-day and 3-day precipitation (rain-scenario presets).",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--resummarize", action="store_true",
                    help="re-derive forcing-summary.json from the cached CSV (no fetch)")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    georef_path = os.path.join(data_dir, "georef.json")
    climate_dir = os.path.join(data_dir, "climate")
    daily_csv = os.path.join(climate_dir, "daymet_daily.csv")
    summary_json = os.path.join(climate_dir, "forcing-summary.json")
    os.makedirs(climate_dir, exist_ok=True)

    lon, lat = anchor_lonlat(georef_path)
    # --resummarize: re-derive forcing-summary.json from the already-fetched
    # daily CSV (no API call) — for when the summary gains new statistics.
    if args.resummarize and os.path.exists(daily_csv):
        print("Re-summarizing existing %s (no fetch)…" % daily_csv)
        csv_text = open(daily_csv).read()
    else:
        print("Fetching Daymet daily record at %.5f, %.5f (%s..%s)…" % (lat, lon, START, END))
        csv_text = fetch_daymet(lat, lon)
        with open(daily_csv, "w") as fh:
            fh.write(csv_text)
    header_block, rows = parse_records(csv_text)

    summary = summarize(rows)
    summary = {
        "source": "Daymet v4 R1 (ORNL DAAC) — Single Pixel Extraction",
        "service": DAYMET_API,
        "acquisition": "api_snapshot",
        "anchor_lonlat": [round(lon, 6), round(lat, 6)],
        "period": {"start": START, "end": END, "daily_records": len(rows)},
        "daymet_header": header_block,
        "variables": VARS.split(","),
        "et_notes": {
            "srad": "Daymet srad is a daylight-average flux (W/m^2); daily total shortwave Rs(MJ/m^2/d) = srad * dayl / 1e6.",
            "vp": "Daymet vp is modeled humidity derived from Tmin-dewpoint assumptions, not an in-situ vapor-pressure observation.",
        },
        **summary,
    }
    with open(summary_json, "w") as fh:
        json.dump(summary, fh, indent=2)

    print("Wrote %s (%d daily records)" % (daily_csv, len(rows)))
    print("Wrote %s (%d full water years)"
          % (summary_json, summary.get("n_full_water_years", 0)))
    if summary.get("n_full_water_years"):
        print("\nSnowpack climatology (peak SWE, kg/m^2 == mm water):")
        print("  median %.0f   p10 %.0f   p90 %.0f   max %.0f   |   mean annual precip %.0f mm" % (
            summary["peak_swe_kg_m2_median"], summary["peak_swe_kg_m2_p10"],
            summary["peak_swe_kg_m2_p90"], summary["peak_swe_kg_m2_max"],
            summary["mean_annual_precip_mm"]))
        worst = max(summary["water_years"], key=lambda y: y["peak_swe_kg_m2"])
        print("  biggest snowpack on record: WY%d, peak %.0f kg/m^2 (~%.1f in SWE)" % (
            worst["water_year"], worst["peak_swe_kg_m2"], worst["peak_swe_kg_m2"] / 25.4))


if __name__ == "__main__":
    main()
