# DJI Mini 4 Pro + RC-N2 MSDK 5.18 capability audit

Audit date: 2026-07-14; implementation status updated 2026-07-15

Scope: DJI Mini 4 Pro, DJI RC-N2, Android Mobile SDK 5.18.0, and the
`android/dji-mini4-bridge` project. This audit uses DJI's official MSDK
documentation and the capability manifests and public bytecode packaged in the
locally resolved 5.18.0 SDK. It does not assume that an API present in the
generic SDK is supported by this aircraft.

## Executive result

The supported path to programmable flight on this combination is **Virtual
Stick**, not DJI's onboard KMZ waypoint interface.

MSDK 5.18 contains `WaypointMissionManager`, including KMZ upload, start,
pause, resume, stop, breakpoint, and progress APIs. The Mini 4 Pro's 5.18
product capability manifest does not expose the `waypointMission` manager at
all. Enterprise aircraft manifests in the same AAR do expose it. The public
waypoint state enum explicitly includes `NOT_SUPPORTED` for aircraft without
Waypoint Mission 3.0. Therefore those generic waypoint methods must be treated
as unsupported on Mini 4 Pro unless DJI changes the product manifest in a
future SDK.

This also means there is no supported way to load, edit in place, or take over
a DJI Fly consumer waypoint plan from an MSDK application. DJI's documented
KMZ workflow is based on WPML/Pilot 2, and DJI's release notes specifically say
DJI Fly does not support app switching with an MSDK app. The BOOX MSDK bridge
and DJI Fly should be treated as mutually exclusive RC-N2 clients.

The implemented route system is:

1. Plan and edit WGS84 routes on the Mac.
2. Give a complete, versioned `veil.route-revision.v1` document to the retained
   Mac JSON-lines flight session.
3. Compute route guidance from fresh aircraft location/attitude on the Mac at
   20 Hz and send authenticated body-velocity setpoints to the BOOX.
4. Have the thin Android bridge publish the latest setpoint to DJI advanced
   Virtual Stick at 20 Hz and report matched packet acknowledgements/telemetry.
5. Keep atomic revision acceptance, route progress, pause, resume, abort, and
   manual preemption in the Mac session without reacquiring flight authority.

This is physically capable of full commanded flight, but it is not equivalent
to an aircraft-resident waypoint mission: it depends on the Android/RC link and
the Mac flight session, it has no RTK, and DJI does not list Mini 4 Pro among
the aircraft that retain obstacle avoidance in Virtual Stick mode.

## Version and evidence

DJI's 5.18 release notes list the tested/current combination as:

- Mini 4 Pro aircraft firmware `01.00.1100`
- RC-N2 firmware `01.01.0300`

The local 5.18 product manifest says Mini 4 Pro support starts at MSDK 5.13.0
and gives `01.00.0450` as the product-level minimum firmware. The release-note
version is the safer qualification target; the lower manifest value explains
why earlier supported firmware can still enumerate.

Local capability source:

```text
dji-sdk-v5-aircraft-5.18.0.aar
  assets/ProductCapability/DJIMini4Pro/DJIMini4ProCapability.json
  assets/ProductCapability/DJIMini4Pro/DJIMini4ProFlightControllerCapability.json
  assets/ProductCapability/DJIMini4Pro/DJIMini4ProCameraCapability.json
  assets/ProductCapability/DJIMini4Pro/DJIMini4ProGimbalCapability.json
  assets/ProductCapability/DJIMini4Pro/DJIMini4ProBatteryCapability.json
  assets/ProductCapability/DJIMini4Pro/DJIMini4ProAirLinkCapability.json
  assets/ProductCapability/RemoteController/DJIRCN2Capability.json
```

The distinction used throughout this audit is:

- **Declared supported:** present in the Mini 4/RC-N2 capability files or
  explicitly listed for Mini 4 Pro by DJI's official documentation.
- **Generic only:** public class or key exists in the 5.18 SDK, but the Mini 4
  capability files do not declare it.
- **Internal:** callable bytecode/native surface not in DJI's supported public
  API contract. The current raw channel-0 video relay is in this category.

## Feature matrix

