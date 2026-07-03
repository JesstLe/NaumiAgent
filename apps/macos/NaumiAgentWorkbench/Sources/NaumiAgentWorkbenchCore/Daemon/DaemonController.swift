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

    private static func nowISO8601() -> String {
        ISO8601DateFormatter().string(from: Date())
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
                await clearConfiguredDaemonTemplates()
                clearDaemonMetadata()
                clearUnavailableSelectedSession()
                appState.lastError = .protocolVersionMismatch(
                    expected: Self.supportedProtocolVersion,
                    actual: capabilities.protocolVersion
                )
                appState.connectionState = .disconnected
                return
            }

            appState.daemonStatus = bootstrap.daemonStatus
            syncSelectedWorkspace(from: bootstrap.daemonStatus)
            appState.capabilities = capabilities
            appState.sessions = bootstrap.sessions
            appState.connectionState = .connected
            await configureRouteTemplates(capabilities.routeTemplates)
            await configureEventStreamTemplate(bootstrap.daemonStatus.eventStreamURLTemplate)

            if appState.selectedSessionID == nil {
                appState.selectedSessionID = bootstrap.selectedSessionID
                applySnapshot(bootstrap.snapshot)
            } else {
                await refreshSnapshot(clearSessionScopedStateOnFailure: true)
            }
            if appState.selectedSessionID != nil, appState.snapshot != nil {
                await refreshChatMessages()
                await refreshWorkbenchListsAfterConnection()
                await startEventStreamIfAvailable()
            }
        } catch {
            await stopEventStream()
            clearDaemonMetadata()
            clearUnavailableSelectedSession()
            if shouldClearConfiguredDaemonTemplates(after: error) {
                await clearConfiguredDaemonTemplates()
            }
            appState.lastError = error
            appState.connectionState = .disconnected
        }
    }

    /// Starts listening to Workbench event hints for the selected session.
    ///
    /// Incoming `workbench/event` messages never mutate business state directly:
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
        guard appState.capabilities?.supportsEventStream != false else {
            await stopEventStream()
            appState.lastError = .networkFailure("当前本地服务不支持事件流")
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
    /// matching `workbench/event` messages followed by `refresh_complete`, and
    /// existing event handling will refresh the authoritative snapshot when
    /// events arrive.
    public func requestEventStreamRefresh(
        eventType: String? = nil,
        subjectID: String? = nil,
        actor: String? = nil,
        since: String? = nil,
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
                since: since,
                limit: limit
            )
        } catch {
            appState.connectionState = .stale
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
            await stopEventStream()
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
            appState.connectionState = .stale
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
            await stopEventStream()
        }
    }

    private func startEventStreamIfAvailable() async {
        guard eventProvider != nil, appState.selectedSessionID != nil else {
            return
        }
        guard appState.capabilities?.supportsEventStream != false else {
            await stopEventStream()
            return
        }
        await startEventStream()
    }

    private func configureEventStreamTemplate(_ template: String?) async {
        guard let eventProvider = eventProvider as? any WorkbenchEventStreamTemplateConfiguring else {
            return
        }
        await eventProvider.setEventStreamURLTemplate(template)
    }

    private func configureRouteTemplates(_ templates: [String: String]) async {
        guard let apiProvider = apiProvider as? any WorkbenchRouteTemplateConfiguring else {
            return
        }
        await apiProvider.setRouteTemplates(templates)
    }

    private func clearConfiguredDaemonTemplates() async {
        await configureRouteTemplates([:])
        await configureEventStreamTemplate(nil)
    }

    private func shouldClearConfiguredDaemonTemplates(after error: APIError) -> Bool {
        switch error {
        case .httpStatus(404), .invalidURL:
            return true
        default:
            return false
        }
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
        case .connected(let sessionID):
            if let selectedSessionID = appState.selectedSessionID,
               !sessionID.isEmpty,
               selectedSessionID != sessionID {
                appState.connectionState = .stale
                appState.lastError = .networkFailure("事件流返回了不匹配的会话连接")
                await stopEventStream()
                return
            }
            if appState.connectionState == .stale {
                appState.connectionState = .connected
                await refreshSnapshot()
                if appState.lastError == .sessionUnavailable {
                    appState.connectionState = .stale
                    await stopEventStream()
                    return
                }
                if appState.snapshot != nil {
                    await refreshWorkbenchListsAfterConnection()
                }
            }
        case .snapshot(let snapshot):
            if let selectedSessionID = appState.selectedSessionID,
               selectedSessionID != snapshot.sessionID {
                appState.connectionState = .stale
                appState.lastError = .networkFailure("事件流返回了不匹配的会话快照")
                await stopEventStream()
                return
            }
            appState.selectedSessionID = snapshot.sessionID
            applySnapshot(snapshot)
            appState.connectionState = .connected
            appState.lastError = nil
            await refreshWorkbenchListsAfterConnection()
        case .event:
            await refreshSnapshot()
            if appState.lastError == .sessionUnavailable {
                await stopEventStream()
                return
            }
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
            await stopEventStream()
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

    private func syncSelectedWorkspace(from status: DaemonStatusDTO) {
        if !status.workspaceName.isEmpty {
            appState.selectedWorkspace = status.workspaceName
        } else if !status.workspaceRoot.isEmpty {
            appState.selectedWorkspace = status.workspaceRoot
        }
    }

    private func clearDaemonMetadata() {
        appState.daemonStatus = nil
        appState.capabilities = nil
        appState.selectedWorkspace = nil
    }

    private func applySnapshot(_ snapshot: WorkbenchSnapshotDTO?) {
        appState.snapshot = snapshot
        guard let snapshot else {
            return
        }
        // Keep the current Mission visible from the authoritative snapshot if
        // the lightweight mission list pre-warm is unavailable.
        if !snapshot.missions.isEmpty || appState.missions.isEmpty {
            appState.missions = snapshot.missions
        }
        // Keep task-market issues visible from the authoritative snapshot if
        // the lightweight issue list pre-warm is unavailable.
        if !snapshot.issues.isEmpty || appState.issues.isEmpty {
            appState.issues = snapshot.issues
        }
        // Keep agent activity visible from the authoritative snapshot if the
        // lightweight agent profile pre-warm is unavailable.
        if !snapshot.agentProfiles.isEmpty || appState.agentProfiles.isEmpty {
            appState.agentProfiles = snapshot.agentProfiles
        }
        // Keep failure cards visible from the authoritative snapshot if the
        // lightweight failure list pre-warm is unavailable.
        if !snapshot.failures.isEmpty || appState.failures.isEmpty {
            appState.failures = snapshot.failures
        }
        // Use snapshot validation runs as first-screen fallback without erasing
        // a separately refreshed list when the snapshot omits this optional slice.
        if !snapshot.validationRuns.isEmpty || appState.validationRuns.isEmpty {
            appState.validationRuns = snapshot.validationRuns
        }
        // Use snapshot approvals as first-screen fallback so human intervention
        // cards remain visible if the lightweight approvals pre-warm fails.
        if !snapshot.approvals.isEmpty || appState.approvals.isEmpty {
            appState.approvals = snapshot.approvals
        }
        // Keep active lease evidence visible from the authoritative snapshot if
        // the lightweight task-market lease pre-warm is unavailable.
        if !snapshot.leases.isEmpty || appState.leases.isEmpty {
            appState.leases = snapshot.leases
        }
        // Keep worktree cards visible from the authoritative snapshot if the
        // lightweight worktree pre-warm is unavailable.
        if !snapshot.worktrees.isEmpty || appState.worktrees.isEmpty {
            appState.worktrees = snapshot.worktrees
        }
        // Keep context-health evidence visible from the authoritative snapshot
        // if the lightweight context pre-warm is unavailable.
        if !snapshot.contextSnapshots.isEmpty || appState.contextSnapshots.isEmpty {
            appState.contextSnapshots = snapshot.contextSnapshots
        }
        // Keep human-governance intent locks visible from the authoritative
        // snapshot if the mission-scoped pre-warm is unavailable.
        if !snapshot.intentLocks.isEmpty || appState.intentLocks.isEmpty {
            appState.intentLocks = snapshot.intentLocks
        }
        // Keep human-governance decisions visible from the authoritative
        // snapshot if the mission-scoped pre-warm is unavailable.
        if !snapshot.decisions.isEmpty || appState.decisions.isEmpty {
            appState.decisions = snapshot.decisions
        }
        // Keep recent audit events visible from the authoritative snapshot if
        // the lightweight event list pre-warm is unavailable.
        if !snapshot.events.isEmpty || appState.timelineEvents.isEmpty {
            appState.timelineEvents = snapshot.events
        }
    }

    /// Pre-warms lightweight first-screen list states after a successful connection.
    ///
    /// Called after `refreshSnapshot()` when a session is selected and its snapshot
    /// is available. Independent list reads are fetched concurrently to keep the
    /// Mac first screen responsive; results are then applied in a fixed order so
    /// UI state remains deterministic. Successes write to `appState`, failures
    /// preserve existing local data, and the first pre-warm failure remains
    /// visible in `lastError`. Connection state is never mutated here.
    private func refreshWorkbenchListsAfterConnection() async {
        guard let sessionID = appState.selectedSessionID, appState.snapshot != nil else {
            return
        }

        var preWarmError: APIError?
        var sessionUnavailableDuringPreWarm: APIError?

        func recordPreWarmFailure(_ error: APIError) {
            preWarmError = preWarmError ?? error
            if error == .sessionUnavailable {
                sessionUnavailableDuringPreWarm = error
            }
        }

        let initialMissionID = appState.snapshot?.missions.first?.id ?? appState.missions.first?.id

        async let missionsResult = capturePreWarmResult {
            try await self.apiProvider.fetchMissions(sessionID: sessionID, status: nil, limit: 50)
        }
        async let agentProfilesResult = capturePreWarmResult {
            try await self.apiProvider.fetchAgentProfiles(sessionID: sessionID, status: nil, limit: 50)
        }
        async let issuesResult = capturePreWarmResult {
            try await self.apiProvider.fetchIssues(
                sessionID: sessionID,
                missionID: nil,
                riskLevel: nil,
                status: nil,
                limit: 50
            )
        }
        async let leasesResult = capturePreWarmResult {
            try await self.apiProvider.fetchLeases(sessionID: sessionID, state: nil, taskID: nil, agentID: nil, limit: 50)
        }
        async let worktreesResult = capturePreWarmResult {
            try await self.apiProvider.fetchWorktrees(sessionID: sessionID, taskID: nil, status: nil, limit: 50)
        }
        async let failuresResult = capturePreWarmResult {
            try await self.apiProvider.fetchFailures(sessionID: sessionID, taskID: nil, status: nil, kind: nil, limit: 50)
        }
        async let eventsResult = capturePreWarmResult {
            try await self.apiProvider.fetchEvents(
                sessionID: sessionID,
                eventType: nil,
                subjectID: nil,
                actor: nil,
                since: nil,
                limit: 50
            )
        }
        async let approvalsResult = capturePreWarmResult {
            try await self.apiProvider.fetchApprovals(sessionID: sessionID, state: "waiting", missionID: nil, taskID: nil, limit: 50)
        }
        async let validationRunsResult = capturePreWarmResult {
            try await self.apiProvider.fetchValidationRuns(
                sessionID: sessionID,
                taskID: nil,
                status: nil,
                limit: 50
            )
        }
        async let contextSnapshotsResult = capturePreWarmResult {
            try await self.apiProvider.fetchContextSnapshots(
                sessionID: sessionID,
                taskID: nil,
                agentID: nil,
                health: nil,
                limit: 50
            )
        }

        switch await missionsResult {
        case .success(let missions):
            appState.missions = missions.missions
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await agentProfilesResult {
        case .success(let profiles):
            appState.agentProfiles = profiles.agentProfiles
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await issuesResult {
        case .success(let issues):
            appState.issues = issues.issues
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await leasesResult {
        case .success(let leases):
            appState.leases = leases.leases
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await worktreesResult {
        case .success(let worktrees):
            appState.worktrees = worktrees.worktrees
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await failuresResult {
        case .success(let failures):
            appState.failures = failures.failures
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await eventsResult {
        case .success(let events):
            appState.timelineEvents = events.events
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await approvalsResult {
        case .success(let approvals):
            appState.approvals = approvals.approvals
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await validationRunsResult {
        case .success(let runs):
            appState.validationRuns = runs.validationRuns
        case .failure(let error):
            recordPreWarmFailure(error)
        }
        switch await contextSnapshotsResult {
        case .success(let snapshots):
            appState.contextSnapshots = snapshots.contextSnapshots
        case .failure(let error):
            recordPreWarmFailure(error)
        }

        if let missionID = initialMissionID ?? appState.missions.first?.id {
            async let decisionsResult = capturePreWarmResult {
                try await self.apiProvider.fetchDecisions(sessionID: sessionID, missionID: missionID, kind: nil)
            }
            async let intentLocksResult = capturePreWarmResult {
                try await self.apiProvider.fetchIntentLocks(sessionID: sessionID, missionID: missionID, active: nil)
            }

            switch await decisionsResult {
            case .success(let decisions):
                appState.decisions = decisions.decisions
            case .failure(let error):
                recordPreWarmFailure(error)
            }
            switch await intentLocksResult {
            case .success(let intentLocks):
                appState.intentLocks = intentLocks.intentLocks
            case .failure(let error):
                recordPreWarmFailure(error)
            }
        } else {
            appState.decisions = []
            appState.intentLocks = []
        }

        if let preWarmError {
            appState.lastError = preWarmError
        }
        if sessionUnavailableDuringPreWarm != nil {
            clearUnavailableSelectedSession()
        }
    }

    private func capturePreWarmResult<Value: Sendable>(
        _ operation: @escaping @Sendable () async throws -> Value
    ) async -> Result<Value, APIError> {
        do {
            return .success(try await operation())
        } catch let error as APIError {
            return .failure(error)
        } catch {
            return .failure(.networkFailure(error.localizedDescription))
        }
    }

    /// Fetches persisted daily chat messages for the selected session.
    ///
    /// This keeps the Chat page useful after reconnecting, switching sessions,
    /// or relaunching the app. Missing sessions clear local chat state; ordinary
    /// API failures leave the previous chat list visible while reporting the
    /// error.
    public func refreshChatMessages(page: Int = 1, pageSize: Int = 50) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.chatMessages = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchMessages(
                sessionID: sessionID,
                page: page,
                pageSize: pageSize
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.chatMessages = response.messages
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
        since: String? = nil,
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
                since: since,
                limit: limit
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.timelineEvents = response.events
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Fetches validation runs for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the runs are
    /// written to `appState.validationRuns`; on failure `appState.lastError` is
    /// set. Missing session clears the local validation run list to avoid showing
    /// stale data from another session. API failures leave the local list
    /// unchanged (no fake local runs).
    public func refreshValidationRuns(
        taskID: String? = nil,
        status: String? = nil,
        limit: Int = 50
    ) async {
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
                status: status,
                limit: limit
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.validationRuns = response.validationRuns
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
        health: String? = nil,
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
                health: health,
                limit: limit
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.contextSnapshots = response.contextSnapshots
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
            let snapshot = try await apiProvider.fetchContextSnapshot(
                sessionID: sessionID,
                snapshotID: snapshotID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedContextSnapshot = snapshot
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
            applySnapshot(response.snapshot)
            var snapshots = appState.contextSnapshots
            snapshots.removeAll { $0.id == response.contextSnapshot.id }
            appState.contextSnapshots = [response.contextSnapshot] + snapshots
            var refreshError: APIError?
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

    /// Fetches approval requests for the currently selected session.
    ///
    /// Defaults to returning only `waiting` approvals so the UI can present a
    /// pending-approvals list. Requires `appState.selectedSessionID` to be set.
    /// On success the approvals are written to `appState.approvals`; on failure
    /// `appState.lastError` is set. Missing session clears the local approvals
    /// list to avoid showing stale data from another session. API failures leave
    /// the local list unchanged.
    public func refreshApprovals(
        state: String? = "waiting",
        missionID: String? = nil,
        taskID: String? = nil,
        limit: Int = 50
    ) async {
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
                missionID: missionID,
                taskID: taskID,
                limit: limit
            )
            appState.approvals = response.approvals
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    /// Fetches failure cards for the currently selected session.
    ///
    /// Requires `appState.selectedSessionID` to be set. On success the failures
    /// are written to `appState.failures`; on failure `appState.lastError` is
    /// set. Missing session clears the local failures list to avoid showing
    /// stale data from another session. API failures leave the local list
    /// unchanged.
    public func refreshFailures(taskID: String? = nil, status: String? = nil, kind: String? = nil, limit: Int = 50) async {
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
                kind: kind,
                limit: limit
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.failures = response.failures
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
            let failure = try await apiProvider.fetchFailure(
                sessionID: sessionID,
                failureID: failureID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedFailure = failure
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
    public func refreshIssues(
        missionID: String? = nil,
        riskLevel: String? = nil,
        status: String? = nil,
        limit: Int = 50
    ) async {
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
                status: status,
                limit: limit
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.issues = response.issues
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
            let issue = try await apiProvider.fetchIssue(
                sessionID: sessionID,
                taskID: taskID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedIssue = issue
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.leases = response.leases
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
            let lease = try await apiProvider.fetchLease(
                sessionID: sessionID,
                leaseID: leaseID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedLease = lease
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.worktrees = response.worktrees
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
            let worktree = try await apiProvider.fetchWorktree(
                sessionID: sessionID,
                name: name
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedWorktree = worktree
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.missions = response.missions
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
            let mission = try await apiProvider.fetchMission(
                sessionID: sessionID,
                missionID: missionID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedMission = mission
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.agentProfiles = response.agentProfiles
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
            let agentProfile = try await apiProvider.fetchAgentProfile(
                sessionID: sessionID,
                agentID: agentID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedAgentProfile = agentProfile
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
    public func refreshDecisions(missionID: String, kind: String? = nil) async {
        guard let sessionID = appState.selectedSessionID else {
            appState.decisions = []
            appState.lastError = .missingSelectedSession
            return
        }

        appState.lastError = nil
        do {
            let response = try await apiProvider.fetchDecisions(
                sessionID: sessionID,
                missionID: missionID,
                kind: kind
            )
            appState.decisions = response.decisions
        } catch {
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
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
                missionID: missionID,
                active: nil
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
            applySnapshot(response.snapshot)
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
                await clearConfiguredDaemonTemplates()
                clearDaemonMetadata()
                clearUnavailableSelectedSession()
                appState.lastError = .protocolVersionMismatch(
                    expected: Self.supportedProtocolVersion,
                    actual: capabilities.protocolVersion
                )
                appState.connectionState = .disconnected
                return
            }

            await stopEventStream()
            appState.daemonStatus = bootstrap.daemonStatus
            syncSelectedWorkspace(from: bootstrap.daemonStatus)
            appState.capabilities = capabilities
            appState.connectionState = .connected
            await configureRouteTemplates(capabilities.routeTemplates)
            await configureEventStreamTemplate(bootstrap.daemonStatus.eventStreamURLTemplate)
            for session in bootstrap.sessions.reversed() {
                appState.sessions.removeAll { $0.id == session.id }
                appState.sessions.insert(session, at: 0)
            }
            appState.selectedSessionID = bootstrap.selectedSessionID ?? bootstrap.sessions.first?.id
            clearSessionScopedState()
            applySnapshot(bootstrap.snapshot)

            guard appState.snapshot != nil else {
                return
            }

            await refreshWorkbenchListsAfterConnection()
            await startEventStreamIfAvailable()
        } catch {
            if error == .sessionUnavailable {
                await stopEventStream()
                clearUnavailableSelectedSession()
            }
            if shouldClearConfiguredDaemonTemplates(after: error) {
                await clearConfiguredDaemonTemplates()
            }
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

        await refreshChatMessages()
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
        appState.chatMessages = []
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

    /// Sends a daily chat message through the selected session. When an issue
    /// draft is provided, the backend creates the Workbench issue atomically
    /// with the non-streaming message response.
    ///
    /// If no session is selected, a lightweight chat session is created first so
    /// first-run users can talk to the app before creating a Mission.
    public func sendDailyMessage(content: String, issueDraft: ChatIssueDraftDTO?) async {
        let trimmedContent = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedContent.isEmpty else {
            return
        }

        if appState.selectedSessionID == nil {
            await createSession(
                title: AppStrings.Chat.title(appState.locale),
                model: nil,
                systemPrompt: nil
            )
        }

        guard let sessionID = appState.selectedSessionID else {
            if appState.lastError == nil {
                appState.lastError = .missingSelectedSession
            }
            return
        }

        appState.lastError = nil
        let localUserMessage = ChatMessageDTO(
            id: "local-\(UUID().uuidString)",
            role: "user",
            content: trimmedContent,
            timestamp: Self.nowISO8601(),
            metadata: [:]
        )

        do {
            let response = try await apiProvider.sendMessage(
                sessionID: sessionID,
                content: trimmedContent,
                workbenchIssue: issueDraft
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.chatMessages.append(localUserMessage)
            appState.chatMessages.append(response)

            if issueDraft != nil {
                if let snapshot = workbenchSnapshot(from: response, expectedSessionID: sessionID) {
                    applySnapshot(snapshot)
                    return
                }
                await refreshSnapshot()
                guard appState.selectedSessionID != nil else {
                    return
                }
                await refreshIssues(missionID: issueDraft?.missionID)
                guard appState.selectedSessionID != nil else {
                    return
                }
                await refreshEvents(limit: 50)
            }
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.lastError = error
            if error == .sessionUnavailable {
                clearUnavailableSelectedSession()
            }
        }
    }

    private func workbenchSnapshot(
        from message: ChatMessageDTO,
        expectedSessionID: String
    ) -> WorkbenchSnapshotDTO? {
        guard let value = message.metadata["workbench_snapshot"],
              let jsonObject = Self.jsonObject(from: value) as? [String: Any],
              JSONSerialization.isValidJSONObject(jsonObject),
              let data = try? JSONSerialization.data(withJSONObject: jsonObject) else {
            return nil
        }
        let snapshot = try? JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
        guard snapshot?.sessionID == expectedSessionID else {
            return nil
        }
        return snapshot
    }

    private static func jsonObject(from value: JSONValue) -> Any {
        switch value {
        case .string(let string):
            return string
        case .number(let number):
            return number
        case .bool(let bool):
            return bool
        case .object(let object):
            return object.mapValues { jsonObject(from: $0) }
        case .array(let array):
            return array.map { jsonObject(from: $0) }
        case .null:
            return NSNull()
        }
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
            applySnapshot(snapshot)
        } catch APIError.sessionUnavailable {
            appState.lastError = APIError.sessionUnavailable
            clearUnavailableSelectedSession()
        } catch {
            appState.lastError = error
            applySnapshot(nil)
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            applySnapshot(response.snapshot)
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
            let event = try await apiProvider.fetchEvent(
                sessionID: sessionID,
                eventID: eventID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedEvent = event
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
            applySnapshot(response.snapshot)
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
            let run = try await apiProvider.fetchValidationRun(
                sessionID: sessionID,
                runID: runID
            )
            guard appState.selectedSessionID == sessionID else {
                return
            }
            appState.selectedValidationRun = run
        } catch {
            guard appState.selectedSessionID == sessionID else {
                return
            }
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
            applySnapshot(response.snapshot)
            var refreshError: APIError?
            await refreshValidationRuns(taskID: taskID)
            refreshError = refreshError ?? appState.lastError
            await refreshFailures(taskID: taskID)
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
}
