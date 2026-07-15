package com.veil.dji

import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicReference

data class AircraftLocationTelemetry(
    val latitudeDegrees: Double,
    val longitudeDegrees: Double,
    /** Altitude exactly as reported by KeyAircraftLocation3D. */
    val altitudeMeters: Double,
    val updatedAtMonotonicMs: Long
)

/** Ground-frame NED velocity: north, east, and down are positive. */
data class AircraftVelocityTelemetry(
    val northMetersPerSecond: Double,
    val eastMetersPerSecond: Double,
    val downMetersPerSecond: Double,
    val updatedAtMonotonicMs: Long
)

data class AircraftAttitudeTelemetry(
    val pitchDegrees: Double,
    val rollDegrees: Double,
    val yawDegrees: Double,
    val updatedAtMonotonicMs: Long
)

data class AircraftGpsTelemetry(
    val satelliteCount: Int? = null,
    val signalLevel: String = "unknown",
    val satelliteCountUpdatedAtMonotonicMs: Long? = null,
    val signalLevelUpdatedAtMonotonicMs: Long? = null
)

data class AircraftCompassTelemetry(
    val count: Int? = null,
    /** Fused compass heading reported by DJI, in degrees. */
    val headingDegrees: Double? = null,
    val hasError: Boolean? = null,
    val countUpdatedAtMonotonicMs: Long? = null,
    val headingUpdatedAtMonotonicMs: Long? = null,
    val errorUpdatedAtMonotonicMs: Long? = null
)

data class AircraftWindTelemetry(
    val warningLevel: String = "unknown",
    /** Raw KeyWindSpeed value. DJI documents this integer in decimeters/second. */
    val speedDecimetersPerSecond: Int? = null,
    val speedMetersPerSecond: Double? = null,
    /** DJI cardinal direction in the world coordinate system. */
    val direction: String = "unknown",
    val warningUpdatedAtMonotonicMs: Long? = null,
    val speedUpdatedAtMonotonicMs: Long? = null,
    val directionUpdatedAtMonotonicMs: Long? = null
)

data class AircraftGimbalTelemetry(
    val pitchDegrees: Double? = null,
    val rollDegrees: Double? = null,
    /** KeyGimbalAttitude yaw in the world NED frame, not relative to the aircraft. */
    val yawDegrees: Double? = null,
    val yawRelativeToAircraftHeadingDegrees: Double? = null,
    val attitudeUpdatedAtMonotonicMs: Long? = null,
    val yawRelativeUpdatedAtMonotonicMs: Long? = null
)

data class AircraftImuCalibrationTelemetry(
    val imuCount: Int? = null,
    val orientationCalibrationState: String = "unknown",
    val calibrationState: String = "unknown",
    val calibrationProgressPercent: Int? = null,
    val orientationsToCalibrate: List<String>? = null,
    val orientationsCalibrated: List<String>? = null,
    val countUpdatedAtMonotonicMs: Long? = null,
    val calibrationUpdatedAtMonotonicMs: Long? = null
)

/** Status/settings derived by MSDK from the aircraft perception system; this is not imagery. */
data class AircraftPerceptionInformationTelemetry(
    val forwardWorking: Boolean? = null,
    val backwardWorking: Boolean? = null,
    val leftWorking: Boolean? = null,
    val rightWorking: Boolean? = null,
    val upwardWorking: Boolean? = null,
    val downwardWorking: Boolean? = null,
    /** Deprecated DJI field; Mini 4 Pro may leave it default/stale. Never authoritative. */
    val overallObstacleAvoidanceEnabled: Boolean? = null,
    val horizontalObstacleAvoidanceEnabled: Boolean? = null,
    val upwardObstacleAvoidanceEnabled: Boolean? = null,
    val downwardObstacleAvoidanceEnabled: Boolean? = null,
    val obstacleAvoidanceType: String = "unknown",
    val horizontalWarningDistanceMeters: Double? = null,
    val upwardWarningDistanceMeters: Double? = null,
    val downwardWarningDistanceMeters: Double? = null,
    val horizontalBrakingDistanceMeters: Double? = null,
    val upwardBrakingDistanceMeters: Double? = null,
    val downwardBrakingDistanceMeters: Double? = null,
    val visionPositioningEnabled: Boolean? = null,
    val precisionLandingEnabled: Boolean? = null,
    val updatedAtMonotonicMs: Long? = null
)

/** Raw ranging values exposed by ObstacleDataListener; no direction-camera frames are included. */
data class AircraftObstacleDistanceTelemetry(
    /** Full 360-degree horizontal ranging vector, in millimeters, in DJI-provided order. */
    val horizontalDistancesMillimeters: List<Int>? = null,
    val horizontalAngleIntervalDegrees: Int? = null,
    val upwardDistanceMillimeters: Int? = null,
    val downwardDistanceMillimeters: Int? = null,
    val updatedAtMonotonicMs: Long? = null
)

data class AircraftPerceptionTelemetry(
    val information: AircraftPerceptionInformationTelemetry =
        AircraftPerceptionInformationTelemetry(),
    val obstacleDistances: AircraftObstacleDistanceTelemetry =
        AircraftObstacleDistanceTelemetry()
)

enum class RemoteControllerAxis {
    LEFT_HORIZONTAL,
    LEFT_VERTICAL,
    RIGHT_HORIZONTAL,
    RIGHT_VERTICAL,
    LEFT_DIAL
}

enum class RemoteControllerButton {
    SHUTTER,
    RECORD,
    GO_HOME,
    CAMERA_MODE_SWITCH,
    CUSTOM_1
}

data class RemoteControllerTelemetry(
    val connected: Boolean = false,
    val leftStickHorizontal: Int? = null,
    val leftStickVertical: Int? = null,
    val rightStickHorizontal: Int? = null,
    val rightStickVertical: Int? = null,
    val leftDial: Int? = null,
    val shutterButtonDown: Boolean? = null,
    val recordButtonDown: Boolean? = null,
    val goHomeButtonDown: Boolean? = null,
    val cameraModeSwitchDown: Boolean? = null,
    val customButton1Down: Boolean? = null,
    val batteryEnabled: Boolean? = null,
    /** Raw BatteryInfo.batteryPower; DJI does not document a unit for this field. */
    val batteryPowerRaw: Int? = null,
    val batteryPercent: Int? = null,
    val connectionUpdatedAtMonotonicMs: Long? = null,
    val leftStickHorizontalUpdatedAtMonotonicMs: Long? = null,
    val leftStickVerticalUpdatedAtMonotonicMs: Long? = null,
    val rightStickHorizontalUpdatedAtMonotonicMs: Long? = null,
    val rightStickVerticalUpdatedAtMonotonicMs: Long? = null,
    val leftDialUpdatedAtMonotonicMs: Long? = null,
    val shutterButtonUpdatedAtMonotonicMs: Long? = null,
    val recordButtonUpdatedAtMonotonicMs: Long? = null,
    val goHomeButtonUpdatedAtMonotonicMs: Long? = null,
    val cameraModeSwitchUpdatedAtMonotonicMs: Long? = null,
    val customButton1UpdatedAtMonotonicMs: Long? = null,
    val batteryUpdatedAtMonotonicMs: Long? = null
)

data class AircraftHomeRthTelemetry(
    /** Null means that this source has not produced a value since connection. */
    val homeLocationSet: Boolean = false,
    val homeLatitudeDegrees: Double? = null,
    val homeLongitudeDegrees: Double? = null,
    /** Valid raw KeyHomeLocation candidate, even if KeyIsHomeLocationSet arrives later. */
    val homeCandidateLatitudeDegrees: Double? = null,
    val homeCandidateLongitudeDegrees: Double? = null,
    val homeCandidateUpdatedAtMonotonicMs: Long? = null,
    val homeLocationRejectionReason: String? = null,
    val homeLocationSetUpdatedAtMonotonicMs: Long? = null,
    val homeLocationUpdatedAtMonotonicMs: Long? = null,
    val goHomeStatus: String = "unknown",
    /** DJI documents KeyGoHomeHeight and its range in whole meters. */
    val goHomeHeightMeters: Int? = null,
    val goHomeHeightMinimumMeters: Int? = null,
    val goHomeHeightMaximumMeters: Int? = null,
    val goHomeHeightDefaultMeters: Int? = null,
    val goHomeUpdatedAtMonotonicMs: Long? = null,
    val goHomeRangeUpdatedAtMonotonicMs: Long? = null,
    val flightControllerFailsafe: Boolean = false,
    val failsafeUpdatedAtMonotonicMs: Long? = null,
    val failsafeAction: String = "unknown",
    val failsafeActionUpdatedAtMonotonicMs: Long? = null,
    val batteryPercentNeededToGoHome: Int? = null,
    val batteryPercentNeededToLand: Int? = null,
    val remainingFlightTimeSeconds: Int? = null,
    val timeNeededToGoHomeSeconds: Int? = null,
    val timeNeededToLandSeconds: Int? = null,
    val lowBatteryRthStatus: String = "unknown",
    val lowBatteryRthUpdatedAtMonotonicMs: Long? = null
)

data class AircraftBatteryTelemetry(
    val connected: Boolean = false,
    val chargeRemainingPercent: Int? = null,
    val chargeRemainingMah: Int? = null,
    val fullChargeCapacityMah: Int? = null,
    val voltageMillivolts: Int? = null,
    val currentMilliamps: Int? = null,
    val temperatureCelsius: Double? = null,
    val cellVoltagesMillivolts: List<Int>? = null,
    val numberOfDischarges: Int? = null,
    val numberOfCells: Int? = null,
    val manufacturedYear: Int? = null,
    val manufacturedMonth: Int? = null,
    val manufacturedDay: Int? = null,
    val serialNumber: String? = null,
    val firmwareVersion: String? = null,
    val connectionUpdatedAtMonotonicMs: Long? = null,
    val chargePercentUpdatedAtMonotonicMs: Long? = null,
    val numberOfDischargesUpdatedAtMonotonicMs: Long? = null,
    val numberOfCellsUpdatedAtMonotonicMs: Long? = null,
    val manufacturedDateUpdatedAtMonotonicMs: Long? = null,
    val serialNumberUpdatedAtMonotonicMs: Long? = null,
    val firmwareVersionUpdatedAtMonotonicMs: Long? = null,
    val updatedAtMonotonicMs: Long? = null
)

