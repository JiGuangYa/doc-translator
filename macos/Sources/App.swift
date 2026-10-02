import AppKit
import SwiftUI
import UniformTypeIdentifiers

@main
struct DocTranslatorMacApp: App {
    @StateObject private var model = AppModel()
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    var body: some Scene {
        Window("文档翻译", id: "main") {
            ContentView(model: model)
                .frame(minWidth: 1000, minHeight: 670)
                .task { delegate.model = model; await model.boot(); await SmokeChecks.runIfRequested(model) }
                .onReceive(NotificationCenter.default.publisher(for: .quitApplication)) { _ in
                    delegate.requestTermination(NSApplication.shared)
                }
        }
        .windowStyle(.titleBar)
        .defaultSize(width: 1180, height: 780)
        .commands {
            CommandGroup(replacing: .appTermination) {
                Button("退出文档翻译") { delegate.requestTermination(NSApplication.shared) }
                    .keyboardShortcut("q").disabled(model.closing)
            }
            CommandGroup(replacing: .newItem) {
                Button("导入文档…") { NotificationCenter.default.post(name: .importDocument, object: nil) }
                    .keyboardShortcut("o")
            }
            CommandGroup(replacing: .appSettings) {
                Button("API 设置…") { NotificationCenter.default.post(name: .openProviders, object: nil) }
                    .keyboardShortcut(",")
            }
            CommandGroup(after: .textEditing) {
                Button("查找原文与译文…") { NotificationCenter.default.post(name: .findParagraph, object: nil) }
                    .keyboardShortcut("f")
            }
        }
    }
}

extension Notification.Name {
    static let importDocument = Notification.Name("importDocument")
    static let openProviders = Notification.Name("openProviders")
    static let findParagraph = Notification.Name("findParagraph")
    static let quitApplication = Notification.Name("quitApplication")
    static let closeProvidersForQuit = Notification.Name("closeProvidersForQuit")
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?
    private var terminating = false
    private var mayTerminate = false
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if mayTerminate { return .terminateNow }
        requestTermination(sender)
        return .terminateCancel
    }

    func requestTermination(_ sender: NSApplication) {
        guard !terminating else { return }
        terminating = true
        // Keep the normal run loop active while Swift concurrency drains the engine.
        Task {
            let hadSettings = model?.providerEditor != nil
            if let model, !(await model.prepareToQuit()) { terminating = false; return }
            if hadSettings {
                NotificationCenter.default.post(name: .closeProvidersForQuit, object: nil)
                // AppKit ignores terminate while a modal settings sheet is still
                // presented. Unsaved changes have been handled above.
                for window in sender.windows {
                    if let sheet = window.attachedSheet { window.endSheet(sheet); sheet.orderOut(nil) }
                }
            }
            await model?.shutdown()
            mayTerminate = true
            sender.terminate(nil)
        }
    }
}

struct ContentView: View {
    private struct LibraryResultsID: Hashable {
        let filter: LibraryFilter
        let query: String
    }

    @ObservedObject var model: AppModel
    @State private var showingImporter = false
    @State private var showingProviders = false
    @State private var dropTargeted = false
    @FocusState private var paragraphSearchFocused: Bool

