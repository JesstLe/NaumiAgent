import Foundation

/// Dense visual presentation for the Reviews reference screen.
public struct ReviewsDesignPresentation: Equatable, Sendable {
    public let reviewQueues: [ReviewDesignQueue]
    public let selectedReview: ReviewDesignItem
    public let validationChecks: [ReviewDesignCheck]
    public let fileChanges: [ReviewDesignFile]
    public let diffRows: [ReviewDesignDiffRow]
    public let timeline: [ReviewDesignTimelineEvent]
    public let agentReviews: [ReviewDesignAgentNote]

    public init(
        approvals: [ApprovalDTO],
        validationRuns: [ValidationRunDTO],
        snapshot: WorkbenchSnapshotDTO?
    ) {
        let waitingItems = approvals.prefix(2).enumerated().map { index, approval in
            ReviewDesignItem(
                id: approval.id,
                taskID: approval.taskID,
                title: approval.title,
                number: index == 0 ? 3 : 7,
                agent: approval.requester,
                worktree: "issue-\(index == 0 ? "3-market" : "7-failure-cards")",
                time: "09:\(28 + index)",
                risk: "High",
                tone: "red"
            )
        }

        let waiting = waitingItems.isEmpty ? Self.fixtureWaiting : Array(waitingItems)
        reviewQueues = [
            ReviewDesignQueue(title: "WAITING APPROVAL", badge: 2, items: waiting),
            ReviewDesignQueue(title: "REQUEST CHANGES", badge: 1, items: Self.fixtureRequestChanges),
            ReviewDesignQueue(title: "AUTO-MERGE CANDIDATE", badge: 1, items: Self.fixtureAutoMerge),
            ReviewDesignQueue(title: "HIGH RISK", badge: 1, items: Self.fixtureHighRisk)
        ]
        selectedReview = waiting.first ?? Self.fixtureWaiting[0]

        let liveChecks = validationRuns.prefix(2).map {
            ReviewDesignCheck(
                name: $0.command.joined(separator: " "),
                status: $0.status,
                time: String($0.completedAt.suffix(5))
            )
        }
        validationChecks = Array((liveChecks + [
            ReviewDesignCheck(name: "ruff (lint)", status: "passed", time: "09:28"),
            ReviewDesignCheck(name: "pytest tests/unit/test_workbench_market.py -q", status: "passed", time: "09:29"),
            ReviewDesignCheck(name: "frontend protocol", status: "not affected", time: "-")
        ]).prefix(3))

        fileChanges = Self.fixtureFiles
        diffRows = Self.fixtureDiffRows
        timeline = Self.fixtureTimeline
        agentReviews = Self.fixtureAgentReviews
    }

    public func defaultValidationDraft(for review: ReviewDesignItem) -> ValidationRunDraft {
        ValidationRunDraft(
            taskID: review.taskID,
            actor: review.agent,
            commandLine: "pytest tests/unit/test_workbench_market.py -q"
        )
    }

    private static let fixtureWaiting = [
        ReviewDesignItem(id: "wait-1", taskID: "design-lease", title: "Task Market Lease", number: 3, agent: "Backend-Agent", worktree: "issue-3-market", time: "09:28", risk: "High", tone: "red"),
        ReviewDesignItem(id: "wait-2", taskID: "design-failure-cards", title: "Validation Failure Cards", number: 7, agent: "Backend-Agent", worktree: "issue-7-failure-cards", time: "09:21", risk: "High", tone: "red")
    ]

    private static let fixtureRequestChanges = [
        ReviewDesignItem(id: "change-1", taskID: "design-intent-lock", title: "Intent Lock Policy", number: 4, agent: "Backend-Agent", worktree: "issue-4-lock-policy", time: "Yesterday", risk: "Medium", tone: "orange")
    ]

    private static let fixtureAutoMerge = [
        ReviewDesignItem(id: "auto-1", taskID: "design-context-health", title: "Telemetry Context Health", number: 6, agent: "Backend-Agent", worktree: "issue-6-telemetry", time: "May 21", risk: "Low", tone: "green")
    ]

    private static let fixtureHighRisk = [
        ReviewDesignItem(id: "risk-1", taskID: "design-terminal", title: "Terminal UI Protocol", number: 5, agent: "Backend-Agent", worktree: "issue-5-terminal-ui", time: "May 20", risk: "High", tone: "red")
    ]

    private static let fixtureFiles = [
        ReviewDesignFile(path: "src/naumi_agent/workbench", name: "market.py", status: "M", selected: true),
        ReviewDesignFile(path: "src/naumi_agent/workbench", name: "models.py", status: "M", selected: false),
        ReviewDesignFile(path: "tests/unit", name: "test_workbench_market.py", status: "A", selected: false),
        ReviewDesignFile(path: "docs", name: "market_lease.md", status: "M", selected: false),
        ReviewDesignFile(path: "adr/0009-market-lease.md", name: "adr/0009-market-lease.md", status: "A", selected: false),
        ReviewDesignFile(path: "configs", name: "market.yaml", status: "M", selected: false)
    ]

