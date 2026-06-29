import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

/// In-memory fake conforming to `WorkbenchAPIProviding` for unit tests.
actor FakeWorkbenchAPIProvider: WorkbenchAPIProviding {
    var bootstrapResult: Result<WorkbenchBootstrapDTO, APIError>?
    var statusResult: Result<DaemonStatusDTO, APIError>?
    var capabilitiesResult: Result<CapabilitiesDTO, APIError>?
    var snapshotResult: Result<WorkbenchSnapshotDTO, APIError>?
    var sessionsResult: Result<SessionListDTO, APIError>?
    var createSessionResult: Result<SessionDTO, APIError>?
    var createWorkbenchSessionResult: Result<WorkbenchBootstrapDTO, APIError>?
    var eventsResult: Result<WorkbenchEventsDTO, APIError>?
    var eventResult: Result<EventDTO, APIError>?
    var validationRunsResult: Result<ValidationRunsDTO, APIError>?
    var validationRunResult: Result<ValidationRunDTO, APIError>?
    var contextSnapshotsResult: Result<ContextSnapshotsDTO, APIError>?
    var contextSnapshotResult: Result<ContextSnapshotDTO, APIError>?
    var recordContextHealthResult: Result<ContextSnapshotDTO, APIError>?
    var recordContextHealthWithSnapshotResult: Result<ContextHealthSnapshotDTO, APIError>?
    var approvalsResult: Result<ApprovalsDTO, APIError>?
    var approvalResult: Result<ApprovalDTO, APIError>?
    var failuresResult: Result<FailuresDTO, APIError>?
    var failureResult: Result<FailureDTO, APIError>?
    var issuesResult: Result<IssuesDTO, APIError>?
    var issueResult: Result<IssueDTO, APIError>?
    var leasesResult: Result<LeasesDTO, APIError>?
    var leaseResult: Result<LeaseDTO, APIError>?
    var worktreesResult: Result<WorktreesDTO, APIError>?
    var worktreeResult: Result<WorktreeDTO, APIError>?
    var keepWorktreeResult: Result<WorktreeDTO, APIError>?
    var keepWorktreeWithSnapshotResult: Result<WorktreeSnapshotDTO, APIError>?
    var removeWorktreeResult: Result<WorktreeRemovalDTO, APIError>?
    var removeWorktreeWithSnapshotResult: Result<WorktreeRemovalSnapshotDTO, APIError>?
    var missionsResult: Result<MissionsDTO, APIError>?
    var agentProfilesResult: Result<AgentProfilesDTO, APIError>?
    var agentProfileResult: Result<AgentProfileDTO, APIError>?
    var registerAgentProfileResult: Result<AgentProfileDTO, APIError>?
    var registerAgentProfileWithSnapshotResult: Result<AgentProfileSnapshotDTO, APIError>?
    var claimIssueResult: Result<LeaseDTO, APIError>?
    var claimIssueWithSnapshotResult: Result<LeaseSnapshotDTO, APIError>?
    var releaseLeaseResult: Result<LeaseDTO, APIError>?
    var releaseLeaseWithSnapshotResult: Result<LeaseSnapshotDTO, APIError>?
    var expireLeasesResult: Result<ExpiredLeasesDTO, APIError>?
    var expireLeasesWithSnapshotResult: Result<ExpiredLeasesSnapshotDTO, APIError>?
    var createMissionResult: Result<MissionDTO, APIError>?
    var createMissionWithSnapshotResult: Result<MissionSnapshotDTO, APIError>?
    var missionResult: Result<MissionDTO, APIError>?
    var attachIssueResult: Result<IssueDTO, APIError>?
    var attachIssueWithSnapshotResult: Result<IssueSnapshotDTO, APIError>?
    var createIssueResult: Result<IssueDTO, APIError>?
    var createIssueWithSnapshotResult: Result<IssueSnapshotDTO, APIError>?
    var createIntentLockResult: Result<IntentLockDTO, APIError>?
    var createIntentLockWithSnapshotResult: Result<IntentLockSnapshotDTO, APIError>?
    var fetchIntentLocksResult: Result<IntentLocksDTO, APIError>?
    var fetchIntentLockResult: Result<IntentLockDTO, APIError>?
    var createDecisionResult: Result<DecisionDTO, APIError>?
    var createDecisionWithSnapshotResult: Result<DecisionSnapshotDTO, APIError>?
    var fetchDecisionsResult: Result<DecisionsDTO, APIError>?
    var fetchDecisionResult: Result<DecisionDTO, APIError>?
    var resolveApprovalResult: Result<ApprovalDTO, APIError>?
    var resolveApprovalWithSnapshotResult: Result<ApprovalSnapshotDTO, APIError>?
    var runValidationResult: Result<ValidationResultDTO, APIError>?
    var runValidationWithSnapshotResult: Result<ValidationResultSnapshotDTO, APIError>?
    var bootstrapCallCount: Int = 0
    var bootstrapPageSizes: [Int] = []
    var createWorkbenchSessionCallCount: Int = 0
    var statusCallCount: Int = 0
    var capabilitiesCallCount: Int = 0
    var sessionsCallCount: Int = 0
    var snapshotCallCount: Int = 0
    var keepWorktreeWithSnapshotCallCount: Int = 0
    var removeWorktreeWithSnapshotCallCount: Int = 0
    var recordContextHealthWithSnapshotCallCount: Int = 0
    var registerAgentProfileWithSnapshotCallCount: Int = 0
    var claimIssueWithSnapshotCallCount: Int = 0
    var releaseLeaseWithSnapshotCallCount: Int = 0
    var expireLeasesWithSnapshotCallCount: Int = 0
    var createMissionWithSnapshotCallCount: Int = 0
    var attachIssueWithSnapshotCallCount: Int = 0
    var createIssueWithSnapshotCallCount: Int = 0
    var createIntentLockWithSnapshotCallCount: Int = 0
    var createDecisionWithSnapshotCallCount: Int = 0
    var resolveApprovalWithSnapshotCallCount: Int = 0
    var runValidationCallCount: Int = 0
    var runValidationWithSnapshotCallCount: Int = 0
    var createdSessions: [[String: String?]] = []
    var createdMissions: [[String: String]] = []

    func fetchBootstrap(pageSize: Int) async throws(APIError) -> WorkbenchBootstrapDTO {
        bootstrapCallCount += 1
        bootstrapPageSizes.append(pageSize)
        if let result = bootstrapResult {
            return try result.get()
        }

        guard let statusResult, let capabilitiesResult else {
            throw .invalidResponse
        }
        let sessions = try sessionsResult?.get() ?? SessionListDTO(
            sessions: [],
            total: 0,
            page: 1,
            pageSize: pageSize
        )
        let snapshot = try? snapshotResult?.get()
        return WorkbenchBootstrapDTO(
            daemonStatus: try statusResult.get(),
            capabilities: try capabilitiesResult.get(),
            sessions: sessions.sessions,
            totalSessions: sessions.total,
            selectedSessionID: sessions.sessions.first?.id,
            snapshot: snapshot ?? nil
        )
    }

    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO {
        statusCallCount += 1
        guard let result = statusResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO {
        capabilitiesCallCount += 1
        guard let result = capabilitiesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO {
        snapshotCallCount += 1
        guard let result = snapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        sessionsCallCount += 1
        guard let result = sessionsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createSession(
        title: String?,
        model: String?,
        systemPrompt: String?
    ) async throws(APIError) -> SessionDTO {
        createdSessions.append([
            "title": title,
            "model": model,
            "systemPrompt": systemPrompt,
        ])
        guard let result = createSessionResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createWorkbenchSession(
        title: String?,
        model: String?,
        systemPrompt: String?
    ) async throws(APIError) -> WorkbenchBootstrapDTO {
        createWorkbenchSessionCallCount += 1
        createdSessions.append([
            "title": title,
            "model": model,
            "systemPrompt": systemPrompt,
        ])
        guard let result = createWorkbenchSessionResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchEvents(
        sessionID: String,
        eventType: String?,
        subjectID: String?,
        actor: String?,
        limit: Int
    ) async throws(APIError) -> WorkbenchEventsDTO {
        guard let result = eventsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchEvent(sessionID: String, eventID: String) async throws(APIError) -> EventDTO {
        guard let result = eventResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO {
        guard let result = validationRunsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchValidationRun(sessionID: String, runID: String) async throws(APIError) -> ValidationRunDTO {
        guard let result = validationRunResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchContextSnapshots(
        sessionID: String,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> ContextSnapshotsDTO {
        guard let result = contextSnapshotsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchContextSnapshot(sessionID: String, snapshotID: String) async throws(APIError) -> ContextSnapshotDTO {
        guard let result = contextSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func recordContextHealth(
        sessionID: String,
        taskID: String,
        agentID: String,
        minutesSinceSync: Int,
        tokenLoadRatio: Double,
        policyConflict: Bool,
        actor: String
    ) async throws(APIError) -> ContextSnapshotDTO {
        guard let result = recordContextHealthResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func recordContextHealthWithSnapshot(
        sessionID: String,
        taskID: String,
        agentID: String,
        minutesSinceSync: Int,
        tokenLoadRatio: Double,
        policyConflict: Bool,
        actor: String
    ) async throws(APIError) -> ContextHealthSnapshotDTO {
        recordContextHealthWithSnapshotCallCount += 1
        guard let result = recordContextHealthWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchApprovals(
        sessionID: String,
        state: String?,
        limit: Int
    ) async throws(APIError) -> ApprovalsDTO {
        guard let result = approvalsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchApproval(sessionID: String, approvalID: String) async throws(APIError) -> ApprovalDTO {
        guard let result = approvalResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchFailures(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> FailuresDTO {
        guard let result = failuresResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchFailure(sessionID: String, failureID: String) async throws(APIError) -> FailureDTO {
        guard let result = failureResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchIssues(
        sessionID: String,
        missionID: String?,
        riskLevel: String?,
        limit: Int
    ) async throws(APIError) -> IssuesDTO {
        guard let result = issuesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchIssue(sessionID: String, taskID: String) async throws(APIError) -> IssueDTO {
        guard let result = issueResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchLeases(
        sessionID: String,
        state: String?,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> LeasesDTO {
        guard let result = leasesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        guard let result = leaseResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchWorktrees(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> WorktreesDTO {
        guard let result = worktreesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchWorktree(sessionID: String, name: String) async throws(APIError) -> WorktreeDTO {
        guard let result = worktreeResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func keepWorktree(
        sessionID: String,
        name: String,
        actor: String,
        reason: String
    ) async throws(APIError) -> WorktreeDTO {
        guard let result = keepWorktreeResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func keepWorktreeWithSnapshot(
        sessionID: String,
        name: String,
        actor: String,
        reason: String
    ) async throws(APIError) -> WorktreeSnapshotDTO {
        keepWorktreeWithSnapshotCallCount += 1
        guard let result = keepWorktreeWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func removeWorktree(
        sessionID: String,
        name: String,
        discardChanges: Bool
    ) async throws(APIError) -> WorktreeRemovalDTO {
        guard let result = removeWorktreeResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func removeWorktreeWithSnapshot(
        sessionID: String,
        name: String,
        discardChanges: Bool
    ) async throws(APIError) -> WorktreeRemovalSnapshotDTO {
        removeWorktreeWithSnapshotCallCount += 1
        guard let result = removeWorktreeWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchMissions(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> MissionsDTO {
        guard let result = missionsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchMission(sessionID: String, missionID: String) async throws(APIError) -> MissionDTO {
        guard let result = missionResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchAgentProfiles(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> AgentProfilesDTO {
        guard let result = agentProfilesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchAgentProfile(sessionID: String, agentID: String) async throws(APIError) -> AgentProfileDTO {
        guard let result = agentProfileResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func registerAgentProfile(
        sessionID: String,
        agentID: String,
        name: String,
        role: String,
        capabilities: [String],
        permissions: [String],
        maxParallelTasks: Int,
        status: String,
        actor: String
    ) async throws(APIError) -> AgentProfileDTO {
        guard let result = registerAgentProfileResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func registerAgentProfileWithSnapshot(
        sessionID: String,
        agentID: String,
        name: String,
        role: String,
        capabilities: [String],
        permissions: [String],
        maxParallelTasks: Int,
        status: String,
        actor: String
    ) async throws(APIError) -> AgentProfileSnapshotDTO {
        registerAgentProfileWithSnapshotCallCount += 1
        guard let result = registerAgentProfileWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func claimIssue(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseDTO {
        guard let result = claimIssueResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func claimIssueWithSnapshot(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseSnapshotDTO {
        claimIssueWithSnapshotCallCount += 1
        guard let result = claimIssueWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        guard let result = releaseLeaseResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func releaseLeaseWithSnapshot(sessionID: String, leaseID: String) async throws(APIError) -> LeaseSnapshotDTO {
        releaseLeaseWithSnapshotCallCount += 1
        guard let result = releaseLeaseWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func expireLeases(sessionID: String) async throws(APIError) -> ExpiredLeasesDTO {
        guard let result = expireLeasesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func expireLeasesWithSnapshot(sessionID: String) async throws(APIError) -> ExpiredLeasesSnapshotDTO {
        expireLeasesWithSnapshotCallCount += 1
        guard let result = expireLeasesWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createMission(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionDTO {
        createdMissions.append([
            "sessionID": sessionID,
            "title": title,
            "goal": goal,
        ])
        guard let result = createMissionResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createMissionWithSnapshot(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionSnapshotDTO {
        createMissionWithSnapshotCallCount += 1
        createdMissions.append([
            "sessionID": sessionID,
            "title": title,
            "goal": goal,
        ])
        guard let result = createMissionWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func attachIssue(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO {
        guard let result = attachIssueResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func attachIssueWithSnapshot(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueSnapshotDTO {
        attachIssueWithSnapshotCallCount += 1
        guard let result = attachIssueWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createIssue(
        sessionID: String,
        missionID: String,
        title: String,
        description: String,
        blockedBy: [String],
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO {
        guard let result = createIssueResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createIssueWithSnapshot(
        sessionID: String,
        missionID: String,
        title: String,
        description: String,
        blockedBy: [String],
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueSnapshotDTO {
        createIssueWithSnapshotCallCount += 1
        guard let result = createIssueWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchIntentLocks(sessionID: String, missionID: String) async throws(APIError) -> IntentLocksDTO {
        guard let result = fetchIntentLocksResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchIntentLock(
        sessionID: String,
        missionID: String,
        lockID: String
    ) async throws(APIError) -> IntentLockDTO {
        guard let result = fetchIntentLockResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createIntentLock(
        sessionID: String,
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async throws(APIError) -> IntentLockDTO {
        guard let result = createIntentLockResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createIntentLockWithSnapshot(
        sessionID: String,
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async throws(APIError) -> IntentLockSnapshotDTO {
        createIntentLockWithSnapshotCallCount += 1
        guard let result = createIntentLockWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchDecisions(sessionID: String, missionID: String) async throws(APIError) -> DecisionsDTO {
        guard let result = fetchDecisionsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchDecision(
        sessionID: String,
        missionID: String,
        decisionID: String
    ) async throws(APIError) -> DecisionDTO {
        guard let result = fetchDecisionResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createDecision(
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String
    ) async throws(APIError) -> DecisionDTO {
        guard let result = createDecisionResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createDecisionWithSnapshot(
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String
    ) async throws(APIError) -> DecisionSnapshotDTO {
        createDecisionWithSnapshotCallCount += 1
        guard let result = createDecisionWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func resolveApproval(
        sessionID: String,
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async throws(APIError) -> ApprovalDTO {
        guard let result = resolveApprovalResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func resolveApprovalWithSnapshot(
        sessionID: String,
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async throws(APIError) -> ApprovalSnapshotDTO {
        resolveApprovalWithSnapshotCallCount += 1
        guard let result = resolveApprovalWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func runValidation(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultDTO {
        runValidationCallCount += 1
        guard let result = runValidationResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func runValidationWithSnapshot(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultSnapshotDTO {
        runValidationWithSnapshotCallCount += 1
        guard let result = runValidationWithSnapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }
}

actor FakeWorkbenchEventProvider: WorkbenchEventProviding {
    private var continuation: AsyncThrowingStream<WorkbenchEventStreamMessage, Error>.Continuation?
    private let streamRecorder = FakeWorkbenchEventStreamRecorder()
    private var connectResult: Result<Void, APIError> = .success(())
    private(set) var connectedSessionIDs: [String] = []

    func connect(sessionID: String) async throws(APIError) -> any WorkbenchEventStreaming {
        connectedSessionIDs.append(sessionID)
        try connectResult.get()
        let stream = AsyncThrowingStream<WorkbenchEventStreamMessage, Error> { continuation in
            self.continuation = continuation
        }
        return FakeWorkbenchEventStream(stream: stream, recorder: streamRecorder)
    }

    func emit(_ message: WorkbenchEventStreamMessage) {
        continuation?.yield(message)
    }

    func fail(_ error: APIError) {
        continuation?.finish(throwing: error)
    }

    func finish() {
        continuation?.finish()
    }

    func setConnectResult(_ result: Result<Void, APIError>) {
        connectResult = result
    }

    func recordedRefreshRequests() async -> [FakeWorkbenchEventRefreshRequest] {
        await streamRecorder.refreshRequests
    }

    func recordedPingCount() async -> Int {
        await streamRecorder.pingCount
    }

    func setPingResult(_ result: Result<Void, APIError>) async {
        await streamRecorder.setPingResult(result)
    }

    func setRefreshResult(_ result: Result<Void, APIError>) async {
        await streamRecorder.setRefreshResult(result)
    }
}

struct FakeWorkbenchEventStream: WorkbenchEventStreaming {
    let stream: AsyncThrowingStream<WorkbenchEventStreamMessage, Error>
    let recorder: FakeWorkbenchEventStreamRecorder

    func next() async throws(APIError) -> WorkbenchEventStreamMessage {
        do {
            var iterator = stream.makeAsyncIterator()
            guard let message = try await iterator.next() else {
                throw APIError.networkFailure("event stream ended")
            }
            return message
        } catch let error as APIError {
            throw error
        } catch {
            throw .networkFailure(String(describing: error))
        }
    }

    func requestRefresh(
        eventType: String?,
        subjectID: String?,
        actor: String?,
        limit: Int
    ) async throws(APIError) {
        try await recorder.recordRefreshRequest(
            eventType: eventType,
            subjectID: subjectID,
            actor: actor,
            limit: limit
        )
    }

    func sendPing() async throws(APIError) {
        try await recorder.recordPing()
    }

    func cancel() async {}
}

struct FakeWorkbenchEventRefreshRequest: Equatable, Sendable {
    let eventType: String?
    let subjectID: String?
    let actor: String?
    let limit: Int
}

actor FakeWorkbenchEventStreamRecorder {
    private(set) var refreshRequests: [FakeWorkbenchEventRefreshRequest] = []
    private(set) var pingCount = 0
    private var pingResult: Result<Void, APIError> = .success(())
    private var refreshResult: Result<Void, APIError> = .success(())

    func recordRefreshRequest(
        eventType: String?,
        subjectID: String?,
        actor: String?,
        limit: Int
    ) throws(APIError) {
        refreshRequests.append(FakeWorkbenchEventRefreshRequest(
            eventType: eventType,
            subjectID: subjectID,
            actor: actor,
            limit: limit
        ))
        try refreshResult.get()
    }

    func recordPing() throws(APIError) {
        pingCount += 1
        try pingResult.get()
    }

    func setPingResult(_ result: Result<Void, APIError>) {
        pingResult = result
    }

    func setRefreshResult(_ result: Result<Void, APIError>) {
        refreshResult = result
    }
}

@Suite
final class DaemonControllerTests {

    @Test @MainActor func refreshConnectionSuccess() async throws {
        let appState = AppState()
        let api = FakeWorkbenchAPIProvider()

        let status = DaemonStatusDTO(
            status: "running",
            version: "0.2.0",
            pid: 42,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 7
        )
        let capabilities = CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1
        )
        let sessions = SessionListDTO(
            sessions: [],
            total: 0,
            page: 1,
            pageSize: 1
        )

        await api.setStatusResult(.success(status))
        await api.setCapabilitiesResult(.success(capabilities))
        await api.setSessionsResult(.success(sessions))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.daemonStatus == status)
        #expect(appState.capabilities == capabilities)
        #expect(appState.lastError == nil)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func refreshConnectionUsesBootstrapForInitialSessionSelection() async throws {
        let appState = AppState()
        let api = FakeWorkbenchAPIProvider()
        let status = makeStatus()
        let capabilities = makeCapabilities()
        let session = makeSession(id: "sess-bootstrap", title: "Bootstrap Session")
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-bootstrap",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )
        let bootstrap = WorkbenchBootstrapDTO(
            daemonStatus: status,
            capabilities: capabilities,
            sessions: [session],
            totalSessions: 1,
            selectedSessionID: "sess-bootstrap",
            snapshot: snapshot
        )

        await api.setBootstrapResult(.success(bootstrap))
        await configureWorkbenchListResults(for: api, sessionID: "sess-bootstrap")

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.daemonStatus == status)
        #expect(appState.capabilities == capabilities)
        #expect(appState.sessions == [session])
        #expect(appState.selectedSessionID == "sess-bootstrap")
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
        expectWorkbenchListsPopulated(appState)
        #expect(await api.bootstrapCallCount == 1)
        #expect(await api.bootstrapPageSizes == [5])
        #expect(await api.statusCallCount == 0)
        #expect(await api.capabilitiesCallCount == 0)
        #expect(await api.sessionsCallCount == 0)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func refreshConnectionFailure() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()

        await api.setStatusResult(.failure(.httpStatus(503)))
        await api.setCapabilitiesResult(.success(CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: false,
            supportsValidationRunner: false,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN"],
            protocolVersion: 1
        )))

        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )
        await controller.startEventStream()
        await waitUntil {
            controller.hasActiveEventStream
        }

        await controller.refreshConnection()

        #expect(appState.connectionState == .disconnected)
        #expect(appState.lastError == .httpStatus(503))
        #expect(appState.daemonStatus == nil)
        #expect(appState.capabilities == nil)
        #expect(controller.hasActiveEventStream == false)
    }

    @Test @MainActor func refreshConnectionRejectsUnsupportedProtocolVersion() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()

        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 999
        )))

        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )
        await controller.startEventStream()
        await waitUntil {
            controller.hasActiveEventStream
        }

        await controller.refreshConnection()

        #expect(appState.connectionState == .disconnected)
        #expect(appState.daemonStatus == nil)
        #expect(appState.capabilities == nil)
        #expect(appState.lastError == .protocolVersionMismatch(expected: 1, actual: 999))
        #expect(controller.hasActiveEventStream == false)
    }

    @Test @MainActor func refreshConnectionClearsPreviousError() async throws {
        let appState = AppState()
        appState.lastError = .httpStatus(500)

        let api = FakeWorkbenchAPIProvider()
        let status = DaemonStatusDTO(
            status: "running",
            version: "0.2.0",
            pid: 42,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 7
        )
        await api.setStatusResult(.success(status))
        await api.setCapabilitiesResult(.success(CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1
        )))
        await api.setSessionsResult(.success(SessionListDTO(
            sessions: [],
            total: 0,
            page: 1,
            pageSize: 1
        )))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshConnectionRefreshesSnapshotAndPreWarmsListsForSelectedSession() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-existing"

        let api = FakeWorkbenchAPIProvider()
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-existing",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSnapshotResult(.success(snapshot))
        await configureWorkbenchListResults(for: api, sessionID: "sess-existing")

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-existing")
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
        expectWorkbenchListsPopulated(appState)
    }

    @Test @MainActor func refreshConnectionAutoSelectsRecentSessionAndPreWarmsLists() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let session = SessionDTO(
            id: "sess-latest",
            title: "Latest Session",
            model: "gpt-4o",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:30:00",
            messageCount: 3,
            totalTokens: 120,
            totalCostUSD: 0.0012,
            status: "active"
        )
        let sessions = SessionListDTO(
            sessions: [session],
            total: 1,
            page: 1,
            pageSize: 1
        )
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-latest",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSessionsResult(.success(sessions))
        await api.setSnapshotResult(.success(snapshot))
        await configureWorkbenchListResults(for: api, sessionID: "sess-latest")

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-latest")
        #expect(appState.sessions == [session])
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
        expectWorkbenchListsPopulated(appState)
    }

    @Test @MainActor func refreshConnectionPreWarmFailureKeepsConnectedAndReportsFirstError() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-existing"

        let api = FakeWorkbenchAPIProvider()
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-existing",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSnapshotResult(.success(snapshot))
        await configureWorkbenchListResults(for: api, sessionID: "sess-existing")
        await api.setMissionsResult(.failure(.httpStatus(502)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-existing")
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == .httpStatus(502))
        #expect(appState.missions.isEmpty)
        #expect(appState.issues.count == 1)
        #expect(appState.leases.count == 1)
        #expect(appState.failures.count == 1)
        #expect(appState.timelineEvents.count == 1)
        #expect(appState.approvals.count == 1)
        #expect(appState.validationRuns.count == 1)
        #expect(appState.contextSnapshots.count == 1)
    }

    @Test @MainActor func refreshConnectionKeepsConnectedWhenSessionsEmpty() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSessionsResult(.success(SessionListDTO(
            sessions: [],
            total: 0,
            page: 1,
            pageSize: 1
        )))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.sessions.isEmpty)
        #expect(appState.snapshot == nil)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func eventStreamRefreshesSnapshotAndWorkbenchListsOnWorkbenchEvent() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let mission = makeMission(id: "mission-events", sessionID: "sess-events")
        let snapshot = makeSnapshot(sessionID: "sess-events", missions: [mission])

        await api.setSnapshotResult(.success(snapshot))
        await configureWorkbenchListResults(for: api, sessionID: "sess-events")

        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )
        await controller.startEventStream()

        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }
        #expect(await eventProvider.connectedSessionIDs == ["sess-events"])

        await eventProvider.emit(.connected(sessionID: "sess-events"))
        await eventProvider.emit(.event(makeEvent(id: "evt-refresh", type: "issue.claimed", subjectID: "task-1")))

        await waitUntil {
            await api.snapshotCallCount >= 1 && appState.snapshot == snapshot
        }

        #expect(appState.connectionState == .connected)
        #expect(appState.snapshot == snapshot)
        expectWorkbenchListsPopulated(appState)
        #expect(appState.lastError == nil)

        await controller.stopEventStream()
    }

    @Test @MainActor func eventStreamSnapshotAppliesSnapshotWithoutRESTRefresh() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let mission = makeMission(id: "mission-stream", sessionID: "sess-events")
        let snapshot = makeSnapshot(sessionID: "sess-events", missions: [mission])

        await configureWorkbenchListResults(for: api, sessionID: "sess-events")

        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )
        await controller.startEventStream()

        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }

        await eventProvider.emit(.snapshot(snapshot))

        await waitUntil {
            appState.snapshot == snapshot
        }

        #expect(await api.snapshotCallCount == 0)
        #expect(appState.connectionState == .connected)
        #expect(appState.snapshot == snapshot)
        expectWorkbenchListsPopulated(appState)
        #expect(appState.lastError == nil)

        await controller.stopEventStream()
    }

    @Test @MainActor func eventStreamConnectedAfterStaleRefreshesSnapshotAndWorkbenchLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-reconnect"
        appState.connectionState = .stale
        appState.snapshot = makeSnapshot(sessionID: "sess-reconnect", missions: [])

        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let mission = makeMission(id: "mission-reconnect", sessionID: "sess-reconnect")
        let snapshot = makeSnapshot(sessionID: "sess-reconnect", missions: [mission])

        await api.setSnapshotResult(.success(snapshot))
        await configureWorkbenchListResults(for: api, sessionID: "sess-reconnect")

        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )
        await controller.startEventStream()

        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-reconnect"]
        }

        await eventProvider.emit(.connected(sessionID: "sess-reconnect"))

        await waitUntil {
            await api.snapshotCallCount >= 1 && appState.snapshot == snapshot
        }

        #expect(appState.connectionState == .connected)
        #expect(appState.snapshot == snapshot)
        expectWorkbenchListsPopulated(appState)
        #expect(appState.lastError == nil)

        await controller.stopEventStream()
    }

    @Test @MainActor func requestEventStreamRefreshForwardsFiltersToActiveStream() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }

        await controller.requestEventStreamRefresh(
            eventType: "validation.passed",
            subjectID: "task-001",
            actor: "Backend-Agent",
            limit: 25
        )

        await waitUntil {
            await eventProvider.recordedRefreshRequests() == [
                FakeWorkbenchEventRefreshRequest(
                    eventType: "validation.passed",
                    subjectID: "task-001",
                    actor: "Backend-Agent",
                    limit: 25
                )
            ]
        }

        #expect(appState.lastError == nil)

        await controller.stopEventStream()
    }

    @Test @MainActor func requestEventStreamRefreshFailureMarksConnectionStaleAndClearsActiveStream() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        await eventProvider.setRefreshResult(.failure(.networkFailure("refresh send failed")))
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }

        await controller.requestEventStreamRefresh(limit: 10)

        #expect(await eventProvider.recordedRefreshRequests() == [
            FakeWorkbenchEventRefreshRequest(
                eventType: nil,
                subjectID: nil,
                actor: nil,
                limit: 10
            ),
        ])
        #expect(appState.connectionState == .stale)
        #expect(appState.lastError == .networkFailure("refresh send failed"))
        #expect(controller.hasActiveEventStream == false)

        await controller.stopEventStream()
    }

    @Test @MainActor func requestEventStreamRefreshSessionUnavailableClearsSelectedSessionAndSessionState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-missing"
        appState.snapshot = makeSnapshot(sessionID: "sess-missing", missions: [])
        seedWorkbenchLists(appState)
        seedSelectedDetails(appState)
        appState.connectionState = .connected

        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        await eventProvider.setRefreshResult(.failure(.sessionUnavailable))
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-missing"]
        }

        await controller.requestEventStreamRefresh(limit: 10)

        #expect(await eventProvider.recordedRefreshRequests() == [
            FakeWorkbenchEventRefreshRequest(
                eventType: nil,
                subjectID: nil,
                actor: nil,
                limit: 10
            ),
        ])
        #expect(appState.connectionState == .stale)
        #expect(appState.lastError == .sessionUnavailable)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
        expectWorkbenchListsEmpty(appState)
        expectSelectedDetailsEmpty(appState)
        #expect(controller.hasActiveEventStream == false)

        await controller.stopEventStream()
    }

    @Test @MainActor func pingEventStreamForwardsPingToActiveStream() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }

        await controller.pingEventStream()

        await waitUntil {
            await eventProvider.recordedPingCount() == 1
        }

        #expect(appState.lastError == nil)

        await controller.stopEventStream()
    }

    @Test @MainActor func hasActiveEventStreamReflectsStreamLifecycle() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        #expect(controller.hasActiveEventStream == false)

        await controller.startEventStream()
        await waitUntil {
            controller.hasActiveEventStream
        }

        #expect(controller.hasActiveEventStream == true)

        await controller.stopEventStream()

        #expect(controller.hasActiveEventStream == false)
    }

    @Test @MainActor func pingEventStreamFailureMarksConnectionStaleAndRecordsError() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        await eventProvider.setPingResult(.failure(.networkFailure("ping timeout")))
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }

        await controller.pingEventStream()

        #expect(await eventProvider.recordedPingCount() == 1)
        #expect(appState.connectionState == .stale)
        #expect(appState.lastError == .networkFailure("ping timeout"))
        #expect(controller.hasActiveEventStream == false)

        await controller.stopEventStream()
    }

    @Test @MainActor func pingEventStreamSessionUnavailableClearsSelectedSessionAndSessionState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-missing"
        appState.snapshot = makeSnapshot(sessionID: "sess-missing", missions: [])
        seedWorkbenchLists(appState)
        seedSelectedDetails(appState)
        appState.connectionState = .connected

        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        await eventProvider.setPingResult(.failure(.sessionUnavailable))
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-missing"]
        }

        await controller.pingEventStream()

        #expect(await eventProvider.recordedPingCount() == 1)
        #expect(appState.connectionState == .stale)
        #expect(appState.lastError == .sessionUnavailable)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
        expectWorkbenchListsEmpty(appState)
        expectSelectedDetailsEmpty(appState)
        #expect(controller.hasActiveEventStream == false)

        await controller.stopEventStream()
    }

    @Test @MainActor func eventStreamFailureMarksConnectionStaleAndRecordsError() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }
        await eventProvider.fail(.networkFailure("lost websocket"))

        await waitUntil {
            appState.connectionState == .stale
        }

        #expect(appState.lastError == .networkFailure("lost websocket"))
        #expect(controller.hasActiveEventStream == false)

        await controller.stopEventStream()
    }

    @Test @MainActor func eventStreamConnectSessionUnavailableClearsSelectedSessionAndSessionState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-missing"
        appState.snapshot = makeSnapshot(sessionID: "sess-missing", missions: [])
        seedWorkbenchLists(appState)
        seedSelectedDetails(appState)
        appState.connectionState = .connected

        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        await eventProvider.setConnectResult(.failure(.sessionUnavailable))
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()

        await waitUntil {
            appState.connectionState == .stale
        }

        #expect(appState.lastError == .sessionUnavailable)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
        expectWorkbenchListsEmpty(appState)
        expectSelectedDetailsEmpty(appState)
        #expect(controller.hasActiveEventStream == false)

        await controller.stopEventStream()
    }

    @Test @MainActor func eventStreamInvalidAPIKeyRecordsAuthFailed() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-events"
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-events"]
        }
        await eventProvider.emit(.error(message: "Invalid API key"))

        await waitUntil {
            appState.connectionState == .stale
        }

        #expect(appState.lastError == .authFailed)

        await controller.stopEventStream()
    }

    @Test @MainActor func eventStreamSessionNotFoundRecordsSessionUnavailable() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-missing"
        appState.snapshot = makeSnapshot(sessionID: "sess-missing", missions: [])
        seedWorkbenchLists(appState)
        seedSelectedDetails(appState)
        appState.connectionState = .connected
        let api = FakeWorkbenchAPIProvider()
        let eventProvider = FakeWorkbenchEventProvider()
        let controller = DaemonController(
            appState: appState,
            apiProvider: api,
            eventProvider: eventProvider
        )

        await controller.startEventStream()
        await waitUntil {
            await eventProvider.connectedSessionIDs == ["sess-missing"]
        }
        await eventProvider.emit(.error(message: "Session not found"))

        await waitUntil {
            appState.connectionState == .stale
        }

        #expect(appState.lastError == .sessionUnavailable)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
        expectWorkbenchListsEmpty(appState)
        expectSelectedDetailsEmpty(appState)

        await controller.stopEventStream()
    }

    @Test @MainActor func refreshConnectionSnapshotFailureSkipsPreWarmingAndKeepsSnapshotError() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-existing"
        appState.snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-stale",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )
        seedWorkbenchLists(appState)
        seedSelectedDetails(appState)

        let api = FakeWorkbenchAPIProvider()
        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSnapshotResult(.failure(.httpStatus(500)))
        await configureWorkbenchListResults(for: api, sessionID: "sess-existing")

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-existing")
        #expect(appState.snapshot == nil)
        #expect(appState.lastError == .httpStatus(500))
        expectWorkbenchListsEmpty(appState)
        expectSelectedDetailsEmpty(appState)
    }

    @Test @MainActor func refreshConnectionSessionUnavailableClearsSelectedSessionAndSessionState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-missing"
        appState.snapshot = makeSnapshot(sessionID: "sess-missing", missions: [])
        seedWorkbenchLists(appState)
        seedSelectedDetails(appState)

        let api = FakeWorkbenchAPIProvider()
        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSnapshotResult(.failure(.sessionUnavailable))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
        #expect(appState.lastError == .sessionUnavailable)
        expectWorkbenchListsEmpty(appState)
        expectSelectedDetailsEmpty(appState)
    }

    @Test @MainActor func refreshSessionsSuccessWritesSessionsAndClearsStaleError() async throws {
        let appState = AppState()
        appState.lastError = .httpStatus(500)

        let session = makeSession(id: "sess-001", title: "Session One")
        let sessions = SessionListDTO(
            sessions: [session],
            total: 1,
            page: 1,
            pageSize: 10
        )

        let api = FakeWorkbenchAPIProvider()
        await api.setSessionsResult(.success(sessions))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshSessions(page: 1, pageSize: 10)

        #expect(appState.sessions == [session])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshSessionsFailurePreservesOldSessionsAndRecordsError() async throws {
        let appState = AppState()
        let oldSession = makeSession(id: "sess-old", title: "Old Session")
        appState.sessions = [oldSession]

        let api = FakeWorkbenchAPIProvider()
        await api.setSessionsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshSessions(page: 1, pageSize: 10)

        #expect(appState.sessions == [oldSession])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func createSessionSuccessSelectsSessionAndPreWarmsWorkbenchLists() async throws {
        let appState = AppState()
        let existingSession = makeSession(id: "sess-old", title: "Old Session")
        let createdSession = makeSession(id: "sess-new", title: "Mac 工作台")
        let snapshot = makeSnapshot(sessionID: "sess-new", missions: [])
        appState.sessions = [existingSession]

        let api = FakeWorkbenchAPIProvider()
        await api.setCreateWorkbenchSessionResult(.success(WorkbenchBootstrapDTO(
            daemonStatus: makeStatus(),
            capabilities: makeCapabilities(),
            sessions: [createdSession],
            totalSessions: 2,
            selectedSessionID: "sess-new",
            snapshot: snapshot
        )))
        await configureWorkbenchListResults(for: api, sessionID: "sess-new")

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createSession(
            title: "Mac 工作台",
            model: "gpt-5",
            systemPrompt: "默认中文治理工作台"
        )

        #expect(await api.createdSessions == [[
            "title": "Mac 工作台",
            "model": "gpt-5",
            "systemPrompt": "默认中文治理工作台",
        ]])
        #expect(await api.createWorkbenchSessionCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
        #expect(appState.sessions == [createdSession, existingSession])
        #expect(appState.selectedSessionID == "sess-new")
        #expect(appState.snapshot == snapshot)
        #expect(appState.daemonStatus == makeStatus())
        #expect(appState.capabilities == makeCapabilities())
        #expect(appState.lastError == nil)
        expectWorkbenchListsPopulated(appState)
    }

    @Test @MainActor func createSessionFailurePreservesWorkbenchStateAndRecordsError() async throws {
        let appState = AppState()
        let existingSession = makeSession(id: "sess-old", title: "Old Session")
        let snapshot = makeSnapshot(sessionID: "sess-old", missions: [makeMission(id: "mission-1", sessionID: "sess-old")])
        let issue = makeIssue(taskID: "task-existing", missionID: "mission-1")
        appState.sessions = [existingSession]
        appState.selectedSessionID = "sess-old"
        appState.snapshot = snapshot
        appState.issues = [issue]

        let api = FakeWorkbenchAPIProvider()
        await api.setCreateWorkbenchSessionResult(.failure(.httpStatus(503)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createSession(
            title: "Mac 工作台",
            model: "gpt-5",
            systemPrompt: "默认中文治理工作台"
        )

        #expect(await api.createdSessions == [[
            "title": "Mac 工作台",
            "model": "gpt-5",
            "systemPrompt": "默认中文治理工作台",
        ]])
        #expect(await api.createWorkbenchSessionCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
        #expect(appState.sessions == [existingSession])
        #expect(appState.selectedSessionID == "sess-old")
        #expect(appState.snapshot == snapshot)
        #expect(appState.issues == [issue])
        #expect(appState.lastError == .httpStatus(503))
    }

    @Test @MainActor func selectSessionSuccessClearsStaleListsSelectsAndPreWarms() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-old"
        appState.snapshot = makeSnapshot(sessionID: "sess-old", missions: [])
        appState.timelineEvents = [makeEvent(id: "evt-old", type: "old.event", subjectID: "old")]
        appState.validationRuns = [ValidationRunDTO(
            id: "run-old",
            sessionID: "sess-old",
            taskID: "task-old",
            actor: "Runner",
            command: [],
            cwd: "",
            status: "passed",
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T05:00:00",
            completedAt: "2026-06-27T05:00:01"
        )]
        appState.contextSnapshots = [ContextSnapshotDTO(
            id: "ctx-old",
            sessionID: "sess-old",
            agentID: "agent-old",
            taskID: "task-old",
            health: "good",
            reasons: [],
            createdAt: "2026-06-27T05:00:00"
        )]
        appState.approvals = [makeApproval(id: "approval-old", missionID: "mission-old", state: "waiting")]
        appState.failures = [makeFailure(id: "failure-old", taskID: "task-old", status: "open")]
        appState.issues = [makeIssue(taskID: "task-old")]
        appState.leases = [makeLease(id: "lease-old", taskID: "task-old", state: "active")]
        appState.missions = [makeMission(id: "mission-old", sessionID: "sess-old")]
        appState.agentProfiles = [makeAgentProfile(id: "agent-old", sessionID: "sess-old", status: "idle")]
        seedSelectedDetails(appState)

        let api = FakeWorkbenchAPIProvider()
        let snapshot = makeSnapshot(sessionID: "sess-new", missions: [])
        await api.setSnapshotResult(.success(snapshot))
        await configureWorkbenchListResults(for: api, sessionID: "sess-new")

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.selectSession("sess-new")

        #expect(appState.selectedSessionID == "sess-new")
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
        expectWorkbenchListsPopulated(appState)
    }

    @Test @MainActor func selectSessionSnapshotFailureClearsListsKeepsSelectionAndSkipsPreWarm() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-old"
        appState.snapshot = makeSnapshot(sessionID: "sess-old", missions: [])
        appState.timelineEvents = [makeEvent(id: "evt-old", type: "old.event", subjectID: "old")]
        appState.validationRuns = [ValidationRunDTO(
            id: "run-old",
            sessionID: "sess-old",
            taskID: "task-old",
            actor: "Runner",
            command: [],
            cwd: "",
            status: "passed",
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T05:00:00",
            completedAt: "2026-06-27T05:00:01"
        )]
        appState.contextSnapshots = [ContextSnapshotDTO(
            id: "ctx-old",
            sessionID: "sess-old",
            agentID: "agent-old",
            taskID: "task-old",
            health: "good",
            reasons: [],
            createdAt: "2026-06-27T05:00:00"
        )]
        appState.approvals = [makeApproval(id: "approval-old", missionID: "mission-old", state: "waiting")]
        appState.failures = [makeFailure(id: "failure-old", taskID: "task-old", status: "open")]
        appState.issues = [makeIssue(taskID: "task-old")]
        appState.leases = [makeLease(id: "lease-old", taskID: "task-old", state: "active")]
        appState.missions = [makeMission(id: "mission-old", sessionID: "sess-old")]
        appState.agentProfiles = [makeAgentProfile(id: "agent-old", sessionID: "sess-old", status: "idle")]
        seedSelectedDetails(appState)

        let api = FakeWorkbenchAPIProvider()
        await api.setSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.selectSession("sess-new")

        #expect(appState.selectedSessionID == "sess-new")
        #expect(appState.snapshot == nil)
        #expect(appState.lastError == .httpStatus(500))
        expectWorkbenchListsEmpty(appState)
        #expect(appState.selectedEvent == nil)
        #expect(appState.selectedValidationRun == nil)
        #expect(appState.selectedContextSnapshot == nil)
        #expect(appState.selectedApproval == nil)
        #expect(appState.selectedFailure == nil)
        #expect(appState.selectedIssue == nil)
        #expect(appState.selectedLease == nil)
        #expect(appState.selectedWorktree == nil)
        #expect(appState.selectedMission == nil)
        #expect(appState.selectedAgentProfile == nil)
        #expect(appState.selectedDecision == nil)
        #expect(appState.selectedIntentLock == nil)
    }

    @Test @MainActor func claimIssueSuccessRefreshesSnapshotIssuesLeasesAndEvents() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lease = makeLease(id: "lease-001", taskID: "task-001", state: "active")
        let snapshot = makeSnapshot(sessionID: "sess-001", lease: lease)
        let issue = makeIssue(taskID: "task-001")
        let issues = IssuesDTO(issues: [issue], missionID: nil, riskLevel: nil, limit: 50)
        let leases = LeasesDTO(leases: [lease], state: nil, taskID: nil, agentID: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "issue.claimed", subjectID: "task-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setClaimIssueWithSnapshotResult(.success(LeaseSnapshotDTO(lease: lease, snapshot: snapshot)))
        await api.setIssuesResult(.success(issues))
        await api.setLeasesResult(.success(leases))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.claimIssue(
            taskID: "task-001",
            agentID: "local-agent",
            durationMinutes: 30,
            worktreeName: "wt-001"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.issues == [issue])
        #expect(appState.leases == [lease])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func claimIssueSuccessUsesIncludedSnapshotWithoutExtraSnapshotFetch() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lease = makeLease(id: "lease-001", taskID: "task-001", state: "active")
        let snapshot = makeSnapshot(sessionID: "sess-001", lease: lease)
        let issue = makeIssue(taskID: "task-001")
        let issues = IssuesDTO(issues: [issue], missionID: nil, riskLevel: nil, limit: 50)
        let leases = LeasesDTO(leases: [lease], state: nil, taskID: nil, agentID: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "issue.claimed", subjectID: "task-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setClaimIssueWithSnapshotResult(.success(LeaseSnapshotDTO(lease: lease, snapshot: snapshot)))
        await api.setIssuesResult(.success(issues))
        await api.setLeasesResult(.success(leases))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.claimIssue(
            taskID: "task-001",
            agentID: "local-agent",
            durationMinutes: 30,
            worktreeName: "wt-001"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.issues == [issue])
        #expect(appState.leases == [lease])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.claimIssueWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func claimIssueWithSnapshotFailurePreservesExistingState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let staleIssue = makeIssue(taskID: "stale-task")
        let staleLease = makeLease(id: "stale-lease", taskID: "stale-task", state: "active")
        let staleEvent = makeEvent(id: "stale-event", type: "task.updated", subjectID: "stale-task")
        appState.snapshot = staleSnapshot
        appState.issues = [staleIssue]
        appState.leases = [staleLease]
        appState.timelineEvents = [staleEvent]

        let api = FakeWorkbenchAPIProvider()
        await api.setClaimIssueWithSnapshotResult(.failure(.httpStatus(503)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.claimIssue(
            taskID: "task-001",
            agentID: "local-agent",
            durationMinutes: 30,
            worktreeName: "wt-001"
        )

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.issues == [staleIssue])
        #expect(appState.leases == [staleLease])
        #expect(appState.timelineEvents == [staleEvent])
        #expect(appState.lastError == .httpStatus(503))
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func claimIssueSessionUnavailableClearsSelectedSessionAndSessionState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-missing"
        appState.snapshot = makeSnapshot(sessionID: "sess-missing", missions: [])
        seedWorkbenchLists(appState)
        seedSelectedDetails(appState)

        let api = FakeWorkbenchAPIProvider()
        await api.setClaimIssueWithSnapshotResult(.failure(.sessionUnavailable))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.claimIssue(
            taskID: "task-001",
            agentID: "local-agent",
            durationMinutes: 30,
            worktreeName: "wt-001"
        )

        #expect(appState.lastError == .sessionUnavailable)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
        expectWorkbenchListsEmpty(appState)
        expectSelectedDetailsEmpty(appState)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func claimIssueWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.claimIssue(
            taskID: "task-001",
            agentID: "local-agent",
            durationMinutes: 30,
            worktreeName: "wt-001"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func releaseLeaseSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lease = makeLease(id: "lease-001", taskID: "task-001", state: "released")
        let snapshot = makeSnapshot(sessionID: "sess-001", lease: lease)
        let leases = LeasesDTO(leases: [lease], state: nil, taskID: nil, agentID: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "lease.released", subjectID: "lease-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setReleaseLeaseWithSnapshotResult(.success(
            LeaseSnapshotDTO(lease: lease, snapshot: snapshot)
        ))
        await api.setLeasesResult(.success(leases))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.releaseLease(leaseID: "lease-001")

        #expect(appState.snapshot == snapshot)
        #expect(appState.leases == [lease])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.releaseLeaseWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func expireLeasesSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let expiredLease = makeLease(id: "lease-001", taskID: "task-001", state: "expired")
        let snapshot = makeSnapshot(sessionID: "sess-001", lease: expiredLease)
        let leases = LeasesDTO(leases: [expiredLease], state: nil, taskID: nil, agentID: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "leases.expired", subjectID: "lease-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setExpireLeasesWithSnapshotResult(.success(
            ExpiredLeasesSnapshotDTO(expired: [expiredLease], snapshot: snapshot)
        ))
        await api.setLeasesResult(.success(leases))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.expireLeases()

        #expect(appState.snapshot == snapshot)
        #expect(appState.leases == [expiredLease])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.expireLeasesWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func expireLeasesWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.expireLeases()

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func expireLeasesFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setExpireLeasesWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.expireLeases()

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func createMissionSuccessRefreshesMissionsIssuesSnapshotAndEvents() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let mission = makeMission(id: "mission-001", sessionID: "sess-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [mission])
        let issues = IssuesDTO(issues: [], missionID: nil, riskLevel: nil, limit: 50)
        let missions = MissionsDTO(missions: [mission], status: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "mission.created", subjectID: "mission-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setCreateMissionWithSnapshotResult(.success(
            MissionSnapshotDTO(mission: mission, snapshot: snapshot)
        ))
        await api.setIssuesResult(.success(issues))
        await api.setMissionsResult(.success(missions))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Mac 工作台", goal: "补齐 API 调用面")

        #expect(appState.missions == [mission])
        #expect(appState.issues.isEmpty)
        #expect(appState.snapshot == snapshot)
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func createMissionSuccessUsesIncludedSnapshotWithoutExtraSnapshotFetch() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        let mission = makeMission(id: "mission-001", sessionID: "sess-001")
        let freshSnapshot = makeSnapshot(sessionID: "sess-001", missions: [mission])
        let issues = IssuesDTO(issues: [], missionID: nil, riskLevel: nil, limit: 50)
        let missions = MissionsDTO(missions: [mission], status: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "mission.created", subjectID: "mission-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setCreateMissionWithSnapshotResult(.success(
            MissionSnapshotDTO(mission: mission, snapshot: freshSnapshot)
        ))
        await api.setIssuesResult(.success(issues))
        await api.setMissionsResult(.success(missions))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Mac 工作台", goal: "补齐 API 调用面")

        #expect(appState.missions == [mission])
        #expect(appState.issues.isEmpty)
        #expect(appState.snapshot == freshSnapshot)
        #expect(appState.snapshot != staleSnapshot)
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.createMissionWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func createMissionWithoutSelectedSessionCreatesDefaultSessionFirst() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let session = makeSession(id: "sess-new", title: "Mac 工作台")
        let mission = makeMission(id: "mission-001", sessionID: "sess-new")
        let snapshot = makeSnapshot(sessionID: "sess-new", missions: [mission])
        let issues = IssuesDTO(issues: [], missionID: nil, riskLevel: nil, limit: 50)
        let missions = MissionsDTO(missions: [mission], status: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "mission.created", subjectID: "mission-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setCreateSessionResult(.success(session))
        await api.setCreateMissionWithSnapshotResult(.success(
            MissionSnapshotDTO(mission: mission, snapshot: snapshot)
        ))
        await configureWorkbenchListResults(for: api, sessionID: "sess-new")
        await api.setIssuesResult(.success(issues))
        await api.setMissionsResult(.success(missions))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Mac 工作台", goal: "补齐 API 调用面")

        #expect(await api.createdSessions == [[
            "title": "Mac 工作台",
            "model": nil,
            "systemPrompt": nil,
        ]])
        #expect(await api.createdMissions == [[
            "sessionID": "sess-new",
            "title": "Mac 工作台",
            "goal": "补齐 API 调用面",
        ]])
        #expect(appState.sessions == [session])
        #expect(appState.selectedSessionID == "sess-new")
        #expect(appState.snapshot == snapshot)
        #expect(appState.missions == [mission])
        #expect(appState.issues.isEmpty)
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.snapshotCallCount == 1)
        #expect(await api.createMissionWithSnapshotCallCount == 1)
    }

    @Test @MainActor func createMissionWithoutSelectedSessionRecordsSessionCreationError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        await api.setCreateSessionResult(.failure(.httpStatus(503)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Title", goal: "Goal")

        #expect(await api.createdMissions.isEmpty)
        #expect(appState.lastError == .httpStatus(503))
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func createMissionFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setCreateMissionWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Title", goal: "Goal")

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func createMissionListRefreshFailureKeepsIncludedSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let mission = makeMission(id: "mission-001", sessionID: "sess-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [mission])
        let issues = IssuesDTO(issues: [], missionID: nil, riskLevel: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "mission.created", subjectID: "mission-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setCreateMissionWithSnapshotResult(.success(
            MissionSnapshotDTO(mission: mission, snapshot: snapshot)
        ))
        await api.setIssuesResult(.success(issues))
        await api.setMissionsResult(.failure(.httpStatus(503)))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Mac 工作台", goal: "补齐 API 调用面")

        #expect(appState.missions.isEmpty)
        #expect(appState.issues.isEmpty)
        #expect(appState.timelineEvents == [event])
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == .httpStatus(503))
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func refreshMissionsSuccessWritesMissions() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let mission = makeMission(id: "mission-001", sessionID: "sess-001")
        let missions = MissionsDTO(missions: [mission], status: "active", limit: 25)

        await api.setMissionsResult(.success(missions))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshMissions(status: "active", limit: 25)

        #expect(appState.missions == [mission])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshMissionsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshMissions(status: "active", limit: 25)

        #expect(appState.missions.isEmpty)
        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
    }

    @Test @MainActor func refreshMissionsFailurePreservesOldMissions() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleMission = makeMission(id: "mission-stale", sessionID: "sess-001")
        appState.missions = [staleMission]

        let api = FakeWorkbenchAPIProvider()
        await api.setMissionsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshMissions(status: "active", limit: 25)

        #expect(appState.missions == [staleMission])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func attachIssueSuccessRefreshesSnapshotMissionIssuesAndEvents() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let issue = makeIssue(taskID: "task-001", missionID: "mission-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", issues: [issue])
        let issues = IssuesDTO(issues: [issue], missionID: "mission-001", riskLevel: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "issue.attached", subjectID: "task-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setAttachIssueWithSnapshotResult(.success(
            IssueSnapshotDTO(issue: issue, snapshot: snapshot)
        ))
        await api.setIssuesResult(.success(issues))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: ["通过 Swift 编译"],
            parallelMode: "exclusive",
            riskLevel: "medium"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.issues == [issue])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func attachIssueSuccessUsesIncludedSnapshotWithoutExtraSnapshotFetch() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", issues: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        let issue = makeIssue(taskID: "task-001", missionID: "mission-001")
        let freshSnapshot = makeSnapshot(sessionID: "sess-001", issues: [issue])
        let issues = IssuesDTO(issues: [issue], missionID: "mission-001", riskLevel: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "issue.attached", subjectID: "task-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setAttachIssueWithSnapshotResult(.success(
            IssueSnapshotDTO(issue: issue, snapshot: freshSnapshot)
        ))
        await api.setIssuesResult(.success(issues))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: ["通过 Swift 编译"],
            parallelMode: "exclusive",
            riskLevel: "medium"
        )

        #expect(appState.snapshot == freshSnapshot)
        #expect(appState.snapshot != staleSnapshot)
        #expect(appState.issues == [issue])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.attachIssueWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func attachIssueWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: [],
            parallelMode: "exclusive",
            riskLevel: "low"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func attachIssueFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", issues: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setAttachIssueWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: [],
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func attachIssueListRefreshFailureKeepsIncludedSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let issue = makeIssue(taskID: "task-001", missionID: "mission-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", issues: [issue])
        let event = makeEvent(id: "evt-001", type: "issue.attached", subjectID: "task-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setAttachIssueWithSnapshotResult(.success(
            IssueSnapshotDTO(issue: issue, snapshot: snapshot)
        ))
        await api.setIssuesResult(.failure(.httpStatus(503)))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: ["通过 Swift 编译"],
            parallelMode: "exclusive",
            riskLevel: "medium"
        )

        #expect(appState.issues.isEmpty)
        #expect(appState.timelineEvents == [event])
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == .httpStatus(503))
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func createIssueSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let issue = makeIssue(taskID: "task-009", missionID: "mission-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", issues: [issue])
        let issues = IssuesDTO(issues: [issue], missionID: "mission-001", riskLevel: nil, limit: 50)
        let event = makeEvent(id: "evt-009", type: "issue.created", subjectID: "task-009")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setCreateIssueWithSnapshotResult(.success(IssueSnapshotDTO(issue: issue, snapshot: snapshot)))
        await api.setIssuesResult(.success(issues))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createIssue(
            missionID: "mission-001",
            title: "实现 Issue 创建 API",
            description: "创建 backing task",
            blockedBy: ["1"],
            acceptanceCriteria: ["可被 Agent claim"],
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.issues == [issue])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.createIssueWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func createIssueWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createIssue(
            missionID: "mission-001",
            title: "Title",
            description: "",
            blockedBy: [],
            acceptanceCriteria: [],
            parallelMode: "exclusive",
            riskLevel: "low"
        )

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func refreshEventsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let event = EventDTO(
            id: "evt-001",
            sessionID: "sess-001",
            type: "mission.created",
            actor: "Human",
            subjectID: "mission-001",
            payload: ["title": .string("Mac Workbench")],
            timestamp: "2026-06-27T06:00:00"
        )
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshEvents(limit: 50)

        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshEventsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.timelineEvents = [
            EventDTO(
                id: "evt-stale",
                sessionID: "old-session",
                type: "mission.created",
                actor: "Human",
                subjectID: "mission-old",
                payload: ["title": .string("旧事件")],
                timestamp: "2026-06-27T05:00:00"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshEvents(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.timelineEvents.isEmpty)
    }

    @Test @MainActor func refreshValidationRunsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let run = ValidationRunDTO(
            id: "run-001",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "ValidationRunner",
            command: ["pytest", "test.py"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "ok",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )
        let runs = ValidationRunsDTO(validationRuns: [run], taskID: "task-001", limit: 25)

        await api.setValidationRunsResult(.success(runs))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshValidationRuns(taskID: "task-001", limit: 25)

        #expect(appState.validationRuns == [run])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshValidationRunsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.validationRuns = [
            ValidationRunDTO(
                id: "run-stale",
                sessionID: "old-session",
                taskID: "task-old",
                actor: "ValidationRunner",
                command: [],
                cwd: "",
                status: "passed",
                exitCode: 0,
                output: "",
                startedAt: "2026-06-27T05:00:00",
                completedAt: "2026-06-27T05:00:01"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshValidationRuns(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.validationRuns.isEmpty)
    }

    @Test @MainActor func refreshValidationRunsFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleRun = ValidationRunDTO(
            id: "run-stale",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "ValidationRunner",
            command: [],
            cwd: "",
            status: "passed",
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T05:00:00",
            completedAt: "2026-06-27T05:00:01"
        )
        appState.validationRuns = [staleRun]

        let api = FakeWorkbenchAPIProvider()
        await api.setValidationRunsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshValidationRuns(limit: 50)

        #expect(appState.validationRuns == [staleRun])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshContextSnapshotsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let snapshot = ContextSnapshotDTO(
            id: "snap-001",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "good",
            reasons: ["上下文健康"],
            createdAt: "2026-06-27T06:00:00"
        )
        let snapshots = ContextSnapshotsDTO(
            contextSnapshots: [snapshot],
            taskID: "task-001",
            agentID: "agent-001",
            limit: 25
        )

        await api.setContextSnapshotsResult(.success(snapshots))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshContextSnapshots(taskID: "task-001", agentID: "agent-001", limit: 25)

        #expect(appState.contextSnapshots == [snapshot])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshContextSnapshotsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.contextSnapshots = [
            ContextSnapshotDTO(
                id: "snap-stale",
                sessionID: "old-session",
                agentID: "agent-old",
                taskID: "task-old",
                health: "stale",
                reasons: ["旧数据"],
                createdAt: "2026-06-27T05:00:00"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshContextSnapshots(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.contextSnapshots.isEmpty)
    }

    @Test @MainActor func refreshContextSnapshotsFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = ContextSnapshotDTO(
            id: "snap-stale",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "stale",
            reasons: ["旧数据"],
            createdAt: "2026-06-27T05:00:00"
        )
        appState.contextSnapshots = [staleSnapshot]

        let api = FakeWorkbenchAPIProvider()
        await api.setContextSnapshotsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshContextSnapshots(limit: 50)

        #expect(appState.contextSnapshots == [staleSnapshot])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func recordContextHealthSuccessPrependsSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let existingSnapshot = ContextSnapshotDTO(
            id: "snap-002",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "good",
            reasons: ["现有快照"],
            createdAt: "2026-06-27T06:00:00"
        )
        appState.contextSnapshots = [existingSnapshot]

        let api = FakeWorkbenchAPIProvider()
        let newSnapshot = ContextSnapshotDTO(
            id: "snap-001",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "good",
            reasons: ["上下文健康"],
            createdAt: "2026-06-27T06:01:00"
        )
        await api.setRecordContextHealthWithSnapshotResult(.success(
            ContextHealthSnapshotDTO(
                contextSnapshot: newSnapshot,
                snapshot: makeSnapshot(sessionID: "sess-001", missions: [])
            )
        ))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.recordContextHealth(
            taskID: "task-001",
            agentID: "agent-001",
            minutesSinceSync: 5,
            tokenLoadRatio: 0.75,
            policyConflict: false,
            actor: "Human"
        )

        #expect(appState.contextSnapshots == [newSnapshot, existingSnapshot])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func recordContextHealthSuccessUsesIncludedWorkbenchSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleWorkbenchSnapshot = makeSnapshot(
            sessionID: "sess-001",
            missions: [makeMission(id: "mission-stale", sessionID: "sess-001")]
        )
        appState.snapshot = staleWorkbenchSnapshot
        let existingSnapshot = ContextSnapshotDTO(
            id: "snap-002",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "stale",
            reasons: ["旧上下文"],
            createdAt: "2026-06-27T06:00:00"
        )
        appState.contextSnapshots = [existingSnapshot]

        let api = FakeWorkbenchAPIProvider()
        let freshWorkbenchSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let newContextSnapshot = ContextSnapshotDTO(
            id: "snap-001",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "good",
            reasons: ["上下文健康"],
            createdAt: "2026-06-27T06:01:00"
        )
        await api.setRecordContextHealthWithSnapshotResult(.success(
            ContextHealthSnapshotDTO(
                contextSnapshot: newContextSnapshot,
                snapshot: freshWorkbenchSnapshot
            )
        ))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.recordContextHealth(
            taskID: "task-001",
            agentID: "agent-001",
            minutesSinceSync: 5,
            tokenLoadRatio: 0.75,
            policyConflict: false,
            actor: "Human"
        )

        #expect(appState.contextSnapshots == [newContextSnapshot, existingSnapshot])
        #expect(appState.snapshot == freshWorkbenchSnapshot)
        #expect(appState.snapshot != staleWorkbenchSnapshot)
        #expect(appState.lastError == nil)
        #expect(await api.recordContextHealthWithSnapshotCallCount == 1)
    }

    @Test @MainActor func recordContextHealthSuccessReplacesDuplicateSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldSnapshot = ContextSnapshotDTO(
            id: "snap-001",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "stale",
            reasons: ["旧数据"],
            createdAt: "2026-06-27T06:00:00"
        )
        let otherSnapshot = ContextSnapshotDTO(
            id: "snap-002",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "good",
            reasons: ["其他快照"],
            createdAt: "2026-06-27T06:00:30"
        )
        appState.contextSnapshots = [oldSnapshot, otherSnapshot]

        let api = FakeWorkbenchAPIProvider()
        let updatedSnapshot = ContextSnapshotDTO(
            id: "snap-001",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "good",
            reasons: ["已更新"],
            createdAt: "2026-06-27T06:01:00"
        )
        await api.setRecordContextHealthWithSnapshotResult(.success(
            ContextHealthSnapshotDTO(
                contextSnapshot: updatedSnapshot,
                snapshot: makeSnapshot(sessionID: "sess-001", missions: [])
            )
        ))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.recordContextHealth(
            taskID: "task-001",
            agentID: "agent-001",
            minutesSinceSync: 3,
            tokenLoadRatio: 0.5,
            policyConflict: false,
            actor: "Human"
        )

        #expect(appState.contextSnapshots == [updatedSnapshot, otherSnapshot])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func recordContextHealthWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.contextSnapshots = [
            ContextSnapshotDTO(
                id: "snap-stale",
                sessionID: "old-session",
                agentID: "agent-old",
                taskID: "task-old",
                health: "stale",
                reasons: ["旧数据"],
                createdAt: "2026-06-27T05:00:00"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.recordContextHealth(
            taskID: "task-001",
            agentID: "agent-001",
            minutesSinceSync: 5,
            tokenLoadRatio: 0.75,
            policyConflict: false,
            actor: "Human"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.contextSnapshots.isEmpty)
    }

    @Test @MainActor func recordContextHealthFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = ContextSnapshotDTO(
            id: "snap-stale",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "stale",
            reasons: ["旧数据"],
            createdAt: "2026-06-27T05:00:00"
        )
        appState.contextSnapshots = [staleSnapshot]

        let api = FakeWorkbenchAPIProvider()
        await api.setRecordContextHealthWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.recordContextHealth(
            taskID: "task-001",
            agentID: "agent-001",
            minutesSinceSync: 5,
            tokenLoadRatio: 0.75,
            policyConflict: false,
            actor: "Human"
        )

        #expect(appState.contextSnapshots == [staleSnapshot])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshApprovalsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let approval = makeApproval(id: "approval-001", missionID: "mission-001", state: "waiting")
        let approvals = ApprovalsDTO(approvals: [approval], state: "waiting", limit: 25)

        await api.setApprovalsResult(.success(approvals))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshApprovals(state: "waiting", limit: 25)

        #expect(appState.approvals == [approval])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshApprovalsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.approvals = [
            makeApproval(id: "approval-stale", missionID: "mission-001", state: "waiting")
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshApprovals(state: "waiting", limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.approvals.isEmpty)
    }

    @Test @MainActor func refreshApprovalsFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleApproval = makeApproval(id: "approval-stale", missionID: "mission-001", state: "waiting")
        appState.approvals = [staleApproval]

        let api = FakeWorkbenchAPIProvider()
        await api.setApprovalsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshApprovals(state: "waiting", limit: 50)

        #expect(appState.approvals == [staleApproval])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshFailuresSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let failure = makeFailure(id: "failure-001", taskID: "task-001", status: "open")
        let failures = FailuresDTO(failures: [failure], taskID: "task-001", status: "open", limit: 25)

        await api.setFailuresResult(.success(failures))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshFailures(taskID: "task-001", status: "open", limit: 25)

        #expect(appState.failures == [failure])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshFailuresWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.failures = [
            makeFailure(id: "failure-stale", taskID: "task-old", status: "open")
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshFailures(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.failures.isEmpty)
    }

    @Test @MainActor func refreshFailuresFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleFailure = makeFailure(id: "failure-stale", taskID: "task-001", status: "open")
        appState.failures = [staleFailure]

        let api = FakeWorkbenchAPIProvider()
        await api.setFailuresResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshFailures(limit: 50)

        #expect(appState.failures == [staleFailure])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshIssuesSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let issue = makeIssue(taskID: "task-001", missionID: "mission-001")
        let issues = IssuesDTO(issues: [issue], missionID: "mission-001", riskLevel: "medium", limit: 25)

        await api.setIssuesResult(.success(issues))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshIssues(missionID: "mission-001", riskLevel: "medium", limit: 25)

        #expect(appState.issues == [issue])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshIssuesWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.issues = [makeIssue(taskID: "task-stale", missionID: "mission-old")]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshIssues(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.issues.isEmpty)
    }

    @Test @MainActor func refreshIssuesFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleIssue = makeIssue(taskID: "task-stale", missionID: "mission-001")
        appState.issues = [staleIssue]

        let api = FakeWorkbenchAPIProvider()
        await api.setIssuesResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshIssues(limit: 50)

        #expect(appState.issues == [staleIssue])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshLeasesSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lease = makeLease(id: "lease-001", taskID: "task-001", state: "active")
        let leases = LeasesDTO(
            leases: [lease],
            state: "active",
            taskID: "task-001",
            agentID: "agent-001",
            limit: 25
        )

        await api.setLeasesResult(.success(leases))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshLeases(state: "active", taskID: "task-001", agentID: "agent-001", limit: 25)

        #expect(appState.leases == [lease])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshLeasesWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.leases = [makeLease(id: "lease-stale", taskID: "task-old", state: "active")]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshLeases(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.leases.isEmpty)
    }

    @Test @MainActor func refreshLeasesFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleLease = makeLease(id: "lease-stale", taskID: "task-001", state: "active")
        appState.leases = [staleLease]

        let api = FakeWorkbenchAPIProvider()
        await api.setLeasesResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshLeases(limit: 50)

        #expect(appState.leases == [staleLease])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshWorktreesSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let worktree = makeWorktree(name: "wt-api-client", taskID: "task-001", status: "active")
        let worktrees = WorktreesDTO(worktrees: [worktree], taskID: "task-001", status: "active", limit: 25)

        await api.setWorktreesResult(.success(worktrees))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshWorktrees(taskID: "task-001", status: "active", limit: 25)

        #expect(appState.worktrees == [worktree])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshWorktreesWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.worktrees = [makeWorktree(name: "wt-stale", taskID: "task-old", status: "active")]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshWorktrees(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.worktrees.isEmpty)
    }

    @Test @MainActor func refreshWorktreesFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleWorktree = makeWorktree(name: "wt-stale", taskID: "task-001", status: "active")
        appState.worktrees = [staleWorktree]

        let api = FakeWorkbenchAPIProvider()
        await api.setWorktreesResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshWorktrees(limit: 50)

        #expect(appState.worktrees == [staleWorktree])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func keepWorktreeSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        appState.snapshot = makeSnapshot(sessionID: "sess-001", missions: [
            makeMission(id: "mission-stale", sessionID: "sess-001")
        ])
        let staleWorktree = makeWorktree(name: "wt-api-client", taskID: "task-001", status: "active")
        let keptWorktree = makeWorktree(
            name: "wt-api-client",
            taskID: "task-001",
            status: "kept",
            keptReason: "等待人工审查"
        )
        appState.worktrees = [staleWorktree]

        let api = FakeWorkbenchAPIProvider()
        let events = WorkbenchEventsDTO(
            events: [makeEvent(id: "evt-keep", type: "worktree.kept", subjectID: "wt-api-client")],
            limit: 50
        )
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [
            makeMission(id: "mission-kept", sessionID: "sess-001")
        ])
        await api.setKeepWorktreeWithSnapshotResult(.success(
            WorktreeSnapshotDTO(worktree: keptWorktree, snapshot: snapshot)
        ))
        await api.setWorktreesResult(.success(WorktreesDTO(worktrees: [keptWorktree], taskID: nil, status: nil, limit: 50)))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.keepWorktree(
            name: "wt-api-client",
            actor: "Reviewer-Agent",
            reason: "等待人工审查"
        )

        #expect(appState.worktrees == [keptWorktree])
        #expect(appState.timelineEvents == events.events)
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
        #expect(await api.keepWorktreeWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func keepWorktreeWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let staleWorktree = makeWorktree(name: "wt-stale", taskID: "task-old", status: "active")
        appState.worktrees = [staleWorktree]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.keepWorktree(
            name: "wt-stale",
            actor: "Reviewer-Agent",
            reason: "等待人工审查"
        )

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.worktrees == [staleWorktree])
    }

    @Test @MainActor func keepWorktreeFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleWorktree = makeWorktree(name: "wt-stale", taskID: "task-001", status: "active")
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [
            makeMission(id: "mission-stale", sessionID: "sess-001")
        ])
        appState.worktrees = [staleWorktree]
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setKeepWorktreeWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.keepWorktree(
            name: "wt-stale",
            actor: "Reviewer-Agent",
            reason: "等待人工审查"
        )

        #expect(appState.worktrees == [staleWorktree])
        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func removeWorktreeSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let removedWorktree = makeWorktree(name: "wt-remove", taskID: "task-001", status: "clean")
        appState.snapshot = makeSnapshot(sessionID: "sess-001", missions: [
            makeMission(id: "mission-stale", sessionID: "sess-001")
        ])
        appState.worktrees = [removedWorktree]

        let api = FakeWorkbenchAPIProvider()
        let events = WorkbenchEventsDTO(
            events: [makeEvent(id: "evt-remove", type: "worktree.removed", subjectID: "wt-remove")],
            limit: 50
        )
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [
            makeMission(id: "mission-removed", sessionID: "sess-001")
        ])
        await api.setRemoveWorktreeWithSnapshotResult(.success(
            WorktreeRemovalSnapshotDTO(
                removal: WorktreeRemovalDTO(
                    name: "wt-remove",
                    discardChanges: false,
                    message: "已删除 worktree：wt-remove"
                ),
                snapshot: snapshot
            )
        ))
        await api.setWorktreesResult(.success(WorktreesDTO(worktrees: [], taskID: nil, status: nil, limit: 50)))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.removeWorktree(name: "wt-remove", discardChanges: false)

        #expect(appState.worktrees.isEmpty)
        #expect(appState.timelineEvents == events.events)
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
        #expect(await api.removeWorktreeWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func removeWorktreeWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let staleWorktree = makeWorktree(name: "wt-stale", taskID: "task-old", status: "active")
        appState.worktrees = [staleWorktree]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.removeWorktree(name: "wt-stale", discardChanges: false)

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.worktrees == [staleWorktree])
    }

    @Test @MainActor func removeWorktreeFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleWorktree = makeWorktree(name: "wt-stale", taskID: "task-001", status: "dirty")
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [
            makeMission(id: "mission-stale", sessionID: "sess-001")
        ])
        appState.worktrees = [staleWorktree]
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setRemoveWorktreeWithSnapshotResult(.failure(.httpStatus(409)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.removeWorktree(name: "wt-stale", discardChanges: false)

        #expect(appState.worktrees == [staleWorktree])
        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(409))
    }

    @Test @MainActor func runValidationSuccessUsesIncludedSnapshotWithoutExtraSnapshotFetch() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(
            sessionID: "sess-001",
            missions: [makeMission(id: "mission-stale", sessionID: "sess-001")]
        )
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        let freshSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let result = ValidationResultSnapshotDTO(
            validationRun: ValidationResultDTO(
                id: "run-001",
                status: "passed",
                exitCode: 0,
                output: "ok"
            ),
            snapshot: freshSnapshot
        )
        let run = ValidationRunDTO(
            id: "run-001",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "Human",
            command: ["pytest"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "ok",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )
        let runs = ValidationRunsDTO(validationRuns: [run], taskID: "task-001", limit: 50)
        let failure = makeFailure(id: "failure-001", taskID: "task-001", status: "open")
        let failures = FailuresDTO(failures: [failure], taskID: "task-001", status: nil, limit: 50)
        let event = makeEvent(id: "evt-001", type: "validation.ran", subjectID: "run-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setRunValidationWithSnapshotResult(.success(result))
        await api.setValidationRunsResult(.success(runs))
        await api.setFailuresResult(.success(failures))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.validationRuns == [run])
        #expect(appState.failures == [failure])
        #expect(appState.snapshot == freshSnapshot)
        #expect(appState.snapshot != staleSnapshot)
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.runValidationWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func runValidationSuccessRefreshesValidationRunsFailuresSnapshotAndEvents() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let result = ValidationResultDTO(
            id: "run-001",
            status: "passed",
            exitCode: 0,
            output: "ok"
        )
        let run = ValidationRunDTO(
            id: "run-001",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "Human",
            command: ["pytest"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "ok",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )
        let runs = ValidationRunsDTO(validationRuns: [run], taskID: "task-001", limit: 50)
        let failure = makeFailure(id: "failure-001", taskID: "task-001", status: "open")
        let failures = FailuresDTO(failures: [failure], taskID: "task-001", status: nil, limit: 50)
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let event = makeEvent(id: "evt-001", type: "validation.ran", subjectID: "run-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setRunValidationWithSnapshotResult(.success(
            ValidationResultSnapshotDTO(validationRun: result, snapshot: snapshot)
        ))
        await api.setValidationRunsResult(.success(runs))
        await api.setFailuresResult(.success(failures))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.validationRuns == [run])
        #expect(appState.failures == [failure])
        #expect(appState.snapshot == snapshot)
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.runValidationWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func runValidationWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.validationRuns = [
            ValidationRunDTO(
                id: "run-stale",
                sessionID: "old-session",
                taskID: "task-old",
                actor: "ValidationRunner",
                command: [],
                cwd: "",
                status: "passed",
                exitCode: 0,
                output: "",
                startedAt: "2026-06-27T05:00:00",
                completedAt: "2026-06-27T05:00:01"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.validationRuns.count == 1)
    }

    @Test @MainActor func runValidationFailurePreservesOldData() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleRun = ValidationRunDTO(
            id: "run-stale",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "ValidationRunner",
            command: [],
            cwd: "",
            status: "passed",
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T05:00:00",
            completedAt: "2026-06-27T05:00:01"
        )
        appState.validationRuns = [staleRun]
        let staleFailure = makeFailure(id: "failure-stale", taskID: "task-001", status: "open")
        appState.failures = [staleFailure]
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setRunValidationWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.validationRuns == [staleRun])
        #expect(appState.failures == [staleFailure])
        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
        #expect(await api.runValidationWithSnapshotCallCount == 1)
    }

    @Test @MainActor func runValidationBlockedWhenCapabilitiesLackValidationRunner() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        appState.capabilities = CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: false,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1
        )

        let staleRun = ValidationRunDTO(
            id: "run-stale",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "ValidationRunner",
            command: [],
            cwd: "",
            status: "passed",
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T05:00:00",
            completedAt: "2026-06-27T05:00:01"
        )
        let staleFailure = makeFailure(id: "failure-stale", taskID: "task-001", status: "open")
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let staleEvent = makeEvent(id: "evt-stale", type: "validation.ran", subjectID: "run-stale")
        appState.validationRuns = [staleRun]
        appState.failures = [staleFailure]
        appState.snapshot = staleSnapshot
        appState.timelineEvents = [staleEvent]

        let api = FakeWorkbenchAPIProvider()
        await api.setRunValidationWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.lastError == .capabilityUnavailable("validation_runner"))
        #expect(appState.validationRuns == [staleRun])
        #expect(appState.failures == [staleFailure])
        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.timelineEvents == [staleEvent])
        #expect(await api.runValidationCallCount == 0)
        #expect(await api.runValidationWithSnapshotCallCount == 0)
    }

    @Test @MainActor func runValidationWithNilCapabilitiesAllowsSuccess() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        #expect(appState.capabilities == nil)

        let api = FakeWorkbenchAPIProvider()
        let result = ValidationResultDTO(
            id: "run-001",
            status: "passed",
            exitCode: 0,
            output: "ok"
        )
        let run = ValidationRunDTO(
            id: "run-001",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "Human",
            command: ["pytest"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "ok",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )
        let runs = ValidationRunsDTO(validationRuns: [run], taskID: "task-001", limit: 50)
        let failure = makeFailure(id: "failure-001", taskID: "task-001", status: "open")
        let failures = FailuresDTO(failures: [failure], taskID: "task-001", status: nil, limit: 50)
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let event = makeEvent(id: "evt-001", type: "validation.ran", subjectID: "run-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setRunValidationWithSnapshotResult(.success(
            ValidationResultSnapshotDTO(validationRun: result, snapshot: snapshot)
        ))
        await api.setValidationRunsResult(.success(runs))
        await api.setFailuresResult(.success(failures))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.validationRuns == [run])
        #expect(appState.failures == [failure])
        #expect(appState.snapshot == snapshot)
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.runValidationWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func createIntentLockSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lock = makeIntentLock(id: "lock-001", missionID: "mission-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let event = makeEvent(id: "evt-001", type: "intent_lock.created", subjectID: "lock-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)
        let locks = IntentLocksDTO(intentLocks: [lock], missionID: "mission-001")

        await api.setCreateIntentLockWithSnapshotResult(.success(
            IntentLockSnapshotDTO(intentLock: lock, snapshot: snapshot)
        ))
        await api.setFetchIntentLocksResult(.success(locks))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createIntentLock(
            missionID: "mission-001",
            actor: "Planner-Agent",
            rule: "禁止修改 core 模块",
            blockedPaths: ["src/core"],
            allowedPaths: ["src/core/README.md"],
            requireProposalForRisk: "high"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.intentLocks == [lock])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.createIntentLockWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func createIntentLockWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createIntentLock(
            missionID: "mission-001",
            actor: "Human",
            rule: "禁止修改 core 模块",
            blockedPaths: [],
            allowedPaths: [],
            requireProposalForRisk: "high"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func createIntentLockFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setCreateIntentLockWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createIntentLock(
            missionID: "mission-001",
            actor: "Human",
            rule: "禁止修改 core 模块",
            blockedPaths: [],
            allowedPaths: [],
            requireProposalForRisk: "high"
        )

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshIntentLocksSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lock = makeIntentLock(id: "lock-001", missionID: "mission-001")
        await api.setFetchIntentLocksResult(.success(
            IntentLocksDTO(intentLocks: [lock], missionID: "mission-001")
        ))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshIntentLocks(missionID: "mission-001")

        #expect(appState.intentLocks == [lock])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshIntentLocksWithoutSelectedSessionClearsListAndRecordsError() async throws {
        let appState = AppState()
        let staleLock = makeIntentLock(id: "lock-stale", missionID: "mission-old")
        appState.intentLocks = [staleLock]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshIntentLocks(missionID: "mission-001")

        #expect(appState.intentLocks.isEmpty)
        #expect(appState.lastError == .missingSelectedSession)
    }

    @Test @MainActor func refreshIntentLocksFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleLock = makeIntentLock(id: "lock-stale", missionID: "mission-001")
        appState.intentLocks = [staleLock]

        let api = FakeWorkbenchAPIProvider()
        await api.setFetchIntentLocksResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshIntentLocks(missionID: "mission-001")

        #expect(appState.intentLocks == [staleLock])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func createDecisionSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let decision = makeDecision(id: "decision-001", missionID: "mission-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let event = makeEvent(id: "evt-001", type: "decision.created", subjectID: "decision-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)
        let decisions = DecisionsDTO(decisions: [decision], missionID: "mission-001")

        await api.setCreateDecisionWithSnapshotResult(.success(
            DecisionSnapshotDTO(decision: decision, snapshot: snapshot)
        ))
        await api.setFetchDecisionsResult(.success(decisions))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createDecision(
            missionID: "mission-001",
            actor: "Planner-Agent",
            kind: "architecture",
            title: "采用 FastAPI",
            content: "使用 FastAPI 承载 Workbench API"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.decisions == [decision])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.createDecisionWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func createDecisionWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createDecision(
            missionID: "mission-001",
            actor: "Human",
            kind: "architecture",
            title: "采用 FastAPI",
            content: "使用 FastAPI 承载 Workbench API"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func createDecisionFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setCreateDecisionWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createDecision(
            missionID: "mission-001",
            actor: "Human",
            kind: "architecture",
            title: "采用 FastAPI",
            content: "使用 FastAPI 承载 Workbench API"
        )

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshDecisionsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let decision = makeDecision(id: "decision-001", missionID: "mission-001")
        await api.setFetchDecisionsResult(.success(
            DecisionsDTO(decisions: [decision], missionID: "mission-001")
        ))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshDecisions(missionID: "mission-001")

        #expect(appState.decisions == [decision])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshDecisionsWithoutSelectedSessionClearsListAndRecordsError() async throws {
        let appState = AppState()
        let staleDecision = makeDecision(id: "decision-stale", missionID: "mission-old")
        appState.decisions = [staleDecision]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshDecisions(missionID: "mission-001")

        #expect(appState.decisions.isEmpty)
        #expect(appState.lastError == .missingSelectedSession)
    }

    @Test @MainActor func refreshDecisionsFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleDecision = makeDecision(id: "decision-stale", missionID: "mission-001")
        appState.decisions = [staleDecision]

        let api = FakeWorkbenchAPIProvider()
        await api.setFetchDecisionsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshDecisions(missionID: "mission-001")

        #expect(appState.decisions == [staleDecision])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func loadDecisionSuccessStoresSelectedDecision() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let decision = makeDecision(id: "decision-001", missionID: "mission-001")
        await api.setFetchDecisionResult(.success(decision))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadDecision(missionID: "mission-001", decisionID: "decision-001")

        #expect(appState.selectedDecision == decision)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadDecisionWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldDecision = makeDecision(id: "decision-old", missionID: "mission-001")
        appState.selectedDecision = oldDecision

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadDecision(missionID: "mission-001", decisionID: "decision-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedDecision == oldDecision)
    }

    @Test @MainActor func loadDecisionFailurePreservesOldSelectedDecision() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldDecision = makeDecision(id: "decision-old", missionID: "mission-001")
        appState.selectedDecision = oldDecision

        let api = FakeWorkbenchAPIProvider()
        await api.setFetchDecisionResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadDecision(missionID: "mission-001", decisionID: "decision-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedDecision == oldDecision)
    }

    @Test @MainActor func loadIntentLockSuccessStoresSelectedIntentLock() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lock = makeIntentLock(id: "lock-001", missionID: "mission-001")
        await api.setFetchIntentLockResult(.success(lock))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadIntentLock(missionID: "mission-001", lockID: "lock-001")

        #expect(appState.selectedIntentLock == lock)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadIntentLockWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldLock = makeIntentLock(id: "lock-old", missionID: "mission-001")
        appState.selectedIntentLock = oldLock

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadIntentLock(missionID: "mission-001", lockID: "lock-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedIntentLock == oldLock)
    }

    @Test @MainActor func loadIntentLockFailurePreservesOldSelectedIntentLock() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldLock = makeIntentLock(id: "lock-old", missionID: "mission-001")
        appState.selectedIntentLock = oldLock

        let api = FakeWorkbenchAPIProvider()
        await api.setFetchIntentLockResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadIntentLock(missionID: "mission-001", lockID: "lock-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedIntentLock == oldLock)
    }

    @Test @MainActor func loadApprovalSuccessStoresSelectedApproval() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let approval = makeApproval(id: "approval-001", missionID: "mission-001", state: "waiting")
        await api.setApprovalResult(.success(approval))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadApproval(approvalID: "approval-001")

        #expect(appState.selectedApproval == approval)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadApprovalWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldApproval = makeApproval(id: "approval-old", missionID: "mission-001", state: "waiting")
        appState.selectedApproval = oldApproval

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadApproval(approvalID: "approval-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedApproval == oldApproval)
    }

    @Test @MainActor func loadApprovalFailurePreservesOldSelectedApproval() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldApproval = makeApproval(id: "approval-old", missionID: "mission-001", state: "waiting")
        appState.selectedApproval = oldApproval

        let api = FakeWorkbenchAPIProvider()
        await api.setApprovalResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadApproval(approvalID: "approval-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedApproval == oldApproval)
    }

    @Test @MainActor func loadEventSuccessStoresSelectedEvent() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let event = makeEvent(id: "event-001", type: "issue.claimed", subjectID: "task-001")
        await api.setEventResult(.success(event))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadEvent(eventID: "event-001")

        #expect(appState.selectedEvent == event)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadEventWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldEvent = makeEvent(id: "event-old", type: "mission.created", subjectID: "mission-001")
        appState.selectedEvent = oldEvent

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadEvent(eventID: "event-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedEvent == oldEvent)
    }

    @Test @MainActor func loadEventFailurePreservesOldSelectedEvent() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldEvent = makeEvent(id: "event-old", type: "mission.created", subjectID: "mission-001")
        appState.selectedEvent = oldEvent

        let api = FakeWorkbenchAPIProvider()
        await api.setEventResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadEvent(eventID: "event-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedEvent == oldEvent)
    }

    @Test @MainActor func loadValidationRunSuccessStoresSelectedValidationRun() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let run = makeValidationRun(id: "run-001", taskID: "task-001", status: "failed")
        await api.setValidationRunResult(.success(run))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadValidationRun(runID: "run-001")

        #expect(appState.selectedValidationRun == run)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadValidationRunWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldRun = makeValidationRun(id: "run-old", taskID: "task-001", status: "passed")
        appState.selectedValidationRun = oldRun

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadValidationRun(runID: "run-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedValidationRun == oldRun)
    }

    @Test @MainActor func loadValidationRunFailurePreservesOldSelectedValidationRun() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldRun = makeValidationRun(id: "run-old", taskID: "task-001", status: "passed")
        appState.selectedValidationRun = oldRun

        let api = FakeWorkbenchAPIProvider()
        await api.setValidationRunResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadValidationRun(runID: "run-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedValidationRun == oldRun)
    }

    @Test @MainActor func loadContextSnapshotSuccessStoresSelectedContextSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let snapshot = makeContextSnapshot(id: "context-001", taskID: "task-001", health: "stale")
        await api.setContextSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadContextSnapshot(snapshotID: "context-001")

        #expect(appState.selectedContextSnapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadContextSnapshotWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldSnapshot = makeContextSnapshot(id: "context-old", taskID: "task-001", health: "good")
        appState.selectedContextSnapshot = oldSnapshot

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadContextSnapshot(snapshotID: "context-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedContextSnapshot == oldSnapshot)
    }

    @Test @MainActor func loadContextSnapshotFailurePreservesOldSelectedContextSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldSnapshot = makeContextSnapshot(id: "context-old", taskID: "task-001", health: "good")
        appState.selectedContextSnapshot = oldSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setContextSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadContextSnapshot(snapshotID: "context-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedContextSnapshot == oldSnapshot)
    }

    @Test @MainActor func loadFailureSuccessStoresSelectedFailure() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let failure = makeFailure(id: "failure-001", taskID: "task-001", status: "open")
        await api.setFailureResult(.success(failure))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadFailure(failureID: "failure-001")

        #expect(appState.selectedFailure == failure)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadFailureWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldFailure = makeFailure(id: "failure-old", taskID: "task-001", status: "open")
        appState.selectedFailure = oldFailure

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadFailure(failureID: "failure-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedFailure == oldFailure)
    }

    @Test @MainActor func loadFailureFailurePreservesOldSelectedFailure() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldFailure = makeFailure(id: "failure-old", taskID: "task-001", status: "open")
        appState.selectedFailure = oldFailure

        let api = FakeWorkbenchAPIProvider()
        await api.setFailureResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadFailure(failureID: "failure-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedFailure == oldFailure)
    }

    @Test @MainActor func loadIssueSuccessStoresSelectedIssue() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let issue = makeIssue(taskID: "task-001", missionID: "mission-001")
        await api.setIssueResult(.success(issue))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadIssue(taskID: "task-001")

        #expect(appState.selectedIssue == issue)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadIssueWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldIssue = makeIssue(taskID: "task-old", missionID: "mission-001")
        appState.selectedIssue = oldIssue

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadIssue(taskID: "task-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedIssue == oldIssue)
    }

    @Test @MainActor func loadIssueFailurePreservesOldSelectedIssue() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldIssue = makeIssue(taskID: "task-old", missionID: "mission-001")
        appState.selectedIssue = oldIssue

        let api = FakeWorkbenchAPIProvider()
        await api.setIssueResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadIssue(taskID: "task-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedIssue == oldIssue)
    }

    @Test @MainActor func loadLeaseSuccessStoresSelectedLease() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lease = makeLease(id: "lease-001", taskID: "task-001", state: "active")
        await api.setLeaseResult(.success(lease))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadLease(leaseID: "lease-001")

        #expect(appState.selectedLease == lease)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadLeaseWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldLease = makeLease(id: "lease-old", taskID: "task-001", state: "active")
        appState.selectedLease = oldLease

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadLease(leaseID: "lease-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedLease == oldLease)
    }

    @Test @MainActor func loadLeaseFailurePreservesOldSelectedLease() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldLease = makeLease(id: "lease-old", taskID: "task-001", state: "active")
        appState.selectedLease = oldLease

        let api = FakeWorkbenchAPIProvider()
        await api.setLeaseResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadLease(leaseID: "lease-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedLease == oldLease)
    }

    @Test @MainActor func loadWorktreeSuccessStoresSelectedWorktree() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let worktree = makeWorktree(name: "wt-api-client", taskID: "task-001", status: "active")
        await api.setWorktreeResult(.success(worktree))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadWorktree(name: "wt-api-client")

        #expect(appState.selectedWorktree == worktree)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadWorktreeWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldWorktree = makeWorktree(name: "wt-old", taskID: "task-001", status: "active")
        appState.selectedWorktree = oldWorktree

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadWorktree(name: "wt-api-client")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedWorktree == oldWorktree)
    }

    @Test @MainActor func loadWorktreeFailurePreservesOldSelectedWorktree() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldWorktree = makeWorktree(name: "wt-old", taskID: "task-001", status: "active")
        appState.selectedWorktree = oldWorktree

        let api = FakeWorkbenchAPIProvider()
        await api.setWorktreeResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadWorktree(name: "wt-api-client")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedWorktree == oldWorktree)
    }

    @Test @MainActor func loadMissionSuccessStoresSelectedMission() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let mission = makeMission(id: "mission-001", sessionID: "sess-001")
        await api.setMissionResult(.success(mission))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadMission(missionID: "mission-001")

        #expect(appState.selectedMission == mission)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadMissionWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldMission = makeMission(id: "mission-old", sessionID: "sess-001")
        appState.selectedMission = oldMission

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadMission(missionID: "mission-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedMission == oldMission)
    }

    @Test @MainActor func loadMissionFailurePreservesOldSelectedMission() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldMission = makeMission(id: "mission-old", sessionID: "sess-001")
        appState.selectedMission = oldMission

        let api = FakeWorkbenchAPIProvider()
        await api.setMissionResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadMission(missionID: "mission-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedMission == oldMission)
    }

    @Test @MainActor func loadAgentProfileSuccessStoresSelectedAgentProfile() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let profile = makeAgentProfile(id: "agent-001", sessionID: "sess-001", status: "busy")
        await api.setAgentProfileResult(.success(profile))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadAgentProfile(agentID: "agent-001")

        #expect(appState.selectedAgentProfile == profile)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func loadAgentProfileWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        let oldProfile = makeAgentProfile(id: "agent-old", sessionID: "sess-001", status: "idle")
        appState.selectedAgentProfile = oldProfile

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadAgentProfile(agentID: "agent-001")

        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.selectedAgentProfile == oldProfile)
    }

    @Test @MainActor func loadAgentProfileFailurePreservesOldSelectedAgentProfile() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let oldProfile = makeAgentProfile(id: "agent-old", sessionID: "sess-001", status: "idle")
        appState.selectedAgentProfile = oldProfile

        let api = FakeWorkbenchAPIProvider()
        await api.setAgentProfileResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.loadAgentProfile(agentID: "agent-001")

        #expect(appState.lastError == .httpStatus(500))
        #expect(appState.selectedAgentProfile == oldProfile)
    }

    @Test @MainActor func resolveApprovalSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let resolvedApproval = makeApproval(id: "approval-001", missionID: "mission-001", state: "approved")
        let waitingApproval = makeApproval(id: "approval-002", missionID: "mission-001", state: "waiting")
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        let approvals = ApprovalsDTO(approvals: [waitingApproval], state: "waiting", limit: 50)
        let event = makeEvent(id: "evt-001", type: "approval.resolved", subjectID: "approval-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setResolveApprovalWithSnapshotResult(.success(
            ApprovalSnapshotDTO(approval: resolvedApproval, snapshot: snapshot)
        ))
        await api.setApprovalsResult(.success(approvals))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.resolveApproval(
            approvalID: "approval-001",
            actor: "Human",
            state: "approved",
            decisionNote: "同意"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.approvals == [waitingApproval])
        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
        #expect(await api.resolveApprovalWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func resolveApprovalWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.resolveApproval(
            approvalID: "approval-001",
            actor: "Human",
            state: "rejected",
            decisionNote: ""
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func resolveApprovalFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot
        let staleApproval = makeApproval(id: "approval-stale", missionID: "mission-001", state: "waiting")
        appState.approvals = [staleApproval]

        let api = FakeWorkbenchAPIProvider()
        await api.setResolveApprovalWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.resolveApproval(
            approvalID: "approval-001",
            actor: "Human",
            state: "approved",
            decisionNote: "同意"
        )

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.approvals == [staleApproval])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func registerAgentProfileSuccessUsesIncludedSnapshotAndRefreshesLists() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        appState.snapshot = makeSnapshot(sessionID: "sess-001", missions: [
            makeMission(id: "mission-stale", sessionID: "sess-001")
        ])

        let api = FakeWorkbenchAPIProvider()
        let profile = makeAgentProfile(id: "agent-001", sessionID: "sess-001", status: "idle")
        let profiles = AgentProfilesDTO(agentProfiles: [profile], status: nil, limit: 50)
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-001",
            missions: [],
            agentProfiles: [profile],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )
        let event = makeEvent(id: "evt-001", type: "agent.registered", subjectID: "agent-001")
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setRegisterAgentProfileWithSnapshotResult(.success(
            AgentProfileSnapshotDTO(agentProfile: profile, snapshot: snapshot)
        ))
        await api.setAgentProfilesResult(.success(profiles))
        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.registerAgentProfile(
            agentID: "agent-001",
            name: "后端智能体",
            role: "coder",
            capabilities: ["api", "swift-client"],
            permissions: ["read", "write"],
            maxParallelTasks: 2,
            status: "busy",
            actor: "Human"
        )

        #expect(appState.agentProfiles == [profile])
        #expect(appState.timelineEvents == [event])
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
        #expect(await api.registerAgentProfileWithSnapshotCallCount == 1)
        #expect(await api.snapshotCallCount == 0)
    }

    @Test @MainActor func registerAgentProfileWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.agentProfiles = [
            makeAgentProfile(id: "agent-stale", sessionID: "old-session", status: "idle")
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.registerAgentProfile(
            agentID: "agent-001",
            name: "后端智能体",
            role: "coder",
            capabilities: ["api"],
            permissions: ["read"],
            maxParallelTasks: 1,
            status: "idle",
            actor: "Human"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.agentProfiles.isEmpty)
    }

    @Test @MainActor func registerAgentProfileFailurePreservesAgentProfilesAndSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleProfile = makeAgentProfile(id: "agent-stale", sessionID: "sess-001", status: "idle")
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.agentProfiles = [staleProfile]
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setRegisterAgentProfileWithSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.registerAgentProfile(
            agentID: "agent-001",
            name: "后端智能体",
            role: "coder",
            capabilities: ["api"],
            permissions: ["read"],
            maxParallelTasks: 1,
            status: "idle",
            actor: "Human"
        )

        #expect(appState.agentProfiles == [staleProfile])
        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshAgentProfilesSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let profile = makeAgentProfile(id: "agent-001", sessionID: "sess-001", status: "idle")
        let profiles = AgentProfilesDTO(agentProfiles: [profile], status: "idle", limit: 25)

        await api.setAgentProfilesResult(.success(profiles))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshAgentProfiles(status: "idle", limit: 25)

        #expect(appState.agentProfiles == [profile])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshAgentProfilesWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.agentProfiles = [
            makeAgentProfile(id: "agent-stale", sessionID: "old-session", status: "idle")
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshAgentProfiles(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.agentProfiles.isEmpty)
    }

    @Test @MainActor func refreshAgentProfilesFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleProfile = makeAgentProfile(id: "agent-stale", sessionID: "sess-001", status: "idle")
        appState.agentProfiles = [staleProfile]

        let api = FakeWorkbenchAPIProvider()
        await api.setAgentProfilesResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshAgentProfiles(limit: 50)

        #expect(appState.agentProfiles == [staleProfile])
        #expect(appState.lastError == .httpStatus(500))
    }
}

