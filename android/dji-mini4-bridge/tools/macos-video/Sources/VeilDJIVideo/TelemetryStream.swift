import Darwin
import Foundation

struct TelemetryField: Equatable, Sendable {
    let path: String
    let value: String
}

struct ObstacleRangeSample: Equatable, Sendable {
    /// Index in DJI's vector.  The SDK supplies angular spacing but does not
    /// document whether index zero is forward or which way indices rotate.
    let index: Int
    let distanceM: Double
}

struct TelemetryDisplaySnapshot: Equatable, Sendable {
    let sequence: Int64?
    let aircraftConnected: Bool
    let isFlying: Bool
    let motorsOn: Bool
    let flightMode: String
    let authority: String
    let batteryPercent: Double?
    let batteryTemperatureC: Double?
    let altitudeM: Double?
    let pitchDeg: Double?
    let rollDeg: Double?
    let yawDeg: Double?
    let northMPS: Double?
    let eastMPS: Double?
    let downMPS: Double?
    let gpsSatellites: Double?
    let gpsSignal: String
    let controlLatencyMS: Double?
    let telemetryQueueAgeMS: Double?
    let obstacleMeters: [String: Double]
    let horizontalObstacleSamples: [ObstacleRangeSample]
    let horizontalObstacleSampleCount: Int?
    let horizontalObstacleIntervalDeg: Double?
    let obstacleVectorMapping: String
    let obstacleDataObserved: Bool
    let obstacleDataAgeMS: Double?
    let obstacleDataFresh: Bool
    let avoidanceInformationObserved: Bool
    let avoidanceInformationAgeMS: Double?
    let avoidanceInformationFresh: Bool
    let avoidanceType: String
    let avoidanceEffectiveEnabled: Bool?
    let avoidanceWorking: [String: Bool]
    let fields: [TelemetryField]

