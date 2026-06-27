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
        Form {
            languageSection
            governanceSection
        }
        .padding()
        .navigationTitle(AppStrings.Settings.title(appState.locale))
    }

    // MARK: - Language

    private var languageSection: some View {
        Section {
            Picker(
                AppStrings.Settings.currentLanguageLabel(appState.locale),
                selection: $appState.locale
            ) {
                ForEach(AppLocale.allCases) { locale in
                    Text(locale.rawValue).tag(locale)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 240, alignment: .leading)
        } header: {
            Text(AppStrings.Settings.languageSection(appState.locale))
        }
    }

    // MARK: - Governance Policies

    private var governanceSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                policyRow(AppStrings.Settings.highRiskApprovalPolicy(appState.locale))
                policyRow(AppStrings.Settings.localDaemonPolicy(appState.locale))
                policyRow(AppStrings.Settings.writeViaWorkbenchAPIPolicy(appState.locale))

                Divider()

                Text(AppStrings.Settings.createIntentLockSection(appState.locale))
                    .font(.subheadline)
                    .fontWeight(.semibold)
                    .padding(.top, 4)

                TextField(
                    AppStrings.Settings.missionIDFieldLabel(appState.locale),
                    text: $draft.missionID
                )

                TextField(
                    AppStrings.Settings.actorFieldLabel(appState.locale),
                    text: $draft.actor
                )

                TextField(
                    AppStrings.Settings.ruleFieldLabel(appState.locale),
                    text: $draft.rule
                )

                TextField(
                    AppStrings.Settings.blockedPathsFieldLabel(appState.locale),
                    text: $draft.blockedPathsText,
                    axis: .vertical
                )
                .lineLimit(2...4)

                TextField(
                    AppStrings.Settings.allowedPathsFieldLabel(appState.locale),
                    text: $draft.allowedPathsText,
                    axis: .vertical
                )
                .lineLimit(2...4)

                Picker(
                    AppStrings.Settings.requireProposalForRiskLabel(appState.locale),
                    selection: $draft.requireProposalForRisk
                ) {
                    Text(AppStrings.GovernanceRiskLevel.low(appState.locale))
                        .tag("low")
                    Text(AppStrings.GovernanceRiskLevel.medium(appState.locale))
                        .tag("medium")
                    Text(AppStrings.GovernanceRiskLevel.high(appState.locale))
                        .tag("high")
                    Text(AppStrings.GovernanceRiskLevel.critical(appState.locale))
                        .tag("critical")
                }
                .pickerStyle(.segmented)

                HStack(spacing: 12) {
                    Button(
                        AppStrings.Settings.createIntentLockButton(appState.locale)
                    ) {
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
                    }
                    .disabled(!draft.canSubmit || isSubmitting)

                    if isSubmitting {
                        Text(AppStrings.Settings.processingLabel(appState.locale))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 4)
            }
        } header: {
            Text(AppStrings.Settings.governanceSection(appState.locale))
        }
    }

    private func policyRow(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "checkmark.shield")
                .foregroundStyle(.secondary)
                .font(.caption)
            Text(text)
                .font(.body)
            Spacer()
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
            .frame(minWidth: 480, minHeight: 420)
    }
}
#endif
