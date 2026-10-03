import Foundation

/// Drafts are independent of the reader view, so navigation and restarts keep them.
@MainActor
final class RevisionDraftStore: ObservableObject {
    @Published private(set) var drafts: [String: [String: String]] = [:]
    @Published private(set) var saveError: String?
    @Published private(set) var revisions: [String: Int] = [:]
    let url: URL
    private var revisionsURL: URL { url.deletingLastPathComponent().appendingPathComponent("draft-revisions.json") }
    private struct Entry: Codable { var segments: [String: String]; var revision: Int? }
    private struct Snapshot: Codable { var schema_version = 2; var tasks: [String: Entry] }
    private var needsRecoveryCopy = false

    init(url: URL) {
        self.url = url
        reload()
    }

    func reload() {
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        do {
            let data = try Data(contentsOf: url)
            if let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) {
                drafts = snapshot.tasks.mapValues(\.segments)
                revisions = snapshot.tasks.compactMapValues(\.revision)
            } else {
                drafts = try JSONDecoder().decode([String: [String: String]].self, from: data)
                revisions = (try? JSONDecoder().decode([String: Int].self, from: Data(contentsOf: revisionsURL))) ?? [:]
            }
            saveError = nil
        } catch {
            needsRecoveryCopy = true
            saveError = "无法读取本地草稿：\(error.localizedDescription)"
        }
    }

    func text(taskID: String, segmentID: String) -> String? { drafts[taskID]?[segmentID] }
    func count(taskID: String) -> Int { drafts[taskID]?.count ?? 0 }

    func set(_ text: String, taskID: String, segmentID: String, revision: Int? = nil) {
        if revisions[taskID] == nil, let revision { revisions[taskID] = revision }
        drafts[taskID, default: [:]][segmentID] = text
        persist()
    }

    func remove(taskID: String, segmentID: String, matching text: String? = nil) {
        if let text, drafts[taskID]?[segmentID] != text { return }
        drafts[taskID]?[segmentID] = nil
        if drafts[taskID]?.isEmpty == true { drafts[taskID] = nil; revisions[taskID] = nil }
        persist()
    }

    func remove(taskID: String, matching snapshot: [String: String]) {
        for (id, text) in snapshot where drafts[taskID]?[id] == text { drafts[taskID]?[id] = nil }
        if drafts[taskID]?.isEmpty == true { drafts[taskID] = nil; revisions[taskID] = nil }
        persist()
    }

    func advance(taskID: String, from revision: Int?, to newRevision: Int?) {
        if drafts[taskID] != nil && revisions[taskID] == revision { revisions[taskID] = newRevision; persist() }
    }

    func persist() {
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            // Preserve an unreadable previous file before replacing it.
            if needsRecoveryCopy, FileManager.default.fileExists(atPath: url.path) {
                let backup = url.deletingLastPathComponent().appendingPathComponent("drafts-recovery-\(UUID().uuidString).json")
                try FileManager.default.copyItem(at: url, to: backup)
                needsRecoveryCopy = false
            }
            let snapshot = Snapshot(tasks: drafts.mapValues { Entry(segments: $0, revision: nil) })
            var entries = snapshot.tasks
            for id in entries.keys { entries[id]?.revision = revisions[id] }
            try JSONEncoder().encode(Snapshot(tasks: entries)).write(to: url, options: .atomic)
            saveError = nil
        } catch {
            saveError = "草稿暂存在当前窗口，尚未写入磁盘：\(error.localizedDescription)"
        }
    }
}
