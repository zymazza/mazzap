# Native low-latency Mac viewer

`veil-dji-video` is the preferred interactive viewer for the bridge's
authenticated Annex-B HEVC stream and 20 Hz telemetry. One Mac window presents
the unobscured main-camera image beside flight state, attitude, the complete
raw 360-degree obstacle vector, explicit up/down ranges, battery/link/control
metrics, and a scrollable view of every telemetry leaf. The Mini 4 MSDK profile exposes
only the main payload-camera stream; the obstacle visualization is ranging
data, not hidden obstacle-camera imagery.

The viewer begins with Apple's hardware VideoToolbox decoder and renders
decoded pixel buffers directly through Core Image and Metal. If VideoToolbox
reports its hardware-decoder-malfunction status on this DJI stream, a
per-window circuit breaker reconnects at a fresh IDR and permanently uses
VideoToolbox's forced-software path in the same process and window. There is no
container demuxer, probe window, transcoding step, audio clock, generic-player
presentation queue, or ffplay window relaunch.

The dashboard never invents a body-frame direction for DJI's undocumented
horizontal obstacle-vector origin/order. It displays the complete raw indexed
polar vector as **uncalibrated**, filters DJI's invalid range sentinels, and
blanks stale observations. Up/down values are shown only from their exact,
unit-qualified telemetry fields. Video and telemetry age overlays make a
reconnecting or frozen feed unmistakable instead of leaving old data looking
live.

The viewer keeps at most one not-yet-rendered **decoded** frame. Replacing that
frame is reference-safe because VideoToolbox has already consumed the encoded
dependencies. Encoded pictures are never dropped mid-GOP. A six-frame decoder
submission bound instead tears down the session and reconnects through the
bridge's authenticated fresh-IDR gate if hardware decoding ever falls behind.

HEVC streams with B pictures cannot honestly have zero latency. The viewer
parses `sps_max_num_reorder_pics`: a zero-reorder DJI downlink takes the direct
path, while a stream that declares reordering enables VideoToolbox temporal
processing so lower delay never corrupts display order. An unrecognized SPS
takes the correctness-preserving temporal path.

## Build and run

From `android/dji-mini4-bridge`, with `VEIL_DJI_TOKEN` already loaded without
printing it:

```bash
tools/veil_dji_video_native
```

The launcher performs an incremental release build and then opens the window.
Build it once before powering the aircraft when startup time matters:

```bash
swift build -c release --package-path tools/macos-video
```

Optional connection overrides are `--host`, `--port`, `--telemetry-port`, and
`--token`; prefer the `VEIL_DJI_TOKEN` environment variable over a command-line
token because process arguments can be visible to other local processes. Both
video and telemetry sockets reconnect in place after a bounded idle interval.

## Verified behavior

- Swift parser/access-unit/SPS tests cover arbitrary TCP fragmentation, mixed
  Annex-B start codes, continuation slices, parameter-set completeness, and
  both valid and truncated SPS input. Telemetry tests cover exact schema/unit
  extraction, DJI invalid sentinels, source freshness, rejection of incomplete
  circles, and preservation of DJI's full 360-degree millimeter vector without
  claiming an uncalibrated body mapping. Decoder tests also cover synchronous
  hardware failure, retired-session callbacks, and bounded access-unit memory.
- The release viewer decoded and rendered the existing 640×360 synthetic HEVC
  fixture with Apple hardware acceleration. Its SPS declares two reordered
  pictures. A 24 fps access-unit-paced loop showed source frame 71 sent while
  frame 68 was visible: about three frames / 125 ms from mock TCP ingress to
  the captured display. Decoder callback p50/p95 was about 43/48 ms in that run.
- The current `ffplay` command was run against the same access units, cadence,
  machine, and screenshot method. It showed frame 61 when frame 71 had been
  sent: about ten frames / 417 ms. This makes the native path roughly seven
  frames / 292 ms fresher on this deliberately reorder-heavy fixture. These are
  frame-counter observations, not sub-frame photodiode measurements.
- That benchmark deliberately includes two frames of codec reordering and one
  Annex-B access-unit boundary. It excludes the drone's camera/encoder,
  airlink, BOOX callback, Wi-Fi, and monitor scanout, so it is not a claim of
  125 ms camera-to-eye latency. The live window reports decoder latency and
  decoded-frame replacements so regressions are visible.

`tools/veil_dji.py video` remains the portable `ffplay` fallback. The native
viewer is preferred on Apple Silicon for piloting feedback because its display
queue is explicitly bounded and observable.
