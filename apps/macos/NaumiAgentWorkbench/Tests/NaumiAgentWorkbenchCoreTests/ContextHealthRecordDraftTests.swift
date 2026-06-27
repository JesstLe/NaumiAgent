import Testing
@testable import NaumiAgentWorkbenchCore

struct ContextHealthRecordDraftTests {
    @Test func trimsFieldsAndNormalizesMetrics() {
        let draft = ContextHealthRecordDraft(
            taskID: " task-001 ",
            agentID: " Backend-Agent ",
            minutesSinceSync: 42,
            tokenLoadPercent: 87,
            policyConflict: true,
            actor: " Human "
        )

        #expect(draft.trimmedTaskID == "task-001")
        #expect(draft.trimmedAgentID == "Backend-Agent")
        #expect(draft.minutesSinceSync == 42)
        #expect(draft.tokenLoadRatio == 0.87)
        #expect(draft.policyConflict)
        #expect(draft.trimmedActor == "Human")
        #expect(draft.canSubmit)
    }

    @Test func cannotSubmitWithoutTaskAgentOrActor() {
        #expect(!ContextHealthRecordDraft(taskID: "", agentID: "Agent", actor: "Human").canSubmit)
        #expect(!ContextHealthRecordDraft(taskID: "task-1", agentID: "", actor: "Human").canSubmit)
        #expect(!ContextHealthRecordDraft(taskID: "task-1", agentID: "Agent", actor: "  ").canSubmit)
    }
}
