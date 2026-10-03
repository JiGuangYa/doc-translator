import XCTest
@testable import DocTranslatorMac

@MainActor
final class ReadingStateTests: XCTestCase {
    func folder() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testReadingBookmarksAndLastDocumentSurviveRelaunch() throws {
        let url = try folder().appendingPathComponent("reading.json")
        let store = ReadingStateStore(url: url)
        let first = ReadingBookmark(mode: 1, page: 17, zoom: 1.25, paragraphID: "s000031")
        store.update(first, for: "first")
        store.update(ReadingBookmark(mode: 0, page: 4, zoom: 0.8, paragraphID: "s000004"), for: "second")
        store.select("first")
        store.flush()
        let restored = ReadingStateStore(url: url)
        XCTAssertEqual(restored.lastTaskID, "first")
        XCTAssertEqual(restored.bookmark(for: "first"), first)
        XCTAssertEqual(restored.bookmark(for: "second").page, 4)
        XCTAssertEqual(restored.bookmark(for: "unknown"), ReadingBookmark())
    }

    func testCorruptPreferencesAreBackedUpBeforeReplacement() throws {
        let folder = try folder(), url = folder.appendingPathComponent("reading.json")
        let corrupt = Data("incomplete file".utf8)
        try corrupt.write(to: url)
        let store = ReadingStateStore(url: url)
        XCTAssertNotNil(store.saveError)
        store.select("a"); store.update(ReadingBookmark(mode: 99, page: -5, zoom: 9), for: "a")
        store.flush()
        let copies = try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil).filter { $0.lastPathComponent.hasPrefix("reading-recovery-") }
        XCTAssertEqual(copies.count, 1)
        XCTAssertEqual(try Data(contentsOf: copies[0]), corrupt)
        let restored = ReadingStateStore(url: url)
        XCTAssertEqual(restored.bookmark(for: "a"), ReadingBookmark(mode: 0, page: 0, zoom: 2.5))
        XCTAssertNil(store.saveError)
    }

    func testDiskFailureKeepsBookmarkAndCanBeRetried() throws {
        let folder = try folder(), obstruction = folder.appendingPathComponent("not-a-folder")
        try Data("preserve this file".utf8).write(to: obstruction)
        let store = ReadingStateStore(url: obstruction.appendingPathComponent("reading.json"))
        store.select("a"); store.update(ReadingBookmark(mode: 1, page: 8), for: "a"); store.flush()
        XCTAssertNotNil(store.saveError)
        XCTAssertEqual(store.bookmark(for: "a").page, 8)
        XCTAssertEqual(try String(contentsOf: obstruction, encoding: .utf8), "preserve this file")
        try FileManager.default.removeItem(at: obstruction)
        store.flush()
        XCTAssertNil(store.saveError)
        XCTAssertEqual(ReadingStateStore(url: store.url).bookmark(for: "a").page, 8)
    }
}
