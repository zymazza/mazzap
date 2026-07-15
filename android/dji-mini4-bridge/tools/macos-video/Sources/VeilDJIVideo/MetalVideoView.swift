import AppKit
import CoreImage
import CoreVideo
import Metal
import QuartzCore

@MainActor
final class MetalVideoView: NSView {
    private let metalLayer: CAMetalLayer
    private let commandQueue: MTLCommandQueue
    private let context: CIContext
    private let colorSpace = CGColorSpaceCreateDeviceRGB()

    init(configured: Void) throws {
        guard let device = MTLCreateSystemDefaultDevice(),
              let commandQueue = device.makeCommandQueue() else {
            throw ViewerError.metalUnavailable
        }
        self.commandQueue = commandQueue
        self.context = CIContext(
            mtlDevice: device,
            options: [.cacheIntermediates: false]
        )
        self.metalLayer = CAMetalLayer()
        metalLayer.device = device
        metalLayer.pixelFormat = .bgra8Unorm
        metalLayer.framebufferOnly = false
        metalLayer.maximumDrawableCount = 2
        metalLayer.displaySyncEnabled = true
        metalLayer.presentsWithTransaction = false
        super.init(frame: .zero)
        wantsLayer = true
        layer = metalLayer
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    override func layout() {
        super.layout()
        let scale = window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 1
        metalLayer.contentsScale = scale
        metalLayer.drawableSize = CGSize(
            width: max(1, bounds.width * scale),
            height: max(1, bounds.height * scale)
        )
    }

    /// Renders one decoded frame with exactly one GPU command buffer in flight.
    /// The mailbox invokes completion before offering its newest replacement.
    func render(_ pixelBuffer: CVPixelBuffer, completion: @escaping @Sendable (Bool) -> Void) {
        guard let drawable = metalLayer.nextDrawable(),
              let commandBuffer = commandQueue.makeCommandBuffer() else {
            completion(false)
            return
        }

        let source = CIImage(cvPixelBuffer: pixelBuffer)
        let destination = CGRect(origin: .zero, size: metalLayer.drawableSize)
        let scale = min(
            destination.width / max(source.extent.width, 1),
            destination.height / max(source.extent.height, 1)
        )
        var image = source.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let offsetX = destination.midX - image.extent.midX
        let offsetY = destination.midY - image.extent.midY
        image = image.transformed(by: CGAffineTransform(translationX: offsetX, y: offsetY))
        let background = CIImage(color: .black).cropped(to: destination)
        let composed = image.composited(over: background)

        context.render(
            composed,
            to: drawable.texture,
            commandBuffer: commandBuffer,
            bounds: destination,
            colorSpace: colorSpace
        )
        commandBuffer.present(drawable)
        commandBuffer.addCompletedHandler { buffer in
            completion(buffer.status == .completed)
        }
        commandBuffer.commit()
    }
}

/// Single-window composition root.  The transparent overlay intentionally has
/// no telemetry assumptions yet; future flight/obstacle HUD views can be added
/// above the Metal video without creating another window or touching decode.
@MainActor
final class VideoHUDContainerView: NSView {
    let videoView: MetalVideoView
    let hudOverlay = HUDOverlayView(frame: .zero)

