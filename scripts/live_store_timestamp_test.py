#!/usr/bin/env python3
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest


TMP_DATA = tempfile.mkdtemp(prefix="veil-live-a5-")
os.environ["TWIN_DATA_DIR"] = TMP_DATA
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "live"))
live_store = importlib.import_module("live_store")


class LiveStoreTimestampTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(live_store.LIVE_DIR, ignore_errors=True)
        os.makedirs(live_store.LIVE_DIR, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP_DATA, ignore_errors=True)

    def test_append_canonicalizes_and_uses_epoch_for_history_latest_and_snapshot(self):
        live_store.append_event({
            "kind": "position",
            "device_id": "dev-a",
            "message": "middle-z",
            "observed_at": "2026-01-02T03:04:06Z",
            "received_at": "2026-01-02T03:04:07+00:00",
            "position": {"lat": 39.98, "lon": -105.27},
        })
        live_store.append_event({
            "kind": "position",
            "device_id": "dev-b",
            "message": "first-offset",
            "observed_at": "2026-01-02T03:04:05+00:00",
            "received_at": "2026-01-02T03:04:06.250Z",
            "position": {"lat": 39.99, "lon": -105.27},
        })
        live_store.append_event({
            "kind": "position",
            "device_id": "dev-a",
            "message": "last-millis",
            "observed_at": "2026-01-02T03:04:07.999Z",
            "received_at": "2026-01-02T03:04:08.999+00:00",
            "position": {"lat": 40.00, "lon": -105.27},
        })

        history = live_store.telemetry_history(since="2026-01-02T03:04:06+00:00", limit=10)
        self.assertEqual([e["message"] for e in history["events"]], ["middle-z", "last-millis"])
        self.assertEqual(history["events"][1]["observed_at"], "2026-01-02T03:04:07Z")
        self.assertEqual(history["events"][1]["received_at"], "2026-01-02T03:04:08Z")

        snapshot = live_store.telemetry_snapshot(prefer_live_api=False)
        latest_by_id = {event["device_id"]: event for event in snapshot["devices"]}
        self.assertEqual(latest_by_id["dev-a"]["message"], "last-millis")

        selected = live_store.selected_events({"mode": "snapshot", "at": "2026-01-02T03:04:06.500Z"})
        self.assertEqual({e["device_id"]: e["message"] for e in selected}, {
            "dev-a": "middle-z",
            "dev-b": "first-offset",
        })

        with live_store.connect() as conn:
            rows = conn.execute(
                "SELECT observed_at, received_at, observed_at_ms, received_at_ms, event_json"
                " FROM events ORDER BY observed_at_ms"
            ).fetchall()
        self.assertEqual([row["observed_at"] for row in rows], [
            "2026-01-02T03:04:05Z",
            "2026-01-02T03:04:06Z",
            "2026-01-02T03:04:07Z",
        ])
        self.assertTrue(all(row["observed_at_ms"] > 0 and row["received_at_ms"] > 0 for row in rows))
        self.assertEqual(json.loads(rows[0]["event_json"])["received_at"], "2026-01-02T03:04:06Z")

    def test_invalid_timestamps_are_rejected_without_today_partition_fallback(self):
        with self.assertRaises(ValueError):
            live_store.append_event({
                "kind": "position",
                "device_id": "bad",
                "observed_at": "not-a-time",
                "received_at": "2026-01-02T03:04:06Z",
                "position": {"lat": 39.98, "lon": -105.27},
            })
        with self.assertRaises(ValueError):
            live_store.append_event({
                "kind": "position",
                "device_id": "bad",
                "observed_at": "2026-01-02T03:04:06",
                "received_at": "2026-01-02T03:04:06Z",
                "position": {"lat": 39.98, "lon": -105.27},
            })

        with live_store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(live_store.telemetry_days(), [])

    def test_existing_sqlite_rows_are_migrated_and_event_json_is_canonicalized(self):
        conn = sqlite3.connect(live_store.DB_PATH)
        conn.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "observed_day TEXT NOT NULL,"
            "observed_at TEXT NOT NULL,"
            "received_at TEXT NOT NULL,"
            "kind TEXT NOT NULL,"
            "device_id TEXT NOT NULL,"
            "gateway_id TEXT,"
            "label TEXT,"
            "lat REAL,"
            "lon REAL,"
            "event_json TEXT NOT NULL)"
        )
        event = {
            "kind": "position",
            "device_id": "legacy",
            "observed_at": "2026-01-02T03:04:08.123+00:00",
            "received_at": "2026-01-02T03:04:09.456Z",
        }
        conn.execute(
            "INSERT INTO events (observed_day, observed_at, received_at, kind, device_id, event_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-01-02", event["observed_at"], event["received_at"], "position", "legacy", json.dumps(event)),
        )
        conn.commit()
        conn.close()

        with live_store.connect() as migrated:
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(events)")}
            row = migrated.execute(
                "SELECT observed_at, received_at, observed_at_ms, received_at_ms, event_json FROM events"
            ).fetchone()

        self.assertIn("observed_at_ms", columns)
        self.assertIn("received_at_ms", columns)
        self.assertEqual(row["observed_at"], "2026-01-02T03:04:08Z")
        self.assertEqual(row["received_at"], "2026-01-02T03:04:09Z")
        self.assertGreater(row["observed_at_ms"], 0)
        self.assertEqual(json.loads(row["event_json"])["observed_at"], "2026-01-02T03:04:08Z")


if __name__ == "__main__":
    unittest.main()
