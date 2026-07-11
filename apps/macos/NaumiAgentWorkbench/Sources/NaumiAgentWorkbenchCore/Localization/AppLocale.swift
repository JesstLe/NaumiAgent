import Foundation

/// Supported user-facing locales. 默认中文，保底英文。
public enum AppLocale: String, CaseIterable, Identifiable, Sendable, Hashable {
    case zhCN = "zh-CN"
    case enUS = "en-US"

    public var id: String { rawValue }

    public static let `default`: AppLocale = .zhCN

    /// UserDefaults key persisting the user's locale choice across launches.
    private static let storageKey = "naumi.workbench.locale"

    /// The locale previously chosen by the user, or the default (Chinese) on
    /// first launch.
    public static func storedOrDefault() -> AppLocale {
        guard let raw = UserDefaults.standard.string(forKey: storageKey),
              let locale = AppLocale(rawValue: raw) else {
            return .default
        }
        return locale
    }

    /// Persists this locale so the next launch restores the user's choice.
    public func persist() {
        UserDefaults.standard.set(rawValue, forKey: Self.storageKey)
    }
}
