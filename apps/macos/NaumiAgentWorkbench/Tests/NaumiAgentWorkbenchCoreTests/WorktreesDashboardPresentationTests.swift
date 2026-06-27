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

    @Test func summarizesApiWorktreesForTheManagementTable() {
        let presentation = WorktreesDashboardPresentation(
            snapshots: [],
            worktrees: [
                makeWorktree(
                    name: "wt-clean",
                    taskID: "task-1",
                    status: "clean",
                    dirtyFiles: 0,
                    commitsAhead: 0,
                    keptReason: "",
                    metadata: ["agent_id": "Backend-Agent"],
                    removable: true
                ),
                makeWorktree(
                    name: "wt-dirty",
                    taskID: "task-2",
                    status: "dirty",
                    dirtyFiles: 3,
                    commitsAhead: 2,
                    keptReason: "",
                    metadata: ["owner": "Reviewer-Agent"],
                    removable: false
                ),
                makeWorktree(
                    name: "wt-kept",
                    taskID: "task-3",
                    status: "kept",
                    dirtyFiles: 0,
                    commitsAhead: 1,
                    keptReason: "等待人工审查",
                    metadata: [:],
                    removable: false
                ),
            ]
        )

        #expect(presentation.worktreeCount == 3)
        #expect(presentation.cleanWorktreeCount == 1)
        #expect(presentation.dirtyWorktreeCount == 1)
        #expect(presentation.keptWorktreeCount == 1)
        #expect(presentation.removableWorktreeCount == 1)
        #expect(presentation.worktreeRows.map(\.name) == ["wt-clean", "wt-dirty", "wt-kept"])
        #expect(presentation.worktreeRows.map(\.agentID) == ["Backend-Agent", "Reviewer-Agent", "-"])
        #expect(presentation.worktreeRows[1].statusTone == .warning)
        #expect(presentation.worktreeRows[2].statusTone == .kept)
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

    private func makeWorktree(
        name: String,
        taskID: String,
        status: String,
        dirtyFiles: Int,
        commitsAhead: Int,
        keptReason: String,
        metadata: [String: String],
        removable: Bool
    ) -> WorktreeDTO {
        WorktreeDTO(
            name: name,
            path: "/repo/.naumi/worktrees/\(name)",
            branch: "naumi/worktree-\(name)",
            baseRef: "main",
            status: status,
            taskID: taskID,
            dirtyFiles: dirtyFiles,
            commitsAhead: commitsAhead,
            createdAt: "2026-06-27T09:00:00",
            updatedAt: "2026-06-27T09:05:00",
            keptReason: keptReason,
            metadata: metadata,
            removable: removable
        )
    }
}
