import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardRuntimeEvidencePresentationTests {

    @Test func emptyEvidenceDoesNotInventValidationOrContextStatus() {
        let evidence = DashboardRuntimeEvidencePresentation(
            validationRuns: [],
            contextSnapshots: []
        )

        #expect(evidence.validationLines(locale: .zhCN) == ["暂无验证记录"])
        #expect(evidence.contextLines(locale: .zhCN) == ["暂无上下文健康记录"])
    }

    @Test func evidenceUsesLatestDaemonRecords() {
        let run = ValidationRunDTO(
            id: "run-42",
            sessionID: "sess",
            taskID: "task-1",
            actor: "Test-Agent",
            command: ["pytest", "tests/unit", "-q"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "12 passed",
            startedAt: "2026-07-12T04:20:00",
            completedAt: "2026-07-12T04:21:00"
        )
        let context = ContextSnapshotDTO(
            id: "context-5",
            sessionID: "sess",
            agentID: "Test-Agent",
            taskID: "task-1",
            health: "good",
            reasons: ["引用已同步"],
            createdAt: "2026-07-12T04:22:00"
        )
        let evidence = DashboardRuntimeEvidencePresentation(
            validationRuns: [run],
            contextSnapshots: [context]
        )

        #expect(evidence.validationLines(locale: .zhCN) == [
            "最近运行：run-42",
            "结果：passed",
            "命令：pytest tests/unit -q",
        ])
        #expect(evidence.contextLines(locale: .zhCN) == [
            "整体：good",
            "更新：2026-07-12 04:22:00",
            "引用已同步",
        ])
    }
}
