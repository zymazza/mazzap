import AppKit
import Foundation

@MainActor
final class ViewerAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let options: ViewerOptions
    private var window: NSWindow?
    private var worker: VideoStreamWorker?
    private var telemetryWorker: TelemetryStreamWorker?
    private var telemetryHUD: TelemetryHUDView?
    private var videoContainer: VideoHUDContainerView?
    private var telemetryMailbox: LatestTelemetryMailbox?
    private var mailbox: LatestFrameMailbox?
    private var metrics: DecoderMetrics?
    private var metricsTimer: Timer?
    private var lastStatus = "starting"

    init(options: ViewerOptions) {
        self.options = options
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let videoView = try MetalVideoView(configured: ())
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 1500, height: 820),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            window.title = "VEIL DJI — connecting"
            window.minSize = NSSize(width: 1_050, height: 650)
            let container = VideoHUDContainerView(videoView: videoView)
            container.translatesAutoresizingMaskIntoConstraints = false
            let telemetryHUD = TelemetryHUDView(frame: .zero)
            telemetryHUD.translatesAutoresizingMaskIntoConstraints = false
            let dashboard = NSView(frame: .zero)
            dashboard.addSubview(container)
            dashboard.addSubview(telemetryHUD)
            NSLayoutConstraint.activate([
                container.leadingAnchor.constraint(equalTo: dashboard.leadingAnchor),
                container.topAnchor.constraint(equalTo: dashboard.topAnchor),
                container.bottomAnchor.constraint(equalTo: dashboard.bottomAnchor),
                container.trailingAnchor.constraint(equalTo: telemetryHUD.leadingAnchor),
                telemetryHUD.topAnchor.constraint(equalTo: dashboard.topAnchor),
                telemetryHUD.trailingAnchor.constraint(equalTo: dashboard.trailingAnchor),
                telemetryHUD.bottomAnchor.constraint(equalTo: dashboard.bottomAnchor),
                telemetryHUD.widthAnchor.constraint(equalToConstant: 370),
            ])
            self.telemetryHUD = telemetryHUD
            self.videoContainer = container
            window.contentView = dashboard
            window.delegate = self
            window.center()
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            self.window = window

            let mailbox = LatestFrameMailbox { [weak videoView] frame, completion in
                guard let videoView else {
                    completion(false)
                    return
                }
                videoView.render(frame, completion: completion)
            }
            self.mailbox = mailbox
            let pipeline = NativeVideoPipeline(
                frameHandler: { [weak mailbox] frame in mailbox?.submit(frame) },
                statusHandler: Self.statusHandler(for: self)
            )
            metrics = pipeline.metrics
            let worker = VideoStreamWorker(
                options: options,
                pipeline: pipeline,
                statusHandler: Self.statusHandler(for: self)
            )
            self.worker = worker
            worker.start()
            let telemetryMailbox = LatestTelemetryMailbox { [weak telemetryHUD] snapshot in
                telemetryHUD?.update(snapshot)
            }
            self.telemetryMailbox = telemetryMailbox
            let telemetryWorker = TelemetryStreamWorker(
                host: options.host,
                port: options.telemetryPort,
                token: options.token,
                snapshotHandler: { [weak telemetryMailbox] snapshot in
                    telemetryMailbox?.submit(snapshot)
                },
                statusHandler: Self.telemetryStatusHandler(for: self)
            )
            self.telemetryWorker = telemetryWorker
            telemetryWorker.start()
            metricsTimer = Timer.scheduledTimer(
                timeInterval: 1,
                target: self,
                selector: #selector(updateMetrics),
                userInfo: nil,
                repeats: true
            )
        } catch {
            fputs("veil-dji-video: \(error.localizedDescription)\n", stderr)
            NSApp.terminate(nil)
        }
    }

    func windowWillClose(_ notification: Notification) {
        worker?.stop()
        telemetryWorker?.stop()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        metricsTimer?.invalidate()
        worker?.stop()
        telemetryWorker?.stop()
    }

    @objc private func updateMetrics() {
        guard let snapshot = metrics?.snapshot() else { return }
        let median = snapshot.medianDecodeMS.map { String(format: "%.1f", $0) } ?? "—"
        let p95 = snapshot.p95DecodeMS.map { String(format: "%.1f", $0) } ?? "—"
        let replaced = mailbox?.replacementCount() ?? 0
        let frameAge = mailbox?.presentationAgeSeconds()
        let frameAgeText = frameAge.map { String(format: "%.1f", $0) } ?? "—"
        videoContainer?.updateVideoFreshness(ageSeconds: frameAge, status: lastStatus)
        telemetryHUD?.refreshFreshness()
        window?.title = "VEIL DJI — \(lastStatus) — video age \(frameAgeText)s, frames \(snapshot.decodedFrames), decode \(median)/\(p95) ms p50/p95, replacements \(replaced)"
    }

    nonisolated private static func statusHandler(
        for delegate: ViewerAppDelegate
    ) -> @Sendable (String) -> Void {
        { status in
            fputs("veil-dji-video: \(status)\n", stderr)
            DispatchQueue.main.async { [weak delegate] in
                if !status.contains("HEVC active") {
                    delegate?.mailbox?.invalidatePresentationFreshness()
                }
                delegate?.lastStatus = status
                delegate?.videoContainer?.updateVideoFreshness(
                    ageSeconds: delegate?.mailbox?.presentationAgeSeconds(),
                    status: status
                )
                delegate?.updateMetrics()
            }
        }
    }

    nonisolated private static func telemetryStatusHandler(
        for delegate: ViewerAppDelegate
    ) -> @Sendable (String) -> Void {
        { status in
            fputs("veil-dji-video: \(status)\n", stderr)
            DispatchQueue.main.async { [weak delegate] in
                delegate?.telemetryHUD?.updateStatus(status)
            }
        }
    }
}

final class LatestTelemetryMailbox: @unchecked Sendable {
    typealias Renderer = @MainActor (TelemetryDisplaySnapshot) -> Void

    private let lock = NSLock()
    private var latest: TelemetryDisplaySnapshot?
    private var scheduled = false
    private let renderer: Renderer

    init(renderer: @escaping Renderer) {
        self.renderer = renderer
    }

    func submit(_ snapshot: TelemetryDisplaySnapshot) {
        let shouldSchedule = lock.withLock { () -> Bool in
            latest = snapshot
            guard !scheduled else { return false }
            scheduled = true
            return true
        }
        if shouldSchedule {
            DispatchQueue.main.async { [weak self] in self?.drain() }
        }
    }

    @MainActor
    private func drain() {
        let snapshot = lock.withLock { () -> TelemetryDisplaySnapshot? in
            scheduled = false
            defer { latest = nil }
            return latest
        }
        if let snapshot { renderer(snapshot) }
    }
}

@main
struct VeilDJIVideoMain {
    @MainActor
    static func main() {
        do {
            let options = try ViewerOptions.parse(Array(CommandLine.arguments.dropFirst()))
            let application = NSApplication.shared
            application.setActivationPolicy(.regular)
            let delegate = ViewerAppDelegate(options: options)
            application.delegate = delegate
            application.run()
        } catch {
            fputs("veil-dji-video: \(error.localizedDescription)\n", stderr)
            exit(2)
        }
    }
}
