import Foundation

/// API command for turning a market bid into a durable proposal request decision.
public struct TaskMarketProposalRequestCommand: Equatable, Sendable {
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
            ? "提案请求：\(issue.title)"
            : "Proposal Request: \(issue.title)"
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
            "request_reason: Human requested a concrete proposal before assigning the bid.",
        ].joined(separator: "\n")
    }
}
