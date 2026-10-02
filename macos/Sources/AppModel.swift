import AppKit
import Foundation
import UniformTypeIdentifiers

enum DraftExportChoice { case saveAll, savedVersion, cancel }

@MainActor
final class AppModel: ObservableObject {
    let backend: Backend
    let drafts: RevisionDraftStore
    let reading: ReadingStateStore
    let imports: DocumentImportQueue
    weak var providerEditor: ProviderEditor?
    @Published var ready = false
    @Published var booting = false
    @Published var closing = false
    @Published var busy = false
    @Published var busyLabel = ""
    @Published var errorMessage: String?
    @Published var startupError: String?
    @Published var providers: [Provider] = []
    @Published var tasks: [TranslationTask] = []
    @Published var libraryQuery = ""
    @Published var libraryFilter: LibraryFilter = .all
    @Published var paragraphQuery = ""
    @Published var paragraphFilter: ParagraphFilter = .all
    @Published var selectedTaskID: String?
    @Published var segments: [Segment] = []
    @Published var overflowSegmentIDs: Set<String> = []
    @Published var selectedProviderID = "" { didSet { defaultsChanged() } }
    @Published var targetLanguage = "zh-CN" { didSet { defaultsChanged() } }
    @Published var savingDraftIDs: Set<String> = []
    @Published var savingAllDraftTaskIDs: Set<String> = []
    @Published var startingIDs: Set<String> = []
    @Published var cancellingIDs: Set<String> = []
    @Published var archivingIDs: Set<String> = []
    @Published var segmentLoading = false
    @Published var mode = 0 { didSet { if mode != oldValue { readingModeChanged() } } }
    @Published var pageZoom = 1.0 { didSet { rememberReadingState() } }
    @Published var paragraphAnchor: String?
    @Published var renderStatus: RenderStatus?
    @Published var pageCount = 0
    @Published var originalPageCount = 0
    @Published var translatedPageCount = 0
    @Published var pageNumber = 1 { didSet { pageInput = String(pageNumber); pageNavigationError = nil; rememberReadingState() } }
    @Published var pageInput = "1"
    @Published var pageNavigationError: String?
    @Published var originalPage: NSImage?
    @Published var translatedPage: NSImage?
    @Published var pageLoading = false
    @Published var pageError: String?
    private var generation = 0
    private var pageGeneration = 0
    private var monitor: Task<Void, Never>?
    private var segmentRequest: Task<Void, Never>?
    private struct SegmentRequestContext: Equatable {
        let id: String
        let generation: Int
        let version: Int?
        let status: String?
    }
    private var segmentContext: SegmentRequestContext?
    private var segmentRequestID: UUID?
    private var pageRequest: Task<Void, Never>?
    private var renderRequest: Task<Void, Never>?
    private var defaultsSaveTask: Task<Void, Error>?
    private var suppressDefaultSave = false
    private var taskListGeneration = 0
    private var importSelectionGeneration: Int?
    private var restoringReadingState = false
    private var viewPreparation: Task<Void, Never>?

    init(backend: Backend? = nil, drafts: RevisionDraftStore? = nil, reading: ReadingStateStore? = nil) {
        let engine = backend ?? Backend()
        self.backend = engine
        self.drafts = drafts ?? RevisionDraftStore(url: engine.supportURL.appendingPathComponent("revision-drafts.json"))
        self.reading = reading ?? ReadingStateStore(url: self.drafts.url.deletingLastPathComponent().appendingPathComponent("reading-state.json"))
        self.imports = DocumentImportQueue { try await engine.api.upload($0) }
        self.imports.onImported = { [weak self] task in await self?.didImport(task) }
    }
    var selectedTask: TranslationTask? { tasks.first { $0.id == selectedTaskID } }

