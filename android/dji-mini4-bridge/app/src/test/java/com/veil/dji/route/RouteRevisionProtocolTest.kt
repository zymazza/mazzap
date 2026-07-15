package com.veil.dji.route

import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RouteRevisionProtocolTest {
    @Test
    fun capabilityMetadataDoesNotClaimDjiOrAndroidRouteExecution() {
        val capabilities = MINI_4_ROUTE_CAPABILITIES
        assertEquals("bridge_virtual_stick", capabilities.routeEngine)
        assertEquals("mac_persistent_session", capabilities.executionOwner)
        assertTrue(capabilities.revisionAcceptance)
        assertTrue(capabilities.midFlightReplacement)
        assertFalse(capabilities.androidRouteEndpoint)
        assertFalse(capabilities.nativeWaypointExecution)
        assertFalse(capabilities.flyLibraryInterop)
        assertFalse(capabilities.aircraftResidentRoute)
        assertTrue("native_waypoint_start" in capabilities.unsupportedActions)
        assertTrue("dji_fly_import" in capabilities.unsupportedActions)

        val wire = capabilities.toWireMap()
        assertEquals("bridge_virtual_stick", wire["route_engine"])
        assertEquals(false, wire["native_waypoint_execution"])
        assertEquals(false, wire["fly_library_interop"])
        assertEquals(false, wire["android_route_endpoint"])
    }

    @Test
    fun acceptanceSnapshotsMutableInputAndRequiresExactExpectedRevision() {
        val mutableWaypoints = mutableListOf(waypoint(latitude = 0.001))
        val store = AtomicRouteRevisionStore()
        val first = store.accept(request(null, 1, mutableWaypoints))
        assertTrue(first.accepted)
        assertEquals(1L, first.acceptedRevision)

        mutableWaypoints[0] = waypoint(latitude = -0.001)
        mutableWaypoints += waypoint(latitude = -0.002)
        val snapshot = requireNotNull(store.snapshot())
        assertNotSame(mutableWaypoints, snapshot.activePlan.waypoints)
        assertEquals(1, snapshot.activePlan.waypoints.size)
        assertEquals(0.001, snapshot.activePlan.waypoints[0].latitudeDegrees, 0.0)

        val missingExpected = store.accept(request(null, 2, listOf(waypoint(latitude = 0.002))))
        assertEquals(RouteRevisionAcceptanceStatus.REVISION_CONFLICT, missingExpected.status)
        assertEquals(1L, store.newestAcceptedRevision())

        val accepted = store.accept(request(1, 2, listOf(waypoint(latitude = 0.002))))
        assertTrue(accepted.accepted)
        assertEquals(2L, store.newestAcceptedRevision())
    }

    @Test
    fun simultaneousWritersCannotBothReplaceTheSameRevision() {
        val store = AtomicRouteRevisionStore()
        assertTrue(store.accept(request(null, 1, listOf(waypoint()))).accepted)
        val ready = CountDownLatch(2)
        val release = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(2)
        val futures = listOf(2L, 3L).map { revision ->
            pool.submit<RouteRevisionAcceptance> {
                ready.countDown()
                assertTrue(release.await(2, TimeUnit.SECONDS))
                store.accept(request(1, revision, listOf(waypoint(latitude = revision * 0.001))))
            }
        }
        assertTrue(ready.await(2, TimeUnit.SECONDS))
        release.countDown()
        val results = futures.map { it.get(2, TimeUnit.SECONDS) }
        pool.shutdownNow()

        assertEquals(1, results.count { it.status == RouteRevisionAcceptanceStatus.ACCEPTED })
        assertEquals(1, results.count { it.status == RouteRevisionAcceptanceStatus.REVISION_CONFLICT })
        assertTrue(store.newestAcceptedRevision() in setOf(2L, 3L))
    }

    @Test
    fun stagedRevisionIsTheConcurrencyHeadAndActivatesAtDeclaredTarget() {
        val store = AtomicRouteRevisionStore()
        val firstPlan = listOf(
            waypoint(latitude = 0.0),
            waypoint(latitude = 0.001),
            waypoint(latitude = 0.002),
        )
        assertTrue(store.accept(request(null, 1, firstPlan)).accepted)
        assertTrue(requireNotNull(store.start()).accepted)
        val firstBoundary = requireNotNull(store.tick(telemetry(latitude = 0.0), NOW))
        assertEquals(1, firstBoundary.state.targetWaypointIndex)

        val stagedRequest = request(
            expected = 1,
            revision = 2,
            waypoints = listOf(
                waypoint(latitude = 0.0),
                waypoint(latitude = 0.001),
                waypoint(latitude = 0.003),
            ),
            activation = RouteReplacementMode.AT_WAYPOINT_BOUNDARY,
            scope = RouteReplacementScope.FULL_ROUTE_CONTINUE,
        )
        val staged = store.accept(stagedRequest)
        assertTrue(staged.accepted)
        assertEquals(1L, requireNotNull(staged.state).activePlan.revision)
        assertEquals(2L, staged.state.pendingPlan?.revision)
        assertEquals(2L, store.newestAcceptedRevision())

        val staleWriter = store.accept(request(1, 3, listOf(waypoint(latitude = 0.004))))
        assertEquals(RouteRevisionAcceptanceStatus.REVISION_CONFLICT, staleWriter.status)

        val activation = requireNotNull(store.tick(telemetry(latitude = 0.001), NOW))
        assertEquals(RouteCommandReason.PLAN_REPLACED, activation.command.reason)
        assertEquals(2L, activation.state.activePlan.revision)
        assertEquals(2, activation.state.targetWaypointIndex)
        assertNull(activation.state.pendingPlan)
    }

    @Test
    fun unsupportedEngineIsExplicitAndDoesNotMutateState() {
        val store = AtomicRouteRevisionStore()
        val unsupported = store.accept(
            request(null, 1, listOf(waypoint())).copy(engine = "native_dji_waypoint"),
        )
        assertEquals(RouteRevisionAcceptanceStatus.UNSUPPORTED, unsupported.status)
        assertEquals("engine", unsupported.issues.single().path)
        assertNull(store.snapshot())
    }

    @Test
    fun terminalRouteReplacementIsRejectedWithExplicitPhaseIssue() {
        val store = AtomicRouteRevisionStore()
        assertTrue(store.accept(request(null, 1, listOf(waypoint()))).accepted)
        assertTrue(requireNotNull(store.abort()).accepted)

        val replacement = store.accept(request(1, 2, listOf(waypoint(latitude = 0.002))))
        assertEquals(RouteRevisionAcceptanceStatus.INVALID, replacement.status)
        assertEquals("phase", replacement.issues.single().path)
        assertTrue(replacement.issues.single().message.contains("aborted"))
        assertEquals(1L, store.newestAcceptedRevision())
    }

    private fun request(
        expected: Long?,
        revision: Long,
        waypoints: List<RouteWaypoint>,
        activation: RouteReplacementMode = RouteReplacementMode.IMMEDIATE,
        scope: RouteReplacementScope = RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE,
    ) = RouteRevisionRequest(
        schema = BRIDGE_ROUTE_SCHEMA,
        engine = BRIDGE_ROUTE_ENGINE,
        expectedAcceptedRevision = expected,
        activation = activation,
        scope = scope,
        plan = RoutePlan("route-a", revision, waypoints),
    )

    private fun waypoint(latitude: Double = 0.001) = RouteWaypoint(
        latitudeDegrees = latitude,
        longitudeDegrees = 0.0,
        altitudeMeters = 10.0,
    )

    private fun telemetry(latitude: Double) = RouteTelemetry(
        latitudeDegrees = latitude,
        longitudeDegrees = 0.0,
        altitudeMeters = 10.0,
        yawDegrees = 0.0,
        sampleTimeMillis = NOW,
    )

    private companion object {
        const val NOW = 10_000L
    }
}