    var body: some View {
        Group {
            if !model.ready {
                VStack(spacing: 18) {
                    Image(systemName: "doc.text.magnifyingglass").font(.system(size: 48)).foregroundStyle(.tint)
                    Text("文档翻译").font(.largeTitle.bold())
                    if let message = model.startupError {
                        Text(message).foregroundStyle(.secondary)
                        HStack {
                            Button("重试") { Task { await model.boot() } }.buttonStyle(.borderedProminent)
                            Button("查看日志") { model.backend.showLogs() }
                        }
                    } else {
                        ProgressView()
                        Text(model.closing ? "正在保存进度并退出…" : "正在启动翻译引擎…").foregroundStyle(.secondary)
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                NavigationSplitView {
                    sidebar
                } detail: {
                    detail
                }
                .toolbar {
                    ToolbarItemGroup {
                        Button { showingImporter = true } label: {
                            Label("导入文档", systemImage: "plus")
                        }
                        Button { showingProviders = true } label: {
                            Label("API 设置", systemImage: "gearshape")
                        }
                    }
                }
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onOpenURL { model.importFiles([$0]) }
        .dropDestination(for: URL.self) { urls, _ in
            guard !model.closing else { return false }
            model.importFiles(urls)
            return !urls.isEmpty
        } isTargeted: { dropTargeted = $0 }
        .overlay {
            if dropTargeted {
                RoundedRectangle(cornerRadius: 12).stroke(Color.accentColor, lineWidth: 3).padding(6)
                    .allowsHitTesting(false)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .importDocument)) { _ in
            if model.ready { showingImporter = true }
        }
        .onReceive(NotificationCenter.default.publisher(for: .openProviders)) { _ in
            if model.ready { showingProviders = true }
        }
        .onReceive(NotificationCenter.default.publisher(for: .closeProvidersForQuit)) { _ in showingProviders = false }
        .onReceive(NotificationCenter.default.publisher(for: .findParagraph)) { _ in
            if model.ready, model.selectedTaskID != nil { model.mode = 0; paragraphSearchFocused = true }
        }
        .safeAreaInset(edge: .bottom) {
            ImportStatusView(queue: model.imports)
            ReadingSaveStatusView(reading: model.reading)
            if model.busy {
                HStack { ProgressView().controlSize(.small); Text(model.busyLabel).font(.caption); Spacer() }
                    .padding(10).background(.bar)
            }
        }
        .fileImporter(
            isPresented: $showingImporter,
            allowedContentTypes: [.pdf, UTType(filenameExtension: "docx")!, UTType(filenameExtension: "pptx")!, UTType(filenameExtension: "xlsx")!],
            allowsMultipleSelection: true
        ) { result in
            if case .success(let urls) = result { model.importFiles(urls) }
            if case .failure(let error) = result { model.errorMessage = error.localizedDescription }
        }
        .sheet(isPresented: $showingProviders) { ProviderSettingsView(model: model) }
        .alert("发生错误", isPresented: Binding(
            get: { model.errorMessage != nil },
            set: { if !$0 { model.errorMessage = nil } }
        )) {
            Button("确定", role: .cancel) { model.errorMessage = nil }
        } message: {
            Text(model.errorMessage ?? "")
        }
    }

    private var sidebar: some View {
        let visible = DocumentSearch.tasks(model.tasks, query: model.libraryQuery, filter: model.libraryFilter)
        return VStack(spacing: 0) {
            HStack {
                Text("文档").font(.headline)
                Spacer()
                Button { showingImporter = true } label: { Image(systemName: "plus") }
                    .buttonStyle(.borderless)
            }
            .padding()
            TextField("搜索文档名称", text: $model.libraryQuery)
                .textFieldStyle(.roundedBorder).padding(.horizontal).padding(.bottom, 8)
                .accessibilityLabel("搜索历史文档")
            Picker("状态", selection: $model.libraryFilter) {
                ForEach(LibraryFilter.allCases) { Text($0.rawValue).tag($0) }
            }.padding(.horizontal).padding(.bottom, 8)
            List(visible, selection: Binding<String?>(get: {
                visible.contains(where: { $0.id == model.selectedTaskID }) ? model.selectedTaskID : nil
            }, set: { id in
                // Filtering may remove the highlighted row. Keep the open
                // document until another row is explicitly selected.
                guard let id, id != model.selectedTaskID else { return }
                Task { await model.selectTask(id) }
            })) { task in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(task.filename).lineLimit(2).fontWeight(task.id == model.selectedTaskID ? .semibold : .regular)
                        HStack {
                            Text(statusName(task.status))
                            Spacer()
                            Text(task.ext.uppercased().replacingOccurrences(of: ".", with: ""))
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        if task.status == "translating" { ProgressView(value: task.progress) }
                    }
                    .padding(.vertical, 5)
                    .contentShape(Rectangle())
                .tag(task.id)
                .contextMenu { archiveButton(task) }
            }
            .listStyle(.sidebar)
            // A new search starts a new result list. Reusing the macOS table
            // across filtered row diffs can reenter automatic row-height
            // calculation. The open document remains owned by the model.
            .id(LibraryResultsID(filter: model.libraryFilter, query: model.libraryQuery))
            .overlay {
                if visible.isEmpty {
                    Text(model.tasks.isEmpty ? "尚无文档\n可拖入文件或点击导入" : "没有匹配的文档")
                        .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
            }
            Text("\(visible.count) / \(model.tasks.count) 份文档").font(.caption).foregroundStyle(.secondary).padding(8)
        }
        .navigationSplitViewColumnWidth(min: 220, ideal: 270, max: 350)
    }

    @ViewBuilder
    private var detail: some View {
        if let task = model.selectedTask {
            VStack(spacing: 0) {
                taskHeader(task)
                Divider()
                Picker("视图", selection: $model.mode) {
                        Text("段落对照").tag(0)
                        Text("页面预览").tag(1)
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 250)
                    .padding(12)
                if model.mode == 1 {
                    pageView(task)
                } else {
                    segmentView(task)
                }
            }
        } else {
            ContentUnavailableView("开始翻译", systemImage: "doc.text", description: Text("导入 PDF、Word、PowerPoint 或 Excel 文档"))
        }
    }

    private func taskHeader(_ task: TranslationTask) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.filename).font(.title2.bold()).lineLimit(1)
                    Text("\(statusName(task.status)) · \(task.segmentCount ?? 0) 个段落")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                archiveButton(task)
                if task.hasTranslated {
                    Button { Task { await model.exportTranslation() } } label: {
                        Label("导出译文", systemImage: "square.and.arrow.up")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(task.status == "translating" || model.busy || model.hasDraftSaveInFlight(task.id))
                }
            }
            if task.isArchived {
                Label("已归档 · 原文、译文与修订草稿均保留，恢复后可继续翻译", systemImage: "archivebox")
                    .font(.caption).foregroundStyle(.secondary)
            } else if task.status == "translating" {
                HStack {
                    ProgressView(value: task.progress)
                    Text("\(task.doneSegments ?? 0)/\(task.totalSegments ?? task.segmentCount ?? 0)")
                        .monospacedDigit().font(.caption)
                    Button(model.cancellingIDs.contains(task.id) ? "正在取消…" : "取消") { Task { await model.cancelTranslation() } }
                        .disabled(model.cancellingIDs.contains(task.id))
                }
            } else if task.untranslatedCount > 0 {
                HStack {
                    if task.providerId == nil {
                    Picker("模型", selection: $model.selectedProviderID) {
                        Text("请选择 API").tag("")
                        ForEach(model.providers) { provider in
                            Text(provider.name).tag(provider.id)
                        }
                    }
                    .frame(maxWidth: 260)
                    Picker("目标语言", selection: $model.targetLanguage) {
                        Text("简体中文").tag("zh-CN")
                        Text("English").tag("en")
                        Text("日本語").tag("ja")
                        Text("繁體中文").tag("zh-TW")
                        Text("한국어").tag("ko")
                    }
                    .frame(maxWidth: 210)
                    } else {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("继续使用原任务的 API 与目标语言")
                            if let snapshot = task.providerSnapshot {
                                Text("\(snapshot.name) · \(snapshot.model) → \(task.targetLang ?? "")")
                            }
                        }.font(.caption).foregroundStyle(.secondary)
                    }
                    Button(model.startingIDs.contains(task.id) ? "正在开始…" : (task.providerId == nil ? "开始翻译" : "继续翻译")) { Task { await model.startTranslation() } }
                        .buttonStyle(.borderedProminent)
                        .disabled((task.providerId ?? model.selectedProviderID).isEmpty || model.startingIDs.contains(task.id) || model.hasDraftSaveInFlight(task.id))
                    if model.providers.isEmpty {
                        Button("添加 API") { showingProviders = true }
                    }
                }
            }
            if let error = task.error, task.status == "failed" {
                Text(error).font(.caption).foregroundStyle(.red)
            }
            if task.needsLayoutReview {
                HStack {
                    Label("有 \(task.overflowCount ?? 0) 处译文放不进原版式，导出时保留了原文。可缩短译文后重新保存。", systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange)
                    Button("查看段落") { model.mode = 0; model.paragraphQuery = ""; model.paragraphFilter = .layout }
                }
            }
            ForEach(task.warnings, id: \.self) { warning in
                Text(warning).font(.caption).foregroundStyle(.orange)
            }
        }
        .padding(18)
    }

    private func archiveButton(_ task: TranslationTask) -> some View {
        Button {
            Task { await model.setArchived(task, archived: !task.isArchived) }
        } label: {
            Label(task.isArchived ? "恢复文档" : "归档文档", systemImage: task.isArchived ? "arrow.uturn.backward" : "archivebox")
        }
        .disabled(task.status == "translating" || model.archivingIDs.contains(task.id) || model.startingIDs.contains(task.id))
        .help(task.isArchived ? "恢复到文档列表" : "保留文件和草稿，移至已归档列表")
    }

    private func segmentView(_ task: TranslationTask) -> some View {
        ParagraphReaderView(model: model, drafts: model.drafts, task: task, searchFocused: $paragraphSearchFocused)
            .id(task.id)
    }

    private func pageView(_ task: TranslationTask) -> some View {
        VStack(spacing: 8) {
            if let error = model.pageError {
                ContentUnavailableView("页面载入失败", systemImage: "exclamationmark.triangle", description: Text(error))
                Button("重试") { Task { await model.preparePages() } }.padding()
            } else if model.renderStatus?.status == "unavailable" {
                ContentUnavailableView("需要 LibreOffice", systemImage: "doc.richtext", description: Text("安装 LibreOffice 后可查看 Office 文档的原版式双栏页面。段落对照仍可使用。"))
            } else if model.renderStatus?.status == "failed" {
                ContentUnavailableView("页面预览失败", systemImage: "exclamationmark.triangle", description: Text(model.renderStatus?.error ?? ""))
                Button("重试") { Task { await model.preparePages() } }.padding()
            } else if model.renderStatus?.status == "ready", model.pageCount > 0 {
                PageNavigationView(model: model)
                GeometryReader { geometry in
                    let width = max(200, (geometry.size.width - 48) / 2) * model.pageZoom
                    ScrollView([.vertical, .horizontal]) {
                        HStack(alignment: .top, spacing: 16) {
                            pageImage(model.originalPage, label: "原文", width: width, exists: model.pageNumber <= model.originalPageCount)
                            pageImage(model.translatedPage, label: "译文", width: width, exists: model.pageNumber <= model.translatedPageCount,
                                      missingText: task.hasTranslated ? nil : "翻译完成后将在这里显示译文页面")
                        }.padding(16)
                    }
                    .id("page-\(task.id)-\(model.pageNumber)")
                    .defaultScrollAnchor(.topLeading)
                    .background(Color(nsColor: .underPageBackgroundColor))
                }
            } else {
                ProgressView("正在生成页面预览…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func pageImage(_ image: NSImage?, label: String, width: CGFloat, exists: Bool, missingText: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label).font(.caption.bold())
            if let image {
                Image(nsImage: image).resizable().scaledToFit().frame(width: width)
                    .background(.white).shadow(radius: 3)
            } else if !exists {
                Text(missingText ?? "这一侧没有第 \(model.pageNumber) 页").foregroundStyle(.secondary)
                    .frame(width: width, height: 300)
            } else {
                ProgressView().frame(width: width, height: 400)
            }
        }
    }

    private func statusName(_ status: String) -> String {
        switch status {
        case "uploaded", "pending_confirm", "parsing": return "待翻译"
        case "translating": return "翻译中"
        case "done": return "已完成"
        case "failed": return "失败"
        case "cancelled": return "已取消"
        default: return status
        }
    }
}

private struct ParagraphReaderView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var drafts: RevisionDraftStore
    let task: TranslationTask
    var searchFocused: FocusState<Bool>.Binding
    @State private var restoredFor: ParagraphRestoreTrigger?

    var body: some View {
        let visible = DocumentSearch.segments(model.segments, query: model.paragraphQuery,
                                               filter: model.paragraphFilter, drafts: drafts.drafts[task.id] ?? [:], overflowIDs: model.overflowSegmentIDs)
        let restore = ParagraphRestoreTrigger(loading: model.segmentLoading, empty: model.segments.isEmpty,
                                               query: model.paragraphQuery, filter: model.paragraphFilter)
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("搜索原文、译文或草稿", text: $model.paragraphQuery)
                    .textFieldStyle(.roundedBorder).focused(searchFocused)
                    .accessibilityLabel("搜索文档段落")
                if !model.paragraphQuery.isEmpty {
                    Button { model.paragraphQuery = "" } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.plain).help("清除搜索")
                }
                Picker("范围", selection: $model.paragraphFilter) {
                    ForEach(ParagraphFilter.allCases) { Text($0.rawValue).tag($0) }
                }.frame(width: 160)
                Text("\(visible.count) / \(model.segments.count) 段").font(.caption).monospacedDigit().foregroundStyle(.secondary)
            }.padding(.horizontal, 18).padding(.vertical, 8)
            DraftSummaryView(model: model, drafts: drafts, task: task)
            ScrollViewReader { proxy in
              ScrollView {
                LazyVStack(spacing: 0) {
                    HStack {
                        Text("原文").frame(maxWidth: .infinity, alignment: .leading)
                        Divider()
                        Text("译文 · 点击文字可修改").frame(maxWidth: .infinity, alignment: .leading)
                    }.font(.caption.bold()).foregroundStyle(.secondary).padding(.horizontal, 22).padding(.vertical, 10)
                    ForEach(visible) { segment in
                        SegmentRow(drafts: drafts, taskID: task.id, segment: segment,
                                   editable: task.hasTranslated && task.status != "translating",
                                   saving: model.savingAllDraftTaskIDs.contains(task.id) || model.savingDraftIDs.contains(task.id + ":" + segment.id)) { text in
                            await model.revise(taskID: task.id, segment: segment, text: text)
                        }
                        .id(segment.id)
                        .background(GeometryReader { geometry in
                            Color.clear.preference(key: ParagraphFrames.self,
                                                   value: [segment.id: geometry.frame(in: .named("paragraph-reader"))])
                        })
                    }
                }
              }
              .coordinateSpace(name: "paragraph-reader")
              .onPreferenceChange(ParagraphFrames.self) { frames in
                  guard restoredFor == restore else { return }
                  let first = frames.filter { $0.value.maxY > 1 }.min { $0.value.minY < $1.value.minY }
                  model.recordParagraphPosition(first?.key, taskID: task.id)
              }
              .task(id: restore) {
                  restoredFor = nil
                  guard !restore.loading, !restore.empty, restore.query.isEmpty, restore.filter == .all else { return }
                  let anchor = model.paragraphAnchor
                  await Task.yield()
                  guard !Task.isCancelled, model.selectedTaskID == task.id else { return }
                  if let anchor, model.segments.contains(where: { $0.id == anchor }) { proxy.scrollTo(anchor, anchor: .top) }
                  await Task.yield()
                  guard !Task.isCancelled else { return }
                  restoredFor = restore
              }
            }
            .overlay {
                if model.segmentLoading { ProgressView("正在载入段落…") }
                else if visible.isEmpty {
                    ContentUnavailableView(model.segments.isEmpty ? "没有可翻译的段落" : "没有匹配的段落",
                                           systemImage: "text.magnifyingglass",
                                           description: Text(model.segments.isEmpty ? "扫描文档需要先做 OCR" : "尝试其他关键词或选择全部段落"))
                }
            }
        }
    }
}

