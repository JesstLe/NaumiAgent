import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsGlobalStatusTests {

    @Test func globalStatusStringsZhCN() {
        #expect(AppStrings.GlobalStatus.noMission(.zhCN) == "暂无 Mission")
        #expect(AppStrings.GlobalStatus.activeAgents(.zhCN) == "智能体")
        #expect(AppStrings.GlobalStatus.openIssues(.zhCN) == "开放问题")
        #expect(AppStrings.GlobalStatus.blocked(.zhCN) == "阻塞")
        #expect(AppStrings.GlobalStatus.pendingApproval(.zhCN) == "待审批")
        #expect(AppStrings.GlobalStatus.failedValidations(.zhCN) == "验证失败")
    }

    @Test func globalStatusStringsEnUS() {
        #expect(AppStrings.GlobalStatus.noMission(.enUS) == "No Mission")
        #expect(AppStrings.GlobalStatus.activeAgents(.enUS) == "Agents")
        #expect(AppStrings.GlobalStatus.openIssues(.enUS) == "Open Issues")
        #expect(AppStrings.GlobalStatus.blocked(.enUS) == "Blocked")
        #expect(AppStrings.GlobalStatus.pendingApproval(.enUS) == "Pending Approval")
        #expect(AppStrings.GlobalStatus.failedValidations(.enUS) == "Failed Validations")
    }
}