    init(videoView: MetalVideoView) {
        self.videoView = videoView
        super.init(frame: .zero)
        videoView.translatesAutoresizingMaskIntoConstraints = false
        hudOverlay.translatesAutoresizingMaskIntoConstraints = false
        addSubview(videoView)
        addSubview(hudOverlay)
        NSLayoutConstraint.activate([
            videoView.leadingAnchor.constraint(equalTo: leadingAnchor),
            videoView.trailingAnchor.constraint(equalTo: trailingAnchor),
            videoView.topAnchor.constraint(equalTo: topAnchor),
            videoView.bottomAnchor.constraint(equalTo: bottomAnchor),
            hudOverlay.leadingAnchor.constraint(equalTo: leadingAnchor),
            hudOverlay.trailingAnchor.constraint(equalTo: trailingAnchor),
            hudOverlay.topAnchor.constraint(equalTo: topAnchor),
            hudOverlay.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    func updateVideoFreshness(ageSeconds: Double?, status: String) {
        hudOverlay.updateVideoFreshness(ageSeconds: ageSeconds, status: status)
    }
}

/// Empty overlay regions pass pointer events through to the video surface;
/// interactive HUD subviews added later still receive their own events.
@MainActor
final class HUDOverlayView: NSView {
    private let staleLabel = NSTextField(labelWithString: "VIDEO STALE — WAITING FOR FRAME")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        staleLabel.translatesAutoresizingMaskIntoConstraints = false
        staleLabel.font = .systemFont(ofSize: 22, weight: .bold)
        staleLabel.textColor = .white
        staleLabel.backgroundColor = .systemRed.withAlphaComponent(0.88)
        staleLabel.drawsBackground = true
        staleLabel.alignment = .center
        staleLabel.maximumNumberOfLines = 2
        staleLabel.wantsLayer = true
        staleLabel.layer?.cornerRadius = 8
        addSubview(staleLabel)
        NSLayoutConstraint.activate([
            staleLabel.centerXAnchor.constraint(equalTo: centerXAnchor),
            staleLabel.centerYAnchor.constraint(equalTo: centerYAnchor),
            staleLabel.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor, constant: -40),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    func updateVideoFreshness(ageSeconds: Double?, status: String) {
        let active = status.contains("HEVC active")
        let fresh = active && (ageSeconds.map { $0 <= 1.0 } ?? false)
        staleLabel.isHidden = fresh
        guard !fresh else { return }
        let age = ageSeconds.map { String(format: "%.1fs", $0) } ?? "no frame"
        staleLabel.stringValue = "VIDEO STALE · \(age)\n\(status)"
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        let result = super.hitTest(point)
        return result === self ? nil : result
    }
}

/// A one-slot decoded-frame mailbox. Dropping here is reference-safe because
/// VideoToolbox has already decoded every compressed picture. This prevents a
/// slow screen or hidden window from ever growing glass-to-glass latency.
final class LatestFrameMailbox: @unchecked Sendable {
    typealias Renderer = @MainActor (CVPixelBuffer, @escaping @Sendable (Bool) -> Void) -> Void

    private let lock = NSLock()
    private var latest: CVPixelBuffer?
    private var renderScheduled = false
    private var renderInFlight = false
    private var replacedFrames = 0
    private var lastPresentedAt: ContinuousClock.Instant?
    private let renderer: Renderer

    init(renderer: @escaping Renderer) {
        self.renderer = renderer
    }

    func submit(_ pixelBuffer: CVPixelBuffer) {
        let shouldSchedule = lock.withLock { () -> Bool in
            if latest != nil { replacedFrames += 1 }
            latest = pixelBuffer
            guard !renderScheduled, !renderInFlight else { return false }
            renderScheduled = true
            return true
        }
        if shouldSchedule {
            DispatchQueue.main.async { [weak self] in
                self?.drainOnMainActor()
            }
        }
    }

    func replacementCount() -> Int {
        lock.withLock { replacedFrames }
    }

    func presentationAgeSeconds(at now: ContinuousClock.Instant = .now) -> Double? {
        lock.withLock {
            guard let lastPresentedAt else { return nil }
            let components = lastPresentedAt.duration(to: now).components
            return Double(components.seconds) +
                Double(components.attoseconds) / 1_000_000_000_000_000_000.0
        }
    }

    func invalidatePresentationFreshness() {
        lock.withLock { lastPresentedAt = nil }
    }

    @MainActor
    private func drainOnMainActor() {
        let frame = lock.withLock { () -> CVPixelBuffer? in
            renderScheduled = false
            guard !renderInFlight, let frame = latest else { return nil }
            latest = nil
            renderInFlight = true
            return frame
        }
        guard let frame else { return }
        renderer(frame) { [weak self] presented in
            self?.renderCompleted(presented: presented)
        }
    }

    private func renderCompleted(presented: Bool) {
        let shouldSchedule = lock.withLock { () -> Bool in
            renderInFlight = false
            if presented { lastPresentedAt = .now }
            guard latest != nil, !renderScheduled else { return false }
            renderScheduled = true
            return true
        }
        if shouldSchedule {
            DispatchQueue.main.async { [weak self] in
                self?.drainOnMainActor()
            }
        }
    }
}
