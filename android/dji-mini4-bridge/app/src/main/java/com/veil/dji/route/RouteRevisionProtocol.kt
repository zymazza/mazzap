package com.veil.dji.route

import java.util.concurrent.atomic.AtomicReference
import java.util.Collections

/** Wire-level identity for the custom route protocol. It is not a DJI mission format. */
const val BRIDGE_ROUTE_SCHEMA = "veil.route-revision.v1"
const val BRIDGE_ROUTE_ENGINE = "bridge_virtual_stick"

/**
 * Truthful capability metadata shared by protocol adapters. Route execution belongs to the
 * persistent Mac control session; the Android bridge intentionally has no route endpoint or
 * aircraft-resident/native mission adapter.
 */
data class RouteProtocolCapabilities(
    val schema: String = BRIDGE_ROUTE_SCHEMA,
    val routeEngine: String = BRIDGE_ROUTE_ENGINE,
    val executionOwner: String = "mac_persistent_session",
    val revisionAcceptance: Boolean = true,
    val midFlightReplacement: Boolean = true,
    val androidRouteEndpoint: Boolean = false,
    val nativeWaypointExecution: Boolean = false,
    val flyLibraryInterop: Boolean = false,
    val aircraftResidentRoute: Boolean = false,
    val unsupportedActions: Map<String, String> = Collections.unmodifiableMap(
        linkedMapOf(
            "native_waypoint_upload" to "Mini 4 Pro does not expose native waypoint execution through MSDK 5.18",
            "native_waypoint_start" to "Mini 4 Pro does not expose native waypoint execution through MSDK 5.18",
            "dji_fly_import" to "DJI Fly route-library interoperability is not exposed through MSDK",
            "dji_fly_export" to "DJI Fly route-library interoperability is not exposed through MSDK",
        ),
    ),
)

val MINI_4_ROUTE_CAPABILITIES = RouteProtocolCapabilities()

/** Primitive-only adapter with stable snake-case names for a host API/status document. */
fun RouteProtocolCapabilities.toWireMap(): Map<String, Any> = Collections.unmodifiableMap(
    linkedMapOf(
        "schema" to schema,
        "route_engine" to routeEngine,
        "execution_owner" to executionOwner,
        "revision_acceptance" to revisionAcceptance,
        "mid_flight_replacement" to midFlightReplacement,
        "android_route_endpoint" to androidRouteEndpoint,
        "native_waypoint_execution" to nativeWaypointExecution,
        "fly_library_interop" to flyLibraryInterop,
        "aircraft_resident_route" to aircraftResidentRoute,
        "unsupported_actions" to unsupportedActions,
    ),
)

/**
 * Optimistic-concurrency envelope for accepting an immutable route revision.
 *
 * [expectedAcceptedRevision] is the newest revision the caller has observed, including a staged
 * boundary revision. It must be null for the first accepted plan and must match exactly for every
 * replacement. This prevents two simultaneous replans from silently overwriting one another.
 */
data class RouteRevisionRequest(
    val schema: String,
    val engine: String,
    val expectedAcceptedRevision: Long?,
    val activation: RouteReplacementMode,
    val scope: RouteReplacementScope,
    val plan: RoutePlan,
)

enum class RouteRevisionAcceptanceStatus {
    ACCEPTED,
    INVALID,
    REVISION_CONFLICT,
    UNSUPPORTED,
}

data class RouteRevisionAcceptance(
    val status: RouteRevisionAcceptanceStatus,
    val state: RouteExecutionState?,
    val acceptedRevision: Long?,
    val issues: List<RouteValidationIssue> = emptyList(),
) {
    val accepted: Boolean
        get() = status == RouteRevisionAcceptanceStatus.ACCEPTED
}

/**
 * Lock-free atomic owner for the pure route state machine. It performs no I/O and never talks to
 * DJI. A host adapter may use [tick] to publish the state transition and corresponding command as
 * one compare-and-set operation while route revisions can arrive concurrently.
 */
