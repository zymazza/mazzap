package com.veil.dji

import java.util.ArrayDeque
import java.util.Locale
import java.util.concurrent.Executor
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.abs

enum class CameraGimbalMutationStage {
    QUEUED,
    CALLBACK_ACCEPTED,
    READBACK_VERIFIED,
    PHYSICALLY_OBSERVED,
    ACCEPTED_NOT_OBSERVED,
    REJECTED,
    FAILED
}

/**
 * A state-changing camera/gimbal command may produce more than one event.  In
 * particular, CALLBACK_ACCEPTED is never presented as proof that a mechanism
 * moved or that a value was retained by the aircraft.
 */
data class CameraGimbalMutationEvent(
    val commandId: String,
    val operation: String,
    val stage: CameraGimbalMutationStage,
    val terminal: Boolean,
    val message: String? = null,
    val requestedValue: String? = null,
    val observedValue: String? = null,
    val occurredAtMs: Long = System.currentTimeMillis()
)

fun interface CameraGimbalMutationObserver {
    fun onEvent(event: CameraGimbalMutationEvent)
}

data class CameraGimbalCapabilityEntry(
    val operation: String,
    val manifestKeys: Set<String>,
    val validation: String,
    val successEvidence: String
)

data class CameraGimbalCapabilityReport(
    val product: String,
    val publicCameraKeys: Set<String>,
    val publicGimbalKeys: Set<String>,
    val operations: List<CameraGimbalCapabilityEntry>,
    val deliberatelyUnavailable: Map<String, String>
)

data class CameraGimbalHttpParameter(
    val name: String,
    val type: String,
    val required: Boolean,
    val description: String
)

data class CameraGimbalHttpActionSchema(
    val action: String,
    val parameters: List<CameraGimbalHttpParameter>,
    /** Complete parse-only request used by contract tests; excludes action=. */
    val exampleQuery: Map<String, String>
) {
    val requiredParameters: Set<String> = parameters.filter { it.required }.map { it.name }.toSet()
    val optionalParameters: Set<String> = parameters.filterNot { it.required }.map { it.name }.toSet()
}

enum class CameraGimbalHttpAction(val wireName: String) {
    SET_CAMERA_MODE("set_camera_mode"), TAKE_PHOTO("take_photo"),
    SET_INTERVAL("set_interval"), START_INTERVAL("start_interval"),
    STOP_INTERVAL("stop_interval"), STOP_PHOTO("stop_photo"),
    START_RECORD("start_record"), STOP_RECORD("stop_record"), SET_ZOOM("set_zoom"),
    SET_FOCUS_MODE("set_focus_mode"), SET_FOCUS_TARGET("set_focus_target"),
    SET_EXPOSURE_MODE("set_exposure_mode"),
    SET_EXPOSURE_COMPENSATION("set_exposure_compensation"), SET_ISO("set_iso"),
    SET_SHUTTER("set_shutter"), SET_WHITE_BALANCE("set_white_balance"),
    SET_PHOTO_FORMAT("set_photo_format"), SET_PHOTO_RATIO("set_photo_ratio"),
    SET_VIDEO_FORMAT("set_video_format"), SET_VIDEO_SPEC("set_video_spec"),
    FORMAT_STORAGE("format_storage"), SET_GIMBAL_MODE("set_gimbal_mode"),
    GIMBAL_ANGLE("gimbal_angle"), GIMBAL_SPEED("gimbal_speed"),
    GIMBAL_RESET("gimbal_reset"), VERTICAL_SHOT("vertical_shot");

    companion object {
        fun fromWire(raw: String): CameraGimbalHttpAction = entries.firstOrNull {
            it.wireName == raw
        } ?: throw IllegalArgumentException("unknown camera/gimbal action: $raw")
    }
}

/**
 * Exact POST /camera-gimbal/command contract. CameraGimbalHttpApi validates
 * against this registry before dispatch, and /capabilities serializes it.
 */
