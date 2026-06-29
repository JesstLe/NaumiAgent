import Foundation

/// API command for loading a selected task-market issue detail.
public struct TaskMarketIssueSelectionCommand: Equatable, Sendable {
    public let taskID: String

    public init?(issue: TaskMarketDesignIssue) {
        let trimmedTaskID = issue.taskID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedTaskID.isEmpty else {
            return nil
        }

        self.taskID = trimmedTaskID
    }
}
