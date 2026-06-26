import Testing
@testable import NaumiAgentWorkbenchCore

struct TimelineEventPresentationTests {

    @Test func payloadSummarySortsKeysAndFormatsScalars() {
        let event = EventDTO(
            id: "evt-1",
            sessionID: "sess-1",
            type: "mission.updated",
            actor: "Agent",
            subjectID: "mission-1",
            payload: [
                "count": .number(42),
                "title": .string("Hello"),
                "active": .bool(true),
            ],
            timestamp: "2026-06-27T06:00:00"
        )

        let presentation = TimelineEventPresentation(event: event)

        #expect(presentation.payloadSummary == "active=true, count=42, title=Hello")
    }

    @Test func payloadSummaryHandlesNestedCollections() {
        let event = EventDTO(
            id: "evt-2",
            sessionID: "sess-1",
            type: "task.batch",
            actor: "Agent",
            subjectID: "task-1",
            payload: [
                "items": .array([.string("a"), .string("b")]),
                "meta": .object(["nested": .string("value")]),
            ],
            timestamp: "2026-06-27T06:00:00"
        )

        let presentation = TimelineEventPresentation(event: event)

        #expect(presentation.payloadSummary == "items=[2]: a, meta={1}")
    }

    @Test func payloadSummaryIsEmptyForEmptyPayload() {
        let event = EventDTO(
            id: "evt-3",
            sessionID: "sess-1",
            type: "ping",
            actor: "System",
            subjectID: "sess-1",
            payload: [:],
            timestamp: "2026-06-27T06:00:00"
        )

        let presentation = TimelineEventPresentation(event: event)

        #expect(presentation.payloadSummary.isEmpty)
    }

    @Test func presentationCopiesTopLevelFields() {
        let event = EventDTO(
            id: "evt-4",
            sessionID: "sess-1",
            type: "mission.created",
            actor: "Human",
            subjectID: "mission-4",
            payload: ["title": .string("Mac Workbench")],
            timestamp: "2026-06-27T06:00:00"
        )

        let presentation = TimelineEventPresentation(event: event)

        #expect(presentation.id == "evt-4")
        #expect(presentation.type == "mission.created")
        #expect(presentation.actor == "Human")
        #expect(presentation.subjectID == "mission-4")
        #expect(presentation.timestamp == "2026-06-27T06:00:00")
    }
}
