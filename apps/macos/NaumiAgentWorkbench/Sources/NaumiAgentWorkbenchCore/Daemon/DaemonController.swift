import Foundation
import Observation

/// Orchestrates the connection to the local NaumiAgent daemon.
///
/// This controller refreshes connection state against an already-running
/// localhost API and can subscribe to Workbench WebSocket events. It does not
/// start/stop the daemon process.
@MainActor
public final class DaemonController: Sendable {
    public static let supportedProtocolVersion = 1

    public let appState: AppState
    public let apiProvider: WorkbenchAPIProviding
    public let eventProvider: (any WorkbenchEventProviding)?
    private var eventStreamTask: Task<Void, Never>?
    private var activeEventStream: (any WorkbenchEventStreaming)?

    public init(
        appState: AppState,
        apiProvider: WorkbenchAPIProviding,
        eventProvider: (any WorkbenchEventProviding)? = nil
    ) {
        self.appState = appState
        self.apiProvider = apiProvider
        self.eventProvider = eventProvider
    }

    /// Refreshes the daemon connection by fetching the bootstrap payload.
    ///
    /// - Sets `connectionState` to `.connecting` and clears `lastError`.
    /// - On success, writes `daemonStatus`, `capabilities`, recent sessions, and
    ///   the bootstrap snapshot, then sets `.connected`.
    /// - If a session is already selected, preserves that selection and refreshes
    ///   its snapshot instead of switching to the bootstrap latest session.
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
            let bootstrap = try await apiProvider.fetchBootstrap(pageSize: 1)
            let capabilities = bootstrap.capabilities
            guard capabilities.protocolVersion == Self.supportedProtocolVersion else {
                appState.daemonStatus = nil
                appState.capabilities = nil
                appState.lastError = .protocolVersionMismatch(
                    expected: Self.supportedProtocolVersion,
                    actual: capabilities.protocolVersion
                )
                appState.connectionState = .disconnected
                return
            }

            appState.daemonStatus = bootstrap.daemonStatus
            appState.capabilities = capabilities
            appState.sessions = bootstrap.sessions
            appState.connectionState = .connected

            if appState.selectedSessionID == nil {
                appState.selectedSessionID = bootstrap.selectedSessionID
                appState.snapshot = bootstrap.snapshot
            } else {
                await refreshSnapshot()
            }
            await refreshWorkbenchListsAfterConnection()
            await startEventStreamIfAvailable()
        } catch {
            appState.lastError = error
            appState.connectionState = .disconnected
        }
    }

    /// Starts listening to Workbench event hints for the selected session.
    ///
    /// Incoming `workbench.event` messages never mutate business state directly:
    /// they trigger a fresh snapshot/list refresh because the backend remains
    /// the source of truth. Stream failures mark the connection stale so the UI
    /// can prompt the user to refresh instead of trusting old incremental data.
    public func startEventStream() async {
        guard let eventProvider else {
            return
        }
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        await stopEventStream()
        appState.lastError = nil
        eventStreamTask = Task { [weak self, eventProvider] in
            await self?.consumeEventStream(
                sessionID: sessionID,
                eventProvider: eventProvider
            )
        }
    }

    /// Stops any active Workbench event stream subscription.
    public func stopEventStream() async {
        eventStreamTask?.cancel()
        eventStreamTask = nil
        await activeEventStream?.cancel()
        activeEventStream = nil
    }

    private func startEventStreamIfAvailable() async {
        guard eventProvider != nil, appState.selectedSessionID != nil else {
            return
        }
        await startEventStream()
    }

    private func consumeEventStream(
        sessionID: String,
        eventProvider: any WorkbenchEventProviding
    ) async {
        do {
            let stream = try await eventProvider.connect(sessionID: sessionID)
            activeEventStream = stream
            while !Task.isCancelled {
                let message = try await stream.next()
                await handleEventStreamMessage(message)
            }
        } catch {
            guard !Task.isCancelled else {
                return
            }
            appState.connectionState = .stale
            appState.lastError = error
        }
    }

    private func handleEventStreamMessage(_ message: WorkbenchEventStreamMessage) async {
        switch message {
        case .connected:
            if appState.connectionState == .stale {
                appState.connectionState = .connected
            }
        case .event:
            await refreshSnapshot()
            if appState.snapshot != nil {
                await refreshWorkbenchListsAfterConnection()
            }
        case .error(let message):
            appState.connectionState = .stale
            appState.lastError = .networkFailure(message)
        case .ignored:
            break
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

    /// Refreshes the paginated session list and writes it to `appState.sessions`.
    ///
    /// This method does not require a selected session. On success it updates
    /// `sessions` and clears `lastError`. On API failure the existing session
    /// list is preserved and the error is recorded in `lastError`.
    public func refreshSessions(page: Int, pageSize: Int) async {
        do {
            let list = try await apiProvider.fetchSessions(page: page, pageSize: pageSize)
            appState.sessions = list.sessions
            appState.lastError = nil
        } catch {
            appState.lastError = error
        }
    }

    /// Selects a session by ID, clearing any session-scoped local state to avoid
    /// stale cross-session data, then fetches its snapshot.
    ///
    /// If the snapshot is fetched successfully the lightweight first-screen
    /// workbench lists are pre-warmed using the same semantics as
    /// `refreshConnection()`. On snapshot failure the selection remains set to
    /// the requested ID, the local session-scoped lists stay cleared, and the
    /// error is recorded in `lastError`.
    public func selectSession(_ sessionID: String) async {
        await stopEventStream()
        appState.selectedSessionID = sessionID
        clearSessionScopedState()
        appState.lastError = nil

        await refreshSnapshot()

        guard appState.snapshot != nil else {
            return
        }

        await refreshWorkbenchListsAfterConnection()
        await startEventStreamIfAvailable()
    }

    /// Clears local state that is scoped to the currently selected session.
    ///
    /// This is used when switching sessions so that the UI never shows data from
    /// a previously selected session. Global state such as `sessions`,
    /// `daemonStatus`, `capabilities`, `connectionState`, `locale`, and
    /// `selectedSessionID` are intentionally preserved.
    private func clearSessionScopedState() {
        appState.snapshot = nil
        appState.timelineEvents = []
        appState.validationRuns = []
        appState.contextSnapshots = []
        appState.approvals = []
        appState.failures = []
        appState.issues = []
        appState.leases = []
        appState.missions = []
        appState.agentProfiles = []
    }

    /// Refreshes the snapshot for the currently selected session.
    ///
    /// When no session is selected, the most recent session from `GET /sessions`
    /// is chosen automatically and the fetched session list is also written to
    /// `appState.sessions`. Failures are written to `appState.lastError` and the
    /// snapshot is cleared to avoid showing stale data.
    func refreshSnapshot() async {
        let sessionID: String
        if let existingID = appState.selectedSessionID {
            sessionID = existingID
        } else {
            do {
                let list = try await apiProvider.fetchSessions(page: 1, pageSize: 1)
                appState.sessions = list.sessions
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

    /// Creates a backing task, attaches it as an issue, and refreshes the mission
    /// issue list, timeline events, and snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. The API remains the
    /// single writer; local state is refreshed from daemon responses.
    public func createIssue(
        missionID: String,
        title: String,
        description: String,
        blockedBy: [String],
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
            _ = try await apiProvider.createIssue(
                sessionID: sessionID,
                missionID: missionID,
                title: title,
                description: description,
                blockedBy: blockedBy,
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
