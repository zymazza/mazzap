#!/usr/bin/env python3

import hashlib
import hmac
import io
import json
import os
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

import veil_dji_flight as flight


def ready_status(session="0123456789abcdef", server_ms=50_000):
    return {
        "control_packet_version": 2,
        "control_session": session,
        "control_monotonic_ms": server_ms,
        "virtual_stick_enabled": True,
        "virtual_stick_advanced_mode": True,
        "virtual_stick_control_mode": "body_velocity",
        "flight_control_authority": "MSDK",
        "control_failsafe_state": "armed_neutral",
        "aircraft_telemetry": {"authority": {"owner": "MSDK"}},
    }


def rc_status(session="fedcba9876543210", server_ms=50_000):
    return {
        "control_packet_version": 2,
        "control_session": session,
        "control_monotonic_ms": server_ms,
        "virtual_stick_enabled": False,
        "virtual_stick_advanced_mode": False,
        "virtual_stick_control_mode": "disabled",
        "flight_control_authority": "RC",
        "control_failsafe_state": "disarmed",
        "aircraft_telemetry": {"authority": {"owner": "RC"}},
    }


class FakeSocket:
    def __init__(self, *_args):
        self.sent = []
        self.closed = False

    def sendto(self, data, address):
        self.sent.append((data, address, time.monotonic_ns()))
        return len(data)

    def close(self):
        self.closed = True


class FailNextSocket(FakeSocket):
    def __init__(self, *_args):
        super().__init__()
        self.fail_next = False

    def sendto(self, data, address):
        if self.fail_next:
            self.fail_next = False
            raise OSError("injected UDP failure")
        return super().sendto(data, address)


class FakeMonotonicClock:
    def __init__(self, nanoseconds=1_000_000_000):
        self.nanoseconds = nanoseconds

    def __call__(self):
        return self.nanoseconds

    def advance(self, seconds):
        self.nanoseconds += int(seconds * 1_000_000_000)


class StaticTelemetry:
    def __init__(self, value):
        self.snapshot = flight.TelemetrySnapshot(value, 1, time.monotonic_ns())

    def start(self):
        pass

    def stop(self):
        pass

    def latest(self):
        return self.snapshot

    def diagnostics(self):
        return {"generation": self.snapshot.generation, "arrival_age_ms": 0.0}

    def wait_for(self, predicate, _timeout, after_generation=None):
        if (
            (after_generation is None or self.snapshot.generation > after_generation)
            and predicate(self.snapshot)
        ):
            return self.snapshot
        return None

    def publish(self, value):
        self.snapshot = flight.TelemetrySnapshot(
            value, self.snapshot.generation + 1, time.monotonic_ns()
        )


def navigation_status(
    latitude=0.0,
    longitude=0.0,
    altitude=10.0,
    yaw=0.0,
    generated_ms=10_000.0,
    queue_age_ms=0.0,
):
    status = ready_status(server_ms=int(generated_ms))
    status.update({
        "telemetry_sequence": 1,
        "telemetry_generated_monotonic_ms": generated_ms,
        "telemetry_queue_age_ms": queue_age_ms,
    })
    status["aircraft_telemetry"].update({
        "location": {
            "latitude_deg": latitude,
            "longitude_deg": longitude,
            "altitude_m": altitude,
            "updated_monotonic_ms": generated_ms,
        },
        "attitude": {
            "pitch_deg": 0.0,
            "roll_deg": 0.0,
            "yaw_deg": yaw,
            "updated_monotonic_ms": generated_ms,
        },
    })
    return status


def add_obstacle_ranges(
    status,
    horizontal_mm=(20_000, 20_000, 20_000, 20_000),
    interval_deg=90,
    upward_mm=20_000,
    downward_mm=20_000,
    updated_ms=None,
    working=None,
):
    generated_ms = status["telemetry_generated_monotonic_ms"]
    status["aircraft_telemetry"]["perception"] = {
        "information": {
            "observed": True,
            "working": working or {
                "forward": True,
                "backward": True,
                "left": True,
                "right": True,
                "upward": True,
                "downward": True,
            },
        },
        "obstacle_distances": {
            "source": "ObstacleDataListener",
            "observed": True,
            "updated_monotonic_ms": (
                generated_ms if updated_ms is None else updated_ms
            ),
            "horizontal_angle_interval_deg": interval_deg,
            "horizontal_distance_mm": list(horizontal_mm),
            "upward_distance_mm": upward_mm,
            "downward_distance_mm": downward_mm,
        },
    }
    return status


def route_document(
    revision,
    waypoints,
    expected=None,
    activation="immediate",
    scope="remaining_route_from_current_state",
):
    return json.dumps({
        "schema": "veil.route-revision.v1",
        "engine": "bridge_virtual_stick",
        "expected_accepted_revision": expected,
        "activation": activation,
        "scope": scope,
        "plan": {
            "route_id": "route-a",
            "revision": revision,
            "waypoints": waypoints,
        },
    })


def route_waypoint(latitude=0.001, longitude=0.0, altitude=10.0):
    return {
        "latitude_deg": latitude,
        "longitude_deg": longitude,
        "altitude_m": altitude,
    }


