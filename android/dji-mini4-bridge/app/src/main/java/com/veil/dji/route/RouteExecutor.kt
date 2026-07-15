package com.veil.dji.route

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.min
import java.util.Collections

/**
 * Deterministic, side-effect-free route state machine for short routes.
 *
 * Navigation uses a local equirectangular NED approximation. It is appropriate for the validated
 * short route extents, not long-range/geodesic or polar navigation. This class does not account for
 * obstacles, wind, GNSS accuracy, geofences, Remote ID, battery state, link loss, or regulations.
 * It never talks to DJI hardware; a supervised adapter must stream accepted commands at the SDK's
 * required rate and add independent failsafes. Reaching a waypoint emits one neutral boundary tick.
 */
object RouteExecutor {
    private const val EARTH_RADIUS_METERS = 6_378_137.0

    fun prepare(
        plan: RoutePlan,
        bounds: RouteSafetyBounds = RouteSafetyBounds(),
    ): RoutePreparationResult {
        val snapshot = snapshot(plan)
        val validation = RoutePlanValidator.validate(snapshot, bounds)
        if (!validation.isValid) return RoutePreparationResult(null, validation)
        return RoutePreparationResult(
            state = RouteExecutionState(
                activePlan = snapshot,
                bounds = bounds.copy(),
                phase = RoutePhase.READY,
                targetWaypointIndex = 0,
            ),
            validation = validation,
        )
    }

    /** Explicitly arms execution. Preparing/loading alone can never produce an active command. */
    fun start(state: RouteExecutionState): RouteStateChange = if (state.phase == RoutePhase.READY) {
        RouteStateChange(state.copy(phase = RoutePhase.RUNNING), true)
    } else {
        RouteStateChange(state, false)
    }

    fun pause(state: RouteExecutionState): RouteStateChange = if (state.phase == RoutePhase.RUNNING) {
        RouteStateChange(state.copy(phase = RoutePhase.PAUSED), true)
    } else {
        RouteStateChange(state, false)
    }

    fun resume(state: RouteExecutionState): RouteStateChange = if (state.phase == RoutePhase.PAUSED) {
        RouteStateChange(state.copy(phase = RoutePhase.RUNNING), true)
    } else {
        RouteStateChange(state, false)
    }

    fun abort(state: RouteExecutionState): RouteStateChange = when (state.phase) {
        RoutePhase.READY, RoutePhase.RUNNING, RoutePhase.PAUSED -> RouteStateChange(
            state.copy(
                phase = RoutePhase.ABORTED,
                pendingPlan = null,
                pendingTargetWaypointIndex = null,
            ),
            true,
        )
        RoutePhase.COMPLETED, RoutePhase.ABORTED -> RouteStateChange(state, false)
    }

    /**
     * Validates and snapshots an entire replacement before changing state. Rejected replacements
     * leave [state] byte-for-byte equivalent. Revisions are strictly monotonic for a route ID.
     */
    fun replace(
        state: RouteExecutionState,
        replacement: RoutePlan,
        mode: RouteReplacementMode,
        scope: RouteReplacementScope = RouteReplacementScope.FULL_ROUTE_CONTINUE,
    ): RouteStateChange {
        if (state.phase == RoutePhase.COMPLETED || state.phase == RoutePhase.ABORTED) {
            return RouteStateChange(
                state,
                false,
                RouteValidationResult(
                    listOf(
                        RouteValidationIssue(
                            "phase",
                            "cannot replace a ${state.phase.name.lowercase()} route",
                        ),
                    ),
                ),
            )
        }
        val replacementSnapshot = snapshot(replacement)
        val issues = RoutePlanValidator.validate(replacementSnapshot, state.bounds).issues.toMutableList()
        if (replacementSnapshot.routeId != state.activePlan.routeId) {
            issues += RouteValidationIssue("routeId", "must match the active routeId")
        }
        val newestRevision = maxOf(state.activePlan.revision, state.pendingPlan?.revision ?: Long.MIN_VALUE)
        if (replacementSnapshot.revision <= newestRevision) {
            issues += RouteValidationIssue("revision", "must be newer than active and pending revisions")
        }
        val replacementTargetIndex = when (scope) {
            RouteReplacementScope.FULL_ROUTE_CONTINUE -> when (mode) {
                RouteReplacementMode.IMMEDIATE -> state.targetWaypointIndex
                RouteReplacementMode.AT_WAYPOINT_BOUNDARY -> state.targetWaypointIndex + 1
            }
            RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE -> 0
        }
        if (replacementTargetIndex !in replacementSnapshot.waypoints.indices) {
            issues += RouteValidationIssue(
                "waypoints",
                when (scope) {
                    RouteReplacementScope.FULL_ROUTE_CONTINUE ->
                        "must contain continuation target index $replacementTargetIndex"
                    RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE ->
                        "must contain a remaining-route target"
                },
            )
        }
        val validation = RouteValidationResult(issues)
        if (!validation.isValid) return RouteStateChange(state, false, validation)

        val next = when (mode) {
            RouteReplacementMode.IMMEDIATE -> state.copy(
                activePlan = replacementSnapshot,
                targetWaypointIndex = replacementTargetIndex,
                pendingPlan = null,
                pendingTargetWaypointIndex = null,
            )
            RouteReplacementMode.AT_WAYPOINT_BOUNDARY -> state.copy(
                pendingPlan = replacementSnapshot,
                pendingTargetWaypointIndex = replacementTargetIndex,
            )
        }
        return RouteStateChange(next, true, validation)
    }

