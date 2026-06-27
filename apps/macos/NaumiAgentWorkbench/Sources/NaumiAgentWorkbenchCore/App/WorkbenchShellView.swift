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

            Divider()

            routeView(for: appState.currentRoute)
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
                Button("Mac Agent Workbench MVP") {}
            } label: {
                Text("Mission")
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
        .frame(minHeight: 42)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var workspaceLabel: String {
        let workspace = appState.selectedWorkspace ?? "~/naumi"
        return appState.locale == .zhCN ? "工作区: \(workspace)" : "Workspace: \(workspace)"
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
