import SwiftUI

/// Minimal usable dashboard homepage.
/// Displays connection state, daemon/version and counts of missions/tasks/issues/failures/events.
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
        .clipShape(RoundedRectangle(cornerRadius: 12))
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
        .clipShape(RoundedRectangle(cornerRadius: 12))
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
        .clipShape(RoundedRectangle(cornerRadius: 12))
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
