package com.veil.dji.route

/**
 * A JSON-friendly route description. Every field is a value and the executor takes a defensive
 * snapshot of [waypoints] before accepting a plan.
 *
 * [altitudeMeters] is deliberately datum-agnostic: callers must supply waypoint and telemetry
 * altitudes in the same frame (normally height above takeoff). It is not ellipsoid/MSL conversion.
 */
data class RouteWaypoint(
    val latitudeDegrees: Double,
    val longitudeDegrees: Double,
    val altitudeMeters: Double,
    val horizontalSpeedMetersPerSecond: Double = 2.0,
    val verticalSpeedMetersPerSecond: Double = 1.0,
    val horizontalToleranceMeters: Double = 1.0,
    val verticalToleranceMeters: Double = 0.5,
    val yawMode: RouteYawMode = RouteYawMode.FACE_WAYPOINT,
    val yawDegrees: Double? = null,
    val maximumYawRateDegreesPerSecond: Double = 30.0,
)

enum class RouteYawMode {
    /** Rotate toward the current waypoint using a bounded angular-velocity command. */
    FACE_WAYPOINT,

    /** Emit [RouteWaypoint.yawDegrees] as an angle setpoint. */
    FIXED_HEADING,

    /** Emit zero yaw angular velocity; the downstream controller retains heading. */
    HOLD_HEADING,
}

data class RoutePlan(
    val routeId: String,
    val revision: Long,
    val waypoints: List<RouteWaypoint>,
)

/**
 * Conservative defaults for a small-aircraft route executor. These are software command bounds,
 * not a statement of regulatory permission or the aircraft's guaranteed performance.
 */
data class RouteSafetyBounds(
    val maximumWaypointCount: Int = 100,
    val minimumAltitudeMeters: Double = 0.0,
    val maximumAltitudeMeters: Double = 120.0,
    val maximumHorizontalSpeedMetersPerSecond: Double = 5.0,
    val maximumVerticalSpeedMetersPerSecond: Double = 2.0,
    val maximumYawRateDegreesPerSecond: Double = 45.0,
    val maximumHorizontalToleranceMeters: Double = 10.0,
    val maximumVerticalToleranceMeters: Double = 5.0,
    val maximumPlanLegMeters: Double = 2_000.0,
    val maximumPlanExtentMeters: Double = 5_000.0,
    val maximumDistanceToTargetMeters: Double = 2_000.0,
    val telemetryMaximumAgeMillis: Long = 500L,
    val telemetryMaximumFutureSkewMillis: Long = 100L,
    val horizontalProportionalGainPerSecond: Double = 0.8,
    val verticalProportionalGainPerSecond: Double = 0.8,
    val yawProportionalGainPerSecond: Double = 1.5,
)

data class RouteTelemetry(
    val latitudeDegrees: Double,
    val longitudeDegrees: Double,
    val altitudeMeters: Double,
    val yawDegrees: Double,
    val sampleTimeMillis: Long,
)

enum class RoutePhase {
    READY,
    RUNNING,
    PAUSED,
    COMPLETED,
    ABORTED,
}

enum class RouteReplacementMode {
    /** Activate the complete immutable replacement on the next executor tick. */
    IMMEDIATE,

    /** Stage the complete immutable replacement and activate it when the current target is met. */
    AT_WAYPOINT_BOUNDARY,
}

/**
 * Defines how waypoint indices in a replacement relate to the active revision. The distinction is
 * deliberately explicit: silently resetting a full route to waypoint zero during flight can make
 * the aircraft backtrack over an already-flown path.
 */
enum class RouteReplacementScope {
    /**
     * The replacement is a complete revised route. Immediate activation keeps the current target
     * index. Boundary activation continues at the following target index.
     */
    FULL_ROUTE_CONTINUE,

    /**
     * The replacement contains only the desired remaining path. Its waypoint zero becomes the new
     * target and is approached from the next fresh aircraft telemetry sample, never from a cached
     * route origin.
     */
    REMAINING_ROUTE_FROM_CURRENT_STATE,
}

enum class RouteYawCommandMode {
    ANGULAR_VELOCITY,
    ANGLE_SETPOINT,
}

enum class RouteCommandReason {
    ACTIVE,
    NOT_STARTED,
    PAUSED,
    ABORTED,
    COMPLETED,
    WAYPOINT_ADVANCED,
    PLAN_REPLACED,
    STALE_TELEMETRY,
    INVALID_TELEMETRY,
    INVALID_STATE,
    TARGET_TOO_FAR,
}

/** Pure NED command. Down is positive, matching DJI's ground-coordinate NED convention. */
data class RouteVelocityCommand(
    val northMetersPerSecond: Double,
    val eastMetersPerSecond: Double,
    val downMetersPerSecond: Double,
    val yawMode: RouteYawCommandMode,
    val yawRateDegreesPerSecond: Double,
    val yawSetpointDegrees: Double?,
    val yawRateLimitDegreesPerSecond: Double,
    val reason: RouteCommandReason,
) {
    val isActive: Boolean
        get() = reason == RouteCommandReason.ACTIVE

    companion object {
        fun neutral(reason: RouteCommandReason): RouteVelocityCommand = RouteVelocityCommand(
            northMetersPerSecond = 0.0,
            eastMetersPerSecond = 0.0,
            downMetersPerSecond = 0.0,
            yawMode = RouteYawCommandMode.ANGULAR_VELOCITY,
            yawRateDegreesPerSecond = 0.0,
            yawSetpointDegrees = null,
            yawRateLimitDegreesPerSecond = 0.0,
            reason = reason,
        )
    }
}

data class RouteValidationIssue(val path: String, val message: String)

data class RouteValidationResult(val issues: List<RouteValidationIssue>) {
    val isValid: Boolean
        get() = issues.isEmpty()
}

data class RouteExecutionState(
    val activePlan: RoutePlan,
    val bounds: RouteSafetyBounds,
    val phase: RoutePhase,
    val targetWaypointIndex: Int,
    val pendingPlan: RoutePlan? = null,
    /** Target index to publish atomically with [pendingPlan] at its activation boundary. */
    val pendingTargetWaypointIndex: Int? = null,
)

data class RoutePreparationResult(
    val state: RouteExecutionState?,
    val validation: RouteValidationResult,
) {
    val accepted: Boolean
        get() = state != null && validation.isValid
}

data class RouteStateChange(
    val state: RouteExecutionState,
    val accepted: Boolean,
    val validation: RouteValidationResult = RouteValidationResult(emptyList()),
)

data class RouteTickResult(
    val state: RouteExecutionState,
    val command: RouteVelocityCommand,
    val horizontalDistanceMeters: Double? = null,
    val verticalErrorMeters: Double? = null,
)
