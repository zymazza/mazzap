import json
import math
import threading
import unittest

from veil_dji_route import (
    BRIDGE_ROUTE_ENGINE,
    BRIDGE_ROUTE_SCHEMA,
    MAX_JSON_CHARACTERS,
    AtomicRouteRevisionStore,
    NedVelocityCommand,
    ROUTE_CAPABILITIES,
    RouteBounds,
    RouteCommandReason,
    RouteExecutionState,
    RouteParseError,
    RouteParseErrorCode,
    RoutePhase,
    RoutePlan,
    RouteReplacementMode,
    RouteReplacementScope,
    RouteRevisionAcceptanceStatus,
    RouteRevisionRequest,
    RouteTelemetry,
    RouteWaypoint,
    RouteYawMode,
    ground_ned_to_body,
    parse_route_revision,
    route_capabilities_dict,
    state_to_dict,
    tick_route,
    validate_route_plan,
)


NOW_MS = 10_000.0


class RouteParserTest(unittest.TestCase):
    def test_parses_strict_revision_envelope_and_defaults(self):
        request = parse_route_revision(json.dumps(valid_document()))

        self.assertEqual(BRIDGE_ROUTE_SCHEMA, request.schema)
        self.assertEqual(BRIDGE_ROUTE_ENGINE, request.engine)
        self.assertIsNone(request.expected_accepted_revision)
        self.assertIs(RouteReplacementMode.IMMEDIATE, request.activation)
        self.assertIs(
            RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE,
            request.scope,
        )
        self.assertEqual("route-a", request.plan.route_id)
        self.assertEqual(1, request.plan.revision)
        waypoint = request.plan.waypoints[0]
        self.assertEqual(2.0, waypoint.horizontal_speed_mps)
        self.assertEqual(1.0, waypoint.vertical_speed_mps)
        self.assertIs(RouteYawMode.FACE_WAYPOINT, waypoint.yaw_mode)
        self.assertIsNone(waypoint.yaw_deg)

    def test_parses_full_route_boundary_and_fixed_heading(self):
        document = valid_document()
        document["expected_accepted_revision"] = 4
        document["activation"] = "at_waypoint_boundary"
        document["scope"] = "full_route_continue"
        document["plan"]["waypoints"][0].update({
            "yaw_mode": "fixed_heading",
            "yaw_deg": -90,
            "horizontal_speed_mps": 4.5,
        })

        request = parse_route_revision(json.dumps(document))

        self.assertEqual(4, request.expected_accepted_revision)
        self.assertIs(RouteReplacementMode.AT_WAYPOINT_BOUNDARY, request.activation)
        self.assertIs(RouteReplacementScope.FULL_ROUTE_CONTINUE, request.scope)
        waypoint = request.plan.waypoints[0]
        self.assertIs(RouteYawMode.FIXED_HEADING, waypoint.yaw_mode)
        self.assertEqual(-90.0, waypoint.yaw_deg)
        self.assertEqual(4.5, waypoint.horizontal_speed_mps)

    def test_explicit_zero_is_not_silently_replaced_by_default(self):
        document = valid_document()
        document["plan"]["waypoints"][0]["horizontal_speed_mps"] = 0
        request = parse_route_revision(json.dumps(document))
        self.assertEqual(0.0, request.plan.waypoints[0].horizontal_speed_mps)

        accepted = AtomicRouteRevisionStore().accept(request)
        self.assertIs(RouteRevisionAcceptanceStatus.INVALID, accepted.status)
        self.assertIn(
            "waypoints[0].horizontal_speed_mps",
            {issue.path for issue in accepted.issues},
        )

    def test_rejects_unknown_duplicate_and_missing_fields(self):
        document = valid_document()
        document["plan"]["waypoints"][0]["altitdue_m"] = 11
        error = self.assert_parse_error(json.dumps(document))
        self.assertIs(RouteParseErrorCode.UNKNOWN_FIELD, error.code)
        self.assertEqual("$.plan.waypoints[0].altitdue_m", error.path)

        duplicate = (
            '{"schema":"veil.route-revision.v1","schema":"other",'
            '"engine":"bridge_virtual_stick","activation":"immediate",'
            '"scope":"remaining_route_from_current_state","plan":{}}'
        )
        error = self.assert_parse_error(duplicate)
        self.assertIs(RouteParseErrorCode.INVALID_JSON, error.code)
        self.assertIn("duplicate", error.message)

        document = valid_document()
        del document["plan"]["revision"]
        error = self.assert_parse_error(json.dumps(document))
        self.assertIs(RouteParseErrorCode.MISSING_FIELD, error.code)
        self.assertEqual("$.plan.revision", error.path)

    def test_rejects_numeric_coercion_boolean_revision_and_int64_overflow(self):
        for invalid in ("1", 1.0, True):
            document = valid_document()
            document["plan"]["revision"] = invalid
            error = self.assert_parse_error(json.dumps(document))
            self.assertIs(RouteParseErrorCode.WRONG_TYPE, error.code)
            self.assertEqual("$.plan.revision", error.path)

        document = valid_document()
        document["plan"]["revision"] = 1 << 63
        error = self.assert_parse_error(json.dumps(document))
        self.assertIs(RouteParseErrorCode.INVALID_VALUE, error.code)
        self.assertEqual("$.plan.revision", error.path)

    def test_rejects_nonfinite_values_trailing_data_and_oversize_document(self):
        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            raw = json.dumps(valid_document()).replace("0.0", literal, 1)
            error = self.assert_parse_error(raw)
            self.assertIn(
                error.code,
                (RouteParseErrorCode.INVALID_JSON, RouteParseErrorCode.INVALID_VALUE),
            )

        error = self.assert_parse_error(json.dumps(valid_document()) + " true")
        self.assertIs(RouteParseErrorCode.INVALID_JSON, error.code)

        error = self.assert_parse_error(" " * (MAX_JSON_CHARACTERS + 1))
        self.assertIs(RouteParseErrorCode.INVALID_VALUE, error.code)
        self.assertEqual("$", error.path)

    def test_rejects_ambiguous_activation_scope_and_wrong_root_type(self):
        document = valid_document()
        document["activation"] = "sometimes"
        error = self.assert_parse_error(json.dumps(document))
        self.assertIs(RouteParseErrorCode.INVALID_VALUE, error.code)
        self.assertEqual("$.activation", error.path)

        document = valid_document()
        document["scope"] = "restart"
        error = self.assert_parse_error(json.dumps(document))
        self.assertIs(RouteParseErrorCode.INVALID_VALUE, error.code)
        self.assertEqual("$.scope", error.path)

        error = self.assert_parse_error("[]")
        self.assertIs(RouteParseErrorCode.WRONG_TYPE, error.code)
        self.assertEqual("$", error.path)

    def assert_parse_error(self, document):
        with self.assertRaises(RouteParseError) as caught:
            parse_route_revision(document)
        return caught.exception