    fun tick(
        state: RouteExecutionState,
        telemetry: RouteTelemetry,
        nowMillis: Long,
    ): RouteTickResult {
        val inactiveReason = when (state.phase) {
            RoutePhase.READY -> RouteCommandReason.NOT_STARTED
            RoutePhase.PAUSED -> RouteCommandReason.PAUSED
            RoutePhase.COMPLETED -> RouteCommandReason.COMPLETED
            RoutePhase.ABORTED -> RouteCommandReason.ABORTED
            RoutePhase.RUNNING -> null
        }
        if (inactiveReason != null) return neutral(state, inactiveReason)

        if (!validState(state)) return neutral(state, RouteCommandReason.INVALID_STATE)
        val telemetryIssue = validateTelemetry(telemetry, nowMillis, state.bounds)
        if (telemetryIssue != null) return neutral(state, telemetryIssue)

        val target = state.activePlan.waypoints[state.targetWaypointIndex]
        val offset = localOffset(telemetry, target)
        if (!offset.north.isFinite() || !offset.east.isFinite() || !offset.down.isFinite()) {
            return neutral(state, RouteCommandReason.INVALID_TELEMETRY)
        }
        val horizontalDistance = hypot(offset.north, offset.east)
        if (horizontalDistance > state.bounds.maximumDistanceToTargetMeters) {
            return RouteTickResult(
                state,
                RouteVelocityCommand.neutral(RouteCommandReason.TARGET_TOO_FAR),
                horizontalDistance,
                offset.down,
            )
        }

        val reached = horizontalDistance <= target.horizontalToleranceMeters &&
            abs(offset.down) <= target.verticalToleranceMeters
        if (reached) {
            val pending = state.pendingPlan
            if (pending != null) {
                val pendingTarget = state.pendingTargetWaypointIndex
                if (pendingTarget == null || pendingTarget !in pending.waypoints.indices) {
                    return neutral(state, RouteCommandReason.INVALID_STATE)
                }
                val replaced = state.copy(
                    activePlan = pending,
                    targetWaypointIndex = pendingTarget,
                    pendingPlan = null,
                    pendingTargetWaypointIndex = null,
                )
                return RouteTickResult(
                    replaced,
                    RouteVelocityCommand.neutral(RouteCommandReason.PLAN_REPLACED),
                    horizontalDistance,
                    offset.down,
                )
            }
            if (state.targetWaypointIndex == state.activePlan.waypoints.lastIndex) {
                val completed = state.copy(phase = RoutePhase.COMPLETED)
                return RouteTickResult(
                    completed,
                    RouteVelocityCommand.neutral(RouteCommandReason.COMPLETED),
                    horizontalDistance,
                    offset.down,
                )
            }
            val advanced = state.copy(targetWaypointIndex = state.targetWaypointIndex + 1)
            return RouteTickResult(
                advanced,
                RouteVelocityCommand.neutral(RouteCommandReason.WAYPOINT_ADVANCED),
                horizontalDistance,
                offset.down,
            )
        }

        val horizontalMaximum = min(
            target.horizontalSpeedMetersPerSecond,
            state.bounds.maximumHorizontalSpeedMetersPerSecond,
        )
        val horizontalMagnitude = if (horizontalDistance <= target.horizontalToleranceMeters) {
            0.0
        } else {
            min(horizontalMaximum, horizontalDistance * state.bounds.horizontalProportionalGainPerSecond)
        }
        val north = if (horizontalDistance == 0.0) 0.0 else offset.north / horizontalDistance * horizontalMagnitude
        val east = if (horizontalDistance == 0.0) 0.0 else offset.east / horizontalDistance * horizontalMagnitude

        val verticalMaximum = min(
            target.verticalSpeedMetersPerSecond,
            state.bounds.maximumVerticalSpeedMetersPerSecond,
        )
        val down = if (abs(offset.down) <= target.verticalToleranceMeters) {
            0.0
        } else {
            (offset.down * state.bounds.verticalProportionalGainPerSecond)
                .coerceIn(-verticalMaximum, verticalMaximum)
        }

        val yawRateLimit = min(
            target.maximumYawRateDegreesPerSecond,
            state.bounds.maximumYawRateDegreesPerSecond,
        )
        val yaw = yawCommand(target, telemetry.yawDegrees, offset, yawRateLimit, state.bounds)
        val command = RouteVelocityCommand(
            northMetersPerSecond = north,
            eastMetersPerSecond = east,
            downMetersPerSecond = down,
            yawMode = yaw.mode,
            yawRateDegreesPerSecond = yaw.rate,
            yawSetpointDegrees = yaw.setpoint,
            yawRateLimitDegreesPerSecond = yawRateLimit,
            reason = RouteCommandReason.ACTIVE,
        )
        return RouteTickResult(state, command, horizontalDistance, offset.down)
    }

