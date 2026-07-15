import Foundation
import Testing
@testable import VeilDJIVideo

private func nal(
    _ type: Int,
    payload: [UInt8] = [0x80, 0x01],
    fourByteStart: Bool = true
) -> Data {
    var result = Data(fourByteStart ? [0, 0, 0, 1] : [0, 0, 1])
    result.append(contentsOf: [UInt8(type << 1), 1])
    result.append(contentsOf: payload)
    return result
}

private func bytesFromHex(_ text: String) -> Data {
    var result = Data()
    var index = text.startIndex
    while index < text.endIndex {
        let next = text.index(index, offsetBy: 2)
        result.append(UInt8(text[index..<next], radix: 16)!)
        index = next
    }
    return result
}

@Test func parserHandlesEveryByteBoundaryAndMixedStartCodes() throws {
    let stream = nal(32, fourByteStart: true) +
        nal(33, fourByteStart: false) +
        nal(34, fourByteStart: true) +
        nal(20, fourByteStart: false) +
        nal(1, fourByteStart: true)
    let parser = AnnexBParser()
    var output = [HEVCNALUnit]()
    for byte in stream {
        output.append(contentsOf: try parser.append(Data([byte])))
    }
    #expect(output.map(\.type) == [32, 33, 34, 20])
    #expect(output[3].isFirstSlice)
    #expect(output[3].isIDR)
}

@Test func accessUnitBoundaryNeverSplitsContinuationSlices() throws {
    let assembler = HEVCAccessUnitAssembler()
    let idrFirst = HEVCNALUnit(bytes: nal(20).dropFirst(4))
    let idrContinuation = HEVCNALUnit(bytes: nal(20, payload: [0x00, 0x02]).dropFirst(4))
    let nextPicture = HEVCNALUnit(bytes: nal(1).dropFirst(4))

    #expect(try assembler.accept(idrFirst).isEmpty)
    #expect(try assembler.accept(idrContinuation).isEmpty)
    let emitted = try assembler.accept(nextPicture)
    #expect(emitted.count == 1)
    #expect(emitted[0].nals.count == 2)
    #expect(emitted[0].isIDR)
}

@Test func decoderSampleKeepsSlicesButDropsDJISuffixMetadata() {
    let firstSlice = HEVCNALUnit(bytes: nal(1).dropFirst(4))
    let continuation = HEVCNALUnit(bytes: nal(1, payload: [0x00, 0x02]).dropFirst(4))
    let djiSuffixSEI = HEVCNALUnit(bytes: nal(40, payload: [0xff, 0xff]).dropFirst(4))
    let audit = HEVCNALUnit(bytes: nal(35).dropFirst(4))
    let unit = HEVCAccessUnit(
        nals: [audit, firstSlice, djiSuffixSEI, continuation],
        receivedAt: .now
    )

    #expect(unit.decoderNALs.map(\.type) == [1, 1])
}

@Test func bFrameFixtureSPSDeclaresTwoPicturesOfReordering() {
    // Main-profile SPS from the checked synthetic 640x360, 24 fps fixture.
    let sps = bytesFromHex(
        "42010101600000030090000003000003003fa005020171f2e595952930bc05a02000000300200000030301"
    )
    var sets = HEVCParameterSets()
    sets.observe(HEVCNALUnit(bytes: sps))
    #expect(sets.maxReorderPictures == 2)
}

@Test func truncatedSPSCannotSilentlyClaimZeroReordering() {
    var sets = HEVCParameterSets()
    sets.observe(HEVCNALUnit(bytes: Data([0x42, 0x01, 0x01])))
    #expect(sets.maxReorderPictures == nil)
}

@Test func zeroLatencyEncoderSPSSelectsDirectDecodePath() {
    // libx265 zerolatency/bframes=0 SPS, independently checked by ffprobe.
    let sps = bytesFromHex(
        "42010101600000030090000003000003003ca00a080c1f3e5ba4a4c2f0168080000003008000000c04"
    )
    var sets = HEVCParameterSets()
    sets.observe(HEVCNALUnit(bytes: sps))
    #expect(sets.maxReorderPictures == 0)
}

