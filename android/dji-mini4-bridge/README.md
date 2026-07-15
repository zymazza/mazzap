# VEIL DJI Mini 4 Pro bridge

Android bridge for a DJI Mini 4 Pro and RC-N2. The BOOX Note Air5 C runs this
app, connects to the RC-N2 over Android Open Accessory USB, and exposes status,
authenticated control, and DJI's raw encoded video on the local network. Video
is relayed byte-for-byte without decoding or re-encoding on the BOOX. The app
does not attach a local video surface; its screen is status/debug information.

## Safety state

- The app never starts motors or takes off automatically.
- Every control request requires a random token stored in the app's private data.
- Takeoff additionally requires the literal `confirm=TAKEOFF` parameter.
- Virtual-stick packets are range checked and neutralized after 300 ms without
  a fresh packet.
- The network transports are authenticated but not encrypted; use them only on
  trusted private Wi-Fi or through an encrypted tunnel.
- Keep the physical RC-N2 ready to take back control.

## Configure and build

Create `local.properties` (ignored by Git):

```properties
sdk.dir=/absolute/path/to/Android/sdk
dji.apiKey=PASTE_DJI_APP_KEY_HERE
```

The DJI application must be registered for this exact Android package:
`com.dji.sampleV5.aircraft`.

MSDK 5.18's supported-firmware table lists the exact tested combination:
Mini 4 Pro `01.00.1100` and RC-N2 `01.01.0300`.

```bash
# Configure JAVA_HOME for a JDK 17 installation before building.
./gradlew :app:assembleDebug
export BOOX_HOST="<BOOX_LAN_IP>"
export BOOX_ADB="$BOOX_HOST:5555"
export VEIL_DJI_HOST="$BOOX_HOST"
export VEIL_DJI_ADB_DEVICE="$BOOX_ADB"
export VEIL_DJI_BRIDGE_URL="http://$BOOX_HOST:8765"
adb -s "$BOOX_ADB" install -r app/build/outputs/apk/debug/app-debug.apk
adb -s "$BOOX_ADB" shell am start -n com.dji.sampleV5.aircraft/com.veil.dji.MainActivity
```

## Automatic USB accessory recovery

Run the Mac-side watchdog alongside the viewer/control process to recover the
BOOX-to-RC-N2 accessory latch automatically. It polls the authenticated status
endpoint and uses the proven Android USB re-enumeration command only after a
sustained stuck state. The token is read only from `VEIL_DJI_TOKEN` and is never
included in its NDJSON logs:

```bash
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
export ADB="$ANDROID_SDK_ROOT/platform-tools/adb"
python3 tools/veil_dji_connection_watchdog.py
```

On each watchdog start, a sustained `sdk_registered=true`,
`product_connected=true`, `product_type=UNRECOGNIZED`, and
`aircraft_connected=false` state permits one bootstrap recovery. After a real
aircraft connection has been observed, an unexpected disconnect permits one
more recovery for that disconnect episode. It does not reset repeatedly while
the aircraft remains intentionally off; a real aircraft reconnect is required
to re-arm drop recovery. A failed ADB reachability check performs no USB action
and may retry only after the cooldown. Once the reset subprocess has launched,
that episode is consumed even if ADB returns nonzero or times out: intentionally
tearing down USB can interrupt ADB's result path after Android has already
performed the requested re-enumeration.

Defaults are an 8-second grace period, a 30-second cooldown, and localhost.
Set `VEIL_DJI_HOST` and `VEIL_DJI_ADB_DEVICE` for a direct BOOX LAN connection;
`--dry-run` exercises the detection/latching policy without invoking ADB:

```bash
python3 tools/veil_dji_connection_watchdog.py \
  --host "$BOOX_HOST" \
  --device "$BOOX_ADB" \
  --poll-seconds 1 --grace-seconds 8 --cooldown-seconds 30 \
  --dry-run
```

The watchdog never sends flight commands. Stop it cleanly with `Ctrl-C` or
`SIGTERM`.

## Raw low-latency video on the Mac

Load the token into the current Mac shell over trusted ADB without displaying it:

```bash
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
ADB="$ANDROID_SDK_ROOT/platform-tools/adb"
BOOX="$BOOX_ADB"
export VEIL_DJI_TOKEN="$(
  "$ADB" -s "$BOOX" exec-out run-as com.dji.sampleV5.aircraft \
    cat shared_prefs/bridge.xml |
  sed -n 's/.*name="token">\([^<]*\)<.*/\1/p'
)"
test -n "$VEIL_DJI_TOKEN"
tools/veil_dji_video_native
```

