import Foundation

/// API command for loading a selected context health snapshot detail.
public struct ContextSnapshotSelectionCommand: Equatable, Sendable {
    public let snapshotID: String

    public init?(snapshot: ContextSnapshotPresentation) {
        let trimmedID = snapshot.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedID.isEmpty else {
            return nil
        }

        self.snapshotID = trimmedID
    }
}
