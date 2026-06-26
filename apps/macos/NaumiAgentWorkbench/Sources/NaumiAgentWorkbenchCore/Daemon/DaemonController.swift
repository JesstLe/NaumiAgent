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
    /// - If a session is already selected, refreshes its snapshot; otherwise picks
    ///   the most recent session from `GET /sessions` and refreshes that snapshot.
    /// - Snapshot/session failures keep the daemon `.connected` and record the
    ///   error in `lastError` while clearing stale snapshot data.
    /// - On status/capabilities failure, writes the `APIError` to `lastError`
    ///   and sets `.disconnected`.
    public func refreshConnection() async {
        appState.connectionState = .connecting
        appState.lastError = nil

        do {
            let status = try await apiProvider.fetchDaemonStatus()
            let capabilities = try await apiProvider.fetchCapabilities()

            appState.daemonStatus = status
            appState.capabilities = capabilities
            appState.connectionState = .connected

            await refreshSnapshot()
        } catch {
            appState.lastError = error
            appState.connectionState = .disconnected
        }
    }

    private func refreshSnapshot() async {
        let sessionID: String
        if let existingID = appState.selectedSessionID {
            sessionID = existingID
        } else {
            do {
                let list = try await apiProvider.fetchSessions(page: 1, pageSize: 1)
                guard let firstSession = list.sessions.first else {
                    appState.snapshot = nil
                    return
                }
                sessionID = firstSession.id
                appState.selectedSessionID = sessionID
            } catch {
                appState.lastError = error
                appState.snapshot = nil
                return
            }
        }

        do {
            let snapshot = try await apiProvider.fetchSnapshot(sessionID: sessionID)
            appState.snapshot = snapshot
        } catch {
            appState.lastError = error
            appState.snapshot = nil
        }
    }
}
