import Foundation

/// Dense visual presentation for the Task Market reference screen.
/// It uses live snapshot rows first, then deterministic fixture rows to keep the
/// Mac preview visually complete before every backend surface is implemented.
public struct TaskMarketDesignPresentation: Equatable, Sendable {
    public let filters: TaskMarketDesignFilters
    public let rows: [TaskMarketDesignIssue]
    public let bids: [TaskMarketDesignBid]
    public let activeLeases: [TaskMarketDesignLease]

    public var selectedIssue: TaskMarketDesignIssue? { rows.first }

    public init(snapshot: WorkbenchSnapshotDTO?, refreshedLeases: [LeaseDTO] = []) {
        let liveRows = snapshot.map { TaskMarketSnapshotPresentation(snapshot: $0).rows } ?? []
        let mappedLiveRows = liveRows.enumerated().map { index, row in
            TaskMarketDesignIssue(
                number: index + 1,
                taskID: row.taskID,
                title: row.subject,
                detail: Self.detail(for: row.subject),
                parallelMode: row.parallelMode,
                risk: Self.normalizedRisk(row.riskLevel),
                dependency: row.dependencyCount > 0 ? "Blocked by #1" : "-",
                bids: max(row.bidCount, index == 0 ? 3 : 1),
                lease: row.leaseState == .claimed ? "42m remaining" : "Requires proposal",
                worktree: row.worktreeLabel ?? "-",
                status: Self.status(for: row),
                tag: Self.tag(for: row.subject)
            )
        }

        var filledRows = mappedLiveRows
        let fixtureRows = mappedLiveRows.isEmpty ? Self.fixtureRows : Array(Self.fixtureRows.dropFirst())
        for fixture in fixtureRows where filledRows.count < 8 {
            if !filledRows.contains(where: { $0.title == fixture.title }) {
                filledRows.append(fixture.withNumber(filledRows.count + 1))
            }
        }

        rows = Array(filledRows.prefix(8)).enumerated().map { index, row in
            row.withNumber(index + 1)
        }
        filters = TaskMarketDesignFilters.reference
        bids = Self.fixtureBids
        let tasksByID = Dictionary(uniqueKeysWithValues: snapshot?.tasks.map { ($0.id, $0) } ?? [])
        if refreshedLeases.isEmpty {
            let liveLeases = Self.activeLeases(from: snapshot?.leases ?? [], tasksByID: tasksByID)
            var filledLeases = liveLeases
            for fixture in Self.fixtureLeases where filledLeases.count < 4 {
                if !filledLeases.contains(where: { $0.leaseID == fixture.leaseID || $0.worktree == fixture.worktree }) {
                    filledLeases.append(fixture)
                }
            }
            activeLeases = Array(filledLeases.prefix(4))
        } else {
            activeLeases = Array(Self.activeLeases(from: refreshedLeases, tasksByID: tasksByID).prefix(4))
        }
    }

    private static func normalizedRisk(_ risk: String) -> String {
        switch risk.lowercased() {
        case "critical":
            return "Critical"
        case "high":
            return "High"
        case "medium":
            return "Medium"
        case "low":
            return "Low"
        default:
            return risk
        }
    }

    private static func status(for row: TaskMarketIssueRow) -> String {
        if row.dependencyCount > 0 {
            return "Blocked"
        }
        switch row.leaseState {
        case .claimed:
            return "Leased"
        case .open:
            return "Requires proposal"
        }
    }

    private static func detail(for subject: String) -> String {
        if subject.contains("API") {
            return "Create persistent snapshot endpoints for workbench state."
        }
        return "Implement task lease system with heartbeat and expiry."
    }

    private static func tag(for subject: String) -> String {
        if subject.contains("API") {
            return "backend"
        }
        return "core"
    }

    private static func activeLeases(from leases: [LeaseDTO], tasksByID: [String: TaskDTO]) -> [TaskMarketDesignLease] {
        return leases
            .filter { $0.state.lowercased() == "active" }
            .enumerated()
            .map { index, lease in
                let task = tasksByID[lease.taskID]
                return TaskMarketDesignLease(
                    leaseID: lease.id,
                    number: index + 1,
                    title: task?.subject ?? lease.taskID,
                    worktree: lease.worktreeName.isEmpty ? "-" : lease.worktreeName,
                    owner: lease.agentID,
                    status: "Active",
                    time: lease.expiresAt,
                    tone: "green"
                )
            }
    }

