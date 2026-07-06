#!/usr/bin/env python3
"""Derive daily reference/potential ET from the local Daymet forcing CSV.

Inputs:
  <data>/climate/daymet_daily.csv
  <data>/georef.json + <data>/terrain/grid.json for latitude/elevation

Outputs:
  <data>/et/et0_daily.csv
  <data>/et/et0-summary.json

No network access. This is a reduced-data ET0 ensemble: Oudin,
Hargreaves-Samani, Priestley-Taylor, and FAO-56 Penman-Monteith. If available,
gridMET daily wind is reduced to a monthly climatology for FAO-56 PM; otherwise
the reduced-data u2=2 m/s fallback is used. Daymet `vp` is used for actual vapor
pressure when present; older CSVs fall back to FAO-56's Tmin-as-dewpoint estimate
and report that uncertainty.
"""

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_georef  # noqa: E402
import twin_hydrology as hydro  # noqa: E402
import twin_store  # noqa: E402

GSC = 0.0820  # MJ m-2 min-1
SIGMA = 4.903e-9  # MJ K-4 m-2 d-1
LAMBDA = 2.45  # MJ kg-1
ALBEDO = 0.23
METHODS = ("oudin_mm", "hargreaves_samani_mm", "priestley_taylor_mm", "fao56_pm_reduced_mm")
MONTH_LENGTHS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _daymet_date(year, yday):
    m, d = 1, int(yday)
    for n in MONTH_LENGTHS:
        if d <= n:
            return "%04d-%02d-%02d" % (int(year), m, d)
        d -= n
        m += 1
    return "%04d-12-31" % int(year)


def yday_to_month(yday):
    d = int(yday)
    if d < 1 or d > 365:
        raise ValueError("Daymet yday must be in 1..365, got %s" % yday)
    for month, n in enumerate(MONTH_LENGTHS, start=1):
        if d <= n:
            return month
        d -= n
    return 12


def _col(row, stem):
    for key, value in row.items():
        if key == stem or key.startswith(stem + " "):
            return value
    return None


def read_daymet_csv(path):
    text = open(path).read()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("year,"))
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        rows.append({
            "year": int(_col(r, "year")),
            "yday": int(_col(r, "yday")),
            "date": _daymet_date(int(_col(r, "year")), int(_col(r, "yday"))),
            "prcp": float(_col(r, "prcp")),
            "tmax": float(_col(r, "tmax")),
            "tmin": float(_col(r, "tmin")),
            "swe": float(_col(r, "swe") or 0.0),
            "srad": float(_col(r, "srad")),
            "dayl": float(_col(r, "dayl")),
            "vp": float(_col(r, "vp")) if _col(r, "vp") not in (None, "") else None,
        })
    return rows


def load_monthly_wind_climatology(data_dir):
    path = os.path.join(data_dir, "climate", "gridmet_daily.csv")
    if not os.path.exists(path):
        return None
    values = {m: [] for m in range(1, 13)}
    all_values = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            date = row.get("date") or ""
            try:
                month = int(date[5:7])
                u2 = float(row.get("u2"))
            except (TypeError, ValueError):
                continue
            if month < 1 or month > 12 or not math.isfinite(u2):
                continue
            values[month].append(u2)
            all_values.append(u2)
    if not all_values:
        return None
    return {
        "by_month": {m: sum(v) / len(v) for m, v in values.items() if v},
        "overall": sum(all_values) / len(all_values),
        "n_days": len(all_values),
        "source": "gridmet_daily.csv",
    }


def saturation_vapor_pressure_kpa(t_c):
    return 0.6108 * math.exp(17.27 * t_c / (t_c + 237.3))


def slope_vapor_pressure_curve(t_c):
    es = saturation_vapor_pressure_kpa(t_c)
    return 4098.0 * es / ((t_c + 237.3) ** 2)


def pressure_kpa(elev_m):
    return 101.3 * ((293.0 - 0.0065 * elev_m) / 293.0) ** 5.26


