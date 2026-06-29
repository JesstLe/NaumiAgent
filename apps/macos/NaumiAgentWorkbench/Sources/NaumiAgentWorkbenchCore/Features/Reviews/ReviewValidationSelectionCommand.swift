import Foundation

/// API command for loading a selected review validation run detail.
public struct ReviewValidationSelectionCommand: Equatable, Sendable {
    public let runID: String

    public init?(check: ReviewDesignCheck) {
        let trimmedRunID = check.runID?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !trimmedRunID.isEmpty else {
            return nil
        }

        self.runID = trimmedRunID
    }
}
