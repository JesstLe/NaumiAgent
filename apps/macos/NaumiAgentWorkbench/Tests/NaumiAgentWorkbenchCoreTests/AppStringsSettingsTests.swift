import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsSettingsTests {

    @Test func settingsStringsZhCN() {
        #expect(AppStrings.Settings.title(.zhCN) == "设置")
        #expect(AppStrings.Settings.languageSection(.zhCN) == "语言")
        #expect(AppStrings.Settings.governanceSection(.zhCN) == "治理策略")
        #expect(AppStrings.Settings.currentLanguageLabel(.zhCN) == "当前语言")
        #expect(AppStrings.Settings.highRiskApprovalPolicy(.zhCN) == "高风险动作需要人工审批")
        #expect(AppStrings.Settings.localDaemonPolicy(.zhCN) == "本地 daemon 仅监听 127.0.0.1")
        #expect(AppStrings.Settings.writeViaWorkbenchAPIPolicy(.zhCN) == "写操作必须经 Workbench API 转发")
    }

    @Test func settingsStringsEnUS() {
        #expect(AppStrings.Settings.title(.enUS) == "Settings")
        #expect(AppStrings.Settings.languageSection(.enUS) == "Language")
        #expect(AppStrings.Settings.governanceSection(.enUS) == "Governance Policies")
        #expect(AppStrings.Settings.currentLanguageLabel(.enUS) == "Current Language")
        #expect(AppStrings.Settings.highRiskApprovalPolicy(.enUS) == "High-risk actions require human approval")
        #expect(AppStrings.Settings.localDaemonPolicy(.enUS) == "Local daemon only listens on 127.0.0.1")
        #expect(AppStrings.Settings.writeViaWorkbenchAPIPolicy(.enUS) == "Write operations must go through the Workbench API")
    }

    @Test func allSettingsStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Settings.title,
            AppStrings.Settings.languageSection,
            AppStrings.Settings.governanceSection,
            AppStrings.Settings.currentLanguageLabel,
            AppStrings.Settings.highRiskApprovalPolicy,
            AppStrings.Settings.localDaemonPolicy,
            AppStrings.Settings.writeViaWorkbenchAPIPolicy,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
