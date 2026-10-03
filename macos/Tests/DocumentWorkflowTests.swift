import XCTest
@testable import DocTranslatorMac

@MainActor
final class DocumentWorkflowTests: XCTestCase {
    func testArchiveAndAttentionLibraryFilters() throws {
        let state = AppState(api: APIClient(baseURL: URL(string: "http://localhost:1")!))
        func task(_ archive: Bool, _ missing: Int, _ issue: String? = nil) throws -> TranslationTask {
            var object: [String: Any] = ["task_id": "a", "filename": "a.pdf", "ext": ".pdf", "status": "done", "has_translated": true, "archived": archive, "untranslated_count": missing]
            if let issue { object["migration_issue"] = issue }
            let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
            return try decoder.decode(TranslationTask.self, from: JSONSerialization.data(withJSONObject: object))
        }
        state.libraryFilter = "全部"
        XCTAssertTrue(state.libraryIncludes(try task(false, 0)))
        XCTAssertFalse(state.libraryIncludes(try task(true, 0)))
        state.libraryFilter = "已归档"
        XCTAssertTrue(state.libraryIncludes(try task(true, 0)))
        state.libraryFilter = "待处理"
        XCTAssertTrue(state.libraryIncludes(try task(false, 1)))
        XCTAssertTrue(state.libraryIncludes(try task(false, 0, "Check old mapping")))
        state.libraryFilter = "已完成"
        XCTAssertFalse(state.libraryIncludes(try task(false, 1)))
    }
}
