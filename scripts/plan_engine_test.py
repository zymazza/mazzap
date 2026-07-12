#!/usr/bin/env python3
"""Focused persistence/materialization tests for VEIL Plan."""

from __future__ import annotations

import glob
import gzip
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import twin_store
import twin_query
from plan_engine import PlanEngine, PlanError, normalize_edit
from twin_store import Store
from twin_query import (Region, TwinQuery, TwinQueryError,
                        line_geometry_intersects_region)


def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class PlanEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="veil-plan-test-")
        twin_store.DATA_DIR = self.tmp
        twin_store.STORE_PATH = os.path.join(self.tmp, "twin.gpkg")
        twin_store.JOURNAL_DIR = os.path.join(self.tmp, "journal")
        twin_query.ANNOTATIONS_PATH = os.path.join(self.tmp, "annotations.json")
        heights = [100.0] * 81
        grid = {
            "width": 9, "height": 9, "heights": heights,
            "minX": 0.0, "maxX": 8.0, "minY": 0.0, "maxY": 8.0,
            "outerMinX": -0.5, "outerMaxX": 8.5,
            "outerMinY": -0.5, "outerMaxY": 8.5,
            "minElevation": 100.0, "maxElevation": 100.0,
        }
        write(os.path.join(self.tmp, "terrain", "grid.json"), grid)
        write(os.path.join(self.tmp, "terrain", "aoi_local.geojson"), {
            "type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [8, 0], [8, 8], [0, 8], [0, 0]]]}}]})
        write(os.path.join(self.tmp, "georef.json"), {
            "analysis_crs": "EPSG:3857", "geographic_crs": "EPSG:4326",
            "origin_utm": [0, 0], "proj4": "+proj=merc +datum=WGS84 +units=m +no_defs"})
        write(os.path.join(self.tmp, "scene.json"), {
            "name": "Plan fixture", "origin_utm": [0, 0],
            "terrain": {"grid_url": "/data/terrain/grid.json"},
            "vegetation": {"tree_instances_url": "/data/vegetation/tree_instances.json",
                           "shrub_points_url": "/data/vegetation/shrub_points.json"}})
        write(os.path.join(self.tmp, "vegetation", "tree_instances.json"), [
            {"id": "tree:one", "x": 2.0, "y": 2.0, "height": 8.0, "radius": 2.0,
             "type": "deciduous", "species": "Red Maple", "source": "fixture"},
            {"id": "tree:two", "x": 7.0, "y": 7.0, "height": 9.0, "radius": 2.0,
             "type": "evergreen", "species": "Pine", "source": "fixture"},
        ])
        write(os.path.join(self.tmp, "vegetation", "shrub_points.json"), [
            {"id": "shrub:one", "x": 1.0, "y": 7.0, "baseScale": 0.8, "height": 1.0,
             "source": "fixture"},
        ])
        write(os.path.join(self.tmp, "vegetation", "metadata.json"), {"canopy_cover_pct": 20})
        store = Store(twin_store.STORE_PATH)
        store.set_meta("schema_version", twin_store.SCHEMA_VERSION)
        store.set_meta("scene_template", read(os.path.join(self.tmp, "scene.json")))
        store.set_meta("origin_utm", [0, 0])
        store.set_meta("crs", {"analysis_crs": "EPSG:3857"})
        store.close()
        self.engine = PlanEngine(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_linear_region_matching_uses_the_path_instead_of_its_centroid(self):
        region = Region(
            "bbox", (0.0, 0.0, 8.0, 8.0),
            lambda x, y: 0.0 <= x <= 8.0 and 0.0 <= y <= 8.0,
            64.0, "fixture",
        )
        outside_ring_with_inside_vertex_average = {
            "type": "LineString",
            "coordinates": [[-2, -2], [10, -2], [10, 10], [-2, 10], [-2, -2]],
        }
        crossing_line = {
            "type": "LineString",
            "coordinates": [[-2, 4], [10, 4]],
        }
        self.assertFalse(line_geometry_intersects_region(
            outside_ring_with_inside_vertex_average, region))
        self.assertTrue(line_geometry_intersects_region(crossing_line, region))

    def test_create_commit_materialize_and_branch(self):
        created = self.engine.create_plan("Water and orchard")
        plan_id = created["plan"]["plan_id"]
        root_revision = created["revision"]["revision_id"]
        self.assertEqual(created["revision"]["edits"], [])

        edits = [
            {"kind": "terrain_cut", "geometry": {"type": "Point", "coordinates": [4, 4]},
             "params": {"radius_m": 2, "depth_m": 1}, "label": "pond"},
            {"kind": "vegetation_remove", "geometry": None,
             "params": {"entity_ids": ["tree:one"], "kinds": ["tree"]}},
            {"kind": "vegetation_add", "geometry": {"type": "Point", "coordinates": [5, 5]},
             "params": {"habit": "tree", "species": "Cold-hardy apple",
                        "type": "deciduous", "height": 4, "radius": 2}},
        ]
        committed = self.engine.commit(plan_id, root_revision, edits, message="first layout")
        revision = committed["revision"]
        manifest = committed["materialized"]
        grid = read(os.path.join(manifest["data_dir"], "terrain", "grid.json"))
        self.assertLess(grid["heights"][4 * 9 + 4], 99.1)
        trees = read(os.path.join(manifest["data_dir"], "vegetation", "tree_instances.json"))
        ids = {tree["id"] for tree in trees}
        self.assertNotIn("tree:one", ids)
        self.assertTrue(any(tree.get("species") == "Cold-hardy apple" for tree in trees))
        self.assertGreater(manifest["diff"]["terrain"]["cut_m3"], 0)

        branched = self.engine.branch(plan_id, "Earlier alternative", revision_id=root_revision)
        self.assertEqual(branched["revision"]["revision_id"], root_revision)
        self.assertEqual(branched["revision"]["edits"], [])
        self.assertNotEqual(branched["plan"]["plan_id"], plan_id)

        discarded = self.engine.update(branched["plan"]["plan_id"], archived=True)
        self.assertIsNotNone(discarded["plan"]["archived_at"])
        self.assertEqual(len(self.engine.list_plans()["plans"]), 1)
        self.assertEqual(len(self.engine.list_plans(include_archived=True)["plans"]), 2)

        with self.assertRaises(PlanError) as conflict:
            self.engine.commit(plan_id, root_revision, edits)
        self.assertEqual(conflict.exception.payload["error"], "plan_conflict")

        reopened = self.engine.get_plan(plan_id, materialize=True)
        self.assertEqual(reopened["revision"]["content_hash"], revision["content_hash"])
        self.assertEqual(reopened["materialized"]["diff"], manifest["diff"])

        checkpoint = self.engine.checkpoint(
            plan_id, revision["revision_id"], "Same land, separate results")
        self.assertEqual(checkpoint["revision"]["content_hash"], revision["content_hash"])
        self.assertEqual(checkpoint["materialized"]["cache_data_dir"], manifest["cache_data_dir"])
        self.assertNotEqual(checkpoint["materialized"]["data_dir"], manifest["data_dir"])

        unrelated = self.engine.create_plan("Unrelated")
        with self.assertRaises(PlanError) as unreachable:
            self.engine.get_plan(
                unrelated["plan"]["plan_id"], revision_id=revision["revision_id"])
        self.assertEqual(unreachable.exception.payload["error"], "revision_not_found")

    def test_proposal_orchard_confirmation_and_aoi_validation(self):
        created = self.engine.create_plan("GAIA orchard")
        plan_id = created["plan"]["plan_id"]
        head = created["revision"]["revision_id"]
        proposal = self.engine.propose(plan_id, [{
            "kind": "orchard",
            "geometry": {"type": "Polygon", "coordinates": [[
                [1, 1], [7, 1], [7, 7], [1, 7], [1, 1],
            ]]},
            "params": {"habit": "tree", "species": "Cold-hardy apple",
                       "type": "deciduous", "height": 4, "radius": 1.5,
                       "spacing_m": 2},
            "label": "Test orchard",
        }], expected_revision_id=head)
        self.assertEqual(self.engine.get_plan(plan_id)["plan"]["head_revision_id"], head)
        self.assertGreater(proposal["preview"]["vegetation"]["trees_added"], 1)
        with self.assertRaises(PlanError) as unconfirmed:
            self.engine.apply_proposal(proposal["proposal_id"])
        self.assertEqual(unconfirmed.exception.payload["error"], "confirmation_required")
        applied = self.engine.apply_proposal(proposal["proposal_id"], confirmed=True)
        self.assertNotEqual(applied["revision"]["revision_id"], head)
        self.assertEqual(applied["proposal"]["status"], "applied")

        with self.assertRaises(PlanError) as outside:
            self.engine.propose(plan_id, [{
                "kind": "vegetation_add",
                "geometry": {"type": "Point", "coordinates": [100, 100]},
                "params": {"habit": "tree", "species": "Pine"},
            }])
        self.assertEqual(outside.exception.payload["error"], "edit_outside_aoi")

    def test_spatial_vegetation_removal_resolves_before_preview_and_apply(self):
        created = self.engine.create_plan("GAIA creek clearance")
        plan_id = created["plan"]["plan_id"]
        head = created["revision"]["revision_id"]
        proposal = self.engine.propose(plan_id, [{
            "kind": "vegetation_remove",
            "geometry": {"type": "LineString", "coordinates": [[0, 2], [5, 2]]},
            "params": {"entity_ids": [], "distance_m": 1, "kinds": ["tree"]},
            "label": "Remove trees near creek",
        }], expected_revision_id=head)

        removal = proposal["proposed_edits"][0]
        self.assertEqual(removal["params"]["entity_ids"], ["tree:one"])
        self.assertEqual(removal["params"]["buffer_m"], 1.0)
        self.assertNotIn("distance_m", removal["params"])
        self.assertEqual(proposal["preview"]["vegetation"]["entities_removed"], 1)
        self.assertEqual(self.engine.get_plan(plan_id)["plan"]["head_revision_id"], head)

        applied = self.engine.apply_proposal(proposal["proposal_id"], confirmed=True)
        trees = read(os.path.join(
            applied["materialized"]["data_dir"], "vegetation", "tree_instances.json"))
        self.assertEqual({row["id"] for row in trees}, {"tree:two"})
        self.assertEqual(applied["materialized"]["diff"]["vegetation"]["entities_removed"], 1)

        with self.assertRaises(PlanError) as no_matches:
            self.engine.propose(plan_id, [{
                "kind": "vegetation_remove",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [8, 0]]},
                "params": {"buffer_m": 0.1, "kinds": ["tree"]},
            }])
        self.assertEqual(no_matches.exception.payload["error"],
                         "empty_vegetation_removal")

        with self.assertRaises(PlanError) as no_selector:
            self.engine.propose(plan_id, [{
                "kind": "vegetation_remove",
                "geometry": None,
                "params": {"entity_ids": [], "kinds": ["tree"]},
            }])
        self.assertEqual(no_selector.exception.payload["error"],
                         "empty_vegetation_removal")

    def test_semantic_vegetation_clearance_uses_target_entity_geometry(self):
        from osgeo import ogr

        created = self.engine.create_plan("GAIA mapped-feature clearance")
        store = Store(os.path.join(self.tmp, "twin.gpkg"), journal=False)
        run_id = store.begin_run("clearance-fixture")
        stream_id = "stream:fixture-creek"
        geometry = ogr.CreateGeometryFromJson(json.dumps({
            "type": "LineString", "coordinates": [[0, 2], [5, 2]],
        }))
        store.upsert_entity(stream_id, "stream", run_id)
        store.upsert_feature("streams", stream_id, geometry.ExportToWkb(),
                             {"name": "Fixture creek"})
        store.finish_run(run_id)
        store.close()

        query = TwinQuery(os.path.join(self.tmp, "twin.gpkg"))
        try:
            proposal = query.propose_vegetation_clearance(
                created["plan"]["plan_id"], stream_id,
                buffer_m=1, kinds=["tree"], demonstrate=False)
        finally:
            query.store.close()
        self.assertEqual(proposal["clearance_target"]["entity_id"], stream_id)
        self.assertEqual(
            proposal["proposed_edits"][0]["params"]["entity_ids"],
            ["tree:one"],
        )
        self.assertEqual(proposal["preview"]["vegetation"]["entities_removed"], 1)

    def test_appended_spatial_removal_counts_only_effective_vegetation(self):
        created = self.engine.create_plan("Effective removal count")
        first = self.engine.commit(
            created["plan"]["plan_id"], created["revision"]["revision_id"], [{
                "edit_id": "edit_z_existing",
                "kind": "vegetation_remove",
                "geometry": None,
                "params": {"entity_ids": ["tree:one"], "kinds": ["tree"]},
            }])
        proposal = self.engine.propose(first["plan"]["plan_id"], [{
            "edit_id": "edit_a_new",
            "kind": "vegetation_remove",
            "geometry": {"type": "LineString", "coordinates": [[2, 2], [7, 7]]},
            "params": {"buffer_m": 0.1, "kinds": ["tree"]},
        }], expected_revision_id=first["revision"]["revision_id"])
        removal = proposal["proposed_edits"][0]
        self.assertEqual(removal["ordinal"], 1)
        self.assertEqual(removal["params"]["entity_ids"], ["tree:two"])
        self.assertEqual(proposal["preview"]["vegetation"]["entities_removed"], 1)

    def test_held_and_repeated_terrain_brush_strength_materializes_for_fill_and_cut(self):
        created = self.engine.create_plan("Accumulating earthwork")
        edits = [
            {
                "kind": "terrain_fill",
                "geometry": {"type": "Point", "coordinates": [2, 2]},
                "params": {
                    "radius_m": 1,
                    "height_m": 0.5,
                    "accumulation_stamps": [[2, 2, 0.25], [2, 2, 0.75]],
                },
            },
            {
                "kind": "terrain_cut",
                "geometry": {"type": "Point", "coordinates": [6, 6]},
                "params": {
                    "radius_m": 1,
                    "depth_m": 0.5,
                    "accumulation_stamps": [[6, 6, 0.4], [6, 6, 0.6]],
                },
            },
        ]

        committed = self.engine.commit(
            created["plan"]["plan_id"],
            created["revision"]["revision_id"],
            edits,
            message="hold and repaint",
        )
        grid = read(os.path.join(committed["materialized"]["data_dir"], "terrain", "grid.json"))
        self.assertAlmostEqual(grid["heights"][6 * 9 + 2], 101.0, places=3)
        self.assertAlmostEqual(grid["heights"][2 * 9 + 6], 99.0, places=3)
        self.assertEqual(
            committed["revision"]["edits"][0]["params"]["accumulation_stamps"],
            [[2.0, 2.0, 0.25], [2.0, 2.0, 0.75]],
        )
        with self.assertRaises(PlanError) as detached:
            normalize_edit({
                "kind": "terrain_fill",
                "geometry": {"type": "Point", "coordinates": [2, 2]},
                "params": {
                    "radius_m": 1,
                    "height_m": 0.5,
                    "accumulation_stamps": [[7, 7, 0.1]],
                },
            })
        self.assertEqual(detached.exception.payload["error"], "invalid_edit")

    def test_journal_replays_plan_graph(self):
        created = self.engine.create_plan("Journal plan")
        plan_id = created["plan"]["plan_id"]
        root = created["revision"]["revision_id"]
        saved = self.engine.checkpoint(plan_id, root, "Option A")

        replay_path = os.path.join(self.tmp, "replayed.gpkg")
        replay = Store(replay_path, journal=False)
        for path in sorted(glob.glob(os.path.join(self.tmp, "journal", "*.jsonl.gz"))):
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    replay.apply_journal_op(json.loads(line))
        replay.conn.commit()
        rows = replay.plan_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["plan_id"], plan_id)
        revision = replay.plan_revision(saved["revision"]["revision_id"])
        self.assertEqual(revision["checkpoint_name"], "Option A")
        replay.close()

    def test_agent_plan_surface_uses_preview_then_confirm(self):
        created = self.engine.create_plan("Agent garden")
        plan_id = created["plan"]["plan_id"]
        query = TwinQuery(os.path.join(self.tmp, "twin.gpkg"))
        try:
            listed = query.list_plans()
            self.assertEqual(listed["plans"][0]["plan_id"], plan_id)
            proposal = query.propose_garden(
                plan_id,
                [{"x": 1, "y": 1}, {"x": 4, "y": 1},
                 {"x": 4, "y": 4}, {"x": 1, "y": 4}],
                demonstrate=False)
            self.assertEqual(proposal["status"], "proposed")
            self.assertEqual(
                query.get_plan(plan_id)["plan"]["head_revision_id"],
                created["revision"]["revision_id"])
            with self.assertRaises(TwinQueryError) as confirmation:
                query.apply_plan_proposal(proposal["proposal_id"])
            self.assertEqual(confirmation.exception.payload["code"], "confirmation_required")
            applied = query.apply_plan_proposal(proposal["proposal_id"], confirmed=True)
            self.assertEqual(applied["revision"]["edits"][0]["kind"], "garden")
            simulation = query.run_plan_simulation(
                plan_id, applied["revision"]["revision_id"], "hydrology",
                {"mode": "rain", "rain_in": 1.0, "storm_hours": 2.0})
            self.assertEqual(simulation["plan"]["revision_id"],
                             applied["revision"]["revision_id"])
            self.assertEqual(simulation["plan_effects"]["terrain"],
                             "effective planned elevation and depressions")
            self.assertTrue(simulation["layers"])
            visible = query.run_plan_simulation(
                plan_id, applied["revision"]["revision_id"], "viewshed",
                {"point": {"x": 4, "y": 4}, "agl_m": 1.7,
                 "max_km": 0.01, "surface": "canopy"})
            self.assertEqual(visible["plan_effects"]["vegetation"],
                             "effective canopy blockers")
            self.assertIn("visible_area_km2", visible)
        finally:
            query.store.close()


if __name__ == "__main__":
    unittest.main()