| Feature | Mini 4 Pro + RC-N2 result | Supported surface / consequence |
|---|---|---|
| Route planning | Custom planning supported; DJI WPMZ editing library exists, but its output cannot be executed through the Mini 4 MSDK product profile | Use a Mac-owned route schema; do not use KMZ as the bridge contract |
| KMZ upload | Not declared supported | Do not expose `pushKMZFileToAircraft` |
| Onboard mission start/pause/resume/stop | Not declared supported | Do not expose `WaypointMissionManager` controls |
| Onboard waypoint/action progress | Not declared supported | Compute custom-route progress from fresh location/attitude and Mac executor state |
| Mid-flight route edit/replan | No onboard waypoint editing API; implemented by the Mac Virtual Stick executor | Atomically replace a complete revision immediately or at a waypoint boundary; this is not an onboard edit |
| Intelligent flight | POI, Spotlight, and SmartTrack are officially supported; FlyTo is not | These are separate intelligent missions, not arbitrary routes; generic mission API supports target/parameter updates |
| Basic Virtual Stick | Declared supported | `getLeftStick`, `getRightStick`, enable/disable, authority listener |
| Advanced Virtual Stick | Declared supported | Velocity/angle/height command object, recommended 5-25 Hz; use 20 Hz |
| RC handoff | Supported | Monitor authority owner and reason; RC pause/mode/RTH and safety events can take authority |
| Takeoff/landing | Declared supported | Start/stop takeoff, start/stop landing, landing-confirmation state/action |
| RTH/failsafe | Declared supported | Set RC-link-loss action; start/stop RTH; status, height, path mode, home, and low-battery assessment |
| Camera | Declared supported | Photo, interval, panorama, video, exposure, focus, zoom, storage, and media events |
| Gimbal | Declared supported | Angle/speed rotation, reset, calibrate, mode, attitude, tuning, vertical orientation |
| Battery | Declared supported | Percent/mAh, voltage/current, per-cell voltage, temperature, capacity, cycles, identity |
| Device health | Declared supported | Current health list plus change listener with code/title/description/severity |
| Obstacle sensing | Perception manager and raw obstacle listeners declared supported | Do not rely on automatic braking/bypass while Virtual Stick owns control; implement advisory/emergency logic |
| FlySafe/geofence | Consumer license workflow declared supported | Observe notifications/zones and synchronize an already-approved DJI unlock license; this is not legal authorization |
| Remote ID | Status and several regional configuration/status APIs declared supported | Treat non-working RID as a preflight failure where required; Android operator location must be valid |
| RTK | Not present in Mini 4 capability manifest | No centimeter-grade positioning or RTK route execution |
| Payload/PSDK pipeline | Not present | No onboard companion/payload control path |

## Waypoint API: present in SDK, unsupported by this aircraft profile

The exact generic 5.18 `IWaypointMissionManager` surface is:

```text
pushKMZFileToAircraft(path, progressCallback)
startMission(fileName, callback)
startMission(fileName, waylineIDs, callback)
startMission(fileName, breakPointInfo, callback)
pauseMission(callback)
resumeMission(callback)
resumeMission(breakPointInfo, callback)
stopMission(fileName, callback)
getAvailableWaylineIDs(fileName)
queryBreakPointInfoFromAircraft(fileName, callback)
add/remove/clear WaypointMissionExecuteStateListener
add/remove/clear WaylineExecutingInfoListener
add/remove/clear WaypointActionListener
```

On supported aircraft, `pushKMZFileToAircraft` uploads WPML KMZ; uploading a
file with the same name replaces the stored file. Listeners provide state,
current wayline ID/current waypoint index, and action start/finish. The API has
no method to mutate a currently executing waypoint or action in place. A
supported-aircraft replan would require pause/stop, a replacement upload, and a
new start/resume flow. Breakpoint resume is documented only for selected
enterprise aircraft.

None of the above methods appears in the Mini 4 Pro capability file. By
contrast, the Mavic 3 Enterprise capability file in the same AAR contains a
`waypointMission` block listing upload, start, pause, resume, stop, and all
execution listeners. That same-file comparison is the strongest static
evidence that the Mini 4 omission is intentional.

The independent `IWPMZManager`/WPMZ SDK can still generate and validate KMZ
files on Android, but that does not add aircraft execution support.

## Viable route and replanning model

Use `VirtualStickManager.getInstance()` and advanced mode:

```text
enableVirtualStick(callback)
setVirtualStickAdvancedModeEnabled(true)
sendVirtualStickAdvancedParam(VirtualStickFlightControlParam)
disableVirtualStick(callback)
set/remove VirtualStickStateListener
```

`VirtualStickFlightControlParam` supports:

- pitch and roll in velocity or angle mode;
- yaw in angular-velocity or angle mode;
- vertical throttle in velocity or height mode;
- body or ground roll/pitch coordinate system.

