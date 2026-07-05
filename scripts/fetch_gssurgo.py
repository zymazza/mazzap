#!/usr/bin/env python3
"""Fetch and clip gSSURGO/SSURGO soils for the active twin.

Uses USDA Soil Data Access (SDA) to fetch map-unit polygons intersecting the
twin footprint plus hydrologic/tabular attributes. Writes the artifacts VEIL's
query layer already reads:

  <data>/soils/features.geojson
  <data>/soils/tabular.json
  <data>/soils/metadata.json
  <data>/atlas/local/gssurgo_soils.geojson

and registers `gssurgo_soils` in viewer-layers.json and twin.gpkg.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

from osgeo import ogr
from pyproj import Transformer

ogr.UseExceptions()

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_georef  # noqa: E402
import twin_store  # noqa: E402

SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
UM_S_TO_MM_HR = 3.6
UA = "veil/1.0 (+https://github.com; USDA SDA SSURGO fetch)"
PALETTE = ["#f4d35e", "#ee964b", "#f95738", "#0d3b66", "#1b998b", "#8d6a9f",
           "#ff8360", "#3d5a80", "#e07a5f", "#81b29a", "#b5838d", "#6d6875",
           "#995d81", "#5a7d7c", "#a44a3f", "#5b8c5a"]


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)


def sda_query(sql, timeout=180):
    body = json.dumps({"query": sql, "format": "JSON+COLUMNNAME"}).encode("utf-8")
    req = urllib.request.Request(
        SDA_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            table = data.get("Table") or []
            if not table:
                return []
            header = table[0]
            return [dict(zip(header, row)) for row in table[1:]]
        except Exception as err:  # noqa: BLE001
            last = err
            time.sleep(2 * (attempt + 1))
    raise last


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def twin_footprint(data_dir):
    georef_path = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    crs = twin_georef.crs(georef_path)
    ox, oy = twin_georef.origin(georef_path)
    abs_bounds = [
        grid["outerMinX"] + ox, grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox, grid["outerMaxY"] + oy,
    ]
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lons, lats = [], []
    for x, y in ((abs_bounds[0], abs_bounds[1]), (abs_bounds[2], abs_bounds[1]),
                 (abs_bounds[2], abs_bounds[3]), (abs_bounds[0], abs_bounds[3])):
        lon, lat = to_wgs.transform(x, y)
        lons.append(lon)
        lats.append(lat)
    bbox_wgs = [min(lons), min(lats), max(lons), max(lats)]
    rect = ogr.Geometry(ogr.wkbPolygon)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((grid["outerMinX"], grid["outerMinY"]),
                 (grid["outerMaxX"], grid["outerMinY"]),
                 (grid["outerMaxX"], grid["outerMaxY"]),
                 (grid["outerMinX"], grid["outerMaxY"]),
                 (grid["outerMinX"], grid["outerMinY"])):
        ring.AddPoint_2D(x, y)
    rect.AddGeometry(ring)
    return crs, (ox, oy), bbox_wgs, rect


def fetch_soil_polygons(bbox):
    minx, miny, maxx, maxy = bbox
    wkt = (f"POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},"
           f"{minx} {maxy},{minx} {miny}))")
    return sda_query(
        "SELECT mukey, mupolygonkey, mupolygongeo.STAsText() AS wkt "
        "FROM mupolygon "
        f"WHERE mupolygongeo.STIntersects(geometry::STGeomFromText('{wkt}',4326))=1")


def fetch_mapunit_aggregates(mukeys):
    rows = sda_query(
        "SELECT mu.mukey, mu.muname, mu.musym, mag.hydgrpdcd, mag.drclassdcd, "
        "mag.aws0150wta, mag.brockdepmin, mag.wtdepannmin, mag.wtdepaprjunmin, "
        "mag.flodfreqdcd, mag.hydclprs "
        "FROM mapunit mu LEFT JOIN muaggatt mag ON mu.mukey = mag.mukey "
        f"WHERE mu.mukey IN ({','.join(mukeys)})")
    return {
        str(r["mukey"]): {
            "muname": r.get("muname"),
            "musym": r.get("musym"),
            "hydrologic_group": r.get("hydgrpdcd"),
            "drainage_class": r.get("drclassdcd"),
            "available_water_storage_0_150cm_cm": fnum(r.get("aws0150wta")),
            "depth_to_bedrock_min_cm": fnum(r.get("brockdepmin")),
            "water_table_depth_annual_min_cm": fnum(r.get("wtdepannmin")),
            "water_table_depth_apr_jun_min_cm": fnum(r.get("wtdepaprjunmin")),
            "flooding_frequency": r.get("flodfreqdcd"),
            "pct_hydric": fnum(r.get("hydclprs")),
        } for r in rows
    }


def fetch_horizon_profiles(mukeys):
    rows = sda_query(
        "SELECT c.mukey, c.cokey, c.compname, c.comppct_r, "
        "ch.hzdept_r, ch.hzdepb_r, ch.ksat_r, ch.awc_r, "
        "ch.sandtotal_r, ch.silttotal_r, ch.claytotal_r "
        "FROM component c JOIN chorizon ch ON c.cokey = ch.cokey "
        f"WHERE c.mukey IN ({','.join(mukeys)}) AND c.majcompflag = 'Yes' "
        "ORDER BY c.mukey, c.comppct_r DESC, ch.hzdept_r")
    profiles = {}
    for r in rows:
        mk = str(r["mukey"])
        comp = profiles.setdefault(mk, {}).setdefault(str(r["cokey"]), {
            "compname": r.get("compname"),
            "comppct": fnum(r.get("comppct_r")),
            "horizons": [],
        })
        comp["horizons"].append({
            "top_cm": fnum(r.get("hzdept_r")),
            "bottom_cm": fnum(r.get("hzdepb_r")),
            "ksat_um_s": fnum(r.get("ksat_r")),
            "awc_cm_cm": fnum(r.get("awc_r")),
            "sand_pct": fnum(r.get("sandtotal_r")),
            "silt_pct": fnum(r.get("silttotal_r")),
            "clay_pct": fnum(r.get("claytotal_r")),
        })
    return profiles


def summarize_profile(components):
    if not components:
        return {}
    dom = max(components.values(), key=lambda c: c["comppct"] or 0)
    horizons = [h for h in dom["horizons"]
                if h["top_cm"] is not None and h["bottom_cm"] is not None]
    if not horizons:
        return {"dominant_component": dom["compname"],
                "dominant_component_pct": dom["comppct"]}
    soil = [h for h in horizons if (h["ksat_um_s"] or 0) > 0.05]
    ksat_vals = [h["ksat_um_s"] for h in soil if h["ksat_um_s"] is not None]
    total_thickness = sum(h["bottom_cm"] - h["top_cm"] for h in soil) or None
    depthwtd = (
        sum(h["ksat_um_s"] * (h["bottom_cm"] - h["top_cm"])
            for h in soil if h["ksat_um_s"] is not None) / total_thickness
    ) if total_thickness else None
    awc_profile_cm = sum((h["awc_cm_cm"] or 0) * (h["bottom_cm"] - h["top_cm"])
                         for h in horizons)

    def mmhr(v):
        return round(v * UM_S_TO_MM_HR, 2) if v is not None else None

    surface = horizons[0]["ksat_um_s"]
    profile_min = min(ksat_vals) if ksat_vals else None
    return {
        "dominant_component": dom["compname"],
        "dominant_component_pct": dom["comppct"],
        "surface_ksat_um_s": surface,
        "surface_ksat_mm_hr": mmhr(surface),
        "profile_ksat_min_um_s": profile_min,
        "profile_ksat_min_mm_hr": mmhr(profile_min),
        "profile_ksat_depthwtd_um_s": round(depthwtd, 3) if depthwtd else None,
        "profile_ksat_depthwtd_mm_hr": mmhr(depthwtd),
        "awc_profile_cm": round(awc_profile_cm, 2),
        "horizons": dom["horizons"],
    }


def reproject_clip(wkt_4326, transformer, origin, footprint_rect):
    ox, oy = origin
    src = ogr.CreateGeometryFromWkt(wkt_4326)
    if src is None:
        return None
    out = ogr.Geometry(ogr.wkbMultiPolygon)
    polys = [src] if src.GetGeometryName() == "POLYGON" else [
        src.GetGeometryRef(i) for i in range(src.GetGeometryCount())]
    for poly in polys:
        if poly is None or poly.GetGeometryName() != "POLYGON":
            continue
        new_poly = ogr.Geometry(ogr.wkbPolygon)
        for ri in range(poly.GetGeometryCount()):
            ring = poly.GetGeometryRef(ri)
            new_ring = ogr.Geometry(ogr.wkbLinearRing)
            for pi in range(ring.GetPointCount()):
                lon, lat, *_ = ring.GetPoint(pi)
                x, y = transformer.transform(lon, lat)
                new_ring.AddPoint_2D(x - ox, y - oy)
            new_poly.AddGeometry(new_ring)
        out.AddGeometry(new_poly)
    clipped = out.Intersection(footprint_rect)
    if clipped is None or clipped.IsEmpty():
        return None
    return clipped


def register_layer(data_dir, entry):
    viewer = os.path.join(data_dir, "atlas", "local", "viewer-layers.json")
    doc = json.load(open(viewer)) if os.path.exists(viewer) else {"layers": []}
    doc["layers"] = [l for l in doc.get("layers", []) if l.get("id") != entry["id"]]
    doc["layers"].append(entry)
    write_json(viewer, doc)

    src = os.path.join(data_dir, entry["file"])
    sha = None
    if os.path.exists(src):
        h = hashlib.sha1()
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        sha = h.hexdigest()
    twin_store.JOURNAL_DIR = os.path.join(data_dir, "journal")
    store = twin_store.Store(os.path.join(data_dir, "twin.gpkg"), journal=True)
    try:
        run = store.begin_run("fetch_gssurgo.py", inputs={"layer": entry["id"]})
        store.upsert_layer(entry["id"], label=entry["label"], kind="vector",
                           acquisition=entry.get("acquisition"), service=entry.get("service"),
                           source_path=entry["file"], feature_count=entry.get("feature_count"),
                           status="ok", content_sha1=sha)
        store.finish_run(run)
    finally:
        store.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    args = ap.parse_args()
    data_dir = os.path.abspath(args.data_dir)
    crs, origin, bbox, footprint_rect = twin_footprint(data_dir)
    print("fetching gSSURGO soils from USDA SDA over bbox %s" %
          [round(v, 5) for v in bbox])
    rows = fetch_soil_polygons(bbox)
    if not rows:
        write_json(os.path.join(data_dir, "soils", "metadata.json"), {
            "status": "unavailable",
            "source": "USDA Soil Data Access (SDA)",
            "service": SDA_URL,
            "reason": "SDA returned no map-unit polygons for this AOI",
        })
        print("gSSURGO unavailable here")
        return 0

    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    by_mukey = {}
    for row in rows:
        geom = reproject_clip(row["wkt"], transformer, origin, footprint_rect)
        if geom is not None:
            by_mukey.setdefault(str(row["mukey"]), []).append(geom)
    mukeys = sorted(by_mukey)
    if not mukeys:
        print("gSSURGO polygons fetched, but none survived footprint clip")
        return 0

    aggregates = fetch_mapunit_aggregates(mukeys)
    profiles = fetch_horizon_profiles(mukeys)
    tab = {}
    for mk in mukeys:
        rec = dict(aggregates.get(mk, {}))
        rec.update(summarize_profile(profiles.get(mk, {})))
        rec["mukey"] = mk
        tab[mk] = rec

    features = []
    legend = []
    total_area = 0.0
    for i, mk in enumerate(mukeys):
        union = by_mukey[mk][0]
        for extra in by_mukey[mk][1:]:
            union = union.Union(extra)
        area_m2 = union.GetArea()
        total_area += area_m2
        rec = tab[mk]
        name = rec.get("muname") or f"Map unit {mk}"
        color = PALETTE[i % len(PALETTE)]
        props = {
            "mukey": mk,
            "musym": rec.get("musym"),
            "soil_name": name,
            "__label": name,
            "drainage_class": rec.get("drainage_class"),
            "hydrologic_group": rec.get("hydrologic_group"),
            "area_acres": round(area_m2 / 4046.8564, 2),
            "color": color,
        }
        features.append({"type": "Feature", "id": mk, "properties": props,
                         "geometry": json.loads(union.ExportToJson())})
        legend.append({**props, "area_m2": round(area_m2, 2)})
    for row in legend:
        row["area_pct"] = round(100 * row["area_m2"] / (total_area or 1), 1)
    legend.sort(key=lambda r: -r["area_m2"])
    predominant = dict(legend[0]) if legend else None

    fc = {"type": "FeatureCollection", "features": features}
    write_json(os.path.join(data_dir, "soils", "features.geojson"), fc)
    write_json(os.path.join(data_dir, "atlas", "local", "gssurgo_soils.geojson"), fc)
    write_json(os.path.join(data_dir, "soils", "tabular.json"), {
        "source": "USDA Soil Data Access (SDA) - SSURGO tabular + spatial",
        "service": SDA_URL,
        "acquisition": "api_snapshot",
        "ksat_units_note": "Ksat native units are micrometers/second; mm_hr = x3.6.",
        "mukey_count": len(tab),
        "map_units": tab,
    })
    write_json(os.path.join(data_dir, "soils", "metadata.json"), {
        "status": "ok",
        "feature_count": len(features),
        "legend_entries": legend,
        "predominant_soil": predominant,
        "unique_soil_count": len(mukeys),
        "tabular_enriched": True,
        "source": "USDA SDA SSURGO (spatial + tabular)",
        "service": SDA_URL,
    })
    register_layer(data_dir, {
        "id": "gssurgo_soils",
        "label": "Soils (gSSURGO)",
        "type": "polygon",
        "file": "atlas/local/gssurgo_soils.geojson",
        "fill": "rgba(181,131,141,0.45)",
        "stroke": "#b5838d",
        "categorical": True,
        "feature_count": len(features),
        "acquisition": "api_snapshot",
        "service": "USDA Soil Data Access (SSURGO)",
    })
    print("wrote gSSURGO soils: %d map units; predominant: %s" %
          (len(features), predominant["soil_name"] if predominant else "-"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
