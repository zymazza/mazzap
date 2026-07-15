package com.veil.dji

import android.util.Log
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.value.flightcontroller.FlightControlAuthority
import dji.sdk.keyvalue.value.flightcontroller.FlightControlAuthorityChangeReason
import dji.sdk.keyvalue.value.flightcontroller.FlightCoordinateSystem
import dji.sdk.keyvalue.value.flightcontroller.FailsafeAction
import dji.sdk.keyvalue.value.flightcontroller.RollPitchControlMode
import dji.sdk.keyvalue.value.flightcontroller.VerticalControlMode
import dji.sdk.keyvalue.value.flightcontroller.VirtualStickFlightControlParam
import dji.sdk.keyvalue.value.flightcontroller.YawControlMode
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.et.action
import dji.v5.et.create
import dji.v5.et.set
import dji.v5.manager.aircraft.virtualstick.VirtualStickManager
import dji.v5.manager.aircraft.virtualstick.VirtualStickState
import dji.v5.manager.aircraft.virtualstick.VirtualStickStateListener
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

enum class VirtualControlMode(val wireName: String) {
    STICKS("sticks"),
    BODY_VELOCITY("body_velocity"),
    DISABLED("disabled")
}

/**
 * ScheduledThreadPoolExecutor suppresses all future executions when a periodic
 * task throws. This wrapper deliberately contains failures from both the tick
 * and its fail-safe handler so the deadman clock continues to run.
 */
internal class NonThrowingPeriodicTask(
    private val task: () -> Unit,
    private val onFailure: (Throwable) -> Unit
) : Runnable {
    override fun run() {
        try {
            task()
        } catch (failure: Throwable) {
            try {
                onFailure(failure)
            } catch (_: Throwable) {
                // A safety handler must never cancel the periodic scheduler.
            }
        }
    }
}

/** Pure policy shared by disconnect, landing, and unexpected-authority paths. */
internal object VirtualStickSafetyPolicy {
    fun releaseRequired(
        requestedMode: VirtualControlMode,
        previouslyHadMsdkAuthority: Boolean,
        virtualStickEnabled: Boolean,
        authorityOwner: String
    ): Boolean = requestedMode != VirtualControlMode.DISABLED ||
        previouslyHadMsdkAuthority ||
        virtualStickEnabled ||
        authorityOwner == FlightControlAuthority.MSDK.name

    fun authorityGrantIsUnexpected(requestedMode: VirtualControlMode): Boolean =
        requestedMode == VirtualControlMode.DISABLED

    fun releaseConfirmedToRc(
        requestedMode: VirtualControlMode,
        previouslyHadMsdkAuthority: Boolean,
        virtualStickEnabled: Boolean,
        authorityOwner: String
    ): Boolean = requestedMode == VirtualControlMode.DISABLED &&
        !previouslyHadMsdkAuthority &&
        !virtualStickEnabled &&
        authorityOwner == FlightControlAuthority.RC.name
}

/** Lock-free command gate used to prevent duplicate physical action dispatch. */
internal class SingleFlightCommandGate {
    private val activeCommandId = AtomicReference<String?>(null)

    fun tryAcquire(commandId: String): Boolean =
        activeCommandId.compareAndSet(null, commandId)

    fun release(commandId: String): Boolean =
        activeCommandId.compareAndSet(commandId, null)

    fun current(): String? = activeCommandId.get()
}

internal object TakeoffTimeoutReconciliationPolicy {
    const val MINIMUM_POST_TIMEOUT_GROUND_OBSERVATION_DELAY_MS = 3_000L

    fun freshGroundStateProvesNoTakeoff(
        timedOutAtMonotonicMs: Long,
        isFlyingUpdatedAtMonotonicMs: Long?,
        motorsOnUpdatedAtMonotonicMs: Long?,
        isFlying: Boolean,
        motorsOn: Boolean
    ): Boolean {
        val minimumProofTimeMs = timedOutAtMonotonicMs +
            MINIMUM_POST_TIMEOUT_GROUND_OBSERVATION_DELAY_MS
        return isFlyingUpdatedAtMonotonicMs != null &&
        motorsOnUpdatedAtMonotonicMs != null &&
        isFlyingUpdatedAtMonotonicMs >= minimumProofTimeMs &&
        motorsOnUpdatedAtMonotonicMs >= minimumProofTimeMs &&
        !isFlying &&
        !motorsOn
    }
}

/** Prevents mutually incompatible asynchronous flight actions from overlapping. */
internal object CrossActionGatePolicy {
    fun blocksTakeoff(
        enablePending: Boolean,
        disablePending: Boolean,
        landingPending: Boolean,
        landingConfirmationPending: Boolean
    ): Boolean = enablePending || disablePending || landingPending ||
        landingConfirmationPending

    fun blocksVirtualStickEnable(
        takeoffPending: Boolean,
        landingPending: Boolean,
        landingConfirmationPending: Boolean
    ): Boolean = takeoffPending || landingPending || landingConfirmationPending
}

/** A priority landing may invalidate a reserved takeoff before DJI sees it. */
internal object TakeoffDispatchPolicy {
    fun mayDispatch(
        reservedCommandId: String,
        activeCommandId: String?,
        landingPending: Boolean,
        landingConfirmationPending: Boolean
    ): Boolean = activeCommandId == reservedCommandId &&
        !landingPending && !landingConfirmationPending
}

/**
 * A land request is not a takeoff-cancellation primitive. It may overlap an
 * uncertain takeoff callback only after current-connection telemetry proves
 * that the aircraft is actually airborne.
 */
internal object LandingDispatchPolicy {
    fun mayReserve(
        takeoffPending: Boolean,
        connectionUpdatedAtMonotonicMs: Long?,
        isFlyingUpdatedAtMonotonicMs: Long?,
        isFlying: Boolean
    ): Boolean = !takeoffPending || (
        connectionUpdatedAtMonotonicMs != null &&
            isFlyingUpdatedAtMonotonicMs != null &&
            isFlyingUpdatedAtMonotonicMs >= connectionUpdatedAtMonotonicMs &&
            isFlying
        )
}

/** Preserve neutral transmission whenever an authority-release result is uncertain. */
internal object DisableNeutralRetentionPolicy {
    fun modeToRetain(
        capturedMode: VirtualControlMode?,
        persistentMode: VirtualControlMode?,
        releaseStillRequired: Boolean,
        releaseConfirmedToRc: Boolean,
        advancedModeObserved: Boolean
    ): VirtualControlMode? = capturedMode ?: persistentMode ?: if (
        releaseStillRequired || !releaseConfirmedToRc
    ) {
        if (advancedModeObserved) VirtualControlMode.BODY_VELOCITY else VirtualControlMode.STICKS
    } else {
        null
    }
}

/**
 * Serializes DJI control state and continuously emits the latest command at
 * 20 Hz. BODY_VELOCITY uses explicit SI units and DJI's advanced virtual-stick
 * velocity modes; STICKS preserves normalized RC-like control at DJI's native
 * basic-mode 5 Hz sampling rate.
 */
class FlightController {
    private val manager = VirtualStickManager.getInstance()
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private val requestedMode = AtomicReference(VirtualControlMode.DISABLED)
    private val latestCommand = AtomicReference<RealtimeControlCommand?>(null)
    private val lastCommandNanos = AtomicLong(0L)
    private val neutralSent = AtomicBoolean(true)
    private val disableRequested = AtomicBoolean(false)
    private val activeDisableCommandId = AtomicReference<String?>(null)
    private val failsafeNeutralMode = AtomicReference<VirtualControlMode?>(null)
    private val lastDisableAttemptNanos = AtomicLong(0L)
    private val hadMsdkAuthority = AtomicBoolean(false)
    private val closed = AtomicBoolean(false)
    /** Makes incompatible gate checks and reservations one atomic operation. */
    private val actionTransitionLock = Any()
    private val takeoffGate = SingleFlightCommandGate()
    private val takeoffTimedOutAtMonotonicMs = AtomicLong(-1L)
    private val enableGate = SingleFlightCommandGate()
    private val landingGate = SingleFlightCommandGate()
    private val confirmLandingGate = SingleFlightCommandGate()
    private val failsafeActionGate = SingleFlightCommandGate()