def extraterrestrial_radiation_mj(lat_deg, yday):
    phi = math.radians(lat_deg)
    j = int(yday)
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * j / 365.0)
    dec = 0.409 * math.sin(2.0 * math.pi * j / 365.0 - 1.39)
    arg = -math.tan(phi) * math.tan(dec)
    ws = math.acos(max(-1.0, min(1.0, arg)))
    return (24.0 * 60.0 / math.pi) * GSC * dr * (
        ws * math.sin(phi) * math.sin(dec) +
        math.cos(phi) * math.cos(dec) * math.sin(ws))


def net_radiation_mj(row, elev_m, ra_mj, ea_kpa):
    rs = max(0.0, row["srad"] * row["dayl"] / 1e6)
    rso = max(1e-6, (0.75 + 2e-5 * elev_m) * ra_mj)
    rns = (1.0 - ALBEDO) * rs
    tmax_k = row["tmax"] + 273.16
    tmin_k = row["tmin"] + 273.16
    cloud = 1.35 * max(0.3, min(1.0, rs / rso)) - 0.35
    humid = 0.34 - 0.14 * math.sqrt(max(0.0, ea_kpa))
    rnl = SIGMA * ((tmax_k ** 4 + tmin_k ** 4) / 2.0) * humid * cloud
    return max(0.0, rns - rnl), rs, rso


def compute_et0(rows, lat_deg, elev_m, u2=2.0, wind_clim=None):
    gamma = 0.000665 * pressure_kpa(elev_m)
    out = []
    for row in rows:
        month = yday_to_month(row["yday"])
        if wind_clim:
            u2_day = wind_clim["by_month"].get(month, wind_clim["overall"])
            wind_assumed = False
        else:
            u2_day = u2
            wind_assumed = True
        tmean = (row["tmax"] + row["tmin"]) / 2.0
        td = max(0.0, row["tmax"] - row["tmin"])
        es_tmax = saturation_vapor_pressure_kpa(row["tmax"])
        es_tmin = saturation_vapor_pressure_kpa(row["tmin"])
        es = (es_tmax + es_tmin) / 2.0
        if row.get("vp") is not None:
            ea = max(0.0, row["vp"] / 1000.0)
            humidity_source = "daymet_vp"
        else:
            ea = es_tmin
            humidity_source = "estimated_from_tmin"
        delta = slope_vapor_pressure_curve(tmean)
        ra = extraterrestrial_radiation_mj(lat_deg, row["yday"])
        rn, rs, rso = net_radiation_mj(row, elev_m, ra, ea)
        oudin = max(0.0, (ra / LAMBDA) * max(0.0, tmean + 5.0) / 100.0)
        # FAO-56 eq. 52 requires Ra expressed in mm/day, so convert the MJ m-2 d-1
        # value with 0.408 (= 1/lambda). Omitting this overestimates HS by ~2.45x.
        hs = max(0.0, 0.0023 * (0.408 * ra) * (tmean + 17.8) * math.sqrt(td))
        pt = max(0.0, 1.26 * (delta / (delta + gamma)) * rn / LAMBDA)
        vpd = max(0.0, es - ea)
        pm_num = 0.408 * delta * rn + gamma * (900.0 / (tmean + 273.0)) * u2_day * vpd
        pm_den = delta + gamma * (1.0 + 0.34 * u2_day)
        pm = max(0.0, pm_num / pm_den) if pm_den else 0.0
        vals = [oudin, hs, pt, pm]
        out.append({
            "date": row["date"],
            "year": row["year"],
            "month": month,
            "yday": row["yday"],
            "tmean_c": tmean,
            "ra_mj_m2_d": ra,
            "rs_mj_m2_d": rs,
            "rso_mj_m2_d": rso,
            "rn_mj_m2_d": rn,
            "ea_kpa": ea,
            "es_kpa": es,
            "vpd_kpa": vpd,
            "humidity_source": humidity_source,
            "wind_assumed": wind_assumed,
            "oudin_mm": oudin,
            "hargreaves_samani_mm": hs,
            "priestley_taylor_mm": pt,
            "fao56_pm_reduced_mm": pm,
            "method_mean_mm": sum(vals) / len(vals),
            "method_spread_mm": max(vals) - min(vals),
        })
    return out


def _mean(vals):
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


