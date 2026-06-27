import Foundation

/// Abstraction over the NaumiAgent Workbench REST API.
///
/// Allows `WorkbenchAPIClient` to be replaced by fakes in tests,
/// while keeping the real `URLSession` behavior in production.
public protocol WorkbenchAPIProviding: Sendable {
    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO
    func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO
    func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO
    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO

    /// Fetches the most recent audit events for the given session.
    func fetchEvents(sessionID: String, limit: Int) async throws(APIError) -> WorkbenchEventsDTO

    /// Fetches validation runs for the given session, optionally filtered by task.
    func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO

    /// Fetches context health snapshots for the given session.
    func fetchContextSnapshots(
        sessionID: String,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> ContextSnapshotsDTO

    /// Fetches approval requests for the given session, optionally filtered by state.
    func fetchApprovals(
        sessionID: String,
        state: String?,
        limit: Int
    ) async throws(APIError) -> ApprovalsDTO

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