    func boot() async {
        guard !booting, !ready else { return }
        booting = true
        startupError = nil
        defer { booting = false }
        do {
            await backend.stop()
            try await backend.start()
            let list: ProviderList = try await backend.api.request("api/providers")
            providers = list.providers
            selectedProviderID = list.settings.translationProviderId ?? providers.first?.id ?? ""
            targetLanguage = list.settings.targetLang ?? "zh-CN"
            tasks = try await backend.api.request("api/tasks")
            ready = true
            await restoreLastDocument()
            if imports.isImporting { importSelectionGeneration = generation }
            imports.setEnabled(true)
            monitor?.cancel()
            monitor = Task { [weak self] in
                while !Task.isCancelled {
                    do { try await Task.sleep(nanoseconds: 1_500_000_000) } catch { return }
                    guard let self, self.ready else { return }
                    if !self.backend.isRunning {
                        self.ready = false
                        self.imports.setEnabled(false)
                        self.startupError = "翻译引擎已退出。重试后可从已保存的进度继续。"
                        return
                    }
                    do { try await self.refreshTasks() }
                    catch {
                        if !Task.isCancelled {
                            self.ready = false
                            self.imports.setEnabled(false)
                            self.startupError = "无法读取翻译引擎状态，请重试：\(error.localizedDescription)"
                        }
                        return
                    }
                }
            }
        } catch { startupError = error.localizedDescription }
    }

    func shutdown() async {
        rememberReadingState()
        reading.flush()
        closing = true
        ready = false
        monitor?.cancel()
        segmentRequest?.cancel()
        pageRequest?.cancel()
        renderRequest?.cancel()
        viewPreparation?.cancel()
        await imports.shutdown()
        try? await defaultsSaveTask?.value
        await backend.stop()
    }

    func prepareToQuit(decide: (() -> NSApplication.ModalResponse)? = nil) async -> Bool {
        guard let editor = providerEditor else { return true }
        editor.cancelTest()
        await editor.waitForSave()
        guard editor.hasUnsavedChanges else { return true }
        let alert = NSAlert()
        alert.messageText = "API 设置尚未保存"
        alert.informativeText = "退出前可以保存修改，或放弃这些修改。"
        alert.addButton(withTitle: "保存并退出")
        alert.addButton(withTitle: "放弃并退出")
        alert.addButton(withTitle: "取消")
        switch decide?() ?? alert.runModal() {
        case .alertFirstButtonReturn: return await editor.save()
        case .alertSecondButtonReturn: return true
        default: return false
        }
    }

    func refreshTasks() async throws {
        let previous = selectedTask
        let previousSelection = generation
        let expectedList = taskListGeneration
        let fresh: [TranslationTask] = try await backend.api.request("api/tasks")
        guard expectedList == taskListGeneration else { return }
        tasks = fresh
        cancellingIDs = cancellingIDs.filter { id in fresh.contains { $0.id == id && $0.status == "translating" } }
        guard previousSelection == generation, let current = selectedTask else { return }
        if current.status == "translating" || previous?.contentVersion != current.contentVersion || previous?.status != current.status {
            await refreshSegments(current.id, generation: generation)
            if mode == 1, previous?.contentVersion != current.contentVersion { await preparePages() }
        }
    }

    func selectTask(_ id: String?) async {
        rememberReadingState()
        reading.flush()
        generation += 1
        pageGeneration += 1
        segmentRequest?.cancel()
        pageRequest?.cancel()
        renderRequest?.cancel()
        viewPreparation?.cancel()
        restoringReadingState = true
        selectedTaskID = id
        paragraphQuery = ""
        paragraphFilter = .all
        segments = []
        overflowSegmentIDs = []
        let bookmark = id.map { reading.bookmark(for: $0) } ?? ReadingBookmark()
        mode = bookmark.mode
        pageZoom = bookmark.zoom
        paragraphAnchor = bookmark.paragraphID
        originalPage = nil
        translatedPage = nil
        renderStatus = nil
        pageCount = 0
        originalPageCount = 0
        translatedPageCount = 0
        pageNumber = bookmark.page
        pageError = nil
        segmentLoading = false
        pageLoading = false
        restoringReadingState = false
        reading.select(id)
        scheduleReadingView()
        if let id { await refreshSegments(id, generation: generation) }
    }

    func restoreLastDocument() async {
        let previous = reading.lastTaskID.flatMap { id in tasks.first { $0.id == id } }
        let task = previous ?? tasks.first { !$0.isArchived }
        libraryFilter = task?.isArchived == true ? .archived : .all
        await selectTask(task?.id)
    }

    private func readingModeChanged() {
        guard !restoringReadingState else { return }
        rememberReadingState()
        scheduleReadingView()
    }

