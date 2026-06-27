import SwiftUI

/// Shared root shell used by the real app and by local screenshot generation.
public struct WorkbenchShellView: View {
    public let environment: AppEnvironment
    @State private var isPresentingMissionComposer = false

    public init(environment: AppEnvironment) {
        self.environment = environment
    }

    public var body: some View {
        @Bindable var appState = environment.appState

        VStack(spacing: 0) {
            TopNavigationBar(
                appState: environment.appState,
                daemonController: environment.daemonController,
                isPresentingMissionComposer: $isPresentingMissionComposer
            )
            .frame(height: WorkbenchShellPresentation().topNavigationHeight)

            Divider()

            GlobalStatusStrip(appState: environment.appState)
                .frame(height: WorkbenchShellPresentation().globalStatusHeight)

            Divider()

            routeView(for: appState.currentRoute)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .clipped()
        }
        .frame(minWidth: 1180, minHeight: 720)
        .sheet(isPresented: $isPresentingMissionComposer) {
            MissionComposerSheet(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        }
    }

    @ViewBuilder
    private func routeView(for route: AppRoute) -> some View {
        switch route {
        case .dashboard:
            DashboardView(appState: environment.appState)
        case .taskMarket:
            TaskMarketView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        case .timeline:
            TimelineView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        case .reviews:
            ReviewsView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        case .worktrees:
            WorktreesView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        case .settings:
            SettingsView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        }
    }
}

private struct GlobalStatusStrip: View {
    let appState: AppState

    var body: some View {
        let presentation = WorkbenchGlobalStatusPresentation(
            snapshot: appState.snapshot,
            approvals: appState.approvals,
            validationRuns: appState.validationRuns,
            failures: appState.failures,
            locale: appState.locale
        )

        HStack(spacing: 10) {
            ForEach(presentation.items) { item in
                statusItem(item)
                    .frame(
                        minWidth: item.label == "Mission" ? 220 : 96,
                        maxWidth: item.label == "Mission" ? 320 : 150,
                        alignment: .leading
                    )
            }

            Spacer(minLength: 8)
        }
        .padding(.leading, 14)
        .padding(.trailing, 14)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor))
        .layoutPriority(3)
    }

    private func statusItem(_ item: WorkbenchGlobalStatusItem) -> some View {
        HStack(spacing: 7) {
            Image(systemName: item.systemImage)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(color(for: item.tone))
                .frame(width: 14)

            Text(item.label)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            Text(item.value)
                .font(.caption)
                .fontWeight(.semibold)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(color(for: item.tone).opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func color(for tone: WorkbenchGlobalStatusTone) -> Color {
        switch tone {
        case .accent:
            return .accentColor
        case .blue:
            return .blue
        case .orange:
            return .orange
        case .pink:
            return .pink
        case .purple:
            return .purple
        case .red:
            return .red
        case .secondary:
            return .secondary
        }
    }
}

private struct TopNavigationBar: View {
    let appState: AppState
    let daemonController: DaemonController
    @Binding var isPresentingMissionComposer: Bool
    private let shellPresentation = WorkbenchShellPresentation()

    var body: some View {
        @Bindable var appState = appState

        HStack(spacing: 12) {
            Picker("", selection: $appState.currentRoute) {
                ForEach(shellPresentation.navigationRoutes) { route in
                    Label(route.displayName(locale: appState.locale), systemImage: route.systemImage)
                        .tag(route)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(minWidth: 430, idealWidth: 540, maxWidth: 620)
            .layoutPriority(2)

            Spacer(minLength: 12)

            Button {
                if !appState.isPreviewFixture {
                    Task {
                        await daemonController.refreshConnection()
                    }
                }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help(AppStrings.ConnectionControl.refreshButtonHelp(appState.locale))
            .disabled(appState.connectionState == .connecting)

            Button {
                isPresentingMissionComposer = true
            } label: {
                Label(
                    AppStrings.MissionComposer.newMissionButton(appState.locale),
                    systemImage: "plus"
                )
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

            Menu {
                Button(currentMissionTitle) {}
            } label: {
                Text(appState.locale == .zhCN ? "目标" : "Mission")
            }
            .menuStyle(.borderlessButton)
            .controlSize(.small)

            HStack(spacing: 6) {
                Circle()
                    .fill(connectionColor)
                    .frame(width: 7, height: 7)
                Text(workspaceLabel)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .frame(maxWidth: 180, alignment: .trailing)
            .layoutPriority(-1)
        }
        .padding(.leading, shellPresentation.leadingContentInset)
        .padding(.trailing, 14)
        .padding(.vertical, 7)
        .frame(maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var workspaceLabel: String {
        let workspace = appState.selectedWorkspace ?? "~/naumi"
        return appState.locale == .zhCN ? "工作区: \(workspace)" : "Workspace: \(workspace)"
    }

    private var currentMissionTitle: String {
        appState.snapshot?.missions.first?.title
            ?? (appState.locale == .zhCN ? "Mac Agent Workbench MVP" : "Mac Agent Workbench MVP")
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
}

private struct MissionComposerSheet: View {
    let appState: AppState
    let daemonController: DaemonController

    @Environment(\.dismiss) private var dismiss
    @State private var draftTitle: String = ""
    @State private var draftGoal: String = ""

    private var trimmedTitle: String {
        draftTitle.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var trimmedGoal: String {
        draftGoal.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canCreate: Bool {
        !trimmedTitle.isEmpty && !trimmedGoal.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(AppStrings.MissionComposer.sheetTitle(appState.locale))
                .font(.headline)

            Form {
                TextField(
                    AppStrings.MissionComposer.titleFieldLabel(appState.locale),
                    text: $draftTitle
                )

                TextField(
                    AppStrings.MissionComposer.goalFieldLabel(appState.locale),
                    text: $draftGoal
                )
            }
            .frame(minWidth: 320)

            HStack {
                Spacer()

                Button(
                    AppStrings.MissionComposer.cancelButton(appState.locale)
                ) {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)

                Button(
                    AppStrings.MissionComposer.createButton(appState.locale)
                ) {
                    Task {
                        await daemonController.createMission(
                            title: trimmedTitle,
                            goal: trimmedGoal
                        )
                        if appState.lastError == nil {
                            draftTitle = ""
                            draftGoal = ""
                            dismiss()
                        }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canCreate)
            }
        }
        .padding()
        .frame(minWidth: 360, minHeight: 180)
    }
}
