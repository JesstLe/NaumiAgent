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
    public static let bootstrapSessionCandidateCount = 5

    public let appState: AppState
    public let apiProvider: WorkbenchAPIProviding
    public let eventProvider: (any WorkbenchEventProviding)?
    private var eventStreamTask: Task<Void, Never>?
    private var activeEventStream: (any WorkbenchEventStreaming)?

    /// Whether a Workbench event stream is currently connected and probeable.
    public var hasActiveEventStream: Bool {
        activeEventStream != nil
    }

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
    ///   states (missions, agent profiles, issues, leases, worktrees, failures, events,
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
            let bootstrap = try await apiProvider.fetchBootstrap(
                pageSize: Self.bootstrapSessionCandidateCount
            )
            let capabilities = bootstrap.capabilities
            guard capabilities.protocolVersion == Self.supportedProtocolVersion else {
                await stopEventStream()
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
                await refreshSnapshot(clearSessionScopedStateOnFailure: true)
            }
            await refreshWorkbenchListsAfterConnection()
            await startEventStreamIfAvailable()
        } catch {
            await stopEventStream()
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

    /// Requests a filtered replay from the active Workbench event stream.
    ///
    /// This does not mutate local business state directly. The backend will emit
    /// matching `workbench.event` messages followed by `refresh_complete`, and
    /// existing event handling will refresh the authoritative snapshot when
    /// events arrive.
    public func requestEventStreamRefresh(
        eventType: String? = nil,
        subjectID: String? = nil,
        actor: String? = nil,
        limit: Int = 50
    ) async {
        guard let activeEventStream else {
            appState.lastError = .networkFailure("事件流尚未连接")
            return
        }

        appState.lastError = nil
        do {
            try await activeEventStream.requestRefresh(
                eventType: eventType,
                subjectID: subjectID,
                actor: actor,
                limit: limit
            )
        } catch {
            self.activeEventStream = nil
            appState.connectionState = .stale
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Sends a lightweight liveness probe over the active Workbench event stream.
    public func pingEventStream() async {
        guard let activeEventStream else {
            appState.lastError = .networkFailure("事件流尚未连接")
            return
        }

        appState.lastError = nil
        do {
            try await activeEventStream.sendPing()
        } catch {
            self.activeEventStream = nil
            appState.connectionState = .stale
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
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
            activeEventStream = nil
            appState.connectionState = .stale
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    private func handleEventStreamMessage(_ message: WorkbenchEventStreamMessage) async {
        switch message {
        case .connected:
            if appState.connectionState == .stale {
                appState.connectionState = .connected
                await refreshSnapshot()
                if appState.snapshot != nil {
                    await refreshWorkbenchListsAfterConnection()
                }
            }
        case .snapshot(let snapshot):
            if let selectedSessionID = appState.selectedSessionID,
               selectedSessionID != snapshot.sessionID {
                return
            }
            appState.selectedSessionID = snapshot.sessionID
            appState.snapshot = snapshot
            appState.connectionState = .connected
            appState.lastError = nil
            await refreshWorkbenchListsAfterConnection()
        case .event:
            await refreshSnapshot()
            if appState.snapshot != nil {
                await refreshWorkbenchListsAfterConnection()
            }
        case .error(let message):
            appState.connectionState = .stale
            let error = apiError(forEventStreamError: message)
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        case .refreshComplete:
            break
        case .pong:
            break
        case .ignored:
            break
        }
    }

    private func apiError(forEventStreamError message: String) -> APIError {
        let normalizedMessage = message.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalizedMessage == "invalid api key" || normalizedMessage == "unauthorized" {
            return .authFailed
        }
        if normalizedMessage == "session not found" {
            return .sessionUnavailable
        }
        return .networkFailure(message)
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
        await refreshWorktrees()
        preWarmError = preWarmError ?? appState.lastError
        await refreshFailures()
        preWarmError = preWarmError ?? appState.lastError
        await refreshEvents(limit: 50)
        preWarmError = preWarmError ?? appState.lastError
        await refreshApprovals(state: "waiting")
        preWarmError = preWarmError ?? appState.lastError
        if let missionID = appState.snapshot?.missions.first?.id ?? appState.missions.first?.id {
            await refreshDecisions(missionID: missionID)
            preWarmError = preWarmError ?? appState.lastError
            await refreshIntentLocks(missionID: missionID)
            preWarmError = preWarmError ?? appState.lastError
        } else {
            appState.decisions = []
            appState.intentLocks = []
        }
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

    /// Loads one context health snapshot into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected context snapshot is preserved.
    public func loadContextSnapshot(snapshotID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedContextSnapshot = try await apiProvider.fetchContextSnapshot(
                sessionID: sessionID,
                snapshotID: snapshotID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Records a context health update for the currently selected session and issue.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the returned
    /// context snapshot is inserted at the front of `appState.contextSnapshots`
    /// (removing any previous entry with the same ID to avoid duplicates), and
    /// the backend-provided workbench snapshot becomes the global truth; on
    /// failure `appState.lastError` is set. Missing session clears the local
    /// snapshot list to avoid showing stale data from another session. API
    /// failures leave the local list unchanged.
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
            let response = try await apiProvider.recordContextHealthWithSnapshot(
                sessionID: sessionID,
                taskID: taskID,
                agentID: agentID,
                minutesSinceSync: minutesSinceSync,
                tokenLoadRatio: tokenLoadRatio,
                policyConflict: policyConflict,
                actor: actor
            )
            appState.snapshot = response.snapshot
            var snapshots = appState.contextSnapshots
            snapshots.removeAll { $0.id == response.contextSnapshot.id }
            appState.contextSnapshots = [response.contextSnapshot] + snapshots
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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

    /// Loads one failure card into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected failure is preserved.
    public func loadFailure(failureID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedFailure = try await apiProvider.fetchFailure(
                sessionID: sessionID,
                failureID: failureID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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

    /// Loads one issue into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected issue is preserved.
    public func loadIssue(taskID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedIssue = try await apiProvider.fetchIssue(
                sessionID: sessionID,
                taskID: taskID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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

    /// Loads one lease into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected lease is preserved.
    public func loadLease(leaseID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedLease = try await apiProvider.fetchLease(
                sessionID: sessionID,
                leaseID: leaseID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Fetches worktrees for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the worktrees
    /// are written to `appState.worktrees`; on failure `appState.lastError` is
    /// set. Missing session clears the local worktree list to avoid showing
    /// stale data from another session. API failures leave the local list
    /// unchanged.
    public func refreshWorktrees(taskID: String? = nil, status: String? = nil, limit: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.worktrees = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchWorktrees(
                sessionID: sessionID,
                taskID: taskID,
                status: status,
                limit: limit
            )
            appState.worktrees = response.worktrees
        } catch {
            appState.lastError = error
        }
    }

    /// Loads one worktree into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected worktree is preserved.
    public func loadWorktree(name: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedWorktree = try await apiProvider.fetchWorktree(
                sessionID: sessionID,
                name: name
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Marks a worktree as kept and refreshes worktrees plus audit events.
    ///
    /// Requires `appState.selectedSessionID` to be set. The mutation response
    /// supplies the authoritative snapshot; on success the local worktree list
    /// and timeline events are refreshed from the backend. Failures preserve
    /// existing local worktree state and record `lastError`.
    public func keepWorktree(name: String, actor: String, reason: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.keepWorktreeWithSnapshot(
                sessionID: sessionID,
                name: name,
                actor: actor,
                reason: reason
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshWorktrees()
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Removes a tracked worktree and refreshes worktrees plus audit events.
    ///
    /// Requires `appState.selectedSessionID` to be set. Safe removal leaves
    /// dirty worktrees untouched on the backend unless `discardChanges` is true.
    /// The mutation response supplies the authoritative snapshot. Failures
    /// preserve the local worktree list and record `lastError`.
    public func removeWorktree(name: String, discardChanges: Bool) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.removeWorktreeWithSnapshot(
                sessionID: sessionID,
                name: name,
                discardChanges: discardChanges
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshWorktrees()
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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

    /// Loads one mission into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected mission is preserved.
    public func loadMission(missionID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedMission = try await apiProvider.fetchMission(
                sessionID: sessionID,
                missionID: missionID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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

    /// Loads one agent profile into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected agent profile is preserved.
    public func loadAgentProfile(agentID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedAgentProfile = try await apiProvider.fetchAgentProfile(
                sessionID: sessionID,
                agentID: agentID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Fetches human-governance decisions for the selected session and mission.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the decisions
    /// are written to `appState.decisions`; on failure `appState.lastError` is
    /// set. Missing session clears the local decision list to avoid showing
    /// stale governance records from another session. API failures leave the
    /// local list unchanged.
    public func refreshDecisions(missionID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.decisions = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchDecisions(
                sessionID: sessionID,
                missionID: missionID
            )
            appState.decisions = response.decisions
        } catch {
            appState.lastError = error
        }
    }

    /// Fetches intent locks for the selected session and mission.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the locks are
    /// written to `appState.intentLocks`; on failure `appState.lastError` is set.
    /// Missing session clears the local lock list to avoid showing stale policy
    /// records from another session. API failures leave the local list unchanged.
    public func refreshIntentLocks(missionID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.intentLocks = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchIntentLocks(
                sessionID: sessionID,
                missionID: missionID
            )
            appState.intentLocks = response.intentLocks
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Registers or updates an agent capability profile in the selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the profile
    /// is registered with the API, then `agentProfiles`, timeline events, and
    /// snapshot are refreshed from authoritative backend responses; on failure
    /// `appState.lastError` is set and the existing local `agentProfiles` and
    /// snapshot are preserved.
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
            let response = try await apiProvider.registerAgentProfileWithSnapshot(
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
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshAgentProfiles()
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
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

    /// Creates a new backend Workbench session and applies the returned
    /// bootstrap snapshot as the authoritative first UI state.
    ///
    /// This action is intentionally available without an existing selected
    /// session so the Mac app can recover from an empty bootstrap state.
    public func createSession(
        title: String?,
        model: String?,
        systemPrompt: String?
    ) async {
        appState.lastError = nil

        do {
            let bootstrap = try await apiProvider.createWorkbenchSession(
                title: title,
                model: model,
                systemPrompt: systemPrompt
            )
            let capabilities = bootstrap.capabilities
            guard capabilities.protocolVersion == Self.supportedProtocolVersion else {
                await stopEventStream()
                appState.daemonStatus = nil
                appState.capabilities = nil
                appState.lastError = .protocolVersionMismatch(
                    expected: Self.supportedProtocolVersion,
                    actual: capabilities.protocolVersion
                )
                appState.connectionState = .disconnected
                return
            }

            await stopEventStream()
            appState.daemonStatus = bootstrap.daemonStatus
            appState.capabilities = capabilities
            for session in bootstrap.sessions.reversed() {
                appState.sessions.removeAll { $0.id == session.id }
                appState.sessions.insert(session, at: 0)
            }
            appState.selectedSessionID = bootstrap.selectedSessionID ?? bootstrap.sessions.first?.id
            clearSessionScopedState()
            appState.snapshot = bootstrap.snapshot

            guard appState.snapshot != nil else {
                return
            }

            await refreshWorkbenchListsAfterConnection()
            await startEventStreamIfAvailable()
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
        appState.worktrees = []
        appState.missions = []
        appState.agentProfiles = []
        appState.decisions = []
        appState.intentLocks = []
        appState.selectedEvent = nil
        appState.selectedValidationRun = nil
        appState.selectedContextSnapshot = nil
        appState.selectedFailure = nil
        appState.selectedIssue = nil
        appState.selectedLease = nil
        appState.selectedWorktree = nil
        appState.selectedMission = nil
        appState.selectedAgentProfile = nil
        appState.selectedDecision = nil
        appState.selectedIntentLock = nil
        appState.selectedApproval = nil
    }

    private func clearUnavailableSelectedSession() {
        appState.selectedSessionID = nil
        clearSessionScopedState()
    }

    /// Refreshes the snapshot for the currently selected session.
    ///
    /// When no session is selected, the most recent session from `GET /sessions`
    /// is chosen automatically and the fetched session list is also written to
    /// `appState.sessions`. Failures are written to `appState.lastError` and the
    /// snapshot is cleared to avoid showing stale data. If the backend reports
    /// that the selected session no longer exists, the selection is cleared too.
    func refreshSnapshot(clearSessionScopedStateOnFailure: Bool = false) async {
        let sessionID: String
        if let existingID = appState.selectedSessionID {
            sessionID = existingID
        } else {
            do {
                let list = try await apiProvider.fetchSessions(page: 1, pageSize: 1)
                appState.sessions = list.sessions
                guard let firstSession = list.sessions.first else {
                    clearSessionScopedState()
                    return
                }
                sessionID = firstSession.id
                appState.selectedSessionID = sessionID
            } catch {
                appState.lastError = error
                clearSessionScopedState()
                return
            }
        }

        do {
            let snapshot = try await apiProvider.fetchSnapshot(sessionID: sessionID)
            appState.snapshot = snapshot
        } catch APIError.sessionUnavailable {
            appState.lastError = APIError.sessionUnavailable
            clearUnavailableSelectedSession()
        } catch {
            appState.lastError = error
            appState.snapshot = nil
            if clearSessionScopedStateOnFailure {
                clearSessionScopedState()
            }
        }
    }

    /// Claims an issue on behalf of an agent and refreshes leases, issues,
    /// timeline events, and the authoritative snapshot on success.
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
            let response = try await apiProvider.claimIssueWithSnapshot(
                sessionID: sessionID,
                taskID: taskID,
                agentID: agentID,
                durationMinutes: durationMinutes,
                worktreeName: worktreeName
            )
            appState.snapshot = response.snapshot
            await refreshLeases()
            await refreshIssues()
            await refreshEvents(limit: 50)
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Releases a lease and refreshes the leases and timeline events on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. The mutation response
    /// supplies the authoritative snapshot; follow-up refresh failures are
    /// preserved without mutating the local leases list directly.
    public func releaseLease(leaseID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.releaseLeaseWithSnapshot(
                sessionID: sessionID,
                leaseID: leaseID
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshLeases()
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Expires overdue leases in the selected session and refreshes the leases
    /// and timeline events on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. The mutation response
    /// supplies the authoritative snapshot; follow-up refresh failures are
    /// preserved without mutating the local leases list directly.
    public func expireLeases() async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.expireLeasesWithSnapshot(sessionID: sessionID)
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshLeases()
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Creates a mission in the selected session and refreshes the missions,
    /// issues, and timeline events on success. The global snapshot is taken
    /// from the mutation response so the first screen can update without an
    /// extra snapshot fetch.
    ///
    /// If no session is selected, creates a default session named after the
    /// mission first so the first-run "New Mission" action can recover an empty
    /// bootstrap state. Failures are recorded in `appState.lastError`, including
    /// the first follow-up list refresh failure after a successful mutation; the
    /// local missions and issues are never mutated directly.
    public func createMission(title: String, goal: String) async {
        if appState.selectedSessionID == nil {
            await createSession(title: title, model: nil, systemPrompt: nil)
        }

        guard let sessionID = appState.selectedSessionID else {
            if appState.lastError == nil {
                appState.lastError = .missingSelectedSession
            }
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.createMissionWithSnapshot(
                sessionID: sessionID,
                title: title,
                goal: goal
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshMissions()
            refreshError = refreshError ?? appState.lastError
            await refreshIssues()
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Attaches an issue to a mission and refreshes the filtered issues list
    /// and timeline events on success. The global snapshot is taken from the
    /// mutation response so the dashboard can update without an extra snapshot
    /// fetch.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`, including the first follow-up list refresh failure;
    /// the local issues list is never mutated directly.
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
            let response = try await apiProvider.attachIssueWithSnapshot(
                sessionID: sessionID,
                missionID: missionID,
                taskID: taskID,
                acceptanceCriteria: acceptanceCriteria,
                parallelMode: parallelMode,
                riskLevel: riskLevel
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshIssues(missionID: missionID)
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Creates a backing task, attaches it as an issue, and refreshes the mission
    /// issue list and timeline events on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. The mutation response
    /// supplies the authoritative snapshot; follow-up list refresh failures are
    /// preserved without mutating the local issue list directly.
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
            let response = try await apiProvider.createIssueWithSnapshot(
                sessionID: sessionID,
                missionID: missionID,
                title: title,
                description: description,
                blockedBy: blockedBy,
                acceptanceCriteria: acceptanceCriteria,
                parallelMode: parallelMode,
                riskLevel: riskLevel
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshIssues(missionID: missionID)
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Creates an intent lock for a mission and refreshes the intent-lock list
    /// and timeline events on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. The mutation response
    /// supplies the authoritative snapshot; follow-up refresh failures are
    /// preserved without mutating the local intent-lock list directly.
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
            let response = try await apiProvider.createIntentLockWithSnapshot(
                sessionID: sessionID,
                missionID: missionID,
                actor: actor,
                rule: rule,
                blockedPaths: blockedPaths,
                allowedPaths: allowedPaths,
                requireProposalForRisk: requireProposalForRisk
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshIntentLocks(missionID: missionID)
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Creates a decision for a mission and refreshes the decision list and
    /// timeline events on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. The mutation response
    /// supplies the authoritative snapshot; follow-up refresh failures are
    /// preserved without mutating the local decision list directly.
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
            let response = try await apiProvider.createDecisionWithSnapshot(
                sessionID: sessionID,
                missionID: missionID,
                kind: kind,
                title: title,
                content: content,
                actor: actor
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshDecisions(missionID: missionID)
            refreshError = refreshError ?? appState.lastError
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Loads one audit event into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected event is preserved.
    public func loadEvent(eventID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedEvent = try await apiProvider.fetchEvent(
                sessionID: sessionID,
                eventID: eventID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Loads one governance decision into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected decision is preserved.
    public func loadDecision(
        missionID: String,
        decisionID: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedDecision = try await apiProvider.fetchDecision(
                sessionID: sessionID,
                missionID: missionID,
                decisionID: decisionID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Loads one intent lock into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected intent lock is preserved.
    public func loadIntentLock(
        missionID: String,
        lockID: String
    ) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedIntentLock = try await apiProvider.fetchIntentLock(
                sessionID: sessionID,
                missionID: missionID,
                lockID: lockID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Loads one approval request into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected approval is preserved.
    public func loadApproval(approvalID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedApproval = try await apiProvider.fetchApproval(
                sessionID: sessionID,
                approvalID: approvalID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Resolves an approval request as approved or rejected and refreshes the
    /// timeline events and waiting approvals list on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. The mutation response
    /// supplies the authoritative snapshot; follow-up refresh failures are
    /// preserved without mutating the local approvals list directly.
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
            let response = try await apiProvider.resolveApprovalWithSnapshot(
                sessionID: sessionID,
                approvalID: approvalID,
                actor: actor,
                state: state,
                decisionNote: decisionNote
            )
            appState.snapshot = response.snapshot
            var refreshError: APIError?
            await refreshEvents(limit: 50)
            refreshError = refreshError ?? appState.lastError
            await refreshApprovals(state: "waiting")
            refreshError = refreshError ?? appState.lastError
            appState.lastError = refreshError
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Loads one validation run into the selected detail state.
    ///
    /// Requires `appState.selectedSessionID` to be set. Failures are recorded in
    /// `appState.lastError`; the previous selected validation run is preserved.
    public func loadValidationRun(runID: String) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            appState.selectedValidationRun = try await apiProvider.fetchValidationRun(
                sessionID: sessionID,
                runID: runID
            )
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Runs a validation command and refreshes validation runs, failures,
    /// timeline events, and the backend-provided snapshot on success.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success,
    /// `snapshot` uses the mutation response, while `validationRuns`,
    /// `failures`, and `timelineEvents` are refreshed from the backend;
    /// on failure `lastError` is set and the
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
            let response = try await apiProvider.runValidationWithSnapshot(
                sessionID: sessionID,
                taskID: taskID,
                actor: actor,
                argv: argv,
                cwd: cwd
            )
            appState.snapshot = response.snapshot
            await refreshValidationRuns(taskID: taskID)
            await refreshFailures(taskID: taskID)
            await refreshEvents(limit: 50)
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }
}
