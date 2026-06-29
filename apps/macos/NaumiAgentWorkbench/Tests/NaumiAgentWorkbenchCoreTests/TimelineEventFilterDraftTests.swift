import Testing
@testable import NaumiAgentWorkbenchCore

struct TimelineEventFilterDraftTests {

    @Test func trimsAllFilterFields() {
        let draft = TimelineEventFilterDraft(
            eventType: "  validation.failed  ",
            actor: "  Test-Agent  ",
            subjectID: "  task-7  ",
            since: "  2026-06-27T10:00:00+00:00  "
        )

        #expect(draft.trimmedEventType == "validation.failed")
        #expect(draft.trimmedActor == "Test-Agent")
        #expect(draft.trimmedSubjectID == "task-7")
        #expect(draft.trimmedSince == "2026-06-27T10:00:00+00:00")
        #expect(draft.hasFilters)
    }

    @Test func emptyDraftHasNoFilters() {
        let draft = TimelineEventFilterDraft(
            eventType: "  ",
            actor: "",
            subjectID: "\n",
            since: "\t"
        )

        #expect(draft.trimmedEventType == "")
        #expect(draft.trimmedActor == "")
        #expect(draft.trimmedSubjectID == "")
        #expect(draft.trimmedSince == "")
        #expect(!draft.hasFilters)
    }

    @Test func queryValuesUseNilForEmptyFields() {
        let draft = TimelineEventFilterDraft(
            eventType: "mission.created",
            actor: " ",
            subjectID: "mission-1",
            since: " 2026-06-27T10:00:00+00:00 "
        )

        #expect(draft.eventTypeQueryValue == "mission.created")
        #expect(draft.actorQueryValue == nil)
        #expect(draft.subjectIDQueryValue == "mission-1")
        #expect(draft.sinceQueryValue == "2026-06-27T10:00:00+00:00")
    }
}
