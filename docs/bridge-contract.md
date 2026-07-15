# VEIL ↔ Android drone bridge contract

**Contract:** `veil-drone-bridge/1.0`
**Status:** frozen integration seam; VEIL endpoints are not implemented yet
**Aircraft target:** DJI Mini 4 Pro + RC-N2
**Authority:** VEIL plans and guards; the Android APK executes and fails safe

This contract keeps the bridge thin. The APK owns the MSDK session, aircraft-side
heartbeat, and immediate failsafes. VEIL owns mission truth, surface-aware
altitude conversion, the geofence, command-envelope checks, persistence, and UI.
The bridge must not invent a mission, alter an altitude, or silently clamp a
command.

The authoritative mission format is
[`docs/contracts/flight-mission.schema.json`](contracts/flight-mission.schema.json).
Both mission delivery paths consume the same WPML KMZ artifact produced by VEIL.

## 1. Delivery phases and gates

Phase 1 supplies mission discovery, artifact pull, mission execution events, and
native mission verbs. Phase 2 adds telemetry, encoded camera transport, and
virtual-stick control.

- Native mission upload/start is unavailable until B1 proves upload, a second
  in-air upload and start, pause/resume/stop semantics, and upload latency on the
  actual Mini 4 Pro firmware.
- Virtual stick is unavailable until B2 proves engage/disengage behavior, the
  500 ms stale-command brake, RC pause/mode-switch takeover, simulator support,
  and obstacle-avoidance behavior on the actual firmware.
- A failed B1 does not block telemetry or video. It disables `GUIDED-N` and leaves
  mission delivery on the ADB side-load/manual transports.
- A failed or incomplete B2 keeps virtual-stick control disabled.

Capabilities are reported by the bridge; VEIL never infers them from aircraft
model alone.

## 2. Network topology

The default path uses wireless ADB reverse tunnels:

```bash
adb reverse tcp:8425 tcp:8425   # HTTP + WebSocket API
adb reverse tcp:8426 tcp:8426   # encoded video byte stream
```

On Android, the APK connects to:

- `http://127.0.0.1:8425` for HTTP
- `ws://127.0.0.1:8425` for WebSockets
- `tcp://127.0.0.1:8426` for encoded video

A direct LAN address may replace `127.0.0.1` without changing any application
message or framing. The bridge must expose the selected base URL in diagnostics.

All JSON is UTF-8. All `t` fields are Unix epoch milliseconds. Sequence numbers
are non-negative integers and increase monotonically within one bridge session.

## 3. Initial capability message

The first upstream message on each WebSocket is:

```json
{
  "type": "hello",
  "contract": "veil-drone-bridge/1.0",
  "session_id": "01J...",
  "device_id": "boox-tab-ultra-c",
  "aircraft": {
    "model": "mini4pro",
    "serial_suffix": "1234",
    "firmware": "record-the-exact-version"
  },
  "bridge_version": "0.1.0",
  "capabilities": {
    "telemetry": true,
    "encoded_video": true,
    "native_missions": false,
    "virtual_stick": false,
    "rc_takeover_verified": false,
    "oa_under_virtual_stick": "unknown",
    "msdk_simulator": "unknown"
  }
}
```

`native_missions` becomes `true` only after B1 passes on the reported firmware.
`virtual_stick` and `rc_takeover_verified` become `true` only after B2 passes.
`oa_under_virtual_stick` is `unknown`, `active`, or `inactive`; it must be reset
to `unknown` after an aircraft firmware change until re-tested.

VEIL rejects an unsupported major contract version. Minor additive fields are
ignored by both sides.

## 4. Mission HTTP surface

### `GET /api/flights`

Response:

```json
[
  {
    "id": "east-ridge-grid",
    "name": "East ridge grid",
    "updated": "2026-07-14T17:42:00Z",
    "status": "checked"
  }
]
```

### `GET /api/flights/:id/artifact.kmz`

Returns the latest checked WPML KMZ as `application/vnd.google-earth.kmz`.
Response headers include:

```text
ETag: "sha256:<hex>"
X-VEIL-Artifact-SHA256: <hex>
X-VEIL-Mission-ID: east-ridge-grid
```

The bridge verifies the downloaded bytes against `X-VEIL-Artifact-SHA256`
before presenting or pushing the mission. A mismatch is fatal.

### `POST /api/flights/:id/events`

Request:

```json
{
  "t": 1784052000123,
  "type": "progress",
  "wp": 7,
  "detail": {
    "session_id": "01J...",
    "artifact_sha256": "<hex>",
    "verified": true
  }
}
```

