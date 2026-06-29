import Testing
@testable import NaumiAgentWorkbenchCore

struct TimelineEventSelectionCommandTests {

    @Test func commandUsesSelectedEventID() throws {
        let command = try #require(TimelineEventSelectionCommand(
            event: eventPresentation(id: "  evt-123  ")
        ))

        #expect(command.eventID == "evt-123")
    }

    @Test func commandIsNilWhenEventIDIsEmpty() {
        #expect(TimelineEventSelectionCommand(event: eventPresentation(id: "   ")) == nil)
    }

    private func eventPresentation(id: String) -> TimelineEventPresentation {
        TimelineEventPresentation(event: EventDTO(
            id: id,
            sessionID: "sess-001",
            type: "issue.claimed",
            actor: "Backend-Agent",
            subjectID: "task-001",
            payload: [:],
            timestamp: "2026-06-27T10:00:00+00:00"
        ))
    }
}
