import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject private var viewModel = ShrinkyViewModel()
    @State private var isDropTargeted = false

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            HStack(spacing: 0) {
                controlsPane
                    .frame(minWidth: 420, idealWidth: 460, maxWidth: 520)
                Divider()
                detailPane
                    .frame(minWidth: 480)
            }
            Divider()
            footer
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onChange(of: viewModel.settings) {
            viewModel.schedulePreview()
        }
    }

    private var toolbar: some View {
        HStack(spacing: 12) {
            Text("Shrinky")
                .font(.title3.weight(.semibold))
            Label(viewModel.backendReady ? "Backend ready" : "Backend missing", systemImage: viewModel.backendReady ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(viewModel.backendReady ? .green : .orange)
                .font(.callout)
            Spacer()
            Button {
                viewModel.chooseInput()
            } label: {
                Label("Open", systemImage: "folder")
            }
            .disabled(viewModel.isConverting)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    private var controlsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                sourceSection
                targetSection
                videoSection
                audioSection
                HStack {
                    Button {
                        viewModel.convert()
                    } label: {
                        Label("Convert", systemImage: "arrow.triangle.2.circlepath")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!viewModel.settings.canRequestPreview || viewModel.isConverting || !viewModel.backendReady)

                    if viewModel.isConverting {
                        Button {
                            viewModel.cancel()
                        } label: {
                            Label("Cancel", systemImage: "xmark")
                        }
                    }
                }
            }
            .padding(18)
        }
    }

    private var sourceSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("Source")
            dropZone
            PathRow(
                title: "Input",
                value: viewModel.settings.inputURL?.path ?? "No input selected",
                buttonTitle: "Choose",
                systemImage: "folder"
            ) {
                viewModel.chooseInput()
            }
            PathRow(
                title: "Output",
                value: viewModel.settings.outputURL?.path ?? "No output selected",
                buttonTitle: "Save As",
                systemImage: "square.and.arrow.down"
            ) {
                viewModel.chooseOutput()
            }
        }
    }

    private var dropZone: some View {
        RoundedRectangle(cornerRadius: 8)
            .strokeBorder(isDropTargeted ? Color.accentColor : Color.secondary.opacity(0.35), style: StrokeStyle(lineWidth: 1.4, dash: [6, 5]))
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(isDropTargeted ? Color.accentColor.opacity(0.08) : Color.secondary.opacity(0.04))
            )
            .overlay {
                VStack(spacing: 8) {
                    Image(systemName: "film.stack")
                        .font(.title2)
                        .foregroundStyle(.secondary)
                    Text("Drop a video or audio file")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(height: 112)
            .onDrop(of: [UTType.fileURL.identifier], isTargeted: $isDropTargeted) { providers in
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

    private var targetSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("Target")
            Picker("Mode", selection: $viewModel.settings.mode) {
                ForEach(EncodeSettings.Mode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)

            if viewModel.settings.mode == .autoSize {
                LabeledContent("Target MB") {
                    TextField("10", text: $viewModel.settings.targetSizeMB)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 110)
                }
            }

            Toggle("Fail if output exists", isOn: $viewModel.settings.preventOverwrite)
        }
    }

    private var videoSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("Video")
            HStack {
                NumberField("Width", text: $viewModel.settings.width)
                NumberField("Height", text: $viewModel.settings.height)
                NumberField("FPS", text: $viewModel.settings.fps)
            }
            Picker("Preset", selection: $viewModel.settings.preset) {
                ForEach(EncodeSettings.Preset.allCases) { preset in
                    Text(preset.rawValue).tag(preset)
                }
            }
            if viewModel.settings.mode == .manual {
                HStack {
                    NumberField("Video kbps", text: $viewModel.settings.videoBitrateKbps)
                    NumberField("CRF", text: $viewModel.settings.crf)
                }
            }
        }
    }

    private var audioSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("Audio")
            Toggle("Keep audio", isOn: $viewModel.settings.includeAudio)
            NumberField("Audio kbps", text: $viewModel.settings.audioBitrateKbps)
                .disabled(!viewModel.settings.includeAudio)
        }
    }

    private var detailPane: some View {
        VStack(alignment: .leading, spacing: 18) {
            MediaSummaryView(media: viewModel.media)
            EstimateView(text: viewModel.estimateText)
            CommandPreviewView(text: viewModel.commandPreviewText)
            if viewModel.isConverting || viewModel.result != nil {
                ConversionStatusView(viewModel: viewModel)
            }
            Spacer(minLength: 0)
        }
        .padding(18)
    }

    private var footer: some View {
        HStack(spacing: 12) {
            Text(viewModel.statusMessage)
                .foregroundColor(viewModel.errorMessage == nil ? .secondary : .red)
                .lineLimit(1)
            if let result = viewModel.result {
                Spacer()
                Text(ByteCountFormatter.string(fromByteCount: Int64(result.sizeBytes), countStyle: .file))
                    .foregroundStyle(.secondary)
                Button {
                    viewModel.revealOutput()
                } label: {
                    Label("Reveal Output", systemImage: "arrow.up.forward.app")
                }
            } else {
                Spacer()
            }
        }
        .font(.callout)
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
    }
}

