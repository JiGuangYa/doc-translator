import AppKit
import SwiftUI

private extension Optional where Wrapped == [String] {
    var orEmpty: [String] { self ?? [] }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var onTerminate: (() -> Void)?
    var onPrepareTerminate: (() async -> Bool)?
    private var preparingToQuit = false
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let onPrepareTerminate else { return .terminateNow }
        guard !preparingToQuit else { return .terminateLater }
        preparingToQuit = true
        Task {
            let allowed = await onPrepareTerminate()
            preparingToQuit = false
            sender.reply(toApplicationShouldTerminate: allowed)
        }
        return .terminateLater
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationWillTerminate(_ notification: Notification) { onTerminate?() }
}

@main
struct DocTranslatorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var state = AppState()

    var body: some Scene {
        WindowGroup(id: "main") {
            RootView(state: state)
                .frame(minWidth: 980, minHeight: 650)
                .task {
                    appDelegate.onTerminate = { state.stop() }
                    appDelegate.onPrepareTerminate = { await state.prepareToQuit() }
                    await state.launch()
                }
                .onOpenURL { url in Task { await state.importFile(url) } }
                .sheet(isPresented: $state.showingSettings) {
                    ProviderSettingsView(state: state)
                }
                .sheet(isPresented: $state.showingTrash) {
                    TrashView(state: state)
                }
                .sheet(isPresented: $state.showingConflict) {
                    OutputConflictView(state: state)
                }
                .sheet(item: $state.editingSegment) { segment in
                    SegmentEditorView(state: state, segment: segment)
                }
                .sheet(item: $state.editingOCRSegment) { segment in
                    OCRSegmentEditorView(state: state, segment: segment)
                }
                .alert("文档翻译", isPresented: Binding(
                    get: { state.alertText != nil },
                    set: { if !$0 { state.alertText = nil } }
                )) {
                    Button("好") { state.alertText = nil }
                } message: {
                    Text(state.alertText ?? "")
                }
        }
        .defaultSize(width: 1320, height: 840)
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("模型设置…") { state.showingSettings = true }.keyboardShortcut(",")
            }
            CommandGroup(after: .importExport) {
                Button("导入旧资料库…") { Task { await state.importLegacyLibrary() } }.disabled(state.busy || state.phase != .ready)
            }
        }
        MenuBarExtra("文档翻译", systemImage: "doc.text") {
            MenuBarContent(state: state)
        }
    }
}

private struct MenuBarContent: View {
    @Environment(\.openWindow) private var openWindow
    @ObservedObject var state: AppState

    var body: some View {
        let active = state.tasks.filter { $0.status == "translating" || $0.status == "queued" }.count
        Text(active == 0 ? "没有正在进行的任务" : "\(active) 个任务正在翻译或排队")
        Button("打开工作台") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Button("锁定") { Task { await state.lock() } }
        Divider()
        Button("退出并保存进度") { NSApp.terminate(nil) }
    }
}

private struct RootView: View {
    @ObservedObject var state: AppState
    @State private var taskSearch = ""
    @State private var pendingDelete: TranslationTask?

    var body: some View {
        Group {
            switch state.phase {
            case .launching:
                ContentUnavailableView("正在启动翻译引擎", systemImage: "doc.text.magnifyingglass",
                                       description: Text("首次启动可能需要几秒钟"))
                    .overlay(alignment: .bottom) { ProgressView().padding(40) }
            case .failed:
                VStack {
                    ContentUnavailableView("启动失败", systemImage: "exclamationmark.triangle",
                                           description: Text(state.alertText ?? "无法启动本地服务"))
                    Button("重新启动翻译引擎") { Task { await state.restartBackend() } }
                        .buttonStyle(.borderedProminent).padding(.bottom, 40)
                }
            case .authentication:
                AuthenticationView(state: state)
            case .closing:
                ContentUnavailableView("正在保存翻译进度", systemImage: "externaldrive.badge.checkmark",
                    description: Text("当前请求结束后将退出。已完成的段落会保存，下次可继续翻译。"))
                    .overlay(alignment: .bottom) { ProgressView().padding(40) }
            case .ready:
                NavigationSplitView {
                    sidebar
                } detail: {
                    VStack(spacing: 0) {
                        if let notice = state.connectionNotice {
                            HStack {
                                Text("连接中断：\(notice)").font(.caption)
                                Spacer()
                                Button("重试") { Task { await state.reloadAll() } }
                                Button("重启本机服务") { Task { await state.restartBackend() } }
                                    .disabled(state.busy)
                            }
                            .padding(8).background(.orange.opacity(0.12))
                        }
                        if let message = state.drafts.saveError ?? state.reading.saveError {
                            HStack { Text(message).font(.caption); Spacer()
                                Button("重试保存") { state.drafts.persist(); state.reading.flush() }
                            }.padding(8).background(.orange.opacity(0.12))
                        }
                        if let task = state.selectedTask {
                            WorkspaceView(state: state, task: task)
                        } else {
                            ContentUnavailableView("导入一份文档开始", systemImage: "doc.badge.plus",
                                                   description: Text("支持 DOCX、PPTX、XLSX 和 PDF"))
                        }
                    }
                }
            }
        }
        .dropDestination(for: URL.self) { urls, _ in
            Task { await state.importFiles(urls) }
            return !urls.isEmpty
        }
        .confirmationDialog("移到废纸篓？", isPresented: Binding(
            get: { pendingDelete != nil },
            set: { if !$0 { pendingDelete = nil } }
        )) {
            if let pendingDelete {
                Button("移动 \(pendingDelete.filename)", role: .destructive) {
                    Task { await state.moveTaskToTrash(pendingDelete.id) }
                    self.pendingDelete = nil
                }
            }
        } message: {
            Text("文档与译文可在废纸篓中恢复。")
        }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("文档翻译").font(.title2.bold())
                    Text("预览版 0.3.0 · 本机工作台").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button { Task { await state.chooseFile() } } label: {
                    Image(systemName: "plus")
                }
                .buttonStyle(.borderedProminent)
                .help("导入文档")
                .keyboardShortcut("o", modifiers: .command)
            }
            .padding(16)

            if let status = state.importStatus {
                HStack { ProgressView().controlSize(.small); Text(status).font(.caption) }
                    .padding(.horizontal, 16).padding(.bottom, 8)
            }

            TextField("搜索文档", text: $taskSearch)
                .textFieldStyle(.roundedBorder)
                .padding(.horizontal, 16)
                .padding(.bottom, 8)
            if state.tasks.contains(where: { $0.status == "pending_confirm" }) {
                Button {
                    Task { await state.startPendingBatch() }
                } label: {
                    Label("翻译待处理文档", systemImage: "list.bullet.clipboard")
                }
                .buttonStyle(.bordered)
                .disabled(state.busy)
                .padding(.horizontal, 16)
                .padding(.bottom, 8)
            }

