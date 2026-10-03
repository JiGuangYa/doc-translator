import Foundation
import Darwin

enum AppFailure: LocalizedError {
    case message(String)
    var errorDescription: String? {
        if case let .message(value) = self { return value }
        return nil
    }
}

struct APIError: LocalizedError {
    let statusCode: Int
    let detail: String
    let code: String?
    init(statusCode: Int, detail: String, code: String? = nil) {
        self.statusCode = statusCode
        self.detail = detail
        self.code = code
    }
    var errorDescription: String? { detail }
}

final class BackendServer {
    private var process: Process?
    private var logHandle: FileHandle?
    private var input: Pipe?
    private(set) var authToken = ""
    private let executableOverride: URL?
    private let arguments: [String]
    private let startupTimeout: TimeInterval
    private let shutdownTimeout: TimeInterval
    let dataDirectory: URL
    var isRunning: Bool { process?.isRunning == true }

    static func defaultDataDirectory() -> URL {
        if let override = ProcessInfo.processInfo.environment["DOC_TRANSLATOR_APP_SUPPORT"] {
            return URL(fileURLWithPath: override)
        }
        let folder = Bundle.main.bundleIdentifier?.hasSuffix(".smoke") == true
            ? "DocTranslatorUnifiedPreviewSmoke" : "DocTranslatorUnifiedPreview"
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/" + folder)
    }

    init(dataDirectory: URL? = nil, executable: URL? = nil, arguments: [String] = [],
         startupTimeout: TimeInterval = 15, shutdownTimeout: TimeInterval = 165) throws {
        self.executableOverride = executable
        self.arguments = arguments
        self.startupTimeout = startupTimeout
        self.shutdownTimeout = shutdownTimeout
        if let dataDirectory {
            self.dataDirectory = dataDirectory
        } else if let override = ProcessInfo.processInfo.environment["DOC_TRANSLATOR_APP_SUPPORT"] {
            self.dataDirectory = URL(fileURLWithPath: override)
        } else {
            let support = try FileManager.default.url(
                for: .applicationSupportDirectory, in: .userDomainMask,
                appropriateFor: nil, create: true)
            let folder = Bundle.main.bundleIdentifier?.hasSuffix(".smoke") == true
                ? "DocTranslatorUnifiedPreviewSmoke" : "DocTranslatorUnifiedPreview"
            self.dataDirectory = support.appendingPathComponent(folder, isDirectory: true)
        }
        try FileManager.default.createDirectory(at: self.dataDirectory, withIntermediateDirectories: true)
    }

