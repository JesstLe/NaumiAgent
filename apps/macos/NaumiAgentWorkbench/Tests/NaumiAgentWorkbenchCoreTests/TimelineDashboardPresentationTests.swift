import Testing
@testable import NaumiAgentWorkbenchCore

struct TimelineDashboardPresentationTests {

    @Test func summarizesEventBucketsAndActors() {
        let presentation = TimelineDashboardPresentation(events: [
            makeEvent(id: "1", type: "mission.created", actor: "Human"),
            makeEvent(id: "2", type: "task.updated", actor: "Agent"),
            makeEvent(id: "3", type: "task.updated", actor: "Agent"),
        ])

        #expect(presentation.totalCount == 3)
        #expect(presentation.actorCount == 2)
        #expect(presentation.typeBuckets.map(\.type) == ["task.updated", "mission.created"])
        #expect(presentation.typeBuckets.map(\.count) == [2, 1])
        #expect(presentation.latestEvent?.id == "3")
        #expect(presentation.actorBuckets.map(\.actor) == ["Agent", "Human"])
        #expect(presentation.actorBuckets.map(\.count) == [2, 1])
        #expect(presentation.causalChain.map(\.eventID) == ["3", "2", "1"])
    }

    @Test func emptyTimelineUsesNilLatestEvent() {
        let presentation = TimelineDashboardPresentation(events: [])

        #expect(presentation.totalCount == 0)
        #expect(presentation.latestEvent == nil)
        #expect(presentation.typeBuckets.isEmpty)
        #expect(presentation.actorBuckets.isEmpty)
        #expect(presentation.causalChain.isEmpty)
    }

    @Test func causalChainKeepsOnlyMostRecentSixEvents() {
        let events = (1...8).map { index in
            makeEvent(id: "\(index)", type: "task.updated", actor: "Agent")
        }

        let presentation = TimelineDashboardPresentation(events: events)

        #expect(presentation.causalChain.map(\.eventID) == ["8", "7", "6", "5", "4", "3"])
    }

    private func makeEvent(id: String, type: String, actor: String) -> EventDTO {
        EventDTO(
            id: id,
            sessionID: "sess-1",
            type: type,
            actor: actor,
            subjectID: "subject-\(id)",
            payload: ["id": .string(id)],
            timestamp: "2026-06-27T09:0\(id):00"
        )
    }
}
