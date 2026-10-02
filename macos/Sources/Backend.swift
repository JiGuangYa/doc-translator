import AppKit
import Foundation

@MainActor
final class Backend {
    private var process: Process?
    private var input: Pipe?
    private var output: Pipe?
    private var outputBuffer = Data()
    private(set) var api = APIClient(port: 0)
    init(api: APIClient? = nil) { if let api { self.api = api } }
    private(set) var port = 0
    var isRunning: Bool { process?.isRunning == true }
    var supportURL: URL {
        if let override = ProcessInfo.processInfo.environment["DOC_TRANSLATOR_APP_SUPPORT"] {
            return URL(fileURLWithPath: override)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("DocTranslatorMac", isDirectory: true)
    }
    var logURL: URL { supportURL.appendingPathComponent("backend.log") }

    func start() async throws {
        guard !isRunning else { return }
        outputBuffer = Data()
        let resources = Bundle.main.resourceURL!
        let engine = resources.appendingPathComponent("DocTranslatorEngine/DocTranslatorEngine")
        let executable: URL
        let arguments: [String]
        var env = ProcessInfo.processInfo.environment
        env.removeValue(forKey: "PYTHONHOME")
        env.removeValue(forKey: "PYTHONPATH")
        env.removeValue(forKey: "DOC_TRANSLATOR_RESOURCE_DIR")
        if FileManager.default.isExecutableFile(atPath: engine.path) {
            executable = engine
            arguments = []
        } else if let devRoot = env["DOC_TRANSLATOR_DEV_ROOT"] {
            let root = URL(fileURLWithPath: devRoot)
            executable = root.appendingPathComponent(".venv/bin/python")
            arguments = [root.appendingPathComponent("macos/engine_launcher.py").path]
            env["PYTHONPATH"] = root.path
        } else {
            throw APIError(message: "应用缺少内置翻译引擎，请重新构建完整应用。")
        }
        try FileManager.default.createDirectory(at: supportURL, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            _ = FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        // Keep launch logs bounded while preserving the previous run.
        if let size = try? logURL.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 2_000_000 {
            let previous = supportURL.appendingPathComponent("backend.previous.log")
            try? FileManager.default.removeItem(at: previous)
            try FileManager.default.moveItem(at: logURL, to: previous)
            _ = FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try FileHandle(forWritingTo: logURL)
        try log.seekToEnd()
        let child = Process()
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        child.executableURL = executable
        child.arguments = arguments
        child.currentDirectoryURL = URL(fileURLWithPath: "/")
        env["PYTHONUNBUFFERED"] = "1"
        child.environment = env
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = log
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in
                guard let self, self.process === child else { return }
                self.outputBuffer.append(data)
            }
        }
        try child.run()
        try? log.close()
        process = child
        input = stdinPipe
        output = stdoutPipe
        let token = UUID().uuidString + UUID().uuidString
        let bootstrap: [String: Any] = ["token": token, "data_dir": supportURL.appendingPathComponent("data").path]
        var bytes = try JSONSerialization.data(withJSONObject: bootstrap)
        bytes.append(10)
        try stdinPipe.fileHandleForWriting.write(contentsOf: bytes)
        do {
            for _ in 0..<150 {
                try Task.checkCancellation()
                if let newline = outputBuffer.firstIndex(of: 10) {
                    port = try Self.readyPort(from: outputBuffer.prefix(upTo: newline))
                    api = APIClient(port: port, token: token)
                    let _: OKResponse = try await api.request("healthz")
                    return
                }
                if !child.isRunning {
                    // Allow the pipe callback's MainActor delivery to catch up
                    // with a child that reported a startup error and exited.
                    try await Task.sleep(nanoseconds: 100_000_000)
                    if outputBuffer.contains(10) { continue }
                    break
                }
                try await Task.sleep(nanoseconds: 200_000_000)
            }
            throw APIError(message: "翻译引擎启动失败，请查看日志后重试。")
        } catch {
            await stop()
            throw error
        }
    }

    func stop() async {
        let child = process
        try? input?.fileHandleForWriting.close()
        input = nil
        for _ in 0..<140 {
            if child?.isRunning != true { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if let child, child.isRunning { kill(child.processIdentifier, SIGKILL) }
        output?.fileHandleForReading.readabilityHandler = nil
        try? output?.fileHandleForReading.close()
        output = nil
        process = nil
    }

    func showLogs() { NSWorkspace.shared.open(logURL) }
    static func readyPort(from data: Data) throws -> Int {
        let ready = try JSONDecoder().decode(Ready.self, from: data)
        if let error = ready.error { throw APIError(message: error) }
        guard ready.protocol_version == 1, let port = ready.port, (1...65535).contains(port) else {
            throw APIError(message: "内置引擎版本不匹配。")
        }
        return port
    }
    private struct Ready: Decodable { let port: Int?; let protocol_version: Int; let error: String? }
}