private struct SectionTitle: View {
    let title: String

    init(_ title: String) {
        self.title = title
    }

    var body: some View {
        Text(title)
            .font(.headline)
            .foregroundStyle(.primary)
    }
}

private struct NumberField: View {
    let title: String
    @Binding var text: String

    init(_ title: String, text: Binding<String>) {
        self.title = title
        self._text = text
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField(title, text: $text)
                .textFieldStyle(.roundedBorder)
        }
    }
}

private struct PathRow: View {
    let title: String
    let value: String
    let buttonTitle: String
    let systemImage: String
    let action: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Text(value)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(.primary)
                Button(action: action) {
                    Label(buttonTitle, systemImage: systemImage)
                }
            }
        }
    }
}

private struct MediaSummaryView: View {
    let media: MediaInfo?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Input")
            if let media {
                Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 8) {
                    SummaryRow("Size", ByteCountFormatter.string(fromByteCount: Int64(media.sizeBytes), countStyle: .file))
                    SummaryRow("Duration", String(format: "%.2f s", media.duration))
                    SummaryRow("Video", media.hasVideo ? videoText(media) : "none")
                    SummaryRow("Audio", media.hasAudio ? (media.audioCodec ?? "unknown") : "none")
                }
            } else {
                Text("No input selected.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func videoText(_ media: MediaInfo) -> String {
        let size = if let width = media.width, let height = media.height {
            "\(width)x\(height)"
        } else {
            "unknown size"
        }
        let fps = media.fps.map { String(format: "%.2f fps", $0) } ?? "unknown fps"
        return "\(media.videoCodec ?? "unknown") \(size) @ \(fps)"
    }
}

private struct SummaryRow: View {
    let label: String
    let value: String

    init(_ label: String, _ value: String) {
        self.label = label
        self.value = value
    }

    var body: some View {
        GridRow {
            Text(label)
                .foregroundStyle(.secondary)
            Text(value)
                .textSelection(.enabled)
        }
    }
}

private struct EstimateView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Estimate")
            Text(text)
                .font(.system(.callout, design: .monospaced))
                .foregroundStyle(.primary)
                .textSelection(.enabled)
        }
    }
}

private struct CommandPreviewView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionTitle("Command Preview")
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .help("Copy command preview")
            }
            ScrollView {
                Text(text)
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(minHeight: 140)
        }
    }
}

private struct ConversionStatusView: View {
    @ObservedObject var viewModel: ShrinkyViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Activity")
            ProgressView(value: viewModel.progressFraction)
            HStack {
                Text(viewModel.progressPhase)
                Spacer()
                if !viewModel.progressSpeed.isEmpty {
                    Text("speed \(viewModel.progressSpeed)")
                }
                Text("\(Int(viewModel.progressFraction * 100))%")
            }
            .font(.callout)
            .foregroundStyle(.secondary)
        }
    }
}