Allowed event types are `uploaded`, `started`, `progress`, `paused`, `completed`,
`aborted`, and `error`. `wp` is optional and uses the canonical zero-based
waypoint index. `detail` is optional JSON.

An upload event must report the artifact hash and `verified`. If MSDK cannot
read the artifact back from the aircraft, the bridge reports `verified: false`;
successful SDK acceptance is not equivalent to read-back verification.

## 5. Telemetry WebSocket

Endpoint: `WS /api/drone-telemetry`

Nominal rate is 5 Hz, increasing to 10 Hz whenever `MISSION`, `GUIDED-N`,
`GUIDED-VS`, or `DIRECT` is engaged.

```json
{
  "type": "telemetry",
  "seq": 418,
  "t": 1784052000123,
  "lat": 43.60123,
  "lon": -74.10123,
  "alt_rel_m": 42.7,
  "heading_deg": 281.4,
  "battery_pct": 76,
  "mode": "MANUAL",
  "rc_override": false
}
```

Required fields are `type`, `seq`, `t`, `lat`, `lon`, `alt_rel_m`,
`heading_deg`, `battery_pct`, `mode`, and `rc_override`.

Allowed modes are `IDLE`, `MANUAL`, `MISSION`, `GUIDED-N`, `GUIDED-VS`,
`DIRECT`, `ABORT`, and `RTH`. Mode transitions are sent immediately rather than
waiting for the next periodic frame.

The bridge may add velocity, gimbal, GNSS-quality, link-quality, satellite,
camera, and obstacle-sensing fields without a contract version change.

## 6. Encoded video TCP stream

The APK registers `ICameraStreamManager.addReceiveStreamListener` and forwards
the still-encoded transmission stream. It must not attach a display surface or
instantiate a `MediaCodec` decoder.

On each TCP connection the bridge sends exactly one newline-terminated JSON
header:

```json
{"contract":"veil-video/1.0","codec":"h264","width":1920,"height":1080,"clock":"unix_us"}
```

It then sends access-unit records. Integers are unsigned, big-endian:

```text
uint32 record_length       # bytes following this field: 8 + access_unit_length
uint64 capture_time_us     # Unix epoch microseconds
byte[] annex_b_access_unit # record_length - 8 bytes
```

Rules:

- `codec` is `h264` or `h265`.
- H.264/H.265 payloads use Annex B start codes.
- Parameter sets (SPS/PPS and VPS for H.265) are forwarded whenever MSDK emits
  them and after reconnect where possible.
- A record larger than 16 MiB is invalid and VEIL closes the connection.
- The bridge reconnects with bounded exponential backoff and sends a new header.
- Capture timestamps must be monotonic within a connection. If the SDK does not
  expose a capture clock, use bridge receipt time and declare
  `"timestamp_source":"bridge_receive"` in the header.

VEIL owns remux/transcode, WebRTC/WHEP publication, recording, and CV taps.

## 7. Control WebSocket

Endpoint: `WS /api/drone-control`

The channel is bidirectional. The bridge sends its `hello` first. VEIL does not
send a setpoint until the capability message has been accepted and all VEIL-side
interlocks are satisfied.

### 7.1 Realtime setpoint (VEIL → bridge)

```json
{
  "type": "setpoint",
  "seq": 91,
  "t": 1784052000123,
  "frame": "ned",
  "vx_mps": 1.2,
  "vy_mps": 0.0,
  "vz_mps": -0.2,
  "yaw_rate_dps": 0.0
}
```

- In `ned`, `vx` is north, `vy` is east, and positive `vz` is down.
- In `body`, `vx` is forward, `vy` is right, and positive `vz` is down.
- Positive yaw rate is clockwise when viewed from above.
- VEIL sends at 5–10 Hz. The APK interpolates and drives the MSDK virtual-stick
  loop at 25 Hz.
- Stale or out-of-order sequence numbers are rejected.
- A setpoint older than 500 ms at receipt is rejected as stale.

Every setpoint has already passed the VEIL envelope guard. The bridge must not
silently change it. If an aircraft/SDK limit requires a clamp, report the clamp
in the acknowledgement.

### 7.2 Discrete command (VEIL → bridge)

```json
{
  "type": "command",
  "seq": 92,
  "t": 1784052000220,
  "cmd": "mission_upload",
  "params": {
    "flight_id": "east-ridge-grid",
    "artifact_url": "/api/flights/east-ridge-grid/artifact.kmz",
    "sha256": "<hex>"
  }
}
```

Allowed commands:

```text
takeoff  land  rth  abort
mission_upload  mission_start  mission_pause  mission_resume  mission_stop
gimbal_set  photo  record_start  record_stop
```

Unknown commands are rejected. `land`, `rth`, and `abort` are never inferred
from a lost VEIL connection; connection loss follows the explicit dead-man and
aircraft failsafe configuration below.

### 7.3 Acknowledgement (bridge → VEIL)

```json
{
  "type": "ack",
  "seq": 91,
  "t": 1784052000138,
  "applied": true,
  "clamp": null,
  "error": null
}
```

For a clamp:

```json
{
  "type": "ack",
  "seq": 91,
  "t": 1784052000138,
  "applied": true,
  "clamp": {
    "field": "vx_mps",
    "requested": 8.0,
    "applied": 3.0,
    "reason": "aircraft_mode_limit"
  }
}
```

`applied: false` requires a stable machine-readable `error.code` and a human
message. Clamps and rejections are always logged and shown to the pilot.

### 7.4 Immediate events (bridge → VEIL)

```json
{
  "type": "event",
  "t": 1784052000138,
  "event": "rc_override",
  "detail": { "engaged": true, "aircraft_mode": "MANUAL" }
}
```

Event names include `mode_change`, `rc_override`, `vs_engaged`, `vs_dropped`,
`deadman`, `aircraft_warning`, and the mission event types in §4.

## 8. Dead-man and disconnect behavior

The APK owns the virtual-stick heartbeat. Its default command TTL is 500 ms:

1. Accept only a fresh, increasing setpoint sequence.
2. Interpolate accepted VEIL setpoints into the local 25 Hz MSDK loop.
3. If no fresh setpoint arrives within 500 ms, ramp commanded velocities and yaw
   rate to zero, command brake/hover, emit a `deadman` event, and remain stopped.
4. Do not resume motion merely because the socket reconnects. VEIL must re-arm
   the control mode and send a new command sequence.

RC pause/mode-switch takeover must drop virtual stick immediately and emit
`rc_override`. Exact aircraft behavior is recorded by B2 for each firmware.

Native mission link-loss behavior is separate: the aircraft follows the
validated committed mission and configured aircraft failsafe. VEIL displays the
loss but does not assume the aircraft stopped.

## 9. Safety ownership

| Concern | VEIL | Bridge APK |
|---|---|---|
| Canonical mission and WPML hash | authoritative | verifies and reports |
| AGL surface selection / takeoff-relative conversion | authoritative | never converts |
| Terrain/canopy floor and property geofence | checks and clamps | v2 cached floor-grid backstop |
| Realtime command sequencing | emits | rejects stale/out-of-order |
| Virtual-stick 25 Hz heartbeat | no | authoritative |
| 500 ms dead-man / brake-hover | no | authoritative |
| RC takeover detection | displays/logs | detects and acts immediately |
| MSDK session and aircraft errors | displays/logs | authoritative |

No unchecked setpoint may leave VEIL. No stale setpoint may reach MSDK. Neither
side may hide a clamp.

## 10. Required bridge diagnostics

The APK must make these visible in its UI and `adb logcat`:

- contract and bridge version
- Android device ID and aircraft model/firmware
- B1/B2 capability flags
- VEIL base URL and connection/reconnect state
- telemetry and setpoint sequence/age
- last command, ack, clamp, and error
- virtual-stick engaged state and dead-man state
- overall and per-direction `IPerceptionManager` state while VS is engaged
- video codec, dimensions, bitrate estimate, and timestamp source

Never log a complete bearer token or other credential.

## 11. Gate evidence to return to VEIL

### B1

- exact aircraft, DJI firmware, MSDK, and bridge versions
- first KMZ upload/start result
- second KMZ upload and start while already airborne
- pause/resume behavior
- stop result: hover, land, RTH, or other
- small-KMZ upload latency distribution
- whether the uploaded artifact can be read back and hash-verified

### B2

- VS engage/disengage result
- props-off or simulator TTL test proving brake within 500 ms
- RC pause and mode-switch takeover result
- MSDK simulator result
- `IPerceptionManager` overall/per-direction switches and avoidance type during VS
- controlled slow soft-obstacle result
- exact aircraft firmware; repeat after firmware updates

## 12. Compatibility rule

Breaking changes require a new major contract path/version and coordinated
rollout. Additive JSON fields are allowed in 1.x. Receivers must ignore unknown
fields and reject unknown command verbs. This document, not third-party WPML
documentation, is the bridge seam; DJI Fly-harvested fixtures remain the oracle
for aircraft-specific WPML structure and enums.
