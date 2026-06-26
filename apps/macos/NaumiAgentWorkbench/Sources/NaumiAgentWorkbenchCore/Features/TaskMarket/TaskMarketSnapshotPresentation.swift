import Foundation

/// Lease state derived from task ownership.
///
/// The current ``WorkbenchSnapshotDTO`` does not include a dedicated `leases`
/// array, so we approximate lease state from the task's `owner` field. This is
/// an intentional limitation documented in the UI rather than fabricated data.
public enum TaskMarketLeaseState: Equatable, Sendable {
    case claimed
    case open
}

/// Pure presentation model for the Task Market page.
public struct TaskMarketSnapshotPresentation: Equatable, Sendable {
    public let summary: TaskMarketSummary
    public let rows: [TaskMarketIssueRow]

    public init(snapshot: WorkbenchSnapshotDTO) {
        let taskByID = Dictionary(uniqueKeysWithValues: snapshot.tasks.map { ($0.id, $0) })

        // Build one row per issue that can be matched to a task. Issues without
        // a matching task are dropped because the market table needs task-level
        // fields (status, owner, dependencies).
        let unsortedRows: [(index: Int, row: TaskMarketIssueRow)] = snapshot.issues.enumerated().compactMap { index, issue in
            guard let task = taskByID[issue.taskID] else { return nil }
            return (index, TaskMarketIssueRow(issue: issue, task: task))
        }

        self.rows = TaskMarketSnapshotPresentation.sortRows(unsortedRows)

        let totalIssues = snapshot.issues.count
        let openIssues = self.rows.filter { $0.leaseState == .open }.count
        let claimedIssues = self.rows.filter { $0.leaseState == .claimed }.count
        let blockedIssues = self.rows.filter { $0.isBlocked }.count
        let approvalRequiredIssues = self.rows.filter { $0.requiresHumanApproval }.count

        self.summary = TaskMarketSummary(
            totalIssues: totalIssues,
            openIssues: openIssues,
            claimedIssues: claimedIssues,
            blockedIssues: blockedIssues,
            approvalRequiredIssues: approvalRequiredIssues
        )
    }

    /// Sort rules: blocked first, then high/critical risk, then in-progress,
    /// finally preserving original array order for stability.
    private static func sortRows(
        _ rows: [(index: Int, row: TaskMarketIssueRow)]
    ) -> [TaskMarketIssueRow] {
        rows.sorted { lhs, rhs in
            let lhsBlocked = lhs.row.isBlocked ? 0 : 1
            let rhsBlocked = rhs.row.isBlocked ? 0 : 1
            if lhsBlocked != rhsBlocked {
                return lhsBlocked < rhsBlocked
            }

            let lhsSevere = lhs.row.riskLevelIsHighOrCritical ? 0 : 1
            let rhsSevere = rhs.row.riskLevelIsHighOrCritical ? 0 : 1
            if lhsSevere != rhsSevere {
                return lhsSevere < rhsSevere
            }

            let lhsInProgress = lhs.row.status.lowercased() == "in_progress" ? 0 : 1
            let rhsInProgress = rhs.row.status.lowercased() == "in_progress" ? 0 : 1
            if lhsInProgress != rhsInProgress {
                return lhsInProgress < rhsInProgress
            }

            return lhs.index < rhs.index
        }.map(\.row)
    }
}

public struct TaskMarketSummary: Equatable, Sendable {
    public let totalIssues: Int
    public let openIssues: Int
    public let claimedIssues: Int
    public let blockedIssues: Int
    public let approvalRequiredIssues: Int
}

public struct TaskMarketIssueRow: Equatable, Sendable {
    public let taskID: String
    public let subject: String
    public let status: String
    public let parallelMode: String
    public let riskLevel: String
    public let dependencyCount: Int
    public let bidCount: Int
    public let leaseState: TaskMarketLeaseState
    public let worktreeLabel: String?
    public let ownerLabel: String?
    public let requiresHumanApproval: Bool
    public let acceptanceCriteriaCount: Int

    /// `true` when the task has at least one dependency blocking it.
    public var isBlocked: Bool { dependencyCount > 0 }

    /// `true` when the issue risk level is considered high or critical.
    public var riskLevelIsHighOrCritical: Bool {
        let lowercased = riskLevel.lowercased()
        return lowercased == "high" || lowercased == "critical"
    }

    public init(issue: IssueDTO, task: TaskDTO) {
        self.taskID = task.id
        self.subject = task.subject
        self.status = task.status
        self.parallelMode = issue.parallelMode
        self.riskLevel = issue.riskLevel
        self.dependencyCount = task.blockedBy.count
        // The current snapshot does not expose a bid list, so the market table
        // shows zero bids until the backend adds bid/lease fields.
        self.bidCount = 0
        self.leaseState = task.owner == nil ? .open : .claimed
        self.worktreeLabel = TaskMarketIssueRow.deriveWorktreeLabel(from: issue)
        self.ownerLabel = task.owner
        self.requiresHumanApproval = issue.requiresHumanApproval
        self.acceptanceCriteriaCount = issue.acceptanceCriteria.count
    }

    private static func deriveWorktreeLabel(from issue: IssueDTO) -> String? {
        if !issue.relatedWorktree.isEmpty {
            return issue.relatedWorktree
        }
        if !issue.relatedBranch.isEmpty {
            return issue.relatedBranch
        }
        return nil
    }
}
