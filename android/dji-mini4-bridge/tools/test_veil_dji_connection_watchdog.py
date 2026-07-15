#!/usr/bin/env python3

import io
import json
import subprocess
import unittest

import veil_dji_connection_watchdog as watchdog


def disconnected_status(product_connected=True, product_type="UNRECOGNIZED"):
    return {
        "sdk_registered": True,
        "product_connected": product_connected,
        "product_type": product_type,
        "aircraft_connected": False,
    }


def connected_status():
    return {
        "sdk_registered": True,
        "product_connected": True,
        "product_type": "DJI_MINI_4_PRO",
        "aircraft_connected": True,
    }


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeStatusClient:
    def __init__(self, status):
        self.status = status

    def fetch(self):
        if isinstance(self.status, BaseException):
            raise self.status
        return dict(self.status)


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ConnectionRecoveryStateTest(unittest.TestCase):
    def test_bootstrap_recovery_happens_once_after_sustained_signature(self):
        state = watchdog.ConnectionRecoveryState(5.0, 10.0)
        status = disconnected_status()

        self.assertIsNone(state.observe(status, 0.0))
        self.assertIsNone(state.observe(status, 4.999))
        request = state.observe(status, 5.0)
        self.assertEqual(state.BOOTSTRAP_REASON, request.reason)
        state.complete(request, 5.0, performed=True)

        self.assertIsNone(state.observe(status, 15.0))
        self.assertIsNone(state.observe(status, 300.0))

    def test_healthy_connection_never_requests_usb_reset(self):
        state = watchdog.ConnectionRecoveryState(0.0, 0.0)
        for now in (0.0, 1.0, 100.0):
            self.assertIsNone(state.observe(connected_status(), now))

    def test_unexpected_drop_recovers_once_and_rearms_after_reconnect(self):
        state = watchdog.ConnectionRecoveryState(4.0, 0.0)
        self.assertIsNone(state.observe(connected_status(), 0.0))
        # A product disconnect is still an unexpected aircraft-link drop. It
        # does not need the special bootstrap UNRECOGNIZED signature.
        dropped = disconnected_status(product_connected=False, product_type="NONE")
        self.assertIsNone(state.observe(dropped, 1.0))
        self.assertIsNone(state.observe(dropped, 4.999))
        first = state.observe(dropped, 5.0)
        self.assertEqual(state.DROP_REASON, first.reason)
        state.complete(first, 5.0, performed=True)
        self.assertIsNone(state.observe(dropped, 100.0))

        self.assertIsNone(state.observe(connected_status(), 101.0))
        self.assertIsNone(state.observe(dropped, 102.0))
        second = state.observe(dropped, 106.0)
        self.assertEqual(state.DROP_REASON, second.reason)
        self.assertNotEqual(first.generation, second.generation)

    def test_grace_and_global_cooldown_both_apply(self):
        state = watchdog.ConnectionRecoveryState(5.0, 20.0)
        stuck = disconnected_status()
        self.assertIsNone(state.observe(stuck, 0.0))
        bootstrap = state.observe(stuck, 5.0)
        state.complete(bootstrap, 5.0, performed=True)

        self.assertIsNone(state.observe(connected_status(), 6.0))
        self.assertIsNone(state.observe(stuck, 7.0))
        # Grace is complete here, but the successful bootstrap reset is still
        # inside the global cooldown.
        self.assertIsNone(state.observe(stuck, 12.0))
        drop = state.observe(stuck, 25.0)
        self.assertEqual(state.DROP_REASON, drop.reason)

    def test_reconnect_cancels_pending_drop_and_restarts_its_grace(self):
        state = watchdog.ConnectionRecoveryState(5.0, 0.0)
        state.observe(connected_status(), 0.0)
        state.observe(disconnected_status(), 1.0)
        self.assertIsNone(state.observe(connected_status(), 4.0))
        state.observe(disconnected_status(), 5.0)
        self.assertIsNone(state.observe(disconnected_status(), 9.999))
        self.assertIsNotNone(state.observe(disconnected_status(), 10.0))

    def test_status_error_breaks_sustained_disconnect_evidence(self):
        state = watchdog.ConnectionRecoveryState(5.0, 0.0)
        stuck = disconnected_status()
        state.observe(stuck, 0.0)
        state.observe_error()
        self.assertIsNone(state.observe(stuck, 5.0))
        self.assertIsNotNone(state.observe(stuck, 10.0))


