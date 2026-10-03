import AppKit
import Foundation
import Combine
import SwiftUI
import UniformTypeIdentifiers

struct AuthStatus: Decodable {
    let configured: Bool
    let authenticated: Bool
}

struct ProviderList: Decodable {
    let providers: [Provider]
    let settings: AppSettings
}

struct AppSettings: Decodable {
    let taskRetentionDays: Int?
    let translationProviderId: String?
    let sourceLang: String?
    let targetLang: String?
    let useTranslationMemory: Bool?
}

struct GlossaryResponse: Decodable { let entries: [GlossaryEntryDTO] }
struct GlossaryEntryDTO: Decodable { let source: String; let target: String }
struct GlossaryTerm: Identifiable {
    let id = UUID()
    var source: String
    var target: String
}
struct MemorySummary: Decodable { let count: Int }
struct ModelListResponse: Decodable {
    let ok: Bool
    let models: [String]?
    let error: String?
}

struct Provider: Decodable, Identifiable, Hashable {
    let id: String
    let name: String
    let baseUrl: String
    let model: String
    let hasApiKey: Bool
    let inputPricePerMillion: Double?
    let outputPricePerMillion: Double?
}

struct TranslationTask: Decodable, Identifiable, Hashable {
    let taskId: String
    let filename: String
    let ext: String
    let status: String
    let segmentCount: Int?
    let doneSegments: Int?
    let totalSegments: Int?
    let hasTranslated: Bool
    let sourceLang: String?
    let targetLang: String?
    let providerId: String?
    let untranslatedCount: Int?
    let overflowCount: Int?
    let revision: Int?
    let ocrScanned: Bool?
    let ocrPages: Int?
    let ocrRequiredPages: [Int]?
    let ocrCompletedPages: [Int]?
    let ocrEmptyPages: [Int]?
    let ocrDismissedPages: [Int]?
    let ocrOptionalPages: [Int]?
    let ocrReviewCount: Int?
    let usage: TokenUsage?
    let tokenBudget: Int?
    let estimatedCostUsd: Double?
    let externalEdit: Bool?
    let error: String?
    let warnings: [String]?
    let archived: Bool?
    let providerSnapshot: ProviderSnapshot?
    let requiresSnapshotConfirmation: Bool?
    let historyUsageUnknown: Bool?
    let migrationIssue: String?
    var isArchived: Bool { archived ?? false }
    var id: String { taskId }
    var progress: Double {
        guard let total = totalSegments, total > 0 else { return 0 }
        return min(1, Double(doneSegments ?? 0) / Double(total))
    }
    var statusLabel: String {
        switch status {
        case "ocr_pending": return "待完成本机识别"
        case "pending_confirm": return "待翻译"
        case "queued": return "排队中"
        case "translating": return "翻译中"
        case "paused": return "已暂停，可续翻"
        case "done": return "已完成"
        case "failed": return "失败"
        case "cancelled": return "已取消"
        default: return status
        }
    }
    var emptyOCRPagesToReview: [Int] {
        (ocrEmptyPages ?? []).filter { !(ocrDismissedPages ?? []).contains($0) }
    }
}

struct ProviderSnapshot: Decodable, Hashable {
    let name: String?
    let baseUrl: String?
    let model: String?
}
struct RevisionResponse: Decodable { let ok: Bool; let revision: Int? }
struct LibraryImportResponse: Decodable {
    let imported: Int; let skipped: Int; let issues: [ImportIssue]; let credentialRequired: [String]
}
struct ImportIssue: Decodable { let taskId: String; let message: String }
enum DraftExportChoice { case saveAll, savedVersion, cancel }

struct TokenUsage: Decodable, Hashable {
    let promptTokens: Int
    let completionTokens: Int
    var total: Int { promptTokens + completionTokens }
}

struct SegmentList: Decodable {
    let segments: [Segment]
    let overflow: [String]?
    let warnings: [String]?
    let total: Int?
    let revision: Int?
    let sheets: [String]?
    let offset: Int?
}

struct SegmentFilter: Hashable {
    var search = ""
    var onlyUntranslated = false
    var onlyReview = false
    var sheet = ""
    var category = "all"
}

struct SegmentPageIdentity: Hashable {
    let taskID: String?
    let filter: SegmentFilter
    let offset: Int
}

struct Segment: Decodable, Identifiable, Hashable {
    let segId: String
    let text: String
    let translation: String?
    let context: String
    let translatable: Bool
    let meta: SegmentMeta?
    var id: String { segId }
    var needsTranslation: Bool {
        translatable && (translation == nil || translation!.isEmpty ||
                         translation!.hasPrefix("⟪ untranslated:"))
    }
}

struct SegmentMeta: Decodable, Hashable {
    let page: Int?
    let bbox: [Double]?
    let normalizedBbox: [Double]?
    let confidence: Double?
    let ocr: Bool?
    let reviewed: Bool?
    let needsReview: Bool?
    let translationContext: String?
}

struct TrashList: Decodable { let tasks: [TrashedTask] }
struct TrashedTask: Decodable, Identifiable {
    let taskId: String
    let filename: String
    let deletedAt: Double?
    var id: String { taskId }
}

struct RenderStatus: Decodable {
    let status: String
    let pages: Int
    let error: String?
    let originalPages: Int?
    let translatedPages: Int?
}

struct PDFPages: Decodable { let pages: Int }
struct OKResponse: Decodable { let ok: Bool }
struct SettingsSaveResponse: Decodable { let ok: Bool; let settings: AppSettings }
struct ProviderSaveResponse: Decodable { let ok: Bool; let provider: Provider }
struct ProviderTestResponse: Decodable {
    let ok: Bool
    let error: String?
    let latencyMs: Int?
    let normalizedBaseUrl: String?
}

struct PagePair: Identifiable {
    let number: Int
    let hasOriginal: Bool
    let hasTranslated: Bool
    var id: Int { number }

    init(number: Int, hasOriginal: Bool = true, hasTranslated: Bool = true) {
        self.number = number
        self.hasOriginal = hasOriginal
        self.hasTranslated = hasTranslated
    }
}

struct PageNavigation {
    let token = UUID()
    let taskID: String
    let page: Int
}

@MainActor
final class AppState: ObservableObject {
    enum Phase { case launching, authentication, ready, failed, closing }
    enum PreviewMode: String, CaseIterable {
        case text = "逐段校对", pages = "页面对照", table = "单元格对照"
    }

