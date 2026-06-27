import Testing
@testable import NaumiAgentWorkbenchCore

struct TimelineEventFilterDraftTests {

    @Test func trimsAllFilterFields() {
        let draft = TimelineEventFilterDraft(
            eventType: "  validation.failed  ",
            actor: "  Test-Agent  ",
            subjectID: "  task-7  "
        )

        #expect(draft.trimmedEventType == "validation.failed")
        #expect(draft.trimmedActor == "Test-Agent")
        #expect(draft.trimmedSubjectID == "task-7")
        #expect(draft.hasFilters)
    }

    @Test func emptyDraftHasNoFilters() {
        let draft = TimelineEventFilterDraft(
            eventType: "  ",
            actor: "",
            subjectID: "\n"
        )

        #expect(draft.trimmedEventType == "")
        #expect(draft.trimmedActor == "")
        #expect(draft.trimmedSubjectID == "")
        #expect(!draft.hasFilters)
    }

    @Test func queryValuesUseNilForEmptyFields() {
        let draft = TimelineEventFilterDraft(
            eventType: "mission.created",
            actor: " ",
            subjectID: "mission-1"
        )

        #expect(draft.eventTypeQueryValue == "mission.created")
        #expect(draft.actorQueryValue == nil)
        #expect(draft.subjectIDQueryValue == "mission-1")
    }
}