extension FakeWorkbenchAPIProvider {
    fileprivate func setBootstrapResult(_ result: Result<WorkbenchBootstrapDTO, APIError>) {
        bootstrapResult = result
    }

    fileprivate func setStatusResult(_ result: Result<DaemonStatusDTO, APIError>) {
        statusResult = result
    }

    fileprivate func setCapabilitiesResult(_ result: Result<CapabilitiesDTO, APIError>) {
        capabilitiesResult = result
    }

    fileprivate func setSnapshotResult(_ result: Result<WorkbenchSnapshotDTO, APIError>) {
        snapshotResult = result
    }

    fileprivate func setSessionsResult(_ result: Result<SessionListDTO, APIError>) {
        sessionsResult = result
    }

    fileprivate func setCreateSessionResult(_ result: Result<SessionDTO, APIError>) {
        createSessionResult = result
    }

    fileprivate func setCreateWorkbenchSessionResult(_ result: Result<WorkbenchBootstrapDTO, APIError>) {
        createWorkbenchSessionResult = result
    }

    fileprivate func setEventsResult(_ result: Result<WorkbenchEventsDTO, APIError>) {
        eventsResult = result
    }

    fileprivate func setEventResult(_ result: Result<EventDTO, APIError>) {
        eventResult = result
    }

