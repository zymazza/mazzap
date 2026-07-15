# DJI Fly private-feature API feasibility

Audit date: 2026-07-15

Scope: DJI Mini 4 Pro, DJI RC-N2, DJI Fly for Android, and the existing
Mini 4 MSDK bridge. This was a read-only investigation. It did not install DJI
Fly, alter the BOOX, start an Android component, query an app provider, bypass
AppGuard/authentication/DRM, transmit an aircraft command, or modify the bridge.

## Executive result

DJI Fly contains a private consumer waypoint stack that the Mini 4 Pro can use
even though the Mini 4 product profile in Mobile SDK 5.18 does not expose
`WaypointMissionManager`. The official DJI Fly APK contains WPMZ/KMZ parsing,
private Waypoint V3 upload/start/pause/resume/stop code, a local waypoint Room
database, mission-library UI, and cloud-sync code. This is strong evidence that
the native DJI Fly waypoint feature is implemented through private app and
aircraft interfaces. It is not a supported external API, and the presence of
generic native symbols does not by itself prove that every symbol is enabled
for Mini 4 Pro.

There is no clean external entry point to that stack. The mission-library
activity is not exported, no waypoint-specific service/provider/intent is
exported, DJI documents no route import/export, app backup is disabled, and the
release is protected and non-debuggable. The exported media and generic
cross-component providers are not evidence of a flight-control interface.

The practical architecture is therefore two mutually exclusive backends:

1. Keep the public MSDK bridge as the production backend for telemetry, main
   camera, camera/gimbal controls, perception distances, intelligent-flight
   modes that DJI supports, and Mac-resident Virtual Stick routes.
2. Add an experimental DJI Fly UI backend on a separate supported Android
   phone/profile. It can observe and, in carefully allowlisted states, operate
   DJI Fly through the accessibility/UI layer. This can reach native waypoint
   flight and its firmware obstacle behavior, and can relay the currently
   selected Vision Assist view by screen capture. It must not be represented as
   a stable unattended flight API.

DJI Fly and an MSDK app cannot be simultaneous RC-N2 clients. DJI's release
notes explicitly say DJI Fly does not support switching with an MSDK app. A
backend transition must be a deliberate, grounded, motors-off USB handoff.

## Environment and artifact inventory

The connected BOOX is a NoteAir5C running Android 15/API 35 at
`<boox-lan-ip>:5555`. Its only DJI package is the existing bridge:

```text
com.dji.sampleV5.aircraft
```

`dji.go.v5` is not installed. A read-only external-storage search found no DJI
Fly waypoint, mission, or flight-record remnants. Consequently, live UI
hierarchy, app-private storage, and runtime IPC could not be inspected.

The static artifact was downloaded from DJI's official Android distribution
endpoint linked by the DJI Fly download page. It was kept under `/tmp`, not
installed or added to the repository:

```text
Package:              dji.go.v5
Version:              1.21.4 (versionCode 3115357)
Min/target/compile:   24 / 34 / 34
Native ABI:           arm64-v8a only
APK SHA-256:          26649c10483a090ca184fefe70b3694e5cdb72b538718d502dc1419e706e0a03
Signer DN:            CN=dji
Signer cert SHA-256:  01a42c94aa87020f41e8c252598df98b86a0acf6c05ff61c75f18fa29cad1dd4
```

The version, hash, and certificate are evidence fingerprints, not a
recommendation to redistribute the APK.

## Static APK findings

### Protection and packaging

The application class and component factory are AppGuard classes. The manifest
sets `allowBackup=false` and does not set the debuggable flag. The APK has one
small stub DEX, 205 arm64 native libraries, and a 165 MB `libdatajar.so` that
contains much of the Java/Kotlin class metadata and strings. This makes ordinary
debugger attachment or Java decompilation a poor production strategy. No
AppGuard or encryption bypass was attempted.

Manifest totals are 245 activities, 26 services, 25 providers, and 12
receivers. DJI Fly declares no accessibility service of its own.

Relevant externally reachable components are:

- `DJIPureLaunchActivity`, the normal launcher;
- `DJIAoaActivity`, exported for the Android USB-accessory attachment;
- `SchemeUrlActivity`, exported for the `djifly://linkapi` scheme;
- `UAVPushMessageActivity`, an exported push/deep-link handler;
- `UAVMediaProvider`, exported as `dji.go.v5.provider.media`;
- Billy CC's generic `RemoteProvider` and `RemoteConnectionActivity`.

