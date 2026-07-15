package com.veil.dji.route

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import org.json.JSONTokener

enum class RouteRevisionParseErrorCode {
    INVALID_JSON,
    MISSING_FIELD,
    WRONG_TYPE,
    UNKNOWN_FIELD,
    INVALID_VALUE,
}

class RouteRevisionParseException(
    val code: RouteRevisionParseErrorCode,
    val path: String,
    message: String,
    cause: Throwable? = null,
) : IllegalArgumentException(message, cause)

/**
 * Strict parser for [BRIDGE_ROUTE_SCHEMA]. Unknown fields are rejected so a misspelled flight
 * parameter cannot silently fall back to a default. Parsing only creates a request; acceptance is
 * a separate atomic operation in [AtomicRouteRevisionStore].
 */
object RouteRevisionJsonParser {
    private val envelopeFields = setOf(
        "schema",
        "engine",
        "expected_accepted_revision",
        "activation",
        "scope",
        "plan",
    )
    private val planFields = setOf("route_id", "revision", "waypoints")
    private val waypointFields = setOf(
        "latitude_deg",
        "longitude_deg",
        "altitude_m",
        "horizontal_speed_mps",
        "vertical_speed_mps",
        "horizontal_tolerance_m",
        "vertical_tolerance_m",
        "yaw_mode",
        "yaw_deg",
        "maximum_yaw_rate_deg_s",
    )

    fun parse(json: String): RouteRevisionRequest {
        if (json.length > MAX_JSON_CHARS) {
            invalid("$", "route document exceeds $MAX_JSON_CHARS characters")
        }
        val root = try {
            val tokener = JSONTokener(json)
            val value = tokener.nextValue()
            if (value !is JSONObject) wrongType("$", "must be a JSON object")
            if (tokener.nextClean().code != 0) invalid("$", "trailing data after JSON object")
            value
        } catch (error: JSONException) {
            throw RouteRevisionParseException(
                RouteRevisionParseErrorCode.INVALID_JSON,
                "$",
                "invalid JSON",
                error,
            )
        }
        requireOnlyFields(root, envelopeFields, "$")

        val schema = requiredString(root, "schema", "$.schema")
        val engine = requiredString(root, "engine", "$.engine")
        val expectedRevision = nullableLong(
            root,
            "expected_accepted_revision",
            "$.expected_accepted_revision",
        )
        val activation = when (requiredString(root, "activation", "$.activation")) {
            "immediate" -> RouteReplacementMode.IMMEDIATE
            "at_waypoint_boundary" -> RouteReplacementMode.AT_WAYPOINT_BOUNDARY
            else -> invalid("$.activation", "must be immediate or at_waypoint_boundary")
        }
        val scope = when (requiredString(root, "scope", "$.scope")) {
            "full_route_continue" -> RouteReplacementScope.FULL_ROUTE_CONTINUE
            "remaining_route_from_current_state" ->
                RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE
            else -> invalid(
                "$.scope",
                "must be full_route_continue or remaining_route_from_current_state",
            )
        }
        val planJson = requiredObject(root, "plan", "$.plan")
        requireOnlyFields(planJson, planFields, "$.plan")
        val routeId = requiredString(planJson, "route_id", "$.plan.route_id")
        val revision = requiredLong(planJson, "revision", "$.plan.revision")
        val waypointsJson = requiredArray(planJson, "waypoints", "$.plan.waypoints")
        if (waypointsJson.length() > MAX_PARSE_WAYPOINTS) {
            invalid("$.plan.waypoints", "must contain at most $MAX_PARSE_WAYPOINTS entries")
        }
        val waypoints = ArrayList<RouteWaypoint>(waypointsJson.length())
        for (index in 0 until waypointsJson.length()) {
            val path = "$.plan.waypoints[$index]"
            val waypoint = waypointsJson.opt(index)
            if (waypoint !is JSONObject) wrongType(path, "must be an object")
            requireOnlyFields(waypoint, waypointFields, path)
            val yawMode = when (optionalString(waypoint, "yaw_mode", "$path.yaw_mode") ?: "face_waypoint") {
                "face_waypoint" -> RouteYawMode.FACE_WAYPOINT
                "fixed_heading" -> RouteYawMode.FIXED_HEADING
                "hold_heading" -> RouteYawMode.HOLD_HEADING
                else -> invalid(
                    "$path.yaw_mode",
                    "must be face_waypoint, fixed_heading, or hold_heading",
                )
            }
            waypoints += RouteWaypoint(
                latitudeDegrees = requiredDouble(waypoint, "latitude_deg", "$path.latitude_deg"),
                longitudeDegrees = requiredDouble(waypoint, "longitude_deg", "$path.longitude_deg"),
                altitudeMeters = requiredDouble(waypoint, "altitude_m", "$path.altitude_m"),
                horizontalSpeedMetersPerSecond = optionalDouble(
                    waypoint,
                    "horizontal_speed_mps",
                    "$path.horizontal_speed_mps",
                ) ?: 2.0,
                verticalSpeedMetersPerSecond = optionalDouble(
                    waypoint,
                    "vertical_speed_mps",
                    "$path.vertical_speed_mps",
                ) ?: 1.0,
                horizontalToleranceMeters = optionalDouble(
                    waypoint,
                    "horizontal_tolerance_m",
                    "$path.horizontal_tolerance_m",
                ) ?: 1.0,
                verticalToleranceMeters = optionalDouble(
                    waypoint,
                    "vertical_tolerance_m",
                    "$path.vertical_tolerance_m",
                ) ?: 0.5,
                yawMode = yawMode,
                yawDegrees = optionalDouble(waypoint, "yaw_deg", "$path.yaw_deg"),
                maximumYawRateDegreesPerSecond = optionalDouble(
                    waypoint,
                    "maximum_yaw_rate_deg_s",
                    "$path.maximum_yaw_rate_deg_s",
                ) ?: 30.0,
            )
        }

        return RouteRevisionRequest(
            schema = schema,
            engine = engine,
            expectedAcceptedRevision = expectedRevision,
            activation = activation,
            scope = scope,
            plan = RoutePlan(routeId, revision, waypoints),
        )
    }