            Picker("文档状态", selection: $state.libraryFilter) {
                ForEach(["全部", "待翻译", "翻译中", "待处理", "已完成", "已归档"], id: \.self) { Text($0).tag($0) }
            }.padding(.horizontal, 16)
            List(selection: Binding(get: { state.selectedTaskID }, set: { id in
                if let id { Task { await state.selectTask(id) } }
            })) {
                Section("文档") {
                    ForEach(state.tasks.filter {
                        state.libraryIncludes($0) && (taskSearch.isEmpty || $0.filename.localizedCaseInsensitiveContains(taskSearch))
                    }) { task in
                        HStack(spacing: 10) {
                                Image(systemName: symbol(for: task.ext))
                                    .font(.title3)
                                    .frame(width: 26)
                                    .foregroundStyle(.tint)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(task.filename)
                                        .fontWeight(state.selectedTaskID == task.id ? .semibold : .regular)
                                        .lineLimit(2)
                                    Text(task.statusLabel + (task.targetLang.map { " · \($0)" } ?? ""))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                    if task.status == "translating" {
                                        ProgressView(value: task.progress)
                                            .controlSize(.mini)
                                    }
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.vertical, 5)
                            .contentShape(Rectangle())
                        .tag(task.id)
                        .listRowBackground(state.selectedTaskID == task.id ?
                                           Color.accentColor.opacity(0.12) : Color.clear)
                        .contextMenu {
                            Button(task.isArchived ? "恢复归档" : "归档文档") { Task { await state.setArchived(task) } }
                                .disabled(state.busy || ["translating", "queued"].contains(task.status))
                            Button("移到废纸篓", systemImage: "trash") {
                                pendingDelete = task
                            }
                        }
                    }
                }
            }
            .listStyle(.sidebar)
            .id(taskSearch + "|" + state.libraryFilter)

            Button("导入旧资料库…") { Task { await state.importLegacyLibrary() } }
                .buttonStyle(.plain).padding(8).disabled(state.busy)
            HStack {
                Button { state.showingSettings = true } label: {
                    Label("模型设置", systemImage: "slider.horizontal.3")
                }
                .buttonStyle(.plain)
                Button { Task { await state.lock() } } label: {
                    Image(systemName: "lock")
                }
                .buttonStyle(.plain)
                .help("锁定")
                Spacer()
                Button {
                    Task { await state.loadTrash(); state.showingTrash = true }
                } label: { Image(systemName: "trash") }
                .buttonStyle(.plain)
                .help("废纸篓")
                .accessibilityLabel("打开废纸篓")
                Button { Task { await state.reloadAll() } } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .help("刷新")
                .accessibilityLabel("刷新任务")
            }
            .padding(16)
        }
        .frame(minWidth: 240)
    }

    private func symbol(for ext: String) -> String {
        switch ext {
        case ".pdf": return "doc.richtext"
        case ".pptx": return "rectangle.on.rectangle"
        case ".xlsx": return "tablecells"
        default: return "doc.text"
        }
    }
}