These do not expose an evidenced mission API. `UAVMediaProvider` is named and
packaged as playback/media infrastructure. Billy CC is a generic internal
cross-component framework; an exported framework transport is not proof that a
waypoint component has been registered for external callers. It must not be
probed on a flight device without a separate review.

`com.uav.waypoint.missionlib.MissionLibActivity` has no exported flag and no
intent filter. It is therefore not externally launchable on this target. No
waypoint-specific exported provider, receiver, service, deep link, or declared
Binder permission was found.

### Private waypoint stack

`libwpmz_jni.so` exposes readable symbols including:

```text
native_GetWaylineMission
native_GetWaylineMissionConfig
native_GetWaylineTemplates
native_GetWaylines
native_CheckWPMZValid
native_GenerateWaylineTrajectory
```

It parses `wpmz/template.kml` and `wpmz/waylines.wpml`, with WPML fields for
mission configuration, waypoints, speed, heading, gimbal, payload/camera
actions, lost-link behavior, and finish behavior.

`libsdk_jni.so` contains private Waypoint V3 entry points including:

```text
UploadKMZFile / CancelUploadKMZFile
V3StartMission / V3StartWayline / V3StopMission
V3InterruptWayline
V3ResumeFromBreakPoint / V3ResumeFromDeterminedPoint
```

`libdatajar.so` contains Fly-side classes such as:

```text
WaypointCenterManager
WaypointService / IWaypointService
WaypointCapabilityChecker
WaypointRunningAdapter / WaypointSyncAdapter
WPMZRepository / WaypointDBRepository
WpRoomDatabase / WpDao
MissionLibActivity
```

The recovered Room schema includes `way_point_main_table` with a mission UUID,
file path, name, author, timestamps, preview-image paths, start position,
waypoint count, duration, voyage, sync/delete timestamps, and point positions.
Cloud code references mission check/compare/get endpoints. This aligns with
DJI's documentation that routes are app-local and, outside the United States,
can be cloud-synced in DJI Fly 1.17 and newer.

This proves that a private storage and upload pipeline exists. It does not
provide a supported calling convention, storage path, capability negotiation,
or stable authorization boundary.

## Feature reachability

| Desired feature | Public MSDK backend | DJI Fly UI backend | Direct private API prospect |
|---|---|---|---|
| Native/onboard waypoint route | Mini 4 profile does not expose it | Reachable through Fly's documented waypoint UI | Private code exists; no external endpoint |
| Waypoint camera/gimbal actions | Public controls exist separately, but the current Mac route is navigation-only and does not schedule them | Native Fly route supports photo, record, heading, POI, gimbal, zoom, and hover settings | Same private waypoint pipeline; unstable |
| Obstacle braking/bypass during a route | Perception data is public, but Mini 4 is not documented to retain automatic avoidance under Virtual Stick | Fly native waypoints use the selected Bypass/Brake setting | Firmware/private behavior, not safely reproducible client-side |
| Perception distances/status | Supported and structured through `IPerceptionManager` | Visible in Fly overlays | Prefer MSDK; no reason to reverse this |
| Main imaging camera | Current bridge relays channel 0; controls are public | Visible in Fly | Prefer MSDK |
| Camera/gimbal settings | Broadly public; runtime range keys must be honored | Additional Fly modes/settings can be operated in UI when actually shown | UI fallback only after capability enumeration |
| Vision Assist | No supported Mini 4 stream in the current bridge | Selected front/back/left/right grayscale view can be screen-relayed | No demonstrated raw stream endpoint |
| All raw obstacle-camera feeds | Not public | Not shown concurrently; only the selected assist view | Not presently reachable without unsupported hooks/protocol work |
| POI/Spotlight/SmartTrack | Officially supported by MSDK | Available in Fly | Prefer MSDK |
| Advanced RTH planned path | RTH start/status is public; private planned path is not | Fly displays and executes it | Firmware/private algorithm; UI observation only |
| Flight/device/battery/RC health | Broad structured MSDK keys and listeners | UI gives a human summary | Prefer MSDK |
| Historical records/logs | Not the primary live path | DJI documents export workflows | Batch import, not a live-control API |

