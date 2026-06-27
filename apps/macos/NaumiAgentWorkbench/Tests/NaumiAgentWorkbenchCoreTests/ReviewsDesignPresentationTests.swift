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
}