private struct AuthenticationView: View {
    @ObservedObject var state: AppState
    @State private var password = ""

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "character.book.closed.fill")
                .font(.system(size: 50))
                .foregroundStyle(.tint)
            Text(state.configured ? "解锁文档翻译" : "设置本机密码")
                .font(.largeTitle.bold())
            Text(state.configured ? "输入密码以访问本机文档和模型设置" :
                 "密码用于保护本机翻译服务、文档和 API 设置，至少 8 位。")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            SecureField("密码", text: $password)
                .textFieldStyle(.roundedBorder)
                .frame(width: 300)
                .onSubmit { Task { await state.authenticate(password: password) } }
            Toggle("下次使用 macOS 系统解锁", isOn: $state.useSystemUnlock)
                .frame(width: 300)
            if state.configured && SystemUnlock.isEnabled {
                Button("使用 Touch ID 或 Mac 密码") {
                    Task { _ = await state.unlockWithSystem() }
                }
            }
            Button(state.configured ? "解锁" : "创建密码并继续") {
                Task { await state.authenticate(password: password) }
            }
            .buttonStyle(.borderedProminent)
            .disabled(password.count < 8 || state.busy)
            if state.busy { ProgressView() }
            if state.pendingImportCount > 0 {
                Text("解锁后将导入 \(state.pendingImportCount) 份文档")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct WorkspaceView: View {
    @ObservedObject var state: AppState
    let task: TranslationTask
    @State private var showingWarnings = false
    @State private var manualOCRPage: OCRPageReference?
    @State private var reviewingOCRPage = 0
    @State private var showingPartialExport = false

    private var taskIsRunning: Bool { ["translating", "queued"].contains(task.status) }
    private var needsOCRReview: Bool {
        (task.ocrReviewCount ?? state.segments.filter {
            (($0.meta?.confidence ?? 1) < 0.75 || $0.meta?.needsReview == true) && $0.meta?.reviewed != true
        }.count) > 0 || !task.emptyOCRPagesToReview.isEmpty
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.filename).font(.title2.bold()).lineLimit(1)
                    HStack(spacing: 8) {
                        Text(task.statusLabel)
                        Text("·")
                        Text("\(task.segmentCount ?? 0) 段")
                        if task.status == "translating" {
                            Text("· \(task.doneSegments ?? 0)/\(task.totalSegments ?? 0)")
                        }
                        if let usage = task.usage, usage.total > 0 {
                            Text("· \(usage.total) Token")
                        }
                        if let estimate = task.estimatedCostUsd {
                            Text("· 估算 $\(estimate.formatted(.number.precision(.fractionLength(4))))")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                Spacer()
                Button("另译一份") { Task { await state.duplicateTranslation() } }
                    .help("按当前语言和模型创建独立任务，保留已有译文")
                    .disabled(state.busy)
                Button {
                    if (task.untranslatedCount ?? 0) > 0 || !state.segmentOverflow.isEmpty {
                        showingPartialExport = true
                    } else { Task { await state.exportTranslation() } }
                } label: {
                    Label(task.status == "failed" ? "导出上次有效版本" : "导出译文", systemImage: "square.and.arrow.up")
                }
                .disabled(!task.hasTranslated || task.status == "translating" || state.busy)
                .keyboardShortcut("s", modifiers: .command)
                Button { state.openInGenOffice() } label: {
                    Label("GenOffice 编辑", systemImage: "arrow.up.forward.app")
                }
                .disabled(!task.hasTranslated || taskIsRunning)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 16)
            Divider()

            HStack(spacing: 12) {
                Text("原文").foregroundStyle(.secondary)
                TextField("auto", text: $state.sourceLanguage).frame(width: 88)
                    .disabled(taskIsRunning)
                Image(systemName: "arrow.right").foregroundStyle(.secondary)
                Text("译文").foregroundStyle(.secondary)
                TextField("zh-CN", text: $state.targetLanguage).frame(width: 92)
                    .disabled(taskIsRunning)
                Picker("模型", selection: $state.selectedProviderID) {
                    Text("选择模型").tag("")
                    ForEach(state.providers) { provider in
                        Text("\(provider.name) · \(provider.model)").tag(provider.id)
                    }
                }
                .frame(maxWidth: 270)
                .disabled(taskIsRunning)
                TextField("Token 上限", value: $state.tokenBudget, format: .number)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 100)
                    .disabled(taskIsRunning)
                    .help("0 表示不限制；到达上限后暂停，仍可续翻")
                Spacer()
                if task.status == "ocr_pending" {
                    if state.ocrTaskID == task.id {
                        Button("暂停识别") { state.cancelOCR() }
                    } else {
                        Button("继续识别") { Task { await state.runOCR(taskId: task.id) } }
                            .disabled(state.busy)
                    }
                } else if task.status == "translating" || task.status == "queued" {
                    Button("取消") { Task { await state.cancelTranslation() } }
                } else {
                    Button { Task { await state.startTranslation() } } label: {
                        Label(needsOCRReview ? "请先校对识别结果" : task.segmentCount == 0 ? "生成副本" :
                              task.status == "pending_confirm" ? "开始翻译" : "继续翻译",
                              systemImage: "sparkles")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(state.busy || task.isArchived || task.migrationIssue != nil || needsOCRReview || (task.status == "done" &&
                                            (task.untranslatedCount ?? 0) == 0))
                    .keyboardShortcut(.return, modifiers: .command)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 12)
            if task.status == "translating" {
                ProgressView(value: task.progress).padding(.horizontal, 22)
            }
            if task.isArchived {
                HStack { Text("此文档已归档，原文、译文和草稿均保留。")
                    Spacer(); Button("恢复文档") { Task { await state.setArchived(task) } }
                }.font(.caption).padding(10)
            }
            if let issue = task.migrationIssue { Text(issue).font(.caption).foregroundStyle(.orange).padding(8) }
            if let snapshot = task.providerSnapshot, let model = snapshot.model {
                Text("续翻使用：\(model) · \(snapshot.baseUrl ?? "")")
                    .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 22)
            }
            if task.historyUsageUnknown == true { Text("历史用量未记录；仅统计此后新增用量。")
                .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 22) }
            ocrControls
            if state.drafts.count(taskID: task.id) > 0 {
                HStack {
                    Label("\(state.drafts.count(taskID: task.id)) 处草稿已保存在本机", systemImage: "square.and.pencil")
                    Spacer()
                    Button("重新核对草稿") { Task { await state.reviewDrafts(taskID: task.id) } }
                    Button("保存全部草稿") { Task { await state.saveAllDrafts(taskID: task.id) } }
                }.font(.caption).padding(10).background(.bar).disabled(state.busy || taskIsRunning)
            }
            if let error = task.error, !error.isEmpty {
                Text(error).foregroundStyle(.red).font(.caption).padding(8)
            }
            if task.externalEdit == true {
                HStack {
                    Label("正在保留 GenOffice 的外部修改；应用内逐段修订已暂停。",
                          systemImage: "doc.badge.gearshape")
                    Spacer()
                    Button("处理版本") { state.showingConflict = true }
                }
                .font(.caption)
                .padding(8)
                .background(.orange.opacity(0.12))
            }
            if !task.warnings.orEmpty.isEmpty || !state.segmentOverflow.isEmpty {
                DisclosureGroup(isExpanded: $showingWarnings) {
                    ForEach(task.warnings.orEmpty, id: \.self) { warning in
                        Text("• \(warning)").frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if !state.segmentOverflow.isEmpty {
                        Text("有 \(state.segmentOverflow.count) 段译文超出原始版面，请校对页面。")
                    }
                } label: {
                    Label("需要检查的内容", systemImage: "exclamationmark.triangle")
                }
                .font(.caption)
                .padding(.horizontal, 22)
                .padding(.vertical, 6)
            }
            Divider()

            HStack {
                Picker("视图", selection: $state.previewMode) {
                    ForEach(AppState.PreviewMode.allCases.filter {
                        task.ext == ".xlsx" || $0 != .table
                    }, id: \.self) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: task.ext == ".xlsx" ? 360 : 240)
                .onChange(of: state.previewMode) { _, mode in
                    UserDefaults.standard.set(mode.rawValue,
                        forKey: "DocTranslatorPreviewMode-\(task.id)")
                    state.recordMode()
                    if mode == .pages { Task { await state.preparePages() } }
                    if mode != .table && !state.segmentFilter.sheet.isEmpty {
                        state.setSegmentFilter(sheet: "")
                    }
                }
                Spacer()
                if state.previewMode != .pages {
                    Toggle("仅未译", isOn: Binding(
                        get: { state.segmentFilter.onlyUntranslated },
                        set: { state.setSegmentFilter(onlyUntranslated: $0) }))
                        .toggleStyle(.checkbox)
                    if task.ocrScanned == true && ["pending_confirm", "ocr_pending"].contains(task.status) {
                        Toggle("仅待校对", isOn: Binding(
                            get: { state.segmentFilter.onlyReview },
                            set: { state.setSegmentFilter(onlyReview: $0) }))
                            .toggleStyle(.checkbox)
                    }
                    Picker("筛选", selection: Binding(get: { state.segmentFilter.category }, set: { state.setSegmentFilter(category: $0) })) {
                        Text("全部").tag("all"); Text("修订草稿").tag("drafts"); Text("排版待检查").tag("layout")
                    }.frame(width: 125)
                    TextField("搜索全文、草稿或位置", text: Binding(
                        get: { state.segmentFilter.search },
                        set: { state.setSegmentFilter(search: $0) }))
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 200)
                } else {
                    Button("重新载入") { Task { await state.preparePages() } }
                        .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 10)

            if state.previewMode == .text {
                SegmentCompareView(state: state, task: task)
            } else if state.previewMode == .table {
                XLSXCompareView(state: state)
            } else {
                PageCompareView(state: state, task: task)
                    .id(task.id)
            }
        }
        .sheet(item: $manualOCRPage) { reference in
            OCRPageTextEditor(state: state, reference: reference)
        }
        .onChange(of: task.status) { _, status in
            if !["pending_confirm", "ocr_pending"].contains(status), state.segmentFilter.onlyReview {
                state.setSegmentFilter(onlyReview: false)
            }
        }
        .confirmationDialog("译文仍有需要检查的内容", isPresented: $showingPartialExport) {
            Button("导出当前版本") { Task { await state.exportTranslation() } }
            Button("返回校对", role: .cancel) { }
        } message: {
            Text("尚未翻译或未能放入原版面的内容会保留原文。可先返回校对，或导出当前版本。")
        }
    }

    private var ocrControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            if state.ocrTaskID == task.id, let status = state.ocrStatus {
                HStack {
                    ProgressView(value: state.ocrProgress).frame(width: 180)
                    Text(status)
                }
            } else if task.status == "ocr_pending" {
                Text("已保存 \(task.ocrCompletedPages?.count ?? 0)/\(task.ocrRequiredPages?.count ?? task.ocrPages ?? 0) 页识别结果，可继续识别。")
            }
            if task.ext == ".pdf", task.status == "pending_confirm" {
                HStack {
                    if task.ocrScanned != true || !(Set(task.ocrOptionalPages ?? []).subtracting(task.ocrCompletedPages ?? [])).isEmpty {
                        Button("补充图片文字识别") { Task { await state.prepareOCR() } }
                            .disabled(state.busy)
                        Text("本机识别后将导出保留原页的双语 PDF。")
                    } else {
                        Text(needsOCRReview ? "请先校对低可信度文字及未识别出文字的页面。" :
                             "本机识别已完成，原页会保留在双语译文中。")
                    }
                }
            }
            if !task.emptyOCRPagesToReview.isEmpty {
                let pages = task.emptyOCRPagesToReview
                let page = pages.contains(reviewingOCRPage) ? reviewingOCRPage : (pages.first ?? 1)
                HStack {
                    Picker("待核对", selection: Binding(get: { page }, set: { reviewingOCRPage = $0 })) {
                        ForEach(pages, id: \.self) { Text("第 \($0) 页").tag($0) }
                    }
                    .frame(width: 150)
                    Text("未识别到新增文字")
                    Spacer()
                    Button("查看原页") {
                        state.showPage(page)
                    }
                    Button("补录文字") { manualOCRPage = OCRPageReference(taskID: task.id, page: page) }
                    Button("确认无待译文字") { Task { await state.confirmEmptyOCRPage(page) } }
                        .disabled(state.busy)
                }
            }
        }
        .font(.caption).foregroundStyle(.secondary)
        .padding(.horizontal, 22)
    }
}

