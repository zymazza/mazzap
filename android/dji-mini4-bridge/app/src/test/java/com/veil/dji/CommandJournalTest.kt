package com.veil.dji

import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CommandJournalTest {
    @Test
    fun httpPolicyOnlyLabelsActuallyPendingCommandsAccepted() {
        val pending = CommandHttpResponsePolicy.select(CommandState.REQUESTED)
        assertEquals(202, pending.statusCode)
        assertEquals(true, pending.acceptedForProcessing)

        val succeeded = CommandHttpResponsePolicy.select(CommandState.SUCCEEDED)
        assertEquals(200, succeeded.statusCode)
        assertNull(succeeded.acceptedForProcessing)

        val locallyRejected = CommandHttpResponsePolicy.select(CommandState.FAILED)
        assertEquals(409, locallyRejected.statusCode)
        assertNull(locallyRejected.acceptedForProcessing)
    }

    @Test
    fun laterEventsCannotOverwriteTakeoffCallbackResult() {
        val clock = AtomicLong(1_000L)
        val journal = journal(clock)
        val takeoff = journal.request("takeoff", readyPreconditions())
        clock.incrementAndGet()
        journal.succeed(
            takeoff.id,
            mapOf(
                "callback" to "dji_action_succeeded",
                "physical_completion" to "not_implied"
            )
        )

        val landing = journal.request("land", readyPreconditions())
        val landingError = CommandError(
            source = "dji",
            type = "COMMON",
            code = "-7",
            innerCode = "0x1234",
            description = "landing rejected",
            hint = "aircraft is not flying",
            raw = "DJIError{errorCode=-7}"
        )
        journal.fail(landing.id, landingError)

        val retainedTakeoff = requireNotNull(journal.get(takeoff.id))
        assertEquals(CommandState.SUCCEEDED, retainedTakeoff.state)
        assertEquals("dji_action_succeeded", retainedTakeoff.result["callback"])
        assertEquals("not_implied", retainedTakeoff.result["physical_completion"])
        assertNull(retainedTakeoff.error)
        assertEquals(takeoff.id, journal.latest("takeoff")?.id)

        val retainedLanding = requireNotNull(journal.get(landing.id))
        assertEquals(CommandState.FAILED, retainedLanding.state)
        assertEquals(landingError, retainedLanding.error)
        assertEquals("-7", retainedLanding.error?.code)
        assertEquals("0x1234", retainedLanding.error?.innerCode)
        assertEquals("landing rejected", retainedLanding.error?.description)
        assertEquals("aircraft is not flying", retainedLanding.error?.hint)
        assertEquals("DJIError{errorCode=-7}", retainedLanding.error?.raw)
    }

    @Test
    fun terminalResultCannotBeChangedByASecondCallback() {
        val clock = AtomicLong(2_000L)
        val journal = journal(clock)
        val command = journal.request("takeoff", readyPreconditions())
        val succeeded = requireNotNull(journal.succeed(command.id, mapOf("callback" to "ok")))

        clock.incrementAndGet()
        val duplicate = requireNotNull(journal.fail(
            command.id,
            CommandError(source = "dji", code = "late_failure")
        ))

        assertEquals(succeeded, duplicate)
        assertEquals(CommandState.SUCCEEDED, duplicate.state)
        assertEquals("ok", duplicate.result["callback"])
        assertNull(duplicate.error)
        assertEquals(2_000L, duplicate.completedAtMonotonicMs)
    }

    @Test
    fun boundedHistoryEvictsOnlyTheOldestRecords() {
        val clock = AtomicLong(3_000L)
        val journal = journal(clock, capacity = 2)
        val first = journal.request("first", readyPreconditions())
        clock.incrementAndGet()
        val second = journal.request("second", readyPreconditions())
        clock.incrementAndGet()
        val third = journal.request("third", readyPreconditions())

        assertEquals(2, journal.size())
        assertNull(journal.get(first.id))
        assertEquals(listOf(third.id, second.id), journal.history().map { it.id })
    }

    @Test
    fun boundedHistoryPreservesPendingPhysicalActionBeforeTerminalRecords() {
        val clock = AtomicLong(3_500L)
        val journal = journal(clock, capacity = 2)
        val pendingTakeoff = journal.request("takeoff", readyPreconditions())
        clock.incrementAndGet()
        val completed = journal.request("setpoint", readyPreconditions())
        journal.succeed(completed.id)
        clock.incrementAndGet()
        val newest = journal.request("status_action", readyPreconditions())

        assertEquals(2, journal.size())
        assertEquals(CommandState.REQUESTED, journal.get(pendingTakeoff.id)?.state)
        assertNull(journal.get(completed.id))
        assertEquals(CommandState.REQUESTED, journal.get(newest.id)?.state)
    }

    @Test
    fun boundedHistoryPreservesPendingPerceptionMutationUntilReadback() {
        val clock = AtomicLong(3_625L)
        val journal = journal(clock, capacity = 2)
        val pendingConfig = journal.request("perception_config_set", readyPreconditions())
        clock.incrementAndGet()
        val completed = journal.request("setpoint", readyPreconditions())
        journal.succeed(completed.id)
        clock.incrementAndGet()
        journal.request("status_action", readyPreconditions())

        assertEquals(CommandState.REQUESTED, journal.get(pendingConfig.id)?.state)
        assertNull(journal.get(completed.id))
    }

    @Test
    fun boundedHistoryPreservesPendingPhysicalActionBeforeOtherPendingRecords() {
        val clock = AtomicLong(3_750L)
        val journal = journal(clock, capacity = 2)
        val pendingTakeoff = journal.request("takeoff", readyPreconditions())
        clock.incrementAndGet()
        val olderStatus = journal.request("status_action", readyPreconditions())
        clock.incrementAndGet()
        val newerStatus = journal.request("status_action", readyPreconditions())

        assertEquals(CommandState.REQUESTED, journal.get(pendingTakeoff.id)?.state)
        assertNull(journal.get(olderStatus.id))
        assertEquals(CommandState.REQUESTED, journal.get(newerStatus.id)?.state)
    }

    @Test
    fun concurrentRequestsRemainUniqueAndBounded() {
        val clock = AtomicLong(4_000L)
        val journal = journal(clock, capacity = 32)
        val ids = ConcurrentHashMap.newKeySet<String>()
        val pool = Executors.newFixedThreadPool(8)
        val start = CountDownLatch(1)
        repeat(200) {
            pool.execute {
                start.await()
                ids += journal.request("setpoint", readyPreconditions()).id
            }
        }
        start.countDown()
        pool.shutdown()

        assertTrue(pool.awaitTermination(5, TimeUnit.SECONDS))
        assertEquals(200, ids.size)
        assertEquals(32, journal.size())
        assertEquals(32, journal.history(32).map { it.id }.toSet().size)
    }

    @Test
    fun readinessIsConservativeAndExplainsEveryBlocker() {
        val ready = FlightTestReadinessEvaluator.evaluate(readyPreconditions())
        assertTrue(ready.readyForTakeoffCommand)
        assertTrue(ready.blockers.isEmpty())

        val unsafe = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                sdkRegistered = false,
                motorsOn = true,
                flightControllerFailsafe = true,
                batteryConnected = false,
                batteryPercent = null,
                virtualStickEnabled = true,
                flightControlAuthority = "MSDK",
                controlFailsafeState = "disable_failed_neutral",
                gpsSignalLevel = "LEVEL_1"
            )
        )
        assertFalse(unsafe.readyForTakeoffCommand)
        assertTrue("sdk_not_registered" in unsafe.blockers)
        assertTrue("motors_already_on" in unsafe.blockers)
        assertTrue("flight_controller_failsafe_active" in unsafe.blockers)
        assertTrue("battery_telemetry_unavailable" in unsafe.blockers)
        assertTrue("battery_percent_unknown" in unsafe.blockers)
        assertTrue("msdk_control_authority_not_released" in unsafe.blockers)
        assertTrue("bridge_control_not_disarmed_disable_failed_neutral" in unsafe.blockers)
        assertTrue("gps_signal_not_confirmed_good" in unsafe.advisories)
    }

    @Test
    fun takeoffBatteryFloorIsBlockingWhileGpsAndHomeRemainAdvisory() {
        val belowFloor = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                batteryPercent = FlightTestReadinessEvaluator.MINIMUM_TAKEOFF_BATTERY_PERCENT - 1,
                batteryPercentNeededToGoHome = null,
                batteryPercentNeededToLand = null,
                gpsSignalLevel = "LEVEL_1",
                homeLocationSet = false
            )
        )
        assertFalse(belowFloor.readyForTakeoffCommand)
        assertTrue("battery_below_minimum_takeoff_percent" in belowFloor.blockers)
        assertTrue("gps_signal_not_confirmed_good" in belowFloor.advisories)
        assertTrue("home_location_not_set" in belowFloor.advisories)

        val atFloor = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                batteryPercent = FlightTestReadinessEvaluator.MINIMUM_TAKEOFF_BATTERY_PERCENT,
                batteryPercentNeededToGoHome = null,
                batteryPercentNeededToLand = null,
                gpsSignalLevel = "LEVEL_1",
                homeLocationSet = false
            )
        )
        assertTrue(atFloor.readyForTakeoffCommand)
        assertTrue(atFloor.blockers.isEmpty())
        assertTrue(atFloor.advisories.isNotEmpty())
    }

    @Test
    fun staleBatteryAndReportedDeviceHealthErrorsBlockTakeoff() {
        val readiness = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                batteryPercentAgeMs =
                    FlightTestReadinessEvaluator.MAXIMUM_BATTERY_PERCENT_AGE_MS + 1,
                seriousLowBatteryWarning = true,
                blockingDeviceHealthIssues = listOf("SERIOUS_WARNING_0x1610004A")
            )
        )

        assertFalse(readiness.readyForTakeoffCommand)
        assertTrue("battery_percent_stale" in readiness.blockers)
        assertTrue("aircraft_serious_low_battery_warning_active" in readiness.blockers)
        assertTrue("device_health_SERIOUS_WARNING_0x1610004A" in readiness.blockers)
    }

    @Test
    fun takeoffRequiresFreshObservedGroundFailsafeRcHealthAndRidState() {
        val unknown = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                aircraftConnectionAgeMs = 100L,
                isFlyingAgeMs = null,
                motorsOnAgeMs = null,
                flightControllerFailsafeAgeMs = null,
                failsafeAction = "unknown",
                failsafeActionAgeMs = null,
                remoteControllerFlightMode = "UNKNOWN",
                remoteControllerFlightModeAgeMs = null,
                flightControlAuthority = "UNKNOWN",
                flightControlAuthorityAgeMs = null,
                deviceHealthObserved = false,
                remoteIdObserved = false
            )
        )

        assertFalse(unknown.readyForTakeoffCommand)
        assertTrue("aircraft_connection_telemetry_settling" in unknown.blockers)
        assertTrue("is_flying_state_unknown" in unknown.blockers)
        assertTrue("motors_on_state_unknown" in unknown.blockers)
        assertTrue("flight_controller_failsafe_state_unknown" in unknown.blockers)
        assertTrue("failsafe_action_state_unknown" in unknown.blockers)
        assertTrue("failsafe_action_unknown_unknown" in unknown.blockers)
        assertTrue("remote_controller_flight_mode_state_unknown" in unknown.blockers)
        assertTrue("remote_controller_not_normal_mode_UNKNOWN" in unknown.blockers)
        assertTrue("flight_control_authority_state_unknown" in unknown.blockers)
        assertTrue("flight_control_authority_not_rc_UNKNOWN" in unknown.blockers)
        assertTrue("device_health_unknown_for_connection" in unknown.blockers)
        assertTrue("remote_id_unknown_for_connection" in unknown.blockers)

        val preConnect = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                isFlyingAgeMs =
                    FlightTestReadinessEvaluator.MINIMUM_CONNECTION_SETTLE_AGE_MS + 1,
                motorsOnAgeMs =
                    FlightTestReadinessEvaluator.MINIMUM_CONNECTION_SETTLE_AGE_MS + 1,
                flightControllerFailsafeAgeMs =
                    FlightTestReadinessEvaluator.MINIMUM_CONNECTION_SETTLE_AGE_MS + 1,
                remoteControllerFlightMode = "S"
            )
        )
        assertFalse(preConnect.readyForTakeoffCommand)
        assertTrue("is_flying_state_before_current_connection" in preConnect.blockers)
        assertTrue("motors_on_state_before_current_connection" in preConnect.blockers)
        assertTrue(
            "flight_controller_failsafe_state_before_current_connection" in
                preConnect.blockers
        )
        assertTrue("remote_controller_not_normal_mode_S" in preConnect.blockers)
    }

    @Test
    fun unknownAircraftBatteryWarningFlagsBlockTakeoff() {
        val readiness = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                lowBatteryWarning = null,
                seriousLowBatteryWarning = null
            )
        )

        assertFalse(readiness.readyForTakeoffCommand)
        assertTrue("aircraft_low_battery_warning_unknown" in readiness.blockers)
        assertTrue("aircraft_serious_low_battery_warning_unknown" in readiness.blockers)
    }

    @Test
    fun indoorTakeoffGateRejectsGoHomeSignalLossAction() {
        val readiness = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(failsafeAction = "GOHOME")
        )

        assertFalse(readiness.readyForTakeoffCommand)
        assertTrue(
            "failsafe_action_go_home_unsafe_for_indoor_test" in readiness.blockers
        )
    }

    @Test
    fun remoteIdTakeoffErrorsAlwaysBlockButNormalStatusNeverDoes() {
        assertEquals(
            "NOTICE_0X161000B4",
            DeviceHealthTakeoffPolicy.blockerLabel("0x161000B4", "NOTICE")
        )
        assertEquals(
            "NOTICE_0X161000B5",
            DeviceHealthTakeoffPolicy.blockerLabel("0x161000B5", "NOTICE")
        )
        assertNull(DeviceHealthTakeoffPolicy.blockerLabel("0x1B080003", "UNKNOWN"))
        assertEquals(
            "WARNING_0XDEADBEEF",
            DeviceHealthTakeoffPolicy.blockerLabel("0xDEADBEEF", "WARNING")
        )
    }

    @Test
    fun remoteIdErrorWorkingStateBlocksTakeoff() {
        val readiness = FlightTestReadinessEvaluator.evaluate(
            readyPreconditions().copy(
                remoteIdWorkingState = "OPERATOR_LOCATION_LOST_ERROR"
            )
        )

        assertFalse(readiness.readyForTakeoffCommand)
        assertTrue(
            "remote_id_state_OPERATOR_LOCATION_LOST_ERROR" in readiness.blockers
        )
    }

    @Test
    fun landingConfirmationDispatchRequiresAnExplicitDjiRequest() {
        assertTrue(LandingConfirmationPolicy.canDispatch(true))
        assertFalse(LandingConfirmationPolicy.canDispatch(false))
        assertFalse(LandingConfirmationPolicy.canDispatch(null))
    }

    private fun journal(clock: AtomicLong, capacity: Int = 64): CommandJournal = CommandJournal(
        capacity = capacity,
        monotonicClock = clock::get,
        wallClock = { 123_456L },
        idFactory = { now, sequence -> "cmd-$now-$sequence" }
    )

    private fun readyPreconditions(): CommandPreconditions = CommandPreconditions(
        capturedAtMonotonicMs = 999L,
        sdkRegistered = true,
        productConnected = true,
        remoteControllerConnected = true,
        airLinkConnected = true,
        aircraftConnected = true,
        isFlying = false,
        motorsOn = false,
        flightMode = "GPS_NORMAL",
        altitudeMeters = 0.0,
        gpsSignalLevel = "LEVEL_4",
        gpsSatelliteCount = 15,
        homeLocationSet = true,
        flightControllerFailsafe = false,
        batteryConnected = true,
        batteryPercent = 80,
        batteryPercentNeededToGoHome = 30,
        batteryPercentNeededToLand = 15,
        virtualStickEnabled = false,
        virtualStickAdvancedMode = false,
        flightControlAuthority = "RC",
        virtualStickControlMode = "disabled",
        controlFailsafeState = "disarmed",
        batteryPercentAgeMs = 0L,
        lowBatteryWarning = false,
        seriousLowBatteryWarning = false,
        deviceHealthObserved = true,
        aircraftConnectionAgeMs =
            FlightTestReadinessEvaluator.MINIMUM_CONNECTION_SETTLE_AGE_MS,
        isFlyingAgeMs = 0L,
        motorsOnAgeMs = 0L,
        flightControllerFailsafeAgeMs = 0L,
        remoteControllerFlightMode = "P",
        remoteControllerFlightModeAgeMs = 0L,
        remoteIdObserved = true,
        remoteIdWorkingState = "WORKING",
        flightControlAuthorityAgeMs = 0L,
        failsafeAction = "HOVER",
        failsafeActionAgeMs = 0L
    )
}
