import Foundation

struct HEVCNALUnit: Equatable {
    let bytes: Data

    var type: Int {
        guard !bytes.isEmpty else { return -1 }
        return Int((bytes[bytes.startIndex] >> 1) & 0x3f)
    }

    var isVCL: Bool { (0...31).contains(type) }
    var isIDR: Bool { (19...20).contains(type) }

    var isFirstSlice: Bool {
        guard isVCL, bytes.count >= 3 else { return false }
        return (bytes[bytes.startIndex + 2] & 0x80) != 0
    }
}

enum AnnexBError: Error, LocalizedError {
    case oversizedNAL(Int)
    case oversizedAccessUnit(Int)

    var errorDescription: String? {
        switch self {
        case .oversizedNAL(let bytes):
            return "HEVC NAL exceeded the 16 MiB parser limit (\(bytes) bytes)"
        case .oversizedAccessUnit(let bytes):
            return "HEVC access unit exceeded the configured parser limit (\(bytes) bytes)"
        }
    }
}

/// Incrementally removes Annex-B start codes and emits complete NAL units.
/// The final unit remains buffered until the next start code proves its end.
final class AnnexBParser {
    private static let maxNALBytes = 16 * 1024 * 1024
    private var pending = Data()

    func append(_ data: Data) throws -> [HEVCNALUnit] {
        guard !data.isEmpty else { return [] }
        pending.append(data)
        let starts = Self.startCodes(in: pending)
        guard !starts.isEmpty else {
            if pending.count > Self.maxNALBytes {
                throw AnnexBError.oversizedNAL(pending.count)
            }
            if pending.count > 3 {
                pending = pending.suffix(3)
            }
            return []
        }

        var result = [HEVCNALUnit]()
        if starts.count > 1 {
            for index in 0..<(starts.count - 1) {
                let payloadStart = starts[index].offset + starts[index].length
                let payloadEnd = starts[index + 1].offset
                guard payloadEnd > payloadStart else { continue }
                let bytes = pending.subdata(in: payloadStart..<payloadEnd)
                if bytes.count > Self.maxNALBytes {
                    throw AnnexBError.oversizedNAL(bytes.count)
                }
                result.append(HEVCNALUnit(bytes: bytes))
            }
        }

        let finalStart = starts[starts.count - 1].offset
        pending = pending.subdata(in: finalStart..<pending.count)
        if pending.count > Self.maxNALBytes + 4 {
            throw AnnexBError.oversizedNAL(pending.count - 4)
        }
        return result
    }

    func reset() {
        pending.removeAll(keepingCapacity: true)
    }

    private struct StartCode {
        let offset: Int
        let length: Int
    }

    private static func startCodes(in data: Data) -> [StartCode] {
        guard data.count >= 3 else { return [] }
        return data.withUnsafeBytes { rawBuffer in
            let bytes = rawBuffer.bindMemory(to: UInt8.self)
            var result = [StartCode]()
            var index = 0
            while index + 2 < bytes.count {
                if index + 3 < bytes.count,
                   bytes[index] == 0, bytes[index + 1] == 0,
                   bytes[index + 2] == 0, bytes[index + 3] == 1 {
                    result.append(StartCode(offset: index, length: 4))
                    index += 4
                } else if bytes[index] == 0, bytes[index + 1] == 0,
                          bytes[index + 2] == 1 {
                    result.append(StartCode(offset: index, length: 3))
                    index += 3
                } else {
                    index += 1
                }
            }
            return result
        }
    }
}

struct HEVCAccessUnit {
    let nals: [HEVCNALUnit]
    let receivedAt: ContinuousClock.Instant

    var isIDR: Bool { nals.contains(where: { $0.isIDR && $0.isFirstSlice }) }

    /// VideoToolbox gets parameter sets through CMVideoFormatDescription and
    /// only needs the picture's VCL NALs in each sample.  DJI's stream carries
    /// proprietary suffix-SEI (type 40) payloads, including occasional units
    /// that FFmpeg identifies as invalid.  Keeping transport metadata out of
    /// the compressed picture sample prevents those units from poisoning an
    /// otherwise valid hardware-decoder session.
    var decoderNALs: [HEVCNALUnit] { nals.filter(\.isVCL) }
}

/// Groups complete NALs into pictures. Emission uses the next AUD/first slice
/// as the boundary, adding at most one source-frame interval without guessing
/// at callback or TCP packet boundaries.
final class HEVCAccessUnitAssembler {
    private static let defaultMaxAccessUnitBytes = 32 * 1024 * 1024
    private var current = [HEVCNALUnit]()
    private var currentHasVCL = false
    private var currentBytes = 0
    private var currentReceivedAt = ContinuousClock.now
    private let maxAccessUnitBytes: Int

    init(maxAccessUnitBytes: Int = HEVCAccessUnitAssembler.defaultMaxAccessUnitBytes) {
        precondition(maxAccessUnitBytes > 0)
        self.maxAccessUnitBytes = maxAccessUnitBytes
    }

