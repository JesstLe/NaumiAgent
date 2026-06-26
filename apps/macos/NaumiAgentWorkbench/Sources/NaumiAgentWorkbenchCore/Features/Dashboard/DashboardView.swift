import SwiftUI

/// Minimal usable dashboard homepage.
/// Displays connection state, daemon/version, counts and the current snapshot content.
public struct DashboardView: View {
    @Bindable public var appState: AppState

    public init(appState: AppState) {
        self.appState = appState
    }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                if let status = appState.daemonStatus {
                    daemonCard(status: status)
                }
                countsGrid
                if let snapshot = appState.snapshot {
                    snapshotContent(snapshot: snapshot)
                }
                if let lastError = appState.lastError {
                    errorCard(error: lastError)
                }
                if appState.snapshot == nil {
                    emptyState
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(AppStrings.Dashboard.title(appState.locale))
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.Dashboard.title(appState.locale))
                .font(.largeTitle)
                .fontWeight(.bold)

            HStack(spacing: 12) {
                StatusBadge(
                    text: appState.connectionState.displayName(locale: appState.locale),
                    color: connectionColor
                )
                if let daemon = appState.daemonStatus {
                    StatusBadge(
                        text: "v\(daemon.version)",
                        color: .blue
                    )
                }
            }
        }
    }

    private var connectionColor: Color {
        switch appState.connectionState {
        case .connected:
            return .green
        case .connecting:
            return .orange
        case .disconnected, .stale:
            return .red
        }
    }

    // MARK: - Daemon Card

    private func daemonCard(status: DaemonStatusDTO) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Dashboard.daemonSection(appState.locale))
                .font(.headline)
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.daemonStatusLabel(appState.locale),
                    value: status.status
                )
                detailItem(
                    label: AppStrings.Dashboard.daemonHostLabel(appState.locale),
                    value: "\(status.host):\(status.port)"
                )
                detailItem(
                    label: AppStrings.Dashboard.daemonPIDLabel(appState.locale),
                    value: "\(status.pid)"
                )
                detailItem(
                    label: AppStrings.Dashboard.daemonWorkspaceCountLabel(appState.locale),
                    value: "\(status.workspaceCount)"
                )
            }
        }
        .padding()
        .background(Color.secondary.opacity(0.1))
        .clipShape(RoundedRectangle(cornerRadius: 8))
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

    // MARK: - Counts

    private var countsGrid: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.Dashboard.countsSection(appState.locale))
                .font(.headline)

            LazyVGrid(
                columns: [
                    GridItem(.adaptive(minimum: 120), spacing: 12)
                ],
                spacing: 12
            ) {
                countCard(
                    title: AppStrings.Dashboard.missionsLabel(appState.locale),
                    count: appState.snapshot?.missions.count ?? 0,
                    color: .blue
                )
                countCard(
                    title: AppStrings.Dashboard.tasksLabel(appState.locale),
                    count: appState.snapshot?.tasks.count ?? 0,
                    color: .purple
                )
                countCard(
                    title: AppStrings.Dashboard.issuesLabel(appState.locale),
                    count: appState.snapshot?.issues.count ?? 0,
                    color: .orange
                )
                countCard(
                    title: AppStrings.Dashboard.failuresLabel(appState.locale),
                    count: appState.snapshot?.failures.count ?? 0,
                    color: .red
                )
                countCard(
                    title: AppStrings.Dashboard.eventsLabel(appState.locale),
                    count: appState.snapshot?.events.count ?? 0,
                    color: .teal
                )
            }
        }
    }

    private func countCard(title: String, count: Int, color: Color) -> some View {
        VStack(spacing: 8) {
            Text("\(count)")
                .font(.system(size: 32, weight: .bold))
                .foregroundStyle(color)
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding()
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Snapshot Content

    private func snapshotContent(snapshot: WorkbenchSnapshotDTO) -> some View {
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        return VStack(alignment: .leading, spacing: 20) {
            if let mission = presentation.currentMission {
                missionCard(mission: mission)
            }
            taskQueueSection(rows: presentation.taskRows)
            failuresSection(rows: presentation.failureRows)
            eventsSection(rows: presentation.recentEventRows)
        }
    }

    private func missionCard(mission: DashboardMissionSummary) -> some View {
        sectionCard(title: AppStrings.Dashboard.missionSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 8) {
                Text(mission.title)
                    .font(.title3)
                    .fontWeight(.semibold)
                HStack(spacing: 16) {
                    detailItem(
                        label: AppStrings.Dashboard.statusLabel(appState.locale),
                        value: mission.status
                    )
                }
            }
        }
    }

    private func taskQueueSection(rows: [DashboardTaskRow]) -> some View {
        sectionCard(title: AppStrings.Dashboard.taskQueueSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                if rows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyTasks(appState.locale))
                } else {
                    ForEach(rows, id: \.id) { row in
                        taskRowView(row: row)
                        if row.id != rows.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func taskRowView(row: DashboardTaskRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(row.subject)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                StatusBadge(text: row.status, color: statusColor(for: row.status))
            }

            HStack(spacing: 16) {
                if let owner = row.owner {
                    detailItem(
                        label: AppStrings.Dashboard.ownerLabel(appState.locale),
                        value: owner
                    )
                }
                if let activeForm = row.activeForm {
                    detailItem(
                        label: AppStrings.Dashboard.activeFormLabel(appState.locale),
                        value: activeForm
                    )
                }
            }

            if row.riskLevel != nil || row.parallelMode != nil {
                HStack(spacing: 16) {
                    if let riskLevel = row.riskLevel {
                        detailItem(
                            label: AppStrings.Dashboard.riskLabel(appState.locale),
                            value: riskLevel
                        )
                    }
                    if let parallelMode = row.parallelMode {
                        detailItem(
                            label: AppStrings.Dashboard.parallelModeLabel(appState.locale),
                            value: parallelMode
                        )
                    }
                    if let count = row.acceptanceCriteriaCount {
                        detailItem(
                            label: AppStrings.Dashboard.acceptanceCriteriaLabel(appState.locale),
                            value: "\(count)"
                        )
                    }
                }
            }
        }
    }

    private func failuresSection(rows: [DashboardFailureRow]) -> some View {
        sectionCard(title: AppStrings.Dashboard.failuresSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                if rows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyFailures(appState.locale))
                } else {
                    ForEach(rows, id: \.id) { row in
                        failureRowView(row: row)
                        if row.id != rows.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func failureRowView(row: DashboardFailureRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(row.title)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                StatusBadge(text: row.status, color: .red)
            }
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.kindLabel(appState.locale),
                    value: row.kind
                )
                detailItem(
                    label: AppStrings.Dashboard.tasksLabel(appState.locale),
                    value: row.taskID
                )
            }
        }
    }

    private func eventsSection(rows: [DashboardEventRow]) -> some View {
        sectionCard(title: AppStrings.Dashboard.eventsSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                if rows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyEvents(appState.locale))
                } else {
                    ForEach(rows, id: \.id) { row in
                        eventRowView(row: row)
                        if row.id != rows.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func eventRowView(row: DashboardEventRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(row.type)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                Text(row.timestamp)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.actorLabel(appState.locale),
                    value: row.actor
                )
                detailItem(
                    label: AppStrings.Dashboard.subjectsLabel(appState.locale),
                    value: row.subjectID
                )
            }
        }
    }

    private func sectionCard<Content: View>(
        title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.headline)
            content()
        }
        .padding()
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func emptyListLabel(_ text: String) -> some View {
        HStack {
            Spacer()
            Text(text)
                .foregroundStyle(.secondary)
                .font(.callout)
            Spacer()
        }
        .padding(.vertical, 8)
    }

    private func statusColor(for status: String) -> Color {
        switch status.lowercased() {
        case "completed", "done", "closed", "resolved":
            return .green
        case "in_progress", "running", "active":
            return .blue
        case "blocked", "failed", "open":
            return .red
        case "planning", "pending", "waiting":
            return .orange
        default:
            return .secondary
        }
    }

    // MARK: - Error Card

    private func errorCard(error: APIError) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Dashboard.errorSection(appState.locale))
                .font(.headline)
                .foregroundStyle(.red)
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.errorDetailLabel(appState.locale),
                    value: error.localizedMessage(locale: appState.locale)
                )
            }
        }
        .padding()
        .background(Color.red.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Empty State

    private var emptyState: some View {
        HStack {
            Spacer()
            VStack(spacing: 8) {
                Image(systemName: "tray")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text(AppStrings.Dashboard.emptySnapshot(appState.locale))
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.vertical, 32)
    }
}

#if DEBUG
struct DashboardView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        state.locale = .zhCN
        state.connectionState = .connected
        state.daemonStatus = DaemonStatusDTO(
            status: "running",
            version: "0.1.0",
            pid: 12345,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 3
        )
        return DashboardView(appState: state)
            .frame(minWidth: 640, minHeight: 420)
    }
}
#endif
