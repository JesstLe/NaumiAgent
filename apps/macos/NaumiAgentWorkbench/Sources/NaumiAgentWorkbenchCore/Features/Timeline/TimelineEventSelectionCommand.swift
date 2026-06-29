import Foundation

/// API command for loading a selected audit event detail.
public struct TimelineEventSelectionCommand: Equatable, Sendable {
    public let eventID: String

    public init?(event: TimelineEventPresentation) {
        let trimmedID = event.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedID.isEmpty else {
            return nil
        }

        self.eventID = trimmedID
    }
}