def _wind_annual_mean(wind_clim):
    by_month = wind_clim.get("by_month", {})
    overall = wind_clim.get("overall")
    total = 0.0
    days = 0
    for month, n_days in enumerate(MONTH_LENGTHS, start=1):
        u2 = by_month.get(month, overall)
        if u2 is None:
            continue
        total += u2 * n_days
        days += n_days
    return total / days if days else overall


def summarize(records, lat_deg, elev_m, wind_clim=None, u2=2.0):
    annual = {}
    monthly = {}
    for r in records:
        annual.setdefault(str(r["year"]), []).append(r)
        monthly.setdefault("%04d-%02d" % (r["year"], r["month"]), []).append(r)

    def block(recs):
        return {m: round(_mean([r[m] for r in recs]), 3) for m in METHODS + ("method_mean_mm", "method_spread_mm")}

    humidity_sources = sorted({r["humidity_source"] for r in records})
    if wind_clim:
        wind_provenance = {
            "wind_assumed": False,
            "u2_source": "gridmet_monthly_climatology",
            "u2_annual_mean_m_s": round(_wind_annual_mean(wind_clim), 3),
            "n_wind_days": wind_clim["n_days"],
        }
        wind_note = (
            "FAO-56 PM uses gridMET monthly-mean 2 m wind climatology rather "
            "than exact-day pairing, avoiding calendar mismatch with Daymet."
        )
    else:
        wind_provenance = {"wind_assumed": True, "u2_m_s": u2}
        wind_note = (
            "FAO-56 PM uses assumed u2=2 m/s, so method spread should be "
            "reported as uncertainty, especially during windy/dry conditions."
        )
    return {
        "source": "derive_et0_daily.py from local Daymet forcing",
        "latitude_deg": round(lat_deg, 6),
        "elevation_m": round(elev_m, 2),
        "records": len(records),
        "methods": list(METHODS),
        "annual_means_mm_day": {k: block(v) for k, v in sorted(annual.items())},
        "monthly_means_mm_day": {k: block(v) for k, v in sorted(monthly.items())},
        "overall_means_mm_day": block(records),
        "method_spread": {
            "mean_daily_mm": round(_mean([r["method_spread_mm"] for r in records]), 3),
            "p95_daily_mm": round(sorted(r["method_spread_mm"] for r in records)[int(0.95 * (len(records) - 1))], 3) if records else None,
        },
        "humidity_provenance": humidity_sources,
        "wind_provenance": wind_provenance,
        "uncertainty_note": (
            "Reference ET is reduced-data, not flux-tower ET. Daymet vp is modeled "
            "humidity when present; otherwise actual vapor pressure is estimated "
            "from Tmin. " + wind_note
        ),
    }


def write_outputs(records, summary, data_dir):
    out_dir = os.path.join(data_dir, "et")
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "et0_daily.csv")
    fields = ["date", "year", "yday", "tmean_c", "ra_mj_m2_d", "rs_mj_m2_d",
              "rso_mj_m2_d", "rn_mj_m2_d", "ea_kpa", "es_kpa", "vpd_kpa",
              "humidity_source", "wind_assumed", *METHODS,
              "method_mean_mm", "method_spread_mm"]
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in records:
            writer.writerow({k: (round(r[k], 4) if isinstance(r.get(k), float) else r.get(k)) for k in fields})
    summary_path = os.path.join(out_dir, "et0-summary.json")
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    return csv_path, summary_path


def register_outputs(data_dir, paths, summary):
    store_path = os.path.join(data_dir, "twin.gpkg")
    if not os.path.exists(store_path):
        return None
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")
    store = twin_store.Store(store_path)
    run = store.begin_run("derive_et0_daily.py", inputs=paths,
                          notes="daily reduced-data ET0 ensemble")
    eid = "et_dataset:et0_daily"
    store.upsert_entity(eid, "et_dataset", run)
    store.observe(eid, "summary", summary.get("overall_means_mm_day"), run,
                  source="derive_et0_daily.py")
    for path in paths:
        rel = os.path.relpath(path, data_dir)
        store.upsert_layer("et_" + os.path.splitext(os.path.basename(path))[0],
                           label=os.path.basename(path), kind="table" if path.endswith(".csv") else "json",
                           acquisition="derived", source_path=rel,
                           status="ok", content_sha1=twin_store.sha1_file(path))
    store.finish_run(run, notes="wrote et0_daily.csv and et0-summary.json")
    store.close()
    return run


