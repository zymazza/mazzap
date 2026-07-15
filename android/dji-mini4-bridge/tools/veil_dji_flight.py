#!/usr/bin/env python3
"""Persistent, low-latency Mac flight-control session for the VEIL DJI bridge.

The process owns one authenticated UDP V2 control session, refreshes the latest
setpoint at 20 Hz, and accepts newline-delimited JSON commands on stdin.  Manual
setpoint changes are transmitted immediately instead of waiting for the next
periodic refresh.  Navigation policy lives here on the Mac; the Android bridge
remains the thin DJI transport and truth source.
"""

import argparse
import errno
import hashlib
import hmac
import http.client
import json
import math
import os
import socket
import stat
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

from veil_dji_obstacle import (
    AvoidanceMode,
    MissingDataBehavior,
    ObstacleAvoidanceConfig,
    ObstacleObservation,
    limit_body_velocity,
)
from veil_dji_route import (
    AtomicRouteRevisionStore,
    RouteCommandReason,
    RouteParseError,
    RoutePhase,
    RouteTelemetry,
    ground_ned_to_body,
    parse_route_revision,
    route_capabilities_dict,
    state_to_dict,
)


CONTROL_PERIOD_SECONDS = 0.05
ROUTE_SETPOINT_LEASE_SECONDS = 0.20
TELEMETRY_MAX_ARRIVAL_AGE_SECONDS = 0.35
TELEMETRY_MAX_SOURCE_AGE_MS = 350.0
TELEMETRY_MAX_QUEUE_AGE_MS = 250.0
TELEMETRY_STALL_SECONDS = 0.60
TELEMETRY_IDLE_RECONNECT_SECONDS = 1.0
NYMPH_TELEMETRY_STALE_AFTER_MS = (
    TELEMETRY_MAX_ARRIVAL_AGE_SECONDS * 1000.0
)
AUTHORITY_WAIT_SECONDS = 8.0
COMMAND_CALLBACK_WAIT_SECONDS = 8.0
HANDOFF_NEUTRAL_SECONDS = 0.20
HANDOFF_WAIT_SECONDS = 8.0
GROUND_WAIT_SECONDS = 90.0
CONTEXT_RESYNC_SECONDS = 10.0
UINT64_MASK = (1 << 64) - 1
HORIZONTAL_MAPPING_CONSERVATIVE_GLOBAL = "conservative_global"
HORIZONTAL_MAPPING_BODY_CLOCKWISE_ZERO_FORWARD = (
    "body_clockwise_zero_forward"
)
HORIZONTAL_OBSTACLE_MAPPINGS = (
    HORIZONTAL_MAPPING_CONSERVATIVE_GLOBAL,
    HORIZONTAL_MAPPING_BODY_CLOCKWISE_ZERO_FORWARD,
)


CAPABILITIES = route_capabilities_dict()
CAPABILITIES["dji_obstacle_avoidance_integration"] = False
CAPABILITIES["host_obstacle_guard_available"] = True
CAPABILITIES["host_obstacle_guard_modes"] = [
    mode.value for mode in AvoidanceMode
]
CAPABILITIES["host_obstacle_horizontal_mappings"] = list(
    HORIZONTAL_OBSTACLE_MAPPINGS
)
CAPABILITIES["host_obstacle_default_horizontal_mapping"] = (
    HORIZONTAL_MAPPING_CONSERVATIVE_GLOBAL
)
CAPABILITIES["host_obstacle_calibrated_directional_mapping"] = False
CAPABILITIES["dji_firmware_obstacle_avoidance_under_virtual_stick"] = "unverified"


