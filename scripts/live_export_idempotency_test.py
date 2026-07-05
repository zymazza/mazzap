#!/usr/bin/env python3
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TMP_DATA = tempfile.mkdtemp(prefix="veil-live-a8-")
os.environ["TWIN_DATA_DIR"] = TMP_DATA
sys.path.insert(0, str(ROOT / "scripts" / "live"))
live_store = importlib.import_module("live_store")
FAKE_TWIN_PATH = Path(TMP_DATA) / "fake_twin.sqlite"


def encode_value(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class FakeStore:
    def __init__(self):
        self.conn = sqlite3.connect(FAKE_TWIN_PATH)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS pipeline_runs ("
            "run_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "script TEXT NOT NULL,"
            "finished_at TEXT,"
            "notes TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS entities ("
            "entity_id TEXT PRIMARY KEY,"
            "kind TEXT NOT NULL,"
            "created_run_id INTEGER NOT NULL,"
            "created_at TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS observations ("
            "obs_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "entity_id TEXT NOT NULL,"
            "attr TEXT NOT NULL,"
            "value TEXT NOT NULL,"
            "observed_at TEXT NOT NULL,"
            "run_id INTEGER NOT NULL,"
            "source TEXT,"
            "confidence REAL)"
        )
        self.conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.conn.commit()
        self.conn.close()

    def ensure_spatial_layer(self, name, _geom_type, _columns):
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {name} ("
            "entity_id TEXT UNIQUE, source TEXT, x REAL, y REAL,"
            "properties TEXT, geom BLOB)"
        )
        self.conn.commit()

    def begin_run(self, script, notes=None):
        cur = self.conn.execute(
            "INSERT INTO pipeline_runs (script, notes) VALUES (?, ?)",
            (script, notes),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id):
        self.conn.execute(
            "UPDATE pipeline_runs SET finished_at = ? WHERE run_id = ?",
            ("finished", run_id),
        )
        self.conn.commit()

    def upsert_entity(self, eid, kind, run_id, observed_at=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO entities"
            " (entity_id, kind, created_run_id, created_at) VALUES (?, ?, ?, ?)",
            (eid, kind, run_id, observed_at),
        )

    def observe(self, eid, attr, value, run_id, source=None, confidence=None,
                observed_at=None, dedup=True):
        encoded = encode_value(value)
        if dedup:
            row = self.conn.execute(
                "SELECT value FROM observations WHERE entity_id = ? AND attr = ?"
                " ORDER BY obs_id DESC LIMIT 1",
                (eid, attr),
            ).fetchone()
            if row is not None and row[0] == encoded:
                return False
        self.conn.execute(
            "INSERT INTO observations"
            " (entity_id, attr, value, observed_at, run_id, source, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, attr, encoded, observed_at, run_id, source, confidence),
        )
        return True


fake_twin_store = types.ModuleType("twin_store")
fake_twin_store.Store = FakeStore
fake_twin_store.encode_value = encode_value
fake_twin_store.gpkg_point_blob = lambda x, y: b"point"
sys.modules["twin_store"] = fake_twin_store


def write_georef():
    Path(TMP_DATA).mkdir(parents=True, exist_ok=True)
    with open(Path(TMP_DATA) / "georef.json", "w", encoding="utf-8") as fh:
        json.dump({"analysis_crs": "EPSG:4326", "origin_utm": [0, 0, 0]}, fh)


def observation_counts():
    with sqlite3.connect(FAKE_TWIN_PATH) as conn:
        return dict(conn.execute(
            "SELECT attr, COUNT(*) FROM observations GROUP BY attr"
        ).fetchall())


def total_observations():
    with sqlite3.connect(FAKE_TWIN_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


class LiveExportIdempotencyTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP_DATA, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(TMP_DATA, ignore_errors=True)
        write_georef()
        live_store.scene_xy = lambda lon, lat: (float(lon), float(lat))

    def append_position(self, observed_at, seq):
        live_store.append_event({
            "kind": "position",
            "device_id": "dev-a",
            "label": "Device A",
            "observed_at": observed_at,
            "received_at": observed_at,
            "position": {"lat": 39.98 + seq / 1000, "lon": -105.27 - seq / 1000},
            "message": f"event-{seq}",
            "data": {"seq": seq},
            "source": {"protocol": "unit"},
        })

    def test_retry_after_missing_audit_row_does_not_duplicate_observations(self):
        self.append_position("2026-01-02T03:04:05Z", 1)

        first = live_store.export_to_twin({"mode": "day", "date": "2026-01-02"})
        self.assertEqual(first["event_count"], 1)
        first_total = total_observations()
        first_counts = observation_counts()
        self.assertEqual(first_counts["live_event"], 1)
        self.assertEqual(first_counts["position"], 1)

        with live_store.connect() as conn:
            conn.execute("DELETE FROM exports")

        retry = live_store.export_to_twin({"mode": "day", "date": "2026-01-02"})
        self.assertEqual(retry["event_count"], 1)
        self.assertEqual(total_observations(), first_total)
        self.assertEqual(observation_counts(), first_counts)

        self.append_position("2026-01-02T03:04:06Z", 2)
        again = live_store.export_to_twin({"mode": "day", "date": "2026-01-02"})
        self.assertEqual(again["event_count"], 2)
        counts = observation_counts()
        self.assertEqual(counts["live_event"], 2)
        self.assertEqual(counts["position"], 2)
        self.assertEqual(counts["message"], 2)
        self.assertEqual(counts["data"], 2)


if __name__ == "__main__":
    unittest.main()