object CameraGimbalHttpContract {
    private fun required(name: String, type: String, description: String) =
        CameraGimbalHttpParameter(name, type, true, description)

    private fun optional(name: String, type: String, description: String) =
        CameraGimbalHttpParameter(name, type, false, description)

    val actions: List<CameraGimbalHttpActionSchema> = listOf(
        schema("set_camera_mode", mapOf("value" to "VIDEO_NORMAL"),
            required("value", "dji_enum", "CameraMode; checked against live KeyCameraModeRange")),
        schema("take_photo"),
        schema("set_interval", mapOf("count" to "5", "seconds" to "2.0"),
            required("count", "integer", "positive photo count"),
            required("seconds", "finite_double", "positive interval seconds")),
        schema("start_interval"),
        schema("stop_interval"),
        schema("stop_photo"),
        schema("start_record"),
        schema("stop_record"),
        schema("set_zoom", mapOf("ratio" to "2.0"),
            required("ratio", "finite_double", "checked against live zoom range/gears")),
        schema("set_focus_mode", mapOf("value" to "AF"),
            required("value", "dji_enum", "CameraFocusMode; checked against live range")),
        schema("set_focus_target", mapOf("x" to "0.5", "y" to "0.5"),
            required("x", "finite_double", "normalized horizontal coordinate 0..1"),
            required("y", "finite_double", "normalized vertical coordinate 0..1")),
        schema("set_exposure_mode", mapOf("value" to "PROGRAM"),
            required("value", "dji_enum", "CameraExposureMode; checked against live range")),
        schema("set_exposure_compensation", mapOf("value" to "NEG_0EV"),
            required("value", "dji_enum", "CameraExposureCompensation; checked against live range")),
        schema("set_iso", mapOf("value" to "ISO_AUTO"),
            required("value", "dji_enum", "CameraISO; checked against live range")),
        schema("set_shutter", mapOf("value" to "SHUTTER_SPEED_AUTO"),
            required("value", "dji_enum", "CameraShutterSpeed; checked against live range")),
        schema("set_white_balance", mapOf("mode" to "MANUAL", "kelvin" to "5600"),
            required("mode", "dji_enum", "CameraWhiteBalanceMode; checked against live range"),
            optional("kelvin", "integer", "required when mode=MANUAL; 2000..10000 K")),
        schema("set_photo_format", mapOf("value" to "JPEG"),
            required("value", "dji_enum", "PhotoFileFormat; checked against live range")),
        schema("set_photo_ratio", mapOf("value" to "RATIO_4COLON3"),
            required("value", "dji_enum", "PhotoRatio; checked against live range")),
        schema("set_video_format", mapOf("value" to "MP4"),
            required("value", "dji_enum", "VideoFileFormat; checked against live range")),
        schema("set_video_spec", mapOf(
            "resolution" to "RESOLUTION_3840x2160",
            "frame_rate" to "RATE_30FPS"
        ),
            required("resolution", "dji_enum", "VideoResolution component of a live-supported pair"),
            required("frame_rate", "dji_enum", "VideoFrameRate component of a live-supported pair")),
        schema("format_storage", mapOf(
            "storage" to "SDCARD",
            "confirm" to "FORMAT_STORAGE:SDCARD"
        ),
            required("storage", "dji_enum", "CameraStorageLocation"),
            required("confirm", "confirmation_token", "must exactly equal FORMAT_STORAGE:<storage>")),
        schema("set_gimbal_mode", mapOf("value" to "YAW_FOLLOW"),
            required("value", "dji_enum", "GimbalMode declared by Mini 4 Pro manifest")),
        schema("gimbal_angle", mapOf(
            "mode" to "ABSOLUTE_ANGLE",
            "pitch" to "-45.0",
            "roll" to "0.0",
            "yaw" to "0.0",
            "pitch_ignored" to "false",
            "roll_ignored" to "true",
            "yaw_ignored" to "true",
            "duration_seconds" to "1.0"
        ),
            required("mode", "dji_enum", "ABSOLUTE_ANGLE or RELATIVE_ANGLE"),
            required("pitch", "finite_double", "pitch degrees"),
            required("roll", "finite_double", "roll degrees"),
            required("yaw", "finite_double", "yaw degrees"),
            optional("pitch_ignored", "boolean", "defaults false"),
            optional("roll_ignored", "boolean", "defaults false"),
            optional("yaw_ignored", "boolean", "defaults false"),
            required("duration_seconds", "finite_double", "0..60 seconds")),
        schema("gimbal_speed", mapOf(
            "pitch_deg_s" to "0.0",
            "yaw_deg_s" to "0.0",
            "lease_ms" to "250"
        ),
            required("pitch_deg_s", "finite_double", "checked against live pitch maximum"),
            required("yaw_deg_s", "finite_double", "checked against live yaw maximum"),
            optional("lease_ms", "integer", "nonzero lease; default 250 ms, range 50..1000 ms")),
        schema("gimbal_reset", mapOf("value" to "RECENTER"),
            required("value", "dji_enum", "RECENTER or SELFIE")),
        schema("vertical_shot", mapOf("enabled" to "true"),
            required("enabled", "boolean", "vertical-shot enable state"))
    )

