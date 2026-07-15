import AppKit
import Foundation

@MainActor
final class TelemetryHUDView: NSVisualEffectView {
    private let connectionLabel = NSTextField(labelWithString: "TELEMETRY CONNECTING")
    private let flightLabel = NSTextField(labelWithString: "Aircraft offline")
    private let navigationLabel = NSTextField(labelWithString: "GPS —  Alt —")
    private let powerLabel = NSTextField(labelWithString: "Battery —  Temp —")
    private let transportLabel = NSTextField(labelWithString: "Control —  Queue —")
    private let avoidanceLabel = NSTextField(labelWithString: "DJI PERCEPTION —")
    private let attitudeView = AttitudeIndicatorView(frame: .zero)
    private let obstacleView = ObstacleRadarView(frame: .zero)
    private let rawText = NSTextView(frame: .zero)
    private var lastRawRefresh = Date.distantPast
    private var lastSnapshotAt: ContinuousClock.Instant?
    private var transportConnected = false
    private var transportStatus = "connecting"

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        material = .hudWindow
        blendingMode = .withinWindow
        state = .active
        wantsLayer = true

        connectionLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        connectionLabel.textColor = .secondaryLabelColor
        for label in [flightLabel, navigationLabel, powerLabel, transportLabel] {
            label.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
            label.lineBreakMode = .byTruncatingTail
        }
        avoidanceLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        avoidanceLabel.maximumNumberOfLines = 2
        avoidanceLabel.lineBreakMode = .byWordWrapping

        let cameraLabel = NSTextField(labelWithString: "MAIN CAMERA + SDK SENSOR DATA")
        cameraLabel.font = .systemFont(ofSize: 11, weight: .medium)
        cameraLabel.textColor = .secondaryLabelColor

        let attitudeLabel = sectionLabel("ATTITUDE")
        let obstacleLabel = sectionLabel("OBSTACLE RANGES · RAW DJI VECTOR (BODY MAP UNVERIFIED)")
        let rawLabel = sectionLabel("COMPLETE TELEMETRY")

        attitudeView.translatesAutoresizingMaskIntoConstraints = false
        obstacleView.translatesAutoresizingMaskIntoConstraints = false

        rawText.isEditable = false
        rawText.isSelectable = true
        rawText.drawsBackground = false
        rawText.textColor = .labelColor
        rawText.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
        rawText.textContainerInset = NSSize(width: 4, height: 4)
        rawText.minSize = .zero
        rawText.maxSize = NSSize(
            width: CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )
        rawText.isVerticallyResizable = true
        rawText.isHorizontallyResizable = false
        rawText.autoresizingMask = [.width]
        rawText.textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        rawText.textContainer?.widthTracksTextView = true
        rawText.string = "Waiting for telemetry…"
        let scroll = NSScrollView(frame: .zero)
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.documentView = rawText

