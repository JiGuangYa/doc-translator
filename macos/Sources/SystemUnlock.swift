import Foundation
import LocalAuthentication
import Security

enum SystemUnlock {
    private static var service: String {
        (Bundle.main.bundleIdentifier ?? "local.doctranslator.native") + ".unlock"
    }
    private static let account = "admin-password"
    private static let preference = "DocTranslatorSystemUnlockEnabled"

    static var isEnabled: Bool { UserDefaults.standard.bool(forKey: preference) }

    static func save(password: String) -> Bool {
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, .userPresence, &error)
        else { return false }
        let match: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(match as CFDictionary)
        var item = match
        item[kSecAttrAccessControl as String] = access
        item[kSecValueData as String] = Data(password.utf8)
        let success = SecItemAdd(item as CFDictionary, nil) == errSecSuccess
        UserDefaults.standard.set(success, forKey: preference)
        return success
    }

    static func retrieve() -> String? {
        guard isEnabled else { return nil }
        let context = LAContext()
        context.localizedReason = "解锁本机文档和翻译设置"
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecUseAuthenticationContext as String: context,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func disable() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
        UserDefaults.standard.set(false, forKey: preference)
    }
}
