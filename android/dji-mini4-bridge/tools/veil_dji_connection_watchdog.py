#!/usr/bin/env python3
"""Recover a stuck BOOX-to-RC-N2 DJI USB accessory connection.

The watchdog intentionally has a narrow remit: it observes the authenticated
bridge status endpoint and, for a latched disconnect episode, asks Android to
re-enumerate its USB functions with the same command that has recovered the
RC-N2 accessory session during testing.  It never sends a flight command.
"""

import argparse
import dataclasses
import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


DEFAULT_HOST = os.getenv("VEIL_DJI_HOST", "127.0.0.1")
DEFAULT_CONTROL_PORT = 8765
DEFAULT_ADB_DEVICE = os.getenv("VEIL_DJI_ADB_DEVICE", f"{DEFAULT_HOST}:5555")
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_GRACE_SECONDS = 8.0
DEFAULT_COOLDOWN_SECONDS = 30.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 3.0
DEFAULT_ADB_TIMEOUT_SECONDS = 5.0
MAX_STATUS_BYTES = 2 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class RecoveryRequest:
    generation: int
    reason: str
    disconnected_since: float


class ConnectionRecoveryState:
    """Pure connection-state/timing policy, independent of I/O."""

    BOOTSTRAP_REASON = "bootstrap_unrecognized_product"
    DROP_REASON = "aircraft_connection_dropped"

    def __init__(self, grace_seconds, cooldown_seconds):
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        self.grace_seconds = float(grace_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self.ever_aircraft_connected = False
        self.bootstrap_recovered = False
        self.drop_recovered_for_episode = False
        self.bootstrap_pending_since = None
        self.drop_pending_since = None
        self.last_attempt_at = None
        self._inflight = None
        self._generation = 0

    @staticmethod
    def _is_true(value):
        return value is True

    @staticmethod
    def _is_false(value):
        return value is False

    @staticmethod
    def _is_unrecognized(value):
        if not isinstance(value, str):
            return False
        return value.strip().upper().rsplit(".", 1)[-1] == "UNRECOGNIZED"

    def _cooldown_complete(self, now):
        return (
            self.last_attempt_at is None
            or now - self.last_attempt_at >= self.cooldown_seconds
        )

    def _new_request(self, reason, pending_since):
        self._generation += 1
        request = RecoveryRequest(self._generation, reason, pending_since)
        self._inflight = request
        return request

    def _eligible(self, pending_since, now):
        return (
            pending_since is not None
            and now - pending_since >= self.grace_seconds
            and self._cooldown_complete(now)
            and self._inflight is None
        )

    def observe(self, status, now):
        """Observe one valid status document and optionally request recovery."""
        if not isinstance(status, dict):
            self.observe_error()
            return None

        connected = status.get("aircraft_connected")
        if self._is_true(connected):
            self.ever_aircraft_connected = True
            self.bootstrap_pending_since = None
            self.drop_pending_since = None
            self.drop_recovered_for_episode = False
            return None

        # Missing or non-boolean connection state is not evidence of a sustained
        # disconnect. Preserve historical latches, but restart both grace timers.
        if not self._is_false(connected):
            self.observe_error()
            return None

        sdk_ready = self._is_true(status.get("sdk_registered"))
        bootstrap_signature = (
            not self.ever_aircraft_connected
            and sdk_ready
            and self._is_true(status.get("product_connected"))
            and self._is_unrecognized(status.get("product_type"))
        )

        if bootstrap_signature and not self.bootstrap_recovered:
            if self.bootstrap_pending_since is None:
                self.bootstrap_pending_since = now
        else:
            self.bootstrap_pending_since = None

        # Once this watchdog has seen a real aircraft connection, any subsequent
        # SDK-ready disconnect is a drop episode. It is latched after one
        # successful recovery until a real reconnect, so powering the aircraft
        # off cannot cause periodic USB churn.
        if self.ever_aircraft_connected and sdk_ready:
            if self.drop_pending_since is None:
                self.drop_pending_since = now
        else:
            self.drop_pending_since = None

        if (
            not self.drop_recovered_for_episode
            and self._eligible(self.drop_pending_since, now)
        ):
            return self._new_request(self.DROP_REASON, self.drop_pending_since)

        if (
            not self.bootstrap_recovered
            and self._eligible(self.bootstrap_pending_since, now)
        ):
            return self._new_request(
                self.BOOTSTRAP_REASON, self.bootstrap_pending_since
            )
        return None

    def observe_error(self):
        """Break sustained-evidence timers after an invalid/failed status poll."""
        self.bootstrap_pending_since = None
        self.drop_pending_since = None

    def complete(self, request, now, performed):
        """Record an I/O attempt; only a performed reset consumes the episode."""
        if request != self._inflight:
            raise ValueError("recovery request is not the current in-flight request")
        self._inflight = None
        self.last_attempt_at = now
        if not performed:
            return
        if request.reason == self.BOOTSTRAP_REASON:
            self.bootstrap_recovered = True
            self.bootstrap_pending_since = None
        elif request.reason == self.DROP_REASON:
            self.drop_recovered_for_episode = True
            self.drop_pending_since = None
        else:
            raise ValueError("unknown recovery reason")


class StatusClient:
    def __init__(self, host, control_port, token, timeout_seconds):
        self.url = "http://{}:{}/status".format(host, control_port)
        self.token = token
        self.timeout_seconds = timeout_seconds

    def fetch(self):
        request = urllib.request.Request(
            self.url,
            method="GET",
            headers={"X-Veil-Token": self.token},
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout_seconds
        ) as response:
            payload = response.read(MAX_STATUS_BYTES + 1)
        if len(payload) > MAX_STATUS_BYTES:
            raise ValueError("status response exceeds size limit")
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("status response is not an object")
        return parsed


class AdbRecovery:
    def __init__(
        self,
        adb,
        device,
        timeout_seconds,
        runner=subprocess.run,
        dry_run=False,
        environment=None,
    ):
        self.adb = adb
        self.device = device
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.dry_run = dry_run
        self.subprocess_environment = dict(
            os.environ if environment is None else environment
        )
        # The bridge credential is needed only by StatusClient. adb never needs
        # it, so do not expose it to that child process or any configured adb
        # wrapper while preserving PATH and every unrelated environment value.
        self.subprocess_environment.pop("VEIL_DJI_TOKEN", None)

    def _run(self, command):
        try:
            return self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=self.subprocess_environment,
            ), None
        except subprocess.TimeoutExpired:
            # subprocess.run only raises TimeoutExpired after successfully
            # starting the child. For the USB-reset command that is enough to
            # consume the episode: Android may have torn down the transport
            # before adb could report completion.
            return None, "timeout_after_launch"
        except OSError:
            return None, "not_launched"
        except subprocess.SubprocessError:
            # Other subprocess errors do not prove that the child failed to
            # launch. Conservatively avoid issuing a duplicate USB mutation.
            return None, "subprocess_error_after_launch"

    def recover(self):
        """Return (performed, outcome) without exposing command output."""
        if self.dry_run:
            return True, "dry_run"

        state, _state_error = self._run(
            [self.adb, "-s", self.device, "get-state"]
        )
        if (
            state is None
            or state.returncode != 0
            or state.stdout.strip() != "device"
        ):
            return False, "adb_unreachable"

        reset, reset_error = self._run([
            self.adb,
            "-s",
            self.device,
            "shell",
            "svc",
            "usb",
            "setFunctions",
            "none",
        ])
        if reset_error == "not_launched":
            return False, "usb_reset_not_launched"
        if reset_error == "timeout_after_launch":
            return True, "usb_reset_requested_timeout"
        if reset_error is not None:
            return True, "usb_reset_requested_unknown_result"
        if reset.returncode != 0:
            # `svc usb setFunctions none` deliberately tears down USB. adb can
            # therefore return nonzero even though Android performed exactly
            # the requested mutation; live validation observed this behavior.
            return True, "usb_reset_requested_nonzero"
        return True, "usb_reset_requested"


