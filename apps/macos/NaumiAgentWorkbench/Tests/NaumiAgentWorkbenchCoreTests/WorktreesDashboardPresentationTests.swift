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
        #expect(presentation.agentBuckets.map(\.agentID) == ["agent-b", "agent-a"])
        #expect(presentation.agentBuckets.first?.snapshotCount == 2)
        #expect(presentation.agentBuckets.first?.attentionCount == 2)
        #expect(presentation.agentBuckets.first?.worstHealth == "conflicted")
    }

    @Test func emptySnapshotsUseNilSelection() {
        let presentation = WorktreesDashboardPresentation(snapshots: [])

        #expect(presentation.totalCount == 0)
        #expect(presentation.selectedSnapshot == nil)
        #expect(presentation.agentBuckets.isEmpty)
        #expect(presentation.recommendedActions.isEmpty)
    }

    @Test func conflictedSnapshotProducesRemediationActions() {
        let presentation = WorktreesDashboardPresentation(snapshots: [
            makeSnapshot(id: "1", agentID: "agent-a", health: "conflicted"),
        ])

        #expect(presentation.recommendedActions.map(\.kind) == [
            .pauseAgent,
            .refreshContext,
            .openReview,
        ])
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
