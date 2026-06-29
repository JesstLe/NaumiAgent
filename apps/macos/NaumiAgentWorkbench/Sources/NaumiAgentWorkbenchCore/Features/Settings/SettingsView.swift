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

        VStack(spacing: 0) {
            pageHeader
            Divider()

            HStack(spacing: 0) {
                settingsRail(presentation: presentation)
                    .frame(width: 286)
                    .frame(maxHeight: .infinity)
                    .clipped()

                Divider()

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        summaryStrip(presentation: presentation)
                        contentGrid(presentation: presentation)
                    }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                Divider()

                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        runtimePanel(presentation: presentation)
                        capabilitiesPanel
                        selectedIntentLockPanel
                        readinessGrid(presentation: presentation)
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(width: 342)
            }
        }
        .frame(minWidth: 1120, minHeight: 700)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var pageHeader: some View {
        HStack(spacing: 12) {
            Text(AppStrings.Settings.title(appState.locale))
                .font(.system(size: 17, weight: .semibold))
            Text(appState.locale == .zhCN ? "语言、治理策略、运行时能力与意图锁" : "Language, governance, runtime capability, and intent locks")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Picker(AppStrings.Settings.currentLanguageLabel(appState.locale), selection: $appState.locale) {
                ForEach(AppLocale.allCases) { locale in
                    Text(locale.rawValue).tag(locale)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .frame(width: 152)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 11)
    }

    private func settingsRail(presentation: SettingsDashboardPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(appState.locale == .zhCN ? "治理控制台" : "Governance Console", systemImage: "checkmark.shield")
                .font(.system(size: 14, weight: .semibold))

            railRow(
                icon: "server.rack",
                title: appState.locale == .zhCN ? "本地服务" : "Local Service",
                value: connectionLabel(presentation.connectionState),
                tint: presentation.connectionState == .connected ? .green : .orange
            )
            railRow(
                icon: "globe",
                title: AppStrings.Settings.languageSection(appState.locale),
                value: appState.locale.rawValue,
                tint: .blue
            )
            railRow(
                icon: "lock.badge.plus",
                title: AppStrings.Settings.createIntentLockSection(appState.locale),
                value: draft.canSubmit ? (appState.locale == .zhCN ? "可提交" : "Ready") : (appState.locale == .zhCN ? "待补全" : "Draft"),
                tint: draft.canSubmit ? .green : .secondary
            )
            railRow(
                icon: "switch.2",
                title: appState.locale == .zhCN ? "服务能力" : "Capabilities",
                value: "\(presentation.enabledCapabilityCount)",
                tint: .purple
            )

            Divider()

            panel(title: AppStrings.Settings.governanceSection(appState.locale)) {
                VStack(alignment: .leading, spacing: 10) {
                    policyRow(AppStrings.Settings.highRiskApprovalPolicy(appState.locale), systemImage: "person.crop.circle.badge.checkmark")
                    policyRow(AppStrings.Settings.localDaemonPolicy(appState.locale), systemImage: "lock.shield")
                    policyRow(AppStrings.Settings.writeViaWorkbenchAPIPolicy(appState.locale), systemImage: "arrow.triangle.branch")
                }
            }

            Spacer()
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func railRow(icon: String, title: String, value: String, tint: Color) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .foregroundStyle(tint)
                .frame(width: 24, height: 24)
                .background(tint.opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Text(value)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
        }
        .padding(10)
        .background(Color(nsColor: .windowBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
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
            VStack(alignment: .leading, spacing: 14) {
                intentLockPanel
                intentLockRecordsPanel(presentation: presentation)
            }
                .frame(minWidth: 420, maxWidth: .infinity, alignment: .top)

            languagePanel(presentation: presentation)
                .frame(width: 330, alignment: .top)
        }
    }

    private func readinessGrid(presentation: SettingsDashboardPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            checklistPanel(
                title: appState.locale == .zhCN ? "运行时就绪检查" : "Runtime Readiness",
                items: presentation.runtimeChecklist
            )
            checklistPanel(
                title: appState.locale == .zhCN ? "治理护栏检查" : "Governance Guardrails",
                items: presentation.governanceChecklist
            )
        }
    }

    private func checklistPanel(title: String, items: [SettingsChecklistItem]) -> some View {
        panel(title: title) {
            VStack(spacing: 10) {
                ForEach(items) { item in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: item.systemImage)
                            .foregroundStyle(color(for: item.state))
                            .frame(width: 22)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.title(locale: appState.locale))
                                .font(.system(size: 14, weight: .semibold))
                            Text(item.stateLabel(locale: appState.locale))
                                .font(.caption)
                                .foregroundStyle(color(for: item.state))
                        }
                        Spacer()
                    }
                    .padding(12)
                    .background(color(for: item.state).opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
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

    private func intentLockRecordsPanel(presentation: SettingsDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "意图锁记录" : "Intent Lock Records") {
            if presentation.intentLocks.isEmpty {
                Text(appState.locale == .zhCN ? "暂无意图锁记录" : "No intent locks yet")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
                    .background(Color(nsColor: .controlBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                VStack(spacing: 8) {
                    ForEach(presentation.intentLocks) { row in
                        Button {
                            guard let command = SettingsIntentLockSelectionCommand(row: row) else {
                                return
                            }
                            Task {
                                await daemonController.loadIntentLock(
                                    missionID: command.missionID,
                                    lockID: command.lockID
                                )
                            }
                        } label: {
                            intentLockRecordRow(row)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private func intentLockRecordRow(_ row: SettingsIntentLockRow) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: row.isActive ? "lock.fill" : "lock.slash")
                .foregroundStyle(row.isActive ? .orange : .secondary)
                .frame(width: 24, height: 24)
                .background((row.isActive ? Color.orange : Color.secondary).opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 6))

            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(row.rule)
                        .font(.system(size: 13, weight: .semibold))
                        .lineLimit(1)
                    Spacer()
                    Text(row.riskLabel)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.orange.opacity(0.12))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }

                HStack(spacing: 10) {
                    Text(row.scopeSummary)
                    Text(row.createdAt)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(row.id == appState.selectedIntentLock?.id ? Color.accentColor.opacity(0.12) : Color(nsColor: .controlBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(row.id == appState.selectedIntentLock?.id ? Color.accentColor : Color.clear, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder
    private var selectedIntentLockPanel: some View {
        if let lock = appState.selectedIntentLock {
            panel(title: appState.locale == .zhCN ? "已选意图锁" : "Selected Intent Lock") {
                VStack(alignment: .leading, spacing: 10) {
                    settingsRow(label: "ID", value: lock.id)
                    settingsRow(label: "Mission", value: lock.missionID)
                    settingsRow(
                        label: appState.locale == .zhCN ? "状态" : "Status",
                        value: lock.active ? (appState.locale == .zhCN ? "生效中" : "Active") : (appState.locale == .zhCN ? "已停用" : "Inactive")
                    )
                    settingsRow(label: appState.locale == .zhCN ? "需提案风险" : "Proposal Risk", value: lock.requireProposalForRisk)
                    Text(lock.rule)
                        .font(.system(size: 13, weight: .semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    pathSummary(title: appState.locale == .zhCN ? "阻塞路径" : "Blocked Paths", values: lock.blockedPaths)
                    pathSummary(title: appState.locale == .zhCN ? "允许路径" : "Allowed Paths", values: lock.allowedPaths)
                }
            }
        }
    }

    private func pathSummary(title: String, values: [String]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(values.isEmpty ? "-" : values.joined(separator: "\n"))
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .lineLimit(4)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(Color(nsColor: .controlBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
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

    private func color(for state: SettingsChecklistState) -> Color {
        switch state {
        case .passed:
            return .green
        case .warning:
            return .orange
        case .blocked:
            return .red
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
