#!/usr/bin/env python3
"""The twin store: a single GeoPackage at data/twin.gpkg.

GeoPackage is SQLite, so one file carries both the spatial layers (one per
entity kind, written as standard gpkg feature tables) and the plain relational
tables (meta, pipeline_runs, entities, observations, layers). All build
scripts go through this module; nothing else opens the gpkg directly.

Durability: the gpkg itself is a gitignored materialized index. The canonical
history is the write journal in data/journal/ (committed): every Store write
session flushes its ops to one append-only NNNNNN-<script>.jsonl.gz file on
close, and scripts/rebuild_store.py reconstructs the gpkg — same runs, same
timestamps, same observation order — by replaying those files. If a process
dies mid-session its journal file is never written, so on crash the gpkg can
be ahead of the journal; `rebuild_store.py` restores the journaled truth.

Conventions (do not introduce a second one):
  * Coordinates are scene-local meters (x = east, y = north), i.e. EPSG:26918
    minus origin_utm from data/georef.json. origin_utm is recorded once in the
    meta table. Spatial layers use the GeoPackage "Undefined Cartesian SRS"
    (srs_id -1).
  * Entity IDs are deterministic, not autoincrement:
        entity_id = "<kind>:" + sha1(f"{source}|{round(x,1)}|{round(y,1)}")[:12]
    computed from the rounded coordinates that get persisted (3 decimals).
    Named entities (building models) use their natural key instead.
    On the rare hash collision (distinct stems whose coordinates agree to
    0.1 m) a deterministic "-2"/"-3" suffix is appended; resolution probes by
    position so IDs stay stable across rebuilds.
  * Observations are append-only. Re-observing an unchanged value is a no-op;
    entities that vanish are retired (retired_run_id/retired_at), never
    deleted.
"""

import glob
import gzip
import hashlib
import json
import os
import re
import sqlite3
import struct
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DATA_DIR = os.path.abspath(os.environ.get('TWIN_DATA_DIR') or os.path.join(PROJECT, 'data'))
STORE_PATH = os.path.join(DATA_DIR, "twin.gpkg")
JOURNAL_DIR = os.path.join(DATA_DIR, "journal")

SCHEMA_VERSION = 2
SRS_ID = -1  # GeoPackage predefined "Undefined Cartesian SRS": scene-local meters

# Canonical attribute order for vegetation instances (read API + exports).
TREE_ATTRS = ("z", "height", "radius", "type", "community", "species",
              "source", "confidence")
SHRUB_ATTRS = ("baseScale", "height", "z")

# Spatial layers created at schema time. Atlas vector layers are added
# dynamically as atlas_<layer_id> via ensure_spatial_layer().
BASE_SPATIAL_LAYERS = {
    "trees": ("POINT", "entity_id TEXT UNIQUE, source TEXT, x REAL, y REAL"),
    "shrubs": ("POINT", "entity_id TEXT UNIQUE, source TEXT, x REAL, y REAL"),
    "building_footprints": ("POLYGON", "entity_id TEXT UNIQUE, properties TEXT"),
    "parcels": ("POLYGON", "entity_id TEXT UNIQUE, properties TEXT"),
    "streams": ("LINESTRING", "entity_id TEXT UNIQUE, properties TEXT"),
    "roads": ("LINESTRING", "entity_id TEXT UNIQUE, properties TEXT"),
}

