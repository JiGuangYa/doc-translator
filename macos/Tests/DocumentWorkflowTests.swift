import XCTest
@testable import DocTranslatorMac

@MainActor
final class DocumentWorkflowTests: XCTestCase {
    func document(_ name: String, status: String = "pending_confirm", missing: Int = 1) -> TranslationTask {
        let data = try! JSONSerialization.data(withJSONObject: ["task_id": name, "filename": name, "ext": ".pdf", "status": status,
                                                               "warnings": [], "has_translated": status == "done",
                                                               "untranslated_count": missing, "content_version": 0])
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(TranslationTask.self, from: data)
    }
    func file(_ name: String) -> URL { URL(fileURLWithPath: "/tmp/" + name) }

    func testQueueWaitsForEngineAndContinuesAfterBadFileWithoutDuplicates() async {
        var attempted: [String] = [], imported: [String] = []
        let queue = DocumentImportQueue { url in
            attempted.append(url.lastPathComponent)
            if url.lastPathComponent == "broken.docx" { throw APIError(message: "损坏的文档") }
            return self.document(url.lastPathComponent)
        }
        queue.onImported = { imported.append($0.filename) }
        queue.enqueue([file("first.pdf"), file("broken.docx"), file("last.xlsx"), file("first.pdf"), file("unsupported.txt")])
        XCTAssertEqual(queue.pendingCount, 3)
        XCTAssertEqual(queue.totalCount, 3)
        XCTAssertTrue(attempted.isEmpty)
        queue.setEnabled(true)
        await queue.waitUntilIdle()
        XCTAssertEqual(attempted, ["first.pdf", "broken.docx", "last.xlsx"])
        XCTAssertEqual(imported, ["first.pdf", "last.xlsx"])
        XCTAssertEqual(queue.successfulCount, 2)
        XCTAssertEqual(queue.completedCount, 3)
        XCTAssertEqual(queue.failures.map(\.filename), ["unsupported.txt", "broken.docx"])
        XCTAssertFalse(queue.isImporting)
    }

    func testMoreFilesCanBeAddedDuringImportAndProcessedSerially() async {
        var active = 0, peak = 0, imported: [String] = []
        let queue = DocumentImportQueue { url in
            active += 1; peak = max(peak, active)
            try await Task.sleep(nanoseconds: 10_000_000)
            active -= 1
            return self.document(url.lastPathComponent)
        }
        queue.onImported = { task in
            imported.append(task.filename)
            if task.filename == "first.pdf" { queue.enqueue([self.file("added.pptx")]) }
        }
        queue.setEnabled(true)
        queue.enqueue([file("first.pdf"), file("second.docx")])
        await queue.waitUntilIdle()
        XCTAssertEqual(peak, 1)
        XCTAssertEqual(imported, ["first.pdf", "second.docx", "added.pptx"])
        XCTAssertEqual(queue.totalCount, 3)
        XCTAssertEqual(queue.successfulCount, 3)
    }

    func testStopPendingKeepsCurrentFileAndShutdownStopsCallbacks() async {
        var attempted: [String] = []
        let queue = DocumentImportQueue { url in
            attempted.append(url.lastPathComponent)
            try await Task.sleep(nanoseconds: 100_000_000)
            return self.document(url.lastPathComponent)
        }
        queue.setEnabled(true)
        queue.enqueue([file("first.pdf"), file("second.docx")])
        await Task.yield()
        XCTAssertEqual(queue.currentFilename, "first.pdf")
        queue.cancelPending()
        await queue.waitUntilIdle()
        XCTAssertEqual(attempted, ["first.pdf"])
        XCTAssertEqual(queue.successfulCount, 1)
        queue.enqueue([file("third.pdf"), file("fourth.pdf")])
        await Task.yield()
        await queue.shutdown()
        XCTAssertFalse(queue.isImporting)
        XCTAssertEqual(queue.successfulCount, 0)
        XCTAssertTrue(queue.failures.isEmpty)
        XCTAssertFalse(attempted.contains("fourth.pdf"))
    }

    func testHistorySearchAndStatusFiltersIncludeIncompleteCompletedTasks() {
        let tasks = [document("Résumé.PDF", status: "done", missing: 0), document("resume.docx", status: "done"),
                     document("notes.pdf", status: "failed"), document("draft.pdf")]
        XCTAssertEqual(DocumentSearch.tasks(tasks, query: " RESUME ", filter: .all).count, 2)
        XCTAssertEqual(DocumentSearch.tasks(tasks, query: "", filter: .completed).map(\.filename), ["Résumé.PDF"])
        XCTAssertEqual(DocumentSearch.tasks(tasks, query: "", filter: .needsAttention).map(\.filename), ["resume.docx", "notes.pdf"])
        XCTAssertEqual(DocumentSearch.tasks(tasks, query: "", filter: .waiting).map(\.filename), ["draft.pdf"])
    }

    func testParagraphSearchIncludesBothLanguagesDraftsAndPlaceholders() {
        let segments = [Segment(segId: "a", text: "Original text", translation: "翻译结果", context: "", translatable: true),
                        Segment(segId: "b", text: "missing", translation: "⟪ untranslated:missing ⟫", context: "", translatable: true),
                        Segment(segId: "c", text: "not started", translation: nil, context: "", translatable: true)]
        let drafts = ["a": "Pending revision", "c": ""]
        XCTAssertEqual(DocumentSearch.segments(segments, query: "original", filter: .all, drafts: drafts).map(\.id), ["a"])
        XCTAssertEqual(DocumentSearch.segments(segments, query: "翻译", filter: .all, drafts: drafts).map(\.id), ["a"])
        XCTAssertEqual(DocumentSearch.segments(segments, query: "REVISION", filter: .all, drafts: drafts).map(\.id), ["a"])
        XCTAssertEqual(DocumentSearch.segments(segments, query: "", filter: .untranslated, drafts: drafts).map(\.id), ["b", "c"])
        XCTAssertEqual(DocumentSearch.segments(segments, query: "", filter: .drafts, drafts: drafts).map(\.id), ["a", "c"])
        XCTAssertEqual(drafts["c"], "")
    }

    func testArchivedDocumentsHaveTheirOwnSearchableLibrary() {
        var archived = document("已归档.pdf", status: "done", missing: 0)
        archived.archived = true
        let active = document("正在阅读.pdf", status: "done", missing: 0)
        XCTAssertFalse(active.isArchived) // Records saved by earlier app versions.
        XCTAssertEqual(DocumentSearch.tasks([active, archived], query: "", filter: .all).map(\.id), [active.id])
        XCTAssertEqual(DocumentSearch.tasks([active, archived], query: "归档", filter: .archived).map(\.id), [archived.id])
        XCTAssertEqual(DocumentSearch.tasks([active, archived], query: "", filter: .completed).map(\.id), [active.id])
    }

    func testLayoutOverflowIsActionableInLibraryAndParagraphs() {
        var task = document("long.pdf", status: "done", missing: 0)
        task.overflowCount = 1
        XCTAssertTrue(LibraryFilter.needsAttention.includes(task))
        XCTAssertFalse(LibraryFilter.completed.includes(task))
        let segments = [Segment(segId: "a", text: "Title", translation: "Too long", context: "", translatable: true),
                        Segment(segId: "b", text: "Body", translation: "Fits", context: "", translatable: true)]
        XCTAssertEqual(DocumentSearch.segments(segments, query: "", filter: .layout, drafts: [:], overflowIDs: ["a"]).map(\.id), ["a"])
    }
}
