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

    @Test func scrollSignalChangesForMessagesAndStreamingOutput() {
        let message = ChatMessageDTO(
            id: "message-1",
            role: "user",
            content: "开始",
            timestamp: "2026-07-13T08:00:00Z",
            metadata: [:]
        )
        let execution = ChatExecutionPresentation(id: "run-1")
        let initial = ChatConversationScrollSignal(
            messages: [message],
            execution: execution
        )
        let streamed = ChatConversationScrollSignal(
            messages: [message],
            execution: execution.applying(
                ChatStreamEvent(
                    id: "token-1",
                    type: .tokenDelta,
                    data: ["token": .string("正在继续")]
                )
            )
        )
        let withAnotherMessage = ChatConversationScrollSignal(
            messages: [
                message,
                ChatMessageDTO(
                    id: "message-2",
                    role: "assistant",
                    content: "完成",
                    timestamp: "2026-07-13T08:00:02Z",
                    metadata: [:]
                ),
            ],
            execution: nil
        )

        #expect(initial != streamed)
        #expect(streamed != withAnotherMessage)
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