The generic SDK ranges are vertical velocity `[-6, 6] m/s`, horizontal velocity
`[-23, 23] m/s`, roll/pitch angle `[-30, 30] degrees`, yaw angle
`[-180, 180] degrees`, and yaw rate `[-100, 100] degrees/s`. These are SDK
limits, not suitable operating limits for this project. The bridge should use
much smaller configured bounds and acceleration/jerk limits.

DJI recommends sending advanced commands at 5-25 Hz. A 20 Hz Android loop is a
good target. Ground-coordinate horizontal velocity, angular-velocity yaw, and
velocity vertical control give the simplest route controller and are the only
advanced-mode combination for which DJI discusses obstacle-avoidance support.
However, DJI's Virtual Stick documentation lists only M300/M350, M30, Mavic 3E,
and Mavic 3M as supporting obstacle avoidance in Virtual Stick mode. Mini 4 Pro
is absent, so this bridge must assume no automatic avoidance while it owns
flight authority.

Replanning is implemented without a DJI waypoint API:

- Route documents have immutable `route_id` and monotonically increasing
  `revision`.
- The Mac flight session strictly parses and validates a complete revision
  before accepting it; the Android APK has no route endpoint.
- A revision becomes active atomically, never waypoint-by-waypoint over the
  network.
- Immediate replacement retargets the running route in the current authority
  session; boundary replacement remains pending until the current target is
  reached.
- `full_route_continue` retains the current target index, while
  `remaining_route_from_current_state` starts at replacement waypoint zero from
  a fresh current aircraft state.
- Pause commands zero commanded velocity and remain in Virtual Stick hover;
  resume continues the accepted plan in the same authority session.
- Any manual velocity, neutral, relative move/rotation, handoff, or landing
  command pauses route ownership before it can dispatch a competing setpoint.
- Abort zeros velocity; explicit handoff zeros again before disabling Virtual
  Stick and verifying RC ownership.

The current route schema does not schedule camera or gimbal actions. Those are
future supervisory features, not an implied part of atomic route replacement.

## Flight authority and failsafe layers

There are three different links and they must not share one vague "lost link"
policy:

| Failure | Firmware behavior available | Bridge responsibility |
|---|---|---|
| Mac to BOOX Wi-Fi | No DJI firmware failsafe | Android watchdog: neutral after 300 ms, then disable/release Virtual Stick after 1 s; it does not select RTH or landing |
| BOOX/app to RC-N2 USB | Not the RC-to-aircraft failsafe | Avoid claiming guaranteed behavior; physical pilot remains the final recovery path |
| RC-N2 to aircraft O4 | `KeyFailsafeAction` | Configure and verify `GOHOME`, `LANDING`, or `HOVER` before flight |

Virtual Stick authority reasons exposed by 5.18 include MSDK request, RC loss,
RC mode switch/not-normal mode, RC pause button, RC RTH, low-battery RTH,
critical-battery landing, near FlySafe/distance boundary, and unknown. The
bridge must transition to a non-commanding state on any safety/RC takeover and
must **not automatically reacquire authority**. A new operator control lease is
required.

DJI also states that Virtual Stick is unavailable while an automatic task is
running and requires the RC flight switch in normal/N mode.

### Declared Mini 4 RTH/failsafe keys

- `KeyIsFailSafe`, `KeyFailsafeAction`
- `KeyIsHomeLocationSet`, `KeyHomeLocation`,
  `KeyHomeLocationUsingCurrentAircraftLocation`
- `KeyGoHomePathMode`, `KeyGoHomeHeight`, `KeyGoHomeHeightRange`
- `KeyStartGoHome`, `KeyStopGoHome`, `KeyGoHomeStatus`
- `KeyLowBatteryRTHEnabled`, `KeyLowBatteryRTHInfo`
- `KeyLowBatteryWarningThreshold`, `KeyIsLowBatteryWarning`
- `KeySeriousLowBatteryWarningThreshold`,
  `KeyIsSeriousLowBatteryWarning`

`LowBatteryRTHInfo` supplies battery percent needed to return and land,
estimated reachable return radius, RTH countdown/state, remaining flight time,
time to home, and time to land. Use it for route admission and continuous
reserve checks. `KeyLowBatteryRTHConfirm` and the newer generic go-home confirm
keys are not in the Mini 4 capability list, so they should not be exposed
without a live capability test.

## Progress and telemetry

There is no supported onboard mission progress for Mini 4. The custom executor
should publish:

```text
route_id, revision, lifecycle state
current segment/action IDs
along-track and cross-track error
distance and ETA to current waypoint and finish
actual and commanded NED velocity
pause/stop/replan reason
source telemetry timestamps and staleness
```

The Mini 4 manifest declares these flight-controller telemetry/control keys:

