import Foundation

/// API command for loading the task-market current mission detail.
public struct TaskMarketMissionSelectionCommand: Equatable, Sendable {
    public let missionID: String

    public init?(missionID: String) {
        let trimmedMissionID = missionID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedMissionID.isEmpty else {
            return nil
        }

        self.missionID = trimmedMissionID
    }
}
