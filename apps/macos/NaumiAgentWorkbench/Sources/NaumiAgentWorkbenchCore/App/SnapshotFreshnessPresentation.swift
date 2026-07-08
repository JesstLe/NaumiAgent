import Foundation

/// Pure presentation model for the snapshot freshness indicator.
///
/// Converts the last successful refresh timestamp, an optional failure error,
/// and the current locale into the strings the UI shows: how long ago data was
/// refreshed, whether it is stale, and why the latest attempt failed (while
/// emphasizing that the old data is still on screen).
public struct SnapshotFreshnessPresentation: Equatable {
    /// A snapshot is considered stale once this many seconds elapse.
    public static let staleThresholdSeconds: TimeInterval = 90

    public let locale: AppLocale
    public let lastRefreshedAt: Date?
    public let lastError: APIError?
    public let lastRefreshedText: String
    public let failureSummary: String?
    public let isStale: Bool

    public init(
        locale: AppLocale,
        lastRefreshedAt: Date?,
        lastError: APIError?,
        now: Date = Date()
    ) {
        self.locale = locale
        self.lastRefreshedAt = lastRefreshedAt
        self.lastError = lastError

        if let lastRefreshedAt {
            self.lastRefreshedText = Self.relativeElapsed(from: lastRefreshedAt, to: now, locale: locale)
            self.isStale = now.timeIntervalSince(lastRefreshedAt) >= Self.staleThresholdSeconds
        } else {
            self.lastRefreshedText = AppStrings.SnapshotFreshness.neverRefreshed(locale)
            self.isStale = false
        }

        self.failureSummary = lastError.map { $0.localizedMessage(locale: locale) }
    }

    /// Compact "N s / N m / N h ago" relative label.
    static func relativeElapsed(from start: Date, to end: Date, locale: AppLocale) -> String {
        let elapsed = max(0, end.timeIntervalSince(start))
        let agoLabel = AppStrings.SnapshotFreshness.agoSuffix(locale)
        if elapsed < 60 {
            let seconds = Int(elapsed.rounded())
            return locale == .zhCN
                ? "\(seconds) 秒\(agoLabel)"
                : "\(seconds) s \(agoLabel)"
        }
        if elapsed < 3600 {
            let minutes = Int((elapsed / 60).rounded())
            return locale == .zhCN
                ? "\(minutes) 分钟\(agoLabel)"
                : "\(minutes) m \(agoLabel)"
        }
        if elapsed < 86_400 {
            let hours = Int((elapsed / 3600).rounded())
            return locale == .zhCN
                ? "\(hours) 小时\(agoLabel)"
                : "\(hours) h \(agoLabel)"
        }
        let days = Int((elapsed / 86_400).rounded())
        return locale == .zhCN
            ? "\(days) 天\(agoLabel)"
            : "\(days) d \(agoLabel)"
    }
}
