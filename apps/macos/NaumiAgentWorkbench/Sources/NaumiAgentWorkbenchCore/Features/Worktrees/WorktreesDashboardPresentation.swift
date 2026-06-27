import Foundation

/// Dashboard-level summary for the worktree/context health page.
public struct WorktreesDashboardPresentation: Equatable, Sendable {
    public let snapshots: [ContextSnapshotPresentation]
    public let totalCount: Int
    public let goodCount: Int
    public let attentionCount: Int
    public let activeAgentCount: Int
    public let selectedSnapshot: ContextSnapshotPresentation?
    public let healthBuckets: [WorktreeHealthBucket]
    public let agentBuckets: [WorktreeAgentBucket]
    public let recommendedActions: [WorktreeRecommendedAction]

    public init(snapshots: [ContextSnapshotDTO]) {
        let presented = snapshots.map(ContextSnapshotPresentation.init)
        self.snapshots = presented
        self.totalCount = presented.count
        self.goodCount = presented.filter { $0.health.lowercased() == "good" }.count
        self.attentionCount = presented.count - self.goodCount
        self.activeAgentCount = Set(presented.map(\.agentID)).count
        self.selectedSnapshot = presented.max { left, right in
            severityRank(for: left.health) < severityRank(for: right.health)
        }
        self.healthBuckets = Dictionary(grouping: presented, by: { $0.health.lowercased() })
            .map { health, snapshots in
                WorktreeHealthBucket(health: health, count: snapshots.count)
            }
            .sorted { left, right in
                if severityRank(for: left.health) == severityRank(for: right.health) {
                    return left.health < right.health
                }
                return severityRank(for: left.health) > severityRank(for: right.health)
            }
        self.agentBuckets = Dictionary(grouping: presented, by: \.agentID)
            .map { agentID, snapshots in
                let worstHealth = snapshots.max { left, right in
                    severityRank(for: left.health) < severityRank(for: right.health)
                }?.health.lowercased() ?? "unknown"
                return WorktreeAgentBucket(
                    agentID: agentID,
                    snapshotCount: snapshots.count,
                    attentionCount: snapshots.filter { $0.health.lowercased() != "good" }.count,
                    worstHealth: worstHealth
                )
            }
            .sorted { left, right in
                if left.attentionCount == right.attentionCount {
                    if severityRank(for: left.worstHealth) == severityRank(for: right.worstHealth) {
                        return left.agentID < right.agentID
                    }
                    return severityRank(for: left.worstHealth) > severityRank(for: right.worstHealth)
                }
                return left.attentionCount > right.attentionCount
            }
        self.recommendedActions = WorktreeRecommendedAction.actions(for: selectedSnapshot?.health)
    }
}

public struct WorktreeHealthBucket: Equatable, Sendable, Identifiable {
    public var id: String { health }
    public let health: String
    public let count: Int
}

public struct WorktreeAgentBucket: Equatable, Sendable, Identifiable {
    public var id: String { agentID }
    public let agentID: String
    public let snapshotCount: Int
    public let attentionCount: Int
    public let worstHealth: String
}

public enum WorktreeRecommendedActionKind: Equatable, Sendable {
    case pauseAgent
    case refreshContext
    case openReview
    case rehydrateSnapshot
}

public struct WorktreeRecommendedAction: Equatable, Sendable, Identifiable {
    public var id: WorktreeRecommendedActionKind { kind }
    public let kind: WorktreeRecommendedActionKind

    public var systemImage: String {
        switch kind {
        case .pauseAgent:
            return "pause.circle"
        case .refreshContext:
            return "arrow.clockwise"
        case .openReview:
            return "checkmark.shield"
        case .rehydrateSnapshot:
            return "tray.and.arrow.down"
        }
    }

    public func title(locale: AppLocale) -> String {
        switch kind {
        case .pauseAgent:
            return locale == .zhCN ? "暂停相关 Agent" : "Pause Related Agent"
        case .refreshContext:
            return locale == .zhCN ? "刷新上下文快照" : "Refresh Context Snapshot"
        case .openReview:
            return locale == .zhCN ? "进入人工审查" : "Open Human Review"
        case .rehydrateSnapshot:
            return locale == .zhCN ? "重新补齐快照" : "Rehydrate Snapshot"
        }
    }

    public func detail(locale: AppLocale) -> String {
        switch kind {
        case .pauseAgent:
            return locale == .zhCN ? "先冻结写入，避免冲突继续扩大。" : "Freeze writes first so the conflict does not spread."
        case .refreshContext:
            return locale == .zhCN ? "重新读取分支、租约和验证证据。" : "Reload branch, lease, and validation evidence."
        case .openReview:
            return locale == .zhCN ? "把高风险上下文交给人类确认。" : "Escalate the risky context for human confirmation."
        case .rehydrateSnapshot:
            return locale == .zhCN ? "缺失上下文时重新收集任务证据。" : "Collect task evidence again when context is missing."
        }
    }

    fileprivate static func actions(for health: String?) -> [WorktreeRecommendedAction] {
        guard let health else { return [] }
        switch health.lowercased() {
        case "conflicted", "overloaded":
            return [
                WorktreeRecommendedAction(kind: .pauseAgent),
                WorktreeRecommendedAction(kind: .refreshContext),
                WorktreeRecommendedAction(kind: .openReview),
            ]
        case "stale":
            return [
                WorktreeRecommendedAction(kind: .refreshContext),
                WorktreeRecommendedAction(kind: .openReview),
            ]
        case "missing":
            return [
                WorktreeRecommendedAction(kind: .rehydrateSnapshot),
                WorktreeRecommendedAction(kind: .refreshContext),
            ]
        case "good":
            return []
        default:
            return [WorktreeRecommendedAction(kind: .refreshContext)]
        }
    }
}

private func severityRank(for health: String) -> Int {
    switch health.lowercased() {
    case "conflicted":
        return 5
    case "overloaded":
        return 4
    case "stale":
        return 3
    case "missing":
        return 2
    case "unknown":
        return 1
    case "good":
        return 0
    default:
        return 1
    }
}
