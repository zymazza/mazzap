#!/usr/bin/env python3
"""Concurrency tests for scripts/live/live_store.py.

The Node server spawns one `live_store.py append` process per telemetry
event, so several writers (plus an `export` reader) can hit
telemetry.sqlite at the same instant. Before WAL + busy_timeout were set in
connect(), a second concurrent writer failed immediately with
"database is locked" and that event was dropped from the DB. These tests pin
the pragmas and prove that a burst of simultaneous append processes all land.

No framework: run it directly.

    python3 scripts/live_store_concurrency_test.py
"""

import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_DIR = os.path.join(HERE, "live")
STORE = os.path.join(LIVE_DIR, "live_store.py")

PASS = 0
FAIL = 0
FAILURES = []


def check(name, ok):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"FAIL  {name}")


def make_event(i):
    ts = f"2026-06-22T17:{i // 60:02d}:{i % 60:02d}Z"
    return {
        "schema": "veil.live.v1",
        "kind": "position",
        "device_id": f"!dev{i:05x}",
        "observed_at": ts,
        "received_at": ts,
        "position": {"lat": 39.98 + i * 1e-4, "lon": -105.27 - i * 1e-4},
        "link": {"gateway_id": "gw-test"},
    }


def append_one(data_dir, event):
    env = {**os.environ, "TWIN_DATA_DIR": data_dir}
    proc = subprocess.run(
        [sys.executable, STORE, "append"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


with tempfile.TemporaryDirectory() as data_dir:
    db_path = os.path.join(data_dir, "live", "telemetry.sqlite")

    # --- pragmas are set on the connection ----------------------------------
    sys.path.insert(0, LIVE_DIR)
    os.environ["TWIN_DATA_DIR"] = data_dir
    import live_store  # noqa: E402

    conn = live_store.connect()
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    check("journal_mode is WAL", str(journal).lower() == "wal")
    check("busy_timeout matches BUSY_TIMEOUT_MS",
          busy == live_store.BUSY_TIMEOUT_MS and busy > 0)

    # --- a burst of simultaneous writers all land ---------------------------
    N = 24
    events = [make_event(i) for i in range(N)]
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as pool:
        results = list(pool.map(lambda e: append_one(data_dir, e), events))
    for code, _out, err in results:
        if code != 0:
            errors.append(err or f"exit {code}")

    check(f"all {N} concurrent append processes exit cleanly",
          not errors)
    if errors:
        print("    writer errors:", errors[:3])

    db = sqlite3.connect(db_path)
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    distinct = db.execute("SELECT COUNT(DISTINCT device_id) FROM events").fetchone()[0]
    db.close()
    check(f"all {N} events are persisted (no drops)", count == N)
    check(f"all {N} distinct devices are present", distinct == N)

print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
sys.exit(1 if FAIL else 0)
