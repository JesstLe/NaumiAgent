import Foundation

/// Abstraction over the NaumiAgent Workbench REST API.
///
/// Allows `WorkbenchAPIClient` to be replaced by fakes in tests,
/// while keeping the real `URLSession` behavior in production.
public protocol WorkbenchAPIProviding: Sendable {
    func fetchBootstrap(pageSize: Int) async throws(APIError) -> WorkbenchBootstrapDTO
    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO
    func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO
    func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO
    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO
    func createSession(
        title: String?,
        model: String?,
        systemPrompt: String?
    ) async throws(APIError) -> SessionDTO

    /// Fetches audit events for the given session, optionally filtered by event fields.
    func fetchEvents(
        sessionID: String,
        eventType: String?,
        subjectID: String?,
        actor: String?,
        limit: Int
    ) async throws(APIError) -> WorkbenchEventsDTO

    /// Fetches one audit event by id for detail drill-downs.
    func fetchEvent(sessionID: String, eventID: String) async throws(APIError) -> EventDTO

    /// Fetches validation runs for the given session, optionally filtered by task.
    func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO

    /// Fetches one validation run by id for detailed output views.
    func fetchValidationRun(sessionID: String, runID: String) async throws(APIError) -> ValidationRunDTO

    /// Fetches context health snapshots for the given session.
    func fetchContextSnapshots(
        sessionID: String,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> ContextSnapshotsDTO

    /// Fetches one context health snapshot by id for detail drill-downs.
    func fetchContextSnapshot(sessionID: String, snapshotID: String) async throws(APIError) -> ContextSnapshotDTO

    /// Records a context health update for the given session and issue, returning the created snapshot.
    func recordContextHealth(
        sessionID: String,
        taskID: String,
        agentID: String,
        minutesSinceSync: Int,
        tokenLoadRatio: Double,
        policyConflict: Bool,
        actor: String
    ) async throws(APIError) -> ContextSnapshotDTO

    /// Fetches approval requests for the given session, optionally filtered by state.
    func fetchApprovals(
        sessionID: String,
        state: String?,
        limit: Int
    ) async throws(APIError) -> ApprovalsDTO

    /// Fetches failure cards for the given session, optionally filtered by task or status.
    func fetchFailures(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> FailuresDTO

    /// Fetches one failure card by id for detailed diagnostics.
    func fetchFailure(sessionID: String, failureID: String) async throws(APIError) -> FailureDTO

    /// Fetches issues for the given session, optionally filtered by mission or risk level.
    func fetchIssues(
        sessionID: String,
        missionID: String?,
        riskLevel: String?,
        limit: Int
    ) async throws(APIError) -> IssuesDTO

    /// Fetches one issue metadata record by task id for detail drill-downs.
    func fetchIssue(sessionID: String, taskID: String) async throws(APIError) -> IssueDTO

    /// Fetches leases for the given session, optionally filtered by state, task, or agent.
    func fetchLeases(
        sessionID: String,
        state: String?,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> LeasesDTO

    /// Fetches worktrees for the given session, optionally filtered by task or status.
    func fetchWorktrees(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> WorktreesDTO

    /// Fetches one worktree by name.
    func fetchWorktree(sessionID: String, name: String) async throws(APIError) -> WorktreeDTO

    /// Marks a worktree as kept for human review or follow-up.
    func keepWorktree(
        sessionID: String,
        name: String,
        actor: String,
        reason: String
    ) async throws(APIError) -> WorktreeDTO

    /// Removes a tracked worktree. `discardChanges` force-removes dirty worktrees.
    func removeWorktree(
        sessionID: String,
        name: String,
        discardChanges: Bool
    ) async throws(APIError) -> WorktreeRemovalDTO

    /// Fetches missions for the given session, optionally filtered by status.
    func fetchMissions(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> MissionsDTO

    /// Fetches one mission by id for detail drill-downs.
    func fetchMission(sessionID: String, missionID: String) async throws(APIError) -> MissionDTO

    /// Fetches agent capability profiles for the given session, optionally filtered by status.
    func fetchAgentProfiles(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> AgentProfilesDTO

    /// Registers or updates an agent capability profile.
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
    ) async throws(APIError) -> AgentProfileDTO

    /// Claims an open issue for the given agent, creating a new lease.
    func claimIssue(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseDTO

    /// Releases an existing lease, returning the updated lease record.
    func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO

    /// Expires overdue leases in the given session, returning the leases that were expired.
    func expireLeases(sessionID: String) async throws(APIError) -> ExpiredLeasesDTO

    /// Creates a mission inside the selected session.
    func createMission(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionDTO

    /// Attaches an issue to a mission.
    func attachIssue(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO
    func createIssue(
        sessionID: String,
        missionID: String,
        title: String,
        description: String,
        blockedBy: [String],
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO

    /// Fetches intent locks for the given session and mission.
    func fetchIntentLocks(
        sessionID: String,
        missionID: String
    ) async throws(APIError) -> IntentLocksDTO

    /// Creates an intent lock for the given mission.
    func createIntentLock(
        sessionID: String,
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async throws(APIError) -> IntentLockDTO

    /// Fetches decisions for the given session and mission.
    func fetchDecisions(
        sessionID: String,
        missionID: String
    ) async throws(APIError) -> DecisionsDTO

    /// Creates a decision for the given mission.
    func createDecision(
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String
    ) async throws(APIError) -> DecisionDTO

    /// Resolves an approval request as approved or rejected.
    func resolveApproval(
        sessionID: String,
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async throws(APIError) -> ApprovalDTO

    /// Runs a validation command in the given session and returns its result.
    func runValidation(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultDTO
}
