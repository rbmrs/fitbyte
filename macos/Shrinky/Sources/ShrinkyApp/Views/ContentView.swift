import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject private var viewModel = ShrinkyViewModel()
    @State private var inputDropTargeted = false

    var body: some View {
        VStack(spacing: 0) {
            header
            VStack(spacing: 24) {
                targetControl
                HStack(alignment: .center, spacing: 22) {
                    inputZone
                    resultZone
                }
                .frame(maxHeight: .infinity)
                actionRow
            }
            .padding(28)
            footer
        }
        .frame(minWidth: 900, minHeight: 560)
        .background(Color(nsColor: .windowBackgroundColor))
        .onChange(of: viewModel.settings) {
            viewModel.schedulePreview()
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text("Shrinky")
                .font(.title3.weight(.semibold))
            Circle()
                .fill(viewModel.backendReady ? Color.green : Color.orange)
                .frame(width: 8, height: 8)
            Text(viewModel.backendReady ? "Ready" : "Backend missing")
                .foregroundStyle(.secondary)
                .font(.callout)
            Spacer()
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
    }

    private var targetControl: some View {
        HStack(spacing: 10) {
            Text("Target")
                .foregroundStyle(.secondary)
            TextField("10", text: $viewModel.settings.targetSizeMB)
                .textFieldStyle(.plain)
                .font(.system(size: 28, weight: .semibold, design: .rounded))
                .multilineTextAlignment(.center)
                .frame(width: 104)
                .padding(.vertical, 8)
                .padding(.horizontal, 10)
                .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 8))
            Text("MB")
                .foregroundStyle(.secondary)
        }
        .font(.title3)
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private var inputZone: some View {
        DropPanel(
            title: "Input",
            subtitle: inputSubtitle,
            icon: "film",
            accent: .blue,
            isTargeted: inputDropTargeted,
            primaryButton: PanelButton(title: "Choose File", systemImage: "folder") {
                viewModel.chooseInput()
            },
            secondaryButton: nil
        )
        .onDrop(of: [UTType.fileURL.identifier], isTargeted: $inputDropTargeted) { providers in
            guard let provider = providers.first else {
                return false
            }
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                guard let data = item as? Data,
                      let url = URL(dataRepresentation: data, relativeTo: nil) else {
                    return
                }
                Task { @MainActor in
                    viewModel.setInput(url)
                }
            }
            return true
        }
    }

    private var resultZone: some View {
        DropPanel(
            title: resultTitle,
            subtitle: resultSubtitle,
            icon: resultIcon,
            accent: resultAccent,
            isTargeted: false,
            primaryButton: resultPrimaryButton,
            secondaryButton: resultSecondaryButton,
            progress: progressValue
        )
    }

    private var actionRow: some View {
        HStack(spacing: 12) {
            if viewModel.isConverting {
                Button {
                    viewModel.cancel()
                } label: {
                    Label("Cancel", systemImage: "xmark")
                        .frame(width: 120)
                }
                .buttonStyle(.bordered)
            }

            Button {
                viewModel.convert()
            } label: {
                Label(viewModel.isConverting ? "Shrinking..." : "Shrink", systemImage: "arrow.down.circle")
                    .frame(width: 156)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!viewModel.settings.canRequestPreview || viewModel.isConverting || !viewModel.backendReady)
        }
    }

    private var footer: some View {
        HStack {
            Text(viewModel.statusMessage)
                .lineLimit(1)
                .foregroundColor(viewModel.errorMessage == nil ? .secondary : .red)
            Spacer()
        }
        .font(.callout)
        .padding(.horizontal, 24)
        .padding(.bottom, 18)
    }

    private var inputSubtitle: String {
        guard let url = viewModel.settings.inputURL else {
            return "Drop a video or audio file here"
        }
        var lines = [url.lastPathComponent]
        if let media = viewModel.media {
            lines.append(mediaSummary(media))
        }
        return lines.joined(separator: "\n")
    }

    private var resultTitle: String {
        if viewModel.result != nil {
            return "Done"
        }
        if viewModel.isConverting {
            return "Result"
        }
        return "Output"
    }

    private var resultSubtitle: String {
        if let result = viewModel.result {
            let size = ByteCountFormatter.string(fromByteCount: Int64(result.sizeBytes), countStyle: .file)
            return "\(URL(fileURLWithPath: result.outputPath).lastPathComponent)\n\(size)"
        }
        if viewModel.isConverting {
            let percent = Int(viewModel.progressFraction * 100)
            let speed = viewModel.progressSpeed.isEmpty ? "" : "\nSpeed \(viewModel.progressSpeed)"
            return "\(viewModel.progressPhase)\n\(percent)%\(speed)"
        }
        if let output = viewModel.settings.outputURL {
            if let estimate = viewModel.preview?.estimate {
                return "\(output.lastPathComponent)\n\(estimateSummary(estimate))"
            }
            return output.lastPathComponent
        }
        return "The compressed file will appear here"
    }

    private var resultIcon: String {
        if viewModel.result != nil {
            return "checkmark.circle"
        }
        if viewModel.isConverting {
            return "clock"
        }
        return "arrow.down.doc"
    }

    private var resultAccent: Color {
        if viewModel.result != nil {
            return .green
        }
        return .accentColor
    }

    private var progressValue: Double? {
        viewModel.isConverting ? viewModel.progressFraction : nil
    }

    private var resultPrimaryButton: PanelButton? {
        if viewModel.result != nil {
            return PanelButton(title: "Reveal", systemImage: "arrow.up.forward.app") {
                viewModel.revealOutput()
            }
        }
        if viewModel.settings.outputURL == nil {
            return PanelButton(title: "Save As", systemImage: "square.and.arrow.down") {
                viewModel.chooseOutput()
            }
        }
        return nil
    }

    private var resultSecondaryButton: PanelButton? {
        guard viewModel.result == nil, viewModel.settings.outputURL != nil else {
            return nil
        }
        return PanelButton(title: "Change", systemImage: "pencil") {
            viewModel.chooseOutput()
        }
    }

    private func mediaSummary(_ media: MediaInfo) -> String {
        let size = ByteCountFormatter.string(fromByteCount: Int64(media.sizeBytes), countStyle: .file)
        let duration = String(format: "%.1fs", media.duration)
        if media.hasVideo, let width = media.width, let height = media.height {
            return "\(size) · \(duration) · \(width)x\(height)"
        }
        return "\(size) · \(duration)"
    }

    private func estimateSummary(_ estimate: EstimateInfo) -> String {
        if let target = estimate.targetSizeMb {
            return String(format: "Target %.0f MB", target)
        }
        return estimate.message ?? "Ready"
    }
}