class AtomicRouteRevisionStore(
    private val bounds: RouteSafetyBounds = RouteSafetyBounds(),
) {
    private val state = AtomicReference<RouteExecutionState?>(null)

    fun snapshot(): RouteExecutionState? = state.get()

    fun newestAcceptedRevision(): Long? = state.get()?.newestAcceptedRevision()

    fun accept(request: RouteRevisionRequest): RouteRevisionAcceptance {
        if (request.schema != BRIDGE_ROUTE_SCHEMA) {
            return unsupported("schema", "unsupported route schema: ${request.schema}")
        }
        if (request.engine != BRIDGE_ROUTE_ENGINE) {
            return unsupported("engine", "unsupported route engine: ${request.engine}")
        }

        while (true) {
            val before = state.get()
            val newestBefore = before?.newestAcceptedRevision()
            if (request.expectedAcceptedRevision != newestBefore) {
                return RouteRevisionAcceptance(
                    status = RouteRevisionAcceptanceStatus.REVISION_CONFLICT,
                    state = before,
                    acceptedRevision = newestBefore,
                    issues = listOf(
                        RouteValidationIssue(
                            "expectedAcceptedRevision",
                            "expected ${request.expectedAcceptedRevision}, current is $newestBefore",
                        ),
                    ),
                )
            }

            val proposed = if (before == null) {
                val prepared = RouteExecutor.prepare(request.plan, bounds)
                if (!prepared.accepted) {
                    return RouteRevisionAcceptance(
                        RouteRevisionAcceptanceStatus.INVALID,
                        null,
                        null,
                        prepared.validation.issues,
                    )
                }
                requireNotNull(prepared.state)
            } else {
                val replacement = RouteExecutor.replace(
                    before,
                    request.plan,
                    request.activation,
                    request.scope,
                )
                if (!replacement.accepted) {
                    return RouteRevisionAcceptance(
                        RouteRevisionAcceptanceStatus.INVALID,
                        before,
                        newestBefore,
                        replacement.validation.issues,
                    )
                }
                replacement.state
            }

            if (state.compareAndSet(before, proposed)) {
                return RouteRevisionAcceptance(
                    RouteRevisionAcceptanceStatus.ACCEPTED,
                    proposed,
                    proposed.newestAcceptedRevision(),
                )
            }
            // A concurrent writer won. Re-evaluate the caller's expected revision against it.
        }
    }

    fun start(): RouteStateChange? = transition(RouteExecutor::start)

    fun pause(): RouteStateChange? = transition(RouteExecutor::pause)

    fun resume(): RouteStateChange? = transition(RouteExecutor::resume)

    fun abort(): RouteStateChange? = transition(RouteExecutor::abort)

    /** Atomically publishes both route progress and the command derived from that exact state. */
    fun tick(telemetry: RouteTelemetry, nowMillis: Long): RouteTickResult? {
        while (true) {
            val before = state.get() ?: return null
            val result = RouteExecutor.tick(before, telemetry, nowMillis)
            if (result.state === before || result.state == before) {
                // A no-op CAS is still required: without it, a concurrent replan could publish
                // between the read and return, causing one stale-revision command to escape.
                if (state.compareAndSet(before, before)) return result
            } else if (state.compareAndSet(before, result.state)) {
                return result
            }
        }
    }

    private fun transition(transform: (RouteExecutionState) -> RouteStateChange): RouteStateChange? {
        while (true) {
            val before = state.get() ?: return null
            val result = transform(before)
            if (!result.accepted || result.state === before || result.state == before) return result
            if (state.compareAndSet(before, result.state)) return result
        }
    }

    private fun unsupported(path: String, message: String): RouteRevisionAcceptance {
        val current = state.get()
        return RouteRevisionAcceptance(
            status = RouteRevisionAcceptanceStatus.UNSUPPORTED,
            state = current,
            acceptedRevision = current?.newestAcceptedRevision(),
            issues = listOf(RouteValidationIssue(path, message)),
        )
    }

    private fun RouteExecutionState.newestAcceptedRevision(): Long =
        maxOf(activePlan.revision, pendingPlan?.revision ?: Long.MIN_VALUE)
}
