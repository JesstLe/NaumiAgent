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

    /// Fetches the most recent audit events for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the events are
    /// written to `appState.timelineEvents`; on failure `appState.lastError` is
    /// set. Missing session clears the local event list to avoid showing stale
    /// events from another session. API failures leave the local event list
    /// unchanged (no fake local events).
    public func refreshEvents(limit: Int) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.timelineEvents = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchEvents(sessionID: sessionID, limit: limit)
            appState.timelineEvents = response.events
        } catch {
            appState.lastError = error
        }
    }

    /// Fetches validation runs for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the runs are
    /// written to `appState.validationRuns`; on failure `appState.lastError` is
    /// set. Missing session clears the local validation run list to avoid showing
    /// stale data from another session. API failures leave the local list
    /// unchanged (no fake local runs).
    public func refreshValidationRuns(taskID: String? = nil, limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.validationRuns = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchValidationRuns(
                sessionID: sessionID,
                taskID: taskID,
                limit: limit
            )
            appState.validationRuns = response.validationRuns
        } catch {
            appState.lastError = error
        }
    }

    /// Refreshes the snapshot for the currently selected session.
    ///
    /// When no session is selected, the most recent session from `GET /sessions`
    /// is chosen automatically. Failures are written to `appState.lastError` and
    /// the snapshot is cleared to avoid showing stale data.
    func refreshSnapshot() async {
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

    /// Claims an issue on behalf of an agent and refreshes the snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot is never mutated directly.
    public func claimIssue(
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.claimIssue(
                sessionID: sessionID,
                taskID: taskID,
                agentID: agentID,
                durationMinutes: durationMinutes,
                worktreeName: worktreeName
            )
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Releases a lease and refreshes the snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot is never mutated directly.
    public func releaseLease(leaseID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.releaseLease(
                sessionID: sessionID,
                leaseID: leaseID
            )
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }
}
