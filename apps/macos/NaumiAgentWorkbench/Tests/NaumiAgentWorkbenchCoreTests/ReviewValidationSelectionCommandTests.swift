import Testing
@testable import NaumiAgentWorkbenchCore

struct ReviewValidationSelectionCommandTests {

    @Test func commandUsesSelectedRunID() throws {
        let command = try #require(ReviewValidationSelectionCommand(
            check: check(runID: "  run-123  ")
        ))

        #expect(command.runID == "run-123")
    }

    @Test func commandIsNilWhenRunIDIsMissing() {
        #expect(ReviewValidationSelectionCommand(check: check(runID: nil)) == nil)
    }

    @Test func commandIsNilWhenRunIDIsEmpty() {
        #expect(ReviewValidationSelectionCommand(check: check(runID: "   ")) == nil)
    }

    private func check(runID: String?) -> ReviewDesignCheck {
        ReviewDesignCheck(
            runID: runID,
            name: "pytest tests/unit/test_workbench_market.py -q",
            status: "passed",
            time: "09:29"
        )
    }
}