    fileprivate func setValidationRunsResult(_ result: Result<ValidationRunsDTO, APIError>) {
        validationRunsResult = result
    }

    fileprivate func setValidationRunResult(_ result: Result<ValidationRunDTO, APIError>) {
        validationRunResult = result
    }

    fileprivate func setContextSnapshotsResult(_ result: Result<ContextSnapshotsDTO, APIError>) {
        contextSnapshotsResult = result
    }

    fileprivate func setContextSnapshotResult(_ result: Result<ContextSnapshotDTO, APIError>) {
        contextSnapshotResult = result
    }

    fileprivate func setRecordContextHealthResult(_ result: Result<ContextSnapshotDTO, APIError>) {
        recordContextHealthResult = result
    }

    fileprivate func setRecordContextHealthWithSnapshotResult(_ result: Result<ContextHealthSnapshotDTO, APIError>) {
        recordContextHealthWithSnapshotResult = result
    }

    fileprivate func setApprovalsResult(_ result: Result<ApprovalsDTO, APIError>) {
        approvalsResult = result
    }

    fileprivate func setApprovalResult(_ result: Result<ApprovalDTO, APIError>) {
        approvalResult = result
    }

    fileprivate func setFailuresResult(_ result: Result<FailuresDTO, APIError>) {
        failuresResult = result
    }

