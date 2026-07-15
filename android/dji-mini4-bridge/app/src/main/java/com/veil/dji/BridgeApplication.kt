package com.veil.dji

import android.Manifest
import android.app.Application
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.core.content.ContextCompat
import dji.sdk.keyvalue.key.AirLinkKey
import dji.sdk.keyvalue.key.BatteryKey
import dji.sdk.keyvalue.key.FlightAssistantKey
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.key.GimbalKey
import dji.sdk.keyvalue.key.ProductKey
import dji.sdk.keyvalue.key.RemoteControllerKey
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.flightcontroller.FlightControlAuthorityChangeReason
import dji.v5.common.error.IDJIError
import dji.v5.common.register.DJISDKInitEvent
import dji.v5.et.create
import dji.v5.et.get
import dji.v5.et.isKeySupported
import dji.v5.et.listen
import dji.v5.manager.KeyManager
import dji.v5.manager.SDKManager
import dji.v5.manager.aircraft.perception.PerceptionManager
import dji.v5.manager.aircraft.perception.data.ObstacleData
import dji.v5.manager.aircraft.perception.data.PerceptionInfo
import dji.v5.manager.aircraft.perception.listener.ObstacleDataListener
import dji.v5.manager.aircraft.perception.listener.PerceptionInformationListener
import dji.v5.manager.aircraft.virtualstick.VirtualStickManager
import dji.v5.manager.aircraft.virtualstick.VirtualStickState
import dji.v5.manager.aircraft.virtualstick.VirtualStickStateListener
import dji.v5.manager.aircraft.uas.UASRemoteIDManager
import dji.v5.manager.aircraft.uas.UASRemoteIDStatus
import dji.v5.manager.aircraft.uas.UASRemoteIDStatusListener
import dji.v5.manager.dataprotect.DataProtectionManager
import dji.v5.manager.datacenter.camera.CameraStreamManager
import dji.v5.manager.diagnostic.DJIDeviceHealthInfo
import dji.v5.manager.diagnostic.DJIDeviceHealthInfoChangeListener
import dji.v5.manager.diagnostic.DeviceHealthManager
import dji.v5.manager.interfaces.SDKManagerCallback
import dji.v5.network.DJINetworkManager

/**
 * MSDK injects its provided API classes from Helper.install(). Keep the manifest
 * Application subclass free of direct DJI type references so Android does not
 * verify them before attachBaseContext has installed the DJI class loader.
 */
class BridgeApplication : BridgeBaseApplication() {
    override fun attachBaseContext(base: Context?) {
        super.attachBaseContext(base)
        com.cySdkyc.clx.Helper.install(this)
    }
}

open class BridgeBaseApplication : Application() {
    lateinit var runtime: BridgeRuntime
        private set

    override fun onCreate() {
        super.onCreate()
        runtime = BridgeRuntime(this)
        runtime.start()
    }

    override fun onTerminate() {
        runtime.stop()
        super.onTerminate()
    }
}