class BridgeApiTest(unittest.TestCase):
    def test_transport_failure_is_a_structured_session_error(self):
        api = flight.BridgeApi("bridge", "token")
        with mock.patch.object(
            flight.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaises(flight.BridgeTransportError) as caught:
                api.status()
        self.assertEqual("bridge_transport_error", caught.exception.code)
        self.assertEqual("GET", caught.exception.details["method"])
        self.assertEqual("/status", caught.exception.details["path"])


class YawTravelTrackerTest(unittest.TestCase):
    def test_wraparound_accumulates_a_full_clockwise_rotation(self):
        tracker = flight.YawTravelTracker(360.0, tolerance_degrees=1.0)
        start = 170.0
        for travel in range(0, 332, 10):
            yaw = (start + travel + 180.0) % 360.0 - 180.0
            progress = tracker.update(yaw)
        self.assertFalse(progress["complete"])
        self.assertLess(progress["observed_directed_travel_degrees"], 359.0)

        yaw = (start + 359.0 + 180.0) % 360.0 - 180.0
        progress = tracker.update(yaw)
        self.assertTrue(progress["complete"])
        self.assertAlmostEqual(359.0, progress["observed_directed_travel_degrees"])

    def test_331_degrees_can_never_report_360_complete(self):
        tracker = flight.YawTravelTracker(360.0, tolerance_degrees=1.0)
        tracker.update(0.0)
        for total in range(30, 331, 30):
            tracker.update((total + 180.0) % 360.0 - 180.0)
        progress = tracker.update((331.0 + 180.0) % 360.0 - 180.0)
        self.assertAlmostEqual(331.0, progress["observed_directed_travel_degrees"])
        self.assertFalse(progress["complete"])

    def test_counterclockwise_unwrap_uses_requested_direction(self):
        tracker = flight.YawTravelTracker(-180.0, tolerance_degrees=1.0)
        for yaw in (10.0, -40.0, -90.0, -140.0, 179.0, -171.0):
            progress = tracker.update(yaw)
        self.assertGreater(progress["observed_directed_travel_degrees"], 179.0)
        self.assertTrue(progress["complete"])

    def test_rotation_deadline_scales_with_angle_and_rate(self):
        short = flight.rotation_deadline_seconds(90.0, 30.0)
        full = flight.rotation_deadline_seconds(360.0, 30.0)
        slow = flight.rotation_deadline_seconds(360.0, 15.0)
        self.assertGreater(full, short)
        self.assertGreater(slow, full)
        self.assertGreater(full, 360.0 / 30.0)


class RelativeMoveTest(unittest.TestCase):
    def test_duration_and_vector_are_explicit_open_loop_command_values(self):
        duration, velocity = flight.move_duration_and_velocity(3.0, 4.0, 0.0, 1.0)
        self.assertAlmostEqual(5.0, duration)
        self.assertAlmostEqual(0.6, velocity.forward_mps)
        self.assertAlmostEqual(0.8, velocity.right_mps)


class ObstacleNormalizationTest(unittest.TestCase):
    def snapshot(self):
        return StaticTelemetry(add_obstacle_ranges(
            navigation_status(),
            horizontal_mm=(1_000, 2_000, 3_000, 4_000),
            upward_mm=5_000,
            downward_mm=600,
        )).latest()

    def test_default_mapping_is_conservative_and_not_directional_claim(self):
        observation, _now_ms, status = (
            flight._obstacle_observation_from_snapshot(self.snapshot())
        )
        self.assertEqual({
            "forward": 1.0,
            "backward": 1.0,
            "left": 1.0,
            "right": 1.0,
            "upward": 5.0,
            "downward": 0.6,
        }, observation.distances_m)
        self.assertEqual(
            "conservative_global", status["horizontal_mapping"]
        )
        self.assertFalse(status["horizontal_mapping_verified"])
        self.assertEqual(
            "no_directional_assumption",
            status["horizontal_mapping_assumption"],
        )
        self.assertIsInstance(observation.source_updated_monotonic_ms, float)

    def test_explicit_clockwise_mapping_uses_body_sectors_but_is_unverified(self):
        observation, _now_ms, status = (
            flight._obstacle_observation_from_snapshot(
                self.snapshot(), "body_clockwise_zero_forward"
            )
        )
        self.assertEqual(1.0, observation.distances_m["forward"])
        self.assertEqual(2.0, observation.distances_m["right"])
        self.assertEqual(3.0, observation.distances_m["backward"])
        self.assertEqual(4.0, observation.distances_m["left"])
        self.assertFalse(status["horizontal_mapping_verified"])
        self.assertEqual(
            "explicit_unverified_index_0_forward_clockwise_order",
            status["horizontal_mapping_assumption"],
        )

    def test_incomplete_horizontal_vector_is_missing_not_directional_truth(self):
        snapshot = StaticTelemetry(add_obstacle_ranges(
            navigation_status(),
            horizontal_mm=(1_000, 2_000),
            interval_deg=90,
            upward_mm=5_000,
            downward_mm=600,
        )).latest()
        observation, _now_ms, status = (
            flight._obstacle_observation_from_snapshot(snapshot)
        )
        self.assertFalse(status["horizontal_vector_complete"])
        self.assertEqual(
            "horizontal_vector_incomplete_or_invalid", status["reason"]
        )
        self.assertNotIn("forward", observation.distances_m)
        self.assertEqual(5.0, observation.distances_m["upward"])

    def test_all_dji_invalid_sentinels_are_missing_not_clear_ranges(self):
        snapshot = StaticTelemetry(add_obstacle_ranges(
            navigation_status(),
            horizontal_mm=(65_535, 65_535, 65_535, 65_535),
            upward_mm=65_535,
            downward_mm=0,
        )).latest()
        observation, _now_ms, status = (
            flight._obstacle_observation_from_snapshot(snapshot)
        )
        self.assertFalse(status["horizontal_vector_complete"])
        self.assertEqual({}, observation.distances_m)
        self.assertEqual(
            "horizontal_vector_incomplete_or_invalid", status["reason"]
        )

    def test_one_invalid_horizontal_sentinel_invalidates_whole_vector(self):
        snapshot = StaticTelemetry(add_obstacle_ranges(
            navigation_status(),
            horizontal_mm=(1_000, 65_535, 3_000, 4_000),
            upward_mm=5_000,
            downward_mm=600,
        )).latest()
        observation, _now_ms, status = (
            flight._obstacle_observation_from_snapshot(snapshot)
        )
        self.assertFalse(status["horizontal_vector_complete"])
        self.assertNotIn("forward", observation.distances_m)
        self.assertEqual(5.0, observation.distances_m["upward"])
        self.assertEqual(0.6, observation.distances_m["downward"])


class ControlPacketStreamTest(unittest.TestCase):
    def make_stream(self):
        fake_socket = FakeSocket()
        stream = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        return stream, fake_socket

    def test_immediate_packet_is_authenticated_and_uses_v2_body_velocity(self):
        stream, fake_socket = self.make_stream()
        context = flight.ControlContext(
            0x0123456789ABCDEF, "0123456789abcdef", 1234
        )
        stream.arm(context)
        dispatch = stream.set_velocity(
            flight.VelocityCommand(1.25, -0.5, 0.2, 33.0)
        )
        wire = fake_socket.sent[-1][0]
        payload, tag = wire[:-16], wire[-16:]
        self.assertEqual(
            hmac.new(b"secret", payload, hashlib.sha256).digest()[:16], tag
        )
        decoded = struct.unpack(">4sQQQiiii", payload)
        self.assertEqual(b"VDC2", decoded[0])
        self.assertEqual(context.session_id, decoded[1])
        self.assertEqual((1250, -500, 200, 33000), decoded[4:])
        self.assertEqual(f"{decoded[2]:016x}", dispatch["sequence_hex"])

    def test_started_stream_refreshes_latest_target_at_20_hz(self):
        stream, fake_socket = self.make_stream()
        stream.start()
        try:
            stream.arm(flight.ControlContext(7, "0000000000000007", 0))
            stream.set_velocity(flight.VelocityCommand(right_mps=0.25))
            baseline = len(fake_socket.sent)
            time.sleep(0.13)
            self.assertGreaterEqual(len(fake_socket.sent) - baseline, 2)
            latest_payload = fake_socket.sent[-1][0][:-16]
            decoded = struct.unpack(">4sQQQiiii", latest_payload)
            self.assertEqual(250, decoded[5])
        finally:
            stream.stop()

    def test_failed_initial_send_never_arms_periodic_motion(self):
        fake_socket = FailNextSocket()
        stream = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        stream.arm(flight.ControlContext(7, "0000000000000007", 0))
        stream.set_velocity(flight.VelocityCommand(right_mps=0.25))

        fake_socket.fail_next = True
        with self.assertRaises(flight.FlightSessionError) as caught:
            stream.set_velocity(flight.VelocityCommand(forward_mps=0.5))

        self.assertEqual("control_udp_send_failed", caught.exception.code)
        self.assertEqual(
            flight.VelocityCommand().to_dict(), stream.status()["target"]
        )
        stream.emit_once()
        decoded = struct.unpack(">4sQQQiiii", fake_socket.sent[-1][0][:-16])
        self.assertEqual((0, 0, 0, 0), decoded[4:])

    def test_moving_target_lease_expires_in_packet_sender(self):
        fake_socket = FakeSocket()
        clock = FakeMonotonicClock()
        stream = flight.ControlPacketStream(
            "bridge",
            8767,
            "secret",
            socket_factory=lambda *_args: fake_socket,
            monotonic_ns=clock,
        )
        stream.arm(flight.ControlContext(7, "0000000000000007", 0))
        stream.set_velocity(
            flight.VelocityCommand(forward_mps=0.4), lease_seconds=0.20
        )
        clock.advance(0.19)
        stream.emit_once()
        moving = struct.unpack(">4sQQQiiii", fake_socket.sent[-1][0][:-16])
        self.assertEqual(400, moving[4])

        clock.advance(0.02)
        stream.emit_once()
        neutral = struct.unpack(">4sQQQiiii", fake_socket.sent[-1][0][:-16])
        self.assertEqual((0, 0, 0, 0), neutral[4:])
        self.assertEqual(
            flight.VelocityCommand().to_dict(), stream.status()["target"]
        )
        self.assertEqual(1, stream.status()["target_lease_expirations"])


class TelemetryFeedTest(unittest.TestCase):
    def test_idle_half_open_connection_reauthenticates_and_recovers(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(2.0)
        port = listener.getsockname()[1]
        server_error = []

        def serve_two_sessions():
            first = second = None
            try:
                first, _ = listener.accept()
                self.assertEqual(b"TOKEN token\n", first.makefile("rb").readline())
                first.sendall(b'{"telemetry_sequence":10,"session":1}\n')
                # Leave the first socket deliberately half-open and silent.
                second, _ = listener.accept()
                self.assertEqual(b"TOKEN token\n", second.makefile("rb").readline())
                second.sendall(b'{"telemetry_sequence":1,"session":2}\n')
            except Exception as error:
                server_error.append(error)
            finally:
                for connection in (first, second):
                    if connection is not None:
                        connection.close()

        original_timeout = flight.TELEMETRY_IDLE_RECONNECT_SECONDS
        flight.TELEMETRY_IDLE_RECONNECT_SECONDS = 0.10
        server = threading.Thread(target=serve_two_sessions, daemon=True)
        feed = flight.TelemetryFeed(
            "127.0.0.1", "token", port=port, reconnect_delay=0.01
        )
        server.start()
        feed.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                latest = feed.latest()
                if latest is not None and latest.value.get("session") == 2:
                    break
                time.sleep(0.01)
            else:
                self.fail(f"telemetry did not recover: {feed.diagnostics()}")
            self.assertFalse(server_error)
            self.assertGreaterEqual(feed.diagnostics()["reconnects"], 2)
            self.assertTrue(feed.diagnostics()["thread_alive"])
            self.assertEqual(1, feed.diagnostics()["last_sequence"])
        finally:
            feed.stop()
            listener.close()
            server.join(timeout=1.0)
            flight.TELEMETRY_IDLE_RECONNECT_SECONDS = original_timeout

    def test_sequence_baseline_resets_after_android_restart(self):
        feed = flight.TelemetryFeed("unused", "token")
        feed._publish({"telemetry_sequence": 100}, time.monotonic_ns())
        first_generation = feed.latest().generation
        feed._reset_connection_sequence()
        feed._publish({"telemetry_sequence": 1}, time.monotonic_ns())
        self.assertGreater(feed.latest().generation, first_generation)
        self.assertEqual(1, feed.diagnostics()["last_sequence"])

    def test_out_of_order_frame_on_same_connection_is_not_published(self):
        feed = flight.TelemetryFeed("unused", "token")
        feed._publish({"telemetry_sequence": 4}, time.monotonic_ns())
        generation = feed.latest().generation
        feed._publish({"telemetry_sequence": 3}, time.monotonic_ns())
        self.assertEqual(generation, feed.latest().generation)
        self.assertEqual(1, feed.diagnostics()["out_of_order_frames"])


class NymphTelemetrySnapshotTest(unittest.TestCase):
    EXPECTED_FIELDS = {
        "available",
        "arrival_age_ms",
        "fresh",
        "stale",
        "product_connected",
        "aircraft_connected",
        "remote_controller_connected",
        "airlink_connected",
        "flight_mode",
        "is_flying",
        "motors_on",
        "latitude_deg",
        "longitude_deg",
        "relative_altitude_m",
        "yaw_deg",
        "velocity_north_mps",
        "velocity_east_mps",
        "velocity_down_mps",
        "gps_signal_level",
        "gps_satellite_count",
        "battery_percent",
        "authority_owner",
        "airlink_signal_quality",
    }

    def make_status(self):
        sentinel = "DO_NOT_EXPOSE_NYMPH_SENTINEL"
        return {
            "product_connected": True,
            "aircraft_connected": True,
            "remote_controller_connected": True,
            "airlink_connected": True,
            "flight_mode": "GPS_NORMAL",
            "motors_on": True,
            "altitude_m": 12.5,
            "flight_control_authority": "MSDK",
            "airlink_signal_quality": 87,
            "token": sentinel,
            "device_id": sentinel,
            "private": {"unknown": sentinel},
            "command_secret": sentinel,
            "last_control_session": sentinel,
            "aircraft_telemetry": {
                "aircraft_connected": True,
                "is_flying": True,
                "motors_on": True,
                "flight_mode": "GPS_NORMAL",
                "serial_number": sentinel,
                "device_id": sentinel,
                "private": sentinel,
                "location": {
                    "latitude_deg": 40.123,
                    "longitude_deg": -74.456,
                    "altitude_m": 99.0,
                    "unknown": sentinel,
                },
                "attitude": {"yaw_deg": -91.25, "private": sentinel},
                "velocity_ned": {
                    "north_mps": 1.25,
                    "east_mps": -0.5,
                    "down_mps": 0.125,
                    "unknown": sentinel,
                },
                "gps": {
                    "signal_level": "LEVEL_5",
                    "satellite_count": 19,
                    "private": sentinel,
                },
                "battery": {
                    "charge_remaining_percent": 76,
                    "serial_number": sentinel,
                    "manufactured_date": {"private": sentinel},
                    "firmware_version": sentinel,
                },
                "authority": {"owner": "RC", "private": sentinel},
                "remote_controller": {
                    "serial_number": sentinel,
                    "device_id": sentinel,
                },
                "unknown": {"token": sentinel},
            },
            "unknown": {"serial_number": sentinel},
        }

    def make_session(self, telemetry):
        packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: FakeSocket()
        )
        return flight.FlightSession(None, telemetry, packets)

    def test_status_exposes_only_scalar_allowlist_and_excludes_private_fields(self):
        now_ns = 20_000_000_000
        raw = self.make_status()
        telemetry = StaticTelemetry(raw)
        telemetry.snapshot = flight.TelemetrySnapshot(
            raw, 7, now_ns - 100_000_000
        )
        session = self.make_session(telemetry)

        with mock.patch.object(flight.time, "monotonic_ns", return_value=now_ns):
            snapshot = session.status()["telemetry_snapshot"]

        self.assertEqual(self.EXPECTED_FIELDS, set(snapshot))
        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["fresh"])
        self.assertFalse(snapshot["stale"])
        self.assertAlmostEqual(100.0, snapshot["arrival_age_ms"])
        self.assertTrue(snapshot["product_connected"])
        self.assertTrue(snapshot["aircraft_connected"])
        self.assertTrue(snapshot["remote_controller_connected"])
        self.assertTrue(snapshot["airlink_connected"])
        self.assertEqual("GPS_NORMAL", snapshot["flight_mode"])
        self.assertTrue(snapshot["is_flying"])
        self.assertTrue(snapshot["motors_on"])
        self.assertEqual(40.123, snapshot["latitude_deg"])
        self.assertEqual(-74.456, snapshot["longitude_deg"])
        self.assertEqual(12.5, snapshot["relative_altitude_m"])
        self.assertEqual(-91.25, snapshot["yaw_deg"])
        self.assertEqual(1.25, snapshot["velocity_north_mps"])
        self.assertEqual(-0.5, snapshot["velocity_east_mps"])
        self.assertEqual(0.125, snapshot["velocity_down_mps"])
        self.assertEqual("LEVEL_5", snapshot["gps_signal_level"])
        self.assertEqual(19, snapshot["gps_satellite_count"])
        self.assertEqual(76, snapshot["battery_percent"])
        self.assertEqual("MSDK", snapshot["authority_owner"])
        self.assertEqual(87, snapshot["airlink_signal_quality"])
        serialized = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn("DO_NOT_EXPOSE_NYMPH_SENTINEL", serialized)
        for forbidden in (
            "serial_number",
            "manufactured_date",
            "firmware_version",
            "device_id",
            "token",
            "private",
            "command_secret",
            "unknown",
        ):
            self.assertNotIn(forbidden, snapshot)

    def test_status_marks_retained_snapshot_stale_by_arrival_age(self):
        now_ns = 20_000_000_000
        stale_age_ms = flight.NYMPH_TELEMETRY_STALE_AFTER_MS + 1.0
        raw = self.make_status()
        telemetry = StaticTelemetry(raw)
        telemetry.snapshot = flight.TelemetrySnapshot(
            raw, 8, now_ns - int(stale_age_ms * 1_000_000)
        )
        session = self.make_session(telemetry)

        with mock.patch.object(flight.time, "monotonic_ns", return_value=now_ns):
            snapshot = session.status()["telemetry_snapshot"]

        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["fresh"])
        self.assertTrue(snapshot["stale"])
        self.assertAlmostEqual(stale_age_ms, snapshot["arrival_age_ms"])
        self.assertEqual(40.123, snapshot["latitude_deg"])

    def test_status_has_stable_empty_snapshot_before_first_telemetry(self):
        class EmptyTelemetry:
            @staticmethod
            def latest():
                return None

            @staticmethod
            def diagnostics():
                return {"generation": 0, "arrival_age_ms": None}

        snapshot = self.make_session(EmptyTelemetry()).status()[
            "telemetry_snapshot"
        ]
        self.assertEqual(self.EXPECTED_FIELDS, set(snapshot))
        self.assertFalse(snapshot["available"])
        self.assertFalse(snapshot["fresh"])
        self.assertTrue(snapshot["stale"])
        self.assertIsNone(snapshot["arrival_age_ms"])
        self.assertTrue(all(
            value is None
            for key, value in snapshot.items()
            if key not in {"available", "fresh", "stale"}
        ))


