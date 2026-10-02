import AppKit
import SwiftUI

/// Opt-in QA for this app's own views. Uses an isolated Application Support directory.
@MainActor
enum SmokeChecks {
    static func runIfRequested(_ model: AppModel) async {
        guard let destination = ProcessInfo.processInfo.environment["DOC_TRANSLATOR_UI_SMOKE"],
              ProcessInfo.processInfo.environment["DOC_TRANSLATOR_APP_SUPPORT"] != nil else { return }
        let folder = URL(fileURLWithPath: destination)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        var report: [String: Any] = ["ready": model.ready, "documents": model.tasks.count,
                                    "engine_port": model.backend.port,
                                    "captures_enabled": ProcessInfo.processInfo.environment["DOC_TRANSLATOR_SMOKE_SKIP_CAPTURES"] != "1"]
        do {
            guard model.ready else { throw APIError(message: model.startupError ?? "Application not ready") }
            if ProcessInfo.processInfo.environment["DOC_TRANSLATOR_RESTORE_SMOKE"] == "1" {
                try await verifyRestoredReading(model, folder: folder)
                report["reading_restored_after_relaunch"] = true
                report["success"] = true
                finish(report, folder: folder)
                return
            }
            if let expected = ProcessInfo.processInfo.environment["DOC_TRANSLATOR_SMOKE_EXPECT_OPEN"] {
                for _ in 0..<100 {
                    if model.tasks.contains(where: { $0.filename == expected }) { break }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                guard model.tasks.contains(where: { $0.filename == expected }) else {
                    throw APIError(message: "Finder open event did not import the document")
                }
                await model.imports.waitUntilIdle()
                report["finder_open"] = true
            }
            if let fixtures = ProcessInfo.processInfo.environment["DOC_TRANSLATOR_SMOKE_IMPORT_DIR"] {
                let root = URL(fileURLWithPath: fixtures)
                let broken = folder.appendingPathComponent("Broken.docx")
                try Data("Invalid Office document".utf8).write(to: broken)
                var files = ["pdf", "docx", "pptx", "xlsx"].map { root.appendingPathComponent("Sample.\($0)") }
                files.insert(broken, at: 1)
                files.append(files[0])
                model.importFiles(files)
                await model.imports.waitUntilIdle()
                guard model.imports.successfulCount == 4, model.imports.failures.count == 1,
                      model.imports.failures.first?.filename == "Broken.docx" else {
                    throw APIError(message: "Mixed batch import did not produce four successes and one failure")
                }
                report["batch_import_four_formats"] = true
                report["import_continues_after_failure"] = true
            }
            try await verifyLibraryKeyboardNavigation(model)
            report["library_keyboard_navigation"] = true
            try await verifyLibraryFiltering(model)
            report["library_filter_typing_and_keyboard"] = true
            if let original = model.tasks.first(where: { $0.filename == "Sample.pdf" && !$0.hasTranslated }) {
                await model.selectTask(original.id)
                model.mode = 1
                for _ in 0..<100 {
                    if model.originalPage != nil { break }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                guard model.originalPage != nil, model.translatedPage == nil, model.translatedPageCount == 0 else {
                    throw APIError(message: "Original-only preview failed")
                }
                try await Task.sleep(nanoseconds: 300_000_000)
                try captureWindow(to: folder.appendingPathComponent("native-original-preview.png"))
                report["original_preview_before_translation"] = true
            }
            if let task = model.tasks.first(where: { $0.filename == "Sample.pdf" && $0.hasTranslated }) {
                await model.selectTask(task.id)
                try await Task.sleep(nanoseconds: 800_000_000)
                try captureWindow(to: folder.appendingPathComponent("native-paragraphs.png"))
                model.libraryQuery = "Sample"
                model.libraryFilter = .completed
                model.paragraphQuery = "Reading"
                let matches = DocumentSearch.segments(model.segments, query: model.paragraphQuery, filter: .all, drafts: [:])
                guard matches.count == 1 else { throw APIError(message: "Paragraph search mismatch") }
                try await Task.sleep(nanoseconds: 400_000_000)
                try captureWindow(to: folder.appendingPathComponent("native-search.png"))
                model.libraryQuery = ""
                model.libraryFilter = .all
                model.paragraphQuery = ""
                report["history_and_paragraph_search"] = true
                guard let segment = model.segments.first else { throw APIError(message: "No sample paragraph") }
                guard await model.revise(taskID: task.id, segment: segment, text: String(repeating: "Long translation. ", count: 2000)),
                      model.selectedTask?.needsLayoutReview == true,
                      model.overflowSegmentIDs.contains(segment.id) else {
                    throw APIError(message: "PDF overflow was not reported")
                }
                model.paragraphFilter = .layout
                try await Task.sleep(nanoseconds: 300_000_000)
                try captureWindow(to: folder.appendingPathComponent("native-layout-warning.png"))
                guard await model.revise(taskID: task.id, segment: segment, text: "Revised translation."),
                      model.selectedTask?.needsLayoutReview == false, model.overflowSegmentIDs.isEmpty else {
                    throw APIError(message: "Shorter PDF revision did not clear the overflow warning")
                }
                model.paragraphFilter = .all
                report["pdf_overflow_warning_and_repair"] = true
                let draft = "这是一段尚未应用到译文的修订草稿。切换文档后仍会保留。"
                model.drafts.set(draft, taskID: task.id, segmentID: segment.id)
                if let other = model.tasks.first(where: { $0.id != task.id }) {
                    await model.selectTask(other.id)
                    await model.selectTask(task.id)
                }
                let recovered = RevisionDraftStore(url: model.drafts.url)
                guard recovered.text(taskID: task.id, segmentID: segment.id) == draft else {
                    throw APIError(message: "Draft was lost after navigation")
                }
                try await Task.sleep(nanoseconds: 500_000_000)
                model.paragraphFilter = .drafts
                try await Task.sleep(nanoseconds: 200_000_000)
                try captureWindow(to: folder.appendingPathComponent("native-drafts.png"))
                guard await model.setArchived(task, archived: true),
                      !DocumentSearch.tasks(model.tasks, query: "", filter: .all).contains(where: { $0.id == task.id }) else {
                    throw APIError(message: "Archive did not remove the document from the active library")
                }
                model.libraryFilter = .archived
                await model.selectTask(task.id)
                guard let archived = model.selectedTask, archived.isArchived,
                      model.drafts.text(taskID: task.id, segmentID: segment.id) == draft else {
                    throw APIError(message: "Archive lost the document or its draft")
                }
                try await Task.sleep(nanoseconds: 300_000_000)
                try captureWindow(to: folder.appendingPathComponent("native-archive.png"))
                guard await model.setArchived(archived, archived: false) else {
                    throw APIError(message: "Unable to restore the archived document")
                }
                model.libraryFilter = .all
                await model.selectTask(task.id)
                guard model.drafts.text(taskID: task.id, segmentID: segment.id) == draft else {
                    throw APIError(message: "Restore lost the revision draft")
                }
                report["archive_restore_preserves_draft"] = true
                for (index, row) in model.segments.enumerated() {
                    model.drafts.set(index == 0 ? "Revised translation." : "Saved with all drafts.", taskID: task.id, segmentID: row.id)
                }
                let beforeBulk = model.selectedTask?.contentVersion
                let export = folder.appendingPathComponent("native-export-all-drafts.pdf")
                guard await model.exportTranslation(decide: { _ in .saveAll }, chooseDestination: { _ in export }),
                      model.drafts.count(taskID: task.id) == 0,
                      model.selectedTask?.contentVersion == (beforeBulk ?? 0) + 1 else {
                    throw APIError(message: "All-draft export failed or committed more than once")
                }
                let exportedBytes = try Data(contentsOf: export)
                let reopened = try await model.backend.api.upload(export)
                let reopenedSegments: SegmentList = try await model.backend.api.request("api/tasks/\(reopened.id)/segments")
                guard reopenedSegments.segments.contains(where: { $0.text.contains("Revised translation.") }),
                      reopenedSegments.segments.contains(where: { $0.text.contains("Saved with all drafts.") }) else {
                    throw APIError(message: "Export did not contain both saved drafts")
                }
                model.drafts.set("Draft not exported", taskID: task.id, segmentID: segment.id)
                let savedOnly = folder.appendingPathComponent("native-export-saved-version.pdf")
                guard await model.exportTranslation(decide: { _ in .savedVersion }, chooseDestination: { _ in savedOnly }),
                      try Data(contentsOf: savedOnly) == exportedBytes,
                      model.drafts.count(taskID: task.id) == 1 else {
                    throw APIError(message: "Saved-version export changed the document or discarded its draft")
                }
                model.drafts.remove(taskID: task.id, segmentID: segment.id)
                report["bulk_draft_export_and_reopen"] = true
                report["saved_version_export_preserves_drafts"] = true
                model.paragraphFilter = .all
                report["durable_drafts"] = true
                model.mode = 1
                // Changing the model's reading mode starts the preview request.
                for _ in 0..<100 {
                    if model.originalPage != nil && model.translatedPage != nil { break }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                guard model.originalPage != nil, model.translatedPage != nil else {
                    throw APIError(message: model.pageError ?? "No page images")
                }
                try await Task.sleep(nanoseconds: 400_000_000)
                try captureWindow(to: folder.appendingPathComponent("native-pages.png"))
                report["paragraphs"] = model.segments.count
                report["page_preview"] = true
            } else { throw APIError(message: "QA sample not found") }
            if let fixtures = ProcessInfo.processInfo.environment["DOC_TRANSLATOR_SMOKE_IMPORT_DIR"] {
                try await verifyReadingNavigation(model, fixtures: URL(fileURLWithPath: fixtures), folder: folder)
                report["reading_navigation_and_memory"] = true
            }
            let configuration = try await model.saveProvider(name: "本地验收配置", baseURL: "http://127.0.0.1:1/v1",
                                                             model: "local-test", apiKey: "", existing: nil)
            NotificationCenter.default.post(name: .openProviders, object: nil)
            for _ in 0..<30 {
                if model.providerEditor != nil { break }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            guard let editor = model.providerEditor, editor.selectedID == configuration.provider.id else {
                throw APIError(message: "Settings did not select the saved configuration")
            }
            editor.modelName = "修改后的模型（尚未保存）"
            guard !editor.canTest else { throw APIError(message: "Unsaved configuration can still be tested") }
            try await Task.sleep(nanoseconds: 300_000_000)
            try captureWindow(to: folder.appendingPathComponent("native-settings-unsaved.png"), preferSheet: true)
            _ = await editor.complete(.select(configuration.provider.id), save: false)
            editor.test()
            for _ in 0..<100 {
                if !editor.isTesting { break }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            guard !editor.isTesting, !editor.feedback.isEmpty, editor.feedback != "连接成功" else {
                throw APIError(message: "Offline API did not report its connection failure")
            }
            try await Task.sleep(nanoseconds: 300_000_000)
            try captureWindow(to: folder.appendingPathComponent("native-settings-error.png"), preferSheet: true)
            report["settings_protect_unsaved_fields"] = true
            report["settings_connection_error"] = true
            report["settings_dirty_at_quit"] = editor.hasUnsavedChanges
            report["success"] = true
        } catch {
            report["success"] = false
            report["error"] = error.localizedDescription
            try? captureWindow(to: folder.appendingPathComponent("native-failure.png"))
        }
        finish(report, folder: folder)
    }

    private static func finish(_ report: [String: Any], folder: URL) {
        if let bytes = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) {
            try? bytes.write(to: folder.appendingPathComponent("native-smoke.json"))
        }
        DispatchQueue.main.async { NotificationCenter.default.post(name: .quitApplication, object: nil) }
    }

    private struct ReadingExpectation: Codable {
        let taskID: String
        let bookmark: ReadingBookmark
    }

    private static func verifyLibraryKeyboardNavigation(_ model: AppModel) async throws {
        try await Task.sleep(nanoseconds: 200_000_000)
        let visible = DocumentSearch.tasks(model.tasks, query: model.libraryQuery, filter: model.libraryFilter)
        guard let previous = model.selectedTaskID, let index = visible.firstIndex(where: { $0.id == previous }), visible.count > 1 else {
            throw APIError(message: "No selected library row for keyboard verification")
        }
        func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
        guard let window = NSApplication.shared.windows.first(where: { $0.isVisible && $0.frame.width > 700 }),
              let content = window.contentView,
              let table = descendants(content).compactMap({ $0 as? NSTableView }).first(where: { $0.numberOfRows == visible.count && $0.bounds.width < 400 }),
              window.makeFirstResponder(table) else { throw APIError(message: "Sidebar list did not accept keyboard focus") }
        let down = index < visible.count - 1
        let next = visible[index + (down ? 1 : -1)].id
        let character = down ? "\u{F701}" : "\u{F700}"
        guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [],
                                          timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber,
                                          context: nil, characters: character, charactersIgnoringModifiers: character,
                                          isARepeat: false, keyCode: down ? 125 : 126) else { throw APIError(message: "Unable to create the reader keyboard event") }
        table.keyDown(with: event)
        for _ in 0..<50 {
            if model.selectedTaskID == next && !model.segmentLoading { break }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        guard model.selectedTaskID == next, !model.segmentLoading else { throw APIError(message: "Arrow key did not select and load the next document") }
        model.libraryQuery = "no-document-matches-this-query"
        try await Task.sleep(nanoseconds: 100_000_000)
        guard model.selectedTaskID == next else { throw APIError(message: "Filtering the library closed the selected document") }
        model.libraryQuery = ""
        await model.selectTask(previous)
    }

    private static func verifyLibraryFiltering(_ model: AppModel) async throws {
        func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
        guard let window = NSApplication.shared.windows.first(where: { $0.isVisible && $0.frame.width > 700 }),
              let content = window.contentView else { throw APIError(message: "No window for library filter verification") }
        let selected = model.selectedTaskID
        for filter in LibraryFilter.allCases + [.all] {
            for query in ["Sample", "no-matching-document", ""] {
                model.libraryFilter = filter
                model.libraryQuery = query
                let expected = DocumentSearch.tasks(model.tasks, query: query, filter: filter).count
                try await Task.sleep(nanoseconds: 80_000_000)
                guard model.selectedTaskID == selected,
                      descendants(content).compactMap({ $0 as? NSTableView }).contains(where: { $0.bounds.width < 400 && $0.numberOfRows == expected }) else {
                    throw APIError(message: "Library filter did not update its rows or retain the open document")
                }
            }
        }
        guard let field = descendants(content).compactMap({ $0 as? NSTextField }).first(where: { $0.placeholderString == "搜索文档名称" }),
              window.makeFirstResponder(field), let editor = field.currentEditor() as? NSTextView else {
            throw APIError(message: "Library search did not accept text input")
        }
        var typed = ""
        for character in "Sample" {
            typed.append(character)
            editor.insertText(String(character), replacementRange: editor.selectedRange())
            try await Task.sleep(nanoseconds: 80_000_000)
            guard model.libraryQuery == typed, window.firstResponder === editor else {
                throw APIError(message: "Updating search results interrupted text input")
            }
        }
        editor.insertText("", replacementRange: NSRange(location: 0, length: editor.string.utf16.count))
        try await Task.sleep(nanoseconds: 100_000_000)
        guard model.libraryQuery.isEmpty, window.firstResponder === editor,
              model.selectedTaskID == selected else { throw APIError(message: "Clearing search lost focus or the open document") }
        try await verifyLibraryKeyboardNavigation(model)
    }

    private static func waitForOriginalPage(_ model: AppModel) async throws {
        for _ in 0..<100 {
            if model.originalPage != nil { return }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        throw APIError(message: model.pageError ?? "Reading preview was not ready")
    }

    private static func verifyReadingNavigation(_ model: AppModel, fixtures: URL, folder: URL) async throws {
        let previousID = model.selectedTaskID
        let document = try await model.backend.api.upload(fixtures.appendingPathComponent("LongReading.pdf"))
        let paragraphs: SegmentList = try await model.backend.api.request("api/tasks/\(document.id)/segments")
        guard paragraphs.segments.count > 16 else { throw APIError(message: "Long reading fixture has too few paragraphs") }
        var anchor = paragraphs.segments[16].id
        model.reading.update(ReadingBookmark(paragraphID: anchor), for: document.id)
        try await model.refreshTasks()
        await model.selectTask(document.id)
        try await Task.sleep(nanoseconds: 500_000_000)
        guard model.paragraphAnchor == anchor else { throw APIError(message: "Paragraph scroll did not keep its requested position") }
        try captureWindow(to: folder.appendingPathComponent("native-reading-paragraph.png"))
        try scrollReadingWindow(by: 180)
        try await Task.sleep(nanoseconds: 400_000_000)
        guard let scrolled = model.paragraphAnchor, scrolled != anchor else {
            throw APIError(message: "Scrolling the reader did not update its bookmark")
        }
        anchor = scrolled
        model.paragraphQuery = "page 1"
        try await Task.sleep(nanoseconds: 200_000_000)
        model.paragraphQuery = ""
        try await Task.sleep(nanoseconds: 400_000_000)
        guard model.paragraphAnchor == anchor else { throw APIError(message: "Searching changed the reading bookmark") }
        await model.selectTask(previousID)
        await model.selectTask(document.id)
        try await Task.sleep(nanoseconds: 500_000_000)
        guard model.mode == 0, model.paragraphAnchor == anchor else {
            throw APIError(message: "Switching documents lost the paragraph reading position")
        }
        try captureWindow(to: folder.appendingPathComponent("native-reading-returned-paragraph.png"))
        model.mode = 1
        try await waitForOriginalPage(model)
        guard model.pageCount == 20 else { throw APIError(message: "Expected 20 reading pages") }
        model.pageInput = "12"
        guard await model.submitPageInput(), model.pageNumber == 12 else { throw APIError(message: "Direct page navigation failed") }
        model.pageZoom = 1.25
        model.pageInput = "21"
        guard !(await model.submitPageInput()), model.pageNumber == 12, model.pageNavigationError != nil else {
            throw APIError(message: "Invalid page changed the reading page")
        }
        try await Task.sleep(nanoseconds: 300_000_000)
        try captureWindow(to: folder.appendingPathComponent("native-reading-invalid-page.png"))
        model.pageInput = "12"
        guard await model.submitPageInput() else { throw APIError(message: "Valid page did not clear the input error") }
        try await Task.sleep(nanoseconds: 300_000_000)
        model.reading.flush()
        let expected = ReadingExpectation(taskID: document.id, bookmark: ReadingBookmark(mode: 1, page: 12, zoom: 1.25, paragraphID: anchor))
        try JSONEncoder().encode(expected).write(to: model.backend.supportURL.appendingPathComponent("reading-expected.json"), options: .atomic)
        try captureWindow(to: folder.appendingPathComponent("native-reading-page.png"))
    }

    private static func verifyRestoredReading(_ model: AppModel, folder: URL) async throws {
        let expected = try JSONDecoder().decode(ReadingExpectation.self, from: Data(contentsOf: model.backend.supportURL.appendingPathComponent("reading-expected.json")))
        guard model.selectedTaskID == expected.taskID, model.mode == expected.bookmark.mode,
              model.pageNumber == expected.bookmark.page, model.pageZoom == expected.bookmark.zoom else {
            throw APIError(message: "Relaunch did not restore the document, view, page and zoom")
        }
        try await waitForOriginalPage(model)
        guard model.pageCount == 20 else { throw APIError(message: "Restored document page count mismatch") }
        try await Task.sleep(nanoseconds: 400_000_000)
        try captureWindow(to: folder.appendingPathComponent("native-restored-page.png"))
        model.mode = 0
        try await Task.sleep(nanoseconds: 500_000_000)
        guard model.paragraphAnchor == expected.bookmark.paragraphID else {
            throw APIError(message: "Relaunch did not restore the paragraph position")
        }
        try captureWindow(to: folder.appendingPathComponent("native-restored-paragraph.png"))
    }

    /// Scrolls only this app's reader to verify that visible positions reach the persisted bookmark.
    private static func scrollReadingWindow(by offset: CGFloat) throws {
        let scroll = try readingScrollView()
        var position = scroll.contentView.bounds.origin
        position.y += offset
        scroll.contentView.scroll(to: position)
        scroll.reflectScrolledClipView(scroll.contentView)
    }

    private static func readingScrollView() throws -> NSScrollView {
        func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
        guard let content = NSApplication.shared.windows.first(where: { $0.isVisible && $0.frame.width > 700 })?.contentView,
              let scroll = descendants(content).compactMap({ $0 as? NSScrollView }).first(where: {
                  $0.bounds.width > 400 && ($0.documentView?.bounds.height ?? 0) > $0.contentView.bounds.height * 1.5
              }) else { throw APIError(message: "Long document reader scroll view was not found") }
        return scroll
    }

    private static func captureWindow(to url: URL, preferSheet: Bool = false) throws {
        if ProcessInfo.processInfo.environment["DOC_TRANSLATOR_SMOKE_SKIP_CAPTURES"] == "1" { return }
        let main = NSApplication.shared.windows.first(where: { $0.isVisible && $0.frame.width > 700 })
        guard let window = preferSheet ? main?.attachedSheet : main,
              let view = window.contentView,
              let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw APIError(message: "Main window was not visible")
        }
        view.layoutSubtreeIfNeeded()
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw APIError(message: "Unable to capture app view")
        }
        try data.write(to: url)
    }
}