    private func scheduleReadingView() {
        viewPreparation?.cancel()
        if mode != 1 {
            renderRequest?.cancel()
            pageRequest?.cancel()
            pageGeneration += 1
            pageLoading = false
            return
        }
        let expected = generation
        viewPreparation = Task { [weak self] in
            guard let self, self.generation == expected, self.mode == 1, !Task.isCancelled else { return }
            await self.preparePages()
        }
    }

    private func rememberReadingState() {
        guard !restoringReadingState, let task = selectedTask else { return }
        reading.update(ReadingBookmark(mode: mode, page: pageNumber, zoom: pageZoom, paragraphID: paragraphAnchor), for: task.id)
    }

    func recordParagraphPosition(_ id: String?, taskID: String) {
        guard taskID == selectedTaskID, mode == 0, paragraphQuery.isEmpty, paragraphFilter == .all,
              let id, segments.contains(where: { $0.id == id }), id != paragraphAnchor else { return }
        paragraphAnchor = id
        rememberReadingState()
    }

    @discardableResult
    func goToPage(_ number: Int) async -> Bool {
        guard pageCount > 0, (1...pageCount).contains(number) else {
            pageNavigationError = pageCount > 0 ? "请输入 1 至 \(pageCount) 的页码。" : "页面尚未就绪。"
            return false
        }
        pageNumber = number
        await loadPage()
        return true
    }

    @discardableResult
    func submitPageInput() async -> Bool {
        guard let number = Int(pageInput.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            pageNavigationError = "请输入 1 至 \(max(1, pageCount)) 的整数页码。"
            return false
        }
        return await goToPage(number)
    }

    @discardableResult
    func setArchived(_ task: TranslationTask, archived: Bool) async -> Bool {
        guard !closing, task.status != "translating", !startingIDs.contains(task.id), !archivingIDs.contains(task.id) else { return false }
        archivingIDs.insert(task.id)
        taskListGeneration += 1
        defer { archivingIDs.remove(task.id) }
        do {
            let updated: TranslationTask = try await backend.api.request("api/tasks/\(task.id)", method: "PATCH", body: ["archived": archived])
            taskListGeneration += 1
            if let index = tasks.firstIndex(where: { $0.id == updated.id }) { tasks[index] = updated }
            if selectedTaskID == task.id, !libraryFilter.includes(updated) {
                let next = DocumentSearch.tasks(tasks, query: libraryQuery, filter: libraryFilter).first?.id
                await selectTask(next)
            }
            return true
        } catch {
            errorMessage = "\(archived ? "归档" : "恢复")失败：\(error.localizedDescription)"
            return false
        }
    }