```text
KeyConnection
KeyIsFlying
KeyFlightTimeInSeconds
KeyAircraftLocation3D
KeyAircraftAttitude
KeyAircraftVelocity
KeyTakeoffLocationAltitude
KeyFlightLogIndex
KeySerialNumber
KeyFirmwareVersion
KeyGPSSatelliteCount
KeyGPSSignalLevel
KeyCompassCount
KeyCompassHeading
KeyCompassHasError
KeyStartCompassCalibration
KeyStopCompassCalibration
KeyIsCompassCalibrating
KeyCompassCalibrationStatus
KeyIMUCount
KeyStartIMUCalibration
KeyIMUCalibrationInfo
KeyUltrasonicHeight
KeyWindWarning
KeyWindSpeed
KeyWindDirection
KeyMultipleFlightModeEnabled
KeyRemoteControllerFlightMode
KeyFlightMode
KeyIsFailSafe
KeyFailsafeAction
KeyLowBatteryWarningThreshold
KeyIsLowBatteryWarning
KeySeriousLowBatteryWarningThreshold
KeyIsSeriousLowBatteryWarning
KeyLEDsSettings
KeyAreMotorsOn
KeyLockMotors
KeyESCBeepEnabled
KeyStartTakeoff
KeyStopTakeoff
KeyStartAutoLanding
KeyStopAutoLanding
KeyIsLandingConfirmationNeeded
KeyConfirmLanding
KeyLandingProtectionState
KeyRebootDevice
KeyHeightLimitRange
KeyHeightLimit
KeyIsNearHeightLimit
KeyDistanceLimitEnabled
KeyDistanceLimitRange
KeyDistanceLimit
KeyIsNearDistanceLimit
KeyIsHomeLocationSet
KeyHomeLocation
KeyGoHomePathMode
KeyGoHomeHeight
KeyGoHomeHeightRange
KeyHomeLocationUsingCurrentAircraftLocation
KeyStartGoHome
KeyStopGoHome
KeyGoHomeStatus
KeyLowBatteryRTHEnabled
KeyLowBatteryRTHInfo
```

`KeyAltitude`, currently listened to by `BridgeApplication.kt`, is not in this
list. The supported source is `KeyAircraftLocation3D.getAltitude()` plus
`KeyUltrasonicHeight` near the ground. The bridge should expose numeric
latitude/longitude/altitude rather than `toString()` and retain the receive
timestamp of every source update.

The RC-N2 manifest declares connection/type/control mode, battery, identity and
firmware, all four physical stick axes, shutter/record/RTH/mode/C1 buttons,
left dial, firmware switching information, pairing controls/status, and reboot.
It does not declare RC GPS telemetry.

### Telemetry correctness and preflight profiles

The bridge now treats a value as `observed` only after its source callback or
manager snapshot has supplied it. A default Kotlin value is not evidence that a
condition is clear. Every source publishes its receive timestamp/age, and the
GPS-flight report blocks on missing or stale navigation sources.

Important unit and validity details from DJI's 5.18 API contract:

- Despite its name, `KeyFlightTimeInSeconds` is documented in raw units of
  0.1 seconds. The API publishes both the raw decisecond value and derived
  seconds.
- `KeyUltrasonicHeight` is documented in decimeters. The API publishes the raw
  integer and the derived meter value.
- `KeyGoHomeHeight` and `KeyGoHomeHeightRange` are documented in whole meters.
  A reported value such as `400` therefore remains 400 m; the bridge does not
  guess that it means 40 m. It is accepted only when it lies within the
  simultaneously reported range.
- Aircraft and home latitude/longitude must be finite and within `[-90, 90]`
  and `[-180, 180]`. An invalid update clears the prior coordinate instead of
  leaving stale valid-looking data.
- Home coordinates are withheld until `KeyIsHomeLocationSet` has explicitly
  reported `true`; a later `false` clears them.
- DJI documents `GPSSignalLevel.LEVEL_3` as the first hover-capable level.
  GPS-flight readiness requires that level or better, a positive satellite
  count, a valid set home point, and fresh position/velocity/attitude.
- Virtual Stick advanced mode is operationally false whenever Virtual Stick is
  disabled, even if DJI leaves the raw advanced-mode bit set. Authority state
  and the last authority-change event have separate timestamps.

Additional declared Mini 4 sources now included are low/serious-low-battery
warnings, ultrasonic height, landing-confirmation state, landing protection,
RC switch mode, height/distance-limit state, UAS Remote ID, and DJI device
health. `KeyLandingProtectionState` is exposed by `FlightAssistantKey` even
though the Mini 4 capability JSON lists the identifier with flight-controller
capabilities, so the bridge checks runtime key support before subscribing.