    private static let fixtureRows: [TaskMarketDesignIssue] = [
        TaskMarketDesignIssue(
            number: 1,
            taskID: "design-lease",
            title: "Task Market Lease",
            detail: "Implement task lease system w/ heartbeat & expiry.",
            parallelMode: "exclusive",
            risk: "High",
            dependency: "-",
            bids: 3,
            lease: "Requires proposal",
            worktree: "-",
            status: "Requires proposal",
            tag: "apis"
        ),
        TaskMarketDesignIssue(
            number: 2,
            taskID: "design-snapshot",
            title: "Workbench Snapshot API",
            detail: "Create persistent snapshot endpoints for workbench state.",
            parallelMode: "exclusive",
            risk: "Critical",
            dependency: "Blocked by #1",
            bids: 2,
            lease: "-",
            worktree: "-",
            status: "Blocked",
            tag: "backend"
        ),
        TaskMarketDesignIssue(
            number: 3,
            taskID: "design-failure-cards",
            title: "Validation Failure Cards",
            detail: "Render failure cards with logs, tests, and repro steps.",
            parallelMode: "competitive",
            risk: "High",
            dependency: "-",
            bids: 4,
            lease: "42m remaining",
            worktree: "issue-3-failure-cards",
            status: "Leased",
            tag: "ui"
        ),
        TaskMarketDesignIssue(
            number: 4,
            taskID: "design-terminal",
            title: "Terminal UI Protocol",
            detail: "Define protocol for agent terminal UI interactions.",
            parallelMode: "exploratory",
            risk: "Medium",
            dependency: "-",
            bids: 1,
            lease: "Requires proposal",
            worktree: "-",
            status: "Requires proposal",
            tag: "protocol"
        ),
        TaskMarketDesignIssue(
            number: 5,
            taskID: "design-intent-lock",
            title: "Intent Lock Policy",
            detail: "Specify and enforce intent locks to prevent overlap.",
            parallelMode: "exclusive",
            risk: "High",
            dependency: "-",
            bids: 3,
            lease: "-",
            worktree: "-",
            status: "Open",
            tag: "core"
        ),
        TaskMarketDesignIssue(
            number: 6,
            taskID: "design-context-health",
            title: "Context Health Indicators",
            detail: "Show context health in canvas and lists.",
            parallelMode: "competitive",
            risk: "Medium",
            dependency: "-",
            bids: 2,
            lease: "15m remaining",
            worktree: "issue-6-context-health",
            status: "Leased",
            tag: "ui"
        ),
        TaskMarketDesignIssue(
            number: 7,
            taskID: "design-approval",
            title: "Approval Workflow",
            detail: "Add approval gates and audit events.",
            parallelMode: "exclusive",
            risk: "High",
            dependency: "Blocked by #5",
            bids: 2,
            lease: "-",
            worktree: "-",
            status: "Blocked",
            tag: "workflow"
        ),
        TaskMarketDesignIssue(
            number: 8,
            taskID: "design-capabilities",
            title: "Agent Capabilities Registry",
            detail: "Registry of agent skills and limits.",
            parallelMode: "exploratory",
            risk: "Low",
            dependency: "-",
            bids: 1,
            lease: "Requires proposal",
            worktree: "-",
            status: "Requires proposal",
            tag: "core"
        )
    ]

    private static let fixtureBids: [TaskMarketDesignBid] = [
        TaskMarketDesignBid(agent: "Backend-Agent", confidence: "0.82", estimate: "6 files", eta: "2h 40m", note: "Medium complexity. Needs robust concurrency tests.", isLatest: true),
        TaskMarketDesignBid(agent: "Reviewer-Agent", confidence: "0.61", estimate: "4 files", eta: "3h 15m", note: "Lower risk, but limited implementation experience.", isLatest: false),
        TaskMarketDesignBid(agent: "Test-Agent", confidence: "0.58", estimate: "3 files", eta: "3h 45m", note: "Can implement tests first, then iterate.", isLatest: false)
    ]

    private static let fixtureLeases: [TaskMarketDesignLease] = [
        TaskMarketDesignLease(leaseID: "fixture-lease-3", number: 3, title: "Validation Failure Cards", worktree: "issue-3-failure-cards", owner: "Test-Agent", status: "Active", time: "42m remaining", tone: "green"),
        TaskMarketDesignLease(leaseID: "fixture-lease-6", number: 6, title: "Context Health Indicators", worktree: "issue-6-context-health", owner: "Backend-Agent", status: "Active", time: "15m remaining", tone: "green"),
        TaskMarketDesignLease(leaseID: "fixture-lease-9", number: 9, title: "Agent Memory Store", worktree: "issue-9-memory-store", owner: "Backend-Agent", status: "Expiring Soon", time: "3m remaining", tone: "orange"),
        TaskMarketDesignLease(leaseID: "fixture-lease-10", number: 10, title: "Audit Export", worktree: "issue-10-audit-export", owner: "Reviewer-Agent", status: "Expired", time: "-2m overdue", tone: "red")
    ]
}

