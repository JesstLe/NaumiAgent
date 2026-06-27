import Testing
@testable import NaumiAgentWorkbenchCore

struct IssueAttachmentDraftTests {
    @Test func trimsFieldsAndSplitsAcceptanceCriteriaByLine() {
        let draft = IssueAttachmentDraft(
            missionID: " mission-001 ",
            taskID: " task-001 ",
            acceptanceCriteriaText: " 通过现有任务挂载 \n\n 可被 Agent 认领 ",
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(draft.trimmedMissionID == "mission-001")
        #expect(draft.trimmedTaskID == "task-001")
        #expect(draft.acceptanceCriteria == ["通过现有任务挂载", "可被 Agent 认领"])
        #expect(draft.parallelMode == "cooperative")
        #expect(draft.riskLevel == "high")
        #expect(draft.canSubmit)
    }

    @Test func cannotSubmitWithoutMissionOrTask() {
        #expect(!IssueAttachmentDraft(missionID: "", taskID: "task-1").canSubmit)
        #expect(!IssueAttachmentDraft(missionID: "mission-1", taskID: "").canSubmit)
    }
}
