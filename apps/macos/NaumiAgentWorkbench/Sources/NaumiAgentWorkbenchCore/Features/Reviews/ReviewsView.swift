import SwiftUI

/// Reviews page showing validation runs and pending approvals for the selected session.
///
/// Runs are loaded from `GET /workbench/sessions/{id}/validation-runs` and approvals
/// from `GET /workbench/sessions/{id}/approvals?state=waiting`. Neither list is ever
/// fabricated locally. The view refreshes automatically on appear and exposes a
/// manual refresh button in the toolbar.
public struct ReviewsView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    @State private var validationDraft = ValidationRunDraft()
    @State private var approvalDrafts: [String: ApprovalResolveDraft] = [:]
    @State private var resolvingApprovalIDs: Set<String> = []
    @State private var isProcessing: Bool = false

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
                approvalsSection
                runValidationForm
                runList
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(AppStrings.Reviews.title(appState.locale))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: refreshAll) {
                    Label(
                        AppStrings.Reviews.refreshButton(appState.locale),
                        systemImage: "arrow.clockwise"
                    )
                }
            }
        }
        .task {
            syncApprovalDrafts(with: appState.approvals)
            await refreshBoth()
        }
        .onChange(of: appState.approvals) { _, newApprovals in
            syncApprovalDrafts(with: newApprovals)
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.Reviews.title(appState.locale))
                .font(.largeTitle)
                .fontWeight(.bold)

            HStack(spacing: 12) {
                Text(AppStrings.Reviews.runCount(appState.locale, count: appState.validationRuns.count))
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
    }

    // MARK: - Pending Approvals

    private var approvalsSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(AppStrings.Reviews.pendingApprovalsSectionTitle(appState.locale))
                    .font(.headline)
                Spacer()
                Text(AppStrings.Reviews.approvalCount(appState.locale, count: appState.approvals.count))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if appState.approvals.isEmpty {
                emptyApprovalsState
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(appState.approvals, id: \.id) { approval in
                        approvalRow(approval: approval)
                        if approval.id != appState.approvals.last?.id {
                            Divider()
                        }
                    }
                }
                .padding()
                .background(Color.secondary.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
        }
    }

    private func approvalRow(approval: ApprovalDTO) -> some View {
        let draftBinding = binding(for: approval.id)
        let draft = draftBinding.wrappedValue
        let isResolving = resolvingApprovalIDs.contains(approval.id)
        let canResolve = draft.canResolve && !isResolving

        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Text(approval.title)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                StatusBadge(
                    text: approval.state,
                    color: approvalStatusColor(for: approval.state)
                )
            }

            if !approval.detail.isEmpty {
                detailItem(
                    label: AppStrings.Reviews.detailLabel(appState.locale),
                    value: approval.detail
                )
            }

            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Reviews.taskIDLabel(appState.locale),
                    value: approval.taskID
                )
                detailItem(
                    label: AppStrings.Reviews.requesterLabel(appState.locale),
                    value: approval.requester
                )
            }

            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Reviews.createdAtLabel(appState.locale),
                    value: approval.createdAt
                )
                detailItem(
                    label: AppStrings.Reviews.updatedAtLabel(appState.locale),
                    value: approval.updatedAt
                )
            }

            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(AppStrings.Reviews.actorLabel(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField("", text: draftBinding.actor)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel(AppStrings.Reviews.actorLabel(appState.locale))
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text(AppStrings.Reviews.decisionNoteLabel(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField("", text: draftBinding.decisionNote)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel(AppStrings.Reviews.decisionNoteLabel(appState.locale))
                }
            }

            HStack {
                Spacer()
                Button(action: { resolveApproval(approval: approval, state: "rejected") }) {
                    if isResolving {
                        Label(
                            AppStrings.Reviews.processingLabel(appState.locale),
                            systemImage: "arrow.triangle.2.circlepath"
                        )
                    } else {
                        Label(
                            AppStrings.Reviews.rejectButton(appState.locale),
                            systemImage: "xmark.circle"
                        )
                    }
                }
                .disabled(!canResolve)

                Button(action: { resolveApproval(approval: approval, state: "approved") }) {
                    if isResolving {
                        Label(
                            AppStrings.Reviews.processingLabel(appState.locale),
                            systemImage: "arrow.triangle.2.circlepath"
                        )
                    } else {
                        Label(
                            AppStrings.Reviews.approveButton(appState.locale),
                            systemImage: "checkmark.circle"
                        )
                    }
                }
                .disabled(!canResolve)
                .buttonStyle(.borderedProminent)
            }
        }
    }

    private func binding(for approvalID: String) -> Binding<ApprovalResolveDraft> {
        Binding(
            get: { approvalDrafts[approvalID] ?? ApprovalResolveDraft() },
            set: { approvalDrafts[approvalID] = $0 }
        )
    }

    private func syncApprovalDrafts(with approvals: [ApprovalDTO]) {
        let ids = Set(approvals.map(\.id))
        var updated = approvalDrafts.filter { ids.contains($0.key) }
        for approval in approvals where updated[approval.id] == nil {
            updated[approval.id] = ApprovalResolveDraft()
        }
        approvalDrafts = updated
    }

    private func resolveApproval(approval: ApprovalDTO, state: String) {
        let draft = approvalDrafts[approval.id] ?? ApprovalResolveDraft()
        guard draft.canResolve else { return }

        resolvingApprovalIDs.insert(approval.id)
        Task { @MainActor in
            await daemonController.resolveApproval(
                approvalID: approval.id,
                actor: draft.trimmedActor,
                state: state,
                decisionNote: draft.trimmedDecisionNote
            )
            resolvingApprovalIDs.remove(approval.id)
        }
    }

    private var emptyApprovalsState: some View {
        HStack {
            Spacer()
            VStack(spacing: 8) {
                Image(systemName: "checkmark.shield")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text(AppStrings.Reviews.emptyApprovals(appState.locale))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Spacer()
        }
        .padding(.vertical, 32)
    }

    // MARK: - Run Validation Form

    private var runValidationForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.Reviews.runValidationSectionTitle(appState.locale))
                .font(.headline)

            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(AppStrings.Reviews.taskIDLabel(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField("", text: $validationDraft.taskID)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel(AppStrings.Reviews.taskIDLabel(appState.locale))
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text(AppStrings.Reviews.actorLabel(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField("", text: $validationDraft.actor)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel(AppStrings.Reviews.actorLabel(appState.locale))
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(AppStrings.Reviews.commandLabel(appState.locale))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("", text: $validationDraft.commandLine)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel(AppStrings.Reviews.commandLabel(appState.locale))
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(AppStrings.Reviews.cwdLabel(appState.locale))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("", text: $validationDraft.cwd)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel(AppStrings.Reviews.cwdLabel(appState.locale))
            }

            HStack {
                Spacer()
                Button(action: submitRunValidation) {
                    if isProcessing {
                        Label(
                            AppStrings.Reviews.processingLabel(appState.locale),
                            systemImage: "arrow.triangle.2.circlepath"
                        )
                    } else {
                        Label(
                            AppStrings.Reviews.runButton(appState.locale),
                            systemImage: "play.circle"
                        )
                    }
                }
                .disabled(!canSubmitRunValidation)
                .buttonStyle(.borderedProminent)
            }
        }
        .padding()
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var canSubmitRunValidation: Bool {
        !isProcessing && validationDraft.canSubmit
    }

    private func submitRunValidation() {
        let draft = validationDraft

        guard draft.canSubmit else { return }

        isProcessing = true
        Task { @MainActor in
            await daemonController.runValidation(
                taskID: draft.trimmedTaskID,
                actor: draft.trimmedActor,
                argv: draft.argv,
                cwd: draft.normalizedCWD
            )
            isProcessing = false
        }
    }

    // MARK: - Run List

    @ViewBuilder
    private var runList: some View {
        if appState.validationRuns.isEmpty {
            emptyState
        } else {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(presentedRuns) { run in
                    runRow(run: run)
                    if run.id != presentedRuns.last?.id {
                        Divider()
                    }
                }
            }
            .padding()
            .background(Color.secondary.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }

    private var presentedRuns: [ValidationRunPresentation] {
        appState.validationRuns.map(ValidationRunPresentation.init)
    }

    private func runRow(run: ValidationRunPresentation) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                StatusBadge(
                    text: run.statusLabel(locale: appState.locale),
                    color: statusColor(for: run.status)
                )
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(AppStrings.Reviews.completedAtLabel(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(run.completedAt)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Reviews.taskIDLabel(appState.locale),
                    value: run.taskID
                )
                detailItem(
                    label: AppStrings.Reviews.actorLabel(appState.locale),
                    value: run.actor
                )
                detailItem(
                    label: AppStrings.Reviews.exitCodeLabel(appState.locale),
                    value: "\(run.exitCode)"
                )
            }

            detailItem(
                label: AppStrings.Reviews.commandLabel(appState.locale),
                value: run.commandLine
            )

            detailItem(
                label: AppStrings.Reviews.cwdLabel(appState.locale),
                value: run.cwd
            )

            if !run.outputSummary.isEmpty {
                detailItem(
                    label: AppStrings.Reviews.outputLabel(appState.locale),
                    value: run.outputSummary
                )
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
                .lineLimit(2)
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
                Image(systemName: "checkmark.shield")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text(AppStrings.Reviews.emptyRuns(appState.locale))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Spacer()
        }
        .padding(.vertical, 32)
    }

    // MARK: - Helpers

    private func refreshAll() {
        Task {
            await refreshBoth()
        }
    }

    private func refreshBoth() async {
        await daemonController.refreshValidationRuns(limit: 50)
        await daemonController.refreshApprovals(state: "waiting")
    }

    private func statusColor(for status: String) -> Color {
        switch status.lowercased() {
        case "passed":
            return .green
        case "failed":
            return .red
        default:
            return .secondary
        }
    }

    private func approvalStatusColor(for state: String) -> Color {
        switch state.lowercased() {
        case "approved":
            return .green
        case "rejected":
            return .red
        case "waiting":
            return .orange
        default:
            return .secondary
        }
    }
}

#if DEBUG
struct ReviewsView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        state.locale = .zhCN
        state.selectedSessionID = "sess-preview"
        state.validationRuns = [
            ValidationRunDTO(
                id: "run-1",
                sessionID: "sess-preview",
                taskID: "task-1",
                actor: "ValidationRunner",
                command: ["pytest", "tests/unit"],
                cwd: "/workspace",
                status: "passed",
                exitCode: 0,
                output: "All tests passed.\n\n",
                startedAt: "2026-06-27T06:00:00",
                completedAt: "2026-06-27T06:00:05"
            ),
            ValidationRunDTO(
                id: "run-2",
                sessionID: "sess-preview",
                taskID: "task-2",
                actor: "ValidationRunner",
                command: ["ruff", "check", "src"],
                cwd: "/workspace",
                status: "failed",
                exitCode: 1,
                output: "E501 line too long\n\nE502 another error",
                startedAt: "2026-06-27T06:01:00",
                completedAt: "2026-06-27T06:01:02"
            )
        ]
        state.approvals = [
            ApprovalDTO(
                id: "approval-1",
                sessionID: "sess-preview",
                missionID: "mission-1",
                taskID: "task-1",
                state: "waiting",
                title: "请求执行高风险操作",
                detail: "需要人工确认后继续执行",
                requester: "Agent-A",
                reviewer: "",
                decisionNote: "",
                createdAt: "2026-06-27T06:00:00",
                updatedAt: "2026-06-27T06:00:01"
            )
        ]
        return ReviewsView(
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
