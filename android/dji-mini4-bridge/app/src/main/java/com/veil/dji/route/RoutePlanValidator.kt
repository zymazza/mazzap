package com.veil.dji.route

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.hypot

object RoutePlanValidator {
    private const val EARTH_RADIUS_METERS = 6_378_137.0

    fun validate(plan: RoutePlan, bounds: RouteSafetyBounds): RouteValidationResult {
        val issues = validateBounds(bounds).toMutableList()
        if (plan.routeId.isBlank()) issue(issues, "routeId", "must not be blank")
        if (plan.routeId.length > 128) issue(issues, "routeId", "must be at most 128 characters")
        if (plan.revision < 0L) issue(issues, "revision", "must be non-negative")
        if (plan.waypoints.isEmpty()) issue(issues, "waypoints", "must contain at least one waypoint")
        if (plan.waypoints.size > bounds.maximumWaypointCount) {
            issue(issues, "waypoints", "exceeds maximumWaypointCount")
        }

        plan.waypoints.forEachIndexed { index, waypoint ->
            val path = "waypoints[$index]"
            finiteInRange(issues, "$path.latitudeDegrees", waypoint.latitudeDegrees, -90.0, 90.0)
            finiteInRange(issues, "$path.longitudeDegrees", waypoint.longitudeDegrees, -180.0, 180.0)
            finiteInRange(
                issues,
                "$path.altitudeMeters",
                waypoint.altitudeMeters,
                bounds.minimumAltitudeMeters,
                bounds.maximumAltitudeMeters,
            )
            finitePositiveAtMost(
                issues,
                "$path.horizontalSpeedMetersPerSecond",
                waypoint.horizontalSpeedMetersPerSecond,
                bounds.maximumHorizontalSpeedMetersPerSecond,
            )
            finitePositiveAtMost(
                issues,
                "$path.verticalSpeedMetersPerSecond",
                waypoint.verticalSpeedMetersPerSecond,
                bounds.maximumVerticalSpeedMetersPerSecond,
            )
            finitePositiveAtMost(
                issues,
                "$path.horizontalToleranceMeters",
                waypoint.horizontalToleranceMeters,
                bounds.maximumHorizontalToleranceMeters,
            )
            finitePositiveAtMost(
                issues,
                "$path.verticalToleranceMeters",
                waypoint.verticalToleranceMeters,
                bounds.maximumVerticalToleranceMeters,
            )
            finitePositiveAtMost(
                issues,
                "$path.maximumYawRateDegreesPerSecond",
                waypoint.maximumYawRateDegreesPerSecond,
                bounds.maximumYawRateDegreesPerSecond,
            )

            when (waypoint.yawMode) {
                RouteYawMode.FIXED_HEADING -> {
                    val yaw = waypoint.yawDegrees
                    if (yaw == null) {
                        issue(issues, "$path.yawDegrees", "is required for FIXED_HEADING")
                    } else {
                        finiteInRange(issues, "$path.yawDegrees", yaw, -180.0, 180.0)
                    }
                }

                RouteYawMode.FACE_WAYPOINT,
                RouteYawMode.HOLD_HEADING,
                -> if (waypoint.yawDegrees != null) {
                    issue(issues, "$path.yawDegrees", "must be null unless yawMode is FIXED_HEADING")
                }
            }
        }

        if (plan.waypoints.all { coordinatesAreUsable(it) }) {
            for (index in 1 until plan.waypoints.size) {
                val distance = horizontalDistance(plan.waypoints[index - 1], plan.waypoints[index])
                if (distance > bounds.maximumPlanLegMeters) {
                    issue(issues, "waypoints[$index]", "leg exceeds maximumPlanLegMeters")
                }
            }
            val origin = plan.waypoints.firstOrNull()
            if (origin != null) {
                plan.waypoints.drop(1).forEachIndexed { offset, waypoint ->
                    if (horizontalDistance(origin, waypoint) > bounds.maximumPlanExtentMeters) {
                        issue(issues, "waypoints[${offset + 1}]", "exceeds maximumPlanExtentMeters")
                    }
                }
            }
        }

        return RouteValidationResult(issues.toList())
    }

