package com.veil.dji

import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/** Pure cadence math for a fixed-rate publisher that skips, rather than replays, missed ticks. */
internal object TelemetryCadence {
    const val PUBLISH_HZ = 20
    const val PERIOD_MILLIS = 1_000L / PUBLISH_HZ

    fun nextDeadlineMillis(
        previousDeadlineMillis: Long,
        completedAtMillis: Long,
        periodMillis: Long = PERIOD_MILLIS
    ): Long {
        require(periodMillis > 0L)
        if (completedAtMillis <= previousDeadlineMillis) {
            return previousDeadlineMillis + periodMillis
        }

        val elapsed = completedAtMillis - previousDeadlineMillis
        val periods = 1L + (elapsed - 1L) / periodMillis
        return previousDeadlineMillis + periods * periodMillis
    }
}

/**
 * A single-producer, single-consumer pending value. Publishing while full discards the obsolete
 * value and leaves the consumer only the newest state; there is no application-level FIFO backlog.
 */
internal class LatestValueMailbox<T> {
    private val queue = ArrayBlockingQueue<T>(1)

    /** Returns true when an older pending value was replaced. */
    fun offerLatest(value: T): Boolean {
        if (queue.offer(value)) return false
        queue.poll()
        check(queue.offer(value))
        return true
    }

    fun poll(timeout: Long, unit: TimeUnit): T? = queue.poll(timeout, unit)
}

/** Immutable shared state snapshot, rendered with truthful per-client write timing. */
internal data class TelemetryFrame(
    val sequence: Long,
    val generatedAtMonotonicMillis: Long,
    private val jsonObject: String
) {
    init {
        require(jsonObject.startsWith('{') && jsonObject.endsWith('}'))
    }

    fun renderForWrite(
        writeStartedAtMonotonicMillis: Long,
        sequenceGapBeforeWrite: Long,
        tcpSendBufferBytes: Int
    ): ByteArray {
        val queueAgeMillis =
            (writeStartedAtMonotonicMillis - generatedAtMonotonicMillis).coerceAtLeast(0L)
        val hasExistingFields = jsonObject.length > 2
        return buildString(jsonObject.length + 192) {
            append(jsonObject, 0, jsonObject.length - 1)
            if (hasExistingFields) append(',')
            append("\"telemetry_write_started_monotonic_ms\":")
            append(writeStartedAtMonotonicMillis)
            append(",\"telemetry_queue_age_ms\":")
            append(queueAgeMillis)
            append(",\"telemetry_client_sequence_gap_before_write\":")
            append(sequenceGapBeforeWrite.coerceAtLeast(0L))
            append(",\"telemetry_tcp_send_buffer_bytes\":")
            append(tcpSendBufferBytes.coerceAtLeast(0))
            append("}\n")
        }.toByteArray(Charsets.UTF_8)
    }
}

internal fun telemetrySequenceGap(previousSequence: Long?, currentSequence: Long): Long =
    if (previousSequence == null || currentSequence <= previousSequence) {
        0L
    } else {
        (currentSequence - previousSequence - 1L).coerceAtLeast(0L)
    }