    private val byAction = actions.associateBy { it.action }

    init {
        require(byAction.size == actions.size) { "duplicate camera/gimbal action contract" }
        actions.forEach { action ->
            require(action.parameters.map { it.name }.toSet().size == action.parameters.size) {
                "duplicate parameter in ${action.action}"
            }
            require(action.exampleQuery.keys == action.parameters.map { it.name }.toSet()) {
                "${action.action} example must include every required and optional parameter"
            }
        }
        require(actions.map { it.action }.toSet() ==
            CameraGimbalHttpAction.entries.map { it.wireName }.toSet()) {
            "camera/gimbal schema and dispatcher action enum differ"
        }
    }

    fun validate(rawAction: String, query: Map<String, String>): CameraGimbalHttpActionSchema {
        val action = byAction[rawAction]
            ?: throw IllegalArgumentException("unknown camera/gimbal action: $rawAction")
        val supplied = query.keys - "action"
        val allowed = action.parameters.map { it.name }.toSet()
        val unknown = supplied - allowed
        require(unknown.isEmpty()) {
            "unknown parameter(s) for $rawAction: ${unknown.sorted().joinToString()}"
        }
        val missing = action.requiredParameters.filter { query[it].isNullOrBlank() }
        require(missing.isEmpty()) {
            "missing required parameter(s) for $rawAction: ${missing.sorted().joinToString()}"
        }
        return action
    }

    private fun schema(
        action: String,
        example: Map<String, String> = emptyMap(),
        vararg parameters: CameraGimbalHttpParameter
    ) = CameraGimbalHttpActionSchema(action, parameters.toList(), example)
}

object GimbalSpeedLeasePolicy {
    const val DEFAULT_LEASE_MS = 250L
    const val MIN_LEASE_MS = 50L
    const val MAX_LEASE_MS = 1_000L

    fun normalizedLeaseMs(pitch: Double, yaw: Double, requested: Long?): Long {
        require(pitch.isFinite() && yaw.isFinite()) { "gimbal speeds must be finite" }
        if (pitch == 0.0 && yaw == 0.0) return 0L
        val lease = requested ?: DEFAULT_LEASE_MS
        require(lease in MIN_LEASE_MS..MAX_LEASE_MS) {
            "nonzero gimbal speed lease_ms must be $MIN_LEASE_MS..$MAX_LEASE_MS"
        }
        return lease
    }
}

/** Camera/gimbal mutations require the complete live RC-to-aircraft path. */
object CameraGimbalConnectionPolicy {
    fun isUsableAircraftLink(
        aircraftConnected: Boolean,
        remoteControllerConnected: Boolean,
        airLinkConnected: Boolean
    ): Boolean = aircraftConnected && remoteControllerConnected && airLinkConnected
}