class AdbRecoveryTest(unittest.TestCase):
    def test_adb_environment_strips_secret_and_preserves_other_values(self):
        sentinel = "bridge-secret-sentinel-7fb03b4d"
        source_environment = {
            "PATH": "/safe/bin",
            "KEEP_ME": "preserved",
            "VEIL_DJI_TOKEN": sentinel,
        }
        runner = FakeRunner([completed(stdout="device\n"), completed()])
        recovery = watchdog.AdbRecovery(
            "adb",
            "boox",
            1.0,
            runner=runner,
            environment=source_environment,
        )

        self.assertEqual((True, "usb_reset_requested"), recovery.recover())
        for _command, kwargs in runner.calls:
            child_environment = kwargs["env"]
            self.assertNotIn("VEIL_DJI_TOKEN", child_environment)
            self.assertEqual("/safe/bin", child_environment["PATH"])
            self.assertEqual("preserved", child_environment["KEEP_ME"])
        # Sanitizing the child copy must not mutate the caller's environment.
        self.assertEqual(sentinel, source_environment["VEIL_DJI_TOKEN"])

    def test_adb_failure_prevents_usb_mutation(self):
        runner = FakeRunner([completed(returncode=1, stderr="offline")])
        recovery = watchdog.AdbRecovery(
            "/opt/adb", "boox:5555", 2.0, runner=runner
        )

        performed, outcome = recovery.recover()

        self.assertFalse(performed)
        self.assertEqual("adb_unreachable", outcome)
        self.assertEqual([["/opt/adb", "-s", "boox:5555", "get-state"]], [
            call[0] for call in runner.calls
        ])
        self.assertNotIn("shell", runner.calls[0][1])

    def test_reachable_device_runs_exact_reset_without_a_shell(self):
        runner = FakeRunner([
            completed(stdout="device\n"),
            completed(),
        ])
        recovery = watchdog.AdbRecovery(
            "adb", "boox:5555", 2.0, runner=runner
        )

        performed, outcome = recovery.recover()

        self.assertTrue(performed)
        self.assertEqual("usb_reset_requested", outcome)
        self.assertEqual(
            [
                "adb", "-s", "boox:5555", "shell", "svc", "usb",
                "setFunctions", "none",
            ],
            runner.calls[1][0],
        )
        for _command, kwargs in runner.calls:
            self.assertNotIn("shell", kwargs)
            self.assertTrue(kwargs["capture_output"])

    def test_nonzero_reset_completion_consumes_disconnect_episode(self):
        state = watchdog.ConnectionRecoveryState(0.0, 0.0)
        runner = FakeRunner([
            completed(stdout="device\n"),
            completed(returncode=1, stderr="transport closed"),
        ])
        recovery = watchdog.AdbRecovery("adb", "boox", 1.0, runner=runner)
        stuck = disconnected_status()

        request = state.observe(stuck, 0.0)
        performed, outcome = recovery.recover()
        state.complete(request, 0.0, performed)

        self.assertTrue(performed)
        self.assertEqual("usb_reset_requested_nonzero", outcome)
        self.assertIsNone(state.observe(stuck, 1_000.0))
        self.assertEqual(2, len(runner.calls))

    def test_reset_timeout_is_latched_because_child_was_launched(self):
        runner = FakeRunner([
            completed(stdout="device\n"),
            subprocess.TimeoutExpired(["adb"], timeout=1.0),
        ])
        recovery = watchdog.AdbRecovery("adb", "boox", 1.0, runner=runner)

        self.assertEqual(
            (True, "usb_reset_requested_timeout"),
            recovery.recover(),
        )

    def test_reset_exec_failure_does_not_claim_a_performed_mutation(self):
        runner = FakeRunner([
            completed(stdout="device\n"),
            OSError("exec failed"),
        ])
        recovery = watchdog.AdbRecovery("adb", "boox", 1.0, runner=runner)

        self.assertEqual(
            (False, "usb_reset_not_launched"),
            recovery.recover(),
        )

    def test_adb_failure_can_retry_after_cooldown_then_latches(self):
        state = watchdog.ConnectionRecoveryState(0.0, 10.0)
        runner = FakeRunner([
            completed(returncode=1),
            completed(stdout="device\n"),
            completed(),
        ])
        recovery = watchdog.AdbRecovery("adb", "boox", 1.0, runner=runner)
        stuck = disconnected_status()

        first = state.observe(stuck, 0.0)
        performed, _outcome = recovery.recover()
        state.complete(first, 0.0, performed)
        self.assertFalse(performed)
        self.assertIsNone(state.observe(stuck, 9.999))

        second = state.observe(stuck, 10.0)
        performed, _outcome = recovery.recover()
        state.complete(second, 10.0, performed)
        self.assertTrue(performed)
        self.assertIsNone(state.observe(stuck, 100.0))

    def test_dry_run_does_not_invoke_adb(self):
        runner = FakeRunner([])
        recovery = watchdog.AdbRecovery(
            "adb", "boox", 1.0, runner=runner, dry_run=True
        )
        self.assertEqual((True, "dry_run"), recovery.recover())
        self.assertEqual([], runner.calls)


