# DJI Mini 4 Pro live-input bridge

The Android project in `android/dji-mini4-bridge/` adds a DJI Mini 4 Pro source
to VEIL's live-input architecture:

```text
Mini 4 Pro <--O4 radio--> RC-N2 <--USB accessory--> BOOX Android
                                                     |
                                                     | Wi-Fi
                                                     | 8765 authenticated HTTP/status
                                                     | 8766 raw Annex-B HEVC
                                                     | 8767 authenticated V2 UDP control
                                                     | 8768 latest-only 20 Hz NDJSON telemetry
                                                     v
                                       persistent Mac flight session
                                         + native video viewer
                                         + VEIL/operator policy
```

The BOOX is required because DJI Mobile SDK V5 owns the RC-N2 USB accessory
protocol and aircraft link. The Raspberry Pi 3B+, Arduino Mega, and iCE40 FPGA
are not part of this path.

The Android APK is deliberately a thin transport and telemetry layer. The Mac
process `tools/veil_dji_flight.py` keeps one JSON-lines session open, sends a new
manual setpoint immediately, refreshes it at 20 Hz, continuously drains the
highest-sequence telemetry frame, and exposes matched Android control
acknowledgements. An acknowledgement proves that the authenticated packet and
echoed setpoint reached the Android latest-setpoint mailbox; fresh aircraft
telemetry is still required to establish physical response.

Routes are also Mac-owned. The strict `veil.route-revision.v1` engine accepts
complete compare-and-set revisions atomically while Virtual Stick authority
remains active, with immediate or next-waypoint-boundary activation. Manual
velocity, neutral, relative-motion, handoff, or landing commands pause route
ownership before dispatch. This is not DJI Fly interoperability or an onboard
mission: MSDK 5.18 declares `virtualStick` for Mini 4 Pro but its product
capability manifest has no `waypointMission`, and DJI exposes no supported DJI
Fly mission-library import/export or in-place edit API. The route depends on
the Mac/BOOX/RC link, normal GNSS-scale positioning, and DJI's raw reported
altitude; it is not an RTK or exact one-foot positioning system.

Video remains encoded on the BOOX. The preferred Apple Silicon viewer is
`tools/veil_dji_video_native`, which uses VideoToolbox and a one-frame decoded
display mailbox; `python3 tools/veil_dji.py video` is the portable `ffplay`
fallback. Neither path promises zero latency. The bridge joins viewers at a
fresh parameter-set/IDR boundary instead of dropping dependency-bearing HEVC
pictures in the middle of a GOP.

Telemetry is generated at 20 Hz with a capacity-one pending mailbox per client;
missed snapshots are replaced, not replayed. Sequence, generation/write time,
per-client queue age, and sequence-gap fields make delay and dropped updates
observable. TCP can still buffer, so a flight client must continuously drain it
and reject stale or out-of-order state.

DJI does not document Mini 4 Pro as retaining automatic obstacle avoidance
while Virtual Stick owns control. Perception data must not be represented as a
guaranteed brake or bypass capability.

Initial validation order:

1. DJI SDK registration succeeds.
2. RC-N2 and aircraft report connected.
3. Telemetry updates while motors remain off.
4. The raw encoded stream is decoded in the native VideoToolbox Mac window.
5. A grounded neutral packet receives a matching session/sequence/setpoint
   acknowledgement and telemetry remains fresh.
6. RC handoff and Android watchdog behavior are confirmed.
7. Only after an operator safety confirmation may a takeoff/landing test run.
8. Route start and atomic revision are tested only after the manual control path
   is proven.

The first flight test must not be hard-coded or launched at application start.
DJI's normal takeoff behavior may climb above one foot; use the aircraft's safe
takeoff behavior rather than forcing a 0.3 m ground-effect hover.
