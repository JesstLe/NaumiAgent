import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsReviewsTests {

    @Test func reviewsStringsZhCN() {
        #expect(AppStrings.Reviews.title(.zhCN) == "验证审查")
        #expect(AppStrings.Reviews.runCount(.zhCN, count: 3) == "3 条验证")
        #expect(AppStrings.Reviews.refreshButton(.zhCN) == "刷新")
        #expect(AppStrings.Reviews.emptyRuns(.zhCN) == "暂无验证记录")
        #expect(AppStrings.Reviews.statusLabel(.zhCN) == "状态")
        #expect(AppStrings.Reviews.taskIDLabel(.zhCN) == "任务 ID")
        #expect(AppStrings.Reviews.actorLabel(.zhCN) == "执行者")
        #expect(AppStrings.Reviews.exitCodeLabel(.zhCN) == "退出码")
        #expect(AppStrings.Reviews.commandLabel(.zhCN) == "命令")
        #expect(AppStrings.Reviews.cwdLabel(.zhCN) == "工作目录")
        #expect(AppStrings.Reviews.completedAtLabel(.zhCN) == "完成时间")
        #expect(AppStrings.Reviews.outputLabel(.zhCN) == "输出摘要")
        #expect(AppStrings.Reviews.statusPassed(.zhCN) == "通过")
        #expect(AppStrings.Reviews.statusFailed(.zhCN) == "失败")
        #expect(AppStrings.Reviews.statusUnknown(.zhCN, status: "x") == "未知: x")
        #expect(AppStrings.Reviews.runValidationSectionTitle(.zhCN) == "运行验证")
        #expect(AppStrings.Reviews.runButton(.zhCN) == "运行")
        #expect(AppStrings.Reviews.processingLabel(.zhCN) == "处理中…")
    }

    @Test func reviewsStringsEnUS() {
        #expect(AppStrings.Reviews.title(.enUS) == "Validation Reviews")
        #expect(AppStrings.Reviews.runCount(.enUS, count: 3) == "3 validation runs")
        #expect(AppStrings.Reviews.refreshButton(.enUS) == "Refresh")
        #expect(AppStrings.Reviews.emptyRuns(.enUS) == "No validation runs yet")
        #expect(AppStrings.Reviews.statusLabel(.enUS) == "Status")
        #expect(AppStrings.Reviews.taskIDLabel(.enUS) == "Task ID")
        #expect(AppStrings.Reviews.actorLabel(.enUS) == "Actor")
        #expect(AppStrings.Reviews.exitCodeLabel(.enUS) == "Exit Code")
        #expect(AppStrings.Reviews.commandLabel(.enUS) == "Command")
        #expect(AppStrings.Reviews.cwdLabel(.enUS) == "Working Directory")
        #expect(AppStrings.Reviews.completedAtLabel(.enUS) == "Completed At")
        #expect(AppStrings.Reviews.outputLabel(.enUS) == "Output Summary")
        #expect(AppStrings.Reviews.statusPassed(.enUS) == "Passed")
        #expect(AppStrings.Reviews.statusFailed(.enUS) == "Failed")
        #expect(AppStrings.Reviews.statusUnknown(.enUS, status: "x") == "Unknown: x")
        #expect(AppStrings.Reviews.runValidationSectionTitle(.enUS) == "Run Validation")
        #expect(AppStrings.Reviews.runButton(.enUS) == "Run")
        #expect(AppStrings.Reviews.processingLabel(.enUS) == "Processing…")
    }

    @Test func allReviewsStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Reviews.title,
            { AppStrings.Reviews.runCount($0, count: 1) },
            AppStrings.Reviews.refreshButton,
            AppStrings.Reviews.emptyRuns,
            AppStrings.Reviews.statusLabel,
            AppStrings.Reviews.taskIDLabel,
            AppStrings.Reviews.actorLabel,
            AppStrings.Reviews.exitCodeLabel,
            AppStrings.Reviews.commandLabel,
            AppStrings.Reviews.cwdLabel,
            AppStrings.Reviews.completedAtLabel,
            AppStrings.Reviews.outputLabel,
            AppStrings.Reviews.statusPassed,
            AppStrings.Reviews.statusFailed,
            { AppStrings.Reviews.statusUnknown($0, status: "x") },
            AppStrings.Reviews.runValidationSectionTitle,
            AppStrings.Reviews.runButton,
            AppStrings.Reviews.processingLabel,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
