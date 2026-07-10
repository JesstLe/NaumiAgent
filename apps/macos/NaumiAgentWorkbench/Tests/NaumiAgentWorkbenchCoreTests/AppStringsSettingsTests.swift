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
        #expect(AppStrings.Settings.deactivateIntentLockButton(.zhCN) == "停用意图锁")
        #expect(AppStrings.Settings.intentLockCreatedByLabel(.zhCN) == "创建者")
        #expect(AppStrings.Settings.intentLockUpdatedAtLabel(.zhCN) == "更新时间")
        #expect(AppStrings.Settings.intentLockStatusActive(.zhCN) == "生效中")
        #expect(AppStrings.Settings.intentLockStatusInactive(.zhCN) == "已停用")
        #expect(AppStrings.Settings.decisionStrengthLabel(.zhCN) == "决策强度")
        #expect(AppStrings.Settings.policyHitHistorySection(.zhCN) == "策略命中记录")
        #expect(AppStrings.Settings.policyHitEmpty(.zhCN) == "暂无策略命中记录")
        #expect(AppStrings.Settings.policyHitReasonLabel(.zhCN) == "命中原因")
        #expect(AppStrings.Settings.policyHitChangedPathsLabel(.zhCN) == "受影响路径")
        #expect(AppStrings.Settings.policyHitBlockedActionLabel(.zhCN) == "被阻塞动作")
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
        #expect(AppStrings.Settings.deactivateIntentLockButton(.enUS) == "Deactivate Intent Lock")
        #expect(AppStrings.Settings.intentLockCreatedByLabel(.enUS) == "Created By")
        #expect(AppStrings.Settings.intentLockUpdatedAtLabel(.enUS) == "Updated At")
        #expect(AppStrings.Settings.intentLockStatusActive(.enUS) == "Active")
        #expect(AppStrings.Settings.intentLockStatusInactive(.enUS) == "Inactive")
        #expect(AppStrings.Settings.decisionStrengthLabel(.enUS) == "Decision Strength")
        #expect(AppStrings.Settings.policyHitHistorySection(.enUS) == "Policy Hit History")
        #expect(AppStrings.Settings.policyHitEmpty(.enUS) == "No policy hits yet")
        #expect(AppStrings.Settings.policyHitReasonLabel(.enUS) == "Reason")
        #expect(AppStrings.Settings.policyHitChangedPathsLabel(.enUS) == "Changed Paths")
        #expect(AppStrings.Settings.policyHitBlockedActionLabel(.enUS) == "Blocked Action")
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

    @Test func decisionStrengthStringsZhCN() {
        #expect(AppStrings.DecisionStrength.advisory(.zhCN) == "建议")
        #expect(AppStrings.DecisionStrength.required(.zhCN) == "必须遵守")
        #expect(AppStrings.DecisionStrength.blocking(.zhCN) == "阻断")
        #expect(AppStrings.DecisionStrength.label(.zhCN, for: "advisory") == "建议")
        #expect(AppStrings.DecisionStrength.label(.zhCN, for: "blocking") == "阻断")
        #expect(AppStrings.DecisionStrength.label(.zhCN, for: "required") == "必须遵守")
        #expect(AppStrings.DecisionStrength.label(.zhCN, for: "unknown") == "必须遵守")
    }

    @Test func decisionStrengthStringsEnUS() {
        #expect(AppStrings.DecisionStrength.advisory(.enUS) == "Advisory")
        #expect(AppStrings.DecisionStrength.required(.enUS) == "Required")
        #expect(AppStrings.DecisionStrength.blocking(.enUS) == "Blocking")
        #expect(AppStrings.DecisionStrength.label(.enUS, for: "advisory") == "Advisory")
        #expect(AppStrings.DecisionStrength.label(.enUS, for: "blocking") == "Blocking")
        #expect(AppStrings.DecisionStrength.label(.enUS, for: "required") == "Required")
        #expect(AppStrings.DecisionStrength.label(.enUS, for: "unknown") == "Required")
    }

    @Test func policyHitStringsZhCN() {
        #expect(AppStrings.PolicyHit.blockedPathCountLabel(.zhCN, count: 0) == "0 条路径")
        #expect(AppStrings.PolicyHit.blockedPathCountLabel(.zhCN, count: 1) == "1 条路径")
        #expect(AppStrings.PolicyHit.blockedPathCountLabel(.zhCN, count: 3) == "3 条路径")
    }

    @Test func policyHitStringsEnUS() {
        #expect(AppStrings.PolicyHit.blockedPathCountLabel(.enUS, count: 0) == "0 paths")
        #expect(AppStrings.PolicyHit.blockedPathCountLabel(.enUS, count: 1) == "1 path")
        #expect(AppStrings.PolicyHit.blockedPathCountLabel(.enUS, count: 3) == "3 paths")
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
            AppStrings.Settings.deactivateIntentLockButton,
            AppStrings.Settings.intentLockCreatedByLabel,
            AppStrings.Settings.intentLockUpdatedAtLabel,
            AppStrings.Settings.intentLockStatusActive,
            AppStrings.Settings.intentLockStatusInactive,
            AppStrings.Settings.decisionStrengthLabel,
            AppStrings.Settings.policyHitHistorySection,
            AppStrings.Settings.policyHitEmpty,
            AppStrings.Settings.policyHitReasonLabel,
            AppStrings.Settings.policyHitChangedPathsLabel,
            AppStrings.Settings.policyHitBlockedActionLabel,
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
