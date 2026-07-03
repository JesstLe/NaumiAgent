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

    @Test func selectsWorktreeAndExposesKeepActionState() throws {
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
                    name: "wt-kept",
                    taskID: "task-2",
                    status: "kept",
                    dirtyFiles: 0,
                    commitsAhead: 1,
                    keptReason: "等待人工审查",
                    metadata: ["agent_id": "Reviewer-Agent"],
                    removable: false
                ),
            ]
        )

        let defaultSelection = try #require(presentation.selectedWorktree(id: nil))
        #expect(defaultSelection.name == "wt-clean")

        let selected = try #require(presentation.selectedWorktree(id: "wt-kept"))
        #expect(selected.path == "/repo/.naumi/worktrees/wt-kept")
        #expect(selected.canKeep == false)
        #expect(selected.keepDisabledReason(locale: .zhCN) == "已保留")
        #expect(selected.keepDisabledReason(locale: .enUS) == "Already kept")

        let clean = try #require(presentation.selectedWorktree(id: "wt-clean"))
        #expect(clean.canKeep == true)
        #expect(clean.defaultKeepReason(locale: .zhCN) == "人工保留 wt-clean，等待后续治理")
        #expect(clean.defaultKeepReason(locale: .enUS) == "Keep wt-clean for follow-up governance")
    }

    @Test func exposesSafeRemoveActionStateAndLocalizedReasons() throws {
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
                    dirtyFiles: 2,
                    commitsAhead: 1,
                    keptReason: "",
                    metadata: ["agent_id": "Reviewer-Agent"],
                    removable: false
                ),
                makeWorktree(
                    name: "wt-kept",
                    taskID: "task-3",
                    status: "kept",
                    dirtyFiles: 0,
                    commitsAhead: 0,
                    keptReason: "等待人工审查",
                    metadata: [:],
                    removable: false
                ),
            ]
        )

        let clean = try #require(presentation.selectedWorktree(id: "wt-clean"))
        #expect(clean.canRemoveSafely == true)
        #expect(clean.removeDisabledReason(locale: .zhCN) == nil)
        #expect(clean.removeDisabledReason(locale: .enUS) == nil)

        let dirty = try #require(presentation.selectedWorktree(id: "wt-dirty"))
        #expect(dirty.canRemoveSafely == false)
        #expect(dirty.canForceRemove == true)
        #expect(dirty.removeDisabledReason(locale: .zhCN) == "存在未提交或未审查的工作，只能通过强制删除流程处理")
        #expect(dirty.removeDisabledReason(locale: .enUS) == "Uncommitted or unreviewed work requires the force-remove flow")
        #expect(dirty.forceRemoveConfirmationTitle(locale: .zhCN) == "强制删除 wt-dirty？")
        #expect(dirty.forceRemoveConfirmationTitle(locale: .enUS) == "Force remove wt-dirty?")
        #expect(dirty.forceRemoveConfirmationMessage(locale: .zhCN) == "该工作区包含 2 个脏文件和 1 个领先提交。强制删除会丢弃这些未审查改动。")
        #expect(dirty.forceRemoveConfirmationMessage(locale: .enUS) == "This worktree has 2 dirty files and 1 commits ahead. Force removal discards those unreviewed changes.")

        let kept = try #require(presentation.selectedWorktree(id: "wt-kept"))
        #expect(kept.canRemoveSafely == false)
        #expect(kept.canForceRemove == false)
        #expect(kept.removeDisabledReason(locale: .zhCN) == "已人工保留，需先确认治理结果")
        #expect(kept.removeDisabledReason(locale: .enUS) == "Kept worktrees require governance confirmation first")
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