    fun validateBounds(bounds: RouteSafetyBounds): List<RouteValidationIssue> {
        val issues = mutableListOf<RouteValidationIssue>()
        if (bounds.maximumWaypointCount !in 1..10_000) {
            issue(issues, "bounds.maximumWaypointCount", "must be between 1 and 10000")
        }
        finite(issues, "bounds.minimumAltitudeMeters", bounds.minimumAltitudeMeters)
        finite(issues, "bounds.maximumAltitudeMeters", bounds.maximumAltitudeMeters)
        if (bounds.minimumAltitudeMeters.isFinite() && bounds.minimumAltitudeMeters < -500.0) {
            issue(issues, "bounds.minimumAltitudeMeters", "must be at least -500.0")
        }
        if (bounds.maximumAltitudeMeters.isFinite() && bounds.maximumAltitudeMeters > 5_000.0) {
            issue(issues, "bounds.maximumAltitudeMeters", "must be at most 5000.0")
        }
        if (bounds.minimumAltitudeMeters.isFinite() && bounds.maximumAltitudeMeters.isFinite() &&
            bounds.minimumAltitudeMeters >= bounds.maximumAltitudeMeters
        ) {
            issue(issues, "bounds.maximumAltitudeMeters", "must exceed minimumAltitudeMeters")
        }
        // DJI advanced virtual-stick physical ranges are used as hard ceilings; defaults are lower.
        finitePositiveAtMost(issues, "bounds.maximumHorizontalSpeedMetersPerSecond", bounds.maximumHorizontalSpeedMetersPerSecond, 23.0)
        finitePositiveAtMost(issues, "bounds.maximumVerticalSpeedMetersPerSecond", bounds.maximumVerticalSpeedMetersPerSecond, 6.0)
        finitePositiveAtMost(issues, "bounds.maximumYawRateDegreesPerSecond", bounds.maximumYawRateDegreesPerSecond, 100.0)
        finitePositiveAtMost(issues, "bounds.maximumHorizontalToleranceMeters", bounds.maximumHorizontalToleranceMeters, 100.0)
        finitePositiveAtMost(issues, "bounds.maximumVerticalToleranceMeters", bounds.maximumVerticalToleranceMeters, 100.0)
        finitePositiveAtMost(issues, "bounds.maximumPlanLegMeters", bounds.maximumPlanLegMeters, 5_000.0)
        finitePositiveAtMost(issues, "bounds.maximumPlanExtentMeters", bounds.maximumPlanExtentMeters, 10_000.0)
        finitePositiveAtMost(issues, "bounds.maximumDistanceToTargetMeters", bounds.maximumDistanceToTargetMeters, 5_000.0)
        if (bounds.telemetryMaximumAgeMillis <= 0L) issue(issues, "bounds.telemetryMaximumAgeMillis", "must be positive")
        if (bounds.telemetryMaximumAgeMillis > 10_000L) issue(issues, "bounds.telemetryMaximumAgeMillis", "must be at most 10000")
        if (bounds.telemetryMaximumFutureSkewMillis < 0L) issue(issues, "bounds.telemetryMaximumFutureSkewMillis", "must be non-negative")
        if (bounds.telemetryMaximumFutureSkewMillis > 5_000L) issue(issues, "bounds.telemetryMaximumFutureSkewMillis", "must be at most 5000")
        finitePositiveAtMost(issues, "bounds.horizontalProportionalGainPerSecond", bounds.horizontalProportionalGainPerSecond, 10.0)
        finitePositiveAtMost(issues, "bounds.verticalProportionalGainPerSecond", bounds.verticalProportionalGainPerSecond, 10.0)
        finitePositiveAtMost(issues, "bounds.yawProportionalGainPerSecond", bounds.yawProportionalGainPerSecond, 10.0)
        return issues
    }

    private fun coordinatesAreUsable(waypoint: RouteWaypoint): Boolean =
        waypoint.latitudeDegrees.isFinite() && waypoint.latitudeDegrees in -90.0..90.0 &&
            waypoint.longitudeDegrees.isFinite() && waypoint.longitudeDegrees in -180.0..180.0

    private fun horizontalDistance(a: RouteWaypoint, b: RouteWaypoint): Double {
        val latitudeDelta = (b.latitudeDegrees - a.latitudeDegrees) * PI / 180.0
        val longitudeDelta = normalizedLongitudeDelta(b.longitudeDegrees - a.longitudeDegrees) * PI / 180.0
        val meanLatitude = (a.latitudeDegrees + b.latitudeDegrees) * 0.5 * PI / 180.0
        return hypot(
            latitudeDelta * EARTH_RADIUS_METERS,
            longitudeDelta * EARTH_RADIUS_METERS * cos(meanLatitude),
        )
    }

    private fun normalizedLongitudeDelta(delta: Double): Double {
        var result = delta
        while (result > 180.0) result -= 360.0
        while (result < -180.0) result += 360.0
        return result
    }

    private fun finiteInRange(
        issues: MutableList<RouteValidationIssue>,
        path: String,
        value: Double,
        minimum: Double,
        maximum: Double,
    ) {
        if (!value.isFinite()) issue(issues, path, "must be finite")
        else if (value < minimum || value > maximum) issue(issues, path, "must be between $minimum and $maximum")
    }

    private fun finitePositiveAtMost(
        issues: MutableList<RouteValidationIssue>,
        path: String,
        value: Double,
        maximum: Double,
    ) {
        if (!value.isFinite()) issue(issues, path, "must be finite")
        else if (value <= 0.0) issue(issues, path, "must be positive")
        else if (value > maximum) issue(issues, path, "exceeds configured maximum $maximum")
    }

    private fun finite(issues: MutableList<RouteValidationIssue>, path: String, value: Double) {
        if (!value.isFinite()) issue(issues, path, "must be finite")
    }

    private fun issue(issues: MutableList<RouteValidationIssue>, path: String, message: String) {
        issues += RouteValidationIssue(path, message)
    }
}
