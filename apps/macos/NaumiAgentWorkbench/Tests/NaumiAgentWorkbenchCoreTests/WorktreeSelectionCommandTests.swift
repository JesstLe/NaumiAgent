import Testing
@testable import NaumiAgentWorkbenchCore

struct WorktreeSelectionCommandTests {

    @Test func commandUsesSelectedWorktreeName() throws {
        let command = try #require(WorktreeSelectionCommand(
            worktree: worktreeRow(name: "  wt-api-client  ")
        ))

        #expect(command.name == "wt-api-client")
    }

    @Test func commandIsNilWhenWorktreeNameIsEmpty() {
        #expect(WorktreeSelectionCommand(worktree: worktreeRow(name: "   ")) == nil)
    }

    private func worktreeRow(name: String) -> WorktreeManagementRow {
        WorktreeManagementRow(worktree: WorktreeDTO(
            name: name,
            path: "/repo/.naumi/worktrees/\(name)",
            branch: "naumi/worktree-\(name)",
            baseRef: "main",
            status: "active",
            taskID: "task-001",
            dirtyFiles: 0,
            commitsAhead: 0,
            createdAt: "2026-06-27T09:00:00+00:00",
            updatedAt: "2026-06-27T09:05:00+00:00",
            keptReason: "",
            metadata: [:],
            removable: true
        ))
    }
}
