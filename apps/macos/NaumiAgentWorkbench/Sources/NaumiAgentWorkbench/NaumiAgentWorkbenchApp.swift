import SwiftUI
import NaumiAgentWorkbenchCore

@main
struct NaumiAgentWorkbenchApp: App {
    @State private var environment = AppEnvironment()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(environment)
                .task {
                    await environment.refreshCoordinator.startPeriodicRefresh()
                }
        }
    }
}

struct ContentView: View {
    @Environment(AppEnvironment.self) private var environment
    @State private var isPresentingMissionComposer = false

    var body: some View {
        @Bindable var appState = environment.appState
        NavigationSplitView {
            SidebarView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
            .navigationSplitViewColumnWidth(min: 180, ideal: 220)
        } detail: {
            routeView(for: appState.currentRoute)
        }
        .toolbar {
            ToolbarItem {
                Button {
                    Task {
                        await environment.daemonController.refreshConnection()
                    }
                } label: {
                    Label(
                        AppStrings.ConnectionControl.refreshButton(appState.locale),
                        systemImage: "arrow.clockwise"
                    )
                }
                .labelStyle(.titleAndIcon)
                .help(AppStrings.ConnectionControl.refreshButtonHelp(appState.locale))
                .disabled(appState.connectionState == .connecting)
            }

            ToolbarItem {
                Button {
                    isPresentingMissionComposer = true
                } label: {
                    Label(
                        AppStrings.MissionComposer.newMissionButton(appState.locale),
                        systemImage: "plus"
                    )
                }
                .labelStyle(.titleAndIcon)
                .help(AppStrings.MissionComposer.newMissionButton(appState.locale))
            }
        }
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

struct SidebarView: View {
    let appState: AppState
    let daemonController: DaemonController

    var body: some View {
        VStack(spacing: 0) {
            SessionSelectorSection(
                appState: appState,
                daemonController: daemonController
            )

            Divider()

            routeList
        }
    }

    private var routeList: some View {
        @Bindable var appState = appState
        return List(AppRoute.allCases, selection: $appState.currentRoute) { route in
            NavigationLink(value: route) {
                Label(
                    route.displayName(locale: appState.locale),
                    systemImage: route.systemImage
                )
            }
        }
        .listStyle(.sidebar)
    }
}

struct SessionSelectorSection: View {
    let appState: AppState
    let daemonController: DaemonController

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(AppStrings.SessionSelector.sectionTitle(appState.locale))
                    .font(.subheadline)
                    .fontWeight(.semibold)

                Spacer()

                Button {
                    Task {
                        await daemonController.refreshSessions(page: 1, pageSize: 20)
                    }
                } label: {
                    Label(
                        AppStrings.SessionSelector.refreshButton(appState.locale),
                        systemImage: "arrow.clockwise"
                    )
                    .labelStyle(.iconOnly)
                }
                .buttonStyle(.borderless)
                .help(AppStrings.SessionSelector.refreshButton(appState.locale))
            }
            .padding(.horizontal, 12)
            .padding(.top, 12)

            if appState.sessions.isEmpty {
                Text(AppStrings.SessionSelector.emptySessions(appState.locale))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 12)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(appState.sessions, id: \.id) { session in
                            SessionRow(
                                session: session,
                                isSelected: appState.selectedSessionID == session.id,
                                locale: appState.locale
                            )
                            .contentShape(Rectangle())
                            .onTapGesture {
                                Task {
                                    await daemonController.selectSession(session.id)
                                }
                            }
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.bottom, 8)
                }
                .frame(minHeight: 60, maxHeight: 160)
            }
        }
    }
}

struct SessionRow: View {
    let session: SessionDTO
    let isSelected: Bool
    let locale: AppLocale

    private var displayTitle: String {
        if let title = session.title, !title.isEmpty {
            return title
        }
        return session.id
    }

    var body: some View {
        HStack(spacing: 6) {
            VStack(alignment: .leading, spacing: 2) {
                Text(displayTitle)
                    .font(.system(size: 12, weight: .medium))
                    .lineLimit(1)

                HStack(spacing: 4) {
                    Text(session.id)
                        .lineLimit(1)
                    Text("·")
                    Text(session.status)
                    Text("·")
                    Text(AppStrings.SessionSelector.messageCountLabel(locale, count: session.messageCount))
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }

            Spacer(minLength: 4)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(isSelected ? Color.accentColor.opacity(0.15) : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 4))
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
