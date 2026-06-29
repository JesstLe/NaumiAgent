import Foundation

/// API command for loading a selected Dashboard failure detail.
public struct DashboardFailureSelectionCommand: Equatable, Sendable {
    public let failureID: String

    public init?(failure: DashboardFailureRow) {
        let trimmedFailureID = failure.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedFailureID.isEmpty else {
            return nil
        }

        self.failureID = trimmedFailureID
    }
}