The preferred Apple Silicon viewer authenticates directly to TCP port 8766,
decodes the unmodified HEVC elementary stream with VideoToolbox, and displays
the newest decoded frame with Core Image/Metal. It has no container probe,
transcode, audio clock, or generic-player presentation queue. Build it before
powering the aircraft when startup time matters:

```bash
swift build -c release --package-path tools/macos-video
```

The portable fallback reads the codec and cadence from `/status` and pipes the
same elementary stream to `ffplay` through a bounded user-space queue:

```bash
python3 tools/veil_dji.py video
```

See `tools/macos-video/README.md` for the synthetic head-to-head benchmark and
its limits. Neither result is a zero-latency or live camera-to-eye claim; the
airlink, BOOX callback, Wi-Fi, codec reordering, and display scanout still add
latency. The Mini 4 Pro path observes DJI video channel 0 before MSDK's decoder;
the BOOX does not decode or transcode the HEVC feed. `scrcpy` is optional and is
only a setup/debug console.

The relay incrementally assembles complete Annex-B NAL units across DJI callback
boundaries and caches the exact HEVC VPS, SPS, and PPS. A newly authenticated
viewer remains pending while the bridge requests an I-frame from the air link
(with the camera action as fallback). It receives the cached parameter sets and
joins the stream only at a first-slice IDR NAL (type 19 or 20). Active viewers
receive the exact live NAL bytes; only callback chunking changes. Each viewer has
a dedicated socket writer and a 512 KiB byte-bounded queue, so a stalled viewer
cannot block DJI's observer or the broadcaster. Queue exhaustion closes only
that viewer; its Mac client reconnects at a fresh IDR. The
`video_client_queue_rejections` status counter exposes these disconnects.
Observer resets close
active sockets because bytes already buffered by TCP cannot safely be retracted.

Both Mac viewers verify VPS/SPS/PPS and join only on a complete first-slice IDR.
The native viewer creates a VideoToolbox session only after that bootstrap and
keeps a one-frame post-decode display mailbox. It never drops encoded pictures
mid-GOP. Its SPS parser enables temporal processing when the stream declares
picture reordering, because HEVC with B pictures cannot safely be forced to
literal zero delay.

The `ffplay` fallback removes the unsafe decoder `low_delay` and encoded-frame
drop flags, captures repeated reference-loss errors, and caps user-space backlog
at 256 KiB. Before launching the decoder it samples the bridge's first-slice
access-unit rate, takes the median, and snaps near a common nominal cadence
(24/25/30/50/60, including 23.976/29.97/59.94 variants). This avoids an incorrect
raw-demux rate turning a 30 fps feed into steadily growing latency. If `ffplay`
falls behind or loses references, the client discards the
entire decoder session—not arbitrary HEVC frames—reconnects, and thereby asks the
bridge for a new IDR. Kernel socket buffers are also kept small. The client
waits up to eight seconds for a fresh measured cadence and never silently
guesses 25 fps; use an explicit override only when the measured rate is
unavailable. Override the input cadence or backlog cap when needed:

```bash
python3 tools/veil_dji.py video --fps 25 --max-backlog-kib 256
```

One earlier bench stream measured about 650 KiB/s at 1920×1080, but its
`ffprobe` rate was not reliable enough to assume every aircraft/camera profile
is 25 fps. `/status` exposes the raw `VideoBufferInfo.timestamp`, its delta,
callback monotonic age/interval/rate, and ingress byte rate. DJI's timestamp unit
is intentionally reported as unknown until live deltas establish its semantics.
The status also reports the first-slice access-unit count, age, and one-second
arrival rate used to select ffplay's raw-HEVC demux cadence.

The bridge does not initialize `MediaDataCenter`. MSDK nevertheless starts
`MediaManager` and `CameraStreamManager` internally during registration;
immediately after registration the bridge shuts down that unused decoder
pipeline, then installs its raw observer. Otherwise MediaManager installs a YUV
frame listener on channel 0, displaces the raw observer, and tries to create an
HEVC decoder on the BOOX. A lightweight relay-owned background health check
reattaches the raw observer if channel data stops for three seconds; it does not
depend on the status activity remaining visible.

This path uses the public `DJIVideoManager.setVideoObserver` symbol that MSDK
5.18's own `AircraftStreamSource` uses internally. DJI does not document it as
part of the supported Mobile SDK API, so re-check its signature and channel
mapping before upgrading MSDK. Channel 0 is the Mini 4 Pro main camera; vision
assist is not currently relayed.

