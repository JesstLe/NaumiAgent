import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketBidRejectionCommandTests {

    @Test func commandBuildsRejectionDecisionFromSelectedIssueAndBid() throws {
        let command = try #require(TaskMarketBidRejectionCommand(
            issue: issue(taskID: " task-123 "),
            bid: bid(agent: " Backend-Agent "),
            missionID: " mission-001 ",
            locale: .zhCN
        ))

        #expect(command.missionID == "mission-001")
        #expect(command.actor == "Human")
        #expect(command.kind == "temporary")
        #expect(command.title == "拒绝竞标：实现 API Client")
        #expect(command.content.contains("task_id: task-123"))
        #expect(command.content.contains("risk: High"))
        #expect(command.content.contains("parallel_mode: exclusive"))
        #expect(command.content.contains("bid_agent: Backend-Agent"))
        #expect(command.content.contains("bid_confidence: 0.82"))
        #expect(command.content.contains("rejection_reason: Human rejected this bid in the task market."))
    }

    @Test func englishTitleUsesBidRejectionPrefix() throws {
        let command = try #require(TaskMarketBidRejectionCommand(
            issue: issue(taskID: "task-123"),
            bid: bid(agent: "Reviewer-Agent"),
            missionID: "mission-001",
            locale: .enUS
        ))

        #expect(command.title == "Bid Rejected: 实现 API Client")
    }

    @Test func commandIsNilWhenMissionIDIsEmpty() {
        #expect(TaskMarketBidRejectionCommand(
            issue: issue(taskID: "task-123"),
            bid: bid(agent: "Backend-Agent"),
            missionID: "   ",
            locale: .zhCN
        ) == nil)
    }

    @Test func commandIsNilWhenTaskIDIsEmpty() {
        #expect(TaskMarketBidRejectionCommand(
            issue: issue(taskID: "   "),
            bid: bid(agent: "Backend-Agent"),
            missionID: "mission-001",
            locale: .zhCN
        ) == nil)
    }

    @Test func commandIsNilWhenBidAgentIsEmpty() {
        #expect(TaskMarketBidRejectionCommand(
            issue: issue(taskID: "task-123"),
            bid: bid(agent: "   "),
            missionID: "mission-001",
            locale: .zhCN
        ) == nil)
    }

    private func issue(taskID: String) -> TaskMarketDesignIssue {
        TaskMarketDesignIssue(
            number: 1,
            taskID: taskID,
            title: "实现 API Client",
            detail: "Expose API client details.",
            parallelMode: "exclusive",
            risk: "High",
            dependency: "-",
            bids: 2,
            lease: "Requires proposal",
            worktree: "wt-api-client",
            status: "Requires proposal",
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
