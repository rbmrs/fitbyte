import AppKit
import Foundation

@MainActor
final class ShrinkyViewModel: ObservableObject {
    @Published var settings = EncodeSettings()
    @Published var media: MediaInfo?
    @Published var preview: PreviewResponse?
    @Published var progressFraction = 0.0
    @Published var progressPhase = ""
    @Published var progressSpeed = ""
    @Published var statusMessage = "Drop a media file or choose one to begin."
    @Published var errorMessage: String?
    @Published var result: ConversionResult?
    @Published var isConverting = false
    @Published var backendReady = false

    private let backend: ShrinkyBackend?
    private var previewTask: Task<Void, Never>?
    private var conversionTask: Task<Void, Never>?

    init() {
        do {
            self.backend = try ShrinkyBackend()
            self.backendReady = true
        } catch {
            self.backend = nil
            self.backendReady = false
            self.errorMessage = error.localizedDescription
            self.statusMessage = "Backend unavailable."
        }
    }

    var commandPreviewText: String {
        guard let preview else {
            return "Select input and output to preview the ffmpeg command."
        }
        return preview.commands.joined(separator: "\n")
    }

    var estimateText: String {
        guard let estimate = preview?.estimate else {
            return "No estimate yet."
        }
        if let message = estimate.message {
            return message
        }
        let target = estimate.targetSizeMb.map { String(format: "%.2f MB", $0) } ?? "n/a"
        let video = estimate.videoKbps.map { "\($0) kbps" } ?? "none"
        let audio = estimate.audioKbps.map { "\($0) kbps" } ?? "none"
        return "Target \(target)\nVideo \(video)\nAudio \(audio)"
    }

    func chooseInput() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.movie, .audio]
        if panel.runModal() == .OK, let url = panel.url {
            setInput(url)
        }
    }

    func chooseOutput() {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = settings.outputURL?.lastPathComponent ?? "output.mp4"
        if let directory = settings.outputURL?.deletingLastPathComponent() {
            panel.directoryURL = directory
        } else if let input = settings.inputURL {
            panel.directoryURL = input.deletingLastPathComponent()
        }
        if panel.runModal() == .OK, let url = panel.url {
            settings.outputURL = url
            schedulePreview()
        }
    }

    func setInput(_ url: URL) {
        settings.inputURL = url
        media = nil
        preview = nil
        result = nil
        errorMessage = nil
        statusMessage = "Probing \(url.lastPathComponent)..."
        Task {
            await probeSelectedInput()
        }
    }

    func schedulePreview() {
        previewTask?.cancel()
        previewTask = Task {
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else {
                return
            }
            await refreshPreview()
        }
    }

    func refreshPreview() async {
        guard let backend else {
            return
        }
        guard settings.canRequestPreview else {
            preview = nil
            return
        }
        do {
            errorMessage = nil
            preview = try await backend.preview(settings: settings)
            statusMessage = "Ready to convert."
        } catch {
            preview = nil
            errorMessage = error.localizedDescription
            statusMessage = "Preview unavailable."
        }
    }

    func convert() {
        guard let backend else {
            return
        }
        guard settings.canRequestPreview else {
            errorMessage = "Choose input and output paths first."
            return
        }
        conversionTask?.cancel()
        isConverting = true
        result = nil
        errorMessage = nil
        progressFraction = 0
        progressPhase = "Starting"
        progressSpeed = ""
        statusMessage = "Converting..."

        let currentSettings = settings
        conversionTask = Task {
            do {
                let finished = try await backend.convert(settings: currentSettings) { [weak self] event in
                    Task { @MainActor in
                        self?.applyProgressEvent(event)
                    }
                }
                result = finished
                progressFraction = 1
                progressPhase = "Complete"
                statusMessage = "Finished \(URL(fileURLWithPath: finished.outputPath).lastPathComponent)."
            } catch is CancellationError {
                progressPhase = "Canceled"
                statusMessage = "Conversion canceled."
            } catch {
                errorMessage = error.localizedDescription
                statusMessage = "Conversion failed."
            }
            isConverting = false
        }
    }

    func cancel() {
        conversionTask?.cancel()
        statusMessage = "Cancel requested."
    }

    func revealOutput() {
        guard let result else {
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: result.outputPath)])
    }

    private func probeSelectedInput() async {
        guard let backend, let inputURL = settings.inputURL else {
            return
        }
        do {
            let response = try await backend.probe(inputURL: inputURL)
            media = response.media
            if settings.outputURL == nil {
                settings.outputURL = URL(fileURLWithPath: response.suggestedOutput)
            }
            statusMessage = "Loaded \(inputURL.lastPathComponent)."
            await refreshPreview()
        } catch {
            errorMessage = error.localizedDescription
            statusMessage = "Probe failed."
        }
    }

    private func applyProgressEvent(_ event: ProgressEvent) {
        if let fraction = event.fraction {
            progressFraction = max(0, min(1, fraction))
        }
        if let phase = event.phase {
            progressPhase = phase
        }
        if let speed = event.speed {
            progressSpeed = speed
        }
        if let error = event.error {
            errorMessage = error
        }
    }
}
