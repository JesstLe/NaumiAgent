import Foundation

/// Normalizes a Settings decision row into a backend detail-load command.
public struct SettingsDecisionSelectionCommand: Equatable, Sendable {
    public let missionID: String
    public let decisionID: String

    public init?(row: SettingsDecisionRow) {
        let missionID = row.missionID.trimmingCharacters(in: .whitespacesAndNewlines)
        let decisionID = row.id.trimmingCharacters(in: .whitespacesAndNewlines)

        guard !missionID.isEmpty, !decisionID.isEmpty else {
            return nil
        }

        self.missionID = missionID
        self.decisionID = decisionID
    }
}