class ConnectionWatchdogTest(unittest.TestCase):
    def test_poll_uses_fake_status_clock_runner_and_emits_secret_free_ndjson(self):
        sentinel = "bridge-secret-sentinel-2ad891e7"
        clock = FakeClock()
        status = FakeStatusClient(disconnected_status())
        runner = FakeRunner([completed(stdout="device\n"), completed()])
        recovery = watchdog.AdbRecovery(
            "adb",
            "boox",
            1.0,
            runner=runner,
            environment={"PATH": "/safe/bin", "VEIL_DJI_TOKEN": sentinel},
        )
        output = io.StringIO()
        logger = watchdog.NdjsonLogger(output, wall_clock=lambda: 1_700_000_000.0)
        service = watchdog.ConnectionWatchdog(
            status,
            watchdog.ConnectionRecoveryState(2.0, 10.0),
            recovery,
            logger,
            monotonic=clock,
        )

        service.poll_once()
        clock.advance(2.0)
        service.poll_once()

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            ["connection_state", "recovery_due", "recovery_result"],
            [record["event"] for record in records],
        )
        self.assertEqual("usb_reset_requested", records[-1]["outcome"])
        self.assertNotIn(sentinel, output.getvalue())
        for _command, kwargs in runner.calls:
            self.assertNotIn("VEIL_DJI_TOKEN", kwargs["env"])

    def test_subprocess_stdout_stderr_and_errors_cannot_leak_secret_to_logs(self):
        sentinel = "bridge-secret-sentinel-65f92c18"
        scenarios = {
            "get_state_output": [
                completed(returncode=1, stdout=sentinel, stderr=sentinel),
            ],
            "reset_nonzero_output": [
                completed(stdout="device\n"),
                completed(returncode=1, stdout=sentinel, stderr=sentinel),
            ],
            "reset_exec_error": [
                completed(stdout="device\n"),
                OSError(sentinel),
            ],
            "reset_timeout": [
                completed(stdout="device\n"),
                subprocess.TimeoutExpired(
                    ["adb"], timeout=1.0, output=sentinel, stderr=sentinel
                ),
            ],
        }

        for label, results in scenarios.items():
            with self.subTest(label=label):
                output = io.StringIO()
                runner = FakeRunner(results)
                service = watchdog.ConnectionWatchdog(
                    FakeStatusClient(disconnected_status()),
                    watchdog.ConnectionRecoveryState(0.0, 10.0),
                    watchdog.AdbRecovery(
                        "adb",
                        "boox",
                        1.0,
                        runner=runner,
                        environment={
                            "PATH": "/safe/bin",
                            "VEIL_DJI_TOKEN": sentinel,
                        },
                    ),
                    watchdog.NdjsonLogger(
                        output, wall_clock=lambda: 1_700_000_000.0
                    ),
                    monotonic=lambda: 0.0,
                )

                service.poll_once()

                self.assertNotIn(sentinel, output.getvalue())
                for _command, kwargs in runner.calls:
                    self.assertNotIn("VEIL_DJI_TOKEN", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
