#!/usr/bin/env python3
"""Ingest QField survey uploads into the twin store — the store-side half of
the Survey companion (docs/survey.md).

The viewer's Survey panel POSTs a zipped QField project folder to
/api/survey-upload; server.js saves it under data/surveys/incoming/, appends
one line to data/surveys/uploads.log.jsonl ({ts, file, name, bytes} — the
Node -> Python handoff, like the placements log), and spawns this script with
--pending. A meta cursor (lines already ingested) makes re-runs idempotent,
and the exporter also calls ingest_pending() so an upload that arrived while
the Python side was sick is picked up by the next `npm run export`.

Semantics (all settled in docs/survey.md):
  * Identity is the natural key: survey_<layer>:<uuid> from the hidden uuid
    field the package bakes into every form. Never positional.
  * A feature missing from an upload means nothing — partial sync is the
    field norm; nothing is auto-retired. Retirement is the explicit status
    field (retired/removed), and an already-retired, unchanged feature is
    skipped before any write so re-uploading the same gpkg journals no
    entity/observation/feature/retire ops.
  * observed_at: captured_at (normalized to UTC, assuming device-local time
    — QField's now() default writes local time) for a feature's first
    ingest; changes discovered on later uploads get the upload time, since
    captured_at doesn't update on edit.
  * GPS accuracy is its own accuracy_m observation; confidence stays null
    (the store's confidence column is 0-1 quality, not meters).
  * Geometry: scene-local row in the survey_<layer> spatial table
    (upsert_feature — the entity keeps its identity across re-walks), plus,
    whenever it changes, a geometry observation holding the scene-local
    GeoJSON so prior geometry is a plain history() query.
  * Photos: copied to data/surveys/photos/<sha1[:12]>-<name>, observed as
    {path, bytes, sha1, captured_at} (the building model_file pattern).

Run:
  python3 scripts/ingest_survey.py UPLOAD.zip [--name LABEL] [--json]
  python3 scripts/ingest_survey.py --pending [--json]    (server/exporter path)
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

from osgeo import ogr, osr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_georef
import twin_store

ogr.UseExceptions()
osr.UseExceptions()

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Must match build_survey_package.SURVEY_LAYERS (the package defines the
# schema; this side only consumes it).
SURVEY_LAYERS = {
    "trails": "LINESTRING",
    "stream_centerlines": "LINESTRING",
    "photo_points": "POINT",
    "observations": "POINT",
}
PLAIN_ATTRS = ("name", "status", "notes", "accuracy_m")
CURSOR_KEY = "surveys_log_lines_ingested"
# Decompression bounds for an uploaded survey zip (defense against zip bombs;
# the HTTP layer separately caps the compressed body size).
SURVEY_ZIP_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB uncompressed
SURVEY_ZIP_MAX_MEMBERS = 10000


def _data_dir(arg=None):
    return os.path.abspath(arg or os.environ.get("TWIN_DATA_DIR")
                           or os.path.join(PROJECT, "data"))


def _srs(crs):
    s = osr.SpatialReference()
    s.SetFromUserInput(crs)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def to_utc(value, fallback):
    """captured_at (QField writes device-local naive datetimes) -> the store's
    UTC string format. Naive times are assumed device-local = this machine's
    timezone (documented in docs/survey.md)."""
    if not value:
        return fallback
    text = str(value).strip().replace("/", "-")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return fallback
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach local tz
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift(coords, ox, oy):
    if coords and isinstance(coords[0], (int, float)):
        return [round(coords[0] - ox, 3), round(coords[1] - oy, 3)]
    return [_shift(c, ox, oy) for c in coords]


def _find_member(zf_names, suffix):
    hits = [n for n in zf_names if n.lower().endswith(suffix)
            and ".." not in n.split("/")]
    return hits[0] if hits else None


def ingest_zip(store, zip_path, label=None, data_dir=None):
    """Ingest one uploaded zip. Returns the per-layer summary dict."""
    data_dir = _data_dir(data_dir)
    georef = os.path.join(data_dir, "georef.json")
    working = twin_georef.crs(georef)
    ox, oy = twin_georef.origin(georef)
    upload_ts = twin_store.utcnow()
    summary = {"zip": os.path.basename(zip_path), "layers": {}, "photos": 0,
               "warnings": []}

    with tempfile.TemporaryDirectory(prefix="survey-ingest-") as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            names = zf.namelist()
            gpkg_name = _find_member(names, "survey.gpkg") or _find_member(names, ".gpkg")
            if not gpkg_name:
                raise SystemExit(f"no .gpkg inside {zip_path}")
            # Bound decompression so a zip bomb can't exhaust the disk (the HTTP
            # layer caps the compressed upload; this caps what it expands to).
            if len(infos) > SURVEY_ZIP_MAX_MEMBERS:
                raise SystemExit(f"survey zip has too many members "
                                 f"({len(infos)} > {SURVEY_ZIP_MAX_MEMBERS})")
            total = sum(max(0, zi.file_size) for zi in infos)
            if total > SURVEY_ZIP_MAX_TOTAL_BYTES:
                raise SystemExit(f"survey zip expands to {total} bytes "
                                 f"(> {SURVEY_ZIP_MAX_TOTAL_BYTES} cap)")
            extracted = 0
            for zi in infos:
                if ".." in zi.filename.split("/"):
                    continue
                out_path = zf.extract(zi, tmp)
                if os.path.isfile(out_path):
                    extracted += os.path.getsize(out_path)
                if extracted > SURVEY_ZIP_MAX_TOTAL_BYTES:
                    raise SystemExit("survey zip exceeded decompression cap during extract")
        gpkg_path = os.path.join(tmp, gpkg_name)
        project_root = os.path.dirname(gpkg_path)

        run = store.begin_run("ingest_survey.py", inputs=[zip_path],
                              notes=label or os.path.basename(zip_path))
        zip_sha1 = twin_store.sha1_file(zip_path)
        ds = ogr.Open(gpkg_path)
        for layer_name, geom_type in SURVEY_LAYERS.items():
            layer = ds.GetLayerByName(layer_name)
            if layer is None:
                continue
            table = f"survey_{layer_name}"
            kind = table
            store.ensure_spatial_layer(table, geom_type,
                                       "entity_id TEXT UNIQUE, properties TEXT")
            ct = osr.CoordinateTransformation(
                (layer.GetSpatialRef() or _srs(working)).Clone(), _srs(working))
            latest = store.latest_attrs(kind)
            counts = {"seen": 0, "created": 0, "updated": 0, "moved": 0,
                      "retired": 0, "skipped_retired": 0, "unchanged": 0}

            for feat in layer:
                uuid = feat.GetField("uuid") if feat.GetFieldIndex("uuid") >= 0 else None
                geom = feat.GetGeometryRef()
                if not uuid or geom is None:
                    summary["warnings"].append(
                        f"{layer_name}: feature without uuid/geometry skipped")
                    continue
                counts["seen"] += 1
                eid = f"{kind}:{uuid}"

                # ---- gather the incoming state (no writes yet)
                attrs = {}
                for a in PLAIN_ATTRS:
                    idx = feat.GetFieldIndex(a)
                    v = feat.GetField(idx) if idx >= 0 and feat.IsFieldSet(idx) else None
                    if v is not None:
                        attrs[a] = v
                status = attrs.get("status") or "active"
                attrs["status"] = status
                g = geom.Clone()
                g.Transform(ct)
                gj = json.loads(g.ExportToJson())
                gj["coordinates"] = _shift(gj["coordinates"], ox, oy)
                wkb = ogr.CreateGeometryFromJson(json.dumps(gj)).ExportToWkb(ogr.wkbNDR)
                geom_sha1 = hashlib.sha1(wkb).hexdigest()
                captured_utc = to_utc(
                    feat.GetField("captured_at")
                    if feat.GetFieldIndex("captured_at") >= 0 else None, upload_ts)
                photo_obs = None
                pidx = feat.GetFieldIndex("photo")
                if pidx >= 0 and feat.IsFieldSet(pidx) and feat.GetField(pidx):
                    # content-addressed copy is idempotent, so registering
                    # during the diff phase is safe even for skipped features
                    photo_obs = _register_photo(project_root, feat.GetField(pidx),
                                                data_dir, captured_utc, summary)

                # ---- diff before any write (the zero-op invariant)
                prev = latest.get(eid, {})
                enc = twin_store.encode_value
                changed = (
                    any(enc(attrs[a]) != enc(prev.get(a)) for a in attrs)
                    or prev.get("geom_sha1") != geom_sha1
                    or (photo_obs is not None
                        and photo_obs["sha1"] != (prev.get("photo") or {}).get("sha1"))
                )
                state = store.entity_state(eid)
                if (status in ("retired", "removed") and state
                        and state["retired"] and not changed):
                    counts["skipped_retired"] += 1
                    continue
                if state is not None and not changed and not state["retired"]:
                    counts["unchanged"] += 1
                    continue

                # ---- write: first ingest gets the field capture time; later
                # changes get the upload time (captured_at doesn't update on edit)
                at = captured_utc if state is None else upload_ts
                if state is None:
                    store.upsert_entity(eid, kind, run, observed_at=at)
                    counts["created"] += 1
                else:
                    counts["updated"] += 1
                    if state["retired"] and status == "active":
                        store.upsert_entity(eid, kind, run)  # un-retire: it's back
                for a, v in attrs.items():
                    store.observe(eid, a, v, run, source="qfield", observed_at=at)
                store.observe(eid, "member_survey", True, run, source="qfield",
                              observed_at=at)
                if prev.get("geom_sha1") != geom_sha1:
                    store.observe(eid, "geom_sha1", geom_sha1, run,
                                  source="qfield", observed_at=at)
                    store.observe(eid, "geometry", gj, run, source="qfield",
                                  observed_at=at)
                    if state is not None:
                        counts["moved"] += 1
                store.upsert_feature(table, eid, wkb)
                if photo_obs and store.observe(eid, "photo", photo_obs, run,
                                               source="qfield", observed_at=at):
                    summary["photos"] += 1
                if status in ("retired", "removed") and not (state and state["retired"]):
                    store.retire_entity(eid, run)
                    counts["retired"] += 1

            if not counts["seen"]:
                summary["layers"][layer_name] = counts
                continue  # nothing surveyed on this layer; leave its row alone
            store.upsert_layer(table, label=layer_name.replace("_", " ").title()
                               + " (survey)", kind=geom_type.lower(),
                               acquisition="qfield_survey",
                               source_path=os.path.abspath(zip_path),
                               fetched_at=upload_ts,
                               feature_count=counts["seen"], status="ok",
                               content_sha1=zip_sha1)
            summary["layers"][layer_name] = counts
        ds = None
        store.finish_run(run, notes=json.dumps(
            {k: v for k, v in summary["layers"].items()}, sort_keys=True))
    return summary


def _register_photo(project_root, rel_path, data_dir, captured_utc, summary):
    """Copy a referenced photo out of the project folder into
    data/surveys/photos/ (sha1-prefixed) and return the observation value."""
    src = os.path.normpath(os.path.join(project_root, rel_path))
    if not src.startswith(os.path.abspath(project_root)) or not os.path.exists(src):
        summary["warnings"].append(f"photo not in upload: {rel_path}")
        return None
    sha1 = twin_store.sha1_file(src)
    photos_dir = os.path.join(data_dir, "surveys", "photos")
    os.makedirs(photos_dir, exist_ok=True)
    fname = f"{sha1[:12]}-{os.path.basename(src)}"
    dst = os.path.join(photos_dir, fname)
    if not os.path.exists(dst):
        shutil.copyfile(src, dst)
    return {"path": f"surveys/photos/{fname}", "bytes": os.path.getsize(dst),
            "sha1": sha1, "captured_at": captured_utc}


def ingest_pending(store, data_dir=None):
    """Process unread uploads.log.jsonl lines (cursor in meta) — the server's
    synchronous attempt and the exporter's deferred fallback share this."""
    data_dir = _data_dir(data_dir)
    log_path = os.path.join(data_dir, "surveys", "uploads.log.jsonl")
    if not os.path.exists(log_path):
        return []
    with open(log_path) as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    done = store.get_meta(CURSOR_KEY, 0)
    results = []
    for line in lines[done:]:
        try:
            rec = json.loads(line)
            zip_path = os.path.join(data_dir, "surveys", "incoming", rec["file"])
            if os.path.exists(zip_path):
                results.append(ingest_zip(store, zip_path, rec.get("name"),
                                          data_dir))
            else:
                results.append({"zip": rec.get("file"), "error": "file missing"})
        except (ValueError, KeyError) as e:
            results.append({"error": f"bad log line: {e}"})
        # malformed/missing entries still advance the cursor
    if len(lines) != done:
        store.set_meta(CURSOR_KEY, len(lines))
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("zip", nargs="?", help="one uploaded zip (manual use; the"
                    " cursor is not advanced — idempotency makes re-runs safe)")
    ap.add_argument("--pending", action="store_true",
                    help="ingest unread uploads.log.jsonl lines (cursor mode)")
    ap.add_argument("--name", help="outing label for the run notes")
    ap.add_argument("--json", action="store_true",
                    help="print a JSON summary as the last stdout line")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    if not args.zip and not args.pending:
        ap.error("give an upload zip or --pending")

    data_dir = _data_dir(args.data_dir)
    store = twin_store.Store(os.path.join(data_dir, "twin.gpkg"))
    try:
        if args.pending:
            out = ingest_pending(store, data_dir)
        else:
            out = [ingest_zip(store, os.path.abspath(args.zip), args.name,
                              data_dir)]
    finally:
        store.close()

    # refresh the viewer payloads so the new layer shows without an export run
    try:
        import export_viewer_payloads
        export_viewer_payloads.export_surveys(data_dir)
    except Exception as e:  # noqa: BLE001 — ingest succeeded; export can rerun
        print(f"(survey export skipped: {e})", file=sys.stderr)

    if args.json:
        print(json.dumps({"ok": True, "results": out}, sort_keys=True))
    else:
        for r in out:
            print(json.dumps(r, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