    private func refreshSegments(_ id: String, generation expected: Int) async {
        let context = SegmentRequestContext(id: id, generation: expected, version: selectedTask?.contentVersion, status: selectedTask?.status)
        if segmentContext == context, segmentRequest != nil {
            await waitForSegments(id, generation: expected)
            return
        }
        segmentRequest?.cancel()
        segmentLoading = segments.isEmpty
        let requestID = UUID()
        segmentContext = context
        segmentRequestID = requestID
        let request = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.segmentRequestID == requestID {
                    self.segmentLoading = false
                    self.segmentRequest = nil
                    self.segmentContext = nil
                    self.segmentRequestID = nil
                }
            }
            do {
                let result: SegmentList = try await self.backend.api.request("api/tasks/\(id)/segments")
                guard !Task.isCancelled, expected == self.generation, self.selectedTaskID == id else { return }
                self.segments = result.segments.filter(\.translatable)
                self.overflowSegmentIDs = Set(result.overflow ?? [])
            } catch {
                if !Task.isCancelled, expected == self.generation { self.errorMessage = error.localizedDescription }
            }
        }
        segmentRequest = request
        await waitForSegments(id, generation: expected)
    }

    private func waitForSegments(_ id: String, generation expected: Int) async {
        // A newer content version may replace an in-flight request. Selection
        // finishes only after the current request, while navigation can cancel it.
        while generation == expected, selectedTaskID == id, let pending = segmentRequest {
            await pending.value
        }
    }

    func importFiles(_ urls: [URL]) {
        guard !closing else { return }
        if !imports.isImporting { importSelectionGeneration = generation }
        imports.enqueue(urls)
    }

    private func didImport(_ task: TranslationTask) async {
        taskListGeneration += 1
        tasks.removeAll { $0.id == task.id }
        tasks.insert(task, at: 0)
        if selectedTaskID == nil || importSelectionGeneration == generation {
            importSelectionGeneration = nil
            libraryQuery = ""
            libraryFilter = .all
            await selectTask(task.id)
        }
    }

    func saveDefaults() async throws {
        try await enqueueDefaultsSave().value
    }

    private func defaultsChanged() {
        guard ready, !closing, !suppressDefaultSave else { return }
        let pending = enqueueDefaultsSave()
        Task { [weak self] in
            do { try await pending.value }
            catch { self?.errorMessage = "默认设置保存失败：\(error.localizedDescription)" }
        }
    }

    private func enqueueDefaultsSave() -> Task<Void, Error> {
        let previous = defaultsSaveTask
        let provider = selectedProviderID, language = targetLanguage, api = backend.api
        let request = Task {
            // Keep rapid picker changes in order, including a change just before quit.
            _ = try? await previous?.value
            let _: OKResponse = try await api.request("api/settings", method: "PUT", body: [
                "translation_provider_id": provider, "target_lang": language
            ], timeout: 5)
        }
        defaultsSaveTask = request
        return request
    }

    func startTranslation() async {
        guard let task = selectedTask, !task.isArchived, !hasDraftSaveInFlight(task.id), !archivingIDs.contains(task.id), !startingIDs.contains(task.id), task.status != "translating" else { return }
        let provider = task.providerId ?? selectedProviderID
        guard !provider.isEmpty else { return }
        startingIDs.insert(task.id)
        taskListGeneration += 1
        defer { startingIDs.remove(task.id) }
        do {
            try await saveDefaults()
            let _: OKResponse = try await backend.api.request("api/tasks/\(task.id)/start", method: "POST", body: [
                "provider_id": provider, "source_lang": task.sourceLang ?? "auto", "target_lang": task.targetLang ?? targetLanguage
            ])
            taskListGeneration += 1
            try await refreshTasks()
        } catch { errorMessage = error.localizedDescription }
    }

    func cancelTranslation() async {
        guard let id = selectedTaskID, !cancellingIDs.contains(id) else { return }
        cancellingIDs.insert(id)
        do {
            let _: OKResponse = try await backend.api.request("api/tasks/\(id)/cancel", method: "POST")
            try await refreshTasks()
        } catch { errorMessage = error.localizedDescription }
        // Keep the button disabled while the current batch finishes.
        if tasks.first(where: { $0.id == id })?.status != "translating" { cancellingIDs.remove(id) }
    }

    func revise(taskID: String, segment: Segment, text: String) async -> Bool {
        let key = taskID + ":" + segment.id
        guard !savingDraftIDs.contains(key), !savingAllDraftTaskIDs.contains(taskID) else { return false }
        savingDraftIDs.insert(key)
        taskListGeneration += 1
        defer { savingDraftIDs.remove(key) }
        do {
            // appendingPathComponent escapes the raw segment ID, including a possible '#'.
            let _: OKResponse = try await backend.api.request("api/tasks/\(taskID)/segments/\(segment.id)", method: "PATCH", body: ["text": text])
        } catch { errorMessage = error.localizedDescription; return false }
        taskListGeneration += 1
        drafts.remove(taskID: taskID, segmentID: segment.id, matching: text)
        do {
            try await refreshTasks()
            if selectedTaskID == taskID { await refreshSegments(taskID, generation: generation) }
        } catch { errorMessage = "译文已保存，但刷新失败：\(error.localizedDescription)" }
        return true
    }

    func hasDraftSaveInFlight(_ taskID: String) -> Bool {
        savingAllDraftTaskIDs.contains(taskID) || savingDraftIDs.contains { $0.hasPrefix(taskID + ":") }
    }

    @discardableResult
    func saveAllDrafts(taskID: String) async -> Bool {
        guard !hasDraftSaveInFlight(taskID), !startingIDs.contains(taskID), tasks.first(where: { $0.id == taskID })?.status != "translating" else { return false }
        let snapshot = drafts.drafts[taskID] ?? [:]
        guard !snapshot.isEmpty else { return true }
        savingAllDraftTaskIDs.insert(taskID)
        taskListGeneration += 1
        defer { savingAllDraftTaskIDs.remove(taskID) }
        do {
            let _: OKResponse = try await backend.api.request("api/tasks/\(taskID)/segments", method: "PATCH", body: ["revisions": snapshot])
        } catch {
            errorMessage = "草稿保存失败，修改已保留：\(error.localizedDescription)"
            return false
        }
        taskListGeneration += 1
        drafts.remove(taskID: taskID, matching: snapshot)
        do {
            try await refreshTasks()
            if selectedTaskID == taskID { await refreshSegments(taskID, generation: generation) }
        } catch { errorMessage = "草稿已保存，但刷新失败：\(error.localizedDescription)" }
        return true
    }

    func saveProvider(name: String, baseURL: String, model: String, apiKey: String, existing: Provider?) async throws -> ProviderSaveResult {
        let path = existing.map { "api/providers/\($0.id)" } ?? "api/providers"
        let result: ProviderResponse = try await backend.api.request(path, method: existing == nil ? "POST" : "PUT", body: [
            "name": name.trimmingCharacters(in: .whitespacesAndNewlines),
            "base_url": baseURL.trimmingCharacters(in: .whitespacesAndNewlines),
            "model": model.trimmingCharacters(in: .whitespacesAndNewlines), "api_key": apiKey
        ])
        // The provider has committed. Keep its ID even if updating defaults fails.
        if let index = providers.firstIndex(where: { $0.id == result.provider.id }) { providers[index] = result.provider }
        else { providers.append(result.provider) }
        suppressDefaultSave = true
        selectedProviderID = result.provider.id
        suppressDefaultSave = false
        do {
            try await saveDefaults()
            return ProviderSaveResult(provider: result.provider, warning: nil)
        } catch {
            return ProviderSaveResult(provider: result.provider, warning: "配置已保存；默认配置保存失败：\(error.localizedDescription)")
        }
    }

    func testProvider(_ provider: Provider) async -> String {
        do {
            let result: TestResponse = try await backend.api.request("api/providers/\(provider.id)/test", method: "POST")
            return result.ok ? "连接成功" : (result.error ?? "连接失败")
        } catch { return error.localizedDescription }
    }

    @discardableResult
    func exportTranslation(decide: ((Int) -> DraftExportChoice)? = nil, chooseDestination: ((TranslationTask) -> URL?)? = nil) async -> Bool {
        guard let task = selectedTask, task.hasTranslated, task.status != "translating", !busy, !hasDraftSaveInFlight(task.id) else { return false }
        busy = true
        busyLabel = "正在导出译文…"
        defer { busy = false }
        let count = drafts.count(taskID: task.id)
        var choice: DraftExportChoice = .savedVersion
        if count > 0 {
            if let decide { choice = decide(count) }
            else {
                let alert = NSAlert()
                alert.messageText = "有 \(count) 处修订草稿尚未保存到译文"
                alert.informativeText = "选择本次导出使用的内容。仅导出已保存版本会保留草稿，供之后继续修改。"
                alert.addButton(withTitle: "保存全部并导出")
                alert.addButton(withTitle: "仅导出已保存版本")
                alert.addButton(withTitle: "取消")
                switch alert.runModal() {
                case .alertFirstButtonReturn: choice = .saveAll
                case .alertSecondButtonReturn: choice = .savedVersion
                default: choice = .cancel
                }
            }
        }
        guard choice != .cancel else { return false }
        let destination: URL?
        if let chooseDestination { destination = chooseDestination(task) }
        else {
            let panel = NSSavePanel()
            panel.nameFieldStringValue = "\(URL(fileURLWithPath: task.filename).deletingPathExtension().lastPathComponent).\(task.targetLang ?? targetLanguage)\(task.ext)"
            if let type = UTType(filenameExtension: String(task.ext.dropFirst())) { panel.allowedContentTypes = [type] }
            panel.allowsOtherFileTypes = false
            destination = panel.runModal() == .OK ? panel.url : nil
        }
        guard let destination else { return false }
        if choice == .saveAll {
            busyLabel = "正在保存草稿并导出…"
            guard await saveAllDrafts(taskID: task.id) else { return false }
        }
        do {
            let bytes = try await backend.api.data("api/tasks/\(task.id)/download")
            try await Task.detached { try bytes.write(to: destination, options: .atomic) }.value
            return true
        } catch { errorMessage = "导出失败：\(error.localizedDescription)"; return false }
    }

    func preparePages() async {
        guard let task = selectedTask else { return }
        renderRequest?.cancel()
        pageRequest?.cancel()
        pageGeneration += 1
        pageLoading = false
        let expected = generation
        pageError = nil
        originalPage = nil
        translatedPage = nil
        renderStatus = nil
        let request = Task { [weak self] in
            guard let self else { return }
            do {
                if task.ext == ".pdf" {
                    let info: FileInfo = try await self.backend.api.request("api/tasks/\(task.id)/info")
                    guard !Task.isCancelled, expected == self.generation else { return }
                    self.applyPageCounts(original: info.originalPages ?? 0, translated: info.translatedPages ?? 0)
                    self.renderStatus = RenderStatus(status: "ready", pages: self.pageCount, error: nil)
                    await self.loadPage()
                } else {
                    var status: RenderStatus = try await self.backend.api.request("api/tasks/\(task.id)/preview/render", method: "POST")
                    while !Task.isCancelled, expected == self.generation {
                        self.renderStatus = status
                        if status.status == "ready" {
                            self.applyPageCounts(original: status.originalPages ?? status.pages, translated: status.translatedPages ?? status.pages)
                            await self.loadPage()
                            return
                        }
                        if ["failed", "unavailable"].contains(status.status) { return }
                        try await Task.sleep(nanoseconds: 1_000_000_000)
                        status = try await self.backend.api.request("api/tasks/\(task.id)/preview/render")
                    }
                }
            } catch {
                if !Task.isCancelled, expected == self.generation { self.pageError = error.localizedDescription }
            }
        }
        renderRequest = request
        await request.value
    }

    private func applyPageCounts(original: Int, translated: Int) {
        originalPageCount = original
        translatedPageCount = translated
        pageCount = max(original, translated)
        pageNumber = min(max(1, pageNumber), max(1, pageCount))
    }

    func loadPage() async {
        guard let task = selectedTask, (1...max(1, pageCount)).contains(pageNumber) else { return }
        pageRequest?.cancel()
        pageGeneration += 1
        let expected = pageGeneration
        let documentGeneration = generation
        let number = pageNumber
        let originalCount = originalPageCount
        let translatedCount = translatedPageCount
        pageLoading = true
        originalPage = nil
        translatedPage = nil
        pageError = nil
        let request = Task { [weak self] in
            guard let self else { return }
            defer { if expected == self.pageGeneration { self.pageLoading = false } }
            do {
                let path = task.ext == ".pdf" ? "api/tasks/\(task.id)/preview/pdf/\(number)" : "api/tasks/\(task.id)/preview/render/page/\(number)"
                let query = [URLQueryItem(name: "variant", value: "original")]
                let original = number <= originalCount ? try await self.backend.api.data(path, query: query) : nil
                let translated = number <= translatedCount ? try await self.backend.api.data(path, query: [URLQueryItem(name: "variant", value: "translated")]) : nil
                guard !Task.isCancelled, expected == self.pageGeneration, documentGeneration == self.generation else { return }
                let originalImage = original.flatMap { NSImage(data: $0) }
                let translatedImage = translated.flatMap { NSImage(data: $0) }
                guard (original == nil || originalImage != nil), (translated == nil || translatedImage != nil) else {
                    throw APIError(message: "页面图像未完整载入，请重试。")
                }
                self.originalPage = originalImage
                self.translatedPage = translatedImage
            } catch {
                if !Task.isCancelled, expected == self.pageGeneration, documentGeneration == self.generation { self.pageError = error.localizedDescription }
            }
        }
        pageRequest = request
        await request.value
    }
}

struct OKResponse: Decodable { let ok: Bool }
struct TestResponse: Decodable { let ok: Bool; let error: String? }