/** Link epoch gate: disconnected submissions can never become valid on reconnect. */
class CameraGimbalLinkGate(initiallyConnected: Boolean = false) {
    private var generation = 0L
    private var connected = initiallyConnected

    @Synchronized
    fun connect(): Long {
        if (!connected) generation += 1L
        connected = true
        return generation
    }

    @Synchronized
    fun disconnect(): Long {
        connected = false
        generation += 1L
        return generation
    }

    @Synchronized
    fun ticket(): Long = generation

    @Synchronized
    fun isValid(ticket: Long): Boolean = connected && generation == ticket

    @Synchronized
    fun isConnected(): Boolean = connected
}

/** Public MSDK 5.18 surface declared by the packaged Mini 4 Pro manifests. */
object Mini4CameraGimbalPolicy {
    const val FORMAT_CONFIRM_PREFIX = "FORMAT_STORAGE:"

    val cameraKeys = setOf(
        "KeyConnection",
        "KeyCameraModeRange",
        "KeyCameraMode",
        "KeyIsShootingPhoto",
        "KeyStartShootPhoto",
        "KeyStopShootPhoto",
        "KeyPhotoFileFormatRange",
        "KeyPhotoFileFormat",
        "KeyPhotoIntervalShootSettings",
        "KeyPhotoIntervalCountdown",
        "KeyIsRecording",
        "KeyStartRecord",
        "KeyStopRecord",
        "KeyRecordingTime",
        "KeyVideoFileFormatRange",
        "KeyVideoFileFormat",
        "KeyNewlyGeneratedMediaFile",
        "KeyCameraStorageInfos",
        "KeyExposureModeRange",
        "KeyExposureMode",
        "KeyExposureCompensationRange",
        "KeyExposureCompensation",
        "KeyISORange",
        "KeyISO",
        "KeyShutterSpeedRange",
        "KeyShutterSpeed",
        "KeyPhotoRatioRange",
        "KeyPhotoRatio",
        "KeyVideoResolutionFrameRateRange",
        "KeyVideoResolutionFrameRate",
        "KeyCameraZoomRatiosRange",
        "KeyCameraZoomRatios",
        "KeyCameraZoomFocalLength",
        "KeyCameraFocusMode",
        "KeyCameraFocusModeRange",
        "KeyCameraFocusTarget",
        "KeyCameraWhiteBalanceRange",
        "KeyWhiteBalance",
        "KeyFormatStorage",
        "KeyIsShootingIntervalPhotos"
    )

    val gimbalKeys = setOf(
        "KeyConnection",
        "KeyGimbalAttitude",
        "KeyGimbalMode",
        "KeyRotateByAngle",
        "KeyRotateBySpeed",
        "KeyGimbalReset",
        "KeyPitchControlMaxSpeed",
        "KeyYawControlMaxSpeed",
        "KeyGimbalVerticalShotEnabled"
    )