@Test func liveDJIMini4SPSSelectsDirectDecodePath() {
    // Captured from the Mini 4 Pro/RC-N2 raw H.265 relay.  This locks down that
    // the live stream itself requests no display-order buffering.
    let sps = bytesFromHex(
        "42010121600000030000030000030000030096a003c08010e7f96bbb706bb135010101040000030004000003006020"
    )
    var sets = HEVCParameterSets()
    sets.observe(HEVCNALUnit(bytes: sps))
    #expect(sets.maxReorderPictures == 0)
}

@Test func parameterSetCompletenessRequiresAllThreeKinds() {
    var sets = HEVCParameterSets()
    sets.observe(HEVCNALUnit(bytes: nal(32).dropFirst(4)))
    sets.observe(HEVCNALUnit(bytes: nal(33).dropFirst(4)))
    #expect(!sets.isComplete)
    sets.observe(HEVCNALUnit(bytes: nal(34).dropFirst(4)))
    #expect(sets.isComplete)
    #expect(sets.ordered?.count == 3)
}

@Test func decoderRecoveryBeginsHardwareAndPermanentlyFallsBackOnMalfunction() {
    var policy = DecoderRecoveryPolicy()
    #expect(policy.nextAcceleration == .hardware)

    policy.recordCallbackFailure(
        DecoderError.asynchronousDecode(
            DecoderError.videoDecoderMalfunctionStatus,
            "submission=41 nals=[1:2048]"
        ),
        from: .hardware
    )
    #expect(policy.nextAcceleration == .software)

    // Later callbacks from either the retired hardware session or the active
    // software session can never reopen the hardware circuit in this window.
    policy.recordCallbackFailure(
        DecoderError.asynchronousDecode(-1, "late callback"),
        from: .hardware
    )
    policy.recordCallbackFailure(
        DecoderError.asynchronousDecode(-1, "software callback"),
        from: .software
    )
    #expect(policy.nextAcceleration == .software)
}

@Test func nonMalfunctionDoesNotSilentlyDowngradeHardware() {
    var policy = DecoderRecoveryPolicy()
    policy.recordCallbackFailure(
        DecoderError.asynchronousDecode(-1, "ordinary frame error"),
        from: .hardware
    )
    #expect(policy.nextAcceleration == .hardware)
}

@Test func staleStreamDeadlineIsMonotonicAndRefreshable() {
    let start = ContinuousClock.now
    var liveness = StreamLiveness(startedAt: start)
    #expect(!liveness.isStale(at: start.advanced(by: .seconds(2))))
    #expect(liveness.isStale(at: start.advanced(by: .seconds(3))))

    let refreshed = start.advanced(by: .seconds(2))
    liveness.recordBytes(at: refreshed)
    #expect(!liveness.isStale(at: start.advanced(by: .seconds(4))))
    #expect(liveness.isStale(at: start.advanced(by: .seconds(5))))
}