    let drafts: RevisionDraftStore
    let reading: ReadingStateStore
    private var storeObservers: Set<AnyCancellable> = []
    @Published var libraryFilter = "全部"
    @Published var paragraphAnchor: String?
    @Published var settingsDirty = false
    var saveSettingsBeforeClose: (() async -> Bool)?
    @Published var phase: Phase = .launching
    @Published var configured = false
    @Published var useSystemUnlock = true
    @Published private var activeOperations = 0
    var busy: Bool { activeOperations > 0 }
    @Published var alertText: String?
    @Published var tasks: [TranslationTask] = []
    @Published var providers: [Provider] = []
    @Published var selectedTaskID: String? = UserDefaults.standard.string(
        forKey: "DocTranslatorSelectedTask")
    @Published var segments: [Segment] = []
    @Published var segmentRevision: Int?
    @Published var segmentOverflow: [String] = []
    @Published private(set) var segmentFilter = SegmentFilter()
    @Published private(set) var segmentOffset = 0
    @Published private(set) var segmentTotal = 0
    @Published private(set) var segmentSheets: [String] = []
    @Published private(set) var isLoadingSegments = false
    let segmentPageSize = 100
    var segmentPage: Int { segmentOffset / segmentPageSize + 1 }
    var segmentPageCount: Int { max(1, (segmentTotal + segmentPageSize - 1) / segmentPageSize) }
    var segmentPageIdentity: SegmentPageIdentity {
        SegmentPageIdentity(taskID: selectedTaskID, filter: segmentFilter, offset: segmentOffset)
    }
    @Published var trashItems: [TrashedTask] = []
    @Published var taskRetentionDays = 0
    @Published var useTranslationMemory = true
    @Published var glossary: [GlossaryTerm] = []
    @Published var memoryCount = 0
    @Published var modelSuggestions: [String] = []
    @Published var connectionNotice: String?
    @Published var previewMode: PreviewMode = .text
    @Published var pagePairs: [PagePair] = []
    @Published var pageMessage = "选择“页面对照”以载入预览"
    @Published var pageNavigation: PageNavigation?
    @Published var targetLanguage = "zh-CN"
    @Published var sourceLanguage = "auto"
    @Published var tokenBudget = 0
    @Published var selectedProviderID = ""
    @Published var showingSettings = false
    @Published var showingTrash = false
    @Published var showingConflict = false
    @Published var editingSegment: Segment?
    @Published var editingOCRSegment: Segment?
    @Published var ocrStatus: String?
    @Published private(set) var ocrTaskID: String?
    @Published private(set) var ocrProgress = 0.0
    @Published var importStatus: String?
    @Published var pendingImportCount = 0

