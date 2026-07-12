import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

@Suite("Chat execution presentation")
struct ChatExecutionPresentationTests {

    @Test func streamEventCapturesServerRunIDAndCanBecomeCancelled() {
        let execution = ChatExecutionPresentation(id: "local-run")
            .applying(
                ChatStreamEvent(
                    id: "evt-1",
                    type: .turnStart,
                    data: ["run_id": .string("server-run")]
                )
            )
            .cancelling()

        #expect(execution.serverRunID == "server-run")
        #expect(execution.stage == .cancelled)
        #expect(execution.completedAt != nil)
    }

    @Test func persistedRunRestoresStableTimelineAndArtifacts() {
        let run = ChatRunDTO(
            id: "run-1",
            sessionID: "sess-1",
            userMessageID: "msg-1",
            status: "completed",
            startedAt: "2026-07-12T10:00:00Z",
            updatedAt: "2026-07-12T10:00:02Z",
            completedAt: "2026-07-12T10:00:02Z",
            steps: [
                ChatRunStepDTO(
                    sequence: 1,
                    stage: "tool",
                    status: "completed",
                    summary: "运行测试",
                    eventID: "evt-1"
                )
            ],
            artifacts: [
                ChatArtifactDTO(
                    id: "artifact-1",
                    kind: "validation",
                    title: "pytest",
                    summary: ["result": .string("12 passed")],
                    status: "success"
                )
            ]
        )

        let execution = ChatExecutionPresentation.restoring(run)

        #expect(execution.id == "run-1")
        #expect(execution.serverRunID == "run-1")
        #expect(execution.stage == .completed)
        #expect(execution.steps.first?.title == "运行测试")
        #expect(execution.artifacts.first?.kind == .validation)
    }
    @Test func permissionUpdatesReplaceTheToolStepWithTheSameCallID() {
        let initial = ChatExecutionPresentation(id: "run-1")
        let started = initial.applying(
            ChatStreamEvent(
                id: "event-start",
                type: .toolCallStart,
                data: ["name": .string("bash_run"), "call_id": .string("call-1")]
            )
        )
        let waiting = started.applying(
            ChatStreamEvent(
                id: "event-permission",
                type: .permissionRequest,
                data: [
                    "call_id": .string("call-1"),
                    "tool_name": .string("bash_run"),
                    "status": .string("needs_confirmation"),
                ]
            )
        )

        #expect(waiting.steps.filter { $0.id == "call-1" }.count == 1)
        #expect(waiting.steps.first?.status == .awaitingApproval)
    }

    @Test func completedRunUsesCompactElapsedSummary() {
        let summary = ChatRunSummary(stage: .completed, seconds: 727)

        #expect(summary.isCollapsedByDefault)
        #expect(summary.seconds == 727)
    }

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
