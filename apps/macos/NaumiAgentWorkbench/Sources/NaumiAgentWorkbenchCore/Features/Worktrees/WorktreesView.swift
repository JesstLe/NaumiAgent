import SwiftUI

/// Worktrees page showing context health snapshots for the selected session.
public struct WorktreesView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = WorktreesDashboardPresentation(snapshots: appState.contextSnapshots)

        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header(presentation: presentation)
                if let lastError = appState.lastError {
                    errorCard(error: lastError)
                }
                summaryStrip(presentation: presentation)
                dashboardGrid(presentation: presentation)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            guard !appState.isPreviewFixture else { return }
            await daemonController.refreshContextSnapshots(limit: 50)
        }
    }

    private func header(presentation: WorktreesDashboardPresentation) -> some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Text(AppStrings.Worktrees.title(appState.locale))
                    .font(.system(size: 22, weight: .semibold))
                Text(subtitleText(presentation: presentation))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Button {
                if !appState.isPreviewFixture {
                    Task {
                        await daemonController.refreshContextSnapshots(limit: 50)
                    }
                }
            } label: {
                Label(AppStrings.Worktrees.refreshButton(appState.locale), systemImage: "arrow.clockwise")
            }
            .buttonStyle(.bordered)
        }
    }

    private func summaryStrip(presentation: WorktreesDashboardPresentation) -> some View {
        HStack(spacing: 12) {
            metricCard(
                title: appState.locale == .zhCN ? "快照总数" : "Snapshots",
                value: "\(presentation.totalCount)",
                systemImage: "square.stack.3d.up"
            )
            metricCard(
                title: appState.locale == .zhCN ? "需要关注" : "Needs Attention",
                value: "\(presentation.attentionCount)",
                systemImage: "exclamationmark.triangle",
                tint: .orange
            )
            metricCard(
                title: appState.locale == .zhCN ? "健康工作区" : "Healthy",
                value: "\(presentation.goodCount)",
                systemImage: "checkmark.seal",
                tint: .green
            )
            metricCard(
                title: appState.locale == .zhCN ? "活跃智能体" : "Active Agents",
                value: "\(presentation.activeAgentCount)",
                systemImage: "person.2"
            )
        }
    }

    private func dashboardGrid(presentation: WorktreesDashboardPresentation) -> some View {
        HStack(alignment: .top, spacing: 14) {
            panel(title: appState.locale == .zhCN ? "工作区快照" : "Worktree Snapshots") {
                if presentation.snapshots.isEmpty {
                    emptyState
                } else {
                    VStack(spacing: 10) {
                        ForEach(presentation.snapshots) { snapshot in
                            snapshotRow(snapshot: snapshot, isSelected: snapshot.id == presentation.selectedSnapshot?.id)
                        }
                    }
                }
            }
            .frame(minWidth: 360, maxWidth: .infinity, alignment: .top)

            VStack(spacing: 14) {
                selectedSnapshotPanel(snapshot: presentation.selectedSnapshot)
                healthDistributionPanel(presentation: presentation)
            }
            .frame(width: 340, alignment: .top)
        }
    }

    private func selectedSnapshotPanel(snapshot: ContextSnapshotPresentation?) -> some View {
        panel(title: appState.locale == .zhCN ? "当前风险焦点" : "Current Risk Focus") {
            if let snapshot {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 10) {
                        Image(systemName: iconName(for: snapshot.health))
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundStyle(snapshot.healthColor())
                        StatusBadge(text: snapshot.healthLabel(locale: appState.locale), color: snapshot.healthColor())
                    }

                    Text(snapshot.taskID)
                        .font(.system(size: 20, weight: .semibold))
                        .lineLimit(1)

                    twoColumnDetail(
                        leftLabel: AppStrings.Worktrees.agentIDLabel(appState.locale),
                        leftValue: snapshot.agentID,
                        rightLabel: AppStrings.Worktrees.createdAtLabel(appState.locale),
                        rightValue: snapshot.createdAt
                    )

                    detailBlock(
                        label: AppStrings.Worktrees.reasonsLabel(appState.locale),
                        value: snapshot.reasonsSummary(locale: appState.locale)
                    )
                }
            } else {
                emptyState
            }
        }
    }

    private func healthDistributionPanel(presentation: WorktreesDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "健康分布" : "Health Distribution") {
            if presentation.healthBuckets.isEmpty {
                emptyState
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(presentation.healthBuckets) { bucket in
                        HStack(spacing: 10) {
                            Circle()
                                .fill(color(for: bucket.health))
                                .frame(width: 8, height: 8)
                            Text(healthLabel(bucket.health))
                                .font(.system(size: 13, weight: .medium))
                            Spacer()
                            Text("\(bucket.count)")
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 6)
                    }
                }
            }
        }
    }

    private func snapshotRow(snapshot: ContextSnapshotPresentation, isSelected: Bool) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: iconName(for: snapshot.health))
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(snapshot.healthColor())
                .frame(width: 20)

            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(snapshot.taskID)
                        .font(.system(size: 14, weight: .semibold))
                        .lineLimit(1)
                    StatusBadge(text: snapshot.healthLabel(locale: appState.locale), color: snapshot.healthColor())
                    Spacer()
                    Text(snapshot.createdAt)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                HStack(spacing: 18) {
                    compactDetail(label: AppStrings.Worktrees.agentIDLabel(appState.locale), value: snapshot.agentID)
                    compactDetail(label: AppStrings.Worktrees.reasonsLabel(appState.locale), value: snapshot.reasonsSummary(locale: appState.locale))
                }
            }
        }
        .padding(12)
        .background(isSelected ? Color.accentColor.opacity(0.10) : Color(nsColor: .controlBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(isSelected ? Color.accentColor.opacity(0.65) : Color.secondary.opacity(0.13), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func metricCard(title: String, value: String, systemImage: String, tint: Color = .accentColor) -> some View {
        HStack(spacing: 12) {
            Image(systemName: systemImage)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 28, height: 28)
                .background(tint.opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(value)
                    .font(.system(size: 19, weight: .semibold))
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(height: 74)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func panel<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.system(size: 14, weight: .semibold))
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Color.secondary.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func twoColumnDetail(
        leftLabel: String,
        leftValue: String,
        rightLabel: String,
        rightValue: String
    ) -> some View {
        HStack(spacing: 18) {
            detailBlock(label: leftLabel, value: leftValue)
            detailBlock(label: rightLabel, value: rightValue)
        }
    }

    private func compactDetail(label: String, value: String) -> some View {
        HStack(spacing: 5) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption)
                .fontWeight(.medium)
                .lineLimit(1)
        }
    }

    private func detailBlock(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "doc.text.magnifyingglass")
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
            Text(AppStrings.Worktrees.emptySnapshots(appState.locale))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 26)
    }

    private func subtitleText(presentation: WorktreesDashboardPresentation) -> String {
        let count = AppStrings.Worktrees.snapshotCount(appState.locale, count: presentation.totalCount)
        guard let sessionID = appState.selectedSessionID else { return count }
        return "\(count) · \(sessionID)"
    }

    private func healthLabel(_ health: String) -> String {
        ContextSnapshotPresentation(
            snapshot: ContextSnapshotDTO(
                id: health,
                sessionID: "-",
                agentID: "-",
                taskID: "-",
                health: health,
                reasons: [],
                createdAt: "-"
            )
        )
        .healthLabel(locale: appState.locale)
    }

    private func color(for health: String) -> Color {
        ContextSnapshotPresentation(
            snapshot: ContextSnapshotDTO(
                id: health,
                sessionID: "-",
                agentID: "-",
                taskID: "-",
                health: health,
                reasons: [],
                createdAt: "-"
            )
        )
        .healthColor()
    }

    private func iconName(for health: String) -> String {
        switch health.lowercased() {
        case "good":
            return "checkmark.circle"
        case "conflicted":
            return "xmark.octagon"
        case "stale":
            return "clock.badge.exclamationmark"
        case "overloaded":
            return "speedometer"
        case "missing":
            return "questionmark.folder"
        default:
            return "waveform.path.ecg"
        }
    }
}

