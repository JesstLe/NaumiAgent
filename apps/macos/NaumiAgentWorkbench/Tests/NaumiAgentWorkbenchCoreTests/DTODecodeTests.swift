import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct DTODecodeTests {

    @Test func decodeZHSnapshot() throws {
        let data = try loadFixture(named: "workbench_snapshot_zh")
        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)

        #expect(snapshot.sessionID == "sess-zh-001")
        #expect(snapshot.missions.count == 1)
        #expect(snapshot.agentProfiles.count == 2)
        #expect(snapshot.tasks.count == 2)
        #expect(snapshot.issues.count == 1)
        #expect(snapshot.leases.count == 1)
        #expect(snapshot.failures.count == 1)
        #expect(snapshot.events.count == 2)

        let lease = try #require(snapshot.leases.first)
        #expect(lease.id == "lzh-001")
        #expect(lease.taskID == "2")
        #expect(lease.agentID == "agent-a")
        #expect(lease.state == "active")
        #expect(lease.worktreeName == "wt-api-client")

        let mission = try #require(snapshot.missions.first)
        #expect(mission.title == "实现 SwiftUI 工作台骨架")
        #expect(mission.status == "planning")

        let agent = try #require(snapshot.agentProfiles.first { $0.id == "agent-a" })
        #expect(agent.sessionID == "sess-zh-001")
        #expect(agent.name == "后端智能体")
        #expect(agent.role == "coder")
        #expect(agent.capabilities == ["api", "swift-client"])
        #expect(agent.permissions == ["read", "write"])
        #expect(agent.maxParallelTasks == 2)
        #expect(agent.status == "busy")

        let task = try #require(snapshot.tasks.first { $0.id == "2" })
        #expect(task.status == "in_progress")
        #expect(task.owner == "agent-a")

        let issue = try #require(snapshot.issues.first)
        #expect(issue.riskLevel == "medium")
        #expect(issue.acceptanceCriteria == ["通过 swift test", "通过 swift build"])

        let failure = try #require(snapshot.failures.first)
        #expect(failure.kind == "test_failed")

        let event = try #require(snapshot.events.first)
        #expect(event.type == "mission.created")
        #expect(event.payload["title"] == .string("实现 SwiftUI 工作台骨架"))
    }

    @Test func decodeENSnapshot() throws {
        let data = try loadFixture(named: "workbench_snapshot_en")
        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)

        #expect(snapshot.sessionID == "sess-en-001")
        #expect(snapshot.missions.count == 1)
        #expect(snapshot.agentProfiles.count == 2)
        #expect(snapshot.tasks.count == 2)
        #expect(snapshot.issues.count == 1)
        #expect(snapshot.leases.count == 1)
        #expect(snapshot.failures.count == 1)
        #expect(snapshot.events.count == 2)

        let lease = try #require(snapshot.leases.first)
        #expect(lease.id == "len-001")
        #expect(lease.state == "active")

        let mission = try #require(snapshot.missions.first)
        #expect(mission.title == "Build SwiftUI Workbench Shell")

        let agent = try #require(snapshot.agentProfiles.first { $0.id == "agent-a" })
        #expect(agent.name == "Backend Agent")
        #expect(agent.status == "busy")
    }

    @Test func decodeSnapshotSummary() throws {
        let data = Data(
            """
            {
              "session_id": "sess-summary",
              "summary": {
                "current_mission_title": "实现 SwiftUI 工作台骨架",
                "active_agents": 4,
                "open_issues": 12,
                "blocked_issues": 2,
                "pending_approvals": 3,
                "failed_validations": 1
              },
              "missions": [],
              "tasks": [],
              "issues": [],
              "failures": [],
              "events": []
            }
            """.utf8
        )

        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
        let summary = try #require(snapshot.summary)

        #expect(summary.currentMissionTitle == "实现 SwiftUI 工作台骨架")
        #expect(summary.activeAgents == 4)
        #expect(summary.openIssues == 12)
        #expect(summary.blockedIssues == 2)
        #expect(summary.pendingApprovals == 3)
        #expect(summary.failedValidations == 1)
    }

    @Test func decodeSnapshotIntentLocks() throws {
        let data = Data(
            """
            {
              "session_id": "sess-governance",
              "missions": [],
              "agent_profiles": [],
              "intent_locks": [
                {
                  "id": "lock-001",
                  "session_id": "sess-governance",
                  "mission_id": "mission-001",
                  "rule": "高风险任务需要人工审批",
                  "blocked_paths": ["src/core"],
                  "allowed_paths": ["src/core/README.md"],
                  "require_proposal_for_risk": "high",
                  "active": true,
                  "created_at": "2026-06-27T06:00:00"
                }
              ],
              "tasks": [],
              "issues": [],
              "failures": [],
              "events": []
            }
            """.utf8
        )

        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
        let lock = try #require(snapshot.intentLocks.first)

        #expect(lock.id == "lock-001")
        #expect(lock.missionID == "mission-001")
        #expect(lock.rule == "高风险任务需要人工审批")
        #expect(lock.blockedPaths == ["src/core"])
        #expect(lock.allowedPaths == ["src/core/README.md"])
        #expect(lock.requireProposalForRisk == "high")
        #expect(lock.active == true)
    }

    @Test func decodeSnapshotDecisions() throws {
        let data = Data(
            """
            {
              "session_id": "sess-governance",
              "missions": [],
              "agent_profiles": [],
              "decisions": [
                {
                  "id": "decision-001",
                  "session_id": "sess-governance",
                  "mission_id": "mission-001",
                  "kind": "architecture",
                  "title": "采用本地 FastAPI 桥接",
                  "content": "SwiftUI 只通过本地 Workbench API 访问运行时。",
                  "actor": "Planner-Agent",
                  "created_at": "2026-06-27T06:00:00"
                }
              ],
              "tasks": [],
              "issues": [],
              "failures": [],
              "events": []
            }
            """.utf8
        )

        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
        let decision = try #require(snapshot.decisions.first)

        #expect(decision.id == "decision-001")
        #expect(decision.missionID == "mission-001")
        #expect(decision.kind == "architecture")
        #expect(decision.title == "采用本地 FastAPI 桥接")
        #expect(decision.content == "SwiftUI 只通过本地 Workbench API 访问运行时。")
        #expect(decision.actor == "Planner-Agent")
    }

    @Test func decodeValidationRuns() throws {
        let data = Data(
            """
            {"validation_runs":[{"id":"run-001","session_id":"sess-001","task_id":"task-001","actor":"ValidationRunner","command":["pytest","test.py"],"cwd":"/workspace","status":"passed","exit_code":0,"output":"ok","started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:01"}],"task_id":"task-001","limit":25}
            """.utf8
        )

        let response = try JSONDecoder().decode(ValidationRunsDTO.self, from: data)

        #expect(response.taskID == "task-001")
        #expect(response.limit == 25)
        #expect(response.validationRuns.count == 1)

        let run = try #require(response.validationRuns.first)
        #expect(run.id == "run-001")
        #expect(run.sessionID == "sess-001")
        #expect(run.taskID == "task-001")
        #expect(run.actor == "ValidationRunner")
        #expect(run.command == ["pytest", "test.py"])
        #expect(run.cwd == "/workspace")
        #expect(run.status == "passed")
        #expect(run.exitCode == 0)
        #expect(run.output == "ok")
        #expect(run.startedAt == "2026-06-27T06:00:00")
        #expect(run.completedAt == "2026-06-27T06:00:01")
    }

    @Test func decodeValidationRunsWithoutTaskID() throws {
        let data = Data(
            """
            {"validation_runs":[],"task_id":null,"limit":50}
            """.utf8
        )

        let response = try JSONDecoder().decode(ValidationRunsDTO.self, from: data)

        #expect(response.taskID == nil)
        #expect(response.limit == 50)
        #expect(response.validationRuns.isEmpty)
    }

    @Test func decodeContextSnapshots() throws {
        let data = Data(
            """
            {"context_snapshots":[{"id":"snap-001","session_id":"sess-001","agent_id":"agent-001","task_id":"task 001/审查","health":"good","reasons":["上下文健康"],"created_at":"2026-06-27T06:00:00"}],"task_id":"task 001/审查","agent_id":"agent-001","limit":25}
            """.utf8
        )

        let response = try JSONDecoder().decode(ContextSnapshotsDTO.self, from: data)

        #expect(response.taskID == "task 001/审查")
        #expect(response.agentID == "agent-001")
        #expect(response.limit == 25)
        #expect(response.contextSnapshots.count == 1)

        let snapshot = try #require(response.contextSnapshots.first)
        #expect(snapshot.id == "snap-001")
        #expect(snapshot.sessionID == "sess-001")
        #expect(snapshot.agentID == "agent-001")
        #expect(snapshot.taskID == "task 001/审查")
        #expect(snapshot.health == "good")
        #expect(snapshot.reasons == ["上下文健康"])
        #expect(snapshot.createdAt == "2026-06-27T06:00:00")
    }

    @Test func decodeContextSnapshotsWithoutOptionalFilters() throws {
        let data = Data(
            """
            {"context_snapshots":[],"task_id":null,"agent_id":null,"limit":50}
            """.utf8
        )

        let response = try JSONDecoder().decode(ContextSnapshotsDTO.self, from: data)

        #expect(response.taskID == nil)
        #expect(response.agentID == nil)
        #expect(response.limit == 50)
        #expect(response.contextSnapshots.isEmpty)
    }

    @Test func decodeEventPayloadWithNonStringValues() throws {
        let data = Data(
            """
            {
              "id": "evt-json",
              "session_id": "sess-1",
              "type": "validation.finished",
              "actor": "ValidationRunner",
              "subject_id": "task-1",
              "payload": {
                "exit_code": 1,
                "retryable": false,
                "labels": ["validation", "failure"],
                "meta": {"source": "pytest"}
              },
              "timestamp": "2026-06-27T06:00:00"
            }
            """.utf8
        )

        let event = try JSONDecoder().decode(EventDTO.self, from: data)

        #expect(event.payload["exit_code"] == .number(1))
        #expect(event.payload["retryable"] == .bool(false))
        #expect(
            event.payload["labels"]
                == .array([.string("validation"), .string("failure")])
        )
        #expect(event.payload["meta"] == .object(["source": .string("pytest")]))
    }

    @Test func decodeEventSeverityAndCorrelationFields() throws {
        let data = Data(
            """
            {
              "id": "evt-sev",
              "session_id": "sess-1",
              "type": "proposal.created",
              "actor": "Builder-Agent",
              "subject_id": "prop-1",
              "payload": {"title": "高风险改动"},
              "timestamp": "2026-07-11T00:00:00",
              "severity": "warning",
              "correlation_id": "corr-1",
              "parent_event_id": "evt-parent"
            }
            """.utf8
        )

        let event = try JSONDecoder().decode(EventDTO.self, from: data)

        #expect(event.severity == "warning")
        #expect(event.correlationID == "corr-1")
        #expect(event.parentEventID == "evt-parent")
    }

    @Test func decodeEventFallsBackToDefaultSeverityWhenAbsent() throws {
        let data = Data(
            """
            {
              "id": "evt-old",
              "session_id": "sess-1",
              "type": "mission.created",
              "actor": "Human",
              "subject_id": "mission-1",
              "payload": {},
              "timestamp": "2026-06-27T06:00:00"
            }
            """.utf8
        )

        let event = try JSONDecoder().decode(EventDTO.self, from: data)

        #expect(event.severity == "info")
        #expect(event.correlationID == nil)
        #expect(event.parentEventID == nil)
    }

    @Test func decodeProposal() throws {
        let data = Data(
            """
            {
              "id": "prop-1",
              "session_id": "sess-1",
              "mission_id": "m1",
              "task_id": "t1",
              "agent_id": "agent-a",
              "title": "重构认证模块",
              "impact_scope": "影响登录与令牌签发",
              "intended_files": ["src/auth.py", "src/token.py"],
              "validation_plan": ["pytest tests/auth"],
              "risk_level": "high",
              "questions": ["是否兼容旧令牌?"],
              "state": "open",
              "decision_note": "",
              "converted_issue_id": "",
              "created_at": "2026-07-11T00:00:00",
              "updated_at": "2026-07-11T00:00:00"
            }
            """.utf8
        )

        let proposal = try JSONDecoder().decode(ProposalDTO.self, from: data)

        #expect(proposal.id == "prop-1")
        #expect(proposal.title == "重构认证模块")
        #expect(proposal.impactScope == "影响登录与令牌签发")
        #expect(proposal.intendedFiles == ["src/auth.py", "src/token.py"])
        #expect(proposal.validationPlan == ["pytest tests/auth"])
        #expect(proposal.riskLevel == "high")
        #expect(proposal.questions == ["是否兼容旧令牌?"])
        #expect(proposal.state == "open")
    }

    @Test func decodeProposalsEnvelope() throws {
        let data = Data(
            """
            {
              "proposals": [
                {
                  "id": "prop-1",
                  "session_id": "sess-1",
                  "mission_id": "m1",
                  "task_id": "t1",
                  "agent_id": "agent-a",
                  "title": "高风险改动",
                  "impact_scope": "核心调度",
                  "state": "open",
                  "risk_level": "critical",
                  "created_at": "2026-07-11T00:00:00",
                  "updated_at": "2026-07-11T00:00:00"
                }
              ],
              "mission_id": "m1",
              "task_id": null,
              "state": "open",
              "limit": 50
            }
            """.utf8
        )

        let envelope = try JSONDecoder().decode(ProposalsDTO.self, from: data)

        #expect(envelope.proposals.count == 1)
        #expect(envelope.proposals.first?.riskLevel == "critical")
        #expect(envelope.missionID == "m1")
        #expect(envelope.state == "open")
        #expect(envelope.limit == 50)
    }

    @Test func decodeSnapshotIncludesProposals() throws {
        let data = Data(
            """
            {
              "session_id": "sess-1",
              "missions": [],
              "tasks": [],
              "issues": [],
              "failures": [],
              "events": [],
              "proposals": [
                {
                  "id": "prop-1",
                  "session_id": "sess-1",
                  "mission_id": "m1",
                  "task_id": "t1",
                  "agent_id": "agent-a",
                  "title": "高风险改动",
                  "impact_scope": "核心调度",
                  "state": "open",
                  "created_at": "2026-07-11T00:00:00",
                  "updated_at": "2026-07-11T00:00:00"
                }
              ]
            }
            """.utf8
        )

        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)

        #expect(snapshot.proposals.count == 1)
        #expect(snapshot.proposals.first?.title == "高风险改动")
    }

    @Test func decodeChatRunsIncludesOrderedStepsAndArtifacts() throws {
        let data = Data(
            """
            {
              "runs": [{
                "id": "run-1",
                "session_id": "sess-1",
                "user_message_id": "msg-1",
                "assistant_message_id": "msg-2",
                "status": "completed",
                "started_at": "2026-07-12T08:00:00Z",
                "updated_at": "2026-07-12T08:00:02Z",
                "completed_at": "2026-07-12T08:00:02Z",
                "steps": [{
                  "sequence": 1,
                  "stage": "tool",
                  "status": "completed",
                  "summary": "delegate_task",
                  "detail": "UI-PING",
                  "event_id": "event-1",
                  "started_at": "2026-07-12T08:00:00Z",
                  "completed_at": "2026-07-12T08:00:01Z",
                  "metadata": {}
                }],
                "artifacts": [{
                  "id": "artifact-1",
                  "kind": "subagent",
                  "title": "delegate_task",
                  "summary": {"text": "UI-PING"},
                  "status": "success",
                  "created_at": "2026-07-12T08:00:01Z",
                  "metadata": {}
                }]
              }],
              "total": 1
            }
            """.utf8
        )

        let response = try JSONDecoder().decode(ChatRunsDTO.self, from: data)

        #expect(response.total == 1)
        #expect(response.runs[0].steps[0].sequence == 1)
        #expect(response.runs[0].artifacts[0].summary["text"] == .string("UI-PING"))
    }

    // MARK: - Helpers

    private func loadFixture(named: String) throws -> Data {
        let fixturesURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/\(named).json")
        return try Data(contentsOf: fixturesURL)
    }
}