    func start() async throws -> URL {
        guard !isRunning else { throw AppFailure.message("翻译引擎已在运行") }
        let port = try reservePort()
        let baseURL = URL(string: "http://127.0.0.1:\(port)")!
        guard let resourceURL = Bundle.main.resourceURL else {
            throw AppFailure.message("无法找到应用资源目录")
        }
        let executable = executableOverride ?? resourceURL
            .appendingPathComponent("DocTranslatorEngine/DocTranslatorEngine")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw AppFailure.message("翻译引擎未打包，请重新运行 macos/build_app.sh")
        }
        let logURL = dataDirectory.appendingPathComponent("backend.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: logURL)
        let logStart = try handle.seekToEnd()
        logHandle = handle
        let child = Process()
        child.executableURL = executable
        child.arguments = arguments
        child.currentDirectoryURL = resourceURL
        child.standardOutput = handle
        child.standardError = handle
        if executableOverride == nil {
            let pipe = Pipe(); child.standardInput = pipe; input = pipe
            authToken = UUID().uuidString + UUID().uuidString
        }
        var environment = ProcessInfo.processInfo.environment
        environment["DOC_TRANSLATOR_PORT"] = String(port)
        environment["DOC_TRANSLATOR_DATA_DIR"] = dataDirectory.path
        environment["DOC_TRANSLATOR_DESKTOP"] = "1"
        environment["DOC_TRANSLATOR_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        environment["DOC_TRANSLATOR_KEYRING_SERVICE"] = (Bundle.main.bundleIdentifier ?? "com.jiguang.doctranslator.preview") + ".providers"
        // A test/debug host's injected libraries must not enter the companion.
        for key in Array(environment.keys) where key.hasPrefix("DYLD_") || key.hasPrefix("XCTest") || key.hasPrefix("XCInject") {
            environment.removeValue(forKey: key)
        }
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        child.environment = environment
        do { try child.run() } catch { try? handle.close(); throw error }
        process = child
        if let input {
            var bootstrap = try JSONSerialization.data(withJSONObject: ["token": authToken, "data_dir": dataDirectory.path, "port": port])
            bootstrap.append(10)
            try input.fileHandleForWriting.write(contentsOf: bootstrap)
        }

        let health = baseURL.appendingPathComponent("healthz")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = min(5, startupTimeout)
        configuration.timeoutIntervalForResource = min(5, startupTimeout)
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let deadline = ProcessInfo.processInfo.systemUptime + startupTimeout
        var healthFailure = "尚未收到本机响应"
        do {
            while ProcessInfo.processInfo.systemUptime < deadline {
                try Task.checkCancellation()
                if !child.isRunning {
                    if let data = try? Data(contentsOf: logURL), data.count > Int(logStart),
                       let lines = String(data: data.suffix(min(65536, data.count - Int(logStart))), encoding: .utf8) {
                        for line in lines.split(separator: "\n").reversed() {
                            if let object = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                               let message = object["error"] as? String { throw AppFailure.message(message) }
                        }
                    }
                    throw AppFailure.message("翻译引擎启动失败。日志：\(logURL.path)")
                }
                var request = URLRequest(url: health)
                request.timeoutInterval = min(5, startupTimeout)
                if !authToken.isEmpty { request.setValue(authToken, forHTTPHeaderField: "X-DocTranslator-Token") }
                do {
                    let (_, response) = try await session.data(for: request)
                    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                    if status == 200 { return baseURL }
                    healthFailure = "HTTP \(status)"
                } catch { healthFailure = error.localizedDescription }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            throw AppFailure.message("翻译引擎启动超时，可重试。\(healthFailure) 日志：\(logURL.path)")
        } catch {
            if child.isRunning { child.terminate() }
            await waitForExit(child, timeout: min(1, shutdownTimeout))
            try? logHandle?.close()
            process = nil
            throw error
        }
    }

    func stop() {
        try? input?.fileHandleForWriting.close(); input = nil
        if let process, process.isRunning { process.terminate() }
        try? logHandle?.close()
    }

    func stopAndWait() async {
        try? input?.fileHandleForWriting.close(); input = nil
        guard let process else { return }
        if process.isRunning { process.terminate() }
        await waitForExit(process, timeout: shutdownTimeout)
        try? logHandle?.close()
        self.process = nil
    }

    private func waitForExit(_ process: Process, timeout: TimeInterval) async {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                let deadline = ProcessInfo.processInfo.systemUptime + timeout
                let pid = process.processIdentifier
                var status: Int32 = 0
                func exited() -> Bool {
                    let result = waitpid(pid, &status, WNOHANG)
                    return result == pid || (result < 0 && errno == ECHILD)
                }
                while !exited() && ProcessInfo.processInfo.systemUptime < deadline {
                    Thread.sleep(forTimeInterval: 0.05)
                }
                if !exited() {
                    kill(pid, SIGKILL)
                    // Do not depend on Foundation's termination callback run loop.
                    // Headless macOS test hosts may not pump that run loop.
                    let reapDeadline = ProcessInfo.processInfo.systemUptime + 1
                    while !exited() && ProcessInfo.processInfo.systemUptime < reapDeadline {
                        Thread.sleep(forTimeInterval: 0.01)
                    }
                }
                continuation.resume()
            }
        }
    }

    private func reservePort() throws -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw AppFailure.message("无法创建本地连接") }
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bindResult = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else { throw AppFailure.message("无法分配本地端口") }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let nameResult = withUnsafeMutablePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard nameResult == 0 else { throw AppFailure.message("无法读取本地端口") }
        return Int(UInt16(bigEndian: address.sin_port))
    }
}

final class APIClient {
    let baseURL: URL
    private let session: URLSession
    private let decoder = JSONDecoder()