    private static let fixtureDiffRows = [
        ReviewDesignDiffRow(number: 412, old: "def claim(self, issue_id: str, agent_id: str) -> Lease:", new: "def claim(self, issue_id: str, agent_id: str) -> Lease:", tone: "normal"),
        ReviewDesignDiffRow(number: 413, old: "    now = datetime.utcnow()", new: "    now = datetime.utcnow()", tone: "normal"),
        ReviewDesignDiffRow(number: 414, old: "    if self.is_leased(issue_id):", new: "    if self.is_leased(issue_id):", tone: "normal"),
        ReviewDesignDiffRow(number: 415, old: "        raise LeaseError(\"already leased\")", new: "        raise LeaseError(\"already leased\")", tone: "normal"),
        ReviewDesignDiffRow(number: 416, old: "    lease = Lease(", new: "    lease_ttl = self._config.market.lease_ttl_minutes", tone: "added"),
        ReviewDesignDiffRow(number: 417, old: "        issue_id=issue_id,", new: "    heartbeat = self._config.market.heartbeat_seconds", tone: "added"),
        ReviewDesignDiffRow(number: 420, old: "        lease_until=now + timedelta(minutes=30),", new: "        lease_until=now + timedelta(minutes=lease_ttl),", tone: "changed"),
        ReviewDesignDiffRow(number: 421, old: "        heartbeat_seconds=30,", new: "        heartbeat_seconds=heartbeat,", tone: "changed")
    ]

    private static let fixtureTimeline = [
        ReviewDesignTimelineEvent(time: "09:16:03", event: "issue.claimed", actor: "Backend-Agent", detail: "Issue #3 claimed by Backend-Agent", tone: "blue"),
        ReviewDesignTimelineEvent(time: "09:18:11", event: "worktree.created", actor: "Backend-Agent", detail: "Worktree created: issue-3-market", tone: "green"),
        ReviewDesignTimelineEvent(time: "09:28:02", event: "validation.passed", actor: "Backend-Agent", detail: "ruff passed", tone: "green"),
        ReviewDesignTimelineEvent(time: "09:29:13", event: "validation.passed", actor: "Backend-Agent", detail: "pytest tests/unit/test_workbench_market.py passed", tone: "green"),
        ReviewDesignTimelineEvent(time: "09:29:15", event: "validation.info", actor: "Backend-Agent", detail: "frontend protocol not affected", tone: "blue"),
        ReviewDesignTimelineEvent(time: "09:30:47", event: "reviewer.comment", actor: "Reviewer-Agent", detail: "Request concurrency test evidence", tone: "purple"),
        ReviewDesignTimelineEvent(time: "09:36:02", event: "approval.requested", actor: "Backend-Agent", detail: "High risk approval requested", tone: "orange")
    ]

    private static let fixtureAgentReviews = [
        ReviewDesignAgentNote(agent: "Backend-Agent", initials: "BA", time: "09:27", body: "Implements configurable lease TTL and heartbeat. Added event emission for audit visibility."),
        ReviewDesignAgentNote(agent: "Reviewer-Agent", initials: "RA", time: "09:31", body: "Overall looks good. Please provide evidence for concurrency behavior under load.")
    ]
}

public struct ReviewDesignQueue: Equatable, Sendable, Identifiable {
    public var id: String { title }
    public let title: String
    public let badge: Int
    public let items: [ReviewDesignItem]
}

public struct ReviewDesignItem: Equatable, Sendable, Identifiable {
    public let id: String
    public let taskID: String
    public let title: String
    public let number: Int
    public let agent: String
    public let worktree: String
    public let time: String
    public let risk: String
    public let tone: String
}

public struct ReviewDesignCheck: Equatable, Sendable, Identifiable {
    public var id: String { name }
    public let name: String
    public let status: String
    public let time: String
}

public struct ReviewDesignFile: Equatable, Sendable, Identifiable {
    public var id: String { "\(path)/\(name)" }
    public let path: String
    public let name: String
    public let status: String
    public let selected: Bool
}

public struct ReviewDesignDiffRow: Equatable, Sendable, Identifiable {
    public var id: Int { number }
    public let number: Int
    public let old: String
    public let new: String
    public let tone: String
}

public struct ReviewDesignTimelineEvent: Equatable, Sendable, Identifiable {
    public var id: String { "\(time)-\(event)" }
    public let time: String
    public let event: String
    public let actor: String
    public let detail: String
    public let tone: String
}

public struct ReviewDesignAgentNote: Equatable, Sendable, Identifiable {
    public var id: String { agent }
    public let agent: String
    public let initials: String
    public let time: String
    public let body: String
}