PLAIN_TABLES = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    script TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    inputs_hash TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    retired_run_id INTEGER,
    retired_at TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL,
    attr TEXT NOT NULL,
    value TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    source TEXT,
    confidence REAL
);
CREATE TABLE IF NOT EXISTS layers (
    layer_id TEXT PRIMARY KEY,
    label TEXT,
    kind TEXT,
    acquisition TEXT,
    service TEXT,
    source_path TEXT,
    fetched_at TEXT,
    feature_count INTEGER,
    status TEXT,
    content_sha1 TEXT
);
CREATE TABLE IF NOT EXISTS plan_bases (
    base_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    head_revision_id TEXT,
    forked_from_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_revisions (
    revision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    parent_revision_id TEXT,
    base_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    message TEXT,
    checkpoint_name TEXT,
    author TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_edits (
    revision_id TEXT NOT NULL,
    edit_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    geometry TEXT,
    params TEXT NOT NULL,
    label TEXT,
    PRIMARY KEY (revision_id, edit_id)
);
CREATE TABLE IF NOT EXISTS plan_simulation_runs (
    plan_run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    simulator TEXT NOT NULL,
    status TEXT NOT NULL,
    parameters TEXT NOT NULL,
    result TEXT,
    artifact_path TEXT,
    input_hash TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_entity_attr ON observations(entity_id, attr, obs_id);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan ON plan_revisions(plan_id, created_at);
CREATE INDEX IF NOT EXISTS idx_plan_edits_revision ON plan_edits(revision_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_plan_runs_revision ON plan_simulation_runs(revision_id, created_at);
"""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(value):
    """5 and 5.0 are the same observation; JSON would encode them differently."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def encode_value(value):
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"))


def decode_value(text):
    return None if text is None else json.loads(text)


def entity_id(kind, source, x, y):
    """The deterministic ID rule. x/y must be the persisted (rounded) coords."""
    digest = hashlib.sha1(f"{source}|{round(x, 1)}|{round(y, 1)}".encode()).hexdigest()
    return f"{kind}:{digest[:12]}"


def hash_inputs(paths):
    h = hashlib.sha1()
    for p in sorted(paths):
        h.update(os.path.basename(p).encode())
        try:
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gpkg_blob(wkb, srs_id=SRS_ID):
    """GeoPackage geometry: 'GP' magic, version 0, little-endian/no-envelope
    flags, srs_id, then standard WKB."""
    return b"GP\x00\x01" + struct.pack("<i", srs_id) + wkb


def gpkg_point_blob(x, y, srs_id=SRS_ID):
    return gpkg_blob(struct.pack("<BIdd", 1, 1, float(x), float(y)), srs_id)


def gpkg_blob_wkb(blob):
    """Standard WKB from a GeoPackage geometry blob (header is 8 bytes plus
    an optional envelope, sized by the flags byte's envelope indicator)."""
    envelope_doubles = {0: 0, 1: 4, 2: 6, 3: 6, 4: 8}[(blob[3] >> 1) & 0x7]
    return bytes(blob[8 + envelope_doubles * 8:])


def open_store(path=STORE_PATH):
    return Store(path)


class Store:
    def __init__(self, path=STORE_PATH, journal=True):
        self.path = path
        self._journal = journal
        self._ops = []
        self._script = None
        self._create_gpkg_container_if_missing()
        self.conn = sqlite3.connect(path)
        self.ensure_schema()

    def _log(self, **op):
        if self._journal:
            self._ops.append(op)

    def _flush_journal(self):
        if not (self._journal and self._ops):
            return None
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        seqs = [int(m.group(1)) for f in glob.glob(os.path.join(JOURNAL_DIR, "*.jsonl.gz"))
                if (m := re.match(r"(\d+)-", os.path.basename(f)))]
        seq = max(seqs, default=0) + 1
        slug = re.sub(r"[^a-z0-9]+", "-", (self._script or "session").lower()).strip("-")
        path = os.path.join(JOURNAL_DIR, f"{seq:06d}-{slug}.jsonl.gz")
        with gzip.open(path, "wt") as fh:
            for op in self._ops:
                fh.write(json.dumps(op, separators=(",", ":")) + "\n")
        self._ops = []
        return path

    # ---------------------------------------------------------------- schema

    def _create_gpkg_container_if_missing(self):
        """Create a valid empty GeoPackage (gpkg_* metadata tables) via OGR.
        Done once; afterwards everything goes through sqlite3."""
        if os.path.exists(self.path):
            return
        from osgeo import gdal, ogr

        gdal.UseExceptions()
        ogr.UseExceptions()
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(self.path)
        if ds is None:
            raise RuntimeError(f"could not create GeoPackage at {self.path}")
        ds = None

    def ensure_schema(self):
        cur = self.conn.cursor()
        cur.executescript(PLAIN_TABLES)
        # Schema metadata describes the materialized index, not a historical
        # observation.  Upgrade it in place whenever newer code opens an older
        # store; the journal replay path applies the same floor below.
        cur.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (encode_value(SCHEMA_VERSION),),
        )
        # GDAL writes srs_id -1/0/4326 into gpkg_spatial_ref_sys by default,
        # but make sure -1 (Undefined Cartesian) exists before we point layers at it.
        cur.execute(
            "INSERT OR IGNORE INTO gpkg_spatial_ref_sys"
            " (srs_name, srs_id, organization, organization_coordsys_id, definition, description)"
            " VALUES ('Undefined Cartesian SRS', -1, 'NONE', -1, 'undefined',"
            " 'undefined cartesian coordinate reference system')"
        )
        for name, (geom_type, columns) in BASE_SPATIAL_LAYERS.items():
            self._create_spatial_table(cur, name, geom_type, columns)
        self.conn.commit()

    def _create_spatial_table(self, cur, name, geom_type, columns):
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {name}"
            f" (fid INTEGER PRIMARY KEY AUTOINCREMENT, {columns}, geom BLOB)"
        )
        cur.execute(
            "INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, srs_id)"
            " VALUES (?, 'features', ?, ?)",
            (name, name, SRS_ID),
        )
        cur.execute(
            "INSERT OR IGNORE INTO gpkg_geometry_columns"
            " (table_name, column_name, geometry_type_name, srs_id, z, m)"
            " VALUES (?, 'geom', ?, ?, 0, 0)",
            (name, geom_type, SRS_ID),
        )

    def ensure_spatial_layer(self, name, geom_type="GEOMETRY",
                             columns="properties TEXT"):
        if self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone():
            return  # already there; don't journal a no-op
        cur = self.conn.cursor()
        self._create_spatial_table(cur, name, geom_type, columns)
        self.conn.commit()
        self._log(op="spatial_layer", name=name, geom_type=geom_type,
                  columns=columns)

    def close(self):
        self.conn.commit()
        journal = self._flush_journal()
        self.conn.close()
        if journal:
            print(f"journal: {os.path.relpath(journal, DATA_DIR)}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ meta

    def set_meta(self, key, value):
        encoded = encode_value(value)
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is not None and row[0] == encoded:
            return
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, encoded),
        )
        self.conn.commit()
        self._log(op="meta", key=key, value=encoded)

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else decode_value(row[0])

    # ------------------------------------------------------------------ runs

    def begin_run(self, script, inputs=None, notes=None):
        inputs_hash = hash_inputs(inputs) if inputs else None
        started = utcnow()
        cur = self.conn.execute(
            "INSERT INTO pipeline_runs (script, started_at, inputs_hash, notes)"
            " VALUES (?, ?, ?, ?)",
            (script, started, inputs_hash, notes),
        )
        self.conn.commit()
        self._script = self._script or script
        self._log(op="run", run_id=cur.lastrowid, script=script,
                  started_at=started, inputs_hash=inputs_hash, notes=notes)
        return cur.lastrowid

    def finish_run(self, run_id, notes=None):
        finished = utcnow()
        if notes is not None:
            self.conn.execute(
                "UPDATE pipeline_runs SET finished_at = ?, notes = ? WHERE run_id = ?",
                (finished, notes, run_id),
            )
        else:
            self.conn.execute(
                "UPDATE pipeline_runs SET finished_at = ? WHERE run_id = ?",
                (finished, run_id),
            )
        self.conn.commit()
        self._log(op="finish_run", run_id=run_id, finished_at=finished, notes=notes)

    # -------------------------------------------------------------- entities

    def upsert_entity(self, eid, kind, run_id, observed_at=None):
        """Create the entity if new; un-retire it if it reappeared.
        Returns True when the entity was created."""
        now = observed_at or utcnow()
        row = self.conn.execute(
            "SELECT retired_run_id FROM entities WHERE entity_id = ?", (eid,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO entities (entity_id, kind, created_run_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (eid, kind, run_id, now),
            )
            self._log(op="entity", entity_id=eid, kind=kind, run_id=run_id,
                      created_at=now)
            return True
        if row[0] is not None:
            self.conn.execute(
                "UPDATE entities SET retired_run_id = NULL, retired_at = NULL"
                " WHERE entity_id = ?",
                (eid,),
            )
            self._log(op="unretire", entity_id=eid)
        return False

    def retire_entity(self, eid, run_id):
        now = utcnow()
        cur = self.conn.execute(
            "UPDATE entities SET retired_run_id = ?, retired_at = ?"
            " WHERE entity_id = ? AND retired_run_id IS NULL",
            (run_id, now, eid),
        )
        if cur.rowcount:
            self._log(op="retire", entity_id=eid, run_id=run_id, retired_at=now)

    def entity_state(self, eid):
        """None if the entity doesn't exist, else {"retired": bool} — lets
        ingest scripts diff before writing (no unretire/retire churn)."""
        row = self.conn.execute(
            "SELECT retired_run_id FROM entities WHERE entity_id = ?", (eid,)
        ).fetchone()
        return None if row is None else {"retired": row[0] is not None}

    def alive_entities(self, kind):
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT entity_id FROM entities WHERE kind = ? AND retired_run_id IS NULL"
                " ORDER BY entity_id",
                (kind,),
            )
        ]

    # ---------------------------------------------------------- observations

    def observe(self, eid, attr, value, run_id, source=None, confidence=None,
                observed_at=None, dedup=True):
        """Append an observation. With dedup (default), skip when the latest
        observation for (entity, attr) already holds the same value.
        Returns True when a row was written."""
        encoded = encode_value(value)
        if dedup:
            row = self.conn.execute(
                "SELECT value FROM observations WHERE entity_id = ? AND attr = ?"
                " ORDER BY obs_id DESC LIMIT 1",
                (eid, attr),
            ).fetchone()
            if row is not None and row[0] == encoded:
                return False
        at = observed_at or utcnow()
        self.conn.execute(
            "INSERT INTO observations"
            " (entity_id, attr, value, observed_at, run_id, source, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, attr, encoded, at, run_id, source, confidence),
        )
        self._log(op="obs", entity_id=eid, attr=attr, value=encoded, at=at,
                  run_id=run_id, source=source, confidence=confidence)
        return True

    def latest(self, eid, attr):
        row = self.conn.execute(
            "SELECT value FROM observations WHERE entity_id = ? AND attr = ?"
            " ORDER BY obs_id DESC LIMIT 1",
            (eid, attr),
        ).fetchone()
        return None if row is None else decode_value(row[0])

    def history(self, eid, attr=None):
        sql = (
            "SELECT obs_id, attr, value, observed_at, run_id, source, confidence"
            " FROM observations WHERE entity_id = ?"
        )
        params = [eid]
        if attr is not None:
            sql += " AND attr = ?"
            params.append(attr)
        sql += " ORDER BY obs_id"
        return [
            {
                "obs_id": r[0], "attr": r[1], "value": decode_value(r[2]),
                "observed_at": r[3], "run_id": r[4], "source": r[5],
                "confidence": r[6],
            }
            for r in self.conn.execute(sql, params)
        ]

    def _latest_encoded_map(self, kind=None):
        """{(entity_id, attr): encoded_value} for the latest observation per
        pair. Single ordered scan; later rows overwrite earlier ones."""
        if kind is None:
            rows = self.conn.execute(
                "SELECT entity_id, attr, value FROM observations ORDER BY obs_id"
            )
        else:
            rows = self.conn.execute(
                "SELECT o.entity_id, o.attr, o.value FROM observations o"
                " JOIN entities e ON e.entity_id = o.entity_id"
                " WHERE e.kind = ? ORDER BY o.obs_id",
                (kind,),
            )
        return {(eid, attr): value for eid, attr, value in rows}

    def latest_attrs(self, kind):
        """{entity_id: {attr: decoded_value}} for all entities of a kind
        (retired included; filter with alive_entities/membership as needed)."""
        out = {}
        for (eid, attr), value in self._latest_encoded_map(kind).items():
            out.setdefault(eid, {})[attr] = decode_value(value)
        return out

    def member_ids(self, kind, member_attr):
        """Entity IDs of a kind whose latest member_attr observation is true."""
        latest = self._latest_encoded_map(kind)
        return {eid for (eid, attr), v in latest.items()
                if attr == member_attr and v == "true"}

    # ----------------------------------------------------------------- points

    def points(self, layer):
        """{entity_id: (x, y, source)} from a point layer."""
        return {
            r[0]: (r[1], r[2], r[3])
            for r in self.conn.execute(f"SELECT entity_id, x, y, source FROM {layer}")
        }

    def insert_feature(self, layer, eid, wkb, properties=None):
        """Insert a non-point feature (or atlas feature when eid is None)."""
        if eid is None:
            self.conn.execute(
                f"INSERT INTO {layer} (properties, geom) VALUES (?, ?)",
                (encode_value(properties), gpkg_blob(wkb)),
            )
        else:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO {layer} (entity_id, properties, geom) VALUES (?, ?, ?)",
                (eid, encode_value(properties), gpkg_blob(wkb)),
            )
            if not cur.rowcount:
                return
        self._log(op="feature", layer=layer, entity_id=eid,
                  wkb=bytes(wkb).hex(), properties=properties)

    def upsert_feature(self, layer, eid, wkb, properties=None):
        """Insert or replace an entity-keyed feature: the entity keeps its
        identity when its geometry/properties change (survey re-walks). The
        layer table must declare entity_id UNIQUE. Unchanged rows are no-ops.
        Returns True when a row was written."""
        encoded = encode_value(properties)
        blob = gpkg_blob(wkb)
        row = self.conn.execute(
            f"SELECT properties, geom FROM {layer} WHERE entity_id = ?", (eid,)
        ).fetchone()
        if row is not None and row[0] == encoded and bytes(row[1]) == blob:
            return False
        self.conn.execute(
            f"INSERT INTO {layer} (entity_id, properties, geom) VALUES (?, ?, ?)"
            " ON CONFLICT(entity_id) DO UPDATE SET"
            " properties = excluded.properties, geom = excluded.geom",
            (eid, encoded, blob),
        )
        self._log(op="feature_upsert", layer=layer, entity_id=eid,
                  wkb=bytes(wkb).hex(), properties=properties)
        return True

    def features(self, layer):
        """{entity_id: (wkb, properties)} for an entity-keyed spatial layer —
        the read path for non-point survey geometry (exports, queries)."""
        return {
            r[0]: (gpkg_blob_wkb(r[1]), decode_value(r[2]))
            for r in self.conn.execute(
                f"SELECT entity_id, geom, properties FROM {layer}"
                " WHERE entity_id IS NOT NULL"
            )
        }

    # ------------------------------------------------------------ vegetation

    def bulk_upsert_vegetation(self, kind, layer, items, run_id, member_attr,
                               source_default="lidar"):
        """Upsert a full generation of tree/shrub instances.

        Each item is the dict the build scripts produce (x/y already rounded
        to 3 decimals; every key besides x/y becomes an observation, plus
        member_attr=True). Returns (ids, stats). IDs are deterministic with
        position-probed collision suffixes, so the same physical stem keeps
        its ID across rebuilds regardless of encounter order.
        """
        now = utcnow()
        existing_pos = self.points(layer)  # eid -> (x, y, source)
        latest = self._latest_encoded_map(kind)
        stats = {"created": 0, "reactivated": 0, "observations": 0, "unchanged": 0,
                 "collision_suffixed": 0}

        def resolve_id(source, x, y, assigned):
            base = entity_id(kind, source, x, y)
            eid, n = base, 1
            while True:
                if eid in assigned:  # in-batch duplicate -> next suffix
                    n += 1
                    eid = f"{base}-{n}"
                    continue
                prev = existing_pos.get(eid)
                if prev is not None and (abs(prev[0] - x) > 1e-3 or abs(prev[1] - y) > 1e-3):
                    n += 1  # ID taken by a different position -> probe on
                    eid = f"{base}-{n}"
                    continue
                if n > 1:
                    stats["collision_suffixed"] += 1
                return eid

        assigned = {}
        ids = []
        retired_now = {
            r[0]
            for r in self.conn.execute(
                "SELECT entity_id FROM entities WHERE kind = ? AND retired_run_id IS NOT NULL",
                (kind,),
            )
        }
        cur = self.conn.cursor()
        ent_rows, point_rows, obs_rows = [], [], []
        unretire = []
        for item in items:
            x, y = item["x"], item["y"]
            source = item.get("source", source_default)
            eid = resolve_id(source, x, y, assigned)
            assigned[eid] = True
            ids.append(eid)
            if eid not in existing_pos:
                ent_rows.append((eid, kind, run_id, now))
                point_rows.append((eid, source, x, y, gpkg_point_blob(x, y)))
                existing_pos[eid] = (x, y, source)
                stats["created"] += 1
            elif eid in retired_now:
                unretire.append(eid)
                stats["reactivated"] += 1
            changed = False
            for attr, value in item.items():
                if attr in ("x", "y"):
                    continue
                encoded = encode_value(value)
                if latest.get((eid, attr)) != encoded:
                    obs_rows.append((eid, attr, encoded, now, run_id,
                                     source, item.get("confidence")))
                    latest[(eid, attr)] = encoded
                    changed = True
            member_encoded = encode_value(True)
            if latest.get((eid, member_attr)) != member_encoded:
                obs_rows.append((eid, member_attr, member_encoded, now, run_id,
                                 source, None))
                latest[(eid, member_attr)] = member_encoded
                changed = True
            if not changed and eid not in retired_now:
                stats["unchanged"] += 1

        cur.executemany(
            "INSERT INTO entities (entity_id, kind, created_run_id, created_at)"
            " VALUES (?, ?, ?, ?)", ent_rows)
        cur.executemany(
            f"INSERT INTO {layer} (entity_id, source, x, y, geom) VALUES (?, ?, ?, ?, ?)",
            point_rows)
        cur.executemany(
            "INSERT INTO observations"
            " (entity_id, attr, value, observed_at, run_id, source, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", obs_rows)
        cur.executemany(
            "UPDATE entities SET retired_run_id = NULL, retired_at = NULL WHERE entity_id = ?",
            [(e,) for e in unretire])
        if self._journal:
            for eid, ekind, erun, eat in ent_rows:
                self._log(op="entity", entity_id=eid, kind=ekind, run_id=erun,
                          created_at=eat)
            for eid, esource, ex, ey, _blob in point_rows:
                self._log(op="point", layer=layer, entity_id=eid, source=esource,
                          x=ex, y=ey)
            for eid, attr, value, at, orun, osource, oconf in obs_rows:
                self._log(op="obs", entity_id=eid, attr=attr, value=value, at=at,
                          run_id=orun, source=osource, confidence=oconf)
            for eid in unretire:
                self._log(op="unretire", entity_id=eid)
        stats["observations"] = len(obs_rows)
        self.conn.commit()
        return ids, stats

    def reconcile_membership(self, kind, member_attr, seen_ids, run_id,
                             other_member_attrs=()):
        """Mark entities that left this membership (member_attr -> false) and
        retire those that belong to no membership at all. Returns
        (left_count, retired_count)."""
        latest = self._latest_encoded_map(kind)
        true_enc = encode_value(True)
        prev = {eid for (eid, attr), v in latest.items()
                if attr == member_attr and v == true_enc}
        gone = sorted(prev - set(seen_ids))
        retired = 0
        now = utcnow()
        for eid in gone:
            self.observe(eid, member_attr, False, run_id, observed_at=now,
                         dedup=False)
            still_member = any(
                latest.get((eid, other)) == true_enc for other in other_member_attrs
            )
            if not still_member:
                self.retire_entity(eid, run_id)
                retired += 1
        self.conn.commit()
        return len(gone), retired

    # ---------------------------------------------------------------- layers

    def upsert_layer(self, layer_id, **fields):
        row = self.conn.execute(
            f"SELECT {', '.join(fields)} FROM layers WHERE layer_id = ?",
            (layer_id,),
        ).fetchone()
        if row is not None and list(row) == list(fields.values()):
            return  # unchanged
        cols = ["layer_id"] + list(fields.keys())
        sql = (
            f"INSERT INTO layers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
            " ON CONFLICT(layer_id) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in fields)
        )
        self.conn.execute(sql, [layer_id] + list(fields.values()))
        self.conn.commit()
        self._log(op="layer", layer_id=layer_id, fields=fields)

    # --------------------------------------------------------------- plans

    def register_plan_base(self, base_id, fingerprint, manifest, created_at=None):
        """Register one immutable, content-addressed planning baseline.

        The potentially large snapshot files live under data/plans/bases and
        are addressed by hashes in ``manifest``.  The journal records their
        identity and provenance, following the same file-plus-hash convention
        used for rasters and model binaries elsewhere in the twin.
        """
        at = created_at or utcnow()
        encoded_manifest = encode_value(manifest)
        row = self.conn.execute(
            "SELECT fingerprint, manifest FROM plan_bases WHERE base_id = ?",
            (base_id,),
        ).fetchone()
        if row is not None:
            if row != (fingerprint, encoded_manifest):
                raise ValueError(f"plan base id collision: {base_id}")
            return False
        self.conn.execute(
            "INSERT INTO plan_bases (base_id, fingerprint, manifest, created_at)"
            " VALUES (?, ?, ?, ?)",
            (base_id, fingerprint, encoded_manifest, at),
        )
        self.conn.commit()
        self._log(op="plan_base", base_id=base_id, fingerprint=fingerprint,
                  manifest=manifest, created_at=at)
        return True

    def insert_plan(self, plan_id, name, head_revision_id=None,
                    forked_from_revision_id=None, created_at=None):
        at = created_at or utcnow()
        self.conn.execute(
            "INSERT INTO plans"
            " (plan_id, name, head_revision_id, forked_from_revision_id,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, name, head_revision_id, forked_from_revision_id, at, at),
        )
        self.conn.commit()
        self._log(op="plan_create", plan_id=plan_id, name=name,
                  head_revision_id=head_revision_id,
                  forked_from_revision_id=forked_from_revision_id,
                  created_at=at)

    def update_plan(self, plan_id, *, name=None, archived_at=None,
                    set_archived=False, updated_at=None):
        row = self.conn.execute(
            "SELECT name, archived_at FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        next_name = row[0] if name is None else str(name)
        next_archived = archived_at if set_archived else row[1]
        at = updated_at or utcnow()
        if (next_name, next_archived) == row:
            return False
        self.conn.execute(
            "UPDATE plans SET name = ?, archived_at = ?, updated_at = ?"
            " WHERE plan_id = ?",
            (next_name, next_archived, at, plan_id),
        )
        self.conn.commit()
        self._log(op="plan_update", plan_id=plan_id, name=next_name,
                  archived_at=next_archived, updated_at=at)
        return True

    def insert_plan_revision(self, revision_id, plan_id, parent_revision_id,
                             base_id, content_hash, edits, message=None,
                             checkpoint_name=None, author=None, created_at=None):
        """Insert an immutable revision carrying a complete edit snapshot."""
        at = created_at or utcnow()
        self.conn.execute(
            "INSERT INTO plan_revisions"
            " (revision_id, plan_id, parent_revision_id, base_id, content_hash,"
            " message, checkpoint_name, author, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision_id, plan_id, parent_revision_id, base_id, content_hash,
             message, checkpoint_name, author, at),
        )
        normalized = []
        for ordinal, edit in enumerate(edits):
            item = {
                "edit_id": str(edit["edit_id"]),
                "kind": str(edit["kind"]),
                "geometry": edit.get("geometry"),
                "params": edit.get("params") or {},
                "label": edit.get("label"),
                "ordinal": int(edit.get("ordinal", ordinal)),
            }
            normalized.append(item)
            self.conn.execute(
                "INSERT INTO plan_edits"
                " (revision_id, edit_id, ordinal, kind, geometry, params, label)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (revision_id, item["edit_id"], item["ordinal"], item["kind"],
                 encode_value(item["geometry"]), encode_value(item["params"]),
                 item["label"]),
            )
        self.conn.commit()
        self._log(op="plan_revision", revision_id=revision_id, plan_id=plan_id,
                  parent_revision_id=parent_revision_id, base_id=base_id,
                  content_hash=content_hash, edits=normalized, message=message,
                  checkpoint_name=checkpoint_name, author=author, created_at=at)

    def update_plan_head(self, plan_id, revision_id, expected_revision_id=None,
                         updated_at=None):
        """Compare-and-swap a plan head.  Returns False on a stale writer."""
        at = updated_at or utcnow()
        if expected_revision_id is None:
            cur = self.conn.execute(
                "UPDATE plans SET head_revision_id = ?, updated_at = ?"
                " WHERE plan_id = ? AND head_revision_id IS NULL",
                (revision_id, at, plan_id),
            )
        else:
            cur = self.conn.execute(
                "UPDATE plans SET head_revision_id = ?, updated_at = ?"
                " WHERE plan_id = ? AND head_revision_id = ?",
                (revision_id, at, plan_id, expected_revision_id),
            )
        if not cur.rowcount:
            self.conn.rollback()
            return False
        self.conn.commit()
        self._log(op="plan_head", plan_id=plan_id, revision_id=revision_id,
                  expected_revision_id=expected_revision_id, updated_at=at)
        return True

    def upsert_plan_simulation_run(self, plan_run_id, plan_id, revision_id,
                                   simulator, status, parameters, result=None,
                                   artifact_path=None, input_hash=None,
                                   created_at=None, finished_at=None):
        at = created_at or utcnow()
        self.conn.execute(
            "INSERT INTO plan_simulation_runs"
            " (plan_run_id, plan_id, revision_id, simulator, status, parameters,"
            " result, artifact_path, input_hash, created_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(plan_run_id) DO UPDATE SET"
            " status = excluded.status, result = excluded.result,"
            " artifact_path = excluded.artifact_path,"
            " input_hash = excluded.input_hash, finished_at = excluded.finished_at",
            (plan_run_id, plan_id, revision_id, simulator, status,
             encode_value(parameters or {}),
             None if result is None else encode_value(result), artifact_path,
             input_hash, at, finished_at),
        )
        self.conn.commit()
        self._log(op="plan_simulation", plan_run_id=plan_run_id,
                  plan_id=plan_id, revision_id=revision_id,
                  simulator=simulator, status=status,
                  parameters=parameters or {}, result=result,
                  artifact_path=artifact_path, input_hash=input_hash,
                  created_at=at, finished_at=finished_at)

    def plan_rows(self, include_archived=False):
        sql = (
            "SELECT plan_id, name, head_revision_id, forked_from_revision_id,"
            " created_at, updated_at, archived_at FROM plans"
        )
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY updated_at DESC, plan_id"
        return [
            {"plan_id": r[0], "name": r[1], "head_revision_id": r[2],
             "forked_from_revision_id": r[3], "created_at": r[4],
             "updated_at": r[5], "archived_at": r[6]}
            for r in self.conn.execute(sql)
        ]

    def plan_revision(self, revision_id):
        row = self.conn.execute(
            "SELECT revision_id, plan_id, parent_revision_id, base_id,"
            " content_hash, message, checkpoint_name, author, created_at"
            " FROM plan_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            return None
        edits = [
            {"edit_id": r[0], "ordinal": r[1], "kind": r[2],
             "geometry": decode_value(r[3]), "params": decode_value(r[4]),
             "label": r[5]}
            for r in self.conn.execute(
                "SELECT edit_id, ordinal, kind, geometry, params, label"
                " FROM plan_edits WHERE revision_id = ?"
                " ORDER BY ordinal, edit_id", (revision_id,))
        ]
        return {
            "revision_id": row[0], "plan_id": row[1],
            "parent_revision_id": row[2], "base_id": row[3],
            "content_hash": row[4], "message": row[5],
            "checkpoint_name": row[6], "author": row[7],
            "created_at": row[8], "edits": edits,
        }

    def plan_history(self, head_revision_id):
        """Return one plan head's immutable ancestry, newest first.

        A branch intentionally starts at a revision created by its source plan,
        so filtering revisions by ``plan_id`` loses the fork point.  Walking
        parent links also gives callers a single reachability check: a revision
        is addressable through a plan only when it appears in this chain.
        """
        history = []
        seen = set()
        revision_id = head_revision_id
        while revision_id:
            if revision_id in seen:
                raise ValueError("cycle in plan revision graph")
            seen.add(revision_id)
            row = self.conn.execute(
                "SELECT revision_id, plan_id, parent_revision_id, content_hash,"
                " message, checkpoint_name, author, created_at"
                " FROM plan_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"missing plan revision in ancestry: {revision_id}")
            history.append({
                "revision_id": row[0], "created_by_plan_id": row[1],
                "parent_revision_id": row[2], "content_hash": row[3],
                "message": row[4], "checkpoint_name": row[5],
                "author": row[6], "created_at": row[7],
            })
            revision_id = row[2]
        return history

    def plan_simulation_rows(self, plan_id, limit=50):
        rows = self.conn.execute(
            "SELECT plan_run_id, revision_id, simulator, status, parameters,"
            " artifact_path, input_hash, created_at, finished_at, result IS NOT NULL"
            " FROM plan_simulation_runs WHERE plan_id = ?"
            " ORDER BY created_at DESC, plan_run_id DESC LIMIT ?",
            (plan_id, max(1, min(500, int(limit)))),
        )
        return [
            {
                "plan_run_id": row[0], "revision_id": row[1],
                "simulator": row[2], "status": row[3],
                "parameters": decode_value(row[4]), "artifact_path": row[5],
                "input_hash": row[6], "created_at": row[7],
                "finished_at": row[8], "result_available": bool(row[9]),
            }
            for row in rows
        ]

    # -------------------------------------------------------------- read API

    def instances(self, kind, layer, member_attr, attr_order, include_id=True):
        """The store's read path for whole populations: alive entities of a
        kind whose latest member_attr is true, as dicts ordered by entity_id
        ({id, x, y} + attr_order from the latest observations). Build scripts
        and the exporter both consume this — never the exported JSON."""
        alive = set(self.alive_entities(kind))
        attrs = self.latest_attrs(kind)
        positions = self.points(layer)
        out = []
        for eid in sorted(alive):
            a = attrs.get(eid, {})
            if a.get(member_attr) is not True:
                continue
            x, y, _source = positions[eid]
            item = {"id": eid} if include_id else {}
            item.update({"x": x, "y": y})
            for key in attr_order:
                if key in a:
                    item[key] = a[key]
            out.append(item)
        return out

    # ---------------------------------------------------------------- replay

    def apply_journal_op(self, op):
        """Replay one journal op verbatim (used by rebuild_store.py).
        Values arrive already encoded; timestamps and run ids are preserved."""
        kind = op["op"]
        if kind == "run":
            self.conn.execute(
                "INSERT INTO pipeline_runs (run_id, script, started_at, inputs_hash, notes)"
                " VALUES (?, ?, ?, ?, ?)",
                (op["run_id"], op["script"], op["started_at"],
                 op.get("inputs_hash"), op.get("notes")))
        elif kind == "finish_run":
            self.conn.execute(
                "UPDATE pipeline_runs SET finished_at = ?,"
                " notes = COALESCE(?, notes) WHERE run_id = ?",
                (op["finished_at"], op.get("notes"), op["run_id"]))
        elif kind == "meta":
            value = op["value"]
            if op["key"] == "schema_version":
                try:
                    value = encode_value(max(SCHEMA_VERSION, int(decode_value(value))))
                except (TypeError, ValueError):
                    value = encode_value(SCHEMA_VERSION)
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (op["key"], value))
        elif kind == "entity":
            self.conn.execute(
                "INSERT INTO entities (entity_id, kind, created_run_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (op["entity_id"], op["kind"], op["run_id"], op["created_at"]))
        elif kind == "retire":
            self.conn.execute(
                "UPDATE entities SET retired_run_id = ?, retired_at = ?"
                " WHERE entity_id = ?",
                (op["run_id"], op["retired_at"], op["entity_id"]))
        elif kind == "unretire":
            self.conn.execute(
                "UPDATE entities SET retired_run_id = NULL, retired_at = NULL"
                " WHERE entity_id = ?", (op["entity_id"],))
        elif kind == "obs":
            self.conn.execute(
                "INSERT INTO observations"
                " (entity_id, attr, value, observed_at, run_id, source, confidence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (op["entity_id"], op["attr"], op["value"], op["at"],
                 op["run_id"], op.get("source"), op.get("confidence")))
        elif kind == "point":
            self.conn.execute(
                f"INSERT INTO {op['layer']} (entity_id, source, x, y, geom)"
                " VALUES (?, ?, ?, ?, ?)",
                (op["entity_id"], op.get("source"), op["x"], op["y"],
                 gpkg_point_blob(op["x"], op["y"])))
        elif kind == "feature":
            self.insert_feature(op["layer"], op.get("entity_id"),
                                bytes.fromhex(op["wkb"]), op.get("properties"))
        elif kind == "feature_upsert":
            self.upsert_feature(op["layer"], op["entity_id"],
                                bytes.fromhex(op["wkb"]), op.get("properties"))
        elif kind == "spatial_layer":
            # older journals predate the columns field; they all used the default
            self.ensure_spatial_layer(op["name"], op["geom_type"],
                                      op.get("columns", "properties TEXT"))
        elif kind == "layer":
            self.upsert_layer(op["layer_id"], **op["fields"])
        elif kind == "plan_base":
            self.register_plan_base(
                op["base_id"], op["fingerprint"], op["manifest"],
                created_at=op["created_at"])
        elif kind == "plan_create":
            self.insert_plan(
                op["plan_id"], op["name"],
                head_revision_id=op.get("head_revision_id"),
                forked_from_revision_id=op.get("forked_from_revision_id"),
                created_at=op["created_at"])
        elif kind == "plan_update":
            self.update_plan(
                op["plan_id"], name=op.get("name"),
                archived_at=op.get("archived_at"), set_archived=True,
                updated_at=op["updated_at"])
        elif kind == "plan_revision":
            self.insert_plan_revision(
                op["revision_id"], op["plan_id"],
                op.get("parent_revision_id"), op["base_id"],
                op["content_hash"], op.get("edits") or [],
                message=op.get("message"),
                checkpoint_name=op.get("checkpoint_name"),
                author=op.get("author"), created_at=op["created_at"])
        elif kind == "plan_head":
            self.conn.execute(
                "UPDATE plans SET head_revision_id = ?, updated_at = ?"
                " WHERE plan_id = ?",
                (op["revision_id"], op["updated_at"], op["plan_id"]))
        elif kind == "plan_simulation":
            self.upsert_plan_simulation_run(
                op["plan_run_id"], op["plan_id"], op["revision_id"],
                op["simulator"], op["status"], op.get("parameters") or {},
                result=op.get("result"), artifact_path=op.get("artifact_path"),
                input_hash=op.get("input_hash"),
                created_at=op["created_at"], finished_at=op.get("finished_at"))
        else:
            raise ValueError(f"unknown journal op: {kind}")