## API

Status is read-only but still requires authentication:

```bash
curl -H "X-Veil-Token: $VEIL_DJI_TOKEN" "$VEIL_DJI_BRIDGE_URL/status"
```

Supervisory control calls require the same ADB-provisioned token. Automated
takeoff must occur while the RC owns authority; do not enable virtual stick
first:

```bash
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/takeoff?confirm=TAKEOFF"
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/land"
# Only if /status reports landing_confirmation_needed=true:
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/land/confirm"
```

DJI's `KeyStartTakeoff` has no target-height parameter. It uses the aircraft's
firmware-selected automatic takeoff height (approximately 1.2 m), so this API
does not implement a one-foot takeoff.

`/land` is not represented as a takeoff-cancellation command. If a takeoff
callback is still pending and current-connection telemetry does not yet prove
`is_flying=true`, the bridge returns `409` with
`landing_conflicts_with_pending_takeoff`; use the RC to abort or retry once the
aircraft is observably airborne. This prevents an early auto-land rejection
from being followed by a delayed takeoff.

The authenticated bridge can deliberately change the persistent RC signal-loss
action while the aircraft is grounded and fully disarmed. The literal
confirmation prevents an accidental settings write; restore the operator's
preferred value after an indoor test:

```bash
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/failsafe-action?action=HOVER&confirm=SET_FAILSAFE_ACTION"
```

Every takeoff, landing/landing-confirmation, virtual-stick enable/disable, and
supervisory setpoint returns a unique `command_id`, its current state, and a
`result_url`. A genuinely pending action returns `202`; an immediate local
command-conflict or transport failure returns `409` with the failed command
record, never `accepted_for_processing=true`. The initial state for an
asynchronous DJI action is normally `requested`; the response is not a claim
that the aircraft moved:

```json
{
  "accepted_for_processing": true,
  "request_recorded": true,
  "command_id": "cmd-1234-1",
  "state": "requested",
  "result_url": "/commands/cmd-1234-1"
}
```

Query the exact callback result or the newest retained commands with the same
token:

```bash
curl -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/commands/cmd-1234-1"
curl -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/commands?limit=16"
```

The bounded 64-entry journal survives unrelated `last_event` updates for the
life of the app process and retains DJI's error type, error code, inner code,
description, hint, and raw representation. `succeeded` means DJI invoked the
action's success callback (or, for an HTTP setpoint, the bridge accepted the
latest setpoint); it never implies physical takeoff, motion, or landing.
Confirm those separately using `/status` fields `flight_test_result` and
`aircraft_telemetry`.

### Aircraft, perception, and RC telemetry

`/status.aircraft_telemetry` and every port-8768 telemetry frame expose the
Mini 4 Pro/RC-N2 data available through MSDK 5.18. In addition to position,
velocity, attitude, GPS, flight state, home/RTH, authority, safety, health, and
Remote ID, the structured groups include:

- `compass`: count, heading in degrees, and error state.
- `wind`: DJI warning level, speed in decimeters/second and meters/second, and
  DJI's world-direction enum.
- `gimbal`: main-gimbal pitch/roll, world-NED yaw, and a separately sourced yaw
  relative to aircraft heading, all in degrees.
- `imu_calibration`: IMU count, calibration state/progress, and calibration
  orientations. MSDK does not expose raw accelerometer or gyroscope samples for
  this aircraft; the JSON marks both unavailable.
- `perception.information`: directional working flags, enabled flags, avoidance
  type, and warning/braking distances in meters.
- `perception.obstacle_distances`: DJI's complete horizontal distance vector,
  horizontal angular interval, and upward/downward ranges in millimeters. DJI
  does not document the horizontal vector's angle origin or ordering, so the
  bridge preserves the vector and explicitly marks that mapping undocumented.
- `remote_controller`: connection, all four RC-N2 stick axes, left dial,
  shutter/record/go-home/camera-mode-switch/custom-1 buttons, and RC battery
  data.
- `battery`: live electrical/thermal values plus cell count, discharge count,
  manufacture date, serial number, and firmware version.