data class AircraftSafetyTelemetry(
    val lowBatteryWarning: Boolean? = null,
    val seriousLowBatteryWarning: Boolean? = null,
    /** Raw KeyUltrasonicHeight value. DJI documents this integer in decimeters. */
    val ultrasonicHeightDecimeters: Int? = null,
    val ultrasonicHeightMeters: Double? = null,
    val landingConfirmationNeeded: Boolean? = null,
    val landingProtectionState: String = "unknown",
    val remoteControllerFlightMode: String = "unknown",
    val nearHeightLimit: Boolean? = null,
    val nearDistanceLimit: Boolean? = null,
    val lowBatteryWarningUpdatedAtMonotonicMs: Long? = null,
    val seriousLowBatteryWarningUpdatedAtMonotonicMs: Long? = null,
    val ultrasonicHeightUpdatedAtMonotonicMs: Long? = null,
    val landingConfirmationUpdatedAtMonotonicMs: Long? = null,
    val landingProtectionUpdatedAtMonotonicMs: Long? = null,
    val remoteControllerFlightModeUpdatedAtMonotonicMs: Long? = null,
    val nearHeightLimitUpdatedAtMonotonicMs: Long? = null,
    val nearDistanceLimitUpdatedAtMonotonicMs: Long? = null
)

/**
 * Change reason is an event describing the last transition, not the current
 * owner. Separate timestamps prevent an old reason from masquerading as fresh
 * authority state.
 */
data class FlightAuthorityTelemetry(
    val virtualStickEnabled: Boolean = false,
    val virtualStickAdvancedModeEnabled: Boolean = false,
    val owner: String = "UNKNOWN",
    val lastChangeReason: String = "UNKNOWN",
    val stateUpdatedAtMonotonicMs: Long? = null,
    val lastChangeReasonUpdatedAtMonotonicMs: Long? = null
)

data class AndroidLocationReadinessTelemetry(
    val coarsePermissionGranted: Boolean = false,
    val finePermissionGranted: Boolean = false,
    val locationProviderEnabled: Boolean = false,
    val lastKnownLocationAvailable: Boolean = false,
    val lastKnownLocationProvider: String? = null,
    val lastKnownLocationAccuracyMeters: Float? = null,
    val lastKnownLocationElapsedRealtimeMs: Long? = null,
    val lastKnownLocationMock: Boolean? = null,
    val updatedAtMonotonicMs: Long? = null
)

data class UasRemoteIdTelemetry(
    val broadcastEnabled: Boolean? = null,
    val workingState: String = "unknown",
    val updatedAtMonotonicMs: Long? = null
)

data class DeviceHealthIssueTelemetry(
    val informationCode: String,
    val warningLevel: String,
    val componentId: Int,
    val sensorIndex: Int,
    val title: String,
    val description: String
)

data class DeviceHealthTelemetry(
    val issues: List<DeviceHealthIssueTelemetry> = emptyList(),
    /** A non-null timestamp distinguishes an observed empty list from unknown. */
    val updatedAtMonotonicMs: Long? = null
)

data class AircraftTelemetrySnapshot(
    val aircraftConnected: Boolean = false,
    val isFlying: Boolean = false,
    val motorsOn: Boolean = false,
    val flightMode: String = "unknown",
    /** Despite the key name, DJI documents this raw value in units of 0.1 s. */
    val flightTimeDeciseconds: Int? = null,
    val connectionUpdatedAtMonotonicMs: Long? = null,
    val flightStateUpdatedAtMonotonicMs: Long? = null,
    /** Per-key timestamps prevent an unrelated flight-state key from making a default false look observed. */
    val isFlyingUpdatedAtMonotonicMs: Long? = null,
    val motorsOnUpdatedAtMonotonicMs: Long? = null,
    val flightModeUpdatedAtMonotonicMs: Long? = null,
    val location: AircraftLocationTelemetry? = null,
    val locationSourceUpdatedAtMonotonicMs: Long? = null,
    val locationRejectionReason: String? = null,
    val velocity: AircraftVelocityTelemetry? = null,
    val attitude: AircraftAttitudeTelemetry? = null,
    val gps: AircraftGpsTelemetry = AircraftGpsTelemetry(),
    val compass: AircraftCompassTelemetry = AircraftCompassTelemetry(),
    val wind: AircraftWindTelemetry = AircraftWindTelemetry(),
    val gimbal: AircraftGimbalTelemetry = AircraftGimbalTelemetry(),
    val imuCalibration: AircraftImuCalibrationTelemetry = AircraftImuCalibrationTelemetry(),
    val perception: AircraftPerceptionTelemetry = AircraftPerceptionTelemetry(),
    val homeRth: AircraftHomeRthTelemetry = AircraftHomeRthTelemetry(),
    val battery: AircraftBatteryTelemetry = AircraftBatteryTelemetry(),
    val safety: AircraftSafetyTelemetry = AircraftSafetyTelemetry(),
    val authority: FlightAuthorityTelemetry = FlightAuthorityTelemetry(),
    val androidLocation: AndroidLocationReadinessTelemetry = AndroidLocationReadinessTelemetry(),
    val remoteId: UasRemoteIdTelemetry = UasRemoteIdTelemetry(),
    val deviceHealth: DeviceHealthTelemetry = DeviceHealthTelemetry(),
    /** RC data is intentionally retained across an aircraft-only disconnect. */
    val remoteController: RemoteControllerTelemetry = RemoteControllerTelemetry()
)

enum class PreflightProfile(
    val wireName: String,
    val authorizesFlight: Boolean,
    val requiresRemoteIdWorking: Boolean
) {
    /** Bench/read-only readiness. This profile never represents flight clearance. */
    LAB("lab", false, false),
    /** Navigation-source readiness for a GPS-positioned flight workflow. */
    GPS_FLIGHT("gps_flight", true, false),
    /** Caller-selected policy for operations where working RID is required. */
    GPS_FLIGHT_RID_REQUIRED("gps_flight_rid_required", true, true)
}

data class PreflightIssue(val code: String, val message: String)

data class TelemetrySourceFreshness(
    val source: String,
    val updatedAtMonotonicMs: Long?,
    val ageMs: Long?,
    val maximumAgeMs: Long?,
    val fresh: Boolean
)

data class PreflightReadinessReport(
    val profile: PreflightProfile,
    val ready: Boolean,
    val evaluatedAtMonotonicMs: Long,
    val blockers: List<PreflightIssue>,
    val warnings: List<PreflightIssue>,
    val sourceFreshness: List<TelemetrySourceFreshness>
)

/** Thread-safe read-only state populated by supported Mini 4 Pro MSDK 5.18 keys. */
object AircraftTelemetryState {
    const val DEFAULT_NAVIGATION_MAX_AGE_MS = 2_000L
    const val DEFAULT_BATTERY_MAX_AGE_MS = 10_000L
    const val DEFAULT_OPERATOR_LOCATION_MAX_AGE_MS = 10_000L
    const val DEFAULT_OPERATOR_LOCATION_MAX_ACCURACY_METERS = 100.0f

    private val state = AtomicReference(AircraftTelemetrySnapshot())

    fun snapshot(): AircraftTelemetrySnapshot = state.get()

    fun preflightReadiness(
        profile: PreflightProfile,
        nowMs: Long = monotonicMillis(),
        navigationMaximumAgeMs: Long = DEFAULT_NAVIGATION_MAX_AGE_MS,
        batteryMaximumAgeMs: Long = DEFAULT_BATTERY_MAX_AGE_MS
    ): PreflightReadinessReport = evaluateReadiness(
        snapshot(),
        profile,
        nowMs,
        navigationMaximumAgeMs,
        batteryMaximumAgeMs
    )

    /** Stable, primitive-only representation for the authenticated Mac API. */
    fun toJson(nowMs: Long = monotonicMillis()): JSONObject {
        val current = snapshot()
        return JSONObject()
            .put("aircraft_connected", current.aircraftConnected)
            .put("is_flying", current.isFlying)
            .put("motors_on", current.motorsOn)
            .put("flight_mode", current.flightMode)
            .putNullable("flight_time_raw_deciseconds", current.flightTimeDeciseconds)
            .putNullable("flight_time_s", current.flightTimeDeciseconds?.div(10.0))
            .putNullable("connection_updated_monotonic_ms", current.connectionUpdatedAtMonotonicMs)
            .putNullable("flight_state_updated_monotonic_ms", current.flightStateUpdatedAtMonotonicMs)
            .putNullable("is_flying_updated_monotonic_ms", current.isFlyingUpdatedAtMonotonicMs)
            .putNullable("motors_on_updated_monotonic_ms", current.motorsOnUpdatedAtMonotonicMs)
            .putNullable("flight_mode_updated_monotonic_ms", current.flightModeUpdatedAtMonotonicMs)
            .put("location", current.location?.let {
                JSONObject()
                    .put("latitude_deg", it.latitudeDegrees)
                    .put("longitude_deg", it.longitudeDegrees)
                    .put("altitude_m", it.altitudeMeters)
                    .put("updated_monotonic_ms", it.updatedAtMonotonicMs)
            } ?: JSONObject.NULL)
            .putNullable("location_source_updated_monotonic_ms", current.locationSourceUpdatedAtMonotonicMs)
            .putNullable("location_rejection_reason", current.locationRejectionReason)
            .put("velocity_ned", current.velocity?.let {
                JSONObject()
                    .put("north_mps", it.northMetersPerSecond)
                    .put("east_mps", it.eastMetersPerSecond)
                    .put("down_mps", it.downMetersPerSecond)
                    .put("updated_monotonic_ms", it.updatedAtMonotonicMs)
            } ?: JSONObject.NULL)
            .put("attitude", current.attitude?.let {
                JSONObject()
                    .put("pitch_deg", it.pitchDegrees)
                    .put("roll_deg", it.rollDegrees)
                    .put("yaw_deg", it.yawDegrees)
                    .put("updated_monotonic_ms", it.updatedAtMonotonicMs)
            } ?: JSONObject.NULL)
            .put("gps", current.gps.toJson())
            .put("compass", current.compass.toJson(nowMs))
            .put("wind", current.wind.toJson(nowMs))
            .put("gimbal", current.gimbal.toJson(nowMs))
            .put("imu_calibration", current.imuCalibration.toJson(nowMs))
            .put("perception", current.perception.toJson(nowMs))
            .put("home_rth", current.homeRth.toJson())
            .put("battery", current.battery.toJson())
            .put("safety", current.safety.toJson())
            .put("authority", current.authority.toJson())
            .put("android_location", current.androidLocation.toJson())
            .put("remote_id", current.remoteId.toJson())
            .put("device_health", current.deviceHealth.toJson())
            .put("remote_controller", current.remoteController.toJson(nowMs))
            .put("preflight", JSONObject()
                .put(PreflightProfile.LAB.wireName, evaluateReadiness(
                    current,
                    PreflightProfile.LAB,
                    nowMs,
                    DEFAULT_NAVIGATION_MAX_AGE_MS,
                    DEFAULT_BATTERY_MAX_AGE_MS
                ).toJson())
                .put(PreflightProfile.GPS_FLIGHT.wireName, evaluateReadiness(
                    current,
                    PreflightProfile.GPS_FLIGHT,
                    nowMs,
                    DEFAULT_NAVIGATION_MAX_AGE_MS,
                    DEFAULT_BATTERY_MAX_AGE_MS
                ).toJson())
                .put(PreflightProfile.GPS_FLIGHT_RID_REQUIRED.wireName, evaluateReadiness(
                    current,
                    PreflightProfile.GPS_FLIGHT_RID_REQUIRED,
                    nowMs,
                    DEFAULT_NAVIGATION_MAX_AGE_MS,
                    DEFAULT_BATTERY_MAX_AGE_MS
                ).toJson()))
    }