class NdjsonLogger:
    def __init__(self, stream=None, wall_clock=None):
        self.stream = stream or sys.stdout
        self.wall_clock = wall_clock or time.time

    def emit(self, event, **fields):
        timestamp = datetime.datetime.fromtimestamp(
            self.wall_clock(), datetime.timezone.utc
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record = {"time": timestamp, "event": event}
        record.update(fields)
        self.stream.write(json.dumps(record, sort_keys=True) + "\n")
        self.stream.flush()


class ConnectionWatchdog:
    def __init__(
        self,
        status_client,
        state,
        recovery,
        logger,
        monotonic=time.monotonic,
    ):
        self.status_client = status_client
        self.state = state
        self.recovery = recovery
        self.logger = logger
        self.monotonic = monotonic
        self._last_summary = None
        self._status_error_active = False

    @staticmethod
    def _summary(status):
        return {
            "sdk_registered": status.get("sdk_registered") is True,
            "product_connected": status.get("product_connected") is True,
            "product_type": (
                status.get("product_type")
                if isinstance(status.get("product_type"), str)
                else None
            ),
            "aircraft_connected": status.get("aircraft_connected") is True,
        }

    def poll_once(self):
        try:
            status = self.status_client.fetch()
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
            self.state.observe_error()
            if not self._status_error_active:
                self.logger.emit("status_unavailable")
            self._status_error_active = True
            return

        if self._status_error_active:
            self.logger.emit("status_restored")
        self._status_error_active = False
        summary = self._summary(status)
        if summary != self._last_summary:
            self.logger.emit("connection_state", **summary)
            self._last_summary = summary

        now = self.monotonic()
        request = self.state.observe(status, now)
        if request is None:
            return
        self.logger.emit(
            "recovery_due",
            reason=request.reason,
            disconnected_seconds=round(now - request.disconnected_since, 3),
        )
        performed, outcome = self.recovery.recover()
        completed_at = self.monotonic()
        self.state.complete(request, completed_at, performed)
        self.logger.emit(
            "recovery_result",
            reason=request.reason,
            outcome=outcome,
            performed=performed,
        )


def _positive_float(value):
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_float(value):
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Automatically recover a stuck BOOX/RC-N2 USB accessory link"
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    parser.add_argument("--device", default=DEFAULT_ADB_DEVICE)
    parser.add_argument(
        "--poll-seconds", type=_positive_float, default=DEFAULT_POLL_SECONDS
    )
    parser.add_argument(
        "--grace-seconds", type=_nonnegative_float, default=DEFAULT_GRACE_SECONDS
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=_nonnegative_float,
        default=DEFAULT_COOLDOWN_SECONDS,
    )
    parser.add_argument(
        "--http-timeout-seconds",
        type=_positive_float,
        default=DEFAULT_HTTP_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--adb-timeout-seconds",
        type=_positive_float,
        default=DEFAULT_ADB_TIMEOUT_SECONDS,
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    token = os.environ.get("VEIL_DJI_TOKEN")
    if not token:
        raise SystemExit("VEIL_DJI_TOKEN is required")

    logger = NdjsonLogger()
    stop = threading.Event()

    def request_stop(signum, _frame):
        logger.emit("shutdown_requested", signal=signum)
        stop.set()

    old_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.signal(signum, request_stop)

    watchdog = ConnectionWatchdog(
        status_client=StatusClient(
            args.host, args.control_port, token, args.http_timeout_seconds
        ),
        state=ConnectionRecoveryState(args.grace_seconds, args.cooldown_seconds),
        recovery=AdbRecovery(
            args.adb,
            args.device,
            args.adb_timeout_seconds,
            dry_run=args.dry_run,
        ),
        logger=logger,
    )
    logger.emit(
        "watchdog_started",
        host=args.host,
        control_port=args.control_port,
        adb_device=args.device,
        poll_seconds=args.poll_seconds,
        grace_seconds=args.grace_seconds,
        cooldown_seconds=args.cooldown_seconds,
        dry_run=args.dry_run,
    )
    try:
        while not stop.is_set():
            watchdog.poll_once()
            stop.wait(args.poll_seconds)
    finally:
        logger.emit("watchdog_stopped")
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
