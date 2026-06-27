import SwiftUI

/// Settings page for runtime preferences and governance visibility.
public struct SettingsView: View {
    @Bindable public var appState: AppState

    public init(appState: AppState) {
        self.appState = appState
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
        state.locale = .zhCN
        return SettingsView(appState: state)
            .frame(minWidth: 480, minHeight: 280)
    }
}
#endif
