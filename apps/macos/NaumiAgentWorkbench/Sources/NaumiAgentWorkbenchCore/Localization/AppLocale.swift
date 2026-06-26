import Foundation

/// Supported user-facing locales. 默认中文，保底英文。
public enum AppLocale: String, CaseIterable, Identifiable, Sendable, Hashable {
    case zhCN = "zh-CN"
    case enUS = "en-US"

    public var id: String { rawValue }

    public static let `default`: AppLocale = .zhCN
}
