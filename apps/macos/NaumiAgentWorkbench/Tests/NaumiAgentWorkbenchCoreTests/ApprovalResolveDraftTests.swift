import Testing
@testable import NaumiAgentWorkbenchCore

struct ApprovalResolveDraftTests {

    @Test func defaultsToHumanActorWithEmptyNote() {
        let draft = ApprovalResolveDraft()

        #expect(draft.actor == "Human")
        #expect(draft.decisionNote == "")
        #expect(draft.trimmedActor == "Human")
        #expect(draft.trimmedDecisionNote == "")
        #expect(draft.canResolve)
    }

    @Test func trimsWhitespaceFromActorAndNote() {
        let draft = ApprovalResolveDraft(
            actor: "  Human Reviewer  ",
            decisionNote: "  Looks good  "
        )

        #expect(draft.trimmedActor == "Human Reviewer")
        #expect(draft.trimmedDecisionNote == "Looks good")
    }

    @Test func cannotResolveWithEmptyActor() {
        #expect(!ApprovalResolveDraft(actor: "", decisionNote: "").canResolve)
        #expect(!ApprovalResolveDraft(actor: "   ", decisionNote: "note").canResolve)
    }
}