class AckCorrelationTest(unittest.TestCase):
    def setUp(self):
        self.fake_socket = FakeSocket()
        self.packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: self.fake_socket
        )
        self.packets.arm(
            flight.ControlContext(0x22, "0000000000000022", 0)
        )
        self.session = flight.FlightSession(None, StaticTelemetry({}), self.packets)

    def status_for(self, dispatch, sequence_hex=None, setpoint=None):
        return {
            "last_control_session": dispatch["session"],
            "last_control_sequence_hex": sequence_hex or dispatch["sequence_hex"],
            "last_control_sent_monotonic_ms": 1,
            "last_control_received_monotonic_ms": 2,
            "last_control_applied_monotonic_ms": 3,
            "last_control_receive_to_apply_ms": 1,
            "last_control_latency_ms": 2,
            "last_control_setpoint": setpoint or {
                "mode": "body_velocity", **dispatch["setpoint"],
            },
        }

    def test_exact_sequence_ack_is_proof(self):
        dispatch = self.packets.set_velocity(
            flight.VelocityCommand(right_mps=0.3)
        )
        ack = self.session._ack_from_status(self.status_for(dispatch), dispatch)
        self.assertTrue(ack["observed"])
        self.assertEqual("exact_sequence_applied", ack["proof"])

    def test_newer_heartbeat_with_same_echo_proves_dispatch_target(self):
        dispatch = self.packets.set_velocity(
            flight.VelocityCommand(right_mps=0.3)
        )
        newer = (int(dispatch["sequence_hex"], 16) + 4) & flight.UINT64_MASK
        ack = self.session._ack_from_status(
            self.status_for(dispatch, f"{newer:016x}"), dispatch
        )
        self.assertTrue(ack["observed"])
        self.assertEqual("dispatch_or_newer_heartbeat_applied", ack["proof"])

    def test_newer_sequence_with_different_echo_is_not_false_proof(self):
        dispatch = self.packets.set_velocity(
            flight.VelocityCommand(right_mps=0.3)
        )
        newer = (int(dispatch["sequence_hex"], 16) + 4) & flight.UINT64_MASK
        wrong = {"mode": "body_velocity", **dispatch["setpoint"]}
        wrong["right_mps"] = -0.3
        ack = self.session._ack_from_status(
            self.status_for(dispatch, f"{newer:016x}", wrong), dispatch
        )
        self.assertFalse(ack["observed"])
        self.assertEqual("acknowledged_setpoint_does_not_match", ack["reason"])

    def test_unsigned_sequence_comparison_handles_wrap(self):
        self.assertTrue(flight.unsigned_sequence_at_or_after(1, flight.UINT64_MASK))
        self.assertFalse(flight.unsigned_sequence_at_or_after(
            flight.UINT64_MASK, 1
        ))