private struct ParagraphRestoreTrigger: Hashable {
    let loading: Bool
    let empty: Bool
    let query: String
    let filter: ParagraphFilter
}

private struct ParagraphFrames: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, latest in latest })
    }
}

private struct PageNavigationView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Button { Task { await model.goToPage(1) } } label: { Image(systemName: "backward.end") }
                    .disabled(model.pageNumber <= 1).help("首页").accessibilityLabel("首页")
                Button { Task { await model.goToPage(model.pageNumber - 1) } } label: { Image(systemName: "chevron.left") }
                    .disabled(model.pageNumber <= 1).keyboardShortcut(.leftArrow, modifiers: .command)
                    .help("上一页（⌘←）").accessibilityLabel("上一页")
                Text("第")
                TextField("页码", text: $model.pageInput)
                    .textFieldStyle(.roundedBorder).multilineTextAlignment(.center).monospacedDigit().frame(width: 56)
                    .onSubmit { Task { await model.submitPageInput() } }
                    .accessibilityLabel("前往页码").help("输入页码后按回车")
                Text("/ \(model.pageCount) 页").monospacedDigit()
                Button { Task { await model.goToPage(model.pageNumber + 1) } } label: { Image(systemName: "chevron.right") }
                    .disabled(model.pageNumber >= model.pageCount).keyboardShortcut(.rightArrow, modifiers: .command)
                    .help("下一页（⌘→）").accessibilityLabel("下一页")
                Button { Task { await model.goToPage(model.pageCount) } } label: { Image(systemName: "forward.end") }
                    .disabled(model.pageNumber >= model.pageCount).help("末页").accessibilityLabel("末页")
                Spacer(minLength: 8)
                Image(systemName: "minus.magnifyingglass")
                Slider(value: $model.pageZoom, in: 0.5...2.5).frame(width: 100).accessibilityLabel("页面缩放")
                Text("\(Int(model.pageZoom * 100))%").monospacedDigit().frame(width: 44)
                Button("适应宽度") { model.pageZoom = 1 }
            }.buttonStyle(.borderless)
            if let error = model.pageNavigationError { Text(error).font(.caption).foregroundStyle(.red) }
        }.padding(.horizontal, 18)
    }
}

