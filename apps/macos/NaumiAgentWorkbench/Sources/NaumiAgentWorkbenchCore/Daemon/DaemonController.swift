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
    /// - After a successful snapshot, pre-warms the lightweight first-screen list
    ///   states (missions, agent profiles, issues, leases, failures, events,
    ///   waiting approvals, validation runs, and context snapshots). Failures are isolated to
    ///   `lastError` and do not affect the connection state.
    /// - Snapshot/session failures keep the daemon `.connected` and record the
    ///   error in `lastError` while clearing stale snapshot data. List pre-warming
    ///   is skipped in this case to avoid a half-ready UI state.
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
            await refreshWorkbenchListsAfterConnection()
        } catch {
            appState.lastError = error
            appState.connectionState = .disconnected
        }
    }

    /// Pre-warms lightweight first-screen list states after a successful connection.
    ///
    /// Called after `refreshSnapshot()` when a session is selected and its snapshot
    /// is available. Each refresh uses the existing `refreshX` method semantics:
    /// successes write to `appState`, failures preserve existing local data, and
    /// the first pre-warm failure remains visible in `lastError`. Connection state
    /// is never mutated here.
    private func refreshWorkbenchListsAfterConnection() async {
        guard appState.selectedSessionID != nil, appState.snapshot != nil else {
            return
        }

        var preWarmError: APIError?
        await refreshMissions()
        preWarmError = preWarmError ?? appState.lastError
        await refreshAgentProfiles()
        preWarmError = preWarmError ?? appState.lastError
        await refreshIssues()
        preWarmError = preWarmError ?? appState.lastError
        await refreshLeases()
        preWarmError = preWarmError ?? appState.lastError
        await refreshFailures()
        preWarmError = preWarmError ?? appState.lastError
        await refreshEvents(limit: 50)
        preWarmError = preWarmError ?? appState.lastError
        await refreshApprovals(state: "waiting")
        preWarmError = preWarmError ?? appState.lastError
        await refreshValidationRuns()
        preWarmError = preWarmError ?? appState.lastError
        await refreshContextSnapshots()
        preWarmError = preWarmError ?? appState.lastError

        if let preWarmError {
            appState.lastError = preWarmError
        }
    }

    /// Fetches the most recent audit events for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the events are
    /// written to `appState.timelineEvents`; on failure `appState.lastError` is
    /// set. Missing session clears the local event list to avoid showing stale
    /// events from another session. API failures leave the local event list
    /// unchanged (no fake local events).
    public func refreshEvents(
        eventType: String? = nil,
        subjectID: String? = nil,
        actor: String? = nil,
        limit: Int
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.timelineEvents = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchEvents(
                sessionID: sessionID,
                eventType: eventType,
                subjectID: subjectID,
                actor: actor,
                limit: limit
            )
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

    /// Records a context health update for the currently selected session and issue.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the returned
    /// snapshot is inserted at the front of `appState.contextSnapshots` (removing
    /// any previous entry with the same ID to avoid duplicates); on failure
    /// `appState.lastError` is set. Missing session clears the local snapshot list
    /// to avoid showing stale data from another session. API failures leave the
    /// local list unchanged.
    public func recordContextHealth(
        taskID: String,
        agentID: String,
        minutesSinceSync: Int,
        tokenLoadRatio: Double,
        policyConflict: Bool,
        actor: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.contextSnapshots = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let snapshot = try await apiProvider.recordContextHealth(
                sessionID: sessionID,
                taskID: taskID,
                agentID: agentID,
                minutesSinceSync: minutesSinceSync,
                tokenLoadRatio: tokenLoadRatio,
                policyConflict: policyConflict,
                actor: actor
            )
            var snapshots = appState.contextSnapshots
            snapshots.removeAll { $0.id == snapshot.id }
            appState.contextSnapshots = [snapshot] + snapshots
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

    /// Fetches leases for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the leases
    /// are written to `appState.leases`; on failure `appState.lastError` is
    /// set. Missing session clears the local leases list to avoid showing stale
    /// data from another session. API failures leave the local list unchanged.
    public func refreshLeases(state: String? = nil, taskID: String? = nil, agentID: String? = nil, limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.leases = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchLeases(
                sessionID: sessionID,
                state: state,
                taskID: taskID,
                agentID: agentID,
                limit: limit
            )
            appState.leases = response.leases
        } catch {
            appState.lastError = error
        }
    }

    /// Fetches missions for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the missions
    /// are written to `appState.missions`; on failure `appState.lastError` is
    /// set. Missing session clears the local missions list to avoid showing stale
    /// data from another session. API failures leave the local list
    /// unchanged.
    public func refreshMissions(status: String? = nil, limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.missions = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchMissions(
                sessionID: sessionID,
                status: status,
                limit: limit
            )
            appState.missions = response.missions
        } catch {
            appState.lastError = error
        }
    }

    /// Fetches agent capability profiles for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the agent
    /// profiles are written to `appState.agentProfiles`; on failure
    /// `appState.lastError` is set. Missing session clears the local agent
    /// profiles list to avoid showing stale data from another session. API
    /// failures leave the local list unchanged.
    public func refreshAgentProfiles(status: String? = nil, limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.agentProfiles = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchAgentProfiles(
                sessionID: sessionID,
                status: status,
                limit: limit
            )
            appState.agentProfiles = response.agentProfiles
        } catch {
            appState.lastError = error
        }
    }

    /// Registers or updates an agent capability profile in the selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the profile
    /// is registered with the API, then `agentProfiles`, timeline events, and
    /// snapshot are refreshed; on failure `appState.lastError` is set and the
    /// existing local `agentProfiles` and snapshot are preserved.
    public func registerAgentProfile(
        agentID: String,
        name: String,
        role: String,
        capabilities: [String],
        permissions: [String],
        maxParallelTasks: Int,
        status: String,
        actor: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.agentProfiles = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.registerAgentProfile(
                sessionID: sessionID,
                agentID: agentID,
                name: name,
                role: role,
                capabilities: capabilities,
                permissions: permissions,
                maxParallelTasks: maxParallelTasks,
                status: status,
                actor: actor
            )
            await refreshAgentProfiles()
            await refreshEvents(limit: 50)
            await refreshSnapshot()
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

    /// Claims an issue on behalf of an agent and refreshes the leases, issues,
    /// timeline events, and snapshot lists on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot, issues, and leases are never
    /// mutated directly.
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
            await refreshLeases()
            await refreshIssues()
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Releases a lease and refreshes the leases, timeline events, and
    /// snapshot lists on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot and leases are never mutated
    /// directly.
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
            await refreshLeases()
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Expires overdue leases in the selected session and refreshes the leases,
    /// timeline events, and snapshot lists on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot and leases are never mutated
    /// directly.
    public func expireLeases() async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            _ = try await apiProvider.expireLeases(sessionID: sessionID)
            await refreshLeases()
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Creates a mission in the selected session and refreshes the missions,
    /// issues, timeline events, and snapshot lists on success. Snapshot is
    /// refreshed last so that a snapshot failure does not wipe state already
    /// updated by earlier refreshes.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the local snapshot, missions, and issues are never
    /// mutated directly.
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
            await refreshMissions()
            await refreshIssues()
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Attaches an issue to a mission and refreshes the filtered issues list,
    /// timeline events, and snapshot for that mission on success.
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
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Creates an intent lock for a mission and refreshes the timeline events
    /// and snapshot on success.
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
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Creates a decision for a mission and refreshes the timeline events and
    /// snapshot on success.
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
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Resolves an approval request as approved or rejected and refreshes the
    /// timeline events, waiting approvals list, and snapshot on success.
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
            await refreshEvents(limit: 50)
            await refreshApprovals(state: "waiting")
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }

    /// Runs a validation command and refreshes validation runs, failures,
    /// timeline events, and snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success,
    /// `validationRuns`, `failures`, `timelineEvents`, and `snapshot` are
    /// refreshed from the backend; on failure `lastError` is set and the
    /// existing local state is preserved.
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

        if let capabilities = appState.capabilities, !capabilities.supportsValidationRunner {
            appState.lastError = .capabilityUnavailable("validation_runner")
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
            await refreshEvents(limit: 50)
            await refreshSnapshot()
        } catch {
            appState.lastError = error
        }
    }
}
