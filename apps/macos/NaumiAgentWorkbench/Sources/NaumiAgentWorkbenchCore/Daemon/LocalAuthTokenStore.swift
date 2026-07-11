import Foundation
import Security

/// Keychain-backed store for the local daemon auth token.
///
/// The token never lives in plain UserDefaults and is never rendered in the UI.
/// It is written to the macOS Keychain (the app's default keychain) under a
/// fixed service/account pair, and read back only when constructing the
/// `Authorization` header for a request.
public struct LocalAuthTokenStore: Sendable {
    /// Keychain key components. The service ties the token to this app; the
    /// account is fixed because the workbench talks to exactly one local daemon.
    public let service: String
    public let account: String

    public init(service: String = "ai.naumi.workbench", account: String = "local-daemon") {
        self.service = service
        self.account = account
    }

    /// Stores a token in the Keychain, replacing any existing value.
    ///
    /// Returns `nil` on success, or a human-readable Chinese error string when
    /// the Keychain rejects the write (e.g. locked, or access denied).
    @discardableResult
    public func save(_ token: String) -> String? {
        // Never persist an empty token; clear it instead.
        guard !token.isEmpty else {
            return delete()
        }
        let data = Data(token.utf8)

        // Delete any existing item first so upserts don't collide with
        // errSecDuplicateItem.
        delete()

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]

        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            return keychainErrorMessage(for: status)
        }
        return nil
    }

    /// Reads the stored token, or `nil` when none exists.
    public func load() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnData as String: true,
        ]

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    /// Deletes the stored token. Returns `nil` on success or a Chinese error
    /// message on an unexpected Keychain failure (item-not-found is treated as
    /// success).
    @discardableResult
    public func delete() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        if status == errSecSuccess || status == errSecItemNotFound {
            return nil
        }
        return keychainErrorMessage(for: status)
    }

    /// Returns `true` when a non-empty token is stored.
    public var hasToken: Bool {
        (load()?.isEmpty == false)
    }

    private func keychainErrorMessage(for status: OSStatus) -> String {
        switch status {
        case errSecAuthFailed:
            return "Keychain 认证失败，请确认钥匙串已解锁"
        case errSecDuplicateItem:
            return "Keychain 已存在同名条目"
        case errSecNotAvailable:
            return "当前设备不可用 Keychain"
        case errSecAllocate:
            return "Keychain 内存不足"
        default:
            return "Keychain 操作失败（状态码 \(status)），请重试或检查钥匙串权限"
        }
    }
}

/// Abstraction used by `WorkbenchAPIClient` so the token source can be swapped
/// in tests without touching the real Keychain.
public protocol LocalAuthTokenProviding: Sendable {
    func currentToken() -> String?
}

/// Default provider that reads the token from `LocalAuthTokenStore` on demand.
public struct KeychainTokenProvider: LocalAuthTokenProviding {
    private let store: LocalAuthTokenStore

    public init(store: LocalAuthTokenStore = LocalAuthTokenStore()) {
        self.store = store
    }

    public func currentToken() -> String? {
        store.load()
    }
}
