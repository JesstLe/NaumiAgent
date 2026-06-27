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
        #expect(AppStrings.Settings.createIntentLockSection(.zhCN) == "创建意图锁")
        #expect(AppStrings.Settings.missionIDFieldLabel(.zhCN) == "Mission ID")
        #expect(AppStrings.Settings.actorFieldLabel(.zhCN) == "执行者")
        #expect(AppStrings.Settings.ruleFieldLabel(.zhCN) == "规则")
        #expect(AppStrings.Settings.blockedPathsFieldLabel(.zhCN) == "阻塞路径")
        #expect(AppStrings.Settings.allowedPathsFieldLabel(.zhCN) == "允许路径")
        #expect(AppStrings.Settings.requireProposalForRiskLabel(.zhCN) == "需提案的风险等级")
        #expect(AppStrings.Settings.createIntentLockButton(.zhCN) == "创建意图锁")
        #expect(AppStrings.Settings.processingLabel(.zhCN) == "处理中…")
    }

    @Test func settingsStringsEnUS() {
        #expect(AppStrings.Settings.title(.enUS) == "Settings")
        #expect(AppStrings.Settings.languageSection(.enUS) == "Language")
        #expect(AppStrings.Settings.governanceSection(.enUS) == "Governance Policies")
        #expect(AppStrings.Settings.currentLanguageLabel(.enUS) == "Current Language")
        #expect(AppStrings.Settings.highRiskApprovalPolicy(.enUS) == "High-risk actions require human approval")
        #expect(AppStrings.Settings.localDaemonPolicy(.enUS) == "Local daemon only listens on 127.0.0.1")
        #expect(AppStrings.Settings.writeViaWorkbenchAPIPolicy(.enUS) == "Write operations must go through the Workbench API")
        #expect(AppStrings.Settings.createIntentLockSection(.enUS) == "Create Intent Lock")
        #expect(AppStrings.Settings.missionIDFieldLabel(.enUS) == "Mission ID")
        #expect(AppStrings.Settings.actorFieldLabel(.enUS) == "Actor")
        #expect(AppStrings.Settings.ruleFieldLabel(.enUS) == "Rule")
        #expect(AppStrings.Settings.blockedPathsFieldLabel(.enUS) == "Blocked Paths")
        #expect(AppStrings.Settings.allowedPathsFieldLabel(.enUS) == "Allowed Paths")
        #expect(AppStrings.Settings.requireProposalForRiskLabel(.enUS) == "Require Proposal For Risk")
        #expect(AppStrings.Settings.createIntentLockButton(.enUS) == "Create Intent Lock")
        #expect(AppStrings.Settings.processingLabel(.enUS) == "Processing…")
    }

    @Test func governanceRiskLevelStringsZhCN() {
        #expect(AppStrings.GovernanceRiskLevel.low(.zhCN) == "低")
        #expect(AppStrings.GovernanceRiskLevel.medium(.zhCN) == "中")
        #expect(AppStrings.GovernanceRiskLevel.high(.zhCN) == "高")
        #expect(AppStrings.GovernanceRiskLevel.critical(.zhCN) == "严重")
    }

    @Test func governanceRiskLevelStringsEnUS() {
        #expect(AppStrings.GovernanceRiskLevel.low(.enUS) == "Low")
        #expect(AppStrings.GovernanceRiskLevel.medium(.enUS) == "Medium")
        #expect(AppStrings.GovernanceRiskLevel.high(.enUS) == "High")
        #expect(AppStrings.GovernanceRiskLevel.critical(.enUS) == "Critical")
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
            AppStrings.Settings.createIntentLockSection,
            AppStrings.Settings.missionIDFieldLabel,
            AppStrings.Settings.actorFieldLabel,
            AppStrings.Settings.ruleFieldLabel,
            AppStrings.Settings.blockedPathsFieldLabel,
            AppStrings.Settings.allowedPathsFieldLabel,
            AppStrings.Settings.requireProposalForRiskLabel,
            AppStrings.Settings.createIntentLockButton,
            AppStrings.Settings.processingLabel,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }

    @Test func allGovernanceRiskLevelStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.GovernanceRiskLevel.low,
            AppStrings.GovernanceRiskLevel.medium,
            AppStrings.GovernanceRiskLevel.high,
            AppStrings.GovernanceRiskLevel.critical,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
