import Foundation
import SwiftUI

/// Pure presentation model for a single context health snapshot row.
public struct ContextSnapshotPresentation: Equatable, Sendable, Identifiable {
    public let id: String
    public let sessionID: String
    public let agentID: String
    public let taskID: String
    public let health: String
    public let reasons: [String]
    public let createdAt: String

    public init(snapshot: ContextSnapshotDTO) {
        self.id = snapshot.id
        self.sessionID = snapshot.sessionID
        self.agentID = snapshot.agentID
        self.taskID = snapshot.taskID
        self.health = snapshot.health
        self.reasons = snapshot.reasons
        self.createdAt = snapshot.createdAt
    }

    /// Returns a localized, human-readable health label.
    public func healthLabel(locale: AppLocale) -> String {
        switch health.lowercased() {
        case "good":
            return AppStrings.Worktrees.healthGood(locale)
        case "stale":
            return AppStrings.Worktrees.healthStale(locale)
        case "overloaded":
            return AppStrings.Worktrees.healthOverloaded(locale)
        case "missing":
            return AppStrings.Worktrees.healthMissing(locale)
        case "conflicted":
            return AppStrings.Worktrees.healthConflicted(locale)
        default:
            return AppStrings.Worktrees.healthUnknown(locale, health: health)
        }
    }

    /// Returns the color associated with the health state.
    public func healthColor() -> Color {
        switch health.lowercased() {
        case "good":
            return .green
        case "stale":
            return .orange
        case "overloaded":
            return .red
        case "missing":
            return .gray
        case "conflicted":
            return .purple
        default:
            return .secondary
        }
    }

    /// Returns the reasons collapsed into a localized single-line summary.
    public func reasonsSummary(locale: AppLocale) -> String {
        let joined = reasons
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: locale == .zhCN ? "；" : "; ")
        return joined.isEmpty ? "-" : joined
    }

    /// Returns the reasons collapsed into a Chinese-default single-line summary.
    public var reasonsSummary: String {
        reasonsSummary(locale: .zhCN)
    }
}
