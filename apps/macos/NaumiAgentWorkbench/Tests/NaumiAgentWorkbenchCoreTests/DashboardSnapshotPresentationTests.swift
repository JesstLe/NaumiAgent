import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardSnapshotPresentationTests {

    @Test func currentMissionFromZHSnapshot() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        let mission = try #require(presentation.currentMission)
        #expect(mission.id == "mzh-001")
        #expect(mission.title == "实现 SwiftUI 工作台骨架")
        #expect(mission.status == "planning")
    }

    @Test func taskRowsBindIssueRiskAndParallelMode() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.taskRows.count == 2)

        let taskWithIssue = try #require(presentation.taskRows.first { $0.id == "2" })
        #expect(taskWithIssue.subject == "实现 API Client")
        #expect(taskWithIssue.status == "in_progress")
        #expect(taskWithIssue.owner == "agent-a")
        #expect(taskWithIssue.activeForm == "实现 WorkbenchAPIClient")
        #expect(taskWithIssue.riskLevel == "medium")
        #expect(taskWithIssue.parallelMode == "exclusive")
        #expect(taskWithIssue.acceptanceCriteriaCount == 2)

        let taskWithoutIssue = try #require(presentation.taskRows.first { $0.id == "1" })
        #expect(taskWithoutIssue.riskLevel == nil)
        #expect(taskWithoutIssue.parallelMode == nil)
        #expect(taskWithoutIssue.acceptanceCriteriaCount == nil)
    }

    @Test func agentRowsMapProfiles() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.agentRows.count == 2)

        let agent = try #require(presentation.agentRows.first { $0.id == "agent-a" })
        #expect(agent.name == "后端智能体")
        #expect(agent.role == "coder")
        #expect(agent.status == "busy")
        #expect(agent.capabilityCount == 2)
        #expect(agent.maxParallelTasks == 2)
    }

    @Test func issueRowsListAllIssues() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.issueRows.count == 1)

        let issue = try #require(presentation.issueRows.first)
        #expect(issue.taskID == "2")
        #expect(issue.missionID == "mzh-001")
        #expect(issue.riskLevel == "medium")
        #expect(issue.parallelMode == "exclusive")
        #expect(issue.requiresHumanApproval == true)
    }

    @Test func failureRowsMapFields() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.failureRows.count == 1)

        let failure = try #require(presentation.failureRows.first)
        #expect(failure.id == "fzh-001")
        #expect(failure.title == "DTO 解码测试失败")
        #expect(failure.kind == "test_failed")
        #expect(failure.status == "open")
        #expect(failure.taskID == "2")
    }

    @Test func recentEventRowsPreserveTrailingOrder() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.recentEventRows.count == 2)

        let first = presentation.recentEventRows[0]
        #expect(first.type == "mission.created")
        #expect(first.actor == "Human")
        #expect(first.subjectID == "mzh-001")
        #expect(first.timestamp == "2026-06-27T05:00:00")

        let last = presentation.recentEventRows[1]
        #expect(last.type == "task.updated")
        #expect(last.actor == "Agent")
        #expect(last.subjectID == "2")
        #expect(last.timestamp == "2026-06-27T05:40:00")
    }

    @Test func workbenchPresentationKeepsSharedCanvasLandmarks() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.workbench.leftMissionTitle == "实现 SwiftUI 工作台骨架")
        #expect(presentation.workbench.leftIssueCount == 1)
        #expect(presentation.workbench.leftTaskCount == 2)
        #expect(presentation.workbench.leftFailureCount == 1)

        #expect(presentation.workbench.canvasNodes.map(\.kind) == [
            .mission,
            .issue,
            .agents,
            .worktrees,
            .validation,
            .failure,
            .approval
        ])

        let inspector = try #require(presentation.workbench.inspector)
        #expect(inspector.title == "实现 API Client")
        #expect(inspector.status == "in_progress")
        #expect(inspector.riskLevel == "medium")
        #expect(inspector.parallelMode == "exclusive")
        #expect(inspector.owner == "agent-a")
        #expect(inspector.requiresHumanApproval == true)
        #expect(inspector.acceptanceCriteriaCount == 2)

        #expect(presentation.workbench.auditRows.map(\.type) == [
            "mission.created",
            "task.updated"
        ])
    }

    @Test func recentEventRowsLimitToFive() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-limit",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: (1...7).map { index in
                EventDTO(
                    id: "evt-\(index)",
                    sessionID: "sess-limit",
                    type: "event.\(index)",
                    actor: "Actor",
                    subjectID: "subject-\(index)",
                    payload: [:],
                    timestamp: "2026-06-27T0\(index):00:00"
                )
            }
        )

        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.recentEventRows.count == 5)
        #expect(presentation.recentEventRows.map(\.id) == [
            "evt-3", "evt-4", "evt-5", "evt-6", "evt-7"
        ])
    }

    @Test func taskRowsLimitToFive() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-limit",
            missions: [],
            tasks: (1...8).map { index in
                TaskDTO(
                    id: "\(index)",
                    sessionID: "sess-limit",
                    subject: "Task \(index)",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                )
            },
            issues: [],
            failures: [],
            events: []
        )

        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.taskRows.count == 5)
        #expect(presentation.taskRows.map(\.id) == ["1", "2", "3", "4", "5"])
    }

    @Test func agentRowsLimitToFive() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-limit",
            missions: [],
            agentProfiles: (1...8).map { index in
                AgentProfileDTO(
                    id: "agent-\(index)",
                    sessionID: "sess-limit",
                    name: "Agent \(index)",
                    role: "coder",
                    capabilities: ["code"],
                    permissions: ["read"],
                    maxParallelTasks: 1,
                    status: "idle",
                    createdAt: "",
                    updatedAt: ""
                )
            },
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.agentRows.count == 5)
        #expect(presentation.agentRows.map(\.id) == ["agent-1", "agent-2", "agent-3", "agent-4", "agent-5"])
    }

    // MARK: - Helpers

    private func loadZHSnapshot() throws -> WorkbenchSnapshotDTO {
        let data = try loadFixture(named: "workbench_snapshot_zh")
        return try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
    }

    private func loadFixture(named: String) throws -> Data {
        let fixturesURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/\(named).json")
        return try Data(contentsOf: fixturesURL)
    }
}