public struct TaskMarketDesignFilters: Equatable, Sendable {
    public let riskLevels: [TaskMarketDesignFilter]
    public let parallelModes: [TaskMarketDesignFilter]
    public let dependencyStates: [TaskMarketDesignFilter]
    public let contextHealth: [TaskMarketDesignFilter]

    public static let reference = TaskMarketDesignFilters(
        riskLevels: [
            TaskMarketDesignFilter(label: "Critical", count: 2, tone: "red"),
            TaskMarketDesignFilter(label: "High", count: 4, tone: "orange"),
            TaskMarketDesignFilter(label: "Medium", count: 7, tone: "yellow"),
            TaskMarketDesignFilter(label: "Low", count: 3, tone: "green")
        ],
        parallelModes: [
            TaskMarketDesignFilter(label: "exclusive", count: 5, tone: "blue"),
            TaskMarketDesignFilter(label: "competitive", count: 6, tone: "green"),
            TaskMarketDesignFilter(label: "exploratory", count: 4, tone: "purple")
        ],
        dependencyStates: [
            TaskMarketDesignFilter(label: "All", count: 15, tone: "gray"),
            TaskMarketDesignFilter(label: "No Dependencies", count: 6, tone: "green"),
            TaskMarketDesignFilter(label: "Unblocked", count: 6, tone: "green"),
            TaskMarketDesignFilter(label: "Blocked", count: 2, tone: "red"),
            TaskMarketDesignFilter(label: "Blocking Others", count: 1, tone: "orange")
        ],
        contextHealth: [
            TaskMarketDesignFilter(label: "All", count: 15, tone: "gray"),
            TaskMarketDesignFilter(label: "Good", count: 9, tone: "green"),
            TaskMarketDesignFilter(label: "Stale", count: 4, tone: "yellow"),
            TaskMarketDesignFilter(label: "Conflicted", count: 2, tone: "red")
        ]
    )
}

public struct TaskMarketDesignFilter: Equatable, Sendable {
    public let label: String
    public let count: Int
    public let tone: String
}

public struct TaskMarketDesignIssue: Equatable, Sendable, Identifiable {
    public var id: String { taskID }
    public let number: Int
    public let taskID: String
    public let title: String
    public let detail: String
    public let parallelMode: String
    public let risk: String
    public let dependency: String
    public let bids: Int
    public let lease: String
    public let worktree: String
    public let status: String
    public let tag: String

    public var canClaim: Bool {
        !isBlocked && !hasActiveLease
    }

    public var defaultClaimWorktreeName: String {
        let sanitized = taskID
            .lowercased()
            .map { character in
                character.isLetter || character.isNumber ? character : "-"
            }
        let collapsed = String(sanitized).split(separator: "-").joined(separator: "-")
        return "wt-\(collapsed)"
    }

    private var isBlocked: Bool {
        dependency != "-"
    }

    private var hasActiveLease: Bool {
        status.lowercased() == "leased" || lease.lowercased().contains("remaining")
    }

    public func claimDisabledReason(locale: AppLocale) -> String? {
        guard !canClaim else { return nil }
        if isBlocked {
            return locale == .zhCN
                ? "存在未完成依赖，暂不能认领"
                : "Unresolved dependencies block this claim"
        }
        if hasActiveLease {
            return locale == .zhCN
                ? "已有活跃租约，需先释放或转派"
                : "An active lease must be released or reassigned first"
        }
        return locale == .zhCN ? "当前状态不可认领" : "Current state cannot be claimed"
    }

    fileprivate func withNumber(_ number: Int) -> TaskMarketDesignIssue {
        TaskMarketDesignIssue(
            number: number,
            taskID: taskID,
            title: title,
            detail: detail,
            parallelMode: parallelMode,
            risk: risk,
            dependency: dependency,
            bids: bids,
            lease: lease,
            worktree: worktree,
            status: status,
            tag: tag
        )
    }
}

public struct TaskMarketDesignBid: Equatable, Sendable, Identifiable {
    public var id: String { agent }
    public let agent: String
    public let confidence: String
    public let estimate: String
    public let eta: String
    public let note: String
    public let isLatest: Bool
}

public struct TaskMarketDesignLease: Equatable, Sendable, Identifiable {
    public var id: String { leaseID }
    public let leaseID: String
    public let number: Int
    public let title: String
    public let worktree: String
    public let owner: String
    public let status: String
    public let time: String
    public let tone: String
}
