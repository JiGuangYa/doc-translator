import SwiftUI

struct ProviderSettingsView: View {
    @ObservedObject var model: AppModel
    @StateObject private var editor: ProviderEditor
    @Environment(\.dismiss) private var dismiss

    init(model: AppModel) {
        self.model = model
        _editor = StateObject(wrappedValue: ProviderEditor(model: model))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("AI API 设置").font(.title2.bold())
                Spacer()
                Button("完成") { if editor.request(.close) { dismiss() } }.disabled(editor.isBusy)
            }
            Picker("配置", selection: Binding(get: { editor.selectedID }, set: { _ = editor.request(.select($0)) })) {
                Text("新增配置").tag("")
                ForEach(model.providers) { provider in Text(provider.name).tag(provider.id) }
            }.disabled(editor.isBusy)
            Form {
                TextField("名称", text: $editor.name)
                TextField("Base URL", text: $editor.baseURL)
                TextField("模型", text: $editor.modelName)
                SecureField(editor.original == nil ? "API Key" : "API Key（留空则不修改）", text: $editor.apiKey)
            }.disabled(editor.isBusy)
            Text("支持 OpenAI 兼容接口。API Key 保存在系统钥匙串或本机加密存储中。文档文字会发送到你配置的服务。")
                .font(.caption).foregroundStyle(.secondary)
            if editor.hasUnsavedChanges {
                Label("有未保存的修改 · 保存后才能测试新配置", systemImage: "pencil.circle")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button(editor.isSaving ? "正在保存…" : "保存") { Task { await editor.save() } }
                    .buttonStyle(.borderedProminent).disabled(!editor.isValid || editor.isBusy)
                if editor.isTesting {
                    ProgressView().controlSize(.small)
                    Button("取消测试") { editor.cancelTest() }
                } else {
                    Button("测试已保存配置") { editor.test() }.disabled(!editor.canTest)
                }
                Spacer()
            }
            if !editor.feedback.isEmpty {
                Text(editor.feedback).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(24).frame(width: 600)
        .background(Color(nsColor: .windowBackgroundColor))
        .interactiveDismissDisabled(editor.hasUnsavedChanges || editor.isBusy)
        .confirmationDialog("保存 API 设置的修改？", isPresented: Binding(
            get: { editor.pendingTransition != nil }, set: { if !$0 { editor.pendingTransition = nil } }
        ), titleVisibility: .visible, presenting: editor.pendingTransition) { transition in
            Button("保存修改") { Task { if await editor.complete(transition, save: true) { dismiss() } } }
                .disabled(!editor.isValid)
            Button("放弃修改", role: .destructive) { Task { if await editor.complete(transition, save: false) { dismiss() } } }
            Button("继续编辑", role: .cancel) { editor.pendingTransition = nil }
        } message: { _ in
            Text("尚未保存的服务地址、模型或 API Key 将不会应用到翻译。")
        }
        .onAppear { model.providerEditor = editor }
        .onDisappear {
            editor.cancelTest()
            if model.providerEditor === editor { model.providerEditor = nil }
        }
    }
}
