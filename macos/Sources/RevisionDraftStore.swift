import Foundation

/// Drafts are independent of the reader view, so navigation and restarts keep them.
@MainActor
final class RevisionDraftStore: ObservableObject {
    @Published private(set) var drafts: [String: [String: String]] = [:]
    @Published private(set) var saveError: String?
    let url: URL
    private var needsRecoveryCopy = false

    init(url: URL) {
        self.url = url
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        do {
            drafts = try JSONDecoder().decode([String: [String: String]].self, from: Data(contentsOf: url))
        } catch {
            needsRecoveryCopy = true
            saveError = "无法读取本地草稿：\(error.localizedDescription)"
        }
    }

    func text(taskID: String, segmentID: String) -> String? { drafts[taskID]?[segmentID] }
    func count(taskID: String) -> Int { drafts[taskID]?.count ?? 0 }

    func set(_ text: String, taskID: String, segmentID: String) {
        drafts[taskID, default: [:]][segmentID] = text
        persist()
    }

    func remove(taskID: String, segmentID: String, matching text: String? = nil) {
        if let text, drafts[taskID]?[segmentID] != text { return }
        drafts[taskID]?[segmentID] = nil
        if drafts[taskID]?.isEmpty == true { drafts[taskID] = nil }
        persist()
    }

    func remove(taskID: String, matching snapshot: [String: String]) {
        for (id, text) in snapshot where drafts[taskID]?[id] == text { drafts[taskID]?[id] = nil }
        if drafts[taskID]?.isEmpty == true { drafts[taskID] = nil }
        persist()
    }

    private func persist() {
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            // Preserve an unreadable previous file before replacing it.
            if needsRecoveryCopy, FileManager.default.fileExists(atPath: url.path) {
                let backup = url.deletingLastPathComponent().appendingPathComponent("drafts-recovery-\(UUID().uuidString).json")
                try FileManager.default.copyItem(at: url, to: backup)
                needsRecoveryCopy = false
            }
            try JSONEncoder().encode(drafts).write(to: url, options: .atomic)
            saveError = nil
        } catch {
            saveError = "草稿暂存在当前窗口，尚未写入磁盘：\(error.localizedDescription)"
        }
    }
}
