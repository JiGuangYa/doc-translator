import XCTest
import AppKit
@testable import DocTranslatorMac

final class StubProtocol: URLProtocol, @unchecked Sendable {
    static var respond: (URLRequest) -> (Double, Int, Data) = { _ in (0, 200, Data("{}".utf8)) }
    private let lock = NSLock()
    private var stopped = false
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (delay, status, data) = Self.respond(request)
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [self] in
            lock.lock(); defer { lock.unlock() }; guard !stopped else { return }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() { lock.lock(); stopped = true; lock.unlock() }
}
final class Count: @unchecked Sendable {
    private let lock = NSLock(); private var count = 0
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
    func add() { lock.lock(); count += 1; lock.unlock() }
}

@MainActor
final class AppModelTests: XCTestCase {
    func json(_ object: Any) -> Data { try! JSONSerialization.data(withJSONObject: object) }
    func api() -> APIClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        return APIClient(baseURL: URL(string: "http://127.0.0.1:12345")!, configuration: config, token: "test-token")
    }
    func state() throws -> AppState {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let state = AppState(api: api(), storageDirectory: directory); state.phase = .ready
        return state
    }
    func task(_ id: String = "a", revision: Int = 0, archived: Bool = false) -> TranslationTask {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(TranslationTask.self, from: json(taskJSON(id, revision: revision, archived: archived)))
    }
    func taskJSON(_ id: String = "a", revision: Int = 0, archived: Bool = false) -> [String: Any] {
        ["task_id": id, "filename": "test.pdf", "ext": ".pdf", "status": "done", "has_translated": true, "untranslated_count": 0, "revision": revision, "archived": archived]
    }
    func segments(_ text: String = "Source") -> Data {
        json(["segments": [["seg_id": "s000000", "text": text, "translation": "Saved", "context": "", "translatable": true]], "total": 1, "revision": 0])
    }
    func provider() -> Provider {
        Provider(id: "p", name: "Fixture", baseUrl: "https://example.invalid/v1", model: "old-model", hasApiKey: true, inputPricePerMillion: nil, outputPricePerMillion: nil)
    }
    func testDraftsRelaunchEmptyTextVersionAndBaselineReset() throws {
        let state = try state()
        state.drafts.set("Text", taskID: "a", segmentID: "first", revision: 7)
        state.drafts.set("", taskID: "b", segmentID: "second", revision: 4)
        let reopened = RevisionDraftStore(url: state.drafts.url)
        XCTAssertEqual(reopened.text(taskID: "a", segmentID: "first"), "Text")
        XCTAssertEqual(reopened.text(taskID: "b", segmentID: "second"), "")
        XCTAssertEqual(reopened.revisions["a"], 7)
        reopened.remove(taskID: "a", segmentID: "first")
        reopened.set("New", taskID: "a", segmentID: "first", revision: 9)
        XCTAssertEqual(reopened.revisions["a"], 9)
    }
    func testLegacyDraftCompatibilityAndCorruptRecovery() throws {
        let state = try state(), url = state.drafts.url
        try json(["a": ["first": "legacy"]]).write(to: url)
        let old = RevisionDraftStore(url: url)
        XCTAssertEqual(old.text(taskID: "a", segmentID: "first"), "legacy")
        let corrupt = Data("unfinished file".utf8); try corrupt.write(to: url)
        let recovered = RevisionDraftStore(url: url)
        XCTAssertNotNil(recovered.saveError)
        recovered.set("new", taskID: "b", segmentID: "second", revision: 0)
        let copies = try FileManager.default.contentsOfDirectory(at: url.deletingLastPathComponent(), includingPropertiesForKeys: nil).filter { $0.lastPathComponent.hasPrefix("drafts-recovery-") }
        XCTAssertEqual(copies.count, 1); XCTAssertEqual(try Data(contentsOf: copies[0]), corrupt)
    }
    func testDraftDiskFailureKeepsTextAndRetrySucceeds() throws {
        let state = try state(), url = state.drafts.url
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        state.drafts.set("unsaved", taskID: "a", segmentID: "s", revision: 1)
        XCTAssertNotNil(state.drafts.saveError); XCTAssertEqual(state.drafts.text(taskID: "a", segmentID: "s"), "unsaved")
        try FileManager.default.removeItem(at: url); state.drafts.persist()
        XCTAssertNil(state.drafts.saveError)
    }
    func testBulkSavePreservesConcurrentEditAndAdvancesItsVersion() async throws {
        let state = try state(), patches = Count(), ok = json(["ok": true, "revision": 1])
        state.tasks = [task()]; state.selectedTaskID = "a"
        state.drafts.set("first", taskID: "a", segmentID: "s000000", revision: 0)
        state.drafts.set("second", taskID: "a", segmentID: "s000001", revision: 0)
        let list = json([taskJSON(revision: 1)]), page = segments()
        StubProtocol.respond = { request in
            if request.httpMethod == "PATCH" { patches.add(); return (0.05, 200, ok) }
            return (0, 200, request.url!.path == "/api/tasks" ? list : page)
        }
        let save = Task { await state.saveAllDrafts(taskID: "a") }
        try await Task.sleep(nanoseconds: 10_000_000)
        state.drafts.set("newer", taskID: "a", segmentID: "s000000", revision: 0)
        let duplicate = await state.saveAllDrafts(taskID: "a")
        let saved = await save.value
        XCTAssertTrue(saved); XCTAssertFalse(duplicate); XCTAssertEqual(patches.value, 1)
        let reopened = RevisionDraftStore(url: state.drafts.url)
        XCTAssertEqual(reopened.text(taskID: "a", segmentID: "s000000"), "newer")
        XCTAssertNil(reopened.text(taskID: "a", segmentID: "s000001")); XCTAssertEqual(reopened.revisions["a"], 1)
        state.stop()
    }
    func testFailedBulkSaveAndExportKeepsEveryDraftAndDestination() async throws {
        let state = try state(), calls = Count(), failure = json(["detail": ["code": "revision_conflict", "message": "stale"]])
        state.tasks = [task()]; state.selectedTaskID = "a"
        for id in ["s000000", "s000001"] { state.drafts.set("draft", taskID: "a", segmentID: id, revision: 0) }
        let file = state.drafts.url.deletingLastPathComponent().appendingPathComponent("export.pdf")
        try Data("original file".utf8).write(to: file)
        StubProtocol.respond = { request in if request.httpMethod != "PATCH" { calls.add() }; return (0, 409, failure) }
        let result = await state.exportTranslation(decide: { _ in .saveAll }, destination: file)
        XCTAssertFalse(result); XCTAssertEqual(calls.value, 0)
        XCTAssertEqual(RevisionDraftStore(url: state.drafts.url).count(taskID: "a"), 2)
        XCTAssertEqual(try String(contentsOf: file), "original file")
    }
    func testCancellingExportBeforeDestinationNeverSaves() async throws {
        let state = try state(), calls = Count(), ok = json(["ok": true])
        state.tasks = [task()]; state.selectedTaskID = "a"
        state.drafts.set("draft", taskID: "a", segmentID: "s000000", revision: 0)
        StubProtocol.respond = { _ in calls.add(); return (0, 200, ok) }
        let cancelled = await state.exportTranslation(decide: { _ in .cancel })
        let cancelledPanel = await state.exportTranslation(decide: { _ in .saveAll }, chooseDestination: { nil })
        XCTAssertFalse(cancelled); XCTAssertFalse(cancelledPanel); XCTAssertEqual(calls.value, 0)
        XCTAssertEqual(state.drafts.count(taskID: "a"), 1)
    }
    func testExportSavedVersionAndApplyAll() async throws {
        let state = try state(), patches = Count(), ok = json(["ok": true, "revision": 1]), page = segments(), list = json([taskJSON(revision: 1)])
        state.tasks = [task()]; state.selectedTaskID = "a"
        state.drafts.set("draft", taskID: "a", segmentID: "s000000", revision: 0)
        let file = state.drafts.url.deletingLastPathComponent().appendingPathComponent("out.pdf")
        StubProtocol.respond = { request in
            if request.httpMethod == "PATCH" { patches.add(); return (0, 200, ok) }
            if request.url!.path.hasSuffix("/download") { return (0, 200, Data((patches.value == 0 ? "saved" : "revised").utf8)) }
            return (0, 200, request.url!.path == "/api/tasks" ? list : page)
        }
        let saved = await state.exportTranslation(decide: { _ in .savedVersion }, destination: file)
        XCTAssertTrue(saved); XCTAssertEqual(try String(contentsOf: file), "saved"); XCTAssertEqual(state.drafts.count(taskID: "a"), 1)
        let revised = await state.exportTranslation(decide: { _ in .saveAll }, destination: file)
        XCTAssertTrue(revised); XCTAssertEqual(try String(contentsOf: file), "revised"); XCTAssertEqual(state.drafts.count(taskID: "a"), 0)
        state.stop()
    }
    func testSelectionAndSearchCannotReplaceNormalBookmark() async throws {
        let state = try state(), page = segments()
        StubProtocol.respond = { _ in (0, 200, page) }
        state.tasks = [task(), task("b")]
        await state.selectTask("a"); state.recordParagraph("s000500", taskID: "a")
        state.setSegmentFilter(search: "search"); state.recordParagraph("s000900", taskID: "a")
        XCTAssertEqual(state.reading.bookmark(for: "a").paragraphID, "s000500")
        state.setSegmentFilter(search: "")
        XCTAssertEqual(state.paragraphAnchor, "s000500")
        await state.selectTask("b"); state.recordParagraph("old callback", taskID: "a")
        XCTAssertNil(state.reading.bookmark(for: "b").paragraphID)
        await state.selectTask("a"); XCTAssertEqual(state.paragraphAnchor, "s000500")
        state.stop()
    }
    func testStaleHistoryCannotUndoArchiveAndArchiveDeduplicates() async throws {
        let state = try state(), count = Count(), old = json([taskJSON()]), archived = json(taskJSON(archived: true))
        state.tasks = [task()]; state.selectedTaskID = "a"
        state.drafts.set("retain", taskID: "a", segmentID: "s", revision: 0)
        StubProtocol.respond = { request in
            if request.httpMethod == "PATCH" { count.add(); return (0.02, 200, archived) }
            return (0.10, 200, old)
        }
        let refresh = Task { await state.refreshTasks() }; await Task.yield()
        async let first = state.setArchived(task()); async let second = state.setArchived(task())
        let results = await [first, second]; await refresh.value
        XCTAssertEqual(results.filter { $0 }.count, 1); XCTAssertEqual(count.value, 1)
        XCTAssertTrue(state.tasks[0].isArchived); XCTAssertEqual(state.drafts.count(taskID: "a"), 1)
    }
    func testProviderDraftParametersSavedKeyAndStaleResult() async throws {
        let state = try state(), editor = ProviderEditor(state: state)
        editor.load(provider()); editor.model = "new-model"
        let ok = json(["ok": true, "latency_ms": 3])
        StubProtocol.respond = { request in
            XCTAssertEqual(request.url?.path, "/api/providers/test")
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-DocTranslator-Token"), "test-token")
            return (0.10, 200, ok)
        }
        editor.test(); XCTAssertTrue(editor.isTesting)
        await Task.yield(); editor.cancel(); editor.model = "latest-model"
        try await Task.sleep(nanoseconds: 140_000_000)
        XCTAssertFalse(editor.isTesting); XCTAssertNil(editor.feedback); XCTAssertEqual(editor.model, "latest-model")
        XCTAssertTrue(state.settingsDirty)
    }
    func testProviderFailedSaveAndUnsavedCloseChoice() async throws {
        let state = try state(), editor = ProviderEditor(state: state), failure = json(["detail": "disk full"])
        editor.load(provider()); editor.key = "fake-new-key"
        state.saveSettingsBeforeClose = { await editor.save() }
        StubProtocol.respond = { _ in (0, 500, failure) }
        let cancelled = await state.confirmSettingsTransition(decide: { .alertThirdButtonReturn })
        let failed = await state.confirmSettingsTransition(decide: { .alertFirstButtonReturn })
        XCTAssertFalse(cancelled); XCTAssertFalse(failed); XCTAssertEqual(editor.key, "fake-new-key")
        let discarded = await state.confirmSettingsTransition(decide: { .alertSecondButtonReturn })
        XCTAssertTrue(discarded)
    }
    func testProviderSaveRetainsIDAndDoesNotDuplicate() async throws {
        let state = try state(), editor = ProviderEditor(state: state), posts = Count()
        editor.name = "Fixture"; editor.model = "m"; editor.baseURL = "https://example.invalid/v1"
        let saved = json(["ok": true, "provider": ["id": "saved", "name": "Fixture", "base_url": "https://example.invalid/v1", "model": "m", "has_api_key": true]])
        StubProtocol.respond = { request in if request.httpMethod == "POST" { posts.add() }; return (0.02, 200, saved) }
        async let one = editor.save(); async let two = editor.save(); let result = await [one, two]
        XCTAssertEqual(result.filter { $0 }.count, 1); XCTAssertEqual(posts.value, 1)
        XCTAssertEqual(editor.selectedID, "saved"); XCTAssertFalse(editor.dirty)
        editor.key = "replacement"; _ = await editor.save(); XCTAssertEqual(posts.value, 1)
    }
}