private struct ParagraphPositions: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

private struct SegmentCompareView: View {
    @ObservedObject var state: AppState
    let task: TranslationTask
    @State private var restored = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text("原文").frame(maxWidth: .infinity, alignment: .leading)
                Text("译文").frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.headline)
            .padding(.horizontal, 24)
            .padding(.vertical, 10)
            .background(.quaternary.opacity(0.4))
            ScrollViewReader { reader in
            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(state.segments) { segment in
                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(segment.text).textSelection(.enabled)
                                if !segment.context.isEmpty {
                                    HStack {
                                        Text(segment.context)
                                            .foregroundStyle(.secondary)
                                        if task.ext == ".pdf", let page = segment.meta?.page {
                                            Button("定位第 \(page) 页") {
                                                state.showPage(page)
                                            }
                                            .buttonStyle(.plain)
                                        }
                                    }
                                    .font(.caption)
                                }
                                if task.ocrScanned == true && ["pending_confirm", "ocr_pending"].contains(task.status) {
                                    HStack {
                                        if segment.meta?.reviewed != true &&
                                            ((segment.meta?.confidence ?? 1) < 0.75 || segment.meta?.needsReview == true) {
                                            Label(segment.meta?.needsReview == true ? "与文字层重叠，请核对" : "识别可信度较低",
                                                  systemImage: "exclamationmark.triangle")
                                                .foregroundStyle(.orange)
                                        }
                                        Spacer()
                                        Button("校正识别文字") { state.editingOCRSegment = segment }
                                            .buttonStyle(.plain)
                                            .disabled(state.busy || state.isLoadingSegments)
                                    }
                                    .font(.caption)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(15)
                            .frame(minHeight: 70, alignment: .topLeading)
                            .background(.background, in: RoundedRectangle(cornerRadius: 10))
                            Button { state.editingSegment = segment } label: {
                                HStack(alignment: .top) {
                                    Text(state.drafts.text(taskID: task.id, segmentID: segment.id) ?? (!segment.translatable ? "保留原文" :
                                         segment.needsTranslation ? "尚未翻译" :
                                         (segment.translation ?? "")))
                                        .foregroundStyle(segment.needsTranslation ||
                                            !segment.translatable ? .secondary : .primary)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    if state.drafts.text(taskID: task.id, segmentID: segment.id) != nil {
                                        Text("草稿").font(.caption).foregroundStyle(.orange)
                                    }
                                    if segment.translatable && !segment.needsTranslation {
                                        Image(systemName: "pencil").foregroundStyle(.secondary)
                                    }
                                }
                                .padding(15)
                                .frame(minHeight: 70, alignment: .topLeading)
                                .background(Color.accentColor.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                            }
                            .buttonStyle(.plain)
                            .disabled(!segment.translatable ||
                                      task.externalEdit == true || state.busy ||
                                      state.isLoadingSegments ||
                                      ["translating", "queued"].contains(task.status))
                            .frame(maxWidth: .infinity)
                        }
                        .id(segment.id)
                        .background(GeometryReader { geometry in
                            Color.clear.preference(key: ParagraphPositions.self,
                                value: [segment.id: geometry.frame(in: .named("paragraph-reader"))])
                        })
                    }
                }
                .padding(16)
            }
            .coordinateSpace(name: "paragraph-reader")
            .onPreferenceChange(ParagraphPositions.self) { values in
                guard restored else { return }
                let first = values.filter { $0.value.maxY > 0 }.min { $0.value.minY < $1.value.minY }?.key
                if let first { state.recordParagraph(first, taskID: task.id) }
            }
            .onChange(of: state.isLoadingSegments, initial: true) { _, loading in
                guard !loading, !restored, !state.segments.isEmpty else { return }
                if let id = state.paragraphAnchor { reader.scrollTo(id, anchor: .top) }
                Task { @MainActor in
                    await Task.yield(); restored = true
                }
            }
            .onChange(of: state.segmentPageIdentity) { _, _ in restored = false }
            .overlay {
                if state.segments.isEmpty && !state.isLoadingSegments {
                    ContentUnavailableView("没有符合条件的段落", systemImage: "text.magnifyingglass")
                }
            }
            }
            SegmentPaginationBar(state: state)
        }
    }
}

private struct XLSXCompareView: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Picker("工作表", selection: Binding(
                    get: { state.segmentFilter.sheet }, set: { state.setSegmentFilter(sheet: $0) })) {
                    Text("全部工作表").tag("")
                    ForEach(state.segmentSheets, id: \.self) { Text($0).tag($0) }
                }
                .frame(width: 240)
                Spacer()
                Text("仅显示需要翻译的文本单元格；公式与数字保持原样")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 8)
            Divider()
            ScrollView {
                LazyVStack(spacing: 0) {
                    HStack {
                        Text("位置").frame(width: 130, alignment: .leading)
                        Text("原文").frame(maxWidth: .infinity, alignment: .leading)
                        Text("译文").frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .font(.headline)
                    .padding(12)
                    ForEach(state.segments) { segment in
                        HStack(alignment: .top, spacing: 12) {
                            Text(segment.context)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                                .frame(width: 130, alignment: .leading)
                            Text(segment.text)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Button {
                                state.editingSegment = segment
                            } label: {
                                Text(state.drafts.text(taskID: state.selectedTaskID ?? "", segmentID: segment.id) ?? (segment.needsTranslation ? "尚未翻译" :
                                     (segment.translation ?? "")))
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .buttonStyle(.plain)
                            .disabled(state.busy || state.isLoadingSegments ||
                                      state.selectedTask?.externalEdit == true ||
                                      ["translating", "queued"].contains(state.selectedTask?.status ?? ""))
                            .frame(maxWidth: .infinity)
                        }
                        .padding(12)
                        Divider()
                    }
                }
                .padding(.horizontal, 12)
            }
            .id(state.segmentPageIdentity)
            .overlay {
                if state.segments.isEmpty && !state.isLoadingSegments {
                    ContentUnavailableView("没有符合条件的单元格", systemImage: "text.magnifyingglass")
                }
            }
            SegmentPaginationBar(state: state)
        }
    }
}

