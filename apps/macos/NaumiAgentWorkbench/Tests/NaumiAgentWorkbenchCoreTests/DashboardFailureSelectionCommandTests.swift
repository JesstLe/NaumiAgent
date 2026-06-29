import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardFailureSelectionCommandTests {

    @Test func commandUsesSelectedFailureID() throws {
        let command = try #require(DashboardFailureSelectionCommand(
            failure: failure(id: "  failure-123  ")
        ))

        #expect(command.failureID == "failure-123")
    }

    @Test func commandIsNilWhenFailureIDIsEmpty() {
        #expect(DashboardFailureSelectionCommand(failure: failure(id: "   ")) == nil)
    }

    private func failure(id: String) -> DashboardFailureRow {
        DashboardFailureRow(
            id: id,
            title: "DTO 解码测试失败",
            kind: "test_failed",
            status: "open",
            taskID: "task-1"
        )
    }
}
