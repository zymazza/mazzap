package com.veil.dji

import dji.sdk.DJIVideoManager
import dji.sdk.common.CallBack2
import dji.sdk.keyvalue.key.AirLinkKey
import dji.sdk.keyvalue.key.CameraKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.common.EmptyMsg
import dji.sdk.keyvalue.value.media.VideoBufferInfo
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.KeyManager
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.ServerSocket
import java.net.Socket
import java.util.ArrayDeque
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** Relays the Mini 4 Pro's native Annex-B HEVC stream without decoding it. */
class RawVideoRelay(
    private val token: String,
    private val port: Int = 8766
) {
    private val videoManager by lazy { DJIVideoManager.getInstance() }
    private val clientLock = Any()
    private val clients = LinkedHashSet<VideoClient>()
    private val pendingClients = LinkedHashSet<VideoClient>()
    private val assemblerLock = Any()
    private val assembler = HevcAnnexBAssembler()
    private val nalPackets = ArrayBlockingQueue<QueuedHevcNal>(NAL_QUEUE_CAPACITY)
    private val observerEpoch = AtomicLong(0L)
    private val streamEpoch = AtomicLong(0L)
    private val executor = Executors.newFixedThreadPool(3)
    private val clientSenderExecutor = Executors.newCachedThreadPool()
    private val iframeRetryExecutor = Executors.newSingleThreadScheduledExecutor()

    @Volatile private var server: ServerSocket? = null
    @Volatile private var observerAttached = false
    @Volatile private var lastAttachNanos = 0L
    @Volatile private var lastDataNanos = 0L
    @Volatile private var lastIFrameRequestNanos = 0L
    @Volatile private var iframeRequestInFlight = false
    private var lastSourceTimestamp: Long? = null
    private var lastCallbackNanos = 0L
    private var rateWindowStartNanos = 0L
    private var rateWindowCallbacks = 0L
    private var rateWindowBytes = 0L
    private val accessUnitRateEstimator = AccessUnitRateEstimator(RATE_WINDOW_NANOS)

    fun start() {
        executor.execute { acceptClients() }
        executor.execute { broadcast() }
        iframeRetryExecutor.scheduleWithFixedDelay(
            {
                // Observer recovery belongs to the relay lifecycle, not the
                // status Activity lifecycle. The BOOX may dim, rotate, or
                // recreate its UI while this foreground bridge keeps running.
                try {
                    if (BridgeState.productConnected.get()) ensureMainCameraObserver()
                    if (observerAttached && hasPendingClients()) requestIFrame()
                } catch (error: Exception) {
                    // Scheduled executors suppress every later run when a task
                    // throws. Keep recovery alive and expose the transient.
                    BridgeState.lastEvent.set(
                        "video_health_check_failed:${error.javaClass.simpleName}:${error.message}"
                    )
                }
            },
            IFRAME_RETRY_POLL_MILLIS,
            IFRAME_RETRY_POLL_MILLIS,
            TimeUnit.MILLISECONDS
        )
    }

    @Synchronized
    fun ensureMainCameraObserver(force: Boolean = false) {
        val now = System.nanoTime()
        val dataIsFresh = lastDataNanos != 0L && now - lastDataNanos < OBSERVER_STALE_NANOS
        val attachIsRecent = lastAttachNanos != 0L && now - lastAttachNanos < ATTACH_RETRY_NANOS
        if (!force && observerAttached && (dataIsFresh || attachIsRecent)) return

        if (observerAttached) {
            observerAttached = false
            videoManager.removeVideoObserver(MAIN_VIDEO_CHANNEL)
        }

        val callbackEpoch = observerEpoch.incrementAndGet()
        resetStreamSynchronization("video_observer_reset")
        BridgeState.videoCodec.set("H265")
        videoManager.setVideoObserver(
            MAIN_VIDEO_CHANNEL,
            CallBack2<VideoBufferInfo, ByteArray> { info, data ->
                receiveRawData(callbackEpoch, info?.timestamp, data)
            }
        )
        observerAttached = true
        lastAttachNanos = now
        BridgeState.availableCameras.set("MAIN:raw-channel-$MAIN_VIDEO_CHANNEL")
        BridgeState.lastEvent.set("video_raw_channel:$MAIN_VIDEO_CHANNEL")
        if (hasPendingClients()) requestIFrame()
    }

    @Synchronized
    fun detachMainCameraObserver() {
        observerEpoch.incrementAndGet()
        if (observerAttached) videoManager.removeVideoObserver(MAIN_VIDEO_CHANNEL)
        observerAttached = false
        lastAttachNanos = 0L
        lastDataNanos = 0L
        resetStreamSynchronization("video_observer_detached")
    }

    private fun receiveRawData(callbackEpoch: Long, sourceTimestamp: Long?, data: ByteArray) {
        if (data.isEmpty() || callbackEpoch != observerEpoch.get()) return
        var queueOverflow = false
        synchronized(assemblerLock) {
            if (callbackEpoch != observerEpoch.get()) return
            val now = System.nanoTime()
            lastDataNanos = now
            recordIngressMeasurement(sourceTimestamp, data.size, now)
            BridgeState.videoCodec.set("H265")
            BridgeState.videoBytes.addAndGet(data.size.toLong())
            val packets = assembler.accept(data)
            recordAccessUnitMeasurements(packets, now)
            val callbackStreamEpoch = streamEpoch.get()

            // DJI does not promise that observer callbacks cannot overlap.
            // Assembly and publication therefore share one critical section:
            // otherwise callback B can enqueue its first NAL between two NALs
            // already assembled from callback A, corrupting reference-picture
            // order without incrementing any queue-drop counter.
            for ((index, packet) in packets.withIndex()) {
                if (
                    callbackStreamEpoch != streamEpoch.get() ||
                    callbackEpoch != observerEpoch.get()
                ) return
                if (!nalPackets.offer(QueuedHevcNal(callbackStreamEpoch, packet))) {
                    val notPublished = packets.size - index
                    val dropped = nalPackets.size.toLong() + notPublished
                    BridgeState.videoDroppedChunks.addAndGet(dropped)
                    queueOverflow = true
                    break
                }
            }
        }
        if (queueOverflow) {
            resetStreamSynchronization("video_nal_queue_overflow")
            if (observerAttached && hasPendingClients()) requestIFrame()
        }
    }

    private fun resetStreamSynchronization(event: String) {
        synchronized(assemblerLock) {
            streamEpoch.incrementAndGet()
            assembler.reset()
            nalPackets.clear()
            resetIngressMeasurements()
        }
        // Bytes already accepted by TCP cannot be retracted. Close active
        // sessions on a source discontinuity so the Mac reconnects with an
        // empty socket and decoder at a newly requested IDR. Pending clients
        // have not received stream bytes and can remain behind the IDR gate.
        val staleClients = synchronized(clientLock) {
            val stale = clients.toList()
            clients.clear()
            updateClientCountLocked()
            stale
        }
        staleClients.forEach(VideoClient::close)
        resetIFrameRequestState()
        BridgeState.lastEvent.set(event)
    }

    private fun acceptClients() {
        try {
            server = ServerSocket(port)
            while (server?.isClosed == false) {
                val client = server?.accept() ?: break
                executor.execute { authenticate(client) }
            }
        } catch (error: Exception) {
            if (server?.isClosed == false) BridgeState.lastEvent.set("video_server_failed:${error.message}")
        }
    }

    private fun authenticate(client: Socket) {
        try {
            client.soTimeout = 2_000
            val line = BufferedReader(InputStreamReader(client.getInputStream())).readLine()
            if (line != "TOKEN $token") {
                client.close()
                return
            }
            client.soTimeout = 0
            client.tcpNoDelay = true
            // A large kernel send buffer hides a slow viewer and turns latency
            // into an ever-growing archive. The Mac client reconnects at a
            // fresh IDR if this small bounded path applies backpressure.
            client.sendBufferSize = 64 * 1024
            val viewer = VideoClient(client, VIDEO_CLIENT_QUEUE_BYTES)

            val firstPending = synchronized(clientLock) {
                val wasEmpty = pendingClients.isEmpty()
                pendingClients.add(viewer)
                updateClientCountLocked()
                wasEmpty
            }
            try {
                clientSenderExecutor.execute { sendToClient(viewer) }
            } catch (_: Exception) {
                synchronized(clientLock) {
                    pendingClients.remove(viewer)
                    updateClientCountLocked()
                }
                viewer.close()
                return
            }
            if (firstPending) requestIFrame()
        } catch (_: Exception) {
            client.close()
        }
    }

    private fun sendToClient(client: VideoClient) {
        try {
            val output = client.socket.getOutputStream()
            while (!Thread.currentThread().isInterrupted) {
                val block = client.take() ?: break
                output.write(block)
            }
        } catch (_: Exception) {
            // A bounded queue or a socket error removes only this viewer. The
            // Mac reconnect path will authenticate and request a fresh IDR.
        } finally {
            client.close()
            synchronized(clientLock) {
                clients.remove(client)
                pendingClients.remove(client)
                updateClientCountLocked()
            }
        }
    }

    @Synchronized
    private fun requestIFrame() {
        if (!observerAttached || !hasPendingClients()) return
        val now = System.nanoTime()
        if (iframeRequestInFlight && now - lastIFrameRequestNanos < IFRAME_REQUEST_TIMEOUT_NANOS) return
        if (now - lastIFrameRequestNanos < IFRAME_REQUEST_COOLDOWN_NANOS) return
        iframeRequestInFlight = true
        lastIFrameRequestNanos = now
        BridgeState.videoIFrameRequests.incrementAndGet()

        try {
            val key = KeyTools.createKey(AirLinkKey.KeyM300RTKRequestIFrame)
            KeyManager.getInstance().performAction(
                key,
                MAIN_VIDEO_CHANNEL,
                object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                    override fun onSuccess(result: EmptyMsg) {
                        finishIFrameRequest("video_iframe_requested:airlink")
                    }

                    override fun onFailure(error: IDJIError) {
                        requestCameraIFrame(error.toString())
                    }
                }
            )
        } catch (error: Exception) {
            // Mini 4 Pro firmware may reject the generic AirLink action
            // synchronously before a callback exists. Use the same camera-key
            // fallback as the asynchronous failure path.
            requestCameraIFrame("airlink_exception:${error.message}")
        }
    }

    private fun requestCameraIFrame(airLinkError: String) {
        try {
            val key = KeyTools.createKey(
                CameraKey.KeyAppRequestIFrame,
                ComponentIndexType.LEFT_OR_MAIN
            )
            KeyManager.getInstance().performAction(
                key,
                EmptyMsg(),
                object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                    override fun onSuccess(result: EmptyMsg) {
                        finishIFrameRequest("video_iframe_requested:camera")
                    }

                    override fun onFailure(error: IDJIError) {
                        finishIFrameRequest("video_iframe_request_failed:$airLinkError:$error")
                    }
                }
            )
        } catch (error: Exception) {
            finishIFrameRequest("video_iframe_request_failed:$airLinkError:${error.message}")
        }
    }

    @Synchronized
    private fun finishIFrameRequest(event: String) {
        iframeRequestInFlight = false
        BridgeState.lastEvent.set(event)
    }

    @Synchronized
    private fun resetIFrameRequestState() {
        iframeRequestInFlight = false
        lastIFrameRequestNanos = 0L
    }

    private fun broadcast() {
        val parameterSets = HevcParameterSetCache()
        var cacheEpoch = Long.MIN_VALUE

        while (!Thread.currentThread().isInterrupted) {
            try {
                val queued = nalPackets.take()
                if (queued.streamEpoch != streamEpoch.get()) continue
                if (cacheEpoch != queued.streamEpoch) {
                    parameterSets.reset()
                    cacheEpoch = queued.streamEpoch
                }

                val packet = queued.packet
                parameterSets.observe(packet)
                BridgeState.videoNalUnits.incrementAndGet()
                val bootstrap = if (packet.isFirstSliceIdr) parameterSets.snapshot() else null
                var joined = 0
                var missingParameterSets = false
                var currentEpoch = true
                val rejected = ArrayList<VideoClient>()
                val joinedNow = HashSet<VideoClient>()

                synchronized(clientLock) {
                    if (queued.streamEpoch != streamEpoch.get()) {
                        currentEpoch = false
                    } else {
                        if (packet.isFirstSliceIdr && pendingClients.isNotEmpty()) {
                            if (bootstrap == null) {
                                missingParameterSets = true
                            } else {
                                val iterator = pendingClients.iterator()
                                while (iterator.hasNext()) {
                                    val client = iterator.next()
                                    if (client.offerAll(bootstrap, packet.bytes)) {
                                        iterator.remove()
                                        clients.add(client)
                                        joinedNow.add(client)
                                        joined++
                                    } else {
                                        iterator.remove()
                                        rejected.add(client)
                                    }
                                }
                            }
                        }

                        val iterator = clients.iterator()
                        while (iterator.hasNext()) {
                            val client = iterator.next()
                            // A viewer admitted above already has this IDR
                            // atomically queued after its parameter sets.
                            if (client !in joinedNow && !client.offer(packet.bytes)) {
                                iterator.remove()
                                rejected.add(client)
                            }
                        }
                        updateClientCountLocked()
                    }
                }
                if (!currentEpoch) continue
                // Socket close happens outside clientLock. It unblocks a
                // sender stuck in kernel backpressure without ever wedging the
                // broadcaster or observer-reset path.
                if (rejected.isNotEmpty()) {
                    rejected.forEach(VideoClient::close)
                    BridgeState.videoClientQueueRejections.addAndGet(rejected.size.toLong())
                }

                if (joined > 0) {
                    BridgeState.videoIrapJoins.addAndGet(joined.toLong())
                    BridgeState.videoIdrJoins.addAndGet(joined.toLong())
                    BridgeState.lastEvent.set("video_clients_joined_idr:$joined")
                } else if (missingParameterSets) {
                    BridgeState.lastEvent.set("video_idr_missing_parameter_sets")
                }
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
    }

    private fun hasPendingClients(): Boolean = synchronized(clientLock) {
        pendingClients.isNotEmpty()
    }

    private fun updateClientCountLocked() {
        BridgeState.videoClients.set(clients.size + pendingClients.size)
        BridgeState.videoPendingClients.set(pendingClients.size)
    }

    /** Must be called under assemblerLock. DJI timestamp units are unknown. */
    private fun recordIngressMeasurement(sourceTimestamp: Long?, bytes: Int, now: Long) {
        BridgeState.videoCallbackMonotonicNs.set(now)
        if (lastCallbackNanos != 0L) {
            BridgeState.videoCallbackIntervalUs.set((now - lastCallbackNanos).coerceAtLeast(0L) / 1_000L)
        }
        lastCallbackNanos = now

        if (sourceTimestamp != null) {
            BridgeState.videoSourceTimestampRaw.set(sourceTimestamp)
            lastSourceTimestamp?.let { previous ->
                // Preserve signed deltas: a backwards jump is useful evidence
                // while DJI's timestamp epoch/unit remains undocumented.
                BridgeState.videoSourceTimestampDeltaRaw.set(sourceTimestamp - previous)
            }
            lastSourceTimestamp = sourceTimestamp
            BridgeState.videoTimestampSamples.incrementAndGet()
        }

        if (rateWindowStartNanos == 0L) rateWindowStartNanos = now
        rateWindowCallbacks++
        rateWindowBytes += bytes
        val elapsed = now - rateWindowStartNanos
        if (elapsed >= RATE_WINDOW_NANOS) {
            BridgeState.videoCallbackRateHz.set(rateWindowCallbacks * 1_000_000_000.0 / elapsed)
            BridgeState.videoIngressBytesPerSecond.set(rateWindowBytes * 1_000_000_000.0 / elapsed)
            rateWindowStartNanos = now
            rateWindowCallbacks = 0L
            rateWindowBytes = 0L
        }
    }

    /** Must be called under assemblerLock. One first-slice VCL NAL is one picture. */
    private fun recordAccessUnitMeasurements(packets: List<HevcNalPacket>, now: Long) {
        val accessUnits = packets.count { it.firstSlice }.toLong()
        if (accessUnits == 0L) return
        BridgeState.videoAccessUnits.addAndGet(accessUnits)
        BridgeState.videoAccessUnitMonotonicNs.set(now)
        accessUnitRateEstimator.observe(accessUnits, now)?.let(
            BridgeState.videoAccessUnitRateHz::set
        )
    }

    /** Must be called under assemblerLock. */
    private fun resetIngressMeasurements() {
        lastSourceTimestamp = null
        lastCallbackNanos = 0L
        rateWindowStartNanos = 0L
        rateWindowCallbacks = 0L
        rateWindowBytes = 0L
        accessUnitRateEstimator.reset()
        BridgeState.videoSourceTimestampRaw.set(Long.MIN_VALUE)
        BridgeState.videoSourceTimestampDeltaRaw.set(Long.MIN_VALUE)
        BridgeState.videoCallbackMonotonicNs.set(-1L)
        BridgeState.videoCallbackIntervalUs.set(-1L)
        BridgeState.videoCallbackRateHz.set(Double.NaN)
        BridgeState.videoIngressBytesPerSecond.set(Double.NaN)
        BridgeState.videoAccessUnitMonotonicNs.set(-1L)
        BridgeState.videoAccessUnitRateHz.set(Double.NaN)
    }

    fun stop() {
        server?.close()
        detachMainCameraObserver()
        iframeRetryExecutor.shutdownNow()
        executor.shutdownNow()
        val viewers = synchronized(clientLock) {
            val all = (clients + pendingClients).toList()
            clients.clear()
            pendingClients.clear()
            updateClientCountLocked()
            all
        }
        viewers.forEach(VideoClient::close)
        clientSenderExecutor.shutdownNow()
        nalPackets.clear()
    }

    private companion object {
        const val MAIN_VIDEO_CHANNEL = 0
        const val NAL_QUEUE_CAPACITY = 256
        const val VIDEO_CLIENT_QUEUE_BYTES = 512 * 1024
        const val ATTACH_RETRY_NANOS = 3_000_000_000L
        const val OBSERVER_STALE_NANOS = 3_000_000_000L
        const val IFRAME_RETRY_POLL_MILLIS = 250L
        const val IFRAME_REQUEST_COOLDOWN_NANOS = 1_000_000_000L
        const val IFRAME_REQUEST_TIMEOUT_NANOS = 1_500_000_000L
        const val RATE_WINDOW_NANOS = 1_000_000_000L
    }
}

/**
 * Measures completed-picture cadence using intervals after the first sample.
 * Counting the baseline picture against zero elapsed intervals biases the
 * first one-second window upward (for example, 25 fps appears as 26 fps).
 */
internal class AccessUnitRateEstimator(private val windowNanos: Long) {
    private var windowStartNanos = 0L
    private var intervals = 0L

    init {
        require(windowNanos > 0L)
    }

    fun observe(accessUnits: Long, nowNanos: Long): Double? {
        require(accessUnits > 0L)
        if (windowStartNanos == 0L) {
            windowStartNanos = nowNanos
            return null
        }
        intervals += accessUnits
        val elapsed = nowNanos - windowStartNanos
        if (elapsed < windowNanos) return null
        val rate = intervals * 1_000_000_000.0 / elapsed.coerceAtLeast(1L)
        windowStartNanos = nowNanos
        intervals = 0L
        return rate
    }

    fun reset() {
        windowStartNanos = 0L
        intervals = 0L
    }
}

private data class QueuedHevcNal(
    val streamEpoch: Long,
    val packet: HevcNalPacket
)

/** A byte-bounded FIFO. Rejection is atomic so a bootstrap is never partial. */
internal class BoundedByteQueue(private val maxBytes: Int) {
    private val lock = ReentrantLock()
    private val notEmpty = lock.newCondition()
    private val blocks = ArrayDeque<ByteArray>()
    private var queuedBytes = 0
    private var closed = false

    init {
        require(maxBytes > 0)
    }

    fun offer(vararg newBlocks: ByteArray): Boolean = lock.withLock {
        val additionalBytes = newBlocks.sumOf { it.size.toLong() }
        if (
            closed || additionalBytes > maxBytes.toLong() ||
            queuedBytes.toLong() + additionalBytes > maxBytes.toLong()
        ) {
            return false
        }
        newBlocks.forEach(blocks::addLast)
        queuedBytes += additionalBytes.toInt()
        notEmpty.signal()
        true
    }

    @Throws(InterruptedException::class)
    fun take(): ByteArray? = lock.withLock {
        while (!closed && blocks.isEmpty()) notEmpty.await()
        if (blocks.isEmpty()) return null
        val block = blocks.removeFirst()
        queuedBytes -= block.size
        block
    }

    fun close() = lock.withLock {
        closed = true
        blocks.clear()
        queuedBytes = 0
        notEmpty.signalAll()
    }

    internal fun queuedBytes(): Int = lock.withLock { queuedBytes }
}

private class VideoClient(
    val socket: Socket,
    maxQueuedBytes: Int
) {
    private val blocks = BoundedByteQueue(maxQueuedBytes)

    fun offer(block: ByteArray): Boolean = blocks.offer(block)

    fun offerAll(vararg newBlocks: ByteArray): Boolean = blocks.offer(*newBlocks)

    @Throws(InterruptedException::class)
    fun take(): ByteArray? = blocks.take()

    fun close() {
        blocks.close()
        try {
            socket.close()
        } catch (_: Exception) {
            // Already closed or being closed concurrently by the sender.
        }
    }
}

internal data class HevcNalPacket(
    val bytes: ByteArray,
    val type: Int,
    val firstSlice: Boolean
) {
    val isFirstSliceIdr: Boolean
        get() = type in 19..20 && firstSlice
}

/** Assembles exact, complete Annex-B NAL packets across arbitrary callbacks. */
internal class HevcAnnexBAssembler {
    private var pending = ByteArray(0)

    fun accept(data: ByteArray): List<HevcNalPacket> {
        if (data.isEmpty()) return emptyList()
        val combined = ByteArray(pending.size + data.size)
        pending.copyInto(combined)
        data.copyInto(combined, pending.size)
        val starts = findStartCodes(combined)

        if (starts.isEmpty()) {
            pending = combined.copyOfRange((combined.size - 3).coerceAtLeast(0), combined.size)
            return emptyList()
        }

        val packets = ArrayList<HevcNalPacket>((starts.size - 1).coerceAtLeast(0))
        for (index in 0 until starts.lastIndex) {
            createPacket(combined, starts[index], starts[index + 1])?.let(packets::add)
        }
        pending = combined.copyOfRange(starts.last(), combined.size)
        if (pending.size > MAX_NAL_BYTES) {
            pending = pending.copyOfRange((pending.size - 3).coerceAtLeast(0), pending.size)
        }
        return packets
    }

    fun reset() {
        pending = ByteArray(0)
    }

    private fun createPacket(data: ByteArray, start: Int, end: Int): HevcNalPacket? {
        val startCodeLength = startCodeLength(data, start, end) ?: return null
        val header = start + startCodeLength
        if (header + 1 >= end) return null
        val type = (data[header].toInt() ushr 1) and 0x3f
        val firstSlice = type in 0..31 && header + 2 < end &&
            (data[header + 2].toInt() and 0x80) != 0
        return HevcNalPacket(data.copyOfRange(start, end), type, firstSlice)
    }

    private fun startCodeLength(data: ByteArray, start: Int, end: Int): Int? = when {
        start + 2 < end && data[start] == 0.toByte() && data[start + 1] == 0.toByte() &&
            data[start + 2] == 1.toByte() -> 3
        start + 3 < end && data[start] == 0.toByte() && data[start + 1] == 0.toByte() &&
            data[start + 2] == 0.toByte() && data[start + 3] == 1.toByte() -> 4
        else -> null
    }

    private fun findStartCodes(data: ByteArray): List<Int> {
        val result = ArrayList<Int>()
        var index = 0
        while (index + 2 < data.size) {
            when {
                index + 3 < data.size && data[index] == 0.toByte() &&
                    data[index + 1] == 0.toByte() && data[index + 2] == 0.toByte() &&
                    data[index + 3] == 1.toByte() -> {
                    result.add(index)
                    index += 4
                }
                data[index] == 0.toByte() && data[index + 1] == 0.toByte() &&
                    data[index + 2] == 1.toByte() -> {
                    result.add(index)
                    index += 3
                }
                else -> index++
            }
        }
        return result
    }

    private companion object {
        const val MAX_NAL_BYTES = 16 * 1024 * 1024
    }
}

/** Cache is intentionally owned and accessed only by the broadcaster thread. */
internal class HevcParameterSetCache {
    private var vps: ByteArray? = null
    private var sps: ByteArray? = null
    private var pps: ByteArray? = null

    fun observe(packet: HevcNalPacket) {
        when (packet.type) {
            32 -> vps = packet.bytes.copyOf()
            33 -> sps = packet.bytes.copyOf()
            34 -> pps = packet.bytes.copyOf()
        }
    }

    fun snapshot(): ByteArray? {
        val currentVps = vps ?: return null
        val currentSps = sps ?: return null
        val currentPps = pps ?: return null
        val result = ByteArray(currentVps.size + currentSps.size + currentPps.size)
        currentVps.copyInto(result)
        currentSps.copyInto(result, currentVps.size)
        currentPps.copyInto(result, currentVps.size + currentSps.size)
        return result
    }

    fun reset() {
        vps = null
        sps = null
        pps = null
    }
}
