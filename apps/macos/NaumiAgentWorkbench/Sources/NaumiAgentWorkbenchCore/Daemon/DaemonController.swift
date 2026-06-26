import Foundation
import Observation

/// Orchestrates the connection to the local NaumiAgent daemon.
///
/// This first slice only refreshes the connection against an already-running
/// localhost API. It does not start/stop the daemon process and does not use
/// WebSockets.
@MainActor
public final class DaemonController: Sendable {
    public let appState: AppState
    public let apiProvider: WorkbenchAPIProviding

    public init(
        appState: AppState,
        apiProvider: WorkbenchAPIProviding
    ) {
        self.appState = appState
        self.apiProvider = apiProvider
    }

    /// Refreshes the daemon connection by fetching status and capabilities.
    ///
    /// - Sets `connectionState` to `.connecting` and clears `lastError`.
    /// - On success, writes `daemonStatus` and `capabilities`, then sets `.connected`.
    /// - On failure, writes the `APIError` to `lastError` and sets `.disconnected`.
    public func refreshConnection() async {
        appState.connectionState = .connecting
        appState.lastError = nil

        do {
            let status = try await apiProvider.fetchDaemonStatus()
            let capabilities = try await apiProvider.fetchCapabilities()

            appState.daemonStatus = status
            appState.capabilities = capabilities
            appState.connectionState = .connected
        } catch {
            appState.lastError = error
            appState.connectionState = .disconnected
        }
    }
}
