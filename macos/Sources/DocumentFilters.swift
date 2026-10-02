import Foundation

enum LibraryFilter: String, CaseIterable, Identifiable {
    case all = "全部", waiting = "待翻译", active = "翻译中", needsAttention = "待处理", completed = "已完成", archived = "已归档"
    var id: Self { self }

    func includes(_ task: TranslationTask) -> Bool {
        if self == .archived { return task.isArchived }
        guard !task.isArchived else { return false }
        switch self {
        case .all: return true
        case .waiting: return ["uploaded", "parsing", "pending_confirm"].contains(task.status)
        case .active: return task.status == "translating"
        case .needsAttention: return ["failed", "cancelled"].contains(task.status) || (task.status == "done" && task.untranslatedCount > 0) || task.needsLayoutReview
        case .completed: return task.status == "done" && task.untranslatedCount == 0 && !task.needsLayoutReview
        case .archived: return false
        }
    }
}

enum ParagraphFilter: String, CaseIterable, Identifiable {
    case all = "全部段落", untranslated = "未翻译", drafts = "修订草稿", layout = "排版待检查"
    var id: Self { self }
}

enum DocumentSearch {
    static func tasks(_ tasks: [TranslationTask], query: String, filter: LibraryFilter) -> [TranslationTask] {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return tasks.filter { filter.includes($0) && (query.isEmpty || $0.filename.localizedStandardContains(query)) }
    }

    static func segments(_ segments: [Segment], query: String, filter: ParagraphFilter, drafts: [String: String], overflowIDs: Set<String> = []) -> [Segment] {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return segments.filter { segment in
            let translation = segment.translation?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let missing = translation.isEmpty || translation.hasPrefix("⟪ untranslated:")
            if filter == .untranslated && !missing { return false }
            if filter == .drafts && drafts[segment.id] == nil { return false }
            if filter == .layout && !overflowIDs.contains(segment.id) { return false }
            return query.isEmpty || [segment.text, translation, drafts[segment.id] ?? ""].contains { $0.localizedStandardContains(query) }
        }
    }
}
