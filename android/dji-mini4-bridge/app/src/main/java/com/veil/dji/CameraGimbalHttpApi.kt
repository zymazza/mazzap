package com.veil.dji

import dji.sdk.keyvalue.value.camera.CameraExposureCompensation
import dji.sdk.keyvalue.value.camera.CameraExposureMode
import dji.sdk.keyvalue.value.camera.CameraFocusMode
import dji.sdk.keyvalue.value.camera.CameraISO
import dji.sdk.keyvalue.value.camera.CameraMode
import dji.sdk.keyvalue.value.camera.CameraShutterSpeed
import dji.sdk.keyvalue.value.camera.CameraStorageInfos
import dji.sdk.keyvalue.value.camera.CameraStorageLocation
import dji.sdk.keyvalue.value.camera.CameraWhiteBalanceInfo
import dji.sdk.keyvalue.value.camera.CameraWhiteBalanceMode
import dji.sdk.keyvalue.value.camera.PhotoFileFormat
import dji.sdk.keyvalue.value.camera.PhotoRatio
import dji.sdk.keyvalue.value.camera.VideoFileFormat
import dji.sdk.keyvalue.value.camera.VideoFrameRate
import dji.sdk.keyvalue.value.camera.VideoResolution
import dji.sdk.keyvalue.value.camera.VideoResolutionFrameRate
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotation
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotationMode
import dji.sdk.keyvalue.value.gimbal.GimbalMode
import dji.sdk.keyvalue.value.gimbal.GimbalResetType
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/** HTTP parsing/journaling facade kept separate from the MSDK controller. */
class CameraGimbalHttpApi(
    private val controller: CameraGimbalController,
    private val journal: CommandJournal = BridgeCommandJournal.journal
) {
    fun statusJson(): JSONObject = controller.cachedStatus().toJson()

    fun capabilitiesJson(): JSONObject = controller.capabilityReport().toJson()

    fun submit(rawAction: String?, query: Map<String, String>): CommandRecord {
        val actionName = rawAction?.lowercase(Locale.US)
            ?: throw IllegalArgumentException("action is required")
        val parsedAction = CameraGimbalHttpAction.fromWire(actionName)
        val action = parsedAction.wireName
        // Parse all parameters before creating a journal record. A malformed
        // HTTP request therefore cannot leave a permanently pending command.
        CameraGimbalHttpContract.validate(action, query)
        val dispatch = buildDispatch(parsedAction, query)
        val record = journal.request(
            type = "camera_gimbal_$action",
            preconditions = BridgeCommandJournal.capturePreconditions(),
            details = mapOf(
                "action" to action,
                "success_evidence_policy" to
                    "readback_verified|physically_observed|callback_accepted_explicit"
            ) + query.filterKeys { it != "confirm" }
        )
        val observer = CameraGimbalMutationObserver { event ->
            if (!event.terminal) return@CameraGimbalMutationObserver
            when (event.stage) {
                CameraGimbalMutationStage.READBACK_VERIFIED,
                CameraGimbalMutationStage.PHYSICALLY_OBSERVED,
                CameraGimbalMutationStage.CALLBACK_ACCEPTED -> journal.succeed(
                    record.id,
                    mapOf(
                        "msdk_command_id" to event.commandId,
                        "evidence" to event.stage.name.lowercase(Locale.US),
                        "physically_observed" to
                            (event.stage == CameraGimbalMutationStage.PHYSICALLY_OBSERVED).toString(),
                        "requested_value" to (event.requestedValue ?: ""),
                        "observed_value" to (event.observedValue ?: ""),
                        "message" to (event.message ?: "")
                    )
                )
                CameraGimbalMutationStage.ACCEPTED_NOT_OBSERVED -> journal.fail(
                    record.id,
                    CommandError(
                        source = "observation",
                        type = "unknown_physical_outcome",
                        code = "camera_gimbal_physical_state_not_observed",
                        description = event.message,
                        hint = "Do not automatically replay an accepted action; inspect status first."
                    )
                )
                CameraGimbalMutationStage.REJECTED,
                CameraGimbalMutationStage.FAILED -> journal.fail(
                    record.id,
                    CommandError(
                        source = "camera_gimbal",
                        type = event.stage.name.lowercase(Locale.US),
                        code = "camera_gimbal_${event.stage.name.lowercase(Locale.US)}",
                        description = event.message,
                        raw = event.observedValue
                    )
                )
                CameraGimbalMutationStage.QUEUED -> Unit
            }
        }
        try {
            dispatch(observer)
        } catch (error: Throwable) {
            journal.fail(
                record.id,
                CommandError(
                    source = "bridge",
                    type = error.javaClass.name,
                    code = "camera_gimbal_dispatch_exception",
                    description = error.message,
                    raw = error.toString()
                )
            )
        }
        return journal.get(record.id) ?: record
    }

    /** Parse-only entry used to prove every advertised schema reaches dispatch. */
    internal fun validateRequest(rawAction: String, query: Map<String, String>) {
        val action = rawAction.lowercase(Locale.US)
        CameraGimbalHttpContract.validate(action, query)
        buildDispatch(CameraGimbalHttpAction.fromWire(action), query)
    }

    private fun buildDispatch(
        action: CameraGimbalHttpAction,
        query: Map<String, String>
    ): (CameraGimbalMutationObserver) -> String = when (action) {
        CameraGimbalHttpAction.SET_CAMERA_MODE -> enumValue<CameraMode>(query.required("value")).let { value ->
            command { observer -> controller.setCameraMode(value, observer) }
        }
        CameraGimbalHttpAction.TAKE_PHOTO -> command { observer -> controller.takePhoto(observer) }
        CameraGimbalHttpAction.SET_INTERVAL -> {
            val count = query.int("count")
            val seconds = query.double("seconds")
            command { observer -> controller.setPhotoIntervalSettings(count, seconds, observer) }
        }
        CameraGimbalHttpAction.START_INTERVAL -> command { observer -> controller.startIntervalPhotos(observer) }
        CameraGimbalHttpAction.STOP_INTERVAL -> command { observer -> controller.stopIntervalPhotos(observer) }
        CameraGimbalHttpAction.STOP_PHOTO -> command { observer -> controller.stopPhotoCapture(observer) }
        CameraGimbalHttpAction.START_RECORD -> command { observer -> controller.startRecording(observer) }
        CameraGimbalHttpAction.STOP_RECORD -> command { observer -> controller.stopRecording(observer) }
        CameraGimbalHttpAction.SET_ZOOM -> query.double("ratio").let { ratio ->
            command { observer -> controller.setZoomRatio(ratio, observer) }
        }
        CameraGimbalHttpAction.SET_FOCUS_MODE -> enumValue<CameraFocusMode>(query.required("value")).let { value ->
            command { observer -> controller.setFocusMode(value, observer) }
        }
        CameraGimbalHttpAction.SET_FOCUS_TARGET -> {
            val x = query.double("x")
            val y = query.double("y")
            command { observer -> controller.setFocusTarget(x, y, observer) }
        }
        CameraGimbalHttpAction.SET_EXPOSURE_MODE ->
            enumValue<CameraExposureMode>(query.required("value")).let { value ->
                command { observer -> controller.setExposureMode(value, observer) }
            }
        CameraGimbalHttpAction.SET_EXPOSURE_COMPENSATION ->
            enumValue<CameraExposureCompensation>(query.required("value")).let { value ->
                command { observer -> controller.setExposureCompensation(value, observer) }
            }
        CameraGimbalHttpAction.SET_ISO -> enumValue<CameraISO>(query.required("value")).let { value ->
            command { observer -> controller.setISO(value, observer) }
        }
        CameraGimbalHttpAction.SET_SHUTTER -> enumValue<CameraShutterSpeed>(query.required("value")).let { value ->
            command { observer -> controller.setShutterSpeed(value, observer) }
        }
        CameraGimbalHttpAction.SET_WHITE_BALANCE -> {
            val mode = enumValue<CameraWhiteBalanceMode>(query.required("mode"))
            val kelvin = query["kelvin"]?.toIntOrNull()
                ?: if (mode == CameraWhiteBalanceMode.MANUAL) {
                    throw IllegalArgumentException("manual white balance requires integer kelvin")
                } else {
                    0
                }
            val value = CameraWhiteBalanceInfo(mode, kelvin)
            command { observer -> controller.setWhiteBalance(value, observer) }
        }
        CameraGimbalHttpAction.SET_PHOTO_FORMAT -> enumValue<PhotoFileFormat>(query.required("value")).let { value ->
            command { observer -> controller.setPhotoFileFormat(value, observer) }
        }
        CameraGimbalHttpAction.SET_PHOTO_RATIO -> enumValue<PhotoRatio>(query.required("value")).let { value ->
            command { observer -> controller.setPhotoRatio(value, observer) }
        }
        CameraGimbalHttpAction.SET_VIDEO_FORMAT -> enumValue<VideoFileFormat>(query.required("value")).let { value ->
            command { observer -> controller.setVideoFileFormat(value, observer) }
        }
        CameraGimbalHttpAction.SET_VIDEO_SPEC -> {
            val value = VideoResolutionFrameRate(
                enumValue<VideoResolution>(query.required("resolution")),
                enumValue<VideoFrameRate>(query.required("frame_rate"))
            )
            command { observer -> controller.setVideoResolutionFrameRate(value, observer) }
        }
        CameraGimbalHttpAction.FORMAT_STORAGE -> {
            val location = enumValue<CameraStorageLocation>(query.required("storage"))
            val confirmation = query.required("confirm")
            command { observer -> controller.formatStorage(location, confirmation, observer) }
        }
        CameraGimbalHttpAction.SET_GIMBAL_MODE -> enumValue<GimbalMode>(query.required("value")).let { value ->
            command { observer -> controller.setGimbalMode(value, observer) }
        }
        CameraGimbalHttpAction.GIMBAL_ANGLE -> {
            val request = GimbalAngleRotation(
                enumValue<GimbalAngleRotationMode>(query.required("mode")),
                query.double("pitch"),
                query.double("roll"),
                query.double("yaw"),
                query.boolean("pitch_ignored", false),
                query.boolean("roll_ignored", false),
                query.boolean("yaw_ignored", false),
                query.double("duration_seconds"),
                false,
                0
            )
            command { observer -> controller.rotateGimbalByAngle(request, observer) }
        }
        CameraGimbalHttpAction.GIMBAL_SPEED -> {
            val pitch = query.double("pitch_deg_s")
            val yaw = query.double("yaw_deg_s")
            val rawLeaseMs = query["lease_ms"]
            val requestedLeaseMs = rawLeaseMs?.toLongOrNull()
                ?: if (rawLeaseMs == null) GimbalSpeedLeasePolicy.DEFAULT_LEASE_MS
                else throw IllegalArgumentException("lease_ms must be an integer")
            GimbalSpeedLeasePolicy.normalizedLeaseMs(pitch, yaw, requestedLeaseMs)
            command { observer ->
                controller.rotateGimbalBySpeed(pitch, yaw, requestedLeaseMs, observer)
            }
        }
        CameraGimbalHttpAction.GIMBAL_RESET -> enumValue<GimbalResetType>(query.required("value")).let { value ->
            command { observer -> controller.resetGimbal(value, observer) }
        }
        CameraGimbalHttpAction.VERTICAL_SHOT -> query.boolean("enabled").let { enabled ->
            command { observer -> controller.setVerticalShotEnabled(enabled, observer) }
        }
    }

    private fun command(
        dispatch: (CameraGimbalMutationObserver) -> String
    ): (CameraGimbalMutationObserver) -> String = dispatch

    private inline fun <reified T : Enum<T>> enumValue(raw: String): T = try {
        enumValueOf<T>(raw.uppercase(Locale.US)).also {
            require(it.name != "UNKNOWN") { "UNKNOWN is not a settable value" }
        }
    } catch (error: IllegalArgumentException) {
        throw IllegalArgumentException("invalid ${T::class.java.simpleName}: $raw", error)
    }

    private fun Map<String, String>.required(name: String): String =
        get(name)?.takeIf { it.isNotBlank() }
            ?: throw IllegalArgumentException("missing value: $name")

    private fun Map<String, String>.int(name: String): Int =
        get(name)?.toIntOrNull() ?: throw IllegalArgumentException("missing integer: $name")

    private fun Map<String, String>.double(name: String): Double =
        get(name)?.toDoubleOrNull()?.takeIf { it.isFinite() }
            ?: throw IllegalArgumentException("missing finite number: $name")

    private fun Map<String, String>.boolean(name: String, default: Boolean? = null): Boolean {
        val raw = get(name) ?: return default
            ?: throw IllegalArgumentException("missing boolean: $name")
        return when (raw.lowercase(Locale.US)) {
            "true", "1" -> true
            "false", "0" -> false
            else -> throw IllegalArgumentException("$name must be true or false")
        }
    }
}