Each new source includes observation/update timestamps and age where applicable.
MSDK does not expose the Mini 4 Pro's raw obstacle-camera imagery or a depth map;
`raw_obstacle_camera_imagery_exposed=false` is deliberate. Channel 0 remains the
only relayed camera feed. `/status.perception_listener_recovery` reports listener
registration, bounded-backoff retries, consecutive issues, last error, and
attempt/success/next-retry timestamps. While the aircraft is connected, the
Android watchdog re-registers both perception listeners if either source is
silent for more than five seconds; retries back off from 2 to at most 30 seconds.

The authenticated perception configuration endpoint reads the latest complete
`PerceptionInformationListener` snapshot:

```bash
curl -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/perception/config"
```

One confirmed POST changes exactly one setting. It is journaled as
`perception_config_set`; success requires both DJI's set callback and a direct
DJI getter readback matching the requested value:

```bash
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/perception/config?confirm=SET_PERCEPTION_CONFIG&setting=avoidance_type&value=BRAKE"
```

The Mini 4 Pro delegate packaged in MSDK 5.18 implements only the writable
`avoidance_type` setting (`BRAKE`, `BYPASS`, or `CLOSE`), so the bridge does not
advertise the other generic perception setters that return `UNSUPPORTED` on this
aircraft. Their values remain read-only in `perception.information`. DJI's
deprecated overall-enabled value is explicitly non-authoritative; effective
enablement is derived from avoidance type instead. Only one mutation/readback
may be pending; a concurrent POST returns a journaled `409` conflict. Poll the
returned `/commands/<id>` URL for the verified result. Configuration and obstacle
telemetry do not prove that braking or bypass remains active while Virtual Stick
owns flight authority; that capability remains explicitly unverified and must
not be used as a flight-safety guarantee.

### Camera and gimbal API

The authenticated public-MSDK surface is reported directly by:

```bash
curl -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/camera-gimbal/capabilities"
curl -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/camera-gimbal/status"
```

All mutations use one serialized endpoint and the existing command journal:

```bash
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/camera-gimbal/command?action=set_camera_mode&value=VIDEO_NORMAL"
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/camera-gimbal/command?action=start_record"
curl -X POST -H "X-Veil-Token: $VEIL_DJI_TOKEN" \
  "$VEIL_DJI_BRIDGE_URL/camera-gimbal/command?action=gimbal_angle&mode=ABSOLUTE_ANGLE&pitch=-45&roll=0&yaw=0&roll_ignored=true&yaw_ignored=true&duration_seconds=1"
```

Supported actions cover camera mode, single/interval photos, recording, zoom,
focus, exposure/ISO/shutter/white balance, photo format/ratio, video
format/resolution/frame rate, storage status/confirmed format, and gimbal
mode/angle/speed/reset/vertical-shot. Setters query the live aircraft range
where DJI exposes one and require a direct matching readback. Action results
separate `callback_accepted` from `physically_observed`; an accepted action that
cannot be observed before timeout is reported as an unknown physical outcome
and must not be blindly replayed. Storage formatting additionally requires
`confirm=FORMAT_STORAGE:SDCARD` (or the exact selected storage name).

This API exposes only keys declared in the packaged Mini 4 Pro MSDK 5.18
manifests. It does not invent raw obstacle-camera video, a depth map, photo
pixel-resolution selection, or DJI Fly's private mission library.

DJI can pause auto-landing near the ground and set
`aircraft_telemetry.safety.landing_confirmation_needed=true`. The bridge never
confirms this automatically. A supervised caller may then POST `/land/confirm`;
the endpoint rejects the request locally unless that exact telemetry flag is
true, and journals DJI's callback or full error like every other flight action.

`/status.flight_test_readiness` is an informational snapshot for the Mac policy
layer. Its battery, GPS, home-location, failsafe, flight-mode, health, and Remote
ID findings are journaled with each takeoff request but do not veto the request
inside the APK. After token authentication and the literal `confirm=TAKEOFF`,
the bridge dispatches `KeyStartTakeoff` unless a conflicting asynchronous
takeoff/landing/authority transition is already in progress. Those remaining
gates prevent duplicate or contradictory DJI actions; they are transport
correctness, not flight policy. Aircraft firmware still applies its own limits.
If DJI rejects takeoff, `/commands/<id>` returns DJI's error type, code, inner
code, description, hint, and raw representation without replacing it with a
bridge policy error. Readiness never authorizes flight; the Mac-side controller
and pilot decide how to use the reported findings.

