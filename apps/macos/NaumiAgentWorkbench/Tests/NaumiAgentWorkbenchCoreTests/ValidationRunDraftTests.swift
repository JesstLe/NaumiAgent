import Testing
@testable import NaumiAgentWorkbenchCore

struct ValidationRunDraftTests {
    @Test func trimsRequiredFieldsAndSplitsCommandByWhitespace() {
        let draft = ValidationRunDraft(
            taskID: " task-001 ",
            actor: " ValidationRunner ",
            commandLine: "  pytest   tests/unit  -x ",
            cwd: " /workspace "
        )

        #expect(draft.trimmedTaskID == "task-001")
        #expect(draft.trimmedActor == "ValidationRunner")
        #expect(draft.argv == ["pytest", "tests/unit", "-x"])
        #expect(draft.normalizedCWD == "/workspace")
        #expect(draft.canSubmit)
    }

    @Test func emptyWorkingDirectoryBecomesNil() {
        let draft = ValidationRunDraft(
            taskID: "task-001",
            actor: "ValidationRunner",
            commandLine: "ruff check src",
            cwd: "  "
        )

        #expect(draft.normalizedCWD == nil)
    }

    @Test func cannotSubmitWithoutTaskActorOrCommand() {
        #expect(!ValidationRunDraft(taskID: "", actor: "ValidationRunner", commandLine: "pytest").canSubmit)
        #expect(!ValidationRunDraft(taskID: "task-001", actor: "", commandLine: "pytest").canSubmit)
        #expect(!ValidationRunDraft(taskID: "task-001", actor: "ValidationRunner", commandLine: "  ").canSubmit)
    }
}