    fileprivate func setFailureResult(_ result: Result<FailureDTO, APIError>) {
        failureResult = result
    }

    fileprivate func setIssuesResult(_ result: Result<IssuesDTO, APIError>) {
        issuesResult = result
    }

    fileprivate func setIssueResult(_ result: Result<IssueDTO, APIError>) {
        issueResult = result
    }

    fileprivate func setLeasesResult(_ result: Result<LeasesDTO, APIError>) {
        leasesResult = result
    }

    fileprivate func setWorktreesResult(_ result: Result<WorktreesDTO, APIError>) {
        worktreesResult = result
    }

    fileprivate func setWorktreeResult(_ result: Result<WorktreeDTO, APIError>) {
        worktreeResult = result
    }

    fileprivate func setKeepWorktreeResult(_ result: Result<WorktreeDTO, APIError>) {
        keepWorktreeResult = result
    }

    fileprivate func setKeepWorktreeWithSnapshotResult(_ result: Result<WorktreeSnapshotDTO, APIError>) {
        keepWorktreeWithSnapshotResult = result
    }

    fileprivate func setRemoveWorktreeResult(_ result: Result<WorktreeRemovalDTO, APIError>) {
        removeWorktreeResult = result
    }

    fileprivate func setRemoveWorktreeWithSnapshotResult(_ result: Result<WorktreeRemovalSnapshotDTO, APIError>) {
        removeWorktreeWithSnapshotResult = result
    }