Continuous control uses authenticated V2 UDP packets on port 8767. Each packet
is bound to the current arming session, has an unsigned 64-bit sequence and a
bridge-monotonic timestamp, and carries a 128-bit truncated HMAC-SHA256 tag.
Fetch `/status` after every enable to obtain `control_session` and
`control_monotonic_ms`. Send fresh packets at 5–25 Hz. The bridge emits the
latest advanced command to DJI at 20 Hz, sends explicit zero velocity after
300 ms without a valid packet, and disables virtual stick/relinquishes MSDK
authority after 1 second. A new enable is then required.

Every accepted V2 packet is echoed as one matched acknowledgement snapshot in
`/status` and the telemetry stream: `last_control_session`,
`last_control_sequence_hex`, the sent/received/applied monotonic timestamps,
`last_control_receive_to_apply_ms`, `last_control_latency_ms`, and
`last_control_setpoint`. Session rotation clears the acknowledgement. The Mac
flight session treats an exact sequence, or a newer 20 Hz heartbeat in the same
session with the same echoed setpoint, as proof that the Android latest-setpoint
mailbox accepted the command. This is transport proof, not proof of aircraft
motion; use fresh aircraft telemetry to assess the physical response.

`body_velocity` is the default and uses DJI advanced mode: forward/right/up are
meters per second in the aircraft BODY frame and positive yaw is clockwise in
degrees per second. `tools/veil_dji.py velocity` is the VDC2 reference encoder.
The optional `sticks` mode uses normalized `[-660,660]` Mode-2-like axes and
DJI's basic mode, which its 5.18 implementation transmits at 5 Hz; use
`enable-sticks` plus `tools/veil_dji.py sticks`. DJI does not list Mini 4 Pro
among aircraft guaranteed to retain obstacle avoidance during virtual-stick
control, so the pilot must not rely on obstacle sensing as the control failsafe.

## Persistent Mac flight session

Run one retained newline-delimited JSON process instead of starting a new
client for every command:

```bash
python3 -u tools/veil_dji_flight.py --no-auto-arm
```

The process continuously drains telemetry, retains only the highest sequence,
owns one authenticated V2 UDP session, and refreshes its current setpoint at
20 Hz. A new `velocity` or `neutral` command sends a datagram synchronously
instead of waiting for the next periodic tick. `arm`, `move_relative`,
`rotate_relative`, `handoff`, and `land` run as preemptible operations, while
the REPL remains available for immediate manual override. For example:

```json
{"command":"arm","request_id":"arm-1"}
{"command":"velocity","request_id":"right","right_mps":0.25}
{"command":"neutral","request_id":"stop"}
{"command":"status","request_id":"status-1"}
{"command":"handoff","request_id":"rc-1"}
```

The default startup attempts to arm in neutral; `--no-auto-arm` requires the
explicit `arm` command shown above. Arming and accepting a route never cause
takeoff by themselves. `move_relative` estimates displacement from speed and
time and is therefore open-loop, not a promise of exact positioning.
`rotate_relative` closes its stopping decision around fresh unwrapped yaw
telemetry, but reports the observed result rather than claiming the requested
angle was achieved. A language-model or UI caller remains supervisory; the
JSON-lines process is the low-latency machine interface.

### Nymph Manager integration

Run the retained flight process with its private local API enabled. Keep the
Android token in this process only:

```bash
RUNTIME_DIR="${TMPDIR:-/tmp}/veil-dji-$UID"
install -d -m 700 "$RUNTIME_DIR"
export VEIL_DJI_UNIX_SOCKET="$RUNTIME_DIR/flight.sock"
python3 -u tools/veil_dji_flight.py \
  --host "$VEIL_DJI_HOST" \
  --no-auto-arm \
  --unix-socket "$VEIL_DJI_UNIX_SOCKET"
```

From the VEIL repository root, point Node at the same socket path:

```bash
export VEIL_DJI_UNIX_SOCKET="${TMPDIR:-/tmp}/veil-dji-$UID/flight.sock"
```

The browser polls `/api/nymphs/dji/status` and receives only an explicit safe
projection. The Nymph Manager can issue acknowledged arm and hold/resume calls;
the client object at `window.__twin.nymphBridge` exposes the remaining
allowlisted route, neutral, handoff, and land operations. Every mutation has an
exact confirmation and is loopback-only by default. The Node process never
receives `VEIL_DJI_TOKEN`.

Arming, start, and resume remain unavailable until the server-owned flight
qualification is intentionally enabled after its checks. Route acceptance also
requires an exact SHA-256 attestation of the checked document bytes:

