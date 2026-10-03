import Foundation
import AppKit
import Vision

// Exercises the real API client and AppState with an isolated in-memory backend.
// No saved credentials, notification permissions or UI automation are involved.
final class StubBackend: URLProtocol {
    static let lock = NSLock()
    static var tasks: [[String: Any]] = []
    static var uploadAttempts = 0
    static var activeUploads = 0
    static var maximumUploads = 0
    static var rejectNextUpload = false
    static var failSegments = false
    static var segmentsErrorStatus = 503
    static var segmentHandler: ((URLRequest) -> [String: Any])?
    static var segmentDelay: TimeInterval = 0
    static var pageImageData: Data?
    static var requests: [URLRequest] = []
    private var stopped = false

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {
        Self.lock.lock(); stopped = true; Self.lock.unlock()
    }

    override func startLoading() {
        let isUpload = request.url!.path == "/api/upload"
        Self.lock.lock()
        Self.requests.append(request)
        let delay = request.url!.path.hasSuffix("/segments") ? Self.segmentDelay :
            (request.url!.path.contains("/preview/pdf/") ? 0.1 : 0)
        Self.lock.unlock()
        if isUpload {
            Self.lock.lock()
            Self.uploadAttempts += 1
            Self.activeUploads += 1
            Self.maximumUploads = max(Self.maximumUploads, Self.activeUploads)
            Self.lock.unlock()
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + (isUpload ? 0.04 : delay)) {
            Self.lock.lock()
            if self.stopped { Self.lock.unlock(); return }
            let path = self.request.url!.path
            var status = 200
            var object: Any = ["ok": true]
            switch path {
            case "/api/providers": object = ["providers": [], "settings": [:]]
            case "/api/glossary": object = ["entries": []]
            case "/api/memory": object = ["count": 0]
            case "/api/tasks": object = Self.tasks
            case "/download/error": status = 503; object = ["detail": "Export unavailable"]
            case "/api/upload":
                Self.activeUploads -= 1
                if Self.rejectNextUpload {
                    Self.rejectNextUpload = false
                    status = 401
                    object = ["detail": "Session expired"]
                } else {
                    let id = String(format: "%032d", Self.tasks.count + 1)
                    let task: [String: Any] = ["task_id": id, "filename": "sample.docx",
                        "ext": ".docx", "status": "pending_confirm", "has_translated": false,
                        "segment_count": 0, "revision": 0]
                    Self.tasks.append(task)
                    object = task
                }
            default:
                if path.hasSuffix("/preview/render") {
                    object = ["status": "ready", "pages": 3, "original_pages": 1, "translated_pages": 3]
                } else if path.hasSuffix("/segments") {
                    if Self.failSegments {
                        status = Self.segmentsErrorStatus
                        object = ["detail": "Engine unavailable"]
                    } else {
                        object = Self.segmentHandler?(self.request) ?? ["segments": [], "total": 0, "revision": 0]
                    }
                }
            }
            let bytes = path.contains("/preview/pdf/") ? Self.pageImageData : nil
            Self.lock.unlock()
            let response = HTTPURLResponse(url: self.request.url!, statusCode: status,
                                           httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: bytes ?? (try! JSONSerialization.data(withJSONObject: object)))
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }
}

@main
struct NativeWorkflowTests {
    @MainActor static func main() async throws {
        if CommandLine.arguments.dropFirst().first == "--ocr-json" {
            let source = URL(fileURLWithPath: CommandLine.arguments[2])
            let pages = CommandLine.arguments.dropFirst(3).compactMap(Int.init)
            let lines = try await Task.detached { try OCRService.recognize(source, pages: pages.isEmpty ? nil : pages) }.value
            let data = try JSONSerialization.data(withJSONObject: lines.map(\.json))
            print(String(decoding: data, as: UTF8.self))
            return
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubBackend.self]
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:19761")!, configuration: configuration)
        let state = AppState(api: api)
        state.configured = true
        state.useSystemUnlock = false
        state.phase = .authentication
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory); state.stop() }
        let files = try (1...3).map { number -> URL in
            let file = directory.appendingPathComponent("sample-\(number).docx")
            try Data("test document \(number)".utf8).write(to: file)
            return file
        }