        let stack = NSStackView(views: [
            cameraLabel,
            connectionLabel,
            flightLabel,
            navigationLabel,
            powerLabel,
            transportLabel,
            separator(),
            attitudeLabel,
            attitudeView,
            avoidanceLabel,
            obstacleLabel,
            obstacleView,
            rawLabel,
            scroll,
        ])
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 7
        addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 12),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -12),
            attitudeView.widthAnchor.constraint(equalTo: stack.widthAnchor),
            attitudeView.heightAnchor.constraint(equalToConstant: 82),
            obstacleView.widthAnchor.constraint(equalTo: stack.widthAnchor),
            obstacleView.heightAnchor.constraint(equalToConstant: 180),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 120),
        ])
        stack.setHuggingPriority(.defaultLow, for: .vertical)
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    func updateStatus(_ status: String) {
        transportStatus = status
        transportConnected = status == "telemetry connected"
        refreshFreshness()
    }

    func update(_ snapshot: TelemetryDisplaySnapshot) {
        lastSnapshotAt = .now
        transportConnected = true
        transportStatus = "telemetry connected"
        refreshFreshness()
        let state = snapshot.aircraftConnected
            ? (snapshot.isFlying ? "FLYING" : (snapshot.motorsOn ? "MOTORS ON" : "READY"))
            : "AIRCRAFT OFFLINE"
        flightLabel.stringValue = "\(state)  \(snapshot.flightMode)  \(snapshot.authority)"
        navigationLabel.stringValue = String(
            format: "GPS %@/%@  Alt %@ m  Yaw %@°",
            format(snapshot.gpsSatellites, decimals: 0),
            snapshot.gpsSignal,
            format(snapshot.altitudeM, decimals: 1),
            format(snapshot.yawDeg, decimals: 0)
        )
        powerLabel.stringValue = String(
            format: "Battery %@%%  Temp %@°C  V N/E/D %@/%@/%@",
            format(snapshot.batteryPercent, decimals: 0),
            format(snapshot.batteryTemperatureC, decimals: 1),
            format(snapshot.northMPS, decimals: 1),
            format(snapshot.eastMPS, decimals: 1),
            format(snapshot.downMPS, decimals: 1)
        )
        transportLabel.stringValue = String(
            format: "Control %@ ms  Queue %@ ms  Seq %@",
            format(snapshot.controlLatencyMS, decimals: 0),
            format(snapshot.telemetryQueueAgeMS, decimals: 0),
            snapshot.sequence.map(String.init) ?? "—"
        )
        attitudeView.pitchDeg = snapshot.pitchDeg ?? 0
        attitudeView.rollDeg = snapshot.rollDeg ?? 0
        attitudeView.yawDeg = snapshot.yawDeg ?? 0
        obstacleView.distances = snapshot.obstacleMeters
        obstacleView.samples = snapshot.horizontalObstacleSamples
        obstacleView.sampleCount = snapshot.horizontalObstacleSampleCount
        obstacleView.intervalDeg = snapshot.horizontalObstacleIntervalDeg
        let ranges = snapshot.obstacleDataFresh
            ? "RANGES LIVE \(formatAge(snapshot.obstacleDataAgeMS))"
            : "RANGES STALE \(formatAge(snapshot.obstacleDataAgeMS))"
        let enabled = snapshot.avoidanceEffectiveEnabled.map { $0 ? "ON" : "OFF" } ?? "—"
        let info = snapshot.avoidanceInformationFresh ? "INFO LIVE" : "INFO STALE"
        avoidanceLabel.stringValue = "DJI PERCEPTION \(snapshot.avoidanceType) · effective \(enabled) · \(info)\n\(ranges) · vector origin/order unverified"
        avoidanceLabel.textColor = snapshot.obstacleDataFresh ? .labelColor : .systemRed

        if !snapshot.fields.isEmpty, Date().timeIntervalSince(lastRawRefresh) >= 0.5 {
            lastRawRefresh = Date()
            rawText.string = snapshot.fields
                .map { "\($0.path) = \($0.value)" }
                .joined(separator: "\n")
        }
    }

    func refreshFreshness(at now: ContinuousClock.Instant = .now) {
        let age = lastSnapshotAt.map { instant -> Double in
            let components = instant.duration(to: now).components
            return Double(components.seconds) +
                Double(components.attoseconds) / 1_000_000_000_000_000_000.0
        }
        let fresh = transportConnected && (age.map { $0 <= 1.0 } ?? false)
        if fresh {
            connectionLabel.stringValue = String(format: "TELEMETRY LIVE · %.1fs", age ?? 0)
            connectionLabel.textColor = .systemGreen
        } else {
            let ageText = age.map { String(format: "%.1fs", $0) } ?? "no sample"
            connectionLabel.stringValue = "TELEMETRY STALE · \(ageText) · \(transportStatus)".uppercased()
            connectionLabel.textColor = .systemRed
        }
    }

    private func sectionLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 10, weight: .medium)
        label.textColor = .secondaryLabelColor
        return label
    }

    private func separator() -> NSBox {
        let box = NSBox(frame: .zero)
        box.boxType = .separator
        return box
    }

    private func format(_ value: Double?, decimals: Int) -> String {
        guard let value, value.isFinite else { return "—" }
        return String(format: "%.*f", decimals, value)
    }

    private func formatAge(_ milliseconds: Double?) -> String {
        guard let milliseconds, milliseconds.isFinite else { return "age —" }
        return String(format: "age %.0fms", milliseconds)
    }
}

