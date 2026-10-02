// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "DocTranslatorMac",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "DocTranslatorMac", targets: ["DocTranslatorMac"])],
    targets: [.executableTarget(name: "DocTranslatorMac", path: "Sources"),
              .testTarget(name: "DocTranslatorMacTests", dependencies: ["DocTranslatorMac"], path: "Tests")],
    swiftLanguageModes: [.v5]
)
