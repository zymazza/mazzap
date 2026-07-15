package com.veil.dji

import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

data class AcceptedControlPacket(
    val sessionHex: String,
    val sequenceHex: String,
    val sentAtControlMonotonicMs: Long,
    val receivedAtControlMonotonicMs: Long,
    val appliedAtControlMonotonicMs: Long,
    val latencyMs: Long,
    val receiveToApplyMs: Long,
    val setpoint: AcceptedControlSetpoint
)

data class AcceptedControlSetpoint(
    val mode: String,
    val values: Map<String, Number>
)

object BridgeState {
    val sdkInitialized = AtomicBoolean(false)
    val sdkRegistered = AtomicBoolean(false)
    val djiProductImprovementOptOutRequested = AtomicBoolean(false)
    val djiProductImprovementAgreed = AtomicReference<Boolean?>(null)
    val djiPrivacyConfigurationError = AtomicReference<String?>(null)
    val productConnected = AtomicBoolean(false)
    val remoteControllerConnected = AtomicBoolean(false)
    val remoteControllerFirmware = AtomicReference("unknown")
    val pairingState = AtomicReference("unknown")
    val airLinkConnected = AtomicBoolean(false)
    val aircraftConnected = AtomicBoolean(false)
    val productType = AtomicReference("unknown")
    val productFirmware = AtomicReference("unknown")
    val airLinkSignalQuality = AtomicInteger(-1)
    val videoDataRate = AtomicReference(Double.NaN)
    val videoSourceTimestampRaw = AtomicLong(Long.MIN_VALUE)
    val videoSourceTimestampDeltaRaw = AtomicLong(Long.MIN_VALUE)
    val videoCallbackMonotonicNs = AtomicLong(-1L)
    val videoCallbackIntervalUs = AtomicLong(-1L)
    val videoCallbackRateHz = AtomicReference(Double.NaN)
    val videoIngressBytesPerSecond = AtomicReference(Double.NaN)
    val videoTimestampSamples = AtomicLong(0L)
    val videoAccessUnits = AtomicLong(0L)
    val videoAccessUnitMonotonicNs = AtomicLong(-1L)
    val videoAccessUnitRateHz = AtomicReference(Double.NaN)
    val channelCodecFormat = AtomicReference("unknown")
    val liveVideoSources = AtomicReference("unknown")
    val motorsOn = AtomicBoolean(false)
    val virtualStickEnabled = AtomicBoolean(false)
    val virtualStickAdvancedMode = AtomicBoolean(false)
    val flightControlAuthority = AtomicReference("UNKNOWN")
    val virtualStickControlMode = AtomicReference("disabled")
    val controlFailsafeState = AtomicReference("disarmed")
    val controlSession = AtomicReference("unavailable")
    val controlSessionReason = AtomicReference("startup")
    val altitudeMeters = AtomicReference(Double.NaN)
    val flightMode = AtomicReference("unknown")
    val aircraftLocation = AtomicReference("unknown")
    val videoCodec = AtomicReference("unknown")
    val availableCameras = AtomicReference("unknown")
    val videoClients = AtomicInteger(0)
    val videoPendingClients = AtomicInteger(0)
    val videoBytes = AtomicLong(0L)
    val videoDroppedChunks = AtomicLong(0L)
    val videoNalUnits = AtomicLong(0L)
    val videoIFrameRequests = AtomicLong(0L)
    val videoIrapJoins = AtomicLong(0L)
    val videoIdrJoins = AtomicLong(0L)
    val videoClientQueueRejections = AtomicLong(0L)
    val telemetryClients = AtomicInteger(0)
    val telemetryDroppedUpdates = AtomicLong(0L)
    val telemetrySlowClientDisconnects = AtomicLong(0L)
    val perceptionListenersRegistered = AtomicBoolean(false)
    val perceptionListenerRecoveryAttempts = AtomicLong(0L)
    val perceptionListenerConsecutiveIssues = AtomicInteger(0)
    val perceptionListenerLastAttemptMonotonicMs = AtomicLong(-1L)
    val perceptionListenerLastSuccessMonotonicMs = AtomicLong(-1L)
    val perceptionListenerNextRetryMonotonicMs = AtomicLong(-1L)
    val perceptionListenerLastError = AtomicReference<String?>(null)
    val controlPackets = AtomicLong(0L)
    val controlRejectedPackets = AtomicLong(0L)
    val lastControlLatencyMs = AtomicLong(-1L)
    val lastAcceptedControlPacket = AtomicReference<AcceptedControlPacket?>(null)
    val lastControlAgeMs = AtomicLong(-1L)
    val lastEvent = AtomicReference("starting")

