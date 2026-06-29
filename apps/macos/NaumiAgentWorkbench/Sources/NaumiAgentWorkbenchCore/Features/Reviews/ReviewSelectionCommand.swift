import Foundation

/// API command for loading a selected approval review detail.
public struct ReviewSelectionCommand: Equatable, Sendable {
    public let approvalID: String

    public init?(review: ReviewDesignItem) {
        let trimmedApprovalID = review.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedApprovalID.isEmpty else {
            return nil
        }

        self.approvalID = trimmedApprovalID
    }
}