    fileprivate func setMissionsResult(_ result: Result<MissionsDTO, APIError>) {
        missionsResult = result
    }

    fileprivate func setMissionResult(_ result: Result<MissionDTO, APIError>) {
        missionResult = result
    }

    fileprivate func setCreateMissionWithSnapshotResult(_ result: Result<MissionSnapshotDTO, APIError>) {
        createMissionWithSnapshotResult = result
    }

    fileprivate func setAttachIssueWithSnapshotResult(_ result: Result<IssueSnapshotDTO, APIError>) {
        attachIssueWithSnapshotResult = result
    }

    fileprivate func setAgentProfilesResult(_ result: Result<AgentProfilesDTO, APIError>) {
        agentProfilesResult = result
    }

    fileprivate func setAgentProfileResult(_ result: Result<AgentProfileDTO, APIError>) {
        agentProfileResult = result
    }

    fileprivate func setRegisterAgentProfileResult(_ result: Result<AgentProfileDTO, APIError>) {
        registerAgentProfileResult = result
    }

    fileprivate func setRegisterAgentProfileWithSnapshotResult(_ result: Result<AgentProfileSnapshotDTO, APIError>) {
        registerAgentProfileWithSnapshotResult = result
    }

    fileprivate func setClaimIssueResult(_ result: Result<LeaseDTO, APIError>) {
        claimIssueResult = result
    }