DJI documents Vision Assist as a selected 566x424, 10 fps view powered by the
horizontal vision system. It can show forward, backward, left, or right, remains
available when the obstacle-avoidance action is Off, and stops when motors stop.
DJI explicitly says its footage cannot be downloaded. Screen capture can relay
what the operator sees; it cannot honestly be labeled a raw vision-camera feed,
nor can it expose every direction concurrently.

DJI documents APAS 5.0 and omnidirectional bypass for Mini 4 Pro in DJI Fly.
That is different from MSDK Virtual Stick: DJI's Virtual Stick API currently
lists only M300/M350, M30, Mavic 3E, and Mavic 3M as retaining obstacle
avoidance in virtual-joystick mode. The safest useful split is native DJI Fly
waypoints when aircraft-resident avoidance is required, and public perception
telemetry plus conservative stop/advisory logic for the custom MSDK controller.

## Approach assessment

### 1. Accessibility, UiAutomator, or Appium

Feasibility: medium for observation and simple navigation; low-to-medium for
reliable route construction; unsuitable as an unattended production pilot.

Advantages:

- works above DJI's protection boundary and does not require private-code
  decryption;
- can use only controls visible to the operator;
- can capture waypoint state, APAS selection, warnings, and Vision Assist;
- can fail closed when the expected screen is not present.

Limits:

- map, video, and custom-rendered surfaces may not expose semantic UI nodes;
- coordinates, view IDs, dialogs, locale, resolution, and layout change across
  releases;
- pinning geographic coordinates through map gestures requires calibration and
  a human-verifiable route review;
- a successful tap proves only that input was delivered, not that DJI Fly or
  the aircraft accepted the operation;
- Appium/UiAutomator2 installs host tooling and on-device helper packages, so it
  belongs on a dedicated lab phone, never the qualified flight device by
  default.

A production-shaped implementation would use a user-enabled accessibility
service for semantic observation/allowlisted actions and Android
MediaProjection for screen video. DJI Fly remains foreground. Every action is
guarded by package/version/signer, locale, resolution, foreground activity,
screen fingerprint, aircraft state, and an explicit confirmation policy.

### 2. Intent, content-provider, or Binder IPC

Feasibility: low.

There is no waypoint-specific exported component. The `djifly://linkapi` deep
link and push handlers are not evidenced route transports. The media provider
is not a mission provider. The generic Billy CC provider might bridge internal
components, but static presence does not establish an externally callable
waypoint service or authorization contract. Treat this as an observation target
for a sacrificial lab only, not an API assumption or an invitation to fuzz it.

### 3. Route-file/database injection

Feasibility: medium as a rooted research experiment; low and unsafe on a stock
production device.

The KMZ/WPML file plus Room row is a promising architectural seam, but DJI does
not document import/export. `allowBackup=false`, Android app-private storage,
possible SQLCipher use, previews/metadata, migrations, checksums, capability
checks, and cloud state all need to agree. A malformed or partially registered
mission could fail late at upload or execution. No file should be injected into
a flight profile. If later authorized, observe before/after saves on a rooted,
disposable, user-owned lab device with no RC or aircraft connected; do not
bypass AppGuard, account authentication, or storage encryption.

### 4. Runtime hooking/instrumentation

Feasibility: low-to-medium in a rooted lab; high maintenance and inappropriate
for the flight build.

The release is protected, non-debuggable, and heavily native. Root, repackaging,
or a gadget would change the trust/integrity boundary and may trigger defenses
or violate contractual/legal restrictions. Observation-only hooks could reveal
method arguments and file paths in a legally approved lab, but any AppGuard,
authentication, or cryptographic bypass is outside this plan.

### 5. O4/RC/private protocol emulation

Feasibility: very low; risk: extreme.

Private Waypoint V3 symbols show that DJI Fly can talk to the stack, but they do
not reveal the complete RC-N2/O4 session, activation, signing/encryption,
capability negotiation, transport arbitration, or firmware state machine.
Reimplementing it would be firmware-fragile, could create unsafe commands, and
would require a separate legal and safety program. It should be the last option
and is likely a no-go.

## Recommended unified API

