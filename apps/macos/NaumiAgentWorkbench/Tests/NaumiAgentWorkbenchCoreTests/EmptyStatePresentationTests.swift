import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

/// M05-2 acceptance: empty real data must be visibly empty — presentations must
/// never pad an empty backend with fixture rows in real mode. Each page reaches
/// one of three states (no session, session-but-no-data, real data); these
/// tests lock in the "session-but-no-data" branch for every listed category:
/// mission, issues, leases, approvals, validation runs, events, worktrees.
struct EmptyStatePresentationTests {

    private func emptySnapshot(sessionID: String = "sess-empty") -> WorkbenchSnapshotDTO {
        WorkbenchSnapshotDTO(
            sessionID: sessionID,
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )
    }

    // MARK: - Dashboard (mission, issues, failures, events, agents, tasks)

    @Test func dashboardEmptySnapshotProducesNoRows() {
        let presentation = DashboardSnapshotPresentation(snapshot: emptySnapshot())

        // No mission, no issues, no leases, no approvals, no validation runs,
        // no events — every row collection must be empty.
        #expect(presentation.currentMission == nil)
        #expect(presentation.taskRows == [])
        #expect(presentation.issueRows == [])
        #expect(presentation.failureRows == [])
        #expect(presentation.agentRows == [])
        #expect(presentation.recentEventRows == [])
    }

    @Test func dashboardEmptySnapshotHasNoCanvasLandmarksOrInspector() {
        let workbench = DashboardSnapshotPresentation(snapshot: emptySnapshot()).workbench

        #expect(workbench.leftMissionTitle == nil)
        #expect(workbench.leftIssueCount == 0)
        #expect(workbench.leftTaskCount == 0)
        #expect(workbench.leftFailureCount == 0)
        // No mission/issue/agent/lease/task/failure/approval nodes are fabricated.
        #expect(workbench.canvasNodes == [])
        #expect(workbench.inspector == nil)
        #expect(workbench.auditRows == [])
    }

    @Test func dashboardEmptySnapshotEmitsNoDerivedCommands() {
        let presentation = DashboardSnapshotPresentation(snapshot: emptySnapshot())

        #expect(presentation.validationRerunCommand(validationRuns: []) == nil)
        #expect(presentation.contextRefreshCommand() == nil)
    }

    // MARK: - Task Market (issues + leases + bids)

    @Test func taskMarketEmptySnapshotProducesNoIssueRows() {
        let presentation = TaskMarketSnapshotPresentation(snapshot: emptySnapshot())

        #expect(presentation.rows == [])
        #expect(presentation.summary.totalIssues == 0)
        #expect(presentation.summary.openIssues == 0)
        #expect(presentation.summary.claimedIssues == 0)
        #expect(presentation.summary.blockedIssues == 0)
        #expect(presentation.summary.approvalRequiredIssues == 0)
    }

    @Test func taskMarketDesignRealModeDoesNotFabricateRowsOrLeases() {
        // Real mode must surface only live data; an empty snapshot yields no
        // issue rows, no bids, and no leases.
        let presentation = TaskMarketDesignPresentation(
            snapshot: emptySnapshot(),
            refreshedLeases: [],
            policy: .real
        )

        #expect(presentation.policy == .real)
        #expect(presentation.rows == [])
        #expect(presentation.bids == [])
        #expect(presentation.activeLeases == [])
        #expect(presentation.selectedIssue == nil)
    }

    @Test func taskMarketDesignPreviewModeIsAllowedToFill() {
        // Guard against an accidental tightening: preview mode keeps the
        // fixture padding so the design canvas stays illustrative offline.
        let presentation = TaskMarketDesignPresentation(
            snapshot: nil,
            refreshedLeases: [],
            policy: .preview
        )

        #expect(presentation.policy == .preview)
        #expect(!presentation.rows.isEmpty)
        #expect(!presentation.activeLeases.isEmpty)
    }

    // MARK: - Reviews (approvals + validation runs)

    @Test func reviewsDesignRealModeProducesNoQueuesWhenEmpty() {
        let presentation = ReviewsDesignPresentation(
            approvals: [],
            validationRuns: [],
            snapshot: emptySnapshot(),
            policy: .real
        )

        // No waiting approvals → no review queue at all (not even a fabricated one).
        #expect(presentation.reviewQueues == [])
        #expect(presentation.selectedReview == nil)
        #expect(presentation.validationChecks == [])
        // No diff-evidence endpoint yet → never fabricate file changes/diffs.
        #expect(presentation.fileChanges == [])
        #expect(presentation.diffRows == [])
        #expect(presentation.timeline == [])
        #expect(presentation.agentReviews == [])
    }

    // MARK: - Worktrees (worktrees + context snapshots)

    @Test func worktreesEmptyProducesNoRows() {
        let presentation = WorktreesDashboardPresentation(snapshots: [], worktrees: [])

        #expect(presentation.worktreeRows == [])
        #expect(presentation.worktreeCount == 0)
        #expect(presentation.snapshots == [])
        #expect(presentation.totalCount == 0)
        #expect(presentation.goodCount == 0)
        #expect(presentation.attentionCount == 0)
        #expect(presentation.activeAgentCount == 0)
        #expect(presentation.selectedSnapshot == nil)
        #expect(presentation.healthBuckets == [])
        #expect(presentation.agentBuckets == [])
    }

    // MARK: - Timeline (events)

    @Test func timelineEmptyEventsProducesNoRows() {
        let presentation = TimelineDashboardPresentation(events: [])

        #expect(presentation.events == [])
        #expect(presentation.totalCount == 0)
        #expect(presentation.actorCount == 0)
        #expect(presentation.latestEvent == nil)
        #expect(presentation.typeBuckets == [])
        #expect(presentation.actorBuckets == [])
        #expect(presentation.causalChain == [])
    }

    // MARK: - Validation runs

    @Test func emptyValidationRunsMapToNoPresentationRows() {
        // The Reviews page maps [ValidationRunDTO] → [ValidationRunPresentation];
        // an empty backend list must yield an empty presentation list.
        let presentations = [ValidationRunDTO]().map(ValidationRunPresentation.init)

        #expect(presentations == [])
    }
}