    private val stateListener = object : VirtualStickStateListener {
        override fun onVirtualStickStateUpdate(stickState: VirtualStickState) {
            val authority = stickState.currentFlightControlAuthorityOwner
            val hasAuthority = stickState.isVirtualStickEnable &&
                authority == FlightControlAuthority.MSDK
            BridgeState.virtualStickEnabled.set(stickState.isVirtualStickEnable)
            BridgeState.virtualStickAdvancedMode.set(stickState.isVirtualStickAdvancedModeEnabled)
            BridgeState.flightControlAuthority.set(authority.name)

            if (hasAuthority) {
                if (VirtualStickSafetyPolicy.authorityGrantIsUnexpected(requestedMode.get())) {
                    hadMsdkAuthority.set(true)
                    releaseUnexpectedAuthority()
                    return
                }
                if (failsafeNeutralMode.get() != null || disableRequested.get()) {
                    hadMsdkAuthority.set(true)
                    BridgeState.controlFailsafeState.set("disabling_neutral")
                    return
                }
                if (!hadMsdkAuthority.getAndSet(true)) {
                    // Start the arming timeout when DJI actually grants authority,
                    // not when the asynchronous enable request was submitted.
                    lastCommandNanos.set(System.nanoTime())
                    latestCommand.set(neutralCommand(requestedMode.get()))
                    neutralSent.set(false)
                }
                BridgeState.virtualStickControlMode.set(requestedMode.get().wireName)
                BridgeState.controlFailsafeState.set("armed_neutral")
            } else if (hadMsdkAuthority.getAndSet(false)) {
                handleAuthorityLoss("authority_${authority.name.lowercase()}")
            }
        }

        override fun onChangeReasonUpdate(reason: FlightControlAuthorityChangeReason) {
            BridgeState.lastEvent.set("virtual_stick_authority:$reason")
            if (reason != FlightControlAuthorityChangeReason.MSDK_REQUEST &&
                hadMsdkAuthority.getAndSet(false)
            ) {
                handleAuthorityLoss("authority_reason_${reason.name.lowercase()}")
            }
        }
    }

    init {
        manager.setVirtualStickStateListener(stateListener)
        // Fixed delay prevents Android from replaying a backlog of control
        // ticks in a burst after a process scheduling pause.
        scheduler.scheduleWithFixedDelay(
            NonThrowingPeriodicTask(::controlTick, ::handleControlTickFailure),
            CONTROL_PERIOD_MILLIS,
            CONTROL_PERIOD_MILLIS,
            TimeUnit.MILLISECONDS
        )
    }

