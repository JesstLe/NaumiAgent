import Testing
@testable import NaumiAgentWorkbenchCore

struct WorktreesDashboardPresentationTests {

    @Test func summarizesHealthBucketsAndAgents() {
        let presentation = WorktreesDashboardPresentation(snapshots: [
            makeSnapshot(id: "1", agentID: "agent-a", health: "good"),
            makeSnapshot(id: "2", agentID: "agent-b", health: "stale"),
            makeSnapshot(id: "3", agentID: "agent-b", health: "conflicted"),
        ])

        #expect(presentation.totalCount == 3)
        #expect(presentation.goodCount == 1)
        #expect(presentation.attentionCount == 2)
        #expect(presentation.activeAgentCount == 2)
        #expect(presentation.selectedSnapshot?.id == "3")
    }

    @Test func emptySnapshotsUseNilSelection() {
        let presentation = WorktreesDashboardPresentation(snapshots: [])

        #expect(presentation.totalCount == 0)
        #expect(presentation.selectedSnapshot == nil)
    }

    private func makeSnapshot(
        id: String,
        agentID: String,
        health: String
    ) -> ContextSnapshotDTO {
        ContextSnapshotDTO(
            id: id,
            sessionID: "sess-1",
            agentID: agentID,
            taskID: "task-\(id)",
            health: health,
            reasons: ["reason-\(id)"],
            createdAt: "2026-06-27T09:0\(id):00"
        )
    }
}
