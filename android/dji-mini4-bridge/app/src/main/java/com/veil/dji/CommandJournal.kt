package com.veil.dji

import org.json.JSONArray
import org.json.JSONObject
import java.util.LinkedHashMap

enum class CommandState(val wireName: String) {
    REQUESTED("requested"),
    SUCCEEDED("succeeded"),
    FAILED("failed")
}

data class CommandHttpResponseSelection(
    val statusCode: Int,
    /** Present only while an asynchronous action is genuinely pending. */
    val acceptedForProcessing: Boolean?
)

/** HTTP-independent mapping so a locally failed command is never labeled accepted. */
object CommandHttpResponsePolicy {
    fun select(state: CommandState): CommandHttpResponseSelection = when (state) {
        CommandState.REQUESTED -> CommandHttpResponseSelection(202, true)
        CommandState.SUCCEEDED -> CommandHttpResponseSelection(200, null)
        CommandState.FAILED -> CommandHttpResponseSelection(409, null)
    }
}

data class CommandError(
    val source: String,
    val type: String? = null,
    val code: String? = null,
    val innerCode: String? = null,
    val description: String? = null,
    val hint: String? = null,
    val raw: String? = null
)

/** Immutable state captured before an action is dispatched to DJI. */
data class CommandPreconditions(
    val capturedAtMonotonicMs: Long,
    val sdkRegistered: Boolean,
    val productConnected: Boolean,
    val remoteControllerConnected: Boolean,
    val airLinkConnected: Boolean,
    val aircraftConnected: Boolean,
    val isFlying: Boolean,
    val motorsOn: Boolean,
    val flightMode: String,
    val altitudeMeters: Double?,
    val gpsSignalLevel: String,
    val gpsSatelliteCount: Int?,
    val homeLocationSet: Boolean,
    val flightControllerFailsafe: Boolean,
    val batteryConnected: Boolean,
    val batteryPercent: Int?,
    val batteryPercentNeededToGoHome: Int?,
    val batteryPercentNeededToLand: Int?,
    val virtualStickEnabled: Boolean,
    val virtualStickAdvancedMode: Boolean,
    val flightControlAuthority: String,
    val virtualStickControlMode: String,
    val controlFailsafeState: String,
    val batteryPercentAgeMs: Long? = null,
    val lowBatteryWarning: Boolean? = null,
    val seriousLowBatteryWarning: Boolean? = null,
    val landingConfirmationNeeded: Boolean? = null,
    val deviceHealthObserved: Boolean = false,
    val blockingDeviceHealthIssues: List<String> = emptyList(),
    val aircraftConnectionAgeMs: Long? = null,
    val isFlyingAgeMs: Long? = null,
    val motorsOnAgeMs: Long? = null,
    val flightControllerFailsafeAgeMs: Long? = null,
    val remoteControllerFlightMode: String = "unknown",
    val remoteControllerFlightModeAgeMs: Long? = null,
    val remoteIdObserved: Boolean = false,
    val remoteIdWorkingState: String = "unknown",
    val flightControlAuthorityAgeMs: Long? = null,
    val failsafeAction: String = "unknown",
    val failsafeActionAgeMs: Long? = null
)

data class CommandRecord(
    val id: String,
    val type: String,
    val state: CommandState,
    val requestedAtMonotonicMs: Long,
    val requestedAtWallTimeMs: Long,
    val completedAtMonotonicMs: Long? = null,
    val details: Map<String, String> = emptyMap(),
    val result: Map<String, String> = emptyMap(),
    val error: CommandError? = null,
    val preconditions: CommandPreconditions
)

