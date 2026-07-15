package com.veil.dji

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.TimeUnit

class TelemetryTransportTest {
    @Test
    fun cadenceMaintainsTwentyHertzAndSkipsMissedTicksWithoutBurstReplay() {
        assertEquals(1_050L, TelemetryCadence.nextDeadlineMillis(1_000L, 1_001L))
        assertEquals(1_050L, TelemetryCadence.nextDeadlineMillis(1_000L, 1_050L))
        assertEquals(1_100L, TelemetryCadence.nextDeadlineMillis(1_000L, 1_051L))
        assertEquals(1_200L, TelemetryCadence.nextDeadlineMillis(1_000L, 1_175L))
    }

    @Test
    fun mailboxRetainsOnlyNewestPendingSnapshot() {
        val mailbox = LatestValueMailbox<Long>()

        assertFalse(mailbox.offerLatest(1L))
        assertTrue(mailbox.offerLatest(2L))
        assertTrue(mailbox.offerLatest(3L))

        assertEquals(3L, mailbox.poll(0L, TimeUnit.MILLISECONDS))
    }

    @Test
    fun renderedFrameReportsHonestQueueAgeAndSequenceGap() {
        val frame = TelemetryFrame(
            sequence = 12L,
            generatedAtMonotonicMillis = 1_000L,
            jsonObject = "{\"telemetry_sequence\":12}"
        )

        val rendered = frame.renderForWrite(
            writeStartedAtMonotonicMillis = 1_037L,
            sequenceGapBeforeWrite = 2L,
            tcpSendBufferBytes = 65_536
        ).toString(Charsets.UTF_8)

        assertTrue(rendered.endsWith("\n"))
        assertTrue(rendered.contains("\"telemetry_write_started_monotonic_ms\":1037"))
        assertTrue(rendered.contains("\"telemetry_queue_age_ms\":37"))
        assertTrue(rendered.contains("\"telemetry_client_sequence_gap_before_write\":2"))
        assertTrue(rendered.contains("\"telemetry_tcp_send_buffer_bytes\":65536"))
    }

    @Test
    fun sequenceGapDoesNotInventLossForFirstOrOutOfOrderValue() {
        assertEquals(0L, telemetrySequenceGap(null, 50L))
        assertEquals(0L, telemetrySequenceGap(50L, 51L))
        assertEquals(3L, telemetrySequenceGap(50L, 54L))
        assertEquals(0L, telemetrySequenceGap(54L, 53L))
    }
}
