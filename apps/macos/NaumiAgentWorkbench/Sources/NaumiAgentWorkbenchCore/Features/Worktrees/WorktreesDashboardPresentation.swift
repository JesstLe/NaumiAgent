import Foundation

/// Dashboard-level summary for the worktree/context health page.
public struct WorktreesDashboardPresentation: Equatable, Sendable {
    public let snapshots: [ContextSnapshotPresentation]
    public let worktreeRows: [WorktreeManagementRow]
    public let worktreeCount: Int
    public let cleanWorktreeCount: Int
    public let dirtyWorktreeCount: Int
    public let keptWorktreeCount: Int
    public let removableWorktreeCount: Int
    public let totalCount: Int
    public let goodCount: Int
    public let attentionCount: Int
    public let activeAgentCount: Int
    public let selectedSnapshot: ContextSnapshotPresentation?
    public let healthBuckets: [WorktreeHealthBucket]
    public let agentBuckets: [WorktreeAgentBucket]
    public let recommendedActions: [WorktreeRecommendedAction]

    public init(snapshots: [ContextSnapshotDTO], worktrees: [WorktreeDTO] = []) {
        let presented = snapshots.map(ContextSnapshotPresentation.init)
        let worktreeRows = worktrees.map(WorktreeManagementRow.init)
        self.worktreeRows = worktreeRows
        self.worktreeCount = worktreeRows.count
        self.cleanWorktreeCount = worktreeRows.filter { $0.status.lowercased() == "clean" }.count
        self.dirtyWorktreeCount = worktreeRows.filter { $0.dirtyFiles > 0 || $0.status.lowercased() == "dirty" }.count
        self.keptWorktreeCount = worktreeRows.filter { !$0.keptReason.isEmpty || $0.status.lowercased() == "kept" }.count
        self.removableWorktreeCount = worktreeRows.filter(\.removable).count
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

    public func selectedWorktree(id: String?) -> WorktreeManagementRow? {
        guard let id else {
            return worktreeRows.first
        }
        return worktreeRows.first { $0.id == id } ?? worktreeRows.first
    }
}

public enum WorktreeStatusTone: Equatable, Sendable {
    case normal
    case warning
    case kept
    case blocked
}

public struct WorktreeManagementRow: Equatable, Sendable, Identifiable {
    public var id: String { name }
    public let name: String
    public let path: String
    public let taskID: String
    public let agentID: String
    public let branch: String
    public let status: String
    public let dirtyFiles: Int
    public let commitsAhead: Int
    public let removable: Bool
    public let updatedAt: String
    public let keptReason: String

    public var statusTone: WorktreeStatusTone {
        let normalizedStatus = status.lowercased()
        if !keptReason.isEmpty || normalizedStatus == "kept" {
            return .kept
        }
        if dirtyFiles > 0 || normalizedStatus == "dirty" || normalizedStatus == "conflicted" {
            return .warning
        }
        if normalizedStatus == "blocked" || normalizedStatus == "missing" {
            return .blocked
        }
        return .normal
    }

    public var canKeep: Bool {
        !isKept
    }

    public var canRemoveSafely: Bool {
        removable
    }

    public var canForceRemove: Bool {
        hasUnreviewedWork && !isKept
    }

    private var isKept: Bool {
        !keptReason.isEmpty || status.lowercased() == "kept"
    }

    private var hasUnreviewedWork: Bool {
        dirtyFiles > 0 || commitsAhead > 0 || status.lowercased() == "dirty"
    }

    public func defaultKeepReason(locale: AppLocale) -> String {
        locale == .zhCN
            ? "人工保留 \(name)，等待后续治理"
            : "Keep \(name) for follow-up governance"
    }

    public func keepDisabledReason(locale: AppLocale) -> String? {
        guard !canKeep else { return nil }
        return locale == .zhCN ? "已保留" : "Already kept"
    }

    public func removeDisabledReason(locale: AppLocale) -> String? {
        guard !canRemoveSafely else { return nil }
        if isKept {
            return locale == .zhCN
                ? "已人工保留，需先确认治理结果"
                : "Kept worktrees require governance confirmation first"
        }
        if hasUnreviewedWork {
            return locale == .zhCN
                ? "存在未提交或未审查的工作，只能通过强制删除流程处理"
                : "Uncommitted or unreviewed work requires the force-remove flow"
        }
        return locale == .zhCN ? "当前状态不可安全删除" : "Current state cannot be safely removed"
    }

    public func forceRemoveConfirmationTitle(locale: AppLocale) -> String {
        locale == .zhCN ? "强制删除 \(name)？" : "Force remove \(name)?"
    }

    public func forceRemoveConfirmationMessage(locale: AppLocale) -> String {
        if locale == .zhCN {
            return "该工作区包含 \(dirtyFiles) 个脏文件和 \(commitsAhead) 个领先提交。强制删除会丢弃这些未审查改动。"
        }
        return "This worktree has \(dirtyFiles) dirty files and \(commitsAhead) commits ahead. Force removal discards those unreviewed changes."
    }

    public init(worktree: WorktreeDTO) {
        self.name = worktree.name
        self.path = worktree.path
        self.taskID = worktree.taskID
        self.agentID = worktree.metadata["agent_id"] ?? worktree.metadata["owner"] ?? "-"
        self.branch = worktree.branch
        self.status = worktree.status
        self.dirtyFiles = worktree.dirtyFiles
        self.commitsAhead = worktree.commitsAhead
        self.removable = worktree.removable
        self.updatedAt = worktree.updatedAt
        self.keptReason = worktree.keptReason
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