class RouteValidationAndCapabilityTest(unittest.TestCase):
    def test_capability_metadata_is_truthful_immutable_and_serializable(self):
        self.assertEqual("bridge_virtual_stick", ROUTE_CAPABILITIES["route_engine"])
        self.assertEqual("mac_persistent_session", ROUTE_CAPABILITIES["execution_owner"])
        self.assertFalse(ROUTE_CAPABILITIES["native_waypoint_execution"])
        self.assertFalse(ROUTE_CAPABILITIES["fly_library_interop"])
        self.assertFalse(ROUTE_CAPABILITIES["android_route_endpoint"])
        self.assertIn(
            "native_waypoint_start", ROUTE_CAPABILITIES["unsupported_actions"]
        )
        with self.assertRaises(TypeError):
            ROUTE_CAPABILITIES["native_waypoint_execution"] = True

        copy = route_capabilities_dict()
        json.dumps(copy)
        copy["unsupported_actions"]["test"] = "local mutation"
        self.assertNotIn("test", ROUTE_CAPABILITIES["unsupported_actions"])

    def test_validator_reports_every_invalid_numeric_and_yaw_field(self):
        invalid = RouteWaypoint(
            latitude_deg=math.nan,
            longitude_deg=181.0,
            altitude_m=math.inf,
            horizontal_speed_mps=0.0,
            vertical_speed_mps=-1.0,
            horizontal_tolerance_m=0.0,
            vertical_tolerance_m=-0.1,
            yaw_mode=RouteYawMode.FIXED_HEADING,
            yaw_deg=None,
            maximum_yaw_rate_deg_s=0.0,
        )
        plan = RoutePlan("", -1, (invalid,))

        paths = {issue.path for issue in validate_route_plan(plan)}

        self.assertIn("route_id", paths)
        self.assertIn("revision", paths)
        self.assertIn("waypoints[0].latitude_deg", paths)
        self.assertIn("waypoints[0].longitude_deg", paths)
        self.assertIn("waypoints[0].altitude_m", paths)
        self.assertIn("waypoints[0].horizontal_speed_mps", paths)
        self.assertIn("waypoints[0].vertical_speed_mps", paths)
        self.assertIn("waypoints[0].horizontal_tolerance_m", paths)
        self.assertIn("waypoints[0].vertical_tolerance_m", paths)
        self.assertIn("waypoints[0].yaw_deg", paths)
        self.assertIn("waypoints[0].maximum_yaw_rate_deg_s", paths)

    def test_invalid_bounds_are_rejected_before_store_creation(self):
        with self.assertRaisesRegex(ValueError, "maximum_horizontal_speed_mps"):
            AtomicRouteRevisionStore(
                RouteBounds(maximum_horizontal_speed_mps=24.0)
            )
        with self.assertRaisesRegex(ValueError, "telemetry_maximum_future_skew_ms"):
            AtomicRouteRevisionStore(
                RouteBounds(telemetry_maximum_future_skew_ms=-1.0)
            )

    def test_plan_leg_and_extent_are_checked_across_dateline_correctly(self):
        short_dateline = RoutePlan(
            "dateline",
            1,
            (
                waypoint(longitude=179.999),
                waypoint(longitude=-179.999),
            ),
        )
        self.assertEqual((), validate_route_plan(short_dateline))

        long_leg = RoutePlan(
            "long",
            1,
            (waypoint(latitude=0.0), waypoint(latitude=0.1)),
        )
        self.assertIn(
            "waypoints[1]", {issue.path for issue in validate_route_plan(long_leg)}
        )