```text
Mac dashboard / client
          |
          v
CapabilityBroker + SafetySupervisor
   |                          |
   v                          v
MsdkBackend              DjiFlyUiBackend
(production)             (experimental)
   |                          |
BOOX + MSDK              supported Android phone + DJI Fly
   \________________ mutually exclusive RC-N2 USB lease __/

FlyLabProbe (static/runtime observation) is a separate research build and is
never linked into or reachable from the flight service.
```

Suggested surface:

```text
GET  /v1/capabilities
GET  /v1/backend
POST /v1/backend/prepare-handoff       grounded + local confirmation only
GET  /v1/state
WS   /v1/telemetry
GET  /v1/video/main
GET  /v1/vision-assist                 screen-derived and labeled experimental
GET  /v1/routes
POST /v1/routes/stage                  never starts a mission
POST /v1/actions/request               creates a pending, policy-checked intent
POST /v1/actions/{id}/confirm          requires configured human confirmation
```

Every capability record should include:

```json
{
  "supported": true,
  "source": "msdk_public | fly_ui | firmware_inferred",
  "confidence": "high | medium | low",
  "requires_foreground": false,
  "requires_human_confirmation": false,
  "mutually_exclusive_backend": null,
  "version_fingerprint": "..."
}
```

Every action result must distinguish:

```text
request_validated
input_delivered
ui_state_observed
app_acceptance_observed
aircraft_state_observed
physical_effect_observed
```

Never translate `input_delivered` into “flight command succeeded.”

The safety supervisor owns one RC lease. It permits backend changes only while
motors are stopped, no mission is active, both sides report disconnected, and a
local operator confirms the physical USB handoff. A Fly UI version/signature or
screen mismatch disables actions and leaves screen observation only. Mission
`GO`, takeoff, landing, and motor actions are not exposed during the initial
proof of concept.

## Staged proof of concept

0. **Static inventory — complete.** Preserve the APK fingerprint, manifest
   inventory, private-waypoint evidence, and current BOOX package inventory.
1. **Dedicated lab phone.** After explicit approval, use a DJI-supported
   Android phone/profile and install the official APK only. Do not replace the
   BOOX bridge. Keep the RC and aircraft disconnected.
2. **UI map.** Capture screenshots and UI hierarchies for home, settings,
   waypoint library/editor, camera settings, connection states, and all dialogs.
   Record which controls have semantic accessibility nodes and which require
   image recognition. No automated taps yet.
3. **Offline waypoint observation.** DJI officially permits map planning without
   an aircraft. Create a disposable route manually, observe UI/state changes,
   and test read-only extraction of its name, count, route image, and settings.
4. **Observer API.** Relay foreground package/activity, exact APK fingerprint,
   connection status, visible APAS setting, waypoint-library summaries, screen
   frames, and UI confidence. Do not issue input.
5. **Grounded connection characterization.** With propellers removed, motors
   stopped, and no flight commands, connect RC-N2 and aircraft. Map grounded
   connection/settings states. Vision Assist cannot be dynamically qualified
   here because DJI says it ends with motors stopped.
6. **Allowlisted ground UI.** Permit navigation and benign setting edits only
   after the expected before-state and a local confirmation. Re-read the UI and
   verify the after-state. Exclude waypoint upload/GO, takeoff, landing, RTH,
   motor, and control-stick input.
7. **Waypoint staging.** Automate create/edit/save only. Generate a route-review
   artifact showing every waypoint/order/altitude/action and the Fly-rendered
   map. Require operator comparison and approval. Do not upload or start it.
8. **Supervised flight qualification.** Only after a separate test plan and
   explicit authorization, test one minimal route in a legal open area with a
   pilot in visual line of sight and the RC pause control ready. The human, not
   the API, initiates `GO` in the first flight phase; the API observes progress.
9. **Escalation only if UI proves insufficient.** A rooted sacrificial device may
   observe app-private filename/database changes with no aircraft attached. Stop
   at any AppGuard, authentication, or encryption boundary. Protocol emulation
   remains out of scope absent a new decision.

## Versioning, legal, and operational risks

- Pin package name, versionCode/versionName, signer certificate, APK hash,
  locale, display metrics, and every allowed screen fingerprint.
- Re-run the manifest/static scan and the full no-aircraft UI suite after every
  DJI Fly update. No action capability carries forward automatically.
- Expect region/account differences: DJI says waypoint cloud sync is unavailable
  in the United States, and server/account state may alter the UI.
