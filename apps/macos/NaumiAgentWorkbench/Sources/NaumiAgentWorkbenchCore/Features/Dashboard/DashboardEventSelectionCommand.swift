import Foundation

/// API command for loading a selected Dashboard audit event detail.
public struct DashboardEventSelectionCommand: Equatable, Sendable {
    public let eventID: String

    public init?(event: DashboardEventRow) {
        let trimmedEventID = event.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedEventID.isEmpty else {
            return nil
        }

        self.eventID = trimmedEventID
    }
}
