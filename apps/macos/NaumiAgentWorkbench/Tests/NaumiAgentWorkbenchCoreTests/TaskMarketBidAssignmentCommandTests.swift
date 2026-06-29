import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketBidAssignmentCommandTests {

    @Test func commandBuildsClaimFromSelectedIssueAndBid() throws {
        let command = try #require(TaskMarketBidAssignmentCommand(
            issue: issue(taskID: " task-123 ", worktree: " wt-api-client "),
            bid: bid(agent: " Backend-Agent "),
            durationMinutes: 60
        ))

        #expect(command.taskID == "task-123")
        #expect(command.agentID == "Backend-Agent")
        #expect(command.durationMinutes == 60)
        #expect(command.worktreeName == "wt-api-client")
    }

    @Test func commandUsesDefaultWorktreeWhenIssueWorktreeIsPlaceholder() throws {
        let command = try #require(TaskMarketBidAssignmentCommand(
            issue: issue(taskID: "task-123", worktree: "-"),
            bid: bid(agent: "Backend-Agent"),
            durationMinutes: 45
        ))

        #expect(command.worktreeName == "wt-task-123")
    }

    @Test func commandClampsDurationToSupportedRange() throws {
        let low = try #require(TaskMarketBidAssignmentCommand(
            issue: issue(taskID: "task-123", worktree: "-"),
            bid: bid(agent: "Backend-Agent"),
            durationMinutes: -5
        ))
        let high = try #require(TaskMarketBidAssignmentCommand(
            issue: issue(taskID: "task-123", worktree: "-"),
            bid: bid(agent: "Backend-Agent"),
            durationMinutes: 360
        ))

        #expect(low.durationMinutes == 1)
        #expect(high.durationMinutes == 240)
    }

    @Test func commandIsNilWhenIssueCannotBeClaimed() {
        #expect(TaskMarketBidAssignmentCommand(
            issue: issue(taskID: "task-123", worktree: "-", status: "Leased"),
            bid: bid(agent: "Backend-Agent"),
            durationMinutes: 45
        ) == nil)
    }

    @Test func commandIsNilWhenTaskIDIsEmpty() {
        #expect(TaskMarketBidAssignmentCommand(
            issue: issue(taskID: "   ", worktree: "-"),
            bid: bid(agent: "Backend-Agent"),
            durationMinutes: 45
        ) == nil)
    }

    @Test func commandIsNilWhenBidAgentIsEmpty() {
        #expect(TaskMarketBidAssignmentCommand(
            issue: issue(taskID: "task-123", worktree: "-"),
            bid: bid(agent: "   "),
            durationMinutes: 45
        ) == nil)
    }

    private func issue(
        taskID: String,
        worktree: String,
        status: String = "Requires proposal"
    ) -> TaskMarketDesignIssue {
        TaskMarketDesignIssue(
            number: 1,
            taskID: taskID,
            title: "实现 API Client",
            detail: "Expose API client details.",
            parallelMode: "exclusive",
            risk: "High",
            dependency: "-",
            bids: 2,
            lease: status == "Leased" ? "42m remaining" : "Requires proposal",
            worktree: worktree,
            status: status,
            tag: "backend"
        )
    }

    private func bid(agent: String) -> TaskMarketDesignBid {
        TaskMarketDesignBid(
            agent: agent,
            confidence: "0.82",
            estimate: "6 files",
            eta: "2h 40m",
            note: "Medium complexity. Needs robust concurrency tests.",
            isLatest: true
        )
    }
}