        await state.importFiles(files)
        precondition(state.pendingImportCount == 3 && StubBackend.uploadAttempts == 0,
                     "Locked imports must all wait for authentication")
        await state.authenticate(password: "test-only-password")
        precondition(state.phase == .ready && state.pendingImportCount == 0)
        precondition(StubBackend.tasks.count == 3 && !state.busy)
        print("PASS: three locked imports drain after login")

        let first = Task { await state.importFiles(Array(files.prefix(2))) }
        try await Task.sleep(nanoseconds: 10_000_000)
        precondition(state.busy)
        await state.importFiles([files[0], files[2]])
        await first.value
        precondition(StubBackend.tasks.count == 6 && StubBackend.maximumUploads == 1,
                     "Concurrent drops must serialize and avoid duplicate in-flight files")
        precondition(!state.busy)
        print("PASS: concurrent imports stay serialized and busy until drained")

        StubBackend.rejectNextUpload = true
        await state.importFiles(Array(files.prefix(2)))
        precondition(state.phase == .authentication && state.pendingImportCount == 2)
        await state.authenticate(password: "test-only-password")
        precondition(StubBackend.tasks.count == 8 && state.pendingImportCount == 0)
        print("PASS: expired session preserves both pending imports")

        state.alertText = nil
        StubBackend.failSegments = true
        await state.loadSegments()
        precondition(state.alertText == nil && state.connectionNotice != nil)
        print("PASS: background segment errors use a recoverable notice")

        let failedCount = StubBackend.requests.filter { $0.url!.path.hasSuffix("/segments") }.count
        StubBackend.failSegments = false
        await state.refreshTasks()
        precondition(StubBackend.requests.filter { $0.url!.path.hasSuffix("/segments") }.count > failedCount)
        precondition(state.connectionNotice == nil)
        print("PASS: unchanged task summaries still retry a failed paragraph page")
        StubBackend.failSegments = true

        let exported = directory.appendingPathComponent("export.docx")
        try Data("previous export".utf8).write(to: exported)
        try await api.download("/download/success", to: exported)
        let saved = try Data(contentsOf: exported)
        let exportedJSON = try JSONSerialization.jsonObject(with: saved) as? [String: Bool]
        precondition(exportedJSON?["ok"] == true)
        do {
            try await api.download("/download/error", to: exported)
            preconditionFailure("A failed export must not replace the destination")
        } catch let error as APIError {
            precondition(error.statusCode == 503)
        }
        let afterFailure = try Data(contentsOf: exported)
        precondition(afterFailure == saved)
        print("PASS: streamed export replaces an existing file and preserves it on HTTP failure")

        StubBackend.segmentsErrorStatus = 401
        await state.loadSegments()
        precondition(state.phase == .authentication && state.connectionNotice == nil)
        print("PASS: expired background session returns to authentication")

        StubBackend.failSegments = false
        StubBackend.tasks[0]["has_translated"] = true
        state.phase = .ready
        await state.refreshTasks()
        await state.selectTask(StubBackend.tasks[0]["task_id"] as! String)
        await state.preparePages()
        precondition(state.pagePairs.count == 3)
        precondition(state.pagePairs[0].hasOriginal && !state.pagePairs[1].hasOriginal)
        precondition(state.pagePairs.allSatisfy(\.hasTranslated))
        print("PASS: translated overflow pages remain visible without nonexistent original requests")
        state.showPage(3)
        let firstToken = state.pageNavigation?.token
        state.showPage(3)
        precondition(state.pageNavigation?.page == 3 && state.pageNavigation?.token != firstToken)
        print("PASS: page navigation works repeatedly while already in page preview")

        await testSegmentPaging(api: api)

