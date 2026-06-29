import Foundation

/// API command for recording a durable human rejection of a task-market bid.
public struct TaskMarketBidRejectionCommand: Equatable, Sendable {
    public let missionID: String
    public let actor: String
    public let kind: String
    public let title: String
    public let content: String

    public init?(
        issue: TaskMarketDesignIssue,
        bid: TaskMarketDesignBid,
        missionID: String,
        locale: AppLocale
    ) {
        let trimmedMissionID = missionID.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedTaskID = issue.taskID.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedAgent = bid.agent.trimmingCharacters(in: .whitespacesAndNewlines)

        guard !trimmedMissionID.isEmpty, !trimmedTaskID.isEmpty, !trimmedAgent.isEmpty else {
            return nil
        }

        self.missionID = trimmedMissionID
        self.actor = "Human"
        self.kind = "temporary"
        self.title = locale == .zhCN
            ? "拒绝竞标：\(issue.title)"
            : "Bid Rejected: \(issue.title)"
        self.content = Self.content(
            issue: issue,
            taskID: trimmedTaskID,
            bid: bid,
            bidAgent: trimmedAgent
        )
    }

    private static func content(
        issue: TaskMarketDesignIssue,
        taskID: String,
        bid: TaskMarketDesignBid,
        bidAgent: String
    ) -> String {
        [
            "task_id: \(taskID)",
            "risk: \(issue.risk)",
            "parallel_mode: \(issue.parallelMode)",
            "status: \(issue.status)",
            "worktree: \(issue.worktree)",
            "bid_agent: \(bidAgent)",
            "bid_confidence: \(bid.confidence)",
            "bid_estimate: \(bid.estimate)",
            "bid_eta: \(bid.eta)",
            "bid_note: \(bid.note)",
            "rejection_reason: Human rejected this bid in the task market.",
        ].joined(separator: "\n")
    }
}
