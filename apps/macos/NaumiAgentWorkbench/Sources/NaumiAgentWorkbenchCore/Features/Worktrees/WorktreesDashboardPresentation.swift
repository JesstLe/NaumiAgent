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
    }
}

public struct WorktreeHealthBucket: Equatable, Sendable, Identifiable {
    public var id: String { health }
    public let health: String
    public let count: Int
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