private fun Mini4CameraGimbalStatus.toJson(): JSONObject = JSONObject()
    .put("captured_at_ms", capturedAtMs)
    .put("source", "KeyManager latest values")
    .put("camera", JSONObject()
        .put("connected", cameraConnected ?: JSONObject.NULL)
        .put("mode", cameraMode?.name ?: JSONObject.NULL)
        .put("mode_range", JSONArray(cameraModeRange?.map { it.name } ?: emptyList<String>()))
        .put("is_shooting_photo", isShootingPhoto ?: JSONObject.NULL)
        .put("is_shooting_interval", isShootingIntervalPhotos ?: JSONObject.NULL)
        .put("interval_countdown_s", intervalCountdownSeconds ?: JSONObject.NULL)
        .put("is_recording", isRecording ?: JSONObject.NULL)
        .put("recording_time_s", recordingTimeSeconds ?: JSONObject.NULL)
        .put("zoom_ratio", zoomRatio ?: JSONObject.NULL)
        .put("zoom_focal_length_mm", zoomFocalLengthMillimeters ?: JSONObject.NULL)
        .put("zoom_range", zoomRange?.let {
            JSONObject()
                .put("continuous", it.isContinuous)
                .put("gears", JSONArray(it.gears.toList()))
        } ?: JSONObject.NULL)
        .put("focus_mode", focusMode?.name ?: JSONObject.NULL)
        .put("focus_target", focusTarget?.let {
            JSONObject().put("x", it.x).put("y", it.y)
        } ?: JSONObject.NULL)
        .put("exposure_mode", exposureMode?.name ?: JSONObject.NULL)
        .put("exposure_compensation", exposureCompensation?.name ?: JSONObject.NULL)
        .put("iso", iso?.name ?: JSONObject.NULL)
        .put("shutter_speed", shutterSpeed?.name ?: JSONObject.NULL)
        .put("white_balance", whiteBalance?.let {
            JSONObject()
                .put("mode", it.whiteBalanceMode.name)
                .put("color_temperature_k", it.colorTemperature)
        } ?: JSONObject.NULL)
        .put("photo_file_format", photoFileFormat?.name ?: JSONObject.NULL)
        .put("photo_ratio", photoRatio?.name ?: JSONObject.NULL)
        .put("video_file_format", videoFileFormat?.name ?: JSONObject.NULL)
        .put("video_resolution_frame_rate", videoResolutionFrameRate?.let {
            JSONObject()
                .put("resolution", it.resolution.name)
                .put("frame_rate", it.frameRate.name)
        } ?: JSONObject.NULL)
        .put("storage", storage?.toJson() ?: JSONObject.NULL))
    .put("gimbal", JSONObject()
        .put("connected", gimbalConnected ?: JSONObject.NULL)
        .put("mode", gimbalMode?.name ?: JSONObject.NULL)
        .put("vertical_shot_enabled", gimbalVerticalShotEnabled ?: JSONObject.NULL)
        .put("attitude_deg", gimbalAttitude?.let {
            JSONObject().put("pitch", it.pitch).put("roll", it.roll).put("yaw", it.yaw)
        } ?: JSONObject.NULL))

