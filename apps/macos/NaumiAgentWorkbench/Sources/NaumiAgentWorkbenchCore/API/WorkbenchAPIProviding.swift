import Foundation

/// Optional API-provider capability for daemon-supplied REST route templates.
public protocol WorkbenchRouteTemplateConfiguring: Sendable {
    func setRouteTemplates(_ templates: [String: String]) async
}

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

    /// Creates a Workbench session and returns the backend's startup payload,
    /// including daemon metadata, capabilities, session registry, and snapshot.
    func createWorkbenchSession(
        title: String?,
        model: String?,
        systemPrompt: String?
    ) async throws(APIError) -> WorkbenchBootstrapDTO

    /// Sends a daily chat message, optionally linking it to a new Workbench issue.
    func sendMessage(
        sessionID: String,
        content: String,
        workbenchIssue: ChatIssueDraftDTO?
    ) async throws(APIError) -> ChatMessageDTO

    /// Fetches persisted chat messages for the selected session.
    func fetchMessages(
        sessionID: String,
        page: Int,
        pageSize: Int
    ) async throws(APIError) -> ChatMessageListDTO

    /// Fetches audit events for the given session, optionally filtered by event fields.
    func fetchEvents(
        sessionID: String,
        eventType: String?,
        subjectID: String?,
        actor: String?,
        since: String?,
        severity: String?,
        correlationID: String?,
        parentEventID: String?,
        limit: Int
    ) async throws(APIError) -> WorkbenchEventsDTO

    /// Fetches one audit event by id for detail drill-downs.
    func fetchEvent(sessionID: String, eventID: String) async throws(APIError) -> EventDTO

    /// Fetches validation runs for the given session, optionally filtered by task.
    func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO

    /// Fetches one validation run by id for detailed output views.
    func fetchValidationRun(sessionID: String, runID: String) async throws(APIError) -> ValidationRunDTO

    /// Fetches context health snapshots for the given session.
    func fetchContextSnapshots(
        sessionID: String,
        taskID: String?,
        agentID: String?,
        health: String?,
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

    /// Records a context health update and returns the backend's fresh authoritative snapshot.
    func recordContextHealthWithSnapshot(
        sessionID: String,
        taskID: String,
        agentID: String,
        minutesSinceSync: Int,
        tokenLoadRatio: Double,
        policyConflict: Bool,
        actor: String
    ) async throws(APIError) -> ContextHealthSnapshotDTO

    /// Fetches approval requests for the given session, optionally filtered by state.
    func fetchApprovals(
        sessionID: String,
        state: String?,
        missionID: String?,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ApprovalsDTO

    /// Fetches one approval request by id for human-governance detail views.
    func fetchApproval(sessionID: String, approvalID: String) async throws(APIError) -> ApprovalDTO

    /// Fetches real review evidence (diff, changed files, validation runs, ...)
    /// for an approval, collected from the store + local git worktree.
    func fetchReviewEvidence(sessionID: String, approvalID: String) async throws(APIError) -> ReviewEvidenceDTO

    /// Fetches failure cards for the given session, optionally filtered by task or status.
    func fetchFailures(
        sessionID: String,
        taskID: String?,
        status: String?,
        kind: String?,
        limit: Int
    ) async throws(APIError) -> FailuresDTO

    /// Fetches one failure card by id for detailed diagnostics.
    func fetchFailure(sessionID: String, failureID: String) async throws(APIError) -> FailureDTO

    /// Fetches issues for the given session, optionally filtered by mission or risk level.
    func fetchIssues(
        sessionID: String,
        missionID: String?,
        riskLevel: String?,
        status: String?,
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

    /// Fetches one lease by id for task-market detail views.
    func fetchLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO

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

    /// Marks a worktree as kept and returns the backend's fresh authoritative snapshot.
    func keepWorktreeWithSnapshot(
        sessionID: String,
        name: String,
        actor: String,
        reason: String
    ) async throws(APIError) -> WorktreeSnapshotDTO

    /// Removes a tracked worktree. `discardChanges` force-removes dirty worktrees.
    func removeWorktree(
        sessionID: String,
        name: String,
        discardChanges: Bool
    ) async throws(APIError) -> WorktreeRemovalDTO

    /// Removes a tracked worktree and returns the backend's fresh authoritative snapshot.
    func removeWorktreeWithSnapshot(
        sessionID: String,
        name: String,
        discardChanges: Bool
    ) async throws(APIError) -> WorktreeRemovalSnapshotDTO

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

    /// Fetches one agent capability profile by id for task-market detail views.
    func fetchAgentProfile(sessionID: String, agentID: String) async throws(APIError) -> AgentProfileDTO

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

    /// Registers or updates an agent capability profile and returns the backend's fresh authoritative snapshot.
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
    ) async throws(APIError) -> AgentProfileSnapshotDTO

    /// Records a heartbeat for the given agent.
    func recordAgentHeartbeat(sessionID: String, agentID: String) async throws(APIError) -> AgentProfileDTO

    /// Records a heartbeat and returns the backend's fresh authoritative snapshot.
    func recordAgentHeartbeatWithSnapshot(sessionID: String, agentID: String) async throws(APIError) -> AgentProfileSnapshotDTO

    /// Claims an open issue for the given agent, creating a new lease.
    func claimIssue(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseDTO

    /// Claims an open issue and returns the backend's fresh authoritative snapshot.
    func claimIssueWithSnapshot(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseSnapshotDTO

    /// Fetches competing bids for an issue (task).
    func fetchIssueBids(
        sessionID: String,
        taskID: String,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> IssueBidsDTO

    /// Submits a new agent bid for an issue, returning the bids for that issue.
    func submitIssueBid(
        sessionID: String,
        taskID: String,
        draft: IssueBidDraft
    ) async throws(APIError) -> IssueBidsDTO

    /// Lists proposals for a session, optionally filtered.
    func fetchProposals(
        sessionID: String,
        missionID: String?,
        taskID: String?,
        state: String?,
        limit: Int
    ) async throws(APIError) -> ProposalsDTO

    /// Fetches a single proposal by id.
    func fetchProposal(sessionID: String, proposalID: String) async throws(APIError) -> ProposalDTO

    /// Creates a new proposal and returns it.
    func createProposal(
        sessionID: String,
        draft: ProposalDraft
    ) async throws(APIError) -> ProposalDTO

    /// Approves an open proposal and returns the updated record.
    func approveProposal(
        sessionID: String,
        proposalID: String,
        draft: ProposalResolveDraft
    ) async throws(APIError) -> ProposalDTO

    /// Rejects an open proposal and returns the updated record.
    func rejectProposal(
        sessionID: String,
        proposalID: String,
        draft: ProposalResolveDraft
    ) async throws(APIError) -> ProposalDTO

    /// Converts an open proposal into a tracked issue and returns the updated record.
    func convertProposal(
        sessionID: String,
        proposalID: String,
        draft: ProposalResolveDraft
    ) async throws(APIError) -> ProposalDTO

    /// Releases an existing lease, returning the updated lease record.
    func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO

    /// Releases an existing lease and returns the backend's fresh authoritative snapshot.
    func releaseLeaseWithSnapshot(sessionID: String, leaseID: String) async throws(APIError) -> LeaseSnapshotDTO

    /// Expires overdue leases in the given session, returning the leases that were expired.
    func expireLeases(sessionID: String) async throws(APIError) -> ExpiredLeasesDTO

    /// Expires overdue leases and returns the backend's fresh authoritative snapshot.
    func expireLeasesWithSnapshot(sessionID: String) async throws(APIError) -> ExpiredLeasesSnapshotDTO

    /// Creates a mission inside the selected session.
    func createMission(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionDTO

    /// Creates a mission and returns the backend's fresh authoritative snapshot.
    func createMissionWithSnapshot(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionSnapshotDTO

    /// Attaches an issue to a mission.
    func attachIssue(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO

    /// Attaches an issue to a mission and returns the backend's fresh authoritative snapshot.
    func attachIssueWithSnapshot(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueSnapshotDTO

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

    /// Creates a backing task, attaches it as an issue, and returns the fresh authoritative snapshot.
    func createIssueWithSnapshot(
        sessionID: String,
        missionID: String,
        title: String,
        description: String,
        blockedBy: [String],
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueSnapshotDTO

    /// Fetches intent locks for the given session and mission.
    func fetchIntentLocks(
        sessionID: String,
        missionID: String,
        active: Bool?
    ) async throws(APIError) -> IntentLocksDTO

    /// Fetches one intent lock by id for human-governance detail views.
    func fetchIntentLock(
        sessionID: String,
        missionID: String,
        lockID: String
    ) async throws(APIError) -> IntentLockDTO

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

    /// Creates an intent lock and returns the backend's fresh authoritative snapshot.
    func createIntentLockWithSnapshot(
        sessionID: String,
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async throws(APIError) -> IntentLockSnapshotDTO

    /// Deactivates an intent lock so it no longer blocks actions.
    func deactivateIntentLock(
        sessionID: String,
        missionID: String,
        lockID: String,
        actor: String
    ) async throws(APIError) -> IntentLockDTO

    /// Fetches decisions for the given session and mission.
    func fetchDecisions(
        sessionID: String,
        missionID: String,
        kind: String?
    ) async throws(APIError) -> DecisionsDTO

    /// Fetches one governance decision by id for review detail views.
    func fetchDecision(
        sessionID: String,
        missionID: String,
        decisionID: String
    ) async throws(APIError) -> DecisionDTO

    /// Creates a decision for the given mission.
    func createDecision(
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String
    ) async throws(APIError) -> DecisionDTO

    /// Creates a governance decision and returns the backend's fresh authoritative snapshot.
    func createDecisionWithSnapshot(
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String
    ) async throws(APIError) -> DecisionSnapshotDTO

    /// Resolves an approval request as approved or rejected.
    func resolveApproval(
        sessionID: String,
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async throws(APIError) -> ApprovalDTO

    /// Resolves an approval and returns the backend's fresh authoritative snapshot.
    func resolveApprovalWithSnapshot(
        sessionID: String,
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async throws(APIError) -> ApprovalSnapshotDTO

    /// Runs a validation command in the given session and returns its result.
    func runValidation(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultDTO

    /// Runs a validation command and returns the backend's fresh authoritative snapshot.
    func runValidationWithSnapshot(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultSnapshotDTO
}
