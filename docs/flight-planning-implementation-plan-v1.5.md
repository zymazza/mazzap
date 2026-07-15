# VEIL Flight Planning — Implementation Plan v1.5

> Durable copy of the working plan supplied by Zy on 2026-07-14. Preserve this
> version as the v1.5 baseline; make later material changes in a new version or
> with an explicit changelog. The Android bridge APK remains a separate effort.

Frozen companion contracts:

- [Canonical flight mission JSON Schema](contracts/flight-mission.schema.json)
- [VEIL ↔ Android bridge contract](bridge-contract.md)

**Scope:** Everything except the Android bridge APK (separate parallel effort, "the bridge"). This plan defines the seam so the bridge plugs in whenever it's ready, without either side blocking the other.

**Priority of loops:** drone → VEIL (tracks, media, surface refresh — the twin is the accumulating asset) is the **primary loop**. VEIL → drone (missions, guarded control) is the enabling loop, and its altitude floors are keyed to how fresh the drone-derived surface is (§12.4).

**Targets:** DJI Mini 4 Pro + RC-N2. Android host device: BOOX first, any Android by design. Transport substrate: wireless ADB.

**Consumers of this doc:** Zy + coding agents working in the VEIL repo. Sections are written to be executable as work orders.

---

## 1. Design invariants — freeze these before any code

These five decisions make the plan "general enough." Everything else is replaceable.

1. **One canonical mission model, in the store.** Flights and waypoints are journaled twin-store entities. Every other representation (WPML, CSV, viewer payload, bridge payload) is a pure derivation. Nothing downstream is ever hand-edited.
2. **One artifact: the WPML KMZ.** Both delivery paths consume the *same file* — the DJI Fly side-load copies it into the app's folder, and the bridge (MSDK V5 `WPMZManager` / `pushKMZFileToAircraft`) pushes the same KMZ format to the aircraft. One builder serves both. This is the load-bearing unification.
3. **Altitude discipline.** User-facing altitudes are always AGL over a *named surface* (`ground` or `canopy`). Exported altitudes are always takeoff-relative (`executeHeightMode: relativeToStartPoint` — the only mode the Mini 4 Pro honors). The altitude engine (WS2) is the only code allowed to convert between them. No raw altitude ever passes through unconverted.
4. **Transport is an interface.** `MissionTransport.deliver(artifact, device, opts)` — side-load-via-adb, manual-download, and bridge are three implementations. Adding a device or path never touches the planner or the builder.
5. **Every delivery is verified by read-back.** A transport that cannot read the mission back off the device and confirm a hash match reports `verified: false`. The transfer step is safety-critical: an unverified delivery means the aircraft may fly a mission other than the one VEIL planned.
6. **No unchecked realtime command.** Every piloting setpoint passes the envelope guard (terrain floor + geofence + mode speed caps, §11) before transmit, and the APK independently enforces a TTL dead-man on everything it receives. Clamps are logged and surfaced to the pilot, never silent.

**Interfaces to freeze on day 0** (so parallel work — including the bridge dev's — doesn't collide):

- The mission JSON schema (§4.2)
- The artifact format (WPML KMZ, §7)
- The HTTP surface: `GET /api/flights`, `GET /api/flights/:id/artifact.kmz`, `POST /api/flight-export`, `POST /api/flight-push`, `POST /api/flights/:id/events`
- `docs/bridge-contract.md` (§9) — send to the bridge dev immediately

---

## 2. Gates — cheap verifications before building on assumptions

