package com.veil.dji

import kotlin.math.min

/** Pure timing policy for the Android perception-listener watchdog. */
internal object PerceptionRecoveryPolicy {
    const val CHECK_INTERVAL_MS = 1_000L
    const val SOURCE_STALE_AFTER_MS = 5_000L
    private const val BASE_RETRY_MS = 2_000L
    private const val MAX_RETRY_MS = 30_000L

    fun isSourceStale(updatedAtMonotonicMs: Long?, nowMs: Long): Boolean {
        val age = updatedAtMonotonicMs?.let { nowMs - it } ?: return true
        return age < 0L || age > SOURCE_STALE_AFTER_MS
    }

    /** 2, 4, 8, 16, then at most 30 seconds, with overflow-safe clamping. */
    fun retryDelayMillis(consecutiveIssues: Int): Long {
        val exponent = (consecutiveIssues.coerceAtLeast(1) - 1).coerceAtMost(4)
        return min(BASE_RETRY_MS shl exponent, MAX_RETRY_MS)
    }
}