    fun recordAcceptedControlPacket(
        sessionId: Long,
        sequence: Long,
        sentAtControlMonotonicMs: Long,
        receivedAtControlMonotonicMs: Long,
        appliedAtControlMonotonicMs: Long,
        setpoint: AcceptedControlSetpoint
    ) {
        val latencyMs = ControlPacketFreshness.ageMillis(
            appliedAtControlMonotonicMs,
            sentAtControlMonotonicMs
        ).coerceAtLeast(0L)
        lastAcceptedControlPacket.set(
            AcceptedControlPacket(
                sessionHex = sessionId.toUnsignedHex(),
                sequenceHex = sequence.toUnsignedHex(),
                sentAtControlMonotonicMs = sentAtControlMonotonicMs,
                receivedAtControlMonotonicMs = receivedAtControlMonotonicMs,
                appliedAtControlMonotonicMs = appliedAtControlMonotonicMs,
                latencyMs = latencyMs,
                receiveToApplyMs =
                    (appliedAtControlMonotonicMs - receivedAtControlMonotonicMs)
                        .coerceAtLeast(0L),
                setpoint = setpoint
            )
        )
        lastControlLatencyMs.set(latencyMs)
    }

    fun clearAcceptedControlPacket() {
        lastAcceptedControlPacket.set(null)
        lastControlLatencyMs.set(-1L)
        lastControlAgeMs.set(-1L)
    }