private struct ReadingSaveStatusView: View {
    @ObservedObject var reading: ReadingStateStore
    var body: some View {
        if let error = reading.saveError {
            HStack {
                Text(error).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("重试保存阅读位置") { reading.flush() }
            }.padding(10).background(.bar)
        }
    }
}

private struct ImportStatusView: View {
    @ObservedObject var queue: DocumentImportQueue
    @State private var showingFailures = false
    var body: some View {
        if queue.isImporting || !queue.failures.isEmpty {
            HStack(spacing: 12) {
                if queue.isImporting {
                    ProgressView().controlSize(.small)
                    Text(queue.currentFilename.map { "正在导入 \(queue.completedCount + 1)/\(queue.totalCount)：\($0)" }
                         ?? "等待引擎就绪 · \(queue.pendingCount) 个文件").lineLimit(1)
                    if queue.pendingCount > 0 {
                        Button("停止后续导入") { queue.cancelPending() }.help("当前文件继续处理，尚未开始的文件移出队列")
                    }
                }
                Spacer()
                if !queue.failures.isEmpty {
                    Button("\(queue.failures.count) 个文件导入失败 · 查看") { showingFailures = true }
                }
            }.font(.caption).padding(10).background(.bar)
            .sheet(isPresented: $showingFailures) {
                VStack(alignment: .leading, spacing: 16) {
                    Text("未能导入的文件").font(.title2.bold())
                    Text("其他文件会继续导入。请修复以下问题后重新选择文件。").foregroundStyle(.secondary)
                    List(queue.failures) { failure in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(failure.filename).fontWeight(.medium)
                            Text(failure.message).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        }.padding(.vertical, 5)
                    }
                    HStack {
                        Button("清除记录") { queue.clearFailures(); showingFailures = false }
                        Spacer()
                        Button("完成") { showingFailures = false }.keyboardShortcut(.defaultAction)
                    }
                }.padding(20).frame(width: 560, height: 380)
            }
        }
    }
}

