import AppKit
import Foundation

let folder = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
for size in [16, 32, 64, 128, 256, 512, 1024] {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                 isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: size * 4, bitsPerPixel: 32)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let scale = AffineTransform(scale: Double(size) / 1024)
    (scale as NSAffineTransform).concat()
    let background = NSBezierPath(roundedRect: NSRect(x: 80, y: 80, width: 864, height: 864), xRadius: 195, yRadius: 195)
    NSGradient(starting: NSColor(calibratedRed: 0.19, green: 0.40, blue: 0.94, alpha: 1),
               ending: NSColor(calibratedRed: 0.31, green: 0.21, blue: 0.73, alpha: 1))!.draw(in: background, angle: -55)
    NSColor.white.withAlphaComponent(0.82).setFill()
    NSBezierPath(roundedRect: NSRect(x: 210, y: 360, width: 350, height: 430), xRadius: 40, yRadius: 40).fill()
    NSColor.white.setFill()
    NSBezierPath(roundedRect: NSRect(x: 460, y: 225, width: 350, height: 430), xRadius: 40, yRadius: 40).fill()
    let ink = NSColor(calibratedRed: 0.21, green: 0.27, blue: 0.62, alpha: 1)
    ("A" as NSString).draw(at: NSPoint(x: 265, y: 510), withAttributes: [.font: NSFont.systemFont(ofSize: 205, weight: .medium), .foregroundColor: ink])
    ("文" as NSString).draw(at: NSPoint(x: 525, y: 345), withAttributes: [.font: NSFont.systemFont(ofSize: 192, weight: .medium), .foregroundColor: ink])
    NSGraphicsContext.restoreGraphicsState()
    let data = bitmap.representation(using: .png, properties: [:])!
    if size <= 512 { try data.write(to: folder.appendingPathComponent("icon_\(size)x\(size).png")) }
    if size >= 32 { try data.write(to: folder.appendingPathComponent("icon_\(size/2)x\(size/2)@2x.png")) }
}
