import Foundation

struct MediaInfo: Decodable, Equatable {
    let path: String
    let duration: Double
    let sizeBytes: Int
    let hasVideo: Bool
    let hasAudio: Bool
    let width: Int?
    let height: Int?
    let fps: Double?
    let videoCodec: String?
    let audioCodec: String?
}

struct ProbeResponse: Decodable {
    let ok: Bool
    let media: MediaInfo
    let suggestedOutput: String
}

struct PreviewResponse: Decodable {
    let ok: Bool
    let media: MediaInfo
    let outputKind: String
    let estimate: EstimateInfo
    let commands: [String]
}

struct EstimateInfo: Decodable {
    let mode: String
    let message: String?
    let targetSizeMb: Double?
    let videoKbps: Int?
    let audioKbps: Int?
}

struct ConversionResult: Decodable {
    let outputPath: String
    let sizeBytes: Int
    let duration: Double
    let attempts: Int
    let mode: String
    let commandLines: [String]
}

struct ProgressEvent: Decodable {
    let event: String?
    let ok: Bool
    let phase: String?
    let fraction: Double?
    let speed: String?
    let outTimeSeconds: Double?
    let logPath: String?
    let result: ConversionResult?
    let error: String?
}

struct EncodeSettings: Equatable {
    enum Mode: String, CaseIterable, Identifiable {
        case autoSize = "auto_size"
        case manual = "manual"

        var id: String { rawValue }

        var title: String {
            switch self {
            case .autoSize:
                "Auto Size"
            case .manual:
                "Manual"
            }
        }
    }

    enum Preset: String, CaseIterable, Identifiable {
        case medium
        case slow
        case veryslow

        var id: String { rawValue }
    }

    var inputURL: URL?
    var outputURL: URL?
    var mode: Mode = .autoSize
    var targetSizeMB = "10"
    var includeAudio = true
    var audioBitrateKbps = "96"
    var width = ""
    var height = ""
    var fps = ""
    var videoBitrateKbps = ""
    var crf = "23"
    var preset: Preset = .slow
    var preventOverwrite = false

    var canRequestPreview: Bool {
        inputURL != nil && outputURL != nil
    }

    func arguments() -> [String] {
        var args: [String] = []
        if let inputURL {
            args.append(contentsOf: ["--input", inputURL.path])
        }
        if let outputURL {
            args.append(contentsOf: ["--output", outputURL.path])
        }
        args.append(contentsOf: ["--mode", mode.rawValue])
        if mode == .autoSize {
            args.append(contentsOf: ["--target-size-mb", targetSizeMB])
        }
        appendOptional("--width", width, to: &args)
        appendOptional("--height", height, to: &args)
        appendOptional("--fps", fps, to: &args)
        appendOptional("--audio-bitrate-kbps", audioBitrateKbps, to: &args)
        if mode == .manual {
            appendOptional("--video-bitrate-kbps", videoBitrateKbps, to: &args)
            if videoBitrateKbps.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                appendOptional("--crf", crf, to: &args)
            }
        }
        args.append(contentsOf: ["--preset", preset.rawValue])
        if !includeAudio {
            args.append("--no-audio")
        }
        if preventOverwrite {
            args.append("--no-overwrite")
        }
        return args
    }

    private func appendOptional(_ flag: String, _ value: String, to args: inout [String]) {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            args.append(contentsOf: [flag, trimmed])
        }
    }
}
