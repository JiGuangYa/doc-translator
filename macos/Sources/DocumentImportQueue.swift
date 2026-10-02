import Foundation

struct ImportFailure: Identifiable {
    let id = UUID()
    let filename: String
    let message: String
}

/// Only one document is parsed at a time; URLs keep their access until consumed.
@MainActor
final class DocumentImportQueue: ObservableObject {
    static let supportedExtensions: Set<String> = ["pdf", "docx", "pptx", "xlsx"]
    @Published private(set) var currentFilename: String?
    @Published private(set) var pendingCount = 0
    @Published private(set) var completedCount = 0
    @Published private(set) var totalCount = 0
    @Published private(set) var successfulCount = 0
    @Published private(set) var failures: [ImportFailure] = []
    private var pending: [(url: URL, access: Bool)] = []
    private var currentURL: URL?
    private var enabled = false
    private var runner: Task<Void, Never>?
    private let upload: (URL) async throws -> TranslationTask
    var onImported: ((TranslationTask) async -> Void)?
    var isImporting: Bool { currentFilename != nil || pendingCount > 0 }

    init(upload: @escaping (URL) async throws -> TranslationTask) { self.upload = upload }

    @discardableResult
    func enqueue(_ urls: [URL]) -> Bool {
        if !isImporting { completedCount = 0; totalCount = 0; successfulCount = 0 }
        var accepted = false
        for url in urls {
            guard url.isFileURL, Self.supportedExtensions.contains(url.pathExtension.lowercased()) else {
                failures.append(ImportFailure(filename: url.lastPathComponent, message: "仅支持 PDF、DOCX、PPTX 和 XLSX 本地文件"))
                continue
            }
            let canonical = url.standardizedFileURL
            guard currentURL?.standardizedFileURL != canonical,
                  !pending.contains(where: { $0.url.standardizedFileURL == canonical }) else { continue }
            pending.append((url, url.startAccessingSecurityScopedResource()))
            totalCount += 1
            accepted = true
        }
        pendingCount = pending.count
        startIfNeeded()
        return accepted
    }

    func setEnabled(_ value: Bool) { enabled = value; startIfNeeded() }

    func cancelPending() {
        for item in pending where item.access { item.url.stopAccessingSecurityScopedResource() }
        totalCount -= pending.count
        pending.removeAll()
        pendingCount = 0
    }

    func clearFailures() { failures.removeAll() }

    func shutdown() async {
        enabled = false
        cancelPending()
        runner?.cancel()
        await runner?.value
    }

    func waitUntilIdle() async { await runner?.value }

    private func startIfNeeded() {
        guard enabled, runner == nil, !pending.isEmpty else { return }
        runner = Task { [weak self] in
            guard let self else { return }
            defer { self.runner = nil; self.currentURL = nil; self.currentFilename = nil }
            while self.enabled, !Task.isCancelled, !self.pending.isEmpty {
                let item = self.pending.removeFirst()
                self.pendingCount = self.pending.count
                self.currentURL = item.url
                self.currentFilename = item.url.lastPathComponent
                do {
                    let document = try await self.upload(item.url)
                    if !Task.isCancelled {
                        self.successfulCount += 1
                        await self.onImported?(document)
                    }
                } catch {
                    if !Task.isCancelled {
                        self.failures.append(ImportFailure(filename: item.url.lastPathComponent, message: error.localizedDescription))
                    }
                }
                if item.access { item.url.stopAccessingSecurityScopedResource() }
                self.completedCount += 1
                self.currentURL = nil
                self.currentFilename = nil
            }
        }
    }
}
