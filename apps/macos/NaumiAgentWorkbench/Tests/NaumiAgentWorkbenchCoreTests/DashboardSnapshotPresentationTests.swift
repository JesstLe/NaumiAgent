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

    @Test func validationRerunCommandReusesLatestMatchingValidationRun() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)
        let runs = [
            validationRun(
                id: "run-old",
                taskID: "1",
                actor: "Lint-Agent",
                command: ["ruff", "check", "src/"],
                cwd: "/repo",
                status: "passed"
            ),
            validationRun(
                id: "run-failed",
                taskID: "2",
                actor: "Test-Agent",
                command: ["pytest", "tests/unit/test_api_workbench.py", "-q"],
                cwd: "/repo",
                status: "failed"
            )
        ]

        let command = try #require(presentation.validationRerunCommand(validationRuns: runs))

        #expect(command.taskID == "2")
        #expect(command.actor == "Test-Agent")
        #expect(command.command == ["pytest", "tests/unit/test_api_workbench.py", "-q"])
        #expect(command.cwd == "/repo")
        #expect(command.canSubmit)
    }

    @Test func validationRerunCommandUsesFailureTaskAndTargetedFallbackWithoutHistory() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        let command = try #require(presentation.validationRerunCommand(validationRuns: []))

        #expect(command.taskID == "2")
        #expect(command.actor == "Dashboard")
        #expect(command.command == ["pytest", "tests/unit", "-q"])
        #expect(command.cwd == nil)
        #expect(command.canSubmit)
    }

    @Test func contextRefreshCommandTargetsCurrentFailureTaskAndOwner() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        let command = try #require(presentation.contextRefreshCommand())

        #expect(command.taskID == "2")
        #expect(command.agentID == "agent-a")
        #expect(command.limit == 50)
        #expect(command.canSubmit)
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

    @Test func agentStatusBusyWhenHeartbeatFreshAndLeasePresent() throws {
        let formatter = ISO8601DateFormatter()
        let heartbeat = formatter.string(from: Date())
        let lease = LeaseDTO(
            id: "lease-1",
            sessionID: "s",
            taskID: "task-1",
            agentID: "agent-a",
            state: "active",
            expiresAt: "2026-12-31T23:59:59",
            worktreeName: "wt1",
            createdAt: "",
            updatedAt: ""
        )
        let profile = AgentProfileDTO(
            id: "agent-a",
            sessionID: "s",
            name: "Agent A",
            role: "coder",
            capabilities: ["code"],
            permissions: ["write"],
            maxParallelTasks: 1,
            status: "idle",
            lastHeartbeatAt: heartbeat,
            currentLease: lease,
            createdAt: "",
            updatedAt: ""
        )
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "s",
            missions: [],
            agentProfiles: [profile],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)
        let row = try #require(presentation.agentRows.first)
        #expect(row.status == "busy")
        #expect(row.permissions == ["write"])
        #expect(row.currentLease?.leaseID == "lease-1")
    }

    @Test func agentStatusIdleWhenHeartbeatFreshAndNoLease() throws {
        let formatter = ISO8601DateFormatter()
        let heartbeat = formatter.string(from: Date())
        let profile = AgentProfileDTO(
            id: "agent-a",
            sessionID: "s",
            name: "Agent A",
            role: "coder",
            capabilities: ["code"],
            permissions: [],
            maxParallelTasks: 1,
            status: "busy",
            lastHeartbeatAt: heartbeat,
            createdAt: "",
            updatedAt: ""
        )
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "s",
            missions: [],
            agentProfiles: [profile],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)
        let row = try #require(presentation.agentRows.first)
        #expect(row.status == "idle")
        #expect(row.currentLease == nil)
    }

    @Test func agentStatusStaleWhenHeartbeatOld() throws {
        let formatter = ISO8601DateFormatter()
        let staleHeartbeat = formatter.string(from: Date().addingTimeInterval(-400))
        let profile = AgentProfileDTO(
            id: "agent-a",
            sessionID: "s",
            name: "Agent A",
            role: "coder",
            capabilities: ["code"],
            permissions: [],
            maxParallelTasks: 1,
            status: "busy",
            lastHeartbeatAt: staleHeartbeat,
            createdAt: "",
            updatedAt: ""
        )
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "s",
            missions: [],
            agentProfiles: [profile],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        let presentation = DashboardSnapshotPresentation(
            snapshot: snapshot,
            thresholds: AgentHeartbeatThresholds(staleSeconds: 300, offlineSeconds: 900)
        )
        let row = try #require(presentation.agentRows.first)
        #expect(row.status == "stale")
    }

    @Test func agentStatusOfflineWhenHeartbeatVeryOld() throws {
        let formatter = ISO8601DateFormatter()
        let offlineHeartbeat = formatter.string(from: Date().addingTimeInterval(-1000))
        let profile = AgentProfileDTO(
            id: "agent-a",
            sessionID: "s",
            name: "Agent A",
            role: "coder",
            capabilities: ["code"],
            permissions: [],
            maxParallelTasks: 1,
            status: "busy",
            lastHeartbeatAt: offlineHeartbeat,
            createdAt: "",
            updatedAt: ""
        )
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "s",
            missions: [],
            agentProfiles: [profile],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        let presentation = DashboardSnapshotPresentation(
            snapshot: snapshot,
            thresholds: AgentHeartbeatThresholds(staleSeconds: 300, offlineSeconds: 900)
        )
        let row = try #require(presentation.agentRows.first)
        #expect(row.status == "offline")
    }

    // MARK: - Minimal real-mode fixture (no fake fillers)

    @Test func minimalSnapshotDecodesAndSurfacesOnlyTheMission() throws {
        // A freshly created real session carries one mission and nothing else.
        // The presentation must surface exactly that — no fabricated tasks,
        // issues, agents, failures, or events.
        for token in ["workbench_snapshot_minimal_zh", "workbench_snapshot_minimal_en"] {
            let data = try loadFixture(named: token)
            let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
            let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

            let mission = try #require(presentation.currentMission)
            #expect(mission.status == "planning")

            #expect(presentation.taskRows == [])
            #expect(presentation.issueRows == [])
            #expect(presentation.agentRows == [])
            #expect(presentation.failureRows == [])
            #expect(presentation.recentEventRows == [])
            // Only the mission landmark appears; no fabricated nodes.
            #expect(presentation.workbench.canvasNodes.map(\.kind) == [.mission])
            #expect(presentation.workbench.inspector == nil)
            #expect(presentation.workbench.auditRows == [])
            #expect(presentation.validationRerunCommand(validationRuns: []) == nil)
            #expect(presentation.contextRefreshCommand() == nil)
        }
    }

    @Test func minimalSnapshotTaskMarketDesignShowsNoFillerRows() throws {
        // Real mode must not pad the sparse minimal session with fixture rows.
        let data = try loadFixture(named: "workbench_snapshot_minimal_en")
        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
        let market = TaskMarketDesignPresentation(snapshot: snapshot, policy: .real)

        #expect(market.rows == [])
        #expect(market.bids == [])
        #expect(market.activeLeases == [])
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

    private func validationRun(
        id: String,
        taskID: String,
        actor: String,
        command: [String],
        cwd: String,
        status: String
    ) -> ValidationRunDTO {
        ValidationRunDTO(
            id: id,
            sessionID: "sess-zh-001",
            taskID: taskID,
            actor: actor,
            command: command,
            cwd: cwd,
            status: status,
            exitCode: status == "passed" ? 0 : 1,
            output: "",
            startedAt: "2026-06-27T09:00:00",
            completedAt: "2026-06-27T09:01:00"
        )
    }
}