Preflight reports are deterministic but do not issue commands or encode a
legal conclusion:

- `lab` checks connection/observation health, emits warnings for missing GPS,
  home, battery, RID, and device-health sources, and explicitly never
  authorizes flight.
- `gps_flight` blocks on missing/stale navigation, unobserved or active DJI
  safety warnings, non-P (normal/N) RC mode, invalid home/RTH configuration,
  reached limits, failsafe, and blocking device-health severity/codes.
- `gps_flight_rid_required` adds a caller-selected requirement for both Android
  coarse/fine location grants, an enabled provider with a last-known location,
  RID broadcast enabled, and RID state `WORKING`. This separate profile avoids
  hardcoding a jurisdictional RID policy.

MSDK 5.18 checks both Android location permissions and obtains operator
location from enabled providers' last-known location. The Activity now requests
coarse/fine permission through the Android runtime prompt; it does not fabricate
or upload a location. For the app lifetime, the bridge requests real updates
from each enabled standard GPS/network provider at 1 s / 1 m and removes the
listener on shutdown. This permits a BOOX network provider to refresh DJI's
last-known operator location even when the tablet has no GNSS. The bridge
exposes permission/provider/last-known-location readiness without exposing the
operator coordinate itself. The RID-required profile rejects a missing,
future-dated, or older-than-10-second fix and a missing/non-finite accuracy or
accuracy worse than 100 m; these are explicit bridge readiness bounds, not a
claim about jurisdictional policy.

The following local 5.18 Remote ID health codes are preserved verbatim. The two
`cannot take off` codes block GPS-flight readiness regardless of their reported
generic severity:

```text
0x1B080003  Remote ID normal
0x161000B4  REMOTE_ID_CANNOT_TAKE_OFF_USER_LOCATION_UNAVALIABLE
0x1B080001  REMOTE_ID_USER_LOCATION_ABNORMAL
0x161000B5  Remote ID cannot take off: link error
0x1B080002  Remote ID link error
```

Device-health output retains each information code, warning level, component
and sensor index, title, and description. This is required to distinguish a
transport-accepted takeoff request from an aircraft refusal such as IMU
preheating or IMU/compass calibration error.

## Camera, gimbal, and media

All camera and gimbal keys use the `LEFT_OR_MAIN` component index and default
camera lens.

### Declared camera keys

```text
KeyConnection
KeyCameraType
KeyFirmwareVersion
KeyCameraModeRange
KeyCameraMode
KeyIsShootingPhoto
KeyStartShootPhoto
KeyStopShootPhoto
KeyPhotoFileFormatRange
KeyPhotoFileFormat
KeyPhotoIntervalShootSettings
KeyPhotoIntervalCountdown
KeyPhotoPanoramaMode
KeyIsShootingPhotoPanorama
KeyPhotoPanoramaProgress
KeyIsRecording
KeyStartRecord
KeyStopRecord
KeyRecordingTime
KeyVideoFileFormatRange
KeyVideoFileFormat
KeyNewlyGeneratedMediaFile
KeyCustomExpandDirectoryNameSettings
KeyCustomExpandFileNameSettings
KeyCameraStorageInfos
KeyLockGimbalDuringShootPhotoEnabled
KeyCameraVideoStreamSourceRange
KeyCameraVideoStreamSource
KeyExposureModeRange
KeyExposureMode
KeyExposureCompensationRange
KeyExposureCompensation
KeyAELockEnabled
KeyCameraMeteringMode
KeyISORange
KeyISO
KeyShutterSpeedRange
KeyShutterSpeed
KeyPhotoRatioRange
KeyPhotoRatio
KeyVideoResolutionFrameRateRange
KeyVideoResolutionFrameRate
KeyCameraZoomRatiosRange
KeyCameraZoomRatios
KeyCameraZoomFocalLength
KeyCameraFocusMode
KeyCameraFocusRingMinValue
KeyCameraFocusRingMaxValue
KeyCameraFocusRingValue
KeyCameraFocusTarget
KeyAntiFlicker
KeyCameraWhiteBalanceRange
KeyWhiteBalance
KeyResetCameraSetting
KeyFormatStorage
KeyIsShootingIntervalPhotos
```

Range keys should be queried at runtime; do not hard-code ISO, shutter, zoom,
format, aspect-ratio, or resolution/frame-rate choices.

### Declared gimbal keys