```bash
export VEIL_DJI_ENVELOPE_READY=1
export VEIL_DJI_RC_TAKEOVER_VERIFIED=1
export VEIL_DJI_CHECKED_ROUTE_SHA256="$(shasum -a 256 checked-route.json | awk '{print $1}')"
```

Set only the qualification values that have actually been checked, then start
Node without the Android credential in its environment:

```bash
env -u VEIL_DJI_TOKEN npm start
```

Do not set those flags merely to clear a UI warning. A Unix disconnect
neutralizes translation and pauses route ownership; reconnecting never
automatically re-arms or resumes. Browser click-to-route waypoints stay
unchecked drafts and are not submitted as route revisions.

## Mac Virtual Stick routes

The persistent flight session implements the strict `veil.route-revision.v1`
route engine on the Mac. The Android APK intentionally has no route-upload
endpoint: it remains the thin 20 Hz Virtual Stick transport and telemetry truth
source. A route revision has this shape:

```json
{
  "schema": "veil.route-revision.v1",
  "engine": "bridge_virtual_stick",
  "expected_accepted_revision": null,
  "activation": "immediate",
  "scope": "remaining_route_from_current_state",
  "plan": {
    "route_id": "survey-42",
    "revision": 1,
    "waypoints": [
      {
        "latitude_deg": 38.0001,
        "longitude_deg": -77.0001,
        "altitude_m": 10.0,
        "horizontal_speed_mps": 2.0,
        "vertical_speed_mps": 1.0,
        "horizontal_tolerance_m": 1.0,
        "vertical_tolerance_m": 0.5,
        "yaw_mode": "face_waypoint",
        "maximum_yaw_rate_deg_s": 30.0
      }
    ]
  }
}
```

Send the complete route JSON as the string-valued `document` in a
`route_accept` REPL command; keeping the original text allows strict duplicate
and unknown-key rejection. Then use `route_start`, `route_pause`,
`route_resume`, `route_abort`, and `route_status`. Revisions use compare-and-set
through `expected_accepted_revision`, become visible atomically, and may activate
immediately or at the next waypoint boundary. `full_route_continue` preserves
the current target index; `remaining_route_from_current_state` starts at
replacement waypoint zero from the aircraft's fresh current state. Acceptance
alone is non-disruptive, while an accepted immediate revision to a running
route retargets the existing authority session without disabling/re-enabling
Virtual Stick. Any manual velocity, neutral, relative move/rotation, handoff,
or landing command first pauses route ownership so the route loop cannot
overwrite the manual command.

This is a Mac-resident guidance loop, not a DJI Fly plan or an aircraft-resident
mission. MSDK 5.18's Mini 4 Pro capability manifest declares `virtualStick` but
does not expose `waypointMission`; DJI also exposes no supported DJI Fly mission
library import/export or in-place editing API. Route execution therefore stops
when the Mac/BOOX/RC path is no longer healthy. It uses the aircraft's
reported WGS84 position and raw `KeyAircraftLocation3D` altitude, has no RTK,
and cannot promise one-foot or centimeter-level waypoint accuracy. Ensure route
altitudes use the same reference observed in live telemetry. Mini 4 Pro is not
documented as retaining automatic obstacle avoidance under Virtual Stick, so
the bridge advertises that capability as false rather than treating perception
data as guaranteed braking or bypass.

Persistent newline-delimited JSON telemetry is published at 20 Hz on TCP port
8768; `python3 tools/veil_dji.py telemetry` is the simple printer. Each client
has a capacity-one latest-value mailbox, so a delayed writer replaces an
obsolete pending snapshot instead of replaying a growing application queue.
Frames include `telemetry_sequence`, generated and per-client write timestamps,
`telemetry_queue_age_ms`, `telemetry_client_sequence_gap_before_write`, the
actual TCP send-buffer size, and source-specific update ages. A control client
must continuously drain the socket, reject out-of-order frames, retain the
highest sequence, and reset its sequence baseline after reconnect because an
Android process restart may begin again at sequence one. TCP can still add
transport delay; queue age and Mac arrival age must be checked before using a
frame for flight guidance.

At DJI SDK `START_TO_INITIALIZE`, the bridge calls the documented product-
improvement opt-out, before DJI initializes its analytics engine, and verifies
the setting again at initialization completion. `/status` exposes the request,
the actual agreement value, and any configuration error. DJI registration still
requires networking; MSDK logs remain local and enabled for test diagnostics.

Do not send physical-control requests until registration, aircraft connection,
telemetry, video, location legality, and the surrounding flight area have all
been verified.
