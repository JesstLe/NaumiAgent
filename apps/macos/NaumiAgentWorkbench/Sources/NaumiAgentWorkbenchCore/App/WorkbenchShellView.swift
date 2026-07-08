import SwiftUI

/// Shared root shell used by the real app and by local screenshot generation.
public struct WorkbenchShellView: View {
    public let environment: AppEnvironment
    @State private var isPresentingMissionComposer = false
    @State private var isPresentingConnectionSetup = false
    private let shellPresentation = WorkbenchShellPresentation()

    public init(environment: AppEnvironment) {
        self.environment = environment
    }

    public var body: some View {
        @Bindable var appState = environment.appState

        GeometryReader { proxy in
            let routeLayout = appState.currentRoute.workbenchPageLayout
            let viewport = shellPresentation.shellViewport(
                for: proxy.size,
                pageLayout: routeLayout
            )
            let shellScale = CGFloat(viewport.scale)

            VStack(spacing: 0) {
                TopNavigationBar(
                    appState: environment.appState,
                    daemonController: environment.daemonController,
                    isPresentingMissionComposer: $isPresentingMissionComposer
                )
                .frame(
                    width: shellPresentation.designCanvasWidth,
                    height: shellPresentation.topNavigationHeight,
                    alignment: .topLeading
                )

                routeView(for: appState.currentRoute)
                    .frame(
                        width: routeLayout.baseWidth,
                        height: routeLayout.baseHeight,
                        alignment: .topLeading
                    )
                    .clipped()
            }
            .frame(
                width: shellPresentation.designCanvasWidth,
                height: shellPresentation.topNavigationHeight + routeLayout.baseHeight,
                alignment: .topLeading
            )
            .scaleEffect(shellScale, anchor: .topLeading)
            .frame(
                width: viewport.scaledSize.width,
                height: viewport.scaledSize.height,
                alignment: .topLeading
            )
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
            .clipped()
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .frame(
            minWidth: shellPresentation.minimumWindowWidth,
            minHeight: shellPresentation.minimumWindowHeight
        )
        .sheet(isPresented: $isPresentingMissionComposer) {
            MissionComposerSheet(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        }
        .onChange(of: appState.connectionState) { _, newState in
            // Auto-present the setup sheet on the first hard failure so the
            // user sees a clear reason and next action instead of a blank UI.
            if newState.isFailure, !appState.isPreviewFixture {
                isPresentingConnectionSetup = true
            }
        }
        .sheet(isPresented: $isPresentingConnectionSetup) {
            ConnectionSetupSheet(environment: environment)
        }
    }

    @ViewBuilder
    private func routeView(for route: AppRoute) -> some View {
        switch route {
        case .dashboard:
            DashboardView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        case .chat:
            ChatView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
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
                daemonController: environment.daemonController,
                daemonProcessController: environment.daemonProcessController,
                onEditEndpoint: { isPresentingConnectionSetup = true }
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
                    .frame(width: item.label == "Mission" ? 210 : 112, alignment: .leading)
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
        .frame(maxWidth: .infinity, alignment: .leading)
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
            .frame(minWidth: 460, idealWidth: 620, maxWidth: 740)
            .layoutPriority(2)

            #if DEBUG
            if appState.isPreviewFixture {
                Text(AppStrings.Debug.previewFixtureBadge(appState.locale))
                    .font(.caption2)
                    .fontWeight(.semibold)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Color.orange.opacity(0.18))
                    .foregroundStyle(.orange)
                    .clipShape(Capsule())
                    .help(AppStrings.Debug.previewFixtureBadgeHelp(appState.locale))
                    .layoutPriority(-1)
            }
            #endif

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
            .disabled(!appState.canPerformWrites)

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
        case .authFailed:
            return .purple
        case .protocolMismatch:
            return .pink
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
                .disabled(!canCreate || !appState.canPerformWrites)
            }
        }
        .padding()
        .frame(minWidth: 360, minHeight: 180)
    }
}

/// First-run / connection-failure setup sheet. Lets the user edit the daemon
/// endpoint and token, copy the start command, or retry the connection.
struct ConnectionSetupSheet: View {
    let environment: AppEnvironment
    @Environment(\.dismiss) private var dismiss
    @State private var endpointText = ""
    @State private var tokenText = ""
    @State private var isConnecting = false
    @State private var didCopyCommand = false

    private var appState: AppState { environment.appState }
    private var locale: AppLocale { appState.locale }

    private var trimmedEndpoint: String {
        endpointText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canSave: Bool {
        !trimmedEndpoint.isEmpty
        && URL(string: trimmedEndpoint) != nil
        && !isConnecting
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(AppStrings.ConnectionSetup.title(locale))
                        .font(.headline)
                    if appState.connectionState.isFailure {
                        Text(AppStrings.ConnectionSetup.reason(locale, for: appState.connectionState))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer()
            }

            Form {
                TextField(
                    AppStrings.ConnectionSetup.endpointLabel(locale),
                    text: $endpointText
                )
                .textFieldStyle(.roundedBorder)

                SecureField(
                    AppStrings.ConnectionSetup.tokenLabel(locale),
                    text: $tokenText
                )
                .textFieldStyle(.roundedBorder)

                VStack(alignment: .leading, spacing: 6) {
                    Text(AppStrings.ConnectionSetup.startCommandLabel(locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    HStack {
                        Text(AppStrings.ConnectionSetup.startCommand(locale))
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .padding(8)
                            .background(Color.secondary.opacity(0.10))
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                        Spacer()
                        Button {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(
                                AppStrings.ConnectionSetup.startCommand(locale),
                                forType: .string
                            )
                            didCopyCommand = true
                        } label: {
                            Label(
                                didCopyCommand
                                    ? (locale == .zhCN ? "已复制" : "Copied")
                                    : AppStrings.ConnectionSetup.copyCommandButton(locale),
                                systemImage: "doc.on.doc"
                            )
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }

            HStack {
                Spacer()
                if appState.connectionState.isFailure {
                    Button {
                        retry()
                    } label: {
                        Label(
                            isConnecting
                                ? AppStrings.ConnectionSetup.connectingHint(locale)
                                : AppStrings.ConnectionSetup.retryButton(locale),
                            systemImage: "arrow.clockwise"
                        )
                    }
                    .buttonStyle(.bordered)
                    .disabled(isConnecting)
                }
                Button {
                    saveAndConnect()
                } label: {
                    Label(
                        isConnecting
                            ? AppStrings.ConnectionSetup.connectingHint(locale)
                            : AppStrings.ConnectionSetup.saveButton(locale),
                        systemImage: "checkmark.circle"
                    )
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!canSave)

                Button(AppStrings.MissionComposer.cancelButton(locale)) {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
            }
        }
        .padding()
        .frame(minWidth: 480, minHeight: 360)
        .onAppear {
            endpointText = environment.connectionSettings.baseURLString
            tokenText = environment.connectionSettings.bearerToken ?? ""
        }
    }

    private func saveAndConnect() {
        guard canSave else { return }
        let settings = WorkbenchConnectionSettings(
            baseURLString: trimmedEndpoint,
            bearerToken: tokenText
        )
        isConnecting = true
        Task {
            await environment.updateConnection(settings)
            isConnecting = false
            if environment.appState.connectionState == .connected {
                dismiss()
            }
        }
    }

    private func retry() {
        guard !isConnecting else { return }
        isConnecting = true
        Task {
            await environment.daemonController.refreshConnection()
            isConnecting = false
            if environment.appState.connectionState == .connected {
                dismiss()
            }
        }
    }
}

