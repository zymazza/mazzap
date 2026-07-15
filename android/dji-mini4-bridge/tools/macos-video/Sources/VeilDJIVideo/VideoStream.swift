import Darwin
import Foundation

enum ViewerError: Error, LocalizedError {
    case missingToken
    case invalidArgument(String)
    case connection(String)
    case socketClosed
    case metalUnavailable

    var errorDescription: String? {
        switch self {
        case .missingToken:
            return "set VEIL_DJI_TOKEN or pass --token"
        case .invalidArgument(let message):
            return message
        case .connection(let message):
            return message
        case .socketClosed:
            return "video socket closed"
        case .metalUnavailable:
            return "Metal is unavailable on this Mac"
        }
    }
}

struct ViewerOptions: Sendable {
    var host = ProcessInfo.processInfo.environment["VEIL_DJI_HOST"] ?? "127.0.0.1"
    var port: UInt16 = 8766
    var telemetryPort: UInt16 = 8768
    var token = ProcessInfo.processInfo.environment["VEIL_DJI_TOKEN"] ?? ""

    static func parse(_ arguments: [String]) throws -> ViewerOptions {
        var options = ViewerOptions()
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            if argument == "--help" || argument == "-h" {
                throw ViewerError.invalidArgument(Self.usage)
            }
            guard index + 1 < arguments.count else {
                throw ViewerError.invalidArgument("\(argument) requires a value")
            }
            let value = arguments[index + 1]
            switch argument {
            case "--host": options.host = value
            case "--port":
                guard let port = UInt16(value), port > 0 else {
                    throw ViewerError.invalidArgument("--port must be between 1 and 65535")
                }
                options.port = port
            case "--telemetry-port":
                guard let port = UInt16(value), port > 0 else {
                    throw ViewerError.invalidArgument(
                        "--telemetry-port must be between 1 and 65535"
                    )
                }
                options.telemetryPort = port
            case "--token": options.token = value
            default:
                throw ViewerError.invalidArgument("unknown argument: \(argument)\n\(Self.usage)")
            }
            index += 2
        }
        guard !options.token.isEmpty else { throw ViewerError.missingToken }
        return options
    }

    static let usage = "usage: veil-dji-video [--host HOST] [--port 8766] "
        + "[--telemetry-port 8768] [--token TOKEN]"
}

/// Starts on the lowest-latency decoder and opens a permanent circuit breaker
/// only for VideoToolbox's hardware-malfunction callback.  Ordinary transport
/// reconnects and malformed/incomplete bootstrap data do not silently degrade
/// the decoder mode.
struct DecoderRecoveryPolicy: Sendable {
    private(set) var nextAcceleration = DecoderAcceleration.hardware

    mutating func recordFailure(_ error: Error, from acceleration: DecoderAcceleration) {
        guard acceleration == .hardware,
              let decoderError = error as? DecoderError,
              decoderError.isVideoDecoderMalfunction else { return }
        nextAcceleration = .software
    }

    mutating func recordCallbackFailure(_ error: Error, from acceleration: DecoderAcceleration) {
        recordFailure(error, from: acceleration)
    }
}

/// Identifies the decoder session allowed to affect pipeline recovery.  An
/// invalidated VideoToolbox session may finish callbacks after its replacement
/// is active; those callbacks must release their own frame context but cannot
/// tear down the replacement session.
struct DecoderGenerationTracker: Sendable {
    private(set) var active: UInt64?
    private var next: UInt64 = 0

    mutating func activate() -> UInt64 {
        next &+= 1
        active = next
        return next
    }

    mutating func retire() {
        active = nil
    }

    func isActive(_ generation: UInt64) -> Bool {
        active == generation
    }
}

/// Pure monotonic stale-stream detector.  TCP receive timeouts poll this state
/// so an authenticated socket that stops delivering bytes cannot leave a
/// frozen last frame on screen indefinitely.
struct StreamLiveness: Sendable {
    static let timeout = Duration.seconds(3)
    private(set) var lastBytesAt: ContinuousClock.Instant

    init(startedAt: ContinuousClock.Instant) {
        lastBytesAt = startedAt
    }

    mutating func recordBytes(at instant: ContinuousClock.Instant) {
        lastBytesAt = instant
    }

    func isStale(at instant: ContinuousClock.Instant) -> Bool {
        lastBytesAt.duration(to: instant) >= Self.timeout
    }
}

final class NativeVideoPipeline: @unchecked Sendable {
    private let parser = AnnexBParser()
    private let assembler = HEVCAccessUnitAssembler()
    private var parameterSets = HEVCParameterSets()
    private var decoder: HEVCHardwareDecoder?
    private let failureLock = NSLock()
    private var asynchronousFailure: Error?
    private var recoveryPolicy = DecoderRecoveryPolicy()
    private var decoderGenerations = DecoderGenerationTracker()
    private var deliveredFrames: UInt64 = 0
    private let frameHandler: HEVCHardwareDecoder.FrameHandler
    private let statusHandler: @Sendable (String) -> Void
    let metrics = DecoderMetrics()

    init(
        frameHandler: @escaping HEVCHardwareDecoder.FrameHandler,
        statusHandler: @escaping @Sendable (String) -> Void
    ) {
        self.frameHandler = frameHandler
        self.statusHandler = statusHandler
    }

