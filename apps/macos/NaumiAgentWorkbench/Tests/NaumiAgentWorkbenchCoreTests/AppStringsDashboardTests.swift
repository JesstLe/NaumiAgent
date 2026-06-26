import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsDashboardTests {

    @Test func newDashboardStringsZhCN() {
        #expect(AppStrings.Dashboard.missionSection(.zhCN) == "当前目标")
        #expect(AppStrings.Dashboard.taskQueueSection(.zhCN) == "任务队列")
        #expect(AppStrings.Dashboard.failuresSection(.zhCN) == "失败验证")
        #expect(AppStrings.Dashboard.eventsSection(.zhCN) == "最近事件")
        #expect(AppStrings.Dashboard.statusLabel(.zhCN) == "状态")
        #expect(AppStrings.Dashboard.ownerLabel(.zhCN) == "负责人")
        #expect(AppStrings.Dashboard.riskLabel(.zhCN) == "风险")
        #expect(AppStrings.Dashboard.emptyTasks(.zhCN) == "暂无任务")
        #expect(AppStrings.Dashboard.emptyFailures(.zhCN) == "暂无失败")
        #expect(AppStrings.Dashboard.emptyEvents(.zhCN) == "暂无事件")
    }

    @Test func newDashboardStringsEnUS() {
        #expect(AppStrings.Dashboard.missionSection(.enUS) == "Current Mission")
        #expect(AppStrings.Dashboard.taskQueueSection(.enUS) == "Task Queue")
        #expect(AppStrings.Dashboard.failuresSection(.enUS) == "Validation Failures")
        #expect(AppStrings.Dashboard.eventsSection(.enUS) == "Recent Events")
        #expect(AppStrings.Dashboard.statusLabel(.enUS) == "Status")
        #expect(AppStrings.Dashboard.ownerLabel(.enUS) == "Owner")
        #expect(AppStrings.Dashboard.riskLabel(.enUS) == "Risk")
        #expect(AppStrings.Dashboard.emptyTasks(.enUS) == "No tasks")
        #expect(AppStrings.Dashboard.emptyFailures(.enUS) == "No failures")
        #expect(AppStrings.Dashboard.emptyEvents(.enUS) == "No events")
    }

    @Test func allNewDashboardStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Dashboard.missionSection,
            AppStrings.Dashboard.taskQueueSection,
            AppStrings.Dashboard.failuresSection,
            AppStrings.Dashboard.eventsSection,
            AppStrings.Dashboard.statusLabel,
            AppStrings.Dashboard.ownerLabel,
            AppStrings.Dashboard.riskLabel,
            AppStrings.Dashboard.activeFormLabel,
            AppStrings.Dashboard.kindLabel,
            AppStrings.Dashboard.actorLabel,
            AppStrings.Dashboard.parallelModeLabel,
            AppStrings.Dashboard.acceptanceCriteriaLabel,
            AppStrings.Dashboard.subjectsLabel,
            AppStrings.Dashboard.emptyTasks,
            AppStrings.Dashboard.emptyFailures,
            AppStrings.Dashboard.emptyEvents,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
