import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsWorktreesTests {

    @Test func worktreesStringsZhCN() {
        #expect(AppStrings.Worktrees.title(.zhCN) == "上下文健康快照")
        #expect(AppStrings.Worktrees.snapshotCount(.zhCN, count: 3) == "3 条快照")
        #expect(AppStrings.Worktrees.refreshButton(.zhCN) == "刷新")
        #expect(AppStrings.Worktrees.emptySnapshots(.zhCN) == "暂无上下文快照")
        #expect(AppStrings.Worktrees.healthLabel(.zhCN) == "健康状态")
        #expect(AppStrings.Worktrees.taskIDLabel(.zhCN) == "任务 ID")
        #expect(AppStrings.Worktrees.agentIDLabel(.zhCN) == "代理 ID")
        #expect(AppStrings.Worktrees.createdAtLabel(.zhCN) == "创建时间")
        #expect(AppStrings.Worktrees.reasonsLabel(.zhCN) == "原因")
        #expect(AppStrings.Worktrees.healthGood(.zhCN) == "健康")
        #expect(AppStrings.Worktrees.healthStale(.zhCN) == "过期")
        #expect(AppStrings.Worktrees.healthOverloaded(.zhCN) == "过载")
        #expect(AppStrings.Worktrees.healthMissing(.zhCN) == "缺失")
        #expect(AppStrings.Worktrees.healthConflicted(.zhCN) == "冲突")
        #expect(AppStrings.Worktrees.healthUnknown(.zhCN, health: "x") == "未知: x")
    }

    @Test func worktreesStringsEnUS() {
        #expect(AppStrings.Worktrees.title(.enUS) == "Context Health Snapshots")
        #expect(AppStrings.Worktrees.snapshotCount(.enUS, count: 3) == "3 snapshots")
        #expect(AppStrings.Worktrees.refreshButton(.enUS) == "Refresh")
        #expect(AppStrings.Worktrees.emptySnapshots(.enUS) == "No context snapshots")
        #expect(AppStrings.Worktrees.healthLabel(.enUS) == "Health")
        #expect(AppStrings.Worktrees.taskIDLabel(.enUS) == "Task ID")
        #expect(AppStrings.Worktrees.agentIDLabel(.enUS) == "Agent ID")
        #expect(AppStrings.Worktrees.createdAtLabel(.enUS) == "Created At")
        #expect(AppStrings.Worktrees.reasonsLabel(.enUS) == "Reasons")
        #expect(AppStrings.Worktrees.healthGood(.enUS) == "Good")
        #expect(AppStrings.Worktrees.healthStale(.enUS) == "Stale")
        #expect(AppStrings.Worktrees.healthOverloaded(.enUS) == "Overloaded")
        #expect(AppStrings.Worktrees.healthMissing(.enUS) == "Missing")
        #expect(AppStrings.Worktrees.healthConflicted(.enUS) == "Conflicted")
        #expect(AppStrings.Worktrees.healthUnknown(.enUS, health: "x") == "Unknown: x")
    }

    @Test func allWorktreesStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Worktrees.title,
            { AppStrings.Worktrees.snapshotCount($0, count: 1) },
            AppStrings.Worktrees.refreshButton,
            AppStrings.Worktrees.emptySnapshots,
            AppStrings.Worktrees.healthLabel,
            AppStrings.Worktrees.taskIDLabel,
            AppStrings.Worktrees.agentIDLabel,
            AppStrings.Worktrees.createdAtLabel,
            AppStrings.Worktrees.reasonsLabel,
            AppStrings.Worktrees.healthGood,
            AppStrings.Worktrees.healthStale,
            AppStrings.Worktrees.healthOverloaded,
            AppStrings.Worktrees.healthMissing,
            AppStrings.Worktrees.healthConflicted,
            { AppStrings.Worktrees.healthUnknown($0, health: "x") },
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
