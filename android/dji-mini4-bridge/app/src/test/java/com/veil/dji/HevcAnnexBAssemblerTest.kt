package com.veil.dji

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class HevcAnnexBAssemblerTest {
    @Test
    fun assemblesAcrossEveryCallbackBoundaryAndPreservesBytes() {
        val vps = nal(type = 32, payload = byteArrayOf(1, 2), fourByteStart = true)
        val sps = nal(type = 33, payload = byteArrayOf(3, 4), fourByteStart = false)
        val pps = nal(type = 34, payload = byteArrayOf(5), fourByteStart = true)
        val irap = nal(type = 19, payload = byteArrayOf(0x80.toByte(), 6, 7), fourByteStart = false)
        val next = nal(type = 1, payload = byteArrayOf(0x80.toByte(), 8), fourByteStart = true)
        val terminator = nal(type = 35, payload = byteArrayOf(9), fourByteStart = false)
        val stream = vps + sps + pps + irap + next + terminator
        val assembler = HevcAnnexBAssembler()
        val packets = ArrayList<HevcNalPacket>()

        // Deliberately split inside start codes, headers, and payloads.
        var offset = 0
        val fragmentSizes = intArrayOf(1, 2, 1, 5, 3, 2, 7, 1, 4, 2, 9, 1, 3, 20)
        for (size in fragmentSizes) {
            if (offset >= stream.size) break
            val end = (offset + size).coerceAtMost(stream.size)
            packets += assembler.accept(stream.copyOfRange(offset, end))
            offset = end
        }
        if (offset < stream.size) packets += assembler.accept(stream.copyOfRange(offset, stream.size))

        assertEquals(listOf(32, 33, 34, 19, 1), packets.map { it.type })
        assertArrayEquals(vps + sps + pps + irap + next, packets.fold(ByteArray(0)) { all, packet -> all + packet.bytes })
        assertTrue(packets[3].isFirstSliceIdr)
        assertFalse(packets[4].isFirstSliceIdr)
    }

    @Test
    fun onlyFirstSliceIrapCanBeJoinPoint() {
        val assembler = HevcAnnexBAssembler()
        val continuation = nal(19, byteArrayOf(0x00, 1))
        val firstSliceIdr = nal(20, byteArrayOf(0x80.toByte(), 2))
        val firstSliceCra = nal(21, byteArrayOf(0x80.toByte(), 3))
        val reservedIrapLikeType = nal(22, byteArrayOf(0x80.toByte(), 3))
        val terminator = nal(35, byteArrayOf(4))
        val packets = assembler.accept(continuation + firstSliceIdr + firstSliceCra + reservedIrapLikeType + terminator)

        assertFalse(packets[0].isFirstSliceIdr)
        assertTrue(packets[1].isFirstSliceIdr)
        assertFalse(packets[2].isFirstSliceIdr)
        assertFalse(packets[3].isFirstSliceIdr)
    }

    @Test
    fun parameterCacheRequiresAndOrdersAllThreeSets() {
        val cache = HevcParameterSetCache()
        val vps = packet(32, 1)
        val sps = packet(33, 2)
        val pps = packet(34, 3)

        cache.observe(sps)
        cache.observe(pps)
        assertNull(cache.snapshot())
        cache.observe(vps)
        assertArrayEquals(vps.bytes + sps.bytes + pps.bytes, cache.snapshot())
        cache.reset()
        assertNull(cache.snapshot())
    }

    @Test
    fun resetDropsIncompleteNalAndResynchronizes() {
        val assembler = HevcAnnexBAssembler()
        val incomplete = nal(19, byteArrayOf(0x80.toByte(), 1, 2, 3))
        assembler.accept(incomplete.copyOfRange(0, incomplete.size - 1))
        assembler.reset()

        val pps = nal(34, byteArrayOf(4))
        val next = nal(1, byteArrayOf(0x80.toByte(), 5))
        val packets = assembler.accept(pps + next)
        assertEquals(1, packets.size)
        assertEquals(34, packets.single().type)
        assertArrayEquals(pps, packets.single().bytes)
    }

    @Test
    fun boundedViewerQueueRejectsBackpressureWithoutDroppingOlderNal() {
        val queue = BoundedByteQueue(4)
        assertTrue(queue.offer(byteArrayOf(1, 2, 3, 4)))
        assertFalse(queue.offer(byteArrayOf(5)))
        assertEquals(4, queue.queuedBytes())
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), queue.take())
    }

    @Test
    fun boundedViewerQueueAdmitsBootstrapAtomicallyOrNotAtAll() {
        val queue = BoundedByteQueue(5)
        assertFalse(queue.offer(byteArrayOf(1, 2, 3), byteArrayOf(4, 5, 6)))
        assertEquals(0, queue.queuedBytes())
        assertTrue(queue.offer(byteArrayOf(7, 8), byteArrayOf(9, 10, 11)))
        assertEquals(5, queue.queuedBytes())
    }

    @Test
    fun accessUnitRateUsesIntervalsAfterBaselineWithoutFirstWindowBias() {
        val estimator = AccessUnitRateEstimator(1_000_000_000L)
        assertNull(estimator.observe(1, 10_000_000_000L))
        for (frame in 1 until 25) {
            assertNull(estimator.observe(1, 10_000_000_000L + frame * 40_000_000L))
        }
        assertEquals(
            25.0,
            estimator.observe(1, 11_000_000_000L) ?: error("rate not emitted"),
            1e-9
        )
    }

    @Test
    fun accessUnitRateResetRequiresANewBaseline() {
        val estimator = AccessUnitRateEstimator(1_000_000_000L)
        assertNull(estimator.observe(1, 1_000_000_000L))
        estimator.reset()
        assertNull(estimator.observe(1, 5_000_000_000L))
        assertEquals(
            1.0,
            estimator.observe(1, 6_000_000_000L) ?: error("rate not emitted"),
            1e-9
        )
    }

    private fun packet(type: Int, marker: Int): HevcNalPacket =
        HevcNalPacket(nal(type, byteArrayOf(marker.toByte())), type, false)

    private fun nal(type: Int, payload: ByteArray, fourByteStart: Boolean = true): ByteArray {
        val start = if (fourByteStart) byteArrayOf(0, 0, 0, 1) else byteArrayOf(0, 0, 1)
        // layer_id = 0 and temporal_id_plus1 = 1.
        return start + byteArrayOf((type shl 1).toByte(), 1) + payload
    }
}
