import Foundation

struct ProviderSaveResult {
    let provider: Provider
    let warning: String?
}

@MainActor
final class ProviderEditor: ObservableObject {
    enum Transition { case select(String), close }
    @Published private(set) var selectedID = ""
    @Published var name = "" { didSet { clearFeedback() } }
    @Published var baseURL = "" { didSet { clearFeedback() } }
    @Published var modelName = "" { didSet { clearFeedback() } }
    @Published var apiKey = "" { didSet { clearFeedback() } }
    @Published private(set) var feedback = ""
    @Published private(set) var isSaving = false
    @Published private(set) var isTesting = false
    @Published var pendingTransition: Transition?
    private(set) var original: Provider?
    private weak var model: AppModel?
    private var saveOperation: Task<Bool, Never>?
    private var testOperation: Task<Void, Never>?
    private var testGeneration = 0

    init(model: AppModel) {
        self.model = model
        load(model.providers.first { $0.id == model.selectedProviderID } ?? model.providers.first)
    }

    var isBusy: Bool { isSaving || isTesting }
    var isValid: Bool {
        guard !trim(name).isEmpty, !trim(modelName).isEmpty,
              let url = URLComponents(string: trim(baseURL)), let host = url.host, !host.isEmpty else { return false }
        return ["http", "https"].contains(url.scheme?.lowercased() ?? "") && url.user == nil && url.password == nil
    }
    var hasUnsavedChanges: Bool {
        trim(name) != (original?.name ?? "") || trim(baseURL).trimmingCharacters(in: CharacterSet(charactersIn: "/")) != (original?.baseUrl ?? "") ||
        trim(modelName) != (original?.model ?? "") || !apiKey.isEmpty
    }
    var canTest: Bool { original != nil && !hasUnsavedChanges && !isBusy }

    func request(_ transition: Transition) -> Bool {
        guard !isBusy else { return false }
        if hasUnsavedChanges { pendingTransition = transition; return false }
        return perform(transition)
    }

    func complete(_ transition: Transition, save: Bool) async -> Bool {
        pendingTransition = nil
        if save, !(await self.save()) { return false }
        return perform(transition)
    }

    private func perform(_ transition: Transition) -> Bool {
        switch transition {
        case .close: return true
        case .select(let id): load(model?.providers.first { $0.id == id }); return false
        }
    }

    private func load(_ provider: Provider?) {
        original = provider
        selectedID = provider?.id ?? ""
        name = provider?.name ?? ""
        baseURL = provider?.baseUrl ?? ""
        modelName = provider?.model ?? ""
        apiKey = ""
        feedback = ""
    }

    @discardableResult
    func save() async -> Bool {
        if let saveOperation { return await saveOperation.value }
        guard isValid else { feedback = "请填写名称、有效的 HTTP(S) 服务地址和模型名称"; return false }
        guard !isTesting, let model else { return false }
        isSaving = true
        let request = Task { [self] in
            defer { isSaving = false }
            do {
                let result = try await model.saveProvider(name: name, baseURL: baseURL, model: modelName, apiKey: apiKey, existing: original)
                load(result.provider)
                feedback = result.warning ?? "配置已保存"
                return true
            } catch {
                feedback = "保存失败：\(error.localizedDescription)"
                return false
            }
        }
        saveOperation = request
        let saved = await request.value
        saveOperation = nil
        return saved
    }

    func waitForSave() async { _ = await saveOperation?.value }

    func test() {
        guard canTest, let provider = original, let model else { return }
        isTesting = true
        feedback = "正在测试已保存的配置…"
        testGeneration += 1
        let expected = testGeneration
        testOperation = Task { [weak self] in
            let result = await model.testProvider(provider)
            guard let self, !Task.isCancelled, expected == self.testGeneration else { return }
            self.feedback = result
            self.isTesting = false
            self.testOperation = nil
        }
    }

    func cancelTest() {
        guard isTesting else { return }
        testGeneration += 1
        testOperation?.cancel()
        testOperation = nil
        isTesting = false
        feedback = "已取消测试"
    }

    private func trim(_ value: String) -> String { value.trimmingCharacters(in: .whitespacesAndNewlines) }
    private func clearFeedback() { if !isBusy { feedback = "" } }
}
