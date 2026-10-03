import AppKit
import Foundation
import UserNotifications

@MainActor enum TaskNotifications {
    static func requestPermission() async {
        _ = try? await UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound])
    }

    static func finished(_ task: TranslationTask) async {
        guard !NSApp.isActive else { return }
        let content = UNMutableNotificationContent()
        content.title = task.status == "done" ? "文档翻译完成" : "文档翻译需要处理"
        content.body = "\(task.filename) · \(task.statusLabel)"
        content.sound = .default
        let request = UNNotificationRequest(identifier: task.id + "-" + task.status,
                                            content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
    }
}