| Gate | What | Needs | Pass criteria |
|---|---|---|---|
| **G1** | adb write test into DJI Fly's folder on the BOOX | BOOX + DJI Fly installed, no aircraft | `adb push` + `adb pull` + sha256 match on a file inside `/sdcard/Android/data/dji.go.v5/files/waypoint/` |
| **G2** | Fixture harvest: create a real dummy waypoint mission in DJI Fly, pull the GUID folder | Field session, aircraft connected | Harvested KMZ committed to `fixtures/harvested/`; opens, parses, contains `waylines.wpml` |
| **G3** | End-to-end overwrite proof: modify the harvested KMZ (move two waypoints), push back, fly it | Field session, open area | Aircraft flies the *modified* route; confirms the overwrite trick works on current firmware |
| **G4** | BOOX field viability | BOOX + RC-N2 | DJI Fly side-load installs and runs; RC-N2 handshake works (`adb shell pm list features \| grep usb.accessory` must show `android.hardware.usb.accessory`); display usable in fast-refresh mode; note GPS presence (FlySafe/map centering) |
| **B1** (bridge dev's gate, tracked here) | MSDK V5 native waypoint push on Mini 4 Pro — expanded for GUIDED-N: **in-air upload and in-air start** (the micro-mission pattern lives or dies here), pause/resume via MSDK, stop behavior (hover vs. RTH), and measured upload time for a small KMZ (that number is the click-to-committed latency budget) | His APK skeleton + aircraft | `pushKMZFileToAircraft` accepted and mission executes; a *second* mission uploads and starts **while airborne**; pause/resume/stop verified; upload time recorded. Known risk: Dronelink routes Mini 4 Pro through the file workflow citing an SDK limitation. If B1 fails: bridge scope = telemetry/monitor only, delivery stays on adb, GUIDED-N is off the table — see §13 risk table. |
| **B2** (bridge dev's gate) | Virtual-stick behavior on Mini 4 Pro: engage/disengage semantics, RC pause-button / mode-switch takeover while VS is active, TTL brake behavior, MSDK simulator availability, **and OA-under-VS characterization** — query `IPerceptionManager` state (overall + per-direction switches, avoidance type) while VS is engaged, then a controlled slow approach to a soft obstacle in open field | His APK + aircraft, bench / props consideration | Documented, repeatable: pause button kills VS to a hover; stale-setpoint TTL ramps to brake; OA-under-VS verdict recorded per firmware version (re-verify after firmware updates); simulator verdict recorded (if it works, the whole control loop is bench-testable) |

**G1 procedure (run tonight, ~15 min):**

```bash
# On the BOOX: Settings → (About → tap build number 7×) → Developer options
#   → Wireless debugging ON → "Pair device with pairing code"
adb pair <boox-ip>:<pair-port>     # enter the 6-digit code
adb connect <boox-ip>:<port>
adb devices                         # confirm device shows

# Install DJI Fly on the BOOX first (side-load the APK from DJI's site; no Play needed)
# Open DJI Fly once so it creates its data dirs, then:
adb shell ls -la /sdcard/Android/data/dji.go.v5/files/
echo test > /tmp/probe.txt
adb push /tmp/probe.txt /sdcard/Android/data/dji.go.v5/files/probe.txt
adb pull /sdcard/Android/data/dji.go.v5/files/probe.txt /tmp/probe_back.txt
sha256sum /tmp/probe.txt /tmp/probe_back.txt   # must match
adb shell rm /sdcard/Android/data/dji.go.v5/files/probe.txt
```

Pass → the side-load transport is viable on this device. Fail (permission denied) → scoped storage blocks shell on this build; fall back to `ManualTransport` for this device and note it — the architecture doesn't change.

**G2 note:** DJI Fly typically requires the aircraft connected to enter waypoint mode, so dummy creation happens at field sessions. Practical convention: create several dummies per session, named `SLOT-A`, `SLOT-B`, `SLOT-C` in DJI Fly. VEIL's overwrite doesn't change the name Fly displays (that lives in Fly's own DB), so VEIL reports which slot it filled and the user selects that slot in the app.

**G2 is the schema oracle.** Do not trust third-party WPML documentation for enum values (`droneEnumValue`, `payloadEnumValue`, action encodings). The dummy mission DJI Fly itself generates *is* ground truth for exactly this aircraft + app version. All golden-file tests (§7) derive from it.

---

## 3. Architecture

```text
┌──────────────────────────── VEIL (Linux) ─────────────────────────────┐
│ public/flight.js (planner UI)                                         │
│        │                                                              │
│ server.js ── /api/flights* ── twin_store: flight_path / waypoint      │
│        │                            │                                 │
│        │                 scripts/flight_altitude.py                   │
│        │                 (DEM+CHM → exec heights, clearance report)   │
│        │                            │                                 │
│        ├── POST /api/flight-export ── scripts/build_wpml.py           │
│        │                            └──► artifact: <flight>.kmz       │
│        │                                                              │
│        └── POST /api/flight-push ── MissionTransport                  │
│                    ┌──────────────┼───────────────┐                   │
│              SideloadTransport  Manual        BridgeTransport         │
│              (wireless adb)   (download)     (stub + contract)        │
└─────────┬──────────────────────────────────────────┬──────────────────┘
    wireless adb push                        adb reverse tcp:8425
          │                                           │
┌─────────▼──────────────┐                 ┌──────────▼─────────┐
│ Android device (BOOX…) │                 │ Bridge APK (guy's) │
│ DJI Fly waypoint/ dir  │                 │ MSDK V5            │
└─────────┬──────────────┘                 └──────────┬─────────┘
          │ USB (AOA)                                 │ USB (AOA)
          ▼                                           ▼
        RC-N2 ───────────── O4 link ─────────────► Mini 4 Pro
```

Wireless ADB is the common substrate for *both* paths: `adb push` delivers the side-load; `adb reverse` gives the bridge APK a zero-config socket back to VEIL (it dials `127.0.0.1:8425` on-device and reaches the Linux box — works even on networks with client isolation); `adb logcat` is the debug channel for the bridge. One `AndroidDevice` abstraction serves everything.

---

## 4. WS1 — Mission core (store entities + API)

### 4.1 Entity kinds

Written through `twin_store.py` like everything else (`begin_run → upsert_entity → observe`), journaled and rebuildable.

**`flight_path:<slug>`** observations:

- `name`, `created`, `drone_model: "mini4pro"`, `status: draft|checked|exported|flown`
- `takeoff: {x, y, lat, lon, dem_m}` (scene-local + geo; DEM sampled at pin)
- `defaults: {speed_mps, agl_mode, target_agl_m, clearance_ground_m, clearance_canopy_m}`
- Derived after check: `rth_min_rel_m`, `est_duration_s`, `max_exec_height_rel_m`

**`waypoint:<flight>:<n>`** observations:

- `pos: {x, y, lat, lon}`
- `agl_mode: ground|canopy`, `target_agl_m`
- `speed_mps?` (override), `heading: {mode: follow_wayline|fixed, deg?}`, `gimbal_pitch_deg`
- `actions: [{type: photo|start_record|stop_record|hover, params}]`
- Derived by engine: `dem_m`, `chm_m`, `h_cmd_abs_m`, `exec_height_rel_m`, `clamped: bool`

Identity is natural-key by flight + index (waypoints move during editing — survey-feature semantics, not tree semantics). Provide a `reindex` operation for insert/delete so indices stay dense.

### 4.2 Canonical mission JSON (the frozen schema)

```json
{
  "id": "east-ridge-grid",
  "name": "East ridge grid",
  "drone_model": "mini4pro",
  "takeoff": {"lat": 43.6, "lon": -74.1, "dem_m": 512.3},
  "defaults": {"speed_mps": 5.0, "agl_mode": "canopy",
               "target_agl_m": 25, "clearance_canopy_m": 15,
               "clearance_ground_m": 30},
  "waypoints": [
    {"n": 0, "lat": 43.601, "lon": -74.101,
     "agl_mode": "canopy", "target_agl_m": 25,
     "heading": {"mode": "follow_wayline"}, "gimbal_pitch_deg": -90,
     "actions": [{"type": "photo"}],
     "derived": {"dem_m": 518.0, "chm_m": 22.5,
                 "h_cmd_abs_m": 565.5, "exec_height_rel_m": 53.2,
                 "clamped": false}}
  ]
}
```

### 4.3 Deliverables

- Store kinds + `export_viewer_payloads.py` additions (flights render as sequenced polylines, annotations-style)
- CRUD via `server.js`: create flight, add/move/delete waypoint, set fields
- MCP: flights visible via `find_entities` for free; add a `list_flights` convenience tool

**Acceptance:** create/edit/delete round-trips a journal rebuild; MCP lists the flight; viewer payload renders the path.

---

## 5. WS2 — Altitude engine (`scripts/flight_altitude.py`) — the IP

Pure Python, no UI dependency, fully unit-tested. This is the capability DJI Fly structurally cannot offer on this aircraft, and it is the safety-critical core.

### 5.1 Inputs

- DEM raster (bare earth — existing 3DEP-derived)
- CHM raster (canopy height model). **Until the CHM is mature, accept `--canopy-const <m>` as a conservative constant fallback** (e.g. 22 m over the forested mask). This resolves the open question from earlier design work: parameterize, don't block.
- Takeoff point, waypoint list (canonical JSON)

### 5.2 Definitions

```text
DSM(s)              = DEM(s) + CHM(s)                    # surface, not bare earth
h_req(wp)           = DEM(wp) + agl        if agl_mode == ground
                    = DSM(wp) + agl        if agl_mode == canopy
floor(s)            = DSM(s) + clearance_canopy_m        # (reduces to DEM + clearance where CHM≈0)
h_cmd(wp)           = max(h_req(wp), floor(wp))           # mark clamped=true if raised
exec_height_rel(wp) = h_cmd(wp) − DEM(takeoff)            # what WPML gets
```

### 5.3 Corridor check

Sample every ~5 m along each leg. Interpolate commanded altitude linearly between `h_cmd(wp_i)` and `h_cmd(wp_{i+1})`. Require `alt(s) − floor(s) ≥ 0` everywhere. On violation, apply policy (pluggable): `raise` (default — lift the lower endpoint minimally, or insert a midpoint waypoint at `floor + clearance` if raising endpoints distorts the plan), or `reject` with a report. Never silently pass.

### 5.4 Compliance + operational checks (same pass)

- **400 ft rule:** assert `h_cmd(s) − DEM(s) ≤ 121.9 m` everywhere along the corridor (AGL over terrain). Hard fail with locations if violated.
- **RTH advisory:** `rth_min_rel_m = max(DSM over mission bbox) − DEM(takeoff) + clearance_canopy_m`. DJI Fly's RTH altitude is a *manual app setting* the mission file cannot set — the engine computes the minimum safe value and the UI/docs instruct the pilot to set it before launch.
- **Duration estimate:** `Σ(leg_len / speed) + action dwell + 20% overhead`. Warn above 20 min (real-world Mini 4 Pro endurance ≈ 25 min with reserve).

### 5.5 Output

`clearance_report.json`: per-leg min clearance + location, clamped waypoints, max exec height, RTH minimum, duration, 400-ft check result. Consumed by the UI (red segments, HUD numbers) and stored as a flight observation.

**Tests:** synthetic rasters — flat plane, ridge crossing, cliff edge, canopy block in mid-leg, takeoff-in-valley. Each asserts exact expected exec heights and clamp/violation behavior.

**Acceptance:** engine output on the synthetic suite matches hand-computed values; a mission over the real parcel DEM+CHM produces a report with no corridor sample below floor.

---

## 6. WS3 — Planner UI (`public/flight.js`)

Collapsible panel in the bottom-right stack, mirroring `survey.js` / `simulation.js`.

- **Modes:** set-takeoff (pin), add-waypoints (click terrain → waypoint), select/drag. Reuses the existing terrain raycast → scene-local → `georef.js` → lat/lon path.
- **Rendering:** numbered waypoint markers + sequenced polyline (annotations-style). After a check, legs below clearance render red; clamped waypoints get a badge.
- **Inspector:** per-waypoint AGL mode + value, speed override, heading, gimbal pitch, actions.
- **Buttons:** `Check` (runs WS2, renders report), `Preview` (Three.js camera fly-through at planned gimbal pitch; HUD shows live ground-gap *and* canopy-gap), `Export`, `Push` (device + transport picker; calls `/api/flight-push`).
- **Generators (P1):** Grid — draw a polygon, set spacing/overlap/heading, writes ordinary waypoints (bread-and-butter for mapping capture). Orbit (P1b) — center + radius + count. Generators are parameterized *writers into normal waypoints*, never special entities.

**Acceptance:** full plan → check → preview → export → push loop without touching a CLI.

---

## 7. WS4 — Artifact builder (`scripts/build_wpml.py` + endpoints)

- KMZ layout: `wpmz/template.kml` + `wpmz/waylines.wpml` (+ `wpmz/res/` if the fixture contains it). Zip with stdlib.
- Hardcode `executeHeightMode: relativeToStartPoint`. Per-waypoint `executeHeight` from WS2. Global speed with per-waypoint overrides. Action mapping: photo, record start/stop, hover. Heading: `follow_wayline` default, `fixed` per waypoint.
- **All enum values and structural details pinned from the G2 harvested fixture, not documentation.** Build a golden-file test: construct the canonical model that mirrors the harvested dummy, emit, normalized-XML-diff against the harvest. Enum/ID fields must match byte-for-byte.
- Endpoints (`server.js`, same posture as `/api/simulate` — built-in Node, spawn Python for serialization):
  - `POST /api/flight-export {flight_id, format: "wpml" | "litchi_csv"}` → artifact path (Litchi CSV is P2 backlog; see §11)
  - `GET /api/flights/:id/artifact.kmz` — serves the latest artifact. **This same URL is the bridge's pull endpoint.**

**Acceptance:** golden-file reproduction passes; the G3 modified-fixture mission flies.

---

## 8. WS5 — Transport layer

### 8.1 `scripts/adb_transport.py` — the AndroidDevice abstraction

Subprocess wrapper around the `adb` binary (zero Python deps; `platform-tools` is a documented external requirement). Methods: `pair(ip:port, code)`, `connect(ip:port)`, `devices()`, `shell(cmd)`, `push(src, dst)`, `pull(src, dst)`, `forward(spec)`, `reverse(spec)`, `logcat_tail(filter)`.

### 8.2 `MissionTransport` interface

```python
deliver(artifact_path, device, opts) -> {
  "ok": bool, "verified": bool,
  "target_guid": str|None, "backup_path": str|None,
  "notes": [str]
}
```

Exposed via `scripts/flight_push.py` (CLI) and `POST /api/flight-push` (UI button).

### 8.3 `SideloadTransport` algorithm (the side-load, automated)

1. `shell ls` `/sdcard/Android/data/dji.go.v5/files/waypoint/*/` with mtimes.
2. Choose target GUID: `--guid` explicit wins; else newest folder; **abort if newest is older than `--fresh-window` (default 30 min)** unless `--force`. This is the stale-dummy guard — the classic failure is overwriting last week's slot.
3. `pull` the existing `<guid>.kmz` → `backups/<guid>-<ts>.kmz`; print the restore command.
4. `push` the artifact to a temp name in the same folder, then `shell mv` into `<guid>.kmz` (atomic within the filesystem).
5. `pull` it back, sha256-compare against what was sent, parse the waypoint count out of the pulled file.
6. Print the verification line the user acts on: `Mission 'east-ridge-grid' → SLOT-B (guid 3f2a…), 24 waypoints, max rel height 53 m — confirm 24 waypoints in DJI Fly's preview before launch.`
7. Non-zero exit on any mismatch; `verified: false` in the report.

### 8.4 `ManualTransport`

Copies the artifact to outputs with an instructions doc. Always available; the floor that always ships.

### 8.5 `BridgeTransport`

Stub raising `NotImplemented` + the contract doc (§9). Wired into the picker so the seam exists in code from day 1.

**Acceptance:** round-trip verified on the G1 device; stale-dummy guard demonstrated (two dummies, old one refused without `--force`).

---

## 9. Bridge contract — `docs/bridge-contract.md` (send to the bridge dev now)

Keep VEIL authoritative and the bridge thin. Control arrives in two phases: **phase 1** is mission-level commands only; **phase 2** adds the realtime setpoint channel below (design in §11), gated strictly behind B2 and test cards TC0–TC2.

**Networking:** VEIL runs `adb reverse tcp:8425 tcp:8425` whenever the device is attached; the APK dials `http://127.0.0.1:8425` on-device and reaches VEIL with zero WiFi configuration. Plain LAN URL is the documented alternative.

**Pull model (bridge → VEIL):**

- `GET /api/flights` → `[{id, name, updated, status}]`
- `GET /api/flights/:id/artifact.kmz` → the same WPML KMZ the side-load uses
- `POST /api/flights/:id/events` → `{t, type: uploaded|started|progress|paused|completed|aborted|error, wp?: n, detail?}`

**Telemetry (phase 2):** `WS /api/drone-telemetry`, messages `{t, lat, lon, alt_rel_m, heading_deg, battery_pct, mode, rc_override}` at 5 Hz (10 Hz whenever a control mode is engaged) → feeds a live store (the `live_store.py` / Meshtastic pattern) → rendered by `live-inputs.js` unchanged.

**Video (phase 2 — independent of B1):** the APK registers `ICameraStreamManager.addReceiveStreamListener` and relays the *still-encoded* H.264/H.265 transmission stream — no decode on the device, no surface attached. Connect out to `tcp://127.0.0.1:8426` (VEIL runs `adb reverse tcp:8426 tcp:8426` alongside the 8425 reverse), send one JSON header line `{codec, width, height}`, then length-prefixed access units with capture timestamps. VEIL owns everything downstream; the APK is a byte pump. Note camera stream access is core MSDK and does not depend on waypoint push — this channel is worth building even if B1 fails.

**Control channel (phase 2):** `WS /api/drone-control`, bidirectional. Down: setpoints `{seq, t, frame: body|ned, vx_mps, vy_mps, vz_mps, yaw_rate_dps}` at 5–10 Hz, or discrete commands `{cmd: takeoff|land|rth|abort|mission_upload|mission_start|mission_pause|mission_resume|mission_stop|gimbal_set|photo|record_start|record_stop, params}` (the `mission_*` verbs are the GUIDED-N loop: upload/start on each click, pause/resume as hold/continue). Up: acks `{seq, applied, clamp?}` plus events (mode change, RC override, VS engaged/dropped). The APK runs the 25 Hz virtual-stick loop locally, interpolating between setpoints, and enforces a **TTL dead-man** (default 500 ms): no fresh setpoint → ramp to zero velocity → brake/hover. Stale or out-of-order sequence numbers are rejected. The APK stays dumb on purpose — it is a reflex arc, not a planner.

**Safety split:** the APK owns aircraft-side execution and failsafes (it holds the MSDK session and the VS heartbeat); VEIL owns planning and the envelope guard — no setpoint leaves VEIL unchecked (§1 invariant 6, §11.3). The RC pause button / mode switch is the hardware kill; its exact takeover semantics while VS is active are B2's deliverable.

**His gate B1:** verify `pushKMZFileToAircraft` / WPMZ actually works on the Mini 4 Pro before building mission control around it. If it doesn't (the Dronelink precedent suggests it may not), the bridge scopes to telemetry + monitoring, and **mission delivery stays on adb — which the APK cannot replace anyway**: a non-root app cannot write into another app's `Android/data`; only adb shell privileges can. The architecture already absorbs this outcome.

---

## 10. WS6 — Live video ingest (drone → BOOX → VEIL, decode on the computer)

**Requirement:** live drone video into VEIL with all decoding on the Linux box. **Design:** MediaMTX (single static binary, zero deps — matches VEIL's posture) as the video hub; two publishers matching the two operating modes; all consumers decode computer-side.

### 10.1 Mode A — DJI Fly is flying (available today, zero Android code)

- DJI Fly's built-in RTMP livestream → MediaMTX on the VEIL box. URL `rtmp://127.0.0.1:1935/drone` via `adb reverse tcp:1935 tcp:1935`, or the LAN IP directly.
- Honest caveat: DJI Fly decodes and re-encodes on the BOOX — that's how Fly's livestream works and it can't be bypassed while Fly owns the USB. The BOOX SoC's hardware codec handles 1080p fine (encoding is unrelated to the e-ink display). Latency ~2–5 s: **monitoring-grade, label it as such in the UI.**
- Browser display: MediaMTX serves WebRTC out (WHEP). `public/video.js` panel with a WHEP player — decoding happens in the browser on the computer, hardware-accelerated.

### 10.2 Mode B — bridge APK is connected (the true passthrough)

- MSDK V5's `ICameraStreamManager.addReceiveStreamListener` hands the APK the **still-encoded** H.264/H.265 O4 transmission stream. The drone already encoded it; the BOOX never decodes — it's a byte pump (contract in §9).
- APK → `tcp://127.0.0.1:8426` (adb reverse) → VEIL shim: one ffmpeg process in the managed-subprocess pattern, `-f h264 -i tcp://... -c copy -f rtsp rtsp://127.0.0.1:8554/drone` — a remux, **no transcode anywhere**.
- Same MediaMTX, same WHEP player, same panel. Latency target 300–600 ms glass-to-glass.

### 10.3 Consumers (all decode on the computer)

- `public/video.js` WHEP panel now; video as a texture in the twin later.
- Recording: MediaMTX's built-in recorder or an ffmpeg segmenter — nearly free, the stream is already encoded.
- CV tap (future): PyAV/ffmpeg pulls the RTSP leg → numpy frames → detections georeferenced onto the twin using the telemetry pose stream.

### 10.4 Reality checks

- The live feed is the **O4 transmission proxy (~1080p)**, not the 4K recording. Full-res capture for splats/orthos still comes off the SD card post-flight. Live = monitoring; SD = data. Don't conflate them.
- Codec: prefer H.264 where the stream is selectable — browser WebRTC support for H.265 is uneven. If the feed is H.265-only, the shim transcodes on the 4090 (NVDEC→NVENC, trivial load) — still "decode on the computer."
- **Gate G1b (piggyback on G1):** time a ~20 MB TCP transfer through the adb-reverse tunnel from the BOOX. Need sustained throughput ≥ 2× the stream bitrate (~12 Mbps → want ≥ 24 Mbps). If the tunnel disappoints, fall back to a direct LAN socket — same framing, different address.

**Acceptance:** Mode A — DJI Fly streaming into a live VEIL panel with config only. Mode B — glass-to-glass under ~600 ms with zero decode on the BOOX (verify: APK CPU stays low, no `MediaCodec` decoder instantiated).

---

## 11. WS7 — Piloting from VEIL (guarded virtual stick)

**Goal:** full pilot authority from the VEIL console — takeoff, fly, point the camera, shoot, land — with the twin acting as the safety system.

### 11.1 The premise

On the Mini 4 Pro, virtual stick is the *reliable* MSDK primitive (it's how Litchi flies this airframe); native waypoint push is the uncertain one (B1). Obstacle avoidance is **mode-dependent, not absent**:

- **Native waypoint missions:** OA active (field-evidenced on this exact airframe — brake mode intervenes mid-mission). Side-loaded DJI Fly missions already fly protected.
- **Manual sticks (with or without the bridge observing):** full APAS, stock behavior.
- **Virtual stick:** historically disabled, but DJI's V5 docs show obstacle sensing under VS is *firmware-dependent* and enabled on updated firmware for Mavic 3E/3M; guaranteed/documented only on enterprise airframes; **undocumented for the Mini 4 Pro → B2 answers it empirically** (query `IPerceptionManager` state while VS is engaged; controlled brake test). Until B2 says otherwise, treat OA under VS as absent.

Regardless of mode, vision OA is weakest against exactly what canopy tops are made of — thin branches, twigs, wires (the manual's own list; community crash reports confirm). So OA is **defense-in-depth, never the safety case**. The safety case is *minimum-safe-altitude enforcement*: TAWS/MSAW semantics — conservative floors over a terrain/canopy database of known, dated error (§12.4) — plus a geofence, with Airbus-style command clamping rather than error dialogs. The canopy database is presumed stale and undersampled until drone-derived surveys (§12) say otherwise.

### 11.2 Brain / reflex split

- **VEIL (brain):** reads pilot input, runs the envelope guard, emits setpoints at 5–10 Hz on the §9 control channel.
- **APK (reflex):** 25 Hz VS loop, interpolating between setpoints; **TTL dead-man** (500 ms default: no fresh setpoint → ramp to zero → brake/hover); executes discrete verbs (takeoff/land/RTH/gimbal/camera); reports mode and RC state. Deliberately dumb.
- **Hardware kill:** RC pause button / mode switch drops VS instantly — exact semantics per B2.
- Defense layers, in order: pilot judgment → VEIL envelope guard → APK TTL → aircraft obstacle braking (counted only if B2 verifies it under the active mode) → RC hardware kill → aircraft's own failsafes (RTH, low-battery).

### 11.3 Envelope guard (reuses WS2 wholesale)

- Same floor math: `floor(s) = DSM(s) + clearance`. The guard forward-simulates each candidate setpoint ~3 s ahead (current position + commanded velocity + braking margin), clamps vertical rate on floor approach, applies horizontal stop-before-fence against the property polygon, and enforces per-mode speed/range caps. **Clamp, don't reject** — envelope-protection semantics. Every clamp is logged and displayed.
- **v2 hardening:** at session start VEIL pushes a precomputed floor grid (tiny raster — min safe relative height per 5 m cell) to the APK, so the reflex layer enforces the floor even during TTL hold or if VEIL dies mid-command.
- **Honest limit:** the error stack is bigger than GPS alone. The public-lidar canopy surface is multi-year stale (canopy grows ~0.3+ m/yr — meters since acquisition), ~2 pts/m² undersamples thin emergents and snags, and no dataset knows about wires or fresh deadfall; the ground-based Livox scan sees trunks well but canopy tops poorly (occluded from below); the drone adds ±1.5 m horizontal / ±0.5 m vertical GPS plus baro drift. Floors therefore carry **vintage-dependent buffers** (§12.4; never below +10 m over the best surface) and the fence is inset ≥ 10 m from the property line. Capability is scoped as *confident above-canopy piloting*, never threading trees. Buffers tighten only as drone-derived surfaces (§12) replace public ones — and never below the hard minimum.

### 11.4 Control modes (ascending authority)

Explicit state machine — `IDLE / MANUAL / MISSION / GUIDED-N / GUIDED-VS / DIRECT / ABORT / RTH` — mode always on screen, every transition logged.

- **MANUAL:** pilot flies sticks; VS disengaged; aircraft in its normal regime with **full APAS active**; the bridge is a passive observer feeding VEIL telemetry, video, and live twin position. Expected workhorse for capture sorties — you fly, VEIL watches. (Note: with the bridge foregrounded, DJI Fly's UI is unavailable; the pilot flies VLOS with VEIL as the instrument panel.)
- **MISSION:** native waypoint execution (side-loaded or B1-pushed); aircraft mission engine flies with **OA active**; VEIL monitors progress.
- **GUIDED-N (B1-contingent):** click-to-fly as *micro-missions* — Garmin **Direct-To** semantics: each click generates a native mission, pushes it, starts it, and the aircraft's own mission engine flies it with **OA active**. Not a naive 2-point line: the generator runs the WS2 corridor check and inserts intermediate waypoints where a straight line would bust the floor — floors enforced at *plan* time, sensing active at *fly* time, two independent layers. Verbs: go-there (preemption: stop current → visible `REPLANNING` hover → push → start; the aircraft never moves without a validated committed plan), hold (pause), continue (resume), abort. Latency is per-*click* (upload seconds), not per-second — guided flying issues sparse commands, so the profile fits. Link-loss grace: native legs execute on-board, so a dropout mid-leg means the aircraft finishes its committed plan with defined failsafes rather than braking into an ambiguous hover. Ship this variant first if B1 passes.
- **GUIDED-VS:** click the twin → VEIL computes a floor-safe path → streams setpoints. Instant response; OA status per B2's verdict. "Go there," "orbit this," "hold here."
- **DIRECT:** gamepad or keyboard via the browser Gamepad API in `flight.js` → body/NED velocity setpoints under the guard. Speed-capped by mode (default ≤ 3 m/s over canopy — at that speed, 600 ms of video latency is ~1.8 m of travel against a ≥15 m floor).
- **Not offered:** aggressive low-latency FPV. 300–600 ms video is outside the envelope by design, not by omission.
- **Discrete verbs:** takeoff (climbs to floor + clearance before accepting lateral input), land (confirmation gate), RTH (preloaded with the engine's `rth_min_rel_m`; retains the aircraft's own return-path obstacle behavior), gimbal pitch, photo, record start/stop.

### 11.5 Flying the twin (the UX inversion)

Telemetry arrives at ~100–200 ms with full 360° context; video arrives at ~300–600 ms and only looks forward. So the **twin is the primary flight display and the video is confirmation** — inverted from normal FPV. Chase-cam or top-down over the real splats/mesh, live drone marker with commanded-vs-actual velocity vectors, floor surface ghosted, fence rendered, HUD showing AGL-over-DSM, floor margin, battery, mode, link ages (setpoint / telemetry / video), and clamp indicators. Synthetic vision where the synthetic terrain is survey-real.

### 11.6 Test cards (incremental, aviation-style)

- **TC0 — bench:** MSDK simulator if B2 finds it works on this airframe; otherwise props-off VS engage/disengage and TTL verification.
- **TC1 — open field, low hover:** gentle DIRECT inputs; kill VEIL mid-command → verify brake within TTL; exercise the RC pause takeover.
- **TC2 — open field, synthetic hazards:** virtual fence and artificial floor placed in the open; command through them → verify clamps behave exactly as designed. **TC2b:** with B2's `IPerceptionManager` readout in hand, a controlled slow VS approach toward a soft obstacle at safe altitude — record whether braking triggers, per firmware version.
- **TC3 — above canopy:** GUIDED sorties with generous clearance (floor + 25 m), then normal ops.
- VLOS applies throughout per Part 107 — station the console within sight of the ops area or crew a visual observer.

**Acceptance:** TC0–TC2 pass; then one full GUIDED sortie — takeoff → three click-to-fly legs → photo → RTH → land — flown entirely from the VEIL console with zero RC input and zero envelope violations.

---

## 12. WS8 — Drone → VEIL ingest (the primary loop)

The twin is the accumulating asset; the drone is a sensor that updates it. Everything in §§4–11 exists to make this loop run safely. Three sub-streams, cheapest first.

### 12.1 Track ingest (startable today)

- **SRT importer** (promoted from backlog): DJI embeds a per-frame GPS/altitude/camera-settings track as subtitles alongside recorded video. `scripts/ingest_srt.py` parses SRT → `flight_track:<id>` store entities (journaled, dated) → rendered over the twin by the existing live-overlay path. Runs against footage **already on disk** — no new stack required, so this starts day 0 and puts the first drone-derived data into the twin before anything else ships.
- Mode B telemetry persists to the same `flight_track` kind. Planned-vs-flown comparison becomes a pure derivation once both exist.

### 12.2 Media ingest (post-flight)

- Import from SD/USB: EXIF-geotagged photos → `photo_obs:<id>` entities carrying pose (position, gimbal pitch, heading) and an estimated ground footprint; video linked to its track with frame indexing. The twin becomes browsable evidence: click a point, see every photo that ever looked at it.

### 12.3 Surface refresh (the loop proper)

- Grid missions (WS3 generator) → downward capture → photogrammetry (ODM first; the splat pipeline as the quality path) → **dated** DSM/canopy layers written to the store with acquisition date, method, and coverage polygon. Layers never overwrite; they stack with provenance.

### 12.4 The confidence loop (how floors are allowed to tighten)

Clearance is a function of the *vintage and provenance* of the best surface at each point — not a constant:

- Public lidar only (multi-year vintage): floor = surface + 20 m + growth allowance
- Drone-derived surface < 12 months old: floor = surface + 15 m
- Drone-derived surface < 3 months old: floor = surface + 10 m (**hard minimum; never lower**)

This is the mechanism by which *updating VEIL with drone data* is what eventually and safely tightens *updating the drone with VEIL data* — and the only such mechanism. No survey, no tightening. The numbers are policy defaults in one config block, reviewed after the first refresh cycle.

**Acceptance:** one grid sortie produces a dated canopy layer visible in the twin; the next mission's clearance report cites that layer and its vintage; an SRT-imported historical flight renders over the twin.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| DJI Fly/firmware update breaks the overwrite trick | Re-run G3 after every DJI Fly update before trusting the pipeline; backups from §8.3 restore prior state; ManualTransport always ships |
| adb blocked from `Android/data` on some device/build | G1 per device; transport interface means the fallback is a picker choice, not a redesign |
| Wrong-slot overwrite (stale dummy) | Fresh-window guard + explicit `--guid` + read-back verify + waypoint-count confirmation in DJI Fly preview |
| Dummy creation requires connected aircraft | Batch-create slots each field session (SLOT-A/B/C convention); documented |
| CHM immature → bad canopy floor | `--canopy-const` conservative fallback; clearance defaults err high |
| B1 fails (no MSDK waypoint push on Mini 4 Pro) | Bridge = telemetry only; delivery stays adb; no architectural change |
| BOOX quirks (AOA support, GPS absence, e-ink refresh, cold) | G4 checks each explicitly; any Android substitutes; note e-ink slows dramatically below ~0 °C — winter ops may want a conventional phone |
| Takeoff-pin elevation error propagates to every waypoint | Pin is mandatory; DEM(takeoff) displayed prominently; docs require launching from the pinned spot |
| Mode A RTMP latency (2–5 s) mistaken for a control-grade feed | UI labels Mode A "monitoring only"; Mode B is the low-latency path |
| H.265 transmission stream vs. browser WebRTC support | Prefer H.264 where selectable; else shim transcodes on the 4090 — decode stays computer-side |
| adb-reverse tunnel throughput/jitter under ~12 Mbps video | G1b probe; fall back to direct LAN socket with identical framing |
| OA under VS undocumented on this airframe (enterprise-guaranteed only; firmware-dependent elsewhere) | B2 characterization per firmware version; floors remain the safety case in every mode; GUIDED-N preserves native-mission OA where latency permits; OA counted as a defense layer only after B2 verifies it |
| Canopy surface stale / undersampled (public-lidar vintage, growth, snags, wires) | Vintage-dependent buffers (§12.4) with a hard minimum; WS8 surface refresh is the only mechanism that tightens them; growth allowance on any surface older than a season |
| GPS (meters) vs. twin (decimeters) registration gap | Buffered floors/fences (+5 m vertical / +10 m horizontal); capability honestly scoped as above-canopy, never tree-threading |
| Network partition mid-DIRECT | APK TTL brake-to-hover ≤ 500 ms; RC pause hardware kill; RTH executable from the APK alone; v2 floor grid keeps terrain protection alive without VEIL |
| RC takeover semantics under VS unverified | B2 completes before any airborne VS test; TC1 exercises it live |

---

## 14. Sequencing

1. **Day 0:** Freeze §1 invariants + §4.2 schema. Send §9 to the bridge dev with B1 flagged. Run **G1** on the BOOX.
2. **Immediately, no hardware needed:** WS1 (store) + WS2 (altitude engine) in parallel — both pure backend against existing rasters. **WS8.1 (SRT importer)** starts here too: it runs against footage already on disk and lands the first drone-derived data in the twin before anything flies.
3. **First field session:** G2 (harvest fixtures + create slots) + G3 (overwrite proof) + G4 (BOOX viability). ~One session covers all three.
4. **After G2:** WS4 against harvested fixtures (golden-file tests). After G1: WS5 side-load transport.
5. **WS3 UI**, then integrate: full plan → check → preview → push → fly loop, dogfooded on the parcel.
6. **Video Mode A:** any time after step 1 — MediaMTX drop-in + DJI Fly RTMP config + the `public/video.js` WHEP panel. Config-heavy, code-light; a good early demo and it exercises the exact viewer path Mode B will reuse. **Video Mode B** rides the bridge APK timeline — the §9 video addendum goes to the bridge dev with the rest of the contract.
7. **WS8.2–12.4 ingest loop:** media ingest after the first field session; the first surface-refresh grid sortie as soon as WS3's generator and WS4 export are live. The confidence loop activates with the first dated layer.
8. **P2 backlog:** Litchi CSV emitter (parked pending the virtual-stick-vs-native question on Litchi Pilot), orbit/reveal generators, iOS path (`wpmz.sqlite3` — parked), mission splitting across batteries.
9. **WS7 piloting:** last, deliberately — after Mode B video plumbing exists (control shares the §9 channels and adb-reverse infrastructure), and ideally after the first §12.4 surface refresh so guided modes fly against a dated drone-derived floor rather than public lidar. **GUIDED-N if B1 passed** (else GUIDED-VS per B2's OA verdict), DIRECT last, strictly gated behind B2 and TC0–TC2. The guard reuses the WS2 floor engine wholesale.

**Operational notes for the docs (`docs/flight-planning.md`):** wireless-debugging pairing walkthrough; slot convention; pre-launch checklist (set RTH ≥ engine's `rth_min_rel_m`, confirm waypoint count in Fly preview, launch from the pinned takeoff point, VLOS throughout).