/** Thread-safe process-lifetime ring journal. A terminal result cannot be overwritten. */
class CommandJournal(
    private val capacity: Int = 64,
    private val monotonicClock: () -> Long = ::controlMonotonicMillis,
    private val wallClock: () -> Long = System::currentTimeMillis,
    private val idFactory: (Long, Long) -> String = { now, sequence ->
        "cmd-${now.toString(16)}-${sequence.toString(16)}"
    }
) {
    private val records = LinkedHashMap<String, CommandRecord>()
    private var sequence = 0L

    init {
        require(capacity > 0) { "capacity must be positive" }
    }

    @Synchronized
    fun request(
        type: String,
        preconditions: CommandPreconditions,
        details: Map<String, String> = emptyMap()
    ): CommandRecord {
        val now = monotonicClock()
        val id = idFactory(now, ++sequence)
        val record = CommandRecord(
            id = id,
            type = type,
            state = CommandState.REQUESTED,
            requestedAtMonotonicMs = now,
            requestedAtWallTimeMs = wallClock(),
            details = details.toMap(),
            preconditions = preconditions.copy(
                blockingDeviceHealthIssues = preconditions.blockingDeviceHealthIssues.toList()
            )
        )
        records[id] = record
        while (records.size > capacity) {
            // Preserve pending physical actions whenever a terminal record can
            // be evicted instead; watchdogs need the pending ID to close safely.
            val eviction = records.entries.firstOrNull {
                it.value.state != CommandState.REQUESTED
            } ?: records.entries.firstOrNull {
                it.value.type !in PROTECTED_PENDING_COMMAND_TYPES
            } ?: records.entries.first()
            records.remove(eviction.key)
        }
        return record
    }

    @Synchronized
    fun succeed(id: String, result: Map<String, String> = emptyMap()): CommandRecord? =
        transition(id, CommandState.SUCCEEDED, result.toMap(), null)

    @Synchronized
    fun fail(id: String, error: CommandError): CommandRecord? =
        transition(id, CommandState.FAILED, emptyMap(), error)

    @Synchronized
    fun get(id: String): CommandRecord? = records[id]

    @Synchronized
    fun history(limit: Int = capacity): List<CommandRecord> =
        records.values.toList().asReversed().take(limit.coerceIn(0, capacity))

    @Synchronized
    fun latest(type: String): CommandRecord? =
        records.values.lastOrNull { it.type == type }

    @Synchronized
    fun size(): Int = records.size

    private fun transition(
        id: String,
        state: CommandState,
        result: Map<String, String>,
        error: CommandError?
    ): CommandRecord? {
        val existing = records[id] ?: return null
        if (existing.state != CommandState.REQUESTED) return existing
        return existing.copy(
            state = state,
            completedAtMonotonicMs = monotonicClock(),
            result = result,
            error = error
        ).also { records[id] = it }
    }

    private companion object {
        val PROTECTED_PENDING_COMMAND_TYPES = setOf(
            "takeoff",
            "land",
            "confirm_landing",
            "virtual_stick_enable",
            "virtual_stick_disable",
            "perception_config_set"
        )
    }
}

data class FlightTestReadiness(
    val assessedAtMonotonicMs: Long,
    val readyForTakeoffCommand: Boolean,
    val blockers: List<String>,
    val advisories: List<String>
)

object FlightTestReadinessEvaluator {
    const val MINIMUM_TAKEOFF_BATTERY_PERCENT = 30
    const val MAXIMUM_BATTERY_PERCENT_AGE_MS = 10_000L
    const val MINIMUM_CONNECTION_SETTLE_AGE_MS = 2_000L