private struct SegmentPaginationBar: View {
    @ObservedObject var state: AppState
    @State private var pageInput = "1"

    var body: some View {
        HStack(spacing: 10) {
            if state.isLoadingSegments { ProgressView().controlSize(.small) }
            Text(state.segmentTotal == 0 ? "共 0 段" :
                 "第 \(state.segmentOffset + 1)–\(state.segmentOffset + state.segments.count) 段，共 \(state.segmentTotal) 段")
                .foregroundStyle(.secondary)
            Spacer()
            Button("上一页") { Task { await state.goToSegmentPage(state.segmentPage - 1) } }
                .disabled(state.isLoadingSegments || state.segmentPage <= 1)
            Text("第")
            TextField("页码", text: $pageInput)
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.center)
                .frame(width: 52)
                .accessibilityLabel("段落页码")
                .onSubmit {
                    Task {
                        if let page = Int(pageInput) { await state.goToSegmentPage(page) }
                        pageInput = String(state.segmentPage)
                    }
                }
                .disabled(state.isLoadingSegments)
            Text("/ \(state.segmentPageCount) 页")
            Button("下一页") { Task { await state.goToSegmentPage(state.segmentPage + 1) } }
                .disabled(state.isLoadingSegments || state.segmentPage >= state.segmentPageCount)
        }
        .font(.caption)
        .padding(.horizontal, 18).padding(.vertical, 10)
        .background(.bar)
        .onAppear { pageInput = String(state.segmentPage) }
        .onChange(of: state.segmentPage) { _, page in pageInput = String(page) }
    }
}

private struct PageCompareView: View {
    @ObservedObject var state: AppState
    let task: TranslationTask
    @State private var zoom = 1.0
    @GestureState private var pinchScale: CGFloat = 1.0
    @State private var selectedPage = 0
    @State private var pageInput = ""
    @State private var restoredContinuous = false

    private static func clampedZoom(_ value: Double) -> Double {
        min(2.0, max(0.4, value))
    }

    private var currentZoom: Double {
        Self.clampedZoom(zoom * Double(pinchScale))
    }

    private var zoomBinding: Binding<Double> {
        Binding(get: { currentZoom }, set: { zoom = Self.clampedZoom($0) })
    }

    private var storedZoom: Double {
        let value = UserDefaults.standard.double(forKey: "DocTranslatorZoom-\(task.id)")
        return value == 0 ? 1.0 : Self.clampedZoom(value)
    }

    private var pinchGesture: some Gesture {
        MagnifyGesture(minimumScaleDelta: 0.01)
            .updating($pinchScale) { value, scale, _ in
                scale = value.magnification
            }
            .onEnded { value in
                zoom = Self.clampedZoom(zoom * Double(value.magnification))
            }
    }

    private func toggleZoom() {
        zoom = currentZoom > 1.05 ? 1.0 : 1.5
    }

