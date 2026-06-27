import Foundation

/// Outcome of a single coordinated refresh attempt.
public enum WorkbenchRefreshOutcome: Sendable, Equatable {
    /// The refresh operation ran to completion.
    case refreshed
    /// Another refresh was already running, so this tick was skipped.
    case skippedInProgress
}

/// Coordinates periodic auto-refresh of the daemon/session snapshot data.
///
/// The coordinator owns the refresh timing and reentrancy guard so that SwiftUI
/// views do not need their own timers. It is designed to be easy to replace
/// later with a WebSocket-driven push model: only this type needs to change,
/// while callers keep using `refreshOnce()` or `startPeriodicRefresh()`.
@MainActor
public final class WorkbenchRefreshCoordinator: Sendable {
    public var refreshInterval: Duration

    private let daemonController: DaemonController?
    private let refreshOperation: @MainActor () async -> Void
    private var isRefreshing = false

    /// Creates a coordinator that refreshes through the given daemon controller.
    public init(
        daemonController: DaemonController,
        refreshInterval: Duration = .seconds(5)
    ) {
        self.daemonController = daemonController
        self.refreshInterval = refreshInterval
        self.refreshOperation = { await daemonController.refreshConnection() }
    }

    /// Creates a coordinator with a custom refresh operation.
    ///
    /// Used by tests to inject a controllable refresh closure.
    internal init(
        refreshInterval: Duration = .seconds(5),
        refreshOperation: @escaping @MainActor () async -> Void
    ) {
        self.daemonController = nil
        self.refreshInterval = refreshInterval
        self.refreshOperation = refreshOperation
    }

    /// Runs one refresh if no refresh is currently running.
    ///
    /// - Returns: `.refreshed` when the operation completed, or
    ///   `.skippedInProgress` if another refresh was already in flight.
    public func refreshOnce() async -> WorkbenchRefreshOutcome {
        guard !isRefreshing else {
            return .skippedInProgress
        }

        isRefreshing = true
        defer { isRefreshing = false }

        await refreshOperation()
        return .refreshed
    }

    /// Starts an endless refresh loop that sleeps between ticks.
    ///
    /// The loop respects task cancellation: as soon as the surrounding SwiftUI
    /// `.task` is cancelled, the sleep is interrupted and the loop exits.
    public func startPeriodicRefresh() async {
        while !Task.isCancelled {
            _ = await refreshOnce()

            // Sleep until the next tick. Cancellation immediately ends the loop
            // without leaving multiple timers running.
            try? await Task.sleep(for: refreshInterval)
        }
    }
}
