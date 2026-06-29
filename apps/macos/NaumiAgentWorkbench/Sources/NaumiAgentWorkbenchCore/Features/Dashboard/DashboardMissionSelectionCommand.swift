import Foundation

/// API command for loading a selected Dashboard mission detail.
public struct DashboardMissionSelectionCommand: Equatable, Sendable {
    public let missionID: String

    public init?(mission: DashboardMissionSummary) {
        let trimmedMissionID = mission.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedMissionID.isEmpty else {
            return nil
        }

        self.missionID = trimmedMissionID
    }
}
