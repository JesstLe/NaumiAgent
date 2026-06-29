import Testing
@testable import NaumiAgentWorkbenchCore

struct ContextSnapshotSelectionCommandTests {

    @Test func commandUsesSelectedSnapshotID() throws {
        let command = try #require(ContextSnapshotSelectionCommand(
            snapshot: snapshotPresentation(id: "  ctx-123  ")
        ))

        #expect(command.snapshotID == "ctx-123")
    }

    @Test func commandIsNilWhenSnapshotIDIsEmpty() {
        #expect(ContextSnapshotSelectionCommand(snapshot: snapshotPresentation(id: "   ")) == nil)
    }

    private func snapshotPresentation(id: String) -> ContextSnapshotPresentation {
        ContextSnapshotPresentation(snapshot: ContextSnapshotDTO(
            id: id,
            sessionID: "sess-001",
            agentID: "Backend-Agent",
            taskID: "task-001",
            health: "stale",
            reasons: ["Files analyzed 18m ago"],
            createdAt: "2026-06-27T10:00:00+00:00"
        ))
    }
}