```text
KeyConnection
KeyFirmwareVersion
KeyGimbalAttitude
KeyYawRelativeToAircraftHeading
KeyFineTunePitchTotalDegree
KeyFineTuneYawTotalDegree
KeyFineTuneRollTotalDegree
KeyGimbalMode
KeyRotateByAngle
KeyRotateBySpeed
KeyGimbalReset
KeyFineTunePitchInDegrees
KeyFineTuneYawInDegrees
KeyFineTuneRollInDegrees
KeyGimbalCalibrate
KeyGimbalCalibrationStatus
KeyRestoreFactorySettings
KeyPitchControlMaxSpeed
KeyYawControlMaxSpeed
KeyPitchSmoothingFactor
KeyYawSmoothingFactor
KeyGimbalVerticalShotEnabled
```

Angle and speed rotation, reset, and camera capture can be scheduled as custom
route actions. Gimbal calibration and factory reset must be ground-only
maintenance operations, not remote flight actions.

The Mini 4 product manifest also exposes media file list, delete, camera video
playback, seek/pause/resume/stop, and associated listeners. `MediaFile` exposes
thumbnail, preview, XMP, and original-file pull methods. The current bridge
destroys DJI's `CameraStreamManager`/decoder pipeline so its internal raw video
observer can own channel 0. Media playback/download and the raw live relay must
therefore be serialized as explicit bridge modes and tested for observer
contention; they should not run concurrently by assumption.

## Battery and device health

Declared battery keys are:

```text
KeyConnection
KeyFullChargeCapacity
KeyChargeRemaining
KeyChargeRemainingInPercent
KeyBatteryTemperature
KeyVoltage
KeyCurrent
KeyNumberOfDischarges
KeyNumberOfCells
KeyCellVoltages
KeyBatteryManufacturedDate
KeySerialNumber
KeyFirmwareVersion
```

There is no declared Mini 4 battery self-discharge setter or generic remaining
lifetime/health percentage. Derive an advisory capacity ratio from full-charge
capacity only with an explicit warning that it is not DJI's health verdict.
Battery admission should primarily use charge percent, temperature, cell
spread, current/voltage, `LowBatteryRTHInfo`, and current device health items.

`DeviceHealthManager.getInstance()` is declared with:

```text
getCurrentDJIDeviceHealthInfos()
addDJIDeviceHealthInfoChangeListener(listener)
removeDJIDeviceHealthInfoChangeListener(listener)
clearAllListeners()
```

Each item provides an information code, localized title/description, and
warning level. Preserve the stable information code in the Mac API; prose is
for display, not machine policy.

## Obstacle sensing

The Mini 4 capability manifest declares the full visual perception manager:

```text
set/getOverallObstacleAvoidanceEnabled       (deprecated generic main switch)
set/getObstacleAvoidanceEnabled(direction)
set/getObstacleAvoidanceType                 (BRAKE, BYPASS, CLOSE)
set/getObstacleAvoidanceWarningDistance
set/getObstacleAvoidanceBrakingDistance
set/getVisionPositioningEnabled
set/getPrecisionLandingEnabled
add/remove/clear PerceptionInformationListener
add/remove/clear ObstacleDataListener
getRadarManager
```

Directions are upward, downward, and horizontal. `ObstacleData` provides a
360-degree horizontal distance array in millimeters plus upward and downward
distance. `PerceptionInfo` provides working/enabled state and warning/braking
distances by direction.

`getRadarManager` is generic and only usable with a supported mmWave radar
accessory; Mini 4 has no radar capability block, so treat radar as unavailable.

The perception feed is valuable for operator display and a conservative
emergency zero-velocity rule. It does not turn the custom controller into a
certified planner, and it must not be represented as guaranteed obstacle
avoidance during Virtual Stick flight.

## FlySafe/geofence

The Mini 4 manifest declares:

```text
add/remove/clear FlySafeNotificationListener
getFlyZonesInSurroundingArea
unlockAllEnhancedWarningFlyZone
unlockAuthorizationFlyZone
downloadFlyZoneLicensesFromServer
getFlyZonesByAreaID
pushFlyZoneLicensesToAircraft
pullFlyZoneLicensesFromAircraft
deleteFlyZoneLicensesFromAircraft
setFlyZoneLicensesEnabled
```

For consumer aircraft, DJI documents a license workflow: the operator applies
through DJI FlySafe, logs into the same DJI account in MSDK, downloads the
approved license, pushes it to the aircraft, and enables it. Directly passing a
zone ID is documented for enterprise aircraft, not consumer aircraft, despite
the generic method being present.

FlySafe data and unlocks are not substitutes for FAA authorization or any
other legal approval. Route validation must treat legal authorization and DJI
firmware geofencing as separate checks.

