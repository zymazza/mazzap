package com.veil.dji

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test

class PerceptionRecoveryPolicyTest {
    @Test
    fun detectsMissingOldAndFutureDatedSources() {
        assertTrue(PerceptionRecoveryPolicy.isSourceStale(null, nowMs = 10_000L))
        assertTrue(PerceptionRecoveryPolicy.isSourceStale(4_999L, nowMs = 10_000L))
        assertTrue(PerceptionRecoveryPolicy.isSourceStale(10_001L, nowMs = 10_000L))
        assertFalse(PerceptionRecoveryPolicy.isSourceStale(5_000L, nowMs = 10_000L))
    }

    @Test
    fun retryDelayUsesBoundedExponentialBackoff() {
        assertEquals(2_000L, PerceptionRecoveryPolicy.retryDelayMillis(1))
        assertEquals(4_000L, PerceptionRecoveryPolicy.retryDelayMillis(2))
        assertEquals(8_000L, PerceptionRecoveryPolicy.retryDelayMillis(3))
        assertEquals(16_000L, PerceptionRecoveryPolicy.retryDelayMillis(4))
        assertEquals(30_000L, PerceptionRecoveryPolicy.retryDelayMillis(5))
        assertEquals(30_000L, PerceptionRecoveryPolicy.retryDelayMillis(Int.MAX_VALUE))
    }
}
