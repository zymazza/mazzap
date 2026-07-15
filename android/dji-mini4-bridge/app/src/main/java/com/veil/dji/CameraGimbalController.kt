package com.veil.dji

import dji.sdk.keyvalue.key.CameraKey
import dji.sdk.keyvalue.key.DJIActionKeyInfo
import dji.sdk.keyvalue.key.DJIKey
import dji.sdk.keyvalue.key.DJIKeyInfo
import dji.sdk.keyvalue.key.GimbalKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.value.camera.CameraExposureCompensation
import dji.sdk.keyvalue.value.camera.CameraExposureMode
import dji.sdk.keyvalue.value.camera.CameraFocusMode
import dji.sdk.keyvalue.value.camera.CameraFocusModeMsg
import dji.sdk.keyvalue.value.camera.CameraISO
import dji.sdk.keyvalue.value.camera.CameraMode
import dji.sdk.keyvalue.value.camera.CameraShutterSpeed
import dji.sdk.keyvalue.value.camera.CameraStorageInfos
import dji.sdk.keyvalue.value.camera.CameraStorageLocation
import dji.sdk.keyvalue.value.camera.CameraWhiteBalanceInfo
import dji.sdk.keyvalue.value.camera.CameraWhiteBalanceMode
import dji.sdk.keyvalue.value.camera.PhotoFileFormat
import dji.sdk.keyvalue.value.camera.PhotoIntervalShootSettings
import dji.sdk.keyvalue.value.camera.PhotoRatio
import dji.sdk.keyvalue.value.camera.VideoFileFormat
import dji.sdk.keyvalue.value.camera.VideoResolutionFrameRate
import dji.sdk.keyvalue.value.camera.ZoomRatiosRange
import dji.sdk.keyvalue.value.common.Attitude
import dji.sdk.keyvalue.value.common.CameraLensType
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.common.DoublePoint2D
import dji.sdk.keyvalue.value.common.EmptyMsg
import dji.sdk.keyvalue.value.gimbal.CtrlInfo
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotation
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotationMode
import dji.sdk.keyvalue.value.gimbal.GimbalMode
import dji.sdk.keyvalue.value.gimbal.GimbalResetType
import dji.sdk.keyvalue.value.gimbal.GimbalSpeedRotation
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.KeyManager
import dji.v5.manager.interfaces.IKeyManager
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.math.abs

data class Mini4CameraGimbalStatus(
    val capturedAtMs: Long,
    val cameraConnected: Boolean?,
    val cameraMode: CameraMode?,
    val cameraModeRange: List<CameraMode>?,
    val isShootingPhoto: Boolean?,
    val isShootingIntervalPhotos: Boolean?,
    val intervalCountdownSeconds: Int?,
    val isRecording: Boolean?,
    val recordingTimeSeconds: Int?,
    val zoomRatio: Double?,
    val zoomRange: ZoomRatiosRange?,
    val zoomFocalLengthMillimeters: Int?,
    val focusMode: CameraFocusMode?,
    val focusTarget: DoublePoint2D?,
    val exposureMode: CameraExposureMode?,
    val exposureCompensation: CameraExposureCompensation?,
    val iso: CameraISO?,
    val shutterSpeed: CameraShutterSpeed?,
    val whiteBalance: CameraWhiteBalanceInfo?,
    val photoFileFormat: PhotoFileFormat?,
    val photoRatio: PhotoRatio?,
    val videoFileFormat: VideoFileFormat?,
    val videoResolutionFrameRate: VideoResolutionFrameRate?,
    val storage: CameraStorageInfos?,
    val gimbalConnected: Boolean?,
    val gimbalAttitude: Attitude?,
    val gimbalMode: GimbalMode?,
    val gimbalVerticalShotEnabled: Boolean?
)

/**
 * Public-MSDK camera and gimbal control for the Mini 4 Pro's single main lens.
 *
 * Mutations are serialized across asynchronous DJI callbacks.  Setters first
 * query an aircraft-reported range when the Mini 4 Pro manifest exposes one,
 * then perform a direct readback.  Action callbacks are reported separately
 * from physical-state observation.
 */
