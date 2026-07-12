import Testing
@testable import NaumiAgentWorkbenchCore

struct ChatPresentationTests {
    @Test func assistantUsesDocumentStyleAndUserUsesCompactBubble() {
        #expect(ChatPresentation.style(forRole: "assistant") == .document)
        #expect(ChatPresentation.style(forRole: "user") == .compactBubble)
    }

    @Test func issuesAreRiskSortedWithoutInventingCounts() {
        let issues = [
            issue(id: "medium", risk: "medium"),
            issue(id: "critical", risk: "critical"),
            issue(id: "low", risk: "low"),
            issue(id: "high", risk: "high"),
        ]

        let summaries = ChatPresentation.issueSummaries(
            from: issues,
            taskTitlesByID: ["critical": "关键任务"]
        )

        #expect(summaries.map(\.id) == ["critical", "high", "medium", "low"])
        #expect(summaries.map(\.title) == ["关键任务", "high", "medium", "low"])
    }

    @Test func unknownRolesRemainReadableDocuments() {
        #expect(ChatPresentation.style(forRole: "custom") == .document)
    }

    private func issue(id: String, risk: String) -> IssueDTO {
        IssueDTO(
            sessionID: "session",
            taskID: id,
            missionID: "mission",
            parallelMode: "exclusive",
            riskLevel: risk,
            requiresHumanApproval: false,
            acceptanceCriteria: [],
            expectedArtifacts: [],
            relatedBranch: "",
            relatedWorktree: "",
            relatedPR: "",
            createdAt: "",
            updatedAt: ""
        )
    }
}
