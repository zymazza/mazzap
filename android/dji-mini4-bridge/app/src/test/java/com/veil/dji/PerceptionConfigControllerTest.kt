package com.veil.dji

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class PerceptionConfigControllerTest {
    @Test
    fun canonicalizesAvoidanceType() {
        val request = PerceptionConfigRequest.parse("avoidance_type", "bypass")

        assertEquals(PerceptionConfigSetting.AVOIDANCE_TYPE, request.setting)
        assertEquals("BYPASS", request.value)
    }

    @Test
    fun rejectsUnsupportedMini4SettingsAndInvalidValues() {
        listOf("horizontal_enabled", "overall_enabled", "horizontal_warning_distance_m")
            .forEach { setting ->
                assertThrows(IllegalArgumentException::class.java) {
                    PerceptionConfigRequest.parse(setting, "true")
                }
            }
        assertThrows(IllegalArgumentException::class.java) {
            PerceptionConfigRequest.parse("avoidance_type", "AVOID")
        }
        assertThrows(IllegalArgumentException::class.java) {
            PerceptionConfigRequest.parse("avoidance_type", null)
        }
    }

    @Test
    fun singleFlightGateRejectsConcurrentMutationAndReleasesByOwner() {
        val gate = SingleFlightCommandGate()

        assertTrue(gate.tryAcquire("first"))
        assertFalse(gate.tryAcquire("second"))
        assertEquals("first", gate.current())
        assertFalse(gate.release("second"))
        assertTrue(gate.release("first"))
        assertTrue(gate.tryAcquire("third"))
    }
}
