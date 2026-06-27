import Testing
@testable import NaumiAgentWorkbenchCore

struct IssueCreationDraftTests {
    @Test func trimsFieldsAndSplitsAcceptanceCriteriaByLine() {
        let draft = IssueCreationDraft(
            missionID: " mission-001 ",
            title: "  实现 issue 创建闭环  ",
            description: "  从任务市场创建真实 issue  ",
            blockedByText: " task-1 \n\n task-2 ",
            acceptanceCriteriaText: " 可在任务市场看到 \n\n 可被 Agent 认领 ",
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(draft.trimmedMissionID == "mission-001")
        #expect(draft.trimmedTitle == "实现 issue 创建闭环")
        #expect(draft.trimmedDescription == "从任务市场创建真实 issue")
        #expect(draft.blockedBy == ["task-1", "task-2"])
        #expect(draft.acceptanceCriteria == ["可在任务市场看到", "可被 Agent 认领"])
        #expect(draft.parallelMode == "cooperative")
        #expect(draft.riskLevel == "high")
        #expect(draft.canSubmit)
    }

    @Test func cannotSubmitWithoutMissionTitleOrDescription() {
        #expect(!IssueCreationDraft(missionID: "", title: "Title", description: "Description").canSubmit)
        #expect(!IssueCreationDraft(missionID: "mission-1", title: "", description: "Description").canSubmit)
        #expect(!IssueCreationDraft(missionID: "mission-1", title: "Title", description: "  ").canSubmit)
    }
}