    fun takeoff(): CommandRecord {
        val preconditions = BridgeCommandJournal.capturePreconditions()
        val readiness = FlightTestReadinessEvaluator.evaluate(preconditions)
        val command = BridgeCommandJournal.journal.request(
            "takeoff",
            preconditions,
            mapOf(
                "readiness" to if (readiness.readyForTakeoffCommand) "ready" else "blocked",
                "readiness_enforcement" to "informational_only",
                "readiness_blockers" to readiness.blockers.joinToString(","),
                "readiness_advisories" to readiness.advisories.joinToString(",")
            )
        )
        val takeoffReservation = synchronized(actionTransitionLock) {
            when {
                CrossActionGatePolicy.blocksTakeoff(
                    enablePending = enableGate.current() != null,
                    disablePending = disableRequested.get(),
                    landingPending = landingGate.current() != null,
                    landingConfirmationPending = confirmLandingGate.current() != null
                ) || failsafeActionGate.current() != null -> "conflict"
                !takeoffGate.tryAcquire(command.id) -> "already_pending"
                else -> null
            }
        }
        if (takeoffReservation == "conflict") {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "takeoff_conflicts_with_pending_action",
                    "takeoff cannot overlap a virtual-stick or landing transition"
                )
            )
            BridgeState.lastEvent.set("takeoff_conflicts_with_pending_action:${command.id}")
            return currentCommand(command)
        }
        if (takeoffReservation == "already_pending") {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "takeoff_already_pending",
                    "another takeoff action is pending or in its post-acceptance guard interval"
                )
            )
            BridgeState.lastEvent.set("takeoff_already_pending:${command.id}")
            return currentCommand(command)
        }
        if (!scheduleTakeoffCallbackTimeout(command.id)) {
            takeoffGate.release(command.id)
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "takeoff_timeout_schedule_failed",
                    "takeoff was not dispatched because its callback timeout could not be armed"
                )
            )
            return currentCommand(command)
        }
        BridgeState.lastEvent.set("takeoff_requested:${command.id}")
        try {
            val dispatched = synchronized(actionTransitionLock) {
                if (!TakeoffDispatchPolicy.mayDispatch(
                        reservedCommandId = command.id,
                        activeCommandId = takeoffGate.current(),
                        landingPending = landingGate.current() != null,
                        landingConfirmationPending = confirmLandingGate.current() != null
                    )
                ) {
                    false
                } else {
                    // Keep priority-landing reservation and DJI action
                    // registration mutually exclusive. The lock is released
                    // immediately after registration; callbacks remain async.
                    dispatchTakeoffAction(command)
                    true
                }
            }
            if (!dispatched) {
                takeoffGate.release(command.id)
                takeoffTimedOutAtMonotonicMs.set(-1L)
                BridgeCommandJournal.journal.fail(
                    command.id,
                    bridgeError(
                        "takeoff_preempted_by_landing",
                        "priority landing prevented takeoff dispatch"
                    )
                )
                BridgeState.lastEvent.set("takeoff_preempted_by_landing:${command.id}")
            }
        } catch (error: Exception) {
            val commandError = bridgeException("takeoff_dispatch_failed", error)
            val completed = BridgeCommandJournal.journal.fail(command.id, commandError)
            if (completed?.state == CommandState.FAILED &&
                completed.error?.code == commandError.code
            ) {
                takeoffTimedOutAtMonotonicMs.set(controlMonotonicMillis())
            }
            BridgeState.lastEvent.set("takeoff_dispatch_failed:${command.id}:${error.message}")
        }
        return currentCommand(command)
    }

    private fun dispatchTakeoffAction(command: CommandRecord) {
        FlightControllerKey.KeyStartTakeoff.create().action({
            val wasPending = BridgeCommandJournal.journal.get(command.id)?.state ==
                CommandState.REQUESTED
            val completed = BridgeCommandJournal.journal.succeed(
                command.id,
                djiActionResult("start_takeoff")
            )
            if (wasPending && completed?.state == CommandState.SUCCEEDED) {
                takeoffTimedOutAtMonotonicMs.set(-1L)
                BridgeState.lastEvent.set("takeoff_accepted:${command.id}")
                scheduleTakeoffGuardRelease(command.id)
            } else {
                BridgeState.lastEvent.set(
                    "takeoff_late_success_callback_ignored:${command.id}"
                )
            }
        }, { error: IDJIError ->
            val wasPending = BridgeCommandJournal.journal.get(command.id)?.state ==
                CommandState.REQUESTED
            val completed = BridgeCommandJournal.journal.fail(command.id, djiError(error))
            if (wasPending) takeoffGate.release(command.id)
            if (wasPending && completed?.state == CommandState.FAILED) {
                BridgeState.lastEvent.set("takeoff_failed:${command.id}:$error")
            }
        })
    }

    private fun scheduleTakeoffCallbackTimeout(commandId: String): Boolean = try {
        scheduler.schedule({
            val current = BridgeCommandJournal.journal.get(commandId)
            if (current?.state == CommandState.REQUESTED) {
                val commandError = bridgeError(
                    "takeoff_callback_timeout",
                    "DJI did not return a takeoff action callback within " +
                        "$TAKEOFF_CALLBACK_TIMEOUT_MILLIS ms"
                ).copy(
                    hint = "inspect motors/is_flying telemetry; timeout does not prove the aircraft stayed grounded"
                )
                val completed = BridgeCommandJournal.journal.fail(commandId, commandError)
                if (completed?.state == CommandState.FAILED &&
                    completed.error?.code == commandError.code
                ) {
                    BridgeState.lastEvent.set("takeoff_callback_timeout:$commandId")
                    takeoffTimedOutAtMonotonicMs.set(controlMonotonicMillis())
                }
            } else if (current == null) {
                takeoffTimedOutAtMonotonicMs.set(controlMonotonicMillis())
            }
        }, TAKEOFF_CALLBACK_TIMEOUT_MILLIS, TimeUnit.MILLISECONDS)
        true
    } catch (_: Exception) {
        false
    }

    private fun scheduleTakeoffGuardRelease(commandId: String) {
        try {
            scheduler.schedule(
                { takeoffGate.release(commandId) },
                TAKEOFF_POST_ACCEPT_GUARD_MILLIS,
                TimeUnit.MILLISECONDS
            )
        } catch (error: Exception) {
            // Keeping the guard closed is safer than permitting a duplicate
            // takeoff if the scheduler is shutting down.
            Log.e("VeilDjiBridge", "Takeoff guard release scheduling failed", error)
        }
    }

    private fun scheduleActionCallbackTimeout(
        commandId: String,
        action: String,
        gate: SingleFlightCommandGate,
        timeoutMillis: Long
    ): Boolean = try {
        scheduler.schedule({
            val current = BridgeCommandJournal.journal.get(commandId)
            if (current?.state == CommandState.REQUESTED && gate.release(commandId)) {
                BridgeCommandJournal.journal.fail(
                    commandId,
                    bridgeError(
                        "${action}_callback_timeout",
                        "DJI did not return an $action callback within $timeoutMillis ms"
                    ).copy(
                        hint = "inspect aircraft telemetry; callback timeout does not prove physical state"
                    )
                )
                BridgeState.lastEvent.set("${action}_callback_timeout:$commandId")
            } else if (current == null) {
                gate.release(commandId)
            }
        }, timeoutMillis, TimeUnit.MILLISECONDS)
        true
    } catch (_: Exception) {
        false
    }

    private fun scheduleActionGuardRelease(
        gate: SingleFlightCommandGate,
        commandId: String,
        delayMillis: Long
    ) {
        try {
            scheduler.schedule(
                { gate.release(commandId) },
                delayMillis,
                TimeUnit.MILLISECONDS
            )
        } catch (error: Exception) {
            Log.e("VeilDjiBridge", "Action guard release scheduling failed", error)
        }
    }

    fun land(): CommandRecord {
        // Capture the aircraft state before invalidating the control session so
        // the landing record reflects the exact HTTP action boundary.
        val command = requestCommand("land")
        val landingReservation = synchronized(actionTransitionLock) {
            val aircraft = AircraftTelemetryState.snapshot()
            when {
                !LandingDispatchPolicy.mayReserve(
                    takeoffPending = takeoffGate.current() != null,
                    connectionUpdatedAtMonotonicMs =
                        aircraft.connectionUpdatedAtMonotonicMs,
                    isFlyingUpdatedAtMonotonicMs =
                        aircraft.isFlyingUpdatedAtMonotonicMs,
                    isFlying = aircraft.isFlying
                ) -> "takeoff_conflict" to null
                !landingGate.tryAcquire(command.id) -> "already_pending" to null
                else -> null to enableGate.current()
            }
        }
        if (landingReservation.first == "takeoff_conflict") {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "landing_conflicts_with_pending_takeoff",
                    "landing cannot cancel a takeoff whose physical state is still uncertain"
                ).copy(
                    hint = "use the RC to abort, or retry after current-connection telemetry reports is_flying=true"
                )
            )
            BridgeState.lastEvent.set(
                "landing_conflicts_with_pending_takeoff:${command.id}"
            )
            return currentCommand(command)
        }
        if (landingReservation.first == "already_pending") {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError("landing_already_pending", "another landing action is pending")
            )
            return currentCommand(command)
        }
        landingReservation.second?.let { enableCommandId ->
            failEnable(
                enableCommandId,
                bridgeError(
                    "virtual_stick_enable_preempted_by_landing",
                    "priority landing preempted the virtual-stick enable transition"
                )
            )
        }
        if (!scheduleActionCallbackTimeout(
                command.id,
                "landing",
                landingGate,
                LANDING_CALLBACK_TIMEOUT_MILLIS
            )
        ) {
            landingGate.release(command.id)
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "landing_timeout_schedule_failed",
                    "landing was not dispatched because its callback timeout could not be armed"
                )
            )
            return currentCommand(command)
        }
        BridgeControlSession.rotate("landing")
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)
        BridgeState.lastEvent.set("landing_requested:${command.id}")

        if (virtualStickReleaseRequired()) {
            if (disableRequested.get()) {
                // A deadman/operator release is already in flight. The landing
                // dispatcher polls observed authority under its own watchdog.
                scheduleLandingDispatch(command.id)
            } else {
                requestVirtualStickDisable("landing") { releaseError ->
                    if (landingGate.current() != command.id) {
                        return@requestVirtualStickDisable
                    }
                    if (releaseError != null) {
                        // Disable failures remain in persistent neutral/retry
                        // mode. Keep the priority landing pending and let its
                        // authority poll dispatch immediately after release.
                        BridgeState.lastEvent.set(
                            "landing_waiting_for_authority:${command.id}:${releaseError.code}"
                        )
                    }
                    scheduleLandingDispatch(command.id)
                }
            }
        } else {
            scheduleLandingDispatch(command.id)
        }
        return currentCommand(command)
    }

    private fun scheduleLandingDispatch(commandId: String) {
        try {
            scheduler.execute { dispatchAutoLanding(commandId) }
        } catch (error: Exception) {
            landingGate.release(commandId)
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeException("landing_schedule_failed", error)
            )
            BridgeState.lastEvent.set(
                "landing_schedule_failed:$commandId:${error.message}"
            )
        }
    }

    private fun dispatchAutoLanding(commandId: String) {
        if (landingGate.current() != commandId) return
        if (closed.get()) {
            landingGate.release(commandId)
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeError("controller_closed", "flight controller is closed")
            )
            return
        }
        if (!landingAuthorityClear()) {
            // The manager callback can precede the authority-state listener.
            // Poll under the existing action watchdog; never dispatch landing
            // while any source still reports MSDK/virtual-stick authority.
            try {
                scheduler.schedule(
                    { dispatchAutoLanding(commandId) },
                    LANDING_AUTHORITY_RECHECK_MILLIS,
                    TimeUnit.MILLISECONDS
                )
            } catch (error: Exception) {
                landingGate.release(commandId)
                BridgeCommandJournal.journal.fail(
                    commandId,
                    bridgeException("landing_authority_recheck_schedule_failed", error)
                )
            }
            return
        }
        try {
            FlightControllerKey.KeyStartAutoLanding.create().action({
                if (landingGate.current() != commandId) return@action
                val wasPending = BridgeCommandJournal.journal.get(commandId)?.state ==
                    CommandState.REQUESTED
                BridgeCommandJournal.journal.succeed(
                    commandId,
                    djiActionResult("start_auto_landing")
                )
                if (wasPending) {
                    BridgeState.lastEvent.set("landing_accepted:$commandId")
                    scheduleActionGuardRelease(
                        landingGate,
                        commandId,
                        LANDING_POST_ACCEPT_GUARD_MILLIS
                    )
                }
            }, { error: IDJIError ->
                if (landingGate.current() != commandId) return@action
                val wasPending = BridgeCommandJournal.journal.get(commandId)?.state ==
                    CommandState.REQUESTED
                BridgeCommandJournal.journal.fail(commandId, djiError(error))
                if (wasPending) {
                    landingGate.release(commandId)
                    BridgeState.lastEvent.set("landing_failed:$commandId:$error")
                }
            })
        } catch (error: Exception) {
            landingGate.release(commandId)
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeException("landing_dispatch_failed", error)
            )
            BridgeState.lastEvent.set(
                "landing_dispatch_failed:$commandId:${error.message}"
            )
        }
    }

    private fun landingAuthorityClear(): Boolean =
        requestedMode.get() == VirtualControlMode.DISABLED &&
            failsafeNeutralMode.get() == null &&
            !disableRequested.get() &&
            !hadMsdkAuthority.get() &&
            !BridgeState.virtualStickEnabled.get() &&
            BridgeState.flightControlAuthority.get() != FlightControlAuthority.MSDK.name

    /**
     * Continues an auto-landing only after DJI explicitly reports that pilot
     * confirmation is required. This is intentionally never called automatically.
     */
    fun confirmLanding(): CommandRecord {
        val command = requestCommand("confirm_landing")
        if (!LandingConfirmationPolicy.canDispatch(
                command.preconditions.landingConfirmationNeeded
            )
        ) {
            BridgeCommandJournal.journal.fail(
                command.id,
                CommandError(
                    source = "bridge",
                    type = "precondition",
                    code = "landing_confirmation_not_requested",
                    description = "DJI telemetry does not report that landing confirmation is needed",
                    hint = "wait for aircraft_telemetry.safety.landing_confirmation_needed=true"
                )
            )
            BridgeState.lastEvent.set(
                "landing_confirmation_precondition_failed:${command.id}"
            )
            return currentCommand(command)
        }

        val confirmationReservation = synchronized(actionTransitionLock) {
            val acquired = confirmLandingGate.tryAcquire(command.id)
            acquired to enableGate.current().takeIf { acquired }
        }
        if (!confirmationReservation.first) {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "landing_confirmation_already_pending",
                    "another landing-confirmation action is pending"
                )
            )
            return currentCommand(command)
        }
        confirmationReservation.second?.let { enableCommandId ->
            failEnable(
                enableCommandId,
                bridgeError(
                    "virtual_stick_enable_preempted_by_landing_confirmation",
                    "priority landing confirmation preempted virtual-stick enable"
                )
            )
        }
        if (!scheduleActionCallbackTimeout(
                command.id,
                "landing_confirmation",
                confirmLandingGate,
                LANDING_CONFIRM_CALLBACK_TIMEOUT_MILLIS
            )
        ) {
            confirmLandingGate.release(command.id)
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "landing_confirmation_timeout_schedule_failed",
                    "landing confirmation was not dispatched because its timeout could not be armed"
                )
            )
            return currentCommand(command)
        }

        BridgeState.lastEvent.set("landing_confirmation_requested:${command.id}")
        if (virtualStickReleaseRequired()) {
            if (disableRequested.get()) {
                scheduleLandingConfirmationDispatch(command.id)
            } else {
                requestVirtualStickDisable("landing_confirmation") { releaseError ->
                    if (confirmLandingGate.current() != command.id) {
                        return@requestVirtualStickDisable
                    }
                    if (releaseError != null) {
                        BridgeState.lastEvent.set(
                            "landing_confirmation_waiting_for_authority:" +
                                "${command.id}:${releaseError.code}"
                        )
                    }
                    scheduleLandingConfirmationDispatch(command.id)
                }
            }
        } else {
            scheduleLandingConfirmationDispatch(command.id)
        }
        return currentCommand(command)
    }

    /** Sets the persistent RC-link-loss action only while safely grounded. */
    fun setFailsafeAction(action: FailsafeAction): CommandRecord {
        val previous = AircraftTelemetryState.snapshot().homeRth.failsafeAction
        val command = requestCommand(
            "set_failsafe_action",
            mapOf("previous" to previous, "requested" to action.name)
        )
        if (action == FailsafeAction.UNKNOWN) {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError("invalid_failsafe_action", "UNKNOWN cannot be configured")
            )
            return currentCommand(command)
        }

        val reservation = synchronized(actionTransitionLock) {
            when {
                !groundConfigurationReady() -> "not_grounded"
                takeoffGate.current() != null ||
                    enableGate.current() != null ||
                    disableRequested.get() ||
                    landingGate.current() != null ||
                    confirmLandingGate.current() != null -> "conflict"
                !failsafeActionGate.tryAcquire(command.id) -> "already_pending"
                else -> null
            }
        }
        if (reservation != null) {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "failsafe_action_$reservation",
                    "failsafe action changes require a grounded, disarmed aircraft with no flight transition"
                )
            )
            return currentCommand(command)
        }
        if (!scheduleActionCallbackTimeout(
                command.id,
                "failsafe_action",
                failsafeActionGate,
                FAILSAFE_ACTION_CALLBACK_TIMEOUT_MILLIS
            )
        ) {
            failsafeActionGate.release(command.id)
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "failsafe_action_timeout_schedule_failed",
                    "failsafe action was not changed because its watchdog could not be armed"
                )
            )
            return currentCommand(command)
        }

        BridgeState.lastEvent.set("failsafe_action_requested:${command.id}:${action.name}")
        try {
            FlightControllerKey.KeyFailsafeAction.create().set(action, {
                if (!failsafeActionGate.release(command.id)) return@set
                AircraftTelemetryState.updateFailsafeAction(action.name)
                BridgeCommandJournal.journal.succeed(
                    command.id,
                    djiActionResult("set_failsafe_action") +
                        mapOf("previous" to previous, "configured" to action.name)
                )
                BridgeState.lastEvent.set(
                    "failsafe_action_configured:${command.id}:${action.name}"
                )
            }, { error: IDJIError ->
                if (!failsafeActionGate.release(command.id)) return@set
                BridgeCommandJournal.journal.fail(command.id, djiError(error))
                BridgeState.lastEvent.set(
                    "failsafe_action_failed:${command.id}:${action.name}:$error"
                )
            })
        } catch (error: Exception) {
            failsafeActionGate.release(command.id)
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeException("failsafe_action_dispatch_failed", error)
            )
        }
        return currentCommand(command)
    }

    private fun scheduleLandingConfirmationDispatch(commandId: String) {
        try {
            scheduler.execute { dispatchLandingConfirmation(commandId) }
        } catch (error: Exception) {
            confirmLandingGate.release(commandId)
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeException("landing_confirmation_schedule_failed", error)
            )
        }
    }

    private fun dispatchLandingConfirmation(commandId: String) {
        if (confirmLandingGate.current() != commandId) return
        if (closed.get()) {
            confirmLandingGate.release(commandId)
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeError("controller_closed", "flight controller is closed")
            )
            return
        }
        if (!landingAuthorityClear()) {
            try {
                scheduler.schedule(
                    { dispatchLandingConfirmation(commandId) },
                    LANDING_AUTHORITY_RECHECK_MILLIS,
                    TimeUnit.MILLISECONDS
                )
            } catch (error: Exception) {
                confirmLandingGate.release(commandId)
                BridgeCommandJournal.journal.fail(
                    commandId,
                    bridgeException(
                        "landing_confirmation_authority_recheck_schedule_failed",
                        error
                    )
                )
            }
            return
        }
        try {
            FlightControllerKey.KeyConfirmLanding.create().action({
                if (!confirmLandingGate.release(commandId)) return@action
                BridgeCommandJournal.journal.succeed(
                    commandId,
                    djiActionResult("confirm_landing")
                )
                BridgeState.lastEvent.set("landing_confirmation_accepted:$commandId")
            }, { error: IDJIError ->
                if (!confirmLandingGate.release(commandId)) return@action
                BridgeCommandJournal.journal.fail(commandId, djiError(error))
                BridgeState.lastEvent.set(
                    "landing_confirmation_failed:$commandId:$error"
                )
            })
        } catch (error: Exception) {
            confirmLandingGate.release(commandId)
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeException("landing_confirmation_dispatch_failed", error)
            )
            BridgeState.lastEvent.set(
                "landing_confirmation_dispatch_failed:$commandId:${error.message}"
            )
        }
    }

    fun enableVirtualStick(
        mode: VirtualControlMode = VirtualControlMode.BODY_VELOCITY
    ): CommandRecord {
        require(mode != VirtualControlMode.DISABLED) { "a control mode is required" }
        val command = requestCommand(
            "virtual_stick_enable",
            mapOf("mode" to mode.wireName)
        )
        val enableReservation = synchronized(actionTransitionLock) {
            when {
                !controlLinksReady() ||
                    requestedMode.get() != VirtualControlMode.DISABLED ||
                    failsafeNeutralMode.get() != null ||
                    disableRequested.get() ||
                    !authorityReleasedToRcForCurrentConnection() -> "not_disarmed"
                CrossActionGatePolicy.blocksVirtualStickEnable(
                    takeoffPending = takeoffGate.current() != null,
                    landingPending = landingGate.current() != null,
                    landingConfirmationPending = confirmLandingGate.current() != null
                ) || failsafeActionGate.current() != null -> "conflict"
                !enableGate.tryAcquire(command.id) -> "already_pending"
                else -> {
                    // Publish arming state while the same lock still excludes a
                    // priority landing reservation. A later landing may then
                    // preempt this command through failEnable().
                    BridgeControlSession.rotate("enable_${mode.wireName}")
                    requestedMode.set(mode)
                    latestCommand.set(neutralCommand(mode))
                    lastCommandNanos.set(System.nanoTime())
                    neutralSent.set(false)
                    disableRequested.set(false)
                    BridgeState.virtualStickControlMode.set("arming_${mode.wireName}")
                    BridgeState.controlFailsafeState.set("arming")
                    null
                }
            }
        }
        if (enableReservation != null) {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    when (enableReservation) {
                        "not_disarmed" -> "virtual_stick_transition_not_disarmed"
                        "conflict" -> "virtual_stick_enable_conflicts_with_flight_action"
                        else -> "virtual_stick_enable_already_pending"
                    },
                    when (enableReservation) {
                        "not_disarmed" ->
                            "all control links and current-connection RC authority must be fully disarmed"
                        "conflict" ->
                            "virtual-stick enable cannot overlap takeoff or landing"
                        else -> "another virtual-stick enable callback is pending"
                    }
                )
            )
            return currentCommand(command)
        }
        try {
            scheduler.schedule({
                handleEnableCallbackTimeout(command.id, mode)
            }, VIRTUAL_STICK_CALLBACK_TIMEOUT_MILLIS, TimeUnit.MILLISECONDS)
            scheduler.execute {
                if (closed.get() || enableGate.current() != command.id) {
                    if (closed.get()) {
                        failEnable(command.id, bridgeError(
                            "controller_closed",
                            "flight controller closed before virtual-stick enable was dispatched"
                        ))
                    }
                    return@execute
                }
                try {
                    neutralizeManagerState()
                    manager.setSpeedLevel(1.0) // Advanced values retain their documented SI units.
                    manager.setVirtualStickAdvancedModeEnabled(
                        mode == VirtualControlMode.BODY_VELOCITY
                    )
                    manager.enableVirtualStick(object : CommonCallbacks.CompletionCallback {
                        override fun onSuccess() {
                            if (!enableGate.release(command.id)) return
                            val completed = BridgeCommandJournal.journal.succeed(
                                command.id,
                                djiActionResult("enable_virtual_stick") +
                                    ("mode" to mode.wireName)
                            )
                            if (completed?.state == CommandState.SUCCEEDED) {
                                BridgeState.lastEvent.set(
                                    "virtual_stick_enable_accepted:${command.id}:${mode.wireName}"
                                )
                            }
                        }

                        override fun onFailure(error: IDJIError) {
                            failEnable(command.id, djiError(error))
                            BridgeState.lastEvent.set(
                                "virtual_stick_enable_failed:${command.id}:$error"
                            )
                        }
                    })
                } catch (error: Exception) {
                    failEnable(
                        command.id,
                        bridgeException("virtual_stick_enable_dispatch_failed", error)
                    )
                    BridgeState.lastEvent.set(
                        "virtual_stick_enable_dispatch_failed:${command.id}:${error.message}"
                    )
                }
            }
        } catch (error: Exception) {
            failEnable(
                command.id,
                bridgeException("virtual_stick_enable_schedule_failed", error)
            )
            BridgeState.lastEvent.set(
                "virtual_stick_enable_schedule_failed:${command.id}:${error.message}"
            )
        }
        return currentCommand(command)
    }

    private fun handleEnableCallbackTimeout(commandId: String, mode: VirtualControlMode) {
        if (!enableGate.release(commandId)) return
        val commandError = bridgeError(
            "virtual_stick_enable_callback_timeout",
            "DJI did not return a virtual-stick enable callback within " +
                "$VIRTUAL_STICK_CALLBACK_TIMEOUT_MILLIS ms"
        ).copy(
            hint = "the control session was invalidated and authority release was requested"
        )
        BridgeCommandJournal.journal.fail(commandId, commandError)
        BridgeControlSession.rotate("enable_callback_timeout")
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)
        failsafeNeutralMode.set(mode)
        requestedMode.set(VirtualControlMode.DISABLED)
        BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
        BridgeState.controlFailsafeState.set("enable_timeout_disabling")
        BridgeState.lastEvent.set("virtual_stick_enable_callback_timeout:$commandId")
        bestEffortNeutralize(mode, includeAdvancedNeutral = true)
        if (!disableRequested.get()) {
            runCatching { requestVirtualStickDisable("enable_callback_timeout") }
        }
    }

    fun disableVirtualStick(reason: String = "operator"): CommandRecord {
        return requestVirtualStickDisable(reason)
    }

    private fun requestVirtualStickDisable(
        reason: String,
        completion: ((CommandError?) -> Unit)? = null
    ): CommandRecord {
        val command = requestCommand(
            "virtual_stick_disable",
            mapOf("reason" to reason)
        )
        val completionNotified = AtomicBoolean(false)
        if (!synchronized(actionTransitionLock) {
                disableRequested.compareAndSet(false, true)
            }
        ) {
            val error = bridgeError(
                "disable_already_requested",
                "a virtual-stick disable request is already in progress"
            )
            BridgeCommandJournal.journal.fail(
                command.id,
                error
            )
            notifyDisableCompletion(completion, completionNotified, error)
            return currentCommand(command)
        }
        activeDisableCommandId.set(command.id)
        if (closed.get()) {
            disableRequested.set(false)
            activeDisableCommandId.compareAndSet(command.id, null)
            val error = bridgeError("controller_closed", "flight controller is closed")
            BridgeCommandJournal.journal.fail(
                command.id,
                error
            )
            requestedMode.set(VirtualControlMode.DISABLED)
            notifyDisableCompletion(completion, completionNotified, error)
            return currentCommand(command)
        }
        val neutralMode = failsafeNeutralMode.get() ?: if (virtualStickReleaseRequired()) {
            requestedMode.get().takeUnless { it == VirtualControlMode.DISABLED }
                ?: if (BridgeState.virtualStickAdvancedMode.get()) {
                    VirtualControlMode.BODY_VELOCITY
                } else {
                    VirtualControlMode.STICKS
                }
        } else {
            null
        }
        try {
            BridgeControlSession.rotate("disable_$reason")
            latestCommand.set(null)
            lastCommandNanos.set(0L)
            neutralMode?.let(failsafeNeutralMode::set)
            lastDisableAttemptNanos.set(System.nanoTime())
            BridgeState.controlFailsafeState.set("disabling")
            scheduler.schedule({
                handleDisableCallbackTimeout(
                    command.id,
                    neutralMode,
                    completion,
                    completionNotified
                )
            }, VIRTUAL_STICK_CALLBACK_TIMEOUT_MILLIS, TimeUnit.MILLISECONDS)
            scheduler.execute {
                disableOnControlThread(
                    reason,
                    command.id,
                    neutralMode,
                    completion,
                    completionNotified
                )
            }
        } catch (error: Exception) {
            disableRequested.set(false)
            activeDisableCommandId.compareAndSet(command.id, null)
            val commandError = bridgeException(
                "virtual_stick_disable_schedule_failed",
                error
            )
            BridgeCommandJournal.journal.fail(
                command.id,
                commandError
            )
            requestedMode.set(VirtualControlMode.DISABLED)
            latestCommand.set(null)
            lastCommandNanos.set(0L)
            BridgeState.controlFailsafeState.set("disable_failed_neutral")
            BridgeState.lastEvent.set(
                "virtual_stick_disable_schedule_failed:${command.id}:${error.message}"
            )
            notifyDisableCompletion(completion, completionNotified, commandError)
        }
        return currentCommand(command)
    }

    private fun handleDisableCallbackTimeout(
        commandId: String,
        neutralMode: VirtualControlMode?,
        completion: ((CommandError?) -> Unit)?,
        completionNotified: AtomicBoolean
    ) {
        if (!activeDisableCommandId.compareAndSet(commandId, null)) return
        disableRequested.set(false)
        val commandError = bridgeError(
            "virtual_stick_disable_callback_timeout",
            "DJI did not return a virtual-stick disable callback within " +
                "$VIRTUAL_STICK_CALLBACK_TIMEOUT_MILLIS ms"
        ).copy(
            hint = "neutral transmission and periodic authority-release retries remain active"
        )
        BridgeCommandJournal.journal.fail(commandId, commandError)
        requestedMode.set(VirtualControlMode.DISABLED)
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        val retainedNeutralMode = DisableNeutralRetentionPolicy.modeToRetain(
            capturedMode = neutralMode,
            persistentMode = failsafeNeutralMode.get(),
            releaseStillRequired = virtualStickReleaseRequired(),
            releaseConfirmedToRc = virtualStickReleaseConfirmedToRc(),
            advancedModeObserved = BridgeState.virtualStickAdvancedMode.get()
        )
        if (retainedNeutralMode != null) {
            failsafeNeutralMode.set(retainedNeutralMode)
            BridgeState.controlFailsafeState.set("disable_timeout_neutral")
        } else {
            failsafeNeutralMode.set(null)
            BridgeState.controlFailsafeState.set("disarmed")
        }
        BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
        BridgeState.lastEvent.set("virtual_stick_disable_callback_timeout:$commandId")
        notifyDisableCompletion(completion, completionNotified, commandError)
    }

    fun submitSticks(
        leftHorizontal: Int,
        leftVertical: Int,
        rightHorizontal: Int,
        rightVertical: Int
    ): Boolean {
        require(leftHorizontal in -660..660)
        require(leftVertical in -660..660)
        require(rightHorizontal in -660..660)
        require(rightVertical in -660..660)
        if (!isReady(VirtualControlMode.STICKS)) return false
        latestCommand.set(
            RealtimeControlCommand.Sticks(
                leftHorizontal, leftVertical, rightHorizontal, rightVertical
            )
        )
        markFreshCommand()
        return true
    }

    /** Journaled supervisory equivalent of [submitSticks]. UDP packets stay unjournaled. */
    fun submitSticksObserved(
        leftHorizontal: Int,
        leftVertical: Int,
        rightHorizontal: Int,
        rightVertical: Int
    ): CommandRecord {
        require(leftHorizontal in -660..660)
        require(leftVertical in -660..660)
        require(rightHorizontal in -660..660)
        require(rightVertical in -660..660)
        val command = requestCommand(
            "virtual_stick_setpoint",
            mapOf(
                "mode" to VirtualControlMode.STICKS.wireName,
                "left_horizontal" to leftHorizontal.toString(),
                "left_vertical" to leftVertical.toString(),
                "right_horizontal" to rightHorizontal.toString(),
                "right_vertical" to rightVertical.toString()
            )
        )
        if (submitSticks(leftHorizontal, leftVertical, rightHorizontal, rightVertical)) {
            BridgeCommandJournal.journal.succeed(
                command.id,
                bridgeSetpointResult(VirtualControlMode.STICKS)
            )
        } else {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "virtual_stick_not_ready",
                    "virtual stick is not active in sticks mode with MSDK authority"
                )
            )
        }
        return currentCommand(command)
    }

    fun submitBodyVelocity(
        forwardMetersPerSecond: Double,
        rightMetersPerSecond: Double,
        upMetersPerSecond: Double,
        yawRateDegreesPerSecond: Double
    ): Boolean {
        require(forwardMetersPerSecond.isFinite() && forwardMetersPerSecond in -23.0..23.0)
        require(rightMetersPerSecond.isFinite() && rightMetersPerSecond in -23.0..23.0)
        require(upMetersPerSecond.isFinite() && upMetersPerSecond in -6.0..6.0)
        require(yawRateDegreesPerSecond.isFinite() && yawRateDegreesPerSecond in -100.0..100.0)
        if (!isReady(VirtualControlMode.BODY_VELOCITY)) return false
        latestCommand.set(
            RealtimeControlCommand.BodyVelocity(
                forwardMetersPerSecond,
                rightMetersPerSecond,
                upMetersPerSecond,
                yawRateDegreesPerSecond
            )
        )
        markFreshCommand()
        return true
    }

    /** Journaled supervisory equivalent of [submitBodyVelocity]. */
    fun submitBodyVelocityObserved(
        forwardMetersPerSecond: Double,
        rightMetersPerSecond: Double,
        upMetersPerSecond: Double,
        yawRateDegreesPerSecond: Double
    ): CommandRecord {
        require(forwardMetersPerSecond.isFinite() && forwardMetersPerSecond in -23.0..23.0)
        require(rightMetersPerSecond.isFinite() && rightMetersPerSecond in -23.0..23.0)
        require(upMetersPerSecond.isFinite() && upMetersPerSecond in -6.0..6.0)
        require(yawRateDegreesPerSecond.isFinite() && yawRateDegreesPerSecond in -100.0..100.0)
        val command = requestCommand(
            "virtual_stick_setpoint",
            mapOf(
                "mode" to VirtualControlMode.BODY_VELOCITY.wireName,
                "forward_mps" to forwardMetersPerSecond.toString(),
                "right_mps" to rightMetersPerSecond.toString(),
                "up_mps" to upMetersPerSecond.toString(),
                "yaw_rate_deg_s" to yawRateDegreesPerSecond.toString()
            )
        )
        if (submitBodyVelocity(
                forwardMetersPerSecond,
                rightMetersPerSecond,
                upMetersPerSecond,
                yawRateDegreesPerSecond
            )
        ) {
            BridgeCommandJournal.journal.succeed(
                command.id,
                bridgeSetpointResult(VirtualControlMode.BODY_VELOCITY)
            )
        } else {
            BridgeCommandJournal.journal.fail(
                command.id,
                bridgeError(
                    "virtual_stick_not_ready",
                    "virtual stick is not active in body_velocity mode with MSDK authority"
                )
            )
        }
        return currentCommand(command)
    }

    fun onControlTransportStopped() {
        onControlLinkDisconnected("transport")
    }

    /**
     * Invalidates authenticated packets and releases MSDK authority on every
     * product, RC, air-link, flight-controller, or UDP transport interruption.
     */
    fun onControlLinkDisconnected(source: String) {
        if (closed.get()) return
        val safeSource = source.lowercase().replace(Regex("[^a-z0-9_]+"), "_")
        failPendingTakeoffForLinkLoss(safeSource)
        failPendingActionForLinkLoss(enableGate, "virtual_stick_enable", safeSource)
        failPendingActionForLinkLoss(landingGate, "landing", safeSource)
        failPendingActionForLinkLoss(
            confirmLandingGate,
            "landing_confirmation",
            safeSource
        )
        failPendingActionForLinkLoss(
            failsafeActionGate,
            "failsafe_action",
            safeSource
        )
        BridgeControlSession.rotate("${safeSource}_disconnected")
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)

        if (!virtualStickReleaseRequired()) {
            requestedMode.set(VirtualControlMode.DISABLED)
            BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
            BridgeState.controlFailsafeState.set("disarmed")
            return
        }

        BridgeState.controlFailsafeState.set("link_loss_disabling")
        val neutralMode = requestedMode.get().takeUnless {
            it == VirtualControlMode.DISABLED
        } ?: if (BridgeState.virtualStickAdvancedMode.get()) {
            VirtualControlMode.BODY_VELOCITY
        } else {
            VirtualControlMode.STICKS
        }
        failsafeNeutralMode.set(neutralMode)
        bestEffortNeutralize(neutralMode, includeAdvancedNeutral = true)
        if (!disableRequested.get()) {
            runCatching { requestVirtualStickDisable("${safeSource}_disconnected") }
                .onFailure { failure ->
                    requestedMode.set(VirtualControlMode.DISABLED)
                    BridgeState.virtualStickControlMode.set(
                        VirtualControlMode.DISABLED.wireName
                    )
                    BridgeState.controlFailsafeState.set("disable_failed_neutral")
                    BridgeState.lastEvent.set(
                        "link_loss_disable_failed:$safeSource:${failure.javaClass.simpleName}"
                    )
                }
        }
    }

    private fun markFreshCommand() {
        lastCommandNanos.set(System.nanoTime())
        neutralSent.set(false)
        BridgeState.controlFailsafeState.set("active")
    }

    private fun failPendingTakeoffForLinkLoss(source: String) {
        val commandId = takeoffGate.current() ?: return
        val current = BridgeCommandJournal.journal.get(commandId)
        if (current?.state == CommandState.REQUESTED) {
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeError(
                    "control_link_disconnected",
                    "$source disconnected while the takeoff callback was pending"
                )
            )
        }
        takeoffTimedOutAtMonotonicMs.set(-1L)
        takeoffGate.release(commandId)
    }

    /** Called after either ground-state key publishes a new connection-scoped sample. */
    fun onFlightStateObserved() {
        val timedOutAtMs = takeoffTimedOutAtMonotonicMs.get()
        if (timedOutAtMs < 0L) return
        val aircraft = AircraftTelemetryState.snapshot()
        if (TakeoffTimeoutReconciliationPolicy.freshGroundStateProvesNoTakeoff(
                timedOutAtMs,
                aircraft.isFlyingUpdatedAtMonotonicMs,
                aircraft.motorsOnUpdatedAtMonotonicMs,
                aircraft.isFlying,
                aircraft.motorsOn
            )
        ) {
            val commandId = takeoffGate.current() ?: return
            if (takeoffGate.release(commandId)) {
                takeoffTimedOutAtMonotonicMs.compareAndSet(timedOutAtMs, -1L)
                BridgeState.lastEvent.set("takeoff_timeout_reconciled_grounded:$commandId")
            }
        }
    }

    private fun failPendingActionForLinkLoss(
        gate: SingleFlightCommandGate,
        action: String,
        source: String
    ) {
        val commandId = gate.current() ?: return
        val current = BridgeCommandJournal.journal.get(commandId)
        if (current?.state == CommandState.REQUESTED) {
            BridgeCommandJournal.journal.fail(
                commandId,
                bridgeError(
                    "control_link_disconnected",
                    "$source disconnected while $action callback was pending"
                )
            )
        }
        gate.release(commandId)
    }

    private fun controlTick() {
        if (closed.get()) return
        val faultNeutralMode = failsafeNeutralMode.get()
        if (faultNeutralMode != null) {
            if (!disableRequested.get() && virtualStickReleaseConfirmedToRc()) {
                failsafeNeutralMode.compareAndSet(faultNeutralMode, null)
                BridgeState.controlFailsafeState.set("disarmed")
                return
            }
            // Continue transmitting neutral even after a disable callback fails;
            // only an observed authority release may end this state.
            bestEffortNeutralize(faultNeutralMode, includeAdvancedNeutral = true)
            val retryAgeNanos = System.nanoTime() - lastDisableAttemptNanos.get()
            if (!disableRequested.get() &&
                retryAgeNanos >= TimeUnit.MILLISECONDS.toNanos(
                    FAILSAFE_DISABLE_RETRY_MILLIS
                )
            ) {
                runCatching { requestVirtualStickDisable("failsafe_retry") }
            }
            return
        }
        val mode = requestedMode.get()
        if (mode == VirtualControlMode.DISABLED || !isReady(mode)) return

        val last = lastCommandNanos.get()
        if (last == 0L) return
        val ageMillis = ((System.nanoTime() - last).coerceAtLeast(0L)) / 1_000_000L
        BridgeState.lastControlAgeMs.set(ageMillis)
        when {
            ageMillis <= NEUTRAL_TIMEOUT_MILLIS -> {
                latestCommand.get()?.let(::sendCommand)
            }

            ageMillis < DISABLE_TIMEOUT_MILLIS -> {
                // Keep transmitting zero until authority is released; a single
                // neutral packet is not an adequate radio-link failsafe.
                sendCommand(neutralCommand(mode))
                if (neutralSent.compareAndSet(false, true)) {
                    BridgeState.controlFailsafeState.set("deadman_neutral")
                    BridgeState.lastEvent.set("virtual_stick_deadman_neutral")
                }
            }

            else -> if (!disableRequested.get()) disableVirtualStick("deadman")
        }
    }

    private fun handleControlTickFailure(failure: Throwable) {
        val modeAtFailure = requestedMode.get()
        if (modeAtFailure != VirtualControlMode.DISABLED) {
            failsafeNeutralMode.set(modeAtFailure)
        }
        BridgeControlSession.rotate("control_tick_exception")
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)
        BridgeState.controlFailsafeState.set("control_tick_fault_disabling")
        BridgeState.lastEvent.set(
            "control_tick_exception:${failure.javaClass.simpleName}:${failure.message.orEmpty()}"
        )
        Log.e("VeilDjiBridge", "Virtual-stick control tick failed", failure)

        // Do not let a failed neutral write prevent the authority-release call.
        bestEffortNeutralize(modeAtFailure, includeAdvancedNeutral = true)
        if (!disableRequested.get() && virtualStickReleaseRequired()) {
            runCatching { requestVirtualStickDisable("control_tick_exception") }
                .onFailure {
                    requestedMode.set(VirtualControlMode.DISABLED)
                    BridgeState.virtualStickControlMode.set(
                        VirtualControlMode.DISABLED.wireName
                    )
                    BridgeState.controlFailsafeState.set("disable_failed_neutral")
                }
        } else if (!virtualStickReleaseRequired()) {
            requestedMode.set(VirtualControlMode.DISABLED)
            BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
            BridgeState.controlFailsafeState.set("disarmed")
        }
    }

    private fun sendCommand(command: RealtimeControlCommand) {
        when (command) {
            is RealtimeControlCommand.Sticks -> {
                manager.leftStick.horizontalPosition = command.leftHorizontal
                manager.leftStick.verticalPosition = command.leftVertical
                manager.rightStick.horizontalPosition = command.rightHorizontal
                manager.rightStick.verticalPosition = command.rightVertical
            }

            is RealtimeControlCommand.BodyVelocity -> {
                // In DJI's BODY velocity frame roll is forward (X) and pitch is right (Y).
                manager.sendVirtualStickAdvancedParam(
                    VirtualStickFlightControlParam(
                        command.rightMetersPerSecond,
                        command.forwardMetersPerSecond,
                        command.yawRateDegreesPerSecond,
                        command.upMetersPerSecond,
                        VerticalControlMode.VELOCITY,
                        RollPitchControlMode.VELOCITY,
                        YawControlMode.ANGULAR_VELOCITY,
                        FlightCoordinateSystem.BODY
                    )
                )
            }
        }
    }

    private fun neutralCommand(mode: VirtualControlMode): RealtimeControlCommand = when (mode) {
        VirtualControlMode.STICKS -> RealtimeControlCommand.Sticks(0, 0, 0, 0)
        VirtualControlMode.BODY_VELOCITY -> RealtimeControlCommand.BodyVelocity(0.0, 0.0, 0.0, 0.0)
        VirtualControlMode.DISABLED -> RealtimeControlCommand.Sticks(0, 0, 0, 0)
    }

    private fun neutralizeManagerState() {
        manager.leftStick.horizontalPosition = 0
        manager.leftStick.verticalPosition = 0
        manager.rightStick.horizontalPosition = 0
        manager.rightStick.verticalPosition = 0
        if (requestedMode.get() == VirtualControlMode.BODY_VELOCITY &&
            BridgeState.virtualStickEnabled.get()
        ) {
            sendCommand(neutralCommand(VirtualControlMode.BODY_VELOCITY))
        }
    }

    /** Never throws; each neutral channel is attempted independently. */
    private fun bestEffortNeutralize(
        mode: VirtualControlMode,
        includeAdvancedNeutral: Boolean = false
    ) {
        runCatching { manager.leftStick.horizontalPosition = 0 }
        runCatching { manager.leftStick.verticalPosition = 0 }
        runCatching { manager.rightStick.horizontalPosition = 0 }
        runCatching { manager.rightStick.verticalPosition = 0 }
        if (includeAdvancedNeutral ||
            mode == VirtualControlMode.BODY_VELOCITY ||
            BridgeState.virtualStickAdvancedMode.get()
        ) {
            runCatching {
                sendCommand(neutralCommand(VirtualControlMode.BODY_VELOCITY))
            }
        }
    }

    private fun virtualStickReleaseRequired(): Boolean =
        VirtualStickSafetyPolicy.releaseRequired(
            requestedMode = requestedMode.get(),
            previouslyHadMsdkAuthority = hadMsdkAuthority.get(),
            virtualStickEnabled = BridgeState.virtualStickEnabled.get(),
            authorityOwner = BridgeState.flightControlAuthority.get()
        )

    private fun virtualStickReleaseConfirmedToRc(): Boolean =
        VirtualStickSafetyPolicy.releaseConfirmedToRc(
            requestedMode = requestedMode.get(),
            previouslyHadMsdkAuthority = hadMsdkAuthority.get(),
            virtualStickEnabled = BridgeState.virtualStickEnabled.get(),
            authorityOwner = BridgeState.flightControlAuthority.get()
        )

    private fun releaseUnexpectedAuthority() {
        if (closed.get()) return
        val neutralMode = if (BridgeState.virtualStickAdvancedMode.get()) {
            VirtualControlMode.BODY_VELOCITY
        } else {
            VirtualControlMode.STICKS
        }
        // A grant can arrive after a disable request captured "no authority."
        // Install persistent neutral even when a release call is already in
        // flight; only the duplicate release request itself may be skipped.
        failsafeNeutralMode.set(neutralMode)
        BridgeControlSession.rotate("unexpected_msdk_authority")
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)
        BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
        BridgeState.controlFailsafeState.set("unexpected_authority_disabling")
        BridgeState.lastEvent.set("unexpected_msdk_authority")
        bestEffortNeutralize(
            neutralMode,
            includeAdvancedNeutral = true
        )
        if (!disableRequested.get()) {
            runCatching { requestVirtualStickDisable("unexpected_authority") }
                .onFailure { failure ->
                    requestedMode.set(VirtualControlMode.DISABLED)
                    BridgeState.controlFailsafeState.set("disable_failed_neutral")
                    BridgeState.lastEvent.set(
                        "unexpected_authority_disable_failed:${failure.javaClass.simpleName}"
                    )
                }
        }
    }

    private fun disableOnControlThread(
        reason: String,
        commandId: String,
        neutralMode: VirtualControlMode?,
        completion: ((CommandError?) -> Unit)?,
        completionNotified: AtomicBoolean
    ) {
        if (closed.get()) {
            val commandError = bridgeError(
                "controller_closed",
                "flight controller closed before virtual-stick disable was dispatched"
            )
            if (activeDisableCommandId.compareAndSet(commandId, null)) {
                disableRequested.set(false)
            }
            requestedMode.set(VirtualControlMode.DISABLED)
            BridgeCommandJournal.journal.fail(
                commandId,
                commandError
            )
            notifyDisableCompletion(completion, completionNotified, commandError)
            return
        }
        try {
            // Neutral writes are best-effort; authority release must still be
            // attempted if one manager setter throws during a disconnect.
            bestEffortNeutralize(
                neutralMode ?: requestedMode.get(),
                includeAdvancedNeutral = neutralMode == VirtualControlMode.BODY_VELOCITY
            )
            manager.disableVirtualStick(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    val wasPending = BridgeCommandJournal.journal.get(commandId)?.state ==
                        CommandState.REQUESTED
                    BridgeCommandJournal.journal.succeed(
                        commandId,
                        djiActionResult("disable_virtual_stick") + ("reason" to reason)
                    )
                    if (activeDisableCommandId.compareAndSet(commandId, null)) {
                        finishDisabled("virtual_stick_disabled:$commandId:$reason")
                    } else if (wasPending) {
                        // The callback raced its watchdog. Physical release still
                        // wins, but do not overwrite a newer retry's bookkeeping.
                        hadMsdkAuthority.set(false)
                        failsafeNeutralMode.set(null)
                    }
                    notifyDisableCompletion(completion, completionNotified, null)
                }

                override fun onFailure(error: IDJIError) {
                    val commandError = djiError(error)
                    BridgeCommandJournal.journal.fail(commandId, commandError)
                    if (activeDisableCommandId.compareAndSet(commandId, null)) {
                        disableRequested.set(false)
                        // Reject new setpoints but keep sending neutral and retrying
                        // until DJI reports that MSDK authority was actually released.
                        requestedMode.set(VirtualControlMode.DISABLED)
                        neutralMode?.let(failsafeNeutralMode::set)
                        BridgeState.virtualStickControlMode.set(
                            VirtualControlMode.DISABLED.wireName
                        )
                        BridgeState.controlFailsafeState.set("disable_failed_neutral")
                        BridgeState.lastEvent.set(
                            "virtual_stick_disable_failed:$commandId:$error"
                        )
                    }
                    notifyDisableCompletion(
                        completion,
                        completionNotified,
                        commandError
                    )
                }
            })
        } catch (error: Exception) {
            val commandError = bridgeException(
                "virtual_stick_disable_dispatch_failed",
                error
            )
            BridgeCommandJournal.journal.fail(
                commandId,
                commandError
            )
            if (activeDisableCommandId.compareAndSet(commandId, null)) {
                disableRequested.set(false)
                requestedMode.set(VirtualControlMode.DISABLED)
                neutralMode?.let(failsafeNeutralMode::set)
                BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
                BridgeState.controlFailsafeState.set("disable_failed_neutral")
                BridgeState.lastEvent.set(
                    "virtual_stick_disable_dispatch_failed:$commandId:${error.message}"
                )
            }
            notifyDisableCompletion(completion, completionNotified, commandError)
        }
    }

    private fun notifyDisableCompletion(
        completion: ((CommandError?) -> Unit)?,
        completionNotified: AtomicBoolean,
        error: CommandError?
    ) {
        if (completion == null || !completionNotified.compareAndSet(false, true)) return
        runCatching { completion(error) }
            .onFailure { failure ->
                Log.e("VeilDjiBridge", "Virtual-stick completion failed", failure)
            }
    }

    private fun finishDisabled(event: String) {
        disableRequested.set(false)
        activeDisableCommandId.set(null)
        failsafeNeutralMode.set(null)
        requestedMode.set(VirtualControlMode.DISABLED)
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)
        hadMsdkAuthority.set(false)
        BridgeState.virtualStickEnabled.set(false)
        BridgeState.virtualStickAdvancedMode.set(false)
        BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
        BridgeState.controlFailsafeState.set("disarmed")
        BridgeState.lastEvent.set(event)
    }

    private fun failEnable(commandId: String, error: CommandError) {
        // Ignore a callback that lost the race to the watchdog or a prior
        // terminal callback; that path has already entered its safety state.
        if (!enableGate.release(commandId)) return
        val failedMode = requestedMode.get().takeUnless {
            it == VirtualControlMode.DISABLED
        } ?: if (BridgeState.virtualStickAdvancedMode.get()) {
            VirtualControlMode.BODY_VELOCITY
        } else {
            VirtualControlMode.STICKS
        }
        requestedMode.set(VirtualControlMode.DISABLED)
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)
        BridgeControlSession.rotate("enable_failed")
        BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
        BridgeCommandJournal.journal.fail(commandId, error)
        // An authority grant and the failure callback are delivered on
        // independent paths. Treat every failed enable as uncertain until an
        // explicit disable succeeds or authority release is observed.
        failsafeNeutralMode.set(failedMode)
        BridgeState.controlFailsafeState.set("enable_failed_disabling")
        bestEffortNeutralize(failedMode, includeAdvancedNeutral = true)
        if (!disableRequested.get()) {
            runCatching { requestVirtualStickDisable("enable_failed") }
        }
    }

    private fun authorityReleasedToRcForCurrentConnection(): Boolean {
        val aircraft = AircraftTelemetryState.snapshot()
        val connectionAt = aircraft.connectionUpdatedAtMonotonicMs ?: return false
        val authorityAt = aircraft.authority.stateUpdatedAtMonotonicMs ?: return false
        val automaticVerticalAction = aircraft.flightMode.uppercase().let { mode ->
            "TAKEOFF" in mode || "LANDING" in mode
        }
        return aircraft.aircraftConnected &&
            authorityAt >= connectionAt &&
            !automaticVerticalAction &&
            aircraft.safety.landingConfirmationNeeded != true &&
            aircraft.authority.owner == FlightControlAuthority.RC.name &&
            !aircraft.authority.virtualStickEnabled &&
            BridgeState.flightControlAuthority.get() == FlightControlAuthority.RC.name &&
            !BridgeState.virtualStickEnabled.get() &&
            !hadMsdkAuthority.get() &&
            !virtualStickReleaseRequired()
    }

    private fun groundConfigurationReady(): Boolean {
        val aircraft = AircraftTelemetryState.snapshot()
        val connectionAt = aircraft.connectionUpdatedAtMonotonicMs ?: return false
        val flyingAt = aircraft.isFlyingUpdatedAtMonotonicMs ?: return false
        val motorsAt = aircraft.motorsOnUpdatedAtMonotonicMs ?: return false
        return controlLinksReady() &&
            aircraft.aircraftConnected &&
            flyingAt >= connectionAt &&
            motorsAt >= connectionAt &&
            !aircraft.isFlying &&
            !aircraft.motorsOn &&
            authorityReleasedToRcForCurrentConnection()
    }

    private fun requestCommand(
        type: String,
        details: Map<String, String> = emptyMap()
    ): CommandRecord = BridgeCommandJournal.journal.request(
        type,
        BridgeCommandJournal.capturePreconditions(),
        details
    )

    private fun currentCommand(command: CommandRecord): CommandRecord =
        BridgeCommandJournal.journal.get(command.id) ?: command

    private fun djiActionResult(action: String): Map<String, String> = mapOf(
        "action" to action,
        "callback" to "dji_action_succeeded",
        "physical_completion" to "not_implied"
    )

    private fun bridgeSetpointResult(mode: VirtualControlMode): Map<String, String> = mapOf(
        "mode" to mode.wireName,
        "callback" to "bridge_latest_setpoint_accepted",
        "physical_completion" to "not_implied"
    )

    private fun djiError(error: IDJIError): CommandError = CommandError(
        source = "dji",
        type = errorField { error.errorType() },
        code = errorField { error.errorCode() },
        innerCode = errorField { error.innerCode() },
        description = errorField { error.description() },
        hint = errorField { error.hint() },
        raw = error.toString()
    )

    private fun bridgeError(code: String, description: String): CommandError = CommandError(
        source = "bridge",
        type = "bridge_state",
        code = code,
        description = description
    )

    private fun bridgeException(code: String, error: Exception): CommandError = CommandError(
        source = "bridge",
        type = error.javaClass.name,
        code = code,
        description = error.message,
        raw = error.toString()
    )

    private fun errorField(value: () -> Any?): String? =
        runCatching { value()?.toString() }.getOrNull()

    private fun handleAuthorityLoss(reason: String) {
        BridgeControlSession.rotate(reason)
        failsafeNeutralMode.set(null)
        requestedMode.set(VirtualControlMode.DISABLED)
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        neutralSent.set(true)
        BridgeState.virtualStickControlMode.set(VirtualControlMode.DISABLED.wireName)
        BridgeState.controlFailsafeState.set("authority_lost")
    }

    private fun isReady(mode: VirtualControlMode): Boolean =
        requestedMode.get() == mode &&
            failsafeNeutralMode.get() == null &&
            !disableRequested.get() &&
            controlLinksReady() &&
            BridgeState.virtualStickEnabled.get() &&
            BridgeState.flightControlAuthority.get() == FlightControlAuthority.MSDK.name &&
            (mode != VirtualControlMode.BODY_VELOCITY ||
                BridgeState.virtualStickAdvancedMode.get())

    private fun controlLinksReady(): Boolean =
        BridgeState.productConnected.get() &&
            BridgeState.remoteControllerConnected.get() &&
            BridgeState.airLinkConnected.get() &&
            BridgeState.aircraftConnected.get()

    fun close() {
        if (!closed.compareAndSet(false, true)) return
        failPendingTakeoffForLinkLoss("controller_closed")
        failPendingActionForLinkLoss(
            enableGate,
            "virtual_stick_enable",
            "controller_closed"
        )
        failPendingActionForLinkLoss(landingGate, "landing", "controller_closed")
        failPendingActionForLinkLoss(
            confirmLandingGate,
            "landing_confirmation",
            "controller_closed"
        )
        failPendingActionForLinkLoss(
            failsafeActionGate,
            "failsafe_action",
            "controller_closed"
        )
        BridgeControlSession.rotate("controller_closed")
        latestCommand.set(null)
        lastCommandNanos.set(0L)
        bestEffortNeutralize(requestedMode.get(), includeAdvancedNeutral = true)
        runCatching {
            manager.disableVirtualStick(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() = Unit
                override fun onFailure(error: IDJIError) = Unit
            })
        }
        manager.removeVirtualStickStateListener(stateListener)
        scheduler.shutdownNow()
        finishDisabled("controller_closed")
    }

    private companion object {
        const val CONTROL_PERIOD_MILLIS = 50L // 20 Hz, within DJI's documented 5-25 Hz.
        const val NEUTRAL_TIMEOUT_MILLIS = 300L
        const val DISABLE_TIMEOUT_MILLIS = 1_000L
        const val TAKEOFF_CALLBACK_TIMEOUT_MILLIS = 10_000L
        const val TAKEOFF_POST_ACCEPT_GUARD_MILLIS = 5_000L
        const val VIRTUAL_STICK_CALLBACK_TIMEOUT_MILLIS = 5_000L
        const val FAILSAFE_DISABLE_RETRY_MILLIS = 1_000L
        const val LANDING_CALLBACK_TIMEOUT_MILLIS = 12_000L
        const val LANDING_POST_ACCEPT_GUARD_MILLIS = 5_000L
        const val LANDING_CONFIRM_CALLBACK_TIMEOUT_MILLIS = 10_000L
        const val FAILSAFE_ACTION_CALLBACK_TIMEOUT_MILLIS = 5_000L
        const val LANDING_AUTHORITY_RECHECK_MILLIS = 50L
    }
}
