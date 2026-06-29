import Foundation

/// Normalizes a Settings intent-lock row into a backend detail-load command.
public struct SettingsIntentLockSelectionCommand: Equatable, Sendable {
    public let missionID: String
    public let lockID: String

    public init?(row: SettingsIntentLockRow) {
        let missionID = row.missionID.trimmingCharacters(in: .whitespacesAndNewlines)
        let lockID = row.id.trimmingCharacters(in: .whitespacesAndNewlines)

        guard !missionID.isEmpty, !lockID.isEmpty else {
            return nil
        }

        self.missionID = missionID
        self.lockID = lockID
    }
}
