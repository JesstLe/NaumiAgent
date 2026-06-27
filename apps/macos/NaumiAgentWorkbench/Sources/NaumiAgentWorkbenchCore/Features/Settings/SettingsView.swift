import SwiftUI

/// Settings page for runtime preferences and governance visibility.
public struct SettingsView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    @State private var draft = IntentLockDraft()
    @State private var isSubmitting = false

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = SettingsDashboardPresentation(appState: appState)

        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                pageHeader
                summaryStrip(presentation: presentation)
                contentGrid(presentation: presentation)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var pageHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(AppStrings.Settings.title(appState.locale))
                .font(.system(size: 22, weight: .semibold))
            Text(appState.locale == .zhCN ? "语言、治理策略、运行时能力与意图锁" : "Language, governance, runtime capability, and intent locks")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    private func summaryStrip(presentation: SettingsDashboardPresentation) -> some View {
        HStack(spacing: 12) {
            metricCard(
                title: appState.locale == .zhCN ? "本地服务" : "Local Service",
                value: presentation.runtimeEndpoint,
                systemImage: "server.rack"
            )
            metricCard(
                title: appState.locale == .zhCN ? "当前目标" : "Active Mission",
                value: presentation.activeMissionTitle,
                systemImage: "scope",
                tint: .purple
            )
            metricCard(
                title: appState.locale == .zhCN ? "可用能力" : "Capabilities",
                value: "\(presentation.enabledCapabilityCount)",
                systemImage: "switch.2",
                tint: .green
            )
            metricCard(
                title: appState.locale == .zhCN ? "治理策略" : "Policies",
                value: "\(presentation.governancePolicyCount)",
                systemImage: "checkmark.shield",
                tint: .orange
            )
        }
    }

    private func contentGrid(presentation: SettingsDashboardPresentation) -> some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 14) {
                runtimePanel(presentation: presentation)
                governancePanel
            }
            .frame(width: 360, alignment: .top)

            intentLockPanel
                .frame(minWidth: 420, maxWidth: .infinity, alignment: .top)

            VStack(spacing: 14) {
                languagePanel(presentation: presentation)
                capabilitiesPanel
            }
            .frame(width: 330, alignment: .top)
        }
    }

    private func runtimePanel(presentation: SettingsDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "运行时状态" : "Runtime Status") {
            VStack(alignment: .leading, spacing: 12) {
                settingsRow(label: appState.locale == .zhCN ? "连接" : "Connection", value: connectionLabel(presentation.connectionState))
                settingsRow(label: appState.locale == .zhCN ? "地址" : "Endpoint", value: presentation.runtimeEndpoint)
                settingsRow(
                    label: appState.locale == .zhCN ? "进程" : "PID",
                    value: appState.daemonStatus.map { "\($0.pid)" } ?? "-"
                )
                settingsRow(
                    label: appState.locale == .zhCN ? "工作区" : "Workspaces",
                    value: appState.daemonStatus.map { "\($0.workspaceCount)" } ?? "-"
                )
            }
        }
    }

    private var governancePanel: some View {
        panel(title: AppStrings.Settings.governanceSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 10) {
                policyRow(AppStrings.Settings.highRiskApprovalPolicy(appState.locale), systemImage: "person.crop.circle.badge.checkmark")
                policyRow(AppStrings.Settings.localDaemonPolicy(appState.locale), systemImage: "lock.shield")
                policyRow(AppStrings.Settings.writeViaWorkbenchAPIPolicy(appState.locale), systemImage: "arrow.triangle.branch")
            }
        }
    }

    private var intentLockPanel: some View {
        panel(title: AppStrings.Settings.createIntentLockSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                TextField(AppStrings.Settings.missionIDFieldLabel(appState.locale), text: $draft.missionID)
                    .textFieldStyle(.roundedBorder)

                HStack(spacing: 10) {
                    TextField(AppStrings.Settings.actorFieldLabel(appState.locale), text: $draft.actor)
                        .textFieldStyle(.roundedBorder)
                    Picker(
                        AppStrings.Settings.requireProposalForRiskLabel(appState.locale),
                        selection: $draft.requireProposalForRisk
                    ) {
                        Text(AppStrings.GovernanceRiskLevel.low(appState.locale)).tag("low")
                        Text(AppStrings.GovernanceRiskLevel.medium(appState.locale)).tag("medium")
                        Text(AppStrings.GovernanceRiskLevel.high(appState.locale)).tag("high")
                        Text(AppStrings.GovernanceRiskLevel.critical(appState.locale)).tag("critical")
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(width: 260)
                }

                TextField(AppStrings.Settings.ruleFieldLabel(appState.locale), text: $draft.rule)
                    .textFieldStyle(.roundedBorder)

                HStack(alignment: .top, spacing: 10) {
                    pathEditor(
                        title: AppStrings.Settings.blockedPathsFieldLabel(appState.locale),
                        text: $draft.blockedPathsText
                    )
                    pathEditor(
                        title: AppStrings.Settings.allowedPathsFieldLabel(appState.locale),
                        text: $draft.allowedPathsText
                    )
                }

                HStack(spacing: 12) {
                    Button {
                        Task {
                            isSubmitting = true
                            await daemonController.createIntentLock(
                                missionID: draft.trimmedMissionID,
                                actor: draft.trimmedActor,
                                rule: draft.trimmedRule,
                                blockedPaths: draft.blockedPaths,
                                allowedPaths: draft.allowedPaths,
                                requireProposalForRisk: draft.requireProposalForRisk
                            )
                            isSubmitting = false
                            if appState.lastError == nil {
                                draft = IntentLockDraft()
                            }
                        }
                    } label: {
                        Label(AppStrings.Settings.createIntentLockButton(appState.locale), systemImage: "lock.badge.plus")
                    }
                    .disabled(!draft.canSubmit || isSubmitting)

                    if isSubmitting {
                        Text(AppStrings.Settings.processingLabel(appState.locale))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 2)
            }
        }
    }

    private func languagePanel(presentation: SettingsDashboardPresentation) -> some View {
        panel(title: AppStrings.Settings.languageSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                Picker(AppStrings.Settings.currentLanguageLabel(appState.locale), selection: $appState.locale) {
                    ForEach(AppLocale.allCases) { locale in
                        Text(locale.rawValue).tag(locale)
                    }
                }
                .pickerStyle(.segmented)

                settingsRow(
                    label: appState.locale == .zhCN ? "默认语言" : "Default",
                    value: "zh-CN"
                )
                settingsRow(
                    label: appState.locale == .zhCN ? "服务支持" : "Supported",
                    value: presentation.supportedLocales.isEmpty ? "-" : presentation.supportedLocales.joined(separator: ", ")
                )
            }
        }
    }

    private var capabilitiesPanel: some View {
        panel(title: appState.locale == .zhCN ? "服务能力" : "Service Capabilities") {
            VStack(alignment: .leading, spacing: 10) {
                capabilityRow(
                    title: appState.locale == .zhCN ? "Daemon 管理" : "Daemon Management",
                    enabled: appState.capabilities?.supportsDaemonManagement == true
                )
                capabilityRow(
                    title: appState.locale == .zhCN ? "工作区注册" : "Workspace Registry",
                    enabled: appState.capabilities?.supportsWorkspaceRegistry == true
                )
                capabilityRow(
                    title: appState.locale == .zhCN ? "验证运行器" : "Validation Runner",
                    enabled: appState.capabilities?.supportsValidationRunner == true
                )
                capabilityRow(
                    title: appState.locale == .zhCN ? "云同步" : "Cloud Sync",
                    enabled: appState.capabilities?.supportsCloudSync == true
                )
            }
        }
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
                    .font(.system(size: value.count > 14 ? 12 : 19, weight: .semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
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

    private func pathEditor(title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField(title, text: text, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(4...5)
        }
    }

    private func settingsRow(label: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Text(value)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
    }

    private func policyRow(_ text: String, systemImage: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: systemImage)
                .foregroundStyle(.orange)
                .frame(width: 18)
            Text(text)
                .font(.system(size: 13, weight: .medium))
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding(10)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func capabilityRow(title: String, enabled: Bool) -> some View {
        HStack(spacing: 9) {
            Image(systemName: enabled ? "checkmark.circle.fill" : "minus.circle")
                .foregroundStyle(enabled ? .green : .secondary)
            Text(title)
                .font(.system(size: 13, weight: .medium))
            Spacer()
            Text(enabled ? (appState.locale == .zhCN ? "启用" : "On") : (appState.locale == .zhCN ? "未启用" : "Off"))
                .font(.caption)
                .foregroundStyle(enabled ? .green : .secondary)
        }
        .padding(.vertical, 4)
    }

    private func connectionLabel(_ state: AppState.ConnectionState) -> String {
        switch state {
        case .connected:
            return appState.locale == .zhCN ? "已连接" : "Connected"
        case .connecting:
            return appState.locale == .zhCN ? "连接中" : "Connecting"
        case .disconnected:
            return appState.locale == .zhCN ? "未连接" : "Disconnected"
        case .stale:
            return appState.locale == .zhCN ? "已过期" : "Stale"
        }
    }
}

#if DEBUG
struct SettingsView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        let controller = DaemonController(
            appState: state,
            apiProvider: WorkbenchAPIClient()
        )
        state.locale = .zhCN
        return SettingsView(appState: state, daemonController: controller)
            .frame(minWidth: 1060, minHeight: 620)
    }
}
#endif
