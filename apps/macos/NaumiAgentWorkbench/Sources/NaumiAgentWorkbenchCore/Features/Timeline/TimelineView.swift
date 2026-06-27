import SwiftUI

/// Standalone audit-event timeline for the selected session.
///
/// Events are loaded from `GET /workbench/sessions/{id}/events` and never
/// fabricated locally. The view refreshes automatically on appear and exposes
/// a manual refresh button in the toolbar.
public struct TimelineView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                if let lastError = appState.lastError {
                    errorCard(error: lastError)
                }
                eventList
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            guard !appState.isPreviewFixture else { return }
            await daemonController.refreshEvents(limit: 50)
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 10) {
                Text(AppStrings.Timeline.title(appState.locale))
                    .font(.system(size: 22, weight: .semibold))

                HStack(spacing: 12) {
                    Text(AppStrings.Timeline.eventCount(appState.locale, count: appState.timelineEvents.count))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)

                    if let sessionID = appState.selectedSessionID {
                        Text(sessionID)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }

            Spacer()

            Button {
                if !appState.isPreviewFixture {
                    Task {
                        await daemonController.refreshEvents(limit: 50)
                    }
                }
            } label: {
                Label(
                    AppStrings.Timeline.refreshButton(appState.locale),
                    systemImage: "arrow.clockwise"
                )
            }
            .buttonStyle(.bordered)
        }
    }

    // MARK: - Event List

    @ViewBuilder
    private var eventList: some View {
        if appState.timelineEvents.isEmpty {
            emptyState
        } else {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(presentedEvents) { event in
                    eventRow(event: event)
                    if event.id != presentedEvents.last?.id {
                        Divider()
                    }
                }
            }
            .padding()
            .background(Color.secondary.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }

    private var presentedEvents: [TimelineEventPresentation] {
        appState.timelineEvents.map(TimelineEventPresentation.init)
    }

    private func eventRow(event: TimelineEventPresentation) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(event.type)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                Text(event.timestamp)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Timeline.actorLabel(appState.locale),
                    value: event.actor
                )
                detailItem(
                    label: AppStrings.Timeline.subjectLabel(appState.locale),
                    value: event.subjectID
                )
            }

            if !event.payloadSummary.isEmpty {
                Text(event.payloadSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
    }

    private func detailItem(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.body)
                .fontWeight(.medium)
        }
    }

    private func errorCard(error: APIError) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Dashboard.errorSection(appState.locale))
                .font(.headline)
                .foregroundStyle(.red)
            Text(error.localizedMessage(locale: appState.locale))
                .font(.body)
                .foregroundStyle(.red)
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Empty State

    private var emptyState: some View {
        HStack {
            Spacer()
            VStack(spacing: 8) {
                Image(systemName: "clock.badge.questionmark")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text(AppStrings.Timeline.emptyEvents(appState.locale))
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.vertical, 32)
    }
}

#if DEBUG
struct TimelineView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        state.locale = .zhCN
        state.selectedSessionID = "sess-preview"
        state.timelineEvents = [
            EventDTO(
                id: "evt-1",
                sessionID: "sess-preview",
                type: "mission.created",
                actor: "Human",
                subjectID: "mission-1",
                payload: ["title": .string("Mac 工作台")],
                timestamp: "2026-06-27T06:00:00"
            )
        ]
        return TimelineView(
            appState: state,
            daemonController: DaemonController(
                appState: state,
                apiProvider: PreviewWorkbenchAPIProvider()
            )
        )
        .frame(minWidth: 640, minHeight: 420)
    }
}

/// Minimal fake provider so the preview compiles without a real daemon.
@MainActor
private final class PreviewWorkbenchAPIProvider: WorkbenchAPIProviding {
    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO {
        DaemonStatusDTO(
            status: "running",
            version: "0.1.0",
            pid: 1,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 0
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
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )
    }

    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        SessionListDTO(sessions: [], total: 0, page: page, pageSize: pageSize)
    }

    func createSession(
        title: String?,
        model: String?,
        systemPrompt: String?
    ) async throws(APIError) -> SessionDTO {
        SessionDTO(
            id: "preview-session",
            title: title,
            model: model ?? "preview",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00",
            messageCount: 0,
            totalTokens: 0,
            totalCostUSD: 0,
            status: "active"
        )
    }

    func fetchEvents(
        sessionID: String,
        eventType: String?,
        subjectID: String?,
        actor: String?,
        limit: Int
    ) async throws(APIError) -> WorkbenchEventsDTO {
        WorkbenchEventsDTO(events: [], eventType: eventType, subjectID: subjectID, actor: actor, limit: limit)
    }

    func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO {
        ValidationRunsDTO(validationRuns: [], taskID: taskID, limit: limit)
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
            id: "preview-snap",
            sessionID: sessionID,
            agentID: agentID,
            taskID: taskID,
            health: "good",
            reasons: [],
            createdAt: "2026-06-27T06:00:00"
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
        FailuresDTO(failures: [], taskID: taskID, status: status, limit: limit)
    }

    func fetchIssues(
        sessionID: String,
        missionID: String?,
        riskLevel: String?,
        limit: Int
    ) async throws(APIError) -> IssuesDTO {
        IssuesDTO(issues: [], missionID: missionID, riskLevel: riskLevel, limit: limit)
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
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
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
            id: "lease-1",
            sessionID: sessionID,
            taskID: taskID,
            agentID: agentID,
            state: "active",
            expiresAt: "2026-06-27T08:00:00",
            worktreeName: worktreeName,
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
        )
    }

    func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        LeaseDTO(
            id: leaseID,
            sessionID: sessionID,
            taskID: "task-1",
            agentID: "agent-1",
            state: "released",
            expiresAt: "2026-06-27T08:00:00",
            worktreeName: "",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
        )
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
            id: "mission-1",
            sessionID: sessionID,
            title: title,
            goal: goal,
            status: "active",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
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
        IssueDTO(
            sessionID: sessionID,
            taskID: taskID,
            missionID: missionID,
            parallelMode: parallelMode,
            riskLevel: riskLevel,
            requiresHumanApproval: false,
            acceptanceCriteria: acceptanceCriteria,
            expectedArtifacts: [],
            relatedBranch: "",
            relatedWorktree: "",
            relatedPR: "",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
        )
    }

    func fetchIntentLocks(sessionID: String, missionID: String) async throws(APIError) -> IntentLocksDTO {
        IntentLocksDTO(intentLocks: [], missionID: missionID)
    }

    func fetchDecisions(sessionID: String, missionID: String) async throws(APIError) -> DecisionsDTO {
        DecisionsDTO(decisions: [], missionID: missionID)
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
            id: "lock-1",
            sessionID: sessionID,
            missionID: missionID,
            rule: rule,
            blockedPaths: blockedPaths,
            allowedPaths: allowedPaths,
            requireProposalForRisk: requireProposalForRisk,
            active: true,
            createdAt: "2026-06-27T06:00:00"
        )
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
            id: "decision-1",
            sessionID: sessionID,
            missionID: missionID,
            kind: kind,
            title: title,
            content: content,
            actor: actor,
            createdAt: "2026-06-27T06:00:00"
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
            missionID: "mission-1",
            taskID: "task-1",
            state: state,
            title: "预览审批",
            detail: "预览详情",
            requester: "Agent-A",
            reviewer: actor,
            decisionNote: decisionNote,
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
        )
    }

    func runValidation(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultDTO {
        ValidationResultDTO(
            id: "run-preview",
            status: "passed",
            exitCode: 0,
            output: "Preview validation passed."
        )
    }
}
#endif
