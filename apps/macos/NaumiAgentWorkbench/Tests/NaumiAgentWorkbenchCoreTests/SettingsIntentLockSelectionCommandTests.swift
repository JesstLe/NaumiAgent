import Testing
@testable import NaumiAgentWorkbenchCore

struct SettingsIntentLockSelectionCommandTests {

    @Test func commandUsesSelectedIntentLockIdentifiers() throws {
        let command = try #require(SettingsIntentLockSelectionCommand(
            row: row(id: " lock-001 ", missionID: " mission-001 ")
        ))

        #expect(command.lockID == "lock-001")
        #expect(command.missionID == "mission-001")
    }

    @Test func commandIsNilWhenLockIDIsEmpty() {
        #expect(SettingsIntentLockSelectionCommand(
            row: row(id: "   ", missionID: "mission-001")
        ) == nil)
    }

    @Test func commandIsNilWhenMissionIDIsEmpty() {
        #expect(SettingsIntentLockSelectionCommand(
            row: row(id: "lock-001", missionID: "   ")
        ) == nil)
    }

    private func row(id: String, missionID: String) -> SettingsIntentLockRow {
        SettingsIntentLockRow(
            id: id,
            missionID: missionID,
            rule: "禁止直接改动认证模块",
            scopeSummary: "阻塞 1 / 允许 1",
            riskLabel: "high",
            isActive: true,
            createdBy: "Human",
            createdAt: "2026-06-27T09:12:00",
            updatedAt: "2026-06-27T09:12:00"
        )
    }
}