    fileprivate func setClaimIssueWithSnapshotResult(_ result: Result<LeaseSnapshotDTO, APIError>) {
        claimIssueWithSnapshotResult = result
    }

    fileprivate func setLeaseResult(_ result: Result<LeaseDTO, APIError>) {
        leaseResult = result
    }

    fileprivate func setReleaseLeaseResult(_ result: Result<LeaseDTO, APIError>) {
        releaseLeaseResult = result
    }

    fileprivate func setReleaseLeaseWithSnapshotResult(_ result: Result<LeaseSnapshotDTO, APIError>) {
        releaseLeaseWithSnapshotResult = result
    }

    fileprivate func setExpireLeasesResult(_ result: Result<ExpiredLeasesDTO, APIError>) {
        expireLeasesResult = result
    }

    fileprivate func setExpireLeasesWithSnapshotResult(_ result: Result<ExpiredLeasesSnapshotDTO, APIError>) {
        expireLeasesWithSnapshotResult = result
    }

    fileprivate func setCreateMissionResult(_ result: Result<MissionDTO, APIError>) {
        createMissionResult = result
    }

    fileprivate func setAttachIssueResult(_ result: Result<IssueDTO, APIError>) {
        attachIssueResult = result
    }

    fileprivate func setCreateIssueResult(_ result: Result<IssueDTO, APIError>) {
        createIssueResult = result
    }

