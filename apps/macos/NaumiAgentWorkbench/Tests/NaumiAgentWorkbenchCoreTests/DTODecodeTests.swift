import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct DTODecodeTests {

    @Test func decodeZHSnapshot() throws {
        let data = try loadFixture(named: "workbench_snapshot_zh")
        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)

        #expect(snapshot.sessionID == "sess-zh-001")
        #expect(snapshot.missions.count == 1)
        #expect(snapshot.tasks.count == 2)
        #expect(snapshot.issues.count == 1)
        #expect(snapshot.failures.count == 1)
        #expect(snapshot.events.count == 2)

        let mission = try #require(snapshot.missions.first)
        #expect(mission.title == "实现 SwiftUI 工作台骨架")
        #expect(mission.status == "planning")

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
        #expect(snapshot.tasks.count == 2)
        #expect(snapshot.issues.count == 1)
        #expect(snapshot.failures.count == 1)
        #expect(snapshot.events.count == 2)

        let mission = try #require(snapshot.missions.first)
        #expect(mission.title == "Build SwiftUI Workbench Shell")
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
