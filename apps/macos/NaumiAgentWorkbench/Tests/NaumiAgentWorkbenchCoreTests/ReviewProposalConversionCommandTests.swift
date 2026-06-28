import Testing
@testable import NaumiAgentWorkbenchCore

struct ReviewProposalConversionCommandTests {

    @Test func commandBuildsTemporaryDecisionFromSelectedReview() throws {
        let review = ReviewDesignItem(
            id: "approval-123",
            taskID: "task-42",
            title: "任务市场租约策略",
            number: 3,
            agent: "Backend-Agent",
            worktree: "issue-3-market",
            time: "09:28",
            risk: "High",
            tone: "red"
        )

        let command = try #require(
            ReviewProposalConversionCommand(
                review: review,
                missionID: "  mission-001  ",
                actor: "  Human Reviewer  ",
                decisionNote: "  需要先形成提案再审批  ",
                locale: .zhCN
            )
        )

        #expect(command.missionID == "mission-001")
        #expect(command.actor == "Human Reviewer")
        #expect(command.kind == "temporary")
        #expect(command.title == "提案：任务市场租约策略")
        #expect(command.content.contains("approval_id: approval-123"))
        #expect(command.content.contains("task_id: task-42"))
        #expect(command.content.contains("risk: High"))
        #expect(command.content.contains("worktree: issue-3-market"))
        #expect(command.content.contains("requested_by: Backend-Agent"))
        #expect(command.content.contains("human_note: 需要先形成提案再审批"))
    }

    @Test func commandBuildsEnglishTitleWhenLocaleIsEnglish() throws {
        let command = try #require(
            ReviewProposalConversionCommand(
                review: review(),
                missionID: "mission-001",
                actor: "Human",
                decisionNote: "",
                locale: .enUS
            )
        )

        #expect(command.title == "Proposal: Task Market Lease")
    }

    @Test func commandOmitsEmptyHumanNote() throws {
        let command = try #require(
            ReviewProposalConversionCommand(
                review: review(),
                missionID: "mission-001",
                actor: "Human",
                decisionNote: "   ",
                locale: .zhCN
            )
        )

        #expect(!command.content.contains("human_note:"))
    }

    @Test func commandIsNilWhenMissionOrActorIsMissing() {
        #expect(
            ReviewProposalConversionCommand(
                review: review(),
                missionID: "   ",
                actor: "Human",
                decisionNote: "",
                locale: .zhCN
            ) == nil
        )
        #expect(
            ReviewProposalConversionCommand(
                review: review(),
                missionID: "mission-001",
                actor: "   ",
                decisionNote: "",
                locale: .zhCN
            ) == nil
        )
    }

    private func review() -> ReviewDesignItem {
        ReviewDesignItem(
            id: "approval-123",
            taskID: "task-42",
            title: "Task Market Lease",
            number: 3,
            agent: "Backend-Agent",
            worktree: "issue-3-market",
            time: "09:28",
            risk: "High",
            tone: "red"
        )
    }
}
