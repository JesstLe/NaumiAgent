import Testing
@testable import NaumiAgentWorkbenchCore

struct ReviewSelectionCommandTests {

    @Test func commandUsesSelectedApprovalID() throws {
        let command = try #require(ReviewSelectionCommand(
            review: review(id: "  approval-123  ")
        ))

        #expect(command.approvalID == "approval-123")
    }

    @Test func commandIsNilWhenApprovalIDIsEmpty() {
        #expect(ReviewSelectionCommand(review: review(id: "   ")) == nil)
    }

    private func review(id: String) -> ReviewDesignItem {
        ReviewDesignItem(
            id: id,
            taskID: "task-1",
            title: "允许合并",
            number: 3,
            agent: "Backend-Agent",
            worktree: "issue-3-market",
            time: "09:28",
            risk: "High",
            tone: "red"
        )
    }
}
