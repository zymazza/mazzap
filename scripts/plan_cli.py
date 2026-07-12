#!/usr/bin/env python3
"""JSON command-line boundary used by server.js for Plan operations."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plan_catalog import catalog
from plan_engine import PlanEngine, PlanError


def read_payload() -> dict:
    text = sys.stdin.read()
    if not text.strip():
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise PlanError("request payload must be an object", code="invalid_request")
    return value


def run(engine: PlanEngine, action: str, body: dict) -> dict:
    if action == "list":
        return engine.list_plans(bool(body.get("include_archived")))
    if action == "catalog":
        return catalog(str(engine.data_dir))
    if action == "create":
        return engine.create_plan(body.get("name"), author=body.get("author"))
    if action == "get":
        return engine.get_plan(str(body.get("plan_id") or ""),
                               revision_id=body.get("revision_id"),
                               materialize=bool(body.get("materialize")))
    if action == "commit":
        return engine.commit(
            str(body.get("plan_id") or ""),
            str(body.get("expected_revision_id") or ""), body.get("edits"),
            message=body.get("message"), checkpoint_name=body.get("checkpoint_name"),
            author=body.get("author"))
    if action == "checkpoint":
        return engine.checkpoint(
            str(body.get("plan_id") or ""),
            str(body.get("expected_revision_id") or ""),
            str(body.get("name") or "Saved version"), author=body.get("author"))
    if action == "branch":
        return engine.branch(
            str(body.get("plan_id") or ""), str(body.get("name") or ""),
            revision_id=body.get("revision_id"), author=body.get("author"))
    if action == "update":
        return engine.update(str(body.get("plan_id") or ""),
                             name=body.get("name"), archived=body.get("archived"))
    if action == "materialize":
        return engine.materialize(str(body.get("revision_id") or ""),
                                  force=bool(body.get("force")))
    if action == "simulate":
        return engine.run_simulation(
            str(body.get("plan_id") or ""),
            str(body.get("revision_id") or ""),
            str(body.get("simulator") or ""),
            body.get("parameters") or {})
    raise PlanError("unknown plan command", code="invalid_action", action=action)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action")
    parser.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"))
    args = parser.parse_args()
    try:
        result = run(PlanEngine(args.data_dir), args.action, read_payload())
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except PlanError as exc:
        print(json.dumps(exc.payload, separators=(",", ":")))
        return 2
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": "invalid_request", "message": str(exc)},
                         separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
