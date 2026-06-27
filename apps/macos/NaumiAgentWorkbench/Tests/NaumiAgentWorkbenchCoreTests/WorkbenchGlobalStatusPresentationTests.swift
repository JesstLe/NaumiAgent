import Testing
@testable import NaumiAgentWorkbenchCore

struct WorkbenchGlobalStatusPresentationTests {

    @Test func buildsStatusItemsFromSnapshotAndAuxiliaryCollections() {
        let presentation = WorkbenchGlobalStatusPresentation(
            snapshot: snapshot,
            approvals: [
                approval(id: "approval-1", state: "waiting"),
                approval(id: "approval-2", state: "approved"),
            ],
            validationRuns: [
                validationRun(id: "run-1", status: "failed"),
                validationRun(id: "run-2", status: "passed"),
            ],
            failures: [
                failure(id: "failure-1", status: "open"),
            ],
            locale: .zhCN
        )

        #expect(presentation.missionTitle == "实现 SwiftUI 工作台骨架")
        #expect(presentation.items.map(\.label) == ["Mission", "智能体", "开放问题", "阻塞", "待审批", "验证失败"])
        #expect(presentation.items.map(\.value) == ["实现 SwiftUI 工作台骨架", "2", "2", "1", "1", "2"])
    }

    @Test func fallsBackToEmptyMissionWhenSnapshotIsMissing() {
        let presentation = WorkbenchGlobalStatusPresentation(
            snapshot: nil,
            approvals: [],
            validationRuns: [],
            failures: [],
            locale: .enUS
        )

        #expect(presentation.missionTitle == "No Mission")
        #expect(presentation.items.first?.value == "No Mission")
        #expect(presentation.items[1].value == "0")
    }

    private var snapshot: WorkbenchSnapshotDTO {
        WorkbenchSnapshotDTO(
            sessionID: "session-1",
            missions: [
                MissionDTO(
                    id: "mission-1",
                    sessionID: "session-1",
                    title: "实现 SwiftUI 工作台骨架",
                    goal: "补齐导航页面",
                    status: "active",
                    createdAt: "2026-06-27T09:00:00",
                    updatedAt: "2026-06-27T09:10:00"
                )
            ],
            agentProfiles: [
                agentProfile(id: "agent-a"),
                agentProfile(id: "agent-b"),
            ],
            tasks: [
                task(id: "task-1", status: "in_progress"),
                task(id: "task-2", status: "blocked"),
                task(id: "task-3", status: "completed"),
            ],
            issues: [
                issue(taskID: "task-1"),
                issue(taskID: "task-2"),
                issue(taskID: "task-3"),
            ],
            leases: [],
            failures: [],
            events: []
        )
    }

    private func agentProfile(id: String) -> AgentProfileDTO {
        AgentProfileDTO(
            id: id,
            sessionID: "session-1",
            name: id,
            role: "worker",
            capabilities: ["code"],
            permissions: ["read"],
            maxParallelTasks: 1,
            status: "active",
            createdAt: "2026-06-27T09:00:00",
            updatedAt: "2026-06-27T09:00:00"
        )
    }

    private func task(id: String, status: String) -> TaskDTO {
        TaskDTO(
            id: id,
            sessionID: "session-1",
            subject: id,
            description: id,
            status: status,
            activeForm: "plan",
            owner: nil,
            blocks: [],
            blockedBy: [],
            createdAt: "2026-06-27T09:00:00",
            updatedAt: "2026-06-27T09:00:00"
        )
    }

    private func issue(taskID: String) -> IssueDTO {
        IssueDTO(
            sessionID: "session-1",
            taskID: taskID,
            missionID: "mission-1",
            parallelMode: "exclusive",
            riskLevel: "medium",
            requiresHumanApproval: true,
            acceptanceCriteria: ["done"],
            expectedArtifacts: [],
            relatedBranch: "main",
            relatedWorktree: "",
            relatedPR: "",
            createdAt: "2026-06-27T09:00:00",
            updatedAt: "2026-06-27T09:00:00"
        )
    }

    private func approval(id: String, state: String) -> ApprovalDTO {
        ApprovalDTO(
            id: id,
            sessionID: "session-1",
            missionID: "mission-1",
            taskID: "task-1",
            state: state,
            title: id,
            detail: id,
            requester: "agent-a",
            reviewer: "",
            decisionNote: "",
            createdAt: "2026-06-27T09:00:00",
            updatedAt: "2026-06-27T09:00:00"
        )
    }

    private func validationRun(id: String, status: String) -> ValidationRunDTO {
        ValidationRunDTO(
            id: id,
            sessionID: "session-1",
            taskID: "task-1",
            actor: "agent-a",
            command: ["pytest"],
            cwd: "/tmp",
            status: status,
            exitCode: status == "failed" ? 1 : 0,
            output: status,
            startedAt: "2026-06-27T09:00:00",
            completedAt: "2026-06-27T09:01:00"
        )
    }

    private func failure(id: String, status: String) -> FailureDTO {
        FailureDTO(
            id: id,
            sessionID: "session-1",
            taskID: "task-1",
            kind: "test_failed",
            title: id,
            detail: id,
            sourceID: "run-1",
            status: status,
            createdAt: "2026-06-27T09:00:00"
        )
    }
}
