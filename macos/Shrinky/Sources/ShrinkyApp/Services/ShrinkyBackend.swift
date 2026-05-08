import Foundation

enum BackendError: LocalizedError {
    case scriptNotFound
    case processFailed(String)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .scriptNotFound:
            "Could not find app.py. Set SHRINKY_BACKEND or run from the repository root."
        case .processFailed(let message):
            message
        case .invalidResponse:
            "Shrinky returned an invalid response."
        }
    }
}

actor ShrinkyBackend {
    private let decoder: JSONDecoder
    private let scriptURL: URL

    init(scriptURL: URL? = nil) throws {
        self.decoder = JSONDecoder()
        self.decoder.keyDecodingStrategy = .convertFromSnakeCase
        if let scriptURL {
            self.scriptURL = scriptURL
        } else if let located = Self.locateBackendScript() {
            self.scriptURL = located
        } else {
            throw BackendError.scriptNotFound
        }
    }

    func probe(inputURL: URL) async throws -> ProbeResponse {
        try await runSingleJSON(arguments: ["--probe-json", "--input", inputURL.path])
    }

    func preview(settings: EncodeSettings) async throws -> PreviewResponse {
        try await runSingleJSON(arguments: ["--preview-json"] + settings.arguments())
    }

    func convert(
        settings: EncodeSettings,
        onEvent: @escaping @Sendable (ProgressEvent) -> Void
    ) async throws -> ConversionResult {
        let events = try await runJSONLines(arguments: ["--progress-json"] + settings.arguments(), onEvent: onEvent)
        if let complete = events.last(where: { $0.event == "complete" }), let result = complete.result {
            return result
        }
        if let failed = events.last(where: { $0.ok == false }), let error = failed.error {
            throw BackendError.processFailed(error)
        }
        throw BackendError.invalidResponse
    }

    private func runSingleJSON<T: Decodable>(arguments: [String]) async throws -> T {
        let result = try await runProcess(arguments: arguments)
        if result.status != 0 {
            if let error = try? decodeError(from: result.stdout), !error.isEmpty {
                throw BackendError.processFailed(error)
            }
            throw BackendError.processFailed(result.stderr.isEmpty ? result.stdout : result.stderr)
        }
        guard let data = result.stdout.data(using: .utf8) else {
            throw BackendError.invalidResponse
        }
        return try decoder.decode(T.self, from: data)
    }

    private func runJSONLines(
        arguments: [String],
        onEvent: @escaping @Sendable (ProgressEvent) -> Void
    ) async throws -> [ProgressEvent] {
        let processArguments = ["python3", scriptURL.path] + arguments
        let processBox = ProcessBox()
        return try await withTaskCancellationHandler {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = processArguments
            process.environment = Self.processEnvironment()

            let stdout = Pipe()
            let stderr = Pipe()
            process.standardOutput = stdout
            process.standardError = stderr
            processBox.set(process)

            try process.run()

            var events: [ProgressEvent] = []
            for try await line in stdout.fileHandleForReading.bytes.lines {
                if Task.isCancelled {
                    processBox.terminate()
                    throw CancellationError()
                }
                guard let data = line.data(using: .utf8) else {
                    continue
                }
                let event = try decoder.decode(ProgressEvent.self, from: data)
                events.append(event)
                onEvent(event)
            }

            process.waitUntilExit()
            let stderrData = stderr.fileHandleForReading.readDataToEndOfFile()
            let stderrText = String(data: stderrData, encoding: .utf8) ?? ""

            if process.terminationStatus != 0 {
                if let error = events.last(where: { $0.ok == false })?.error {
                    throw BackendError.processFailed(error)
                }
                throw BackendError.processFailed(stderrText)
            }
            return events
        } onCancel: {
            processBox.terminate()
        }
    }

    private func runProcess(arguments: [String]) async throws -> ProcessResult {
        let processArguments = ["python3", scriptURL.path] + arguments
        let processBox = ProcessBox()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    let process = Process()
                    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                    process.arguments = processArguments
                    process.environment = Self.processEnvironment()

                    let stdout = Pipe()
                    let stderr = Pipe()
                    process.standardOutput = stdout
                    process.standardError = stderr
                    processBox.set(process)

                    do {
                        try process.run()
                        process.waitUntilExit()

                        let stdoutData = stdout.fileHandleForReading.readDataToEndOfFile()
                        let stderrData = stderr.fileHandleForReading.readDataToEndOfFile()

                        if Task.isCancelled {
                            continuation.resume(throwing: CancellationError())
                            return
                        }

                        continuation.resume(
                            returning: ProcessResult(
                                status: process.terminationStatus,
                                stdout: String(data: stdoutData, encoding: .utf8) ?? "",
                                stderr: String(data: stderrData, encoding: .utf8) ?? ""
                            )
                        )
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
        } onCancel: {
            processBox.terminate()
        }
    }

    private func decodeError(from output: String) throws -> String {
        guard let data = output.data(using: .utf8) else {
            return ""
        }
        let event = try decoder.decode(ProgressEvent.self, from: data)
        return event.error ?? ""
    }

    private static func locateBackendScript() -> URL? {
        let fileManager = FileManager.default
        if let explicit = ProcessInfo.processInfo.environment["SHRINKY_BACKEND"] {
            let url = URL(fileURLWithPath: explicit)
            if fileManager.fileExists(atPath: url.path) {
                return url
            }
        }

        let cwd = URL(fileURLWithPath: fileManager.currentDirectoryPath)
        let candidates = [
            cwd.appendingPathComponent("app.py"),
            cwd.appendingPathComponent("../../app.py").standardizedFileURL,
            Bundle.main.resourceURL?.appendingPathComponent("app.py"),
        ].compactMap { $0 }

        return candidates.first { fileManager.fileExists(atPath: $0.path) }
    }

    private static func processEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let currentPath = environment["PATH"] ?? ""
        let existingPaths = currentPath.split(separator: ":").map(String.init)
        let commonToolPaths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        let mergedPaths = existingPaths + commonToolPaths.filter { !existingPaths.contains($0) }
        environment["PATH"] = mergedPaths.joined(separator: ":")
        return environment
    }
}

private struct ProcessResult {
    let status: Int32
    let stdout: String
    let stderr: String
}

private final class ProcessBox: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?

    func set(_ process: Process) {
        lock.lock()
        self.process = process
        lock.unlock()
    }

    func terminate() {
        lock.lock()
        let process = self.process
        lock.unlock()

        guard let process, process.isRunning else {
            return
        }
        process.terminate()
    }
}
