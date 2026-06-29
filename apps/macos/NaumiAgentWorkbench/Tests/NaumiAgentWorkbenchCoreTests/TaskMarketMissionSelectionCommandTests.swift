import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketMissionSelectionCommandTests {

    @Test func commandUsesTrimmedMissionID() throws {
        let command = try #require(TaskMarketMissionSelectionCommand(missionID: "  mission-123  "))

        #expect(command.missionID == "mission-123")
    }

    @Test func commandIsNilWhenMissionIDIsEmpty() {
        #expect(TaskMarketMissionSelectionCommand(missionID: "   ") == nil)
    }
}