    private fun validState(state: RouteExecutionState): Boolean {
        if (!RoutePlanValidator.validate(state.activePlan, state.bounds).isValid) return false
        if (state.targetWaypointIndex !in state.activePlan.waypoints.indices) return false
        val pending = state.pendingPlan ?: return state.pendingTargetWaypointIndex == null
        val pendingTarget = state.pendingTargetWaypointIndex ?: return false
        return pending.routeId == state.activePlan.routeId &&
            pending.revision > state.activePlan.revision &&
            pendingTarget in pending.waypoints.indices &&
            RoutePlanValidator.validate(pending, state.bounds).isValid
    }

    private fun validateTelemetry(
        telemetry: RouteTelemetry,
        nowMillis: Long,
        bounds: RouteSafetyBounds,
    ): RouteCommandReason? {
        if (nowMillis < 0L || telemetry.sampleTimeMillis < 0L ||
            !telemetry.latitudeDegrees.isFinite() || telemetry.latitudeDegrees !in -90.0..90.0 ||
            !telemetry.longitudeDegrees.isFinite() || telemetry.longitudeDegrees !in -180.0..180.0 ||
            !telemetry.altitudeMeters.isFinite() || !telemetry.yawDegrees.isFinite()
        ) return RouteCommandReason.INVALID_TELEMETRY

        return if (telemetry.sampleTimeMillis <= nowMillis) {
            if (nowMillis - telemetry.sampleTimeMillis > bounds.telemetryMaximumAgeMillis) {
                RouteCommandReason.STALE_TELEMETRY
            } else null
        } else {
            if (telemetry.sampleTimeMillis - nowMillis > bounds.telemetryMaximumFutureSkewMillis) {
                RouteCommandReason.STALE_TELEMETRY
            } else null
        }
    }

    private fun yawCommand(
        target: RouteWaypoint,
        currentYaw: Double,
        offset: LocalOffset,
        rateLimit: Double,
        bounds: RouteSafetyBounds,
    ): YawCommand = when (target.yawMode) {
        RouteYawMode.FACE_WAYPOINT -> {
            if (hypot(offset.north, offset.east) <= target.horizontalToleranceMeters) {
                YawCommand(RouteYawCommandMode.ANGULAR_VELOCITY, 0.0, null)
            } else {
                val desired = normalizeDegrees(atan2(offset.east, offset.north) * 180.0 / PI)
                val error = normalizeDegrees(desired - normalizeDegrees(currentYaw))
                YawCommand(
                    RouteYawCommandMode.ANGULAR_VELOCITY,
                    (error * bounds.yawProportionalGainPerSecond).coerceIn(-rateLimit, rateLimit),
                    null,
                )
            }
        }
        RouteYawMode.FIXED_HEADING -> YawCommand(
            RouteYawCommandMode.ANGLE_SETPOINT,
            0.0,
            normalizeDegrees(requireNotNull(target.yawDegrees)),
        )
        RouteYawMode.HOLD_HEADING -> YawCommand(RouteYawCommandMode.ANGULAR_VELOCITY, 0.0, null)
    }

    private fun localOffset(current: RouteTelemetry, target: RouteWaypoint): LocalOffset {
        val latitudeDelta = (target.latitudeDegrees - current.latitudeDegrees) * PI / 180.0
        val longitudeDelta = normalizedLongitudeDelta(target.longitudeDegrees - current.longitudeDegrees) * PI / 180.0
        val meanLatitude = (target.latitudeDegrees + current.latitudeDegrees) * 0.5 * PI / 180.0
        return LocalOffset(
            north = latitudeDelta * EARTH_RADIUS_METERS,
            east = longitudeDelta * EARTH_RADIUS_METERS * cos(meanLatitude),
            // Altitude is positive up while NED velocity is positive down.
            down = current.altitudeMeters - target.altitudeMeters,
        )
    }

    private fun normalizedLongitudeDelta(delta: Double): Double {
        var result = delta
        while (result > 180.0) result -= 360.0
        while (result < -180.0) result += 360.0
        return result
    }

    private fun normalizeDegrees(value: Double): Double {
        var result = value % 360.0
        if (result >= 180.0) result -= 360.0
        if (result < -180.0) result += 360.0
        return result
    }

    private fun snapshot(plan: RoutePlan): RoutePlan = plan.copy(
        waypoints = Collections.unmodifiableList(plan.waypoints.map { it.copy() }),
    )

    private fun neutral(state: RouteExecutionState, reason: RouteCommandReason): RouteTickResult =
        RouteTickResult(state, RouteVelocityCommand.neutral(reason))

    private data class LocalOffset(val north: Double, val east: Double, val down: Double)
    private data class YawCommand(val mode: RouteYawCommandMode, val rate: Double, val setpoint: Double?)
}
