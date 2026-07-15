#!/usr/bin/env python3

import io
import queue
import types
import unittest
from unittest import mock

import veil_dji


def nal(nal_type, payload=b"\x01", four_byte_start=True):
    start = b"\x00\x00\x00\x01" if four_byte_start else b"\x00\x00\x01"
    return start + bytes((nal_type << 1, 1)) + payload


class HevcBootstrapDetectorTest(unittest.TestCase):
    def test_measured_video_rate_snaps_to_common_nominal_rate(self):
        self.assertEqual(30.0, veil_dji._snap_video_fps(30.18))
        self.assertEqual(29.97, veil_dji._snap_video_fps(29.8))
        self.assertEqual(25.0, veil_dji._snap_video_fps(24.9))
        self.assertEqual(27.0, veil_dji._snap_video_fps(27.0))

    def test_missing_fresh_video_rate_never_guesses_25_fps(self):
        args = types.SimpleNamespace(fps=None)
        stale_status = {
            "video_access_unit_rate_hz": 30.0,
            "video_access_unit_age_ms": 5_000.0,
        }
        monotonic_values = iter((0.0, veil_dji.VIDEO_RATE_WAIT_SECONDS))
        with (
            mock.patch.object(veil_dji.time, "monotonic", side_effect=monotonic_values),
            mock.patch.object(veil_dji.time, "sleep"),
            mock.patch.object(veil_dji, "request", return_value=stale_status),
        ):
            with self.assertRaisesRegex(SystemExit, "pass --fps explicitly"):
                veil_dji._resolve_video_fps(args, stale_status)

    def test_fresh_video_rate_is_sampled_then_snapped(self):
        args = types.SimpleNamespace(fps=None)
        statuses = [
            {"video_access_unit_rate_hz": 30.2, "video_access_unit_age_ms": 5.0},
            {"video_access_unit_rate_hz": 30.0, "video_access_unit_age_ms": 5.0},
            {"video_access_unit_rate_hz": 29.9, "video_access_unit_age_ms": 5.0},
        ]
        with (
            mock.patch.object(
                veil_dji.time,
                "monotonic",
                side_effect=(0.0, 0.0, 0.8, veil_dji.VIDEO_RATE_SAMPLE_SECONDS),
            ),
            mock.patch.object(veil_dji.time, "sleep"),
            mock.patch.object(veil_dji, "request", side_effect=statuses[1:]),
            mock.patch.object(veil_dji.sys, "stderr", new=io.StringIO()),
        ):
            self.assertEqual(30.0, veil_dji._resolve_video_fps(args, statuses[0]))

    def test_bytewise_fragments_require_complete_parameter_sets_and_idr(self):
        stream = b"".join((
            nal(32, b"v", True),
            nal(33, b"s", False),
            nal(34, b"p", True),
            nal(20, b"\x80idr", False),
            nal(35, b"aud", True),
        ))
        detector = veil_dji.HevcBootstrapDetector()
        ready_at = None
        for index, value in enumerate(stream):
            if detector.feed(bytes((value,))):
                ready_at = index
                break

        self.assertIsNotNone(ready_at)
        self.assertEqual([32, 33, 34, 20], detector.nal_types)
        self.assertTrue(detector.ready)

    def test_irap_continuation_slice_is_not_a_safe_join(self):
        detector = veil_dji.HevcBootstrapDetector()
        stream = b"".join((
            nal(32), nal(33), nal(34),
            nal(19, b"\x00continuation"),
            nal(1, b"\x80next"),
            nal(35),
        ))
        self.assertFalse(detector.feed(stream))

    def test_missing_pps_never_becomes_ready(self):
        detector = veil_dji.HevcBootstrapDetector()
        stream = nal(32) + nal(33) + nal(20, b"\x80idr") + nal(35)
        self.assertFalse(detector.feed(stream))

    def test_mid_gop_input_waits_for_new_parameter_sets_and_idr(self):
        detector = veil_dji.HevcBootstrapDetector()
        predictive_prefix = nal(1, b"\x80p1") + nal(1, b"\x80p2")
        self.assertFalse(detector.feed(predictive_prefix))
        fresh_gop = nal(32) + nal(33) + nal(34) + nal(20, b"\x80idr") + nal(35)
        self.assertTrue(detector.feed(fresh_gop))

    def test_ffplay_command_preserves_startup_idr_and_bounds_latency(self):
        args = types.SimpleNamespace(ffplay="ffplay", fps=25.0)
        command = veil_dji._ffplay_command(args, "hevc")
        self.assertNotIn("low_delay", command)
        self.assertNotIn("nobuffer", command)
        self.assertNotIn("-use_wallclock_as_timestamps", command)
        self.assertNotIn("-probesize", command)
        self.assertNotIn("-analyzeduration", command)
        self.assertIn("-nofind_stream_info", command)
        self.assertIn("-noinfbuf", command)
        self.assertIn("-framedrop", command)
        self.assertEqual("25", command[command.index("-framerate") + 1])
        self.assertEqual("setpts=N/(25*TB)", command[command.index("-vf") + 1])
        self.assertEqual("video", command[command.index("-sync") + 1])

    def test_decoder_reference_errors_are_resync_signals(self):
        self.assertTrue(veil_dji._decoder_sync_error("Could not find ref with POC 14"))
        self.assertTrue(veil_dji._decoder_sync_error("PPS id out of range: 0"))
        self.assertFalse(veil_dji._decoder_sync_error("some unrelated warning"))

    def test_repeated_decoder_errors_request_session_resync(self):
        state = veil_dji._SessionState()
        stderr = io.BytesIO(b"Could not find ref with POC 1\n" * 3)
        veil_dji._ffplay_stderr_reader(stderr, state)
        self.assertTrue(state.stop.is_set())
        self.assertIn("reference synchronization", state.reason)

    def test_full_video_queue_never_discards_an_arbitrary_frame(self):
        blocks = queue.Queue(maxsize=1)
        blocks.put_nowait(b"older-reference-data")
        with self.assertRaises(veil_dji.VideoResync):
            veil_dji._enqueue_live(blocks, b"newer-frame", 16)
        self.assertEqual(b"older-reference-data", blocks.get_nowait())

    def test_decoder_resync_discards_session_and_reconnects(self):
        args = types.SimpleNamespace(fps=25.0, max_backlog_kib=256)
        with (
            mock.patch.object(
                veil_dji,
                "request",
                return_value={"video_codec": "H265"},
            ),
            mock.patch.object(
                veil_dji,
                "_play_video_session",
                side_effect=(veil_dji.VideoResync("lost reference"), False),
            ) as session,
            mock.patch.object(veil_dji.time, "sleep"),
            mock.patch.object(veil_dji.sys, "stderr", new=io.StringIO()),
        ):
            veil_dji.play_video(args)

        self.assertEqual(2, session.call_count)


if __name__ == "__main__":
    unittest.main()
