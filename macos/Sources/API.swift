import AppKit
import Foundation

struct AuthStatus: Decodable {
    let configured: Bool
    let authenticated: Bool
}

struct Provider: Identifiable, Decodable, Hashable {
    let id: String
    let name: String
    let baseUrl: String
    let model: String
    let hasApiKey: Bool
}

struct ProviderList: Decodable {
    let providers: [Provider]
    let settings: TranslationSettings
}

struct TranslationSettings: Decodable {
    let translationProviderId: String?
    let assistantProviderId: String?
    let sourceLang: String?
    let targetLang: String?
}

struct ProviderResponse: Decodable {
    let provider: Provider
}

struct TranslationTask: Identifiable, Decodable, Hashable {
    let taskId: String
    let filename: String
    let ext: String
    let status: String
    let segmentCount: Int?
    let doneSegments: Int?
    let totalSegments: Int?
    let targetLang: String?
    let sourceLang: String?
    let providerId: String?
    let providerSnapshot: ProviderSnapshot?
    let untranslatedCount: Int
    let contentVersion: Int
    let error: String?
    let warnings: [String]
    let hasTranslated: Bool
    var archived: Bool? = nil
    var overflowCount: Int? = nil

    var id: String { taskId }
    var isArchived: Bool { archived ?? false }
    var needsLayoutReview: Bool { (overflowCount ?? 0) > 0 }
    var progress: Double {
        let total = totalSegments ?? segmentCount ?? 0
        guard total > 0 else { return 0 }
        return min(1, max(0, Double(doneSegments ?? 0) / Double(total)))
    }
}

struct ProviderSnapshot: Decodable, Hashable {
    let name: String
    let baseUrl: String
    let model: String
}

struct Segment: Identifiable, Decodable, Hashable {
    let segId: String
    let text: String
    let translation: String?
    let context: String
    let translatable: Bool
    var id: String { segId }
}

struct SegmentList: Decodable {
    let segments: [Segment]
    let warnings: [String]
    var overflow: [String]? = nil
}

struct RenderStatus: Decodable {
    let status: String
    let pages: Int
    let error: String?
    var originalPages: Int? = nil
    var translatedPages: Int? = nil
    var contentVersion: Int? = nil
}

struct FileInfo: Decodable {
    let pages: Int?
    let renderAvailable: Bool
    let originalPages: Int?
    let translatedPages: Int?
    let contentVersion: Int
}

struct APIError: Error, LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

final class APIClient {
    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder
    private let token: String

    init(port: Int, token: String = "", configuration injected: URLSessionConfiguration? = nil) {
        self.token = token
        baseURL = URL(string: "http://127.0.0.1:\(port)")!
        let configuration = injected ?? URLSessionConfiguration.ephemeral
        configuration.httpCookieAcceptPolicy = .always
        configuration.httpShouldSetCookies = true
        session = URLSession(configuration: configuration)
        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
    }

    private func url(_ path: String, query: [URLQueryItem] = []) -> URL {
        var parts = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        parts.queryItems = query.isEmpty ? nil : query
        return parts.url!
    }

    func request<T: Decodable>(_ path: String, method: String = "GET", body: [String: Any]? = nil, query: [URLQueryItem] = [], timeout: TimeInterval = 180) async throws -> T {
        var req = URLRequest(url: url(path, query: query))
        req.httpMethod = method
        req.timeoutInterval = timeout
        req.setValue(token, forHTTPHeaderField: "X-DocTranslator-Token")
        if let body {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: req)
        try validate(data, response)
        return try decoder.decode(T.self, from: data)
    }

    func data(_ path: String, query: [URLQueryItem] = []) async throws -> Data {
        var req = URLRequest(url: url(path, query: query))
        req.timeoutInterval = 180
        req.setValue(token, forHTTPHeaderField: "X-DocTranslator-Token")
        let (data, response) = try await session.data(for: req)
        try validate(data, response)
        return data
    }

    func upload(_ file: URL) async throws -> TranslationTask {
        let access = file.startAccessingSecurityScopedResource()
        defer { if access { file.stopAccessingSecurityScopedResource() } }
        let size = try file.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard size <= 100 * 1024 * 1024 else { throw APIError(message: "文件超过 100 MB 上限") }
        let fileData = try await Task.detached { try Data(contentsOf: file) }.value
        let boundary = "DocTranslator-\(UUID().uuidString)"
        var body = Data()
        body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(file.lastPathComponent.replacingOccurrences(of: "\"", with: "_").replacingOccurrences(of: "\r", with: "_").replacingOccurrences(of: "\n", with: "_"))\"\r\nContent-Type: application/octet-stream\r\n\r\n".data(using: .utf8)!)
        body.append(fileData)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        var req = URLRequest(url: url("api/upload"))
        req.httpMethod = "POST"
        req.timeoutInterval = 180
        req.setValue(token, forHTTPHeaderField: "X-DocTranslator-Token")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.upload(for: req, from: body)
        try validate(data, response)
        return try decoder.decode(TranslationTask.self, from: data)
    }

    private func validate(_ data: Data, _ response: URLResponse) throws {
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let detail = object?["detail"] as? String ?? HTTPURLResponse.localizedString(forStatusCode: status)
            throw APIError(message: "\(status): \(detail)")
        }
    }
}
