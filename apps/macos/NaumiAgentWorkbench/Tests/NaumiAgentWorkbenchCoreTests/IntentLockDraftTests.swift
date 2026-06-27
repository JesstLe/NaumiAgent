import Testing
@testable import NaumiAgentWorkbenchCore

struct IntentLockDraftTests {

    @Test func defaultsToHumanActorAndHighRisk() {
        let draft = IntentLockDraft()

        #expect(draft.missionID == "")
        #expect(draft.actor == "Human")
        #expect(draft.rule == "")
        #expect(draft.blockedPathsText == "")
        #expect(draft.allowedPathsText == "")
        #expect(draft.requireProposalForRisk == "high")
        #expect(draft.trimmedMissionID.isEmpty)
        #expect(draft.trimmedActor == "Human")
        #expect(draft.trimmedRule.isEmpty)
    }

    @Test func trimsAndSplitsPathsByCommaAndNewline() {
        let draft = IntentLockDraft(
            missionID: "",
            actor: "",
            rule: "",
            blockedPathsText: " /etc/passwd , /secret\n /tmp ",
            allowedPathsText: "/src, ,\n/allowed",
            requireProposalForRisk: "low"
        )

        #expect(draft.blockedPaths == ["/etc/passwd", "/secret", "/tmp"])
        #expect(draft.allowedPaths == ["/src", "/allowed"])
    }

    @Test func emptyPathsBecomeEmptyArray() {
        let draft = IntentLockDraft(
            blockedPathsText: "  , \n  ",
            allowedPathsText: ""
        )

        #expect(draft.blockedPaths.isEmpty)
        #expect(draft.allowedPaths.isEmpty)
    }

    @Test func canSubmitRequiresMissionIDActorAndRule() {
        #expect(
            IntentLockDraft(
                missionID: "mission-1",
                actor: "Human",
                rule: "deny-root"
            ).canSubmit
        )
        #expect(
            !IntentLockDraft(
                missionID: "",
                actor: "Human",
                rule: "deny-root"
            ).canSubmit
        )
        #expect(
            !IntentLockDraft(
                missionID: "mission-1",
                actor: "   ",
                rule: "deny-root"
            ).canSubmit
        )
        #expect(
            !IntentLockDraft(
                missionID: "mission-1",
                actor: "Human",
                rule: "  "
            ).canSubmit
        )
    }
}
