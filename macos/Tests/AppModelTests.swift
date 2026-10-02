import XCTest
import AppKit
@testable import DocTranslatorMac

final class StubProtocol: URLProtocol, @unchecked Sendable {
    static var respond: (URLRequest) -> (Double, Int, Data) = { _ in (0, 200, Data("{}".utf8)) }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (delay, status, data) = Self.respond(request)
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [self] in
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() {}
}

@MainActor
final class AppModelTests: XCTestCase {
    func api() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return APIClient(port: 12345, token: "test-token", configuration: config)
    }
    func json(_ object: Any) -> Data { try! JSONSerialization.data(withJSONObject: object) }
    func model() -> AppModel {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return AppModel(backend: Backend(api: api()), drafts: RevisionDraftStore(url: directory.appendingPathComponent("drafts.json")))
    }
    func segment(_ text: String) -> Data {
        json(["segments": [["seg_id": "s000000", "text": text, "translation": "译文", "context": "", "translatable": true]], "warnings": []])
    }
    func task(_ status: String = "pending_confirm", id: String = "a") -> TranslationTask {
        let data = json(["task_id": id, "filename": "test.pdf", "ext": ".pdf", "status": status,
                         "warnings": [], "has_translated": status == "done", "untranslated_count": 1, "content_version": 0])
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(TranslationTask.self, from: data)
    }

    func provider(_ id: String = "p") -> Provider {
        Provider(id: id, name: "Test service", baseUrl: "http://127.0.0.1:9999/v1", model: "test-model", hasApiKey: true)
    }
    func providerResponse(_ id: String = "p") -> Data {
        json(["ok": true, "provider": ["id": id, "name": "Test service", "base_url": "http://127.0.0.1:9999/v1",
                                      "model": "test-model", "has_api_key": true]])
    }

    func testProviderEditorDoesNotTestUnsavedFieldsAndKeepsDraftOnCancelledSwitch() {
        let model = model()
        model.providers = [provider(), provider("other")]; model.selectedProviderID = "p"
        let editor = ProviderEditor(model: model)
        XCTAssertEqual(editor.selectedID, "p")
        XCTAssertTrue(editor.canTest)
        editor.modelName = "unsaved-model"
        XCTAssertFalse(editor.canTest)
        let counter = Counter(), ok = json(["ok": true])
        StubProtocol.respond = { _ in counter.increment(); return (0, 200, ok) }
        editor.test()
        XCTAssertFalse(editor.isTesting)
        XCTAssertEqual(counter.value, 0)
        XCTAssertFalse(editor.request(.select("other")))
        XCTAssertNotNil(editor.pendingTransition)
        editor.pendingTransition = nil
        XCTAssertEqual(editor.selectedID, "p")
        XCTAssertEqual(editor.modelName, "unsaved-model")
    }

    func testProviderSaveRetainsIDWhenDefaultsFailAndNextSaveUpdatesIt() async {
        let model = model(), editor = ProviderEditor(model: model)
        editor.name = "Test service"; editor.baseURL = "http://127.0.0.1:9999/v1"; editor.modelName = "test-model"; editor.apiKey = "test-placeholder"
        let saved = providerResponse("saved"), failed = json(["detail": "settings disk full"]), posts = Counter(), puts = Counter()
        StubProtocol.respond = { request in
            if request.url!.path == "/api/settings" { return (0, 500, failed) }
            if request.httpMethod == "POST" { posts.increment() }
            if request.httpMethod == "PUT" { puts.increment() }
            return (0, 200, saved)
        }
        let first = await editor.save()
        XCTAssertTrue(first)
        XCTAssertEqual(editor.selectedID, "saved")
        XCTAssertEqual(model.providers.map(\.id), ["saved"])
        XCTAssertTrue(editor.feedback.contains("默认配置保存失败"))
        XCTAssertFalse(editor.hasUnsavedChanges)
        XCTAssertEqual(editor.apiKey, "")
        editor.apiKey = "replacement-placeholder"
        let second = await editor.save()
        XCTAssertTrue(second)
        XCTAssertEqual(posts.value, 1)
        XCTAssertEqual(puts.value, 1)
    }

    func testFailedProviderSavePreservesFieldsAndDoesNotCompleteSwitch() async {
        let model = model()
        model.providers = [provider(), provider("other")]
        let editor = ProviderEditor(model: model)
        editor.apiKey = "unsaved-placeholder"
        editor.modelName = "unsaved-model"
        let error = json(["detail": "disk full"])
        StubProtocol.respond = { _ in (0, 500, error) }
        let close = await editor.complete(.select("other"), save: true)
        XCTAssertFalse(close)
        XCTAssertEqual(editor.selectedID, "p")
        XCTAssertEqual(editor.modelName, "unsaved-model")
        XCTAssertEqual(editor.apiKey, "unsaved-placeholder")
        XCTAssertTrue(editor.feedback.contains("保存失败"))
    }

    func testDuplicateProviderSaveOnlySubmitsOnce() async {
        let model = model(); model.providers = [provider()]
        let editor = ProviderEditor(model: model)
        editor.apiKey = "placeholder"
        let saved = providerResponse(), ok = json(["ok": true]), counter = Counter()
        StubProtocol.respond = { request in
            if request.url!.path == "/api/settings" { return (0, 200, ok) }
            counter.increment(); return (0.03, 200, saved)
        }
        async let first = editor.save()
        async let second = editor.save()
        let results = await [first, second]
        XCTAssertEqual(results, [true, true])
        XCTAssertEqual(counter.value, 1)
    }

    func testCancelledProviderTestCannotOverwriteNewForm() async {
        let model = model(); model.providers = [provider(), provider("other")]
        let editor = ProviderEditor(model: model), ok = json(["ok": true])
        StubProtocol.respond = { _ in (0.10, 200, ok) }
        editor.test()
        await Task.yield()
        XCTAssertTrue(editor.isTesting)
        editor.cancelTest()
        _ = editor.request(.select("other"))
        editor.modelName = "new-unsaved-model"
        try? await Task.sleep(nanoseconds: 130_000_000)
        XCTAssertEqual(editor.modelName, "new-unsaved-model")
        XCTAssertEqual(editor.feedback, "")
        XCTAssertFalse(editor.isTesting)
    }

    func testQuitRespectsUnsavedProviderChoiceAndFailedSave() async {
        let model = model(); model.providers = [provider()]
        let editor = ProviderEditor(model: model); model.providerEditor = editor
        editor.apiKey = "unsaved-placeholder"
        let cancelled = await model.prepareToQuit(decide: { .alertThirdButtonReturn })
        XCTAssertFalse(cancelled)
        let error = json(["detail": "save failed"])
        StubProtocol.respond = { _ in (0, 500, error) }
        let failed = await model.prepareToQuit(decide: { .alertFirstButtonReturn })
        XCTAssertFalse(failed)
        XCTAssertEqual(editor.apiKey, "unsaved-placeholder")
        let discarded = await model.prepareToQuit(decide: { .alertSecondButtonReturn })
        XCTAssertTrue(discarded)
    }

    func testSelectionDiscardsLateDocument() async {
        let slow = segment("Old document"), fast = segment("Current document")
        StubProtocol.respond = { request in (request.url!.path.contains("/old/") ? 0.15 : 0.005, 200, request.url!.path.contains("/old/") ? slow : fast) }
        let model = model()
        let first = Task { await model.selectTask("old") }
        try? await Task.sleep(nanoseconds: 10_000_000)
        await model.selectTask("new")
        await first.value
        try? await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(model.selectedTaskID, "new")
        XCTAssertEqual(model.segments.first?.text, "Current document")
    }

    func testFailedRevisionReportsFailureAndPreservesContent() async {
        let failure = json(["detail": "write failed"])
        StubProtocol.respond = { _ in (0, 500, failure) }
        let model = model()
        let segment = Segment(segId: "s000000", text: "source", translation: "saved", context: "", translatable: true)
        model.segments = [segment]
        let saved = await model.revise(taskID: "a", segment: segment, text: "draft")
        XCTAssertFalse(saved)
        XCTAssertEqual(model.segments.first?.translation, "saved")
        XCTAssertTrue(model.errorMessage?.contains("write failed") == true)
    }

    func testArchiveFailureKeepsSelectionAndDurableDraft() async {
        let model = model(), task = task()
        model.tasks = [task]; model.selectedTaskID = task.id
        model.drafts.set("尚未保存的修改", taskID: task.id, segmentID: "s000000")
        let failure = json(["detail": "disk full"])
        StubProtocol.respond = { _ in (0, 500, failure) }
        let result = await model.setArchived(task, archived: true)
        XCTAssertFalse(result)
        XCTAssertEqual(model.selectedTaskID, task.id)
        XCTAssertFalse(model.tasks[0].isArchived)
        XCTAssertEqual(RevisionDraftStore(url: model.drafts.url).text(taskID: task.id, segmentID: "s000000"), "尚未保存的修改")
        XCTAssertTrue(model.errorMessage?.contains("disk full") == true)
    }

    func testArchiveDeduplicatesAndRejectsStaleHistoryWithoutLosingDrafts() async throws {
        let model = model(), task = task(), patches = Counter()
        model.tasks = [task]; model.selectedTaskID = task.id
        model.drafts.set("保留草稿", taskID: task.id, segmentID: "s000000")
        let object: [String: Any] = ["task_id": "a", "filename": "test.pdf", "ext": ".pdf", "status": "pending_confirm",
                                    "warnings": [], "has_translated": false, "untranslated_count": 1, "content_version": 0]
        var archiveObject = object; archiveObject["archived"] = true
        let archived = json(archiveObject), old = json([object]), source = segment("Original")
        StubProtocol.respond = { request in
            if request.httpMethod == "PATCH" { patches.increment(); return (0.025, 200, archived) }
            if request.url!.path == "/api/tasks" { return (0.10, 200, old) }
            return (0, 200, source)
        }
        let refresh = Task { try await model.refreshTasks() }
        try await Task.sleep(nanoseconds: 10_000_000)
        async let first = model.setArchived(task, archived: true)
        async let second = model.setArchived(task, archived: true)
        let results = await [first, second]
        try await refresh.value
        XCTAssertEqual(results.filter { $0 }.count, 1)
        XCTAssertEqual(patches.value, 1)
        XCTAssertTrue(model.tasks[0].isArchived)
        XCTAssertNil(model.selectedTaskID)
        XCTAssertEqual(model.drafts.text(taskID: task.id, segmentID: "s000000"), "保留草稿")
        model.libraryFilter = .archived
        await model.selectTask(task.id)
        let restored = json(object)
        StubProtocol.respond = { _ in (0, 200, restored) }
        let result = await model.setArchived(model.tasks[0], archived: false)
        XCTAssertTrue(result)
        XCTAssertFalse(model.tasks[0].isArchived)
        XCTAssertEqual(model.drafts.text(taskID: task.id, segmentID: "s000000"), "保留草稿")
    }

    func testStartupHandshakeShowsEngineFailureWithoutNeedingLogs() throws {
        XCTAssertEqual(try Backend.readyPort(from: json(["port": 12345, "protocol_version": 1])), 12345)
        XCTAssertThrowsError(try Backend.readyPort(from: json(["protocol_version": 1, "error": "资料库正在使用"]))) { error in
            XCTAssertEqual(error.localizedDescription, "资料库正在使用")
        }
        XCTAssertThrowsError(try Backend.readyPort(from: json(["port": 0, "protocol_version": 1])))
    }

    func testDoubleStartIsSubmittedOnce() async {
        let ok = json(["ok": true]), segments = segment("source")
        let tasks = json([["task_id": "a", "filename": "test.pdf", "ext": ".pdf", "status": "translating", "warnings": [], "has_translated": false, "untranslated_count": 1, "content_version": 0]])
        let counter = Counter()
        StubProtocol.respond = { request in
            if request.url!.path.hasSuffix("/start") { counter.increment(); return (0.05, 200, ok) }
            if request.url!.path.hasSuffix("/segments") { return (0, 200, segments) }
            if request.url!.path == "/api/tasks" { return (0, 200, tasks) }
            return (0, 200, ok)
        }
        let model = model()
        model.tasks = [task()]; model.selectedTaskID = "a"; model.selectedProviderID = "p"
        async let first: Void = model.startTranslation()
        async let second: Void = model.startTranslation()
        _ = await (first, second)
        XCTAssertEqual(counter.value, 1)
        XCTAssertEqual(model.selectedTask?.status, "translating")
    }

    func png(width: Int) -> Data {
        let image = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: width,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                     isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: width * 4, bitsPerPixel: 32)!
        return image.representation(using: .png, properties: [:])!
    }

    func testRapidPageChangeDiscardsPreviousImage() async {
        let old = png(width: 2), current = png(width: 4)
        StubProtocol.respond = { request in
            let slow = request.url!.path.hasSuffix("/1")
            return (slow ? 0.15 : 0.005, 200, slow ? old : current)
        }
        let model = model()
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        model.pageCount = 2; model.originalPageCount = 2; model.translatedPageCount = 2
        let first = Task { await model.loadPage() }
        try? await Task.sleep(nanoseconds: 10_000_000)
        model.pageNumber = 2
        await model.loadPage(); await first.value
        try? await Task.sleep(nanoseconds: 180_000_000)
        XCTAssertEqual(model.originalPage?.size.width, 4)
        XCTAssertEqual(model.translatedPage?.size.width, 4)
    }

    func testUnequalPageCountsOnlyFetchAvailableSide() async {
        let image = png(width: 4)
        StubProtocol.respond = { request in
            XCTAssertTrue(request.url!.absoluteString.contains("variant=translated"))
            return (0, 200, image)
        }
        let model = model()
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        model.pageCount = 2; model.originalPageCount = 1; model.translatedPageCount = 2; model.pageNumber = 2
        await model.loadPage()
        XCTAssertNil(model.originalPage)
        XCTAssertNotNil(model.translatedPage)
        XCTAssertNil(model.pageError)
    }

    func testBrokenPageImageReportsRetryableErrorInsteadOfSpinning() async {
        let broken = Data("partial image".utf8)
        StubProtocol.respond = { _ in (0, 200, broken) }
        let model = model()
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        model.pageCount = 1; model.originalPageCount = 1; model.translatedPageCount = 1
        await model.loadPage()
        XCTAssertFalse(model.pageLoading)
        XCTAssertNil(model.originalPage)
        XCTAssertTrue(model.pageError?.contains("重试") == true)
    }

    func testOriginalPagePreviewWorksBeforeTranslation() async {
        let model = model(), image = png(width: 8)
        let info = json(["pages": 1, "original_pages": 1, "translated_pages": 0, "render_available": false, "content_version": 0])
        StubProtocol.respond = { request in
            if request.url!.path.hasSuffix("/info") { return (0, 200, info) }
            XCTAssertTrue(request.url!.absoluteString.contains("variant=original"))
            return (0, 200, image)
        }
        model.tasks = [task()]; model.selectedTaskID = "a"
        await model.preparePages()
        XCTAssertEqual(model.originalPage?.size.width, 8)
        XCTAssertNil(model.translatedPage)
        XCTAssertEqual(model.translatedPageCount, 0)
        XCTAssertEqual(model.renderStatus?.status, "ready")
    }

    func waitForPreview(_ model: AppModel) async throws {
        for _ in 0..<100 {
            if model.originalPage != nil { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail(model.pageError ?? "Preview was not ready")
    }

    func testHistoryRefreshCannotInterruptNewDocumentSelection() async throws {
        let model = model(), calls = Counter(), source = segment("Document B")
        let list = json([
            ["task_id": "a", "filename": "a.pdf", "ext": ".pdf", "status": "done", "warnings": [], "has_translated": true, "untranslated_count": 0, "content_version": 0],
            ["task_id": "b", "filename": "b.pdf", "ext": ".pdf", "status": "pending_confirm", "warnings": [], "has_translated": false, "untranslated_count": 1, "content_version": 0]
        ])
        model.tasks = [task("done"), task(id: "b")]; model.selectedTaskID = "a"
        StubProtocol.respond = { request in
            if request.url!.path == "/api/tasks" { return (0.03, 200, list) }
            calls.increment(); return (0.07, 200, source)
        }
        let refresh = Task { try await model.refreshTasks() }
        try await Task.sleep(nanoseconds: 10_000_000)
        await model.selectTask("b")
        XCTAssertEqual(model.segments.first?.text, "Document B")
        try await refresh.value
        XCTAssertEqual(calls.value, 1)
        await model.shutdown()
    }

    func testPollingJoinsPendingSegmentRequestInsteadOfCancellingIt() async throws {
        let model = model(), calls = Counter(), source = segment("In progress")
        let list = json([["task_id": "a", "filename": "a.pdf", "ext": ".pdf", "status": "translating", "warnings": [], "has_translated": false, "untranslated_count": 1, "content_version": 0]])
        model.tasks = [task("translating")]
        StubProtocol.respond = { request in
            if request.url!.path == "/api/tasks" { return (0, 200, list) }
            calls.increment(); return (0.06, 200, source)
        }
        let selection = Task { await model.selectTask("a") }
        try await Task.sleep(nanoseconds: 10_000_000)
        try await model.refreshTasks()
        await selection.value
        XCTAssertEqual(calls.value, 1)
        XCTAssertEqual(model.segments.first?.text, "In progress")
        XCTAssertFalse(model.segmentLoading)
        await model.shutdown()
    }

    func testSelectionWaitsForNewerVersionWhenPendingSegmentsAreReplaced() async throws {
        let model = model(), calls = Counter(), old = segment("Old"), new = segment("Committed")
        let list = json([["task_id": "a", "filename": "a.pdf", "ext": ".pdf", "status": "done", "warnings": [], "has_translated": true, "untranslated_count": 0, "content_version": 1]])
        model.tasks = [task("translating")]
        StubProtocol.respond = { request in
            if request.url!.path == "/api/tasks" { return (0, 200, list) }
            calls.increment(); return calls.value == 1 ? (0.07, 200, old) : (0.04, 200, new)
        }
        let selection = Task { await model.selectTask("a") }
        try await Task.sleep(nanoseconds: 10_000_000)
        let refresh = Task { try await model.refreshTasks() }
        await selection.value
        XCTAssertEqual(model.segments.first?.text, "Committed")
        try await refresh.value
        XCTAssertEqual(calls.value, 2)
        try await Task.sleep(nanoseconds: 40_000_000)
        XCTAssertEqual(model.segments.first?.text, "Committed")
        await model.shutdown()
    }

    func testReadingRestorationClampsObsoletePageAndKeepsSeparateDocuments() async throws {
        let model = model(), image = png(width: 8), source = segment("Source")
        let info = json(["pages": 5, "original_pages": 5, "translated_pages": 0, "render_available": false, "content_version": 0])
        StubProtocol.respond = { request in
            if request.url!.path.hasSuffix("/segments") { return (0, 200, source) }
            if request.url!.path.hasSuffix("/info") { return (0, 200, info) }
            XCTAssertTrue(request.url!.path.hasSuffix("/5"))
            return (0, 200, image)
        }
        model.tasks = [task(), task(id: "b")]
        model.reading.select("a")
        model.reading.update(ReadingBookmark(mode: 1, page: 99, zoom: 1.4, paragraphID: "s000000"), for: "a")
        await model.restoreLastDocument()
        try await waitForPreview(model)
        XCTAssertEqual(model.selectedTaskID, "a")
        XCTAssertEqual(model.pageNumber, 5)
        XCTAssertEqual(model.pageZoom, 1.4)
        await model.selectTask("b")
        XCTAssertEqual(model.mode, 0); XCTAssertEqual(model.pageNumber, 1); XCTAssertEqual(model.pageZoom, 1)
        model.pageZoom = 0.7
        await model.selectTask("a")
        try await waitForPreview(model)
        XCTAssertEqual(model.mode, 1); XCTAssertEqual(model.pageNumber, 5); XCTAssertEqual(model.pageZoom, 1.4)
        await model.shutdown()
        let restored = ReadingStateStore(url: model.reading.url)
        XCTAssertEqual(restored.lastTaskID, "a")
        XCTAssertEqual(restored.bookmark(for: "b").zoom, 0.7)
        XCTAssertEqual(restored.bookmark(for: "a").page, 5)
    }

    func testInvalidPageInputKeepsCurrentPageAndDoesNotFetch() async throws {
        let model = model(), calls = Counter(), image = png(width: 8)
        model.tasks = [task()]; model.selectedTaskID = "a"
        model.pageCount = 20; model.originalPageCount = 20; model.pageNumber = 7
        StubProtocol.respond = { _ in calls.increment(); return (0, 200, image) }
        for value in ["", "abc", "0", "21", "1.5", "9999999999999999999999999"] {
            model.pageInput = value
            let result = await model.submitPageInput()
            XCTAssertFalse(result); XCTAssertEqual(model.pageNumber, 7)
            XCTAssertNotNil(model.pageNavigationError)
        }
        XCTAssertEqual(calls.value, 0)
        model.pageInput = "  13  "
        let jumped = await model.submitPageInput()
        XCTAssertTrue(jumped); XCTAssertEqual(model.pageNumber, 13); XCTAssertEqual(model.pageInput, "13")
        XCTAssertNil(model.pageNavigationError); XCTAssertEqual(calls.value, 1)
        await model.shutdown()
        XCTAssertEqual(ReadingStateStore(url: model.reading.url).bookmark(for: "a").page, 13)
    }

    func testSearchAndOldViewCallbacksCannotOverwriteReadingAnchor() async throws {
        let model = model(), source = segment("Source")
        StubProtocol.respond = { _ in (0, 200, source) }
        model.tasks = [task(), task(id: "b")]
        await model.selectTask("a")
        model.recordParagraphPosition("s000000", taskID: "a")
        model.segments.append(Segment(segId: "s000001", text: "Other", translation: nil, context: "", translatable: true))
        model.paragraphQuery = "search"
        model.recordParagraphPosition("s000001", taskID: "a")
        XCTAssertEqual(model.paragraphAnchor, "s000000")
        await model.selectTask("b")
        model.recordParagraphPosition("s000000", taskID: "a")
        XCTAssertNil(model.paragraphAnchor)
        await model.selectTask("a")
        XCTAssertEqual(model.paragraphAnchor, "s000000")
        await model.shutdown()
    }

    func testLastArchivedDocumentRestoresAndMissingDocumentFallsBack() async {
        let model = model(), source = segment("Source")
        StubProtocol.respond = { _ in (0, 200, source) }
        var archived = task(id: "archived"); archived.archived = true
        model.tasks = [task(), archived]
        model.reading.select("archived")
        await model.restoreLastDocument()
        XCTAssertEqual(model.selectedTaskID, "archived")
        XCTAssertEqual(model.libraryFilter, .archived)
        model.reading.select("deleted")
        await model.restoreLastDocument()
        XCTAssertEqual(model.selectedTaskID, "a")
        XCTAssertEqual(model.libraryFilter, .all)
        await model.shutdown()
    }

    func savedTaskList() -> Data {
        json([["task_id": "a", "filename": "test.pdf", "ext": ".pdf", "status": "done", "warnings": [],
               "has_translated": true, "untranslated_count": 0, "content_version": 1]])
    }

    func testBulkSaveUsesOneRequestAndPreservesEditsMadeWhileSaving() async throws {
        let model = model(), patches = Counter(), ok = json(["ok": true]), tasks = savedTaskList(), source = segment("Source")
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        model.drafts.set("First draft", taskID: "a", segmentID: "s000000")
        model.drafts.set("Second draft", taskID: "a", segmentID: "s000001")
        StubProtocol.respond = { request in
            if request.httpMethod == "PATCH" {
                XCTAssertEqual(request.url?.path, "/api/tasks/a/segments")
                patches.increment(); return (0.05, 200, ok)
            }
            return (0, 200, request.url!.path == "/api/tasks" ? tasks : source)
        }
        let first = Task { await model.saveAllDrafts(taskID: "a") }
        try await Task.sleep(nanoseconds: 10_000_000)
        model.drafts.set("Newer text", taskID: "a", segmentID: "s000000")
        let duplicate = await model.saveAllDrafts(taskID: "a")
        let saved = await first.value
        XCTAssertTrue(saved); XCTAssertFalse(duplicate); XCTAssertEqual(patches.value, 1)
        let reopened = RevisionDraftStore(url: model.drafts.url)
        XCTAssertEqual(reopened.text(taskID: "a", segmentID: "s000000"), "Newer text")
        XCTAssertNil(reopened.text(taskID: "a", segmentID: "s000001"))
    }

    func testExportCancellationDoesNotSaveDraftsOrTouchDestination() async throws {
        let model = model(), calls = Counter(), ok = json(["ok": true])
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        model.drafts.set("Draft", taskID: "a", segmentID: "s000000")
        let destination = model.drafts.url.deletingLastPathComponent().appendingPathComponent("export.pdf")
        try Data("Keep original file".utf8).write(to: destination)
        StubProtocol.respond = { _ in calls.increment(); return (0, 200, ok) }
        let cancelled = await model.exportTranslation(decide: { _ in .cancel }, chooseDestination: { _ in destination })
        let cancelledPanel = await model.exportTranslation(decide: { _ in .saveAll }, chooseDestination: { _ in nil })
        XCTAssertFalse(cancelled); XCTAssertFalse(cancelledPanel)
        XCTAssertEqual(calls.value, 0)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "Keep original file")
        XCTAssertEqual(model.drafts.count(taskID: "a"), 1)
    }

    func testFailedBulkSaveStopsExportAndRetainsAllDrafts() async throws {
        let model = model(), failure = json(["detail": "disk full"]), downloads = Counter()
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        for id in ["s000000", "s000001"] { model.drafts.set("Draft", taskID: "a", segmentID: id) }
        let destination = model.drafts.url.deletingLastPathComponent().appendingPathComponent("export.pdf")
        try Data("Previous export".utf8).write(to: destination)
        StubProtocol.respond = { request in
            if request.url!.path.hasSuffix("/download") { downloads.increment() }
            return (0, 500, failure)
        }
        let exported = await model.exportTranslation(decide: { count in XCTAssertEqual(count, 2); return .saveAll }, chooseDestination: { _ in destination })
        XCTAssertFalse(exported); XCTAssertEqual(downloads.value, 0)
        XCTAssertEqual(RevisionDraftStore(url: model.drafts.url).count(taskID: "a"), 2)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "Previous export")
    }

    func testExportCanKeepSavedVersionOrApplyAllDrafts() async throws {
        let model = model(), patches = Counter(), ok = json(["ok": true]), tasks = savedTaskList(), source = segment("Source")
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        model.drafts.set("Draft", taskID: "a", segmentID: "s000000")
        let destination = model.drafts.url.deletingLastPathComponent().appendingPathComponent("export.pdf")
        StubProtocol.respond = { request in
            if request.httpMethod == "PATCH" { patches.increment(); return (0, 200, ok) }
            if request.url!.path.hasSuffix("/download") { return (0, 200, Data((patches.value == 0 ? "Saved version" : "With drafts").utf8)) }
            return (0, 200, request.url!.path == "/api/tasks" ? tasks : source)
        }
        let savedOnly = await model.exportTranslation(decide: { _ in .savedVersion }, chooseDestination: { _ in destination })
        XCTAssertTrue(savedOnly); XCTAssertEqual(patches.value, 0)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "Saved version")
        XCTAssertEqual(model.drafts.count(taskID: "a"), 1)
        let withDrafts = await model.exportTranslation(decide: { _ in .saveAll }, chooseDestination: { _ in destination })
        XCTAssertTrue(withDrafts); XCTAssertEqual(patches.value, 1)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "With drafts")
        XCTAssertEqual(model.drafts.count(taskID: "a"), 0)
    }

    func testOlderHistoryResponseCannotUndoSuccessfulRevision() async throws {
        let model = model(), lists = Counter(), ok = json(["ok": true]), source = segment("Source")
        model.tasks = [task("done")]; model.selectedTaskID = "a"
        let original: [String: Any] = ["task_id": "a", "filename": "test.pdf", "ext": ".pdf", "status": "done",
                                       "warnings": [], "has_translated": true, "untranslated_count": 0, "content_version": 0]
        var revised = original; revised["content_version"] = 1
        let old = json([original]), current = json([revised])
        StubProtocol.respond = { request in
            if request.url!.path == "/api/tasks" {
                lists.increment()
                return lists.value == 1 ? (0.10, 200, old) : (0, 200, current)
            }
            if request.httpMethod == "PATCH" { return (0.025, 200, ok) }
            return (0, 200, source)
        }
        let refresh = Task { try await model.refreshTasks() }
        try await Task.sleep(nanoseconds: 10_000_000)
        let segment = Segment(segId: "s000000", text: "Source", translation: "Saved", context: "", translatable: true)
        let saved = await model.revise(taskID: "a", segment: segment, text: "New revision")
        try await refresh.value
        XCTAssertTrue(saved)
        XCTAssertEqual(model.selectedTask?.contentVersion, 1)
    }

    func testRawSegmentIDAndCredentialHeader() async throws {
        let ok = json(["ok": true])
        StubProtocol.respond = { request in
            XCTAssertEqual(request.url?.path, "/api/tasks/a/segments/s000000#p1")
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-DocTranslator-Token"), "test-token")
            return (0, 200, ok)
        }
        let result: OKResponse = try await api().request("api/tasks/a/segments/s000000#p1", method: "PATCH", body: ["text": "new"])
        XCTAssertTrue(result.ok)
    }

    func testDraftsSurviveNavigationAndRelaunchIncludingEmptyDraft() async {
        let data = segment("source")
        StubProtocol.respond = { _ in (0, 200, data) }
        let model = model()
        model.drafts.set("保留修改", taskID: "first", segmentID: "s000000")
        model.drafts.set("", taskID: "second", segmentID: "s000000")
        await model.selectTask("first")
        await model.selectTask("second")
        let reopened = RevisionDraftStore(url: model.drafts.url)
        XCTAssertEqual(reopened.text(taskID: "first", segmentID: "s000000"), "保留修改")
        XCTAssertEqual(reopened.text(taskID: "second", segmentID: "s000000"), "")
        XCTAssertNil(reopened.saveError)
    }

    func testFailedSaveKeepsDurableDraftAndSuccessfulSaveRemovesIt() async {
        let model = model()
        let segment = Segment(segId: "s000000", text: "source", translation: "saved", context: "", translatable: true)
        model.drafts.set("修订草稿", taskID: "a", segmentID: segment.id)
        let failure = json(["detail": "disk full"])
        StubProtocol.respond = { _ in (0, 500, failure) }
        let failed = await model.revise(taskID: "a", segment: segment, text: "修订草稿")
        XCTAssertFalse(failed)
        XCTAssertEqual(RevisionDraftStore(url: model.drafts.url).text(taskID: "a", segmentID: segment.id), "修订草稿")
        let ok = json(["ok": true]), tasks = json([])
        StubProtocol.respond = { request in (0, 200, request.httpMethod == "PATCH" ? ok : tasks) }
        let saved = await model.revise(taskID: "a", segment: segment, text: "修订草稿")
        XCTAssertTrue(saved)
        XCTAssertNil(RevisionDraftStore(url: model.drafts.url).text(taskID: "a", segmentID: segment.id))
    }

    func testEarlierSaveDoesNotRemoveNewerDraft() async {
        let model = model()
        let segment = Segment(segId: "s000000", text: "source", translation: "saved", context: "", translatable: true)
        let ok = json(["ok": true]), tasks = json([])
        StubProtocol.respond = { request in (request.httpMethod == "PATCH" ? 0.06 : 0, 200, request.httpMethod == "PATCH" ? ok : tasks) }
        model.drafts.set("first draft", taskID: "a", segmentID: segment.id)
        let save = Task { await model.revise(taskID: "a", segment: segment, text: "first draft") }
        try? await Task.sleep(nanoseconds: 10_000_000)
        model.drafts.set("newer draft", taskID: "a", segmentID: segment.id)
        _ = await save.value
        XCTAssertEqual(model.drafts.text(taskID: "a", segmentID: segment.id), "newer draft")
    }

    func testDraftDiskFailureKeepsTextInMemory() throws {
        let model = model()
        try FileManager.default.createDirectory(at: model.drafts.url, withIntermediateDirectories: true)
        model.drafts.set("unsaved text", taskID: "a", segmentID: "s000000")
        XCTAssertEqual(model.drafts.text(taskID: "a", segmentID: "s000000"), "unsaved text")
        XCTAssertNotNil(model.drafts.saveError)
    }

    func testUnreadableDraftFileIsPreservedBeforeNewDraftsAreSaved() throws {
        let model = model(), url = model.drafts.url
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let original = Data("incomplete previous file".utf8)
        try original.write(to: url)
        let recovered = RevisionDraftStore(url: url)
        XCTAssertNotNil(recovered.saveError)
        recovered.set("new draft", taskID: "a", segmentID: "s000000")
        recovered.set("updated draft", taskID: "a", segmentID: "s000000")
        let backups = try FileManager.default.contentsOfDirectory(at: url.deletingLastPathComponent(), includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix("drafts-recovery-") }
        XCTAssertEqual(backups.count, 1)
        XCTAssertEqual(try Data(contentsOf: XCTUnwrap(backups.first)), original)
        XCTAssertEqual(RevisionDraftStore(url: url).text(taskID: "a", segmentID: "s000000"), "updated draft")
    }

    func testDefaultsSaveInOrderAndFinishBeforeShutdownWithoutStartingTranslation() async {
        let model = model(), requests = RequestLog(), ok = json(["ok": true])
        StubProtocol.respond = { request in
            XCTAssertEqual(request.url?.path, "/api/settings")
            requests.append(request)
            return (0.02, 200, ok)
        }
        model.ready = true
        model.selectedProviderID = "p"
        model.targetLanguage = "en"
        model.targetLanguage = "ja"
        await model.shutdown()
        XCTAssertEqual(requests.bodies.compactMap { $0["target_lang"] as? String }, ["zh-CN", "en", "ja"])
        XCTAssertEqual(requests.bodies.last?["translation_provider_id"] as? String, "p")
    }

    func testImportPreservesUserSelectionAndCannotBeErasedByOlderHistoryResponse() async throws {
        let model = model(), folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("imported.pdf")
        try Data("fake pdf for the upload stub".utf8).write(to: file)
        let uploaded = json(["task_id": "imported", "filename": "imported.pdf", "ext": ".pdf", "status": "pending_confirm",
                             "warnings": [], "has_translated": false, "untranslated_count": 1, "content_version": 0])
        let oldList = json([]), segments = segment("source")
        StubProtocol.respond = { request in
            if request.url!.path == "/api/upload" { return (0.04, 200, uploaded) }
            if request.url!.path == "/api/tasks" { return (0.10, 200, oldList) }
            return (0, 200, segments)
        }
        model.imports.setEnabled(true)
        let refresh = Task { try await model.refreshTasks() }
        model.importFiles([file])
        await model.selectTask("user-chosen")
        await model.imports.waitUntilIdle()
        try await refresh.value
        XCTAssertEqual(model.selectedTaskID, "user-chosen")
        XCTAssertEqual(model.tasks.map(\.id), ["imported"])
        XCTAssertEqual(model.imports.successfulCount, 1)
    }
}

final class RequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private var records: [[String: Any]] = []
    func append(_ request: URLRequest) {
        var body = request.httpBody ?? Data()
        if body.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(contentsOf: buffer.prefix(count))
            }
        }
        let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
        lock.lock(); records.append(object); lock.unlock()
    }
    var bodies: [[String: Any]] { lock.lock(); defer { lock.unlock() }; return records }
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func increment() { lock.lock(); defer { lock.unlock() }; count += 1 }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}