private struct SegmentRow: View {
    @ObservedObject var drafts: RevisionDraftStore
    let taskID: String
    let segment: Segment
    let editable: Bool
    let saving: Bool
    let save: (String) async -> Bool

    private var editing: Bool { drafts.text(taskID: taskID, segmentID: segment.id) != nil }
    private var draft: String { drafts.text(taskID: taskID, segmentID: segment.id) ?? "" }

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            Text(segment.text)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
            Divider()
            Group {
                if editing && editable {
                    VStack(alignment: .trailing, spacing: 8) {
                        TextEditor(text: Binding(get: { draft }, set: {
                            drafts.set($0, taskID: taskID, segmentID: segment.id)
                        })).frame(minHeight: 100).disabled(saving)
                        HStack {
                            Text(drafts.saveError == nil ? "草稿已在本机保存" : "草稿尚未写入磁盘")
                                .font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Button("放弃修改") { drafts.remove(taskID: taskID, segmentID: segment.id) }.disabled(saving)
                            Button(saving ? "正在保存…" : "保存到译文") {
                                Task { _ = await save(draft) }
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(saving)
                        }
                    }
                } else {
                    Text(segment.translation ?? (editable ? "未翻译" : "等待翻译"))
                        .foregroundStyle(segment.translation == nil ? .secondary : .primary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            if editable { drafts.set(segment.translation ?? "", taskID: taskID, segmentID: segment.id) }
                        }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
        }
        .font(.body)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.5))
        .overlay(alignment: .bottom) { Divider() }
    }
}

private struct DraftSummaryView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var drafts: RevisionDraftStore
    let task: TranslationTask
    var body: some View {
        if let error = drafts.saveError {
            Text(error).font(.caption).foregroundStyle(.red).padding(10)
        }
        if drafts.count(taskID: task.id) > 0 {
            HStack {
                Text("\(drafts.count(taskID: task.id)) 处修订草稿 · 保存到译文后才会用于导出")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button(model.savingAllDraftTaskIDs.contains(task.id) ? "正在保存…" : "保存全部草稿") {
                    Task { await model.saveAllDrafts(taskID: task.id) }
                }
                .disabled(task.status == "translating" || model.startingIDs.contains(task.id) || model.hasDraftSaveInFlight(task.id))
            }.padding(10)
        }
    }
}