class ArmOrderingTest(unittest.TestCase):
    def test_neutral_stream_begins_before_waiting_for_enable_callback(self):
        fake_socket = FakeSocket()
        packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        ready = ready_status()

        class Api:
            def __init__(self):
                self.first = True

            def status(self):
                if self.first:
                    self.first = False
                    return rc_status()
                return ready

            def request(inner_self, method, path):
                if method == "POST":
                    return {
                        "command_id": "enable-1", "state": "requested",
                        "result_url": "/commands/enable-1",
                        "command": {"id": "enable-1", "state": "requested"},
                    }
                self.assertGreater(len(fake_socket.sent), 0)
                return {"id": "enable-1", "state": "succeeded"}

        telemetry = StaticTelemetry(ready)
        session = flight.FlightSession(Api(), telemetry, packets)
        result = session.arm()
        self.assertTrue(result["ok"])
        self.assertEqual("armed_neutral", result["state"])
        self.assertGreater(len(fake_socket.sent), 0)

    def test_explicit_rearm_acquires_a_rotated_session(self):
        fake_socket = FakeSocket()
        packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        second = ready_status("2222222222222222")

        class Api:
            def status(self):
                return second

        session = flight.FlightSession(Api(), StaticTelemetry(second), packets)
        session._authority_lost("test")
        result = session.arm()
        self.assertEqual("2222222222222222", result["session"])
        self.assertEqual("2222222222222222", packets.status()["session"])


class AuthorityMonitorTest(unittest.TestCase):
    def make_armed_session(self, status, api=None):
        fake_socket = FakeSocket()
        packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        session_hex = status["control_session"]
        packets.arm(flight.ControlContext(int(session_hex, 16), session_hex, 0))
        packets.set_velocity(flight.VelocityCommand(forward_mps=0.4))
        telemetry = StaticTelemetry(status)
        session = flight.FlightSession(api, telemetry, packets)
        session._armed = True
        session._armed_at_monotonic = time.monotonic()
        session._arm_generation = 0
        session._last_context_sync = time.monotonic()
        worker = threading.Thread(target=session._monitor, daemon=True)
        session._monitor_thread = worker
        return session, telemetry, packets, fake_socket, worker

    def wait_for_disarm(self, session):
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            with session._lock:
                if not session._armed:
                    return
            time.sleep(0.01)
        self.fail("authority monitor did not disarm the stale session")

    def stop_monitor(self, session, worker):
        session._stop.set()
        worker.join(timeout=1.0)

    def test_unchanged_telemetry_generation_stalls_and_neutralizes(self):
        session, telemetry, packets, _socket, worker = self.make_armed_session(
            ready_status()
        )
        telemetry.snapshot = flight.TelemetrySnapshot(
            telemetry.snapshot.value,
            telemetry.snapshot.generation,
            time.monotonic_ns()
            - int((flight.TELEMETRY_STALL_SECONDS + 0.1) * 1_000_000_000),
        )
        worker.start()
        try:
            self.wait_for_disarm(session)
            self.assertTrue(worker.is_alive())
            self.assertEqual(
                "telemetry_stall", session.status()["authority_fault"]["reason"]
            )
            self.assertEqual(
                flight.VelocityCommand().to_dict(), packets.status()["target"]
            )
            self.assertFalse(packets.status()["armed"])
            self.assertTrue(session.status()["monitor_thread_alive"])
        finally:
            self.stop_monitor(session, worker)

    def test_bridge_transport_error_disarms_without_killing_monitor(self):
        class OfflineApi:
            def status(self):
                raise flight.BridgeTransportError(
                    "GET", "/status", urllib.error.URLError("offline")
                )

        session, _telemetry, packets, _socket, worker = self.make_armed_session(
            ready_status(), api=OfflineApi()
        )
        session._last_context_sync = 0.0
        worker.start()
        try:
            self.wait_for_disarm(session)
            self.assertTrue(worker.is_alive())
            status = session.status()
            self.assertEqual(
                "bridge_transport_error", status["authority_fault"]["reason"]
            )
            self.assertEqual(
                "bridge_transport_error", status["monitor_last_error"]["code"]
            )
            self.assertFalse(packets.status()["armed"])
        finally:
            self.stop_monitor(session, worker)