- UI automation has no source or binary compatibility promise. Maintain it as a
  separately deployable adapter with a kill switch, not bridge core logic.
- DJI Fly may require foreground execution and Internet/account/activation
  state. Never attempt to bypass those controls.
- Reverse engineering, automation, and private interfaces may be restricted by
  DJI terms or applicable law. Obtain appropriate legal review before runtime
  instrumentation or redistribution.
- Aviation rules, visual-line-of-sight requirements, Remote ID, geofencing, and
  a human pilot's responsibilities are unchanged by an API.

## Reproducible read-only evidence commands

Current BOOX inventory:

```bash
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
ADB="$ANDROID_SDK_ROOT/platform-tools/adb"
D=<boox-lan-ip>:5555
"$ADB" -s "$D" shell getprop ro.product.model
"$ADB" -s "$D" shell getprop ro.build.version.release
"$ADB" -s "$D" shell getprop ro.build.version.sdk
"$ADB" -s "$D" shell pm list packages -f -3 | rg -i 'dji|fly|sampleV5'
"$ADB" -s "$D" shell dumpsys package dji.go.v5
```

Official APK inventory, using a locally downloaded artifact in `/tmp`:

```bash
APK=/tmp/DJI-v1.21.4-official-sec.apk
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
BUILD_TOOLS_VERSION=35.0.1
AAPT="$ANDROID_SDK_ROOT/build-tools/$BUILD_TOOLS_VERSION/aapt"
APKSIGNER="$ANDROID_SDK_ROOT/build-tools/$BUILD_TOOLS_VERSION/apksigner"

shasum -a 256 "$APK"
"$AAPT" dump badging "$APK"
"$APKSIGNER" verify --print-certs "$APK"
"$AAPT" dump xmltree "$APK" AndroidManifest.xml
zipinfo -1 "$APK"
unzip -p "$APK" lib/arm64-v8a/libwpmz_jni.so | strings -a
unzip -p "$APK" lib/arm64-v8a/libsdk_jni.so | strings -a
unzip -p "$APK" lib/arm64-v8a/libdatajar.so | strings -a
```

After an approved install on the dedicated lab phone, the first pass remains
read-only and host-output-only:

```bash
"$ADB" -s "$D" shell pm path dji.go.v5
"$ADB" -s "$D" shell dumpsys package dji.go.v5
"$ADB" -s "$D" shell cmd package resolve-activity --brief dji.go.v5
"$ADB" -s "$D" shell dumpsys activity activities
"$ADB" -s "$D" exec-out uiautomator dump /dev/tty
"$ADB" -s "$D" exec-out screencap -p > /tmp/dji-fly-screen.png
```

Do not use `pm clear`, `am force-stop`, installation/uninstallation, `input`,
provider queries, deep-link launches, or app-private file writes on the flight
profile without a separately approved test step.

## Primary references

- [DJI Fly official download page](https://www.dji.com/downloads/djiapp/dji-fly)
- [DJI Mini 4 Pro support and official feature list](https://www.dji.com/support/product/mini-4-pro)
- [DJI Mini 4 Pro user manual v1.4](https://dl.djicdn.com/downloads/DJI_Mini_4_Pro/20240627/DJI_Mini_4_Pro_User_Manual_en.pdf)
- [DJI Fly Waypoint Flight instructions](https://repair.dji.com/help/content?customId=01700007343&documentType=&lang=en&paperDocType=ARTICLE&re=US&spaceId=17)
- [DJI Vision Assist instructions](https://repair.dji.com/help/content?customId=01700008741&lang=en&paperDocType=ARTICLE&re=US&spaceId=17)
- [DJI APAS instructions](https://repair.dji.com/help/content?customId=en-us03400006561&documentType=artical&lang=en&paperDocType=paper&re=US&spaceId=34)
- [DJI Virtual Stick API limitations](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager.html)
- [DJI MSDK release notes and app-switching limitation](https://developer.dji.com/doc/mobile-sdk-tutorial/en/?pbc=D3IDBfR5&pm=custom)
- [Android exported-component behavior](https://developer.android.com/privacy-and-security/risks/android-exported)
- [Appium UiAutomator2 setup](https://appium.io/docs/en/latest/quickstart/uiauto2-driver/)