class FlightSessionError(RuntimeError):
    """Structured error surfaced unchanged by the JSON-lines API."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        result = {"ok": False, "state": "failed", "error": self.code,
                  "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


class BridgeRequestError(FlightSessionError):
    def __init__(self, status, method, path, body):
        super().__init__(
            "bridge_http_error",
            f"bridge returned HTTP {status} for {method} {path}",
            {"http_status": status, "method": method, "path": path,
             "response": body},
        )
        self.status = status
        self.body = body


class BridgeTransportError(FlightSessionError):
    """A bounded HTTP attempt failed before a trustworthy response arrived."""

    def __init__(self, method, path, error):
        super().__init__(
            "bridge_transport_error",
            f"bridge transport failed for {method} {path}",
            {
                "method": method,
                "path": path,
                "exception": type(error).__name__,
                "description": str(error),
            },
        )


class BridgeApi:
    def __init__(self, host, token, port=8765, timeout=3.0):
        self.host = host
        self.token = token
        self.port = port
        self.timeout = timeout

    def request(self, method, path):
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}{path}",
            method=method,
            headers={"X-Veil-Token": self.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"raw": raw.decode("utf-8", errors="replace")}
            raise BridgeRequestError(error.code, method, path, body) from error
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
        ) as error:
            raise BridgeTransportError(method, path, error) from error
        except (json.JSONDecodeError, UnicodeError, ValueError) as error:
            raise FlightSessionError(
                "bridge_response_invalid",
                f"bridge returned an invalid JSON response for {method} {path}",
                {
                    "method": method,
                    "path": path,
                    "exception": type(error).__name__,
                    "description": str(error),
                },
            ) from error

    def status(self):
        return self.request("GET", "/status")


@dataclass(frozen=True)
class TelemetrySnapshot:
    value: dict
    generation: int
    received_monotonic_ns: int

    @property
    def arrival_age_ms(self):
        return max(0.0, (time.monotonic_ns() - self.received_monotonic_ns) / 1e6)


class TelemetryFeed:
    """Continuously drains TCP telemetry and retains only the newest frame."""

    def __init__(self, host, token, port=8768, reconnect_delay=0.10):
        self.host = host
        self.token = token
        self.port = port
        self.reconnect_delay = reconnect_delay
        self._condition = threading.Condition()
        self._latest = None
        self._generation = 0
        self._last_sequence = None
        self._sequence_gaps = 0
        self._out_of_order = 0
        self._last_error = None
        self._reconnects = 0
        self._stop = threading.Event()
        self._socket = None
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="veil-dji-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        current = self._socket
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                current.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def latest(self):
        with self._condition:
            return self._latest

    def diagnostics(self):
        with self._condition:
            return {
                "generation": self._generation,
                "last_sequence": self._last_sequence,
                "sequence_gaps": self._sequence_gaps,
                "out_of_order_frames": self._out_of_order,
                "last_error": self._last_error,
                "reconnects": self._reconnects,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "arrival_age_ms": (
                    self._latest.arrival_age_ms if self._latest is not None else None
                ),
            }

    def wait_for(self, predicate, timeout, after_generation=None):
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                current = self._latest
                if (
                    current is not None
                    and (after_generation is None or
                         current.generation > after_generation)
                    and predicate(current)
                ):
                    return current
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _publish(self, value, received_ns):
        sequence = _integer_or_none(value.get("telemetry_sequence"))
        with self._condition:
            if sequence is not None and self._last_sequence is not None:
                if sequence <= self._last_sequence:
                    self._out_of_order += 1
                    return
                if sequence > self._last_sequence + 1:
                    self._sequence_gaps += sequence - self._last_sequence - 1
            if sequence is not None:
                self._last_sequence = sequence
            self._generation += 1
            self._latest = TelemetrySnapshot(value, self._generation, received_ns)
            self._last_error = None
            self._condition.notify_all()

    def _reset_connection_sequence(self):
        with self._condition:
            self._last_sequence = None

    def _run(self):
        while not self._stop.is_set():
            connection = None
            try:
                connection = socket.create_connection(
                    (self.host, self.port), timeout=2.0
                )
                self._socket = connection
                with self._condition:
                    self._reconnects += 1
                # telemetry_sequence is process/session scoped on Android. A
                # reconnect after an app restart may legitimately begin at 1.
                self._reset_connection_sequence()
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.sendall(f"TOKEN {self.token}\n".encode("utf-8"))
                # A half-open or server-forgotten TCP stream otherwise leaves
                # makefile iteration blocked forever. Telemetry is nominally
                # 20 Hz, so one silent second is a transport failure: discard
                # the connection and authenticate a fresh one.
                connection.settimeout(TELEMETRY_IDLE_RECONNECT_SECONDS)
                reader = connection.makefile("r", encoding="utf-8")
                for line in reader:
                    if self._stop.is_set():
                        return
                    received_ns = time.monotonic_ns()
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        self._publish(value, received_ns)
                raise OSError("telemetry socket closed")
            except (OSError, UnicodeError) as error:
                with self._condition:
                    self._last_error = f"{type(error).__name__}: {error}"
                    self._condition.notify_all()
                if not self._stop.wait(self.reconnect_delay):
                    continue
            finally:
                self._socket = None
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass


@dataclass(frozen=True)
class ControlContext:
    session_id: int
    session_hex: str
    clock_offset_ms: int


@dataclass(frozen=True)
class VelocityCommand:
    forward_mps: float = 0.0
    right_mps: float = 0.0
    up_mps: float = 0.0
    yaw_rate_deg_s: float = 0.0

    def __post_init__(self):
        values = (
            ("forward_mps", self.forward_mps, 23.0),
            ("right_mps", self.right_mps, 23.0),
            ("up_mps", self.up_mps, 6.0),
            ("yaw_rate_deg_s", self.yaw_rate_deg_s, 100.0),
        )
        for name, value, limit in values:
            if not math.isfinite(value) or abs(value) > limit:
                raise FlightSessionError(
                    "velocity_out_of_range",
                    f"{name} must be finite and within +/-{limit:g}",
                )

    @property
    def is_neutral(self):
        return self == VelocityCommand()

    def to_dict(self):
        return {
            "forward_mps": self.forward_mps,
            "right_mps": self.right_mps,
            "up_mps": self.up_mps,
            "yaw_rate_deg_s": self.yaw_rate_deg_s,
        }


def _avoidance_config_dict(config):
    return {
        "mode": config.mode.value,
        "minimum_clearance_m": config.minimum_clearance_m,
        "reaction_time_s": config.reaction_time_s,
        "maximum_deceleration_mps2": config.maximum_deceleration_mps2,
        "maximum_source_age_ms": config.maximum_source_age_ms,
        "missing_data_behavior": config.missing_data_behavior.value,
    }


def _finite_float_or_none(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _nymph_telemetry_snapshot(snapshot):
    """Return the small, scalar-only telemetry contract exposed to Nymph.

    This is intentionally constructed field by field.  Never replace it with a
    copy, filter, or recursive projection of the Android telemetry object: that
    object also contains stable device identifiers and other private state.
    """
    result = {
        "available": False,
        "arrival_age_ms": None,
        "fresh": False,
        "stale": True,
        "product_connected": None,
        "aircraft_connected": None,
        "remote_controller_connected": None,
        "airlink_connected": None,
        "flight_mode": None,
        "is_flying": None,
        "motors_on": None,
        "latitude_deg": None,
        "longitude_deg": None,
        "relative_altitude_m": None,
        "yaw_deg": None,
        "velocity_north_mps": None,
        "velocity_east_mps": None,
        "velocity_down_mps": None,
        "gps_signal_level": None,
        "gps_satellite_count": None,
        "battery_percent": None,
        "authority_owner": None,
        "airlink_signal_quality": None,
    }
    if snapshot is None:
        return result

    arrival_age_ms = _finite_float_or_none(snapshot.arrival_age_ms)
    result["available"] = True
    result["arrival_age_ms"] = arrival_age_ms
    result["fresh"] = (
        arrival_age_ms is not None
        and arrival_age_ms <= NYMPH_TELEMETRY_STALE_AFTER_MS
    )
    result["stale"] = not result["fresh"]

    value = snapshot.value if isinstance(snapshot.value, dict) else {}
    aircraft = value.get("aircraft_telemetry")
    aircraft = aircraft if isinstance(aircraft, dict) else {}
    location = aircraft.get("location")
    location = location if isinstance(location, dict) else {}
    attitude = aircraft.get("attitude")
    attitude = attitude if isinstance(attitude, dict) else {}
    velocity = aircraft.get("velocity_ned")
    velocity = velocity if isinstance(velocity, dict) else {}
    gps = aircraft.get("gps")
    gps = gps if isinstance(gps, dict) else {}
    battery = aircraft.get("battery")
    battery = battery if isinstance(battery, dict) else {}
    authority = aircraft.get("authority")
    authority = authority if isinstance(authority, dict) else {}

    def first_boolean(*candidates):
        for candidate in candidates:
            if isinstance(candidate, bool):
                return candidate
        return None

    def first_number(*candidates):
        for candidate in candidates:
            number = _finite_float_or_none(candidate)
            if number is not None:
                return number
        return None

    def bounded_integer(candidate, minimum, maximum):
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            return None
        return candidate if minimum <= candidate <= maximum else None

    def first_text(*candidates):
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            if not candidate or len(candidate) > 64 or not candidate.isascii():
                continue
            if not all(
                character.isalnum() or character in " _-./:"
                for character in candidate
            ):
                continue
            return candidate
        return None

    result.update({
        "product_connected": first_boolean(value.get("product_connected")),
        "aircraft_connected": first_boolean(
            value.get("aircraft_connected"),
            aircraft.get("aircraft_connected"),
        ),
        "remote_controller_connected": first_boolean(
            value.get("remote_controller_connected")
        ),
        "airlink_connected": first_boolean(value.get("airlink_connected")),
        "flight_mode": first_text(
            value.get("flight_mode"), aircraft.get("flight_mode")
        ),
        "is_flying": first_boolean(aircraft.get("is_flying")),
        "motors_on": first_boolean(
            value.get("motors_on"), aircraft.get("motors_on")
        ),
        "latitude_deg": first_number(location.get("latitude_deg")),
        "longitude_deg": first_number(location.get("longitude_deg")),
        # Nymph names this display field relative_altitude_m.  The adapter does
        # not derive or copy home/terrain data; it forwards only the bridge's
        # explicitly reported flight-altitude scalar.
        "relative_altitude_m": first_number(
            value.get("altitude_m"), location.get("altitude_m")
        ),
        "yaw_deg": first_number(attitude.get("yaw_deg")),
        "velocity_north_mps": first_number(velocity.get("north_mps")),
        "velocity_east_mps": first_number(velocity.get("east_mps")),
        "velocity_down_mps": first_number(velocity.get("down_mps")),
        "gps_signal_level": first_text(gps.get("signal_level")),
        "gps_satellite_count": bounded_integer(
            gps.get("satellite_count"), 0, 255
        ),
        "battery_percent": bounded_integer(
            battery.get("charge_remaining_percent"), 0, 100
        ),
        "authority_owner": first_text(
            value.get("flight_control_authority"), authority.get("owner")
        ),
        "airlink_signal_quality": bounded_integer(
            value.get("airlink_signal_quality"), 0, 100
        ),
    })
    return result


def _valid_obstacle_distance_mm(value):
    """Return a finite DJI obstacle range, excluding invalid wire sentinels."""
    numeric = _finite_float_or_none(value)
    if numeric is None or numeric <= 0.0 or numeric >= 65_535.0:
        return None
    return numeric


def _obstacle_observation_from_snapshot(
    snapshot,
    horizontal_mapping=HORIZONTAL_MAPPING_CONSERVATIVE_GLOBAL,
):
    """Normalize Android perception telemetry without inventing angle order.

    MSDK exposes a complete horizontal ranging vector but does not document its
    angular origin or clockwise/counterclockwise ordering for this aircraft.
    The default therefore assigns the global minimum to all four body
    directions. An explicitly selected, still-unverified calibration mapping
    may sector index 0 as forward with clockwise-positive angles. Upward and
    downward ranges have explicit semantics.
    """
    normalized = {
        "source": "ObstacleDataListener",
        "observed": False,
        "source_updated_monotonic_ms": None,
        "source_age_ms": None,
        "telemetry_queue_age_ms": None,
        "telemetry_arrival_age_ms": (
            snapshot.arrival_age_ms if snapshot is not None else None
        ),
        "distances_m": {},
        "horizontal_mapping": horizontal_mapping,
        "horizontal_mapping_verified": False,
        "horizontal_mapping_assumption": (
            "no_directional_assumption"
            if horizontal_mapping == HORIZONTAL_MAPPING_CONSERVATIVE_GLOBAL
            else "explicit_unverified_index_0_forward_clockwise_order"
        ),
        "horizontal_sample_count": None,
        "horizontal_angle_interval_deg": None,
        "directions_marked_not_working": [],
        "reason": "telemetry_unavailable",
    }
    if snapshot is None or not isinstance(snapshot.value, dict):
        return None, 0.0, normalized

    value = snapshot.value
    generated_ms = _finite_float_or_none(
        value.get("telemetry_generated_monotonic_ms")
    )
    if generated_ms is None:
        generated_ms = _finite_float_or_none(value.get("monotonic_ms"))
    queue_age_ms = _finite_float_or_none(value.get("telemetry_queue_age_ms"))
    if queue_age_ms is None or queue_age_ms < 0.0:
        queue_age_ms = 0.0
    normalized["telemetry_queue_age_ms"] = queue_age_ms
    if generated_ms is None:
        now_ms = 0.0
    else:
        now_ms = generated_ms + queue_age_ms + snapshot.arrival_age_ms

    aircraft = value.get("aircraft_telemetry")
    aircraft = aircraft if isinstance(aircraft, dict) else {}
    perception = aircraft.get("perception")
    perception = perception if isinstance(perception, dict) else {}
    ranges = perception.get("obstacle_distances")
    ranges = ranges if isinstance(ranges, dict) else {}
    information = perception.get("information")
    information = information if isinstance(information, dict) else {}
    working = information.get("working")
    working = working if isinstance(working, dict) else {}
    updated_ms = _finite_float_or_none(ranges.get("updated_monotonic_ms"))
    normalized["observed"] = ranges.get("observed") is True or updated_ms is not None
    normalized["source_updated_monotonic_ms"] = updated_ms
    normalized["horizontal_angle_interval_deg"] = _finite_float_or_none(
        ranges.get("horizontal_angle_interval_deg")
    )

    raw_horizontal = ranges.get("horizontal_distance_mm")
    valid_horizontal = []
    if isinstance(raw_horizontal, list):
        normalized["horizontal_sample_count"] = len(raw_horizontal)
        for index, value_mm in enumerate(raw_horizontal):
            numeric = _valid_obstacle_distance_mm(value_mm)
            if numeric is not None:
                valid_horizontal.append((index, numeric))

    interval = normalized["horizontal_angle_interval_deg"]
    horizontal_coverage_degrees = (
        len(raw_horizontal) * interval
        if isinstance(raw_horizontal, list)
        and interval is not None
        and interval > 0.0
        else None
    )
    horizontal_vector_complete = bool(
        isinstance(raw_horizontal, list)
        and raw_horizontal
        and len(valid_horizontal) == len(raw_horizontal)
        and horizontal_coverage_degrees is not None
        and abs(horizontal_coverage_degrees - 360.0) <= 0.5
    )
    normalized["horizontal_coverage_deg"] = horizontal_coverage_degrees
    normalized["horizontal_vector_complete"] = horizontal_vector_complete

    distances = {}
    unavailable = []
    normalization_warning = None
    if valid_horizontal and horizontal_vector_complete:
        horizontal_minimum_m = min(
            value_mm for _index, value_mm in valid_horizontal
        ) / 1000.0
        normalized["horizontal_global_minimum_m"] = horizontal_minimum_m
        if horizontal_mapping == HORIZONTAL_MAPPING_CONSERVATIVE_GLOBAL:
            horizontal_distances = {
                direction: horizontal_minimum_m
                for direction in ("forward", "backward", "left", "right")
            }
        elif (
            horizontal_mapping
            == HORIZONTAL_MAPPING_BODY_CLOCKWISE_ZERO_FORWARD
        ):
            sectors = {
                "forward": [], "right": [], "backward": [], "left": [],
            }
            for index, value_mm in valid_horizontal:
                angle = (index * interval) % 360.0
                if angle < 45.0 or angle >= 315.0:
                    direction = "forward"
                elif angle < 135.0:
                    direction = "right"
                elif angle < 225.0:
                    direction = "backward"
                else:
                    direction = "left"
                sectors[direction].append(value_mm / 1000.0)
            horizontal_distances = {
                direction: min(values)
                for direction, values in sectors.items()
                if values
            }
            normalized["horizontal_sector_minimum_m"] = dict(
                horizontal_distances
            )
        else:
            raise ValueError(
                f"unsupported horizontal obstacle mapping: {horizontal_mapping}"
            )
        for direction, distance_m in horizontal_distances.items():
            if working.get(direction) is False:
                unavailable.append(direction)
            else:
                distances[direction] = distance_m
    elif isinstance(raw_horizontal, list) and raw_horizontal:
        normalization_warning = "horizontal_vector_incomplete_or_invalid"

    for direction, field in (
        ("upward", "upward_distance_mm"),
        ("downward", "downward_distance_mm"),
    ):
        value_mm = _valid_obstacle_distance_mm(ranges.get(field))
        if working.get(direction) is False:
            unavailable.append(direction)
        elif value_mm is not None:
            distances[direction] = value_mm / 1000.0

    normalized["directions_marked_not_working"] = unavailable
    normalized["distances_m"] = dict(distances)
    if updated_ms is None:
        normalized["reason"] = "source_timestamp_unavailable"
        return None, now_ms, normalized
    if generated_ms is None:
        normalized["reason"] = "telemetry_clock_unavailable"
        return None, now_ms, normalized

    source_age_ms = now_ms - updated_ms
    normalized["source_age_ms"] = source_age_ms
    normalized["reason"] = (
        "source_timestamp_in_future"
        if source_age_ms < 0.0
        else normalization_warning or "normalized"
    )
    return ObstacleObservation(distances, updated_ms), now_ms, normalized


class ControlPacketStream:
    """Persistent VDC2 sender with immediate updates and 20 Hz refresh."""

    def __init__(
        self,
        host,
        port,
        token,
        period_seconds=CONTROL_PERIOD_SECONDS,
        socket_factory=socket.socket,
        monotonic_ns=time.monotonic_ns,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.period_seconds = period_seconds
        self._monotonic_ns = monotonic_ns
        self._socket = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._context = None
        self._requested_command = VelocityCommand()
        self._command = VelocityCommand()
        self._command_limiter = None
        self._limiter_status = {
            "configured": False,
            "reason": "command_limiter_not_configured",
        }
        self._command_generation = 0
        self._target_deadline_monotonic_ns = None
        self._target_lease_expirations = 0
        self._sequence = self._monotonic_ns() & UINT64_MASK
        self._sent_packets = 0
        self._last_sequence = None
        self._last_sent_at_control_ms = None
        self._last_dispatch_monotonic_ns = None
        self._last_error = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="veil-dji-control-20hz", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self._socket.close()
        except OSError:
            pass

    def set_command_limiter(self, limiter):
        if limiter is not None and not callable(limiter):
            raise TypeError("limiter must be callable or None")
        with self._lock:
            self._command_limiter = limiter
            if limiter is None:
                self._limiter_status = {
                    "configured": False,
                    "reason": "command_limiter_not_configured",
                }
        self._wake.set()

    def arm(self, context):
        with self._lock:
            self._context = context
            self._requested_command = VelocityCommand()
            self._command = VelocityCommand()
            self._command_generation += 1
            self._target_deadline_monotonic_ns = None
            self._last_error = None
        dispatch = self.emit_once()
        self._wake.set()
        return dispatch

    def update_context(self, context):
        with self._lock:
            if self._context is None:
                return False
            if self._context.session_id != context.session_id:
                return False
            self._context = context
            return True

    def disarm(self):
        with self._lock:
            if (
                not self._requested_command.is_neutral
                or not self._command.is_neutral
                or self._target_deadline_monotonic_ns is not None
            ):
                self._command_generation += 1
            self._requested_command = VelocityCommand()
            self._command = VelocityCommand()
            self._target_deadline_monotonic_ns = None
            self._context = None
        self._wake.set()

    def set_velocity(self, command, lease_seconds=None):
        """Send a candidate once, then publish it to the periodic refresher.

        A failed first datagram must never arm delayed motion. The retained
        target is forced neutral before the error escapes, so a later network
        recovery can only refresh zero velocity.
        """
        with self._lock:
            if self._context is None:
                raise FlightSessionError(
                    "control_not_armed", "no live bridge control session is armed"
                )
            local_ns = self._monotonic_ns()
            deadline_ns = self._lease_deadline(lease_seconds, local_ns)
            candidate_generation = self._command_generation + 1
            # Synchronous send makes receipt-to-network dispatch independent of
            # the 50 ms periodic phase. Keep the candidate private until sendto
            # succeeds so the refresh thread cannot replay a reported failure.
            try:
                applied, limiter_status = self._apply_limiter_locked(command)
                dispatch = self._emit_command_locked(
                    self._context,
                    applied,
                    local_ns,
                    candidate_generation,
                    requested_command=command,
                    limiter_status=limiter_status,
                )
            except BaseException as error:
                self._requested_command = VelocityCommand()
                self._command = VelocityCommand()
                self._target_deadline_monotonic_ns = None
                self._command_generation = candidate_generation
                self._limiter_status = {
                    **self._limiter_status,
                    "fail_neutral": True,
                    "dispatch_failure": f"{type(error).__name__}: {error}",
                }
                try:
                    self._emit_command_locked(
                        self._context,
                        VelocityCommand(),
                        self._monotonic_ns(),
                        candidate_generation,
                        requested_command=VelocityCommand(),
                        limiter_status=self._limiter_status,
                    )
                except BaseException:
                    pass
                self._wake.set()
                raise
            self._requested_command = command
            self._command = applied
            self._limiter_status = limiter_status
            self._target_deadline_monotonic_ns = deadline_ns
            self._command_generation = candidate_generation
            self._last_error = None
            return dispatch

    def set_target(self, command, lease_seconds=None):
        """Update the next 20 Hz packet without emitting an extra datagram."""
        with self._lock:
            if self._context is None:
                raise FlightSessionError(
                    "control_not_armed", "no live bridge control session is armed"
                )
            deadline_ns = self._lease_deadline(
                lease_seconds, self._monotonic_ns()
            )
            try:
                applied, limiter_status = self._apply_limiter_locked(command)
            except BaseException:
                changed = (
                    not self._requested_command.is_neutral
                    or not self._command.is_neutral
                )
                self._requested_command = VelocityCommand()
                self._command = VelocityCommand()
                self._target_deadline_monotonic_ns = None
                if changed:
                    self._command_generation += 1
                self._wake.set()
                raise
            if command != self._requested_command or applied != self._command:
                self._command_generation += 1
            self._requested_command = command
            self._command = applied
            self._limiter_status = limiter_status
            self._target_deadline_monotonic_ns = deadline_ns
            return {
                "session": self._context.session_hex,
                "command_generation": self._command_generation,
                "setpoint": self._command.to_dict(),
                "requested_setpoint": self._requested_command.to_dict(),
                "applied_setpoint": self._command.to_dict(),
                "avoidance": dict(self._limiter_status),
                "scheduled_for_periodic_stream": True,
                "target_deadline_monotonic_ns": deadline_ns,
            }

    def force_neutral_target(self):
        """Clear retained motion without requiring a working network socket."""
        with self._lock:
            changed = (
                not self._requested_command.is_neutral
                or not self._command.is_neutral
                or self._target_deadline_monotonic_ns is not None
            )
            self._requested_command = VelocityCommand()
            self._command = VelocityCommand()
            self._target_deadline_monotonic_ns = None
            self._limiter_status = {
                **self._limiter_status,
                "target_forced_neutral": True,
            }
            if changed:
                self._command_generation += 1
            result = {
                "session": (
                    self._context.session_hex if self._context is not None else None
                ),
                "command_generation": self._command_generation,
                "setpoint": self._command.to_dict(),
                "requested_setpoint": self._requested_command.to_dict(),
                "applied_setpoint": self._command.to_dict(),
                "avoidance": dict(self._limiter_status),
                "scheduled_for_periodic_stream": self._context is not None,
            }
        self._wake.set()
        return result

    def emit_once(self):
        with self._lock:
            context = self._context
            if context is None:
                return None
            local_ns = self._monotonic_ns()
            self._expire_target_lease_locked(local_ns)
            try:
                applied, limiter_status = self._apply_limiter_locked(
                    self._requested_command
                )
            except FlightSessionError as error:
                self._requested_command = VelocityCommand()
                self._command = VelocityCommand()
                self._target_deadline_monotonic_ns = None
                self._command_generation += 1
                limiter_status = dict(self._limiter_status)
                dispatch = self._emit_command_locked(
                    context,
                    VelocityCommand(),
                    local_ns,
                    self._command_generation,
                    requested_command=VelocityCommand(),
                    limiter_status=limiter_status,
                )
                dispatch["limiter_failure"] = error.to_dict()
                return dispatch
            if applied != self._command:
                self._command_generation += 1
            self._command = applied
            self._limiter_status = limiter_status
            return self._emit_command_locked(
                context,
                self._command,
                local_ns,
                self._command_generation,
                requested_command=self._requested_command,
                limiter_status=limiter_status,
            )

    def _apply_limiter_locked(self, command):
        limiter = self._command_limiter
        if limiter is None:
            return command, {
                "configured": False,
                "reason": "command_limiter_not_configured",
            }
        try:
            result = limiter(command)
            if (
                not isinstance(result, tuple)
                or len(result) != 2
                or not isinstance(result[0], VelocityCommand)
                or not isinstance(result[1], dict)
            ):
                raise TypeError(
                    "limiter must return (VelocityCommand, status_dict)"
                )
            return result
        except BaseException as error:
            self._limiter_status = {
                "configured": True,
                "reason": "limiter_exception_neutral",
                "fail_neutral": True,
                "exception": type(error).__name__,
                "description": str(error),
            }
            raise FlightSessionError(
                "command_limiter_failed_neutral",
                "command limiter failed; retained translation was neutralized",
                dict(self._limiter_status),
            ) from error

    def _emit_command_locked(
        self,
        context,
        command,
        local_ns,
        generation,
        requested_command=None,
        limiter_status=None,
    ):
        """Pack and send one command while the caller owns ``_lock``."""
        self._sequence = (self._sequence + 1) & UINT64_MASK
        sequence = self._sequence
        sent_at_ms = local_ns // 1_000_000 + context.clock_offset_ms
        fixed = (
            round(command.forward_mps * 1000),
            round(command.right_mps * 1000),
            round(command.up_mps * 1000),
            round(command.yaw_rate_deg_s * 1000),
        )
        payload = struct.pack(
            ">4sQQQiiii",
            b"VDC2",
            context.session_id,
            sequence,
            sent_at_ms,
            *fixed,
        )
        tag = hmac.new(
            self.token.encode("utf-8"), payload, hashlib.sha256
        ).digest()[:16]
        try:
            self._socket.sendto(payload + tag, (self.host, self.port))
        except OSError as error:
            self._last_error = str(error)
            raise FlightSessionError(
                "control_udp_send_failed", str(error)
            ) from error
        self._sent_packets += 1
        self._last_sequence = sequence
        self._last_sent_at_control_ms = sent_at_ms
        self._last_dispatch_monotonic_ns = local_ns
        return {
            "session": context.session_hex,
            "sequence_hex": f"{sequence:016x}",
            "sent_at_control_monotonic_ms": sent_at_ms,
            "dispatched_monotonic_ns": local_ns,
            "setpoint": command.to_dict(),
            "requested_setpoint": (
                requested_command or command
            ).to_dict(),
            "applied_setpoint": command.to_dict(),
            "avoidance": dict(limiter_status or {}),
            "command_generation": generation,
        }

    def _lease_deadline(self, lease_seconds, now_ns):
        if lease_seconds is None:
            return None
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0.0
        ):
            raise FlightSessionError(
                "invalid_control_lease", "lease_seconds must be a positive finite number"
            )
        return now_ns + int(float(lease_seconds) * 1_000_000_000)

    def _expire_target_lease_locked(self, now_ns):
        deadline_ns = self._target_deadline_monotonic_ns
        if deadline_ns is None or now_ns < deadline_ns:
            return False
        self._requested_command = VelocityCommand()
        self._command = VelocityCommand()
        self._target_deadline_monotonic_ns = None
        self._command_generation += 1
        self._target_lease_expirations += 1
        return True

    def status(self):
        with self._lock:
            deadline_ns = self._target_deadline_monotonic_ns
            lease_remaining_ms = None if deadline_ns is None else max(
                0.0, (deadline_ns - self._monotonic_ns()) / 1e6
            )
            return {
                "armed": self._context is not None,
                "session": (
                    self._context.session_hex if self._context is not None else None
                ),
                "target": self._command.to_dict(),
                "requested_target": self._requested_command.to_dict(),
                "applied_target": self._command.to_dict(),
                "avoidance": dict(self._limiter_status),
                "command_generation": self._command_generation,
                "target_lease_remaining_ms": lease_remaining_ms,
                "target_lease_expirations": self._target_lease_expirations,
                "sent_packets": self._sent_packets,
                "last_sequence_hex": (
                    f"{self._last_sequence:016x}"
                    if self._last_sequence is not None else None
                ),
                "last_sent_at_control_monotonic_ms": self._last_sent_at_control_ms,
                "last_dispatch_monotonic_ns": self._last_dispatch_monotonic_ns,
                "last_error": self._last_error,
                "refresh_hz": 1.0 / self.period_seconds,
            }

    def _run(self):
        next_deadline = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            remaining = max(0.0, next_deadline - now)
            self._wake.wait(remaining)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.emit_once()
            except FlightSessionError:
                # A synchronous command reports its own error. Periodic errors
                # remain observable through status without killing the process.
                pass
            next_deadline = time.monotonic() + self.period_seconds


def _integer_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def shortest_yaw_delta(previous_degrees, current_degrees):
    """Signed shortest angular change in [-180, 180)."""
    return (current_degrees - previous_degrees + 180.0) % 360.0 - 180.0


class YawTravelTracker:
    """Unwrap successive attitude samples into directed physical travel."""

    def __init__(self, requested_degrees, tolerance_degrees=1.0):
        if not math.isfinite(requested_degrees) or requested_degrees == 0.0:
            raise FlightSessionError(
                "invalid_rotation", "requested rotation must be finite and non-zero"
            )
        if not math.isfinite(tolerance_degrees) or tolerance_degrees < 0.0:
            raise FlightSessionError(
                "invalid_rotation_tolerance",
                "rotation tolerance must be finite and non-negative",
            )
        self.requested_degrees = requested_degrees
        self.tolerance_degrees = min(
            tolerance_degrees, abs(requested_degrees) * 0.25
        )
        self.direction = 1.0 if requested_degrees > 0 else -1.0
        self.required_travel_degrees = max(
            0.0, abs(requested_degrees) - self.tolerance_degrees
        )
        self.previous_yaw_degrees = None
        self.unwrapped_signed_degrees = 0.0
        self.samples = 0

    def update(self, yaw_degrees):
        if not math.isfinite(yaw_degrees):
            raise FlightSessionError(
                "invalid_yaw_telemetry", "yaw telemetry is not finite"
            )
        if self.previous_yaw_degrees is not None:
            self.unwrapped_signed_degrees += shortest_yaw_delta(
                self.previous_yaw_degrees, yaw_degrees
            )
        self.previous_yaw_degrees = yaw_degrees
        self.samples += 1
        return self.progress()

    def progress(self):
        directed = self.unwrapped_signed_degrees * self.direction
        return {
            "requested_degrees": self.requested_degrees,
            "required_directed_travel_degrees": self.required_travel_degrees,
            "observed_signed_travel_degrees": self.unwrapped_signed_degrees,
            "observed_directed_travel_degrees": directed,
            "remaining_degrees": max(0.0, self.required_travel_degrees - directed),
            "samples": self.samples,
            "complete": directed >= self.required_travel_degrees,
        }


def rotation_deadline_seconds(requested_degrees, yaw_rate_degrees_per_second):
    if not math.isfinite(requested_degrees) or not math.isfinite(
        yaw_rate_degrees_per_second
    ):
        raise FlightSessionError(
            "invalid_rotation", "rotation and yaw rate must be finite"
        )
    if requested_degrees == 0.0 or yaw_rate_degrees_per_second == 0.0:
        raise FlightSessionError(
            "invalid_rotation", "rotation and yaw rate must be non-zero"
        )
    expected = abs(requested_degrees) / abs(yaw_rate_degrees_per_second)
    # Enough margin for acceleration, closed-loop sampling, and wind without a
    # fixed-duration success claim. Completion still requires observed travel.
    return max(3.0, expected * 1.75 + 2.0)


def move_duration_and_velocity(forward_m, right_m, up_m, speed_mps):
    values = (forward_m, right_m, up_m, speed_mps)
    if not all(math.isfinite(value) for value in values):
        raise FlightSessionError(
            "invalid_relative_move", "relative move values must be finite"
        )
    if speed_mps <= 0.0:
        raise FlightSessionError(
            "invalid_relative_move", "speed_mps must be positive"
        )
    distance = math.sqrt(forward_m ** 2 + right_m ** 2 + up_m ** 2)
    if distance == 0.0:
        return 0.0, VelocityCommand()
    duration = distance / speed_mps
    scale = speed_mps / distance
    return duration, VelocityCommand(
        forward_mps=forward_m * scale,
        right_mps=right_m * scale,
        up_mps=up_m * scale,
    )


def _authority_ready(status):
    aircraft = status.get("aircraft_telemetry") or {}
    authority = aircraft.get("authority") or {}
    return (
        status.get("virtual_stick_enabled") is True
        and status.get("virtual_stick_advanced_mode") is True
        and status.get("virtual_stick_control_mode") == "body_velocity"
        and str(status.get("flight_control_authority", authority.get("owner", ""))).upper()
        == "MSDK"
    )


def _authority_released(status):
    aircraft = status.get("aircraft_telemetry") or {}
    authority = aircraft.get("authority") or {}
    owner = str(status.get("flight_control_authority",
                           authority.get("owner", "UNKNOWN"))).upper()
    return (
        status.get("virtual_stick_enabled") is False
        and status.get("virtual_stick_control_mode") == "disabled"
        and owner == "RC"
    )


def _queue_age_ms(snapshot):
    value = snapshot.value
    explicit = value.get("telemetry_queue_age_ms")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return float(explicit)
    generated = value.get("telemetry_generated_monotonic_ms")
    published = value.get("monotonic_ms")
    if isinstance(generated, (int, float)) and isinstance(published, (int, float)):
        return max(0.0, float(published) - float(generated))
    return None


def _fresh_attitude(snapshot):
    if snapshot is None:
        raise FlightSessionError(
            "telemetry_unavailable", "no aircraft telemetry frame has arrived"
        )
    arrival_age_ms = snapshot.arrival_age_ms
    if arrival_age_ms > TELEMETRY_MAX_ARRIVAL_AGE_SECONDS * 1000.0:
        raise FlightSessionError(
            "telemetry_stale",
            f"latest telemetry arrived {arrival_age_ms:.1f} ms ago",
        )
    queue_age_ms = _queue_age_ms(snapshot)
    if queue_age_ms is not None and queue_age_ms > TELEMETRY_MAX_QUEUE_AGE_MS:
        raise FlightSessionError(
            "telemetry_queue_stale",
            f"telemetry queue age is {queue_age_ms:.1f} ms",
        )
    aircraft = snapshot.value.get("aircraft_telemetry") or {}
    attitude = aircraft.get("attitude")
    if not isinstance(attitude, dict):
        raise FlightSessionError(
            "attitude_unavailable", "aircraft attitude telemetry is unavailable"
        )
    yaw = attitude.get("yaw_deg")
    updated = attitude.get("updated_monotonic_ms")
    server_now = snapshot.value.get(
        "telemetry_generated_monotonic_ms", snapshot.value.get("monotonic_ms")
    )
    if not isinstance(yaw, (int, float)) or isinstance(yaw, bool) or not math.isfinite(yaw):
        raise FlightSessionError("attitude_unavailable", "yaw telemetry is invalid")
    if not isinstance(updated, (int, float)) or not isinstance(server_now, (int, float)):
        raise FlightSessionError(
            "attitude_freshness_unknown", "attitude source timestamps are unavailable"
        )
    source_age_ms = max(0.0, float(server_now) - float(updated))
    if source_age_ms > TELEMETRY_MAX_SOURCE_AGE_MS:
        raise FlightSessionError(
            "attitude_stale", f"yaw source age is {source_age_ms:.1f} ms"
        )
    return {
        "yaw_deg": float(yaw),
        "source_updated_monotonic_ms": float(updated),
        "source_age_ms": source_age_ms,
        "queue_age_ms": queue_age_ms,
        "arrival_age_ms": arrival_age_ms,
        "generation": snapshot.generation,
    }


def _route_telemetry(snapshot):
    """Translate one BOOX frame into the Mac monotonic clock domain."""
    if snapshot is None:
        raise FlightSessionError(
            "route_telemetry_unavailable", "no telemetry snapshot is available"
        )
    arrival_age_ms = snapshot.arrival_age_ms
    if arrival_age_ms > TELEMETRY_MAX_ARRIVAL_AGE_SECONDS * 1000.0:
        raise FlightSessionError(
            "route_telemetry_arrival_stale",
            f"latest telemetry arrived {arrival_age_ms:.1f} ms ago",
            {"arrival_age_ms": arrival_age_ms},
        )
    queue_age_ms = _queue_age_ms(snapshot)
    if (
        not isinstance(queue_age_ms, (int, float))
        or isinstance(queue_age_ms, bool)
        or not math.isfinite(queue_age_ms)
        or queue_age_ms < 0.0
    ):
        raise FlightSessionError(
            "route_telemetry_queue_age_unavailable",
            "telemetry_queue_age_ms is required for route execution",
        )
    if queue_age_ms > TELEMETRY_MAX_QUEUE_AGE_MS:
        raise FlightSessionError(
            "route_telemetry_queue_stale",
            f"telemetry queue age is {queue_age_ms:.1f} ms",
            {"queue_age_ms": queue_age_ms},
        )
    value = snapshot.value
    generated_ms = value.get("telemetry_generated_monotonic_ms")
    aircraft = value.get("aircraft_telemetry") or {}
    location = aircraft.get("location")
    attitude = aircraft.get("attitude")
    if not isinstance(location, dict) or not isinstance(attitude, dict):
        raise FlightSessionError(
            "route_navigation_unavailable",
            "fresh aircraft location and attitude are required",
        )
    fields = {
        "generated_monotonic_ms": generated_ms,
        "latitude_deg": location.get("latitude_deg"),
        "longitude_deg": location.get("longitude_deg"),
        "altitude_m": location.get("altitude_m"),
        "location_updated_monotonic_ms": location.get("updated_monotonic_ms"),
        "yaw_deg": attitude.get("yaw_deg"),
        "attitude_updated_monotonic_ms": attitude.get("updated_monotonic_ms"),
    }
    for name, number in fields.items():
        if (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(number)
        ):
            raise FlightSessionError(
                "route_navigation_invalid", f"{name} is missing or non-finite"
            )
    source_updated_ms = min(
        float(fields["location_updated_monotonic_ms"]),
        float(fields["attitude_updated_monotonic_ms"]),
    )
    source_age_ms = float(generated_ms) - source_updated_ms
    if source_age_ms < 0.0:
        raise FlightSessionError(
            "route_navigation_future_sample",
            "navigation source timestamp is newer than its telemetry frame",
            {"source_age_ms": source_age_ms},
        )
    # Never compare BOOX elapsedRealtime directly to Mac monotonic. Preserve
    # only the observed age, then apply it to the Mac receipt timestamp. Queue
    # delay and time since receipt are therefore both included in freshness.
    sample_mac_ms = (
        snapshot.received_monotonic_ns / 1e6 - source_age_ms - queue_age_ms
    )
    now_mac_ms = time.monotonic_ns() / 1e6
    telemetry = RouteTelemetry(
        float(fields["latitude_deg"]),
        float(fields["longitude_deg"]),
        float(fields["altitude_m"]),
        float(fields["yaw_deg"]),
        sample_mac_ms,
    )
    freshness = {
        "arrival_age_ms": arrival_age_ms,
        "queue_age_ms": queue_age_ms,
        "source_age_ms": source_age_ms,
        "effective_age_ms": max(0.0, now_mac_ms - sample_mac_ms),
        "telemetry_sequence": value.get("telemetry_sequence"),
        "location_updated_monotonic_ms": fields[
            "location_updated_monotonic_ms"
        ],
        "attitude_updated_monotonic_ms": fields[
            "attitude_updated_monotonic_ms"
        ],
    }
    return telemetry, now_mac_ms, freshness


def _command_record(response):
    if not isinstance(response, dict):
        return None
    nested = response.get("command")
    if isinstance(nested, dict):
        return nested
    if "state" in response and "id" in response:
        return response
    return None


def _parse_unsigned_hex(value):
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16) & UINT64_MASK
    except ValueError:
        return None


def unsigned_sequence_at_or_after(candidate, reference):
    """RFC1982-style comparison for a practically bounded uint64 window."""
    return ((candidate - reference) & UINT64_MASK) < (1 << 63)


def _setpoints_equal(echo, expected):
    if not isinstance(echo, dict) or not isinstance(expected, dict):
        return False
    if echo.get("mode") not in (None, "body_velocity"):
        return False
    for name in ("forward_mps", "right_mps", "up_mps", "yaw_rate_deg_s"):
        actual = echo.get(name)
        wanted = expected.get(name)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        if not isinstance(wanted, (int, float)) or isinstance(wanted, bool):
            return False
        if abs(float(actual) - float(wanted)) > 0.00051:
            return False
    return True


class FlightSession:
    """Long-lived authority, telemetry, and physical-command coordinator."""

    def __init__(self, api, telemetry, packets):
        self.api = api
        self.telemetry = telemetry
        self.packets = packets
        self._lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor_thread = None
        self._armed = False
        self._armed_at_monotonic = None
        self._arm_generation = 0
        self._authority_fault = None
        self._monitor_last_error = None
        self._last_context_sync = 0.0
        self._local_api_status_provider = None
        self._avoidance_lock = threading.RLock()
        self._avoidance_config = ObstacleAvoidanceConfig()
        self._avoidance_horizontal_mapping = (
            HORIZONTAL_MAPPING_CONSERVATIVE_GLOBAL
        )
        self._last_avoidance_decision = {
            "mode": self._avoidance_config.mode.value,
            "reason": "no_control_packet_evaluated",
            "requested_setpoint": VelocityCommand().to_dict(),
            "applied_setpoint": VelocityCommand().to_dict(),
        }
        self._routes = AtomicRouteRevisionStore()
        self._route_control_lock = threading.RLock()
        self._route_runtime_lock = threading.Lock()
        self._route_runtime = {
            "ownership": "operator",
            "last_reason": "no_route",
            "last_tick_monotonic_ns": None,
            "last_error": None,
            "last_body_setpoint": None,
            "last_tick": None,
            "loop_faults": 0,
        }
        self._route_stop = threading.Event()
        self._route_wake = threading.Event()
        self._route_thread = None
        self.packets.set_command_limiter(self._limit_command_for_obstacles)

    def _limit_command_for_obstacles(self, command):
        with self._avoidance_lock:
            config = self._avoidance_config
            horizontal_mapping = self._avoidance_horizontal_mapping
        observation, now_ms, normalization = _obstacle_observation_from_snapshot(
            self.telemetry.latest(), horizontal_mapping
        )
        result = limit_body_velocity(
            forward_mps=command.forward_mps,
            right_mps=command.right_mps,
            up_mps=command.up_mps,
            yaw_rate_deg_s=command.yaw_rate_deg_s,
            observation=observation,
            now_monotonic_ms=now_ms,
            config=config,
        )
        applied = VelocityCommand(
            result.forward_mps,
            result.right_mps,
            result.up_mps,
            result.yaw_rate_deg_s,
        )
        decision_reason = result.reason
        if result.missing_directions and result.reason == "clear":
            decision_reason = (
                "missing_obstacle_direction_pass_through"
                if result.observation_fresh
                else "missing_or_stale_obstacle_data_pass_through"
            )
        decision = {
            "configured": True,
            "mode": result.mode.value,
            "reason": decision_reason,
            "observation_fresh": result.observation_fresh,
            "translation_limited": result.translation_limited,
            "limited_directions": list(result.limited_directions),
            "threat_directions": list(result.threat_directions),
            "missing_directions": list(result.missing_directions),
            "requested_setpoint": command.to_dict(),
            "applied_setpoint": applied.to_dict(),
            "config": _avoidance_config_dict(config),
            "perception": normalization,
        }
        with self._avoidance_lock:
            self._last_avoidance_decision = decision
        return applied, decision

    def avoidance_status(self):
        with self._avoidance_lock:
            config = self._avoidance_config
            horizontal_mapping = self._avoidance_horizontal_mapping
            decision = dict(self._last_avoidance_decision)
        _observation, _now_ms, normalization = (
            _obstacle_observation_from_snapshot(
                self.telemetry.latest(), horizontal_mapping
            )
        )
        return {
            "mode": config.mode.value,
            "config": _avoidance_config_dict(config),
            "horizontal_mapping": horizontal_mapping,
            "horizontal_mapping_verified": False,
            "directional_mapping_requires_ground_calibration": (
                horizontal_mapping
                == HORIZONTAL_MAPPING_BODY_CLOCKWISE_ZERO_FORWARD
            ),
            "default_is_non_blocking_advisory": True,
            "translation_enforcement_active": (
                config.mode is AvoidanceMode.BRAKE
            ),
            "missing_data_behavior_scope": "enforced_only_in_brake_mode",
            "last_decision": decision,
            "perception": normalization,
            "dji_firmware_retention_under_virtual_stick": "unverified",
        }

    def configure_avoidance(
        self,
        *,
        mode=None,
        missing_data_behavior=None,
        minimum_clearance_m=None,
        reaction_time_s=None,
        maximum_deceleration_mps2=None,
        maximum_source_age_ms=None,
        horizontal_mapping=None,
        received_monotonic_ns=None,
    ):
        with self._avoidance_lock:
            previous = self._avoidance_config
            previous_mapping = self._avoidance_horizontal_mapping
        try:
            selected_mode = (
                previous.mode
                if mode is None
                else mode if isinstance(mode, AvoidanceMode)
                else AvoidanceMode(str(mode).lower())
            )
            selected_missing = (
                previous.missing_data_behavior
                if missing_data_behavior is None
                else missing_data_behavior
                if isinstance(missing_data_behavior, MissingDataBehavior)
                else MissingDataBehavior(str(missing_data_behavior).lower())
            )
            selected_mapping = (
                previous_mapping
                if horizontal_mapping is None
                else str(horizontal_mapping).lower()
            )
            if selected_mapping not in HORIZONTAL_OBSTACLE_MAPPINGS:
                raise ValueError(
                    "horizontal_mapping must be one of "
                    + ", ".join(HORIZONTAL_OBSTACLE_MAPPINGS)
                )
            updated = ObstacleAvoidanceConfig(
                mode=selected_mode,
                missing_data_behavior=selected_missing,
                minimum_clearance_m=(
                    previous.minimum_clearance_m
                    if minimum_clearance_m is None else minimum_clearance_m
                ),
                reaction_time_s=(
                    previous.reaction_time_s
                    if reaction_time_s is None else reaction_time_s
                ),
                maximum_deceleration_mps2=(
                    previous.maximum_deceleration_mps2
                    if maximum_deceleration_mps2 is None
                    else maximum_deceleration_mps2
                ),
                maximum_source_age_ms=(
                    previous.maximum_source_age_ms
                    if maximum_source_age_ms is None else maximum_source_age_ms
                ),
            )
        except (TypeError, ValueError) as error:
            raise FlightSessionError(
                "invalid_avoidance_config", str(error)
            ) from error

        changed = updated != previous or selected_mapping != previous_mapping
        with self._avoidance_lock:
            self._avoidance_config = updated
            self._avoidance_horizontal_mapping = selected_mapping
        reapplied = self.packets.emit_once() if changed else None
        result = {
            "ok": True,
            "state": "avoidance_configured" if changed else "avoidance_status",
            "avoidance": self.avoidance_status(),
            "current_target_reapplied": reapplied,
        }
        if received_monotonic_ns is not None:
            result["command_received_monotonic_ns"] = received_monotonic_ns
        return result

    def set_local_api_status_provider(self, provider):
        if provider is not None and not callable(provider):
            raise TypeError("local API status provider must be callable or None")
        with self._lock:
            self._local_api_status_provider = provider

    def _local_api_status(self):
        with self._lock:
            provider = self._local_api_status_provider
        if provider is None:
            return {
                "mode": "stdin",
                "listening": False,
                "client_generation": 0,
            }
        try:
            status = provider()
            if not isinstance(status, dict):
                raise TypeError("status provider did not return an object")
            return status
        except BaseException as error:
            return {
                "mode": "unix_ndjson",
                "listening": False,
                "healthy": False,
                "status_error": f"{type(error).__name__}: {error}",
            }

    def start(self, auto_arm=True):
        self.telemetry.start()
        self.packets.start()
        self._route_thread = threading.Thread(
            target=self._route_loop, name="veil-dji-route-20hz", daemon=True
        )
        self._route_thread.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor, name="veil-dji-authority-monitor", daemon=True
        )
        self._monitor_thread.start()
        if auto_arm:
            return self.arm()
        return {"ok": True, "state": "started_disarmed",
                "capabilities": CAPABILITIES}

    def close(self, handoff=True):
        result = None
        if handoff:
            try:
                result = self.handoff()
            except FlightSessionError as error:
                result = error.to_dict()
        self._stop.set()
        self._route_stop.set()
        self._route_wake.set()
        if self._route_thread is not None:
            self._route_thread.join(timeout=1.0)
        self.packets.stop()
        self.telemetry.stop()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
        return result

    def _wait_bridge_command(self, response, timeout=COMMAND_CALLBACK_WAIT_SECONDS):
        record = _command_record(response)
        result_url = response.get("result_url") if isinstance(response, dict) else None
        deadline = time.monotonic() + timeout
        while record is not None and record.get("state") == "requested":
            if not result_url:
                command_id = record.get("id") or response.get("command_id")
                result_url = f"/commands/{command_id}" if command_id else None
            if result_url is None or time.monotonic() >= deadline:
                raise FlightSessionError(
                    "bridge_command_timeout",
                    "bridge command callback did not become terminal",
                    {"command": record},
                )
            time.sleep(0.05)
            record = _command_record(self.api.request("GET", result_url))
        if record is not None and record.get("state") == "failed":
            raise FlightSessionError(
                "bridge_command_failed",
                "DJI/bridge rejected the command",
                {"command": record},
            )
        return record


    def _post_and_wait(self, path, timeout=COMMAND_CALLBACK_WAIT_SECONDS):
        response = self.api.request("POST", path)
        return response, self._wait_bridge_command(response, timeout)


    def _synchronize_context(self, require_ready=True, preserve_target=False):
        before_ms = time.monotonic_ns() // 1_000_000
        status = self.api.status()
        after_ms = time.monotonic_ns() // 1_000_000
        if status.get("control_packet_version") != 2:
            raise FlightSessionError(
                "unsupported_control_protocol",
                "bridge does not advertise control packet V2",
            )
        if require_ready and not _authority_ready(status):
            raise FlightSessionError(
                "authority_not_ready",
                "MSDK body-velocity authority is not active",
                {"authority": self._authority_summary(status)},
            )
        session_hex = status.get("control_session")
        try:
            session_id = int(session_hex, 16)
        except (TypeError, ValueError) as error:
            raise FlightSessionError(
                "control_session_unavailable",
                "bridge did not provide a valid control session",
            ) from error
        server_ms = _integer_or_none(status.get("control_monotonic_ms"))
        if server_ms is None:
            raise FlightSessionError(
                "control_clock_unavailable", "bridge control clock is unavailable"
            )
        context = ControlContext(
            session_id, session_hex,
            server_ms - ((before_ms + after_ms) // 2),
        )
        if preserve_target:
            if not self.packets.update_context(context):
                raise FlightSessionError(
                    "control_session_rotated",
                    "control session changed during clock synchronization",
                )
            dispatch = None
        else:
            dispatch = self.packets.arm(context)
        self._last_context_sync = time.monotonic()
        return status, context, dispatch


    def arm(self):
        with self._transition_lock:
            status = self.api.status()
            if not _authority_ready(status):
                if status.get("virtual_stick_enabled") is True:
                    try:
                        self._post_and_wait("/virtual-stick/disable")
                    except FlightSessionError:
                        # The exact enable response below remains authoritative;
                        # this is only cleanup of a wrong/stale control mode.
                        pass
                # Do not wait for DJI's asynchronous enable callback here. Its
                # authority grant can precede that callback, and Android's
                # deadman begins at the grant. Observe the grant, acquire the
                # rotated UDP session, and start neutral refresh immediately.
                response = self.api.request(
                    "POST", "/virtual-stick/enable?mode=body_velocity"
                )
                record = _command_record(response)
            else:
                response = {"reused_existing_authority": True}
                record = None

            deadline = time.monotonic() + AUTHORITY_WAIT_SECONDS
            status = None
            while time.monotonic() < deadline:
                latest = self.telemetry.latest()
                candidate = latest.value if latest is not None else self.api.status()
                if _authority_ready(candidate):
                    status = candidate
                    break
                time.sleep(0.05)
            if status is None:
                raise FlightSessionError(
                    "authority_grant_timeout",
                    "DJI did not report MSDK advanced body-velocity authority",
                    {"command": record},
                )

            status, context, dispatch = self._synchronize_context()
            latest = self.telemetry.latest()
            with self._lock:
                self._armed = True
                self._armed_at_monotonic = time.monotonic()
                self._authority_fault = None
                self._monitor_last_error = None
                self._arm_generation = latest.generation if latest else 0
            if record is not None:
                try:
                    record = self._wait_bridge_command(response)
                except FlightSessionError:
                    self.packets.disarm()
                    with self._lock:
                        self._armed = False
                        self._armed_at_monotonic = None
                    try:
                        self.api.request("POST", "/virtual-stick/disable")
                    except FlightSessionError:
                        pass
                    raise
            return {
                "ok": True,
                "state": "armed_neutral",
                "session": context.session_hex,
                "initial_neutral_dispatch": dispatch,
                "bridge_command": record,
                "authority": self._authority_summary(status),
                "capabilities": CAPABILITIES,
                "enable_response": response if record is None else None,
            }


    def _authority_summary(self, status):
        aircraft = status.get("aircraft_telemetry") or {}
        authority = aircraft.get("authority") or {}
        return {
            "virtual_stick_enabled": status.get("virtual_stick_enabled"),
            "advanced_mode_enabled": status.get("virtual_stick_advanced_mode"),
            "owner": status.get("flight_control_authority", authority.get("owner")),
            "mode": status.get("virtual_stick_control_mode"),
            "control_failsafe_state": status.get("control_failsafe_state"),
        }


    def _require_armed(self):
        with self._lock:
            if not self._armed:
                raise FlightSessionError(
                    "control_not_armed",
                    "flight-control session is not armed",
                    {"authority_fault": self._authority_fault},
                )


    def route_accept(self, document, received_monotonic_ns=None):
        if not isinstance(document, str):
            raise FlightSessionError(
                "invalid_route_document",
                "route_accept requires the complete revision JSON as string field 'document'",
            )
        try:
            request = parse_route_revision(document)
        except RouteParseError as error:
            raise FlightSessionError(
                "route_parse_error", error.message, error.to_dict()
            ) from error
        guidance = None
        terminal_route_reset = False
        with self._route_control_lock:
            before = self._routes.snapshot()
            if (
                before is not None
                and before.phase in (RoutePhase.COMPLETED, RoutePhase.ABORTED)
                and request.expected_accepted_revision
                == before.newest_accepted_revision
                and request.plan.revision > before.newest_accepted_revision
            ):
                # A terminal immutable store cannot be mutated in place. Build
                # and validate a fresh store first, then swap it atomically only
                # after the caller proves knowledge of the terminal revision.
                # Authority and the authenticated UDP session are untouched.
                candidate = AtomicRouteRevisionStore()
                acceptance = candidate.accept(replace(
                    request, expected_accepted_revision=None
                ))
                if acceptance.accepted:
                    self.packets.force_neutral_target()
                    self._routes = candidate
                    terminal_route_reset = True
                    self._set_route_runtime(
                        ownership="operator",
                        last_reason="terminal_route_replaced",
                        last_error=None,
                        last_body_setpoint=VelocityCommand().to_dict(),
                        last_tick=None,
                    )
            else:
                acceptance = self._routes.accept(request)
            if acceptance.accepted and acceptance.state.phase is RoutePhase.RUNNING:
                # Atomic acceptance never disarms or reacquires MSDK authority.
                # An immediate revision reaches the wire without waiting for an
                # arbitrary periodic phase; boundary activation remains in the
                # immutable store's tick transition.
                guidance = self._route_tick_once(
                    immediate=True, lock_held=True
                )
            route_snapshot = self._routes.snapshot()
        result = acceptance.to_dict()
        result.update({
            "ok": acceptance.accepted,
            "state": (
                "route_revision_accepted"
                if acceptance.accepted else f"route_{acceptance.status.value}"
            ),
            "guidance_after_acceptance": guidance,
            "authority_reacquired": False,
            "terminal_route_reset": terminal_route_reset,
            "capabilities": dict(CAPABILITIES),
        })
        result["route"] = state_to_dict(route_snapshot)
        if received_monotonic_ns is not None:
            result["command_received_monotonic_ns"] = received_monotonic_ns
        return result


    def route_start(self, received_monotonic_ns=None):
        self._require_armed()
        with self._route_control_lock:
            change = self._routes.start()
            if change is None:
                raise FlightSessionError(
                    "route_unavailable", "no accepted route is loaded"
                )
            if not change.accepted:
                raise FlightSessionError(
                    "route_transition_rejected",
                    f"route cannot start from phase {change.state.phase.value}",
                    {"route": state_to_dict(change.state)},
                )
            self._set_route_runtime(ownership="route", last_reason="starting",
                                    last_error=None)
            guidance = self._route_tick_once(immediate=True, lock_held=True)
        return self._route_action_result(
            "route_started", change, guidance, received_monotonic_ns
        )


    def route_pause(self, received_monotonic_ns=None):
        with self._route_control_lock:
            change = self._routes.pause()
            if change is None:
                raise FlightSessionError(
                    "route_unavailable", "no accepted route is loaded"
                )
            dispatch = self._route_neutral(immediate=True)
            self._set_route_runtime(
                ownership="operator", last_reason="paused", last_error=None,
                last_body_setpoint=VelocityCommand().to_dict(),
            )
        if not change.accepted and change.state.phase is not RoutePhase.PAUSED:
            raise FlightSessionError(
                "route_transition_rejected",
                f"route cannot pause from phase {change.state.phase.value}",
                {"route": state_to_dict(change.state)},
            )
        return self._route_action_result(
            "route_paused", change, {"neutral_dispatch": dispatch},
            received_monotonic_ns,
        )


    def route_resume(self, received_monotonic_ns=None):
        self._require_armed()
        with self._route_control_lock:
            change = self._routes.resume()
            if change is None:
                raise FlightSessionError(
                    "route_unavailable", "no accepted route is loaded"
                )
            if not change.accepted:
                raise FlightSessionError(
                    "route_transition_rejected",
                    f"route cannot resume from phase {change.state.phase.value}",
                    {"route": state_to_dict(change.state)},
                )
            self._set_route_runtime(ownership="route", last_reason="resuming",
                                    last_error=None)
            guidance = self._route_tick_once(immediate=True, lock_held=True)
        return self._route_action_result(
            "route_resumed", change, guidance, received_monotonic_ns
        )


    def route_abort(self, received_monotonic_ns=None):
        with self._route_control_lock:
            change = self._routes.abort()
            if change is None:
                raise FlightSessionError(
                    "route_unavailable", "no accepted route is loaded"
                )
            dispatch = self._route_neutral(immediate=True)
            self._set_route_runtime(
                ownership="operator", last_reason="aborted", last_error=None,
                last_body_setpoint=VelocityCommand().to_dict(),
            )
        if not change.accepted and change.state.phase is not RoutePhase.ABORTED:
            raise FlightSessionError(
                "route_transition_rejected",
                f"route cannot abort from phase {change.state.phase.value}",
                {"route": state_to_dict(change.state)},
            )
        return self._route_action_result(
            "route_aborted", change, {"neutral_dispatch": dispatch},
            received_monotonic_ns,
        )


    def route_status(self):
        with self._route_runtime_lock:
            runtime = dict(self._route_runtime)
        return {
            "ok": True,
            "state": "route_status",
            "route": state_to_dict(self._routes.snapshot()),
            "runtime": runtime,
            "capabilities": dict(CAPABILITIES),
        }


    def _route_action_result(self, state, change, guidance, received_ns):
        result = {
            "ok": True,
            "state": state,
            "route": state_to_dict(self._routes.snapshot()),
            "guidance": guidance,
            "capabilities": dict(CAPABILITIES),
        }
        if received_ns is not None:
            result["command_received_monotonic_ns"] = received_ns
        return result


    def _set_route_runtime(self, **updates):
        with self._route_runtime_lock:
            self._route_runtime.update(updates)


    def _pause_route_for_manual(self, reason):
        """Transfer setpoint ownership before any operator command is sent."""
        with self._route_control_lock:
            state = self._routes.snapshot()
            if state is not None and state.phase is RoutePhase.RUNNING:
                self._routes.pause()
            self._set_route_runtime(
                ownership="operator",
                last_reason=f"manual_override:{reason}",
                last_error=None,
            )


    def _route_neutral(self, immediate):
        with self._lock:
            armed = self._armed
        if not armed:
            return self.packets.force_neutral_target()
        try:
            if immediate:
                return self.packets.set_velocity(VelocityCommand())
            return self.packets.set_target(VelocityCommand())
        except Exception as error:
            cleared = self.packets.force_neutral_target()
            return {
                "target_cleared": cleared,
                "send_error": f"{type(error).__name__}: {error}",
            }


    def _route_loop(self):
        next_deadline = time.monotonic()
        while not self._route_stop.is_set():
            remaining = max(0.0, next_deadline - time.monotonic())
            self._route_wake.wait(remaining)
            self._route_wake.clear()
            if self._route_stop.is_set():
                return
            try:
                self._route_tick_once(immediate=False)
            except BaseException as error:
                # A route bug must not terminate this thread while the separate
                # 20 Hz packet sender keeps refreshing its last moving target.
                # Clear the target first; reporting and state transitions are
                # deliberately secondary to that no-throw safety boundary.
                try:
                    self._handle_route_loop_fault(error)
                except BaseException:
                    # The setpoint lease still expires independently in the
                    # packet thread. Best-effort local clearing here preserves
                    # the stronger immediate-neutral behavior even if fault
                    # reporting itself is unexpectedly broken.
                    try:
                        self.packets.force_neutral_target()
                    except BaseException:
                        pass
            next_deadline = time.monotonic() + CONTROL_PERIOD_SECONDS


    def _handle_route_loop_fault(self, error):
        try:
            cleared = self.packets.force_neutral_target()
        except BaseException as clear_failure:
            cleared = {
                "clear_error": f"{type(clear_failure).__name__}: {clear_failure}"
            }
        dispatch = None
        send_error = None
        try:
            dispatch = self.packets.emit_once()
        except BaseException as send_failure:
            send_error = f"{type(send_failure).__name__}: {send_failure}"
        try:
            with self._route_control_lock:
                state = self._routes.snapshot()
                if state is not None and state.phase is RoutePhase.RUNNING:
                    self._routes.pause()
        except BaseException as state_failure:
            send_error = send_error or (
                f"route_state_fault:{type(state_failure).__name__}: {state_failure}"
            )
        with self._route_runtime_lock:
            faults = int(self._route_runtime.get("loop_faults", 0)) + 1
            self._route_runtime.update({
                "ownership": "operator",
                "last_reason": "route_loop_fault_neutral",
                "last_error": f"{type(error).__name__}: {error}",
                "last_tick_monotonic_ns": time.monotonic_ns(),
                "last_body_setpoint": VelocityCommand().to_dict(),
                "last_tick": {
                    "target_cleared": cleared,
                    "neutral_dispatch": dispatch,
                    "neutral_send_error": send_error,
                },
                "loop_faults": faults,
            })


    def _route_tick_once(self, immediate=False, lock_held=False):
        if not lock_held:
            self._route_control_lock.acquire()
        try:
            state = self._routes.snapshot()
            if state is None or state.phase is not RoutePhase.RUNNING:
                return None
            with self._lock:
                armed = self._armed
            if not armed:
                self._set_route_runtime(
                    ownership="operator", last_reason="control_not_armed",
                    last_error="route remains loaded but cannot emit motion",
                    last_tick_monotonic_ns=time.monotonic_ns(),
                )
                return None
            snapshot = self.telemetry.latest()
            try:
                route_telemetry, now_ms, freshness = _route_telemetry(snapshot)
            except FlightSessionError as error:
                scheduled = self._route_neutral(immediate=immediate)
                self._set_route_runtime(
                    ownership="route", last_reason=error.code,
                    last_error=error.message,
                    last_tick_monotonic_ns=time.monotonic_ns(),
                    last_body_setpoint=VelocityCommand().to_dict(),
                    last_tick={"neutral": scheduled, "freshness": error.details},
                )
                return {
                    "active": False, "reason": error.code,
                    "neutral": scheduled, "freshness": error.details,
                }
            tick = self._routes.tick(route_telemetry, now_ms)
            if tick is None:
                return None
            body = ground_ned_to_body(tick.command, route_telemetry.yaw_deg)
            velocity = VelocityCommand(
                body.forward_mps,
                body.right_mps,
                body.up_mps,
                body.yaw_rate_deg_s,
            )
            lease_seconds = (
                ROUTE_SETPOINT_LEASE_SECONDS if tick.command.is_active else None
            )
            scheduled = (
                self.packets.set_velocity(velocity, lease_seconds=lease_seconds)
                if immediate else self.packets.set_target(
                    velocity, lease_seconds=lease_seconds
                )
            )
            tick_status = {
                "active": tick.command.is_active,
                "reason": tick.command.reason.value,
                "route": state_to_dict(tick.state),
                "horizontal_distance_m": tick.horizontal_distance_m,
                "vertical_error_down_m": tick.vertical_error_down_m,
                "body_setpoint": velocity.to_dict(),
                "requested_body_setpoint": velocity.to_dict(),
                "applied_body_setpoint": scheduled.get("applied_setpoint"),
                "avoidance": scheduled.get("avoidance"),
                "wire": scheduled,
                "telemetry_freshness": freshness,
            }
            owner = (
                "route" if tick.state.phase is RoutePhase.RUNNING else "operator"
            )
            self._set_route_runtime(
                ownership=owner,
                last_reason=tick.command.reason.value,
                last_error=None,
                last_tick_monotonic_ns=time.monotonic_ns(),
                last_body_setpoint=velocity.to_dict(),
                last_tick=tick_status,
            )
            return tick_status
        except FlightSessionError as error:
            neutral = self._route_neutral(immediate=immediate)
            self._set_route_runtime(
                ownership="route", last_reason=error.code,
                last_error=error.message,
                last_tick_monotonic_ns=time.monotonic_ns(),
                last_body_setpoint=VelocityCommand().to_dict(),
            )
            return {"active": False, "reason": error.code, "neutral": neutral}
        except Exception as error:
            cleared = self.packets.force_neutral_target()
            try:
                neutral_dispatch = self.packets.emit_once()
                neutral_send_error = None
            except Exception as send_failure:
                neutral_dispatch = None
                neutral_send_error = (
                    f"{type(send_failure).__name__}: {send_failure}"
                )
            state = self._routes.snapshot()
            if state is not None and state.phase is RoutePhase.RUNNING:
                self._routes.pause()
            self._set_route_runtime(
                ownership="operator",
                last_reason="route_tick_exception_neutral",
                last_error=f"{type(error).__name__}: {error}",
                last_tick_monotonic_ns=time.monotonic_ns(),
                last_body_setpoint=VelocityCommand().to_dict(),
                last_tick={
                    "target_cleared": cleared,
                    "neutral_dispatch": neutral_dispatch,
                    "neutral_send_error": neutral_send_error,
                },
            )
            return {
                "active": False,
                "reason": "route_tick_exception_neutral",
                "neutral": cleared,
                "error": f"{type(error).__name__}: {error}",
            }
        finally:
            if not lock_held:
                self._route_control_lock.release()


    def _dispatch_velocity_fail_neutral(self, command):
        """Dispatch once without ever retaining a failed motion command."""
        try:
            return self.packets.set_velocity(command)
        except BaseException:
            # ControlPacketStream already provides this invariant. Keep the
            # session boundary defensive as well so alternate/test transports
            # cannot leave an earlier moving target refreshing after failure.
            try:
                self.packets.force_neutral_target()
            except BaseException:
                pass
            raise


    def set_velocity(self, command, received_monotonic_ns=None):
        self._pause_route_for_manual("velocity")
        self._require_armed()
        dispatch = self._dispatch_velocity_fail_neutral(command)
        result = {
            "ok": True,
            "state": "setpoint_dispatched",
            "dispatch": dispatch,
            "requested_setpoint": dispatch.get("requested_setpoint"),
            "applied_setpoint": dispatch.get("applied_setpoint"),
            "avoidance": dispatch.get("avoidance"),
            "physical_response_confirmed": False,
        }
        return self._with_timing_and_ack(result, received_monotonic_ns, dispatch)


    def neutral(self, received_monotonic_ns=None):
        return self.set_velocity(VelocityCommand(), received_monotonic_ns)


    def move_relative(
        self, forward_m, right_m, up_m, speed_mps,
        cancel_event=None, received_monotonic_ns=None,
    ):
        self._pause_route_for_manual("move_relative")
        self._require_armed()
        cancel_event = cancel_event or threading.Event()
        duration, velocity = move_duration_and_velocity(
            forward_m, right_m, up_m, speed_mps
        )
        if duration == 0.0:
            dispatch = self._dispatch_velocity_fail_neutral(VelocityCommand())
            return self._with_timing_and_ack({
                "ok": True,
                "state": "zero_distance_noop",
                "commanded_displacement_m": {
                    "forward": forward_m, "right": right_m, "up": up_m,
                },
                "physical_displacement_confirmed": False,
                "measurement": "commanded_open_loop",
                "dispatch": dispatch,
            }, received_monotonic_ns, dispatch)
        dispatch = self._dispatch_velocity_fail_neutral(velocity)
        started = time.monotonic()
        state = "command_window_completed"
        ok = True
        try:
            deadline = started + duration
            while time.monotonic() < deadline:
                if cancel_event.wait(min(0.02, deadline - time.monotonic())):
                    state = "cancelled_neutral"
                    ok = False
                    break
                with self._lock:
                    if not self._armed:
                        state = "authority_lost_neutral"
                        ok = False
                        break
        finally:
            neutral_dispatch = None
            try:
                neutral_dispatch = self._dispatch_velocity_fail_neutral(
                    VelocityCommand()
                )
            except Exception:
                pass
        elapsed = time.monotonic() - started
        result = {
            "ok": ok,
            "state": state,
            "measurement": "commanded_open_loop",
            "commanded_displacement_m": {
                "forward": forward_m, "right": right_m, "up": up_m,
            },
            "commanded_velocity": velocity.to_dict(),
            "commanded_duration_s": duration,
            "actual_command_window_s": elapsed,
            "physical_displacement_confirmed": False,
            "dispatch": dispatch,
            "neutral_dispatch": neutral_dispatch,
        }
        return self._with_timing_and_ack(result, received_monotonic_ns, dispatch)


    def rotate_relative(
        self, degrees, yaw_rate_deg_s=25.0, tolerance_deg=1.0,
        cancel_event=None, received_monotonic_ns=None,
    ):
        self._pause_route_for_manual("rotate_relative")
        self._require_armed()
        if not math.isfinite(yaw_rate_deg_s) or yaw_rate_deg_s <= 0.0 or yaw_rate_deg_s > 100.0:
            raise FlightSessionError(
                "invalid_yaw_rate", "yaw_rate_deg_s must be in (0, 100]"
            )
        tracker = YawTravelTracker(degrees, tolerance_deg)
        cancel_event = cancel_event or threading.Event()
        initial = _fresh_attitude(self.telemetry.latest())
        tracker.update(initial["yaw_deg"])
        last_source_timestamp = initial["source_updated_monotonic_ms"]
        last_generation = initial["generation"]
        commanded_rate = yaw_rate_deg_s * tracker.direction
        dispatch = self._dispatch_velocity_fail_neutral(
            VelocityCommand(yaw_rate_deg_s=commanded_rate)
        )
        started = time.monotonic()
        last_fresh_local = started
        deadline_seconds = rotation_deadline_seconds(degrees, commanded_rate)
        deadline = started + deadline_seconds
        state = "rotation_timeout_neutral"
        ok = False
        freshness = initial
        try:
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    state = "cancelled_neutral"
                    break
                with self._lock:
                    if not self._armed:
                        state = "authority_lost_neutral"
                        break
                wait = min(0.05, deadline - time.monotonic())
                snapshot = self.telemetry.wait_for(
                    lambda candidate: (
                        ((candidate.value.get("aircraft_telemetry") or {})
                         .get("attitude") or {})
                        .get("updated_monotonic_ms", -1) > last_source_timestamp
                    ),
                    wait,
                    after_generation=last_generation,
                )
                if snapshot is None:
                    if time.monotonic() - last_fresh_local >= TELEMETRY_STALL_SECONDS:
                        state = "telemetry_stall_neutral"
                        break
                    continue
                try:
                    freshness = _fresh_attitude(snapshot)
                except FlightSessionError as error:
                    state = f"{error.code}_neutral"
                    break
                last_source_timestamp = freshness["source_updated_monotonic_ms"]
                last_generation = freshness["generation"]
                last_fresh_local = time.monotonic()
                progress = tracker.update(freshness["yaw_deg"])
                if progress["complete"]:
                    state = "rotation_observed_complete"
                    ok = True
                    break
        finally:
            neutral_dispatch = None
            try:
                neutral_dispatch = self._dispatch_velocity_fail_neutral(
                    VelocityCommand()
                )
            except Exception:
                pass
        progress = tracker.progress()
        # No timeout/cancel/telemetry path may be labeled successful merely
        # because its nominal command duration elapsed (e.g. 331 of 360 deg).
        ok = bool(ok and progress["complete"])
        result = {
            "ok": ok,
            "state": state,
            "rotation": progress,
            "yaw_rate_deg_s": commanded_rate,
            "deadline_s": deadline_seconds,
            "elapsed_s": time.monotonic() - started,
            "last_telemetry_freshness": {
                key: freshness.get(key) for key in (
                    "source_age_ms", "queue_age_ms", "arrival_age_ms",
                    "source_updated_monotonic_ms", "generation",
                )
            },
            "dispatch": dispatch,
            "neutral_dispatch": neutral_dispatch,
        }
        return self._with_timing_and_ack(result, received_monotonic_ns, dispatch)


    def handoff(self, cancel_event=None, received_monotonic_ns=None):
        """Transmit neutral, disable virtual stick, and prove RC ownership."""
        cancel_event = cancel_event or threading.Event()
        self._pause_route_for_manual("handoff")
        with self._transition_lock:
            with self._lock:
                was_armed = self._armed
            initial_dispatch = None
            if was_armed:
                initial_dispatch = self.packets.set_velocity(VelocityCommand())
                # Four refresh periods make the neutral boundary visible before
                # Android rotates the authenticated control session on disable.
                cancel_event.wait(HANDOFF_NEUTRAL_SECONDS)
            try:
                response, record = self._post_and_wait(
                    "/virtual-stick/disable", timeout=HANDOFF_WAIT_SECONDS
                )
            except BridgeRequestError as error:
                current = self.telemetry.latest()
                status = current.value if current is not None else self.api.status()
                if not _authority_released(status):
                    raise
                response = error.body
                record = _command_record(error.body)
            finally:
                self.packets.disarm()
                with self._lock:
                    self._armed = False
                    self._armed_at_monotonic = None

            deadline = time.monotonic() + HANDOFF_WAIT_SECONDS
            released_status = None
            generation = self.telemetry.latest().generation if self.telemetry.latest() else 0
            while time.monotonic() < deadline:
                snapshot = self.telemetry.wait_for(
                    lambda candidate: _authority_released(candidate.value),
                    min(0.25, deadline - time.monotonic()),
                    after_generation=generation,
                )
                if snapshot is not None:
                    released_status = snapshot.value
                    break
                status = self.api.status()
                if _authority_released(status):
                    released_status = status
                    break
            if released_status is None:
                raise FlightSessionError(
                    "authority_handoff_timeout",
                    "virtual stick was disabled but current RC ownership was not observed",
                    {"bridge_command": record},
                )
            result = {
                "ok": True,
                "state": "rc_handoff_confirmed",
                "initial_neutral_dispatch": initial_dispatch,
                "bridge_command": record,
                "disable_response": response if record is None else None,
                "authority": self._authority_summary(released_status),
            }
            return self._with_timing_and_ack(
                result, received_monotonic_ns, initial_dispatch
            )


    def land(self, cancel_event=None, received_monotonic_ns=None):
        """Handoff first, request DJI landing, and report only observed ground."""
        cancel_event = cancel_event or threading.Event()
        handoff = self.handoff(cancel_event=cancel_event)
        if cancel_event.is_set():
            return {
                "ok": False,
                "state": "cancelled_after_rc_handoff_before_landing_request",
                "handoff": handoff,
                "physical_ground_state_confirmed": False,
            }
        before = self.telemetry.latest()
        if before is None:
            status = self.api.status()
            boundary_ms = status.get("control_monotonic_ms", 0)
            boundary_generation = 0
        else:
            status = before.value
            boundary_ms = status.get(
                "telemetry_generated_monotonic_ms", status.get("monotonic_ms", 0)
            )
            boundary_generation = before.generation
        response, record = self._post_and_wait("/land")
        confirmation_record = None
        confirmation_sent = False
        deadline = time.monotonic() + GROUND_WAIT_SECONDS
        latest_status = status
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                return {
                    "ok": False,
                    "state": "landing_monitor_cancelled_action_may_continue",
                    "bridge_command": record,
                    "handoff": handoff,
                    "physical_ground_state_confirmed": False,
                }
            snapshot = self.telemetry.wait_for(
                lambda candidate: True,
                min(0.25, deadline - time.monotonic()),
                after_generation=boundary_generation,
            )
            if snapshot is None:
                continue
            boundary_generation = snapshot.generation
            latest_status = snapshot.value
            aircraft = latest_status.get("aircraft_telemetry") or {}
            safety = aircraft.get("safety") or {}
            if (
                safety.get("landing_confirmation_needed") is True
                and not confirmation_sent
            ):
                _, confirmation_record = self._post_and_wait("/land/confirm")
                confirmation_sent = True
            flying_updated = aircraft.get("is_flying_updated_monotonic_ms")
            motors_updated = aircraft.get("motors_on_updated_monotonic_ms")
            grounded = (
                aircraft.get("is_flying") is False
                and aircraft.get("motors_on") is False
                and isinstance(flying_updated, (int, float))
                and isinstance(motors_updated, (int, float))
                and flying_updated >= boundary_ms
                and motors_updated >= boundary_ms
            )
            if grounded:
                result = {
                    "ok": True,
                    "state": "grounded_confirmed",
                    "physical_ground_state_confirmed": True,
                    "bridge_command": record,
                    "landing_confirmation_command": confirmation_record,
                    "handoff": handoff,
                    "ground_observation": {
                        "is_flying": False,
                        "motors_on": False,
                        "is_flying_updated_monotonic_ms": flying_updated,
                        "motors_on_updated_monotonic_ms": motors_updated,
                    },
                }
                return self._with_timing_and_ack(
                    result, received_monotonic_ns, None
                )
        raise FlightSessionError(
            "landing_ground_observation_timeout",
            "landing was accepted but fresh motors-off ground telemetry was not observed",
            {
                "bridge_command": record,
                "landing_confirmation_command": confirmation_record,
                "last_aircraft_telemetry": latest_status.get("aircraft_telemetry"),
            },
        )


    def status(self):
        latest = self.telemetry.latest()
        value = latest.value if latest is not None else None
        with self._lock:
            armed = self._armed
            fault = self._authority_fault
            monitor_last_error = self._monitor_last_error
        return {
            "ok": True,
            "state": "armed" if armed else "disarmed",
            "authority_fault": fault,
            "monitor_last_error": monitor_last_error,
            "monitor_thread_alive": bool(
                self._monitor_thread and self._monitor_thread.is_alive()
            ),
            "route_thread_alive": bool(
                self._route_thread and self._route_thread.is_alive()
            ),
            "control": self.packets.status(),
            "avoidance": self.avoidance_status(),
            "local_api": self._local_api_status(),
            "authority": self._authority_summary(value or {}),
            "telemetry": self.telemetry.diagnostics(),
            "telemetry_snapshot": _nymph_telemetry_snapshot(latest),
            "control_ack": self._ack_from_status(value, None),
            "route": self.route_status(),
            "capabilities": CAPABILITIES,
        }


    def _ack_from_status(self, status, dispatch):
        if not isinstance(status, dict):
            return {"observed": False, "reason": "telemetry_unavailable"}
        ack = {
            "session": status.get("last_control_session"),
            "sequence_hex": status.get("last_control_sequence_hex"),
            "sent_monotonic_ms": status.get("last_control_sent_monotonic_ms"),
            "received_monotonic_ms": status.get("last_control_received_monotonic_ms"),
            "applied_monotonic_ms": status.get("last_control_applied_monotonic_ms"),
            "receive_to_apply_ms": status.get("last_control_receive_to_apply_ms"),
            "end_to_end_latency_ms": status.get("last_control_latency_ms"),
            "setpoint": status.get("last_control_setpoint"),
        }
        if dispatch is None:
            ack["observed"] = ack["sequence_hex"] is not None
            return ack
        same_session = (
            str(ack.get("session", "")).lower()
            == str(dispatch.get("session", "")).lower()
        )
        expected_sequence = _parse_unsigned_hex(dispatch.get("sequence_hex"))
        actual_sequence = _parse_unsigned_hex(ack.get("sequence_hex"))
        same_local_target = _setpoints_equal(
            {"mode": "body_velocity", **self.packets.status().get("target", {})},
            dispatch.get("setpoint"),
        )
        echoed_target = _setpoints_equal(
            ack.get("setpoint"), dispatch.get("setpoint")
        )
        if not same_session:
            ack.update(observed=False, reason="matching_session_not_yet_observed")
        elif not same_local_target:
            ack.update(observed=False, reason="target_superseded_locally")
        elif expected_sequence is None or actual_sequence is None:
            ack.update(observed=False, reason="matching_ack_not_yet_observed")
        elif not unsigned_sequence_at_or_after(actual_sequence, expected_sequence):
            ack.update(observed=False, reason="matching_ack_not_yet_observed")
        elif not echoed_target:
            ack.update(observed=False, reason="acknowledged_setpoint_does_not_match")
        elif actual_sequence == expected_sequence:
            ack.update(observed=True, proof="exact_sequence_applied")
        else:
            ack.update(observed=True, proof="dispatch_or_newer_heartbeat_applied")
        return ack


    def _with_timing_and_ack(self, result, received_ns, dispatch):
        if received_ns is not None:
            result["command_received_monotonic_ns"] = received_ns
        if dispatch is not None:
            dispatched_ns = dispatch.get("dispatched_monotonic_ns")
            if received_ns is not None and isinstance(dispatched_ns, int):
                result["receipt_to_udp_dispatch_ms"] = max(
                    0.0, (dispatched_ns - received_ns) / 1e6
                )
        latest = self.telemetry.latest()
        result["control_ack"] = self._ack_from_status(
            latest.value if latest is not None else None, dispatch
        )
        result["telemetry_freshness"] = self.telemetry.diagnostics()
        return result


    def _monitor(self):
        last_generation = 0
        while not self._stop.wait(0.05):
            try:
                latest = self.telemetry.latest()
                now = time.monotonic()
                with self._lock:
                    armed = self._armed
                    arm_generation = self._arm_generation
                    armed_at = self._armed_at_monotonic
                    if armed and armed_at is None:
                        # Defensive for restored/test sessions that predate the
                        # explicit arm timestamp. Persist the grace boundary so
                        # a missing feed cannot postpone the stall indefinitely.
                        armed_at = now
                        self._armed_at_monotonic = now
                if not armed:
                    continue

                if latest is None:
                    age_ms = max(0.0, (now - armed_at) * 1000.0)
                    if age_ms >= TELEMETRY_STALL_SECONDS * 1000.0:
                        self._authority_lost(
                            "telemetry_stall",
                            {"arrival_age_ms": None, "armed_age_ms": age_ms},
                        )
                    continue

                # This guard intentionally runs before the generation gate. A
                # half-open telemetry connection leaves generation unchanged;
                # without an arrival-age lease it could preserve motion forever.
                arrival_age_ms = latest.arrival_age_ms
                if arrival_age_ms >= TELEMETRY_STALL_SECONDS * 1000.0:
                    self._authority_lost(
                        "telemetry_stall",
                        {
                            "arrival_age_ms": arrival_age_ms,
                            "generation": latest.generation,
                        },
                    )
                    continue

                if latest.generation <= last_generation:
                    continue
                last_generation = latest.generation
                if latest.generation <= arm_generation:
                    continue
                packet_status = self.packets.status()
                expected_session = packet_status.get("session")
                actual_session = latest.value.get("control_session")
                if (
                    expected_session
                    and actual_session
                    and expected_session != actual_session
                ):
                    self._authority_lost(
                        "control_session_rotated",
                        {"expected": expected_session, "observed": actual_session},
                    )
                    continue
                if not _authority_ready(latest.value):
                    self._authority_lost(
                        "msdk_authority_lost",
                        self._authority_summary(latest.value),
                    )
                    continue
                if now - self._last_context_sync >= CONTEXT_RESYNC_SECONDS:
                    self._synchronize_context(
                        require_ready=True, preserve_target=True
                    )
            except FlightSessionError as error:
                with self._lock:
                    self._monitor_last_error = {
                        "code": error.code,
                        "message": error.message,
                        "details": error.details,
                    }
                self._authority_lost(error.code, error.details)
            except BaseException as error:
                details = {
                    "exception": type(error).__name__,
                    "description": str(error),
                }
                with self._lock:
                    self._monitor_last_error = details
                # Never let an unexpected telemetry/API/status exception kill
                # the authority monitor while UDP motion remains retained.
                try:
                    self._authority_lost("authority_monitor_exception", details)
                except BaseException:
                    try:
                        self.packets.force_neutral_target()
                    except BaseException:
                        pass
                    try:
                        self.packets.disarm()
                    except BaseException:
                        pass


    def _authority_lost(self, reason, details=None):
        with self._route_control_lock:
            state = self._routes.snapshot()
            if state is not None and state.phase is RoutePhase.RUNNING:
                self._routes.pause()
            self._set_route_runtime(
                ownership="operator",
                last_reason="authority_lost_route_paused",
                last_error=reason,
            )
        with self._lock:
            was_armed = self._armed
            self._armed = False
            self._armed_at_monotonic = None
            if was_armed or self._authority_fault is None:
                self._authority_fault = {"reason": reason, "details": details}
        # Clear retained motion locally first, make one best-effort neutral
        # transmission while the authenticated context still exists, then stop
        # the stream. Android's deadman remains the final independent boundary.
        try:
            self.packets.force_neutral_target()
            try:
                self.packets.emit_once()
            except BaseException:
                pass
        finally:
            self.packets.disarm()


class JsonLinesRepl:
    """Preemptible long-lived command loop intended for a retained PTY."""

    def __init__(
        self,
        session,
        input_stream=sys.stdin,
        output_stream=sys.stdout,
        *,
        neutralize_on_disconnect=False,
        output_failure_callback=None,
        quit_callback=None,
    ):
        self.session = session
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.neutralize_on_disconnect = neutralize_on_disconnect
        self.output_failure_callback = output_failure_callback
        self.quit_callback = quit_callback
        self._output_lock = threading.Lock()
        self._output_failed = threading.Event()
        self._operation_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._operation_thread = None
        self._operation_cancel = None
        self._operation_id = None
        self._request_counter = 0
        self._closing = False
        self._cleaned_up = False

    def emit(self, value):
        with self._output_lock:
            if self._output_failed.is_set():
                return False
            try:
                self.output_stream.write(
                    json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
                )
                self.output_stream.flush()
                return True
            except (BrokenPipeError, ConnectionError, OSError, ValueError):
                self._output_failed.set()
                callback = self.output_failure_callback
                if callback is not None:
                    try:
                        callback()
                    except BaseException:
                        pass
                return False

    def run(self):
        try:
            self.emit({
                "event": "repl_ready",
                "protocol": "veil.flight-repl.v1",
                "commands": [
                    "status", "arm", "velocity", "neutral", "move_relative",
                    "rotate_relative", "route_accept", "route_start",
                    "route_pause", "route_resume", "route_abort", "route_status",
                    "avoidance", "avoidance_config", "handoff", "land", "quit",
                ],
                "capabilities": CAPABILITIES,
            })
            for raw_line in self.input_stream:
                received_ns = time.monotonic_ns()
                line = raw_line.strip()
                if not line:
                    continue
                request_id = None
                command_name = None
                try:
                    command = json.loads(line)
                    if isinstance(command, str):
                        command = {"command": command}
                    if not isinstance(command, dict):
                        raise FlightSessionError(
                            "invalid_repl_command", "command must be a JSON object"
                        )
                    if command.get("request_id") is not None:
                        request_id = str(command["request_id"])
                    if isinstance(command.get("command"), str):
                        command_name = command["command"].lower()
                    if self._dispatch(command, received_ns) is False:
                        break
                except FlightSessionError as error:
                    result = {
                        "event": "command_result", **error.to_dict(),
                        "command_received_monotonic_ns": received_ns,
                    }
                    if request_id is not None:
                        result["request_id"] = request_id
                    if command_name is not None:
                        result["command"] = command_name
                    self.emit(result)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    result = {
                        "event": "command_result", "ok": False, "state": "failed",
                        "error": "invalid_repl_command", "message": str(error),
                        "command_received_monotonic_ns": received_ns,
                    }
                    if request_id is not None:
                        result["request_id"] = request_id
                    if command_name is not None:
                        result["command"] = command_name
                    self.emit(result)
                if self._output_failed.is_set():
                    break
        finally:
            self.close_client()

    def _neutralize_retained_translation(self):
        try:
            self.session.neutral()
            return
        except BaseException:
            pass
        packets = getattr(self.session, "packets", None)
        if packets is not None:
            try:
                packets.force_neutral_target()
            except BaseException:
                pass

    def close_client(self):
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
        self._closing = True
        with self._operation_lock:
            operation = self._operation_thread
        settled = False
        try:
            settled = self._cancel_active(wait_seconds=1.0)
        finally:
            if self.neutralize_on_disconnect:
                self._neutralize_retained_translation()
                if not settled and operation is not None:
                    def neutralize_after_operation():
                        operation.join()
                        self._neutralize_retained_translation()

                    threading.Thread(
                        target=neutralize_after_operation,
                        name="veil-dji-disconnect-neutral-waiter",
                        daemon=True,
                    ).start()

    def _next_request_id(self, command):
        supplied = command.get("request_id")
        if supplied is not None:
            return str(supplied)
        self._request_counter += 1
        return f"local-{self._request_counter}"

    def _cancel_active(self, wait_seconds=0.12):
        with self._operation_lock:
            thread = self._operation_thread
            cancel = self._operation_cancel
        if thread is None or not thread.is_alive():
            return True
        cancel.set()
        thread.join(timeout=wait_seconds)
        return not thread.is_alive()

    def _start_operation(self, request_id, name, received_ns, function):
        if not self._cancel_active():
            raise FlightSessionError(
                "operation_preemption_timeout",
                "the previous physical operation did not reach neutral in time",
                {"active_request_id": self._operation_id},
            )
        cancel = threading.Event()

        def run_operation():
            try:
                try:
                    result = function(cancel)
                    result.update(event="command_result", request_id=request_id,
                                  command=name)
                except FlightSessionError as error:
                    result = {
                        "event": "command_result", "request_id": request_id,
                        "command": name, **error.to_dict(),
                        "command_received_monotonic_ns": received_ns,
                    }
                except Exception as error:  # Keep the retained REPL alive and neutral.
                    try:
                        self.session.neutral()
                    except FlightSessionError:
                        pass
                    result = {
                        "event": "command_result", "request_id": request_id,
                        "command": name, "ok": False, "state": "failed",
                        "error": "unhandled_operation_error",
                        "message": f"{type(error).__name__}: {error}",
                        "command_received_monotonic_ns": received_ns,
                    }
                self.emit(result)
            finally:
                with self._operation_lock:
                    if self._operation_thread is threading.current_thread():
                        self._operation_thread = None
                        self._operation_cancel = None
                        self._operation_id = None

        thread = threading.Thread(
            target=run_operation,
            name=f"veil-dji-operation-{request_id}",
            daemon=True,
        )
        with self._operation_lock:
            self._operation_thread = thread
            self._operation_cancel = cancel
            self._operation_id = request_id
        accepted_ns = time.monotonic_ns()
        self.emit({
            "event": "command_accepted", "ok": True, "state": "accepted",
            "request_id": request_id, "command": name,
            "command_received_monotonic_ns": received_ns,
            "worker_started_monotonic_ns": accepted_ns,
            "receipt_to_worker_start_ms": max(0.0, (accepted_ns - received_ns) / 1e6),
        })
        thread.start()

    def _dispatch(self, command, received_ns):
        name = command.get("command")
        if not isinstance(name, str):
            raise FlightSessionError(
                "invalid_repl_command", "string field 'command' is required"
            )
        name = name.lower()
        request_id = self._next_request_id(command)
        if name == "quit":
            self.emit({
                "event": "command_result", "request_id": request_id,
                "command": name, "ok": True, "state": "closing",
            })
            if self.quit_callback is not None:
                self.quit_callback()
            return False
        if name == "status":
            result = self.session.status()
            result.update(event="command_result", request_id=request_id, command=name,
                          command_received_monotonic_ns=received_ns)
            self.emit(result)
            return True
        if name == "route_status":
            result = self.session.route_status()
            result.update(
                event="command_result", request_id=request_id, command=name,
                command_received_monotonic_ns=received_ns,
            )
            self.emit(result)
            return True
        if name in ("avoidance", "avoidance_config"):
            result = self.session.configure_avoidance(
                mode=command.get("mode"),
                missing_data_behavior=command.get("missing_data_behavior"),
                minimum_clearance_m=_json_float(
                    command, "minimum_clearance_m", None
                ),
                reaction_time_s=_json_float(command, "reaction_time_s", None),
                maximum_deceleration_mps2=_json_float(
                    command, "maximum_deceleration_mps2", None
                ),
                maximum_source_age_ms=_json_float(
                    command, "maximum_source_age_ms", None
                ),
                horizontal_mapping=command.get("horizontal_mapping"),
                received_monotonic_ns=received_ns,
            )
            result.update(
                event="command_result", request_id=request_id, command=name
            )
            self.emit(result)
            return True
        if name == "route_accept":
            result = self.session.route_accept(
                command.get("document"), received_monotonic_ns=received_ns
            )
            result.update(event="command_result", request_id=request_id, command=name)
            self.emit(result)
            return True
        if name in ("route_start", "route_pause", "route_resume", "route_abort"):
            if not self._cancel_active():
                raise FlightSessionError(
                    "operation_preemption_timeout",
                    "previous operation did not neutralize within 120 ms",
                )
            operation = {
                "route_start": self.session.route_start,
                "route_pause": self.session.route_pause,
                "route_resume": self.session.route_resume,
                "route_abort": self.session.route_abort,
            }[name]
            result = operation(received_monotonic_ns=received_ns)
            result.update(event="command_result", request_id=request_id, command=name)
            self.emit(result)
            return True
        if name in ("velocity", "neutral"):
            if not self._cancel_active():
                raise FlightSessionError(
                    "operation_preemption_timeout",
                    "previous operation did not neutralize within 120 ms",
                )
            velocity = VelocityCommand() if name == "neutral" else VelocityCommand(
                forward_mps=_json_float(command, "forward_mps", 0.0),
                right_mps=_json_float(command, "right_mps", 0.0),
                up_mps=_json_float(command, "up_mps", 0.0),
                yaw_rate_deg_s=_json_float(command, "yaw_rate_deg_s", 0.0),
            )
            result = self.session.set_velocity(velocity, received_ns)
            result.update(event="command_result", request_id=request_id, command=name)
            self.emit(result)
            return True
        if name in ("arm", "rearm"):
            self._start_operation(
                request_id, name, received_ns,
                lambda _cancel: self.session.arm(),
            )
        elif name == "move_relative":
            self._start_operation(
                request_id, name, received_ns,
                lambda cancel: self.session.move_relative(
                    _json_float(command, "forward_m", 0.0),
                    _json_float(command, "right_m", 0.0),
                    _json_float(command, "up_m", 0.0),
                    _json_float(command, "speed_mps", 0.25),
                    cancel_event=cancel,
                    received_monotonic_ns=received_ns,
                ),
            )
        elif name == "rotate_relative":
            self._start_operation(
                request_id, name, received_ns,
                lambda cancel: self.session.rotate_relative(
                    _json_float(command, "degrees", required=True),
                    _json_float(command, "yaw_rate_deg_s", 25.0),
                    _json_float(command, "tolerance_deg", 1.0),
                    cancel_event=cancel,
                    received_monotonic_ns=received_ns,
                ),
            )
        elif name == "handoff":
            self._start_operation(
                request_id, name, received_ns,
                lambda cancel: self.session.handoff(
                    cancel_event=cancel, received_monotonic_ns=received_ns
                ),
            )
        elif name == "land":
            self._start_operation(
                request_id, name, received_ns,
                lambda cancel: self.session.land(
                    cancel_event=cancel, received_monotonic_ns=received_ns
                ),
            )
        else:
            raise FlightSessionError(
                "unknown_repl_command", f"unknown command: {name}"
            )
        return True


class UnixNdjsonServer:
    """Single-client local API retaining one FlightSession across reconnects."""

    def __init__(self, session, path, initial_event=None):
        if not isinstance(path, str) or not path or "\x00" in path:
            raise FlightSessionError(
                "local_api_socket_path_invalid",
                "Unix socket path must be a non-empty string without NUL bytes",
            )
        self.session = session
        self.path = os.path.abspath(os.path.expanduser(path))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._listener = None
        self._listener_identity = None
        self._accept_thread = None
        self._serving = False
        self._active_connection = None
        self._active_repl = None
        self._active_thread = None
        self._client_generation = 0
        self._accepted_clients = 0
        self._rejected_busy_clients = 0
        self._last_error = None
        self._last_disconnect_reason = None
        self._initial_event = initial_event
        self.session.set_local_api_status_provider(self.status)

    @staticmethod
    def _identity(metadata):
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            stat.S_IFMT(metadata.st_mode),
        )

    def set_initial_event(self, event):
        with self._lock:
            self._initial_event = event

    def _raise_unsafe_path(self, message, details=None):
        raise FlightSessionError(
            "local_api_socket_path_unsafe", message, details
        )

    def _remove_owned_stale_socket(self):
        try:
            original = os.lstat(self.path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(original.st_mode):
            self._raise_unsafe_path(
                "refusing to replace a non-socket local API path",
                {"path": self.path, "file_type": stat.S_IFMT(original.st_mode)},
            )
        if original.st_uid != os.geteuid():
            self._raise_unsafe_path(
                "refusing to replace a Unix socket owned by another user",
                {
                    "path": self.path,
                    "owner_uid": original.st_uid,
                    "effective_uid": os.geteuid(),
                },
            )

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.10)
        try:
            probe.connect(self.path)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return
            if error.errno != errno.ECONNREFUSED:
                self._raise_unsafe_path(
                    "existing Unix socket could not be proven stale",
                    {
                        "path": self.path,
                        "errno": error.errno,
                        "description": str(error),
                    },
                )
        else:
            raise FlightSessionError(
                "local_api_socket_in_use",
                "another process is listening on the Unix socket",
                {"path": self.path},
            )
        finally:
            probe.close()

        try:
            current = os.lstat(self.path)
        except FileNotFoundError:
            return
        if (
            self._identity(current) != self._identity(original)
            or not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.geteuid()
        ):
            self._raise_unsafe_path(
                "Unix socket changed while checking stale ownership",
                {"path": self.path},
            )
        os.unlink(self.path)

    def _unlink_bound_socket_if_unchanged(self):
        identity = self._listener_identity
        if identity is None:
            return
        try:
            current = os.lstat(self.path)
        except FileNotFoundError:
            return
        if (
            self._identity(current) == identity
            and stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.geteuid()
        ):
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
        else:
            with self._lock:
                self._last_error = (
                    "socket_path_changed_before_cleanup; path left untouched"
                )

    def _open_listener(self):
        with self._lock:
            if self._listener is not None:
                return
        parent = os.path.dirname(self.path) or "."
        if not os.path.isdir(parent):
            raise FlightSessionError(
                "local_api_socket_parent_invalid",
                "Unix socket parent directory does not exist",
                {"path": self.path, "parent": parent},
            )
        self._remove_owned_stale_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        bound_identity = None
        try:
            old_umask = os.umask(0o177)
            try:
                listener.bind(self.path)
                bound = True
            finally:
                os.umask(old_umask)
            metadata = os.lstat(self.path)
            if not stat.S_ISSOCK(metadata.st_mode):
                self._raise_unsafe_path(
                    "bound Unix path is unexpectedly not a socket",
                    {"path": self.path},
                )
            bound_identity = self._identity(metadata)
            os.chmod(self.path, 0o600)
            metadata = os.lstat(self.path)
            identity = self._identity(metadata)
            if metadata.st_uid != os.geteuid():
                self._raise_unsafe_path(
                    "bound Unix socket ownership is unexpected",
                    {"path": self.path, "owner_uid": metadata.st_uid},
                )
            listener.listen(4)
            listener.settimeout(0.20)
        except FlightSessionError:
            listener.close()
            if bound:
                try:
                    current = os.lstat(self.path)
                    if (
                        bound_identity is not None
                        and self._identity(current) == bound_identity
                        and stat.S_ISSOCK(current.st_mode)
                    ):
                        os.unlink(self.path)
                except FileNotFoundError:
                    pass
            raise
        except OSError as error:
            listener.close()
            if bound:
                try:
                    current = os.lstat(self.path)
                    if (
                        bound_identity is not None
                        and self._identity(current) == bound_identity
                        and stat.S_ISSOCK(current.st_mode)
                    ):
                        os.unlink(self.path)
                except FileNotFoundError:
                    pass
            raise FlightSessionError(
                "local_api_socket_bind_failed",
                f"could not bind Unix socket: {error}",
                {"path": self.path, "errno": error.errno},
            ) from error
        with self._lock:
            self._listener = listener
            self._listener_identity = identity
            self._last_error = None

    def start(self):
        self._open_listener()
        with self._lock:
            if self._accept_thread is not None and self._accept_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._accept_loop,
                name="veil-dji-unix-api",
                daemon=True,
            )
            self._accept_thread = thread
        thread.start()

    def serve_forever(self):
        self._open_listener()
        self._accept_loop()

    def request_stop(self):
        """Stop accepting clients; the owner retains the explicit handoff policy."""
        self._stop.set()
        with self._lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            listener.close()

    def _send_busy(self, connection):
        response = {
            "event": "server_busy",
            "ok": False,
            "state": "rejected",
            "error": "local_api_client_busy",
            "message": "another command client is already connected",
        }
        try:
            connection.sendall(
                (json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
                .encode("utf-8")
            )
        except OSError:
            pass
        finally:
            connection.close()

    @staticmethod
    def _shutdown_connection(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _accept_loop(self):
        with self._lock:
            self._serving = True
        try:
            while not self._stop.is_set():
                with self._lock:
                    listener = self._listener
                if listener is None:
                    return
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                except OSError as error:
                    if self._stop.is_set():
                        return
                    with self._lock:
                        self._last_error = f"accept_failed:{error}"
                    return
                with self._lock:
                    busy = self._active_connection is not None
                    if busy:
                        self._rejected_busy_clients += 1
                    else:
                        self._client_generation += 1
                        self._accepted_clients += 1
                        generation = self._client_generation
                        self._active_connection = connection
                if busy:
                    self._send_busy(connection)
                    continue
                thread = threading.Thread(
                    target=self._serve_client,
                    args=(connection, generation),
                    name=f"veil-dji-unix-client-{generation}",
                    daemon=True,
                )
                with self._lock:
                    self._active_thread = thread
                thread.start()
        finally:
            with self._lock:
                self._serving = False

    def _serve_client(self, connection, generation):
        reader = writer = repl = None
        reason = "client_eof"
        try:
            reader = connection.makefile("r", encoding="utf-8", newline="\n")
            writer = connection.makefile("w", encoding="utf-8", newline="\n")
            repl = JsonLinesRepl(
                self.session,
                reader,
                writer,
                neutralize_on_disconnect=True,
                output_failure_callback=lambda: self._shutdown_connection(
                    connection
                ),
                quit_callback=self.request_stop,
            )
            with self._lock:
                self._active_repl = repl
                initial_event = self._initial_event
            if initial_event is not None:
                repl.emit(dict(initial_event))
            repl.run()
            if repl._output_failed.is_set():
                reason = "client_output_disconnected"
        except (ConnectionError, OSError, ValueError) as error:
            reason = f"client_io_error:{type(error).__name__}"
        except BaseException as error:
            reason = f"client_handler_error:{type(error).__name__}"
            with self._lock:
                self._last_error = f"{type(error).__name__}: {error}"
        finally:
            if repl is not None:
                repl.close_client()
            for stream in (reader, writer):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            self._shutdown_connection(connection)
            connection.close()
            with self._lock:
                if self._active_connection is connection:
                    self._active_connection = None
                    self._active_repl = None
                    self._active_thread = None
                    self._last_disconnect_reason = reason

    def status(self):
        with self._lock:
            listener = self._listener
            identity = self._listener_identity
            serving = self._serving
            accept_thread = self._accept_thread
            active = self._active_connection is not None
            result = {
                "mode": "unix_ndjson",
                "socket_path": self.path,
                "listening": listener is not None and not self._stop.is_set(),
                "accept_loop_alive": serving and (
                    accept_thread is None or accept_thread.is_alive()
                ),
                "active_client": active,
                "client_generation": self._client_generation,
                "accepted_clients": self._accepted_clients,
                "rejected_busy_clients": self._rejected_busy_clients,
                "last_disconnect_reason": self._last_disconnect_reason,
                "last_error": self._last_error,
                "token_required_or_transmitted": False,
            }
        try:
            metadata = os.lstat(self.path)
            mode = stat.S_IMODE(metadata.st_mode)
            path_healthy = (
                identity is not None
                and self._identity(metadata) == identity
                and stat.S_ISSOCK(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and mode == 0o600
            )
            result.update({
                "socket_mode_octal": f"{mode:04o}",
                "socket_owner_uid": metadata.st_uid,
                "socket_path_healthy": path_healthy,
            })
        except FileNotFoundError:
            result.update({
                "socket_mode_octal": None,
                "socket_owner_uid": None,
                "socket_path_healthy": False,
            })
        result["healthy"] = bool(
            result["listening"]
            and result["accept_loop_alive"]
            and result["socket_path_healthy"]
        )
        return result

    def close(self):
        self._stop.set()
        with self._lock:
            listener = self._listener
            connection = self._active_connection
            repl = self._active_repl
            accept_thread = self._accept_thread
            active_thread = self._active_thread
            self._listener = None
        if listener is not None:
            listener.close()
        if connection is not None:
            self._shutdown_connection(connection)
        if repl is not None:
            repl.close_client()
        current = threading.current_thread()
        if active_thread is not None and active_thread is not current:
            active_thread.join(timeout=1.5)
        if accept_thread is not None and accept_thread is not current:
            accept_thread.join(timeout=1.0)
        self._unlink_bound_socket_if_unchanged()
        with self._lock:
            self._serving = False


def _json_float(value, name, default=None, required=False):
    if name not in value:
        if required:
            raise FlightSessionError(
                "invalid_repl_command", f"missing numeric field: {name}"
            )
        return default
    candidate = value[name]
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise FlightSessionError(
            "invalid_repl_command", f"{name} must be a finite number"
        )
    candidate = float(candidate)
    if not math.isfinite(candidate):
        raise FlightSessionError(
            "invalid_repl_command", f"{name} must be a finite number"
        )
    return candidate


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="persistent low-latency VEIL DJI flight-control session"
    )
    parser.add_argument(
        "--host", default=os.getenv("VEIL_DJI_HOST", "127.0.0.1")
    )
    parser.add_argument("--token", default=os.getenv("VEIL_DJI_TOKEN"))
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument("--realtime-control-port", type=int, default=8767)
    parser.add_argument("--telemetry-port", type=int, default=8768)
    parser.add_argument(
        "--unix-socket",
        default=os.getenv("VEIL_DJI_UNIX_SOCKET"),
        help=(
            "serve the retained NDJSON command API on this mode-0600 Unix "
            "socket instead of reading stdin"
        ),
    )
    parser.add_argument(
        "--no-auto-arm", action="store_true",
        help="start the REPL disarmed; use the arm command explicitly",
    )
    parser.add_argument(
        "--no-handoff-on-exit", action="store_true",
        help="skip best-effort RC handoff when the process exits",
    )
    return parser


def main(argv=None):
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("set VEIL_DJI_TOKEN or pass --token")
    api = BridgeApi(args.host, args.token, args.control_port)
    telemetry = TelemetryFeed(args.host, args.token, args.telemetry_port)
    packets = ControlPacketStream(
        args.host, args.realtime_control_port, args.token
    )
    session = FlightSession(api, telemetry, packets)
    repl = None
    server = None
    startup_event = None
    try:
        try:
            startup = session.start(auto_arm=not args.no_auto_arm)
            startup_event = {"event": "startup", **startup}
        except FlightSessionError as error:
            # Keep the process available for status/rearm after transient power,
            # USB, or authority setup problems.
            startup_event = {"event": "startup", **error.to_dict()}
        if args.unix_socket:
            server = UnixNdjsonServer(
                session, args.unix_socket, initial_event=startup_event
            )
            server.serve_forever()
        else:
            repl = JsonLinesRepl(session)
            repl.emit(startup_event)
            repl.run()
    except KeyboardInterrupt:
        if repl is not None:
            repl.emit({"event": "interrupt", "state": "closing"})
    finally:
        if server is not None:
            server.close()
        handoff = session.close(handoff=not args.no_handoff_on_exit)
        if repl is not None:
            repl.emit({"event": "closed", "handoff": handoff})


if __name__ == "__main__":
    main()
