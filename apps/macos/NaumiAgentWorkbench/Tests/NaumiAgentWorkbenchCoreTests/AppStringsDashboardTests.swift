import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsDashboardTests {

    @Test func newDashboardStringsZhCN() {
        #expect(AppStrings.Dashboard.missionSection(.zhCN) == "当前目标")
        #expect(AppStrings.Dashboard.agentsSection(.zhCN) == "智能体状态")
        #expect(AppStrings.Dashboard.taskQueueSection(.zhCN) == "任务队列")
        #expect(AppStrings.Dashboard.failuresSection(.zhCN) == "失败验证")
        #expect(AppStrings.Dashboard.eventsSection(.zhCN) == "最近事件")
        #expect(AppStrings.Dashboard.agentsLabel(.zhCN) == "智能体")
        #expect(AppStrings.Dashboard.statusLabel(.zhCN) == "状态")
        #expect(AppStrings.Dashboard.ownerLabel(.zhCN) == "负责人")
        #expect(AppStrings.Dashboard.roleLabel(.zhCN) == "角色")
        #expect(AppStrings.Dashboard.capabilitiesLabel(.zhCN) == "能力数")
        #expect(AppStrings.Dashboard.maxParallelTasksLabel(.zhCN) == "并行上限")
        #expect(AppStrings.Dashboard.riskLabel(.zhCN) == "风险")
        #expect(AppStrings.Dashboard.emptyAgents(.zhCN) == "暂无智能体")
        #expect(AppStrings.Dashboard.emptyTasks(.zhCN) == "暂无任务")
        #expect(AppStrings.Dashboard.emptyFailures(.zhCN) == "暂无失败")
        #expect(AppStrings.Dashboard.emptyEvents(.zhCN) == "暂无事件")
        #expect(AppStrings.Dashboard.validationStateTitle(.zhCN) == "验证状态")
        #expect(AppStrings.Dashboard.contextHealthTitle(.zhCN) == "上下文健康")
        #expect(AppStrings.Dashboard.rerunValidationButton(.zhCN) == "重新运行验证")
        #expect(AppStrings.Dashboard.refreshContextButton(.zhCN) == "刷新上下文")
        #expect(AppStrings.Dashboard.runningValidationLabel(.zhCN) == "验证中…")
        #expect(AppStrings.Dashboard.refreshingContextLabel(.zhCN) == "刷新中…")
    }

    @Test func newDashboardStringsEnUS() {
        #expect(AppStrings.Dashboard.missionSection(.enUS) == "Current Mission")
        #expect(AppStrings.Dashboard.agentsSection(.enUS) == "Agent Status")
        #expect(AppStrings.Dashboard.taskQueueSection(.enUS) == "Task Queue")
        #expect(AppStrings.Dashboard.failuresSection(.enUS) == "Validation Failures")
        #expect(AppStrings.Dashboard.eventsSection(.enUS) == "Recent Events")
        #expect(AppStrings.Dashboard.agentsLabel(.enUS) == "Agents")
        #expect(AppStrings.Dashboard.statusLabel(.enUS) == "Status")
        #expect(AppStrings.Dashboard.ownerLabel(.enUS) == "Owner")
        #expect(AppStrings.Dashboard.roleLabel(.enUS) == "Role")
        #expect(AppStrings.Dashboard.capabilitiesLabel(.enUS) == "Capabilities")
        #expect(AppStrings.Dashboard.maxParallelTasksLabel(.enUS) == "Parallel Limit")
        #expect(AppStrings.Dashboard.riskLabel(.enUS) == "Risk")
        #expect(AppStrings.Dashboard.emptyAgents(.enUS) == "No agents")
        #expect(AppStrings.Dashboard.emptyTasks(.enUS) == "No tasks")
        #expect(AppStrings.Dashboard.emptyFailures(.enUS) == "No failures")
        #expect(AppStrings.Dashboard.emptyEvents(.enUS) == "No events")
        #expect(AppStrings.Dashboard.validationStateTitle(.enUS) == "Validation State")
        #expect(AppStrings.Dashboard.contextHealthTitle(.enUS) == "Context Health")
        #expect(AppStrings.Dashboard.rerunValidationButton(.enUS) == "Re-run Validation")
        #expect(AppStrings.Dashboard.refreshContextButton(.enUS) == "Refresh Context")
        #expect(AppStrings.Dashboard.runningValidationLabel(.enUS) == "Running…")
        #expect(AppStrings.Dashboard.refreshingContextLabel(.enUS) == "Refreshing…")
    }

    @Test func allNewDashboardStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Dashboard.missionSection,
            AppStrings.Dashboard.agentsSection,
            AppStrings.Dashboard.taskQueueSection,
            AppStrings.Dashboard.failuresSection,
            AppStrings.Dashboard.eventsSection,
            AppStrings.Dashboard.agentsLabel,
            AppStrings.Dashboard.statusLabel,
            AppStrings.Dashboard.ownerLabel,
            AppStrings.Dashboard.riskLabel,
            AppStrings.Dashboard.activeFormLabel,
            AppStrings.Dashboard.roleLabel,
            AppStrings.Dashboard.capabilitiesLabel,
            AppStrings.Dashboard.maxParallelTasksLabel,
            AppStrings.Dashboard.kindLabel,
            AppStrings.Dashboard.actorLabel,
            AppStrings.Dashboard.parallelModeLabel,
            AppStrings.Dashboard.acceptanceCriteriaLabel,
            AppStrings.Dashboard.subjectsLabel,
            AppStrings.Dashboard.emptyAgents,
            AppStrings.Dashboard.emptyTasks,
            AppStrings.Dashboard.emptyFailures,
            AppStrings.Dashboard.emptyEvents,
            AppStrings.Dashboard.validationStateTitle,
            AppStrings.Dashboard.contextHealthTitle,
            AppStrings.Dashboard.rerunValidationButton,
            AppStrings.Dashboard.refreshContextButton,
            AppStrings.Dashboard.runningValidationLabel,
            AppStrings.Dashboard.refreshingContextLabel,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
