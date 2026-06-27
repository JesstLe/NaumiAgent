import Testing
@testable import NaumiAgentWorkbenchCore

struct ReviewApprovalResolutionCommandTests {

    @Test func approvedCommandUsesSelectedApprovalAndTrimmedDraft() throws {
        let review = ReviewDesignItem(
            id: "approval-123",
            taskID: "task-1",
            title: "允许合并",
            number: 3,
            agent: "Backend-Agent",
            worktree: "issue-3-market",
            time: "09:28",
            risk: "High",
            tone: "red"
        )
        let draft = ApprovalResolveDraft(
            actor: "  Human Reviewer  ",
            decisionNote: "  验证通过，同意合并  "
        )

        let command = try #require(
            ReviewApprovalResolutionCommand(
                review: review,
                draft: draft,
                state: .approved
            )
        )

        #expect(command.approvalID == "approval-123")
        #expect(command.actor == "Human Reviewer")
        #expect(command.state == "approved")
        #expect(command.decisionNote == "验证通过，同意合并")
    }

    @Test func rejectedCommandUsesRejectedState() throws {
        let review = ReviewDesignItem(
            id: "approval-123",
            taskID: "task-1",
            title: "允许合并",
            number: 3,
            agent: "Backend-Agent",
            worktree: "issue-3-market",
            time: "09:28",
            risk: "High",
            tone: "red"
        )

        let command = try #require(
            ReviewApprovalResolutionCommand(
                review: review,
                draft: ApprovalResolveDraft(actor: "Human", decisionNote: "需要补测试"),
                state: .rejected
            )
        )

        #expect(command.approvalID == "approval-123")
        #expect(command.state == "rejected")
        #expect(command.decisionNote == "需要补测试")
    }

    @Test func commandIsNilWhenActorIsMissing() {
        let review = ReviewDesignItem(
            id: "approval-123",
            taskID: "task-1",
            title: "允许合并",
            number: 3,
            agent: "Backend-Agent",
            worktree: "issue-3-market",
            time: "09:28",
            risk: "High",
            tone: "red"
        )

        #expect(
            ReviewApprovalResolutionCommand(
                review: review,
                draft: ApprovalResolveDraft(actor: "   ", decisionNote: "ok"),
                state: .approved
            ) == nil
        )
    }
}
