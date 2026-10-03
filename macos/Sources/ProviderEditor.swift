import Foundation

/// Owns the form and request generations independently of SwiftUI redraws.
@MainActor
final class ProviderEditor: ObservableObject {
    @Published var name = "" { didSet { changed() } }
    @Published var baseURL = "" { didSet { changed() } }
    @Published var model = "" { didSet { changed() } }
    @Published var key = "" { didSet { changed() } }
    @Published var inputPriceText = "" { didSet { changed() } }
    @Published var outputPriceText = "" { didSet { changed() } }
    @Published private(set) var selectedID: String?
    @Published private(set) var feedback: String?
    @Published private(set) var succeeded = false
    @Published private(set) var isTesting = false
    @Published private(set) var isSaving = false
    @Published private(set) var suggestions: [String] = []
    private var original: [String] = ["", "", "", "", "", ""]
    private var generation = UUID()
    private var operation: Task<Void, Never>?
    private weak var state: AppState?
    init(state: AppState) { self.state = state }
    private var fields: [String] { [name, baseURL, model, key, inputPriceText, outputPriceText] }
    var dirty: Bool { fields != original }
    var valid: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !model.isEmpty &&
        ["http", "https"].contains(URLComponents(string: baseURL)?.scheme ?? "") &&
        URLComponents(string: baseURL)?.host != nil &&
        [inputPriceText, outputPriceText].allSatisfy { $0.isEmpty || (Double($0).map { $0.isFinite && $0 >= 0 } == true) }
    }
    func load(_ provider: Provider?) {
        cancel()
        selectedID = provider?.id
        name = provider?.name ?? ""; baseURL = provider?.baseUrl ?? ""; model = provider?.model ?? ""
        key = ""
        inputPriceText = provider?.inputPricePerMillion.map(String.init(describing:)) ?? ""
        outputPriceText = provider?.outputPricePerMillion.map(String.init(describing:)) ?? ""
        original = fields; changed()
    }
    private func changed() {
        cancel(); feedback = nil; suggestions = []
        state?.settingsDirty = dirty
    }
    func cancel() {
        generation = UUID(); operation?.cancel(); operation = nil; isTesting = false
    }
    func test(discover: Bool = false) {
        guard !isSaving, let state, !baseURL.isEmpty, discover || !model.isEmpty else { return }
        cancel(); let token = generation; isTesting = true; feedback = discover ? "正在获取模型…" : "正在测试当前配置…"
        let url = baseURL, modelName = model, credential = key, id = selectedID
        operation = Task { [weak self] in
            if discover {
                let result = await state.discoverDraftModels(baseURL: url, key: credential, providerID: id)
                guard let self, !Task.isCancelled, generation == token else { return }
                suggestions = result.models ?? []; succeeded = result.ok
                feedback = result.ok ? "已读取 \(suggestions.count) 个模型" : result.error
            } else {
                let result = await state.testDraftProvider(baseURL: url, model: modelName, key: credential, providerID: id)
                guard let self, !Task.isCancelled, generation == token else { return }
                succeeded = result.ok
                feedback = result.ok ? "连接成功\(result.latencyMs.map { " · \($0) 毫秒" } ?? "")" : result.error
                if let normalized = result.normalizedBaseUrl, normalized != url { feedback = "\(feedback ?? "") · 使用地址：\(normalized)" }
            }
            self?.isTesting = false; self?.operation = nil
        }
    }
    func save() async -> Bool {
        guard !isSaving, valid, let state else { return false }
        cancel(); isSaving = true; defer { isSaving = false }
        let snapshot = fields, id = selectedID
        let result = await state.saveProviderConfiguration(id: id, name: name, baseURL: baseURL,
            model: model, key: key, inputPrice: Double(inputPriceText), outputPrice: Double(outputPriceText))
        guard let result else { feedback = state.alertText ?? "保存失败，修改已保留"; return false }
        selectedID = result.id
        // A configuration committed before a follow-up refresh is still saved.
        if fields == snapshot { load(result) }
        else { original = [result.name, result.baseUrl, result.model, "", result.inputPricePerMillion.map(String.init(describing:)) ?? "", result.outputPricePerMillion.map(String.init(describing:)) ?? ""]; state.settingsDirty = dirty }
        feedback = "配置已保存"; succeeded = true
        return true
    }
}
