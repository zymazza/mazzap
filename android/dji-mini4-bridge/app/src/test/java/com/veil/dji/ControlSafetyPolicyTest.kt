package com.veil.dji

import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ControlSafetyPolicyTest {
    @Test
    fun crossActionGatesBlockTakeoffAndAuthorityTransitions() {
        assertTrue(CrossActionGatePolicy.blocksTakeoff(
            enablePending = true,
            disablePending = false,
            landingPending = false,
            landingConfirmationPending = false
        ))
        assertTrue(CrossActionGatePolicy.blocksTakeoff(
            enablePending = false,
            disablePending = false,
            landingPending = true,
            landingConfirmationPending = false
        ))
        assertFalse(CrossActionGatePolicy.blocksTakeoff(
            enablePending = false,
            disablePending = false,
            landingPending = false,
            landingConfirmationPending = false
        ))
        assertTrue(CrossActionGatePolicy.blocksVirtualStickEnable(
            takeoffPending = true,
            landingPending = false,
            landingConfirmationPending = false
        ))
        assertTrue(CrossActionGatePolicy.blocksVirtualStickEnable(
            takeoffPending = false,
            landingPending = false,
            landingConfirmationPending = true
        ))
        assertFalse(CrossActionGatePolicy.blocksVirtualStickEnable(
            takeoffPending = false,
            landingPending = false,
            landingConfirmationPending = false
        ))
        assertFalse(TakeoffDispatchPolicy.mayDispatch(
            reservedCommandId = "takeoff-1",
            activeCommandId = "takeoff-1",
            landingPending = true,
            landingConfirmationPending = false
        ))
        assertTrue(TakeoffDispatchPolicy.mayDispatch(
            reservedCommandId = "takeoff-1",
            activeCommandId = "takeoff-1",
            landingPending = false,
            landingConfirmationPending = false
        ))
    }

    @Test
    fun landingCannotMasqueradeAsTakeoffCancellation() {
        assertTrue(LandingDispatchPolicy.mayReserve(
            takeoffPending = false,
            connectionUpdatedAtMonotonicMs = null,
            isFlyingUpdatedAtMonotonicMs = null,
            isFlying = false
        ))
        assertFalse(LandingDispatchPolicy.mayReserve(
            takeoffPending = true,
            connectionUpdatedAtMonotonicMs = 2_000L,
            isFlyingUpdatedAtMonotonicMs = null,
            isFlying = false
        ))
        assertFalse(LandingDispatchPolicy.mayReserve(
            takeoffPending = true,
            connectionUpdatedAtMonotonicMs = 2_000L,
            isFlyingUpdatedAtMonotonicMs = 1_999L,
            isFlying = true
        ))
        assertFalse(LandingDispatchPolicy.mayReserve(
            takeoffPending = true,
            connectionUpdatedAtMonotonicMs = 2_000L,
            isFlyingUpdatedAtMonotonicMs = 2_100L,
            isFlying = false
        ))
        assertTrue(LandingDispatchPolicy.mayReserve(
            takeoffPending = true,
            connectionUpdatedAtMonotonicMs = 2_000L,
            isFlyingUpdatedAtMonotonicMs = 2_100L,
            isFlying = true
        ))
    }

    @Test
    fun disableTimeoutRetainsNeutralAcrossLateAuthorityGrant() {
        assertEquals(
            VirtualControlMode.BODY_VELOCITY,
            DisableNeutralRetentionPolicy.modeToRetain(
                capturedMode = null,
                persistentMode = VirtualControlMode.BODY_VELOCITY,
                releaseStillRequired = false,
                releaseConfirmedToRc = true,
                advancedModeObserved = false
            )
        )
        assertEquals(
            VirtualControlMode.STICKS,
            DisableNeutralRetentionPolicy.modeToRetain(
                capturedMode = null,
                persistentMode = null,
                releaseStillRequired = true,
                releaseConfirmedToRc = false,
                advancedModeObserved = false
            )
        )
        assertEquals(
            VirtualControlMode.BODY_VELOCITY,
            DisableNeutralRetentionPolicy.modeToRetain(
                capturedMode = null,
                persistentMode = null,
                releaseStillRequired = true,
                releaseConfirmedToRc = false,
                advancedModeObserved = true
            )
        )
        assertNull(DisableNeutralRetentionPolicy.modeToRetain(
            capturedMode = null,
            persistentMode = null,
            releaseStillRequired = false,
            releaseConfirmedToRc = true,
            advancedModeObserved = false
        ))
        assertEquals(
            VirtualControlMode.STICKS,
            DisableNeutralRetentionPolicy.modeToRetain(
                capturedMode = null,
                persistentMode = null,
                releaseStillRequired = false,
                releaseConfirmedToRc = false,
                advancedModeObserved = false
            )
        )
    }

    @Test
    fun periodicTaskContainsTickAndFailureHandlerExceptions() {
        val ticks = AtomicInteger()
        val failures = AtomicInteger()
        val task = NonThrowingPeriodicTask(
            task = {
                if (ticks.getAndIncrement() == 0) error("first tick failed")
            },
            onFailure = {
                failures.incrementAndGet()
                error("failure handler also failed")
            }
        )

        task.run()
        task.run()

        assertEquals(2, ticks.get())
        assertEquals(1, failures.get())
    }

    @Test
    fun releasePolicyCoversArmingLinkLossAndLateAuthority() {
        assertTrue(VirtualStickSafetyPolicy.releaseRequired(
            requestedMode = VirtualControlMode.BODY_VELOCITY,
            previouslyHadMsdkAuthority = false,
            virtualStickEnabled = false,
            authorityOwner = "RC"
        ))
        assertTrue(VirtualStickSafetyPolicy.releaseRequired(
            requestedMode = VirtualControlMode.DISABLED,
            previouslyHadMsdkAuthority = false,
            virtualStickEnabled = true,
            authorityOwner = "MSDK"
        ))
        assertFalse(VirtualStickSafetyPolicy.releaseRequired(
            requestedMode = VirtualControlMode.DISABLED,
            previouslyHadMsdkAuthority = false,
            virtualStickEnabled = false,
            authorityOwner = "RC"
        ))
        assertTrue(
            VirtualStickSafetyPolicy.authorityGrantIsUnexpected(
                VirtualControlMode.DISABLED
            )
        )
        assertFalse(
            VirtualStickSafetyPolicy.authorityGrantIsUnexpected(
                VirtualControlMode.STICKS
            )
        )
        assertTrue(VirtualStickSafetyPolicy.releaseConfirmedToRc(
            requestedMode = VirtualControlMode.DISABLED,
            previouslyHadMsdkAuthority = false,
            virtualStickEnabled = false,
            authorityOwner = "RC"
        ))
        assertFalse(VirtualStickSafetyPolicy.releaseConfirmedToRc(
            requestedMode = VirtualControlMode.DISABLED,
            previouslyHadMsdkAuthority = false,
            virtualStickEnabled = false,
            authorityOwner = "UNKNOWN"
        ))
    }

    @Test
    fun singleFlightGateRejectsDuplicatesAndUsesCommandBoundRelease() {
        val gate = SingleFlightCommandGate()
        assertTrue(gate.tryAcquire("takeoff-1"))
        assertFalse(gate.tryAcquire("takeoff-2"))
        assertEquals("takeoff-1", gate.current())
        assertFalse(gate.release("takeoff-2"))
        assertEquals("takeoff-1", gate.current())
        assertTrue(gate.release("takeoff-1"))
        assertNull(gate.current())
        assertTrue(gate.tryAcquire("takeoff-2"))
    }

    @Test
    fun takeoffTimeoutRequiresBothFreshPostTimeoutGroundSamples() {
        assertFalse(TakeoffTimeoutReconciliationPolicy.freshGroundStateProvesNoTakeoff(
            timedOutAtMonotonicMs = 1_000L,
            isFlyingUpdatedAtMonotonicMs = 4_000L,
            motorsOnUpdatedAtMonotonicMs = 999L,
            isFlying = false,
            motorsOn = false
        ))
        assertFalse(TakeoffTimeoutReconciliationPolicy.freshGroundStateProvesNoTakeoff(
            timedOutAtMonotonicMs = 1_000L,
            isFlyingUpdatedAtMonotonicMs = 4_000L,
            motorsOnUpdatedAtMonotonicMs = 4_000L,
            isFlying = true,
            motorsOn = true
        ))
        assertTrue(TakeoffTimeoutReconciliationPolicy.freshGroundStateProvesNoTakeoff(
            timedOutAtMonotonicMs = 1_000L,
            isFlyingUpdatedAtMonotonicMs = 4_000L,
            motorsOnUpdatedAtMonotonicMs = 4_000L,
            isFlying = false,
            motorsOn = false
        ))
    }
}
