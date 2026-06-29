import Foundation

/// API command for assigning a selected bid by claiming the issue lease.
public struct TaskMarketBidAssignmentCommand: Equatable, Sendable {
    public let taskID: String
    public let agentID: String
    public let durationMinutes: Int
    public let worktreeName: String

    public init?(
        issue: TaskMarketDesignIssue,
        bid: TaskMarketDesignBid,
        durationMinutes: Int
    ) {
        let taskID = issue.taskID.trimmingCharacters(in: .whitespacesAndNewlines)
        let agentID = bid.agent.trimmingCharacters(in: .whitespacesAndNewlines)

        guard issue.canClaim, !taskID.isEmpty, !agentID.isEmpty else {
            return nil
        }

        self.taskID = taskID
        self.agentID = agentID
        self.durationMinutes = min(max(durationMinutes, 1), 240)
        self.worktreeName = Self.worktreeName(for: issue)
    }

    private static func worktreeName(for issue: TaskMarketDesignIssue) -> String {
        let trimmed = issue.worktree.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty, trimmed != "-" {
            return trimmed
        }
        return issue.defaultClaimWorktreeName
    }
}
