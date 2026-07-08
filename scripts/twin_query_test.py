#!/usr/bin/env python3
"""Tests for scripts/twin_query.py against the real data/twin.gpkg.

No mocks, no test framework: the store is deterministic (seeded RNGs,
journaled history), so real-data assertions are cheap and meaningful.

    python3 scripts/twin_query_test.py
"""

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
os.environ.setdefault("TWIN_DATA_DIR",
                      os.path.join(PROJECT, "tests", "fixtures", "mini-twin", "data"))
sys.path.insert(0, HERE)

import twin_astro  # noqa: E402
import twin_viewshed  # noqa: E402
import twin_query  # noqa: E402
import twin_store  # noqa: E402
from twin_query import (TwinQuery, TwinQueryError, resolve_region,  # noqa: E402
                        geometry_distance_m, point_geometry, point_in_rings)

ANN = twin_query.ANNOTATIONS_PATH
PASS = 0
FAIL = 0
FAILURES = []


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


def expect_error(name, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except TwinQueryError as e:
        check(name, True)
        return e.payload
    check(name, False, "expected TwinQueryError")
    return {}


def finish():
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        print("failures:", ", ".join(FAILURES))
        raise SystemExit(1)
    raise SystemExit(0)


def _distinctive_anchor_literal(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    if abs(value) in (0.0, 1.0, 2.0, 3.0):
        return None
    text = str(value)
    if "." not in text:
        return None
    frac = text.split(".", 1)[1].rstrip("0")
    if abs(value) >= 10.0 and frac:
        return text
    if len(frac) >= 3:
        return text
    return None


def _benchmark_coordinate_anchor_literals():
    cases_path = os.path.join(os.path.dirname(HERE), "benchmarks", "veil_geoqa",
                              "cases.veil-geoqa-v1.json")
    with open(cases_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    cases = doc.get("cases", doc) if isinstance(doc, dict) else doc
    literals = set()

    def add_points(points):
        for point in points or []:
            if not isinstance(point, dict):
                continue
            for key in ("x", "y", "lat", "lon"):
                lit = _distinctive_anchor_literal(point.get(key))
                if lit:
                    literals.add(lit)

    for case in cases or []:
        if not isinstance(case, dict):
            continue
        add_points(case.get("gold_points"))
        add_points(case.get("acceptable_points"))
        site_selection = case.get("site_selection") or {}
        if isinstance(site_selection, dict):
            add_points(site_selection.get("acceptable_points"))
            add_points(site_selection.get("gold_points"))
        gold_region = case.get("gold_region") or {}
        if isinstance(gold_region, dict):
            for vertex in gold_region.get("polygon") or []:
                if isinstance(vertex, (list, tuple)):
                    for coord in vertex[:2]:
                        lit = _distinctive_anchor_literal(coord)
                        if lit:
                            literals.add(lit)
    return literals


tq = TwinQuery()
G = tq.georef

# A scene-local polygon around a known tree stand (NE of the buildings,
# inside the parcel) used throughout the region tests.
STAND = [[-50.0, 22.0], [113.0, 22.0], [113.0, 190.0], [-50.0, 190.0]]
STAND_REGION = {"polygon": STAND}


print("== georef ==")
# round-trip: scene -> lat/lon -> scene within 1e-4 m (acceptance 8)
for x, y in [(0.0, 0.0), (50.0, 100.0), (-287.5, -393.1), (700.0, 808.0)]:
    e = G.echo(x, y)
    x2, y2 = G.to_scene(e["lon"], e["lat"])
    check(f"round-trip ({x},{y}) within 1e-4 m",
          math.hypot(x2 - x, y2 - y) < 1e-4, f"err={math.hypot(x2 - x, y2 - y)}")

# agreement with the viewer: proj4js (vendored, fed the proj4 string from
# data/georef.json — same path georef.js uses in the browser) must agree with
# the pyproj transform used here to better than 1e-4 m.
node = shutil.which("node")
if node:
    out = subprocess.run(
        [node, "-e",
         "const g=require(process.argv[1]);"
         "for (const [e,n] of JSON.parse(process.argv[2])) {"
         "const r=g.projectedToGeographic(e,n);"
         "console.log(r.lon.toPrecision(17), r.lat.toPrecision(17));}",
         os.path.join(twin_store.PROJECT, "public", "viewer", "georef.js"),
         json.dumps([[G.ox, G.oy], [G.ox + 287, G.oy - 393], [G.ox - 287, G.oy + 393]])],
        capture_output=True, text=True, check=True)
    worst = 0.0
    for line, (dx, dy) in zip(out.stdout.splitlines(), [(0, 0), (287, -393), (-287, 393)]):
        lon_js, lat_js = map(float, line.split())
        lon_py, lat_py = G.to_lonlat(dx, dy)
        x_js, y_js = G.to_scene(lon_js, lat_js)
        worst = max(worst, math.hypot(x_js - dx, y_js - dy))
    check("viewer proj4js agrees with pyproj (<1e-4 m)", worst < 1e-4, f"worst={worst}")
else:
    print("  skip  georef.js comparison (node not found)")

# a lat/lon for scene point (50,100), derived from this twin's own georef
# (no hardcoded coordinates — works for any twin)
_lon, _lat = G.to_lonlat(50, 100)
p = twin_query.resolve_point({"lat": _lat, "lon": _lon}, G)
check("lat/lon point resolves near (50,100)",
      math.hypot(p[0] - 50, p[1] - 100) < 0.05, str(p))
expect_error("point with both coordinate pairs rejected",
             twin_query.resolve_point, {"lat": 1, "lon": 2, "x": 3, "y": 4}, G)
expect_error("point with neither pair rejected",
             twin_query.resolve_point, {"lat": 1}, G)


print("== resolve_region ==")
r = resolve_region({"aoi": True}, G)
check("aoi region has positive area", r.area_m2 > 1e5, str(r.area_m2))
check("aoi contains scene origin", r.contains(0, 0))
far_x = r.bounds[2] + max(100.0, r.bounds[2] - r.bounds[0])
far_y = r.bounds[3] + max(100.0, r.bounds[3] - r.bounds[1])
check("aoi excludes far point", not r.contains(far_x, far_y))

r = resolve_region({"bbox": [-10, -20, 10, 20]}, G)
check("bbox area", abs(r.area_m2 - 800) < 1e-6)
check("bbox contains/excludes", r.contains(0, 0) and not r.contains(11, 0))

r = resolve_region({"within_m": 50, "point": {"x": 100, "y": 100}}, G)
check("circle contains center+edge", r.contains(100, 100) and r.contains(149, 100))
check("circle excludes outside", not r.contains(151, 100))

r = resolve_region(STAND_REGION, G)
check("scene polygon contains interior", r.contains(0, 100))
check("scene polygon excludes just-outside point", not r.contains(114, 100))
check("scene polygon area (shoelace)", abs(r.area_m2 - 163 * 168) < 1)

geo_poly = [[G.echo(x, y)["lon"], G.echo(x, y)["lat"]] for x, y in STAND]
rg = resolve_region({"polygon": geo_poly}, G)
check("lon/lat polygon auto-detected and matches scene polygon",
      abs(rg.area_m2 - r.area_m2) < 1.0 and rg.contains(0, 100)
      and not rg.contains(114, 100))

expect_error("region with two shapes rejected", resolve_region,
             {"aoi": True, "bbox": [0, 0, 1, 1]}, G)
expect_error("bbox min>=max rejected", resolve_region, {"bbox": [1, 0, 0, 1]}, G)
expect_error("within_m without point rejected", resolve_region, {"within_m": 5}, G)
expect_error("two-vertex polygon rejected", resolve_region,
             {"polygon": [[0, 0], [1, 1]]}, G)
check("region=None means no filter", resolve_region(None, G) is None)


print("== grid contract ==")
import ingest_dem  # noqa: E402
for grid_file, primary in (("grid.json", True), ("grid.apron.json", False)):
    grid_path = os.path.join(twin_store.DATA_DIR, "terrain", grid_file)
    if not os.path.exists(grid_path):
        print(f"  skip  {grid_file} not present")
        continue
    errors = ingest_dem.validate_grid(json.load(open(grid_path)), primary=primary)
    check(f"{grid_file} honors docs/grid-contract.md", not errors, "; ".join(errors))


print("== describe_twin ==")
d = tq.describe_twin()
conn = sqlite3.connect(twin_store.STORE_PATH)
for kind in ("tree", "shrub", "building", "parcel"):
    alive = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE kind=? AND retired_run_id IS NULL",
        (kind,)).fetchone()[0]
    check(f"{kind} alive count matches store",
          d["entity_counts"].get(kind, {}).get("alive", 0) == alive,
          f"{d['entity_counts'].get(kind, {}).get('alive', 0)} != {alive}")
import twin_georef  # noqa: E402
check("store crs matches data/georef.json",
      d["crs"]["analysis_crs"] == twin_georef.crs())
check("origin matches georef", d["origin_utm"][:2] == list(twin_georef.origin()))
check("run history present", len(d["pipeline_runs"]) >= 1)
check("extent corners carry lat/lon",
      "lat" in d["extent_corners"]["southwest"])

print("== astronomy ==")
astro_site = twin_astro.site_from_georef()
winter_sky = tq.sky_at("2026-01-15T17:00:00Z")
check("sky_at returns sun/moon/twilight shape",
      "sun" in winter_sky and "moon" in winter_sky and winter_sky.get("twilight"))
polaris = tq.body_position("polaris", "2026-01-01T05:00:00Z")
check("Polaris altitude tracks site latitude",
      abs(polaris["altitude_deg"] - astro_site.lat) < 1.0,
      f"{polaris['altitude_deg']} vs {astro_site.lat}")
noon_sun = tq.body_position("sun", "2026-06-21T19:00:00Z")
check("solstice solar-noon sun altitude matches site latitude",
      abs(noon_sun["altitude_deg"] - (90.0 - astro_site.lat + 23.44)) < 3.0,
      str(noon_sun["altitude_deg"]))

eclipse = tq.next_sky_event("solar_eclipse", from_time="2024-04-01T00:00:00Z")
first_eclipse = eclipse["events"][0] if eclipse.get("events") else {}
check("April 2024 local solar eclipse found with partial coverage here",
      str(first_eclipse.get("peak", {}).get("iso", "")).startswith("2024-04-08")
      and 0.2 < first_eclipse.get("obscuration", 0) < 0.95,
      json.dumps({k: first_eclipse.get(k) for k in ("peak", "obscuration", "local_kind")},
                 sort_keys=True))

twin_site_dict = {"lat": astro_site.lat, "lon": astro_site.lon, "height_m": astro_site.height_m}
dallas = {"lat": 32.7767, "lon": -96.797, "height_m": 150.0}
tse = twin_astro.next_sky_event("total_solar_eclipse", from_time="2024-03-01T00:00:00Z",
                                site=dallas, horizon_years=1.0)
check("path of totality found for Dallas 2024-04-08",
      tse["count"] == 1 and tse["events"][0]["local_kind"] == "total"
      and tse["events"][0]["peak"]["iso"].startswith("2024-04-08")
      and tse["events"][0]["total_begin"] is not None)
tse_here = twin_astro.next_sky_event("total_solar_eclipse", from_time="2024-03-01T00:00:00Z",
                                     site=twin_site_dict, horizon_years=2.0)
check("a partial eclipse at the twin site does not count as totality",
      tse_here["count"] == 0 and "no total_solar_eclipse" in str(tse_here.get("note", "")))

lun = tq.next_sky_event("lunar_eclipse", from_time="2025-03-01T00:00:00Z")
lun_ev = lun["events"][0]
check("2025-03-14 total lunar eclipse visible from the twin site",
      lun_ev["eclipse_kind"] == "total" and lun_ev["peak"]["iso"].startswith("2025-03-14")
      and lun_ev["visible_from_site"] is True and lun_ev["total_begin"] is not None)
lun_away = twin_astro.next_sky_event("lunar_eclipse", from_time="2025-08-01T00:00:00Z",
                                     site=twin_site_dict)
check("2025-09-07 total lunar eclipse correctly not visible from the twin site",
      lun_away["events"][0]["peak"]["iso"].startswith("2025-09-07")
      and lun_away["events"][0]["visible_from_site"] is False)
blood = twin_astro.next_sky_event("blood_moon", from_time="2025-08-01T00:00:00Z",
                                  site=twin_site_dict, horizon_years=3.0)
check("blood moon search skips the eclipse the site cannot see",
      bool(blood["events"]) and blood["events"][0]["peak"]["iso"].startswith("2026-03-03")
      and blood["events"][0]["visible_from_site"] is True,
      json.dumps((blood.get("events") or [{}])[0].get("peak"), sort_keys=True))

align = twin_astro.next_sky_event("planetary_alignment", from_time="2040-08-01T00:00:00Z",
                                  site=twin_site_dict, max_span_deg=20.0, horizon_years=2.0)
align_ev = align["events"][0] if align.get("events") else {}
check("September 2040 five-planet gathering found",
      str(align_ev.get("peak", {}).get("iso", "")).startswith("2040-09")
      and align_ev.get("span_deg", 99.0) <= 20.0
      and len(align_ev.get("planets", [])) == 5,
      json.dumps(align_ev.get("peak"), sort_keys=True))

supermoon = twin_astro.next_sky_event("supermoon", from_time="2026-01-01T00:00:00Z",
                                      site=twin_site_dict, horizon_years=2.0)
check("next supermoon is the 2026-12-24 perigee full moon",
      bool(supermoon["events"])
      and supermoon["events"][0]["time"]["iso"].startswith("2026-12-24")
      and supermoon["events"][0]["time"]["distance_km"] <= twin_astro.SUPERMOON_MAX_DISTANCE_KM,
      json.dumps((supermoon.get("events") or [{}])[0].get("time"), sort_keys=True))

irr = tq.solar_irradiance("2026-06-21T19:00:00Z")
check("clear-sky irradiance is physical at solar noon",
      700.0 < irr["ghi_wm2"] < 1200.0 and irr["dni_wm2"] > irr["dhi_wm2"],
      json.dumps({k: irr.get(k) for k in ("ghi_wm2", "dni_wm2", "dhi_wm2")}))

print("== sky directives ==")
astro_ann_backup = open(ANN).read() if os.path.exists(ANN) else None
try:
    seeded = {
        "version": 1,
        "updated_at": "2000-01-01T00:00:00Z",
        "annotations": [{"id": "drawing:0001", "type": "point", "x": 1, "y": 2}],
        "layer_views": [{"layer_id": "dummy", "visible": True}],
        "sky_views": [],
        "view_time": None,
    }
    with open(ANN, "w") as fh:
        json.dump(seeded, fh)
    view_time = tq.set_view_time("9999-01-01T00:00:00Z", rate=1e9)
    check("set_view_time clamps time and rate",
          view_time["view_time"]["iso"].startswith("2500-01-01")
          and view_time["view_time"]["rate"] == twin_astro.MAX_RATE)
    sky_highlight = tq.highlight_sky("orion", label="winter hunter")
    check("highlight_sky resolves a constellation",
          sky_highlight["sky_view"]["target_type"] == "constellation"
          and sky_highlight["sky_view"]["name"] == "Orion")
    with open(ANN) as fh:
        doc = json.load(fh)
    check("sky directives preserve drawings and layer views",
          len(doc["annotations"]) == 1 and len(doc["layer_views"]) == 1
          and doc["view_time"]["iso"].startswith("2500-01-01")
          and len(doc["sky_views"]) == 1)
    tq.draw_point({"x": 3, "y": 4}, label="preserve sky")
    with open(ANN) as fh:
        doc = json.load(fh)
    check("drawing writers preserve sky directives",
          len(doc["annotations"]) == 2 and len(doc["sky_views"]) == 1
          and doc["view_time"]["iso"].startswith("2500-01-01"))
    drape_ids = [l["id"] for l in tq._atlas_layers()
                 if l.get("type") in twin_query.DRAPE_TYPES]
    if drape_ids:
        tq.set_layer_visibility(drape_ids[0], visible=False)
        with open(ANN) as fh:
            doc = json.load(fh)
        check("layer writers preserve sky directives",
              len(doc["sky_views"]) == 1
              and doc["view_time"]["iso"].startswith("2500-01-01"))
    else:
        print("  skip  layer writer sky-preservation check (no drape-able layers)")
    cleared = tq.clear_sky_highlights()
    with open(ANN) as fh:
        doc = json.load(fh)
    check("clear_sky_highlights empties only sky_views",
          cleared["cleared"] == 1 and doc["sky_views"] == []
          and len(doc["annotations"]) == 2 and len(doc["layer_views"]) >= 1)
    now = tq.set_view_time("now")
    with open(ANN) as fh:
        doc = json.load(fh)
    check("set_view_time now clears to realtime",
          now["view_time"] is None and doc["view_time"] is None)
    demo = tq.next_sky_event("solar_eclipse", from_time="2024-04-01T00:00:00Z", demonstrate=True)
    with open(ANN) as fh:
        doc = json.load(fh)
    check("demonstrate scrubs the clock and highlights the sun",
          str(demo["demonstration"]["view_time"]["iso"]).startswith("2024-04-08")
          and demo["demonstration"]["view_time"]["rate"] == 60.0
          and demo["demonstration"]["highlighted"]
          and doc["view_time"]["iso"].startswith("2024-04-08")
          and any(str(v.get("name", "")).lower() == "sun"
                  and v.get("label") == "Solar eclipse" for v in doc["sky_views"]),
          json.dumps(demo.get("demonstration"), sort_keys=True))
finally:
    if astro_ann_backup is None:
        if os.path.exists(ANN):
            os.remove(ANN)
    else:
        with open(ANN, "w") as fh:
            fh.write(astro_ann_backup)
err = expect_error("unknown sky target rejected with suggestions",
                   tq.body_position, "__not_a_sky_target__")
check("unknown sky target error carries suggestions",
      isinstance(err.get("suggestions"), list))

print("== viewshed ==")
stack = twin_viewshed.RingStack.from_local_files(twin_store.DATA_DIR)
vx, vy = twin_viewshed.nearest_valid_point(stack)
bare_eye = twin_viewshed.sweep(stack, vx, vy, 1.7, n_az=360, surface="bare_earth")
bare_tower = twin_viewshed.sweep(stack, vx, vy, 120.0, n_az=360, surface="bare_earth")
canopy_eye = twin_viewshed.sweep(stack, vx, vy, 1.7, n_az=360, surface="canopy")
ring_name = stack.rings[0].name
check("AGL 120 viewshed is a superset of eye-level bare-earth mask",
      not np.any((bare_eye["visible"][ring_name] == 1) & (bare_tower["visible"][ring_name] == 0)))
check("canopy viewshed is subset of bare-earth mask",
      not np.any((canopy_eye["visible"][ring_name] == 1) & (bare_eye["visible"][ring_name] == 0)))
check("tower sees at least as much as eye level",
      np.count_nonzero(bare_tower["visible"][ring_name]) >= np.count_nonzero(bare_eye["visible"][ring_name]))

# Regression: the near ring must composite the parcel LiDAR over the apron so
# the parcel interior is real 3 m terrain, not a nodata hole that drops the
# near-field sweep onto the coarse distant ring (the Great-Sacandaga false-hide).
near_ring = stack.rings[0]
near_finite = float(np.isfinite(near_ring.ground).mean())
has_apron = os.path.exists(os.path.join(twin_store.DATA_DIR, "terrain", "grid.apron.json"))
if has_apron:
    check("near ring has no parcel-interior nodata hole (parcel merged over apron)",
          near_finite > 0.9, f"near-ring finite fraction {near_finite:.3f} (expected ~1.0)")
else:
    check("near ring loads finite terrain cells",
          near_finite > 0.1, f"near-ring finite fraction {near_finite:.3f}")
_scene_center = near_ring.sample_ground(np.asarray([0.0]), np.asarray([0.0]))[0]
check("near ring samples finite terrain at the scene interior",
      bool(np.isfinite(_scene_center)), f"center ground {_scene_center}")

vreg = {"visible_from": {"point": {"x": vx, "y": vy}, "agl_m": 120, "surface": "canopy"}}
before_sweep_keys = {k for k in tq._caches if isinstance(k, tuple) and k and k[0] == "viewshed_sweep"}
all_trees = tq.aggregate_entities("tree", "count")
vis_trees = tq.aggregate_entities("tree", "count", region=vreg)
check("visible_from region composes with aggregate_entities as a subset",
      0 <= vis_trees["groups"]["all"]["entity_count"] <= all_trees["groups"]["all"]["entity_count"])
_ = tq.find_entities("tree", region=vreg, limit=5)
after_sweep_keys = {k for k in tq._caches if isinstance(k, tuple) and k and k[0] == "viewshed_sweep"}
check("visible_from memo reuses one sweep across stacked calls", len(after_sweep_keys - before_sweep_keys) == 1, str(after_sweep_keys - before_sweep_keys))
summary_v = tq.summarize_region(vreg)
check("summarize_region accepts visible_from region", summary_v["region"]["shape"] == "visible_from")

vf = tq.viewshed_from({"x": vx, "y": vy}, agl_m=10, surface="canopy", demonstrate=True)
check("viewshed_from reports provenance and canopy cost",
      vf["surface"] == "canopy" and vf["provenance"]["manifest_hash"]
      and "canopy_hidden_km2" in vf and vf.get("demonstration", {}).get("layer", {}).get("id") == "viewshed_current")
los = tq.can_see({"x": vx, "y": vy}, {"x": vx + 30, "y": vy + 30}, from_agl_m=30, surface="canopy")
check("can_see returns intervisibility payload",
      ("visible" in los or los.get("error") == "needs_fetch") and ("k" in los or los.get("error")))
hz = tq.horizon_at({"x": vx, "y": vy}, date="2026-12-21", surface="canopy")
check("horizon_at returns sun windows and GEO arc",
      len(hz["horizon_72_deg"]) == 72 and any(w["blocked"] for w in hz["sun_windows"])
      and len(hz["geo_arc"]["samples"]) == 72)


required_full_kinds = {"building_model", "parcel", "stream"}
available_kinds = set(d["entity_counts"].keys())
is_full_integration_twin = required_full_kinds.issubset(available_kinds) \
    and len(d["pipeline_runs"]) >= 8
if not is_full_integration_twin:
    print("== fixture smoke ==")
    stand = tq.find_entities("tree", region=STAND_REGION)
    check("fixture polygon returns trees", stand["total_matched"] > 0)
    check("fixture returned trees are inside the polygon", all(
        point_in_rings([STAND], e["position"]["x"], e["position"]["y"])
        for e in stand["entities"]))

    first_tree = stand["entities"][0]
    ent = tq.get_entity(first_tree["entity_id"])
    check("fixture tree entity has attrs and position",
          ent["position"]["x"] == first_tree["position"]["x"]
          and "height" in ent["attrs"])
    hist = tq.entity_history(first_tree["entity_id"], attr="height")
    check("fixture tree history returns height observation",
          hist["count"] >= 1 and hist["observations"][0]["attr"] == "height")

    ag = tq.aggregate_entities("tree", "count", group_by="type", region=STAND_REGION)
    check("fixture aggregate count matches find_entities",
          sum(g["value"] for g in ag["groups"].values()) == stand["total_matched"])
    mh = tq.aggregate_entities("tree", "mean:height", region=STAND_REGION)
    check("fixture mean height is positive", mh["groups"]["all"]["value"] > 0)

    cc = tq.canopy_change()
    check("fixture canopy history has one tree run",
          len(cc["runs"]) >= 1 and cc["runs"][-1]["tree_count"] > 0)
    expect_error("fixture bad metric rejected",
                 tq.aggregate_entities, "tree", "median:height")

    print("== map drawings ==")
    ann_backup = open(ANN).read() if os.path.exists(ANN) else None
    try:
        tq.clear_drawings()
        pt = tq.draw_point({"x": 50, "y": 100}, label="fixture point")
        check("fixture draw_point writes annotation",
              pt["annotations_total"] == 1 and "lat" in pt["drawn"]["position"])
        square = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
        pg = tq.draw_polygon(square, label="fixture square")
        check("fixture draw_polygon reports area",
              abs(pg["drawn"]["area_m2"] - 10000) < 1)
        cleared = tq.clear_drawings()
        check("fixture clear_drawings reports count", cleared["cleared"] == 2)
    finally:
        if ann_backup is None:
            if os.path.exists(ANN):
                os.remove(ANN)
        else:
            with open(ANN, "w") as fh:
                fh.write(ann_backup)

    finish()


print("== find_entities ==")
barn = tq.find_entities("building_model", attr_filters=["name = Barn"])
check("barn found by name filter", barn["total_matched"] == 1
      and barn["entities"][0]["entity_id"] == "building_model:B-4")
bpos = barn["entities"][0]["position"]

tall = tq.find_entities("tree", near={"x": bpos["x"], "y": bpos["y"]},
                        within_m=50, attr_filters=["height > 20"])
check("tall trees near barn found", tall["total_matched"] > 0)
check("near results sorted by distance",
      [e["distance_m"] for e in tall["entities"]]
      == sorted(e["distance_m"] for e in tall["entities"]))
check("all within 50 m", all(e["distance_m"] <= 50 for e in tall["entities"]))
check("all heights > 20", all(e["attrs"]["height"]["value"] > 20
                              for e in tall["entities"]))
check("height provenance present", all(
    e["attrs"]["height"]["source"] and e["attrs"]["height"]["run_id"]
    and e["attrs"]["height"]["observed_at"] for e in tall["entities"]))

# Regression: vector proximity must use the real line/polygon geometry, not the
# display centroid. A point on a long stream or parcel boundary should match the
# feature even when the centroid is many meters away.
stream_case = None
for eid, geom in tq._geometries("stream").items():
    for path in twin_query.line_paths(geom):
        if len(path) >= 2:
            px, py = path[0][:2]
            cx, cy = tq._positions("stream")[eid]
            if math.hypot(px - cx, py - cy) > 5:
                stream_case = eid, px, py
                break
    if stream_case:
        break
check("test stream has endpoint away from centroid", stream_case is not None)
if stream_case:
    sid, sx, sy = stream_case
    near_stream = tq.find_entities("stream", near={"x": sx, "y": sy},
                                   within_m=1, limit=10)
    row = next((e for e in near_stream["entities"] if e["entity_id"] == sid), None)
    check("stream within_m uses line geometry, not centroid",
          row is not None and row["distance_m"] <= 0.01,
          json.dumps(near_stream["entities"][:3], indent=2))

parcel_tree_case = None
for pid, parcel_geom in tq._geometries("parcel").items():
    pcx, pcy = tq._positions("parcel")[pid]
    for tid, (tx, ty) in tq._positions("tree").items():
        if geometry_distance_m(point_geometry(tx, ty), parcel_geom) <= 0.01:
            if math.hypot(tx - pcx, ty - pcy) > 5:
                parcel_tree_case = pid, tid
                break
    if parcel_tree_case:
        break
check("test parcel has interior tree away from centroid", parcel_tree_case is not None)
if parcel_tree_case:
    pid, tid = parcel_tree_case
    near_parcel = tq.find_entities("tree", near={"entity_id": pid},
                                   within_m=1, limit=1000)
    row = next((e for e in near_parcel["entities"] if e["entity_id"] == tid), None)
    check("near entity_id uses polygon geometry, not centroid",
          row is not None and row["distance_m"] <= 0.01,
          f"tree={tid} parcel={pid} returned={near_parcel['total_matched']}")

near_eid = tq.find_entities("tree", near={"entity_id": "building_model:B-4"},
                            within_m=50, attr_filters=["height > 20"])
check("near accepts entity_id", near_eid["total_matched"] == tall["total_matched"])

stand = tq.find_entities("tree", region=STAND_REGION, limit=1000)
reg = resolve_region(STAND_REGION, G)
check("polygon region returns trees", stand["total_matched"] > 100)
check("every returned tree is inside the polygon", all(
    reg.contains(e["position"]["x"], e["position"]["y"])
    for e in stand["entities"]))

evg = tq.find_entities("tree", region=STAND_REGION,
                       attr_filters=["type = evergreen"], limit=1)
check("string filter case-insensitive subset",
      0 < evg["total_matched"] <= stand["total_matched"])

lim = tq.find_entities("tree", region=STAND_REGION, limit=5)
check("limit caps returned, not total",
      lim["returned"] == 5 and lim["total_matched"] == stand["total_matched"])

expect_error("near+region together rejected", tq.find_entities, "tree",
             near={"x": 0, "y": 0}, within_m=5, region=STAND_REGION)
expect_error("unknown kind rejected", tq.find_entities, "dragon")
expect_error("bad filter syntax rejected", tq.find_entities, "tree",
             attr_filters=["height >>"])


print("== get_entity / entity_history ==")
tree_id = stand["entities"][0]["entity_id"]
e = tq.get_entity(tree_id)
check("tree entity has position + attrs + creation run",
      "position" in e and "height" in e["attrs"] and e["created"]["run"])
check("tree attrs carry provenance", all(
    "source" in a and "run_id" in a and "observed_at" in a
    for a in e["attrs"].values()))

parcel_id = conn.execute("SELECT entity_id FROM parcels LIMIT 1").fetchone()[0]
pe = tq.get_entity(parcel_id)
check("parcel entity has scene-local geometry",
      pe.get("geometry_scene_m", {}).get("type", "").endswith("Polygon"))

lidar = tq.find_entities("tree", attr_filters=["source = lidar"], limit=1)
h = tq.entity_history(lidar["entities"][0]["entity_id"])
check("lidar tree has history", h["count"] >= 5)
check("history is oldest-first",
      [o["obs_id"] for o in h["observations"]]
      == sorted(o["obs_id"] for o in h["observations"]))
check("history rows carry run script", all(o["run_script"] for o in h["observations"]))
hh = tq.entity_history(lidar["entities"][0]["entity_id"], attr="height")
check("attr filter narrows history",
      0 < hh["count"] <= h["count"]
      and all(o["attr"] == "height" for o in hh["observations"]))
expect_error("unknown entity rejected", tq.get_entity, "tree:nope")


print("== identify_at / sample_raster ==")
# Every expectation below is derived from the twin under test — the layer
# catalog, the terrain grid — never hardcoded to a place.
a = tq.identify_at({"x": 50.0, "y": 100.0})
ids = {r["layer_id"] for r in a["atlas"]}
catalog_ids = {l["layer_id"] for l in tq.list_layers()["layers"]}
check("identify returns atlas layers at an interior point",
      len(ids) >= 2, str(ids))
check("every identified layer exists in the twin's catalog",
      ids <= catalog_ids, str(ids - catalog_ids))
if "gssurgo_soils" in ids:
    soil = next(r for r in a["atlas"] if r["layer_id"] == "gssurgo_soils")
    check("soil card carries drainage/hydrologic group",
          "drclassdcd" in soil["properties"] and "hydgrpdcd" in soil["properties"])
check("identify provenance on every atlas fact",
      all(r["provenance"].get("acquisition") for r in a["atlas"]))
if a.get("species_habitat"):
    check("species habitat list present", a["species_habitat"]["count"] > 0)
check("containing parcel reported",
      any(c["kind"] == "parcel" for c in a["entities_here"]))
with open(os.path.join(os.environ["TWIN_DATA_DIR"], "terrain", "grid.json")) as fh:
    _grid = json.load(fh)
check("elevation sampled within the twin's terrain range",
      _grid["minElevation"] - 1.0 <= (a["elevation_m"] or float("-inf"))
      <= _grid["maxElevation"] + 1.0)

b = tq.identify_at({"lat": a["point"]["lat"], "lon": a["point"]["lon"]})
check("identify identical via lat/lon and x/y (acceptance 8)",
      [r["name"] for r in a["atlas"]] == [r["name"] for r in b["atlas"]]
      and a["species_habitat"]["common_names"] == b["species_habitat"]["common_names"])

out = tq.identify_at({"x": 5000, "y": 5000})
check("outside extent is a structured result", out.get("outside_extent") is True
      and "extent_scene_m" in out)

s = tq.sample_raster("landfire_evt_2024", {"x": 50.0, "y": 100.0})
check("sample_raster value matches identify",
      s["value"] == next(r["value"] for r in a["atlas"]
                         if r["layer_id"] == "landfire_evt_2024"))
check("sample_raster has legend name", bool(s["name"]))
bad = expect_error("unknown raster lists valid ones", tq.sample_raster,
                   "nope", {"x": 0, "y": 0})
check("error payload lists raster ids",
      "landfire_evt_2024" in bad.get("valid_raster_layers", []))


print("== recommend_sites ==")
rec = tq.recommend_sites(objective="overlook", count=3, min_separation_m=120.0, draw=False)
check("recommend_sites returns requested candidate count", rec["returned_count"] == 3)
check("recommend_sites returns core objective evidence", all(
    isinstance(row.get("evidence", {}).get("elevation_m"), (int, float))
    and isinstance(row.get("evidence", {}).get("prominence_m"), (int, float))
    and isinstance(row.get("evidence", {}).get("hydrology"), dict)
    and isinstance(row.get("evidence", {}).get("landcover_scores"), dict)
    and isinstance(row.get("evidence", {}).get("soil_drainage_score"), (int, float))
    for row in rec["candidates"]))
aoi = resolve_region({"aoi": True}, G)
assert aoi is not None
check("recommend_sites candidates are inside AOI", all(
    aoi.contains(row["x"], row["y"]) for row in rec["candidates"]))
separation_ok = True
for i, left in enumerate(rec["candidates"]):
    for right in rec["candidates"][i + 1:]:
        if math.hypot(float(left["x"]) - float(right["x"]), float(left["y"]) - float(right["y"])) < 120.0:
            separation_ok = False
            break
check("recommend_sites candidates satisfy requested min separation", separation_ok)
check("recommend_sites returns scene-local and lat/lon", all(
    all(k in row for k in ("x", "y", "lat", "lon"))
    for row in rec["candidates"]))

ann_backup2 = open(ANN).read() if os.path.exists(ANN) else None
try:
    tq.clear_drawings()
    rec_draw = tq.recommend_sites(objective="overlook", count=2, min_separation_m=120.0,
                                  draw=True, label_prefix="rec-test")
    check("recommend_sites draw=True writes requested points", rec_draw["draw_count"] == rec_draw["returned_count"] == 2)
    with open(ANN) as fh:
        ann = json.load(fh)
    check("recommend_sites draw=True creates annotations entries", len(ann["annotations"]) == 2)
finally:
    if ann_backup2 is None:
        if os.path.exists(ANN):
            os.remove(ANN)
    else:
        with open(ANN, "w") as fh:
            fh.write(ann_backup2)


def test_recommend_sites_records_objective_and_scoring_profile():
    overlap = tq.recommend_sites(objective="overlook", count=2, min_separation_m=60.0, draw=False)
    spring = tq.recommend_sites(objective="well", count=2, min_separation_m=60.0, draw=False)

    check("recommend_sites normalizes and returns objective in response",
          overlap["objective"] == "overlook" and spring["objective"] == "well")
    check("recommend_sites echoes objective in candidate provenance",
          overlap["candidates"][0]["provenance"]["objective"] == "overlook"
          and spring["candidates"][0]["provenance"]["objective"] == "well")
    check("recommend_sites reports objective-specific scoring profiles",
          overlap["candidates"][0]["provenance"]["scoring"]["weights"]
          != spring["candidates"][0]["provenance"]["scoring"]["weights"])
    expected_inputs = {
        "DEM/terrain derivatives",
        "hydrology grids",
        "SSURGO soil drainage",
        "NLCD/LANDFIRE land-cover classes",
        "objective weights and NMS spacing",
    }
    inputs = set(overlap["candidates"][0]["provenance"]["scoring"].get("feature_inputs", []))
    check("recommend_sites scoring feature_inputs snapshot is general feature categories only",
          inputs == expected_inputs, sorted(inputs))
    check("recommend_sites response-level provenance records candidate count and draw flag",
          overlap["provenance"]["candidates_considered"] >= overlap["requested_count"]
          and overlap["provenance"]["draw"] is False)
    check("overlook recommendations report DEM peak refinement",
          "dem_peak_refinement" in overlap["provenance"]["lattice_strategy"]
          and all("peak_refinement_m" in c.get("evidence", {}) for c in overlap["candidates"]))

    source = open(os.path.join(HERE, "twin_query.py"), encoding="utf-8").read()
    start = source.find("def recommend_sites")
    end = source.find("    # -- point identify", start)
    recommend_source = source[start:end if end != -1 else len(source)]
    check("recommend_sites runtime has no benchmark fractional-lattice constants",
          "0.12 + 0.76" not in source and "coarse_7x7" not in source)
    check("recommend_sites runtime does not cite benchmark/gold lineage",
          "generated the benchmark" not in source and "gold" not in recommend_source)
    forbidden_anchor_fields = ("acceptable_points", "gold_points", "gold_region", "expected_claims")
    check("recommend_sites source does not reference benchmark answer-key fields",
          all(field not in recommend_source for field in forbidden_anchor_fields))
    anchor_literals = _benchmark_coordinate_anchor_literals()
    literal_leaks = [
        literal for literal in sorted(anchor_literals)
        if re.search(rf"(?<![\w.+-]){re.escape(literal)}(?![\w.+-])", recommend_source)
    ]
    check("recommend_sites source does not include benchmark coordinate anchor literals",
          not literal_leaks, literal_leaks[:10])
    weights_by_objective = [
        overlap["candidates"][0]["provenance"]["scoring"]["weights"],
        spring["candidates"][0]["provenance"]["scoring"]["weights"],
    ]
    normalized_weights = all(
        weights
        and all(isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0
                for v in weights.values())
        and abs(sum(float(v) for v in weights.values()) - 1.0) < 1e-9
        for weights in weights_by_objective
    )
    check("recommend_sites profile weights are normalized objective weights, not coordinate-scale anchors",
          normalized_weights, weights_by_objective)


test_recommend_sites_records_objective_and_scoring_profile()


def _annotation_points():
    if not os.path.exists(ANN):
        return []
    with open(ANN) as fh:
        doc = json.load(fh)
    return [a for a in doc.get("annotations", []) if a.get("type") == "point"]


def test_recommend_sites_species_intent_binds_hard_filter():
    res = tq.recommend_sites(objective="trail camera for Gray Fox", count=4,
                             min_separation_m=60.0, draw=False)
    # The bug: "trail camera for Gray Fox" used to normalize to trailcam and
    # silently drop "Gray Fox". Now the objective binds AND Gray Fox is a hard
    # filter reported transparently.
    check("species intent still binds the trailcam objective",
          res["objective"] == "trailcam")
    check("recommend_sites preserves the raw intent text",
          "gray fox" in str(res.get("raw_intent", "")).lower())
    gap_filters = [f for f in res.get("applied_filters", [])
                   if f.get("signal") == "gap_species"]
    check("Gray Fox is bound as a gap_species hard filter",
          any("gray fox" in [str(v).lower() for v in (f.get("value") or [])]
              for f in gap_filters))
    check("a resolvable species produces no unresolved terms",
          not res.get("unresolved_terms"))
    check("species-constrained recommendation still returns candidates",
          res["returned_count"] > 0)
    # Independent identify_at re-check: every candidate is inside Gray Fox
    # modeled habitat per the same sampler the viewer uses.
    recheck_ok = True
    for c in res["candidates"]:
        detail = tq.identify_at({"x": c["x"], "y": c["y"]})
        names = (detail.get("species_habitat") or {}).get("common_names") or []
        if "Gray Fox" not in names:
            recheck_ok = False
            break
    check("every candidate passes an independent Gray Fox identify_at re-check",
          recheck_ok)
    check("every candidate reports all hard constraints passed",
          all(c.get("constraint_report", {}).get("all_hard_passed") is True
              for c in res["candidates"]))
    check("each candidate carries per-constraint results",
          all(any(r.get("signal") == "gap_species"
                  for r in c.get("constraint_results", []))
              for c in res["candidates"]))


test_recommend_sites_species_intent_binds_hard_filter()


def test_recommend_sites_species_draw_is_validated():
    ann_backup = open(ANN).read() if os.path.exists(ANN) else None
    try:
        tq.clear_drawings()
        res = tq.recommend_sites(objective="trail camera for Gray Fox", count=3,
                                 min_separation_m=60.0, draw=True,
                                 label_prefix="fox-cam")
        check("draw_count counts only validated drawings",
              res["draw_count"] == res["returned_count"])
        pts = _annotation_points()
        check("draw=True writes one annotation per validated candidate",
              len(pts) == res["returned_count"])
        # Every drawn point must itself sit in Gray Fox habitat.
        all_in_habitat = True
        for a in pts:
            detail = tq.identify_at({"x": a["x"], "y": a["y"]})
            names = (detail.get("species_habitat") or {}).get("common_names") or []
            if "Gray Fox" not in names:
                all_in_habitat = False
                break
        check("every drawn point corresponds to a candidate in Gray Fox habitat",
              all_in_habitat)
    finally:
        if ann_backup is None:
            if os.path.exists(ANN):
                os.remove(ANN)
        else:
            with open(ANN, "w") as fh:
                fh.write(ann_backup)


test_recommend_sites_species_draw_is_validated()


def test_recommend_sites_impossible_species_draws_nothing():
    ann_backup = open(ANN).read() if os.path.exists(ANN) else None
    try:
        tq.clear_drawings()
        res = tq.recommend_sites(objective="trail camera for Unicorn", count=3,
                                 draw=True)
        check("an unresolved species target is reported transparently",
              any("unicorn" in str(t).lower()
                  for t in res.get("unresolved_terms", [])))
        check("an impossible/unresolved species yields zero candidates",
              res["returned_count"] == 0)
        check("an impossible/unresolved species draws nothing",
              res["draw_count"] == 0 and len(_annotation_points()) == 0)
        # strict=True turns the same intent into a structured error and draws nothing.
        payload = expect_error(
            "strict=True raises a structured error for unresolved terms",
            tq.recommend_sites, objective="trail camera for Unicorn",
            count=3, draw=True, strict=True)
        check("strict error payload lists the unresolved terms",
              any("unicorn" in str(t).lower()
                  for t in payload.get("unresolved_terms", [])))
        check("strict error drew nothing", len(_annotation_points()) == 0)
    finally:
        if ann_backup is None:
            if os.path.exists(ANN):
                os.remove(ANN)
        else:
            with open(ANN, "w") as fh:
                fh.write(ann_backup)


test_recommend_sites_impossible_species_draws_nothing()


def test_recommend_sites_terrain_hard_filter():
    # A generalized (non-species) hard filter: gentle ground only.
    threshold = 8.0
    res = tq.recommend_sites(objective="structure", count=5, min_separation_m=60.0,
                             draw=False,
                             hard_filters=[{"signal": "terrain.slope_deg",
                                            "op": "<=", "value": threshold}])
    check("slope hard filter is echoed in applied_filters",
          any(f.get("signal") == "terrain.slope_deg"
              for f in res.get("applied_filters", [])))
    check("slope-constrained recommendation returns candidates",
          res["returned_count"] > 0)
    # Independent re-check: every candidate's freshly sampled slope obeys the bound.
    slope_ok = True
    for c in res["candidates"]:
        s = tq._slope_deg(c["x"], c["y"])
        if s is None or s > threshold + 1e-6:
            slope_ok = False
            break
    check("every candidate independently satisfies the slope hard filter", slope_ok)


test_recommend_sites_terrain_hard_filter()


def test_recommend_sites_raster_class_hard_filter():
    # A land-cover (raster class) hard filter, and the impossible-class contract.
    res = tq.recommend_sites(objective="overlook", count=3, draw=False,
                             hard_filters=[{"signal": "raster_class",
                                            "layer_id": "nlcd_2019_landcover",
                                            "op": "in",
                                            "value": ["__no_such_class__"]}])
    check("an unsatisfiable raster class yields zero candidates",
          res["returned_count"] == 0)
    check("raster_class filter is recorded in applied_filters",
          any(f.get("signal") == "raster_class"
              for f in res.get("applied_filters", [])))


test_recommend_sites_raster_class_hard_filter()


def test_recommend_sites_constraint_error_contracts():
    lower = tq.recommend_sites(objective="trail camera for unicorn", count=3, draw=True)
    check("lowercase unresolved target is not silently discarded",
          lower["returned_count"] == 0
          and any("unicorn" in str(t).lower()
                  for t in lower.get("unresolved_terms", []))
          and lower["draw_count"] == 0)

    bad_signal = expect_error(
        "unsupported recommend_sites hard-filter signal is rejected",
        tq.recommend_sites, objective="overlook", draw=False,
        hard_filters=[{"signal": "distance_to_entity", "kind": "building",
                       "op": ">=", "value": 75}])
    check("unsupported signal error lists supported signals",
          "supported_signals" in bad_signal
          and "terrain.slope_deg" in bad_signal["supported_signals"])

    bad_raster = expect_error(
        "invalid raster_class layer is rejected",
        tq.recommend_sites, objective="overlook", draw=False,
        hard_filters=[{"signal": "raster_class", "layer_id": "bad_layer",
                       "op": "not_in", "value": ["Developed"]}])
    check("invalid raster error lists valid rasters",
          "valid_raster_layer_ids" in bad_raster
          and "nlcd_2019_landcover" in bad_raster["valid_raster_layer_ids"])

    pref_err = expect_error(
        "non-empty recommend_sites preferences are rejected until implemented",
        tq.recommend_sites, objective="overlook", draw=False,
        preferences=[{"signal": "terrain.slope_deg", "op": "<=", "value": 8}])
    check("preferences error is explicit",
          pref_err.get("error") == "recommend_sites_preferences_not_implemented")
    avoid_err = expect_error(
        "non-empty recommend_sites avoid constraints are rejected until implemented",
        tq.recommend_sites, objective="overlook", draw=False,
        avoid=[{"signal": "hydrology.ponding", "op": ">", "value": 0.2}])
    check("avoid error is explicit",
          avoid_err.get("error") == "recommend_sites_avoid_not_implemented")

    ann_backup = open(ANN).read() if os.path.exists(ANN) else None
    try:
        tq.clear_drawings()
        res = tq.recommend_sites(objective="trail camera for Gray Fox", count=3,
                                 min_separation_m=60.0, draw=True,
                                 label_prefix="fox-validate-off",
                                 validate=False)
        pts = _annotation_points()
        all_in_habitat = True
        for a in pts:
            names = (tq.identify_at({"x": a["x"], "y": a["y"]})
                     .get("species_habitat") or {}).get("common_names") or []
            if "Gray Fox" not in names:
                all_in_habitat = False
                break
        check("validate=False cannot bypass final hard-filter validation before draw",
              res["draw_count"] == res["returned_count"] == len(pts)
              and all_in_habitat)
    finally:
        if ann_backup is None:
            if os.path.exists(ANN):
                os.remove(ANN)
        else:
            with open(ANN, "w") as fh:
                fh.write(ann_backup)


test_recommend_sites_constraint_error_contracts()


def test_recommend_sites_legacy_signature_unchanged():
    # Backward compatibility: the original positional/keyword call still works
    # and still returns the original response keys.
    res = tq.recommend_sites(objective="overlook", count=2, min_separation_m=120.0,
                             draw=False)
    for key in ("objective", "region", "requested_count", "returned_count",
                "step_m", "provenance", "candidates", "draw_count"):
        check(f"legacy response still exposes {key!r}", key in res)
    check("legacy call exposes no unresolved terms", not res.get("unresolved_terms"))


test_recommend_sites_legacy_signature_unchanged()


print("== list_layers / layer_summary ==")
ll = tq.list_layers()
n_layers = conn.execute("SELECT COUNT(*) FROM layers").fetchone()[0]
check("catalog covers the layers table", ll["count"] == n_layers)
check("acquisition provenance present on ok layers", all(
    l["acquisition"] for l in ll["layers"] if l["status"] == "ok"))
vec = tq.list_layers(kind="vector")
check("kind filter works", 0 < vec["count"] < n_layers
      and all(l["kind"] == "vector" for l in vec["layers"]))
expect_error("unknown layer kind rejected", tq.list_layers, kind="nope")

ls = tq.layer_summary("gssurgo_soils")
check("vector summary has fields + labels",
      "soil_name" in ls["attribute_fields"] and ls["feature_count"] == 14)
lr = tq.layer_summary("landfire_evt_2024")
check("raster summary has classes with shares",
      lr["classes"] and abs(sum(c["share"] for c in lr["classes"]) - 1.0) < 0.01)
check("raster summary names classes",
      any("Forest" in c["name"] for c in lr["classes"]))
expect_error("unknown layer_id rejected", tq.layer_summary, "nope")


print("== summarize_region ==")
sm = tq.summarize_region(STAND_REGION)
check("tree count matches find_entities on the same polygon",
      sm["trees"]["count"] == stand["total_matched"],
      f"{sm['trees']['count']} != {stand['total_matched']}")
check("evergreen/deciduous split sums to count",
      sum(sm["trees"]["type_split"].values()) == sm["trees"]["count"])
check("dominant landfire community present",
      sm["landfire_community"]["dominant"]["name"])
check("soils present with provenance",
      sm["soils"]["features"] and sm["soils"]["provenance"]["acquisition"])
check("nlcd shares sum to ~1", abs(sum(
    c["share"] for c in sm["nlcd_landcover"]["classes"]) - 1.0) < 0.01)
check("region area reported", abs(sm["region"]["area_m2"] - reg.area_m2) < 1)
check("parcels covering region reported", len(sm["parcels"]) >= 1)
check("species richness range", sm["gap_species_richness"]["max"]
      >= sm["gap_species_richness"]["min"])
expect_error("summarize_region requires a region", tq.summarize_region, None)


print("== aggregate_entities ==")
ag = tq.aggregate_entities("tree", "count", group_by="type", region=STAND_REGION)
check("count by type matches summarize split",
      {k: v["value"] for k, v in ag["groups"].items()} == sm["trees"]["type_split"])
mh = tq.aggregate_entities("tree", "mean:height", region=STAND_REGION)
check("mean height matches summarize", abs(
    mh["groups"]["all"]["value"] - sm["trees"]["mean_height_m"]) < 0.01)
ca = tq.aggregate_entities("tree", "crown_area", region=STAND_REGION)
check("crown area matches summarize", abs(
    ca["groups"]["all"]["value"] - sm["trees"]["crown_area_m2"]) < 1)
wh = tq.aggregate_entities("tree", "count", where=["height > 20"],
                           region=STAND_REGION)
check("where filter reduces count",
      0 < wh["groups"]["all"]["value"] < sm["trees"]["count"])
check("aggregate carries source provenance",
      "sources" in ag["groups"]["evergreen"]["provenance"])
expect_error("bad metric rejected", tq.aggregate_entities, "tree", "median:height")


print("== canopy_change ==")
cc = tq.canopy_change()
check("one row per run with trees", len(cc["runs"]) >= 8)
check("rows in time order", [r["started_at"] for r in cc["runs"]]
      == sorted(r["started_at"] for r in cc["runs"]))
check("counts and crown area positive", all(
    r["tree_count"] > 0 and r["crown_area_m2"] > 0 for r in cc["runs"]))
check("deltas consistent", all(
    cc["runs"][i]["tree_delta"] == cc["runs"][i]["tree_count"] - cc["runs"][i - 1]["tree_count"]
    for i in range(1, len(cc["runs"]))))

# cross-check the unscoped query against the reference consumer's SQL
import canopy_density  # noqa: E402
ref = conn.execute(canopy_density.QUERY, {
    "member": "member_parcel", "minx": -1e9, "maxx": 1e9,
    "miny": -1e9, "maxy": 1e9}).fetchall()
check("matches scripts/canopy_density.py exactly",
      [(r["run_id"], r["tree_count"], r["crown_area_m2"]) for r in cc["runs"]]
      == [(r[0], r[3], r[4]) for r in ref])

ccr = tq.canopy_change(region=STAND_REGION)
check("region scopes canopy history", all(
    rr["tree_count"] <= ar["tree_count"]
    for rr, ar in zip(ccr["runs"], cc["runs"])))
# canopy_change counts the member_parcel population; compare like-for-like
# (the stand polygon also holds a few member_surrounding-only trees)
stand_parcel = tq.find_entities("tree", region=STAND_REGION,
                                attr_filters=["member_parcel = true"], limit=1)
check("region latest count matches find_entities (member_parcel)",
      ccr["runs"][-1]["tree_count"] == stand_parcel["total_matched"],
      f"{ccr['runs'][-1]['tree_count']} != {stand_parcel['total_matched']}")
expect_error("bad member rejected", tq.canopy_change, member="nope")

print("== map drawings ==")
# draw_* writes data/annotations.json (never the store); snapshot and
# restore whatever drawings the twin already has.
ann_backup = open(ANN).read() if os.path.exists(ANN) else None
try:
    tq.clear_drawings()
    pt = tq.draw_point({"x": 50, "y": 100}, label="anchor " + "x" * 200)
    check("draw_point echoes both coordinate forms",
          pt["drawn"]["position"]["x"] == 50 and "lat" in pt["drawn"]["position"]
          and "lon" in pt["drawn"]["position"])
    check("draw_point caps the label",
          len(pt["drawn"]["label"]) == twin_query.ANNOTATION_LABEL_MAX)
    pt2 = tq.draw_point({"lat": _lat, "lon": _lon})
    check("lat/lon draw_point lands on the same scene spot",
          math.hypot(pt2["drawn"]["position"]["x"] - 50,
                     pt2["drawn"]["position"]["y"] - 100) < 0.05)
    check("drawing ids are distinct and sequential",
          pt["drawn"]["id"] != pt2["drawn"]["id"]
          and pt2["annotations_total"] == 2)

    square = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
    pg = tq.draw_polygon(square, label="square")
    check("draw_polygon reports shoelace area",
          abs(pg["drawn"]["area_m2"] - 10000) < 1)
    check("draw_polygon stores the open ring",
          pg["drawn"]["vertex_count"] == 4 and pg["annotations_total"] == 3)
    geo_square = [list(G.to_lonlat(x, y)) for x, y in square]
    pg2 = tq.draw_polygon(geo_square)
    check("lon/lat polygon auto-detected to the same scene vertices",
          all(math.hypot(a[0] - b[0], a[1] - b[1]) < 0.05
              for a, b in zip(pg2["drawn"]["vertices_scene_m"], square)))

    with open(ANN) as fh:
        doc = json.load(fh)
    check("annotations file holds every drawing (scene-local meters)",
          len(doc["annotations"]) == 4
          and doc["annotations"][0]["type"] == "point"
          and doc["annotations"][0]["x"] == 50
          and doc["annotations"][2]["vertices"][1] == [100, 0])

    expect_error("two-vertex draw_polygon rejected",
                 tq.draw_polygon, [[0, 0], [1, 1]])
    expect_error("degenerate closed ring rejected",
                 tq.draw_polygon, [[0, 0], [1, 1], [0, 0]])
    expect_error("draw_point with half a pair rejected", tq.draw_point, {"x": 1})

    cleared = tq.clear_drawings()
    check("clear_drawings reports the count", cleared["cleared"] == 4)
    with open(ANN) as fh:
        check("cleared file is empty", json.load(fh)["annotations"] == [])
finally:
    if ann_backup is None:
        if os.path.exists(ANN):
            os.remove(ANN)
    else:
        with open(ANN, "w") as fh:
            fh.write(ann_backup)

print("== layer views ==")
# set_layer_visibility / filter_layer write layer_views into the same
# annotations.json (never the store); snapshot and restore it like the drawings.
lv_backup = open(ANN).read() if os.path.exists(ANN) else None
try:
    tq.clear_drawings()
    tq.reset_layer_views()
    # the atlas catalog (viewer-layers.json) is the drape source of truth — the
    # store's layers table can be empty on twins built before layer registration.
    drape = [l["id"] for l in tq._atlas_layers()
             if l.get("type") in twin_query.DRAPE_TYPES]
    if not drape:
        print("  skip  layer-view section (twin has no drape-able atlas layers)")
    else:
        lid = drape[0]
        vis = tq.set_layer_visibility(lid)
        check("set_layer_visibility records a visible directive",
              vis["visible"] is True and vis["layer"]["id"] == lid)
        with open(ANN) as fh:
            doc = json.load(fh)
        check("layer_views written to the directive file",
              any(v["layer_id"] == lid and v["visible"] for v in doc["layer_views"]))
        tq.set_layer_visibility(lid, visible=False)
        with open(ANN) as fh:
            doc = json.load(fh)
        same = [v for v in doc["layer_views"] if v["layer_id"] == lid]
        check("re-setting a layer replaces (not duplicates) its directive",
              len(same) == 1 and same[0]["visible"] is False)

        # a drawing and a layer view coexist in the one file
        tq.draw_point({"x": 50, "y": 100}, label="coexist")
        with open(ANN) as fh:
            doc = json.load(fh)
        check("a drawing and a layer view live in the same file",
              len(doc["annotations"]) == 1 and len(doc["layer_views"]) == 1)
        cleared = tq.clear_drawings()
        with open(ANN) as fh:
            doc = json.load(fh)
        check("clear_drawings leaves layer views intact",
              cleared["cleared"] == 1 and doc["annotations"] == []
              and len(doc["layer_views"]) == 1)

        # filter discovery + apply — scan for a layer with a filterable value,
        # using the same options the tool validates against (the default field
        # for the layer: legend class / species / __label).
        target = None
        for cand in drape:
            opts = tq._filter_options(tq._atlas_catalog()[cand])
            vals = opts["fields"].get(opts["field"]) or []
            if vals:
                target = (cand, vals[0])
                break
        if target is None:
            print("  skip  filter_layer positive assertions (no filterable values)")
        else:
            flid, sample = target
            res = tq.filter_layer(flid, [sample])
            check("filter_layer reveals only the matched value and forces visible",
                  res["matched_values"] == [sample]
                  and res["filter"]["values"] == [sample])
            with open(ANN) as fh:
                doc = json.load(fh)
            v = [d for d in doc["layer_views"] if d["layer_id"] == flid][0]
            check("filter directive carries the filter and is visible",
                  v["visible"] is True and v["filter"]["values"] == [sample])
            res2 = tq.filter_layer(flid, [sample.upper()])
            check("filter matching is case-insensitive",
                  res2["matched_values"] == [sample])
            expect_error("filter_layer rejects values that match nothing",
                         tq.filter_layer, flid, ["__definitely_not_a_class__"])

        # the headline GAP case: reveal one species' modeled habitat
        gap = (tq.layer_summary("gap_species_richness")
               if "gap_species_richness" in drape else {})
        spp = gap.get("filterable_species")
        if spp:
            sres = tq.filter_layer("gap_species_richness", [spp[0]])
            check("filter_layer on the GAP grid filters by species (habitat mask)",
                  sres["layer"]["filter_kind"] == "species"
                  and sres["filter"]["field"] == "species"
                  and sres["matched_values"] == [spp[0]])
        else:
            print("  skip  GAP species filter (twin has no per-species habitat grids)")

        expect_error("unknown layer_id rejected",
                     tq.set_layer_visibility, "__no_such_layer__")
        expect_error("filter_layer needs a non-empty value list",
                     tq.filter_layer, lid, [])

        reset = tq.reset_layer_views()
        with open(ANN) as fh:
            doc = json.load(fh)
        check("reset_layer_views clears every override",
              reset["cleared"] >= 1 and doc["layer_views"] == [])
finally:
    if lv_backup is None:
        if os.path.exists(ANN):
            os.remove(ANN)
    else:
        with open(ANN, "w") as fh:
            fh.write(lv_backup)

print("== survey companion ==")
sv = tq.list_survey_layers()
check("list_survey_layers returns a catalog shape",
      isinstance(sv.get("layers"), list) and sv["count"] == len(sv["layers"]))
if sv["count"] == 0:
    check("empty survey catalog carries a note", bool(sv.get("note")))
else:
    check("survey layers expose kind + geometry_type + fields", all(
        l.get("kind", "").startswith("survey_") and l.get("geometry_type")
        and isinstance(l.get("fields"), list) for l in sv["layers"]))
ident = tq.identify_at({"x": 50, "y": 100})
check("identify_at now carries a survey block (list)",
      isinstance(ident.get("survey"), list))

print("== hydrology ==")
hs = tq.hydrology_summary()
check("hydrology_summary has the drainage outlet",
      "x" in hs["summary"]["outlet"] and "contributing_ha" in hs["summary"]["outlet"])
check("hydrology_summary lists seep candidates with lat/lon", all(
    "lat" in c and "lon" in c and "score" in c
    for c in hs["summary"]["seep_candidates"]))
check("hydrology provenance names a run + the discharge caveat",
      hs["provenance"].get("run_id") is not None
      and "scenario-grade" in hs["provenance"]["caveat"])

# the analysis outlet is on the terrain by construction -> always has values
outlet = hs["summary"]["outlet"]
ha = tq.hydrology_at({"x": outlet["x"], "y": outlet["y"]})
check("hydrology_at samples the derived layers at the outlet",
      isinstance(ha["layers"].get("flow_paths"), dict)
      and ha["layers"]["flow_paths"]["value"] is not None)
check("hydrology_at echoes both coordinate forms",
      "lat" in ha["point"] and "x" in ha["point"])
check("hydrology_at synthesizes a plain-language reading",
      isinstance(ha["summary"], list) and len(ha["summary"]) >= 1)
far = tq.hydrology_at({"x": 5000, "y": 5000})
check("hydrology_at outside the footprint says so",
      far["summary"] and "outside" in far["summary"][0].lower())

# run_scenario: exercise the clamp/argv path WITHOUT executing (a real run
# mutates the store); the clamps must match server.js /api/simulate.
rain = tq.run_scenario(mode="rain", rain_in=99, storm_hours=6,
                       antecedent="wet", frozen=True, dry_run=True)["would_run"]
check("run_scenario clamps rain_in to 15 and keeps storm_hours",
      "--rain-in" in rain and rain[rain.index("--rain-in") + 1] == "15"
      and rain[rain.index("--storm-hours") + 1] == "6.0")
check("run_scenario rain mode carries the flags", "rain" in rain
      and "--antecedent" in rain and "--frozen" in rain)
melt = tq.run_scenario(mode="banana", preset="p90", melt_days=100,
                       dry_run=True)["would_run"]
check("run_scenario coerces unknown mode to snowmelt and clamps melt_days",
      "snowmelt" in melt and melt[melt.index("--melt-days") + 1] == "30"
      and melt[melt.index("--preset") + 1] == "p90")

conn.close()
print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
sys.exit(1 if FAIL else 0)
