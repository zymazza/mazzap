import CoreMedia
import CoreVideo
import Foundation
import VideoToolbox

enum DecoderAcceleration: String, Sendable {
    case hardware
    case software
}

enum DecoderError: Error, LocalizedError {
    case incompleteParameterSets
    case formatDescription(OSStatus)
    case sessionCreation(OSStatus)
    case blockBuffer(OSStatus)
    case sampleBuffer(OSStatus)
    case decode(OSStatus)
    case asynchronousDecode(OSStatus, String)
    case backlog(Int)

    var errorDescription: String? {
        switch self {
        case .incompleteParameterSets: return "HEVC VPS/SPS/PPS are incomplete"
        case .formatDescription(let status): return "HEVC format description failed (OSStatus \(status))"
        case .sessionCreation(let status): return "HEVC decoder creation failed (OSStatus \(status))"
        case .blockBuffer(let status): return "compressed frame buffer creation failed (OSStatus \(status))"
        case .sampleBuffer(let status): return "compressed sample creation failed (OSStatus \(status))"
        case .decode(let status): return "HEVC decode submission failed (OSStatus \(status))"
        case .asynchronousDecode(let status, let frame):
            return "HEVC decoder callback failed (OSStatus \(status); \(frame))"
        case .backlog(let frames): return "HEVC decoder backlog reached \(frames) frames"
        }
    }

    var isVideoDecoderMalfunction: Bool {
        switch self {
        case .sessionCreation(let status), .decode(let status),
             .asynchronousDecode(let status, _):
            return status == kVTVideoDecoderMalfunctionErr
        default:
            return false
        }
    }

    static let videoDecoderMalfunctionStatus = kVTVideoDecoderMalfunctionErr
}

final class DecoderMetrics: @unchecked Sendable {
    private let lock = NSLock()
    private var decodedFrames = 0
    private var decoderDrops = 0
    private var latenciesMS = [Double]()

    func recordDecoded(latencyMS: Double) {
        lock.withLock {
            decodedFrames += 1
            latenciesMS.append(latencyMS)
            if latenciesMS.count > 2_000 {
                latenciesMS.removeFirst(latenciesMS.count - 2_000)
            }
        }
    }

    func recordDecoderDrop() {
        lock.withLock { decoderDrops += 1 }
    }

    func decodedFrameCount() -> Int {
        lock.withLock { decodedFrames }
    }

    struct Snapshot {
        let decodedFrames: Int
        let decoderDrops: Int
        let medianDecodeMS: Double?
        let p95DecodeMS: Double?
    }

    func snapshot() -> Snapshot {
        lock.withLock {
            let sorted = latenciesMS.sorted()
            func percentile(_ fraction: Double) -> Double? {
                guard !sorted.isEmpty else { return nil }
                let index = min(sorted.count - 1, Int((Double(sorted.count - 1) * fraction).rounded()))
                return sorted[index]
            }
            return Snapshot(
                decodedFrames: decodedFrames,
                decoderDrops: decoderDrops,
                medianDecodeMS: percentile(0.5),
                p95DecodeMS: percentile(0.95)
            )
        }
    }
}

private final class FrameContext {
    let receivedAt: ContinuousClock.Instant
    let description: String

    init(receivedAt: ContinuousClock.Instant, description: String) {
        self.receivedAt = receivedAt
        self.description = description
    }
}

final class HEVCHardwareDecoder: @unchecked Sendable {
    typealias FrameHandler = @Sendable (CVPixelBuffer) -> Void
    typealias FailureHandler = @Sendable (Error) -> Void

    private(set) var parameterSets: HEVCParameterSets
    private(set) var maxReorderPictures: Int?
    private(set) var acceleration: DecoderAcceleration
    private let formatDescription: CMVideoFormatDescription
    private var session: VTDecompressionSession?
    private let inFlightLock = NSLock()
    private var inFlightFrames = 0
    private let frameHandler: FrameHandler
    private let failureHandler: FailureHandler
    private var submittedFrames: UInt64 = 0
    let metrics: DecoderMetrics