    func receive(_ bytes: Data, receivedAt: ContinuousClock.Instant = .now) throws {
        if let failure = failureLock.withLock({ asynchronousFailure }) { throw failure }
        for nal in try parser.append(bytes) {
            let completed = try assembler.accept(nal, receivedAt: receivedAt)
            for accessUnit in completed {
                try decode(accessUnit)
            }
            parameterSets.observe(nal)
        }
        if let failure = failureLock.withLock({ asynchronousFailure }) { throw failure }
    }

    func reset() {
        let retiredDecoder = decoder
        decoder = nil
        failureLock.withLock {
            decoderGenerations.retire()
            asynchronousFailure = nil
        }
        retiredDecoder?.invalidate()
        parser.reset()
        assembler.reset()
        parameterSets = HEVCParameterSets()
    }

    func deliveredFrameCount() -> UInt64 {
        failureLock.withLock { deliveredFrames }
    }

    private func decode(_ accessUnit: HEVCAccessUnit) throws {
        if decoder?.parameterSets != parameterSets {
            guard accessUnit.isIDR, parameterSets.isComplete else { return }
            let retiredDecoder = decoder
            decoder = nil
            failureLock.withLock { decoderGenerations.retire() }
            retiredDecoder?.invalidate()
            let acceleration = failureLock.withLock { recoveryPolicy.nextAcceleration }
            let generation = failureLock.withLock { decoderGenerations.activate() }
            let newDecoder: HEVCHardwareDecoder
            do {
                newDecoder = try HEVCHardwareDecoder(
                    parameterSets: parameterSets,
                    acceleration: acceleration,
                    metrics: metrics,
                    frameHandler: { [weak self] frame in
                        guard let self else { return }
                        let shouldDeliver = self.failureLock.withLock { () -> Bool in
                            guard self.decoderGenerations.isActive(generation) else { return false }
                            self.deliveredFrames &+= 1
                            return true
                        }
                        if shouldDeliver { self.frameHandler(frame) }
                    },
                    failureHandler: { [weak self] error in
                        self?.failureLock.withLock {
                            guard self?.decoderGenerations.isActive(generation) == true else {
                                return
                            }
                            self?.recoveryPolicy.recordFailure(error, from: acceleration)
                            if self?.asynchronousFailure == nil {
                                self?.asynchronousFailure = error
                            }
                        }
                    }
                )
            } catch {
                failureLock.withLock {
                    if decoderGenerations.isActive(generation) {
                        recoveryPolicy.recordFailure(error, from: acceleration)
                        decoderGenerations.retire()
                    }
                }
                throw error
            }
            decoder = newDecoder
            let reorderDescription = newDecoder.maxReorderPictures.map(String.init) ?? "unknown"
            statusHandler(
                "\(newDecoder.acceleration.rawValue) HEVC active; SPS reorder=\(reorderDescription) frame(s)"
            )
        }
        guard let decoder else { return }
        do {
            try decoder.decode(accessUnit)
        } catch {
            failureLock.withLock {
                recoveryPolicy.recordFailure(error, from: decoder.acceleration)
            }
            throw error
        }
    }
}

final class VideoStreamWorker: @unchecked Sendable {
    private let options: ViewerOptions
    private let pipeline: NativeVideoPipeline
    private let statusHandler: @Sendable (String) -> Void
    private let lock = NSLock()
    private var stopped = false
    private var activeSocket: Int32 = -1
    private var thread: Thread?

    init(
        options: ViewerOptions,
        pipeline: NativeVideoPipeline,
        statusHandler: @escaping @Sendable (String) -> Void
    ) {
        self.options = options
        self.pipeline = pipeline
        self.statusHandler = statusHandler
    }

    func start() {
        let workerThread = Thread { [weak self] in self?.run() }
        workerThread.name = "veil-dji-native-video"
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
                        statusHandler("resynchronizing: \(error.localizedDescription)")
                    }
                }
                pipeline.reset()
            }
            if !shouldStop { Thread.sleep(forTimeInterval: 0.15) }
        }
    }

    private func runSession() throws {
        statusHandler("connecting to \(options.host):\(options.port)")
        let socket = try ViewerSocket.connect(
            host: options.host,
            port: options.port,
            streamName: "video",
            receiveBufferBytes: 64 * 1024
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
            data: Data("TOKEN \(options.token)\n".utf8),
            streamName: "video"
        )
        statusHandler("authenticated; waiting for fresh VPS/SPS/PPS + IDR")
        var buffer = [UInt8](repeating: 0, count: 16 * 1024)
        let clock = ContinuousClock()
        var liveness = StreamLiveness(startedAt: clock.now)
        var lastDecodedCount = pipeline.deliveredFrameCount()
        var lastDecodedAt = clock.now
        while !shouldStop {
            let count = Darwin.recv(socket, &buffer, buffer.count, 0)
            if count > 0 {
                let receivedAt = clock.now
                liveness.recordBytes(at: receivedAt)
                try pipeline.receive(Data(buffer[0..<count]), receivedAt: receivedAt)
                let decodedCount = pipeline.deliveredFrameCount()
                if decodedCount != lastDecodedCount {
                    lastDecodedCount = decodedCount
                    lastDecodedAt = receivedAt
                } else if lastDecodedAt.duration(to: receivedAt) >= StreamLiveness.timeout {
                    throw ViewerError.connection("video decoded output stale for 3 seconds")
                }
            } else if count == 0 {
                throw ViewerError.socketClosed
            } else if errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR {
                if liveness.isStale(at: clock.now) {
                    throw ViewerError.connection("video stream stale for 3 seconds")
                }
                continue
            } else {
                throw ViewerError.connection(
                    "video receive failed: \(ViewerSocket.errorText(errno))"
                )
            }
        }
    }
}