        try await testBackendLifecycle(directory: directory)
        try await testLocalOCR()
    }

    @MainActor static func testSegmentPaging(api: APIClient) async {
        let state = AppState(api: api)
        state.phase = .ready
        state.selectedTaskID = "paging"
        var total = 10000
        StubBackend.segmentHandler = { request in
            let query = Dictionary(uniqueKeysWithValues: URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
                .queryItems!.map { ($0.name, $0.value ?? "") })
            let offset = Int(query["offset"]!)!, limit = Int(query["limit"]!)!
            return ["segments": (offset..<max(offset, min(total, offset + limit))).map {
                ["seg_id": "s\($0)", "text": query["q"]!.isEmpty ? "Sentence \($0)" : query["q"]!,
                 "context": request.url!.path, "translatable": true] as [String: Any]
            }, "total": total, "revision": 12, "sheets": ["First", "Late sheet"]]
        }
        let before = StubBackend.requests.count
        await state.loadSegments()
        precondition(state.segments.count == 100 && state.segmentTotal == 10000)
        precondition(StubBackend.requests.count == before + 1, "Initial browsing must fetch only one page")
        await state.goToSegmentPage(100)
        precondition(state.segments.first?.id == "s9900" && state.segments.count == 100)
        precondition(state.segmentSheets == ["First", "Late sheet"])
        print("PASS: large documents load one bounded page and retain all worksheet names")

        state.setSegmentFilter(search: "A+B & 中文", onlyUntranslated: true, onlyReview: true, sheet: "Late sheet")
        await state.goToSegmentPage(1)
        let request = StubBackend.requests.last!
        precondition(request.url!.absoluteString.contains("%2B"), "Literal plus must not become a space")
        let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
        precondition(items.contains(URLQueryItem(name: "filter", value: "untranslated")))
        precondition(items.contains(URLQueryItem(name: "review_only", value: "true")))
        precondition(items.contains(URLQueryItem(name: "sheet", value: "Late sheet")))
        precondition(state.segments.first?.text == "A+B & 中文" && state.segmentOffset == 0)
        print("PASS: full-document search, review and sheet filters use encoded server queries")

        total = 230
        await state.loadSegments()
        await state.goToSegmentPage(3)
        precondition(state.segmentOffset == 200 && state.segments.count == 30)
        total = 30
        await state.loadSegments()
        precondition(state.segmentOffset == 0 && state.segmentTotal == 30 && state.segments.count == 30)
        print("PASS: shrinking result sets return to the last valid page")

        StubBackend.segmentDelay = 0.15
        state.selectedTaskID = "old"
        let previous = Task { await state.loadSegments() }
        try? await Task.sleep(nanoseconds: 10_000_000)
        StubBackend.segmentDelay = 0
        await state.selectTask("new")
        await previous.value
        precondition(state.segments.allSatisfy { $0.context == "/api/tasks/new/segments" })
        precondition(state.segmentFilter == SegmentFilter() && state.segmentOffset == 0)
        precondition(state.connectionNotice == nil && state.alertText == nil)
        print("PASS: switching tasks cancels stale pages and resets filters without an error alert")

        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 2, pixelsHigh: 2, bitsPerSample: 8,
                                      samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                      colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        StubBackend.pageImageData = bitmap.representation(using: .png, properties: [:])!
        let late = Task { await state.pageImage(taskId: "new", ext: ".pdf", page: 1, variant: "original") }
        try? await Task.sleep(nanoseconds: 10_000_000)
        await state.selectTask("other")
        let lateImage = await late.value
        precondition(lateImage == nil, "A late image must not populate another document's cache")
        await state.selectTask("new")
        let fresh = await state.pageImage(taskId: "new", ext: ".pdf", page: 1, variant: "original")
        precondition(fresh != nil)
        let count = StubBackend.requests.count
        let cached = await state.pageImage(taskId: "new", ext: ".pdf", page: 1, variant: "original")
        precondition(cached != nil && StubBackend.requests.count == count)
        print("PASS: late preview images are discarded; current pages use the bounded cache")
        state.stop()
        StubBackend.segmentHandler = nil
        StubBackend.pageImageData = nil
    }

    static func testBackendLifecycle(directory: URL) async throws {
        let python = URL(fileURLWithPath: ProcessInfo.processInfo.environment["NATIVE_TEST_PYTHON"] ?? "/usr/bin/python3")
        let server = #"""
import os, signal
from http.server import HTTPServer, BaseHTTPRequestHandler
signal.signal(signal.SIGTERM, signal.SIG_IGN)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(os.environ['DOC_TRANSLATOR_PORT'])), Handler).serve_forever()
"""#
        let ready = try BackendServer(dataDirectory: directory.appendingPathComponent("ready"),
            executable: python, arguments: ["-c", server], startupTimeout: 15, shutdownTimeout: 0.3)
        do { _ = try await ready.start() }
        catch {
            let log = (try? String(contentsOf: directory.appendingPathComponent("ready/backend.log"))) ?? "No startup log"
            throw AppFailure.message("\(error.localizedDescription)\n\(log)")
        }
        precondition(ready.isRunning)
        let stopping = ProcessInfo.processInfo.systemUptime
        await ready.stopAndWait()
        precondition(!ready.isRunning && ProcessInfo.processInfo.systemUptime - stopping < 2)
        print("PASS: a healthy engine that ignores termination is stopped within its deadline")

        let stalledCode = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
        let stalled = try BackendServer(dataDirectory: directory.appendingPathComponent("stalled"),
            executable: python, arguments: ["-c", stalledCode], startupTimeout: 0.5, shutdownTimeout: 0.2)
        let starting = ProcessInfo.processInfo.systemUptime
        do {
            _ = try await stalled.start()
            preconditionFailure("A stalled startup must fail")
        } catch { }
        precondition(!stalled.isRunning && ProcessInfo.processInfo.systemUptime - starting < 3)
        print("PASS: stalled startup has a bounded wait and leaves no running engine")

        let cancelled = try BackendServer(dataDirectory: directory.appendingPathComponent("cancelled"),
            executable: python, arguments: ["-c", stalledCode], startupTimeout: 10, shutdownTimeout: 0.2)
        let startup = Task { try await cancelled.start() }
        try await Task.sleep(nanoseconds: 100_000_000)
        startup.cancel()
        do { _ = try await startup.value; preconditionFailure("Startup cancellation must propagate") }
        catch is CancellationError { }
        precondition(!cancelled.isRunning)
        print("PASS: cancelled startup cleans up its child process")
    }

    static func testLocalOCR() async throws {
        let source = URL(fileURLWithPath: ProcessInfo.processInfo.environment["NATIVE_TEST_OCR_FIXTURE"]!)
        let lines = try await Task.detached { try OCRService.recognize(source, pages: [2, 4]) }.value
        for (page, title) in [(2, "SCANNED PAGE TWO"), (4, "SCANNED PAGE FOUR")] {
            let pageLines = lines.filter { $0.page == page }
            precondition(pageLines.contains { $0.text.uppercased().contains(title) }, "Vision missed the selected scan page")
            precondition(pageLines.allSatisfy { line in
                line.bbox.count == 4 && line.bbox.allSatisfy { $0 >= 0 && $0 <= 1 } &&
                line.bbox[2] > line.bbox[0] && line.bbox[3] > line.bbox[1] &&
                line.confidence >= 0 && line.confidence <= 1
            })
            let selected = pageLines.first { $0.text.uppercased().contains(title) }!
            let image = NSImage(contentsOf: source.deletingLastPathComponent().appendingPathComponent("ocr-page-\(page).png"))!
            let crop = OCRService.previewCrop(image, bbox: selected.bbox)!
            var bounds = NSRect.zero
            let pixels = crop.cgImage(forProposedRect: &bounds, context: nil, hints: nil)!
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            try VNImageRequestHandler(cgImage: pixels).perform([request])
            let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
            precondition(text.uppercased().contains(title), "OCR crop does not correspond to the source pixels")
        }
        precondition(Set(lines.map(\.page)) == Set([2, 4]))
        print("PASS: Apple Vision recognizes only requested pages, including rotation and crop boxes")
        print("PASS: OCR correction crops match the recognized source text")

        let chinese = source.deletingLastPathComponent().appendingPathComponent("ocr-chinese.pdf")
        if FileManager.default.fileExists(atPath: chinese.path) {
            let recognized = try await Task.detached { try OCRService.recognize(chinese) }.value
            let text = recognized.map(\.text).joined().replacingOccurrences(of: " ", with: "")
            precondition(text.contains("扫描文字校对"), "Local Chinese OCR did not recognize the fixture")
            print("PASS: Apple Vision recognizes the Chinese scan fixture locally")
        }
        let interrupted = Task.detached { try OCRService.recognize(source, pages: [2, 4]) }
        interrupted.cancel()
        do { _ = try await interrupted.value; preconditionFailure("OCR cancellation must propagate") }
        catch is CancellationError { }
        print("PASS: cancelled local OCR does not publish incomplete page results")
    }
}