    init(
        parameterSets: HEVCParameterSets,
        acceleration: DecoderAcceleration = .hardware,
        metrics: DecoderMetrics,
        frameHandler: @escaping FrameHandler,
        failureHandler: @escaping FailureHandler
    ) throws {
        guard let ordered = parameterSets.ordered else { throw DecoderError.incompleteParameterSets }
        self.parameterSets = parameterSets
        self.maxReorderPictures = parameterSets.maxReorderPictures
        self.acceleration = acceleration
        self.frameHandler = frameHandler
        self.failureHandler = failureHandler
        self.metrics = metrics

        let format = try Self.makeFormatDescription(ordered)
        self.formatDescription = format
        var callback = VTDecompressionOutputCallbackRecord(
            decompressionOutputCallback: { outputRefCon, sourceFrameRefCon, status, infoFlags, imageBuffer, _, _ in
                guard let outputRefCon, let sourceFrameRefCon else { return }
                let decoder = Unmanaged<HEVCHardwareDecoder>
                    .fromOpaque(outputRefCon)
                    .takeUnretainedValue()
                let context = Unmanaged<FrameContext>
                    .fromOpaque(sourceFrameRefCon)
                    .takeRetainedValue()
                decoder.handleDecodedFrame(
                    status: status,
                    infoFlags: infoFlags,
                    imageBuffer: imageBuffer,
                    receivedAt: context.receivedAt,
                    frameDescription: context.description
                )
            },
            decompressionOutputRefCon: Unmanaged.passUnretained(self).toOpaque()
        )
        let decoderSpecification: [CFString: Any]
        switch acceleration {
        case .hardware:
            decoderSpecification = [
                kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder: true,
            ]
        case .software:
            // VideoToolbox enables hardware by default on modern macOS.  This
            // explicitly selects its software decoder after the hardware path
            // reports a decoder malfunction for an otherwise software-valid
            // DJI picture sequence.
            decoderSpecification = [
                kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder: false,
            ]
        }
        let imageAttributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: NSNumber(
                value: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
            ),
            kCVPixelBufferMetalCompatibilityKey: true,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
        ]
        var createdSession: VTDecompressionSession?
        let status = VTDecompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            formatDescription: format,
            decoderSpecification: decoderSpecification as CFDictionary,
            imageBufferAttributes: imageAttributes as CFDictionary,
            outputCallback: &callback,
            decompressionSessionOut: &createdSession
        )
        guard status == noErr, let createdSession else {
            throw DecoderError.sessionCreation(status)
        }
        session = createdSession
        let realTimeStatus = VTSessionSetProperty(
            createdSession,
            key: kVTDecompressionPropertyKey_RealTime,
            value: kCFBooleanTrue
        )
        if realTimeStatus != noErr {
            fputs("warning: could not set VideoToolbox realtime mode (\(realTimeStatus))\n", stderr)
        }
    }

    deinit {
        invalidate()
    }

    func decode(_ accessUnit: HEVCAccessUnit) throws {
        guard let session else { throw DecoderError.decode(kVTInvalidSessionErr) }
        let decoderNALs = accessUnit.decoderNALs
        guard !decoderNALs.isEmpty else { return }
        let sample = try Self.makeSampleBuffer(decoderNALs, format: formatDescription)
        let reserved = inFlightLock.withLock { () -> Bool in
            guard inFlightFrames < Self.maxInFlightFrames else { return false }
            inFlightFrames += 1
            return true
        }
        guard reserved else { throw DecoderError.backlog(Self.maxInFlightFrames) }
        submittedFrames &+= 1
        let nalDescription = decoderNALs.map { "\($0.type):\($0.bytes.count)" }.joined(separator: ",")
        let frameDescription = "submission=\(submittedFrames) nals=[\(nalDescription)]"
        let context = Unmanaged.passRetained(
            FrameContext(receivedAt: accessUnit.receivedAt, description: frameDescription)
        )
        let contextPointer = context.toOpaque()
        var infoFlags = VTDecodeInfoFlags()
        var decodeFlags: VTDecodeFrameFlags = [._EnableAsynchronousDecompression]
        // An unknown SPS shape takes the correctness-preserving path. The
        // common DJI low-delay SPS parses as zero and avoids temporal delay.
        if (maxReorderPictures ?? 1) > 0 {
            decodeFlags.insert(._EnableTemporalProcessing)
        }
        let status = VTDecompressionSessionDecodeFrame(
            session,
            sampleBuffer: sample,
            flags: decodeFlags,
            frameRefcon: contextPointer,
            infoFlagsOut: &infoFlags
        )
        if status != noErr {
            // The VideoToolbox contract guarantees no callback only when the
            // submission itself returns an error.  A successful submission
            // with .frameDropped still receives a callback, so releasing its
            // retained context here would be a use-after-free.
            context.release()
            releaseInFlightSlot()
            if infoFlags.contains(.frameDropped) { metrics.recordDecoderDrop() }
            throw DecoderError.decode(status)
        }
    }

    func invalidate() {
        guard let current = session else { return }
        session = nil
        // Recovery must never wait for a malfunctioning hardware decoder to
        // finish corrupt/delayed work.  In particular, VideoToolbox can stop
        // completing frames after kVTVideoDecoderMalfunctionErr (-12909), so
        // FinishDelayedFrames/WaitForAsynchronousFrames is an unbounded wait.
        // Invalidate is the documented deterministic teardown and discards
        // outstanding work; the stream worker can then reconnect at a fresh
        // VPS/SPS/PPS + IDR immediately.
        VTDecompressionSessionInvalidate(current)
    }

    private func handleDecodedFrame(
        status: OSStatus,
        infoFlags: VTDecodeInfoFlags,
        imageBuffer: CVImageBuffer?,
        receivedAt: ContinuousClock.Instant,
        frameDescription: String
    ) {
        releaseInFlightSlot()
        if infoFlags.contains(.frameDropped) { metrics.recordDecoderDrop() }
        guard status == noErr else {
            failureHandler(DecoderError.asynchronousDecode(status, frameDescription))
            return
        }
        guard let imageBuffer else {
            metrics.recordDecoderDrop()
            return
        }
        let elapsed = receivedAt.duration(to: .now)
        let components = elapsed.components
        let latencyMS = Double(components.seconds) * 1_000.0 +
            Double(components.attoseconds) / 1_000_000_000_000_000.0
        metrics.recordDecoded(latencyMS: latencyMS)
        frameHandler(imageBuffer)
    }

    private func releaseInFlightSlot() {
        inFlightLock.withLock {
            inFlightFrames = max(0, inFlightFrames - 1)
        }
    }

    private static func makeFormatDescription(_ sets: [Data]) throws -> CMVideoFormatDescription {
        precondition(sets.count == 3)
        var format: CMFormatDescription?
        let status: OSStatus = sets[0].withUnsafeBytes { vpsRaw in
            sets[1].withUnsafeBytes { spsRaw in
                sets[2].withUnsafeBytes { ppsRaw in
                    let pointers = [
                        vpsRaw.bindMemory(to: UInt8.self).baseAddress!,
                        spsRaw.bindMemory(to: UInt8.self).baseAddress!,
                        ppsRaw.bindMemory(to: UInt8.self).baseAddress!,
                    ]
                    let sizes = [vpsRaw.count, spsRaw.count, ppsRaw.count]
                    return pointers.withUnsafeBufferPointer { pointerBuffer in
                        sizes.withUnsafeBufferPointer { sizeBuffer in
                            CMVideoFormatDescriptionCreateFromHEVCParameterSets(
                                allocator: kCFAllocatorDefault,
                                parameterSetCount: 3,
                                parameterSetPointers: pointerBuffer.baseAddress!,
                                parameterSetSizes: sizeBuffer.baseAddress!,
                                nalUnitHeaderLength: 4,
                                extensions: nil,
                                formatDescriptionOut: &format
                            )
                        }
                    }
                }
            }
        }
        guard status == noErr, let format else { throw DecoderError.formatDescription(status) }
        return format
    }

    private static func makeSampleBuffer(
        _ nals: [HEVCNALUnit],
        format: CMFormatDescription
    ) throws -> CMSampleBuffer {
        let totalBytes = nals.reduce(0) { $0 + 4 + $1.bytes.count }
        var lengthPrefixed = Data(capacity: totalBytes)
        for nal in nals {
            var length = UInt32(nal.bytes.count).bigEndian
            withUnsafeBytes(of: &length) { lengthPrefixed.append(contentsOf: $0) }
            lengthPrefixed.append(nal.bytes)
        }

        var blockBuffer: CMBlockBuffer?
        var status = CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: nil,
            blockLength: lengthPrefixed.count,
            blockAllocator: kCFAllocatorDefault,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: lengthPrefixed.count,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        guard status == kCMBlockBufferNoErr, let blockBuffer else {
            throw DecoderError.blockBuffer(status)
        }
        status = lengthPrefixed.withUnsafeBytes { raw in
            CMBlockBufferReplaceDataBytes(
                with: raw.baseAddress!,
                blockBuffer: blockBuffer,
                offsetIntoDestination: 0,
                dataLength: raw.count
            )
        }
        guard status == kCMBlockBufferNoErr else { throw DecoderError.blockBuffer(status) }

        var sampleBuffer: CMSampleBuffer?
        var sampleSize = lengthPrefixed.count
        status = CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault,
            dataBuffer: blockBuffer,
            formatDescription: format,
            sampleCount: 1,
            sampleTimingEntryCount: 0,
            sampleTimingArray: nil,
            sampleSizeEntryCount: 1,
            sampleSizeArray: &sampleSize,
            sampleBufferOut: &sampleBuffer
        )
        guard status == noErr, let sampleBuffer else { throw DecoderError.sampleBuffer(status) }
        return sampleBuffer
    }

    private static let maxInFlightFrames = 6
}