#if NAUMI_WORKBENCH_LOCAL_PREVIEWS
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
        .frame(minWidth: 900, minHeight: 560)
    }
}

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
        WorkbenchSnapshotDTO(sessionID: sessionID, missions: [], tasks: [], issues: [], failures: [], events: [])
    }

    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        SessionListDTO(sessions: [], total: 0, page: page, pageSize: pageSize)
    }

    func createSession(title: String?, model: String?, systemPrompt: String?) async throws(APIError) -> SessionDTO {
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

    func fetchValidationRuns(sessionID: String, taskID: String?, limit: Int) async throws(APIError) -> ValidationRunsDTO {
        ValidationRunsDTO(validationRuns: [], taskID: taskID, limit: limit)
    }

    func fetchContextSnapshots(sessionID: String, limit: Int) async throws(APIError) -> ContextSnapshotsDTO {
        ContextSnapshotsDTO(snapshots: [], limit: limit)
    }

    func fetchApprovals(status: String?, limit: Int) async throws(APIError) -> ApprovalsDTO {
        ApprovalsDTO(approvals: [], status: status, limit: limit)
    }

    func fetchAgentProfiles(sessionID: String?, status: String?, limit: Int) async throws(APIError) -> AgentProfilesDTO {
        AgentProfilesDTO(agents: [], status: status, limit: limit)
    }

    func createMission(sessionID: String, title: String, goal: String) async throws(APIError) -> MissionDTO {
        MissionDTO(
            id: "preview-mission",
            sessionID: sessionID,
            title: title,
            goal: goal,
            status: "active",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
        )
    }

    func createIntentLock(
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async throws(APIError) -> IntentLockDTO {
        IntentLockDTO(
            id: "preview-lock",
            missionID: missionID,
            actor: actor,
            rule: rule,
            blockedPaths: blockedPaths,
            allowedPaths: allowedPaths,
            requireProposalForRisk: requireProposalForRisk,
            createdAt: "2026-06-27T06:00:00"
        )
    }

    func createApprovalDecision(
        approvalID: String,
        decision: String,
        reviewer: String,
        note: String?
    ) async throws(APIError) -> DecisionDTO {
        DecisionDTO(
            id: "preview-decision",
            approvalID: approvalID,
            decision: decision,
            reviewer: reviewer,
            note: note,
            createdAt: "2026-06-27T06:00:00"
        )
    }

    func runValidation(
        sessionID: String,
        taskID: String,
        command: [String],
        cwd: String?,
        actor: String
    ) async throws(APIError) -> ValidationRunDTO {
        ValidationRunDTO(
            id: "preview-run",
            sessionID: sessionID,
            taskID: taskID,
            command: command,
            cwd: cwd,
            status: "passed",
            exitCode: 0,
            outputSummary: "preview",
            actor: actor,
            completedAt: "2026-06-27T06:00:00"
        )
    }
}
#endif
