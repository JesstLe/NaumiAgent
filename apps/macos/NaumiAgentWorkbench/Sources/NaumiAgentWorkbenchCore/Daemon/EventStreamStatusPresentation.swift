import Foundation

/// Visual category used by the status badge tint. Maps to a small set of
/// semantic colors the SwiftUI layer can render consistently.
public enum EventStreamStatusTone: String, Equatable, Sendable {
    case neutral
    case healthy
    case warning
    case error
}

/// Pure presentation model for the Workbench event-stream lifecycle indicator.
///
/// Converts ``AppState/eventStreamStatus`` (and supporting counters/timestamps)
/// into the strings, SF Symbol name, and tone the top bar and Timeline header
/// render. SwiftUI-agnostic so it stays unit-testable.
public struct EventStreamStatusPresentation: Equatable {
    public let locale: AppLocale
    public let status: EventStreamStatus
    public let reconnectAttempt: Int
    public let maxReconnectAttempts: Int
    public let lastConnectedAt: Date?
    public let now: Date

    public let statusText: String
    public let shortLabel: String
    public let iconName: String
    public let tone: EventStreamStatusTone
    public let helpText: String?
    public let lastConnectedText: String?
    public let showsManualReconnect: Bool

    public init(
        locale: AppLocale,
        status: EventStreamStatus,
        reconnectAttempt: Int,
        maxReconnectAttempts: Int,
        lastConnectedAt: Date?,
        now: Date = Date()
    ) {
        self.locale = locale
        self.status = status
        self.reconnectAttempt = reconnectAttempt
        self.maxReconnectAttempts = maxReconnectAttempts
        self.lastConnectedAt = lastConnectedAt
        self.now = now

        switch status {
        case .idle:
            statusText = AppStrings.EventStreamStatus.statusIdle(locale)
            shortLabel = AppStrings.EventStreamStatus.statusIdle(locale)
            iconName = "antenna.radiowaves.left.and.right.slash"
            tone = .neutral
            helpText = nil
        case .connecting:
            statusText = AppStrings.EventStreamStatus.statusConnecting(locale)
            shortLabel = AppStrings.EventStreamStatus.statusConnecting(locale)
            iconName = "antenna.radiowaves.left.and.right"
            tone = .warning
            helpText = nil
        case .connected:
            statusText = AppStrings.EventStreamStatus.statusConnected(locale)
            shortLabel = AppStrings.EventStreamStatus.liveLabel(locale)
            iconName = "dot.radiowaves.left.and.right"
            tone = .healthy
            helpText = nil
        case .reconnecting:
            statusText = AppStrings.EventStreamStatus.statusReconnecting(
                locale,
                attempt: min(max(reconnectAttempt, 1), max(maxReconnectAttempts, 1)),
                max: max(maxReconnectAttempts, 1)
            )
            shortLabel = AppStrings.EventStreamStatus.statusReconnecting(
                locale,
                attempt: min(max(reconnectAttempt, 1), max(maxReconnectAttempts, 1)),
                max: max(maxReconnectAttempts, 1)
            )
            iconName = "arrow.triangle.2.circlepath"
            tone = .warning
            helpText = nil
        case .stale:
            statusText = AppStrings.EventStreamStatus.statusStale(locale)
            shortLabel = AppStrings.EventStreamStatus.staleLabel(locale)
            iconName = "exclamationmark.triangle"
            tone = .error
            helpText = AppStrings.EventStreamStatus.reconnectHelp(locale)
        case .stoppedBySessionSwitch:
            statusText = AppStrings.EventStreamStatus.statusStoppedBySessionSwitch(locale)
            shortLabel = AppStrings.EventStreamStatus.statusStoppedBySessionSwitch(locale)
            iconName = "arrow.left.arrow.right"
            tone = .neutral
            helpText = nil
        case .stoppedByAuthOrProtocol:
            statusText = AppStrings.EventStreamStatus.statusStoppedByAuthOrProtocol(locale)
            shortLabel = AppStrings.EventStreamStatus.statusStoppedByAuthOrProtocol(locale)
            iconName = "lock.trianglebadge.exclamationmark"
            tone = .error
            helpText = AppStrings.EventStreamStatus.stoppedByAuthHelp(locale)
        }

        showsManualReconnect = status.allowsManualReconnect

        if let lastConnectedAt {
            lastConnectedText = Self.relativeElapsed(from: lastConnectedAt, to: now, locale: locale)
        } else {
            lastConnectedText = nil
        }
    }

    /// Compact "N s / N m / N h ago" relative label, matching the snapshot
    /// freshness formatter so the two indicators read consistently.
    static func relativeElapsed(from start: Date, to end: Date, locale: AppLocale) -> String {
        let elapsed = max(0, end.timeIntervalSince(start))
        if elapsed < 60 {
            let seconds = Int(elapsed.rounded())
            return locale == .zhCN ? "\(seconds) 秒前" : "\(seconds) s ago"
        }
        if elapsed < 3600 {
            let minutes = Int((elapsed / 60).rounded())
            return locale == .zhCN ? "\(minutes) 分钟前" : "\(minutes) m ago"
        }
        if elapsed < 86_400 {
            let hours = Int((elapsed / 3600).rounded())
            return locale == .zhCN ? "\(hours) 小时前" : "\(hours) h ago"
        }
        let days = Int((elapsed / 86_400).rounded())
        return locale == .zhCN ? "\(days) 天前" : "\(days) d ago"
    }
}

/// Computes the bounded exponential-backoff delay for a given reconnect attempt.
///
/// Delay grows as `base * 2^(attempt-1)` up to `maxDelay`. Used by both the
/// production reconnect loop and tests so the schedule stays consistent.
public enum EventStreamBackoff {
    /// Base delay (first reconnect attempt), in whole seconds.
    public static let baseSeconds: Int = 1
    /// Ceiling for a single reconnect delay, in whole seconds.
    public static let maxSeconds: Int = 30

    /// Base delay (first reconnect attempt).
    public static var baseDelay: Duration { .seconds(baseSeconds) }
    /// Ceiling for a single reconnect delay.
    public static var maxDelay: Duration { .seconds(maxSeconds) }

    /// Returns the delay to wait before the `attempt`-th reconnect (1-based).
    public static func delay(forAttempt attempt: Int) -> Duration {
        guard attempt > 0 else { return baseDelay }
        // Exponential growth with a hard ceiling: base * 2^(attempt-1).
        let cap = maxSeconds / baseSeconds
        let multiplier = Swift.min(cap, 1 << (attempt - 1))
        return .seconds(baseSeconds * multiplier)
    }
}