@MainActor
final class AttitudeIndicatorView: NSView {
    var pitchDeg = 0.0 { didSet { needsDisplay = true } }
    var rollDeg = 0.0 { didSet { needsDisplay = true } }
    var yawDeg = 0.0 { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let center = NSPoint(x: bounds.midX, y: bounds.midY)
        let context = NSGraphicsContext.current?.cgContext
        context?.saveGState()
        context?.translateBy(x: center.x, y: center.y)
        context?.rotate(by: CGFloat(-rollDeg * .pi / 180))
        context?.translateBy(x: 0, y: CGFloat(pitchDeg * 1.4))
        NSColor.systemBlue.withAlphaComponent(0.85).setStroke()
        let horizon = NSBezierPath()
        horizon.move(to: NSPoint(x: -bounds.width, y: 0))
        horizon.line(to: NSPoint(x: bounds.width, y: 0))
        horizon.lineWidth = 2
        horizon.stroke()
        context?.restoreGState()

        NSColor.labelColor.setStroke()
        let aircraft = NSBezierPath()
        aircraft.move(to: NSPoint(x: center.x - 28, y: center.y))
        aircraft.line(to: NSPoint(x: center.x - 6, y: center.y))
        aircraft.line(to: NSPoint(x: center.x, y: center.y - 5))
        aircraft.line(to: NSPoint(x: center.x + 6, y: center.y))
        aircraft.line(to: NSPoint(x: center.x + 28, y: center.y))
        aircraft.lineWidth = 2
        aircraft.stroke()

        let text = String(format: "P %.1f°   R %.1f°   H %.0f°", pitchDeg, rollDeg, yawDeg)
        text.draw(
            at: NSPoint(x: 5, y: 3),
            withAttributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .regular),
                .foregroundColor: NSColor.labelColor,
            ]
        )
    }
}

@MainActor
final class ObstacleRadarView: NSView {
    var distances: [String: Double] = [:] { didSet { needsDisplay = true } }
    var samples: [ObstacleRangeSample] = [] { didSet { needsDisplay = true } }
    var sampleCount: Int? { didSet { needsDisplay = true } }
    var intervalDeg: Double? { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let radius = min(bounds.width * 0.30, bounds.height * 0.36)
        let center = NSPoint(x: bounds.midX, y: bounds.midY + 8)
        NSColor.separatorColor.setStroke()
        for fraction in [0.33, 0.66, 1.0] {
            let ringRadius = radius * fraction
            NSBezierPath(
                ovalIn: NSRect(
                    x: center.x - ringRadius,
                    y: center.y - ringRadius,
                    width: ringRadius * 2,
                    height: ringRadius * 2
                )
            ).stroke()
        }
        let axes = NSBezierPath()
        axes.move(to: NSPoint(x: center.x - radius, y: center.y))
        axes.line(to: NSPoint(x: center.x + radius, y: center.y))
        axes.move(to: NSPoint(x: center.x, y: center.y - radius))
        axes.line(to: NSPoint(x: center.x, y: center.y + radius))
        axes.stroke()

        for sample in samples {
            guard let sampleCount, sampleCount > 0 else { continue }
            let angle = Double(sample.index) / Double(sampleCount) * 2 * .pi
            let normalized = CGFloat(min(sample.distanceM, 10) / 10)
            let point = NSPoint(
                x: center.x + CGFloat(sin(angle)) * radius * normalized,
                y: center.y + CGFloat(cos(angle)) * radius * normalized
            )
            color(for: sample.distanceM).withAlphaComponent(0.85).setFill()
            NSBezierPath(
                ovalIn: NSRect(x: point.x - 2, y: point.y - 2, width: 4, height: 4)
            ).fill()
        }

        let vertical = "UP \(formatDistance(distances["upward"]))   DOWN \(formatDistance(distances["downward"]))"
        vertical.draw(
            at: NSPoint(x: 5, y: 2),
            withAttributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .regular),
                .foregroundColor: NSColor.labelColor,
            ]
        )
        let vectorText = "INDEX 0 ↑ · ORIGIN/ORDER UNVERIFIED · Δ \(intervalDeg.map { String(format: "%.1f°", $0) } ?? "—")"
        vectorText.draw(
            at: NSPoint(x: 5, y: bounds.height - 14),
            withAttributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 9, weight: .medium),
                .foregroundColor: NSColor.systemOrange,
            ]
        )

        NSColor.labelColor.setFill()
        let aircraft = NSBezierPath()
        aircraft.move(to: NSPoint(x: center.x, y: center.y + 8))
        aircraft.line(to: NSPoint(x: center.x - 6, y: center.y - 6))
        aircraft.line(to: NSPoint(x: center.x + 6, y: center.y - 6))
        aircraft.close()
        aircraft.fill()
    }

    private func color(for distance: Double?) -> NSColor {
        guard let distance else { return .secondaryLabelColor }
        if distance < 1.5 { return .systemRed }
        if distance < 3.0 { return .systemOrange }
        return .systemGreen
    }

    private func formatDistance(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "—" }
        return String(format: "%.1fm", value)
    }
}