class AtomicRevisionAndReplacementTest(unittest.TestCase):
    def test_acceptance_snapshots_mutable_input_and_requires_exact_head(self):
        mutable_waypoints = [waypoint(latitude=0.001)]
        store = AtomicRouteRevisionStore()
        first = store.accept(request(None, 1, mutable_waypoints))
        self.assertTrue(first.accepted)

        mutable_waypoints[0] = waypoint(latitude=-0.001)
        mutable_waypoints.append(waypoint(latitude=-0.002))
        snapshot = store.snapshot()
        self.assertIsInstance(snapshot.active_plan.waypoints, tuple)
        self.assertEqual(1, len(snapshot.active_plan.waypoints))
        self.assertEqual(0.001, snapshot.active_plan.waypoints[0].latitude_deg)

        conflict = store.accept(request(None, 2, [waypoint(latitude=0.002)]))
        self.assertIs(RouteRevisionAcceptanceStatus.REVISION_CONFLICT, conflict.status)
        self.assertEqual(1, store.newest_accepted_revision())

        accepted = store.accept(request(1, 2, [waypoint(latitude=0.002)]))
        self.assertTrue(accepted.accepted)
        self.assertEqual(2, store.newest_accepted_revision())

    def test_concurrent_writers_cannot_both_replace_same_revision(self):
        store = AtomicRouteRevisionStore()
        self.assertTrue(store.accept(request(None, 1, [waypoint()])).accepted)
        barrier = threading.Barrier(3)
        results = []
        result_lock = threading.Lock()

        def writer(revision):
            barrier.wait(timeout=2.0)
            result = store.accept(
                request(1, revision, [waypoint(latitude=revision * 0.001)])
            )
            with result_lock:
                results.append(result)

        threads = [
            threading.Thread(target=writer, args=(2,)),
            threading.Thread(target=writer, args=(3,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2.0)
        for thread in threads:
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(
            1,
            sum(result.status is RouteRevisionAcceptanceStatus.ACCEPTED
                for result in results),
        )
        self.assertEqual(
            1,
            sum(result.status is RouteRevisionAcceptanceStatus.REVISION_CONFLICT
                for result in results),
        )
        self.assertIn(store.newest_accepted_revision(), (2, 3))

    def test_full_route_immediate_replacement_preserves_current_target(self):
        store = running_store([
            waypoint(latitude=0.0),
            waypoint(latitude=0.001),
            waypoint(latitude=0.002),
        ])
        boundary = store.tick(telemetry(latitude=0.0), NOW_MS)
        self.assertEqual(1, boundary.state.target_waypoint_index)

        revised = [
            waypoint(latitude=0.0),
            waypoint(latitude=0.0015),
            waypoint(latitude=0.0025),
        ]
        accepted = store.accept(request(
            1,
            2,
            revised,
            scope=RouteReplacementScope.FULL_ROUTE_CONTINUE,
        ))
        self.assertTrue(accepted.accepted)
        self.assertIs(RoutePhase.RUNNING, accepted.state.phase)
        self.assertEqual(1, accepted.state.target_waypoint_index)

        tick = store.tick(telemetry(latitude=0.001), NOW_MS)
        self.assertTrue(tick.command.is_active)
        self.assertGreater(tick.command.north_mps, 0.0)

    def test_full_route_boundary_replacement_continues_at_following_index(self):
        store = running_store([
            waypoint(latitude=0.0),
            waypoint(latitude=0.001),
            waypoint(latitude=0.002),
        ])
        store.tick(telemetry(latitude=0.0), NOW_MS)
        accepted = store.accept(request(
            1,
            2,
            [
                waypoint(latitude=0.0),
                waypoint(latitude=0.001),
                waypoint(latitude=0.0025),
            ],
            activation=RouteReplacementMode.AT_WAYPOINT_BOUNDARY,
            scope=RouteReplacementScope.FULL_ROUTE_CONTINUE,
        ))
        self.assertTrue(accepted.accepted)
        self.assertEqual(1, accepted.state.active_plan.revision)
        self.assertEqual(2, accepted.state.pending_plan.revision)
        self.assertEqual(2, accepted.state.pending_target_waypoint_index)

        activation = store.tick(telemetry(latitude=0.001), NOW_MS)
        self.assertIs(RouteCommandReason.PLAN_REPLACED, activation.command.reason)
        self.assertEqual(2, activation.state.active_plan.revision)
        self.assertEqual(2, activation.state.target_waypoint_index)
        self.assertIsNone(activation.state.pending_plan)
        assert_neutral(self, activation.command)

    def test_remaining_route_starts_at_zero_from_fresh_current_position(self):
        store = running_store([
            waypoint(latitude=0.0),
            waypoint(latitude=0.001),
        ])
        store.tick(telemetry(latitude=0.0), NOW_MS)
        accepted = store.accept(request(
            1,
            2,
            [waypoint(latitude=0.0012)],
            scope=RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE,
        ))
        self.assertTrue(accepted.accepted)
        self.assertEqual(0, accepted.state.target_waypoint_index)

        tick = store.tick(telemetry(latitude=0.001), NOW_MS)
        self.assertTrue(tick.command.is_active)
        self.assertGreater(tick.command.north_mps, 0.0)

    def test_staged_revision_is_cas_head_and_short_full_plan_is_rejected(self):
        store = running_store([
            waypoint(latitude=0.0),
            waypoint(latitude=0.001),
            waypoint(latitude=0.002),
        ])
        store.tick(telemetry(latitude=0.0), NOW_MS)
        staged = store.accept(request(
            1,
            2,
            [
                waypoint(latitude=0.0),
                waypoint(latitude=0.001),
                waypoint(latitude=0.003),
            ],
            activation=RouteReplacementMode.AT_WAYPOINT_BOUNDARY,
            scope=RouteReplacementScope.FULL_ROUTE_CONTINUE,
        ))
        self.assertTrue(staged.accepted)
        self.assertEqual(2, store.newest_accepted_revision())

        stale = store.accept(request(1, 3, [waypoint(latitude=0.004)]))
        self.assertIs(RouteRevisionAcceptanceStatus.REVISION_CONFLICT, stale.status)

        too_short = store.accept(request(
            2,
            3,
            [waypoint(latitude=0.0), waypoint(latitude=0.001)],
            activation=RouteReplacementMode.AT_WAYPOINT_BOUNDARY,
            scope=RouteReplacementScope.FULL_ROUTE_CONTINUE,
        ))
        self.assertIs(RouteRevisionAcceptanceStatus.INVALID, too_short.status)
        self.assertIn("continuation target index 2", too_short.issues[-1].message)
        self.assertEqual(2, store.newest_accepted_revision())

    def test_unsupported_engine_and_terminal_replacement_are_explicit(self):
        store = AtomicRouteRevisionStore()
        unsupported = store.accept(
            request(None, 1, [waypoint()], engine="native_dji_waypoint")
        )
        self.assertIs(RouteRevisionAcceptanceStatus.UNSUPPORTED, unsupported.status)
        self.assertEqual("engine", unsupported.issues[0].path)
        self.assertIsNone(store.snapshot())

        self.assertTrue(store.accept(request(None, 1, [waypoint()])).accepted)
        self.assertTrue(store.abort().accepted)
        terminal = store.accept(request(1, 2, [waypoint(latitude=0.002)]))
        self.assertIs(RouteRevisionAcceptanceStatus.INVALID, terminal.status)
        self.assertEqual("phase", terminal.issues[0].path)
        self.assertIn("aborted", terminal.issues[0].message)

    def test_direct_request_cannot_smuggle_bool_cas_or_unknown_mode(self):
        store = AtomicRouteRevisionStore()
        invalid_cas = store.accept(request(True, 1, [waypoint()]))
        self.assertIs(RouteRevisionAcceptanceStatus.INVALID, invalid_cas.status)
        self.assertEqual("expected_accepted_revision", invalid_cas.issues[0].path)

        invalid_mode = request(None, 1, [waypoint()])
        invalid_mode = RouteRevisionRequest(
            invalid_mode.schema,
            invalid_mode.engine,
            invalid_mode.expected_accepted_revision,
            "immediate",
            invalid_mode.scope,
            invalid_mode.plan,
        )
        result = store.accept(invalid_mode)
        self.assertIs(RouteRevisionAcceptanceStatus.INVALID, result.status)
        self.assertEqual("activation", result.issues[0].path)


class RouteGuidanceTest(unittest.TestCase):
    def test_explicit_start_then_progresses_and_completes_with_neutral_boundaries(self):
        store = AtomicRouteRevisionStore()
        store.accept(request(None, 1, [
            waypoint(latitude=0.0, altitude=10.0),
            waypoint(latitude=0.0001, altitude=10.0),
        ]))
        before = store.tick(telemetry(latitude=0.0, altitude=10.0), NOW_MS)
        self.assertIs(RouteCommandReason.NOT_STARTED, before.command.reason)
        assert_neutral(self, before.command)

        self.assertTrue(store.start().accepted)
        advanced = store.tick(telemetry(latitude=0.0, altitude=10.0), NOW_MS)
        self.assertIs(RouteCommandReason.WAYPOINT_ADVANCED, advanced.command.reason)
        self.assertEqual(1, advanced.state.target_waypoint_index)
        assert_neutral(self, advanced.command)

        moving = store.tick(telemetry(latitude=0.0, altitude=10.0), NOW_MS)
        self.assertTrue(moving.command.is_active)
        self.assertGreater(moving.command.north_mps, 0.0)

        completed = store.tick(
            telemetry(latitude=0.0001, altitude=10.0), NOW_MS
        )
        self.assertIs(RoutePhase.COMPLETED, completed.state.phase)
        self.assertIs(RouteCommandReason.COMPLETED, completed.command.reason)
        assert_neutral(self, completed.command)

    def test_pause_resume_abort_are_always_neutral_when_not_running(self):
        store = running_store([waypoint(latitude=0.001)])
        self.assertTrue(store.pause().accepted)
        paused = store.tick(telemetry(), NOW_MS)
        self.assertIs(RouteCommandReason.PAUSED, paused.command.reason)
        assert_neutral(self, paused.command)

        self.assertTrue(store.resume().accepted)
        self.assertTrue(store.tick(telemetry(), NOW_MS).command.is_active)
        self.assertTrue(store.abort().accepted)
        aborted = store.tick(telemetry(), NOW_MS)
        self.assertIs(RouteCommandReason.ABORTED, aborted.command.reason)
        assert_neutral(self, aborted.command)

    def test_stale_future_invalid_telemetry_are_neutral_and_fresh_recovers(self):
        store = running_store([waypoint(latitude=0.001)])
        stale = store.tick(telemetry(sample_time=NOW_MS - 501.0), NOW_MS)
        self.assertIs(RouteCommandReason.STALE_TELEMETRY, stale.command.reason)
        assert_neutral(self, stale.command)

        future = store.tick(telemetry(sample_time=NOW_MS + 101.0), NOW_MS)
        self.assertIs(RouteCommandReason.STALE_TELEMETRY, future.command.reason)
        assert_neutral(self, future.command)

        invalid = store.tick(telemetry(yaw=math.nan), NOW_MS)
        self.assertIs(RouteCommandReason.INVALID_TELEMETRY, invalid.command.reason)
        assert_neutral(self, invalid.command)

        exact_old = store.tick(telemetry(sample_time=NOW_MS - 500.0), NOW_MS)
        self.assertTrue(exact_old.command.is_active)
        exact_future = store.tick(telemetry(sample_time=NOW_MS + 100.0), NOW_MS)
        self.assertTrue(exact_future.command.is_active)

    def test_velocity_and_yaw_commands_stay_within_configured_bounds(self):
        bounds = RouteBounds(
            maximum_horizontal_speed_mps=3.0,
            maximum_vertical_speed_mps=1.0,
            maximum_yaw_rate_deg_s=20.0,
        )
        target = waypoint(
            latitude=0.001,
            longitude=0.001,
            altitude=100.0,
            horizontal_speed=3.0,
            vertical_speed=1.0,
            maximum_yaw_rate=20.0,
        )
        store = running_store([target], bounds)
        tick = store.tick(telemetry(yaw=-170.0), NOW_MS)

        self.assertTrue(tick.command.is_active)
        self.assertLessEqual(
            math.hypot(tick.command.north_mps, tick.command.east_mps), 3.0 + 1e-9
        )
        self.assertLessEqual(abs(tick.command.down_mps), 1.0 + 1e-9)
        self.assertLessEqual(abs(tick.command.yaw_rate_deg_s), 20.0 + 1e-9)
        self.assertEqual(-1.0, tick.command.down_mps)

    def test_fixed_face_and_hold_yaw_use_shortest_bounded_angular_velocity(self):
        fixed = waypoint(
            latitude=0.001,
            yaw_mode=RouteYawMode.FIXED_HEADING,
            yaw=-170.0,
        )
        store = running_store([fixed])
        tick = store.tick(telemetry(yaw=170.0), NOW_MS)
        # Shortest error is +20 degrees; gain 1.5 reaches the default 30 deg/s cap.
        self.assertAlmostEqual(30.0, tick.command.yaw_rate_deg_s)

        face_east = waypoint(latitude=0.0, longitude=0.001)
        store = running_store([face_east])
        tick = store.tick(telemetry(yaw=0.0), NOW_MS)
        self.assertAlmostEqual(30.0, tick.command.yaw_rate_deg_s)

        hold = waypoint(
            latitude=0.0,
            longitude=0.001,
            yaw_mode=RouteYawMode.HOLD_HEADING,
        )
        store = running_store([hold])
        tick = store.tick(telemetry(yaw=123.0), NOW_MS)
        self.assertEqual(0.0, tick.command.yaw_rate_deg_s)

    def test_target_too_far_is_neutral_and_does_not_advance_state(self):
        bounds = RouteBounds(maximum_distance_to_target_m=10.0)
        store = running_store([waypoint(latitude=0.001)], bounds)
        before = store.snapshot()
        tick = store.tick(telemetry(), NOW_MS)
        self.assertIs(RouteCommandReason.TARGET_TOO_FAR, tick.command.reason)
        self.assertIs(before, tick.state)
        assert_neutral(self, tick.command)

    def test_invalid_execution_state_never_emits_motion(self):
        plan = RoutePlan("route-a", 1, (waypoint(),))
        state = RouteExecutionState(
            plan,
            RouteBounds(),
            RoutePhase.RUNNING,
            99,
        )
        tick = tick_route(state, telemetry(), NOW_MS)
        self.assertIs(RouteCommandReason.INVALID_STATE, tick.command.reason)
        assert_neutral(self, tick.command)


class GroundToBodyConversionTest(unittest.TestCase):
    def test_cardinal_headings_rotate_ned_into_forward_right(self):
        north = NedVelocityCommand(1.0, 0.0, -0.5, 10.0, RouteCommandReason.ACTIVE)

        facing_north = ground_ned_to_body(north, 0.0)
        self.assertEqual(1.0, facing_north.forward_mps)
        self.assertEqual(0.0, facing_north.right_mps)
        self.assertEqual(0.5, facing_north.up_mps)
        self.assertEqual(10.0, facing_north.yaw_rate_deg_s)

        facing_east = ground_ned_to_body(north, 90.0)
        self.assertEqual(0.0, facing_east.forward_mps)
        self.assertAlmostEqual(-1.0, facing_east.right_mps)

        east = NedVelocityCommand(0.0, 1.0, 0.0, 0.0, RouteCommandReason.ACTIVE)
        facing_east = ground_ned_to_body(east, 90.0)
        self.assertAlmostEqual(1.0, facing_east.forward_mps)
        self.assertEqual(0.0, facing_east.right_mps)

        facing_west = ground_ned_to_body(north, -90.0)
        self.assertEqual(0.0, facing_west.forward_mps)
        self.assertAlmostEqual(1.0, facing_west.right_mps)

    def test_arbitrary_rotation_preserves_horizontal_magnitude_and_reason(self):
        command = NedVelocityCommand(2.0, -3.0, 1.0, -12.0, RouteCommandReason.ACTIVE)
        body = ground_ned_to_body(command, 37.5)
        self.assertAlmostEqual(
            math.hypot(command.north_mps, command.east_mps),
            math.hypot(body.forward_mps, body.right_mps),
            places=12,
        )
        self.assertEqual(-1.0, body.up_mps)
        self.assertEqual(-12.0, body.yaw_rate_deg_s)
        self.assertIs(RouteCommandReason.ACTIVE, body.reason)

    def test_route_tick_converts_using_same_fresh_yaw_sample(self):
        store = running_store([waypoint(latitude=0.001)])
        sample = telemetry(yaw=90.0)
        tick = store.tick(sample, NOW_MS)
        body = ground_ned_to_body(tick.command, sample.yaw_deg)

        self.assertGreater(tick.command.north_mps, 0.0)
        self.assertEqual(0.0, body.forward_mps)
        self.assertLess(body.right_mps, 0.0)

    def test_rejects_invalid_command_or_yaw(self):
        command = NedVelocityCommand.neutral(RouteCommandReason.PAUSED)
        with self.assertRaisesRegex(ValueError, "yaw_deg"):
            ground_ned_to_body(command, math.nan)
        with self.assertRaisesRegex(TypeError, "NedVelocityCommand"):
            ground_ned_to_body({}, 0.0)


def valid_document():
    return {
        "schema": BRIDGE_ROUTE_SCHEMA,
        "engine": BRIDGE_ROUTE_ENGINE,
        "expected_accepted_revision": None,
        "activation": "immediate",
        "scope": "remaining_route_from_current_state",
        "plan": {
            "route_id": "route-a",
            "revision": 1,
            "waypoints": [{
                "latitude_deg": 0.0,
                "longitude_deg": 0.0,
                "altitude_m": 10.0,
            }],
        },
    }


def request(
    expected,
    revision,
    waypoints,
    activation=RouteReplacementMode.IMMEDIATE,
    scope=RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE,
    engine=BRIDGE_ROUTE_ENGINE,
):
    return RouteRevisionRequest(
        schema=BRIDGE_ROUTE_SCHEMA,
        engine=engine,
        expected_accepted_revision=expected,
        activation=activation,
        scope=scope,
        plan=RoutePlan("route-a", revision, waypoints),
    )


def waypoint(
    latitude=0.001,
    longitude=0.0,
    altitude=10.0,
    horizontal_speed=2.0,
    vertical_speed=1.0,
    yaw_mode=RouteYawMode.FACE_WAYPOINT,
    yaw=None,
    maximum_yaw_rate=30.0,
):
    return RouteWaypoint(
        latitude_deg=latitude,
        longitude_deg=longitude,
        altitude_m=altitude,
        horizontal_speed_mps=horizontal_speed,
        vertical_speed_mps=vertical_speed,
        yaw_mode=yaw_mode,
        yaw_deg=yaw,
        maximum_yaw_rate_deg_s=maximum_yaw_rate,
    )


def telemetry(
    latitude=0.0,
    longitude=0.0,
    altitude=10.0,
    yaw=0.0,
    sample_time=NOW_MS,
):
    return RouteTelemetry(latitude, longitude, altitude, yaw, sample_time)


def running_store(waypoints, bounds=RouteBounds()):
    store = AtomicRouteRevisionStore(bounds)
    result = store.accept(request(None, 1, waypoints))
    if not result.accepted:
        raise AssertionError(result.to_dict())
    if not store.start().accepted:
        raise AssertionError("route did not start")
    return store


def assert_neutral(test_case, command):
    test_case.assertEqual(0.0, command.north_mps)
    test_case.assertEqual(0.0, command.east_mps)
    test_case.assertEqual(0.0, command.down_mps)
    test_case.assertEqual(0.0, command.yaw_rate_deg_s)
    test_case.assertFalse(command.is_active)


if __name__ == "__main__":
    unittest.main()
