import SwiftUI

/// Reviews page showing validation runs for the selected session.
///
/// Runs are loaded from `GET /workbench/sessions/{id}/validation-runs` and never
/// fabricated locally. The view refreshes automatically on appear and exposes a
/// manual refresh button in the toolbar.
public struct ReviewsView: View {
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
                runList
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(AppStrings.Reviews.title(appState.locale))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: {
                    Task {
                        await daemonController.refreshValidationRuns(limit: 50)
                    }
                }) {
                    Label(
                        AppStrings.Reviews.refreshButton(appState.locale),
                        systemImage: "arrow.clockwise"
                    )
                }
            }
        }
        .task {
            await daemonController.refreshValidationRuns(limit: 50)
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
}
#endif
