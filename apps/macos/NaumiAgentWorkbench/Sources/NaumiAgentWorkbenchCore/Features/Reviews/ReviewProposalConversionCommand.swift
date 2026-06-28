import Foundation

/// API command for converting a pending approval review into a durable proposal decision.
public struct ReviewProposalConversionCommand: Equatable, Sendable {
    public let missionID: String
    public let actor: String
    public let kind: String
    public let title: String
    public let content: String

    public init?(
        review: ReviewDesignItem,
        missionID: String,
        actor: String,
        decisionNote: String,
        locale: AppLocale
    ) {
        let trimmedMissionID = missionID.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedActor = actor.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedMissionID.isEmpty, !trimmedActor.isEmpty else {
            return nil
        }

        self.missionID = trimmedMissionID
        self.actor = trimmedActor
        self.kind = "temporary"
        self.title = locale == .zhCN ? "提案：\(review.title)" : "Proposal: \(review.title)"
        self.content = Self.content(review: review, decisionNote: decisionNote)
    }

    private static func content(review: ReviewDesignItem, decisionNote: String) -> String {
        var lines = [
            "approval_id: \(review.id)",
            "task_id: \(review.taskID)",
            "risk: \(review.risk)",
            "worktree: \(review.worktree)",
            "requested_by: \(review.agent)",
            "proposal_reason: Human reviewer converted this approval into a proposal before final decision.",
        ]
        let trimmedNote = decisionNote.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedNote.isEmpty {
            lines.append("human_note: \(trimmedNote)")
        }
        return lines.joined(separator: "\n")
    }
}
