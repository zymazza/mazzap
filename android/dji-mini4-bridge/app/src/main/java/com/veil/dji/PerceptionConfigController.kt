package com.veil.dji

import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.aircraft.perception.PerceptionManager
import dji.v5.manager.aircraft.perception.data.ObstacleAvoidanceType
import dji.v5.manager.interfaces.IPerceptionManager
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

enum class PerceptionConfigSetting(val wireName: String, val valueType: String) {
    AVOIDANCE_TYPE("avoidance_type", "BRAKE|BYPASS|CLOSE");

    companion object {
        fun fromWire(value: String?): PerceptionConfigSetting = entries.firstOrNull {
            it.wireName == value?.lowercase(Locale.US)
        } ?: throw IllegalArgumentException("setting must be avoidance_type")
    }
}

data class PerceptionConfigRequest(
    val setting: PerceptionConfigSetting,
    /** Canonical value used for command details and readback comparison. */
    val value: String
) {
    companion object {
        fun parse(setting: String?, rawValue: String?): PerceptionConfigRequest {
            val parsedSetting = PerceptionConfigSetting.fromWire(setting)
            val canonical = rawValue?.uppercase(Locale.US)
                ?: throw IllegalArgumentException("value is required")
            require(canonical in setOf("BRAKE", "BYPASS", "CLOSE")) {
                "value must be BRAKE, BYPASS, or CLOSE"
            }
            return PerceptionConfigRequest(parsedSetting, canonical)
        }
    }
}

/**
 * Mini 4 Pro's packaged MSDK 5.18 perception delegate implements only avoidance-type writes.
 * A request waits for the DJI set callback, then performs a direct get readback before succeeding.
 * This does not claim that DJI retains braking or bypass while Virtual Stick owns authority.
 */