    fun evaluate(state: CommandPreconditions): FlightTestReadiness {
        val blockers = ArrayList<String>()
        val advisories = ArrayList<String>()
        if (!state.sdkRegistered) blockers += "sdk_not_registered"
        if (!state.productConnected) blockers += "product_not_connected"
        if (!state.remoteControllerConnected) blockers += "remote_controller_not_connected"
        if (!state.airLinkConnected) blockers += "airlink_not_connected"
        if (!state.aircraftConnected) blockers += "flight_controller_not_connected"
        when (val ageMs = state.aircraftConnectionAgeMs) {
            null -> blockers += "aircraft_connection_freshness_unknown"
            !in 0L..Long.MAX_VALUE -> blockers += "aircraft_connection_timestamp_invalid"
            in 0 until MINIMUM_CONNECTION_SETTLE_AGE_MS ->
                blockers += "aircraft_connection_telemetry_settling"
        }

        fun requireObservedGroundState(label: String, ageMs: Long?) {
            when (ageMs) {
                null -> blockers += "${label}_state_unknown"
                !in 0L..Long.MAX_VALUE -> blockers += "${label}_state_timestamp_invalid"
                else -> if (state.aircraftConnectionAgeMs != null &&
                    ageMs > state.aircraftConnectionAgeMs
                ) {
                    blockers += "${label}_state_before_current_connection"
                }
            }
        }
        requireObservedGroundState("is_flying", state.isFlyingAgeMs)
        requireObservedGroundState("motors_on", state.motorsOnAgeMs)
        requireObservedGroundState(
            "flight_controller_failsafe",
            state.flightControllerFailsafeAgeMs
        )
        requireObservedGroundState("failsafe_action", state.failsafeActionAgeMs)
        requireObservedGroundState(
            "remote_controller_flight_mode",
            state.remoteControllerFlightModeAgeMs
        )
        requireObservedGroundState(
            "flight_control_authority",
            state.flightControlAuthorityAgeMs
        )
        if (state.isFlying) blockers += "aircraft_already_flying"
        if (state.motorsOn) blockers += "motors_already_on"
        if (state.flightControllerFailsafe) blockers += "flight_controller_failsafe_active"
        when (state.failsafeAction) {
            "HOVER", "LANDING" -> Unit
            "GOHOME" -> blockers += "failsafe_action_go_home_unsafe_for_indoor_test"
            else -> blockers += "failsafe_action_unknown_${state.failsafeAction}"
        }
        if (state.remoteControllerFlightMode != "P") {
            blockers += "remote_controller_not_normal_mode_${state.remoteControllerFlightMode}"
        }
        if (state.virtualStickEnabled || state.flightControlAuthority == "MSDK") {
            blockers += "msdk_control_authority_not_released"
        }
        if (state.flightControlAuthority != "RC") {
            blockers += "flight_control_authority_not_rc_${state.flightControlAuthority}"
        }

        if (!state.batteryConnected) blockers += "battery_telemetry_unavailable"
        if (state.batteryPercent == null) blockers += "battery_percent_unknown"
        if (state.batteryPercent != null &&
            state.batteryPercent < MINIMUM_TAKEOFF_BATTERY_PERCENT
        ) blockers += "battery_below_minimum_takeoff_percent"
        if (state.batteryPercent != null) {
            when (val ageMs = state.batteryPercentAgeMs) {
                null -> blockers += "battery_percent_freshness_unknown"
                !in 0..MAXIMUM_BATTERY_PERCENT_AGE_MS -> blockers += "battery_percent_stale"
            }
        }
        when (state.lowBatteryWarning) {
            true -> blockers += "aircraft_low_battery_warning_active"
            null -> blockers += "aircraft_low_battery_warning_unknown"
            false -> Unit
        }
        when (state.seriousLowBatteryWarning) {
            true -> blockers += "aircraft_serious_low_battery_warning_active"
            null -> blockers += "aircraft_serious_low_battery_warning_unknown"
            false -> Unit
        }
        if (!state.deviceHealthObserved) blockers += "device_health_unknown_for_connection"
        if (!state.remoteIdObserved) {
            blockers += "remote_id_unknown_for_connection"
        }
        if (state.remoteIdObserved && state.remoteIdWorkingState !in setOf(
                "WORKING",
                "IDLE",
                "NOT_SUPPORTED"
            )
        ) {
            blockers += "remote_id_state_${state.remoteIdWorkingState}"
        }
        state.blockingDeviceHealthIssues.forEach { issue ->
            blockers += "device_health_$issue"
        }
        val landingPercent = state.batteryPercentNeededToLand
        if (state.batteryPercent != null && landingPercent != null &&
            state.batteryPercent <= landingPercent
        ) blockers += "battery_at_or_below_landing_requirement"
        val rthPercent = state.batteryPercentNeededToGoHome
        if (state.batteryPercent != null && rthPercent != null &&
            state.batteryPercent <= rthPercent
        ) advisories += "battery_at_or_below_go_home_requirement"
        if (!state.homeLocationSet) advisories += "home_location_not_set"
        if (state.gpsSignalLevel !in setOf("LEVEL_3", "LEVEL_4", "LEVEL_5", "LEVEL_10")) {
            advisories += "gps_signal_not_confirmed_good"
        }
        when (state.controlFailsafeState) {
            "disarmed" -> Unit
            "authority_lost" -> advisories += "bridge_control_state_authority_lost"
            else -> blockers += "bridge_control_not_disarmed_${state.controlFailsafeState}"
        }
        return FlightTestReadiness(
            state.capturedAtMonotonicMs,
            blockers.isEmpty(),
            blockers,
            advisories
        )
    }
}