    var body: some View {
        Group {
            if state.pagePairs.isEmpty {
                ContentUnavailableView(state.pageMessage, systemImage: "doc.viewfinder")
            } else {
                VStack(spacing: 0) {
                    HStack {
                        Picker("页面", selection: $selectedPage) {
                            Text("全部").tag(0)
                            ForEach(state.pagePairs) { pair in
                                Text("第 \(pair.number) 页").tag(pair.number)
                            }
                        }
                        .frame(width: 180)
                        Button("首页") { selectedPage = 1 }
                        Button { selectedPage = max(1, selectedPage - 1) } label: { Image(systemName: "chevron.left") }
                            .keyboardShortcut(.leftArrow, modifiers: .command)
                        TextField("页码", text: $pageInput).frame(width: 48).textFieldStyle(.roundedBorder)
                            .onSubmit {
                                if let value = Int(pageInput.trimmingCharacters(in: .whitespacesAndNewlines)), state.pagePairs.contains(where: { $0.number == value }) { selectedPage = value }
                                else { state.alertText = "请输入 1–\(state.pagePairs.count) 的页码" }
                            }
                        Button { selectedPage = min(state.pagePairs.count, max(1, selectedPage + 1)) } label: { Image(systemName: "chevron.right") }
                            .keyboardShortcut(.rightArrow, modifiers: .command)
                        Button("末页") { selectedPage = state.pagePairs.count }
                        Spacer()
                        Button {
                            zoom = Self.clampedZoom(currentZoom / 1.25)
                        } label: {
                            Image(systemName: "minus.magnifyingglass")
                        }
                        .buttonStyle(.plain)
                        .help("缩小页面 ⌘−")
                        .keyboardShortcut("-", modifiers: .command)
                        .disabled(currentZoom <= 0.4)
                        Slider(value: zoomBinding, in: 0.4...2.0)
                            .frame(width: 140)
                            .accessibilityLabel("页面缩放")
                            .help("双指捏合也可缩放页面")
                        Button {
                            zoom = Self.clampedZoom(currentZoom * 1.25)
                        } label: {
                            Image(systemName: "plus.magnifyingglass")
                        }
                        .buttonStyle(.plain)
                        .help("放大页面 ⌘+")
                        .keyboardShortcut("+", modifiers: .command)
                        .disabled(currentZoom >= 2.0)
                        Text("\(Int((currentZoom * 100).rounded()))%")
                            .font(.caption).monospacedDigit().frame(width: 45)
                        Button(task.ocrScanned == true && task.hasTranslated ?
                               "适应页面" : "适应双栏") { zoom = 1.0 }
                            .keyboardShortcut("0", modifiers: .command)
                            .disabled(abs(currentZoom - 1.0) < 0.01)
                    }
                    .padding(.horizontal, 18)
                    .padding(.vertical, 8)
                    GeometryReader { geometry in
                        let contentWidth = max(1, geometry.size.width - 36)
                        let hasTwoColumns = task.ocrScanned != true || !task.hasTranslated
                        let paneWidth = hasTwoColumns ? max(1, (contentWidth - 12) / 2) : contentWidth
                        ScrollViewReader { reader in
                        ScrollView(.vertical) {
                            LazyVStack(spacing: 18) {
                                ForEach(state.pagePairs.filter {
                                    selectedPage == 0 || $0.number == selectedPage
                                }) { pair in
                                    VStack(alignment: .leading, spacing: 8) {
                                        Text("第 \(pair.number) 页")
                                            .font(.caption.weight(.medium))
                                            .foregroundStyle(.secondary)
                                        HStack(alignment: .top, spacing: 12) {
                                            if task.ocrScanned != true || !task.hasTranslated {
                                                if pair.hasOriginal {
                                                    PageImageView(state: state, task: task,
                                                                  page: pair.number, variant: "original",
                                                                  zoom: currentZoom, paneWidth: paneWidth,
                                                                  onDoubleTap: toggleZoom)
                                                } else {
                                                    Text("原文无对应页").foregroundStyle(.secondary)
                                                        .frame(width: paneWidth, height: 260)
                                                }
                                            }
                                            if task.hasTranslated {
                                                if pair.hasTranslated {
                                                    PageImageView(state: state, task: task,
                                                                  page: pair.number, variant: "translated",
                                                                  zoom: currentZoom, paneWidth: paneWidth,
                                                                  onDoubleTap: toggleZoom)
                                                } else {
                                                    Text("译文无对应页").foregroundStyle(.secondary)
                                                        .frame(width: paneWidth, height: 260)
                                                }
                                            } else {
                                                Text("等待译文")
                                                    .frame(width: paneWidth, height: 260)
                                                    .foregroundStyle(.secondary)
                                            }
                                        }
                                    }
                                    .frame(width: contentWidth, alignment: .leading)
                                    .id(pair.number)
                                    .background(GeometryReader { geometry in
                                        Color.clear.preference(key: ParagraphPositions.self,
                                            value: [String(pair.number): geometry.frame(in: .named("page-reader"))])
                                    })
                                }
                            }
                            .padding(18)
                        }
                        .coordinateSpace(name: "page-reader")
                        .id(selectedPage)
                        .onAppear {
                            if selectedPage == 0, let page = state.reading.bookmark(for: task.id).continuousPage {
                                reader.scrollTo(min(page, state.pagePairs.count), anchor: .top)
                            }
                            Task { @MainActor in await Task.yield(); restoredContinuous = true }
                        }
                        .onPreferenceChange(ParagraphPositions.self) { positions in
                            guard selectedPage == 0, restoredContinuous else { return }
                            if let key = positions.filter({ $0.value.maxY > 0 }).min(by: { $0.value.minY < $1.value.minY })?.key,
                               let page = Int(key) { state.recordVisiblePage(taskID: task.id, page: page) }
                        }
                        .simultaneousGesture(pinchGesture)
                        .background(TrackpadSmartZoom(onSmartZoom: toggleZoom))
                        }
                    }
                }
            }
        }
        .onAppear {
            zoom = storedZoom
            let savedPage = UserDefaults.standard.integer(forKey: "DocTranslatorSelectedPage-\(task.id)")
            selectedPage = state.pagePairs.isEmpty ? savedPage : min(savedPage, state.pagePairs.count)
        }
        .onChange(of: zoom) { _, value in
            UserDefaults.standard.set(value, forKey: "DocTranslatorZoom-\(task.id)")
            state.recordPages(taskID: task.id, page: selectedPage, zoom: value)
        }
        .onChange(of: selectedPage) { _, page in
            UserDefaults.standard.set(page, forKey: "DocTranslatorSelectedPage-\(task.id)")
            pageInput = page > 0 ? String(page) : ""
            state.recordPages(taskID: task.id, page: page, zoom: currentZoom)
        }
        .onChange(of: state.pageNavigation?.token) { _, _ in
            if let request = state.pageNavigation, request.taskID == task.id {
                selectedPage = request.page
            }
        }
        .onChange(of: state.pagePairs.count) { _, count in
            if count > 0 && selectedPage > count { selectedPage = count }
        }
    }

}

private struct TrackpadSmartZoom: NSViewRepresentable {
    let onSmartZoom: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onSmartZoom: onSmartZoom) }

    func makeNSView(context: Context) -> NSView {
        let view = NSView(frame: .zero)
        let coordinator = context.coordinator
        coordinator.view = view
        coordinator.monitor = NSEvent.addLocalMonitorForEvents(matching: .smartMagnify) {
            [weak coordinator] event in
            guard let coordinator, let view = coordinator.view,
                  view.window == event.window,
                  view.bounds.contains(view.convert(event.locationInWindow, from: nil)) else {
                return event
            }
            coordinator.onSmartZoom()
            return nil
        }
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        context.coordinator.onSmartZoom = onSmartZoom
    }

    static func dismantleNSView(_ view: NSView, coordinator: Coordinator) {
        if let monitor = coordinator.monitor { NSEvent.removeMonitor(monitor) }
        coordinator.monitor = nil
    }

    final class Coordinator {
        weak var view: NSView?
        var monitor: Any?
        var onSmartZoom: () -> Void

        init(onSmartZoom: @escaping () -> Void) {
            self.onSmartZoom = onSmartZoom
        }
    }
}

private struct PageImageView: View {
    @ObservedObject var state: AppState
    let task: TranslationTask
    let page: Int
    let variant: String
    let zoom: Double
    let paneWidth: CGFloat
    let onDoubleTap: () -> Void
    @State private var image: NSImage?
    @State private var failed = false
    @State private var loading = false
    @State private var loadToken = UUID()
    @State private var retry = 0
    @State private var imageAspectRatio: CGFloat = 1.414

    var body: some View {
        Group {
            if let image {
                ScrollView(.horizontal) {
                    Image(nsImage: image).resizable().aspectRatio(contentMode: .fit)
                        .frame(width: paneWidth * zoom)
                        .frame(width: max(paneWidth, paneWidth * zoom))
                }
                .frame(width: paneWidth)
                .onTapGesture(count: 2, perform: onDoubleTap)
                .help("双指滚动查看页面；双指捏合缩放；双指轻点两次或双击切换放大和适应")
            } else if failed {
                Button("重试载入第 \(page) 页") {
                    retry += 1
                }
                .frame(width: paneWidth, height: paneWidth * zoom * imageAspectRatio)
            } else {
                ProgressView().frame(width: paneWidth, height: paneWidth * zoom * imageAspectRatio)
            }
        }
        .frame(width: paneWidth)
        .background(.white, in: RoundedRectangle(cornerRadius: 4))
        .shadow(color: .black.opacity(0.12), radius: 6, y: 2)
        .task(id: "\(task.id)-\(task.revision ?? 0)-\(variant)-\(page)-\(retry)") {
            await loadImage()
        }
        .onDisappear {
            loadToken = UUID()
            loading = false
            image = nil
        }
    }

    private func loadImage() async {
        let token = UUID()
        loadToken = token
        loading = true
        defer { if loadToken == token { loading = false } }
        failed = false
        let result = await state.pageImage(taskId: task.id, ext: task.ext,
                                          page: page, variant: variant)
        guard loadToken == token, !Task.isCancelled else { return }
        image = result
        if let result, result.size.width > 0 { imageAspectRatio = result.size.height / result.size.width }
        failed = image == nil
    }
}