class PerceptionConfigController(
    private val managerProvider: () -> IPerceptionManager = { PerceptionManager.getInstance() },
    private val journal: CommandJournal = BridgeCommandJournal.journal
) {
    private val timeoutExecutor = Executors.newSingleThreadScheduledExecutor()
    private val pendingGate = SingleFlightCommandGate()
    // PerceptionManager consults DJI's process context. Do not construct the
    // singleton with BridgeRuntime, before SDKManager.init() has run.
    private val manager: IPerceptionManager by lazy(managerProvider)

    fun set(setting: String?, rawValue: String?): CommandRecord {
        val request = PerceptionConfigRequest.parse(setting, rawValue)
        val created = journal.request(
            type = COMMAND_TYPE,
            preconditions = BridgeCommandJournal.capturePreconditions(),
            details = mapOf(
                "setting" to request.setting.wireName,
                "requested_value" to request.value,
                "virtual_stick_retention" to "unverified"
            )
        )
        pendingGate.current()?.takeIf {
            journal.get(it)?.state != CommandState.REQUESTED
        }?.let(pendingGate::release)
        val acquired = pendingGate.tryAcquire(created.id)
        val activeId = if (acquired) null else pendingGate.current()
        val command = if (!acquired) {
            val conflictId = activeId ?: "unknown"
            journal.fail(created.id, CommandError(
                source = "bridge",
                type = "conflict",
                code = "perception_config_in_progress",
                description = "Perception config command $conflictId is still pending"
            )) ?: created
        } else {
            created
        }
        if (!acquired) return command
        try {
            timeoutExecutor.schedule(
                { timeout(command.id, request) },
                CALLBACK_TIMEOUT_SECONDS,
                TimeUnit.SECONDS
            )
            manager.setObstacleAvoidanceType(
                ObstacleAvoidanceType.valueOf(request.value),
                object : CommonCallbacks.CompletionCallback {
                    override fun onSuccess() {
                        readBack(command.id, request)
                    }

                    override fun onFailure(error: IDJIError) {
                        failDji(command.id, request, "set", error)
                    }
                }
            )
        } catch (error: Exception) {
            val failure = CommandError(
                source = "bridge",
                type = error.javaClass.name,
                code = "perception_config_dispatch_exception",
                description = error.message,
                raw = error.toString()
            )
            val result = journal.fail(command.id, failure)
            releasePending(command.id)
            if (result?.state == CommandState.FAILED && result.error == failure) {
                BridgeState.lastEvent.set(
                    "perception_config_dispatch_failed:${error.javaClass.simpleName}"
                )
            }
        }
        return journal.get(command.id) ?: command
    }

    fun statusJson(): JSONObject = JSONObject()
        .put("current", AircraftTelemetryState.toJson().getJSONObject("perception"))
        .put("set_endpoint", "/perception/config")
        .put("set_method", "POST")
        .put("confirmation", "confirm=SET_PERCEPTION_CONFIG")
        .put("one_setting_per_request", true)
        .put("settings", JSONArray(PerceptionConfigSetting.entries.map {
            JSONObject().put("setting", it.wireName).put("value_type", it.valueType)
        }))
        .put("read_only_reported_fields", JSONArray(listOf(
            "directional_working_flags",
            "directional_enabled_flags",
            "warning_distances",
            "braking_distances",
            "vision_positioning_enabled",
            "precision_landing_enabled"
        )))
        .put(
            "latest_set_command",
            journal.latest(COMMAND_TYPE)?.let(BridgeCommandJournal::recordJson) ?: JSONObject.NULL
        )
        .put("set_success_requires_direct_get_readback", true)
        .put("raw_obstacle_camera_imagery_exposed", false)
        .put("virtual_stick_retention_verified", false)

    fun close() {
        timeoutExecutor.shutdownNow()
    }

    private fun readBack(commandId: String, request: PerceptionConfigRequest) {
        manager.getObstacleAvoidanceType(
            object : CommonCallbacks.CompletionCallbackWithParam<ObstacleAvoidanceType> {
                override fun onSuccess(value: ObstacleAvoidanceType) {
                    finishReadback(commandId, request, value.name)
                }

                override fun onFailure(error: IDJIError) {
                    failDji(commandId, request, "readback", error)
                }
            }
        )
    }

    private fun finishReadback(
        commandId: String,
        request: PerceptionConfigRequest,
        readbackValue: String
    ) {
        if (!request.value.equals(readbackValue, ignoreCase = true)) {
            val failure = CommandError(
                source = "dji",
                type = "readback_mismatch",
                code = "perception_config_readback_mismatch",
                description = "DJI readback '$readbackValue' does not match requested '${request.value}'"
            )
            val result = journal.fail(commandId, failure)
            releasePending(commandId)
            if (result?.state == CommandState.FAILED && result.error == failure) {
                BridgeState.lastEvent.set("perception_config_readback_mismatch:avoidance_type")
            }
            return
        }
        val result = journal.succeed(commandId, mapOf(
            "setting" to request.setting.wireName,
            "requested_value" to request.value,
            "readback_value" to readbackValue,
            "readback_verified" to "true",
            "callback" to "dji_set_and_get_succeeded",
            "virtual_stick_retention" to "unverified"
        ))
        releasePending(commandId)
        if (result?.state == CommandState.SUCCEEDED) {
            BridgeState.lastEvent.set("perception_config_verified:avoidance_type:$readbackValue")
        }
    }

    private fun failDji(
        commandId: String,
        request: PerceptionConfigRequest,
        stage: String,
        error: IDJIError
    ) {
        val failure = CommandError(
            source = "dji",
            type = errorField { error.errorType() },
            code = errorField { error.errorCode() },
            innerCode = errorField { error.innerCode() },
            description = listOfNotNull(
                "perception config $stage failed for ${request.setting.wireName}",
                errorField { error.description() }
            ).joinToString(": "),
            hint = errorField { error.hint() },
            raw = error.toString()
        )
        val result = journal.fail(commandId, failure)
        releasePending(commandId)
        if (result?.state == CommandState.FAILED && result.error == failure) {
            BridgeState.lastEvent.set(
                "perception_config_${stage}_failed:${request.setting.wireName}:$error"
            )
        }
    }

    private fun timeout(commandId: String, request: PerceptionConfigRequest) {
        if (journal.get(commandId)?.state != CommandState.REQUESTED) return
        val failure = CommandError(
            source = "bridge",
            type = "timeout",
            code = "perception_config_callback_timeout",
            description = "DJI set/readback did not finish within $CALLBACK_TIMEOUT_SECONDS seconds"
        )
        val result = journal.fail(commandId, failure)
        releasePending(commandId)
        if (result?.state == CommandState.FAILED && result.error == failure) {
            BridgeState.lastEvent.set("perception_config_timeout:${request.setting.wireName}")
        }
    }

    private fun errorField(value: () -> Any?): String? =
        runCatching { value()?.toString() }.getOrNull()

    private fun releasePending(commandId: String) {
        pendingGate.release(commandId)
    }

    companion object {
        const val COMMAND_TYPE = "perception_config_set"
        private const val CALLBACK_TIMEOUT_SECONDS = 8L
    }
}
