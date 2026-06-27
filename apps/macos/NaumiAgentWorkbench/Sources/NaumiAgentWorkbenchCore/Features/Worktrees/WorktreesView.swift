import SwiftUI

/// Worktrees page showing context health snapshots for the selected session.
public struct WorktreesView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController
    @State private var selectedSnapshotID: String?

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = WorktreesDashboardPresentation(snapshots: appState.contextSnapshots)
        let selectedSnapshot = presentation.snapshots.first { $0.id == selectedSnapshotID }
            ?? presentation.selectedSnapshot

        VStack(spacing: 0) {
            header(presentation: presentation)
            Divider()

            HStack(spacing: 0) {
                snapshotRail(presentation: presentation)
                    .frame(width: 304)
                    .frame(maxHeight: .infinity)
                    .clipped()

                Divider()

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        summaryStrip(presentation: presentation)
                        operationsGrid(presentation: presentation)
                        healthDistributionPanel(presentation: presentation)
                    }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                Divider()

                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        selectedSnapshotPanel(snapshot: selectedSnapshot)
                        remediationPanel(presentation: presentation)
                        if let lastError = appState.lastError {
                            errorCard(error: lastError)
                        }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(width: 334)
            }
        }
        .frame(minWidth: 1120, minHeight: 700)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            if selectedSnapshotID == nil {
                selectedSnapshotID = presentation.selectedSnapshot?.id
            }
        }
        .task {
            guard !appState.isPreviewFixture else { return }
            await daemonController.refreshContextSnapshots(limit: 50)
        }
    }

    private func header(presentation: WorktreesDashboardPresentation) -> some View {
        HStack(alignment: .center, spacing: 16) {
            Text(AppStrings.Worktrees.title(appState.locale))
                .font(.system(size: 17, weight: .semibold))
            Text("\(AppStrings.Worktrees.subtitle(appState.locale)) · \(subtitleText(presentation: presentation))")
                .font(.caption)
                .foregroundStyle(.secondary)

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
        .padding(.horizontal, 18)
        .padding(.vertical, 11)
    }

    private func snapshotRail(presentation: WorktreesDashboardPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label(appState.locale == .zhCN ? "工作区队列" : "Worktree Queue", systemImage: "folder.badge.gearshape")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Text("\(presentation.totalCount)")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.10))
                    .clipShape(Capsule())
            }

            HStack(spacing: 8) {
                Label(appState.locale == .zhCN ? "需关注" : "Attention", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                Spacer()
                Text("\(presentation.attentionCount)")
                    .fontWeight(.semibold)
            }
            .font(.caption)
            .padding(10)
            .background(Color.orange.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 8))

            ScrollView {
                VStack(spacing: 10) {
                    if presentation.snapshots.isEmpty {
                        emptyState
                    } else {
                        ForEach(presentation.snapshots) { snapshot in
                            snapshotRow(snapshot: snapshot, isSelected: snapshot.id == selectedSnapshotID)
                                .contentShape(Rectangle())
                                .onTapGesture {
                                    selectedSnapshotID = snapshot.id
                                }
                        }
                    }
                }
            }
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
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

    private func operationsGrid(presentation: WorktreesDashboardPresentation) -> some View {
        HStack(alignment: .top, spacing: 14) {
            agentWorkloadPanel(presentation: presentation)
                .frame(minWidth: 420, maxWidth: .infinity, alignment: .top)
            remediationPanel(presentation: presentation)
                .frame(width: 420, alignment: .top)
        }
    }

    private func agentWorkloadPanel(presentation: WorktreesDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "Agent / Worktree 负载" : "Agent / Worktree Load") {
            if presentation.agentBuckets.isEmpty {
                emptyState
            } else {
                VStack(spacing: 10) {
                    ForEach(presentation.agentBuckets) { bucket in
                        HStack(spacing: 12) {
                            Image(systemName: "person.crop.circle.badge.gearshape")
                                .foregroundStyle(color(for: bucket.worstHealth))
                                .frame(width: 24)
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    Text(bucket.agentID)
                                        .font(.system(size: 14, weight: .semibold))
                                    Spacer()
                                    StatusBadge(
                                        text: healthLabel(bucket.worstHealth),
                                        color: color(for: bucket.worstHealth)
                                    )
                                }
                                HStack(spacing: 18) {
                                    compactDetail(
                                        label: appState.locale == .zhCN ? "快照" : "Snapshots",
                                        value: "\(bucket.snapshotCount)"
                                    )
                                    compactDetail(
                                        label: appState.locale == .zhCN ? "需关注" : "Attention",
                                        value: "\(bucket.attentionCount)"
                                    )
                                }
                            }
                        }
                        .padding(12)
                        .background(Color(nsColor: .controlBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                }
            }
        }
    }

    private func remediationPanel(presentation: WorktreesDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "建议处置" : "Recommended Actions") {
            if presentation.recommendedActions.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Label(
                        appState.locale == .zhCN ? "当前无需人工介入" : "No manual action needed",
                        systemImage: "checkmark.seal"
                    )
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(.green)
                    Text(appState.locale == .zhCN ? "所有工作区上下文处于可继续执行状态。" : "All worktree contexts are safe to continue.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.green.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                VStack(spacing: 10) {
                    ForEach(presentation.recommendedActions) { action in
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: action.systemImage)
                                .foregroundStyle(.orange)
                                .frame(width: 22)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(action.title(locale: appState.locale))
                                    .font(.system(size: 14, weight: .semibold))
                                Text(action.detail(locale: appState.locale))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                        }
                        .padding(12)
                        .background(Color.orange.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                }
            }
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

                    detailBlock(
                        label: AppStrings.Worktrees.agentIDLabel(appState.locale),
                        value: snapshot.agentID
                    )
                    detailBlock(
                        label: AppStrings.Worktrees.createdAtLabel(appState.locale),
                        value: snapshot.createdAt
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
                        .truncationMode(.middle)
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
                .truncationMode(.middle)
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

#endif