    static func parse(
        line: Data,
        includeFlattenedFields: Bool = true
    ) throws -> TelemetryDisplaySnapshot {
        let raw = try JSONSerialization.jsonObject(with: line)
        guard let root = raw as? [String: Any] else {
            throw ViewerError.connection("telemetry frame is not a JSON object")
        }
        var flattened: [TelemetryField] = []
        if includeFlattenedFields {
            flatten(root, prefix: "", into: &flattened)
            flattened.sort { $0.path < $1.path }
        }

        func first(_ paths: [String]) -> Any? {
            for path in paths {
                if let value = value(at: path, in: root), !(value is NSNull) { return value }
            }
            return nil
        }
        func optionalBool(_ paths: String...) -> Bool? {
            if let value = first(paths) as? Bool { return value }
            if let number = first(paths) as? NSNumber { return number.boolValue }
            return nil
        }
        func bool(_ paths: String...) -> Bool {
            if let value = first(paths) as? Bool { return value }
            if let number = first(paths) as? NSNumber { return number.boolValue }
            return false
        }
        func number(_ paths: String...) -> Double? {
            guard let value = first(paths) else { return nil }
            if let number = value as? NSNumber { return number.doubleValue }
            if let text = value as? String { return Double(text) }
            return nil
        }
        func string(_ paths: String..., fallback: String = "unknown") -> String {
            guard let value = first(paths) else { return fallback }
            if let text = value as? String { return text }
            return String(describing: value)
        }

        let obstacleObserved = optionalBool(
            "aircraft_telemetry.perception.obstacle_distances.observed"
        ) == true
        let obstacleAgeMS = number(
            "aircraft_telemetry.perception.obstacle_distances.age_ms"
        )
        let obstacleFresh = sourceIsFresh(observed: obstacleObserved, ageMS: obstacleAgeMS)
        let informationObserved = optionalBool(
            "aircraft_telemetry.perception.information.observed"
        ) == true
        let informationAgeMS = number(
            "aircraft_telemetry.perception.information.age_ms"
        )
        let informationFresh = sourceIsFresh(
            observed: informationObserved,
            ageMS: informationAgeMS
        )
        let obstacle = value(
            at: "aircraft_telemetry.perception.obstacle_distances", in: root
        ) as? [String: Any]
        let horizontal = horizontalObstacleVector(from: obstacle, sourceFresh: obstacleFresh)
        let workingPaths: [(String, String)] = [
            ("forward", "aircraft_telemetry.perception.information.working.forward"),
            ("right", "aircraft_telemetry.perception.information.working.right"),
            ("backward", "aircraft_telemetry.perception.information.working.backward"),
            ("left", "aircraft_telemetry.perception.information.working.left"),
            ("upward", "aircraft_telemetry.perception.information.working.upward"),
            ("downward", "aircraft_telemetry.perception.information.working.downward"),
        ]
        var avoidanceWorking: [String: Bool] = [:]
        if informationFresh {
            for (name, path) in workingPaths {
                if let state = optionalBool(path) { avoidanceWorking[name] = state }
            }
        }

        return TelemetryDisplaySnapshot(
            sequence: number("telemetry_sequence").map(Int64.init),
            aircraftConnected: bool(
                "aircraft_telemetry.aircraft_connected", "aircraft_connected"
            ),
            isFlying: bool("aircraft_telemetry.is_flying", "is_flying"),
            motorsOn: bool("aircraft_telemetry.motors_on", "motors_on"),
            flightMode: string("aircraft_telemetry.flight_mode", "flight_mode"),
            authority: string(
                "aircraft_telemetry.authority.owner", "flight_control_authority"
            ),
            batteryPercent: number(
                "aircraft_telemetry.battery.charge_remaining_percent"
            ),
            batteryTemperatureC: number(
                "aircraft_telemetry.battery.temperature_c"
            ),
            altitudeM: number(
                "aircraft_telemetry.location.altitude_m", "altitude_m"
            ),
            pitchDeg: number("aircraft_telemetry.attitude.pitch_deg"),
            rollDeg: number("aircraft_telemetry.attitude.roll_deg"),
            yawDeg: number("aircraft_telemetry.attitude.yaw_deg"),
            northMPS: number("aircraft_telemetry.velocity_ned.north_mps"),
            eastMPS: number("aircraft_telemetry.velocity_ned.east_mps"),
            downMPS: number("aircraft_telemetry.velocity_ned.down_mps"),
            gpsSatellites: number("aircraft_telemetry.gps.satellite_count"),
            gpsSignal: string("aircraft_telemetry.gps.signal_level"),
            controlLatencyMS: number("last_control_latency_ms"),
            telemetryQueueAgeMS: number("telemetry_queue_age_ms"),
            obstacleMeters: obstacleDistances(from: obstacle, sourceFresh: obstacleFresh),
            horizontalObstacleSamples: horizontal.samples,
            horizontalObstacleSampleCount: horizontal.sampleCount,
            horizontalObstacleIntervalDeg: horizontal.intervalDeg,
            obstacleVectorMapping: string(
                "aircraft_telemetry.perception.obstacle_distances.horizontal_angle_origin_and_order",
                fallback: "dji_provided_undocumented"
            ),
            obstacleDataObserved: obstacleObserved,
            obstacleDataAgeMS: obstacleAgeMS,
            obstacleDataFresh: obstacleFresh,
            avoidanceInformationObserved: informationObserved,
            avoidanceInformationAgeMS: informationAgeMS,
            avoidanceInformationFresh: informationFresh,
            avoidanceType: string(
                "aircraft_telemetry.perception.information.obstacle_avoidance_type"
            ),
            avoidanceEffectiveEnabled: informationFresh ? optionalBool(
                "aircraft_telemetry.perception.information.enabled.effective_from_avoidance_type"
            ) : nil,
            avoidanceWorking: avoidanceWorking,
            fields: flattened
        )
    }
}

final class TelemetryStreamWorker: @unchecked Sendable {
    typealias SnapshotHandler = @Sendable (TelemetryDisplaySnapshot) -> Void

    private let host: String
    private let port: UInt16
    private let token: String
    private let snapshotHandler: SnapshotHandler
    private let statusHandler: @Sendable (String) -> Void
    private let lock = NSLock()
    private var stopped = false
    private var activeSocket: Int32 = -1
    private var thread: Thread?

    init(
        host: String,
        port: UInt16 = 8768,
        token: String,
        snapshotHandler: @escaping SnapshotHandler,
        statusHandler: @escaping @Sendable (String) -> Void
    ) {
        self.host = host
        self.port = port
        self.token = token
        self.snapshotHandler = snapshotHandler
        self.statusHandler = statusHandler
    }