object DeviceHealthTakeoffPolicy {
    fun blockerLabel(informationCode: String, warningLevel: String): String? {
        val code = informationCode.uppercase()
        return when {
            code in NON_BLOCKING_CODES -> null
            code in ALWAYS_BLOCKING_CODES || warningLevel in BLOCKING_LEVELS ->
                "${warningLevel}_$code"
            else -> null
        }
    }

    private val BLOCKING_LEVELS = setOf("WARNING", "SERIOUS_WARNING", "UNKNOWN")
    private val ALWAYS_BLOCKING_CODES = setOf("0X161000B4", "0X161000B5")
    private val NON_BLOCKING_CODES = setOf("0X1B080003")
}

object LandingConfirmationPolicy {
    fun canDispatch(landingConfirmationNeeded: Boolean?): Boolean =
        landingConfirmationNeeded == true
}

object BridgeCommandJournal {
    const val CAPACITY = 64
    val journal = CommandJournal(CAPACITY)

    fun capturePreconditions(): CommandPreconditions {
        val aircraft = AircraftTelemetryState.snapshot()
        val nowMs = controlMonotonicMillis()
        fun ageOf(updatedAtMs: Long?): Long? = updatedAtMs?.let { nowMs - it }
        val connectionUpdatedAtMs = aircraft.connectionUpdatedAtMonotonicMs
        val deviceHealthUpdatedAtMs = aircraft.deviceHealth.updatedAtMonotonicMs
        val remoteIdUpdatedAtMs = aircraft.remoteId.updatedAtMonotonicMs
        return CommandPreconditions(
            capturedAtMonotonicMs = nowMs,
            sdkRegistered = BridgeState.sdkRegistered.get(),
            productConnected = BridgeState.productConnected.get(),
            remoteControllerConnected = BridgeState.remoteControllerConnected.get(),
            airLinkConnected = BridgeState.airLinkConnected.get(),
            aircraftConnected = aircraft.aircraftConnected == true,
            isFlying = aircraft.isFlying == true,
            motorsOn = aircraft.motorsOn == true,
            flightMode = aircraft.flightMode,
            altitudeMeters = aircraft.location?.altitudeMeters,
            gpsSignalLevel = aircraft.gps.signalLevel,
            gpsSatelliteCount = aircraft.gps.satelliteCount,
            homeLocationSet = aircraft.homeRth.homeLocationSet == true,
            flightControllerFailsafe = aircraft.homeRth.flightControllerFailsafe == true,
            batteryConnected = aircraft.battery.connected == true,
            batteryPercent = aircraft.battery.chargeRemainingPercent,
            batteryPercentNeededToGoHome = aircraft.homeRth.batteryPercentNeededToGoHome,
            batteryPercentNeededToLand = aircraft.homeRth.batteryPercentNeededToLand,
            virtualStickEnabled = aircraft.authority.virtualStickEnabled == true,
            virtualStickAdvancedMode = aircraft.authority.virtualStickAdvancedModeEnabled == true,
            flightControlAuthority = aircraft.authority.owner,
            virtualStickControlMode = BridgeState.virtualStickControlMode.get(),
            controlFailsafeState = BridgeState.controlFailsafeState.get(),
            batteryPercentAgeMs = ageOf(aircraft.battery.chargePercentUpdatedAtMonotonicMs),
            lowBatteryWarning = aircraft.safety.lowBatteryWarning,
            seriousLowBatteryWarning = aircraft.safety.seriousLowBatteryWarning,
            landingConfirmationNeeded = aircraft.safety.landingConfirmationNeeded,
            deviceHealthObserved = connectionUpdatedAtMs != null &&
                deviceHealthUpdatedAtMs != null &&
                deviceHealthUpdatedAtMs >= connectionUpdatedAtMs,
            blockingDeviceHealthIssues = aircraft.deviceHealth.issues
                .mapNotNull { issue ->
                    DeviceHealthTakeoffPolicy.blockerLabel(
                        issue.informationCode,
                        issue.warningLevel
                    )
                }
                .distinct()
                .sorted(),
            aircraftConnectionAgeMs = ageOf(connectionUpdatedAtMs),
            isFlyingAgeMs = ageOf(aircraft.isFlyingUpdatedAtMonotonicMs),
            motorsOnAgeMs = ageOf(aircraft.motorsOnUpdatedAtMonotonicMs),
            flightControllerFailsafeAgeMs = ageOf(
                aircraft.homeRth.failsafeUpdatedAtMonotonicMs
            ),
            remoteControllerFlightMode = aircraft.safety.remoteControllerFlightMode,
            remoteControllerFlightModeAgeMs = ageOf(
                aircraft.safety.remoteControllerFlightModeUpdatedAtMonotonicMs
            ),
            remoteIdObserved = connectionUpdatedAtMs != null &&
                remoteIdUpdatedAtMs != null &&
                remoteIdUpdatedAtMs >= connectionUpdatedAtMs,
            remoteIdWorkingState = aircraft.remoteId.workingState,
            flightControlAuthorityAgeMs = ageOf(
                aircraft.authority.stateUpdatedAtMonotonicMs
            ),
            failsafeAction = aircraft.homeRth.failsafeAction,
            failsafeActionAgeMs = ageOf(
                aircraft.homeRth.failsafeActionUpdatedAtMonotonicMs
            )
        )
    }