The Mini 4 manifest does not list the 5.8 dynamic FlySafe database import APIs
that are present in the generic interface. Do not expose those for this product
without a capability test.

## Remote ID and privacy mode

The Mini 4 manifest declares:

```text
setAreaCode                                      (deprecated generic API)
get/add/remove/clear UASRemoteIDStatus
set/get ElectronicIDEnabled and status listeners
set/get UARegistrationNumber and status listeners
```

The generic 5.18 interface additionally contains area-strategy, operator
registration, EU C-class, China real-name, and 5.18 real-name-tag methods, but
those methods are not listed in the Mini 4 product manifest. The bridge should
monitor the automatically selected regional state and expose it, but should
not publish unsupported configuration calls to the Mac API without a live
capability test.

`UASRemoteIDStatus` reports whether broadcast is enabled and a working state
including `WORKING`, operator-location-lost, firmware error, no broadcast, and
not supported. Where Remote ID is required, anything other than `WORKING`
should block automated takeoff.

The Android manifest now declares coarse/fine location permissions and
`MainActivity` requests them at runtime. The bridge reports permission,
provider, last-known-fix age, accuracy, and mock-provider state without
publishing the operator coordinate. RC-N2's capability file contains no RC GPS
key, and permission alone cannot create location hardware, so a RID-dependent
caller must still verify that the BOOX supplies an acceptable fresh fix.

For minimal DJI network access, MSDK's supported Local Data Mode can prevent
MSDK network requests, but it requires an approved LDM license. If online
registration or FlySafe license sync is needed, explicitly exempt only
`MSDK_INIT_AND_REGISTRATION`, `FLY_SAFE`, and any legally required regional
module. LDM state and exemptions should be visible in preflight status.

## Implemented Mac/Android architecture

```text
VEIL/Mac
  operator policy/UI
  persistent JSON-lines flight session
    AtomicRouteRevisionStore -> 20 Hz guidance -> immediate + 20 Hz VDC2 UDP
    continuously drained latest telemetry <- 20 Hz capacity-one NDJSON
  native VideoToolbox viewer <- raw Annex-B HEVC
       v
BOOX Android bridge
  authenticated HTTP status/supervisory actions + callback journal
  session/sequence/freshness/HMAC validation -> latest setpoint -> 20 Hz MSDK
  matched packet acknowledgement + timestamped DJI telemetry
  raw channel-0 relay; no local decode/transcode
       |
       v
RC-N2 --O4--> Mini 4 Pro
```

The APK intentionally does not own navigation policy or expose an Android
route endpoint. The Mac flight process owns one arming session, sends a changed
manual setpoint synchronously, refreshes the current target at 20 Hz, and keeps
the JSON-lines command channel available for preemption. Android neutralizes
after 300 ms without a valid packet and releases Virtual Stick after one
second. Those watchdogs address Mac-to-BOOX command loss; they are not the
RC-to-aircraft O4 failsafe.

Telemetry is published at 20 Hz. Every client has a capacity-one pending
mailbox, so an obsolete snapshot is replaced rather than replayed after a
stall. Each frame carries a process-scoped sequence, generation/write times,
per-client queue age, sequence-gap count, and source timestamps. The Mac flight
session continuously drains the TCP stream, keeps only increasing sequences,
and resets the sequence baseline after reconnect because an Android restart can
begin at one.

Each accepted VDC2 packet is exposed with its session, unsigned sequence,
echoed setpoint, sent/received/applied timestamps, and receive-to-apply/combined
latency values. An exact sequence, or a newer same-setpoint refresh in the same
session, proves Android accepted that setpoint. It does not prove aircraft
motion; physical claims require corresponding fresh aircraft telemetry.

### Current machine interfaces

```text
Android 8765/TCP  authenticated HTTP status, takeoff/land, authority, journal
Android 8766/TCP  authenticated raw Annex-B HEVC
Android 8767/UDP  authenticated V2 latest-setpoint control
Android 8768/TCP  authenticated 20 Hz latest-only NDJSON telemetry

Mac stdin/stdout  veil.flight-repl.v1 JSON lines:
                  status, arm, velocity, neutral, move_relative,
                  rotate_relative, route_accept/start/pause/resume/abort/status,
                  handoff, land, quit
```

### Route schema