class BridgeRuntime(private val app: Application) {
    val controller = FlightController()
    val perceptionConfig = PerceptionConfigController()
    val cameraGimbal = CameraGimbalController()
    val cameraGimbalApi = CameraGimbalHttpApi(cameraGimbal)
    val server = BridgeHttpServer(app, controller, perceptionConfig, cameraGimbalApi)
    val videoRelay = RawVideoRelay(server.token)
    val realtimeControl = RealtimeControlServer(server.token, controller)
    val telemetry = TelemetryServer(server.token)
    private var telemetryStarted = false
    private var perceptionStarted = false
    private var perceptionManagerInitialized = false
    private var perceptionConsecutiveIssues = 0
    private var perceptionNextRetryAtMonotonicMs = 0L
    private var locationReceiverRegistered = false
    private val locationManager = app.getSystemService(Context.LOCATION_SERVICE) as LocationManager
    private val requestedLocationProviders = mutableSetOf<String>()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val refreshConnectionScopedHealth = Runnable {
        if (AircraftTelemetryState.snapshot().aircraftConnected) {
            runCatching { publishRemoteIdStatus(remoteIdManager.uasRemoteIDStatus) }
            runCatching { publishDeviceHealth(deviceHealthManager.currentDJIDeviceHealthInfos) }
        }
    }
    private val refreshTakeoffTelemetry = object : Runnable {
        override fun run() {
            if (!AircraftTelemetryState.snapshot().aircraftConnected) return
            refreshTakeoffTelemetrySnapshot()
            mainHandler.postDelayed(this, TAKEOFF_TELEMETRY_REFRESH_MILLIS)
        }
    }
    private val perceptionRecoveryWatchdog = object : Runnable {
        override fun run() {
            if (!telemetryStarted) return
            val nowMs = SystemClock.elapsedRealtime()
            val snapshot = AircraftTelemetryState.snapshot()
            val informationStale = snapshot.aircraftConnected &&
                PerceptionRecoveryPolicy.isSourceStale(
                    snapshot.perception.information.updatedAtMonotonicMs,
                    nowMs
                )
            val obstacleDataStale = snapshot.aircraftConnected &&
                PerceptionRecoveryPolicy.isSourceStale(
                    snapshot.perception.obstacleDistances.updatedAtMonotonicMs,
                    nowMs
                )
            val registrationMissing = !perceptionStarted
            if (registrationMissing || informationStale || obstacleDataStale) {
                if (nowMs >= perceptionNextRetryAtMonotonicMs) {
                    val staleSources = buildList {
                        if (informationStale) add("information")
                        if (obstacleDataStale) add("obstacle_data")
                    }.joinToString("+")
                    registerPerceptionListeners(
                        reason = if (registrationMissing) {
                            "registration_missing"
                        } else {
                            "sources_stale:$staleSources"
                        },
                        recoveryAttempt = true,
                        waitForFreshSources = snapshot.aircraftConnected
                    )
                }
            } else if (snapshot.aircraftConnected) {
                clearPerceptionRecoveryIssueIfHealthy(nowMs)
            } else {
                clearPerceptionRecoveryIssue()
            }
            if (telemetryStarted) {
                mainHandler.postDelayed(this, PerceptionRecoveryPolicy.CHECK_INTERVAL_MS)
            }
        }
    }
    private val operatorLocationListener = object : LocationListener {
        override fun onLocationChanged(location: Location) {
            refreshAndroidLocationReadiness()
        }

        override fun onProviderEnabled(provider: String) {
            refreshAndroidLocationReadiness()
        }

        override fun onProviderDisabled(provider: String) {
            refreshAndroidLocationReadiness()
        }

        @Deprecated("Required by LocationListener on older Android versions")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit
    }
    private val locationReadinessReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            refreshAndroidLocationReadiness()
        }
    }
    // These managers consult DJI's process context while their singleton is
    // created. Defer lookup until startTelemetry(), after SDKManager.init()
    // and successful app registration, so the bridge can start with no RC.
    private val remoteIdManager by lazy { UASRemoteIDManager.getInstance() }
    private val remoteIdStatusListener = object : UASRemoteIDStatusListener {
        override fun onUpdate(status: UASRemoteIDStatus) {
            publishRemoteIdStatus(status)
        }
    }
    private val deviceHealthManager by lazy { DeviceHealthManager.getInstance() }
    private val deviceHealthListener = object : DJIDeviceHealthInfoChangeListener {
        override fun onDeviceHealthInfoUpdate(infos: List<DJIDeviceHealthInfo>) {
            publishDeviceHealth(infos)
        }
    }
    private val perceptionManager by lazy { PerceptionManager.getInstance() }
    @Suppress("DEPRECATION")
    private val perceptionInformationListener = object : PerceptionInformationListener {
        override fun onUpdate(info: PerceptionInfo) {
            AircraftTelemetryState.updatePerceptionInformation(
                forwardWorking = info.forwardObstacleAvoidanceWorking,
                backwardWorking = info.backwardObstacleAvoidanceWorking,
                leftWorking = info.leftSideObstacleAvoidanceWorking,
                rightWorking = info.rightSideObstacleAvoidanceWorking,
                upwardWorking = info.upwardObstacleAvoidanceWorking,
                downwardWorking = info.downwardObstacleAvoidanceWorking,
                overallObstacleAvoidanceEnabled = info.isOverallObstacleAvoidanceEnabled,
                horizontalObstacleAvoidanceEnabled = info.isHorizontalObstacleAvoidanceEnabled,
                upwardObstacleAvoidanceEnabled = info.isUpwardObstacleAvoidanceEnabled,
                downwardObstacleAvoidanceEnabled = info.isDownwardObstacleAvoidanceEnabled,
                obstacleAvoidanceType = info.obstacleAvoidanceType?.name ?: "unknown",
                horizontalWarningDistanceMeters =
                    info.horizontalObstacleAvoidanceWarningDistance,
                upwardWarningDistanceMeters = info.upwardObstacleAvoidanceWarningDistance,
                downwardWarningDistanceMeters = info.downwardObstacleAvoidanceWarningDistance,
                horizontalBrakingDistanceMeters =
                    info.horizontalObstacleAvoidanceBrakingDistance,
                upwardBrakingDistanceMeters = info.upwardObstacleAvoidanceBrakingDistance,
                downwardBrakingDistanceMeters = info.downwardObstacleAvoidanceBrakingDistance,
                visionPositioningEnabled = info.isVisionPositioningEnabled,
                precisionLandingEnabled = info.isPrecisionLandingEnabled
            )
            onPerceptionTelemetrySample()
        }
    }
    private val obstacleDataListener = object : ObstacleDataListener {
        override fun onUpdate(data: ObstacleData) {
            AircraftTelemetryState.updateObstacleDistances(
                horizontalDistancesMillimeters = data.horizontalObstacleDistance,
                horizontalAngleIntervalDegrees = data.horizontalAngleInterval,
                upwardDistanceMillimeters = data.upwardObstacleDistance,
                downwardDistanceMillimeters = data.downwardObstacleDistance
            )
            onPerceptionTelemetrySample()
        }
    }
    private val telemetryVirtualStickListener = object : VirtualStickStateListener {
        override fun onVirtualStickStateUpdate(stickState: VirtualStickState) {
            AircraftTelemetryState.updateVirtualStickState(
                enabled = stickState.isVirtualStickEnable,
                advancedModeEnabled = stickState.isVirtualStickAdvancedModeEnabled,
                owner = stickState.currentFlightControlAuthorityOwner.name
            )
        }

        override fun onChangeReasonUpdate(reason: FlightControlAuthorityChangeReason) {
            AircraftTelemetryState.updateAuthorityChangeReason(reason.name)
        }
    }

    fun start() {
        refreshAndroidLocationReadiness()
        ContextCompat.registerReceiver(
            app,
            locationReadinessReceiver,
            IntentFilter(LocationManager.PROVIDERS_CHANGED_ACTION),
            ContextCompat.RECEIVER_NOT_EXPORTED
        )
        locationReceiverRegistered = true
        server.start()
        videoRelay.start()
        realtimeControl.start()
        telemetry.start()
        initializeDjiSdk()
    }

    fun onControlLinkConnected(source: String) {
        // SDK "product" connectivity is already true with only the RC-N2.
        // Reconcile all three live-path signals on every positive callback so
        // callback ordering cannot leave the gate falsely open or closed.
        if (CameraGimbalConnectionPolicy.isUsableAircraftLink(
                aircraftConnected = BridgeState.aircraftConnected.get(),
                remoteControllerConnected = BridgeState.remoteControllerConnected.get(),
                airLinkConnected = BridgeState.airLinkConnected.get()
            )
        ) {
            cameraGimbal.onLinkConnected(source)
        }
    }

    fun onControlLinkDisconnected(source: String) {
        controller.onControlLinkDisconnected(source)
        cameraGimbal.onLinkDisconnected(source)
    }

    private fun initializeDjiSdk() {
        SDKManager.getInstance().init(app, object : SDKManagerCallback {
            override fun onInitProcess(event: DJISDKInitEvent, totalProcess: Int) {
                if (event == DJISDKInitEvent.START_TO_INITIALIZE ||
                    event == DJISDKInitEvent.INITIALIZE_COMPLETE
                ) {
                    configureDjiBackgroundReporting()
                }
                BridgeState.lastEvent.set("sdk_init:$event:$totalProcess")
                if (event == DJISDKInitEvent.INITIALIZE_COMPLETE) {
                    BridgeState.sdkInitialized.set(true)
                    SDKManager.getInstance().registerApp()
                }
            }

            override fun onRegisterSuccess() {
                /*
                 * BaseManager.initBaseManagers() unconditionally starts
                 * MediaManager and CameraStreamManager before this callback.
                 * MediaManager then installs a YUV frame listener, which claims
                 * DJI video channel 0 and starts the HEVC decoder. Quiesce that
                 * unused pipeline before installing our pre-decoder observer.
                 */
                CameraStreamManager.getInstance().destroy()
                videoRelay.ensureMainCameraObserver(force = true)
                BridgeState.sdkRegistered.set(true)
                BridgeState.lastEvent.set("sdk_registered")
                startTelemetry()
            }

            override fun onRegisterFailure(error: IDJIError) {
                BridgeState.sdkRegistered.set(false)
                BridgeState.lastEvent.set("sdk_registration_failed:$error")
            }

            override fun onProductConnect(productId: Int) {
                BridgeState.productConnected.set(true)
                onControlLinkConnected("product")
                BridgeState.lastEvent.set("product_connected:$productId")
                videoRelay.ensureMainCameraObserver(force = true)
            }

            override fun onProductDisconnect(productId: Int) {
                BridgeState.productConnected.set(false)
                BridgeState.aircraftConnected.set(false)
                AircraftTelemetryState.updateAircraftConnection(false)
                onControlLinkDisconnected("product")
                videoRelay.detachMainCameraObserver()
                BridgeState.lastEvent.set("product_disconnected:$productId")
            }

            override fun onProductChanged(productId: Int) {
                BridgeState.lastEvent.set("product_changed:$productId")
            }

            override fun onDatabaseDownloadProgress(current: Long, total: Long) {
                BridgeState.lastEvent.set("database:$current/$total")
            }
        })

        DJINetworkManager.getInstance().addNetworkStatusListener { available ->
            if (available && BridgeState.sdkInitialized.get() && !SDKManager.getInstance().isRegistered) {
                SDKManager.getInstance().registerApp()
            }
        }
    }

    /**
     * Apply DJI's documented product-improvement opt-out before its analytics
     * engine initializes. MSDK emits START_TO_INITIALIZE after ContextUtil is
     * ready but before DataProtectionManager/AnalyticsEngine initialization;
     * the completion call is an idempotent verification guard.
     */
    private fun configureDjiBackgroundReporting() {
        BridgeState.djiProductImprovementOptOutRequested.set(true)
        runCatching {
            DataProtectionManager.getInstance().apply {
                agreeToProductImprovement(false)
                BridgeState.djiProductImprovementAgreed.set(
                    isAgreeToProductImprovement
                )
            }
            BridgeState.djiPrivacyConfigurationError.set(null)
        }.onFailure { error ->
            BridgeState.djiProductImprovementAgreed.set(null)
            BridgeState.djiPrivacyConfigurationError.set(
                "${error.javaClass.simpleName}:${error.message}"
            )
        }
    }

    private fun registerPerceptionListeners(
        reason: String,
        recoveryAttempt: Boolean,
        waitForFreshSources: Boolean
    ) {
        val nowMs = SystemClock.elapsedRealtime()
        if (recoveryAttempt) {
            BridgeState.perceptionListenerRecoveryAttempts.incrementAndGet()
        }
        BridgeState.perceptionListenerLastAttemptMonotonicMs.set(nowMs)
        try {
            if (perceptionStarted) {
                perceptionManager.removePerceptionInformationListener(
                    perceptionInformationListener
                )
                perceptionManager.removeObstacleDataListener(obstacleDataListener)
                perceptionStarted = false
            }
            if (!perceptionManagerInitialized) {
                perceptionManager.init()
                perceptionManagerInitialized = true
            }
            perceptionManager.addPerceptionInformationListener(perceptionInformationListener)
            perceptionManager.addObstacleDataListener(obstacleDataListener)
            perceptionStarted = true
            BridgeState.perceptionListenersRegistered.set(true)
            BridgeState.perceptionListenerLastSuccessMonotonicMs.set(nowMs)
            if (waitForFreshSources) {
                recordPerceptionRecoveryIssue(
                    nowMs,
                    "awaiting_fresh_sources_after_reregister:$reason"
                )
            } else {
                clearPerceptionRecoveryIssue()
            }
            if (recoveryAttempt) {
                BridgeState.lastEvent.set("perception_listeners_reregistered:$reason")
            }
        } catch (error: Exception) {
            runCatching {
                perceptionManager.removePerceptionInformationListener(
                    perceptionInformationListener
                )
                perceptionManager.removeObstacleDataListener(obstacleDataListener)
                perceptionManager.destroy()
            }
            perceptionStarted = false
            perceptionManagerInitialized = false
            BridgeState.perceptionListenersRegistered.set(false)
            recordPerceptionRecoveryIssue(
                nowMs,
                "${error.javaClass.simpleName}:${error.message}"
            )
            BridgeState.lastEvent.set(
                "perception_telemetry_start_failed:${error.javaClass.simpleName}:${error.message}"
            )
        }
    }

    private fun onPerceptionTelemetrySample() {
        val nowMs = SystemClock.elapsedRealtime()
        mainHandler.post {
            if (telemetryStarted) clearPerceptionRecoveryIssueIfHealthy(nowMs)
        }
    }

    private fun clearPerceptionRecoveryIssueIfHealthy(nowMs: Long) {
        val snapshot = AircraftTelemetryState.snapshot()
        if (!snapshot.aircraftConnected) return
        if (PerceptionRecoveryPolicy.isSourceStale(
                snapshot.perception.information.updatedAtMonotonicMs,
                nowMs
            ) || PerceptionRecoveryPolicy.isSourceStale(
                snapshot.perception.obstacleDistances.updatedAtMonotonicMs,
                nowMs
            )
        ) return
        clearPerceptionRecoveryIssue()
    }

    private fun clearPerceptionRecoveryIssue() {
        perceptionConsecutiveIssues = 0
        perceptionNextRetryAtMonotonicMs = 0L
        BridgeState.perceptionListenerConsecutiveIssues.set(0)
        BridgeState.perceptionListenerNextRetryMonotonicMs.set(-1L)
        BridgeState.perceptionListenerLastError.set(null)
    }

    private fun recordPerceptionRecoveryIssue(nowMs: Long, issue: String) {
        perceptionConsecutiveIssues = (perceptionConsecutiveIssues + 1).coerceAtMost(1_000)
        perceptionNextRetryAtMonotonicMs = nowMs +
            PerceptionRecoveryPolicy.retryDelayMillis(perceptionConsecutiveIssues)
        BridgeState.perceptionListenerConsecutiveIssues.set(perceptionConsecutiveIssues)
        BridgeState.perceptionListenerNextRetryMonotonicMs.set(
            perceptionNextRetryAtMonotonicMs
        )
        BridgeState.perceptionListenerLastError.set(issue)
    }

    private fun startTelemetry() {
        if (telemetryStarted) return
        telemetryStarted = true

        remoteIdManager.addUASRemoteIDStatusListener(remoteIdStatusListener)
        runCatching { publishRemoteIdStatus(remoteIdManager.uasRemoteIDStatus) }
        deviceHealthManager.addDJIDeviceHealthInfoChangeListener(deviceHealthListener)
        runCatching { publishDeviceHealth(deviceHealthManager.currentDJIDeviceHealthInfos) }
        registerPerceptionListeners(
            reason = "telemetry_start",
            recoveryAttempt = false,
            waitForFreshSources = false
        )
        mainHandler.removeCallbacks(perceptionRecoveryWatchdog)
        mainHandler.post(perceptionRecoveryWatchdog)

        ProductKey.KeyProductType.create().listen(this) {
            BridgeState.productType.set(it?.name ?: "unknown")
        }
        ProductKey.KeyFirmwareVersion.create().listen(this) {
            BridgeState.productFirmware.set(it ?: "unknown")
        }
        RemoteControllerKey.KeyConnection.create().listen(this) { connected ->
            val isConnected = connected == true
            BridgeState.remoteControllerConnected.set(isConnected)
            AircraftTelemetryState.updateRemoteControllerConnection(isConnected)
            if (isConnected) {
                onControlLinkConnected("remote_controller")
                RemoteControllerKey.KeyFirmwareVersion.create().get({ version ->
                    BridgeState.remoteControllerFirmware.set(version ?: "unknown")
                }, { error ->
                    BridgeState.remoteControllerFirmware.set("unavailable")
                    BridgeState.lastEvent.set("rc_firmware_failed:$error")
                })
            } else {
                BridgeState.remoteControllerFirmware.set("unknown")
                onControlLinkDisconnected("remote_controller")
            }
        }
        RemoteControllerKey.KeyPairingStatus.create().listen(this) {
            BridgeState.pairingState.set(it?.name ?: "unknown")
        }
        RemoteControllerKey.KeyStickLeftHorizontal.create().listen(this) { value ->
            AircraftTelemetryState.updateRemoteControllerStick(
                RemoteControllerAxis.LEFT_HORIZONTAL,
                value
            )
        }
        RemoteControllerKey.KeyStickLeftVertical.create().listen(this) { value ->
            AircraftTelemetryState.updateRemoteControllerStick(
                RemoteControllerAxis.LEFT_VERTICAL,
                value
            )
        }
        RemoteControllerKey.KeyStickRightHorizontal.create().listen(this) { value ->
            AircraftTelemetryState.updateRemoteControllerStick(
                RemoteControllerAxis.RIGHT_HORIZONTAL,
                value
            )
        }
        RemoteControllerKey.KeyStickRightVertical.create().listen(this) { value ->
            AircraftTelemetryState.updateRemoteControllerStick(
                RemoteControllerAxis.RIGHT_VERTICAL,
                value
            )
        }
        RemoteControllerKey.KeyLeftDial.create().listen(this) { value ->
            AircraftTelemetryState.updateRemoteControllerStick(RemoteControllerAxis.LEFT_DIAL, value)
        }
        RemoteControllerKey.KeyShutterButtonDown.create().listen(this) { down ->
            AircraftTelemetryState.updateRemoteControllerButton(
                RemoteControllerButton.SHUTTER,
                down
            )
        }
        RemoteControllerKey.KeyRecordButtonDown.create().listen(this) { down ->
            AircraftTelemetryState.updateRemoteControllerButton(
                RemoteControllerButton.RECORD,
                down
            )
        }
        RemoteControllerKey.KeyGoHomeButtonDown.create().listen(this) { down ->
            AircraftTelemetryState.updateRemoteControllerButton(
                RemoteControllerButton.GO_HOME,
                down
            )
        }
        RemoteControllerKey.KeyRCSwitchButtonDown.create().listen(this) { down ->
            AircraftTelemetryState.updateRemoteControllerButton(
                RemoteControllerButton.CAMERA_MODE_SWITCH,
                down
            )
        }
        RemoteControllerKey.KeyCustomButton1Down.create().listen(this) { down ->
            AircraftTelemetryState.updateRemoteControllerButton(
                RemoteControllerButton.CUSTOM_1,
                down
            )
        }
        RemoteControllerKey.KeyBatteryInfo.create().listen(this) { battery ->
            AircraftTelemetryState.updateRemoteControllerBattery(
                enabled = battery?.enabled,
                batteryPowerRaw = battery?.batteryPower,
                batteryPercent = battery?.batteryPercent
            )
        }
        AirLinkKey.KeyConnection.create().listen(this) { connected ->
            val isConnected = connected == true
            BridgeState.airLinkConnected.set(isConnected)
            if (isConnected) {
                onControlLinkConnected("airlink")
                videoRelay.ensureMainCameraObserver(force = true)
            } else {
                onControlLinkDisconnected("airlink")
            }
        }
        AirLinkKey.KeySignalQuality.create().listen(this) {
            BridgeState.airLinkSignalQuality.set(it ?: -1)
        }
        AirLinkKey.KeyVideoDataRate.create().listen(this) {
            BridgeState.videoDataRate.set(it ?: Double.NaN)
        }
        AirLinkKey.KeyLiveVideoSource.create().listen(this) { sources ->
            BridgeState.liveVideoSources.set(sources?.joinToString(";") ?: "unknown")
            BridgeState.channelCodecFormat.set("aircraft-native raw channel 0")
        }
        FlightControllerKey.KeyConnection.create().listen(this) { connected ->
            val isConnected = connected == true
            BridgeState.aircraftConnected.set(isConnected)
            if (connected != null) {
                AircraftTelemetryState.updateAircraftConnection(connected)
                mainHandler.removeCallbacks(refreshConnectionScopedHealth)
                mainHandler.removeCallbacks(refreshTakeoffTelemetry)
                if (connected) {
                    onControlLinkConnected("flight_controller")
                    // Device-health and RID managers can emit a cached pre-connect
                    // snapshot. Refresh after the new FC session has settled.
                    mainHandler.postDelayed(
                        refreshConnectionScopedHealth,
                        CONNECTION_HEALTH_REFRESH_DELAY_MILLIS
                    )
                    mainHandler.post(refreshTakeoffTelemetry)
                } else {
                    onControlLinkDisconnected("flight_controller")
                }
            }
        }
        FlightControllerKey.KeyIsFlying.create().listen(this) { isFlying ->
            if (isFlying != null) {
                AircraftTelemetryState.updateFlightState(isFlying = isFlying)
                controller.onFlightStateObserved()
            }
        }
        FlightControllerKey.KeyFlightTimeInSeconds.create().listen(this) { deciseconds ->
            AircraftTelemetryState.updateFlightState(flightTimeDeciseconds = deciseconds)
        }
        FlightControllerKey.KeyAreMotorsOn.create().listen(this) { motorsOn ->
            val areMotorsOn = motorsOn == true
            BridgeState.motorsOn.set(areMotorsOn)
            if (motorsOn != null) {
                AircraftTelemetryState.updateFlightState(motorsOn = motorsOn)
                controller.onFlightStateObserved()
            }
        }
        FlightControllerKey.KeyFlightMode.create().listen(this) { mode ->
            val name = mode?.name ?: "unknown"
            BridgeState.flightMode.set(name)
            AircraftTelemetryState.updateFlightState(flightMode = name)
        }
        FlightControllerKey.KeyAircraftLocation3D.create().listen(this) { location ->
            BridgeState.aircraftLocation.set(location?.toString() ?: "unknown")
            val latitude = location?.latitude
            val longitude = location?.longitude
            val altitude = location?.altitude
            if (latitude != null && longitude != null && altitude != null) {
                // KeyAltitude is not declared in the Mini 4 Pro capability file.
                // KeyAircraftLocation3D is its supported numeric altitude source.
                BridgeState.altitudeMeters.set(altitude)
                AircraftTelemetryState.updateLocation(latitude, longitude, altitude)
            } else {
                AircraftTelemetryState.updateLocationUnavailable("location_value_missing")
            }
        }
        FlightControllerKey.KeyAircraftVelocity.create().listen(this) { velocity ->
            val north = velocity?.x
            val east = velocity?.y
            val down = velocity?.z
            if (north != null && east != null && down != null) {
                AircraftTelemetryState.updateVelocity(north, east, down)
            } else {
                AircraftTelemetryState.updateVelocity(Double.NaN, Double.NaN, Double.NaN)
            }
        }
        FlightControllerKey.KeyAircraftAttitude.create().listen(this) { attitude ->
            val pitch = attitude?.pitch
            val roll = attitude?.roll
            val yaw = attitude?.yaw
            if (pitch != null && roll != null && yaw != null) {
                AircraftTelemetryState.updateAttitude(pitch, roll, yaw)
            } else {
                AircraftTelemetryState.updateAttitude(Double.NaN, Double.NaN, Double.NaN)
            }
        }
        FlightControllerKey.KeyGPSSatelliteCount.create().listen(this) { count ->
            AircraftTelemetryState.updateGpsSatelliteCount(count)
        }
        FlightControllerKey.KeyGPSSignalLevel.create().listen(this) { level ->
            AircraftTelemetryState.updateGpsSignalLevel(level?.name ?: "unknown")
        }
        FlightControllerKey.KeyCompassCount.create().listen(this) { count ->
            AircraftTelemetryState.updateCompassCount(count)
        }
        FlightControllerKey.KeyCompassHeading.create().listen(this) { headingDegrees ->
            AircraftTelemetryState.updateCompassHeading(headingDegrees)
        }
        FlightControllerKey.KeyCompassHasError.create().listen(this) { hasError ->
            AircraftTelemetryState.updateCompassError(hasError)
        }
        FlightControllerKey.KeyWindWarning.create().listen(this) { warning ->
            AircraftTelemetryState.updateWindWarning(warning?.name ?: "unknown")
        }
        FlightControllerKey.KeyWindSpeed.create().listen(this) { speedDecimetersPerSecond ->
            AircraftTelemetryState.updateWindSpeed(speedDecimetersPerSecond)
        }
        FlightControllerKey.KeyWindDirection.create().listen(this) { direction ->
            AircraftTelemetryState.updateWindDirection(direction?.name ?: "unknown")
        }
        FlightControllerKey.KeyIMUCount.create().listen(this) { count ->
            AircraftTelemetryState.updateImuCount(count)
        }
        FlightControllerKey.KeyIMUCalibrationInfo.create().listen(this) { info ->
            AircraftTelemetryState.updateImuCalibration(
                orientationCalibrationState =
                    info?.orientationCalibrationState?.name ?: "unknown",
                calibrationState = info?.calibrationState?.name ?: "unknown",
                calibrationProgressPercent = info?.calibrationProgress,
                orientationsToCalibrate = info?.orientationsToCalibrate?.map { it.name },
                orientationsCalibrated = info?.orientationsCalibrated?.map { it.name }
            )
        }
        GimbalKey.KeyGimbalAttitude.create(ComponentIndexType.LEFT_OR_MAIN).listen(this) { attitude ->
            AircraftTelemetryState.updateGimbalAttitude(
                pitchDegrees = attitude?.pitch,
                rollDegrees = attitude?.roll,
                yawDegrees = attitude?.yaw
            )
        }
        GimbalKey.KeyYawRelativeToAircraftHeading.create(
            ComponentIndexType.LEFT_OR_MAIN
        ).listen(this) { yawDegrees ->
            AircraftTelemetryState.updateGimbalYawRelativeToAircraftHeading(yawDegrees)
        }
        FlightControllerKey.KeyIsHomeLocationSet.create().listen(this) { isSet ->
            if (isSet != null) AircraftTelemetryState.updateHomeLocationSet(isSet)
        }
        FlightControllerKey.KeyHomeLocation.create().listen(this) { home ->
            val latitude = home?.latitude
            val longitude = home?.longitude
            if (latitude != null && longitude != null) {
                AircraftTelemetryState.updateHomeLocation(latitude, longitude)
            }
        }
        FlightControllerKey.KeyGoHomeStatus.create().listen(this) { status ->
            AircraftTelemetryState.updateGoHomeStatus(status?.name ?: "unknown")
        }
        FlightControllerKey.KeyGoHomeHeight.create().listen(this) { heightMeters ->
            AircraftTelemetryState.updateGoHomeHeight(heightMeters)
        }
        FlightControllerKey.KeyGoHomeHeightRange.create().listen(this) { range ->
            AircraftTelemetryState.updateGoHomeHeightRange(
                minimumMeters = range?.min,
                maximumMeters = range?.max,
                defaultMeters = range?.defaultValue
            )
        }
        FlightControllerKey.KeyIsFailSafe.create().listen(this) { active ->
            if (active != null) AircraftTelemetryState.updateFlightControllerFailsafe(active)
        }
        FlightControllerKey.KeyFailsafeAction.create().listen(this) { action ->
            AircraftTelemetryState.updateFailsafeAction(action?.name ?: "unknown")
        }
        FlightControllerKey.KeyLowBatteryRTHInfo.create().listen(this) { info ->
            if (info != null) {
                AircraftTelemetryState.updateLowBatteryRth(
                    batteryPercentNeededToGoHome = info.batteryPercentNeededToGoHome,
                    batteryPercentNeededToLand = info.batteryPercentNeededToLand,
                    remainingFlightTimeSeconds = info.remainingFlightTime,
                    timeNeededToGoHomeSeconds = info.timeNeededToGoHome,
                    timeNeededToLandSeconds = info.timeNeededToLand,
                    status = info.lowBatteryRTHStatus?.name ?: "unknown"
                )
            }
        }
        FlightControllerKey.KeyIsLowBatteryWarning.create().listen(this) { active ->
            if (active != null) AircraftTelemetryState.updateLowBatteryWarning(active)
        }
        FlightControllerKey.KeyIsSeriousLowBatteryWarning.create().listen(this) { active ->
            if (active != null) AircraftTelemetryState.updateSeriousLowBatteryWarning(active)
        }
        FlightControllerKey.KeyUltrasonicHeight.create().listen(this) { rawDecimeters ->
            AircraftTelemetryState.updateUltrasonicHeight(rawDecimeters)
        }
        FlightControllerKey.KeyIsLandingConfirmationNeeded.create().listen(this) { needed ->
            if (needed != null) AircraftTelemetryState.updateLandingConfirmationNeeded(needed)
        }
        FlightControllerKey.KeyRemoteControllerFlightMode.create().listen(this) { mode ->
            AircraftTelemetryState.updateRemoteControllerFlightMode(mode?.name ?: "unknown")
        }
        FlightControllerKey.KeyIsNearHeightLimit.create().listen(this) { reached ->
            if (reached != null) AircraftTelemetryState.updateNearHeightLimit(reached)
        }
        FlightControllerKey.KeyIsNearDistanceLimit.create().listen(this) { reached ->
            if (reached != null) AircraftTelemetryState.updateNearDistanceLimit(reached)
        }
        FlightAssistantKey.KeyLandingProtectionState.create().let { key ->
            if (key.isKeySupported()) {
                key.listen(this) { state ->
                    AircraftTelemetryState.updateLandingProtectionState(
                        state?.name ?: "unknown"
                    )
                }
            }
        }

        BatteryKey.KeyConnection.create().listen(this) { connected ->
            if (connected != null) AircraftTelemetryState.updateBatteryConnection(connected)
        }
        BatteryKey.KeyChargeRemainingInPercent.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryChargePercent(value)
        }
        BatteryKey.KeyChargeRemaining.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryChargeRemaining(value)
        }
        BatteryKey.KeyFullChargeCapacity.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryFullChargeCapacity(value)
        }
        BatteryKey.KeyVoltage.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryVoltage(value)
        }
        BatteryKey.KeyCurrent.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryCurrent(value)
        }
        BatteryKey.KeyBatteryTemperature.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryTemperature(value)
        }
        BatteryKey.KeyCellVoltages.create().listen(this) { values ->
            AircraftTelemetryState.updateBatteryCellVoltages(values)
        }
        BatteryKey.KeyNumberOfDischarges.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryNumberOfDischarges(value)
        }
        BatteryKey.KeyNumberOfCells.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryNumberOfCells(value)
        }
        BatteryKey.KeyBatteryManufacturedDate.create().listen(this) { date ->
            AircraftTelemetryState.updateBatteryManufacturedDate(
                year = date?.year,
                month = date?.month,
                day = date?.day
            )
        }
        BatteryKey.KeySerialNumber.create().listen(this) { value ->
            AircraftTelemetryState.updateBatterySerialNumber(value)
        }
        BatteryKey.KeyFirmwareVersion.create().listen(this) { value ->
            AircraftTelemetryState.updateBatteryFirmwareVersion(value)
        }

        // Unlike the generic authority keys, this listener is declared by the
        // Mini 4 Pro virtual-stick capability and preserves the takeover reason.
        VirtualStickManager.getInstance().setVirtualStickStateListener(
            telemetryVirtualStickListener
        )
    }

    fun stop() {
        mainHandler.removeCallbacks(refreshConnectionScopedHealth)
        mainHandler.removeCallbacks(refreshTakeoffTelemetry)
        mainHandler.removeCallbacks(perceptionRecoveryWatchdog)
        server.stop()
        videoRelay.stop()
        realtimeControl.stop()
        telemetry.stop()
        controller.close()
        perceptionConfig.close()
        cameraGimbal.close()
        if (telemetryStarted) {
            remoteIdManager.removeUASRemoteIDStatusListener(remoteIdStatusListener)
            deviceHealthManager.removeDJIDeviceHealthInfoChangeListener(deviceHealthListener)
            if (perceptionStarted || perceptionManagerInitialized) {
                runCatching {
                    if (perceptionStarted) {
                        perceptionManager.removePerceptionInformationListener(
                            perceptionInformationListener
                        )
                        perceptionManager.removeObstacleDataListener(obstacleDataListener)
                    }
                    perceptionManager.destroy()
                }
                perceptionStarted = false
                perceptionManagerInitialized = false
                BridgeState.perceptionListenersRegistered.set(false)
            }
            VirtualStickManager.getInstance().removeVirtualStickStateListener(
                telemetryVirtualStickListener
            )
            telemetryStarted = false
        }
        if (locationReceiverRegistered) {
            app.unregisterReceiver(locationReadinessReceiver)
            locationReceiverRegistered = false
        }
        runCatching { locationManager.removeUpdates(operatorLocationListener) }
        requestedLocationProviders.clear()
        KeyManager.getInstance().cancelListen(this)
        SDKManager.getInstance().destroy()
    }

    fun refreshAndroidLocationReadiness() {
        val coarseGranted = ContextCompat.checkSelfPermission(
            app,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        val fineGranted = ContextCompat.checkSelfPermission(
            app,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        updateOperatorLocationSubscriptions()
        // Mirror MSDK 5.18 LocationUtil.isLocationEnabled(): DJI requires both
        // permissions and specifically an enabled GPS or network provider.
        val providerEnabled = coarseGranted && fineGranted && runCatching {
            locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        }.getOrDefault(false)
        // Mirror MSDK 5.18 LocationUtil.getLastLocation(). DJI selects the
        // enabled-provider fix with the smallest reported accuracy, not the
        // newest fix. Readiness must judge the exact sample DJI will consume.
        val lastKnownLocation = if (coarseGranted && fineGranted) {
            runCatching {
                locationManager.getProviders(true)
                    .mapNotNull { provider -> locationManager.getLastKnownLocation(provider) }
                    .minByOrNull { location -> location.accuracy }
            }.getOrNull()
        } else {
            null
        }
        val lastKnownLocationValid = lastKnownLocation != null &&
            lastKnownLocation.latitude.isFinite() &&
            lastKnownLocation.longitude.isFinite() &&
            kotlin.math.abs(lastKnownLocation.latitude) > 1e-6 &&
            kotlin.math.abs(lastKnownLocation.longitude) > 1e-6 &&
            lastKnownLocation.latitude in -90.0..90.0 &&
            lastKnownLocation.longitude in -180.0..180.0
        AircraftTelemetryState.updateAndroidLocationReadiness(
            coarsePermissionGranted = coarseGranted,
            finePermissionGranted = fineGranted,
            locationProviderEnabled = providerEnabled,
            lastKnownLocationAvailable = lastKnownLocationValid,
            lastKnownLocationProvider = lastKnownLocation?.provider?.takeIf {
                lastKnownLocationValid
            },
            lastKnownLocationAccuracyMeters = if (
                lastKnownLocationValid && lastKnownLocation?.hasAccuracy() == true
            ) lastKnownLocation.accuracy else null,
            lastKnownLocationElapsedRealtimeMs = lastKnownLocation?.elapsedRealtimeNanos
                ?.takeIf { lastKnownLocationValid }
                ?.div(1_000_000L),
            lastKnownLocationMock = lastKnownLocation?.takeIf {
                lastKnownLocationValid
            }?.let { location ->
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    location.isMock
                } else {
                    @Suppress("DEPRECATION")
                    location.isFromMockProvider
                }
            }
        )
    }

    @Synchronized
    private fun updateOperatorLocationSubscriptions() {
        // Keep the check adjacent to requestLocationUpdates so lint and a
        // runtime permission revocation both fail closed.
        if (ContextCompat.checkSelfPermission(
                app,
                Manifest.permission.ACCESS_COARSE_LOCATION
            ) != PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(
                app,
                Manifest.permission.ACCESS_FINE_LOCATION
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            if (requestedLocationProviders.isNotEmpty()) {
                runCatching { locationManager.removeUpdates(operatorLocationListener) }
                requestedLocationProviders.clear()
            }
            return
        }

        listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .filter { provider ->
                provider !in requestedLocationProviders && runCatching {
                    locationManager.allProviders.contains(provider) &&
                        locationManager.isProviderEnabled(provider)
                }.getOrDefault(false)
            }
            .forEach { provider ->
                val registered = runCatching {
                    locationManager.requestLocationUpdates(
                        provider,
                        1_000L,
                        1.0f,
                        operatorLocationListener,
                        Looper.getMainLooper()
                    )
                }.isSuccess
                if (registered) requestedLocationProviders += provider
            }
    }

    private fun publishRemoteIdStatus(status: UASRemoteIDStatus) {
        AircraftTelemetryState.updateRemoteIdStatus(
            broadcastEnabled = status.isBroadcastRemoteIdEnabled,
            workingState = status.remoteIdWorkingState?.name ?: "unknown"
        )
    }

    private fun refreshTakeoffTelemetrySnapshot() {
        BatteryKey.KeyConnection.create().get({ connected ->
            if (connected != null) AircraftTelemetryState.updateBatteryConnection(connected)
        }, {})
        BatteryKey.KeyChargeRemainingInPercent.create().get({ percent ->
            AircraftTelemetryState.updateBatteryChargePercent(percent)
        }, {})
        FlightControllerKey.KeyIsFlying.create().get({ isFlying ->
            if (isFlying != null) {
                AircraftTelemetryState.updateFlightState(isFlying = isFlying)
                controller.onFlightStateObserved()
            }
        }, {})
        FlightControllerKey.KeyAreMotorsOn.create().get({ motorsOn ->
            if (motorsOn != null) {
                BridgeState.motorsOn.set(motorsOn)
                AircraftTelemetryState.updateFlightState(motorsOn = motorsOn)
                controller.onFlightStateObserved()
            }
        }, {})
        FlightControllerKey.KeyIsFailSafe.create().get({ active ->
            if (active != null) AircraftTelemetryState.updateFlightControllerFailsafe(active)
        }, {})
        FlightControllerKey.KeyFailsafeAction.create().get({ action ->
            AircraftTelemetryState.updateFailsafeAction(action?.name ?: "unknown")
        }, {})
        FlightControllerKey.KeyRemoteControllerFlightMode.create().get({ mode ->
            AircraftTelemetryState.updateRemoteControllerFlightMode(mode?.name ?: "unknown")
        }, {})
        FlightControllerKey.KeyIsLowBatteryWarning.create().get({ active ->
            if (active != null) AircraftTelemetryState.updateLowBatteryWarning(active)
        }, {})
        FlightControllerKey.KeyIsSeriousLowBatteryWarning.create().get({ active ->
            if (active != null) AircraftTelemetryState.updateSeriousLowBatteryWarning(active)
        }, {})
    }

    private fun publishDeviceHealth(infos: List<DJIDeviceHealthInfo>) {
        val mapped = runCatching {
            infos.map { info ->
                DeviceHealthIssueTelemetry(
                    informationCode = info.informationCode() ?: "unknown",
                    warningLevel = info.warningLevel()?.name ?: "UNKNOWN",
                    componentId = info.componentId(),
                    sensorIndex = info.sensorIndex(),
                    title = info.title() ?: "",
                    description = info.description() ?: ""
                )
            }
        }.getOrElse { failure ->
            BridgeState.lastEvent.set(
                "device_health_mapping_failed:${failure.javaClass.simpleName}"
            )
            return
        }
        AircraftTelemetryState.updateDeviceHealth(mapped)
    }

    private companion object {
        const val CONNECTION_HEALTH_REFRESH_DELAY_MILLIS = 1_500L
        const val TAKEOFF_TELEMETRY_REFRESH_MILLIS = 5_000L
    }
}
