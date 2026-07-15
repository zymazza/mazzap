#!/usr/bin/env python3
"""Mac CLI for the VEIL DJI Android bridge."""

import argparse
import collections
import hashlib
import hmac
import json
import os
import queue
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request


VIDEO_READ_SIZE = 16 * 1024
VIDEO_SYNC_TIMEOUT_SECONDS = 6.0
VIDEO_SYNC_MAX_BYTES = 4 * 1024 * 1024
VIDEO_SOCKET_BUFFER_BYTES = 64 * 1024
VIDEO_RATE_SAMPLE_SECONDS = 1.6
VIDEO_RATE_WAIT_SECONDS = 8.0
VIDEO_RATE_SAMPLE_INTERVAL_SECONDS = 0.35
VIDEO_RATE_MAX_AGE_MS = 500.0
DECODER_ERROR_LIMIT = 3
DECODER_ERROR_WINDOW_SECONDS = 2.0


class VideoResync(RuntimeError):
    """The current decoder session must be discarded at the next clean GOP."""


class HevcBootstrapDetector:
    """Incrementally verifies VPS/SPS/PPS followed by a complete first-slice IDR."""

    def __init__(self):
        self.pending = b""
        self.parameter_sets = set()
        self.nal_types = []
        self.ready = False

    def feed(self, data):
        combined = self.pending + data
        starts = _annexb_start_codes(combined)
        if not starts:
            self.pending = combined[-3:]
            return self.ready

        for start, end in zip(starts, starts[1:]):
            parsed = _parse_hevc_nal(combined, start, end)
            if parsed is None:
                continue
            nal_type, first_slice = parsed
            self.nal_types.append(nal_type)
            if nal_type in (32, 33, 34):
                self.parameter_sets.add(nal_type)
            elif nal_type in (19, 20) and first_slice:
                if self.parameter_sets.issuperset((32, 33, 34)):
                    self.ready = True

        self.pending = combined[starts[-1]:]
        return self.ready


def _annexb_start_codes(data):
    starts = []
    index = 0
    while index + 2 < len(data):
        if data[index:index + 4] == b"\x00\x00\x00\x01":
            starts.append(index)
            index += 4
        elif data[index:index + 3] == b"\x00\x00\x01":
            starts.append(index)
            index += 3
        else:
            index += 1
    return starts


def _parse_hevc_nal(data, start, end):
    if data[start:start + 3] == b"\x00\x00\x01":
        header = start + 3
    elif data[start:start + 4] == b"\x00\x00\x00\x01":
        header = start + 4
    else:
        return None
    if header + 1 >= end:
        return None
    nal_type = (data[header] >> 1) & 0x3f
    first_slice = nal_type <= 31 and header + 2 < end and bool(data[header + 2] & 0x80)
    return nal_type, first_slice


def _decoder_sync_error(line):
    lowered = line.lower()
    return any(pattern in lowered for pattern in (
        "could not find ref with poc",
        "pps id out of range",
        "sps id out of range",
        "vps id out of range",
        "invalid nal unit",
    ))


class _SessionState:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.reason = None
        self.stderr_tail = collections.deque(maxlen=8)

    def fail(self, reason):
        with self.lock:
            if self.reason is None:
                self.reason = reason
        self.stop.set()

    def record_stderr(self, line):
        with self.lock:
            self.stderr_tail.append(line)

    def failure_text(self, fallback):
        with self.lock:
            if self.reason:
                return self.reason
            if self.stderr_tail:
                return f"{fallback}: {self.stderr_tail[-1]}"
        return fallback


def request(args, method, path):
    req = urllib.request.Request(
        f"http://{args.host}:{args.control_port}{path}",
        method=method,
        headers={"X-Veil-Token": args.token},
    )
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


def _connect_video_stream(args):
    try:
        stream = socket.create_connection((args.host, args.video_port), timeout=3)
    except OSError as error:
        raise VideoResync(f"video connection failed: {error}") from error
    stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    stream.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, VIDEO_SOCKET_BUFFER_BYTES)
    stream.sendall(f"TOKEN {args.token}\n".encode())
    stream.settimeout(0.25)
    return stream


def _read_hevc_bootstrap(stream):
    detector = HevcBootstrapDetector()
    received = bytearray()
    deadline = time.monotonic() + VIDEO_SYNC_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            block = stream.recv(VIDEO_READ_SIZE)
        except socket.timeout:
            continue
        if not block:
            raise VideoResync("video socket closed while waiting for a fresh IDR")
        received.extend(block)
        if len(received) > VIDEO_SYNC_MAX_BYTES:
            raise VideoResync("HEVC bootstrap exceeded 4 MiB without a complete IDR")
        if detector.feed(block):
            return bytes(received)
    types = ",".join(str(value) for value in detector.nal_types[-12:]) or "none"
    raise VideoResync(f"timed out waiting for VPS/SPS/PPS + IDR (recent NALs: {types})")


