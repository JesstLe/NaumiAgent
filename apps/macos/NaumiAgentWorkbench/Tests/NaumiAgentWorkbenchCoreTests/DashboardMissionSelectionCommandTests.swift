import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardMissionSelectionCommandTests {

    @Test func commandUsesSelectedMissionID() throws {
        let command = try #require(DashboardMissionSelectionCommand(
            mission: mission(id: "  mission-123  ")
        ))

        #expect(command.missionID == "mission-123")
    }

    @Test func commandIsNilWhenMissionIDIsEmpty() {
        #expect(DashboardMissionSelectionCommand(mission: mission(id: "   ")) == nil)
    }

    private func mission(id: String) -> DashboardMissionSummary {
        DashboardMissionSummary(
            id: id,
            title: "实现 SwiftUI 工作台骨架",
            status: "planning"
        )
    }
}
