import Foundation

/// API command for keeping the worktree behind a review item for human governance.
public struct ReviewWorktreeKeepCommand: Equatable, Sendable {
    public let name: String
    public let actor: String
    public let reason: String

    public init?(
        review: ReviewDesignItem,
        worktrees: [WorktreeDTO],
        actor: String,
        locale: AppLocale
    ) {
        let trimmedActor = actor.trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedName = Self.resolvedWorktreeName(review: review, worktrees: worktrees)
        guard !trimmedActor.isEmpty, !resolvedName.isEmpty else {
            return nil
        }

        self.name = resolvedName
        self.actor = trimmedActor
        self.reason = Self.keepReason(name: resolvedName, reviewTitle: review.title, locale: locale)
    }

    private static func resolvedWorktreeName(
        review: ReviewDesignItem,
        worktrees: [WorktreeDTO]
    ) -> String {
        if let matched = worktrees.first(where: { $0.taskID == review.taskID }) {
            return matched.name.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return review.worktree.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func keepReason(name: String, reviewTitle: String, locale: AppLocale) -> String {
        if locale == .zhCN {
            return "人工保留 \(name)，等待审查：\(reviewTitle)"
        }
        return "Keep \(name) for review: \(reviewTitle)"
    }
}