The implemented strict contract is `veil.route-revision.v1`:

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
    "waypoints": [{
      "latitude_deg": 38.0001,
      "longitude_deg": -77.0001,
      "altitude_m": 10.0,
      "horizontal_speed_mps": 2.0,
      "vertical_speed_mps": 1.0,
      "horizontal_tolerance_m": 1.0,
      "vertical_tolerance_m": 0.5,
      "yaw_mode": "face_waypoint",
      "maximum_yaw_rate_deg_s": 30.0
    }]
  }
}
```

`expected_accepted_revision` gives compare-and-set acceptance. `activation` is
`immediate` or `at_waypoint_boundary`; `scope` is `full_route_continue` or
`remaining_route_from_current_state`. The Mac swaps only a fully parsed and
validated revision. An immediate in-flight replacement produces a new target
without disabling/re-enabling Virtual Stick; boundary activation keeps the
existing target until it is reached. The route command itself is sent as the
complete original JSON string inside the REPL's `route_accept.document` field,
preserving duplicate-key detection.

Waypoint altitude is compared to DJI's raw `KeyAircraftLocation3D.altitude`.
The SDK contract does not make this bridge an RTK or centimeter-precision
system, so callers must verify the live altitude reference and use realistic
GNSS-scale tolerances. `move_relative` is separately an open-loop speed/time
primitive and must not be described as exact displacement.

### Policy placement

The APK publishes deterministic readiness reports and journals them with
takeoff requests, but battery, GPS, home-location, RID, and health findings are informational to
the Mac policy layer; they do not veto an authenticated takeoff inside the APK.
The APK retains transport-correctness checks such as literal takeoff
confirmation, mutually exclusive asynchronous actions, session binding,
freshness/range validation, and deadman behavior. DJI firmware still applies
its own limits. Jurisdictional, mission, and operator policy belongs in the Mac
caller and must not be inferred from a transport acknowledgement.

## Remaining limitations and verification

- The route is Mac-resident. It cannot continue as an onboard mission if the
  Mac/BOOX/USB/RC path fails.
- Mini 4 Pro has no MSDK `waypointMission` capability, DJI Fly library
  import/export/in-place editing API, RTK, or documented guarantee of obstacle
  braking/bypass while Virtual Stick owns control.
- The route schema currently contains navigation waypoints only; it does not
  schedule camera/gimbal actions.
- The native VideoToolbox path substantially reduced presentation backlog in a
  synthetic benchmark, but live camera-to-eye latency remains to be measured.
  The portable `ffplay` path remains a fallback, not the preferred Mac viewer.
- The control/telemetry/video transports are authenticated but plaintext. Use
  an isolated trusted network or encrypted tunnel.
- Raw channel-0 video uses an internal MSDK observer. Keep the path
  version-pinned and retest it after SDK or media-mode changes.
- Staged live tests still need to measure grounded packet acknowledgement,
  manual command response, route following/revision, RC takeover, stale
  telemetry, Wi-Fi loss, USB/product disconnect, and watchdog behavior. Offline
  tests do not establish exact positioning or live obstacle-avoidance behavior.

## Official DJI references

- [MSDK 5.18 release notes and supported firmware](https://developer.dji.com/doc/mobile-sdk-tutorial/en/?pbc=D3IDBfR5&pm=custom)
- [Waypoint tutorial and WPML/KMZ workflow](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/waypoint.html)
- [`IWaypointMissionManager` API](https://developer.dji.com/api-reference-v5/android-api/Components/IWaypointMissionManager/IWaypointMissionManager.html)
- [Virtual Stick tutorial](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/virtual-stick.html)
- [`IVirtualStickManager` API and safety limitations](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager.html)
- [Mini 4 Pro intelligent-flight support table](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/intelligent-flight.html)
- [`FlightControllerKey` API](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_FlightController_FlightControllerKey.html)
- [`GimbalKey` API](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Gimbal_GimbalKey.html)
- [`BatteryKey` API](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Battery_BatteryKey.html)
- [Perception tutorial](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/perception.html)
- [`IPerceptionManager` API](https://developer.dji.com/api-reference-v5/android-api/Components/IPerceptionManager/IPerceptionManager.html)
- [FlySafe/unlock tutorial](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/flyzone.html)
- [`IFlyZoneManager` API](https://developer.dji.com/api-reference-v5/android-api/Components/IFlyZoneManager/IFlyZoneManager.html)
- [`IUASRemoteIDManager` API](https://developer.dji.com/api-reference-v5/android-api/Components/IUASRemoteIDManager/IUASRemoteIDManager.html)
- [Official Android setup permissions](https://developer.dji.com/doc/mobile-sdk-tutorial/en/quick-start/run-sample.html)
- [Local Data Mode tutorial](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/ldm.html)
- [`IDeviceHealthManager` API](https://developer.dji.com/api-reference-v5/android-api/Components/IDeviceHealthManager/IDeviceHealthManager.html)
