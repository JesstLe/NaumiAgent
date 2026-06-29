import Testing
@testable import NaumiAgentWorkbenchCore

struct SettingsDecisionSelectionCommandTests {

    @Test func commandUsesSelectedDecisionIdentifiers() throws {
        let command = try #require(SettingsDecisionSelectionCommand(
            row: row(id: " decision-001 ", missionID: " mission-001 ")
        ))

        #expect(command.decisionID == "decision-001")
        #expect(command.missionID == "mission-001")
    }

    @Test func commandIsNilWhenDecisionIDIsEmpty() {
        #expect(SettingsDecisionSelectionCommand(
            row: row(id: "   ", missionID: "mission-001")
        ) == nil)
    }

    @Test func commandIsNilWhenMissionIDIsEmpty() {
        #expect(SettingsDecisionSelectionCommand(
            row: row(id: "decision-001", missionID: "   ")
        ) == nil)
    }

    private func row(id: String, missionID: String) -> SettingsDecisionRow {
        SettingsDecisionRow(
            id: id,
            missionID: missionID,
            kind: "architecture",
            title: "采用本地 daemon 桥接",
            actor: "Planner-Agent",
            createdAt: "2026-06-27T09:18:00"
        )
    }
}
