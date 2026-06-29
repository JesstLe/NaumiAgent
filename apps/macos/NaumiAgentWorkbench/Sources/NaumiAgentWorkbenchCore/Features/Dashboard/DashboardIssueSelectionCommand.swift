import Foundation

/// API command for loading a selected Dashboard issue detail.
public struct DashboardIssueSelectionCommand: Equatable, Sendable {
    public let taskID: String

    public init?(issue: TaskMarketDesignIssue) {
        let trimmedTaskID = issue.taskID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedTaskID.isEmpty else {
            return nil
        }

        self.taskID = trimmedTaskID
    }
}