    fun toJson(): JSONObject {
        // One immutable read keeps every acknowledgement field matched to the
        // same accepted datagram even while the UDP receiver publishes at 20 Hz.
        val acceptedControl = lastAcceptedControlPacket.get()
        return JSONObject()
        .put("sdk_initialized", sdkInitialized.get())
        .put("sdk_registered", sdkRegistered.get())
        .put(
            "dji_product_improvement_opt_out_requested",
            djiProductImprovementOptOutRequested.get()
        )
        .put(
            "dji_product_improvement_agreed",
            djiProductImprovementAgreed.get() ?: JSONObject.NULL
        )
        .put(
            "dji_privacy_configuration_error",
            djiPrivacyConfigurationError.get() ?: JSONObject.NULL
        )
        .put("product_connected", productConnected.get())
        .put("remote_controller_connected", remoteControllerConnected.get())
        .put("remote_controller_firmware", remoteControllerFirmware.get())
        .put("pairing_state", pairingState.get())
        .put("airlink_connected", airLinkConnected.get())
        .put("aircraft_connected", aircraftConnected.get())
        .put("product_type", productType.get())
        .put("product_firmware", productFirmware.get())
        .put("airlink_signal_quality", airLinkSignalQuality.get())
        .put("video_data_rate", videoDataRate.get().takeUnless { it.isNaN() } ?: JSONObject.NULL)
        .put("video_source_timestamp_raw", videoSourceTimestampRaw.get().takeUnless { it == Long.MIN_VALUE } ?: JSONObject.NULL)
        .put("video_source_timestamp_delta_raw", videoSourceTimestampDeltaRaw.get().takeUnless { it == Long.MIN_VALUE } ?: JSONObject.NULL)
        .put("video_source_timestamp_unit", "unknown_dji_videobufferinfo")
        .put("video_callback_monotonic_ns", videoCallbackMonotonicNs.get().takeIf { it >= 0L } ?: JSONObject.NULL)
        .put("video_callback_age_ms", videoCallbackMonotonicNs.get().takeIf { it >= 0L }?.let {
            ((System.nanoTime() - it).coerceAtLeast(0L)) / 1_000_000.0
        } ?: JSONObject.NULL)
        .put("video_callback_interval_us", videoCallbackIntervalUs.get().takeIf { it >= 0L } ?: JSONObject.NULL)
        .put("video_callback_rate_hz", videoCallbackRateHz.get().takeUnless { it.isNaN() } ?: JSONObject.NULL)
        .put("video_ingress_bytes_per_second", videoIngressBytesPerSecond.get().takeUnless { it.isNaN() } ?: JSONObject.NULL)
        .put("video_timestamp_samples", videoTimestampSamples.get())
        .put("video_access_units", videoAccessUnits.get())
        .put("video_access_unit_monotonic_ns", videoAccessUnitMonotonicNs.get().takeIf { it >= 0L } ?: JSONObject.NULL)
        .put("video_access_unit_age_ms", videoAccessUnitMonotonicNs.get().takeIf { it >= 0L }?.let {
            ((System.nanoTime() - it).coerceAtLeast(0L)) / 1_000_000.0
        } ?: JSONObject.NULL)
        .put("video_access_unit_rate_hz", videoAccessUnitRateHz.get().takeUnless { it.isNaN() } ?: JSONObject.NULL)
        .put("channel_codec_format", channelCodecFormat.get())
        .put("live_video_sources", liveVideoSources.get())
        .put("motors_on", motorsOn.get())
        .put("virtual_stick_enabled", virtualStickEnabled.get())
        .put("virtual_stick_advanced_mode", virtualStickAdvancedMode.get())
        .put("flight_control_authority", flightControlAuthority.get())
        .put("virtual_stick_control_mode", virtualStickControlMode.get())
        .put("control_failsafe_state", controlFailsafeState.get())
        .put("control_packet_version", 2)
        .put("control_session", controlSession.get())
        .put("control_session_reason", controlSessionReason.get())
        .put("control_monotonic_ms", controlMonotonicMillis())
        .put("altitude_m", altitudeMeters.get().takeUnless { it.isNaN() } ?: JSONObject.NULL)
        .put("flight_mode", flightMode.get())
        .put("aircraft_location", aircraftLocation.get())
        .put("video_codec", videoCodec.get())
        .put("available_cameras", availableCameras.get())
        .put("video_clients", videoClients.get())
        .put("video_pending_clients", videoPendingClients.get())
        .put("video_bytes", videoBytes.get())
        .put("video_dropped_chunks", videoDroppedChunks.get())
        .put("video_nal_units", videoNalUnits.get())
        .put("video_iframe_requests", videoIFrameRequests.get())
        .put("video_irap_joins", videoIrapJoins.get())
        .put("video_idr_joins", videoIdrJoins.get())
        .put("video_client_queue_rejections", videoClientQueueRejections.get())
        .put("telemetry_clients", telemetryClients.get())
        .put("telemetry_dropped_updates", telemetryDroppedUpdates.get())
        .put("telemetry_slow_client_disconnects", telemetrySlowClientDisconnects.get())
        .put("perception_listener_recovery", JSONObject()
            .put("registered", perceptionListenersRegistered.get())
            .put("retry_attempts_total", perceptionListenerRecoveryAttempts.get())
            .put("consecutive_issues", perceptionListenerConsecutiveIssues.get())
            .put(
                "last_attempt_monotonic_ms",
                perceptionListenerLastAttemptMonotonicMs.get()
                    .takeIf { it >= 0L } ?: JSONObject.NULL
            )
            .put(
                "last_success_monotonic_ms",
                perceptionListenerLastSuccessMonotonicMs.get()
                    .takeIf { it >= 0L } ?: JSONObject.NULL
            )
            .put(
                "next_retry_monotonic_ms",
                perceptionListenerNextRetryMonotonicMs.get()
                    .takeIf { it >= 0L } ?: JSONObject.NULL
            )
            .put(
                "last_error",
                perceptionListenerLastError.get() ?: JSONObject.NULL
            ))
        .put("control_packets", controlPackets.get())
        .put("control_rejected_packets", controlRejectedPackets.get())
        .put("last_control_latency_ms", lastControlLatencyMs.get())
        .put(
            "last_control_session",
            acceptedControl?.sessionHex ?: JSONObject.NULL
        )
        .put(
            "last_control_sequence_hex",
            acceptedControl?.sequenceHex ?: JSONObject.NULL
        )
        .put(
            "last_control_sent_monotonic_ms",
            acceptedControl?.sentAtControlMonotonicMs ?: JSONObject.NULL
        )
        .put(
            "last_control_received_monotonic_ms",
            acceptedControl?.receivedAtControlMonotonicMs ?: JSONObject.NULL
        )
        .put(
            "last_control_applied_monotonic_ms",
            acceptedControl?.appliedAtControlMonotonicMs ?: JSONObject.NULL
        )
        .put(
            "last_control_receive_to_apply_ms",
            acceptedControl?.receiveToApplyMs ?: JSONObject.NULL
        )
        .put(
            "last_control_setpoint",
            acceptedControl?.setpoint?.let { setpoint ->
                JSONObject(setpoint.values).put("mode", setpoint.mode)
            } ?: JSONObject.NULL
        )
        .put("last_control_age_ms", lastControlAgeMs.get())
        .put("aircraft_telemetry", AircraftTelemetryState.toJson())
        .put("flight_test_readiness", BridgeCommandJournal.readinessJson())
        .put("flight_test_result", BridgeCommandJournal.flightTestResultJson())
        .put("command_journal", BridgeCommandJournal.statusJson())
        .put("last_event", lastEvent.get())
    }
}

private fun Long.toUnsignedHex(): String =
    java.lang.Long.toUnsignedString(this, 16).padStart(16, '0')

internal fun RealtimeControlCommand.toAcceptedSetpoint(): AcceptedControlSetpoint = when (this) {
    is RealtimeControlCommand.Sticks -> AcceptedControlSetpoint(
        mode = VirtualControlMode.STICKS.wireName,
        values = linkedMapOf(
            "left_horizontal" to leftHorizontal,
            "left_vertical" to leftVertical,
            "right_horizontal" to rightHorizontal,
            "right_vertical" to rightVertical
        )
    )

    is RealtimeControlCommand.BodyVelocity -> AcceptedControlSetpoint(
        mode = VirtualControlMode.BODY_VELOCITY.wireName,
        values = linkedMapOf(
            "forward_mps" to forwardMetersPerSecond,
            "right_mps" to rightMetersPerSecond,
            "up_mps" to upMetersPerSecond,
            "yaw_rate_deg_s" to yawRateDegreesPerSecond
        )
    )
}