class ObstacleLimiterIntegrationTest(unittest.TestCase):
    def make_session(self, status=None):
        fake_socket = FakeSocket()
        packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        packets.arm(flight.ControlContext(0x22, "0000000000000022", 0))
        telemetry = StaticTelemetry(status or navigation_status())
        session = flight.FlightSession(None, telemetry, packets)
        session._armed = True
        return session, telemetry, packets, fake_socket

    def test_default_advisory_reports_threat_without_hidden_blocking(self):
        status = add_obstacle_ranges(
            navigation_status(), horizontal_mm=(900, 5_000, 5_000, 5_000)
        )
        session, _telemetry, packets, _socket = self.make_session(status)

        result = session.set_velocity(
            flight.VelocityCommand(forward_mps=2.0, yaw_rate_deg_s=15.0)
        )

        self.assertEqual("advisory", result["avoidance"]["mode"])
        self.assertEqual("threat_advisory", result["avoidance"]["reason"])
        self.assertEqual(result["requested_setpoint"], result["applied_setpoint"])
        self.assertEqual(2.0, packets.status()["applied_target"]["forward_mps"])
        self.assertEqual(15.0, packets.status()["applied_target"]["yaw_rate_deg_s"])
        avoidance = session.status()["avoidance"]
        self.assertTrue(avoidance["default_is_non_blocking_advisory"])
        self.assertEqual("conservative_global", avoidance["horizontal_mapping"])

    def test_brake_reapplies_each_packet_as_perception_changes(self):
        clear = add_obstacle_ranges(navigation_status())
        session, telemetry, packets, fake_socket = self.make_session(clear)
        session.configure_avoidance(mode="brake")
        first = session.set_velocity(
            flight.VelocityCommand(forward_mps=2.0, yaw_rate_deg_s=20.0)
        )
        self.assertEqual(2.0, first["applied_setpoint"]["forward_mps"])

        telemetry.publish(add_obstacle_ranges(
            navigation_status(), horizontal_mm=(800, 800, 800, 800)
        ))
        dispatch = packets.emit_once()

        self.assertEqual(2.0, dispatch["requested_setpoint"]["forward_mps"])
        self.assertEqual(0.0, dispatch["applied_setpoint"]["forward_mps"])
        self.assertEqual(20.0, dispatch["applied_setpoint"]["yaw_rate_deg_s"])
        self.assertEqual("braking", dispatch["avoidance"]["reason"])
        self.assertEqual(2.0, packets.status()["requested_target"]["forward_mps"])
        decoded = struct.unpack(">4sQQQiiii", fake_socket.sent[-1][0][:-16])
        self.assertEqual(0, decoded[4])
        self.assertEqual(20_000, decoded[7])

    def test_brake_missing_data_can_fail_closed_while_preserving_yaw(self):
        session, _telemetry, _packets, _socket = self.make_session()
        configured = session.configure_avoidance(
            mode="brake", missing_data_behavior="stop_translation",
            minimum_clearance_m=1.0, reaction_time_s=0.3,
            maximum_deceleration_mps2=1.5, maximum_source_age_ms=200.0,
        )
        result = session.set_velocity(flight.VelocityCommand(
            forward_mps=1.0, right_mps=0.5, yaw_rate_deg_s=12.0
        ))

        self.assertEqual((0.0, 0.0, 0.0), (
            result["applied_setpoint"]["forward_mps"],
            result["applied_setpoint"]["right_mps"],
            result["applied_setpoint"]["up_mps"],
        ))
        self.assertEqual(12.0, result["applied_setpoint"]["yaw_rate_deg_s"])
        self.assertEqual(
            "missing_or_stale_obstacle_data_stop",
            result["avoidance"]["reason"],
        )
        config = configured["avoidance"]["config"]
        self.assertEqual("stop_translation", config["missing_data_behavior"])
        self.assertEqual(1.0, config["minimum_clearance_m"])
        self.assertEqual(0.3, config["reaction_time_s"])
        self.assertEqual(1.5, config["maximum_deceleration_mps2"])
        self.assertEqual(200.0, config["maximum_source_age_ms"])

    def test_source_timestamp_age_drives_stale_missing_behavior(self):
        status = add_obstacle_ranges(
            navigation_status(generated_ms=10_000.0),
            updated_ms=9_500.0,
        )
        session, _telemetry, _packets, _socket = self.make_session(status)
        session.configure_avoidance(
            mode="brake",
            missing_data_behavior="stop_translation",
            maximum_source_age_ms=100.0,
        )
        result = session.set_velocity(
            flight.VelocityCommand(up_mps=1.0, yaw_rate_deg_s=8.0)
        )
        self.assertFalse(result["avoidance"]["observation_fresh"])
        self.assertGreaterEqual(
            result["avoidance"]["perception"]["source_age_ms"], 500.0
        )
        self.assertEqual(0.0, result["applied_setpoint"]["up_mps"])
        self.assertEqual(8.0, result["applied_setpoint"]["yaw_rate_deg_s"])

    def test_off_mode_passes_near_obstacle_unchanged(self):
        status = add_obstacle_ranges(
            navigation_status(), horizontal_mm=(100, 100, 100, 100)
        )
        session, _telemetry, _packets, _socket = self.make_session(status)
        session.configure_avoidance(mode="off")
        result = session.set_velocity(
            flight.VelocityCommand(forward_mps=1.0)
        )
        self.assertEqual(1.0, result["applied_setpoint"]["forward_mps"])
        self.assertEqual("disabled", result["avoidance"]["reason"])

    def test_explicit_unverified_body_mapping_changes_directional_braking(self):
        status = add_obstacle_ranges(
            navigation_status(), horizontal_mm=(500, 20_000, 20_000, 20_000)
        )
        session, _telemetry, _packets, _socket = self.make_session(status)
        configured = session.configure_avoidance(
            mode="brake", horizontal_mapping="body_clockwise_zero_forward"
        )
        result = session.set_velocity(
            flight.VelocityCommand(right_mps=2.0)
        )

        self.assertEqual(2.0, result["applied_setpoint"]["right_mps"])
        self.assertEqual(
            "body_clockwise_zero_forward",
            configured["avoidance"]["horizontal_mapping"],
        )
        self.assertFalse(
            configured["avoidance"]["horizontal_mapping_verified"]
        )
        self.assertTrue(
            configured["avoidance"][
                "directional_mapping_requires_ground_calibration"
            ]
        )

    def test_relative_move_and_route_use_same_brake_path(self):
        near = add_obstacle_ranges(
            navigation_status(), horizontal_mm=(900, 900, 900, 900)
        )
        move_session, _telemetry, _packets, _socket = self.make_session(near)
        move_session.configure_avoidance(mode="brake")
        moved = move_session.move_relative(0.01, 0.0, 0.0, 1.0)
        self.assertLess(moved["dispatch"]["applied_setpoint"]["forward_mps"], 1.0)
        self.assertEqual(
            "braking", moved["dispatch"]["avoidance"]["reason"]
        )

        route_session, _telemetry, _packets, _socket = self.make_session(near)
        route_session.configure_avoidance(mode="brake")
        route_session.route_accept(route_document(
            1, [route_waypoint(latitude=0.001)]
        ))
        started = route_session.route_start()
        guidance = started["guidance"]
        self.assertGreater(
            guidance["requested_body_setpoint"]["forward_mps"],
            guidance["applied_body_setpoint"]["forward_mps"],
        )
        self.assertEqual("braking", guidance["avoidance"]["reason"])

    def test_limiter_exception_neutralizes_periodic_and_manual_paths(self):
        session, _telemetry, packets, fake_socket = self.make_session()
        session.set_velocity(flight.VelocityCommand(forward_mps=1.0))

        def broken_limiter(_command):
            raise RuntimeError("injected limiter failure")

        packets.set_command_limiter(broken_limiter)
        periodic = packets.emit_once()
        self.assertEqual(
            flight.VelocityCommand().to_dict(), periodic["applied_setpoint"]
        )
        self.assertEqual(
            "command_limiter_failed_neutral",
            periodic["limiter_failure"]["error"],
        )
        with self.assertRaises(flight.FlightSessionError) as caught:
            session.set_velocity(flight.VelocityCommand(right_mps=1.0))
        self.assertEqual("command_limiter_failed_neutral", caught.exception.code)
        self.assertEqual(
            flight.VelocityCommand().to_dict(), packets.status()["target"]
        )
        self.assertEqual(
            "limiter_exception_neutral",
            packets.status()["avoidance"]["reason"],
        )
        decoded = struct.unpack(">4sQQQiiii", fake_socket.sent[-1][0][:-16])
        self.assertEqual((0, 0, 0, 0), decoded[4:])

    def test_repl_avoidance_command_updates_complete_config(self):
        session, _telemetry, _packets, _socket = self.make_session()
        lines = io.StringIO(
            '{"command":"avoidance","mode":"brake",'
            '"missing_data_behavior":"stop_translation",'
            '"minimum_clearance_m":1.2,"reaction_time_s":0.4,'
            '"maximum_deceleration_mps2":1.25,'
            '"maximum_source_age_ms":180,'
            '"horizontal_mapping":"body_clockwise_zero_forward"}\n'
        )
        output = io.StringIO()
        flight.JsonLinesRepl(session, lines, output).run()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        result = [item for item in records if item.get("command") == "avoidance"][-1]
        self.assertTrue(result["ok"])
        config = result["avoidance"]["config"]
        self.assertEqual("brake", config["mode"])
        self.assertEqual("stop_translation", config["missing_data_behavior"])
        self.assertEqual(1.2, config["minimum_clearance_m"])
        self.assertEqual(0.4, config["reaction_time_s"])
        self.assertEqual(1.25, config["maximum_deceleration_mps2"])
        self.assertEqual(180.0, config["maximum_source_age_ms"])

    def test_repl_synchronous_failure_echoes_request_id_and_command(self):
        session, _telemetry, _packets, _socket = self.make_session()
        lines = io.StringIO(
            '{"command":"route_start","request_id":"route-7"}\n'
        )
        output = io.StringIO()

        flight.JsonLinesRepl(session, lines, output).run()

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        result = records[-1]
        self.assertEqual("command_result", result["event"])
        self.assertFalse(result["ok"])
        self.assertEqual("route-7", result["request_id"])
        self.assertEqual("route_start", result["command"])
        self.assertEqual("route_unavailable", result["error"])


class UnixNdjsonServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temporary.name, "flight.sock")
        self.servers = []

    def tearDown(self):
        for server in reversed(self.servers):
            server.close()
        self.temporary.cleanup()

    def make_session(self):
        fake_socket = FakeSocket()
        packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        packets.arm(flight.ControlContext(0x22, "0000000000000022", 0))
        telemetry = StaticTelemetry(navigation_status())
        session = flight.FlightSession(None, telemetry, packets)
        session._armed = True
        return session, packets

    def start_server(self, session):
        server = flight.UnixNdjsonServer(session, self.path)
        self.servers.append(server)
        server.start()
        self.wait_until(lambda: server.status()["healthy"])
        return server

    def wait_until(self, predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition did not become true before timeout")

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(self.path)
        reader = client.makefile("r", encoding="utf-8", newline="\n")
        return client, reader

    def read_json(self, reader):
        line = reader.readline()
        self.assertTrue(line, "server closed before emitting an NDJSON record")
        return json.loads(line)

    def close_client(self, client, reader):
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        reader.close()
        client.close()

    def test_socket_is_mode_0600_and_health_is_in_session_status(self):
        session, _packets = self.make_session()
        server = self.start_server(session)
        metadata = os.lstat(self.path)
        self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
        self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
        health = session.status()["local_api"]
        self.assertTrue(health["healthy"])
        self.assertEqual("unix_ndjson", health["mode"])
        self.assertEqual("0600", health["socket_mode_octal"])
        self.assertEqual(0, health["client_generation"])
        self.assertFalse(health["token_required_or_transmitted"])
        self.assertEqual(server.path, health["socket_path"])

    def test_owned_stale_socket_is_replaced_but_regular_path_is_refused(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.path)
        stale.close()
        session, _packets = self.make_session()
        server = self.start_server(session)
        self.assertTrue(server.status()["socket_path_healthy"])
        server.close()
        self.servers.remove(server)

        with open(self.path, "w", encoding="utf-8") as output:
            output.write("do not replace")
        refused = flight.UnixNdjsonServer(session, self.path)
        with self.assertRaises(flight.FlightSessionError) as caught:
            refused.start()
        self.assertEqual("local_api_socket_path_unsafe", caught.exception.code)
        with open(self.path, "r", encoding="utf-8") as source:
            self.assertEqual("do not replace", source.read())
        os.unlink(self.path)
        target = os.path.join(self.temporary.name, "target")
        with open(target, "w", encoding="utf-8") as output:
            output.write("target")
        os.symlink(target, self.path)
        symlink_server = flight.UnixNdjsonServer(session, self.path)
        with self.assertRaises(flight.FlightSessionError) as caught:
            symlink_server.start()
        self.assertEqual("local_api_socket_path_unsafe", caught.exception.code)
        self.assertTrue(os.path.islink(self.path))

    def test_live_or_foreign_owned_socket_is_never_removed(self):
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(self.path)
        live.listen(1)
        session, _packets = self.make_session()
        server = flight.UnixNdjsonServer(session, self.path)
        with self.assertRaises(flight.FlightSessionError) as caught:
            server.start()
        self.assertEqual("local_api_socket_in_use", caught.exception.code)
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.path).st_mode))
        live.close()
        os.unlink(self.path)

        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.path)
        stale.close()
        foreign = flight.UnixNdjsonServer(session, self.path)
        with mock.patch.object(
            flight.os, "geteuid", return_value=os.geteuid() + 1
        ):
            with self.assertRaises(flight.FlightSessionError) as caught:
                foreign.start()
        self.assertEqual("local_api_socket_path_unsafe", caught.exception.code)
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.path).st_mode))

    def test_cleanup_does_not_unlink_a_replaced_path(self):
        session, _packets = self.make_session()
        server = self.start_server(session)
        original_path = self.path + ".original"
        os.rename(self.path, original_path)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(self.path)
        replacement.listen(1)
        server.close()
        self.servers.remove(server)
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.path).st_mode))
        replacement.close()
        os.unlink(self.path)
        os.unlink(original_path)

    def test_single_client_reconnect_retains_session_and_disconnect_neutralizes(self):
        session, packets = self.make_session()
        server = self.start_server(session)
        original_control_session = packets.status()["session"]
        first, first_reader = self.connect()
        ready = self.read_json(first_reader)
        self.assertEqual("repl_ready", ready["event"])
        first.sendall(
            b'{"command":"avoidance","request_id":"cfg","mode":"off"}\n'
        )
        configured = self.read_json(first_reader)
        self.assertEqual("cfg", configured["request_id"])
        first.sendall(
            b'{"command":"velocity","request_id":"v1","forward_mps":1}\n'
        )
        moved = self.read_json(first_reader)
        self.assertEqual("v1", moved["request_id"])
        self.assertEqual("command_result", moved["event"])
        self.assertEqual(1.0, packets.status()["target"]["forward_mps"])

        self.close_client(first, first_reader)
        self.wait_until(lambda: not server.status()["active_client"])
        self.assertEqual(
            flight.VelocityCommand().to_dict(), packets.status()["target"]
        )
        self.assertTrue(packets.status()["armed"])
        self.assertTrue(session._armed)
        self.assertEqual(original_control_session, packets.status()["session"])

        second, second_reader = self.connect()
        self.assertEqual("repl_ready", self.read_json(second_reader)["event"])
        second.sendall(b'{"command":"status","request_id":"s2"}\n')
        status = self.read_json(second_reader)
        self.assertEqual("s2", status["request_id"])
        self.assertEqual(2, status["local_api"]["client_generation"])
        self.assertTrue(status["local_api"]["active_client"])
        self.assertEqual("off", status["avoidance"]["mode"])
        serialized = json.dumps(status)
        self.assertNotIn("secret", serialized)
        self.close_client(second, second_reader)

    def test_second_concurrent_client_is_rejected_without_new_generation(self):
        session, _packets = self.make_session()
        server = self.start_server(session)
        first, first_reader = self.connect()
        self.assertEqual("repl_ready", self.read_json(first_reader)["event"])
        second, second_reader = self.connect()
        busy = self.read_json(second_reader)
        self.assertEqual("server_busy", busy["event"])
        self.assertEqual("local_api_client_busy", busy["error"])
        self.wait_until(
            lambda: server.status()["rejected_busy_clients"] == 1
        )
        self.assertEqual(1, server.status()["client_generation"])
        self.close_client(second, second_reader)
        self.close_client(first, first_reader)

    def test_disconnect_cancels_operation_and_keeps_authority(self):
        session, packets = self.make_session()
        server = self.start_server(session)
        client, reader = self.connect()
        self.assertEqual("repl_ready", self.read_json(reader)["event"])
        client.sendall(
            b'{"command":"move_relative","request_id":"move-1",'
            b'"forward_m":10,"speed_mps":1}\n'
        )
        accepted = self.read_json(reader)
        self.assertEqual("command_accepted", accepted["event"])
        self.assertEqual("move-1", accepted["request_id"])
        self.wait_until(
            lambda: packets.status()["target"]["forward_mps"] > 0.0
        )
        self.close_client(client, reader)
        self.wait_until(lambda: not server.status()["active_client"])
        self.assertEqual(
            flight.VelocityCommand().to_dict(), packets.status()["target"]
        )
        self.assertTrue(session._armed)
        self.assertTrue(packets.status()["armed"])

    def test_operation_preserves_accepted_then_result_request_semantics(self):
        session, _packets = self.make_session()
        self.start_server(session)
        client, reader = self.connect()
        self.assertEqual("repl_ready", self.read_json(reader)["event"])
        client.sendall(
            b'{"command":"move_relative","request_id":"short-move",'
            b'"forward_m":0.01,"speed_mps":1}\n'
        )
        accepted = self.read_json(reader)
        result = self.read_json(reader)
        self.assertEqual("command_accepted", accepted["event"])
        self.assertEqual("short-move", accepted["request_id"])
        self.assertEqual("command_result", result["event"])
        self.assertEqual("short-move", result["request_id"])
        self.assertEqual("move_relative", result["command"])
        self.close_client(client, reader)

    def test_disconnect_pauses_background_route_without_handoff(self):
        session, packets = self.make_session()
        session.route_accept(route_document(
            1, [route_waypoint(latitude=0.001)]
        ))
        session.route_start()
        self.assertGreater(packets.status()["target"]["forward_mps"], 0.0)
        server = self.start_server(session)
        client, reader = self.connect()
        self.assertEqual("repl_ready", self.read_json(reader)["event"])
        self.close_client(client, reader)
        self.wait_until(lambda: not server.status()["active_client"])
        self.assertEqual("paused", session.route_status()["route"]["phase"])
        self.assertEqual(
            flight.VelocityCommand().to_dict(), packets.status()["target"]
        )
        self.assertTrue(session._armed)

    def test_quit_stops_server_but_leaves_handoff_to_process_owner(self):
        session, packets = self.make_session()
        server = self.start_server(session)
        client, reader = self.connect()
        self.assertEqual("repl_ready", self.read_json(reader)["event"])
        client.sendall(b'{"command":"quit","request_id":"quit-1"}\n')
        result = self.read_json(reader)
        self.assertEqual("quit-1", result["request_id"])
        self.assertEqual("closing", result["state"])
        self.wait_until(lambda: not server.status()["accept_loop_alive"])
        self.assertFalse(server.status()["listening"])
        self.assertTrue(session._armed)
        self.assertTrue(packets.status()["armed"])
        self.close_client(client, reader)


