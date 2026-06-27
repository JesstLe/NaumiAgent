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

    /// Fetches context health snapshots for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the snapshots
    /// are written to `appState.contextSnapshots`; on failure `appState.lastError`
    /// is set. Missing session clears the local snapshot list to avoid showing
    /// stale data from another session. API failures leave the local list
    /// unchanged (no fake local snapshots).
    public func refreshContextSnapshots(
        taskID: String? = nil,
        agentID: String? = nil,
        limit: Int = 50
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.contextSnapshots = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchContextSnapshots(
                sessionID: sessionID,
                taskID: taskID,
                agentID: agentID,
                limit: limit
            )
            appState.contextSnapshots = response.contextSnapshots
        } catch {
            appState.lastError = error
        }
    }

    /// Fetches approval requests for the currently selected session.
    ///
    /// Defaults to returning only `waiting` approvals so the UI can present a
    /// pending-approvals list. Requires `appState.selectedSessionID` to be set.
    /// On success the approvals are written to `appState.approvals`; on failure
    /// `appState.lastError` is set. Missing session clears the local approvals
    /// list to avoid showing stale data from another session. API failures leave
    /// the local list unchanged.
    public func refreshApprovals(state: String? = "waiting", limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.approvals = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchApprovals(
                sessionID: sessionID,
                state: state,
                limit: limit
            )
            appState.approvals = response.approvals
        } catch {
            appState.lastError = error
        }
    }

    /// Fetches failure cards for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the failures
    /// are written to `appState.failures`; on failure `appState.lastError` is
    /// set. Missing session clears the local failures list to avoid showing
    /// stale data from another session. API failures leave the local list
    /// unchanged.
    public func refreshFailures(taskID: String? = nil, status: String? = nil, limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.failures = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchFailures(
                sessionID: sessionID,
                taskID: taskID,
                status: status,
                limit: limit
            )
            appState.failures = response.failures
        } catch {
            appState.lastError = error
        }
    }

    /// Fetches issues for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the issues
    /// are written to `appState.issues`; on failure `appState.lastError` is
    /// set. Missing session clears the local issues list to avoid showing
    /// stale data from another session. API failures leave the local list
    /// unchanged.
    public func refreshIssues(missionID: String? = nil, riskLevel: String? = nil, limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.issues = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchIssues(
                sessionID: sessionID,
                missionID: missionID,
                riskLevel: riskLevel,
                limit: limit
            )
            appState.issues = response.issues
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

    /// Claims an issue on behalf of an agent and refreshes the snapshot and
    /// issues list on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot and issues are never mutated
    /// directly.
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
            await refreshIssues()
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

    /// Expires overdue leases in the selected session and refreshes the snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot is never mutated directly.
    public func expireLeases() async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.expireLeases(sessionID: sessionID)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Creates a mission in the selected session and refreshes the snapshot
    /// and issues list on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot and issues are never mutated
    /// directly.
    public func createMission(title: String, goal: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.createMission(
                sessionID: sessionID,
                title: title,
                goal: goal
            )
            await refreshIssues()
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Attaches an issue to a mission and refreshes the snapshot and the
    /// filtered issues list for that mission on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot and issues are never mutated
    /// directly.
    public func attachIssue(
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.attachIssue(
                sessionID: sessionID,
                missionID: missionID,
                taskID: taskID,
                acceptanceCriteria: acceptanceCriteria,
                parallelMode: parallelMode,
                riskLevel: riskLevel
            )
            await refreshIssues(missionID: missionID)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Creates an intent lock for a mission and refreshes the snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot is never mutated directly.
    public func createIntentLock(
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.createIntentLock(
                sessionID: sessionID,
                missionID: missionID,
                actor: actor,
                rule: rule,
                blockedPaths: blockedPaths,
                allowedPaths: allowedPaths,
                requireProposalForRisk: requireProposalForRisk
            )
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Creates a decision for a mission and refreshes the snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot is never mutated directly.
    public func createDecision(
        missionID: String,
        actor: String,
        kind: String,
        title: String,
        content: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.createDecision(
                sessionID: sessionID,
                missionID: missionID,
                kind: kind,
                title: title,
                content: content,
                actor: actor
            )
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Resolves an approval request as approved or rejected and refreshes the
    /// snapshot and waiting approvals list on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot and approvals are never mutated
    /// directly.
    public func resolveApproval(
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.resolveApproval(
                sessionID: sessionID,
                approvalID: approvalID,
                actor: actor,
                state: state,
                decisionNote: decisionNote
            )
            await refreshSnapshot()
            await refreshApprovals(state: "waiting")
        } catch {
            appState.lastError = error
        }
    }

    /// Runs a validation command and refreshes validation runs, failures, and
    /// snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success, `validationRuns`,
    /// `failures`, and `snapshot` are refreshed from the backend; on failure
    /// `lastError` is set and the existing local state is preserved.
    public func runValidation(
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String? = nil
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.runValidation(
                sessionID: sessionID,
                taskID: taskID,
                actor: actor,
                argv: argv,
                cwd: cwd
            )
            await refreshValidationRuns(taskID: taskID)
            await refreshFailures(taskID: taskID)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }
}