    func start() {
        let workerThread = Thread { [weak self] in self?.run() }
        workerThread.name = "veil-dji-telemetry"
        thread = workerThread
        workerThread.start()
    }

    func stop() {
        let socket = lock.withLock { () -> Int32 in
            stopped = true
            let socket = activeSocket
            activeSocket = -1
            return socket
        }
        if socket >= 0 {
            Darwin.shutdown(socket, SHUT_RDWR)
            Darwin.close(socket)
        }
    }

    private var shouldStop: Bool { lock.withLock { stopped } }

    private func run() {
        while !shouldStop {
            autoreleasepool {
                do {
                    try runSession()
                } catch {
                    if !shouldStop {
                        statusHandler("telemetry resynchronizing: \(error.localizedDescription)")
                    }
                }
            }
            if !shouldStop { Thread.sleep(forTimeInterval: 0.10) }
        }
    }

    private func runSession() throws {
        let socket = try ViewerSocket.connect(
            host: host,
            port: port,
            streamName: "telemetry"
        )
        let accepted = lock.withLock { () -> Bool in
            guard !stopped else { return false }
            activeSocket = socket
            return true
        }
        guard accepted else {
            Darwin.close(socket)
            return
        }
        defer {
            let ownsSocket = lock.withLock { () -> Bool in
                guard activeSocket == socket else { return false }
                activeSocket = -1
                return true
            }
            if ownsSocket { Darwin.close(socket) }
        }

        try ViewerSocket.sendAll(
            socket,
            data: Data("TOKEN \(token)\n".utf8),
            streamName: "telemetry"
        )
        statusHandler("telemetry connected")
        var pending = Data()
        var latestLine: Data?
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        var lastSequence: Int64?
        var lastByteAt = DispatchTime.now().uptimeNanoseconds
        var lastPublishedAt: UInt64 = 0
        var lastFullFieldsAt: UInt64 = 0
        while !shouldStop {
            let count = Darwin.recv(socket, &buffer, buffer.count, 0)
            if count > 0 {
                lastByteAt = DispatchTime.now().uptimeNanoseconds
                pending.append(contentsOf: buffer[0..<count])
                guard pending.count <= Self.maximumPendingBytes else {
                    throw ViewerError.connection("telemetry frame exceeded size limit")
                }
                while let newline = pending.firstIndex(of: 0x0a) {
                    let line = pending.prefix(upTo: newline)
                    pending.removeSubrange(...newline)
                    guard !line.isEmpty else { continue }
                    latestLine = Data(line)
                }
                let now = DispatchTime.now().uptimeNanoseconds
                if let line = latestLine,
                   lastPublishedAt == 0 || now - lastPublishedAt >= Self.uiPeriodNanoseconds {
                    let includeFields = lastFullFieldsAt == 0 ||
                        now - lastFullFieldsAt >= Self.fullFieldsPeriodNanoseconds
                    let snapshot = try TelemetryDisplaySnapshot.parse(
                        line: line,
                        includeFlattenedFields: includeFields
                    )
                    latestLine = nil
                    if let sequence = snapshot.sequence,
                       let previous = lastSequence,
                       sequence <= previous {
                        continue
                    }
                    lastSequence = snapshot.sequence ?? lastSequence
                    lastPublishedAt = now
                    if includeFields { lastFullFieldsAt = now }
                    snapshotHandler(snapshot)
                }
            } else if count == 0 {
                throw ViewerError.socketClosed
            } else if errno == EAGAIN || errno == EWOULDBLOCK {
                let now = DispatchTime.now().uptimeNanoseconds
                if now - lastByteAt >= Self.idleReconnectNanoseconds {
                    let idleSeconds = Double(now - lastByteAt) / 1_000_000_000.0
                    throw ViewerError.connection(
                        String(format: "telemetry stream stale for %.1f seconds", idleSeconds)
                    )
                }
            } else if errno == EINTR {
                continue
            } else {
                throw ViewerError.connection(
                    "telemetry receive failed: \(ViewerSocket.errorText(errno))"
                )
            }
        }
    }

    private static let maximumPendingBytes = 2 * 1_024 * 1_024
    private static let uiPeriodNanoseconds: UInt64 = 100_000_000
    private static let fullFieldsPeriodNanoseconds: UInt64 = 500_000_000
    private static let idleReconnectNanoseconds: UInt64 = 1_000_000_000
}