    fun updateAircraftConnection(connected: Boolean, nowMs: Long = monotonicMillis()) {
        update { current ->
            if (connected) {
                if (current.aircraftConnected) {
                    // Preserve the rising-edge timestamp and connection-scoped
                    // observations when a connected=true key update repeats.
                    current
                } else {
                    // Key listeners can populate cached values before the FC
                    // connection rising edge. Start a clean aircraft session so
                    // no pre-connect default/safety/battery sample can authorize
                    // an action on the newly connected aircraft.
                    AircraftTelemetrySnapshot(
                        aircraftConnected = true,
                        connectionUpdatedAtMonotonicMs = nowMs,
                        androidLocation = current.androidLocation,
                        remoteController = current.remoteController
                    )
                }
            } else {
                AircraftTelemetrySnapshot(
                    aircraftConnected = false,
                    connectionUpdatedAtMonotonicMs = nowMs,
                    battery = AircraftBatteryTelemetry(
                        connected = false,
                        connectionUpdatedAtMonotonicMs = nowMs,
                        updatedAtMonotonicMs = nowMs
                    ),
                    authority = FlightAuthorityTelemetry(
                        owner = "UNKNOWN",
                        lastChangeReason = "AIRCRAFT_DISCONNECTED",
                        stateUpdatedAtMonotonicMs = nowMs,
                        lastChangeReasonUpdatedAtMonotonicMs = nowMs
                    ),
                    androidLocation = current.androidLocation,
                    remoteController = current.remoteController
                )
            }
        }
    }

