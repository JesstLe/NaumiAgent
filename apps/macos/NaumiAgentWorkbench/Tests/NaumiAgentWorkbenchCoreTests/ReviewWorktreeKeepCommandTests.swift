import Testing
@testable import NaumiAgentWorkbenchCore

struct ReviewWorktreeKeepCommandTests {

    @Test func commandUsesMatchingSnapshotWorktreeNameAndLocalizedReason() throws {
        let command = try #require(
            ReviewWorktreeKeepCommand(
                review: review(taskID: "task-42", worktree: "issue-3-market"),
                worktrees: [
                    worktree(name: "wt-unrelated", taskID: "task-1"),
                    worktree(name: "wt-review-real", taskID: "task-42"),
                ],
                actor: "  Human Reviewer  ",
                locale: .zhCN
            )
        )

        #expect(command.name == "wt-review-real")
        #expect(command.actor == "Human Reviewer")
        #expect(command.reason == "人工保留 wt-review-real，等待审查：任务市场租约策略")
    }

    @Test func commandFallsBackToReviewWorktreeWhenSnapshotHasNoMatch() throws {
        let command = try #require(
            ReviewWorktreeKeepCommand(
                review: review(taskID: "task-42", worktree: " issue-3-market "),
                worktrees: [worktree(name: "wt-other", taskID: "task-1")],
                actor: "Human",
                locale: .enUS
            )
        )

        #expect(command.name == "issue-3-market")
        #expect(command.reason == "Keep issue-3-market for review: 任务市场租约策略")
    }

    @Test func commandIsNilWhenActorOrWorktreeNameIsMissing() {
        #expect(
            ReviewWorktreeKeepCommand(
                review: review(taskID: "task-42", worktree: "issue-3-market"),
                worktrees: [],
                actor: "   ",
                locale: .zhCN
            ) == nil
        )

        #expect(
            ReviewWorktreeKeepCommand(
                review: review(taskID: "task-42", worktree: "   "),
                worktrees: [],
                actor: "Human",
                locale: .zhCN
            ) == nil
        )
    }

    private func review(taskID: String, worktree: String) -> ReviewDesignItem {
        ReviewDesignItem(
            id: "approval-123",
            taskID: taskID,
            title: "任务市场租约策略",
            number: 3,
            agent: "Backend-Agent",
            worktree: worktree,
            time: "09:28",
            risk: "High",
            tone: "red"
        )
    }

    private func worktree(name: String, taskID: String) -> WorktreeDTO {
        WorktreeDTO(
            name: name,
            path: "/repo/.naumi/worktrees/\(name)",
            branch: "naumi/worktree-\(name)",
            baseRef: "main",
            status: "active",
            taskID: taskID,
            dirtyFiles: 0,
            commitsAhead: 0,
            createdAt: "2026-06-27T09:00:00",
            updatedAt: "2026-06-27T09:05:00",
            keptReason: "",
            metadata: [:],
            removable: false
        )
    }
}