    init(baseURL: URL, configuration: URLSessionConfiguration? = nil, token: String = "") {
        self.baseURL = baseURL
        let config = configuration ?? URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.connectionProxyDictionary = [:] // Companion traffic must stay on loopback.
        if !token.isEmpty { config.httpAdditionalHeaders = ["X-DocTranslator-Token": token] }
        config.httpCookieAcceptPolicy = .always
        session = URLSession(configuration: config)
        decoder.keyDecodingStrategy = .convertFromSnakeCase
    }

    func json<T: Decodable>(_ path: String, method: String = "GET",
                            body: [String: Any]? = nil) async throws -> T {
        var request = URLRequest(url: url(path))
        request.httpMethod = method
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let data = try await perform(request)
        return try decoder.decode(T.self, from: data)
    }

    func upload(_ fileURL: URL) async throws -> TranslationTask {
        let accessed = fileURL.startAccessingSecurityScopedResource()
        defer { if accessed { fileURL.stopAccessingSecurityScopedResource() } }
        let attributes = try FileManager.default.attributesOfItem(atPath: fileURL.path)
        if let size = attributes[.size] as? NSNumber, size.int64Value > 100 * 1024 * 1024 {
            throw AppFailure.message("文件超过 100 MB 上限")
        }
        let boundary = UUID().uuidString
        let safeName = fileURL.lastPathComponent
            .replacingOccurrences(of: "\"", with: "_")
            .replacingOccurrences(of: "\r", with: "_")
            .replacingOccurrences(of: "\n", with: "_")
        let staging = FileManager.default.temporaryDirectory
            .appendingPathComponent("doc-translator-upload-\(UUID().uuidString)")
        FileManager.default.createFile(atPath: staging.path, contents: nil)
        defer { try? FileManager.default.removeItem(at: staging) }
        let input = try FileHandle(forReadingFrom: fileURL)
        let output = try FileHandle(forWritingTo: staging)
        defer { try? input.close(); try? output.close() }
        let header = "--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        try output.write(contentsOf: Data(header.utf8))
        while let chunk = try input.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            try output.write(contentsOf: chunk)
        }
        try output.write(contentsOf: Data("\r\n--\(boundary)--\r\n".utf8))
        try output.synchronize()
        try output.close()
        try input.close()
        var request = URLRequest(url: url("/api/upload"))
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 120
        let (data, response) = try await session.upload(for: request, fromFile: staging)
        let checked = try validate(data: data, response: response)
        return try decoder.decode(TranslationTask.self, from: checked)
    }

    func bytes(_ path: String) async throws -> Data {
        try await perform(URLRequest(url: url(path)))
    }

    func download(_ path: String, to destination: URL) async throws {
        let (temporary, response) = try await session.download(for: URLRequest(url: url(path)))
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let handle = try FileHandle(forReadingFrom: temporary)
            defer { try? handle.close() }
            _ = try validate(data: try handle.read(upToCount: 64 * 1024) ?? Data(), response: response)
            return
        }
        let staging = destination.deletingLastPathComponent()
            .appendingPathComponent(".doc-translator-\(UUID().uuidString).pending")
        defer { try? FileManager.default.removeItem(at: staging) }
        try FileManager.default.copyItem(at: temporary, to: staging)
        if FileManager.default.fileExists(atPath: destination.path) {
            _ = try FileManager.default.replaceItemAt(destination, withItemAt: staging)
        } else {
            try FileManager.default.moveItem(at: staging, to: destination)
        }
    }

    private func url(_ path: String) -> URL {
        URL(string: baseURL.absoluteString + path)!
    }

    private func perform(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: request)
        return try validate(data: data, response: response)
    }

    private func validate(data: Data, response: URLResponse) throws -> Data {
        guard let response = response as? HTTPURLResponse else {
            throw AppFailure.message("服务没有返回 HTTP 响应")
        }
        guard (200..<300).contains(response.statusCode) else {
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let detail = object?["detail"] as? String ??
                HTTPURLResponse.localizedString(forStatusCode: response.statusCode)
            throw APIError(statusCode: response.statusCode, detail: detail,
                           code: object?["code"] as? String)
        }
        return data
    }
}
