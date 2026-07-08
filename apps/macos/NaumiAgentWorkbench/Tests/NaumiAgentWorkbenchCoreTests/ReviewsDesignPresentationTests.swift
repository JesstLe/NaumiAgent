import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct ReviewsDesignPresentationTests {

    // MARK: - Preview mode (fixture fillers explicitly allowed)

    @Test func emptyRuntimeStillProducesReferenceReviewFixture() {
        let presentation = ReviewsDesignPresentation(
            approvals: [],
            validationRuns: [],
            snapshot: nil,
            policy: .preview
        )

        #expect(presentation.reviewQueues.count == 4)
        #expect(presentation.reviewQueues.first?.items.count == 2)
        #expect(presentation.fileChanges.count == 6)
        #expect(presentation.diffRows.count == 8)
        #expect(presentation.validationChecks.count == 3)
        #expect(presentation.timeline.count == 7)
        #expect(presentation.selectedReview?.title == "Task Market Lease")
    }

    // MARK: - Real mode (no fixture fillers)

    @Test func realModeNeverAppendsFixtureReviewData() {
        let presentation = ReviewsDesignPresentation(
            approvals: [],
            validationRuns: [],
            snapshot: nil,
            policy: .real
        )

        // No fixture queues, files, diffs, timeline, or agent notes.
        #expect(presentation.reviewQueues.isEmpty)
        #expect(presentation.fileChanges.isEmpty)
        #expect(presentation.diffRows.isEmpty)
        #expect(presentation.timeline.isEmpty)
        #expect(presentation.agentReviews.isEmpty)
        #expect(presentation.validationChecks.isEmpty)
        #expect(presentation.selectedReview == nil)
    }

    @Test func realModeBuildsWaitingQueueFromLiveApprovalsOnly() throws {
        let approval = ApprovalDTO(
            id: "approval-1",
            sessionID: "sess-1",
            missionID: "mission-1",
            taskID: "task-market-lease",
            state: "waiting",
            title: "任务市场租约策略",
            detail: "需要人工审查",
            requester: "Backend-Agent",
            reviewer: "",
            decisionNote: "",
            createdAt: "2026-06-27T09:28:00",
            updatedAt: "2026-06-27T09:36:00"
        )

        let presentation = ReviewsDesignPresentation(
            approvals: [approval],
            validationRuns: [],
            snapshot: nil,
            policy: .real
        )

        // Only the waiting queue is derived from live approvals; no fabricated
        // REQUEST CHANGES / AUTO-MERGE / HIGH RISK queues.
        #expect(presentation.reviewQueues.count == 1)
        let waiting = try #require(presentation.reviewQueues.first)
        #expect(waiting.items.count == 1)
        #expect(waiting.items.first?.taskID == "task-market-lease")
        #expect(presentation.selectedReview?.taskID == "task-market-lease")

        // No fixture file changes, diffs, timeline, or agent notes.
        #expect(presentation.fileChanges.isEmpty)
        #expect(presentation.diffRows.isEmpty)
        #expect(presentation.timeline.isEmpty)
        #expect(presentation.agentReviews.isEmpty)
    }

    @Test func realModeKeepsBackendTaskIDAndBuildsValidationDraft() throws {
        let approval = ApprovalDTO(
            id: "approval-1",
            sessionID: "sess-1",
            missionID: "mission-1",
            taskID: "task-market-lease",
            state: "waiting",
            title: "任务市场租约策略",
            detail: "需要人工审查",
            requester: "Backend-Agent",
            reviewer: "",
            decisionNote: "",
            createdAt: "2026-06-27T09:28:00",
            updatedAt: "2026-06-27T09:36:00"
        )

        let presentation = ReviewsDesignPresentation(
            approvals: [approval],
            validationRuns: [],
            snapshot: nil,
            policy: .real
        )

        let selected = try #require(presentation.selectedReview)
        #expect(selected.taskID == "task-market-lease")
        let draft = presentation.defaultValidationDraft(for: selected)
        #expect(draft.taskID == "task-market-lease")
        #expect(draft.actor == "Backend-Agent")
        #expect(draft.canSubmit)
    }

    @Test func realModeValidationChecksUseLiveRunsOnly() throws {
        let run = ValidationRunDTO(
            id: "run-001",
            sessionID: "sess-1",
            taskID: "task-market-lease",
            actor: "Backend-Agent",
            command: ["pytest", "tests/unit/test_workbench_market.py", "-q"],
            cwd: ".",
            status: "passed",
            exitCode: 0,
            output: "passed",
            startedAt: "2026-06-27T09:28:00",
            completedAt: "2026-06-27T09:29:00"
        )

        let presentation = ReviewsDesignPresentation(
            approvals: [],
            validationRuns: [run],
            snapshot: nil,
            policy: .real
        )

        // Live run is surfaced; no fixture padding.
        #expect(presentation.validationChecks.count == 1)
        let firstCheck = try #require(presentation.validationChecks.first)
        #expect(firstCheck.id == "run-001")
        #expect(firstCheck.runID == "run-001")
        #expect(firstCheck.name == "pytest tests/unit/test_workbench_market.py -q")
    }

    @Test func realModeDefaultsToNoFixtureFillers() {
        // The default policy must be real — production views that forget to
        // pass a policy must never accidentally render fixtures.
        let presentation = ReviewsDesignPresentation(
            approvals: [],
            validationRuns: [],
            snapshot: nil
        )

        #expect(presentation.reviewQueues.isEmpty)
        #expect(presentation.fileChanges.isEmpty)
        #expect(presentation.diffRows.isEmpty)
        #expect(presentation.selectedReview == nil)
    }
}