private fun CameraStorageInfos.toJson(): JSONObject = JSONObject()
    .put("current_storage", currentStorageType.name)
    .put("devices", JSONArray(cameraStorageInfoList.map { info ->
        JSONObject()
            .put("type", info.storageType.name)
            .put("state", info.storageState.name)
            .put("capacity_mb", info.storageCapacity)
            .put("remaining_mb", info.storageLeftCapacity)
            .put("available_photo_count", info.availablePhotoCount)
            .put("available_video_duration_s", info.availableVideoDuration)
    }))

private fun CameraGimbalCapabilityReport.toJson(): JSONObject = JSONObject()
    .put("product", product)
    .put("source", "packaged MSDK 5.18 Mini 4 Pro capability manifests")
    .put("public_camera_keys", JSONArray(publicCameraKeys.sorted()))
    .put("public_gimbal_keys", JSONArray(publicGimbalKeys.sorted()))
    .put("operations", JSONArray(operations.map { operation ->
        JSONObject()
            .put("operation", operation.operation)
            .put("manifest_keys", JSONArray(operation.manifestKeys.sorted()))
            .put("validation", operation.validation)
            .put("success_evidence", operation.successEvidence)
    }))
    .put("deliberately_unavailable", JSONObject(deliberatelyUnavailable))
    .put("accepted_actions", JSONArray(CameraGimbalHttpContract.actions.map { action ->
        JSONObject()
            .put("action", action.action)
            .put("required_parameters", JSONArray(action.requiredParameters.sorted()))
            .put("optional_parameters", JSONArray(action.optionalParameters.sorted()))
            .put("parameters", JSONArray(action.parameters.map { parameter ->
                JSONObject()
                    .put("name", parameter.name)
                    .put("type", parameter.type)
                    .put("required", parameter.required)
                    .put("description", parameter.description)
            }))
            .put("example_query", JSONObject(action.exampleQuery))
    }))
    .put("command_endpoint", "/camera-gimbal/command")
    .put("command_method", "POST")
    .put("status_endpoint", "/camera-gimbal/status")