    fun readiness(): FlightTestReadiness =
        FlightTestReadinessEvaluator.evaluate(capturePreconditions())

    fun recordJson(record: CommandRecord): JSONObject = JSONObject()
        .put("id", record.id)
        .put("type", record.type)
        .put("state", record.state.wireName)
        .put("requested_at_monotonic_ms", record.requestedAtMonotonicMs)
        .put("requested_at_wall_time_ms", record.requestedAtWallTimeMs)
        .put("completed_at_monotonic_ms", record.completedAtMonotonicMs ?: JSONObject.NULL)
        .put("details", JSONObject(record.details))
        .put("result", JSONObject(record.result))
        .put("error", record.error?.let(::errorJson) ?: JSONObject.NULL)
        .put("preconditions", preconditionsJson(record.preconditions))

    fun historyJson(limit: Int = 16): JSONObject = JSONObject()
        .put("capacity", CAPACITY)
        .put("size", journal.size())
        .put("commands", JSONArray(journal.history(limit).map(::recordJson)))

    fun statusJson(): JSONObject = JSONObject()
        .put("capacity", CAPACITY)
        .put("size", journal.size())
        .put("retention_scope", "process_lifetime")
        .put("latest", journal.history(1).firstOrNull()?.let(::recordJson) ?: JSONObject.NULL)
        .put("latest_takeoff", journal.latest("takeoff")?.let(::recordJson) ?: JSONObject.NULL)
        .put("latest_land", journal.latest("land")?.let(::recordJson) ?: JSONObject.NULL)
        .put(
            "latest_confirm_landing",
            journal.latest("confirm_landing")?.let(::recordJson) ?: JSONObject.NULL
        )
        .put("history_endpoint", "/commands")

    fun flightTestResultJson(): JSONObject {
        val aircraft = AircraftTelemetryState.snapshot()
        return JSONObject()
            .put("observed_at_monotonic_ms", controlMonotonicMillis())
            .put("is_flying", aircraft.isFlying)
            .put("motors_on", aircraft.motorsOn)
            .put("flight_mode", aircraft.flightMode)
            .put("altitude_m", aircraft.location?.altitudeMeters ?: JSONObject.NULL)
            .put("flight_control_authority", aircraft.authority.owner)
            .put(
                "latest_takeoff_command",
                journal.latest("takeoff")?.let(::recordJson) ?: JSONObject.NULL
            )
            .put(
                "latest_land_command",
                journal.latest("land")?.let(::recordJson) ?: JSONObject.NULL
            )
            .put(
                "latest_confirm_landing_command",
                journal.latest("confirm_landing")?.let(::recordJson) ?: JSONObject.NULL
            )
            .put(
                "command_success_semantics",
                "DJI action callback succeeded; physical flight state must be verified here"
            )
    }

    fun readinessJson(): JSONObject = readiness().let {
        JSONObject()
            .put("assessed_at_monotonic_ms", it.assessedAtMonotonicMs)
            .put("ready_for_takeoff_command", it.readyForTakeoffCommand)
            .put("takeoff_endpoint_enforces_assessment", false)
            .put("authorizes_flight", false)
            .put(
                "minimum_takeoff_battery_percent",
                FlightTestReadinessEvaluator.MINIMUM_TAKEOFF_BATTERY_PERCENT
            )
            .put(
                "maximum_battery_percent_age_ms",
                FlightTestReadinessEvaluator.MAXIMUM_BATTERY_PERCENT_AGE_MS
            )
            .put("blockers", JSONArray(it.blockers))
            .put("advisories", JSONArray(it.advisories))
            .put("external_checks_required", JSONArray(listOf(
                "pilot_in_command", "props_and_people_clear", "airspace_and_remote_id",
                "weather", "visual_line_of_sight"
            )))
    }