class CameraGimbalController(
    private val managerProvider: () -> IKeyManager = { KeyManager.getInstance() },
    private val queue: SerializedCameraGimbalQueue = SerializedCameraGimbalQueue()
) : AutoCloseable {
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private val manager: IKeyManager by lazy(managerProvider)
    private val linkGate = CameraGimbalLinkGate()
    private val activeContexts = ConcurrentHashMap.newKeySet<MutationContext>()
    private val speedGeneration = AtomicLong(0L)
    private val speedLeaseFuture = AtomicReference<ScheduledFuture<*>?>(null)
    private val activeSpeedContext = AtomicReference<MutationContext?>(null)
    private val gimbalSpeedTouched = AtomicBoolean(false)

    fun capabilityReport(): CameraGimbalCapabilityReport =
        Mini4CameraGimbalPolicy.capabilityReport()

    /** A non-blocking snapshot from KeyManager's latest values. */
    fun cachedStatus(): Mini4CameraGimbalStatus = Mini4CameraGimbalStatus(
        capturedAtMs = System.currentTimeMillis(),
        cameraConnected = cached(camera(CameraKey.KeyConnection)),
        cameraMode = cached(camera(CameraKey.KeyCameraMode)),
        cameraModeRange = cached(camera(CameraKey.KeyCameraModeRange)),
        isShootingPhoto = cached(camera(CameraKey.KeyIsShootingPhoto)),
        isShootingIntervalPhotos = cached(camera(CameraKey.KeyIsShootingIntervalPhotos)),
        intervalCountdownSeconds = cached(camera(CameraKey.KeyPhotoIntervalCountdown)),
        isRecording = cached(camera(CameraKey.KeyIsRecording)),
        recordingTimeSeconds = cached(camera(CameraKey.KeyRecordingTime)),
        zoomRatio = cached(camera(CameraKey.KeyCameraZoomRatios)),
        zoomRange = cached(camera(CameraKey.KeyCameraZoomRatiosRange)),
        zoomFocalLengthMillimeters = cached(camera(CameraKey.KeyCameraZoomFocalLength)),
        focusMode = cached(camera(CameraKey.KeyCameraFocusMode)),
        focusTarget = cached(camera(CameraKey.KeyCameraFocusTarget)),
        exposureMode = cached(camera(CameraKey.KeyExposureMode)),
        exposureCompensation = cached(camera(CameraKey.KeyExposureCompensation)),
        iso = cached(camera(CameraKey.KeyISO)),
        shutterSpeed = cached(camera(CameraKey.KeyShutterSpeed)),
        whiteBalance = cached(camera(CameraKey.KeyWhiteBalance)),
        photoFileFormat = cached(camera(CameraKey.KeyPhotoFileFormat)),
        photoRatio = cached(camera(CameraKey.KeyPhotoRatio)),
        videoFileFormat = cached(camera(CameraKey.KeyVideoFileFormat)),
        videoResolutionFrameRate = cached(camera(CameraKey.KeyVideoResolutionFrameRate)),
        storage = cached(camera(CameraKey.KeyCameraStorageInfos)),
        gimbalConnected = cached(gimbal(GimbalKey.KeyConnection)),
        gimbalAttitude = cached(gimbal(GimbalKey.KeyGimbalAttitude)),
        gimbalMode = cached(gimbal(GimbalKey.KeyGimbalMode)),
        gimbalVerticalShotEnabled = cached(gimbal(GimbalKey.KeyGimbalVerticalShotEnabled))
    )

    fun setCameraMode(value: CameraMode, observer: CameraGimbalMutationObserver): String =
        setWithRuntimeRange(
            operation = "camera_mode",
            requested = value,
            valueKey = camera(CameraKey.KeyCameraMode),
            rangeKey = camera(CameraKey.KeyCameraModeRange),
            observer = observer
        )

    fun setExposureMode(
        value: CameraExposureMode,
        observer: CameraGimbalMutationObserver
    ): String = setWithRuntimeRange(
        "exposure_mode",
        value,
        camera(CameraKey.KeyExposureMode),
        camera(CameraKey.KeyExposureModeRange),
        observer
    )

    fun setExposureCompensation(
        value: CameraExposureCompensation,
        observer: CameraGimbalMutationObserver
    ): String = setWithRuntimeRange(
        "exposure_compensation",
        value,
        camera(CameraKey.KeyExposureCompensation),
        camera(CameraKey.KeyExposureCompensationRange),
        observer
    )

    fun setISO(value: CameraISO, observer: CameraGimbalMutationObserver): String =
        setWithRuntimeRange(
            "iso",
            value,
            camera(CameraKey.KeyISO),
            camera(CameraKey.KeyISORange),
            observer
        )

    fun setShutterSpeed(
        value: CameraShutterSpeed,
        observer: CameraGimbalMutationObserver
    ): String = setWithRuntimeRange(
        "shutter_speed",
        value,
        camera(CameraKey.KeyShutterSpeed),
        camera(CameraKey.KeyShutterSpeedRange),
        observer
    )

    fun setPhotoFileFormat(
        value: PhotoFileFormat,
        observer: CameraGimbalMutationObserver
    ): String = setWithRuntimeRange(
        "photo_file_format",
        value,
        camera(CameraKey.KeyPhotoFileFormat),
        camera(CameraKey.KeyPhotoFileFormatRange),
        observer
    )

    fun setPhotoRatio(value: PhotoRatio, observer: CameraGimbalMutationObserver): String =
        setWithRuntimeRange(
            "photo_ratio",
            value,
            camera(CameraKey.KeyPhotoRatio),
            camera(CameraKey.KeyPhotoRatioRange),
            observer
        )

    fun setVideoFileFormat(
        value: VideoFileFormat,
        observer: CameraGimbalMutationObserver
    ): String = setWithRuntimeRange(
        "video_file_format",
        value,
        camera(CameraKey.KeyVideoFileFormat),
        camera(CameraKey.KeyVideoFileFormatRange),
        observer
    )

    fun setVideoResolutionFrameRate(
        value: VideoResolutionFrameRate,
        observer: CameraGimbalMutationObserver
    ): String = setWithRuntimeRange(
        operation = "video_resolution_frame_rate",
        requested = value,
        valueKey = camera(CameraKey.KeyVideoResolutionFrameRate),
        rangeKey = camera(CameraKey.KeyVideoResolutionFrameRateRange),
        observer = observer,
        equivalent = ::sameVideoSpec
    )

    fun setFocusMode(
        value: CameraFocusMode,
        observer: CameraGimbalMutationObserver
    ): String {
        val rangeKey = camera(CameraKey.KeyCameraFocusModeRange)
        return setWithRuntimeRange(
            operation = "focus_mode",
            requested = value,
            valueKey = camera(CameraKey.KeyCameraFocusMode),
            rangeKey = rangeKey,
            observer = observer,
            rangeTransform = { range: List<CameraFocusModeMsg> -> range.map { it.value } }
        )
    }

    fun setFocusTarget(
        x: Double,
        y: Double,
        observer: CameraGimbalMutationObserver
    ): String = setWithReadback(
        operation = "focus_target",
        requested = DoublePoint2D(x, y),
        valueKey = camera(CameraKey.KeyCameraFocusTarget),
        observer = observer,
        validate = { Mini4CameraGimbalPolicy.requireFocusTarget(it.x, it.y) },
        equivalent = { expected, actual ->
            abs(expected.x - actual.x) <= 1e-6 && abs(expected.y - actual.y) <= 1e-6
        }
    )

    fun setWhiteBalance(
        value: CameraWhiteBalanceInfo,
        observer: CameraGimbalMutationObserver
    ): String {
        return mutation("white_balance", value.toString(), observer) { context ->
            Mini4CameraGimbalPolicy.requireWhiteBalance(
                value.whiteBalanceMode.name,
                value.colorTemperature
            )
            val rangeKey = camera(CameraKey.KeyCameraWhiteBalanceRange)
            directGet(context, rangeKey) { range ->
                Mini4CameraGimbalPolicy.requireRuntimeChoice(
                    "white balance mode",
                    value.whiteBalanceMode,
                    range
                )
                setAndReadBack(
                    context,
                    camera(CameraKey.KeyWhiteBalance),
                    value,
                    ::sameWhiteBalance
                )
            }
        }
    }

    fun setZoomRatio(
        ratio: Double,
        observer: CameraGimbalMutationObserver
    ): String = mutation("zoom_ratio", ratio.toString(), observer) { context ->
        directGet(context, camera(CameraKey.KeyCameraZoomRatiosRange)) { range ->
            Mini4CameraGimbalPolicy.requireZoomRatio(ratio, range.isContinuous, range.gears)
            setAndReadBack(
                context,
                camera(CameraKey.KeyCameraZoomRatios),
                ratio,
                equivalent = { expected, actual -> abs(expected - actual) <= 0.05 }
            )
        }
    }

    fun setGimbalMode(value: GimbalMode, observer: CameraGimbalMutationObserver): String =
        setWithReadback(
            operation = "gimbal_mode",
            requested = value,
            valueKey = gimbal(GimbalKey.KeyGimbalMode),
            observer = observer,
            validate = { require(it != GimbalMode.UNKNOWN) { "unknown gimbal mode is invalid" } }
        )

    fun setVerticalShotEnabled(
        enabled: Boolean,
        observer: CameraGimbalMutationObserver
    ): String = setWithReadback(
        "gimbal_vertical_shot",
        enabled,
        gimbal(GimbalKey.KeyGimbalVerticalShotEnabled),
        observer
    )

    fun setPhotoIntervalSettings(
        count: Int,
        intervalSeconds: Double,
        observer: CameraGimbalMutationObserver
    ): String = setWithReadback(
        operation = "photo_interval_settings",
        requested = PhotoIntervalShootSettings(count, intervalSeconds),
        valueKey = camera(CameraKey.KeyPhotoIntervalShootSettings),
        observer = observer,
        validate = {
            Mini4CameraGimbalPolicy.requirePhotoInterval(it.count, it.interval)
        },
        equivalent = ::sameInterval
    )

    fun takePhoto(observer: CameraGimbalMutationObserver): String =
        observedCameraAction(
            operation = "take_photo",
            action = CameraKey.KeyStartShootPhoto,
            observedKey = CameraKey.KeyIsShootingPhoto,
            desired = true,
            observer = observer
        )

    fun startIntervalPhotos(observer: CameraGimbalMutationObserver): String =
        observedCameraAction(
            operation = "start_interval_photos",
            action = CameraKey.KeyStartShootPhoto,
            observedKey = CameraKey.KeyIsShootingIntervalPhotos,
            desired = true,
            observer = observer
        )

    fun stopPhotoCapture(observer: CameraGimbalMutationObserver): String =
        observedCameraAction(
            operation = "stop_photo_capture",
            action = CameraKey.KeyStopShootPhoto,
            observedKey = CameraKey.KeyIsShootingPhoto,
            desired = false,
            observer = observer
        )

    fun stopIntervalPhotos(observer: CameraGimbalMutationObserver): String =
        observedCameraAction(
            operation = "stop_interval_photos",
            action = CameraKey.KeyStopShootPhoto,
            observedKey = CameraKey.KeyIsShootingIntervalPhotos,
            desired = false,
            observer = observer
        )

    fun startRecording(observer: CameraGimbalMutationObserver): String =
        observedCameraAction(
            operation = "start_recording",
            action = CameraKey.KeyStartRecord,
            observedKey = CameraKey.KeyIsRecording,
            desired = true,
            observer = observer
        )

    fun stopRecording(observer: CameraGimbalMutationObserver): String =
        observedCameraAction(
            operation = "stop_recording",
            action = CameraKey.KeyStopRecord,
            observedKey = CameraKey.KeyIsRecording,
            desired = false,
            observer = observer
        )

    fun formatStorage(
        location: CameraStorageLocation,
        confirmation: String?,
        observer: CameraGimbalMutationObserver
    ): String = mutation("format_storage", location.name, observer) { context ->
        Mini4CameraGimbalPolicy.requireFormatConfirmation(location.name, confirmation)
        performAction(
            context,
            cameraAction(CameraKey.KeyFormatStorage),
            location
        ) {
            context.accepted(
                terminal = true,
                message = "DJI format callback succeeded; no independent format-complete key is exposed"
            )
        }
    }

    fun rotateGimbalByAngle(
        request: GimbalAngleRotation,
        observer: CameraGimbalMutationObserver
    ): String = mutation("gimbal_rotate_angle", request.toString(), observer) { context ->
        Mini4CameraGimbalPolicy.requireGimbalAngles(
            request.pitch,
            request.roll,
            request.yaw,
            request.duration
        )
        require(!(request.pitchIgnored && request.rollIgnored && request.yawIgnored)) {
            "at least one gimbal angle axis must be active"
        }
        directGet(context, gimbal(GimbalKey.KeyGimbalAttitude)) { before ->
            performAction(context, gimbalAction(GimbalKey.KeyRotateByAngle), request) {
                context.accepted(terminal = false)
                observeGimbalAngle(context, before, request)
            }
        }
    }

    fun rotateGimbalBySpeed(
        pitchDegreesPerSecond: Double,
        yawDegreesPerSecond: Double,
        requestedLeaseMs: Long?,
        observer: CameraGimbalMutationObserver
    ): String = priorityMutation(
        "gimbal_rotate_speed",
        "pitch=$pitchDegreesPerSecond,yaw=$yawDegreesPerSecond,roll=0.0," +
            "lease_ms=${requestedLeaseMs ?: GimbalSpeedLeasePolicy.DEFAULT_LEASE_MS}",
        observer
    ) speedMutation@{ context ->
        gimbalSpeedTouched.set(true)
        val leaseMs = GimbalSpeedLeasePolicy.normalizedLeaseMs(
            pitchDegreesPerSecond,
            yawDegreesPerSecond,
            requestedLeaseMs
        )
        val generation = beginSpeedLease(context)
        if (leaseMs == 0L) {
            val request = GimbalSpeedRotation(
                0.0,
                0.0,
                0.0,
                CtrlInfo(false, false)
            )
            performAction(context, gimbalAction(GimbalKey.KeyRotateBySpeed), request) {
                activeSpeedContext.compareAndSet(context, null)
                context.accepted(
                    terminal = true,
                    message = "priority zero-speed callback accepted"
                )
            }
            return@speedMutation
        }
        directGet(context, gimbal(GimbalKey.KeyPitchControlMaxSpeed)) { maxPitch ->
            directGet(context, gimbal(GimbalKey.KeyYawControlMaxSpeed)) { maxYaw ->
                Mini4CameraGimbalPolicy.requireGimbalSpeed(
                    pitchDegreesPerSecond,
                    yawDegreesPerSecond,
                    0.0,
                    maxPitch,
                    maxYaw
                )
                val request = GimbalSpeedRotation(
                    pitchDegreesPerSecond,
                    yawDegreesPerSecond,
                    0.0,
                    CtrlInfo(false, false)
                )
                performAction(context, gimbalAction(GimbalKey.KeyRotateBySpeed), request) {
                    context.accepted(terminal = false)
                    if (!context.isTerminal() && speedGeneration.get() == generation) {
                        val future = scheduler.schedule(
                            { sendPrioritySpeedZero(generation, context, "lease_expired") },
                            leaseMs,
                            TimeUnit.MILLISECONDS
                        )
                        speedLeaseFuture.getAndSet(future)?.cancel(false)
                    }
                }
            }
        }
    }

    fun resetGimbal(
        type: GimbalResetType,
        observer: CameraGimbalMutationObserver
    ): String = mutation("gimbal_reset", type.name, observer) { context ->
        require(type == GimbalResetType.RECENTER || type == GimbalResetType.SELFIE) {
            "Mini 4 Pro manifest supports only RECENTER and SELFIE reset actions"
        }
        directGet(context, gimbal(GimbalKey.KeyGimbalAttitude)) { before ->
            performAction(context, gimbalAction(GimbalKey.KeyGimbalReset), type) {
                context.accepted(terminal = false)
                observeAttitudeChange(context, before)
            }
        }
    }

    /** Invalidates every old-link command before a later USB/product reconnect. */
    fun onLinkDisconnected(reason: String) {
        val generation = linkGate.disconnect()
        activeContexts.toList().forEach { it.invalidateLink(reason, generation) }
        forcePrioritySpeedZero("link_disconnected:$reason")
    }

    fun onLinkConnected(reason: String) {
        val previousGeneration = linkGate.ticket()
        val generation = linkGate.connect()
        if (generation != previousGeneration) {
            activeContexts.toList().forEach {
                it.invalidateLink("link_reconnected:$reason", generation)
            }
        }
        BridgeState.lastEvent.set("camera_gimbal_link_connected:$reason:generation=$generation")
    }

    override fun close() {
        val generation = linkGate.disconnect()
        activeContexts.toList().forEach { it.invalidateLink("controller_close", generation) }
        forcePrioritySpeedZero("controller_close")
        scheduler.shutdownNow()
        queue.close()
    }

    private fun <T> setWithRuntimeRange(
        operation: String,
        requested: T,
        valueKey: DJIKey<T>,
        rangeKey: DJIKey<List<T>>,
        observer: CameraGimbalMutationObserver,
        equivalent: (T, T) -> Boolean = { expected, actual -> expected == actual }
    ): String = setWithRuntimeRange(
        operation,
        requested,
        valueKey,
        rangeKey,
        observer,
        { it },
        equivalent
    )

    private fun <T, R> setWithRuntimeRange(
        operation: String,
        requested: T,
        valueKey: DJIKey<T>,
        rangeKey: DJIKey<R>,
        observer: CameraGimbalMutationObserver,
        rangeTransform: (R) -> Collection<T>,
        equivalent: (T, T) -> Boolean = { expected, actual -> expected == actual }
    ): String = mutation(operation, requested.toString(), observer) { context ->
        directGet(context, rangeKey) { rawRange ->
            Mini4CameraGimbalPolicy.requireRuntimeChoice(
                operation.replace('_', ' '),
                requested,
                rangeTransform(rawRange)
            )
            setAndReadBack(context, valueKey, requested, equivalent)
        }
    }

    private fun <T> setWithReadback(
        operation: String,
        requested: T,
        valueKey: DJIKey<T>,
        observer: CameraGimbalMutationObserver,
        validate: (T) -> Unit = {},
        equivalent: (T, T) -> Boolean = { expected, actual -> expected == actual }
    ): String = mutation(operation, requested.toString(), observer) { context ->
        validate(requested)
        setAndReadBack(context, valueKey, requested, equivalent)
    }

    private fun <T> setAndReadBack(
        context: MutationContext,
        key: DJIKey<T>,
        requested: T,
        equivalent: (T, T) -> Boolean,
        then: (() -> Unit)? = null
    ) {
        requireSupported(context, key)
        manager.setValue(key, requested, object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                if (!context.ensureLinkCurrent()) return
                context.accepted(terminal = false)
                directGet(context, key) { observed ->
                    if (!equivalent(requested, observed)) {
                        context.fail(
                            "readback mismatch: requested=$requested observed=$observed",
                            observed.toString()
                        )
                    } else if (then == null) {
                        context.finish(
                            CameraGimbalMutationStage.READBACK_VERIFIED,
                            "DJI setter callback and direct readback succeeded",
                            observed.toString()
                        )
                    } else {
                        then()
                    }
                }
            }

            override fun onFailure(error: IDJIError) {
                context.failDji("set", error)
            }
        })
    }

    private fun observedCameraAction(
        operation: String,
        action: DJIActionKeyInfo<EmptyMsg, EmptyMsg>,
        observedKey: DJIKeyInfo<Boolean>,
        desired: Boolean,
        observer: CameraGimbalMutationObserver
    ): String = mutation(operation, desired.toString(), observer) { context ->
        performAction(context, cameraAction(action), EmptyMsg()) {
            context.accepted(terminal = false)
            observeValue(
                context,
                camera(observedKey),
                desired,
                equivalent = { expected, actual -> expected == actual }
            )
        }
    }

    private fun <P, R> performAction(
        context: MutationContext,
        key: DJIKey.ActionKey<P, R>,
        parameter: P,
        onAccepted: (R) -> Unit
    ) {
        requireSupported(context, key)
        manager.performAction(
            key,
            parameter,
            object : CommonCallbacks.CompletionCallbackWithParam<R> {
                override fun onSuccess(value: R) {
                    if (context.ensureLinkCurrent()) onAccepted(value)
                }

                override fun onFailure(error: IDJIError) {
                    context.failDji("action", error)
                }
            }
        )
    }

    private fun <T> directGet(
        context: MutationContext,
        key: DJIKey<T>,
        onValue: (T) -> Unit
    ) {
        if (context.isTerminal()) return
        requireSupported(context, key)
        manager.getValue(key, object : CommonCallbacks.CompletionCallbackWithParam<T> {
            override fun onSuccess(value: T) {
                if (!context.ensureLinkCurrent()) return
                try {
                    onValue(value)
                } catch (error: IllegalArgumentException) {
                    context.reject(error.message ?: error.toString())
                } catch (error: Throwable) {
                    context.fail("bridge processing failed: $error")
                }
            }

            override fun onFailure(error: IDJIError) {
                context.failDji("get", error)
            }
        })
    }

    private fun <T> observeValue(
        context: MutationContext,
        key: DJIKey<T>,
        desired: T,
        equivalent: (T, T) -> Boolean,
        deadlineNanos: Long = System.nanoTime() + OBSERVATION_TIMEOUT_NANOS
    ) {
        if (context.isTerminal()) return
        try {
            requireSupported(context, key)
            manager.getValue(key, object : CommonCallbacks.CompletionCallbackWithParam<T> {
                override fun onSuccess(value: T) {
                    if (!context.ensureLinkCurrent()) return
                    if (equivalent(desired, value)) {
                        context.finish(
                            CameraGimbalMutationStage.PHYSICALLY_OBSERVED,
                            "aircraft state observation matched the request",
                            value.toString()
                        )
                    } else {
                        retryObservation(context, deadlineNanos) {
                            observeValue(context, key, desired, equivalent, deadlineNanos)
                        }
                    }
                }

                override fun onFailure(error: IDJIError) {
                    retryObservation(context, deadlineNanos) {
                        observeValue(context, key, desired, equivalent, deadlineNanos)
                    }
                }
            })
        } catch (error: Throwable) {
            retryObservation(context, deadlineNanos) {
                observeValue(context, key, desired, equivalent, deadlineNanos)
            }
        }
    }

    private fun observeGimbalAngle(
        context: MutationContext,
        before: Attitude,
        request: GimbalAngleRotation
    ) {
        val expected = Attitude(
            expectedAngle(before.pitch, request.pitch, request.pitchIgnored, request.mode, false),
            expectedAngle(before.roll, request.roll, request.rollIgnored, request.mode, false),
            expectedAngle(before.yaw, request.yaw, request.yawIgnored, request.mode, true)
        )
        observeValue(
            context,
            gimbal(GimbalKey.KeyGimbalAttitude),
            expected,
            equivalent = { target, actual ->
                (request.pitchIgnored || abs(target.pitch - actual.pitch) <= GIMBAL_TOLERANCE_DEGREES) &&
                    (request.rollIgnored || abs(target.roll - actual.roll) <= GIMBAL_TOLERANCE_DEGREES) &&
                    (request.yawIgnored || angleDistance(target.yaw, actual.yaw) <= GIMBAL_TOLERANCE_DEGREES)
            }
        )
    }

    private fun observeAttitudeChange(context: MutationContext, before: Attitude) {
        val key = gimbal(GimbalKey.KeyGimbalAttitude)
        val deadline = System.nanoTime() + OBSERVATION_TIMEOUT_NANOS
        fun poll() {
            if (context.isTerminal()) return
            try {
                manager.getValue(key, object : CommonCallbacks.CompletionCallbackWithParam<Attitude> {
                    override fun onSuccess(value: Attitude) {
                        if (context.isTerminal()) return
                        val changed = abs(value.pitch - before.pitch) > 1.0 ||
                            abs(value.roll - before.roll) > 1.0 ||
                            angleDistance(value.yaw, before.yaw) > 1.0
                        if (changed) {
                            context.finish(
                                CameraGimbalMutationStage.PHYSICALLY_OBSERVED,
                                "gimbal attitude changed after reset",
                                value.toString()
                            )
                        } else {
                            retryObservation(context, deadline, ::poll)
                        }
                    }

                    override fun onFailure(error: IDJIError) {
                        retryObservation(context, deadline, ::poll)
                    }
                })
            } catch (_: Throwable) {
                retryObservation(context, deadline, ::poll)
            }
        }
        poll()
    }

    private fun retryObservation(
        context: MutationContext,
        deadlineNanos: Long,
        retry: () -> Unit
    ) {
        if (context.isTerminal()) return
        if (System.nanoTime() >= deadlineNanos) {
            context.finish(
                CameraGimbalMutationStage.ACCEPTED_NOT_OBSERVED,
                "DJI accepted the action, but physical state was not observed before timeout"
            )
        } else {
            scheduler.schedule(retry, OBSERVATION_POLL_MILLIS, TimeUnit.MILLISECONDS)
        }
    }

    private fun beginSpeedLease(context: MutationContext): Long {
        val generation = speedGeneration.incrementAndGet()
        speedLeaseFuture.getAndSet(null)?.cancel(false)
        activeSpeedContext.getAndSet(context)?.takeIf { it !== context }?.let { previous ->
            if (previous.wasCallbackAccepted()) {
                previous.finish(
                    CameraGimbalMutationStage.CALLBACK_ACCEPTED,
                    "speed lease superseded by a newer priority command"
                )
            } else {
                previous.fail("speed command superseded before DJI callback")
            }
        }
        return generation
    }

    private fun sendPrioritySpeedZero(
        generation: Long,
        context: MutationContext,
        reason: String
    ) {
        if (context.isTerminal() || speedGeneration.get() != generation) return
        val request = GimbalSpeedRotation(0.0, 0.0, 0.0, CtrlInfo(false, false))
        try {
            val key = gimbalAction(GimbalKey.KeyRotateBySpeed)
            require(manager.isKeySupported(key)) { "gimbal speed zero is unsupported" }
            manager.performAction(
                key,
                request,
                object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                    override fun onSuccess(value: EmptyMsg) {
                        if (speedGeneration.get() != generation) return
                        activeSpeedContext.compareAndSet(context, null)
                        context.finish(
                            CameraGimbalMutationStage.CALLBACK_ACCEPTED,
                            "nonzero speed accepted; priority zero callback accepted ($reason)"
                        )
                    }

                    override fun onFailure(error: IDJIError) {
                        if (speedGeneration.get() != generation) return
                        activeSpeedContext.compareAndSet(context, null)
                        context.failDji("priority zero ($reason)", error)
                    }
                }
            )
        } catch (error: Throwable) {
            activeSpeedContext.compareAndSet(context, null)
            context.fail("priority zero dispatch failed ($reason): $error")
        }
    }

    private fun forcePrioritySpeedZero(reason: String) {
        if (!gimbalSpeedTouched.get()) return
        speedGeneration.incrementAndGet()
        speedLeaseFuture.getAndSet(null)?.cancel(false)
        activeSpeedContext.getAndSet(null)
        val request = GimbalSpeedRotation(0.0, 0.0, 0.0, CtrlInfo(false, false))
        runCatching {
            val key = gimbalAction(GimbalKey.KeyRotateBySpeed)
            manager.performAction(
                key,
                request,
                object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                    override fun onSuccess(value: EmptyMsg) {
                        BridgeState.lastEvent.set("gimbal_priority_zero:$reason")
                    }

                    override fun onFailure(error: IDJIError) {
                        BridgeState.lastEvent.set("gimbal_priority_zero_failed:$reason:$error")
                    }
                }
            )
        }.onFailure {
            BridgeState.lastEvent.set("gimbal_priority_zero_exception:$reason:${it.message}")
        }
    }

    private fun priorityMutation(
        operation: String,
        requestedValue: String,
        observer: CameraGimbalMutationObserver,
        work: (MutationContext) -> Unit
    ): String {
        val context = newContext(operation, requestedValue, observer)
        context.armTimeout()
        try {
            if (!context.ensureLinkCurrent()) return context.commandId
            work(context)
        } catch (error: IllegalArgumentException) {
            context.reject(error.message ?: error.toString())
        } catch (error: Throwable) {
            context.fail("priority dispatch failed: $error")
        }
        return context.commandId
    }

    private fun mutation(
        operation: String,
        requestedValue: String,
        observer: CameraGimbalMutationObserver,
        work: (MutationContext) -> Unit
    ): String {
        val context = newContext(operation, requestedValue, observer)
        try {
            queue.submit { done ->
                context.attachQueueDone(done)
                if (context.isTerminal()) return@submit
                context.armTimeout()
                if (!context.ensureLinkCurrent()) return@submit
                try {
                    work(context)
                } catch (error: IllegalArgumentException) {
                    context.reject(error.message ?: error.toString())
                } catch (error: Throwable) {
                    context.fail("dispatch failed: $error")
                }
            }
        } catch (error: Throwable) {
            context.fail("queue failed: $error")
        }
        return context.commandId
    }

    private fun newContext(
        operation: String,
        requestedValue: String,
        observer: CameraGimbalMutationObserver
    ): MutationContext {
        val context = MutationContext(
            UUID.randomUUID().toString(),
            operation,
            requestedValue,
            observer,
            linkGate.ticket()
        )
        activeContexts += context
        emitSafely(
            observer,
            CameraGimbalMutationEvent(
                context.commandId,
                operation,
                CameraGimbalMutationStage.QUEUED,
                terminal = false,
                requestedValue = requestedValue
            )
        )
        if (!linkGate.isConnected()) {
            context.invalidateLink("link_disconnected", linkGate.ticket())
        }
        return context
    }

    private inner class MutationContext(
        val commandId: String,
        private val operation: String,
        private val requestedValue: String,
        private val observer: CameraGimbalMutationObserver,
        private val capturedLinkGeneration: Long
    ) {
        private val terminal = AtomicBoolean(false)
        private val callbackAccepted = AtomicBoolean(false)
        private val queueDone = AtomicReference<(() -> Unit)?>(null)
        private var timeout: ScheduledFuture<*>? = null

        fun attachQueueDone(done: () -> Unit) {
            check(queueDone.compareAndSet(null, done)) { "queue completion already attached" }
            if (terminal.get()) done()
        }

        fun armTimeout() {
            timeout = scheduler.schedule(
                { fail("DJI mutation timed out") },
                MUTATION_TIMEOUT_SECONDS,
                TimeUnit.SECONDS
            )
        }

        fun isTerminal(): Boolean = terminal.get()

        fun wasCallbackAccepted(): Boolean = callbackAccepted.get()

        fun ensureLinkCurrent(): Boolean {
            if (linkGate.isValid(capturedLinkGeneration)) return !terminal.get()
            invalidateLink("generation_changed_or_disconnected", linkGate.ticket())
            return false
        }

        fun invalidateLink(reason: String, currentGeneration: Long) {
            fail(
                "camera/gimbal command invalidated by link disconnect: $reason " +
                    "(generation $capturedLinkGeneration->$currentGeneration)"
            )
        }

        fun accepted(terminal: Boolean, message: String? = null) {
            callbackAccepted.set(true)
            if (terminal) {
                finish(CameraGimbalMutationStage.CALLBACK_ACCEPTED, message)
            } else if (!this.terminal.get()) {
                emit(CameraGimbalMutationStage.CALLBACK_ACCEPTED, false, message)
            }
        }

        fun reject(message: String) =
            finish(CameraGimbalMutationStage.REJECTED, message)

        fun fail(message: String, observedValue: String? = null) =
            finish(CameraGimbalMutationStage.FAILED, message, observedValue)

        fun failDji(step: String, error: IDJIError) = fail(
            "DJI $step failed: code=${error.errorCode()} inner=${error.innerCode()} " +
                "description=${error.description()} hint=${error.hint()}"
        )

        fun finish(
            stage: CameraGimbalMutationStage,
            message: String? = null,
            observedValue: String? = null
        ) {
            if (!terminal.compareAndSet(false, true)) return
            timeout?.cancel(false)
            emit(stage, true, message, observedValue)
            activeContexts.remove(this)
            queueDone.get()?.invoke()
        }

        private fun emit(
            stage: CameraGimbalMutationStage,
            terminal: Boolean,
            message: String?,
            observedValue: String? = null
        ) {
            emitSafely(
                observer,
                CameraGimbalMutationEvent(
                    commandId,
                    operation,
                    stage,
                    terminal,
                    message,
                    requestedValue,
                    observedValue
                )
            )
        }
    }

    private fun <T> cached(key: DJIKey<T>): T? = runCatching {
        if (manager.isKeySupported(key)) manager.getValue(key) else null
    }.getOrNull()

    private fun requireSupported(context: MutationContext, key: DJIKey<*>) {
        check(context.ensureLinkCurrent()) { "mutation link generation is stale" }
        require(manager.isKeySupported(key)) {
            "Mini 4 Pro runtime does not support ${key.keyIdentifier}"
        }
        check(!context.isTerminal()) { "mutation already terminal" }
    }

    private fun <T> camera(info: DJIKeyInfo<T>): DJIKey<T> =
        KeyTools.createCameraKey(info, MAIN_COMPONENT, MAIN_LENS)

    private fun <P, R> cameraAction(info: DJIActionKeyInfo<P, R>): DJIKey.ActionKey<P, R> =
        KeyTools.createCameraKey(info, MAIN_COMPONENT, MAIN_LENS)

    private fun <T> gimbal(info: DJIKeyInfo<T>): DJIKey<T> =
        KeyTools.createKey(info, MAIN_COMPONENT)

    private fun <P, R> gimbalAction(info: DJIActionKeyInfo<P, R>): DJIKey.ActionKey<P, R> =
        KeyTools.createKey(info, MAIN_COMPONENT)

    companion object {
        private val MAIN_COMPONENT = ComponentIndexType.LEFT_OR_MAIN
        private val MAIN_LENS = CameraLensType.CAMERA_LENS_DEFAULT
        private const val MUTATION_TIMEOUT_SECONDS = 8L
        private const val OBSERVATION_POLL_MILLIS = 100L
        private val OBSERVATION_TIMEOUT_NANOS = TimeUnit.SECONDS.toNanos(3)
        private const val GIMBAL_TOLERANCE_DEGREES = 3.0

        private fun emitSafely(
            observer: CameraGimbalMutationObserver,
            event: CameraGimbalMutationEvent
        ) {
            runCatching { observer.onEvent(event) }
        }

        private fun sameVideoSpec(
            expected: VideoResolutionFrameRate,
            actual: VideoResolutionFrameRate
        ): Boolean = expected.resolution == actual.resolution && expected.frameRate == actual.frameRate

        private fun sameWhiteBalance(
            expected: CameraWhiteBalanceInfo,
            actual: CameraWhiteBalanceInfo
        ): Boolean = expected.whiteBalanceMode == actual.whiteBalanceMode &&
            expected.colorTemperature == actual.colorTemperature

        private fun sameInterval(
            expected: PhotoIntervalShootSettings,
            actual: PhotoIntervalShootSettings
        ): Boolean = expected.count == actual.count && abs(expected.interval - actual.interval) <= 1e-6

        private fun expectedAngle(
            before: Double,
            requested: Double,
            ignored: Boolean,
            mode: GimbalAngleRotationMode,
            wrap: Boolean
        ): Double {
            if (ignored) return before
            val raw = if (mode == GimbalAngleRotationMode.RELATIVE_ANGLE) before + requested else requested
            return if (wrap) normalizeAngle(raw) else raw
        }

        private fun normalizeAngle(value: Double): Double {
            var result = value % 360.0
            if (result > 180.0) result -= 360.0
            if (result < -180.0) result += 360.0
            return result
        }

        private fun angleDistance(a: Double, b: Double): Double = abs(normalizeAngle(a - b))
    }
}
