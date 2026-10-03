import Foundation

struct ReadingBookmark: Codable, Equatable {
    var mode = 0
    var page = 0
    var zoom = 1.0
    var paragraphID: String?
    var continuousPage: Int?

    var normalized: ReadingBookmark {
        ReadingBookmark(mode: (0...2).contains(mode) ? mode : 0, page: max(0, page),
                        zoom: zoom.isFinite ? min(2.5, max(0.4, zoom)) : 1,
                        paragraphID: paragraphID, continuousPage: continuousPage.map { max(1, $0) })
    }
}

/// Reading preferences never modify document content or revision drafts.
@MainActor
final class ReadingStateStore: ObservableObject {
    private struct Library: Codable {
        var lastTaskID: String?
        var documents: [String: ReadingBookmark] = [:]
    }
    @Published private(set) var saveError: String?
    let url: URL
    private var library = Library()
    private var pending: Task<Void, Never>?
    private var dirty = false
    private var needsRecoveryCopy = false
    var lastTaskID: String? { library.lastTaskID }

    init(url: URL) {
        self.url = url
        reload()
    }

    func reload() {
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        do {
            library = try JSONDecoder().decode(Library.self, from: Data(contentsOf: url))
            library.documents = library.documents.mapValues(\.normalized)
        } catch {
            needsRecoveryCopy = true
            saveError = "无法读取上次的阅读位置：\(error.localizedDescription)"
        }
    }

    func bookmark(for taskID: String) -> ReadingBookmark { library.documents[taskID] ?? ReadingBookmark() }

    func select(_ taskID: String?) {
        guard library.lastTaskID != taskID else { return }
        library.lastTaskID = taskID
        scheduleSave()
    }

    func update(_ bookmark: ReadingBookmark, for taskID: String) {
        let value = bookmark.normalized
        guard library.documents[taskID] != value else { return }
        library.documents[taskID] = value
        scheduleSave()
    }

    private func scheduleSave() {
        dirty = true
        pending?.cancel()
        pending = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            self?.flush()
        }
    }

    func flush() {
        pending?.cancel()
        pending = nil
        guard dirty else { return }
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if needsRecoveryCopy, FileManager.default.fileExists(atPath: url.path) {
                let copy = url.deletingLastPathComponent().appendingPathComponent("reading-recovery-\(UUID().uuidString).json")
                try FileManager.default.copyItem(at: url, to: copy)
                needsRecoveryCopy = false
            }
            try JSONEncoder().encode(library).write(to: url, options: .atomic)
            dirty = false
            saveError = nil
        } catch {
            saveError = "阅读位置尚未保存，重启后可能需要重新定位：\(error.localizedDescription)"
        }
    }
}