    private fun errorJson(error: CommandError): JSONObject = JSONObject()
        .put("source", error.source)
        .put("type", error.type ?: JSONObject.NULL)
        .put("code", error.code ?: JSONObject.NULL)
        .put("inner_code", error.innerCode ?: JSONObject.NULL)
        .put("description", error.description ?: JSONObject.NULL)
        .put("hint", error.hint ?: JSONObject.NULL)
        .put("raw", error.raw ?: JSONObject.NULL)

    private fun preconditionsJson(value: CommandPreconditions): JSONObject = JSONObject()
        .put("captured_at_monotonic_ms", value.capturedAtMonotonicMs)
        .put("sdk_registered", value.sdkRegistered)
        .put("product_connected", value.productConnected)
        .put("remote_controller_connected", value.remoteControllerConnected)
        .put("airlink_connected", value.airLinkConnected)
        .put("aircraft_connected", value.aircraftConnected)
        .put("is_flying", value.isFlying)
        .put("motors_on", value.motorsOn)
        .put("flight_mode", value.flightMode)
        .put("altitude_m", value.altitudeMeters ?: JSONObject.NULL)
        .put("gps_signal_level", value.gpsSignalLevel)
        .put("gps_satellite_count", value.gpsSatelliteCount ?: JSONObject.NULL)
        .put("home_location_set", value.homeLocationSet)
        .put("flight_controller_failsafe", value.flightControllerFailsafe)
        .put("battery_connected", value.batteryConnected)
        .put("battery_percent", value.batteryPercent ?: JSONObject.NULL)
        .put("battery_percent_needed_to_go_home", value.batteryPercentNeededToGoHome ?: JSONObject.NULL)
        .put("battery_percent_needed_to_land", value.batteryPercentNeededToLand ?: JSONObject.NULL)
        .put("virtual_stick_enabled", value.virtualStickEnabled)
        .put("virtual_stick_advanced_mode", value.virtualStickAdvancedMode)
        .put("flight_control_authority", value.flightControlAuthority)
        .put("virtual_stick_control_mode", value.virtualStickControlMode)
        .put("control_failsafe_state", value.controlFailsafeState)
        .put("battery_percent_age_ms", value.batteryPercentAgeMs ?: JSONObject.NULL)
        .put("low_battery_warning", value.lowBatteryWarning ?: JSONObject.NULL)
        .put("serious_low_battery_warning", value.seriousLowBatteryWarning ?: JSONObject.NULL)
        .put("landing_confirmation_needed", value.landingConfirmationNeeded ?: JSONObject.NULL)
        .put("device_health_observed", value.deviceHealthObserved)
        .put("blocking_device_health_issues", JSONArray(value.blockingDeviceHealthIssues))
        .put("aircraft_connection_age_ms", value.aircraftConnectionAgeMs ?: JSONObject.NULL)
        .put("is_flying_age_ms", value.isFlyingAgeMs ?: JSONObject.NULL)
        .put("motors_on_age_ms", value.motorsOnAgeMs ?: JSONObject.NULL)
        .put(
            "flight_controller_failsafe_age_ms",
            value.flightControllerFailsafeAgeMs ?: JSONObject.NULL
        )
        .put("remote_controller_flight_mode", value.remoteControllerFlightMode)
        .put(
            "remote_controller_flight_mode_age_ms",
            value.remoteControllerFlightModeAgeMs ?: JSONObject.NULL
        )
        .put("remote_id_observed", value.remoteIdObserved)
        .put("remote_id_working_state", value.remoteIdWorkingState)
        .put(
            "flight_control_authority_age_ms",
            value.flightControlAuthorityAgeMs ?: JSONObject.NULL
        )
        .put("failsafe_action", value.failsafeAction)
        .put("failsafe_action_age_ms", value.failsafeActionAgeMs ?: JSONObject.NULL)

}