    fileprivate func setCreateIssueWithSnapshotResult(_ result: Result<IssueSnapshotDTO, APIError>) {
        createIssueWithSnapshotResult = result
    }

    fileprivate func setCreateIntentLockResult(_ result: Result<IntentLockDTO, APIError>) {
        createIntentLockResult = result
    }

    fileprivate func setCreateIntentLockWithSnapshotResult(_ result: Result<IntentLockSnapshotDTO, APIError>) {
        createIntentLockWithSnapshotResult = result
    }

    fileprivate func setFetchIntentLocksResult(_ result: Result<IntentLocksDTO, APIError>) {
        fetchIntentLocksResult = result
    }

    fileprivate func setFetchIntentLockResult(_ result: Result<IntentLockDTO, APIError>) {
        fetchIntentLockResult = result
    }

    fileprivate func setCreateDecisionResult(_ result: Result<DecisionDTO, APIError>) {
        createDecisionResult = result
    }

    fileprivate func setCreateDecisionWithSnapshotResult(_ result: Result<DecisionSnapshotDTO, APIError>) {
        createDecisionWithSnapshotResult = result
    }

    fileprivate func setFetchDecisionsResult(_ result: Result<DecisionsDTO, APIError>) {
        fetchDecisionsResult = result
    }

    fileprivate func setFetchDecisionResult(_ result: Result<DecisionDTO, APIError>) {
        fetchDecisionResult = result
    }

    fileprivate func setResolveApprovalResult(_ result: Result<ApprovalDTO, APIError>) {
        resolveApprovalResult = result
    }

    fileprivate func setResolveApprovalWithSnapshotResult(_ result: Result<ApprovalSnapshotDTO, APIError>) {
        resolveApprovalWithSnapshotResult = result
    }

    fileprivate func setRunValidationResult(_ result: Result<ValidationResultDTO, APIError>) {
        runValidationResult = result
    }

    fileprivate func setRunValidationWithSnapshotResult(_ result: Result<ValidationResultSnapshotDTO, APIError>) {
        runValidationWithSnapshotResult = result
    }
}

