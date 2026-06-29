import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardWorktreeSelectionCommandTests {

    @Test func commandUsesSelectedWorktreeNodeTitle() throws {
        let command = try #require(DashboardWorktreeSelectionCommand(
            node: node(kind: .worktrees, title: "  wt-api-client  ")
        ))

        #expect(command.name == "wt-api-client")
    }

    @Test func commandIsNilWhenNodeIsNotWorktree() {
        #expect(DashboardWorktreeSelectionCommand(
            node: node(kind: .validation, title: "wt-api-client")
        ) == nil)
    }

    @Test func commandIsNilWhenWorktreeNameIsEmpty() {
        #expect(DashboardWorktreeSelectionCommand(
            node: node(kind: .worktrees, title: "   ")
        ) == nil)
    }

    private func node(kind: DashboardCanvasNodeKind, title: String) -> DashboardCanvasNode {
        DashboardCanvasNode(
            id: "worktrees",
            kind: kind,
            title: title,
            subtitle: "Git Worktrees",
            status: "active"
        )
    }
}
