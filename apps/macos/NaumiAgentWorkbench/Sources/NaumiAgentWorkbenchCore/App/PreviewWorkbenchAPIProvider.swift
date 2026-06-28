import Foundation

#if NAUMI_WORKBENCH_LOCAL_PREVIEWS
/// Local SwiftUI preview data source that mirrors the current Workbench API contract.
final class PreviewWorkbenchAPIProvider: WorkbenchAPIProviding {
    private let now = "2026-06-27T06:00:00"

    func fetchBootstrap(pageSize: Int) async throws(APIError) -> WorkbenchBootstrapDTO {
        let session = makeSession(id: "preview-session", title: "Mac 工作台预览")
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: session.id,
            missions: [makeMission(id: "preview-mission", sessionID: session.id)],
            agentProfiles: [makeAgentProfile(sessionID: session.id, agentID: "preview-agent")],
            tasks: [makeTask(sessionID: session.id, taskID: "preview-task")],
            issues: [makeIssue(sessionID: session.id, missionID: "preview-mission", taskID: "preview-task")],
            leases: [makeLease(sessionID: session.id, leaseID: "preview-lease", taskID: "preview-task")],
            failures: [makeFailure(sessionID: session.id, failureID: "preview-failure", taskID: "preview-task")],
            events: []
        )
        return WorkbenchBootstrapDTO(
            daemonStatus: try await fetchDaemonStatus(),
            capabilities: try await fetchCapabilities(),
            sessions: [session],
            totalSessions: 1,
            selectedSessionID: session.id,
            snapshot: snapshot
        )
    }

    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO {
        DaemonStatusDTO(
            status: "running",
            version: "0.1.0",
            pid: 1,
            host: "127.0.0.1",
            port: 8765,
            startedAt: now,
            workspaceCount: 1
        )
    }

    func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO {
        CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: false,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1
        )
    }

    func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO {
        WorkbenchSnapshotDTO(
            sessionID: sessionID,
            missions: [makeMission(id: "preview-mission", sessionID: sessionID)],
            agentProfiles: [makeAgentProfile(sessionID: sessionID, agentID: "preview-agent")],
            tasks: [makeTask(sessionID: sessionID, taskID: "preview-task")],
            issues: [makeIssue(sessionID: sessionID, missionID: "preview-mission", taskID: "preview-task")],
            leases: [makeLease(sessionID: sessionID, leaseID: "preview-lease", taskID: "preview-task")],
            failures: [makeFailure(sessionID: sessionID, failureID: "preview-failure", taskID: "preview-task")],
            events: []
        )
    }

    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        SessionListDTO(
            sessions: [makeSession(id: "preview-session", title: "Mac 工作台预览")],
            total: 1,
            page: page,
            pageSize: pageSize
        )
    }

    func createSession(title: String?, model: String?, systemPrompt: String?) async throws(APIError) -> SessionDTO {
        makeSession(id: "preview-session", title: title ?? "新建预览会话", model: model ?? "preview")
    }

    func fetchEvents(
        sessionID: String,
        eventType: String?,
        subjectID: String?,
        actor: String?,
        limit: Int
    ) async throws(APIError) -> WorkbenchEventsDTO {
        WorkbenchEventsDTO(
            events: [makeEvent(sessionID: sessionID, eventID: "preview-event")],
            eventType: eventType,
            subjectID: subjectID,
            actor: actor,
            limit: limit
        )
    }

    func fetchEvent(sessionID: String, eventID: String) async throws(APIError) -> EventDTO {
        makeEvent(sessionID: sessionID, eventID: eventID)
    }

    func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO {
        ValidationRunsDTO(
            validationRuns: [makeValidationRun(sessionID: sessionID, runID: "preview-run", taskID: taskID ?? "preview-task")],
            taskID: taskID,
            limit: limit
        )
    }

    func fetchValidationRun(sessionID: String, runID: String) async throws(APIError) -> ValidationRunDTO {
        makeValidationRun(sessionID: sessionID, runID: runID, taskID: "preview-task")
    }

    func fetchContextSnapshots(
        sessionID: String,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> ContextSnapshotsDTO {
        ContextSnapshotsDTO(contextSnapshots: [], taskID: taskID, agentID: agentID, limit: limit)
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
        ContextSnapshotDTO(
            id: "preview-context",
            sessionID: sessionID,
            agentID: agentID,
            taskID: taskID,
            health: policyConflict ? "stale" : "fresh",
            reasons: ["preview"],
            createdAt: now
        )
    }

    func fetchApprovals(
        sessionID: String,
        state: String?,
        limit: Int
    ) async throws(APIError) -> ApprovalsDTO {
        ApprovalsDTO(approvals: [], state: state, limit: limit)
    }

    func fetchFailures(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> FailuresDTO {
        FailuresDTO(
            failures: [makeFailure(sessionID: sessionID, failureID: "preview-failure", taskID: taskID ?? "preview-task")],
            taskID: taskID,
            status: status,
            limit: limit
        )
    }

    func fetchFailure(sessionID: String, failureID: String) async throws(APIError) -> FailureDTO {
        makeFailure(sessionID: sessionID, failureID: failureID, taskID: "preview-task")
    }

    func fetchIssues(
        sessionID: String,
        missionID: String?,
        riskLevel: String?,
        limit: Int
    ) async throws(APIError) -> IssuesDTO {
        IssuesDTO(
            issues: [
                makeIssue(
                    sessionID: sessionID,
                    missionID: missionID ?? "preview-mission",
                    taskID: "preview-task",
                    riskLevel: riskLevel ?? "medium"
                )
            ],
            missionID: missionID,
            riskLevel: riskLevel,
            limit: limit
        )
    }

    func fetchIssue(sessionID: String, taskID: String) async throws(APIError) -> IssueDTO {
        makeIssue(sessionID: sessionID, missionID: "preview-mission", taskID: taskID)
    }

    func fetchLeases(
        sessionID: String,
        state: String?,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> LeasesDTO {
        LeasesDTO(leases: [], state: state, taskID: taskID, agentID: agentID, limit: limit)
    }

    func fetchWorktrees(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> WorktreesDTO {
        WorktreesDTO(
            worktrees: [makeWorktree(sessionID: sessionID, name: "wt-preview", taskID: taskID ?? "preview-task")],
            taskID: taskID,
            status: status,
            limit: limit
        )
    }

    func fetchWorktree(sessionID: String, name: String) async throws(APIError) -> WorktreeDTO {
        makeWorktree(sessionID: sessionID, name: name, taskID: "preview-task")
    }

    func keepWorktree(
        sessionID: String,
        name: String,
        actor: String,
        reason: String
    ) async throws(APIError) -> WorktreeDTO {
        makeWorktree(
            sessionID: sessionID,
            name: name,
            taskID: "preview-task",
            status: "kept",
            keptReason: reason
        )
    }

    func removeWorktree(
        sessionID: String,
        name: String,
        discardChanges: Bool
    ) async throws(APIError) -> WorktreeRemovalDTO {
        WorktreeRemovalDTO(
            name: name,
            discardChanges: discardChanges,
            message: "preview removed \(name)"
        )
    }

    func fetchMissions(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> MissionsDTO {
        MissionsDTO(missions: [], status: status, limit: limit)
    }

    func fetchAgentProfiles(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> AgentProfilesDTO {
        AgentProfilesDTO(agentProfiles: [], status: status, limit: limit)
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
        AgentProfileDTO(
            id: agentID,
            sessionID: sessionID,
            name: name,
            role: role,
            capabilities: capabilities,
            permissions: permissions,
            maxParallelTasks: maxParallelTasks,
            status: status,
            createdAt: now,
            updatedAt: now
        )
    }

    func claimIssue(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseDTO {
        LeaseDTO(
            id: "preview-lease",
            sessionID: sessionID,
            taskID: taskID,
            agentID: agentID,
            state: "active",
            expiresAt: "2026-06-27T07:00:00",
            worktreeName: worktreeName,
            createdAt: now,
            updatedAt: now
        )
    }

    func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        makeLease(sessionID: sessionID, leaseID: leaseID, taskID: "preview-task", state: "released")
    }

    func expireLeases(sessionID: String) async throws(APIError) -> ExpiredLeasesDTO {
        ExpiredLeasesDTO(expired: [])
    }

    func createMission(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionDTO {
        MissionDTO(
            id: "preview-mission",
            sessionID: sessionID,
            title: title,
            goal: goal,
            status: "active",
            createdAt: now,
            updatedAt: now
        )
    }

    func attachIssue(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO {
        makeIssue(
            sessionID: sessionID,
            missionID: missionID,
            taskID: taskID,
            acceptanceCriteria: acceptanceCriteria,
            parallelMode: parallelMode,
            riskLevel: riskLevel
        )
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
        makeIssue(
            sessionID: sessionID,
            missionID: missionID,
            taskID: "preview-created-issue",
            acceptanceCriteria: acceptanceCriteria,
            parallelMode: parallelMode,
            riskLevel: riskLevel
        )
    }

    func fetchIntentLocks(sessionID: String, missionID: String) async throws(APIError) -> IntentLocksDTO {
        IntentLocksDTO(intentLocks: [], missionID: missionID)
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
        IntentLockDTO(
            id: "preview-lock",
            sessionID: sessionID,
            missionID: missionID,
            rule: rule,
            blockedPaths: blockedPaths,
            allowedPaths: allowedPaths,
            requireProposalForRisk: requireProposalForRisk,
            active: true,
            createdAt: now
        )
    }

    func fetchDecisions(sessionID: String, missionID: String) async throws(APIError) -> DecisionsDTO {
        DecisionsDTO(decisions: [], missionID: missionID)
    }

    func createDecision(
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String
    ) async throws(APIError) -> DecisionDTO {
        DecisionDTO(
            id: "preview-decision",
            sessionID: sessionID,
            missionID: missionID,
            kind: kind,
            title: title,
            content: content,
            actor: actor,
            createdAt: now
        )
    }

    func resolveApproval(
        sessionID: String,
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async throws(APIError) -> ApprovalDTO {
        ApprovalDTO(
            id: approvalID,
            sessionID: sessionID,
            missionID: "preview-mission",
            taskID: "preview-task",
            state: state,
            title: "预览审批",
            detail: "本地预览审批记录",
            requester: "Planner-Agent",
            reviewer: actor,
            decisionNote: decisionNote,
            createdAt: now,
            updatedAt: now
        )
    }

    func runValidation(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultDTO {
        ValidationResultDTO(id: "preview-run", status: "passed", exitCode: 0, output: argv.joined(separator: " "))
    }

    private func makeSession(id: String, title: String, model: String = "preview") -> SessionDTO {
        SessionDTO(
            id: id,
            title: title,
            model: model,
            createdAt: now,
            updatedAt: now,
            messageCount: 0,
            totalTokens: 0,
            totalCostUSD: 0,
            status: "active"
        )
    }

    private func makeMission(id: String, sessionID: String) -> MissionDTO {
        MissionDTO(
            id: id,
            sessionID: sessionID,
            title: "实现 SwiftUI 工作台骨架",
            goal: "补齐导航页面预览与本地开发链路",
            status: "active",
            createdAt: now,
            updatedAt: now
        )
    }

    private func makeTask(sessionID: String, taskID: String) -> TaskDTO {
        TaskDTO(
            id: taskID,
            sessionID: sessionID,
            subject: "预览任务",
            description: "用于 SwiftUI 本地预览的数据",
            status: "open",
            activeForm: nil,
            owner: "Preview-Agent",
            blocks: [],
            blockedBy: [],
            createdAt: now,
            updatedAt: now
        )
    }

    private func makeIssue(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String] = ["预览可编译"],
        parallelMode: String = "exclusive",
        riskLevel: String = "medium"
    ) -> IssueDTO {
        IssueDTO(
            sessionID: sessionID,
            taskID: taskID,
            missionID: missionID,
            parallelMode: parallelMode,
            riskLevel: riskLevel,
            requiresHumanApproval: false,
            acceptanceCriteria: acceptanceCriteria,
            expectedArtifacts: [],
            relatedBranch: "codex/mac-workbench-mvp",
            relatedWorktree: "wt-preview",
            relatedPR: "",
            createdAt: now,
            updatedAt: now
        )
    }

    private func makeLease(
        sessionID: String,
        leaseID: String,
        taskID: String,
        state: String = "active"
    ) -> LeaseDTO {
        LeaseDTO(
            id: leaseID,
            sessionID: sessionID,
            taskID: taskID,
            agentID: "Preview-Agent",
            state: state,
            expiresAt: "2026-06-27T07:00:00",
            worktreeName: "wt-preview",
            createdAt: now,
            updatedAt: now
        )
    }

    private func makeEvent(sessionID: String, eventID: String) -> EventDTO {
        EventDTO(
            id: eventID,
            sessionID: sessionID,
            type: "mission.created",
            actor: "Preview-Agent",
            subjectID: "preview-mission",
            payload: ["title": .string("Mac 工作台预览")],
            timestamp: now
        )
    }

    private func makeValidationRun(sessionID: String, runID: String, taskID: String) -> ValidationRunDTO {
        ValidationRunDTO(
            id: runID,
            sessionID: sessionID,
            taskID: taskID,
            actor: "Preview-Agent",
            command: ["pytest", "tests/unit/test_api_workbench.py", "-q"],
            cwd: "/Users/lv/Workspace/NaumiAgent",
            status: "passed",
            exitCode: 0,
            output: "preview validation passed",
            startedAt: now,
            completedAt: now
        )
    }

    private func makeFailure(sessionID: String, failureID: String, taskID: String) -> FailureDTO {
        FailureDTO(
            id: failureID,
            sessionID: sessionID,
            taskID: taskID,
            kind: "test_failed",
            title: "DTO 解码测试失败",
            detail: "pytest tests/unit/test_dto.py -q failed with 2 failures",
            sourceID: "preview-run",
            status: "open",
            createdAt: now
        )
    }

    private func makeWorktree(
        sessionID: String,
        name: String,
        taskID: String,
        status: String = "clean",
        keptReason: String = ""
    ) -> WorktreeDTO {
        WorktreeDTO(
            name: name,
            path: "/repo/.naumi/worktrees/\(name)",
            branch: "naumi/worktree-\(name)",
            baseRef: "preview-base",
            status: status,
            taskID: taskID,
            dirtyFiles: status == "clean" ? 0 : 2,
            commitsAhead: status == "clean" ? 0 : 1,
            createdAt: now,
            updatedAt: now,
            keptReason: keptReason,
            metadata: ["session_id": sessionID],
            removable: status == "clean" && keptReason.isEmpty
        )
    }

    private func makeAgentProfile(sessionID: String, agentID: String) -> AgentProfileDTO {
        AgentProfileDTO(
            id: agentID,
            sessionID: sessionID,
            name: "Preview-Agent",
            role: "ui-preview",
            capabilities: ["swiftui", "validation"],
            permissions: ["read"],
            maxParallelTasks: 1,
            status: "idle",
            createdAt: now,
            updatedAt: now
        )
    }
}
#endif
