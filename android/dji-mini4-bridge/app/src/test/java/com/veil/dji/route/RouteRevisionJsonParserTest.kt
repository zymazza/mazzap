package com.veil.dji.route

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class RouteRevisionJsonParserTest {
    @Test
    fun parsesStrictRevisionEnvelopeAndDefaults() {
        val request = RouteRevisionJsonParser.parse(validJson())
        assertEquals(BRIDGE_ROUTE_SCHEMA, request.schema)
        assertEquals(BRIDGE_ROUTE_ENGINE, request.engine)
        assertNull(request.expectedAcceptedRevision)
        assertEquals(RouteReplacementMode.IMMEDIATE, request.activation)
        assertEquals(RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE, request.scope)
        assertEquals("route-a", request.plan.routeId)
        assertEquals(1L, request.plan.revision)
        val waypoint = request.plan.waypoints.single()
        assertEquals(2.0, waypoint.horizontalSpeedMetersPerSecond, 0.0)
        assertEquals(RouteYawMode.FACE_WAYPOINT, waypoint.yawMode)
        assertNull(waypoint.yawDegrees)
    }

    @Test
    fun parsesFullRouteBoundaryRevisionAndFixedYaw() {
        val request = RouteRevisionJsonParser.parse(
            validJson()
                .replace("null", "4")
                .replace("\"immediate\"", "\"at_waypoint_boundary\"")
                .replace(
                    "\"remaining_route_from_current_state\"",
                    "\"full_route_continue\"",
                )
                .replace(
                    "\"altitude_m\":10.0",
                    "\"altitude_m\":10.0,\"yaw_mode\":\"fixed_heading\",\"yaw_deg\":-90",
                ),
        )
        assertEquals(4L, request.expectedAcceptedRevision)
        assertEquals(RouteReplacementMode.AT_WAYPOINT_BOUNDARY, request.activation)
        assertEquals(RouteReplacementScope.FULL_ROUTE_CONTINUE, request.scope)
        assertEquals(RouteYawMode.FIXED_HEADING, request.plan.waypoints.single().yawMode)
        assertEquals(-90.0, requireNotNull(request.plan.waypoints.single().yawDegrees), 0.0)
    }

    @Test
    fun rejectsUnknownFieldInsteadOfSilentlyIgnoringTypo() {
        val error = expectParseFailure(
            validJson().replace("\"altitude_m\":10.0", "\"altitude_m\":10.0,\"altitdue_m\":11"),
        )
        assertEquals(RouteRevisionParseErrorCode.UNKNOWN_FIELD, error.code)
        assertEquals("$.plan.waypoints[0].altitdue_m", error.path)
    }

    @Test
    fun rejectsTrailingPayloadAfterOtherwiseValidDocument() {
        val error = expectParseFailure(validJson() + " true")
        assertEquals(RouteRevisionParseErrorCode.INVALID_VALUE, error.code)
        assertEquals("$", error.path)
    }

    @Test
    fun rejectsCoercedNumbersNonFiniteValuesAndAmbiguousScope() {
        val stringNumber = expectParseFailure(validJson().replace("\"revision\":1", "\"revision\":\"1\""))
        assertEquals(RouteRevisionParseErrorCode.WRONG_TYPE, stringNumber.code)
        assertEquals("$.plan.revision", stringNumber.path)

        val nonFinite = expectParseFailure(validJson().replace("\"latitude_deg\":0.0", "\"latitude_deg\":1e999"))
        assertEquals(RouteRevisionParseErrorCode.INVALID_VALUE, nonFinite.code)
        assertEquals("$.plan.waypoints[0].latitude_deg", nonFinite.path)

        val ambiguous = expectParseFailure(
            validJson().replace("remaining_route_from_current_state", "restart"),
        )
        assertEquals(RouteRevisionParseErrorCode.INVALID_VALUE, ambiguous.code)
        assertEquals("$.scope", ambiguous.path)
    }

    @Test
    fun parserDoesNotClaimUnsupportedEngineIsValidForAcceptance() {
        val request = RouteRevisionJsonParser.parse(
            validJson().replace(BRIDGE_ROUTE_ENGINE, "native_dji_waypoint"),
        )
        val result = AtomicRouteRevisionStore().accept(request)
        assertEquals(RouteRevisionAcceptanceStatus.UNSUPPORTED, result.status)
        assertTrue(result.issues.single().message.contains("unsupported route engine"))
    }

    private fun expectParseFailure(json: String): RouteRevisionParseException = try {
        RouteRevisionJsonParser.parse(json)
        fail("expected RouteRevisionParseException")
        throw AssertionError("unreachable")
    } catch (error: RouteRevisionParseException) {
        error
    }

    private fun validJson(): String = """
        {
          "schema":"veil.route-revision.v1",
          "engine":"bridge_virtual_stick",
          "expected_accepted_revision":null,
          "activation":"immediate",
          "scope":"remaining_route_from_current_state",
          "plan":{
            "route_id":"route-a",
            "revision":1,
            "waypoints":[
              {"latitude_deg":0.0,"longitude_deg":0.0,"altitude_m":10.0}
            ]
          }
        }
    """.trimIndent()
}
