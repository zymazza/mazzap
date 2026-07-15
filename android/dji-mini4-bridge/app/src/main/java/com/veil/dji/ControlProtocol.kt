package com.veil.dji

import android.os.SystemClock
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** Wire-level real-time controls. All physical values use fixed-point SI units. */
internal sealed class RealtimeControlCommand {
    data class Sticks(
        val leftHorizontal: Int,
        val leftVertical: Int,
        val rightHorizontal: Int,
        val rightVertical: Int
    ) : RealtimeControlCommand()

    /** BODY frame: forward/right/up in m/s, clockwise yaw rate in degrees/s. */
    data class BodyVelocity(
        val forwardMetersPerSecond: Double,
        val rightMetersPerSecond: Double,
        val upMetersPerSecond: Double,
        val yawRateDegreesPerSecond: Double
    ) : RealtimeControlCommand()
}

internal data class DecodedControlPacket(
    val sessionId: Long,
    val sequence: Long,
    val sentAtControlMillis: Long,
    val command: RealtimeControlCommand
)

/**
 * Authenticated V2 datagrams. A boot/arming session and unsigned 64-bit sequence
 * prevent packets from an earlier control window from being replayed.
 */
internal object ControlPacketCodec {
    const val TAG_BYTES = 16
    const val HEADER_BYTES = 28 // magic + session + sequence + monotonic timestamp
    const val STICKS_PACKET_BYTES = HEADER_BYTES + 8 + TAG_BYTES
    const val VELOCITY_PACKET_BYTES = HEADER_BYTES + 16 + TAG_BYTES

    private const val STICKS_MAGIC = 0x56535432 // VST2
    private const val VELOCITY_MAGIC = 0x56444332 // VDC2
    private const val MAX_STICK = 660
    private const val MAX_HORIZONTAL_MM_PER_SECOND = 23_000
    private const val MAX_VERTICAL_MM_PER_SECOND = 6_000
    private const val MAX_YAW_MILLIDEGREES_PER_SECOND = 100_000

    fun decode(
        data: ByteArray,
        offset: Int,
        length: Int,
        secret: SecretKeySpec
    ): DecodedControlPacket? {
        if (length != STICKS_PACKET_BYTES && length != VELOCITY_PACKET_BYTES) return null
        if (offset < 0 || length < 0 || offset > data.size - length) return null

        val payloadBytes = length - TAG_BYTES
        val expectedTag = Mac.getInstance("HmacSHA256").run {
            init(secret)
            update(data, offset, payloadBytes)
            doFinal().copyOf(TAG_BYTES)
        }
        val actualTag = data.copyOfRange(offset + payloadBytes, offset + length)
        if (!MessageDigest.isEqual(expectedTag, actualTag)) return null

        val bytes = ByteBuffer.wrap(data, offset, payloadBytes).order(ByteOrder.BIG_ENDIAN)
        val magic = bytes.int
        val sessionId = bytes.long
        val sequence = bytes.long
        val sentAt = bytes.long
        val command = when (magic) {
            STICKS_MAGIC -> {
                if (length != STICKS_PACKET_BYTES) return null
                val axes = IntArray(4) { bytes.short.toInt() }
                if (axes.any { it !in -MAX_STICK..MAX_STICK }) return null
                RealtimeControlCommand.Sticks(axes[0], axes[1], axes[2], axes[3])
            }

            VELOCITY_MAGIC -> {
                if (length != VELOCITY_PACKET_BYTES) return null
                val forward = bytes.int
                val right = bytes.int
                val up = bytes.int
                val yaw = bytes.int
                if (forward !in -MAX_HORIZONTAL_MM_PER_SECOND..MAX_HORIZONTAL_MM_PER_SECOND ||
                    right !in -MAX_HORIZONTAL_MM_PER_SECOND..MAX_HORIZONTAL_MM_PER_SECOND ||
                    up !in -MAX_VERTICAL_MM_PER_SECOND..MAX_VERTICAL_MM_PER_SECOND ||
                    yaw !in -MAX_YAW_MILLIDEGREES_PER_SECOND..MAX_YAW_MILLIDEGREES_PER_SECOND
                ) return null
                RealtimeControlCommand.BodyVelocity(
                    forward / 1_000.0,
                    right / 1_000.0,
                    up / 1_000.0,
                    yaw / 1_000.0
                )
            }

            else -> return null
        }
        return DecodedControlPacket(sessionId, sequence, sentAt, command)
    }

    fun secret(token: String): SecretKeySpec =
        SecretKeySpec(token.toByteArray(Charsets.UTF_8), "HmacSHA256")
}

/** Session-scoped unsigned sequence window. One authenticated producer owns it. */
internal class ControlSequenceWindow(initialSessionId: Long) {
    private var sessionId = initialSessionId
    private var lastSequence: Long? = null

    @Synchronized
    fun rotate(newSessionId: Long) {
        sessionId = newSessionId
        lastSequence = null
    }

    @Synchronized
    fun accept(candidateSessionId: Long, candidateSequence: Long): Boolean {
        if (candidateSessionId != sessionId) return false
        val previous = lastSequence
        if (previous != null && java.lang.Long.compareUnsigned(candidateSequence, previous) <= 0) {
            return false
        }
        lastSequence = candidateSequence
        return true
    }
}

/** Shared between the HTTP arming path and UDP receiver without widening constructors. */
internal object BridgeControlSession {
    private val random = SecureRandom()
    @Volatile private var sessionId = nextSessionId()
    private val sequences = ControlSequenceWindow(sessionId)

    init {
        publish()
    }

    @Synchronized
    fun rotate(reason: String): Long {
        sessionId = nextSessionId()
        sequences.rotate(sessionId)
        BridgeState.clearAcceptedControlPacket()
        publish()
        BridgeState.controlSessionReason.set(reason)
        return sessionId
    }

    /**
     * Sequence validation and mailbox publication share the same monitor as
     * [rotate]. Therefore an old packet either publishes completely before a
     * rotation (which then clears it), or observes the new session and fails.
     */
    @Synchronized
    fun acceptAndApply(
        candidateSessionId: Long,
        sequence: Long,
        apply: () -> Boolean
    ): Boolean {
        if (!sequences.accept(candidateSessionId, sequence)) return false
        return apply()
    }

    fun currentHex(): String = "%016x".format(sessionId)

    private fun publish() {
        BridgeState.controlSession.set(currentHex())
    }

    private fun nextSessionId(): Long {
        var value: Long
        do value = random.nextLong() while (value == 0L)
        return value
    }
}

internal object ControlPacketFreshness {
    const val MAX_AGE_MILLIS = 250L
    const val MAX_FUTURE_MILLIS = 100L

    fun ageMillis(nowControlMillis: Long, sentAtControlMillis: Long): Long =
        nowControlMillis - sentAtControlMillis

    fun isFresh(nowControlMillis: Long, sentAtControlMillis: Long): Boolean =
        ageMillis(nowControlMillis, sentAtControlMillis) in
            -MAX_FUTURE_MILLIS..MAX_AGE_MILLIS
}

/** Shared with Android Location.elapsedRealtimeNanos and telemetry freshness. */
internal fun controlMonotonicMillis(): Long = SystemClock.elapsedRealtime()
