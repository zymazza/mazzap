package com.veil.dji

import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CameraGimbalPolicyTest {
    @Test
    fun runtimeChoicesRejectModelWideEnumValuesNotReportedByAircraft() {
        Mini4CameraGimbalPolicy.requireRuntimeChoice(
            "camera mode",
            "VIDEO_NORMAL",
            listOf("PHOTO_NORMAL", "VIDEO_NORMAL")
        )

        val error = runCatching {
            Mini4CameraGimbalPolicy.requireRuntimeChoice(
                "camera mode",
                "PHOTO_PANORAMA",
                listOf("PHOTO_NORMAL", "VIDEO_NORMAL")
            )
        }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
        assertTrue(error?.message.orEmpty().contains("runtime range"))
    }

    @Test
    fun continuousAndDiscreteZoomUseLiveGearsDifferently() {
        Mini4CameraGimbalPolicy.requireZoomRatio(2.5, continuous = true, intArrayOf(1, 2, 4))
        Mini4CameraGimbalPolicy.requireZoomRatio(2.0, continuous = false, intArrayOf(1, 2, 4))

        assertTrue(runCatching {
            Mini4CameraGimbalPolicy.requireZoomRatio(2.5, false, intArrayOf(1, 2, 4))
        }.isFailure)
        assertTrue(runCatching {
            Mini4CameraGimbalPolicy.requireZoomRatio(4.1, true, intArrayOf(1, 2, 4))
        }.isFailure)
    }

    @Test
    fun destructiveFormatRequiresStorageSpecificConfirmation() {
        Mini4CameraGimbalPolicy.requireFormatConfirmation("SDCARD", "FORMAT_STORAGE:SDCARD")
        assertTrue(runCatching {
            Mini4CameraGimbalPolicy.requireFormatConfirmation("SDCARD", "yes")
        }.isFailure)
        assertTrue(runCatching {
            Mini4CameraGimbalPolicy.requireFormatConfirmation("INTERNAL", "FORMAT_STORAGE:SDCARD")
        }.isFailure)
    }

    @Test
    fun normalizedFocusAndManualWhiteBalanceAreGuarded() {
        Mini4CameraGimbalPolicy.requireFocusTarget(0.0, 1.0)
        Mini4CameraGimbalPolicy.requireWhiteBalance("MANUAL", 5_600)

        assertTrue(runCatching { Mini4CameraGimbalPolicy.requireFocusTarget(-0.01, 0.5) }.isFailure)
        assertTrue(runCatching {
            Mini4CameraGimbalPolicy.requireWhiteBalance("MANUAL", 12_000)
        }.isFailure)
    }

    @Test
    fun gimbalSpeedUsesAircraftRuntimeMaximaAndForbidsUnboundedRoll() {
        Mini4CameraGimbalPolicy.requireGimbalSpeed(20.0, -10.0, 0.0, 30, 20)
        assertTrue(runCatching {
            Mini4CameraGimbalPolicy.requireGimbalSpeed(31.0, 0.0, 0.0, 30, 20)
        }.isFailure)
        assertTrue(runCatching {
            Mini4CameraGimbalPolicy.requireGimbalSpeed(0.0, 0.0, 1.0, 30, 20)
        }.isFailure)
    }

    @Test
    fun serializedQueueDoesNotStartSecondMutationUntilFirstCompletes() {
        val starts = Collections.synchronizedList(mutableListOf<String>())
        val firstStarted = CountDownLatch(1)
        val secondStarted = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)
        val queue = SerializedCameraGimbalQueue(executor)
        try {
            queue.submit { done ->
                starts += "first"
                firstStarted.countDown()
                executor.execute {
                    releaseFirst.await()
                    done()
                }
            }
            queue.submit { done ->
                starts += "second"
                secondStarted.countDown()
                done()
            }

            assertTrue(firstStarted.await(1, TimeUnit.SECONDS))
            assertFalse(secondStarted.await(100, TimeUnit.MILLISECONDS))
            assertEquals(listOf("first"), starts.toList())
            releaseFirst.countDown()
            assertTrue(secondStarted.await(1, TimeUnit.SECONDS))
            assertEquals(listOf("first", "second"), starts.toList())
        } finally {
            queue.close()
            executor.shutdownNow()
        }
    }

    @Test
    fun capabilityReportNeverPromisesPrivateVisionStreams() {
        val report = Mini4CameraGimbalPolicy.capabilityReport()
        assertTrue("KeyCameraZoomRatiosRange" in report.publicCameraKeys)
        assertTrue("KeyGimbalVerticalShotEnabled" in report.publicGimbalKeys)
        assertTrue("obstacle_camera_video" in report.deliberatelyUnavailable)
        assertTrue(report.operations.any {
            it.operation == "storage" && it.validation.contains("confirmation")
        })
    }

    @Test
    fun advertisedHttpActionsAreExactAndEveryExampleReachesDispatcher() {
        val expected = setOf(
            "set_camera_mode",
            "take_photo",
            "set_interval",
            "start_interval",
            "stop_interval",
            "stop_photo",
            "start_record",
            "stop_record",
            "set_zoom",
            "set_focus_mode",
            "set_focus_target",
            "set_exposure_mode",
            "set_exposure_compensation",
            "set_iso",
            "set_shutter",
            "set_white_balance",
            "set_photo_format",
            "set_photo_ratio",
            "set_video_format",
            "set_video_spec",
            "format_storage",
            "set_gimbal_mode",
            "gimbal_angle",
            "gimbal_speed",
            "gimbal_reset",
            "vertical_shot"
        )
        assertEquals(expected, CameraGimbalHttpContract.actions.map { it.action }.toSet())

        assertEquals(expected, CameraGimbalHttpAction.entries.map { it.wireName }.toSet())
        CameraGimbalHttpContract.actions.forEach { schema ->
            CameraGimbalHttpContract.validate(
                schema.action,
                schema.exampleQuery + ("action" to schema.action)
            )
        }
    }

    @Test
    fun everyAdvertisedRequiredParameterIsActuallyRequired() {
        CameraGimbalHttpContract.actions.forEach { schema ->
            schema.requiredParameters.forEach { required ->
                val incomplete = schema.exampleQuery - required + ("action" to schema.action)
                val error = runCatching {
                    CameraGimbalHttpContract.validate(schema.action, incomplete)
                }.exceptionOrNull()
                assertTrue(
                    "${schema.action} unexpectedly accepted missing $required",
                    error is IllegalArgumentException
                )
            }
        }
    }

    @Test
    fun commandContractRejectsUnadvertisedActionsAndParameters() {
        assertTrue(runCatching {
            CameraGimbalHttpContract.validate("secret_camera", emptyMap())
        }.isFailure)
        assertTrue(runCatching {
            CameraGimbalHttpContract.validate(
                "take_photo",
                mapOf("action" to "take_photo", "private_stream" to "true")
            )
        }.isFailure)
    }

    @Test
    fun gimbalSpeedLeaseIsShortBoundedAndZeroNeedsNoLease() {
        assertEquals(
            GimbalSpeedLeasePolicy.DEFAULT_LEASE_MS,
            GimbalSpeedLeasePolicy.normalizedLeaseMs(1.0, 0.0, null)
        )
        assertEquals(0L, GimbalSpeedLeasePolicy.normalizedLeaseMs(0.0, 0.0, null))
        assertTrue(runCatching {
            GimbalSpeedLeasePolicy.normalizedLeaseMs(
                1.0,
                0.0,
                GimbalSpeedLeasePolicy.MAX_LEASE_MS + 1
            )
        }.isFailure)
    }

    @Test
    fun linkGateRejectsDisconnectedAndOldGenerationCommandsAcrossReconnect() {
        val gate = CameraGimbalLinkGate()
        val disconnectedRecordTicket = gate.ticket()
        assertFalse(gate.isValid(disconnectedRecordTicket))

        val connectedGeneration = gate.connect()
        assertTrue(gate.isValid(connectedGeneration))
        assertEquals("repeat USB start must be idempotent", connectedGeneration, gate.connect())

        val staleFormatTicket = gate.ticket()
        val staleRecordTicket = gate.ticket()
        gate.disconnect()
        assertFalse(gate.isValid(staleFormatTicket))
        assertFalse(gate.isValid(staleRecordTicket))

        val submittedWhileDisconnected = gate.ticket()
        assertFalse(gate.isValid(submittedWhileDisconnected))
        gate.connect()
        assertFalse("disconnected submission must not revive", gate.isValid(submittedWhileDisconnected))
    }

    @Test
    fun cameraGimbalConnectionRequiresAircraftControllerAndAirlink() {
        assertFalse(CameraGimbalConnectionPolicy.isUsableAircraftLink(false, true, true))
        assertFalse(CameraGimbalConnectionPolicy.isUsableAircraftLink(true, false, true))
        assertFalse(CameraGimbalConnectionPolicy.isUsableAircraftLink(true, true, false))
        assertTrue(CameraGimbalConnectionPolicy.isUsableAircraftLink(true, true, true))
    }
}
