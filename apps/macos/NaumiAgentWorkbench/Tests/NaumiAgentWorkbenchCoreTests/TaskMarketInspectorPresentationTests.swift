import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketInspectorPresentationTests {

    @Test func realIssueUsesSnapshotDescriptionAndInspectorEvidence() throws {
        let task = TaskDTO(
            id: "task-real",
            sessionID: "sess-real",
            subject: "验证聊天创建任务",
            description: "从对话页创建任务并在任务市场中显示。",
            status: "pending",
            activeForm: nil,
            owner: nil,
            blocks: [],
            blockedBy: [],
            createdAt: "2026-07-12T04:21:56.596763",
            updatedAt: "2026-07-12T04:21:56.596763"
        )
        let issue = IssueDTO(
            sessionID: "sess-real",
            taskID: task.id,
            missionID: "mission-real",
            parallelMode: "exclusive",
            riskLevel: "medium",
            requiresHumanApproval: true,
            acceptanceCriteria: ["任务市场可见", "聊天消息显示关联状态"],
            expectedArtifacts: [],
            relatedBranch: "",
            relatedWorktree: "",
            relatedPR: "",
            createdAt: "2026-07-12T04:21:56",
            updatedAt: "2026-07-12T04:21:56"
        )
        let context = ContextSnapshotDTO(
            id: "context-real",
            sessionID: "sess-real",
            agentID: "agent-a",
            taskID: task.id,
            health: "good",
            reasons: [],
            createdAt: "2026-07-12T04:25:00"
        )
        let validation = ValidationRunDTO(
            id: "run-real",
            sessionID: "sess-real",
            taskID: task.id,
            actor: "Test-Agent",
            command: ["pytest", "-q"],
            cwd: "/tmp",
            status: "passed",
            exitCode: 0,
            output: "1 passed",
            startedAt: "2026-07-12T04:23:00",
            completedAt: "2026-07-12T04:24:00"
        )
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-real",
            missions: [],
            tasks: [task],
            issues: [issue],
            failures: [],
            events: [],
            validationRuns: [validation],
            contextSnapshots: [context]
        )

        let market = TaskMarketDesignPresentation(snapshot: snapshot, policy: .real)
        let row = try #require(market.rows.first)
        let inspector = TaskMarketInspectorPresentation(issue: row, snapshot: snapshot)

        #expect(row.detail == "从对话页创建任务并在任务市场中显示。")
        #expect(row.status == "pending")
        #expect(row.tag == "pending")
        #expect(inspector.createdAt == task.createdAt)
        #expect(inspector.contextHealth == "good")
        #expect(inspector.validationCount == 1)
        #expect(inspector.acceptanceCriteria == issue.acceptanceCriteria)
    }
}
