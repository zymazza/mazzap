package com.veil.dji.route

import kotlin.math.abs
import kotlin.math.hypot
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class RouteExecutorTest {
    @Test
    fun requiresExplicitStartThenProgressesAndCompletes() {
        val plan = plan(
            revision = 1,
            waypoint(latitude = 0.0, altitude = 10.0),
            waypoint(latitude = 0.0001, altitude = 10.0),
        )
        var state = requireNotNull(RouteExecutor.prepare(plan).state)

        val beforeStart = RouteExecutor.tick(state, telemetry(latitude = 0.0, altitude = 10.0), NOW)
        assertEquals(RouteCommandReason.NOT_STARTED, beforeStart.command.reason)
        assertNeutral(beforeStart.command)

        state = RouteExecutor.start(state).state
        val firstBoundary = RouteExecutor.tick(state, telemetry(latitude = 0.0, altitude = 10.0), NOW)
        assertEquals(RouteCommandReason.WAYPOINT_ADVANCED, firstBoundary.command.reason)
        assertEquals(1, firstBoundary.state.targetWaypointIndex)
        assertNeutral(firstBoundary.command)

        val moving = RouteExecutor.tick(firstBoundary.state, telemetry(latitude = 0.0, altitude = 10.0), NOW)
        assertTrue(moving.command.isActive)
        assertTrue(moving.command.northMetersPerSecond > 0.0)
        assertEquals(0.0, moving.command.eastMetersPerSecond, 1e-6)

        val completed = RouteExecutor.tick(
            moving.state,
            telemetry(latitude = 0.0001, altitude = 10.0),
            NOW,
        )
        assertEquals(RoutePhase.COMPLETED, completed.state.phase)
        assertEquals(RouteCommandReason.COMPLETED, completed.command.reason)
        assertNeutral(completed.command)
    }

    @Test
    fun pauseResumeAndAbortAlwaysHoldNeutralWhenNotRunning() {
        val prepared = requireNotNull(
            RouteExecutor.prepare(plan(1, waypoint(latitude = 0.001, altitude = 10.0))).state,
        )
        val running = RouteExecutor.start(prepared).state
        val pausedChange = RouteExecutor.pause(running)
        assertTrue(pausedChange.accepted)
        val paused = RouteExecutor.tick(pausedChange.state, telemetry(), NOW)
        assertEquals(RouteCommandReason.PAUSED, paused.command.reason)
        assertNeutral(paused.command)

        val resumedChange = RouteExecutor.resume(paused.state)
        assertTrue(resumedChange.accepted)
        assertTrue(RouteExecutor.tick(resumedChange.state, telemetry(), NOW).command.isActive)

        val abortedChange = RouteExecutor.abort(resumedChange.state)
        assertTrue(abortedChange.accepted)
        val aborted = RouteExecutor.tick(abortedChange.state, telemetry(), NOW)
        assertEquals(RoutePhase.ABORTED, aborted.state.phase)
        assertEquals(RouteCommandReason.ABORTED, aborted.command.reason)
        assertNeutral(aborted.command)
        assertFalse(RouteExecutor.resume(aborted.state).accepted)
    }

    @Test
    fun validatesEveryNumericInputAndRejectsUnsafeBounds() {
        val invalid = RoutePlan(
            routeId = "",
            revision = -1,
            waypoints = listOf(
                RouteWaypoint(
                    latitudeDegrees = Double.NaN,
                    longitudeDegrees = 181.0,
                    altitudeMeters = Double.POSITIVE_INFINITY,
                    horizontalSpeedMetersPerSecond = 0.0,
                    verticalSpeedMetersPerSecond = -1.0,
                    horizontalToleranceMeters = 0.0,
                    verticalToleranceMeters = -0.1,
                    yawMode = RouteYawMode.FIXED_HEADING,
                    yawDegrees = null,
                    maximumYawRateDegreesPerSecond = 0.0,
                ),
            ),
        )
        val result = RouteExecutor.prepare(
            invalid,
            RouteSafetyBounds(maximumHorizontalSpeedMetersPerSecond = 24.0),
        )

        assertFalse(result.accepted)
        assertNull(result.state)
        val paths = result.validation.issues.map { it.path }.toSet()
        assertTrue("routeId" in paths)
        assertTrue("revision" in paths)
        assertTrue("waypoints[0].latitudeDegrees" in paths)
        assertTrue("waypoints[0].longitudeDegrees" in paths)
        assertTrue("waypoints[0].altitudeMeters" in paths)
        assertTrue("waypoints[0].horizontalSpeedMetersPerSecond" in paths)
        assertTrue("waypoints[0].verticalSpeedMetersPerSecond" in paths)
        assertTrue("waypoints[0].horizontalToleranceMeters" in paths)
        assertTrue("waypoints[0].verticalToleranceMeters" in paths)
        assertTrue("waypoints[0].yawDegrees" in paths)
        assertTrue("bounds.maximumHorizontalSpeedMetersPerSecond" in paths)
    }

    @Test
    fun replacementIsAtomicImmediateOrAtBoundaryAndRevisionOrdered() {
        val original = plan(1, waypoint(latitude = 0.001, altitude = 10.0))
        var state = RouteExecutor.start(requireNotNull(RouteExecutor.prepare(original).state)).state

        val badReplacement = plan(
            revision = 2,
            waypoint(latitude = 0.002, altitude = 10.0).copy(horizontalSpeedMetersPerSecond = -1.0),
        )
        val rejected = RouteExecutor.replace(state, badReplacement, RouteReplacementMode.IMMEDIATE)
        assertFalse(rejected.accepted)
        assertSame(state, rejected.state)

        val boundaryPlan = plan(2, waypoint(latitude = 0.002, altitude = 10.0))
        val staged = RouteExecutor.replace(
            state,
            boundaryPlan,
            RouteReplacementMode.AT_WAYPOINT_BOUNDARY,
            RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE,
        )
        assertTrue(staged.accepted)
        assertEquals(1L, staged.state.activePlan.revision)
        assertEquals(2L, staged.state.pendingPlan?.revision)

        val staleRevision = RouteExecutor.replace(staged.state, plan(2, waypoint()), RouteReplacementMode.IMMEDIATE)
        assertFalse(staleRevision.accepted)
        assertEquals(staged.state, staleRevision.state)

        val boundary = RouteExecutor.tick(
            staged.state,
            telemetry(latitude = 0.001, altitude = 10.0),
            NOW,
        )
        assertEquals(RouteCommandReason.PLAN_REPLACED, boundary.command.reason)
        assertEquals(2L, boundary.state.activePlan.revision)
        assertNull(boundary.state.pendingPlan)
        assertEquals(0, boundary.state.targetWaypointIndex)
        assertNeutral(boundary.command)

        val immediatePlan = plan(3, waypoint(latitude = -0.001, altitude = 10.0))
        val immediate = RouteExecutor.replace(boundary.state, immediatePlan, RouteReplacementMode.IMMEDIATE)
        assertTrue(immediate.accepted)
        assertEquals(3L, immediate.state.activePlan.revision)
        val moving = RouteExecutor.tick(
            immediate.state,
            telemetry(latitude = 0.0, altitude = 10.0),
            NOW,
        )
        assertTrue(moving.command.northMetersPerSecond < 0.0)
    }

    @Test
    fun fullRouteReplacementPreservesProgressInsteadOfReturningToWaypointZero() {
        val original = plan(
            1,
            waypoint(latitude = 0.0, altitude = 10.0),
            waypoint(latitude = 0.001, altitude = 10.0),
            waypoint(latitude = 0.002, altitude = 10.0),
        )
        var state = RouteExecutor.start(requireNotNull(RouteExecutor.prepare(original).state)).state
        state = RouteExecutor.tick(
            state,
            telemetry(latitude = 0.0, altitude = 10.0),
            NOW,
        ).state
        assertEquals(1, state.targetWaypointIndex)

        val revised = plan(
            2,
            waypoint(latitude = 0.0, altitude = 10.0),
            waypoint(latitude = 0.0015, altitude = 10.0),
            waypoint(latitude = 0.0025, altitude = 10.0),
        )
        val replacement = RouteExecutor.replace(
            state,
            revised,
            RouteReplacementMode.IMMEDIATE,
            RouteReplacementScope.FULL_ROUTE_CONTINUE,
        )

        assertTrue(replacement.accepted)
        assertEquals(RoutePhase.RUNNING, replacement.state.phase)
        assertEquals(1, replacement.state.targetWaypointIndex)
        val command = RouteExecutor.tick(
            replacement.state,
            telemetry(latitude = 0.001, altitude = 10.0),
            NOW,
        ).command
        assertTrue(command.isActive)
        assertTrue("must continue north toward revised current target", command.northMetersPerSecond > 0.0)
    }

    @Test
    fun fullRouteBoundaryReplacementContinuesAtFollowingIndexAtomically() {
        val original = plan(
            1,
            waypoint(latitude = 0.0, altitude = 10.0),
            waypoint(latitude = 0.001, altitude = 10.0),
            waypoint(latitude = 0.002, altitude = 10.0),
        )
        var state = RouteExecutor.start(requireNotNull(RouteExecutor.prepare(original).state)).state
        state = RouteExecutor.tick(
            state,
            telemetry(latitude = 0.0, altitude = 10.0),
            NOW,
        ).state

        val revised = plan(
            2,
            waypoint(latitude = 0.0, altitude = 10.0),
            waypoint(latitude = 0.001, altitude = 10.0),
            waypoint(latitude = 0.0025, altitude = 10.0),
        )
        val staged = RouteExecutor.replace(
            state,
            revised,
            RouteReplacementMode.AT_WAYPOINT_BOUNDARY,
            RouteReplacementScope.FULL_ROUTE_CONTINUE,
        )
        assertTrue(staged.accepted)
        assertEquals(2, staged.state.pendingTargetWaypointIndex)

        val boundary = RouteExecutor.tick(
            staged.state,
            telemetry(latitude = 0.001, altitude = 10.0),
            NOW,
        )
        assertEquals(RouteCommandReason.PLAN_REPLACED, boundary.command.reason)
        assertEquals(2L, boundary.state.activePlan.revision)
        assertEquals(2, boundary.state.targetWaypointIndex)
        assertNull(boundary.state.pendingPlan)
        assertNull(boundary.state.pendingTargetWaypointIndex)
        assertNeutral(boundary.command)
    }

    @Test
    fun remainingRouteReplacementStartsAtZeroFromFreshCurrentTelemetry() {
        val original = plan(
            1,
            waypoint(latitude = 0.0, altitude = 10.0),
            waypoint(latitude = 0.001, altitude = 10.0),
        )
        var state = RouteExecutor.start(requireNotNull(RouteExecutor.prepare(original).state)).state
        state = RouteExecutor.tick(state, telemetry(latitude = 0.0, altitude = 10.0), NOW).state
        assertEquals(1, state.targetWaypointIndex)

        val remaining = plan(2, waypoint(latitude = 0.0012, altitude = 10.0))
        val replaced = RouteExecutor.replace(
            state,
            remaining,
            RouteReplacementMode.IMMEDIATE,
            RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE,
        )
        assertTrue(replaced.accepted)
        assertEquals(0, replaced.state.targetWaypointIndex)

        val tick = RouteExecutor.tick(
            replaced.state,
            telemetry(latitude = 0.001, altitude = 10.0),
            NOW,
        )
        assertTrue(tick.command.isActive)
        assertTrue(tick.command.northMetersPerSecond > 0.0)
    }

    @Test
    fun staleOrInvalidTelemetryIsNeutralAndFreshTelemetryRecovers() {
        val state = RouteExecutor.start(
            requireNotNull(RouteExecutor.prepare(plan(1, waypoint(latitude = 0.001, altitude = 10.0))).state),
        ).state

        val stale = RouteExecutor.tick(state, telemetry(sampleTime = NOW - 501), NOW)
        assertEquals(RouteCommandReason.STALE_TELEMETRY, stale.command.reason)
        assertNeutral(stale.command)

        val future = RouteExecutor.tick(state, telemetry(sampleTime = NOW + 101), NOW)
        assertEquals(RouteCommandReason.STALE_TELEMETRY, future.command.reason)
        assertNeutral(future.command)

        val invalid = RouteExecutor.tick(state, telemetry().copy(yawDegrees = Double.NaN), NOW)
        assertEquals(RouteCommandReason.INVALID_TELEMETRY, invalid.command.reason)
        assertNeutral(invalid.command)

        val fresh = RouteExecutor.tick(state, telemetry(), NOW)
        assertTrue(fresh.command.isActive)
        assertSame(state, fresh.state)
    }

    @Test
    fun activeOutputsStayWithinAllConfiguredBounds() {
        val bounds = RouteSafetyBounds(
            maximumHorizontalSpeedMetersPerSecond = 3.0,
            maximumVerticalSpeedMetersPerSecond = 1.0,
            maximumYawRateDegreesPerSecond = 20.0,
        )
        val target = waypoint(latitude = 0.001, longitude = 0.001, altitude = 100.0).copy(
            horizontalSpeedMetersPerSecond = 3.0,
            verticalSpeedMetersPerSecond = 1.0,
            maximumYawRateDegreesPerSecond = 20.0,
        )
        val state = RouteExecutor.start(requireNotNull(RouteExecutor.prepare(plan(1, target), bounds).state)).state
        val tick = RouteExecutor.tick(state, telemetry(yaw = -170.0), NOW)

        assertTrue(tick.command.isActive)
        assertTrue(hypot(tick.command.northMetersPerSecond, tick.command.eastMetersPerSecond) <= 3.0 + 1e-9)
        assertTrue(abs(tick.command.downMetersPerSecond) <= 1.0 + 1e-9)
        assertTrue(abs(tick.command.yawRateDegreesPerSecond) <= 20.0 + 1e-9)
        assertEquals(-1.0, tick.command.downMetersPerSecond, 1e-9)

        val fixed = RouteExecutor.prepare(
            plan(1, target.copy(yawMode = RouteYawMode.FIXED_HEADING, yawDegrees = 180.0)),
            bounds,
        )
        assertNotNull(fixed.state)
        val fixedTick = RouteExecutor.tick(RouteExecutor.start(requireNotNull(fixed.state)).state, telemetry(), NOW)
        assertEquals(RouteYawCommandMode.ANGLE_SETPOINT, fixedTick.command.yawMode)
        assertEquals(-180.0, requireNotNull(fixedTick.command.yawSetpointDegrees), 0.0)
        assertEquals(0.0, fixedTick.command.yawRateDegreesPerSecond, 0.0)
    }

    private fun plan(revision: Long, vararg waypoints: RouteWaypoint): RoutePlan =
        RoutePlan("route-a", revision, waypoints.toList())

    private fun waypoint(
        latitude: Double = 0.001,
        longitude: Double = 0.0,
        altitude: Double = 10.0,
    ): RouteWaypoint = RouteWaypoint(
        latitudeDegrees = latitude,
        longitudeDegrees = longitude,
        altitudeMeters = altitude,
    )

    private fun telemetry(
        latitude: Double = 0.0,
        longitude: Double = 0.0,
        altitude: Double = 0.0,
        yaw: Double = 0.0,
        sampleTime: Long = NOW,
    ): RouteTelemetry = RouteTelemetry(latitude, longitude, altitude, yaw, sampleTime)

    private fun assertNeutral(command: RouteVelocityCommand) {
        assertEquals(0.0, command.northMetersPerSecond, 0.0)
        assertEquals(0.0, command.eastMetersPerSecond, 0.0)
        assertEquals(0.0, command.downMetersPerSecond, 0.0)
        assertEquals(0.0, command.yawRateDegreesPerSecond, 0.0)
        assertNull(command.yawSetpointDegrees)
        assertFalse(command.isActive)
    }

    private companion object {
        const val NOW = 10_000L
    }
}
