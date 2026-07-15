package com.veil.dji

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.crypto.Mac

class ControlProtocolTest {
    private val secret = ControlPacketCodec.secret("test-token")

    @Test
    fun decodesAuthenticatedBodyVelocityInSiUnits() {
        val packet = signedPacket(
            magic = 0x56444332,
            session = 7,
            sequence = 11,
            sentAt = 1_000,
            values = intArrayOf(1_250, -2_500, 750, -12_500)
        )

        val decoded = ControlPacketCodec.decode(packet, 0, packet.size, secret)!!
        assertEquals(7L, decoded.sessionId)
        assertEquals(11L, decoded.sequence)
        val command = decoded.command as RealtimeControlCommand.BodyVelocity
        assertEquals(1.25, command.forwardMetersPerSecond, 0.0)
        assertEquals(-2.5, command.rightMetersPerSecond, 0.0)
        assertEquals(0.75, command.upMetersPerSecond, 0.0)
        assertEquals(-12.5, command.yawRateDegreesPerSecond, 0.0)
    }

    @Test
    fun rejectsTamperingAndOutOfRangeVelocity() {
        val valid = signedPacket(0x56444332, 7, 11, 1_000, intArrayOf(0, 0, 0, 0))
        valid[30] = (valid[30].toInt() xor 1).toByte()
        assertNull(ControlPacketCodec.decode(valid, 0, valid.size, secret))

        val outOfRange = signedPacket(
            0x56444332, 7, 12, 1_000, intArrayOf(23_001, 0, 0, 0)
        )
        assertNull(ControlPacketCodec.decode(outOfRange, 0, outOfRange.size, secret))
    }

    @Test
    fun sessionSequenceRejectsReplayAndOldSession() {
        val window = ControlSequenceWindow(10)
        assertTrue(window.accept(10, 100))
        assertFalse(window.accept(10, 100))
        assertFalse(window.accept(10, 99))
        assertFalse(window.accept(9, 101))
        window.rotate(11)
        assertTrue(window.accept(11, 1))
        assertFalse(window.accept(10, 101))
    }

    @Test
    fun freshnessIsBoundedByDeadmanBudget() {
        assertTrue(ControlPacketFreshness.isFresh(1_000, 750))
        assertTrue(ControlPacketFreshness.isFresh(1_000, 1_100))
        assertFalse(ControlPacketFreshness.isFresh(1_000, 749))
        assertFalse(ControlPacketFreshness.isFresh(1_000, 1_101))
    }

    @Test
    fun acceptedPacketAcknowledgementIsMatchedAndClearedOnSessionRotation() {
        BridgeState.recordAcceptedControlPacket(
            sessionId = -1L,
            sequence = Long.MIN_VALUE,
            sentAtControlMonotonicMs = 1_000L,
            receivedAtControlMonotonicMs = 1_007L,
            appliedAtControlMonotonicMs = 1_009L,
            setpoint = RealtimeControlCommand.BodyVelocity(
                forwardMetersPerSecond = 1.25,
                rightMetersPerSecond = -0.5,
                upMetersPerSecond = 0.0,
                yawRateDegreesPerSecond = 12.0
            ).toAcceptedSetpoint()
        )

        val accepted = BridgeState.lastAcceptedControlPacket.get()!!
        assertEquals("ffffffffffffffff", accepted.sessionHex)
        assertEquals("8000000000000000", accepted.sequenceHex)
        assertEquals(9L, accepted.latencyMs)
        assertEquals(2L, accepted.receiveToApplyMs)
        assertEquals("body_velocity", accepted.setpoint.mode)
        assertEquals(1.25, accepted.setpoint.values["forward_mps"])
        assertEquals(9L, BridgeState.lastControlLatencyMs.get())

        BridgeControlSession.rotate("ack_unit_test")
        assertNull(BridgeState.lastAcceptedControlPacket.get())
        assertEquals(-1L, BridgeState.lastControlLatencyMs.get())
    }

    @Test
    fun sessionRotationCannotSplitValidationFromMailboxPublication() {
        val oldSession = BridgeControlSession.currentHex().toULong(16).toLong()
        val applyEntered = CountDownLatch(1)
        val releaseApply = CountDownLatch(1)
        val rotateStarted = CountDownLatch(1)
        val rotateFinished = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)
        try {
            val apply = executor.submit<Boolean> {
                BridgeControlSession.acceptAndApply(oldSession, Long.MAX_VALUE - 1L) {
                    applyEntered.countDown()
                    releaseApply.await(2, TimeUnit.SECONDS)
                }
            }
            assertTrue(applyEntered.await(1, TimeUnit.SECONDS))
            val rotate = executor.submit<Long> {
                rotateStarted.countDown()
                BridgeControlSession.rotate("unit_test").also {
                    rotateFinished.countDown()
                }
            }
            assertTrue(rotateStarted.await(1, TimeUnit.SECONDS))
            assertFalse(rotateFinished.await(100, TimeUnit.MILLISECONDS))
            releaseApply.countDown()
            assertTrue(apply.get(1, TimeUnit.SECONDS))
            val newSession = rotate.get(1, TimeUnit.SECONDS)
            assertTrue(rotateFinished.await(1, TimeUnit.SECONDS))

            var staleApplied = false
            assertFalse(BridgeControlSession.acceptAndApply(
                oldSession,
                Long.MAX_VALUE
            ) {
                staleApplied = true
                true
            })
            assertFalse(staleApplied)
            assertTrue(BridgeControlSession.acceptAndApply(newSession, 1L) { true })
        } finally {
            releaseApply.countDown()
            executor.shutdownNow()
        }
    }

    private fun signedPacket(
        magic: Int,
        session: Long,
        sequence: Long,
        sentAt: Long,
        values: IntArray
    ): ByteArray {
        val payload = ByteBuffer.allocate(ControlPacketCodec.HEADER_BYTES + values.size * 4)
            .order(ByteOrder.BIG_ENDIAN)
            .putInt(magic)
            .putLong(session)
            .putLong(sequence)
            .putLong(sentAt)
            .apply { values.forEach(::putInt) }
            .array()
        val tag = Mac.getInstance("HmacSHA256").run {
            init(secret)
            doFinal(payload).copyOf(ControlPacketCodec.TAG_BYTES)
        }
        return payload + tag
    }
}