def _ffplay_command(args, ffmpeg_format):
    fps = f"{args.fps:g}"
    return [
        args.ffplay,
        "-hide_banner", "-loglevel", "error",
        "-window_title", "VEIL DJI Mini 4 Pro",
        # Do not use ffmpeg's `nobuffer` input flag here. For a raw HEVC pipe it
        # can discard the freshly requested IDR while probing, so decoding
        # begins on dependent pictures. The client has already verified the
        # forced raw-HEVC format and a VPS/SPS/PPS + IDR bootstrap, so ffplay
        # does not need to retain a probe window before displaying it.
        "-nofind_stream_info",
        "-noinfbuf",
        "-framerate", fps,
        # Raw HEVC packets carry durations but no PTS/DTS. Synthesize a clock
        # after reference-safe decoding instead of timestamping bursty pipe
        # reads. Late display frames may then be dropped without discarding any
        # encoded reference packets.
        "-vf", f"setpts=N/({fps}*TB)",
        "-sync", "video",
        "-framedrop",
        "-f", ffmpeg_format, "-i", "pipe:0",
    ]


def _snap_video_fps(measured):
    """Snap a measured access-unit rate to a nearby common nominal rate."""
    nominal_rates = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)
    nearest = min(nominal_rates, key=lambda value: abs(value - measured))
    if abs(nearest - measured) <= max(0.35, measured * 0.025):
        return nearest
    return round(measured, 3)


def _resolve_video_fps(args, initial_status):
    if args.fps is not None:
        return args.fps

    samples = []
    status = initial_status
    wait_deadline = time.monotonic() + VIDEO_RATE_WAIT_SECONDS
    sample_deadline = None
    while True:
        now = time.monotonic()
        rate = status.get("video_access_unit_rate_hz")
        age_ms = status.get("video_access_unit_age_ms")
        if (
            isinstance(rate, (int, float)) and 5.0 <= rate <= 120.0 and
            isinstance(age_ms, (int, float)) and 0.0 <= age_ms <= VIDEO_RATE_MAX_AGE_MS
        ):
            samples.append(float(rate))
            if sample_deadline is None:
                sample_deadline = now + VIDEO_RATE_SAMPLE_SECONDS
        if sample_deadline is not None and now >= sample_deadline:
            break
        if sample_deadline is None and now >= wait_deadline:
            break
        time.sleep(VIDEO_RATE_SAMPLE_INTERVAL_SECONDS)
        try:
            status = request(args, "GET", "/status")
        except OSError:
            continue

    if not samples:
        raise SystemExit(
            "fresh video cadence was unavailable after "
            f"{VIDEO_RATE_WAIT_SECONDS:g}s; wait for raw ingress or pass --fps explicitly"
        )

    measured = statistics.median(samples)
    selected = _snap_video_fps(measured)
    print(
        f"video input rate: measured {measured:.3f} fps; decoder set to {selected:g} fps",
        file=sys.stderr,
        flush=True,
    )
    return selected


def _write_all(pipe, block):
    view = memoryview(block)
    while view:
        written = pipe.write(view)
        if not written:
            raise BrokenPipeError("ffplay stdin closed")
        view = view[written:]


def _ffplay_writer(pipe, blocks, state):
    try:
        while not state.stop.is_set():
            try:
                block = blocks.get(timeout=0.1)
            except queue.Empty:
                continue
            if block is None:
                return
            _write_all(pipe, block)
    except (OSError, BrokenPipeError) as error:
        if not state.stop.is_set():
            state.fail(f"ffplay input failed: {error}")