private func configureWorkbenchListResults(for api: FakeWorkbenchAPIProvider, sessionID: String) async {
    let mission = makeMission(id: "mission-\(sessionID)", sessionID: sessionID)
    let issue = makeIssue(taskID: "task-\(sessionID)")
    let lease = makeLease(id: "lease-\(sessionID)", taskID: "task-\(sessionID)", state: "active")
    let worktree = makeWorktree(name: "wt-\(sessionID)", taskID: "task-\(sessionID)", status: "active")
    let failure = makeFailure(id: "failure-\(sessionID)", taskID: "task-\(sessionID)", status: "open")
    let event = makeEvent(id: "evt-\(sessionID)", type: "test.event", subjectID: "subject-\(sessionID)")
    let approval = makeApproval(id: "approval-\(sessionID)", missionID: "mission-\(sessionID)", state: "waiting")
    let decision = makeDecision(id: "decision-\(sessionID)", missionID: "mission-\(sessionID)")
    let intentLock = makeIntentLock(id: "lock-\(sessionID)", missionID: "mission-\(sessionID)")
    let run = ValidationRunDTO(
        id: "run-\(sessionID)",
        sessionID: sessionID,
        taskID: "task-\(sessionID)",
        actor: "ValidationRunner",
        command: ["pytest"],
        cwd: "/workspace",
        status: "passed",
        exitCode: 0,
        output: "ok",
        startedAt: "2026-06-27T06:00:00",
        completedAt: "2026-06-27T06:00:01"
    )
    let contextSnapshot = ContextSnapshotDTO(
        id: "ctx-\(sessionID)",
        sessionID: sessionID,
        agentID: "agent-\(sessionID)",
        taskID: "task-\(sessionID)",
        health: "good",
        reasons: ["上下文健康"],
        createdAt: "2026-06-27T06:00:00"
    )
    let agentProfile = makeAgentProfile(id: "agent-\(sessionID)", sessionID: sessionID, status: "idle")

    await api.setMissionsResult(.success(MissionsDTO(missions: [mission], status: nil, limit: 50)))
    await api.setIssuesResult(.success(IssuesDTO(issues: [issue], missionID: nil, riskLevel: nil, limit: 50)))
    await api.setLeasesResult(.success(LeasesDTO(leases: [lease], state: nil, taskID: nil, agentID: nil, limit: 50)))
    await api.setWorktreesResult(.success(WorktreesDTO(worktrees: [worktree], taskID: nil, status: nil, limit: 50)))
    await api.setFailuresResult(.success(FailuresDTO(failures: [failure], taskID: nil, status: nil, limit: 50)))
    await api.setEventsResult(.success(WorkbenchEventsDTO(events: [event], limit: 50)))
    await api.setApprovalsResult(.success(ApprovalsDTO(approvals: [approval], state: "waiting", limit: 50)))
    await api.setFetchDecisionsResult(.success(DecisionsDTO(decisions: [decision], missionID: mission.id)))
    await api.setFetchIntentLocksResult(.success(IntentLocksDTO(intentLocks: [intentLock], missionID: mission.id)))
    await api.setValidationRunsResult(.success(ValidationRunsDTO(validationRuns: [run], taskID: nil, limit: 50)))
    await api.setContextSnapshotsResult(.success(ContextSnapshotsDTO(contextSnapshots: [contextSnapshot], taskID: nil, agentID: nil, limit: 50)))
    await api.setAgentProfilesResult(.success(AgentProfilesDTO(agentProfiles: [agentProfile], status: nil, limit: 50)))
}

@MainActor
private func waitUntil(
    timeoutNanoseconds: UInt64 = 1_000_000_000,
    condition: @escaping @MainActor () async -> Bool,
    sourceLocation: SourceLocation = #_sourceLocation
) async {
    let deadline = ContinuousClock.now + .nanoseconds(Int64(timeoutNanoseconds))
    while ContinuousClock.now < deadline {
        if await condition() {
            return
        }
        try? await Task.sleep(nanoseconds: 10_000_000)
    }
    Issue.record("Timed out waiting for condition", sourceLocation: sourceLocation)
}

@MainActor
private func expectWorkbenchListsPopulated(
    _ appState: AppState,
    sourceLocation: SourceLocation = #_sourceLocation
) {
    #expect(appState.missions.count == 1, sourceLocation: sourceLocation)
    #expect(appState.issues.count == 1, sourceLocation: sourceLocation)
    #expect(appState.leases.count == 1, sourceLocation: sourceLocation)
    #expect(appState.worktrees.count == 1, sourceLocation: sourceLocation)
    #expect(appState.failures.count == 1, sourceLocation: sourceLocation)
    #expect(appState.timelineEvents.count == 1, sourceLocation: sourceLocation)
    #expect(appState.approvals.count == 1, sourceLocation: sourceLocation)
    #expect(appState.decisions.count == 1, sourceLocation: sourceLocation)
    #expect(appState.intentLocks.count == 1, sourceLocation: sourceLocation)
    #expect(appState.validationRuns.count == 1, sourceLocation: sourceLocation)
    #expect(appState.contextSnapshots.count == 1, sourceLocation: sourceLocation)
    #expect(appState.agentProfiles.count == 1, sourceLocation: sourceLocation)
}

@MainActor
private func expectWorkbenchListsEmpty(
    _ appState: AppState,
    sourceLocation: SourceLocation = #_sourceLocation
) {
    #expect(appState.missions.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.issues.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.leases.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.worktrees.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.failures.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.timelineEvents.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.approvals.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.decisions.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.intentLocks.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.validationRuns.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.contextSnapshots.isEmpty, sourceLocation: sourceLocation)
    #expect(appState.agentProfiles.isEmpty, sourceLocation: sourceLocation)
}

@MainActor
private func expectSelectedDetailsEmpty(
    _ appState: AppState,
    sourceLocation: SourceLocation = #_sourceLocation
) {
    #expect(appState.selectedEvent == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedValidationRun == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedContextSnapshot == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedApproval == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedFailure == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedIssue == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedLease == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedWorktree == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedMission == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedAgentProfile == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedDecision == nil, sourceLocation: sourceLocation)
    #expect(appState.selectedIntentLock == nil, sourceLocation: sourceLocation)
}

@MainActor
private func seedWorkbenchLists(_ appState: AppState) {
    appState.timelineEvents = [makeEvent(id: "evt-stale", type: "old.event", subjectID: "old")]
    appState.validationRuns = [makeValidationRun(id: "run-stale", taskID: "task-old", status: "passed")]
    appState.contextSnapshots = [makeContextSnapshot(id: "ctx-stale", taskID: "task-old", health: "good")]
    appState.approvals = [makeApproval(id: "approval-stale", missionID: "mission-old", state: "waiting")]
    appState.failures = [makeFailure(id: "failure-stale", taskID: "task-old", status: "open")]
    appState.issues = [makeIssue(taskID: "task-old", missionID: "mission-old")]
    appState.leases = [makeLease(id: "lease-stale", taskID: "task-old", state: "active")]
    appState.worktrees = [makeWorktree(name: "wt-stale", taskID: "task-old", status: "active")]
    appState.missions = [makeMission(id: "mission-stale", sessionID: "sess-stale")]
    appState.agentProfiles = [makeAgentProfile(id: "agent-stale", sessionID: "sess-stale", status: "idle")]
    appState.decisions = [makeDecision(id: "decision-stale", missionID: "mission-stale")]
    appState.intentLocks = [makeIntentLock(id: "lock-stale", missionID: "mission-stale")]
}

private func makeAgentProfile(id: String, sessionID: String, status: String) -> AgentProfileDTO {
    AgentProfileDTO(
        id: id,
        sessionID: sessionID,
        name: "智能体 \(id)",
        role: "coder",
        capabilities: ["api"],
        permissions: ["read"],
        maxParallelTasks: 2,
        status: status,
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeStatus() -> DaemonStatusDTO {
    DaemonStatusDTO(
        status: "running",
        version: "0.2.0",
        pid: 42,
        host: "127.0.0.1",
        port: 8765,
        startedAt: "2026-06-27T06:00:00",
        workspaceCount: 7
    )
}

private func makeCapabilities() -> CapabilitiesDTO {
    CapabilitiesDTO(
        supportsDaemonManagement: false,
        supportsWorkspaceRegistry: true,
        supportsValidationRunner: true,
        supportsCloudSync: false,
        supportedLocales: ["zh-CN", "en-US"],
        protocolVersion: 1
    )
}

private func makeSnapshot(sessionID: String, lease: LeaseDTO) -> WorkbenchSnapshotDTO {
    let task = makeTask(id: lease.taskID, subject: "Sample Task", status: "open")
    let issue = makeIssue(taskID: lease.taskID)
    return WorkbenchSnapshotDTO(
        sessionID: sessionID,
        missions: [],
        tasks: [task],
        issues: [issue],
        leases: [lease],
        failures: [],
        events: []
    )
}

private func makeSnapshot(sessionID: String, missions: [MissionDTO]) -> WorkbenchSnapshotDTO {
    WorkbenchSnapshotDTO(
        sessionID: sessionID,
        missions: missions,
        tasks: [],
        issues: [],
        failures: [],
        events: []
    )
}

private func makeSnapshot(sessionID: String, issues: [IssueDTO]) -> WorkbenchSnapshotDTO {
    WorkbenchSnapshotDTO(
        sessionID: sessionID,
        missions: [],
        tasks: [],
        issues: issues,
        failures: [],
        events: []
    )
}

private func makeTask(id: String, subject: String, status: String) -> TaskDTO {
    TaskDTO(
        id: id,
        sessionID: "sess-001",
        subject: subject,
        description: "",
        status: status,
        activeForm: nil,
        owner: nil,
        blocks: [],
        blockedBy: [],
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeIssue(taskID: String, missionID: String = "mission-001") -> IssueDTO {
    IssueDTO(
        sessionID: "sess-001",
        taskID: taskID,
        missionID: missionID,
        parallelMode: "exclusive",
        riskLevel: "medium",
        requiresHumanApproval: false,
        acceptanceCriteria: [],
        expectedArtifacts: [],
        relatedBranch: "",
        relatedWorktree: "",
        relatedPR: "",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeMission(id: String, sessionID: String) -> MissionDTO {
    MissionDTO(
        id: id,
        sessionID: sessionID,
        title: "Mac 工作台",
        goal: "补齐 API 调用面",
        status: "active",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeLease(id: String, taskID: String, state: String) -> LeaseDTO {
    LeaseDTO(
        id: id,
        sessionID: "sess-001",
        taskID: taskID,
        agentID: "local-agent",
        state: state,
        expiresAt: "2026-06-27T08:00:00",
        worktreeName: "wt-001",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeWorktree(
    name: String,
    taskID: String,
    status: String,
    keptReason: String = ""
) -> WorktreeDTO {
    WorktreeDTO(
        name: name,
        path: "/repo/.naumi/worktrees/\(name)",
        branch: "naumi/\(name)",
        baseRef: "main",
        status: status,
        taskID: taskID,
        dirtyFiles: 0,
        commitsAhead: 1,
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00",
        keptReason: keptReason,
        metadata: ["agent_id": "local-agent"],
        removable: true
    )
}

private func makeIntentLock(id: String, missionID: String) -> IntentLockDTO {
    IntentLockDTO(
        id: id,
        sessionID: "sess-001",
        missionID: missionID,
        rule: "禁止修改 core 模块",
        blockedPaths: ["src/core"],
        allowedPaths: ["src/core/README.md"],
        requireProposalForRisk: "high",
        active: true,
        createdAt: "2026-06-27T06:00:00"
    )
}

private func makeDecision(id: String, missionID: String) -> DecisionDTO {
    DecisionDTO(
        id: id,
        sessionID: "sess-001",
        missionID: missionID,
        kind: "architecture",
        title: "采用 FastAPI",
        content: "使用 FastAPI 承载 Workbench API",
        actor: "Planner-Agent",
        createdAt: "2026-06-27T06:00:00"
    )
}

private func makeValidationRun(id: String, taskID: String, status: String) -> ValidationRunDTO {
    ValidationRunDTO(
        id: id,
        sessionID: "sess-001",
        taskID: taskID,
        actor: "ValidationRunner",
        command: ["pytest", "tests/unit/test_workbench.py", "-q"],
        cwd: "/repo",
        status: status,
        exitCode: status == "passed" ? 0 : 1,
        output: status == "passed" ? "passed" : "failed",
        startedAt: "2026-06-27T06:00:00",
        completedAt: "2026-06-27T06:00:10"
    )
}

private func makeContextSnapshot(id: String, taskID: String, health: String) -> ContextSnapshotDTO {
    ContextSnapshotDTO(
        id: id,
        sessionID: "sess-001",
        agentID: "agent-001",
        taskID: taskID,
        health: health,
        reasons: health == "good" ? ["上下文健康"] : ["引用文件已过期"],
        createdAt: "2026-06-27T06:00:00"
    )
}

private func makeApproval(id: String, missionID: String, state: String) -> ApprovalDTO {
    ApprovalDTO(
        id: id,
        sessionID: "sess-001",
        missionID: missionID,
        taskID: "task-001",
        state: state,
        title: "允许重构 core 模块",
        detail: "保持测试通过",
        requester: "Agent-A",
        reviewer: "Human",
        decisionNote: "同意",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:01"
    )
}

private func makeEvent(id: String, type: String, subjectID: String) -> EventDTO {
    EventDTO(
        id: id,
        sessionID: "sess-001",
        type: type,
        actor: "Human",
        subjectID: subjectID,
        payload: ["title": .string("Event \(id)")],
        timestamp: "2026-06-27T06:00:00"
    )
}

private func makeFailure(id: String, taskID: String, status: String) -> FailureDTO {
    FailureDTO(
        id: id,
        sessionID: "sess-001",
        taskID: taskID,
        kind: "test_failed",
        title: "测试失败",
        detail: "保持测试通过",
        sourceID: "run-001",
        status: status,
        createdAt: "2026-06-27T06:00:00"
    )
}

@MainActor
private func seedSelectedDetails(_ appState: AppState) {
    appState.selectedEvent = makeEvent(id: "evt-selected-old", type: "old.event", subjectID: "old")
    appState.selectedValidationRun = makeValidationRun(id: "run-selected-old", taskID: "task-old", status: "passed")
    appState.selectedContextSnapshot = makeContextSnapshot(id: "ctx-selected-old", taskID: "task-old", health: "good")
    appState.selectedApproval = makeApproval(id: "approval-selected-old", missionID: "mission-old", state: "waiting")
    appState.selectedFailure = makeFailure(id: "failure-selected-old", taskID: "task-old", status: "open")
    appState.selectedIssue = makeIssue(taskID: "task-selected-old", missionID: "mission-old")
    appState.selectedLease = makeLease(id: "lease-selected-old", taskID: "task-old", state: "active")
    appState.selectedWorktree = makeWorktree(name: "wt-selected-old", taskID: "task-old", status: "active")
    appState.selectedMission = makeMission(id: "mission-selected-old", sessionID: "sess-old")
    appState.selectedAgentProfile = makeAgentProfile(id: "agent-selected-old", sessionID: "sess-old", status: "idle")
    appState.selectedDecision = makeDecision(id: "decision-selected-old", missionID: "mission-old")
    appState.selectedIntentLock = makeIntentLock(id: "lock-selected-old", missionID: "mission-old")
}

private func makeSession(id: String, title: String) -> SessionDTO {
    SessionDTO(
        id: id,
        title: title,
        model: "gpt-4o",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:30:00",
        messageCount: 1,
        totalTokens: 10,
        totalCostUSD: 0.001,
        status: "active"
    )
}
