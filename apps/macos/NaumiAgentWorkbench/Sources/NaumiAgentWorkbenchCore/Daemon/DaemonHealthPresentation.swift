import Foundation

/// Pure presentation model for the daemon health panel.
///
/// Converts raw connection state, the last health-check timestamp, and the
/// connection log into the exact strings and flags the Settings UI renders.
/// Keeping this logic out of the view makes it unit-testable and locale-safe.
public struct DaemonHealthPresentation: Equatable {
    /// Maximum number of log entries surfaced in the panel (newest first).
    public static let recentLogLimit: Int = 8

    public let locale: AppLocale
    public let connectionState: AppState.ConnectionState
    public let lastHealthCheckAt: Date?
    public let recentLog: [ConnectionLogEntry]
    public let startCommand: String
    public let lastCheckedText: String
    public let statusText: String
    public let nextActionText: String
    public let writesDisabledBanner: String?

    public init(
        locale: AppLocale,
        connectionState: AppState.ConnectionState,
        lastHealthCheckAt: Date?,
        connectionLog: [ConnectionLogEntry],
        startCommand: String = AppStrings.ConnectionSetup.startCommand(.zhCN),
        now: Date = Date(),
        lastCheckedFormatter: (Date, AppLocale) -> String = DaemonHealthPresentation.formatLastChecked
    ) {
        self.locale = locale
        self.connectionState = connectionState
        self.lastHealthCheckAt = lastHealthCheckAt
        self.startCommand = startCommand
        self.statusText = connectionState.displayName(locale: locale)
        self.nextActionText = AppStrings.DaemonHealth.nextAction(locale, for: connectionState)

        if let lastHealthCheckAt {
            self.lastCheckedText = lastCheckedFormatter(lastHealthCheckAt, locale)
        } else {
            self.lastCheckedText = AppStrings.DaemonHealth.lastCheckedNever(locale)
        }

        if connectionState == .protocolMismatch {
            self.writesDisabledBanner = AppStrings.DaemonHealth.writesDisabledBanner(locale)
        } else {
            self.writesDisabledBanner = nil
        }

        // Newest first, capped to the recent limit.
        let sorted = connectionLog.sorted { $0.date > $1.date }
        self.recentLog = Array(sorted.prefix(Self.recentLogLimit))
    }

    /// Whether write actions should be disabled in the current state.
    public var shouldDisableWrites: Bool {
        connectionState == .protocolMismatch
    }

    /// Default locale-aware absolute timestamp formatter for the last check.
    public static func formatLastChecked(_ date: Date, _ locale: AppLocale) -> String {
        let formatter = DateFormatter()
        formatter.locale = locale == .zhCN
            ? Locale(identifier: "zh_CN")
            : Locale(identifier: "en_US")
        formatter.dateStyle = .short
        formatter.timeStyle = .medium
        return formatter.string(from: date)
    }

    /// Renders a single log entry as a compact "HH:mm:ss · state · note" line.
    public func logLine(for entry: ConnectionLogEntry) -> String {
        let formatter = DateFormatter()
        formatter.locale = locale == .zhCN
            ? Locale(identifier: "zh_CN")
            : Locale(identifier: "en_US")
        formatter.dateStyle = .none
        formatter.timeStyle = .medium
        let time = formatter.string(from: entry.date)
        let label = entry.state.displayName(locale: locale)
        if let message = entry.message, !message.isEmpty {
            return "\(time) · \(label) · \(message)"
        }
        return "\(time) · \(label)"
    }
}