def _ffplay_stderr_reader(pipe, state):
    decode_errors = collections.deque()
    try:
        for raw_line in iter(pipe.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            state.record_stderr(line)
            if not _decoder_sync_error(line):
                continue
            now = time.monotonic()
            decode_errors.append(now)
            while decode_errors and now - decode_errors[0] > DECODER_ERROR_WINDOW_SECONDS:
                decode_errors.popleft()
            if len(decode_errors) >= DECODER_ERROR_LIMIT:
                state.fail("ffplay lost HEVC reference synchronization")
                return
    except OSError:
        return


def _enqueue_startup(blocks, bootstrap):
    for offset in range(0, len(bootstrap), VIDEO_READ_SIZE):
        try:
            blocks.put(bootstrap[offset:offset + VIDEO_READ_SIZE], timeout=0.25)
        except queue.Full as error:
            raise VideoResync("ffplay could not consume the initial IDR") from error


def _enqueue_live(blocks, block, max_backlog_kib):
    try:
        blocks.put_nowait(block)
    except queue.Full as error:
        raise VideoResync(
            f"video backlog exceeded {max_backlog_kib} KiB"
        ) from error


def _stop_ffplay(ffplay, writer, stderr_reader, state, blocks):
    state.stop.set()
    try:
        blocks.put_nowait(None)
    except queue.Full:
        pass
    if ffplay.poll() is None:
        ffplay.terminate()
        try:
            ffplay.wait(timeout=1)
        except subprocess.TimeoutExpired:
            ffplay.kill()
            ffplay.wait()
    if writer is not None:
        writer.join(timeout=1)
    if stderr_reader is not None:
        stderr_reader.join(timeout=1)
    if ffplay.stdin:
        try:
            ffplay.stdin.close()
        except OSError:
            pass
    if ffplay.stderr:
        ffplay.stderr.close()


def _play_video_session(args, ffmpeg_format):
    stream = _connect_video_stream(args)
    ffplay = None
    writer = None
    stderr_reader = None
    state = _SessionState()
    queue_chunks = max(4, (args.max_backlog_kib * 1024) // VIDEO_READ_SIZE)
    blocks = queue.Queue(maxsize=queue_chunks)

    try:
        # The bridge authenticates, requests a fresh I-frame, and admits this
        # socket at that type-19/20 IDR. Verify the bootstrap before a decoder is
        # even created so ffplay can never begin on predictive frames.
        bootstrap = _read_hevc_bootstrap(stream)
        ffplay = subprocess.Popen(
            _ffplay_command(args, ffmpeg_format),
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        writer = threading.Thread(
            target=_ffplay_writer,
            args=(ffplay.stdin, blocks, state),
            name="veil-dji-ffplay-writer",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=_ffplay_stderr_reader,
            args=(ffplay.stderr, state),
            name="veil-dji-ffplay-stderr",
            daemon=True,
        )
        writer.start()
        stderr_reader.start()
        _enqueue_startup(blocks, bootstrap)

        while True:
            if state.stop.is_set():
                raise VideoResync(state.failure_text("ffplay requested resynchronization"))
            return_code = ffplay.poll()
            if return_code is not None:
                if return_code == 0:
                    return False
                raise VideoResync(state.failure_text(f"ffplay exited with status {return_code}"))
            try:
                block = stream.recv(VIDEO_READ_SIZE)
            except socket.timeout:
                continue
            if not block:
                raise VideoResync("video socket closed")
            # Never discard an arbitrary HEVC reference frame. Tear down the
            # decoder and reconnect; authentication requests a new IDR.
            _enqueue_live(blocks, block, args.max_backlog_kib)
    finally:
        stream.close()
        if ffplay is not None:
            _stop_ffplay(ffplay, writer, stderr_reader, state, blocks)


def play_video(args):
    deadline = time.monotonic() + 15
    while True:
        try:
            status = request(args, "GET", "/status")
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)
    codec = status.get("video_codec", "unknown").lower()
    if "265" in codec or "hevc" in codec:
        ffmpeg_format = "hevc"
    elif "264" in codec or "avc" in codec:
        ffmpeg_format = "h264"
    else:
        raise SystemExit(f"DJI stream codec is not known yet: {codec!r}")
    if ffmpeg_format != "hevc":
        raise SystemExit("bounded video synchronization currently requires the Mini 4 Pro HEVC stream")
    args.fps = _resolve_video_fps(args, status)
    if args.fps <= 0:
        raise SystemExit("--fps must be positive")
    if args.max_backlog_kib < 64:
        raise SystemExit("--max-backlog-kib must be at least 64")

    while True:
        try:
            if _play_video_session(args, ffmpeg_format) is False:
                return
        except VideoResync as error:
            print(f"video resync: {error}", file=sys.stderr, flush=True)
            time.sleep(0.15)


def stream_telemetry(args):
    with socket.create_connection((args.host, args.telemetry_port), timeout=3) as stream:
        stream.sendall(f"TOKEN {args.token}\n".encode())
        for line in stream.makefile("r"):
            print(json.dumps(json.loads(line), indent=2), flush=True)


def control_context(args):
    before_ms = time.monotonic_ns() // 1_000_000
    status = request(args, "GET", "/status")
    after_ms = time.monotonic_ns() // 1_000_000
    if status.get("control_packet_version") != 2:
        raise RuntimeError("bridge does not advertise control packet V2")
    session = int(status["control_session"], 16)
    server_ms = int(status["control_monotonic_ms"])
    # /status is generated between these two samples; midpoint removes most RTT.
    clock_offset_ms = server_ms - ((before_ms + after_ms) // 2)
    return session, clock_offset_ms


def send_control_packet(args, payload):
    tag = hmac.new(args.token.encode(), payload, hashlib.sha256).digest()[:16]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.sendto(payload + tag, (args.host, args.realtime_control_port))


def send_sticks(args):
    session, clock_offset_ms = control_context(args)
    sequence = time.monotonic_ns() & 0xFFFFFFFFFFFFFFFF
    sent_at_ms = time.monotonic_ns() // 1_000_000 + clock_offset_ms
    payload = struct.pack(
        ">4sQQQhhhh", b"VST2", session, sequence, sent_at_ms,
        args.lh, args.lv, args.rh, args.rv,
    )
    send_control_packet(args, payload)


def send_velocity(args):
    session, clock_offset_ms = control_context(args)
    sequence = time.monotonic_ns() & 0xFFFFFFFFFFFFFFFF
    sent_at_ms = time.monotonic_ns() // 1_000_000 + clock_offset_ms
    values = (
        round(args.forward_mps * 1000),
        round(args.right_mps * 1000),
        round(args.up_mps * 1000),
        round(args.yaw_rate_deg_s * 1000),
    )
    limits = (23_000, 23_000, 6_000, 100_000)
    if any(abs(value) > limit for value, limit in zip(values, limits)):
        raise ValueError("velocity exceeds DJI virtual-stick range")
    payload = struct.pack(
        ">4sQQQiiii", b"VDC2", session, sequence, sent_at_ms, *values
    )
    send_control_packet(args, payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("VEIL_DJI_HOST", "127.0.0.1"))
    parser.add_argument("--token", default=os.getenv("VEIL_DJI_TOKEN"), required=False)
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument("--video-port", type=int, default=8766)
    parser.add_argument("--realtime-control-port", type=int, default=8767)
    parser.add_argument("--telemetry-port", type=int, default=8768)
    parser.add_argument("--ffplay", default="ffplay")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    video = sub.add_parser("video")
    video.add_argument(
        "--fps", type=float, default=None,
        help="raw HEVC input rate override (default: measure access-unit rate from bridge)",
    )
    video.add_argument(
        "--max-backlog-kib", type=int, default=256,
        help="restart at a fresh IDR instead of buffering beyond this limit",
    )
    sub.add_parser("telemetry")
    sub.add_parser("enable")
    sub.add_parser("enable-sticks")
    sub.add_parser("disable")
    sub.add_parser("land")
    failsafe = sub.add_parser("set-failsafe")
    failsafe.add_argument("--action", choices=("HOVER", "LANDING", "GOHOME"), required=True)
    failsafe.add_argument("--confirm", action="store_true", required=True)
    takeoff = sub.add_parser("takeoff")
    takeoff.add_argument("--confirm", action="store_true", required=True)
    sticks = sub.add_parser("sticks")
    for name in ("lh", "lv", "rh", "rv"):
        sticks.add_argument(f"--{name}", type=int, required=True)
    velocity = sub.add_parser("velocity")
    velocity.add_argument("--forward-mps", type=float, required=True)
    velocity.add_argument("--right-mps", type=float, required=True)
    velocity.add_argument("--up-mps", type=float, required=True)
    velocity.add_argument("--yaw-rate-deg-s", type=float, required=True)
    args = parser.parse_args()
    if not args.token:
        parser.error("set VEIL_DJI_TOKEN or pass --token")

    if args.command == "status":
        result = request(args, "GET", "/status")
    elif args.command == "video":
        play_video(args)
        return
    elif args.command == "telemetry":
        stream_telemetry(args)
        return
    elif args.command == "enable":
        result = request(args, "POST", "/virtual-stick/enable?mode=body_velocity")
    elif args.command == "enable-sticks":
        result = request(args, "POST", "/virtual-stick/enable?mode=sticks")
    elif args.command == "disable":
        result = request(args, "POST", "/virtual-stick/disable")
    elif args.command == "land":
        result = request(args, "POST", "/land")
    elif args.command == "set-failsafe":
        result = request(
            args,
            "POST",
            "/failsafe-action?" + urllib.parse.urlencode({
                "action": args.action,
                "confirm": "SET_FAILSAFE_ACTION",
            }),
        )
    elif args.command == "takeoff":
        result = request(args, "POST", "/takeoff?confirm=TAKEOFF")
    elif args.command == "sticks":
        send_sticks(args)
        result = {"sent": True, "transport": "udp", "packet_version": 2}
    else:
        send_velocity(args)
        result = {
            "sent": True,
            "transport": "udp",
            "packet_version": 2,
            "coordinate_system": "BODY",
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