    private var backend: BackendServer?
    private var launching = false
    private var api: APIClient?
    private var pollTask: Task<Void, Never>?
    private var lastTaskStatus: String?
    private var lastRevision: Int?
    private var loadingPagesFor: String?
    private var pageLoadToken = UUID()
    private var pendingImportURLs: [URL] = []
    private var importingURL: URL?
    private var isImporting = false
    private var ocrWork: Task<[OCRLine], Error>?
    private var ocrCancelled = false
    private var lastDoneSegments: Int?
    private var segmentLoadToken = UUID()
    private var segmentRequest: Task<SegmentList, Error>?
    private var segmentFilterTask: Task<Void, Never>?
    private var segmentLoadFailed = false
    private var historyGeneration = UUID()
    private var archiving = Set<String>()
    private var pageCacheGeneration = UUID()
    private let pageCache: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.totalCostLimit = 80 * 1024 * 1024
        cache.countLimit = 8
        return cache
    }()

    var selectedTask: TranslationTask? { tasks.first { $0.taskId == selectedTaskID } }
    var dataDirectory: URL? { backend?.dataDirectory }

    init(api: APIClient? = nil, storageDirectory: URL? = nil) {
        self.api = api
        let directory = storageDirectory ?? (api == nil ? BackendServer.defaultDataDirectory() :
            FileManager.default.temporaryDirectory.appendingPathComponent("DocTranslatorState-" + UUID().uuidString))
        drafts = RevisionDraftStore(url: directory.appendingPathComponent("revision-drafts.json"))
        reading = ReadingStateStore(url: directory.appendingPathComponent("reading-state.json"))
        if let last = reading.lastTaskID { selectedTaskID = last }
        drafts.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &storeObservers)
        reading.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &storeObservers)
    }

    func recordParagraph(_ id: String?, taskID: String) {
        guard selectedTaskID == taskID, segmentFilter == SegmentFilter(), !isLoadingSegments else { return }
        paragraphAnchor = id
        var bookmark = reading.bookmark(for: taskID); bookmark.paragraphID = id
        reading.update(bookmark, for: taskID)
    }

    func recordPages(taskID: String, page: Int, zoom: Double) {
        guard selectedTaskID == taskID else { return }
        var bookmark = reading.bookmark(for: taskID); bookmark.page = page; bookmark.zoom = zoom
        reading.update(bookmark, for: taskID)
    }

    func recordVisiblePage(taskID: String, page: Int) {
        guard selectedTaskID == taskID else { return }
        var bookmark = reading.bookmark(for: taskID); bookmark.continuousPage = page
        reading.update(bookmark, for: taskID)
    }

    func recordMode() {
        guard let id = selectedTaskID else { return }
        var bookmark = reading.bookmark(for: id)
        bookmark.mode = previewMode == .pages ? 1 : previewMode == .table ? 2 : 0
        reading.update(bookmark, for: id)
    }

    func libraryIncludes(_ task: TranslationTask) -> Bool {
        if libraryFilter == "已归档" { return task.isArchived }
        guard !task.isArchived else { return false }
        switch libraryFilter {
        case "待翻译": return ["pending_confirm", "ocr_pending"].contains(task.status)
        case "翻译中": return ["translating", "queued"].contains(task.status)
        case "待处理": return ["paused", "failed", "cancelled"].contains(task.status) ||
            (task.status == "done" && (task.untranslatedCount ?? 0) > 0) || task.migrationIssue != nil || (task.overflowCount ?? 0) > 0
        case "已完成": return task.status == "done" && (task.untranslatedCount ?? 0) == 0 && (task.overflowCount ?? 0) == 0 && task.migrationIssue == nil
        default: return true
        }
    }

    @discardableResult
    func setArchived(_ task: TranslationTask) async -> Bool {
        guard let api, !archiving.contains(task.id) else { return false }
        archiving.insert(task.id); defer { archiving.remove(task.id) }
        do {
            let result: TranslationTask = try await api.json("/api/tasks/\(task.id)", method: "PATCH", body: ["archived": !task.isArchived])
            historyGeneration = UUID(); updateTask(result)
            libraryFilter = result.isArchived ? "已归档" : "全部"
            return true
        } catch { alertText = error.localizedDescription; return false }
    }


    func launch() async {
        guard api == nil, !launching else { return }
        launching = true
        defer { launching = false }
        do {
            let server = try BackendServer()
            backend = server
            let baseURL = try await server.start()
            let client = APIClient(baseURL: baseURL, token: server.authToken)
            api = client
            let status: AuthStatus = try await client.json("/api/auth/status")
            configured = status.configured
            if status.authenticated {
                phase = .ready
                await reloadAll()
                beginPolling()
                await importPendingFiles()
            } else {
                if status.configured, await unlockWithSystem() { return }
                phase = .authentication
            }
        } catch {
            backend?.stop()
            api = nil
            phase = .failed
            alertText = error.localizedDescription
        }
    }

    func authenticate(password: String) async {
        guard let api, !busy else { return }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let path = configured ? "/api/auth/login" : "/api/auth/setup"
            let _: OKResponse = try await api.json(path, method: "POST", body: ["password": password])
            configured = true
            if useSystemUnlock && !SystemUnlock.save(password: password) {
                alertText = "系统解锁未能启用；仍可使用本机密码登录。"
            }
            phase = .ready
            await reloadAll()
            beginPolling()
            await importPendingFiles()
        } catch { alertText = error.localizedDescription }
    }

    @discardableResult
    func unlockWithSystem() async -> Bool {
        guard let api, SystemUnlock.isEnabled else { return false }
        guard let password = await Task.detached(priority: .userInitiated,
                                                 operation: { SystemUnlock.retrieve() }).value
        else { return false }
        do {
            let _: OKResponse = try await api.json(
                "/api/auth/login", method: "POST", body: ["password": password])
            phase = .ready
            await reloadAll()
            beginPolling()
            await importPendingFiles()
            return true
        } catch { return false }
    }

    func lock() async {
        cancelOCR()
        invalidateSegments(clear: true)
        segmentFilterTask?.cancel()
        clearPageCache()
        pollTask?.cancel()
        if let api {
            let _: OKResponse? = try? await api.json("/api/auth/logout", method: "POST")
        }
        phase = .authentication
        segments = []
        pagePairs = []
    }

    func restartBackend() async {
        guard !busy else { return }
        pollTask?.cancel()
        phase = .launching
        await backend?.stopAndWait()
        api = nil
        backend = nil
        connectionNotice = nil
        await launch()
    }

    func reloadAll() async {
        await loadProviders()
        await loadGlossary()
        await loadMemoryCount()
        await refreshTasks()
    }

    private func handleConnectionError(_ error: Error) {
        if let apiError = error as? APIError, apiError.statusCode == 401 {
            invalidateSegments(clear: true)
            segmentFilterTask?.cancel()
            clearPageCache()
            pollTask?.cancel()
            phase = .authentication
            segments = []
            pagePairs = []
            connectionNotice = nil
        } else if connectionNotice == nil {
            connectionNotice = error.localizedDescription
        }
    }

    func loadProviders() async {
        guard let api else { return }
        do {
            let result: ProviderList = try await api.json("/api/providers")
            providers = result.providers
            taskRetentionDays = result.settings.taskRetentionDays ?? 0
            useTranslationMemory = result.settings.useTranslationMemory ?? true
            if !providers.contains(where: { $0.id == selectedProviderID }) {
                selectedProviderID = result.settings.translationProviderId ?? providers.first?.id ?? ""
            }
        } catch { handleConnectionError(error) }
    }

    func saveProvider(name: String, baseURL: String, model: String, key: String,
                      inputPrice: Double?, outputPrice: Double?) async -> Bool {
        guard let api else { return false }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let result: ProviderSaveResponse = try await api.json(
                "/api/providers", method: "POST", body: providerPayload(
                    name: name, baseURL: baseURL, model: model, key: key,
                    inputPrice: inputPrice, outputPrice: outputPrice))
            await loadProviders()
            selectedProviderID = result.provider.id
            return true
        } catch {
            alertText = error.localizedDescription
            return false
        }
    }

    func testProvider(_ id: String) async {
        guard let api else { return }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let result: ProviderTestResponse = try await api.json("/api/providers/\(id)/test", method: "POST")
            alertText = result.ok ? "模型连接成功" : (result.error ?? "模型连接失败")
        } catch { alertText = error.localizedDescription }
    }

    func updateProvider(id: String, name: String, baseURL: String,
                        model: String, key: String,
                        inputPrice: Double?, outputPrice: Double?) async -> Bool {
        guard let api else { return false }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let _: ProviderSaveResponse = try await api.json(
                "/api/providers/\(id)", method: "PUT", body: providerPayload(
                    name: name, baseURL: baseURL, model: model, key: key,
                    inputPrice: inputPrice, outputPrice: outputPrice))
            await loadProviders()
            return true
        } catch {
            alertText = error.localizedDescription
            return false
        }
    }

    private func providerPayload(name: String, baseURL: String, model: String,
                                 key: String, inputPrice: Double?,
                                 outputPrice: Double?) -> [String: Any] {
        ["name": name.trimmingCharacters(in: .whitespacesAndNewlines),
         "base_url": baseURL.trimmingCharacters(in: .whitespacesAndNewlines),
         "model": model.trimmingCharacters(in: .whitespacesAndNewlines),
         "api_key": key,
         "input_price_per_million": inputPrice.map { $0 as Any } ?? NSNull(),
         "output_price_per_million": outputPrice.map { $0 as Any } ?? NSNull()]
    }

    func deleteProvider(_ id: String) async {
        guard let api else { return }
        do {
            let _: OKResponse = try await api.json("/api/providers/\(id)", method: "DELETE")
            await loadProviders()
        } catch { alertText = error.localizedDescription }
    }

    func saveRetention(_ days: Int) async {
        guard let api else { return }
        do {
            let _: SettingsSaveResponse = try await api.json(
                "/api/settings", method: "PUT", body: ["task_retention_days": days])
            taskRetentionDays = days
        } catch { alertText = error.localizedDescription }
    }

    func setTranslationMemoryEnabled(_ enabled: Bool) async {
        guard let api else { return }
        do {
            let _: SettingsSaveResponse = try await api.json(
                "/api/settings", method: "PUT", body: ["use_translation_memory": enabled])
            useTranslationMemory = enabled
        } catch { alertText = error.localizedDescription }
    }

    func loadGlossary() async {
        guard let api else { return }
        do {
            let result: GlossaryResponse = try await api.json("/api/glossary")
            glossary = result.entries.map { GlossaryTerm(source: $0.source, target: $0.target) }
        } catch { handleConnectionError(error) }
    }

    func saveGlossary() async -> Bool {
        guard let api else { return false }
        let entries = glossary.filter { !$0.source.isEmpty && !$0.target.isEmpty }
            .map { ["source": $0.source, "target": $0.target] }
        do {
            let _: GlossaryResponse = try await api.json(
                "/api/glossary", method: "PUT", body: ["entries": entries])
            await loadGlossary()
            return true
        } catch {
            alertText = error.localizedDescription
            return false
        }
    }

    func loadMemoryCount() async {
        guard let api else { return }
        if let result: MemorySummary = try? await api.json("/api/memory") {
            memoryCount = result.count
        }
    }

    func clearTranslationMemory() async {
        guard let api else { return }
        do {
            let _: OKResponse = try await api.json("/api/memory", method: "DELETE")
            memoryCount = 0
        } catch { alertText = error.localizedDescription }
    }

    func saveProviderConfiguration(id: String?, name: String, baseURL: String, model: String,
                                   key: String, inputPrice: Double?, outputPrice: Double?) async -> Provider? {
        guard let api else { return nil }
        do {
            let result: ProviderSaveResponse = try await api.json(id.map { "/api/providers/\($0)" } ?? "/api/providers",
                method: id == nil ? "POST" : "PUT", body: providerPayload(name: name, baseURL: baseURL,
                model: model, key: key, inputPrice: inputPrice, outputPrice: outputPrice))
            providers.removeAll { $0.id == result.provider.id }; providers.append(result.provider)
            selectedProviderID = result.provider.id
            return result.provider
        } catch { alertText = error.localizedDescription; return nil }
    }

    func discoverDraftModels(baseURL: String, key: String, providerID: String?) async -> ModelListResponse {
        guard let api else { return ModelListResponse(ok: false, models: nil, error: "引擎未连接") }
        do {
            return try await api.json("/api/providers/models", method: "POST", body: [
                "base_url": baseURL.trimmingCharacters(in: .whitespacesAndNewlines), "api_key": key,
                "provider_id": providerID.map { $0 as Any } ?? NSNull()])
        } catch { return ModelListResponse(ok: false, models: nil, error: error.localizedDescription) }
    }

    func testDraftProvider(baseURL: String, model: String, key: String, providerID: String? = nil) async -> ProviderTestResponse {
        guard let api else {
            return ProviderTestResponse(ok: false, error: "翻译引擎尚未启动",
                                        latencyMs: nil, normalizedBaseUrl: nil)
        }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            return try await api.json("/api/providers/test", method: "POST", body: [
                "base_url": baseURL.trimmingCharacters(in: .whitespacesAndNewlines),
                "model": model.trimmingCharacters(in: .whitespacesAndNewlines),
                "api_key": key, "provider_id": providerID.map { $0 as Any } ?? NSNull()])
        } catch {
            return ProviderTestResponse(ok: false, error: error.localizedDescription,
                                        latencyMs: nil, normalizedBaseUrl: nil)
        }
    }

    func discoverModels(baseURL: String, key: String, savedProviderID: String?) async {
        guard let api else { return }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let result: ModelListResponse
            if let savedProviderID, key.isEmpty {
                result = try await api.json("/api/providers/\(savedProviderID)/models")
            } else {
                result = try await api.json("/api/providers/models", method: "POST",
                    body: ["base_url": baseURL.trimmingCharacters(in: .whitespacesAndNewlines),
                           "api_key": key])
            }
            if result.ok {
                modelSuggestions = result.models ?? []
                if modelSuggestions.isEmpty { alertText = "接口未返回模型列表，可手动填写模型名。" }
            } else {
                alertText = result.error ?? "模型列表不可用，请手动填写模型名。"
            }
        } catch { alertText = error.localizedDescription }
    }

    func chooseFile() async {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = ["docx", "pptx", "xlsx", "pdf"].compactMap {
            UTType(filenameExtension: $0)
        }
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK else { return }
        await importFiles(panel.urls)
    }

    func importFile(_ url: URL) async {
        await importFiles([url])
    }

    func importFiles(_ urls: [URL]) async {
        for url in urls {
            guard url.isFileURL, ["docx", "pptx", "xlsx", "pdf"].contains(url.pathExtension.lowercased()) else {
                alertText = "请选择 DOCX、PPTX、XLSX 或 PDF 文件。"
                continue
            }
            let normalized = url.standardizedFileURL
            if !pendingImportURLs.contains(normalized), importingURL != normalized {
                pendingImportURLs.append(normalized)
            }
        }
        pendingImportCount = pendingImportURLs.count
        await importPendingFiles()
    }

    private func importPendingFiles() async {
        guard phase == .ready, let api, !isImporting else { return }
        isImporting = true
        activeOperations += 1
        var failures: [String] = []
        defer {
            isImporting = false
            importingURL = nil
            importStatus = nil
            activeOperations -= 1
            if !failures.isEmpty { alertText = failures.joined(separator: "\n") }
        }
        while phase == .ready, !pendingImportURLs.isEmpty {
            let url = pendingImportURLs.removeFirst()
            importingURL = url
            pendingImportCount = pendingImportURLs.count
            importStatus = "正在导入 \(url.lastPathComponent)；剩余 \(pendingImportCount) 份"
            do {
                let task = try await api.upload(url)
                if phase == .ready {
                    await refreshTasks()
                    await selectTask(task.taskId)
                    if task.status == "ocr_pending" { await runOCR(taskId: task.taskId) }
                }
            } catch {
                if let error = error as? APIError, error.statusCode == 401 {
                    pendingImportURLs.insert(url, at: 0)
                    pendingImportCount = pendingImportURLs.count
                    phase = .authentication
                    break
                }
                failures.append("\(url.lastPathComponent)：\(error.localizedDescription)")
            }
        }
    }

    func runOCR(taskId: String) async {
        guard let api, let dataDirectory, ocrTaskID == nil else { return }
        let source = dataDirectory.appendingPathComponent("tasks/\(taskId)/original.pdf")
        activeOperations += 1
        ocrTaskID = taskId
        ocrCancelled = false
        ocrStatus = "正在本机识别扫描页…"
        defer {
            activeOperations -= 1
            ocrStatus = nil
            ocrTaskID = nil
            ocrWork = nil
        }
        do {
            var task: TranslationTask = try await api.json("/api/tasks/\(taskId)")
            guard task.status == "ocr_pending" else { return }
            let pages = task.ocrRequiredPages ?? Array(1...max(1, task.ocrPages ?? 1))
            let remaining = pages.filter { !(task.ocrCompletedPages ?? []).contains($0) }
            for page in remaining {
                if ocrCancelled || phase != .ready { throw CancellationError() }
                let completed = task.ocrCompletedPages?.count ?? 0
                ocrProgress = Double(completed) / Double(max(1, pages.count))
                ocrStatus = "正在识别第 \(page) 页 · 已保存 \(completed)/\(pages.count) 页"
                let work = Task.detached(priority: .userInitiated) {
                    try OCRService.recognize(source, pages: [page])
                }
                ocrWork = work
                let lines = try await work.value
                ocrWork = nil
                if ocrCancelled || phase != .ready { throw CancellationError() }
                task = try await api.json("/api/tasks/\(taskId)/ocr/page/\(page)", method: "POST",
                                          body: ["lines": lines.map(\.json)])
                updateTask(task)
                ocrProgress = Double(task.ocrCompletedPages?.count ?? 0) / Double(max(1, pages.count))
            }
        } catch is CancellationError {
            // Completed pages are already durable; the next run continues from them.
        } catch {
            if let failure = error as? APIError, failure.statusCode == 401 {
                handleConnectionError(error)
            } else { alertText = "本机识别已暂停，已完成的页面会保留。\n\(error.localizedDescription)" }
        }
        if phase == .ready {
            await refreshTasks()
            if selectedTaskID == taskId { await loadSegments() }
        }
    }

    func cancelOCR() {
        guard ocrTaskID != nil else { return }
        ocrCancelled = true
        ocrWork?.cancel()
        ocrStatus = "正在停止识别，已完成的页面已保存…"
    }

    private func updateTask(_ task: TranslationTask) {
        if let index = tasks.firstIndex(where: { $0.id == task.id }) { tasks[index] = task }
    }

    func prepareOCR() async {
        guard let api, let id = selectedTaskID else { return }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let updated: TranslationTask = try await api.json("/api/tasks/\(id)/ocr/prepare", method: "POST")
            updateTask(updated)
            await runOCR(taskId: id)
        } catch { alertText = error.localizedDescription }
    }

    func confirmEmptyOCRPage(_ page: Int) async {
        guard let api, let id = selectedTaskID else { return }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let updated: TranslationTask = try await api.json(
                "/api/tasks/\(id)/ocr/page/\(page)/confirm-empty", method: "POST")
            updateTask(updated)
            await refreshTasks()
        } catch { alertText = error.localizedDescription }
    }

    func addOCRPageText(taskId: String, page: Int, text: String) async -> Bool {
        guard let api else { return false }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let updated: TranslationTask = try await api.json(
                "/api/tasks/\(taskId)/ocr/page/\(page)/text", method: "POST", body: ["text": text])
            updateTask(updated)
            await refreshTasks()
            return true
        } catch { alertText = error.localizedDescription; return false }
    }

    func reviseOCR(_ segment: Segment, text: String, taskID: String, expectedRevision: Int?) async -> Bool {
        guard let api else { return false }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let _: OKResponse = try await api.json(
                "/api/tasks/\(taskID)/ocr/\(segment.id)", method: "PATCH", body: [
                    "text": text, "expected_revision": expectedRevision.map { $0 as Any } ?? NSNull()])
            await refreshTasks()
            return true
        } catch {
            alertText = (error as? APIError)?.code == "revision_conflict" ?
                "识别结果已更新，当前草稿仍保留。请复制草稿并重新打开最新段落后保存。" : error.localizedDescription
            return false
        }
    }

    func refreshTasks() async {
        guard let api else { return }
        let token = UUID(); historyGeneration = token
        do {
            let refreshed: [TranslationTask] = try await api.json("/api/tasks")
            guard historyGeneration == token else { return }
            let oldStatuses = Dictionary(uniqueKeysWithValues: tasks.map { ($0.id, $0.status) })
            tasks = refreshed
            for task in refreshed where oldStatuses[task.id] == "translating" ||
                oldStatuses[task.id] == "queued" {
                if ["done", "failed", "paused", "cancelled"].contains(task.status) {
                    await TaskNotifications.finished(task)
                }
            }
            connectionNotice = nil
            if let selected = selectedTask, lastTaskStatus == nil {
                await selectTask(selected.id)
            } else if selectedTask == nil, let first = refreshed.first {
                await selectTask(first.taskId)
            } else if let selected = selectedTask,
                      selected.status != lastTaskStatus || selected.doneSegments != lastDoneSegments ||
                        selected.revision != lastRevision || segmentLoadFailed {
                lastTaskStatus = selected.status
                lastDoneSegments = selected.doneSegments
                lastRevision = selected.revision
                if ocrTaskID != selected.id {
                    await loadSegments()
                    if previewMode == .pages { await preparePages() }
                }
            }
        } catch {
            handleConnectionError(error)
        }
    }

    func selectTask(_ id: String) async {
        reading.flush()
        reading.select(id)
        let bookmark = reading.bookmark(for: id)
        paragraphAnchor = bookmark.paragraphID
        pageLoadToken = UUID()
        loadingPagesFor = nil
        invalidateSegments(clear: true)
        segmentFilterTask?.cancel()
        segmentFilter = SegmentFilter()
        segmentOffset = 0
        segmentTotal = 0
        segmentSheets = []
        clearPageCache()
        selectedTaskID = id
        if selectedTask?.isArchived == true { libraryFilter = "已归档" }
        UserDefaults.standard.set(id, forKey: "DocTranslatorSelectedTask")
        lastTaskStatus = selectedTask?.status
        lastDoneSegments = selectedTask?.doneSegments
        lastRevision = selectedTask?.revision
        segmentRevision = nil
        if let task = selectedTask {
            sourceLanguage = task.sourceLang ?? "auto"
            targetLanguage = task.targetLang ?? "zh-CN"
            tokenBudget = task.tokenBudget ?? 0
            if let provider = task.providerId { selectedProviderID = provider }
        }
        pagePairs = []
        pageMessage = "选择“页面对照”以载入预览"
        previewMode = bookmark.mode == 1 ? .pages : bookmark.mode == 2 && selectedTask?.ext == ".xlsx" ? .table : .text
        UserDefaults.standard.set(bookmark.page, forKey: "DocTranslatorSelectedPage-\(id)")
        UserDefaults.standard.set(bookmark.zoom, forKey: "DocTranslatorZoom-\(id)")
        await loadSegments()
        if previewMode == .pages { await preparePages() }
    }

    private func invalidateSegments(clear: Bool) {
        segmentLoadToken = UUID()
        segmentRequest?.cancel()
        segmentRequest = nil
        isLoadingSegments = false
        if clear { segments = []; segmentRevision = nil; segmentOverflow = []; segmentLoadFailed = false }
    }

    func setSegmentFilter(search: String? = nil, onlyUntranslated: Bool? = nil,
                          onlyReview: Bool? = nil, sheet: String? = nil, category: String? = nil) {
        var filter = segmentFilter
        if let search { filter.search = search }
        if let onlyUntranslated { filter.onlyUntranslated = onlyUntranslated }
        if let onlyReview { filter.onlyReview = onlyReview }
        if let sheet { filter.sheet = sheet }
        if let category { filter.category = category }
        guard filter != segmentFilter else { return }
        let typing = filter.search != segmentFilter.search
        segmentFilter = filter
        paragraphAnchor = filter == SegmentFilter() ? selectedTaskID.flatMap { reading.bookmark(for: $0).paragraphID } : nil
        segmentOffset = 0
        segmentTotal = 0
        invalidateSegments(clear: true)
        segmentFilterTask?.cancel()
        isLoadingSegments = true
        segmentFilterTask = Task { [weak self] in
            if typing {
                do { try await Task.sleep(nanoseconds: 250_000_000) }
                catch { return }
            }
            guard !Task.isCancelled else { return }
            await self?.loadSegments()
        }
    }

    func goToSegmentPage(_ page: Int) async {
        segmentFilterTask?.cancel()
        paragraphAnchor = nil
        segmentOffset = (min(max(1, page), segmentPageCount) - 1) * segmentPageSize
        invalidateSegments(clear: true)
        await loadSegments()
    }

    func loadSegments() async {
        guard let api, let id = selectedTaskID else { return }
        invalidateSegments(clear: false)
        let token = segmentLoadToken
        let filter = segmentFilter
        let offset = segmentOffset
        isLoadingSegments = true
        defer {
            if segmentLoadToken == token { isLoadingSegments = false; segmentRequest = nil }
        }
        do {
            var route = URLComponents()
            route.path = "/api/tasks/\(id)/segments"
            route.queryItems = [URLQueryItem(name: "offset", value: String(offset)),
                                URLQueryItem(name: "limit", value: String(segmentPageSize)),
                                URLQueryItem(name: "q", value: filter.search),
                                URLQueryItem(name: "sheet", value: filter.sheet),
                                URLQueryItem(name: "review_only", value: String(filter.onlyReview)),
                                URLQueryItem(name: "filter", value: filter.onlyUntranslated ? "untranslated" : filter.category)]
            if let anchor = paragraphAnchor, offset == 0, filter == SegmentFilter() {
                route.queryItems?.append(URLQueryItem(name: "anchor_id", value: anchor))
            }
            // Form-style query decoding treats a literal '+' as a space.
            route.percentEncodedQuery = route.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
            let path = route.string!
            let request = Task<SegmentList, Error> { try await api.json(path) }
            segmentRequest = request
            let result = try await request.value
            guard selectedTaskID == id, segmentLoadToken == token, !Task.isCancelled else { return }
            segmentLoadFailed = false
            segmentTotal = result.total ?? result.segments.count
            segmentSheets = result.sheets ?? []
            segmentOffset = result.offset ?? offset
            if offset > 0 && offset >= segmentTotal {
                // Translating/reviewing the last match can shrink the result set.
                segmentOffset = (segmentPageCount - 1) * segmentPageSize
                await loadSegments()
                return
            }
            segments = Array(result.segments.prefix(segmentPageSize))
            segmentRevision = result.revision
            segmentOverflow = result.overflow ?? []
        } catch {
            if selectedTaskID == id, segmentLoadToken == token,
               !Task.isCancelled, !(error is CancellationError), (error as? URLError)?.code != .cancelled {
                segmentLoadFailed = true
                handleConnectionError(error)
            }
        }
    }

    func duplicateTranslation() async {
        guard let api, let task = selectedTask else { return }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let copy: TranslationTask = try await api.json(
                "/api/tasks/\(task.id)/duplicate", method: "POST", body: [
                    "source_lang": sourceLanguage, "target_lang": targetLanguage,
                    "provider_id": selectedProviderID, "max_total_tokens": max(0, tokenBudget)])
            await refreshTasks()
            await selectTask(copy.id)
        } catch { alertText = error.localizedDescription }
    }

    func startTranslation(confirmSnapshot: Bool = false) async {
        guard let api, let task = selectedTask else { return }
        guard !selectedProviderID.isEmpty || task.segmentCount == 0 else {
            showingSettings = true
            return
        }
        if task.requiresSnapshotConfirmation == true && !confirmSnapshot {
            let dialog = NSAlert(); dialog.messageText = "确认旧任务的续翻配置"
            dialog.informativeText = "此任务未保存完整的历史模型记录。继续后将固定当前选择的模型和地址，历史用量不会按新价格补算。"
            dialog.addButton(withTitle: "按当前配置续翻"); dialog.addButton(withTitle: "取消")
            guard dialog.runModal() == .alertFirstButtonReturn else { return }
        }
        await TaskNotifications.requestPermission()
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let _: OKResponse = try await api.json(
                "/api/tasks/\(task.id)/start", method: "POST",
                body: ["provider_id": selectedProviderID,
                       "source_lang": sourceLanguage,
                       "target_lang": targetLanguage,
                       "max_total_tokens": max(0, tokenBudget), "confirm_snapshot": true])
            await refreshTasks()
        } catch {
            if let error = error as? APIError, error.statusCode == 409 {
                showingConflict = true
            } else { alertText = error.localizedDescription }
        }
    }

    func startPendingBatch() async {
        guard let api else { return }
        guard !selectedProviderID.isEmpty else { showingSettings = true; return }
        let pending = tasks.filter { $0.status == "pending_confirm" && !$0.isArchived && $0.migrationIssue == nil }
        guard !pending.isEmpty else { return }
        await TaskNotifications.requestPermission()
        activeOperations += 1
        defer { activeOperations -= 1 }
        var failures = 0
        for task in pending {
            do {
                let _: OKResponse = try await api.json(
                    "/api/tasks/\(task.id)/start", method: "POST", body: [
                        "provider_id": selectedProviderID,
                        "source_lang": sourceLanguage,
                        "target_lang": targetLanguage,
                        "max_total_tokens": max(0, tokenBudget)])
            } catch { failures += 1 }
        }
        await refreshTasks()
        if failures > 0 {
            alertText = "已提交 \(pending.count - failures) 份文档，\(failures) 份未能加入队列。"
        }
    }

    func cancelTranslation() async {
        guard let api, let id = selectedTaskID else { return }
        do {
            let _: OKResponse = try await api.json("/api/tasks/\(id)/cancel", method: "POST")
            await refreshTasks()
        } catch { alertText = error.localizedDescription }
    }

    func moveTaskToTrash(_ id: String) async {
        guard let api else { return }
        do {
            let _: OKResponse = try await api.json("/api/tasks/\(id)", method: "DELETE")
            if selectedTaskID == id {
                invalidateSegments(clear: true)
                segmentFilterTask?.cancel()
                clearPageCache()
                selectedTaskID = nil; pagePairs = []
            }
            await refreshTasks()
            await loadTrash()
        } catch { alertText = error.localizedDescription }
    }

    func loadTrash() async {
        guard let api else { return }
        do {
            let result: TrashList = try await api.json("/api/trash")
            trashItems = result.tasks
        } catch { alertText = error.localizedDescription }
    }

    func restoreTask(_ id: String) async {
        guard let api else { return }
        do {
            let _: OKResponse = try await api.json("/api/trash/\(id)/restore", method: "POST")
            await loadTrash()
            await refreshTasks()
            await selectTask(id)
        } catch { alertText = error.localizedDescription }
    }

    func revise(_ segment: Segment, text: String, taskID: String, expectedRevision: Int?) async -> Bool {
        drafts.set(text, taskID: taskID, segmentID: segment.id, revision: expectedRevision)
        return await saveDraftSnapshot(taskID: taskID, snapshot: [segment.id: text])
    }

    func saveAllDrafts(taskID: String) async -> Bool {
        let snapshot = drafts.drafts[taskID] ?? [:]
        if snapshot.isEmpty { return true }
        return await saveDraftSnapshot(taskID: taskID, snapshot: snapshot)
    }

    private func saveDraftSnapshot(taskID: String, snapshot: [String: String]) async -> Bool {
        guard let api, !busy else { return false }
        guard let revision = drafts.revisions[taskID] else {
            alertText = "旧草稿缺少版本信息，请先使用“重新核对草稿”再保存。"; return false
        }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            let result: RevisionResponse = try await api.json("/api/tasks/\(taskID)/segments", method: "PATCH", body: [
                "revisions": snapshot, "expected_revision": revision])
            historyGeneration = UUID()
            drafts.remove(taskID: taskID, matching: snapshot)
            drafts.advance(taskID: taskID, from: revision, to: result.revision)
            if selectedTaskID == taskID { await loadSegments(); pagePairs = [] }
            await refreshTasks()
            return true
        } catch {
            if let error = error as? APIError, error.code == "revision_conflict" {
                alertText = "译文已更新，草稿已保留。请使用“重新核对草稿”查看最新内容后再次保存。"
            } else if let error = error as? APIError, error.code == "output_conflict" {
                showingConflict = true
            } else { alertText = error.localizedDescription }
            return false
        }
    }

    func reviewDrafts(taskID: String) async {
        guard let api else { return }
        do {
            let task: TranslationTask = try await api.json("/api/tasks/\(taskID)")
            let dialog = NSAlert(); dialog.messageText = "重新核对草稿"
            dialog.informativeText = "将载入最新已保存译文，并保留你的草稿。请在编辑框内对照后再次保存。"
            dialog.addButton(withTitle: "载入并核对"); dialog.addButton(withTitle: "取消")
            guard dialog.runModal() == .alertFirstButtonReturn else { return }
            drafts.advance(taskID: taskID, from: drafts.revisions[taskID], to: task.revision)
            editingSegment = nil
            await loadSegments()
        } catch { alertText = error.localizedDescription }
    }

    func resolveConflict(_ action: String) async {
        guard let api, let id = selectedTaskID else { return }
        do {
            let _: TranslationTask = try await api.json(
                "/api/tasks/\(id)/resolve-conflict", method: "POST", body: ["action": action])
            showingConflict = false
            await refreshTasks()
            await loadSegments()
            pagePairs = []
        } catch { alertText = error.localizedDescription }
    }

    func revealVersions() {
        guard let dataDirectory, let id = selectedTaskID else { return }
        let directory = dataDirectory.appendingPathComponent("tasks/\(id)/versions")
        guard FileManager.default.fileExists(atPath: directory.path) else {
            alertText = "当前任务尚未生成历史版本。"
            return
        }
        NSWorkspace.shared.open(directory)
    }

    @discardableResult
    func exportTranslation(decide: ((Int) -> DraftExportChoice)? = nil, destination: URL? = nil,
                           chooseDestination: (() -> URL?)? = nil) async -> Bool {
        guard let api, let task = selectedTask, task.hasTranslated, !busy else { return false }
        let count = drafts.count(taskID: task.id)
        var choice = DraftExportChoice.savedVersion
        if count > 0 {
            if let decide { choice = decide(count) }
            else {
                let dialog = NSAlert(); dialog.messageText = "有 \(count) 处草稿尚未保存到译文"
                dialog.informativeText = "选择本次导出内容。导出已保存版本会保留草稿。"
                dialog.addButton(withTitle: "保存全部并导出"); dialog.addButton(withTitle: "导出已保存版本"); dialog.addButton(withTitle: "取消")
                switch dialog.runModal() {
                case .alertFirstButtonReturn: choice = .saveAll
                case .alertSecondButtonReturn: choice = .savedVersion
                default: choice = .cancel
                }
            }
        }
        guard choice != .cancel else { return false }
        var target = destination
        if let chooseDestination {
            guard let chosen = chooseDestination() else { return false }; target = chosen
        }
        if target == nil {
            let panel = NSSavePanel()
            let stem = URL(fileURLWithPath: task.filename).deletingPathExtension().lastPathComponent
            panel.nameFieldStringValue = "\(stem).\(task.targetLang ?? targetLanguage)\(task.ext)"
            if let type = UTType(filenameExtension: String(task.ext.dropFirst())) { panel.allowedContentTypes = [type] }
            panel.allowsOtherFileTypes = false
            guard panel.runModal() == .OK else { return false }
            target = panel.url
        }
        guard let target else { return false }
        if choice == .saveAll, !(await saveAllDrafts(taskID: task.id)) { return false }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            try await api.download("/api/tasks/\(task.id)/download", to: target)
            if destination == nil { alertText = "已保存到 \(target.path)" }
            return true
        } catch { alertText = error.localizedDescription; return false }
    }

    func importLegacyLibrary() async {
        guard let api, !busy else { return }
        drafts.persist(); reading.flush()
        guard drafts.saveError == nil && reading.saveError == nil else {
            alertText = "请先保存当前草稿和阅读位置后再导入"; return
        }
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.message = "选择已退出的旧版资料库（DocTranslatorNative 或 DocTranslatorMac）"
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        guard panel.runModal() == .OK, let source = panel.url else { return }
        let accessed = source.startAccessingSecurityScopedResource()
        defer { if accessed { source.stopAccessingSecurityScopedResource() } }
        activeOperations += 1
        defer { activeOperations -= 1 }
        do {
            var body: [String: Any] = ["source": source.path]
            if source.lastPathComponent == "DocTranslatorNative" {
                let old = UserDefaults.standard.persistentDomain(forName: "local.doctranslator.native") ?? [:]
                var documents: [String: Any] = [:]
                for (key, value) in old where key.hasPrefix("DocTranslatorPreviewMode-") {
                    let id = String(key.dropFirst("DocTranslatorPreviewMode-".count))
                    documents[id] = ["mode": (value as? String) == "页面对照" ? 1 : (value as? String) == "单元格对照" ? 2 : 0,
                                     "page": old["DocTranslatorSelectedPage-" + id] ?? 0,
                                     "zoom": old["DocTranslatorZoom-" + id] ?? 1.0]
                }
                body["native_reading"] = ["documents": documents, "lastTaskID": old["DocTranslatorSelectedTask"] ?? NSNull()]
            }
            let result: LibraryImportResponse = try await api.json("/api/libraries/import", method: "POST", body: body)
            drafts.reload(); reading.reload()
            await reloadAll()
            alertText = "导入 \(result.imported) 份，跳过重复 \(result.skipped) 份。\n" +
                "\(result.credentialRequired.count) 个模型需要补录密钥。" +
                (result.issues.isEmpty ? "" : "\n需要检查：" + result.issues.map(\.message).joined(separator: "\n"))
        } catch { alertText = error.localizedDescription }
    }

    func confirmSettingsTransition(decide: (() -> NSApplication.ModalResponse)? = nil) async -> Bool {
        guard settingsDirty else { return true }
        let dialog = NSAlert(); dialog.messageText = "模型设置尚未保存"
        dialog.addButton(withTitle: "保存修改"); dialog.addButton(withTitle: "放弃修改"); dialog.addButton(withTitle: "返回")
        switch (decide?() ?? dialog.runModal()) {
        case .alertFirstButtonReturn: return await saveSettingsBeforeClose?() ?? false
        case .alertSecondButtonReturn: settingsDirty = false; return true
        default: return false
        }
    }

    func openInGenOffice() {
        guard let task = selectedTask, task.hasTranslated, let dataDirectory else { return }
        let candidates = [
            URL(fileURLWithPath: "/Applications/GenOffice.app"),
            FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/GenOffice.app")
        ]
        guard let appURL = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }) else {
            alertText = "未找到 GenOffice.app。页面校对仍可在本应用中完成。"
            return
        }
        let file = dataDirectory.appendingPathComponent("tasks/\(task.id)/translated\(task.ext)")
        NSWorkspace.shared.open([file], withApplicationAt: appURL,
                                configuration: NSWorkspace.OpenConfiguration()) { _, error in
            if let error { Task { @MainActor in self.alertText = error.localizedDescription } }
        }
    }

    func preparePages() async {
        guard let api, let task = selectedTask, loadingPagesFor != task.id else { return }
        let token = UUID()
        pageLoadToken = token
        loadingPagesFor = task.id
        defer { if pageLoadToken == token { loadingPagesFor = nil } }
        func isCurrent() -> Bool {
            phase == .ready && selectedTaskID == task.id && pageLoadToken == token &&
                selectedTask?.revision == task.revision
        }
        pagePairs = []
        clearPageCache()
        pageMessage = "正在载入页面…"
        do {
            var count = 0
            var originalCount: Int?
            var translatedCount: Int?
            if task.ext == ".pdf" {
                let pages: PDFPages = try await api.json("/api/tasks/\(task.id)/preview/pdf/pages")
                count = pages.pages
            } else {
                var render: RenderStatus = try await api.json("/api/tasks/\(task.id)/preview/render")
                guard isCurrent() else { return }
                if render.status == "unavailable" {
                    pageMessage = "Office 页面预览需要 GenOffice 或 LibreOffice。"
                    return
                }
                if render.status == "none" || render.status == "failed" {
                    render = try await api.json("/api/tasks/\(task.id)/preview/render", method: "POST")
                    guard isCurrent() else { return }
                }
                if render.status == "rendering" {
                    pageMessage = "正在生成 Office 页面预览，请稍候…"
                    return
                }
                if render.status == "failed" {
                    pageMessage = render.error ?? "页面预览失败"
                    return
                }
                count = render.pages
                originalCount = render.originalPages
                translatedCount = render.translatedPages
            }
            guard isCurrent() else { return }
            guard count > 0 else { pageMessage = "文档没有可预览页面"; return }
            pagePairs = (1...count).map {
                PagePair(number: $0, hasOriginal: $0 <= (originalCount ?? count),
                         hasTranslated: task.hasTranslated && $0 <= (translatedCount ?? count))
            }
            pageMessage = ""
        } catch {
            if isCurrent() { pageMessage = error.localizedDescription }
        }
    }

    func showPage(_ page: Int) {
        guard let id = selectedTaskID else { return }
        UserDefaults.standard.set(page, forKey: "DocTranslatorSelectedPage-\(id)")
        pageNavigation = PageNavigation(taskID: id, page: page)
        previewMode = .pages
    }

    private func clearPageCache() {
        pageCacheGeneration = UUID()
        pageCache.removeAllObjects()
    }

    func pageImage(taskId: String, ext: String, page: Int, variant: String) async -> NSImage? {
        guard let api, phase == .ready, selectedTaskID == taskId, !Task.isCancelled else { return nil }
        let generation = pageCacheGeneration
        let revision = selectedTask?.revision
        let key = "\(taskId)-\(revision ?? 0)-\(variant)-\(page)" as NSString
        if let cached = pageCache.object(forKey: key) { return cached }
        let route = ext == ".pdf" ? "preview/pdf" : "preview/render/page"
        guard let data = try? await api.bytes(
            "/api/tasks/\(taskId)/\(route)/\(page)?variant=\(variant)"),
              !Task.isCancelled, phase == .ready, selectedTaskID == taskId,
              generation == pageCacheGeneration, revision == selectedTask?.revision,
              let image = NSImage(data: data) else { return nil }
        let pixels = image.representations.map { max(1, $0.pixelsWide) * max(1, $0.pixelsHigh) * 4 }.max()
            ?? Int(image.size.width * image.size.height * 4)
        pageCache.setObject(image, forKey: key, cost: pixels)
        return image
    }

    func stop() {
        invalidateSegments(clear: true)
        segmentFilterTask?.cancel()
        pollTask?.cancel()
        backend?.stop()
    }

    func prepareToQuit() async -> Bool {
        guard await confirmSettingsTransition() else { return false }
        drafts.persist(); reading.flush()
        guard drafts.saveError == nil && reading.saveError == nil else {
            alertText = drafts.saveError ?? reading.saveError; return false
        }
        showingSettings = false
        phase = .closing
        invalidateSegments(clear: true)
        segmentFilterTask?.cancel()
        cancelOCR()
        pollTask?.cancel()
        await backend?.stopAndWait()
        return true
    }

    private func beginPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                let active = self?.tasks.contains {
                    $0.status == "translating" || $0.status == "queued"
                } ?? false
                try? await Task.sleep(nanoseconds: active ? 1_500_000_000 : 8_000_000_000)
                guard !Task.isCancelled else { break }
                guard let self, self.phase == .ready else { continue }
                await self.refreshTasks()
                if self.previewMode == .pages && self.pagePairs.isEmpty &&
                    self.pageMessage.contains("正在生成") {
                    await self.preparePages()
                }
            }
        }
    }

}