    private fun requiredString(objectValue: JSONObject, name: String, path: String): String {
        if (!objectValue.has(name)) missing(path)
        return optionalString(objectValue, name, path) ?: wrongType(path, "must be a string")
    }

    private fun optionalString(objectValue: JSONObject, name: String, path: String): String? {
        if (!objectValue.has(name)) return null
        val value = objectValue.opt(name)
        if (value === JSONObject.NULL) return null
        return value as? String ?: wrongType(path, "must be a string")
    }

    private fun requiredLong(objectValue: JSONObject, name: String, path: String): Long {
        if (!objectValue.has(name)) missing(path)
        return exactLong(objectValue.opt(name), path)
    }

    private fun nullableLong(objectValue: JSONObject, name: String, path: String): Long? {
        if (!objectValue.has(name) || objectValue.opt(name) === JSONObject.NULL) return null
        return exactLong(objectValue.opt(name), path)
    }

    private fun exactLong(value: Any?, path: String): Long {
        if (value !is Number) wrongType(path, "must be an integer")
        val text = value.toString()
        if (!INTEGER_PATTERN.matches(text)) wrongType(path, "must be an integer")
        return text.toLongOrNull() ?: invalid(path, "is outside signed 64-bit range")
    }

    private fun requiredDouble(objectValue: JSONObject, name: String, path: String): Double {
        if (!objectValue.has(name)) missing(path)
        return finiteDouble(objectValue.opt(name), path)
    }

    private fun optionalDouble(objectValue: JSONObject, name: String, path: String): Double? {
        if (!objectValue.has(name) || objectValue.opt(name) === JSONObject.NULL) return null
        return finiteDouble(objectValue.opt(name), path)
    }

    private fun finiteDouble(value: Any?, path: String): Double {
        if (value !is Number) wrongType(path, "must be a number")
        val result = value.toDouble()
        if (!result.isFinite()) invalid(path, "must be finite")
        return result
    }

    private fun requiredObject(objectValue: JSONObject, name: String, path: String): JSONObject {
        if (!objectValue.has(name)) missing(path)
        return objectValue.opt(name) as? JSONObject ?: wrongType(path, "must be an object")
    }

    private fun requiredArray(objectValue: JSONObject, name: String, path: String): JSONArray {
        if (!objectValue.has(name)) missing(path)
        return objectValue.opt(name) as? JSONArray ?: wrongType(path, "must be an array")
    }

    private fun requireOnlyFields(value: JSONObject, allowed: Set<String>, path: String) {
        val keys = value.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            if (key !in allowed) {
                throw RouteRevisionParseException(
                    RouteRevisionParseErrorCode.UNKNOWN_FIELD,
                    "$path.$key",
                    "unknown field: $key",
                )
            }
        }
    }

    private fun missing(path: String): Nothing = throw RouteRevisionParseException(
        RouteRevisionParseErrorCode.MISSING_FIELD,
        path,
        "missing required field",
    )

    private fun wrongType(path: String, message: String): Nothing =
        throw RouteRevisionParseException(RouteRevisionParseErrorCode.WRONG_TYPE, path, message)

    private fun invalid(path: String, message: String): Nothing =
        throw RouteRevisionParseException(RouteRevisionParseErrorCode.INVALID_VALUE, path, message)

    private val INTEGER_PATTERN = Regex("-?(0|[1-9][0-9]*)")
    private const val MAX_JSON_CHARS = 512 * 1024
    private const val MAX_PARSE_WAYPOINTS = 10_000
}