class RouteSessionIntegrationTest(unittest.TestCase):
    def make_session(self, status=None, fake_socket=None):
        fake_socket = fake_socket or FakeSocket()
        packets = flight.ControlPacketStream(
            "bridge", 8767, "secret", socket_factory=lambda *_args: fake_socket
        )
        packets.arm(
            flight.ControlContext(0x22, "0000000000000022", 0)
        )
        telemetry = StaticTelemetry(status or navigation_status())
        session = flight.FlightSession(None, telemetry, packets)
        session._armed = True
        return session, telemetry, packets, fake_socket

    def test_immediate_midflight_revision_swaps_target_without_rearming(self):
        session, _telemetry, packets, _socket = self.make_session()
        accepted = session.route_accept(route_document(
            1, [route_waypoint(latitude=0.001)]
        ))
        self.assertTrue(accepted["ok"])
        started = session.route_start()
        self.assertTrue(started["guidance"]["active"])
        self.assertGreater(packets.status()["target"]["forward_mps"], 0.0)
        original_session = packets.status()["session"]

        replaced = session.route_accept(route_document(
            2,
            [route_waypoint(latitude=0.0, longitude=0.001)],
            expected=1,
        ))

        self.assertTrue(replaced["ok"])
        self.assertFalse(replaced["authority_reacquired"])
        self.assertEqual(original_session, packets.status()["session"])
        self.assertGreater(packets.status()["target"]["right_mps"], 0.0)
        self.assertAlmostEqual(0.0, packets.status()["target"]["forward_mps"])
        route = session.route_status()["route"]
        self.assertEqual(2, route["active_revision"])
        self.assertEqual("running", route["phase"])

    def test_boundary_revision_stages_then_activates_at_waypoint(self):
        session, telemetry, packets, _socket = self.make_session()
        initial = [
            route_waypoint(latitude=0.0),
            route_waypoint(latitude=0.001),
            route_waypoint(latitude=0.002),
        ]
        self.assertTrue(session.route_accept(route_document(1, initial))["ok"])
        # First tick advances through the current-position waypoint; the next
        # tick emits guidance toward index 1.
        session.route_start()
        session._route_tick_once(immediate=True)
        self.assertEqual(1, session.route_status()["route"]["target_waypoint_index"])
        self.assertGreater(packets.status()["target"]["forward_mps"], 0.0)

        revised = [
            route_waypoint(latitude=0.0),
            route_waypoint(latitude=0.001),
            route_waypoint(latitude=0.001, longitude=0.001),
        ]
        staged = session.route_accept(route_document(
            2,
            revised,
            expected=1,
            activation="at_waypoint_boundary",
            scope="full_route_continue",
        ))
        self.assertTrue(staged["ok"])
        status = session.route_status()["route"]
        self.assertEqual(1, status["active_revision"])
        self.assertEqual(2, status["pending_revision"])
        self.assertEqual(2, status["pending_target_waypoint_index"])

        telemetry.publish(navigation_status(latitude=0.001))
        boundary = session._route_tick_once(immediate=True)
        self.assertEqual("plan_replaced", boundary["reason"])
        self.assertEqual(flight.VelocityCommand().to_dict(), packets.status()["target"])
        activated = session.route_status()["route"]
        self.assertEqual(2, activated["active_revision"])
        self.assertEqual(2, activated["target_waypoint_index"])
        self.assertIsNone(activated["pending_revision"])

        moving = session._route_tick_once(immediate=True)
        self.assertTrue(moving["active"])
        self.assertGreater(packets.status()["target"]["right_mps"], 0.0)

    def test_manual_velocity_pauses_route_and_cannot_be_overwritten(self):
        session, _telemetry, packets, _socket = self.make_session()
        session.route_accept(route_document(
            1, [route_waypoint(latitude=0.001)]
        ))
        session.route_start()
        manual = session.set_velocity(flight.VelocityCommand(right_mps=0.4))
        self.assertTrue(manual["ok"])
        self.assertEqual("paused", session.route_status()["route"]["phase"])
        self.assertEqual(0.4, packets.status()["target"]["right_mps"])

        # A later route-loop tick sees PAUSED and emits nothing; operator target wins.
        self.assertIsNone(session._route_tick_once(immediate=True))
        self.assertEqual(0.4, packets.status()["target"]["right_mps"])
        self.assertEqual("operator", session.route_status()["runtime"]["ownership"])

    def test_manual_move_and_rotate_initial_send_failures_clear_motion(self):
        operations = {
            "velocity": lambda session: session.set_velocity(
                flight.VelocityCommand(forward_mps=0.5)
            ),
            "move_relative": lambda session: session.move_relative(
                1.0, 0.0, 0.0, 1.0
            ),
            "rotate_relative": lambda session: session.rotate_relative(
                90.0, yaw_rate_deg_s=30.0
            ),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                fake_socket = FailNextSocket()
                session, _telemetry, packets, _socket = self.make_session(
                    fake_socket=fake_socket
                )
                packets.set_velocity(flight.VelocityCommand(right_mps=0.2))
                fake_socket.fail_next = True

                with self.assertRaises(flight.FlightSessionError):
                    operation(session)

                self.assertEqual(
                    flight.VelocityCommand().to_dict(),
                    packets.status()["target"],
                )
                packets.emit_once()
                decoded = struct.unpack(
                    ">4sQQQiiii", fake_socket.sent[-1][0][:-16]
                )
                self.assertEqual((0, 0, 0, 0), decoded[4:])

    def test_route_loop_base_exception_neutralizes_and_stays_alive(self):
        session, _telemetry, packets, _socket = self.make_session()
        session.route_accept(route_document(
            1, [route_waypoint(latitude=0.001)]
        ))
        session.route_start()
        self.assertGreater(packets.status()["target"]["forward_mps"], 0.0)

        def crash_tick(*_args, **_kwargs):
            raise SystemExit("injected route-loop fault")

        session._route_tick_once = crash_tick
        worker = threading.Thread(target=session._route_loop, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 1.0
            while (
                time.monotonic() < deadline
                and session.route_status()["runtime"]["loop_faults"] == 0
            ):
                time.sleep(0.01)
            self.assertGreaterEqual(
                session.route_status()["runtime"]["loop_faults"], 1
            )
            self.assertTrue(worker.is_alive())
            self.assertEqual(
                flight.VelocityCommand().to_dict(), packets.status()["target"]
            )
            self.assertEqual("paused", session.route_status()["route"]["phase"])
        finally:
            session._route_stop.set()
            session._route_wake.set()
            worker.join(timeout=1.0)

    def test_completed_route_can_be_cas_replaced_without_rearming(self):
        session, _telemetry, packets, _socket = self.make_session()
        session.route_accept(route_document(
            1, [route_waypoint(latitude=0.0)]
        ))
        completed = session.route_start()
        self.assertEqual("completed", completed["route"]["phase"])
        original_session = packets.status()["session"]

        replaced = session.route_accept(route_document(
            2, [route_waypoint(latitude=0.001)], expected=1
        ))
        self.assertTrue(replaced["ok"])
        self.assertTrue(replaced["terminal_route_reset"])
        self.assertEqual("ready", replaced["route"]["phase"])
        self.assertEqual(original_session, packets.status()["session"])
        restarted = session.route_start()
        self.assertTrue(restarted["guidance"]["active"])

    def test_terminal_route_replacement_requires_exact_revision_cas(self):
        session, _telemetry, _packets, _socket = self.make_session()
        session.route_accept(route_document(
            1, [route_waypoint(latitude=0.0)]
        ))
        session.route_start()

        rejected = session.route_accept(route_document(
            2, [route_waypoint(latitude=0.001)], expected=None
        ))
        self.assertFalse(rejected["ok"])
        self.assertFalse(rejected["terminal_route_reset"])
        self.assertEqual(1, rejected["route"]["newest_accepted_revision"])
        self.assertEqual("completed", rejected["route"]["phase"])

    def test_stale_queue_neutralizes_running_route_without_false_completion(self):
        stale = navigation_status(queue_age_ms=300.0)
        session, _telemetry, packets, _socket = self.make_session(stale)
        session.route_accept(route_document(
            1, [route_waypoint(latitude=0.001)]
        ))
        started = session.route_start()
        self.assertFalse(started["guidance"]["active"])
        self.assertEqual("route_telemetry_queue_stale", started["guidance"]["reason"])
        self.assertEqual(flight.VelocityCommand().to_dict(), packets.status()["target"])
        self.assertEqual("running", session.route_status()["route"]["phase"])

    def test_route_status_is_truthful_about_non_native_execution(self):
        session, _telemetry, _packets, _socket = self.make_session()
        status = session.route_status()
        capabilities = status["capabilities"]
        self.assertEqual("bridge_virtual_stick", capabilities["route_engine"])
        self.assertEqual("mac_persistent_session", capabilities["execution_owner"])
        self.assertFalse(capabilities["native_waypoint_execution"])
        self.assertFalse(capabilities["fly_library_interop"])
        self.assertFalse(capabilities["android_route_endpoint"])
        self.assertFalse(capabilities["dji_obstacle_avoidance_integration"])
        self.assertTrue(capabilities["host_obstacle_guard_available"])
        self.assertEqual(
            "conservative_global",
            capabilities["host_obstacle_default_horizontal_mapping"],
        )
        self.assertFalse(
            capabilities["host_obstacle_calibrated_directional_mapping"]
        )
        self.assertEqual(
            "unverified",
            capabilities[
                "dji_firmware_obstacle_avoidance_under_virtual_stick"
            ],
        )

    def test_route_accept_preserves_strict_duplicate_key_rejection(self):
        session, _telemetry, _packets, _socket = self.make_session()
        duplicate = route_document(1, [route_waypoint()]).replace(
            '"schema": "veil.route-revision.v1"',
            '"schema": "veil.route-revision.v1", "schema": "duplicate"',
        )
        with self.assertRaises(flight.FlightSessionError) as caught:
            session.route_accept(duplicate)
        self.assertEqual("route_parse_error", caught.exception.code)


class ReplPreemptionTest(unittest.TestCase):
    def test_velocity_waits_for_cancelled_operation_neutral_then_wins(self):
        events = []

        class Session:
            def move_relative(self, *_args, cancel_event, **_kwargs):
                events.append("move-dispatched")
                cancel_event.wait(0.5)
                events.append("old-operation-neutral")
                return {"ok": False, "state": "cancelled_neutral"}

            def set_velocity(self, command, _received):
                events.append(("new-setpoint", command.right_mps))
                return {"ok": True, "state": "setpoint_dispatched"}

            def neutral(self):
                events.append("exception-neutral")

        lines = io.StringIO(
            '{"command":"move_relative","right_m":1,"speed_mps":1}\n'
            '{"command":"velocity","right_mps":0.4}\n'
        )
        output = io.StringIO()
        repl = flight.JsonLinesRepl(Session(), lines, output)
        started = time.monotonic()
        repl.run()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.12)
        self.assertEqual(("new-setpoint", 0.4), events[-1])
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        velocity = [item for item in records if item.get("command") == "velocity"]
        self.assertTrue(velocity[-1]["ok"])

    def test_broken_output_cannot_strand_operation_bookkeeping(self):
        events = []

        class BrokenOutput:
            def write(self, _value):
                raise BrokenPipeError("client disconnected")

            def flush(self):
                pass

        class Session:
            def move_relative(self, *_args, cancel_event, **_kwargs):
                events.append("started")
                cancel_event.wait(1.0)
                events.append("cancelled")
                return {"ok": False, "state": "cancelled_neutral"}

            def neutral(self):
                events.append("neutral")

        repl = flight.JsonLinesRepl(
            Session(),
            io.StringIO(
                '{"command":"move_relative","forward_m":1,"speed_mps":1}\n'
            ),
            BrokenOutput(),
            neutralize_on_disconnect=True,
        )
        repl.run()
        self.assertTrue(repl._output_failed.is_set())
        self.assertIsNone(repl._operation_thread)
        self.assertIsNone(repl._operation_cancel)
        self.assertIsNone(repl._operation_id)
        self.assertIn("cancelled", events)
        self.assertEqual("neutral", events[-1])


if __name__ == "__main__":
    unittest.main()
