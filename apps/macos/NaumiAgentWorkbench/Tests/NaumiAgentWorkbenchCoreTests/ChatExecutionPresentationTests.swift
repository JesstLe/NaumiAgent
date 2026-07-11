import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

@Suite("Chat execution presentation")
struct ChatExecutionPresentationTests {
    @Test func permissionRequestUsesSafeFieldsOnly() {
        let initial = ChatExecutionPresentation(id: "run-1")
        let next = initial.applying(
            ChatStreamEvent(
                id: "event-1",
                type: .permissionRequest,
                data: [
                    "call_id": .string("call-1"),
                    "tool_name": .string("bash_run"),
                    "reason": .string("命令执行需要确认。"),
                    "risk_level": .string("medium"),
                    "status": .string("needs_confirmation"),
                    "arguments": .object(["command": .string("echo $API_KEY")]),
                ]
            )
        )

        #expect(next.stage == .awaitingApproval)
        #expect(next.permission?.callID == "call-1")
        #expect(next.permission?.toolName == "bash_run")
        #expect(next.permission?.reason == "命令执行需要确认。")
        #expect(next.partialResponse.isEmpty)
        #expect(next.toolResultSummary == nil)
    }

    @Test func tokenDeltaBuildsPartialAnswerWithoutThinkingContent() {
        let initial = ChatExecutionPresentation(id: "run-1")
        let thinking = initial.applying(
            ChatStreamEvent(
                id: "event-1",
                type: .thinkingDelta,
                data: ["content": .string("never rendered")]
            )
        )
        let answer = thinking.applying(
            ChatStreamEvent(
                id: "event-2",
                type: .tokenDelta,
                data: ["token": .string("正在生成可见答复")]
            )
        )

        #expect(thinking.stage == .analyzing)
        #expect(thinking.partialResponse.isEmpty)
        #expect(answer.stage == .composing)
        #expect(answer.partialResponse == "正在生成可见答复")
    }

    @Test func delegateCompletionKeepsACompactResultSummary() {
        let initial = ChatExecutionPresentation(id: "run-1")
        let next = initial.applying(
            ChatStreamEvent(
                id: "event-1",
                type: .toolCallEnd,
                data: [
                    "name": .string("delegate_task"),
                    "status": .string("success"),
                    "content": .string("研究子 Agent 已整理三条新闻来源。"),
                ]
            )
        )

        #expect(next.stage == .analyzing)
        #expect(next.toolResultSummary == "研究子 Agent 已整理三条新闻来源。")
    }
}
