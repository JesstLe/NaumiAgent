import SwiftUI

/// Worktrees page showing context health snapshots for the selected session.
///
/// Snapshots are loaded from `GET /workbench/sessions/{id}/context-snapshots`
/// and never fabricated locally. The view refreshes automatically on appear and
/// exposes a manual refresh button in the toolbar.
public struct WorktreesView: View {
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
                snapshotList
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(AppStrings.Worktrees.title(appState.locale))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: {
                    Task {
                        await daemonController.refreshContextSnapshots(limit: 50)
                    }
                }) {
                    Label(
                        AppStrings.Worktrees.refreshButton(appState.locale),
                        systemImage: "arrow.clockwise"
                    )
                }
            }
        }
        .task {
            await daemonController.refreshContextSnapshots(limit: 50)
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.Worktrees.title(appState.locale))
                .font(.largeTitle)
                .fontWeight(.bold)

            HStack(spacing: 12) {
                Text(
                    AppStrings.Worktrees.snapshotCount(
                        appState.locale,
                        count: appState.contextSnapshots.count
                    )
                )
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

    // MARK: - Snapshot List

    @ViewBuilder
    private var snapshotList: some View {
        if appState.contextSnapshots.isEmpty {
            emptyState
        } else {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(presentedSnapshots) { snapshot in
                    snapshotRow(snapshot: snapshot)
                    if snapshot.id != presentedSnapshots.last?.id {
                        Divider()
                    }
                }
            }
            .padding()
            .background(Color.secondary.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }

    private var presentedSnapshots: [ContextSnapshotPresentation] {
        appState.contextSnapshots.map(ContextSnapshotPresentation.init)
    }

    private func snapshotRow(snapshot: ContextSnapshotPresentation) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                StatusBadge(
                    text: snapshot.healthLabel(locale: appState.locale),
                    color: snapshot.healthColor()
                )
                Spacer()
                Text(snapshot.createdAt)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Worktrees.taskIDLabel(appState.locale),
                    value: snapshot.taskID
                )
                detailItem(
                    label: AppStrings.Worktrees.agentIDLabel(appState.locale),
                    value: snapshot.agentID
                )
            }

            detailItem(
                label: AppStrings.Worktrees.reasonsLabel(appState.locale),
                value: snapshot.reasonsSummary(locale: appState.locale)
            )
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
                Image(systemName: "doc.text.magnifyingglass")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text(AppStrings.Worktrees.emptySnapshots(appState.locale))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Spacer()
        }
        .padding(.vertical, 32)
    }
}

#if DEBUG
struct WorktreesView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        state.locale = .zhCN
        state.selectedSessionID = "sess-preview"
        state.contextSnapshots = [
            ContextSnapshotDTO(
                id: "snap-1",
                sessionID: "sess-preview",
                agentID: "agent-a",
                taskID: "task-1",
                health: "good",
                reasons: ["上下文健康"],
                createdAt: "2026-06-27T06:00:00"
            ),
            ContextSnapshotDTO(
                id: "snap-2",
                sessionID: "sess-preview",
                agentID: "agent-b",
                taskID: "task-2",
                health: "stale",
                reasons: ["长时间未更新", "依赖文件已变更"],
                createdAt: "2026-06-27T06:05:00"
            ),
            ContextSnapshotDTO(
                id: "snap-3",
                sessionID: "sess-preview",
                agentID: "agent-c",
                taskID: "task-3",
                health: "conflicted",
                reasons: ["与主分支冲突"],
                createdAt: "2026-06-27T06:10:00"
            )
        ]
        return WorktreesView(
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

    func fetchEvents(sessionID: String, limit: Int) async throws(APIError) -> WorkbenchEventsDTO {
        WorkbenchEventsDTO(events: [], limit: limit)
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
