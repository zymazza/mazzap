package com.veil.dji

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Authenticated latest-state UDP control channel. V2 packets are bound to the
 * current arming session, use unsigned 64-bit sequencing, and carry the bridge's
 * monotonic timestamp so delayed commands are rejected before reaching DJI.
 */
class RealtimeControlServer(
    token: String,
    private val controller: FlightController,
    private val port: Int = 8767
) {
    private val secret = ControlPacketCodec.secret(token)
    private val executor = Executors.newSingleThreadExecutor()
    private val stopped = AtomicBoolean(false)
    @Volatile private var socket: DatagramSocket? = null

    fun start() {
        // Initialize and publish the session before clients fetch /status.
        BridgeControlSession.currentHex()
        executor.execute {
            try {
                socket = DatagramSocket(port)
                val buffer = ByteArray(512)
                while (socket?.isClosed == false) {
                    val packet = DatagramPacket(buffer, buffer.size)
                    socket?.receive(packet)
                    if (!accept(packet.data, packet.offset, packet.length)) {
                        BridgeState.controlRejectedPackets.incrementAndGet()
                    }
                }
            } catch (error: Exception) {
                if (!stopped.get()) {
                    BridgeState.lastEvent.set("control_udp_failed:${error.message}")
                    controller.onControlTransportStopped()
                }
            }
        }
    }

    private fun accept(data: ByteArray, offset: Int, length: Int): Boolean {
        val packet = ControlPacketCodec.decode(data, offset, length, secret) ?: return false
        val receivedAt = controlMonotonicMillis()
        if (!ControlPacketFreshness.isFresh(receivedAt, packet.sentAtControlMillis)) return false
        var appliedAt = receivedAt
        val applied = BridgeControlSession.acceptAndApply(
            packet.sessionId,
            packet.sequence
        ) {
            // Recheck after waiting for any concurrent session rotation. The
            // critical section only validates state and publishes atomics; it
            // never calls DJI or waits for a callback.
            appliedAt = controlMonotonicMillis()
            if (!ControlPacketFreshness.isFresh(appliedAt, packet.sentAtControlMillis)) {
                false
            } else {
                val accepted = when (val command = packet.command) {
                    is RealtimeControlCommand.Sticks -> controller.submitSticks(
                        command.leftHorizontal,
                        command.leftVertical,
                        command.rightHorizontal,
                        command.rightVertical
                    )

                    is RealtimeControlCommand.BodyVelocity -> controller.submitBodyVelocity(
                        command.forwardMetersPerSecond,
                        command.rightMetersPerSecond,
                        command.upMetersPerSecond,
                        command.yawRateDegreesPerSecond
                    )
                }
                if (accepted) {
                    // Publish the acknowledgement under the same session lock
                    // as sequence validation and mailbox publication. A later
                    // session rotation therefore cannot expose this packet as
                    // an acknowledgement for the new control session.
                    BridgeState.recordAcceptedControlPacket(
                        sessionId = packet.sessionId,
                        sequence = packet.sequence,
                        sentAtControlMonotonicMs = packet.sentAtControlMillis,
                        receivedAtControlMonotonicMs = receivedAt,
                        appliedAtControlMonotonicMs = appliedAt,
                        setpoint = packet.command.toAcceptedSetpoint()
                    )
                    BridgeState.controlPackets.incrementAndGet()
                }
                accepted
            }
        }
        return applied
    }

    fun stop() {
        if (!stopped.compareAndSet(false, true)) return
        socket?.close()
        controller.onControlTransportStopped()
        executor.shutdownNow()
    }
}
