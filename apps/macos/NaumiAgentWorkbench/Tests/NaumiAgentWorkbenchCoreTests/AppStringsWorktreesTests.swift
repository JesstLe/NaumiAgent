import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsWorktreesTests {

    @Test func worktreesStringsZhCN() {
        #expect(AppStrings.Worktrees.title(.zhCN) == "工作区")
        #expect(AppStrings.Worktrees.subtitle(.zhCN) == "Git 工作区 / 上下文健康 / Agent 负载")
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
        #expect(AppStrings.Worktrees.recordContextButton(.zhCN) == "记录上下文")
        #expect(AppStrings.Worktrees.recordContextSectionTitle(.zhCN) == "记录上下文健康")
        #expect(AppStrings.Worktrees.minutesSinceSyncLabel(.zhCN) == "距上次同步（分钟）")
        #expect(AppStrings.Worktrees.tokenLoadLabel(.zhCN) == "Token 负载")
        #expect(AppStrings.Worktrees.policyConflictLabel(.zhCN) == "存在策略冲突")
        #expect(AppStrings.Worktrees.recordContextSubmitButton(.zhCN) == "保存快照")
        #expect(AppStrings.Worktrees.processingLabel(.zhCN) == "处理中…")
    }

    @Test func worktreesStringsEnUS() {
        #expect(AppStrings.Worktrees.title(.enUS) == "Worktrees")
        #expect(AppStrings.Worktrees.subtitle(.enUS) == "Git worktrees, context health, and agent load")
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
        #expect(AppStrings.Worktrees.recordContextButton(.enUS) == "Record Context")
        #expect(AppStrings.Worktrees.recordContextSectionTitle(.enUS) == "Record Context Health")
        #expect(AppStrings.Worktrees.minutesSinceSyncLabel(.enUS) == "Minutes Since Sync")
        #expect(AppStrings.Worktrees.tokenLoadLabel(.enUS) == "Token Load")
        #expect(AppStrings.Worktrees.policyConflictLabel(.enUS) == "Policy Conflict")
        #expect(AppStrings.Worktrees.recordContextSubmitButton(.enUS) == "Save Snapshot")
        #expect(AppStrings.Worktrees.processingLabel(.enUS) == "Processing…")
    }

    @Test func allWorktreesStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Worktrees.title,
            AppStrings.Worktrees.subtitle,
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
            AppStrings.Worktrees.recordContextButton,
            AppStrings.Worktrees.recordContextSectionTitle,
            AppStrings.Worktrees.minutesSinceSyncLabel,
            AppStrings.Worktrees.tokenLoadLabel,
            AppStrings.Worktrees.policyConflictLabel,
            AppStrings.Worktrees.recordContextSubmitButton,
            AppStrings.Worktrees.processingLabel,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
