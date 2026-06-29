import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardEventSelectionCommandTests {

    @Test func commandUsesSelectedEventID() throws {
        let command = try #require(DashboardEventSelectionCommand(
            event: event(id: "  evt-123  ")
        ))

        #expect(command.eventID == "evt-123")
    }

    @Test func commandIsNilWhenEventIDIsEmpty() {
        #expect(DashboardEventSelectionCommand(event: event(id: "   ")) == nil)
    }

    private func event(id: String) -> DashboardEventRow {
        DashboardEventRow(
            id: id,
            type: "issue.claimed",
            actor: "Backend-Agent",
            subjectID: "task-1",
            timestamp: "2026-06-27T10:00:00Z"
        )
    }
}