private struct PanelButton {
    let title: String
    let systemImage: String
    let action: () -> Void
}

private struct DropPanel: View {
    let title: String
    let subtitle: String
    let icon: String
    let accent: Color
    let isTargeted: Bool
    let primaryButton: PanelButton?
    let secondaryButton: PanelButton?
    var progress: Double?

    var body: some View {
        VStack(spacing: 18) {
            Spacer(minLength: 10)
            Image(systemName: icon)
                .font(.system(size: 46, weight: .regular))
                .foregroundStyle(accent)
                .symbolEffect(.pulse, isActive: progress != nil)
            VStack(spacing: 8) {
                Text(title)
                    .font(.title2.weight(.semibold))
                Text(subtitle)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(4)
                    .frame(maxWidth: 280)
            }
            if let progress {
                ProgressView(value: progress)
                    .frame(width: 220)
            }
            HStack(spacing: 10) {
                if let secondaryButton {
                    Button {
                        secondaryButton.action()
                    } label: {
                        Label(secondaryButton.title, systemImage: secondaryButton.systemImage)
                    }
                    .buttonStyle(.bordered)
                }
                if let primaryButton {
                    Button {
                        primaryButton.action()
                    } label: {
                        Label(primaryButton.title, systemImage: primaryButton.systemImage)
                    }
                    .buttonStyle(.bordered)
                }
            }
            Spacer(minLength: 10)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(isTargeted ? accent.opacity(0.10) : Color.secondary.opacity(0.045))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(isTargeted ? accent : Color.secondary.opacity(0.22), style: StrokeStyle(lineWidth: 1.2, dash: [7, 6]))
        )
        .animation(.easeOut(duration: 0.16), value: isTargeted)
    }
}
