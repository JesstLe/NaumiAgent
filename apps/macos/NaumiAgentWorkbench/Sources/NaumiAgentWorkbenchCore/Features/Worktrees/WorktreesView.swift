import SwiftUI

/// Worktrees page showing context health snapshots for the selected session.
public struct WorktreesView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController
    private let localActionExecutor: WorktreeLocalActionExecutor
    @State private var selectedSnapshotID: String?
    @State private var selectedWorktreeID: String?
    @State private var isKeepingWorktree = false
    @State private var isRemovingWorktree = false
    @State private var localActionErrorMessage: String?
    private let layout = WorkbenchPageLayout.worktrees

    public init(
        appState: AppState,
        daemonController: DaemonController,
        localActionExecutor: WorktreeLocalActionExecutor = WorktreeLocalActionExecutor()
    ) {
        self.appState = appState
        self.daemonController = daemonController
        self.localActionExecutor = localActionExecutor
    }

    public var body: some View {
        let presentation = WorktreesDashboardPresentation(
            snapshots: appState.contextSnapshots,
            worktrees: appState.worktrees
        )
        let selectedSnapshot = presentation.snapshots.first { $0.id == selectedSnapshotID }
            ?? presentation.selectedSnapshot
        let selectedWorktree = presentation.selectedWorktree(id: selectedWorktreeID)

        VStack(spacing: 0) {
            header(presentation: presentation)
            Divider()

            HStack(spacing: 0) {
                snapshotRail(presentation: presentation)
                    .frame(width: layout.railWidth)
                    .frame(maxHeight: .infinity)
                    .clipped()

                Divider()

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        summaryStrip(presentation: presentation)
                        worktreeTable(presentation: presentation)
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
                        selectedWorktreePanel(worktree: selectedWorktree)
                        if let localActionErrorMessage {
                            localActionErrorCard(message: localActionErrorMessage)
                        }
                        selectedSnapshotPanel(snapshot: selectedSnapshot)
                        remediationPanel(presentation: presentation)
                        if let lastError = appState.lastError {
                            errorCard(error: lastError)
                        }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(width: layout.inspectorWidth)
            }
        }
        .frame(minWidth: 1120, minHeight: 700)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            if selectedSnapshotID == nil {
                selectedSnapshotID = presentation.selectedSnapshot?.id
            }
            if selectedWorktreeID == nil {
                selectedWorktreeID = presentation.selectedWorktree(id: nil)?.id
            }
        }
        .task {
            guard !appState.isPreviewFixture else { return }
            await refreshWorktreesPage()
        }
    }

    private func refreshWorktreesPage() async {
        await daemonController.refreshWorktrees(limit: 50)
        await daemonController.refreshContextSnapshots(limit: 50)
    }

    private func keepWorktree(_ worktree: WorktreeManagementRow) {
        guard worktree.canKeep, !isKeepingWorktree else { return }
        isKeepingWorktree = true
        Task {
            await daemonController.keepWorktree(
                name: worktree.name,
                actor: "Human",
                reason: worktree.defaultKeepReason(locale: appState.locale)
            )
            await daemonController.refreshContextSnapshots(limit: 50)
            isKeepingWorktree = false
        }
    }

    private func removeWorktree(_ worktree: WorktreeManagementRow) {
        guard worktree.canRemoveSafely, !isRemovingWorktree else { return }
        let removedID = worktree.id
        isRemovingWorktree = true
        Task {
            await daemonController.removeWorktree(name: worktree.name, discardChanges: false)
            if appState.lastError == nil,
               selectedWorktreeID == removedID,
               !appState.worktrees.contains(where: { $0.name == removedID }) {
                selectedWorktreeID = appState.worktrees.first?.name
            }
            isRemovingWorktree = false
        }
    }

    private func performLocalAction(_ action: WorktreeLocalAction, worktree: WorktreeManagementRow) {
        do {
            try localActionExecutor.perform(action, path: worktree.path)
            localActionErrorMessage = nil
        } catch let error as WorktreeLocalActionError {
            localActionErrorMessage = error.localizedMessage(locale: appState.locale)
        } catch {
            localActionErrorMessage = error.localizedDescription
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
                        await refreshWorktreesPage()
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
                title: appState.locale == .zhCN ? "工作区总数" : "Worktrees",
                value: "\(presentation.worktreeCount)",
                systemImage: "folder.badge.gearshape"
            )
            metricCard(
                title: appState.locale == .zhCN ? "脏工作区" : "Dirty",
                value: "\(presentation.dirtyWorktreeCount)",
                systemImage: "exclamationmark.triangle",
                tint: .orange
            )
            metricCard(
                title: appState.locale == .zhCN ? "可移除" : "Removable",
                value: "\(presentation.removableWorktreeCount)",
                systemImage: "checkmark.seal",
                tint: .green
            )
            metricCard(
                title: appState.locale == .zhCN ? "已保留" : "Kept",
                value: "\(presentation.keptWorktreeCount)",
                systemImage: "pin"
            )
        }
    }

    private func worktreeTable(presentation: WorktreesDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "Git 工作区表格" : "Git Worktree Table") {
            if presentation.worktreeRows.isEmpty {
                emptyWorktreesState
            } else {
                let effectiveSelectedWorktreeID = presentation.selectedWorktree(id: selectedWorktreeID)?.id
                ScrollView(.horizontal, showsIndicators: false) {
                    VStack(spacing: 0) {
                        worktreeHeaderRow
                        Divider()
                        ForEach(presentation.worktreeRows) { row in
                            worktreeDataRow(row, isSelected: row.id == effectiveSelectedWorktreeID)
                                .contentShape(Rectangle())
                                .onTapGesture {
                                    selectedWorktreeID = row.id
                                }
                            if row.id != presentation.worktreeRows.last?.id {
                                Divider()
                            }
                        }
                    }
                    .frame(minWidth: 746, alignment: .leading)
                }
            }
        }
    }

    private func operationsGrid(presentation: WorktreesDashboardPresentation) -> some View {
        HStack(alignment: .top, spacing: 14) {
            agentWorkloadPanel(presentation: presentation)
                .frame(minWidth: layout.primaryColumnWidth, maxWidth: .infinity, alignment: .top)
            remediationPanel(presentation: presentation)
                .frame(width: layout.secondaryColumnWidth, alignment: .top)
        }
    }

    private var worktreeHeaderRow: some View {
        HStack(spacing: 8) {
            worktreeColumnTitle(appState.locale == .zhCN ? "名称" : "Name", width: 106)
            worktreeColumnTitle(appState.locale == .zhCN ? "任务" : "Task", width: 82)
            worktreeColumnTitle("Agent", width: 108)
            worktreeColumnTitle(appState.locale == .zhCN ? "分支" : "Branch", width: 144)
            worktreeColumnTitle(appState.locale == .zhCN ? "状态" : "Status", width: 74)
            worktreeColumnTitle(appState.locale == .zhCN ? "脏文件" : "Dirty", width: 50)
            worktreeColumnTitle("Ahead", width: 48)
            worktreeColumnTitle(appState.locale == .zhCN ? "可移除" : "Removable", width: 58)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
    }

    private func worktreeDataRow(_ row: WorktreeManagementRow, isSelected: Bool) -> some View {
        HStack(spacing: 8) {
            Text(row.name)
                .font(.system(size: 13, weight: .semibold))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 106, alignment: .leading)
            Text(row.taskID)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 82, alignment: .leading)
            Text(row.agentID)
                .font(.caption)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 108, alignment: .leading)
            Text(row.branch)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 144, alignment: .leading)
            StatusBadge(text: worktreeStatusLabel(row.status), color: color(for: row.statusTone))
                .frame(width: 74, alignment: .leading)
            Text("\(row.dirtyFiles)")
                .font(.caption)
                .fontWeight(row.dirtyFiles > 0 ? .semibold : .regular)
                .foregroundStyle(row.dirtyFiles > 0 ? .orange : .secondary)
                .frame(width: 50, alignment: .leading)
            Text("\(row.commitsAhead)")
                .font(.caption)
                .foregroundStyle(row.commitsAhead > 0 ? Color.accentColor : Color.secondary)
                .frame(width: 48, alignment: .leading)
            Image(systemName: row.removable ? "checkmark.circle.fill" : "lock.fill")
                .foregroundStyle(row.removable ? .green : .secondary)
                .frame(width: 58, alignment: .leading)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 10)
        .background(
            isSelected
                ? Color.accentColor.opacity(0.10)
                : color(for: row.statusTone).opacity(row.statusTone == .normal ? 0.0 : 0.06)
        )
    }

    private func worktreeColumnTitle(_ title: String, width: Double) -> some View {
        Text(title)
            .font(.caption)
            .fontWeight(.semibold)
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .leading)
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

    private func selectedWorktreePanel(worktree: WorktreeManagementRow?) -> some View {
        panel(title: appState.locale == .zhCN ? "工作区详情" : "Worktree Details") {
            if let worktree {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "folder.badge.gearshape")
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundStyle(color(for: worktree.statusTone))
                        VStack(alignment: .leading, spacing: 6) {
                            Text(worktree.name)
                                .font(.system(size: 18, weight: .semibold))
                                .lineLimit(1)
                                .truncationMode(.middle)
                            StatusBadge(text: worktreeStatusLabel(worktree.status), color: color(for: worktree.statusTone))
                        }
                        Spacer(minLength: 0)
                    }

                    detailBlock(label: appState.locale == .zhCN ? "路径" : "Path", value: worktree.path)
                    detailBlock(label: appState.locale == .zhCN ? "分支" : "Branch", value: worktree.branch)
                    twoColumnDetail(
                        leftLabel: AppStrings.Worktrees.taskIDLabel(appState.locale),
                        leftValue: worktree.taskID,
                        rightLabel: AppStrings.Worktrees.agentIDLabel(appState.locale),
                        rightValue: worktree.agentID
                    )
                    twoColumnDetail(
                        leftLabel: appState.locale == .zhCN ? "脏文件" : "Dirty Files",
                        leftValue: "\(worktree.dirtyFiles)",
                        rightLabel: appState.locale == .zhCN ? "领先提交" : "Commits Ahead",
                        rightValue: "\(worktree.commitsAhead)"
                    )

                    VStack(alignment: .leading, spacing: 8) {
                        Text(appState.locale == .zhCN ? "动作" : "Actions")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        HStack(spacing: 8) {
                            Button {
                                performLocalAction(.revealInFinder, worktree: worktree)
                            } label: {
                                Label(appState.locale == .zhCN ? "Finder" : "Finder", systemImage: "folder")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)

                            Button {
                                performLocalAction(.openTerminal, worktree: worktree)
                            } label: {
                                Label(appState.locale == .zhCN ? "终端" : "Terminal", systemImage: "terminal")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                        }

                        Button {
                            keepWorktree(worktree)
                        } label: {
                            Label(
                                isKeepingWorktree
                                    ? (appState.locale == .zhCN ? "保留中" : "Keeping")
                                    : (appState.locale == .zhCN ? "保留工作区" : "Keep Worktree"),
                                systemImage: "pin"
                            )
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(!worktree.canKeep || isKeepingWorktree)

                        if let disabledReason = worktree.keepDisabledReason(locale: appState.locale) {
                            Text(disabledReason)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            Text(worktree.defaultKeepReason(locale: appState.locale))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }

                        Button {
                            removeWorktree(worktree)
                        } label: {
                            Label(
                                isRemovingWorktree
                                    ? (appState.locale == .zhCN ? "删除中" : "Removing")
                                    : (appState.locale == .zhCN ? "删除工作区" : "Remove Worktree"),
                                systemImage: "trash"
                            )
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .tint(.red)
                        .disabled(!worktree.canRemoveSafely || isRemovingWorktree)

                        if let removeDisabledReason = worktree.removeDisabledReason(locale: appState.locale) {
                            Text(removeDisabledReason)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            } else {
                emptyWorktreesState
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

    private func localActionErrorCard(message: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(appState.locale == .zhCN ? "本地操作失败" : "Local Action Failed")
                .font(.headline)
                .foregroundStyle(.red)
            Text(message)
                .font(.body)
                .foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
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

    private var emptyWorktreesState: some View {
        VStack(spacing: 8) {
            Image(systemName: "folder.badge.questionmark")
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
            Text(appState.locale == .zhCN ? "暂无 Git 工作区" : "No Git worktrees")
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 26)
    }

    private func subtitleText(presentation: WorktreesDashboardPresentation) -> String {
        let worktreeCount = appState.locale == .zhCN
            ? "\(presentation.worktreeCount) 个工作区"
            : "\(presentation.worktreeCount) worktrees"
        let snapshotCount = AppStrings.Worktrees.snapshotCount(appState.locale, count: presentation.totalCount)
        guard let sessionID = appState.selectedSessionID else { return "\(worktreeCount) · \(snapshotCount)" }
        return "\(worktreeCount) · \(snapshotCount) · \(sessionID)"
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

    private func color(for tone: WorktreeStatusTone) -> Color {
        switch tone {
        case .normal:
            return .green
        case .warning:
            return .orange
        case .kept:
            return .accentColor
        case .blocked:
            return .red
        }
    }

    private func worktreeStatusLabel(_ status: String) -> String {
        switch status.lowercased() {
        case "clean":
            return appState.locale == .zhCN ? "干净" : "Clean"
        case "dirty":
            return appState.locale == .zhCN ? "有变更" : "Dirty"
        case "kept":
            return appState.locale == .zhCN ? "保留" : "Kept"
        case "missing":
            return appState.locale == .zhCN ? "缺失" : "Missing"
        default:
            return status
        }
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
        state.worktrees = [
            WorktreeDTO(
                name: "wt-api-client",
                path: "/repo/.naumi/worktrees/wt-api-client",
                branch: "naumi/wt-api-client",
                baseRef: "main",
                status: "clean",
                taskID: "task-1",
                dirtyFiles: 0,
                commitsAhead: 1,
                createdAt: "2026-06-27T06:00:00",
                updatedAt: "2026-06-27T06:12:00",
                keptReason: "",
                metadata: ["agent_id": "Backend-Agent"],
                removable: true
            ),
            WorktreeDTO(
                name: "wt-review-risk",
                path: "/repo/.naumi/worktrees/wt-review-risk",
                branch: "naumi/wt-review-risk",
                baseRef: "main",
                status: "dirty",
                taskID: "task-2",
                dirtyFiles: 4,
                commitsAhead: 2,
                createdAt: "2026-06-27T06:03:00",
                updatedAt: "2026-06-27T06:18:00",
                keptReason: "",
                metadata: ["agent_id": "Reviewer-Agent"],
                removable: false
            ),
            WorktreeDTO(
                name: "wt-validation-card",
                path: "/repo/.naumi/worktrees/wt-validation-card",
                branch: "naumi/wt-validation-card",
                baseRef: "main",
                status: "kept",
                taskID: "task-3",
                dirtyFiles: 0,
                commitsAhead: 0,
                createdAt: "2026-06-27T06:08:00",
                updatedAt: "2026-06-27T06:20:00",
                keptReason: "等待人工审查",
                metadata: ["agent_id": "Test-Agent"],
                removable: false
            ),
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
