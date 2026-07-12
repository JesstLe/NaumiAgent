import Testing
@testable import NaumiAgentWorkbenchCore

struct ChatArtifactPresentationTests {
    @Test func delegateCompletionCreatesSubagentArtifactWithoutArguments() {
        let result = ChatExecutionPresentation(id: "run").applying(
            ChatStreamEvent(
                id: "event-end",
                type: .toolCallEnd,
                data: [
                    "name": .string("delegate_task"),
                    "call_id": .string("call-1"),
                    "status": .string("success"),
                    "content": .string("UI-PING"),
                    "arguments": .object(["prompt": .string("secret prompt")]),
                ]
            )
        )

        #expect(result.artifacts.count == 1)
        #expect(result.artifacts.first?.kind == .subagent)
        #expect(result.artifacts.first?.summary == "UI-PING")
        #expect(result.artifacts.first?.summary.contains("secret prompt") == false)
    }
}
