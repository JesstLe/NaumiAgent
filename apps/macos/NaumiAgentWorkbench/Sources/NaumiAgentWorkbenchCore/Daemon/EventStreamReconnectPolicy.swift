import Foundation

/// Policy governing automatic reconnection of the Workbench event stream.
///
/// The controller applies this after a *transient* stream error (network
/// drops, server restarts). Hard stops — session switch, auth failure, or
/// incompatible protocol — never auto-reconnect. The `sleep` closure is
/// injectable so tests can make backoff instantaneous and deterministic.
public struct EventStreamReconnectPolicy: Sendable {
    public let enabled: Bool
    public let maxAttempts: Int
    private let sleepClosure: @Sendable (Duration) async -> Void

    public init(
        enabled: Bool,
        maxAttempts: Int,
        sleep: @escaping @Sendable (Duration) async -> Void
    ) {
        self.enabled = enabled
        self.maxAttempts = max(0, maxAttempts)
        self.sleepClosure = sleep
    }

    /// Production policy: up to 5 attempts with real exponential backoff.
    public static let `default` = EventStreamReconnectPolicy(
        enabled: true,
        maxAttempts: 5,
        sleep: { try? await Task.sleep(for: $0) }
    )

    /// Disables automatic reconnect entirely. Used as the test default so that
    /// error-handling tests stay deterministic without per-test opt-out.
    public static let disabled = EventStreamReconnectPolicy(
        enabled: false,
        maxAttempts: 0,
        sleep: { _ in }
    )

    /// Waits out one backoff delay. No-op when disabled.
    public func sleep(for duration: Duration) async {
        guard enabled else { return }
        await sleepClosure(duration)
    }

    /// Whether more reconnect attempts are allowed after `attemptsSoFar`.
    public func canRetry(after attemptsSoFar: Int) -> Bool {
        enabled && attemptsSoFar < maxAttempts
    }
}

extension EventStreamReconnectPolicy: Equatable {
    public static func == (lhs: EventStreamReconnectPolicy, rhs: EventStreamReconnectPolicy) -> Bool {
        lhs.enabled == rhs.enabled && lhs.maxAttempts == rhs.maxAttempts
    }
}