private struct SegmentEditorView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var state: AppState
    let segment: Segment
    @State private var editedText: String
    @State private var taskID: String
    @State private var expectedRevision: Int?

    init(state: AppState, segment: Segment) {
        self.state = state
        self.segment = segment
        _editedText = State(initialValue: state.drafts.text(taskID: state.selectedTaskID ?? "", segmentID: segment.id) ?? segment.translation ?? "")
        _taskID = State(initialValue: state.selectedTaskID ?? "")
        _expectedRevision = State(initialValue: state.drafts.revisions[state.selectedTaskID ?? ""] ?? state.segmentRevision)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("修订译文").font(.title2.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(segment.text).textSelection(.enabled)
                    if state.drafts.text(taskID: taskID, segmentID: segment.id) != nil {
                        Divider(); Text("当前已保存译文").font(.caption).foregroundStyle(.secondary)
                        Text(segment.translation ?? "尚未翻译").font(.callout).textSelection(.enabled)
                    }
                    if let context = segment.meta?.translationContext, context != segment.text {
                        Divider()
                        Text("原文上下文").font(.caption).foregroundStyle(.secondary)
                        Text(context).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
                .frame(height: 160).clipped().padding(12)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
            TextEditor(text: Binding(get: { editedText }, set: { value in
                editedText = value
                if value == (segment.translation ?? "") { state.drafts.remove(taskID: taskID, segmentID: segment.id) }
                else { state.drafts.set(value, taskID: taskID, segmentID: segment.id, revision: expectedRevision) }
            }))
                .font(.body)
                .frame(height: 200)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
            HStack {
                Text(state.drafts.saveError ?? "草稿自动保存；保存到译文后更新导出文件")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("放弃修改") { state.drafts.remove(taskID: taskID, segmentID: segment.id); dismiss() }
                Button("关闭") { dismiss() }
                Button("保存") {
                    Task {
                        if await state.revise(segment, text: editedText, taskID: taskID,
                                              expectedRevision: expectedRevision) { dismiss() }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(editedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || state.busy)
            }
        }
        .padding(22)
        .frame(width: 660, height: 550)
    }
}

private struct OCRSegmentEditorView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var state: AppState
    let segment: Segment
    @State private var editedText: String
    @State private var taskID: String
    @State private var expectedRevision: Int?
    @State private var sourceImage: NSImage?
    @State private var sourceLoading = true

    init(state: AppState, segment: Segment) {
        self.state = state
        self.segment = segment
        _editedText = State(initialValue: segment.text)
        _taskID = State(initialValue: state.selectedTaskID ?? "")
        _expectedRevision = State(initialValue: state.segmentRevision)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("校正本机 OCR 文字").font(.title2.bold())
            Text("请先核对扫描页的识别结果，再开始翻译。")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 6) {
                Text("第 \(segment.meta?.page ?? 1) 页的原文区域").font(.caption).foregroundStyle(.secondary)
                ZStack {
                    if let sourceImage {
                        Image(nsImage: sourceImage).resizable().scaledToFit()
                    } else if sourceLoading {
                        ProgressView()
                    } else {
                        Text("原图暂不可用，可在页面对照中查看").font(.caption).foregroundStyle(.secondary)
                    }
                }
                .frame(height: 115).frame(maxWidth: .infinity).background(.white)
            }
            TextEditor(text: $editedText)
                .font(.body)
                .frame(height: 150)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
            HStack {
                if let confidence = segment.meta?.confidence {
                    Text("识别可信度 \(Int(confidence * 100))%")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("取消") { dismiss() }
                Button("保存") {
                    Task {
                        if await state.reviseOCR(segment, text: editedText, taskID: taskID,
                                                 expectedRevision: expectedRevision) { dismiss() }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(state.busy || editedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(22)
        .frame(width: 600, height: 560)
        .task {
            defer { sourceLoading = false }
            if let page = segment.meta?.page,
               let image = await state.pageImage(taskId: taskID, ext: ".pdf", page: page, variant: "original") {
                let box = segment.meta?.normalizedBbox ?? segment.meta?.bbox
                sourceImage = box.flatMap { OCRService.previewCrop(image, bbox: $0) } ?? image
            }
        }
    }
}

private struct OCRPageReference: Identifiable {
    let taskID: String
    let page: Int
    var id: String { "\(taskID)-\(page)" }
}

private struct OCRPageTextEditor: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var state: AppState
    let reference: OCRPageReference
    @State private var text = ""
    @State private var sourceImage: NSImage?
    @State private var sourceLoading = true

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("补录第 \(reference.page) 页文字").font(.title2.bold())
            Text("按阅读顺序输入未能识别的文字。原始页面会保留在译文中。")
                .font(.caption).foregroundStyle(.secondary)
            ZStack {
                if let sourceImage {
                    Image(nsImage: sourceImage).resizable().scaledToFit()
                } else if sourceLoading {
                    ProgressView()
                } else {
                    Text("可返回页面对照查看原页").font(.caption).foregroundStyle(.secondary)
                }
            }
            .frame(height: 150).frame(maxWidth: .infinity).background(.white)
            TextEditor(text: $text).font(.body).frame(height: 150)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
            HStack {
                Spacer()
                Button("取消") { dismiss() }
                Button("保存文字") {
                    Task {
                        if await state.addOCRPageText(taskId: reference.taskID, page: reference.page, text: text) {
                            dismiss()
                        }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(state.busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(22).frame(width: 640, height: 560)
        .task {
            defer { sourceLoading = false }
            sourceImage = await state.pageImage(taskId: reference.taskID, ext: ".pdf",
                                               page: reference.page, variant: "original")
        }
    }
}

private struct ProviderSettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var state: AppState
    @StateObject private var editor: ProviderEditor
    init(state: AppState) { self.state = state; _editor = StateObject(wrappedValue: ProviderEditor(state: state)) }
    @State private var providerToDelete: Provider?
    @State private var retentionChoice = 0
    @State private var memoryChoice = true
    @State private var showingClearMemory = false
    @State private var glossaryStatus: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Text("设置").font(.title2.bold())
                    Spacer()
                    Button("完成") { Task { if await state.confirmSettingsTransition() { editor.cancel(); dismiss() } } }
                }
                Text("文档保留在本机。API Key 存入系统钥匙串或本机加密存储。")
                    .font(.caption).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 8) {
                    Text("任务保留期限").font(.headline)
                    HStack {
                        Picker("自动清理", selection: $retentionChoice) {
                            Text("永久保留").tag(0)
                            Text("30 天").tag(30)
                            Text("90 天").tag(90)
                        }
                        .frame(width: 250)
                        Button("应用") { Task { await state.saveRetention(retentionChoice) } }
                            .disabled(retentionChoice == state.taskRetentionDays)
                    }
                    Text("旧任务移入可恢复的废纸篓；页面缓存独立清理。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    Text("术语表与翻译记忆").font(.headline)
                    ForEach($state.glossary) { $term in
                        HStack {
                            TextField("原文术语", text: $term.source)
                            TextField("指定译法", text: $term.target)
                            Button {
                                state.glossary.removeAll { $0.id == term.id }
                            } label: { Image(systemName: "minus.circle") }
                            .buttonStyle(.plain)
                        }
                    }
                    HStack {
                        Button("添加术语") {
                            state.glossary.append(GlossaryTerm(source: "", target: ""))
                        }
                        Button("保存术语表") {
                            Task {
                                if await state.saveGlossary() {
                                    glossaryStatus = "术语表已保存"
                                }
                            }
                        }
                        Spacer()
                    }
                    if let glossaryStatus {
                        Label(glossaryStatus, systemImage: "checkmark.circle.fill")
                            .font(.caption).foregroundStyle(.green)
                    }
                    HStack {
                        Toggle("使用本机翻译记忆", isOn: $memoryChoice)
                        Button("应用") {
                            Task { await state.setTranslationMemoryEnabled(memoryChoice) }
                        }
                        .disabled(memoryChoice == state.useTranslationMemory)
                    }
                    Text("已记住 \(state.memoryCount) 条原译对照，人工修订会优先复用。")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("清空翻译记忆") { showingClearMemory = true }
                        .disabled(state.memoryCount == 0)
                }
                Divider()
                if !state.providers.isEmpty {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("已添加模型").font(.headline)
                        ForEach(state.providers) { provider in
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(provider.name).fontWeight(.medium)
                                    Text("\(provider.model) · \(provider.baseUrl)")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("测试") { Task {
                                    if await state.confirmSettingsTransition() { editor.load(provider); editor.test() }
                                } }
                                Button("编辑") { Task {
                                    if await state.confirmSettingsTransition() { editor.load(provider) }
                                } }
                                Button("删除") { providerToDelete = provider }
                                    .foregroundStyle(.red)
                            }
                            .padding(8)
                            .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
                        }
                    }
                }
                Divider()
                Text(editor.selectedID == nil ? "添加模型" : "编辑模型").font(.headline)
                Form {
                    TextField("名称", text: $editor.name, prompt: Text("例如 DeepSeek"))
                    TextField("Base URL", text: $editor.baseURL, prompt: Text("https://api.example.com/v1"))
                    TextField("Model", text: $editor.model, prompt: Text("模型名称"))
                    SecureField(editor.selectedID == nil ? "API Key" : "新 API Key（留空保持）",
                                text: $editor.key)
                    TextField("输入价 / 百万 Token (USD)", text: $editor.inputPriceText)
                    TextField("输出价 / 百万 Token (USD)", text: $editor.outputPriceText)
                }
                .formStyle(.grouped)
                Text("填写服务根地址，通常到 /v1 为止；粘贴完整 /chat/completions 地址也会自动整理。")
                    .font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 12) {
                    Button("获取模型列表") { editor.test(discover: true) }
                        .disabled(editor.baseURL.isEmpty || editor.isSaving)
                    if !editor.suggestions.isEmpty {
                        Menu("选择模型") {
                            ForEach(editor.suggestions.prefix(100), id: \.self) { value in
                                Button(value) { editor.model = value }
                            }
                        }
                    }
                    if editor.isTesting {
                        Button("取消测试") { editor.cancel() }
                    } else {
                        Button("测试连接") { editor.test() }
                            .disabled(editor.baseURL.isEmpty || editor.model.isEmpty || editor.isSaving)
                    }
                    Button(editor.selectedID == nil ? "保存模型" : "保存修改") { Task { _ = await editor.save() } }
                        .buttonStyle(.borderedProminent).disabled(!editor.valid || editor.isSaving)
                    Button("新建 / 清空") { Task {
                        if await state.confirmSettingsTransition() { editor.load(nil) }
                    } }
                }
                if let draftTestMessage = editor.feedback {
                    Label(draftTestMessage,
                          systemImage: editor.succeeded ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(editor.succeeded ? .green : .red)
                        .font(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(22)
        }
        .frame(width: 640, height: 650)
        .interactiveDismissDisabled(state.settingsDirty || editor.isSaving)
        .onDisappear { editor.cancel(); state.settingsDirty = false; state.saveSettingsBeforeClose = nil }
        .onAppear {
            state.saveSettingsBeforeClose = { await editor.save() }
            retentionChoice = state.taskRetentionDays
            memoryChoice = state.useTranslationMemory
        }
        .confirmationDialog("删除模型？", isPresented: Binding(
            get: { providerToDelete != nil },
            set: { if !$0 { providerToDelete = nil } }
        )) {
            if let providerToDelete {
                Button("删除 \(providerToDelete.name)", role: .destructive) {
                    Task { await state.deleteProvider(providerToDelete.id) }
                    self.providerToDelete = nil
                }
            }
        } message: { Text("此操作会从本机移除该模型的 API Key。") }
        .confirmationDialog("清空翻译记忆？", isPresented: $showingClearMemory) {
            Button("清空", role: .destructive) {
                Task { await state.clearTranslationMemory() }
            }
        } message: { Text("术语表和已完成的译文文件不会受影响。") }
    }
}

private struct TrashView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("废纸篓").font(.title2.bold())
                Spacer()
                Button("完成") { dismiss() }
            }
            Text("移入的任务会保留 30 天，可在此恢复。")
                .font(.caption).foregroundStyle(.secondary)
            if state.trashItems.isEmpty {
                ContentUnavailableView("废纸篓为空", systemImage: "trash")
            } else {
                List(state.trashItems) { item in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(item.filename)
                            if let deletedAt = item.deletedAt {
                                Text(Date(timeIntervalSince1970: deletedAt).formatted())
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        Button("恢复") { Task { await state.restoreTask(item.id) } }
                    }
                }
            }
        }
        .padding(22)
        .frame(width: 520, height: 440)
    }
}

private struct OutputConflictView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("译文文件在 App 外被修改", systemImage: "exclamationmark.triangle.fill")
                .font(.title2.bold())
            Text("为了保护 GenOffice 中的编辑，App 已停止自动重建译文。请选择要保留的版本。")
            VStack(alignment: .leading, spacing: 10) {
                Button("查看已保留的版本") { state.revealVersions() }
                Button("保留 GenOffice 版本") {
                    Task { await state.resolveConflict("keep_external") }
                }
                Text("现有文件保持原样；可继续导出，但应用内逐段修订会停用。")
                    .font(.caption).foregroundStyle(.secondary)
                Button("备份外部版本，然后重建应用译文") {
                    Task { await state.resolveConflict("rebuild") }
                }
                Text("先把外部文件存入任务历史版本，再根据当前逐段译文生成新文件。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            HStack { Spacer(); Button("稍后处理") { dismiss() } }
        }
        .padding(24)
        .frame(width: 560, height: 340)
    }
}