def lat_elev(data_dir):
    georef_path = os.path.join(data_dir, "georef.json")
    twin_georef.GEOREF_PATH = georef_path
    g = twin_georef.load(georef_path)
    if g.get("origin_wgs84"):
        lat = float(g["origin_wgs84"]["lat"])
    else:
        ox, oy = twin_georef.origin(georef_path)
        fwd, _ = twin_georef.transformers(georef_path)
        _lon, lat = fwd.transform(ox, oy)
    try:
        grid = hydro.load_grid(data_dir)
        elev = float(grid.get("min_elevation") or 0.0)
    except Exception:
        elev = 0.0
    return lat, elev


def self_test():
    assert yday_to_month(1) == 1
    assert yday_to_month(59) == 2
    assert yday_to_month(60) == 3
    assert yday_to_month(365) == 12
    rows = []
    for j in range(1, 366):
        tmean = 6.0 + 10.0 * math.sin(2 * math.pi * (j - 95) / 365)
        rows.append({
            "year": 2020, "yday": j, "date": _daymet_date(2020, j),
            "prcp": 1.0, "swe": 0.0,
            "tmin": tmean - 3.0, "tmax": tmean + 3.0,
            "srad": 260.0, "dayl": 43000.0,
            "vp": saturation_vapor_pressure_kpa(tmean - 3.0) * 1000.0,
        })
    recs = compute_et0(rows, 44.0, 450.0)
    for m in METHODS:
        vals = [r[m] for r in recs]
        assert min(vals) >= 0.0, m
        assert max(vals) <= 7.5, (m, max(vals))  # reduced-data ET0 rarely exceeds ~7 mm/d here
    # Cross-method physical sanity. Hargreaves-Samani and FAO-56 PM are both "full"
    # methods and must agree within ~2x; a units error (e.g. Ra left in MJ instead of
    # mm) shows up as a ~2.45x Hargreaves outlier, which these bounds catch.
    ann = {m: sum(r[m] for r in recs) for m in METHODS}
    hs_pm = ann["hargreaves_samani_mm"] / ann["fao56_pm_reduced_mm"]
    assert 0.5 < hs_pm < 2.2, ("hargreaves/PM annual ratio out of range", round(hs_pm, 3))
    spread = max(ann.values()) / min(ann.values())
    assert spread < 3.0, ("cross-method annual spread too large", round(spread, 3))
    summ = summarize(recs, 44.0, 450.0)
    assert summ["overall_means_mm_day"]["method_mean_mm"] > 0.5
    print("derive_et0_daily.py self-test ok (hs/pm=%.2f, spread=%.2f)" % (hs_pm, spread))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    data_dir = os.path.abspath(args.data_dir)
    daymet = os.path.join(data_dir, "climate", "daymet_daily.csv")
    if not os.path.exists(daymet):
        raise SystemExit("missing %s; run the pack climate fetcher first" % daymet)
    lat, elev = lat_elev(data_dir)
    wind_clim = load_monthly_wind_climatology(data_dir)
    records = compute_et0(read_daymet_csv(daymet), lat, elev, wind_clim=wind_clim)
    summary = summarize(records, lat, elev, wind_clim=wind_clim)
    paths = write_outputs(records, summary, data_dir)
    run = register_outputs(data_dir, paths, summary)
    if wind_clim:
        print("Wind: gridMET monthly climatology, annual mean u2=%.2f m/s (%d days)" %
              (_wind_annual_mean(wind_clim), wind_clim["n_days"]))
    else:
        print("Wind: assumed FAO-56 fallback u2=2.00 m/s")
    print("Wrote %s (%d records)" % (os.path.relpath(paths[0], data_dir), len(records)))
    print("Wrote %s" % os.path.relpath(paths[1], data_dir))
    if run:
        print("Registered ET0 outputs in store (run %d)" % run)


if __name__ == "__main__":
    main()