@Test func telemetrySnapshotExtractsFlightAndTransportTruth() throws {
    let line = Data(#"""
    {
      "telemetry_sequence": 42,
      "last_control_latency_ms": 8,
      "telemetry_queue_age_ms": 2,
      "aircraft_telemetry": {
        "aircraft_connected": true,
        "is_flying": true,
        "motors_on": true,
        "flight_mode": "GPS_NORMAL",
        "location": {"altitude_m": 3.5},
        "attitude": {"pitch_deg": 1.0, "roll_deg": -2.0, "yaw_deg": 91.0},
        "velocity_ned": {"north_mps": 0.1, "east_mps": 0.2, "down_mps": -0.3},
        "gps": {"satellite_count": 21, "signal_level": "LEVEL_5"},
        "battery": {"charge_remaining_percent": 73, "temperature_c": 31.5},
        "authority": {"owner": "MSDK"}
      }
    }
    """#.utf8)
    let snapshot = try TelemetryDisplaySnapshot.parse(line: line)

    #expect(snapshot.sequence == 42)
    #expect(snapshot.aircraftConnected)
    #expect(snapshot.isFlying)
    #expect(snapshot.flightMode == "GPS_NORMAL")
    #expect(snapshot.authority == "MSDK")
    #expect(snapshot.batteryPercent == 73)
    #expect(snapshot.batteryTemperatureC == 31.5)
    #expect(snapshot.controlLatencyMS == 8)
    #expect(snapshot.fields.contains {
        $0.path == "aircraft_telemetry.attitude.yaw_deg" && Double($0.value) == 91
    })
}

@Test func telemetrySnapshotDoesNotGuessObstacleSchemasOrUnits() throws {
    let line = Data(#"""
    {
      "aircraft_telemetry": {
        "perception": {
          "forward_obstacle_distance_m": 2.25,
          "left_obstacle_distance_m": 4.5,
          "upward_obstacle_distance_m": 1.75,
          "information": {
            "warning_distance_m": {"upward": 3.0},
            "braking_distance_m": {"downward": 2.0}
          },
          "obstacle_distances": {
            "observed": true,
            "age_ms": 10,
            "updated_monotonic_ms": 123456
          }
        }
      }
    }
    """#.utf8)
    let snapshot = try TelemetryDisplaySnapshot.parse(line: line)

    #expect(snapshot.obstacleMeters.isEmpty)
}

@Test func telemetrySnapshotPreservesDJIObstacleVectorWithoutBodyMapping() throws {
    var horizontal = Array(repeating: 65_535, count: 8)
    horizontal[0] = 2_000
    horizontal[2] = 3_000
    horizontal[4] = 4_000
    horizontal[6] = 5_000
    let object: [String: Any] = [
        "aircraft_telemetry": [
            "perception": [
                "obstacle_distances": [
                    "observed": true,
                    "age_ms": 20,
                    "horizontal_angle_origin_and_order": "dji_provided_undocumented",
                    "horizontal_angle_interval_deg": 45,
                    "horizontal_distance_mm": horizontal,
                    "upward_distance_mm": 1_500,
                    "downward_distance_mm": 1_250,
                ]
            ]
        ]
    ]
    let snapshot = try TelemetryDisplaySnapshot.parse(
        line: JSONSerialization.data(withJSONObject: object)
    )

    #expect(snapshot.obstacleMeters["upward"] == 1.5)
    #expect(snapshot.obstacleMeters["downward"] == 1.25)
    #expect(snapshot.horizontalObstacleSamples == [
        ObstacleRangeSample(index: 0, distanceM: 2),
        ObstacleRangeSample(index: 2, distanceM: 3),
        ObstacleRangeSample(index: 4, distanceM: 4),
        ObstacleRangeSample(index: 6, distanceM: 5),
    ])
    #expect(snapshot.obstacleVectorMapping == "dji_provided_undocumented")
}

@Test func staleObstacleSourceIsNeverDisplayedAsCurrent() throws {
    let object: [String: Any] = [
        "aircraft_telemetry": ["perception": ["obstacle_distances": [
            "observed": true,
            "age_ms": 1_001,
            "horizontal_angle_interval_deg": 90,
            "horizontal_distance_mm": [1_000, 2_000, 3_000, 4_000],
            "upward_distance_mm": 500,
        ]]],
    ]
    let snapshot = try TelemetryDisplaySnapshot.parse(
        line: JSONSerialization.data(withJSONObject: object)
    )
    #expect(!snapshot.obstacleDataFresh)
    #expect(snapshot.obstacleMeters.isEmpty)
    #expect(snapshot.horizontalObstacleSamples.isEmpty)
}

@Test func incompleteHorizontalCircleIsRejected() {
    #expect(horizontalVectorIsComplete(sampleCount: 8, intervalDeg: 45))
    #expect(!horizontalVectorIsComplete(sampleCount: 7, intervalDeg: 45))
}

@Test func accessUnitAggregateHasABoundedFailure() throws {
    let assembler = HEVCAccessUnitAssembler(maxAccessUnitBytes: 8)
    let first = HEVCNALUnit(bytes: Data([0x02, 0x01, 0x80, 1, 2]))
    let continuation = HEVCNALUnit(bytes: Data([0x02, 0x01, 0x00, 3, 4]))
    _ = try assembler.accept(first)
    #expect(throws: AnnexBError.self) {
        _ = try assembler.accept(continuation)
    }
}

@Test func synchronousDecoderMalfunctionOpensSoftwareCircuit() {
    var policy = DecoderRecoveryPolicy()
    policy.recordFailure(
        DecoderError.decode(DecoderError.videoDecoderMalfunctionStatus),
        from: .hardware
    )
    #expect(policy.nextAcceleration == .software)
}

@Test func retiredDecoderGenerationCannotAffectReplacement() {
    var tracker = DecoderGenerationTracker()
    let first = tracker.activate()
    let second = tracker.activate()
    #expect(!tracker.isActive(first))
    #expect(tracker.isActive(second))
    tracker.retire()
    #expect(!tracker.isActive(second))
}
