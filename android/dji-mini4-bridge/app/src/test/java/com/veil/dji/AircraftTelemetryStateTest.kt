package com.veil.dji

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class AircraftTelemetryStateTest {
    @Before
    fun resetConnectionScopedState() {
        AircraftTelemetryState.updateAircraftConnection(false, nowMs = 1L)
        AircraftTelemetryState.updateRemoteControllerConnection(false, nowMs = 1L)
        AircraftTelemetryState.updateAndroidLocationReadiness(false, false, false, nowMs = 1L)
    }

    @Test
    fun preservesStructuredNedNavigationSamples() {
        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 2L)
        AircraftTelemetryState.updateLocation(40.0, -74.0, 12.5, nowMs = 3L)
        AircraftTelemetryState.updateVelocity(1.0, 2.0, -0.5, nowMs = 4L)
        AircraftTelemetryState.updateAttitude(3.0, 4.0, 90.0, nowMs = 5L)

        val snapshot = AircraftTelemetryState.snapshot()
        assertTrue(snapshot.aircraftConnected)
        assertEquals(40.0, snapshot.location?.latitudeDegrees ?: Double.NaN, 0.0)
        assertEquals(12.5, snapshot.location?.altitudeMeters ?: Double.NaN, 0.0)
        assertEquals(1.0, snapshot.velocity?.northMetersPerSecond ?: Double.NaN, 0.0)
        assertEquals(2.0, snapshot.velocity?.eastMetersPerSecond ?: Double.NaN, 0.0)
        assertEquals(-0.5, snapshot.velocity?.downMetersPerSecond ?: Double.NaN, 0.0)
        assertEquals(90.0, snapshot.attitude?.yawDegrees ?: Double.NaN, 0.0)
    }

    @Test
    fun invalidCoordinateErasesPreviouslyValidLocation() {
        AircraftTelemetryState.updateLocation(40.0, -74.0, 12.5, nowMs = 2L)
        AircraftTelemetryState.updateLocation(45_836_623.0, -74.0, 12.5, nowMs = 3L)

        val snapshot = AircraftTelemetryState.snapshot()
        assertNull(snapshot.location)
        assertEquals("latitude_out_of_range", snapshot.locationRejectionReason)
        assertEquals(3L, snapshot.locationSourceUpdatedAtMonotonicMs)
    }

    @Test
    fun rejectsDjiZeroCoordinateSentinel() {
        AircraftTelemetryState.updateLocation(40.0, -74.0, 12.5, nowMs = 2L)
        AircraftTelemetryState.updateLocation(0.0, 0.0, 12.5, nowMs = 3L)

        val snapshot = AircraftTelemetryState.snapshot()
        assertNull(snapshot.location)
        assertEquals("dji_zero_coordinate_sentinel", snapshot.locationRejectionReason)
    }

    @Test
    fun homeCoordinateIsWithheldUntilExplicitlySetAndValid() {
        AircraftTelemetryState.updateHomeLocationSet(false, nowMs = 2L)
        AircraftTelemetryState.updateHomeLocation(40.0, -74.0, nowMs = 3L)
        assertNull(AircraftTelemetryState.snapshot().homeRth.homeLatitudeDegrees)
        assertEquals(
            "home_location_not_set",
            AircraftTelemetryState.snapshot().homeRth.homeLocationRejectionReason
        )

        AircraftTelemetryState.updateHomeLocationSet(true, nowMs = 4L)
        assertEquals(
            40.0,
            AircraftTelemetryState.snapshot().homeRth.homeLatitudeDegrees ?: Double.NaN,
            0.0
        )
        AircraftTelemetryState.updateHomeLocation(40.0, -74.0, nowMs = 5L)
        assertEquals(40.0, AircraftTelemetryState.snapshot().homeRth.homeLatitudeDegrees ?: Double.NaN, 0.0)

        AircraftTelemetryState.updateHomeLocationSet(false, nowMs = 6L)
        assertNull(AircraftTelemetryState.snapshot().homeRth.homeLatitudeDegrees)
    }

    @Test
    fun disabledVirtualStickCannotReportAdvancedModeEnabled() {
        AircraftTelemetryState.updateVirtualStickState(
            enabled = false,
            advancedModeEnabled = true,
            owner = "RC",
            nowMs = 2L
        )

        val authority = AircraftTelemetryState.snapshot().authority
        assertFalse(authority.virtualStickEnabled)
        assertFalse(authority.virtualStickAdvancedModeEnabled)
        assertEquals("RC", authority.owner)
        assertEquals(2L, authority.stateUpdatedAtMonotonicMs)
    }

    @Test
    fun disconnectInvalidatesPreviouslyFreshFlightData() {
        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 2L)
        AircraftTelemetryState.updateLocation(40.0, -74.0, 12.5, nowMs = 3L)
        AircraftTelemetryState.updateVirtualStickState(true, true, "MSDK", nowMs = 4L)

        AircraftTelemetryState.updateAircraftConnection(false, nowMs = 5L)

        val snapshot = AircraftTelemetryState.snapshot()
        assertFalse(snapshot.aircraftConnected)
        assertNull(snapshot.location)
        assertFalse(snapshot.authority.virtualStickEnabled)
        assertEquals("UNKNOWN", snapshot.authority.owner)
        assertEquals("AIRCRAFT_DISCONNECTED", snapshot.authority.lastChangeReason)
    }

    @Test
    fun unrelatedFlightStateKeysCannotMakeGroundBooleansLookObserved() {
        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 2L)
        AircraftTelemetryState.updateFlightState(
            flightTimeDeciseconds = 0,
            nowMs = 3L
        )

        var snapshot = AircraftTelemetryState.snapshot()
        assertNull(snapshot.isFlyingUpdatedAtMonotonicMs)
        assertNull(snapshot.motorsOnUpdatedAtMonotonicMs)

        AircraftTelemetryState.updateFlightState(isFlying = false, nowMs = 4L)
        snapshot = AircraftTelemetryState.snapshot()
        assertEquals(4L, snapshot.isFlyingUpdatedAtMonotonicMs)
        assertNull(snapshot.motorsOnUpdatedAtMonotonicMs)
    }

    @Test
    fun newAircraftConnectionInvalidatesAllCachedAircraftSourcesOnlyOnce() {
        AircraftTelemetryState.updateFlightState(
            isFlying = false,
            motorsOn = false,
            nowMs = 2L
        )
        AircraftTelemetryState.updateBatteryConnection(true, nowMs = 2L)
        AircraftTelemetryState.updateBatteryChargePercent(90, nowMs = 2L)
        AircraftTelemetryState.updateLowBatteryWarning(false, nowMs = 2L)
        AircraftTelemetryState.updateFailsafeAction("GOHOME", nowMs = 2L)
        AircraftTelemetryState.updateVirtualStickState(false, false, "RC", nowMs = 2L)
        AircraftTelemetryState.updateDeviceHealth(emptyList(), nowMs = 2L)
        AircraftTelemetryState.updateRemoteIdStatus(true, "WORKING", nowMs = 2L)

        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 3L)
        var snapshot = AircraftTelemetryState.snapshot()
        assertNull(snapshot.deviceHealth.updatedAtMonotonicMs)
        assertNull(snapshot.remoteId.updatedAtMonotonicMs)
        assertNull(snapshot.isFlyingUpdatedAtMonotonicMs)
        assertNull(snapshot.motorsOnUpdatedAtMonotonicMs)
        assertNull(snapshot.battery.chargePercentUpdatedAtMonotonicMs)
        assertNull(snapshot.safety.lowBatteryWarningUpdatedAtMonotonicMs)
        assertNull(snapshot.homeRth.failsafeActionUpdatedAtMonotonicMs)
        assertNull(snapshot.authority.stateUpdatedAtMonotonicMs)
        assertFalse(snapshot.battery.connected)

        AircraftTelemetryState.updateDeviceHealth(emptyList(), nowMs = 4L)
        AircraftTelemetryState.updateRemoteIdStatus(true, "WORKING", nowMs = 4L)
        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 5L)
        snapshot = AircraftTelemetryState.snapshot()
        assertEquals(3L, snapshot.connectionUpdatedAtMonotonicMs)
        assertEquals(4L, snapshot.deviceHealth.updatedAtMonotonicMs)
        assertEquals(4L, snapshot.remoteId.updatedAtMonotonicMs)
    }

    @Test
    fun gpsPreflightIsReadyOnlyAfterAllObservedSourcesAreClear() {
        populateReadyGpsState(nowMs = 1_000L)

        val report = AircraftTelemetryState.preflightReadiness(
            PreflightProfile.GPS_FLIGHT,
            nowMs = 1_100L
        )

        assertTrue(report.blockers.joinToString { it.code }, report.ready)
        assertTrue(report.blockers.isEmpty())
        assertFalse(report.profile.requiresRemoteIdWorking)
    }

    @Test
    fun gpsPreflightBlocksLowBatteryAndStaleNavigation() {
        populateReadyGpsState(nowMs = 1_000L)
        AircraftTelemetryState.updateLowBatteryWarning(true, nowMs = 1_050L)

        val report = AircraftTelemetryState.preflightReadiness(
            PreflightProfile.GPS_FLIGHT,
            nowMs = 4_001L
        )
        val codes = report.blockers.map { it.code }

        assertFalse(report.ready)
        assertTrue("low_battery_active" in codes)
        assertTrue("location_stale" in codes)
        assertTrue("velocity_stale" in codes)
    }

    @Test
    fun remoteIdPolicyIsCallerSelectedAndRequiresObservedOperatorLocation() {
        populateReadyGpsState(nowMs = 1_000L)
        var report = AircraftTelemetryState.preflightReadiness(
            PreflightProfile.GPS_FLIGHT_RID_REQUIRED,
            nowMs = 1_100L
        )
        assertTrue(report.blockers.any { it.code == "fine_location_permission_missing" })
        assertTrue(report.blockers.any { it.code == "remote_id_unknown" })

        AircraftTelemetryState.updateAndroidLocationReadiness(
            coarsePermissionGranted = true,
            finePermissionGranted = true,
            locationProviderEnabled = true,
            lastKnownLocationAvailable = true,
            lastKnownLocationProvider = "gps",
            lastKnownLocationAccuracyMeters = 5.0f,
            lastKnownLocationElapsedRealtimeMs = 1_040L,
            nowMs = 1_050L
        )
        AircraftTelemetryState.updateRemoteIdStatus(true, "WORKING", nowMs = 1_050L)
        report = AircraftTelemetryState.preflightReadiness(
            PreflightProfile.GPS_FLIGHT_RID_REQUIRED,
            nowMs = 1_100L
        )
        assertTrue(report.blockers.joinToString { it.code }, report.ready)
    }

    @Test
    fun ultrasonicHeightUsesDocumentedDecimeters() {
        AircraftTelemetryState.updateUltrasonicHeight(12, nowMs = 2L)
        val safety = AircraftTelemetryState.snapshot().safety
        assertEquals(12, safety.ultrasonicHeightDecimeters)
        assertEquals(1.2, safety.ultrasonicHeightMeters ?: Double.NaN, 0.0)
    }

    @Test
    fun publishesPerceptionStatusAndObstacleRangesWithoutClaimingImagery() {
        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 2L)
        AircraftTelemetryState.updatePerceptionInformation(
            forwardWorking = true,
            backwardWorking = true,
            leftWorking = false,
            rightWorking = false,
            upwardWorking = true,
            downwardWorking = true,
            overallObstacleAvoidanceEnabled = true,
            horizontalObstacleAvoidanceEnabled = true,
            upwardObstacleAvoidanceEnabled = true,
            downwardObstacleAvoidanceEnabled = true,
            obstacleAvoidanceType = "BRAKE",
            horizontalWarningDistanceMeters = 3.0,
            upwardWarningDistanceMeters = 2.0,
            downwardWarningDistanceMeters = 1.5,
            horizontalBrakingDistanceMeters = 2.0,
            upwardBrakingDistanceMeters = 1.5,
            downwardBrakingDistanceMeters = 1.0,
            visionPositioningEnabled = true,
            precisionLandingEnabled = true,
            nowMs = 3L
        )
        val horizontal = mutableListOf(1_000, 2_000, 3_000, 4_000)
        AircraftTelemetryState.updateObstacleDistances(
            horizontalDistancesMillimeters = horizontal,
            horizontalAngleIntervalDegrees = 90,
            upwardDistanceMillimeters = 5_000,
            downwardDistanceMillimeters = 600,
            nowMs = 4L
        )
        horizontal.clear()

        val snapshot = AircraftTelemetryState.snapshot()
        assertEquals(listOf(1_000, 2_000, 3_000, 4_000),
            snapshot.perception.obstacleDistances.horizontalDistancesMillimeters)
        assertEquals(true, snapshot.perception.information.forwardWorking)
        assertEquals("BRAKE", snapshot.perception.information.obstacleAvoidanceType)

        val perception = AircraftTelemetryState.toJson(nowMs = 5L).getJSONObject("perception")
        assertFalse(perception.getBoolean("raw_obstacle_camera_imagery_exposed"))
        assertTrue(perception.getJSONObject("information").getBoolean("observed"))
        val enabled = perception.getJSONObject("information").getJSONObject("enabled")
        assertFalse(enabled.getBoolean("deprecated_overall_is_authoritative"))
        assertTrue(enabled.getBoolean("effective_from_avoidance_type"))
        assertEquals(
            true,
            perception.getJSONObject("information")
                .getJSONObject("working")
                .getBoolean("forward")
        )
        val ranges = perception.getJSONObject("obstacle_distances")
        assertEquals("millimeter", ranges.getString("distance_unit"))
        assertEquals(4, ranges.getInt("horizontal_sample_count"))
        assertEquals(3_000, ranges.getJSONArray("horizontal_distance_mm").getInt(2))
        assertEquals(5_000, ranges.getInt("upward_distance_mm"))
        assertEquals(600, ranges.getInt("downward_distance_mm"))
        assertEquals(4L, ranges.getLong("updated_monotonic_ms"))
    }

    @Test
    fun publishesCompassWindGimbalAndImuCalibrationWithDocumentedUnits() {
        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 2L)
        AircraftTelemetryState.updateCompassCount(1, nowMs = 3L)
        AircraftTelemetryState.updateCompassHeading(-45.5, nowMs = 4L)
        AircraftTelemetryState.updateCompassError(false, nowMs = 5L)
        AircraftTelemetryState.updateWindWarning("LEVEL_1", nowMs = 6L)
        AircraftTelemetryState.updateWindSpeed(37, nowMs = 7L)
        AircraftTelemetryState.updateWindDirection("NORTH_EAST", nowMs = 8L)
        AircraftTelemetryState.updateGimbalAttitude(-20.0, 1.0, 2.0, nowMs = 9L)
        AircraftTelemetryState.updateGimbalYawRelativeToAircraftHeading(3.0, nowMs = 10L)
        val pendingOrientations = mutableListOf("LEFT", "RIGHT")
        AircraftTelemetryState.updateImuCount(1, nowMs = 10L)
        AircraftTelemetryState.updateImuCalibration(
            orientationCalibrationState = "CALIBRATING",
            calibrationState = "CALIBRATING",
            calibrationProgressPercent = 40,
            orientationsToCalibrate = pendingOrientations,
            orientationsCalibrated = listOf("BOTTOM"),
            nowMs = 11L
        )
        pendingOrientations.clear()

        val json = AircraftTelemetryState.toJson(nowMs = 12L)
        assertEquals(-45.5, json.getJSONObject("compass").getDouble("heading_deg"), 0.0)
        assertFalse(json.getJSONObject("compass").getBoolean("has_error"))
        val wind = json.getJSONObject("wind")
        assertEquals(37, wind.getInt("speed_raw_dm_s"))
        assertEquals(3.7, wind.getDouble("speed_m_s"), 0.0)
        assertEquals("NORTH_EAST", wind.getString("direction_world"))
        val gimbal = json.getJSONObject("gimbal")
        assertEquals(-20.0, gimbal.getDouble("pitch_deg"), 0.0)
        assertEquals("world_ned", gimbal.getString("yaw_coordinate_frame"))
        assertFalse(gimbal.getBoolean("yaw_is_aircraft_relative"))
        assertEquals(3.0, gimbal.getDouble("yaw_relative_to_aircraft_heading_deg"), 0.0)
        val imu = json.getJSONObject("imu_calibration")
        assertEquals(1, imu.getInt("imu_count"))
        assertEquals(40, imu.getInt("calibration_progress_percent"))
        assertEquals(2, imu.getJSONArray("orientations_to_calibrate").length())
        assertFalse(imu.getBoolean("raw_accelerometer_exposed"))
        assertFalse(imu.getBoolean("raw_gyroscope_exposed"))
    }

    @Test
    fun remoteControllerSamplesSurviveAircraftReconnectButClearOnRcDisconnect() {
        AircraftTelemetryState.updateRemoteControllerConnection(true, nowMs = 2L)
        AircraftTelemetryState.updateRemoteControllerStick(
            RemoteControllerAxis.LEFT_HORIZONTAL,
            -100,
            nowMs = 3L
        )
        AircraftTelemetryState.updateRemoteControllerStick(
            RemoteControllerAxis.LEFT_DIAL,
            200,
            nowMs = 4L
        )
        AircraftTelemetryState.updateRemoteControllerButton(
            RemoteControllerButton.SHUTTER,
            true,
            nowMs = 5L
        )
        AircraftTelemetryState.updateRemoteControllerBattery(true, 2_400, 75, nowMs = 6L)

        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 7L)
        AircraftTelemetryState.updateAircraftConnection(false, nowMs = 8L)

        var rc = AircraftTelemetryState.snapshot().remoteController
        assertTrue(rc.connected)
        assertEquals(-100, rc.leftStickHorizontal)
        assertEquals(200, rc.leftDial)
        assertEquals(true, rc.shutterButtonDown)
        assertEquals(75, rc.batteryPercent)
        val rcJson = AircraftTelemetryState.toJson(nowMs = 9L).getJSONObject("remote_controller")
        assertEquals(-100, rcJson.getJSONObject("sticks").getInt("left_horizontal"))
        assertEquals("[-660,660]", rcJson.getJSONObject("sticks").getString("range"))
        assertEquals("undocumented", rcJson.getJSONObject("battery").getString("power_raw_unit"))

        AircraftTelemetryState.updateRemoteControllerConnection(false, nowMs = 9L)
        rc = AircraftTelemetryState.snapshot().remoteController
        assertFalse(rc.connected)
        assertNull(rc.leftStickHorizontal)
        assertNull(rc.shutterButtonDown)
        assertNull(rc.batteryPercent)
    }

    @Test
    fun publishesSupportedBatteryMetadataWithIndividualSourceTimestamps() {
        AircraftTelemetryState.updateAircraftConnection(true, nowMs = 2L)
        AircraftTelemetryState.updateBatteryConnection(true, nowMs = 3L)
        AircraftTelemetryState.updateBatteryNumberOfDischarges(12, nowMs = 4L)
        AircraftTelemetryState.updateBatteryNumberOfCells(2, nowMs = 5L)
        AircraftTelemetryState.updateBatteryManufacturedDate(2025, 6, 7, nowMs = 6L)
        AircraftTelemetryState.updateBatterySerialNumber("battery-serial", nowMs = 7L)
        AircraftTelemetryState.updateBatteryFirmwareVersion("10.75.00.17", nowMs = 8L)

        val battery = AircraftTelemetryState.toJson(nowMs = 9L).getJSONObject("battery")
        assertEquals(12, battery.getInt("number_of_discharges"))
        assertEquals(2, battery.getInt("number_of_cells"))
        assertEquals(2025, battery.getJSONObject("manufactured_date").getInt("year"))
        assertEquals("battery-serial", battery.getString("serial_number"))
        assertEquals("10.75.00.17", battery.getString("firmware_version"))
        assertEquals(4L, battery.getLong("number_of_discharges_updated_monotonic_ms"))
        assertEquals(8L, battery.getLong("firmware_version_updated_monotonic_ms"))
    }

    @Test
    fun knownRemoteIdHealthCodeBlocksEvenIfSeverityIsNormal() {
        populateReadyGpsState(nowMs = 1_000L)
        AircraftTelemetryState.updateDeviceHealth(
            listOf(
                DeviceHealthIssueTelemetry(
                    informationCode = "0x161000B4",
                    warningLevel = "NORMAL",
                    componentId = 0,
                    sensorIndex = 0,
                    title = "Remote ID",
                    description = "User location unavailable"
                )
            ),
            nowMs = 1_050L
        )

        val report = AircraftTelemetryState.preflightReadiness(
            PreflightProfile.GPS_FLIGHT,
            nowMs = 1_100L
        )
        assertTrue(report.blockers.any { it.code == "device_health_0x161000B4" })
    }

    @Test
    fun remoteIdProfileRejectsStaleOrInaccurateOperatorLocation() {
        populateReadyGpsState(nowMs = 20_000L)
        AircraftTelemetryState.updateAndroidLocationReadiness(
            coarsePermissionGranted = true,
            finePermissionGranted = true,
            locationProviderEnabled = true,
            lastKnownLocationAvailable = true,
            lastKnownLocationProvider = "network",
            lastKnownLocationAccuracyMeters = 500.0f,
            lastKnownLocationElapsedRealtimeMs = 1_000L,
            lastKnownLocationMock = true,
            nowMs = 20_000L
        )
        AircraftTelemetryState.updateRemoteIdStatus(true, "WORKING", nowMs = 20_000L)

        val report = AircraftTelemetryState.preflightReadiness(
            PreflightProfile.GPS_FLIGHT_RID_REQUIRED,
            nowMs = 20_100L
        )
        val codes = report.blockers.map { it.code }
        assertTrue("operator_location_stale" in codes)
        assertTrue("operator_location_accuracy_unacceptable" in codes)
        assertTrue("operator_location_mock" in codes)
    }

    private fun populateReadyGpsState(nowMs: Long) {
        AircraftTelemetryState.updateAircraftConnection(true, nowMs)
        AircraftTelemetryState.updateFlightState(
            isFlying = false,
            motorsOn = false,
            flightMode = "GPS_NORMAL",
            flightTimeDeciseconds = 0,
            nowMs = nowMs
        )
        AircraftTelemetryState.updateLocation(40.0, -74.0, 10.0, nowMs)
        AircraftTelemetryState.updateVelocity(0.0, 0.0, 0.0, nowMs)
        AircraftTelemetryState.updateAttitude(0.0, 0.0, 0.0, nowMs)
        AircraftTelemetryState.updateGpsSatelliteCount(12, nowMs)
        AircraftTelemetryState.updateGpsSignalLevel("LEVEL_4", nowMs)
        AircraftTelemetryState.updateHomeLocationSet(true, nowMs)
        AircraftTelemetryState.updateHomeLocation(40.0, -74.0, nowMs)
        AircraftTelemetryState.updateGoHomeStatus("IDLE", nowMs)
        AircraftTelemetryState.updateGoHomeHeight(40, nowMs)
        AircraftTelemetryState.updateGoHomeHeightRange(20, 500, 100, nowMs)
        AircraftTelemetryState.updateFlightControllerFailsafe(false, nowMs)
        AircraftTelemetryState.updateFailsafeAction("HOVER", nowMs)
        AircraftTelemetryState.updateBatteryConnection(true, nowMs)
        AircraftTelemetryState.updateBatteryChargePercent(80, nowMs)
        AircraftTelemetryState.updateLowBatteryWarning(false, nowMs)
        AircraftTelemetryState.updateSeriousLowBatteryWarning(false, nowMs)
        AircraftTelemetryState.updateLandingConfirmationNeeded(false, nowMs)
        AircraftTelemetryState.updateRemoteControllerFlightMode("P", nowMs)
        AircraftTelemetryState.updateNearHeightLimit(false, nowMs)
        AircraftTelemetryState.updateNearDistanceLimit(false, nowMs)
        AircraftTelemetryState.updateVirtualStickState(false, false, "RC", nowMs)
        AircraftTelemetryState.updateDeviceHealth(emptyList(), nowMs)
    }
}
