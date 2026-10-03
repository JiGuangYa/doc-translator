import AppKit
import Foundation
import PDFKit
import Vision

struct OCRLine: Sendable {
    let page: Int
    let bbox: [Double]
    let text: String
    let confidence: Double

    var json: [String: Any] {
        ["page": page, "bbox": bbox, "text": text, "confidence": confidence]
    }
}

enum OCRService {
    static func recognize(_ file: URL, pages: [Int]? = nil) throws -> [OCRLine] {
        guard let document = CGPDFDocument(file as CFURL) else {
            throw AppFailure.message("无法读取扫描版 PDF")
        }
        var lines: [OCRLine] = []
        let requested = pages ?? Array(1...max(1, document.numberOfPages))
        for number in requested {
            try Task.checkCancellation()
            guard number >= 1, number <= document.numberOfPages else {
                throw AppFailure.message("扫描页码超出文档范围")
            }
            let pageLines: [OCRLine] = try autoreleasepool {
                guard let page = document.page(at: number) else {
                    throw AppFailure.message("无法读取第 \(number) 页")
                }
                let bounds = page.getBoxRect(.cropBox)
                let rotated = abs(page.rotationAngle) % 180 == 90
                let width = rotated ? bounds.height : bounds.width
                let height = rotated ? bounds.width : bounds.height
                let scale = min(2.5, 2400 / max(width, height))
                let pixelsWide = max(1, Int((width * scale).rounded()))
                let pixelsHigh = max(1, Int((height * scale).rounded()))
                // A fresh bitmap avoids PDFKit's retained thumbnail caches.
                guard let context = CGContext(data: nil, width: pixelsWide, height: pixelsHigh,
                    bitsPerComponent: 8, bytesPerRow: pixelsWide * 4, space: CGColorSpaceCreateDeviceRGB(),
                    bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
                    throw AppFailure.message("无法分配第 \(number) 页的识别图像")
                }
                let rect = CGRect(x: 0, y: 0, width: pixelsWide, height: pixelsHigh)
                context.setFillColor(CGColor(gray: 1, alpha: 1)); context.fill(rect)
                context.scaleBy(x: CGFloat(pixelsWide) / width, y: CGFloat(pixelsHigh) / height)
                context.concatenate(page.getDrawingTransform(.cropBox,
                    rect: CGRect(x: 0, y: 0, width: width, height: height), rotate: 0, preserveAspectRatio: true))
                context.drawPDFPage(page)
                guard let image = context.makeImage() else {
                    throw AppFailure.message("无法生成第 \(number) 页的识别图像")
                }
                let request = VNRecognizeTextRequest()
                request.recognitionLevel = .accurate
                request.usesLanguageCorrection = true
                request.automaticallyDetectsLanguage = true
                try VNImageRequestHandler(cgImage: image).perform([request])
                try Task.checkCancellation()
                return (request.results ?? []).compactMap { result in
                    guard let candidate = result.topCandidates(1).first,
                          !candidate.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
                    let box = result.boundingBox
                    return OCRLine(page: number,
                        bbox: [box.minX, box.minY, box.maxX, box.maxY].map { min(1, max(0, Double($0))) },
                        text: candidate.string, confidence: Double(candidate.confidence))
                }
            }
            lines.append(contentsOf: pageLines)
        }
        return lines.sorted { left, right in
            if left.page != right.page { return left.page < right.page }
            if abs(left.bbox[3] - right.bbox[3]) > 0.02 {
                return left.bbox[3] > right.bbox[3]
            }
            return left.bbox[0] < right.bbox[0]
        }
    }

    static func previewCrop(_ image: NSImage, bbox: [Double]) -> NSImage? {
        guard bbox.count == 4, bbox.allSatisfy({ $0 >= 0 && $0 <= 1 }), bbox[2] > bbox[0], bbox[3] > bbox[1] else { return nil }
        var proposed = NSRect.zero
        guard let source = image.cgImage(forProposedRect: &proposed, context: nil, hints: nil) else { return nil }
        let width = Double(source.width), height = Double(source.height)
        let region = CGRect(x: bbox[0] * width, y: (1 - bbox[3]) * height,
                            width: (bbox[2] - bbox[0]) * width, height: (bbox[3] - bbox[1]) * height)
            .insetBy(dx: -12, dy: -12)
            .intersection(CGRect(x: 0, y: 0, width: width, height: height))
        guard !region.isEmpty, let cropped = source.cropping(to: region) else { return nil }
        return NSImage(cgImage: cropped, size: NSSize(width: cropped.width, height: cropped.height))
    }
}