private func value(at path: String, in root: [String: Any]) -> Any? {
    var current: Any = root
    for component in path.split(separator: ".").map(String.init) {
        guard let object = current as? [String: Any], let next = object[component] else {
            return nil
        }
        current = next
    }
    return current
}

private func flatten(
    _ value: Any,
    prefix: String,
    depth: Int = 0,
    into result: inout [TelemetryField]
) {
    guard result.count < maximumFlattenedFields else { return }
    guard depth < maximumFlattenDepth else {
        result.append(TelemetryField(path: prefix, value: "<depth limit>"))
        return
    }
    if let object = value as? [String: Any] {
        for (key, child) in object {
            let path = prefix.isEmpty ? key : "\(prefix).\(key)"
            flatten(child, prefix: path, depth: depth + 1, into: &result)
            guard result.count < maximumFlattenedFields else { break }
        }
    } else if let array = value as? [Any] {
        for (index, child) in array.enumerated() {
            flatten(
                child,
                prefix: "\(prefix)[\(index)]",
                depth: depth + 1,
                into: &result
            )
            guard result.count < maximumFlattenedFields else { break }
        }
    } else {
        let rendered: String
        switch value {
        case is NSNull: rendered = "—"
        case let boolean as Bool: rendered = boolean ? "true" : "false"
        case let number as NSNumber:
            rendered = String(describing: number)
        default: rendered = String(describing: value)
        }
        result.append(TelemetryField(path: prefix, value: rendered))
    }
}

private func obstacleDistances(
    from obstacle: [String: Any]?,
    sourceFresh: Bool
) -> [String: Double] {
    guard sourceFresh, let obstacle else { return [:] }
    var result: [String: Double] = [:]
    func millimeters(_ key: String) -> Double? {
        guard let number = obstacle[key] as? NSNumber else { return nil }
        let raw = number.doubleValue
        // DJI uses non-positive and 65535 as unavailable sentinels.  These
        // exact `_mm` schema fields are the only vertical ranges accepted.
        guard raw.isFinite, raw > 0, raw < 65_535 else { return nil }
        return raw / 1_000.0
    }
    result["upward"] = millimeters("upward_distance_mm")
    result["downward"] = millimeters("downward_distance_mm")
    return result
}

private struct HorizontalObstacleVector {
    let samples: [ObstacleRangeSample]
    let sampleCount: Int?
    let intervalDeg: Double?
}

private func horizontalObstacleVector(
    from obstacle: [String: Any]?,
    sourceFresh: Bool
) -> HorizontalObstacleVector {
    guard sourceFresh,
          let obstacle,
          let raw = obstacle["horizontal_distance_mm"] as? [Any],
          let intervalNumber = obstacle["horizontal_angle_interval_deg"] as? NSNumber else {
        return HorizontalObstacleVector(samples: [], sampleCount: nil, intervalDeg: nil)
    }
    let interval = intervalNumber.doubleValue
    guard horizontalVectorIsComplete(sampleCount: raw.count, intervalDeg: interval) else {
        return HorizontalObstacleVector(
            samples: [],
            sampleCount: raw.count,
            intervalDeg: interval
        )
    }
    let samples = raw.enumerated().compactMap { index, value -> ObstacleRangeSample? in
        guard let number = value as? NSNumber else { return nil }
        let millimeters = number.doubleValue
        guard millimeters.isFinite, millimeters > 0, millimeters < 65_535 else { return nil }
        return ObstacleRangeSample(
            index: index,
            distanceM: millimeters / 1_000.0
        )
    }
    return HorizontalObstacleVector(
        samples: samples,
        sampleCount: raw.count,
        intervalDeg: interval
    )
}

func horizontalVectorIsComplete(sampleCount: Int, intervalDeg: Double) -> Bool {
    guard sampleCount > 0, intervalDeg.isFinite, intervalDeg > 0 else { return false }
    return abs(Double(sampleCount) * intervalDeg - 360.0) <= 0.000_001
}

private func sourceIsFresh(observed: Bool, ageMS: Double?) -> Bool {
    guard observed, let ageMS, ageMS.isFinite else { return false }
    return ageMS >= 0 && ageMS <= obstacleFreshnessLimitMS
}

private let obstacleFreshnessLimitMS = 1_000.0
private let maximumFlattenDepth = 48
private let maximumFlattenedFields = 10_000