    fun capabilityReport(): CameraGimbalCapabilityReport = CameraGimbalCapabilityReport(
        product = "DJI_MINI_4_PRO",
        publicCameraKeys = cameraKeys,
        publicGimbalKeys = gimbalKeys,
        operations = listOf(
            operation("camera_mode", setOf("KeyCameraModeRange", "KeyCameraMode"), "live range", "direct readback"),
            operation("take_photo", setOf("KeyStartShootPhoto", "KeyIsShootingPhoto"), "mode/state", "physical state when observable"),
            operation("interval_photo", setOf("KeyPhotoIntervalShootSettings", "KeyStartShootPhoto", "KeyStopShootPhoto", "KeyIsShootingIntervalPhotos"), "finite positive settings", "physical state"),
            operation("record", setOf("KeyStartRecord", "KeyStopRecord", "KeyIsRecording"), "mode/state", "physical state"),
            operation("zoom_ratio", setOf("KeyCameraZoomRatiosRange", "KeyCameraZoomRatios"), "live range", "direct readback"),
            operation("focus", setOf("KeyCameraFocusModeRange", "KeyCameraFocusMode", "KeyCameraFocusTarget"), "live mode range + normalized target", "direct readback"),
            operation("exposure", setOf("KeyExposureModeRange", "KeyExposureMode", "KeyExposureCompensationRange", "KeyExposureCompensation", "KeyISORange", "KeyISO", "KeyShutterSpeedRange", "KeyShutterSpeed"), "live range", "direct readback"),
            operation("white_balance", setOf("KeyCameraWhiteBalanceRange", "KeyWhiteBalance"), "live mode range + temperature guard", "direct readback"),
            operation("photo_video_format", setOf("KeyPhotoFileFormatRange", "KeyPhotoFileFormat", "KeyPhotoRatioRange", "KeyPhotoRatio", "KeyVideoFileFormatRange", "KeyVideoFileFormat", "KeyVideoResolutionFrameRateRange", "KeyVideoResolutionFrameRate"), "live range", "direct readback"),
            operation("storage", setOf("KeyCameraStorageInfos", "KeyFormatStorage"), "explicit destructive confirmation", "callback accepted; storage status reported separately"),
            operation("gimbal_mode", setOf("KeyGimbalMode"), "manifest enum", "direct readback"),
            operation("gimbal_angle", setOf("KeyGimbalAttitude", "KeyRotateByAngle"), "finite angle request", "attitude observation"),
            operation("gimbal_speed", setOf("KeyPitchControlMaxSpeed", "KeyYawControlMaxSpeed", "KeyRotateBySpeed"), "live max speed", "callback accepted; continuous motion not held up for observation"),
            operation("gimbal_reset", setOf("KeyGimbalReset", "KeyGimbalAttitude"), "manifest reset variants", "attitude observation when determinable"),
            operation("vertical_shot", setOf("KeyGimbalVerticalShotEnabled"), "boolean", "direct readback")
        ),
        deliberatelyUnavailable = linkedMapOf(
            "obstacle_camera_video" to "not exposed by the Mini 4 Pro MSDK manifest",
            "depth_map" to "not exposed by the Mini 4 Pro MSDK manifest",
            "raw_imu" to "not exposed by the Mini 4 Pro MSDK manifest",
            "native_dji_fly_waypoint_library" to "not exposed by public MSDK 5.18",
            "photo_pixel_resolution" to "no Mini 4 Pro photo-size/resolution key; photo ratio and file format are exposed"
        )
    )

    fun <T> requireRuntimeChoice(name: String, requested: T, available: Collection<T>) {
        require(available.isNotEmpty()) { "$name runtime range is empty" }
        require(requested in available) {
            "$name '$requested' is not in the aircraft runtime range: ${available.joinToString()}"
        }
    }

    fun requireZoomRatio(ratio: Double, continuous: Boolean, gears: IntArray) {
        require(ratio.isFinite() && ratio > 0.0) { "zoom ratio must be finite and positive" }
        require(gears.isNotEmpty()) { "aircraft returned an empty zoom ratio range" }
        val values = gears.map(Int::toDouble)
        if (continuous) {
            require(ratio >= values.min() && ratio <= values.max()) {
                "zoom ratio $ratio is outside runtime range ${values.min()}..${values.max()}"
            }
        } else {
            require(values.any { abs(it - ratio) <= 1e-6 }) {
                "zoom ratio $ratio is not one of the runtime gears: ${values.joinToString()}"
            }
        }
    }

    fun requireFocusTarget(x: Double, y: Double) {
        require(x.isFinite() && y.isFinite()) { "focus target coordinates must be finite" }
        require(x in 0.0..1.0 && y in 0.0..1.0) {
            "focus target coordinates must both be normalized to 0..1"
        }
    }

