import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct ReviewsDesignPresentationTests {

    @Test func emptyRuntimeStillProducesReferenceReviewFixture() {
        let presentation = ReviewsDesignPresentation(
            approvals: [],
            validationRuns: [],
            snapshot: nil
        )

        #expect(presentation.reviewQueues.count == 4)
        #expect(presentation.reviewQueues.first?.items.count == 2)
        #expect(presentation.fileChanges.count == 6)
        #expect(presentation.diffRows.count == 8)
        #expect(presentation.validationChecks.count == 3)
        #expect(presentation.timeline.count == 7)
        #expect(presentation.selectedReview.title == "Task Market Lease")
    }

    @Test func selectedReviewKeepsBackendTaskIDAndBuildsValidationDraft() {
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
            snapshot: nil
        )

        #expect(presentation.selectedReview.taskID == "task-market-lease")
        let draft = presentation.defaultValidationDraft(for: presentation.selectedReview)
        #expect(draft.taskID == "task-market-lease")
        #expect(draft.actor == "Backend-Agent")
        #expect(draft.commandLine == "pytest tests/unit/test_workbench_market.py -q")
        #expect(draft.canSubmit)
    }
}
