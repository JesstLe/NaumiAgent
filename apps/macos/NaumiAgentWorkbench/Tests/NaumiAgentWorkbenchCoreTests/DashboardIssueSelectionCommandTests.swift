import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardIssueSelectionCommandTests {

    @Test func commandUsesSelectedIssueTaskID() throws {
        let command = try #require(DashboardIssueSelectionCommand(
            issue: issue(taskID: "  task-123  ")
        ))

        #expect(command.taskID == "task-123")
    }

    @Test func commandIsNilWhenTaskIDIsEmpty() {
        #expect(DashboardIssueSelectionCommand(issue: issue(taskID: "   ")) == nil)
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
            worktree: "-",
            status: "Requires proposal",
            tag: "backend"
        )
    }
}