    fun requirePhotoInterval(count: Int, intervalSeconds: Double) {
        require(count > 0) { "photo interval count must be positive" }
        require(intervalSeconds.isFinite() && intervalSeconds > 0.0) {
            "photo interval seconds must be finite and positive"
        }
    }

    fun requireWhiteBalance(modeName: String, colorTemperatureKelvin: Int) {
        if (modeName.uppercase(Locale.US) == "MANUAL") {
            require(colorTemperatureKelvin in 2_000..10_000) {
                "manual white balance temperature must be 2000..10000 K"
            }
        }
    }

    fun requireGimbalAngles(
        pitch: Double,
        roll: Double,
        yaw: Double,
        durationSeconds: Double
    ) {
        require(listOf(pitch, roll, yaw, durationSeconds).all(Double::isFinite)) {
            "gimbal angle values and duration must be finite"
        }
        require(pitch in -180.0..180.0 && roll in -180.0..180.0 && yaw in -360.0..360.0) {
            "gimbal angle request is outside the MSDK wire guardrails"
        }
        require(durationSeconds in 0.0..60.0) { "gimbal duration must be 0..60 seconds" }
    }

    fun requireGimbalSpeed(
        pitch: Double,
        yaw: Double,
        roll: Double,
        maxPitch: Int,
        maxYaw: Int
    ) {
        require(listOf(pitch, yaw, roll).all(Double::isFinite)) {
            "gimbal speeds must be finite"
        }
        require(abs(pitch) <= maxPitch.toDouble()) {
            "pitch speed $pitch exceeds aircraft runtime maximum $maxPitch"
        }
        require(abs(yaw) <= maxYaw.toDouble()) {
            "yaw speed $yaw exceeds aircraft runtime maximum $maxYaw"
        }
        require(abs(roll) <= 1e-9) {
            "Mini 4 Pro exposes no runtime roll-speed limit; roll speed must be zero"
        }
    }

    fun requireFormatConfirmation(storageName: String, confirmation: String?) {
        val expected = FORMAT_CONFIRM_PREFIX + storageName.uppercase(Locale.US)
        require(confirmation == expected) { "format requires confirm=$expected" }
    }

    private fun operation(
        name: String,
        keys: Set<String>,
        validation: String,
        evidence: String
    ) = CameraGimbalCapabilityEntry(name, keys, validation, evidence)
}

/**
 * Starts exactly one asynchronous mutation at a time.  A task retains the
 * queue slot until it invokes done, so a DJI callback cannot be overtaken by a
 * later camera/gimbal mutation.
 */
class SerializedCameraGimbalQueue(
    private val executor: Executor = Executors.newSingleThreadExecutor()
) : AutoCloseable {
    private val lock = Any()
    private val pending = ArrayDeque<((() -> Unit) -> Unit)>()
    private var running = false
    private var closed = false

    fun submit(work: (() -> Unit) -> Unit) {
        val shouldStart = synchronized(lock) {
            check(!closed) { "camera/gimbal queue is closed" }
            pending.addLast(work)
            if (running) false else {
                running = true
                true
            }
        }
        if (shouldStart) startNext()
    }

    private fun startNext() {
        val work = synchronized(lock) { pending.firstOrNull() }
        if (work == null) {
            synchronized(lock) { running = false }
            return
        }
        executor.execute {
            val completed = AtomicBoolean(false)
            val done = {
                if (completed.compareAndSet(false, true)) {
                    synchronized(lock) {
                        if (pending.isNotEmpty()) pending.removeFirst()
                    }
                    startNext()
                }
                Unit
            }
            try {
                work(done)
            } catch (_: Throwable) {
                done()
            }
        }
    }

    override fun close() {
        synchronized(lock) {
            closed = true
            pending.clear()
        }
        (executor as? java.util.concurrent.ExecutorService)?.shutdownNow()
    }
}