    fun updateFlightState(
        isFlying: Boolean? = null,
        motorsOn: Boolean? = null,
        flightMode: String? = null,
        flightTimeDeciseconds: Int? = null,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(
                isFlying = isFlying ?: current.isFlying,
                motorsOn = motorsOn ?: current.motorsOn,
                flightMode = flightMode ?: current.flightMode,
                flightTimeDeciseconds = flightTimeDeciseconds ?: current.flightTimeDeciseconds,
                flightStateUpdatedAtMonotonicMs = nowMs,
                isFlyingUpdatedAtMonotonicMs = if (isFlying != null) {
                    nowMs
                } else {
                    current.isFlyingUpdatedAtMonotonicMs
                },
                motorsOnUpdatedAtMonotonicMs = if (motorsOn != null) {
                    nowMs
                } else {
                    current.motorsOnUpdatedAtMonotonicMs
                },
                flightModeUpdatedAtMonotonicMs = if (flightMode != null) {
                    nowMs
                } else {
                    current.flightModeUpdatedAtMonotonicMs
                }
            )
        }
    }

    fun updateLocation(
        latitudeDegrees: Double,
        longitudeDegrees: Double,
        altitudeMeters: Double,
        nowMs: Long = monotonicMillis()
    ) {
        val rejection = coordinateRejection(latitudeDegrees, longitudeDegrees)
            ?: if (!altitudeMeters.isFinite()) "non_finite_altitude" else null
        update { current ->
            if (rejection != null) {
                current.copy(
                    location = null,
                    locationSourceUpdatedAtMonotonicMs = nowMs,
                    locationRejectionReason = rejection
                )
            } else {
                current.copy(
                    location = AircraftLocationTelemetry(
                        latitudeDegrees,
                        longitudeDegrees,
                        altitudeMeters,
                        nowMs
                    ),
                    locationSourceUpdatedAtMonotonicMs = nowMs,
                    locationRejectionReason = null
                )
            }
        }
    }

    fun updateLocationUnavailable(reason: String, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(
                location = null,
                locationSourceUpdatedAtMonotonicMs = nowMs,
                locationRejectionReason = reason
            )
        }
    }

    fun updateVelocity(
        northMetersPerSecond: Double,
        eastMetersPerSecond: Double,
        downMetersPerSecond: Double,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(
                velocity = if (northMetersPerSecond.isFinite() &&
                    eastMetersPerSecond.isFinite() && downMetersPerSecond.isFinite()
                ) AircraftVelocityTelemetry(
                    northMetersPerSecond,
                    eastMetersPerSecond,
                    downMetersPerSecond,
                    nowMs
                ) else null
            )
        }
    }

    fun updateAttitude(
        pitchDegrees: Double,
        rollDegrees: Double,
        yawDegrees: Double,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(
                attitude = if (pitchDegrees.isFinite() && rollDegrees.isFinite() &&
                    yawDegrees.isFinite()
                ) AircraftAttitudeTelemetry(
                    pitchDegrees,
                    rollDegrees,
                    yawDegrees,
                    nowMs
                ) else null
            )
        }
    }

    fun updateGpsSatelliteCount(count: Int?, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(gps = current.gps.copy(
                satelliteCount = count?.takeIf { it >= 0 },
                satelliteCountUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateGpsSignalLevel(level: String, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(gps = current.gps.copy(
                signalLevel = level,
                signalLevelUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateCompassCount(count: Int?, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(compass = current.compass.copy(
                count = count?.takeIf { it >= 0 },
                countUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateCompassHeading(headingDegrees: Double?, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(compass = current.compass.copy(
                headingDegrees = headingDegrees?.takeIf(Double::isFinite),
                headingUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateCompassError(hasError: Boolean?, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(compass = current.compass.copy(
                hasError = hasError,
                errorUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateWindWarning(level: String, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(wind = current.wind.copy(
                warningLevel = level,
                warningUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateWindSpeed(rawDecimetersPerSecond: Int?, nowMs: Long = monotonicMillis()) {
        val validRaw = rawDecimetersPerSecond?.takeIf { it >= 0 }
        update { current ->
            current.copy(wind = current.wind.copy(
                speedDecimetersPerSecond = validRaw,
                speedMetersPerSecond = validRaw?.div(10.0),
                speedUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateWindDirection(direction: String, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(wind = current.wind.copy(
                direction = direction,
                directionUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateGimbalAttitude(
        pitchDegrees: Double?,
        rollDegrees: Double?,
        yawDegrees: Double?,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(gimbal = current.gimbal.copy(
                pitchDegrees = pitchDegrees?.takeIf(Double::isFinite),
                rollDegrees = rollDegrees?.takeIf(Double::isFinite),
                yawDegrees = yawDegrees?.takeIf(Double::isFinite),
                attitudeUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateGimbalYawRelativeToAircraftHeading(
        yawDegrees: Double?,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(gimbal = current.gimbal.copy(
                yawRelativeToAircraftHeadingDegrees = yawDegrees?.takeIf(Double::isFinite),
                yawRelativeUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateImuCount(count: Int?, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(imuCalibration = current.imuCalibration.copy(
                imuCount = count?.takeIf { it >= 0 },
                countUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateImuCalibration(
        orientationCalibrationState: String,
        calibrationState: String,
        calibrationProgressPercent: Int?,
        orientationsToCalibrate: List<String>?,
        orientationsCalibrated: List<String>?,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(imuCalibration = current.imuCalibration.copy(
                orientationCalibrationState = orientationCalibrationState,
                calibrationState = calibrationState,
                calibrationProgressPercent = calibrationProgressPercent?.takeIf { it in 0..100 },
                orientationsToCalibrate = orientationsToCalibrate?.toList(),
                orientationsCalibrated = orientationsCalibrated?.toList(),
                calibrationUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updatePerceptionInformation(
        forwardWorking: Boolean?,
        backwardWorking: Boolean?,
        leftWorking: Boolean?,
        rightWorking: Boolean?,
        upwardWorking: Boolean?,
        downwardWorking: Boolean?,
        overallObstacleAvoidanceEnabled: Boolean,
        horizontalObstacleAvoidanceEnabled: Boolean,
        upwardObstacleAvoidanceEnabled: Boolean,
        downwardObstacleAvoidanceEnabled: Boolean,
        obstacleAvoidanceType: String,
        horizontalWarningDistanceMeters: Double,
        upwardWarningDistanceMeters: Double,
        downwardWarningDistanceMeters: Double,
        horizontalBrakingDistanceMeters: Double,
        upwardBrakingDistanceMeters: Double,
        downwardBrakingDistanceMeters: Double,
        visionPositioningEnabled: Boolean,
        precisionLandingEnabled: Boolean,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(perception = current.perception.copy(
                information = AircraftPerceptionInformationTelemetry(
                    forwardWorking = forwardWorking,
                    backwardWorking = backwardWorking,
                    leftWorking = leftWorking,
                    rightWorking = rightWorking,
                    upwardWorking = upwardWorking,
                    downwardWorking = downwardWorking,
                    overallObstacleAvoidanceEnabled = overallObstacleAvoidanceEnabled,
                    horizontalObstacleAvoidanceEnabled = horizontalObstacleAvoidanceEnabled,
                    upwardObstacleAvoidanceEnabled = upwardObstacleAvoidanceEnabled,
                    downwardObstacleAvoidanceEnabled = downwardObstacleAvoidanceEnabled,
                    obstacleAvoidanceType = obstacleAvoidanceType,
                    horizontalWarningDistanceMeters = horizontalWarningDistanceMeters.takeIf(Double::isFinite),
                    upwardWarningDistanceMeters = upwardWarningDistanceMeters.takeIf(Double::isFinite),
                    downwardWarningDistanceMeters = downwardWarningDistanceMeters.takeIf(Double::isFinite),
                    horizontalBrakingDistanceMeters = horizontalBrakingDistanceMeters.takeIf(Double::isFinite),
                    upwardBrakingDistanceMeters = upwardBrakingDistanceMeters.takeIf(Double::isFinite),
                    downwardBrakingDistanceMeters = downwardBrakingDistanceMeters.takeIf(Double::isFinite),
                    visionPositioningEnabled = visionPositioningEnabled,
                    precisionLandingEnabled = precisionLandingEnabled,
                    updatedAtMonotonicMs = nowMs
                )
            ))
        }
    }

    fun updateObstacleDistances(
        horizontalDistancesMillimeters: List<Int>?,
        horizontalAngleIntervalDegrees: Int?,
        upwardDistanceMillimeters: Int?,
        downwardDistanceMillimeters: Int?,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(perception = current.perception.copy(
                obstacleDistances = AircraftObstacleDistanceTelemetry(
                    horizontalDistancesMillimeters = horizontalDistancesMillimeters?.toList(),
                    horizontalAngleIntervalDegrees = horizontalAngleIntervalDegrees
                        ?.takeIf { it in 1..360 },
                    upwardDistanceMillimeters = upwardDistanceMillimeters?.takeIf { it >= 0 },
                    downwardDistanceMillimeters = downwardDistanceMillimeters?.takeIf { it >= 0 },
                    updatedAtMonotonicMs = nowMs
                )
            ))
        }
    }

    fun updateHomeLocationSet(isSet: Boolean, nowMs: Long = monotonicMillis()) {
        update { current ->
            val candidateReady = isSet &&
                current.homeRth.homeCandidateLatitudeDegrees != null &&
                current.homeRth.homeCandidateLongitudeDegrees != null
            current.copy(homeRth = current.homeRth.copy(
                homeLocationSet = isSet,
                homeLatitudeDegrees = if (candidateReady) {
                    current.homeRth.homeCandidateLatitudeDegrees
                } else {
                    null
                },
                homeLongitudeDegrees = if (candidateReady) {
                    current.homeRth.homeCandidateLongitudeDegrees
                } else {
                    null
                },
                homeLocationRejectionReason = if (isSet) {
                    if (candidateReady) null else "home_location_value_unavailable"
                } else {
                    "home_location_not_set"
                },
                homeLocationSetUpdatedAtMonotonicMs = nowMs,
                homeLocationUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateHomeLocation(
        latitudeDegrees: Double,
        longitudeDegrees: Double,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            val coordinateRejection = coordinateRejection(latitudeDegrees, longitudeDegrees)
            val publish = coordinateRejection == null && current.homeRth.homeLocationSet
            val rejection = coordinateRejection ?: if (!publish) "home_location_not_set" else null
            current.copy(homeRth = current.homeRth.copy(
                homeLatitudeDegrees = if (publish) latitudeDegrees else null,
                homeLongitudeDegrees = if (publish) longitudeDegrees else null,
                homeCandidateLatitudeDegrees = if (coordinateRejection == null) {
                    latitudeDegrees
                } else {
                    null
                },
                homeCandidateLongitudeDegrees = if (coordinateRejection == null) {
                    longitudeDegrees
                } else {
                    null
                },
                homeCandidateUpdatedAtMonotonicMs = nowMs,
                homeLocationRejectionReason = rejection,
                homeLocationUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateGoHomeStatus(status: String, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(homeRth = current.homeRth.copy(
                goHomeStatus = status,
                goHomeUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateGoHomeHeight(heightMeters: Int?, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(homeRth = current.homeRth.copy(
                goHomeHeightMeters = heightMeters,
                goHomeUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateGoHomeHeightRange(
        minimumMeters: Int?,
        maximumMeters: Int?,
        defaultMeters: Int?,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(homeRth = current.homeRth.copy(
                goHomeHeightMinimumMeters = minimumMeters,
                goHomeHeightMaximumMeters = maximumMeters,
                goHomeHeightDefaultMeters = defaultMeters,
                goHomeRangeUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateFlightControllerFailsafe(active: Boolean, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(homeRth = current.homeRth.copy(
                flightControllerFailsafe = active,
                failsafeUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateFailsafeAction(action: String, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(homeRth = current.homeRth.copy(
                failsafeAction = action,
                failsafeActionUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateLowBatteryRth(
        batteryPercentNeededToGoHome: Int?,
        batteryPercentNeededToLand: Int?,
        remainingFlightTimeSeconds: Int?,
        timeNeededToGoHomeSeconds: Int?,
        timeNeededToLandSeconds: Int?,
        status: String,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(homeRth = current.homeRth.copy(
                batteryPercentNeededToGoHome = batteryPercentNeededToGoHome,
                batteryPercentNeededToLand = batteryPercentNeededToLand,
                remainingFlightTimeSeconds = remainingFlightTimeSeconds,
                timeNeededToGoHomeSeconds = timeNeededToGoHomeSeconds,
                timeNeededToLandSeconds = timeNeededToLandSeconds,
                lowBatteryRthStatus = status,
                lowBatteryRthUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateLowBatteryWarning(active: Boolean, nowMs: Long = monotonicMillis()) {
        updateSafety { it.copy(
            lowBatteryWarning = active,
            lowBatteryWarningUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateSeriousLowBatteryWarning(active: Boolean, nowMs: Long = monotonicMillis()) {
        updateSafety { it.copy(
            seriousLowBatteryWarning = active,
            seriousLowBatteryWarningUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateUltrasonicHeight(rawDecimeters: Int?, nowMs: Long = monotonicMillis()) {
        val validRaw = rawDecimeters?.takeIf { it >= 0 }
        updateSafety { it.copy(
            ultrasonicHeightDecimeters = validRaw,
            ultrasonicHeightMeters = validRaw?.div(10.0),
            ultrasonicHeightUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateLandingConfirmationNeeded(needed: Boolean, nowMs: Long = monotonicMillis()) {
        updateSafety { it.copy(
            landingConfirmationNeeded = needed,
            landingConfirmationUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateLandingProtectionState(value: String, nowMs: Long = monotonicMillis()) {
        updateSafety { it.copy(
            landingProtectionState = value,
            landingProtectionUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateRemoteControllerFlightMode(value: String, nowMs: Long = monotonicMillis()) {
        updateSafety { it.copy(
            remoteControllerFlightMode = value,
            remoteControllerFlightModeUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateNearHeightLimit(value: Boolean, nowMs: Long = monotonicMillis()) {
        updateSafety { it.copy(
            nearHeightLimit = value,
            nearHeightLimitUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateNearDistanceLimit(value: Boolean, nowMs: Long = monotonicMillis()) {
        updateSafety { it.copy(
            nearDistanceLimit = value,
            nearDistanceLimitUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateBatteryConnection(connected: Boolean, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(battery = if (connected) {
                current.battery.copy(
                    connected = true,
                    connectionUpdatedAtMonotonicMs = nowMs,
                    updatedAtMonotonicMs = nowMs
                )
            } else {
                AircraftBatteryTelemetry(
                    connected = false,
                    connectionUpdatedAtMonotonicMs = nowMs,
                    updatedAtMonotonicMs = nowMs
                )
            })
        }
    }

    fun updateBatteryChargePercent(value: Int?, nowMs: Long = monotonicMillis()) {
        updateBattery(nowMs) { it.copy(
            chargeRemainingPercent = value?.takeIf { percent -> percent in 0..100 },
            chargePercentUpdatedAtMonotonicMs = nowMs
        ) }
    }

    fun updateBatteryChargeRemaining(valueMah: Int?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(chargeRemainingMah = valueMah?.takeIf { value -> value >= 0 }) }

    fun updateBatteryFullChargeCapacity(valueMah: Int?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(fullChargeCapacityMah = valueMah?.takeIf { value -> value >= 0 }) }

    fun updateBatteryVoltage(valueMillivolts: Int?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(voltageMillivolts = valueMillivolts?.takeIf { value -> value >= 0 }) }

    fun updateBatteryCurrent(valueMilliamps: Int?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(currentMilliamps = valueMilliamps) }

    fun updateBatteryTemperature(valueCelsius: Double?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(temperatureCelsius = valueCelsius?.takeIf(Double::isFinite)) }

    fun updateBatteryCellVoltages(valuesMillivolts: List<Int>?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(
            cellVoltagesMillivolts = valuesMillivolts?.takeIf { values -> values.all { it >= 0 } }?.toList()
        ) }

    fun updateBatteryNumberOfDischarges(value: Int?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(
            numberOfDischarges = value?.takeIf { count -> count >= 0 },
            numberOfDischargesUpdatedAtMonotonicMs = nowMs
        ) }

    fun updateBatteryNumberOfCells(value: Int?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(
            numberOfCells = value?.takeIf { count -> count >= 0 },
            numberOfCellsUpdatedAtMonotonicMs = nowMs
        ) }

    fun updateBatteryManufacturedDate(
        year: Int?,
        month: Int?,
        day: Int?,
        nowMs: Long = monotonicMillis()
    ) = updateBattery(nowMs) { battery ->
        val valid = year != null && year >= 0 && month != null && month in 1..12 &&
            day != null && day in 1..31
        battery.copy(
            manufacturedYear = year?.takeIf { valid },
            manufacturedMonth = month?.takeIf { valid },
            manufacturedDay = day?.takeIf { valid },
            manufacturedDateUpdatedAtMonotonicMs = nowMs
        )
    }

    fun updateBatterySerialNumber(value: String?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(
            serialNumber = value?.takeIf(String::isNotBlank),
            serialNumberUpdatedAtMonotonicMs = nowMs
        ) }

    fun updateBatteryFirmwareVersion(value: String?, nowMs: Long = monotonicMillis()) =
        updateBattery(nowMs) { it.copy(
            firmwareVersion = value?.takeIf(String::isNotBlank),
            firmwareVersionUpdatedAtMonotonicMs = nowMs
        ) }

    fun updateRemoteControllerConnection(
        connected: Boolean,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(remoteController = if (connected) {
                current.remoteController.copy(
                    connected = true,
                    connectionUpdatedAtMonotonicMs = nowMs
                )
            } else {
                RemoteControllerTelemetry(
                    connected = false,
                    connectionUpdatedAtMonotonicMs = nowMs
                )
            })
        }
    }

    fun updateRemoteControllerStick(
        axis: RemoteControllerAxis,
        value: Int?,
        nowMs: Long = monotonicMillis()
    ) {
        val validValue = value?.takeIf { it in -660..660 }
        update { current ->
            val rc = current.remoteController
            current.copy(remoteController = when (axis) {
                RemoteControllerAxis.LEFT_HORIZONTAL -> rc.copy(
                    leftStickHorizontal = validValue,
                    leftStickHorizontalUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerAxis.LEFT_VERTICAL -> rc.copy(
                    leftStickVertical = validValue,
                    leftStickVerticalUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerAxis.RIGHT_HORIZONTAL -> rc.copy(
                    rightStickHorizontal = validValue,
                    rightStickHorizontalUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerAxis.RIGHT_VERTICAL -> rc.copy(
                    rightStickVertical = validValue,
                    rightStickVerticalUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerAxis.LEFT_DIAL -> rc.copy(
                    leftDial = validValue,
                    leftDialUpdatedAtMonotonicMs = nowMs
                )
            })
        }
    }

    fun updateRemoteControllerButton(
        button: RemoteControllerButton,
        down: Boolean?,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            val rc = current.remoteController
            current.copy(remoteController = when (button) {
                RemoteControllerButton.SHUTTER -> rc.copy(
                    shutterButtonDown = down,
                    shutterButtonUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerButton.RECORD -> rc.copy(
                    recordButtonDown = down,
                    recordButtonUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerButton.GO_HOME -> rc.copy(
                    goHomeButtonDown = down,
                    goHomeButtonUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerButton.CAMERA_MODE_SWITCH -> rc.copy(
                    cameraModeSwitchDown = down,
                    cameraModeSwitchUpdatedAtMonotonicMs = nowMs
                )
                RemoteControllerButton.CUSTOM_1 -> rc.copy(
                    customButton1Down = down,
                    customButton1UpdatedAtMonotonicMs = nowMs
                )
            })
        }
    }

    fun updateRemoteControllerBattery(
        enabled: Boolean?,
        batteryPowerRaw: Int?,
        batteryPercent: Int?,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(remoteController = current.remoteController.copy(
                batteryEnabled = enabled,
                batteryPowerRaw = batteryPowerRaw,
                batteryPercent = batteryPercent?.takeIf { it in 0..100 },
                batteryUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateVirtualStickState(
        enabled: Boolean,
        advancedModeEnabled: Boolean,
        owner: String,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(authority = current.authority.copy(
                virtualStickEnabled = enabled,
                // DJI can leave the mode bit set after authority is released.
                // It has no operational meaning while Virtual Stick is off.
                virtualStickAdvancedModeEnabled = enabled && advancedModeEnabled,
                owner = owner,
                stateUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateAuthorityChangeReason(reason: String, nowMs: Long = monotonicMillis()) {
        update { current ->
            current.copy(authority = current.authority.copy(
                lastChangeReason = reason,
                lastChangeReasonUpdatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateAndroidLocationReadiness(
        coarsePermissionGranted: Boolean,
        finePermissionGranted: Boolean,
        locationProviderEnabled: Boolean,
        lastKnownLocationAvailable: Boolean = false,
        lastKnownLocationProvider: String? = null,
        lastKnownLocationAccuracyMeters: Float? = null,
        lastKnownLocationElapsedRealtimeMs: Long? = null,
        lastKnownLocationMock: Boolean? = null,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(androidLocation = AndroidLocationReadinessTelemetry(
                coarsePermissionGranted = coarsePermissionGranted,
                finePermissionGranted = finePermissionGranted,
                locationProviderEnabled = locationProviderEnabled,
                lastKnownLocationAvailable = lastKnownLocationAvailable,
                lastKnownLocationProvider = lastKnownLocationProvider,
                lastKnownLocationAccuracyMeters = lastKnownLocationAccuracyMeters,
                lastKnownLocationElapsedRealtimeMs = lastKnownLocationElapsedRealtimeMs,
                lastKnownLocationMock = lastKnownLocationMock,
                updatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateRemoteIdStatus(
        broadcastEnabled: Boolean,
        workingState: String,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(remoteId = UasRemoteIdTelemetry(
                broadcastEnabled = broadcastEnabled,
                workingState = workingState,
                updatedAtMonotonicMs = nowMs
            ))
        }
    }

    fun updateDeviceHealth(
        issues: List<DeviceHealthIssueTelemetry>,
        nowMs: Long = monotonicMillis()
    ) {
        update { current ->
            current.copy(deviceHealth = DeviceHealthTelemetry(
                issues = issues.distinctBy {
                    listOf(it.informationCode, it.componentId.toString(), it.sensorIndex.toString())
                }.sortedWith(compareBy<DeviceHealthIssueTelemetry> { it.warningLevel }
                    .thenBy { it.informationCode }),
                updatedAtMonotonicMs = nowMs
            ))
        }
    }

    private fun evaluateReadiness(
        current: AircraftTelemetrySnapshot,
        profile: PreflightProfile,
        nowMs: Long,
        navigationMaximumAgeMs: Long,
        batteryMaximumAgeMs: Long
    ): PreflightReadinessReport {
        require(navigationMaximumAgeMs > 0)
        require(batteryMaximumAgeMs > 0)

        val blockers = mutableListOf<PreflightIssue>()
        val warnings = mutableListOf<PreflightIssue>()
        val sources = listOf(
            freshness("flight_controller_connection", current.connectionUpdatedAtMonotonicMs, nowMs, null),
            freshness("flight_state", current.flightStateUpdatedAtMonotonicMs, nowMs, null),
            freshness("location", current.locationSourceUpdatedAtMonotonicMs, nowMs, navigationMaximumAgeMs),
            freshness("velocity", current.velocity?.updatedAtMonotonicMs, nowMs, navigationMaximumAgeMs),
            freshness("attitude", current.attitude?.updatedAtMonotonicMs, nowMs, navigationMaximumAgeMs),
            freshness("gps_satellite_count", current.gps.satelliteCountUpdatedAtMonotonicMs, nowMs, navigationMaximumAgeMs),
            freshness("gps_signal_level", current.gps.signalLevelUpdatedAtMonotonicMs, nowMs, navigationMaximumAgeMs),
            freshness("home_location_set", current.homeRth.homeLocationSetUpdatedAtMonotonicMs, nowMs, null),
            freshness("home_location", current.homeRth.homeLocationUpdatedAtMonotonicMs, nowMs, null),
            freshness("go_home", current.homeRth.goHomeUpdatedAtMonotonicMs, nowMs, null),
            freshness("go_home_range", current.homeRth.goHomeRangeUpdatedAtMonotonicMs, nowMs, null),
            freshness("failsafe", current.homeRth.failsafeUpdatedAtMonotonicMs, nowMs, null),
            freshness("failsafe_action", current.homeRth.failsafeActionUpdatedAtMonotonicMs, nowMs, null),
            freshness("battery_connection", current.battery.connectionUpdatedAtMonotonicMs, nowMs, null),
            freshness("battery_charge", current.battery.chargePercentUpdatedAtMonotonicMs, nowMs, batteryMaximumAgeMs),
            freshness("low_battery_warning", current.safety.lowBatteryWarningUpdatedAtMonotonicMs, nowMs, null),
            freshness("serious_low_battery_warning", current.safety.seriousLowBatteryWarningUpdatedAtMonotonicMs, nowMs, null),
            freshness("landing_confirmation", current.safety.landingConfirmationUpdatedAtMonotonicMs, nowMs, null),
            freshness("remote_controller_flight_mode", current.safety.remoteControllerFlightModeUpdatedAtMonotonicMs, nowMs, null),
            freshness("near_height_limit", current.safety.nearHeightLimitUpdatedAtMonotonicMs, nowMs, null),
            freshness("near_distance_limit", current.safety.nearDistanceLimitUpdatedAtMonotonicMs, nowMs, null),
            freshness("authority_state", current.authority.stateUpdatedAtMonotonicMs, nowMs, null),
            freshness("authority_last_reason", current.authority.lastChangeReasonUpdatedAtMonotonicMs, nowMs, null),
            freshness("android_location_readiness", current.androidLocation.updatedAtMonotonicMs, nowMs, null),
            freshness(
                "android_last_known_location",
                current.androidLocation.lastKnownLocationElapsedRealtimeMs,
                nowMs,
                DEFAULT_OPERATOR_LOCATION_MAX_AGE_MS
            ),
            freshness("remote_id", current.remoteId.updatedAtMonotonicMs, nowMs, null),
            freshness("device_health", current.deviceHealth.updatedAtMonotonicMs, nowMs, null)
        )
        val sourceByName = sources.associateBy { it.source }

        fun block(code: String, message: String) {
            if (blockers.none { it.code == code }) blockers += PreflightIssue(code, message)
        }

        fun warn(code: String, message: String) {
            if (warnings.none { it.code == code }) warnings += PreflightIssue(code, message)
        }

        if (!current.aircraftConnected) {
            block(
                if (current.connectionUpdatedAtMonotonicMs == null) "aircraft_connection_unknown" else "aircraft_disconnected",
                if (current.connectionUpdatedAtMonotonicMs == null) {
                    "Flight-controller connection has not been observed."
                } else {
                    "Flight controller is disconnected."
                }
            )
        }
        if (!current.battery.connected) {
            block(
                if (current.battery.connectionUpdatedAtMonotonicMs == null) "battery_connection_unknown" else "battery_disconnected",
                if (current.battery.connectionUpdatedAtMonotonicMs == null) {
                    "Aircraft battery connection has not been observed."
                } else {
                    "Aircraft battery is disconnected."
                }
            )
        }

        if (profile == PreflightProfile.LAB) {
            warn("lab_not_flight_clearance", "LAB readiness is for bench/read-only checks and never authorizes flight.")
            if (current.battery.chargeRemainingPercent == null) {
                warn("battery_charge_unknown", "Battery charge percentage is unavailable.")
            }
            if (current.safety.lowBatteryWarning == true) {
                warn("low_battery", "The aircraft reports its low-battery warning.")
            }
            if (current.safety.seriousLowBatteryWarning == true) {
                warn("serious_low_battery", "The aircraft reports its serious-low-battery warning.")
            }
            if (current.location == null) warn("location_unavailable", "A valid aircraft location is unavailable; this is expected indoors.")
            if (!gpsSupportsHover(current.gps.signalLevel)) warn("gps_not_hover_grade", "GPS is below DJI's documented hover-capable level.")
            if (current.homeRth.homeLocationSet != true || !hasValidHome(current.homeRth)) {
                warn("home_unavailable", "A valid, explicitly set home location is unavailable.")
            }
            if (current.homeRth.flightControllerFailsafe == true) warn("failsafe_active", "Flight-controller failsafe is active.")
            if (current.safety.landingConfirmationNeeded == true) warn("landing_confirmation_needed", "The aircraft is waiting for landing confirmation.")
            if (current.safety.nearHeightLimit == true) warn("height_limit_reached", "The aircraft reports the height limit reached.")
            if (current.safety.nearDistanceLimit == true) warn("distance_limit_reached", "The aircraft reports the distance limit reached.")
            if (current.deviceHealth.updatedAtMonotonicMs == null) {
                warn("device_health_unknown", "DJI device-health state has not been observed.")
            }
            current.deviceHealth.issues
                .filter { it.warningLevel != "NORMAL" }
                .forEach { issue ->
                    warn("device_health_${issue.informationCode}", "${issue.warningLevel}: ${issue.title} (${issue.informationCode}).")
                }
            if (!current.androidLocation.finePermissionGranted ||
                !current.androidLocation.locationProviderEnabled
            ) {
                warn("operator_location_not_ready", "Android precise-location permission/provider is not ready for operator-location-dependent RID.")
            } else if (!sourceByName.getValue("android_last_known_location").fresh) {
                warn("operator_location_stale", "Android operator location is absent or older than ${DEFAULT_OPERATOR_LOCATION_MAX_AGE_MS} ms.")
            }
            if (current.remoteId.updatedAtMonotonicMs == null) {
                warn("remote_id_unknown", "Remote ID status has not been observed.")
            } else if (current.remoteId.workingState !in setOf("WORKING", "IDLE", "NOT_SUPPORTED")) {
                warn("remote_id_${current.remoteId.workingState.lowercase()}", "Remote ID reports ${current.remoteId.workingState}.")
            }
        } else {
            fun requireFresh(source: String, code: String) {
                if (sourceByName.getValue(source).fresh.not()) {
                    block(code, "$source telemetry is missing or stale.")
                }
            }

            requireFresh("location", "location_stale")
            requireFresh("velocity", "velocity_stale")
            requireFresh("attitude", "attitude_stale")
            requireFresh("gps_satellite_count", "gps_satellite_count_stale")
            requireFresh("gps_signal_level", "gps_signal_level_stale")
            requireFresh("battery_charge", "battery_charge_stale")

            if (current.location == null) {
                block("location_invalid", "Aircraft location is absent or outside valid latitude/longitude ranges.")
            }
            if (current.gps.satelliteCount == null || current.gps.satelliteCount <= 0) {
                block("gps_satellites_unavailable", "No GPS satellites are reported.")
            }
            if (!gpsSupportsHover(current.gps.signalLevel)) {
                block("gps_not_hover_grade", "GPS is below LEVEL_3, DJI's documented hover-capable level.")
            }
            if (current.homeRth.homeLocationSetUpdatedAtMonotonicMs == null) {
                block("home_set_unknown", "Home-location-set state has not been observed.")
            } else if (!current.homeRth.homeLocationSet) {
                block("home_not_set", "The aircraft reports that its home location is not set.")
            }
            if (!hasValidHome(current.homeRth)) {
                block("home_invalid", "A valid home latitude/longitude is unavailable.")
            }
            if (current.homeRth.failsafeUpdatedAtMonotonicMs == null) {
                block("failsafe_unknown", "Flight-controller failsafe state has not been observed.")
            } else if (current.homeRth.flightControllerFailsafe) {
                block("failsafe_active", "Flight-controller failsafe is active.")
            }
            if (current.battery.chargeRemainingPercent == null) {
                block("battery_charge_unknown", "Battery charge percentage is unavailable.")
            }
            if (current.safety.lowBatteryWarningUpdatedAtMonotonicMs == null) {
                block("low_battery_unknown", "Low-battery warning state has not been observed.")
            } else if (current.safety.lowBatteryWarning == true) {
                block("low_battery_active", "The aircraft reports its low-battery warning.")
            }
            if (current.safety.seriousLowBatteryWarningUpdatedAtMonotonicMs == null) {
                block("serious_low_battery_unknown", "Serious-low-battery warning state has not been observed.")
            } else if (current.safety.seriousLowBatteryWarning == true) {
                block("serious_low_battery_active", "The aircraft reports its serious-low-battery warning.")
            }
            if (current.safety.landingConfirmationUpdatedAtMonotonicMs == null) {
                block("landing_confirmation_unknown", "Landing-confirmation state has not been observed.")
            } else if (current.safety.landingConfirmationNeeded == true) {
                block("landing_confirmation_needed", "The aircraft is waiting for landing confirmation.")
            }
            if (current.safety.remoteControllerFlightModeUpdatedAtMonotonicMs == null) {
                block("rc_mode_unknown", "RC switch mode has not been observed.")
            } else if (current.safety.remoteControllerFlightMode != "P") {
                block("rc_not_normal_mode", "RC mode is not P, DJI's name for normal/N positioning mode.")
            }
            if (current.safety.nearHeightLimitUpdatedAtMonotonicMs == null) {
                block("height_limit_unknown", "Height-limit state has not been observed.")
            } else if (current.safety.nearHeightLimit == true) {
                block("height_limit_reached", "The aircraft reports the height limit reached.")
            }
            if (current.safety.nearDistanceLimitUpdatedAtMonotonicMs == null) {
                block("distance_limit_unknown", "Distance-limit state has not been observed.")
            } else if (current.safety.nearDistanceLimit == true) {
                block("distance_limit_reached", "The aircraft reports the distance limit reached.")
            }
            if (current.homeRth.goHomeUpdatedAtMonotonicMs == null) {
                block("rth_status_unknown", "Return-to-home state has not been observed.")
            } else if (current.homeRth.goHomeStatus != "IDLE") {
                block("rth_not_idle", "Return-to-home state is not IDLE.")
            }
            if (current.homeRth.goHomeRangeUpdatedAtMonotonicMs == null ||
                current.homeRth.goHomeHeightMeters == null
            ) {
                block("rth_height_unknown", "RTH height or aircraft-reported meter range has not been observed.")
            } else if (!goHomeHeightIsValid(current.homeRth)) {
                block("rth_height_unverified", "RTH height is absent or outside the aircraft-reported meter range.")
            }
            if (current.authority.virtualStickEnabled && current.authority.owner != "MSDK") {
                block("authority_inconsistent", "Virtual Stick is enabled but MSDK does not own flight authority.")
            }
            if (current.deviceHealth.updatedAtMonotonicMs == null) {
                block("device_health_unknown", "DJI device-health state has not been observed.")
            } else {
                current.deviceHealth.issues.forEach { issue ->
                    when {
                        isKnownTakeoffBlockingHealthCode(issue.informationCode) -> block(
                            "device_health_${issue.informationCode}",
                            "DJI declares ${issue.informationCode} as a takeoff-blocking Remote ID fault: ${knownRemoteIdHealthMeaning(issue.informationCode)}"
                        )
                        issue.warningLevel in setOf("WARNING", "SERIOUS_WARNING", "UNKNOWN") -> block(
                            "device_health_${issue.informationCode}",
                            "${issue.warningLevel}: ${issue.title} (${issue.informationCode})."
                        )
                        issue.warningLevel in setOf("NOTICE", "CAUTION") -> warn(
                            "device_health_${issue.informationCode}",
                            "${issue.warningLevel}: ${issue.title} (${issue.informationCode})."
                        )
                    }
                }
            }
            if (profile.requiresRemoteIdWorking) {
                if (current.androidLocation.updatedAtMonotonicMs == null) {
                    block("operator_location_readiness_unknown", "Android location readiness has not been observed.")
                } else {
                    if (!current.androidLocation.finePermissionGranted) {
                        block("fine_location_permission_missing", "Precise Android location permission is not granted.")
                    }
                    if (!current.androidLocation.coarsePermissionGranted) {
                        block("coarse_location_permission_missing", "Coarse Android location permission is not granted; DJI checks both permissions.")
                    }
                    if (!current.androidLocation.locationProviderEnabled) {
                        block("location_provider_disabled", "Android location provider is disabled.")
                    }
                    if (!current.androidLocation.lastKnownLocationAvailable) {
                        block("operator_location_unavailable", "No enabled Android provider has a last-known operator location for DJI Remote ID.")
                    }
                    if (current.androidLocation.lastKnownLocationMock == true) {
                        block(
                            "operator_location_mock",
                            "Android reports that the operator location came from a mock provider."
                        )
                    }
                    if (!sourceByName.getValue("android_last_known_location").fresh) {
                        block("operator_location_stale", "Android operator location is absent, future-dated, or older than ${DEFAULT_OPERATOR_LOCATION_MAX_AGE_MS} ms.")
                    }
                    val accuracy = current.androidLocation.lastKnownLocationAccuracyMeters
                    if (accuracy == null || !accuracy.isFinite() || accuracy < 0.0f ||
                        accuracy > DEFAULT_OPERATOR_LOCATION_MAX_ACCURACY_METERS
                    ) {
                        block(
                            "operator_location_accuracy_unacceptable",
                            "Android operator-location accuracy is absent/non-finite or worse than ${DEFAULT_OPERATOR_LOCATION_MAX_ACCURACY_METERS} m."
                        )
                    }
                }
                if (current.remoteId.updatedAtMonotonicMs == null) {
                    block("remote_id_unknown", "Remote ID status has not been observed.")
                } else {
                    if (current.remoteId.broadcastEnabled != true) {
                        block("remote_id_broadcast_disabled", "Remote ID broadcast is not enabled.")
                    }
                    if (current.remoteId.workingState != "WORKING") {
                        block("remote_id_not_working", "Remote ID state is ${current.remoteId.workingState}, not WORKING.")
                    }
                }
            } else if (current.remoteId.workingState == "OPERATOR_LOCATION_LOST_ERROR") {
                warn("remote_id_operator_location_lost", "Remote ID reports that operator location is lost; choose the RID-required profile where applicable.")
            }
            if (current.safety.landingProtectionState in setOf("ANALYSIS_FAILED", "NOT_SAFE_TO_LAND")) {
                warn("landing_surface_warning", "Landing protection does not currently report a safe landing surface.")
            }
        }

        return PreflightReadinessReport(
            profile = profile,
            ready = blockers.isEmpty(),
            evaluatedAtMonotonicMs = nowMs,
            blockers = blockers.sortedBy { it.code },
            warnings = warnings.sortedBy { it.code },
            sourceFreshness = sources
        )
    }

    private fun freshness(
        source: String,
        updatedAtMs: Long?,
        nowMs: Long,
        maximumAgeMs: Long?
    ): TelemetrySourceFreshness {
        val ageMs = updatedAtMs?.let { nowMs - it }
        val fresh = ageMs != null && ageMs >= 0L && (maximumAgeMs == null || ageMs <= maximumAgeMs)
        return TelemetrySourceFreshness(source, updatedAtMs, ageMs, maximumAgeMs, fresh)
    }

    private fun gpsSupportsHover(level: String): Boolean =
        level in setOf("LEVEL_3", "LEVEL_4", "LEVEL_5", "LEVEL_10")

    private fun hasValidHome(home: AircraftHomeRthTelemetry): Boolean {
        val latitude = home.homeLatitudeDegrees ?: return false
        val longitude = home.homeLongitudeDegrees ?: return false
        return home.homeLocationSet == true && coordinateRejection(latitude, longitude) == null
    }

    private fun goHomeHeightIsValid(home: AircraftHomeRthTelemetry): Boolean {
        val height = home.goHomeHeightMeters ?: return false
        val minimum = home.goHomeHeightMinimumMeters ?: return false
        val maximum = home.goHomeHeightMaximumMeters ?: return false
        return minimum <= maximum && height in minimum..maximum
    }

    private fun isKnownTakeoffBlockingHealthCode(code: String): Boolean =
        code.uppercase() in TAKEOFF_BLOCKING_REMOTE_ID_CODES

    private fun knownRemoteIdHealthMeaning(code: String): String? =
        REMOTE_ID_HEALTH_CODE_MEANINGS[code.uppercase()]

    private fun coordinateRejection(latitudeDegrees: Double, longitudeDegrees: Double): String? = when {
        !latitudeDegrees.isFinite() || !longitudeDegrees.isFinite() -> "non_finite_coordinate"
        kotlin.math.abs(latitudeDegrees) <= 1e-6 || kotlin.math.abs(longitudeDegrees) <= 1e-6 ->
            "dji_zero_coordinate_sentinel"
        latitudeDegrees !in -90.0..90.0 -> "latitude_out_of_range"
        longitudeDegrees !in -180.0..180.0 -> "longitude_out_of_range"
        else -> null
    }

    private fun updateSafety(transform: (AircraftSafetyTelemetry) -> AircraftSafetyTelemetry) {
        update { current -> current.copy(safety = transform(current.safety)) }
    }

    private fun updateBattery(
        nowMs: Long,
        transform: (AircraftBatteryTelemetry) -> AircraftBatteryTelemetry
    ) {
        update { current ->
            current.copy(battery = transform(current.battery).copy(updatedAtMonotonicMs = nowMs))
        }
    }

    private inline fun update(transform: (AircraftTelemetrySnapshot) -> AircraftTelemetrySnapshot) {
        while (true) {
            val current = state.get()
            if (state.compareAndSet(current, transform(current))) return
        }
    }

    // Location.elapsedRealtimeNanos is explicitly in this clock domain. Using
    // System.nanoTime() here would pause across deep sleep and could make an old
    // operator-location fix look newer than it is after the tablet wakes.
    private fun monotonicMillis(): Long = SystemClock.elapsedRealtime()

    private val TAKEOFF_BLOCKING_REMOTE_ID_CODES = setOf("0X161000B4", "0X161000B5")
    private val REMOTE_ID_HEALTH_CODE_MEANINGS = mapOf(
        "0X1B080003" to "Remote ID normal",
        "0X161000B4" to "REMOTE_ID_CANNOT_TAKE_OFF_USER_LOCATION_UNAVALIABLE",
        "0X1B080001" to "REMOTE_ID_USER_LOCATION_ABNORMAL",
        "0X161000B5" to "Remote ID cannot take off: link error",
        "0X1B080002" to "Remote ID link error"
    )
}

private fun AircraftGpsTelemetry.toJson(): JSONObject = JSONObject()
    .putNullable("satellite_count", satelliteCount)
    .put("signal_level", signalLevel)
    .putNullable("satellite_count_updated_monotonic_ms", satelliteCountUpdatedAtMonotonicMs)
    .putNullable("signal_level_updated_monotonic_ms", signalLevelUpdatedAtMonotonicMs)

private fun AircraftCompassTelemetry.toJson(nowMs: Long): JSONObject {
    val latest = latestTimestamp(
        countUpdatedAtMonotonicMs,
        headingUpdatedAtMonotonicMs,
        errorUpdatedAtMonotonicMs
    )
    return JSONObject()
        .put("source", "FlightControllerKey.KeyCompassCount/KeyCompassHeading/KeyCompassHasError")
        .put("observed", latest != null)
        .putNullable("updated_monotonic_ms", latest)
        .putNullable("age_ms", sourceAge(nowMs, latest))
        .putNullable("count", count)
        .putNullable("heading_deg", headingDegrees)
        .putNullable("has_error", hasError)
        .putNullable("count_updated_monotonic_ms", countUpdatedAtMonotonicMs)
        .putNullable("heading_updated_monotonic_ms", headingUpdatedAtMonotonicMs)
        .putNullable("error_updated_monotonic_ms", errorUpdatedAtMonotonicMs)
}

private fun AircraftWindTelemetry.toJson(nowMs: Long): JSONObject {
    val latest = latestTimestamp(
        warningUpdatedAtMonotonicMs,
        speedUpdatedAtMonotonicMs,
        directionUpdatedAtMonotonicMs
    )
    return JSONObject()
        .put("source", "FlightControllerKey.KeyWindWarning/KeyWindSpeed/KeyWindDirection")
        .put("observed", latest != null)
        .putNullable("updated_monotonic_ms", latest)
        .putNullable("age_ms", sourceAge(nowMs, latest))
        .put("warning_level", warningLevel)
        .putNullable("speed_raw_dm_s", speedDecimetersPerSecond)
        .putNullable("speed_m_s", speedMetersPerSecond)
        .put("direction_world", direction)
        .putNullable("warning_updated_monotonic_ms", warningUpdatedAtMonotonicMs)
        .putNullable("speed_updated_monotonic_ms", speedUpdatedAtMonotonicMs)
        .putNullable("direction_updated_monotonic_ms", directionUpdatedAtMonotonicMs)
}

private fun AircraftGimbalTelemetry.toJson(nowMs: Long): JSONObject {
    val latest = latestTimestamp(attitudeUpdatedAtMonotonicMs, yawRelativeUpdatedAtMonotonicMs)
    return JSONObject()
        .put(
            "source",
            "GimbalKey.KeyGimbalAttitude/KeyYawRelativeToAircraftHeading:LEFT_OR_MAIN"
        )
        .put("observed", latest != null)
        .putNullable("updated_monotonic_ms", latest)
        .putNullable("age_ms", sourceAge(nowMs, latest))
        .putNullable("pitch_deg", pitchDegrees)
        .putNullable("roll_deg", rollDegrees)
        .putNullable("yaw_deg", yawDegrees)
        .put("yaw_coordinate_frame", "world_ned")
        .put("yaw_is_aircraft_relative", false)
        .putNullable(
            "yaw_relative_to_aircraft_heading_deg",
            yawRelativeToAircraftHeadingDegrees
        )
        .putNullable("attitude_updated_monotonic_ms", attitudeUpdatedAtMonotonicMs)
        .putNullable("yaw_relative_updated_monotonic_ms", yawRelativeUpdatedAtMonotonicMs)
}

private fun AircraftImuCalibrationTelemetry.toJson(nowMs: Long): JSONObject {
    val latest = latestTimestamp(countUpdatedAtMonotonicMs, calibrationUpdatedAtMonotonicMs)
    return JSONObject()
        .put("source", "FlightControllerKey.KeyIMUCount/KeyIMUCalibrationInfo")
        .put("observed", latest != null)
        .putNullable("updated_monotonic_ms", latest)
        .putNullable("age_ms", sourceAge(nowMs, latest))
        .putNullable("imu_count", imuCount)
        .put("orientation_calibration_state", orientationCalibrationState)
        .put("calibration_state", calibrationState)
        .putNullable("calibration_progress_percent", calibrationProgressPercent)
        .put("orientations_to_calibrate", orientationsToCalibrate?.let(::JSONArray) ?: JSONObject.NULL)
        .put("orientations_calibrated", orientationsCalibrated?.let(::JSONArray) ?: JSONObject.NULL)
        .putNullable("count_updated_monotonic_ms", countUpdatedAtMonotonicMs)
        .putNullable("calibration_updated_monotonic_ms", calibrationUpdatedAtMonotonicMs)
        .put("raw_accelerometer_exposed", false)
        .put("raw_gyroscope_exposed", false)
}

private fun AircraftPerceptionTelemetry.toJson(nowMs: Long): JSONObject = JSONObject()
    .put("source", "PerceptionManager")
    .put("raw_obstacle_camera_imagery_exposed", false)
    .put("information", information.toJson(nowMs))
    .put("obstacle_distances", obstacleDistances.toJson(nowMs))

private fun AircraftPerceptionInformationTelemetry.toJson(nowMs: Long): JSONObject = JSONObject()
    .put("source", "PerceptionInformationListener")
    .put("observed", updatedAtMonotonicMs != null)
    .putNullable("updated_monotonic_ms", updatedAtMonotonicMs)
    .putNullable("age_ms", sourceAge(nowMs, updatedAtMonotonicMs))
    .put("working", JSONObject()
        .putNullable("forward", forwardWorking)
        .putNullable("backward", backwardWorking)
        .putNullable("left", leftWorking)
        .putNullable("right", rightWorking)
        .putNullable("upward", upwardWorking)
        .putNullable("downward", downwardWorking))
    .put("enabled", JSONObject()
        .putNullable("deprecated_overall_non_authoritative", overallObstacleAvoidanceEnabled)
        .put("deprecated_overall_is_authoritative", false)
        .putNullable(
            "effective_from_avoidance_type",
            obstacleAvoidanceType.takeUnless { it == "unknown" }?.let { it != "CLOSE" }
        )
        .putNullable("horizontal", horizontalObstacleAvoidanceEnabled)
        .putNullable("upward", upwardObstacleAvoidanceEnabled)
        .putNullable("downward", downwardObstacleAvoidanceEnabled)
        .putNullable("vision_positioning", visionPositioningEnabled)
        .putNullable("precision_landing", precisionLandingEnabled))
    .put("obstacle_avoidance_type", obstacleAvoidanceType)
    .put("warning_distance_m", JSONObject()
        .putNullable("horizontal", horizontalWarningDistanceMeters)
        .putNullable("upward", upwardWarningDistanceMeters)
        .putNullable("downward", downwardWarningDistanceMeters))
    .put("braking_distance_m", JSONObject()
        .putNullable("horizontal", horizontalBrakingDistanceMeters)
        .putNullable("upward", upwardBrakingDistanceMeters)
        .putNullable("downward", downwardBrakingDistanceMeters))

private fun AircraftObstacleDistanceTelemetry.toJson(nowMs: Long): JSONObject = JSONObject()
    .put("source", "ObstacleDataListener")
    .put("observed", updatedAtMonotonicMs != null)
    .putNullable("updated_monotonic_ms", updatedAtMonotonicMs)
    .putNullable("age_ms", sourceAge(nowMs, updatedAtMonotonicMs))
    .put("distance_unit", "millimeter")
    .put("horizontal_angle_unit", "degree")
    .put("horizontal_angle_origin_and_order", "dji_provided_undocumented")
    .putNullable("horizontal_angle_interval_deg", horizontalAngleIntervalDegrees)
    .putNullable("horizontal_sample_count", horizontalDistancesMillimeters?.size)
    .put(
        "horizontal_distance_mm",
        horizontalDistancesMillimeters?.let(::JSONArray) ?: JSONObject.NULL
    )
    .putNullable("upward_distance_mm", upwardDistanceMillimeters)
    .putNullable("downward_distance_mm", downwardDistanceMillimeters)

private fun AircraftHomeRthTelemetry.toJson(): JSONObject = JSONObject()
    .putNullable("home_location_set", homeLocationSet)
    .putNullable("home_latitude_deg", homeLatitudeDegrees)
    .putNullable("home_longitude_deg", homeLongitudeDegrees)
    .putNullable("home_location_rejection_reason", homeLocationRejectionReason)
    .putNullable("home_location_set_updated_monotonic_ms", homeLocationSetUpdatedAtMonotonicMs)
    .putNullable("home_location_updated_monotonic_ms", homeLocationUpdatedAtMonotonicMs)
    .put("go_home_status", goHomeStatus)
    .putNullable("go_home_height_m", goHomeHeightMeters)
    .putNullable("go_home_height_range_min_m", goHomeHeightMinimumMeters)
    .putNullable("go_home_height_range_max_m", goHomeHeightMaximumMeters)
    .putNullable("go_home_height_range_default_m", goHomeHeightDefaultMeters)
    .putNullable("go_home_updated_monotonic_ms", goHomeUpdatedAtMonotonicMs)
    .putNullable("go_home_range_updated_monotonic_ms", goHomeRangeUpdatedAtMonotonicMs)
    .putNullable("flight_controller_failsafe", flightControllerFailsafe)
    .putNullable("failsafe_updated_monotonic_ms", failsafeUpdatedAtMonotonicMs)
    .put("failsafe_action", failsafeAction)
    .putNullable("failsafe_action_updated_monotonic_ms", failsafeActionUpdatedAtMonotonicMs)
    .putNullable("battery_percent_needed_to_go_home", batteryPercentNeededToGoHome)
    .putNullable("battery_percent_needed_to_land", batteryPercentNeededToLand)
    .putNullable("remaining_flight_time_s", remainingFlightTimeSeconds)
    .putNullable("time_needed_to_go_home_s", timeNeededToGoHomeSeconds)
    .putNullable("time_needed_to_land_s", timeNeededToLandSeconds)
    .put("low_battery_rth_status", lowBatteryRthStatus)
    .putNullable("low_battery_rth_updated_monotonic_ms", lowBatteryRthUpdatedAtMonotonicMs)

private fun AircraftBatteryTelemetry.toJson(): JSONObject = JSONObject()
    .putNullable("connected", connected)
    .putNullable("charge_remaining_percent", chargeRemainingPercent)
    .putNullable("charge_remaining_mah", chargeRemainingMah)
    .putNullable("full_charge_capacity_mah", fullChargeCapacityMah)
    .putNullable("voltage_mv", voltageMillivolts)
    .putNullable("current_ma", currentMilliamps)
    .putNullable("temperature_c", temperatureCelsius)
    .put("cell_voltages_mv", cellVoltagesMillivolts?.let(::JSONArray) ?: JSONObject.NULL)
    .putNullable("number_of_discharges", numberOfDischarges)
    .putNullable("number_of_cells", numberOfCells)
    .put("manufactured_date", if (
        manufacturedYear != null && manufacturedMonth != null && manufacturedDay != null
    ) {
        JSONObject()
            .put("year", manufacturedYear)
            .put("month", manufacturedMonth)
            .put("day", manufacturedDay)
    } else {
        JSONObject.NULL
    })
    .putNullable("serial_number", serialNumber)
    .putNullable("firmware_version", firmwareVersion)
    .putNullable("connection_updated_monotonic_ms", connectionUpdatedAtMonotonicMs)
    .putNullable("charge_percent_updated_monotonic_ms", chargePercentUpdatedAtMonotonicMs)
    .putNullable("number_of_discharges_updated_monotonic_ms", numberOfDischargesUpdatedAtMonotonicMs)
    .putNullable("number_of_cells_updated_monotonic_ms", numberOfCellsUpdatedAtMonotonicMs)
    .putNullable("manufactured_date_updated_monotonic_ms", manufacturedDateUpdatedAtMonotonicMs)
    .putNullable("serial_number_updated_monotonic_ms", serialNumberUpdatedAtMonotonicMs)
    .putNullable("firmware_version_updated_monotonic_ms", firmwareVersionUpdatedAtMonotonicMs)
    .putNullable("updated_monotonic_ms", updatedAtMonotonicMs)

private fun AircraftSafetyTelemetry.toJson(): JSONObject = JSONObject()
    .putNullable("low_battery_warning", lowBatteryWarning)
    .putNullable("serious_low_battery_warning", seriousLowBatteryWarning)
    .putNullable("ultrasonic_height_raw_dm", ultrasonicHeightDecimeters)
    .putNullable("ultrasonic_height_m", ultrasonicHeightMeters)
    .putNullable("landing_confirmation_needed", landingConfirmationNeeded)
    .put("landing_protection_state", landingProtectionState)
    .put("remote_controller_flight_mode", remoteControllerFlightMode)
    .putNullable("near_height_limit", nearHeightLimit)
    .putNullable("near_distance_limit", nearDistanceLimit)
    .putNullable("low_battery_warning_updated_monotonic_ms", lowBatteryWarningUpdatedAtMonotonicMs)
    .putNullable("serious_low_battery_warning_updated_monotonic_ms", seriousLowBatteryWarningUpdatedAtMonotonicMs)
    .putNullable("ultrasonic_height_updated_monotonic_ms", ultrasonicHeightUpdatedAtMonotonicMs)
    .putNullable("landing_confirmation_updated_monotonic_ms", landingConfirmationUpdatedAtMonotonicMs)
    .putNullable("landing_protection_updated_monotonic_ms", landingProtectionUpdatedAtMonotonicMs)
    .putNullable("remote_controller_flight_mode_updated_monotonic_ms", remoteControllerFlightModeUpdatedAtMonotonicMs)
    .putNullable("near_height_limit_updated_monotonic_ms", nearHeightLimitUpdatedAtMonotonicMs)
    .putNullable("near_distance_limit_updated_monotonic_ms", nearDistanceLimitUpdatedAtMonotonicMs)

private fun FlightAuthorityTelemetry.toJson(): JSONObject = JSONObject()
    .put("virtual_stick_enabled", virtualStickEnabled)
    .put("advanced_mode_enabled", virtualStickAdvancedModeEnabled)
    .put("owner", owner)
    .put("last_change_reason", lastChangeReason)
    .putNullable("state_updated_monotonic_ms", stateUpdatedAtMonotonicMs)
    .putNullable("last_change_reason_updated_monotonic_ms", lastChangeReasonUpdatedAtMonotonicMs)

private fun AndroidLocationReadinessTelemetry.toJson(): JSONObject = JSONObject()
    .put("coarse_permission_granted", coarsePermissionGranted)
    .put("fine_permission_granted", finePermissionGranted)
    .put("location_provider_enabled", locationProviderEnabled)
    .put("last_known_location_available", lastKnownLocationAvailable)
    .putNullable("last_known_location_provider", lastKnownLocationProvider)
    .putNullable("last_known_location_accuracy_m", lastKnownLocationAccuracyMeters)
    .putNullable("last_known_location_elapsed_realtime_ms", lastKnownLocationElapsedRealtimeMs)
    .putNullable("last_known_location_mock", lastKnownLocationMock)
    .putNullable("updated_monotonic_ms", updatedAtMonotonicMs)

private fun UasRemoteIdTelemetry.toJson(): JSONObject = JSONObject()
    .putNullable("broadcast_enabled", broadcastEnabled)
    .put("working_state", workingState)
    .putNullable("updated_monotonic_ms", updatedAtMonotonicMs)

private fun DeviceHealthTelemetry.toJson(): JSONObject = JSONObject()
    .put("observed", updatedAtMonotonicMs != null)
    .putNullable("updated_monotonic_ms", updatedAtMonotonicMs)
    .put("issues", JSONArray(issues.map {
        JSONObject()
            .put("information_code", it.informationCode)
            .put("warning_level", it.warningLevel)
            .put("component_id", it.componentId)
            .put("sensor_index", it.sensorIndex)
            .put("title", it.title)
            .put("description", it.description)
    }))

private fun RemoteControllerTelemetry.toJson(nowMs: Long): JSONObject {
    val latest = latestTimestamp(
        connectionUpdatedAtMonotonicMs,
        leftStickHorizontalUpdatedAtMonotonicMs,
        leftStickVerticalUpdatedAtMonotonicMs,
        rightStickHorizontalUpdatedAtMonotonicMs,
        rightStickVerticalUpdatedAtMonotonicMs,
        leftDialUpdatedAtMonotonicMs,
        shutterButtonUpdatedAtMonotonicMs,
        recordButtonUpdatedAtMonotonicMs,
        goHomeButtonUpdatedAtMonotonicMs,
        cameraModeSwitchUpdatedAtMonotonicMs,
        customButton1UpdatedAtMonotonicMs,
        batteryUpdatedAtMonotonicMs
    )
    return JSONObject()
        .put("source", "RemoteControllerKey:DJI_RC_N2")
        .put("observed", latest != null)
        .put("connected", connected)
        .putNullable("updated_monotonic_ms", latest)
        .putNullable("age_ms", sourceAge(nowMs, latest))
        .putNullable("connection_updated_monotonic_ms", connectionUpdatedAtMonotonicMs)
        .put("sticks", JSONObject()
            .putNullable("left_horizontal", leftStickHorizontal)
            .putNullable("left_vertical", leftStickVertical)
            .putNullable("right_horizontal", rightStickHorizontal)
            .putNullable("right_vertical", rightStickVertical)
            .put("range", "[-660,660]")
            .putNullable("left_horizontal_updated_monotonic_ms", leftStickHorizontalUpdatedAtMonotonicMs)
            .putNullable("left_vertical_updated_monotonic_ms", leftStickVerticalUpdatedAtMonotonicMs)
            .putNullable("right_horizontal_updated_monotonic_ms", rightStickHorizontalUpdatedAtMonotonicMs)
            .putNullable("right_vertical_updated_monotonic_ms", rightStickVerticalUpdatedAtMonotonicMs))
        .put("left_dial", JSONObject()
            .putNullable("value", leftDial)
            .put("range", "[-660,660]")
            .putNullable("updated_monotonic_ms", leftDialUpdatedAtMonotonicMs))
        .put("buttons", JSONObject()
            .putNullable("shutter_down", shutterButtonDown)
            .putNullable("record_down", recordButtonDown)
            .putNullable("go_home_down", goHomeButtonDown)
            .putNullable("camera_mode_switch_down", cameraModeSwitchDown)
            .putNullable("custom_1_down", customButton1Down)
            .put("updated_monotonic_ms", JSONObject()
                .putNullable("shutter", shutterButtonUpdatedAtMonotonicMs)
                .putNullable("record", recordButtonUpdatedAtMonotonicMs)
                .putNullable("go_home", goHomeButtonUpdatedAtMonotonicMs)
                .putNullable("camera_mode_switch", cameraModeSwitchUpdatedAtMonotonicMs)
                .putNullable("custom_1", customButton1UpdatedAtMonotonicMs)))
        .put("battery", JSONObject()
            .putNullable("enabled", batteryEnabled)
            .putNullable("power_raw", batteryPowerRaw)
            .put("power_raw_unit", "undocumented")
            .putNullable("percent", batteryPercent)
            .putNullable("updated_monotonic_ms", batteryUpdatedAtMonotonicMs))
}

private fun PreflightReadinessReport.toJson(): JSONObject = JSONObject()
    .put("profile", profile.wireName)
    .put("ready", ready)
    .put("authorizes_flight", profile.authorizesFlight)
    .put("requires_remote_id_working", profile.requiresRemoteIdWorking)
    .put("evaluated_monotonic_ms", evaluatedAtMonotonicMs)
    .put("blockers", JSONArray(blockers.map {
        JSONObject().put("code", it.code).put("message", it.message)
    }))
    .put("warnings", JSONArray(warnings.map {
        JSONObject().put("code", it.code).put("message", it.message)
    }))
    .put("source_freshness", JSONArray(sourceFreshness.map {
        JSONObject()
            .put("source", it.source)
            .putNullable("updated_monotonic_ms", it.updatedAtMonotonicMs)
            .putNullable("age_ms", it.ageMs)
            .putNullable("maximum_age_ms", it.maximumAgeMs)
            .put("fresh", it.fresh)
    }))

private fun JSONObject.putNullable(name: String, value: Any?): JSONObject =
    put(name, value ?: JSONObject.NULL)

private fun latestTimestamp(vararg values: Long?): Long? = values.filterNotNull().maxOrNull()

private fun sourceAge(nowMs: Long, updatedAtMonotonicMs: Long?): Long? =
    updatedAtMonotonicMs?.let { nowMs - it }