    func accept(
        _ nal: HEVCNALUnit,
        receivedAt: ContinuousClock.Instant = .now
    ) throws -> [HEVCAccessUnit] {
        var emitted = [HEVCAccessUnit]()
        let startsNextUnit = currentHasVCL && (
            nal.type == 35 ||
            nal.type == 32 || nal.type == 33 || nal.type == 34 ||
            (nal.isVCL && nal.isFirstSlice)
        )
        if startsNextUnit {
            emitted.append(HEVCAccessUnit(nals: current, receivedAt: currentReceivedAt))
            current.removeAll(keepingCapacity: true)
            currentHasVCL = false
            currentBytes = 0
        }
        if current.isEmpty {
            currentReceivedAt = receivedAt
        }
        current.append(nal)
        currentBytes += nal.bytes.count
        guard currentBytes <= maxAccessUnitBytes else {
            let oversizedBytes = currentBytes
            reset()
            throw AnnexBError.oversizedAccessUnit(oversizedBytes)
        }
        currentHasVCL = currentHasVCL || nal.isVCL
        return emitted
    }

    func reset() {
        current.removeAll(keepingCapacity: true)
        currentHasVCL = false
        currentBytes = 0
        currentReceivedAt = .now
    }
}

struct HEVCParameterSets: Equatable {
    var vps: Data?
    var sps: Data?
    var pps: Data?

    var isComplete: Bool { vps != nil && sps != nil && pps != nil }

    mutating func observe(_ nal: HEVCNALUnit) {
        switch nal.type {
        case 32: vps = nal.bytes
        case 33: sps = nal.bytes
        case 34: pps = nal.bytes
        default: break
        }
    }

    var ordered: [Data]? {
        guard let vps, let sps, let pps else { return nil }
        return [vps, sps, pps]
    }

    /// HEVC's SPS declares how many pictures may need display-order delay.
    /// Zero is the common low-latency camera-link case; positive values require
    /// VideoToolbox temporal processing to avoid showing B pictures out of order.
    var maxReorderPictures: Int? {
        guard let sps else { return nil }
        return HEVCSPSParser.maxReorderPictures(sps)
    }
}

private enum HEVCSPSParser {
    static func maxReorderPictures(_ nal: Data) -> Int? {
        guard nal.count > 2 else { return nil }
        let ebsp = Array(nal.dropFirst(2))
        var rbsp = [UInt8]()
        rbsp.reserveCapacity(ebsp.count)
        var zeroCount = 0
        for byte in ebsp {
            if zeroCount >= 2 && byte == 3 {
                zeroCount = 0
                continue
            }
            rbsp.append(byte)
            zeroCount = byte == 0 ? zeroCount + 1 : 0
        }

        var bits = BitReader(rbsp)
        guard bits.skip(4), let maxSubLayersMinus1 = bits.read(3), bits.skip(1) else { return nil }
        guard skipProfileTierLevel(&bits, maxSubLayersMinus1: maxSubLayersMinus1) else { return nil }
        guard bits.readUE() != nil, let chromaFormatIDC = bits.readUE() else { return nil }
        if chromaFormatIDC == 3 && !bits.skip(1) { return nil }
        guard bits.readUE() != nil, bits.readUE() != nil, let conformanceWindow = bits.read(1) else { return nil }
        if conformanceWindow == 1 {
            for _ in 0..<4 {
                guard bits.readUE() != nil else { return nil }
            }
        }
        guard bits.readUE() != nil, bits.readUE() != nil, bits.readUE() != nil,
              let subLayerOrderingInfoPresent = bits.read(1) else { return nil }

        let firstLayer = subLayerOrderingInfoPresent == 1 ? 0 : maxSubLayersMinus1
        var maximum = 0
        for _ in firstLayer...maxSubLayersMinus1 {
            guard bits.readUE() != nil, let reorder = bits.readUE(), bits.readUE() != nil else { return nil }
            maximum = max(maximum, reorder)
        }
        return maximum
    }

    private static func skipProfileTierLevel(
        _ bits: inout BitReader,
        maxSubLayersMinus1: Int
    ) -> Bool {
        // general_profile_space/tier/idc, compatibility flags, four source
        // flags, 44 constraint bits, and general_level_idc.
        guard bits.skip(2 + 1 + 5 + 32 + 4 + 44 + 8) else { return false }
        var subLayerFlags = [(profile: Int, level: Int)]()
        for _ in 0..<maxSubLayersMinus1 {
            guard let profile = bits.read(1), let level = bits.read(1) else { return false }
            subLayerFlags.append((profile, level))
        }
        if maxSubLayersMinus1 > 0 {
            guard bits.skip((8 - maxSubLayersMinus1) * 2) else { return false }
        }
        for flags in subLayerFlags {
            if flags.profile == 1 && !bits.skip(88) { return false }
            if flags.level == 1 && !bits.skip(8) { return false }
        }
        return true
    }
}

private struct BitReader {
    private let bytes: [UInt8]
    private var bitOffset = 0

    init(_ bytes: [UInt8]) {
        self.bytes = bytes
    }

    mutating func read(_ count: Int) -> Int? {
        guard count >= 0, bitOffset + count <= bytes.count * 8 else { return nil }
        var value = 0
        for _ in 0..<count {
            value = (value << 1) | Int((bytes[bitOffset / 8] >> (7 - bitOffset % 8)) & 1)
            bitOffset += 1
        }
        return value
    }

    mutating func skip(_ count: Int) -> Bool {
        guard bitOffset + count <= bytes.count * 8 else { return false }
        bitOffset += count
        return true
    }

    mutating func readUE() -> Int? {
        var leadingZeros = 0
        while true {
            guard let bit = read(1) else { return nil }
            if bit == 1 { break }
            leadingZeros += 1
            guard leadingZeros <= 31 else { return nil }
        }
        guard leadingZeros > 0 else { return 0 }
        guard let suffix = read(leadingZeros) else { return nil }
        return (1 << leadingZeros) - 1 + suffix
    }
}
