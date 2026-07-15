package com.veil.dji

import android.os.SystemClock
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/** Authenticated persistent newline-delimited JSON telemetry at 20 Hz. */
class TelemetryServer(
    private val token: String,
    private val port: Int = 8768
) {
    private val executor = Executors.newCachedThreadPool()
    private val clients = CopyOnWriteArrayList<TelemetryClient>()
    private val sequence = AtomicLong(0L)
    @Volatile private var server: ServerSocket? = null

    fun start() {
        executor.execute {
            try {
                server = ServerSocket(port)
                while (server?.isClosed == false) {
                    server?.accept()?.let { client -> executor.execute { authenticate(client) } }
                }
            } catch (error: Exception) {
                if (server?.isClosed == false) BridgeState.lastEvent.set("telemetry_server_failed:${error.message}")
            }
        }
        executor.execute { publish() }
    }

    private fun authenticate(client: Socket) {
        var telemetryClient: TelemetryClient? = null
        try {
            client.tcpNoDelay = true
            client.keepAlive = true
            // Current full status snapshots are about 30 KiB. Request room for one complete
            // frame, not an unbounded multi-frame FIFO hidden in the TCP send buffer.
            client.sendBufferSize = REQUESTED_SEND_BUFFER_BYTES
            client.soTimeout = 2_000
            if (BufferedReader(InputStreamReader(client.getInputStream())).readLine() != "TOKEN $token") {
                client.close()
                return
            }
            client.soTimeout = 0
            val session = TelemetryClient(client)
            telemetryClient = session
            clients.add(session)
            BridgeState.telemetryClients.set(clients.size)
            executor.execute { writeClient(session) }
        } catch (_: Exception) {
            telemetryClient?.let(::closeClient) ?: runCatching { client.close() }
        }
    }

    /**
     * Give every client its own writer. The 20 Hz publisher must never wait on
     * a slow socket because doing so would stall telemetry for every observer.
     * Telemetry is current-state data, so one pending snapshot is sufficient:
     * replace an obsolete pending snapshot with the newest one, and disconnect
     * a client that remains backpressured for two seconds.
     */
    private fun writeClient(client: TelemetryClient) {
        try {
            val output = client.socket.getOutputStream()
            while (!Thread.currentThread().isInterrupted && !client.closed.get()) {
                val frame = client.pending.poll(1, TimeUnit.SECONDS) ?: continue
                val writeStartedAt = SystemClock.elapsedRealtime()
                val sequenceGap = telemetrySequenceGap(
                    client.lastCompletedWriteSequence,
                    frame.sequence
                )
                val line = frame.renderForWrite(
                    writeStartedAtMonotonicMillis = writeStartedAt,
                    sequenceGapBeforeWrite = sequenceGap,
                    tcpSendBufferBytes = client.sendBufferBytes
                )
                output.write(line)
                client.lastCompletedWriteSequence = frame.sequence
                client.consecutiveReplacements.set(0)
            }
        } catch (_: Exception) {
            // Socket loss and forced slow-client disconnect both end here.
        } finally {
            closeClient(client)
        }
    }

    private fun publish() {
        var nextDeadline = SystemClock.elapsedRealtime()
        while (!Thread.currentThread().isInterrupted) {
            val beforeDeadline = SystemClock.elapsedRealtime()
            if (beforeDeadline < nextDeadline) {
                try {
                    Thread.sleep(nextDeadline - beforeDeadline)
                } catch (_: InterruptedException) {
                    Thread.currentThread().interrupt()
                    break
                }
            }
            try {
                val generatedAt = SystemClock.elapsedRealtime()
                val generatedWallTime = System.currentTimeMillis()
                val frameSequence = sequence.incrementAndGet()
                val line = BridgeState.toJson()
                    // Keep the legacy names as aliases for snapshot generation time.
                    .put("monotonic_ms", generatedAt)
                    .put("wall_time_ms", generatedWallTime)
                    .put("telemetry_sequence", frameSequence)
                    .put("telemetry_generated_monotonic_ms", generatedAt)
                    .put("telemetry_generated_wall_time_ms", generatedWallTime)
                    .put("telemetry_publish_hz", TelemetryCadence.PUBLISH_HZ)
                    .put("telemetry_period_ms", TelemetryCadence.PERIOD_MILLIS)
                    .put("telemetry_transport", "tcp_ndjson")
                    .put("telemetry_application_queue", "latest_only")
                    .put("telemetry_pending_capacity", 1)
                    .put("telemetry_tcp_nodelay", true)
                    .put("telemetry_tcp_send_buffer_requested_bytes", REQUESTED_SEND_BUFFER_BYTES)
                    .toString()
                val frame = TelemetryFrame(frameSequence, generatedAt, line)
                clients.forEach { client ->
                    if (client.pending.offerLatest(frame)) {
                        BridgeState.telemetryDroppedUpdates.incrementAndGet()
                        if (client.consecutiveReplacements.incrementAndGet() >=
                            MAX_CONSECUTIVE_REPLACEMENTS
                        ) {
                            BridgeState.telemetrySlowClientDisconnects.incrementAndGet()
                            closeClient(client)
                        }
                    }
                }
            } catch (error: Exception) {
                // Malformed or temporarily unavailable SDK telemetry must not
                // permanently kill the sole 20 Hz publisher task.
                BridgeState.lastEvent.set(
                    "telemetry_publish_failed:${error.javaClass.simpleName}:${error.message.orEmpty()}"
                )
            }
            nextDeadline = TelemetryCadence.nextDeadlineMillis(
                previousDeadlineMillis = nextDeadline,
                completedAtMillis = SystemClock.elapsedRealtime()
            )
        }
    }

    fun stop() {
        server?.close()
        clients.forEach { closeClient(it) }
        clients.clear()
        BridgeState.telemetryClients.set(0)
        executor.shutdownNow()
    }

    private fun closeClient(client: TelemetryClient) {
        if (!client.closed.compareAndSet(false, true)) return
        clients.remove(client)
        BridgeState.telemetryClients.set(clients.size)
        runCatching { client.socket.close() }
    }

    private class TelemetryClient(val socket: Socket) {
        val pending = LatestValueMailbox<TelemetryFrame>()
        val consecutiveReplacements = AtomicInteger(0)
        val closed = AtomicBoolean(false)
        val sendBufferBytes = socket.sendBufferSize
        var lastCompletedWriteSequence: Long? = null
    }

    private companion object {
        /** 40 missed 20 Hz deliveries is a persistently stalled observer. */
        const val MAX_CONSECUTIVE_REPLACEMENTS = 40
        const val REQUESTED_SEND_BUFFER_BYTES = 64 * 1_024
    }
}
