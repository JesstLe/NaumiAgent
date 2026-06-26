import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketSnapshotPresentationTests {

    @Test func zhSnapshotSummaryAndRow() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.summary.totalIssues == 1)
        #expect(presentation.summary.openIssues == 0)
        #expect(presentation.summary.claimedIssues == 1)
        #expect(presentation.summary.blockedIssues == 0)
        #expect(presentation.summary.approvalRequiredIssues == 1)

        #expect(presentation.rows.count == 1)

        let row = try #require(presentation.rows.first)
        #expect(row.taskID == "2")
        #expect(row.subject == "实现 API Client")
        #expect(row.status == "in_progress")
        #expect(row.parallelMode == "exclusive")
        #expect(row.riskLevel == "medium")
        #expect(row.dependencyCount == 0)
        #expect(row.bidCount == 0)
        #expect(row.leaseState == .claimed)
        #expect(row.leaseID == "lzh-001")
        #expect(row.leaseAgentID == "agent-a")
        #expect(row.leaseExpiresAt == "2026-06-27T07:00:00")
        #expect(row.worktreeLabel == "wt-api-client")
        #expect(row.ownerLabel == "agent-a")
        #expect(row.requiresHumanApproval == true)
        #expect(row.acceptanceCriteriaCount == 2)
    }

    @Test func claimedAndOpenLeaseStates() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-lease",
            missions: [],
            tasks: [
                TaskDTO(
                    id: "claimed-task",
                    sessionID: "sess-lease",
                    subject: "Claimed Task",
                    description: "",
                    status: "in_progress",
                    activeForm: nil,
                    owner: "agent-a",
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                ),
                TaskDTO(
                    id: "open-task",
                    sessionID: "sess-lease",
                    subject: "Open Task",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            issues: [
                IssueDTO(
                    sessionID: "sess-lease",
                    taskID: "claimed-task",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                ),
                IssueDTO(
                    sessionID: "sess-lease",
                    taskID: "open-task",
                    missionID: "m-1",
                    parallelMode: "parallel",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.summary.totalIssues == 2)
        #expect(presentation.summary.openIssues == 1)
        #expect(presentation.summary.claimedIssues == 1)

        let claimedRow = try #require(presentation.rows.first { $0.taskID == "claimed-task" })
        #expect(claimedRow.leaseState == .claimed)

        let openRow = try #require(presentation.rows.first { $0.taskID == "open-task" })
        #expect(openRow.leaseState == .open)
    }

    @Test func blockedTasksAreSortedFirst() {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-sort",
            missions: [],
            tasks: [
                TaskDTO(
                    id: "unblocked",
                    sessionID: "sess-sort",
                    subject: "Unblocked",
                    description: "",
                    status: "in_progress",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                ),
                TaskDTO(
                    id: "blocked",
                    sessionID: "sess-sort",
                    subject: "Blocked",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: ["dep-1"],
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            issues: [
                IssueDTO(
                    sessionID: "sess-sort",
                    taskID: "unblocked",
                    missionID: "m-1",
                    parallelMode: "parallel",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                ),
                IssueDTO(
                    sessionID: "sess-sort",
                    taskID: "blocked",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.rows.map(\.taskID) == ["blocked", "unblocked"])
        #expect(presentation.summary.blockedIssues == 1)
    }

    @Test func highRiskAndInProgressSorting() {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-risk",
            missions: [],
            tasks: [
                TaskDTO(
                    id: "pending-high",
                    sessionID: "sess-risk",
                    subject: "Pending High",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                ),
                TaskDTO(
                    id: "in-progress-low",
                    sessionID: "sess-risk",
                    subject: "In Progress Low",
                    description: "",
                    status: "in_progress",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                ),
                TaskDTO(
                    id: "pending-critical",
                    sessionID: "sess-risk",
                    subject: "Pending Critical",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            issues: [
                IssueDTO(
                    sessionID: "sess-risk",
                    taskID: "pending-high",
                    missionID: "m-1",
                    parallelMode: "parallel",
                    riskLevel: "high",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                ),
                IssueDTO(
                    sessionID: "sess-risk",
                    taskID: "in-progress-low",
                    missionID: "m-1",
                    parallelMode: "parallel",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                ),
                IssueDTO(
                    sessionID: "sess-risk",
                    taskID: "pending-critical",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "critical",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)

        // Blocked sorting is not in play here. High/critical risk comes before
        // in_progress, and critical comes before high due to original order
        // after the severe flag groups them together.
        #expect(presentation.rows.map(\.taskID) == ["pending-high", "pending-critical", "in-progress-low"])
    }

    @Test func worktreeLabelPrefersWorktreeThenBranch() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-wt",
            missions: [],
            tasks: [
                TaskDTO(
                    id: "wt-task",
                    sessionID: "sess-wt",
                    subject: "With Worktree",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                ),
                TaskDTO(
                    id: "branch-task",
                    sessionID: "sess-wt",
                    subject: "With Branch",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            issues: [
                IssueDTO(
                    sessionID: "sess-wt",
                    taskID: "wt-task",
                    missionID: "m-1",
                    parallelMode: "parallel",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "branch-only",
                    relatedWorktree: "worktree-path",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                ),
                IssueDTO(
                    sessionID: "sess-wt",
                    taskID: "branch-task",
                    missionID: "m-1",
                    parallelMode: "parallel",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "fallback-branch",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)

        let worktreeRow = try #require(presentation.rows.first { $0.taskID == "wt-task" })
        #expect(worktreeRow.worktreeLabel == "worktree-path")

        let branchRow = try #require(presentation.rows.first { $0.taskID == "branch-task" })
        #expect(branchRow.worktreeLabel == "fallback-branch")
    }

    @Test func issuesWithoutMatchingTaskAreDropped() {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-orphan",
            missions: [],
            tasks: [],
            issues: [
                IssueDTO(
                    sessionID: "sess-orphan",
                    taskID: "missing-task",
                    missionID: "m-1",
                    parallelMode: "parallel",
                    riskLevel: "high",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.rows.isEmpty)
        // Total issues reflects raw snapshot count, not matched rows.
        #expect(presentation.summary.totalIssues == 1)
        #expect(presentation.summary.openIssues == 0)
    }

    @Test func activeLeaseOverridesTaskOwner() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-override",
            missions: [],
            tasks: [
                TaskDTO(
                    id: "owned-task",
                    sessionID: "sess-override",
                    subject: "Owned Task",
                    description: "",
                    status: "in_progress",
                    activeForm: nil,
                    owner: "task-owner",
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            issues: [
                IssueDTO(
                    sessionID: "sess-override",
                    taskID: "owned-task",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            leases: [
                LeaseDTO(
                    id: "lease-override",
                    sessionID: "sess-override",
                    taskID: "owned-task",
                    agentID: "lease-agent",
                    state: "active",
                    expiresAt: "2099-01-01T00:00:00",
                    worktreeName: "wt-lease",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)
        let row = try #require(presentation.rows.first)

        #expect(row.leaseState == .claimed)
        #expect(row.ownerLabel == "lease-agent")
        #expect(row.leaseAgentID == "lease-agent")
        #expect(row.worktreeLabel == "wt-lease")
    }

    @Test func releasedLeaseIsIgnored() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-released",
            missions: [],
            tasks: [
                TaskDTO(
                    id: "released-task",
                    sessionID: "sess-released",
                    subject: "Released Task",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            issues: [
                IssueDTO(
                    sessionID: "sess-released",
                    taskID: "released-task",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            leases: [
                LeaseDTO(
                    id: "lease-released",
                    sessionID: "sess-released",
                    taskID: "released-task",
                    agentID: "former-agent",
                    state: "released",
                    expiresAt: "2099-01-01T00:00:00",
                    worktreeName: "wt-released",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)
        let row = try #require(presentation.rows.first)

        #expect(row.leaseState == .open)
        #expect(row.leaseAgentID == nil)
        #expect(row.ownerLabel == nil)
        #expect(row.worktreeLabel == nil)
    }

    @Test func worktreeFallbackPrefersLeaseThenIssueWorktreeThenBranch() throws {
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-wt-fallback",
            missions: [],
            tasks: [
                TaskDTO(
                    id: "lease-wt",
                    sessionID: "sess-wt-fallback",
                    subject: "Lease Worktree",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                ),
                TaskDTO(
                    id: "issue-wt",
                    sessionID: "sess-wt-fallback",
                    subject: "Issue Worktree",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                ),
                TaskDTO(
                    id: "issue-branch",
                    sessionID: "sess-wt-fallback",
                    subject: "Issue Branch",
                    description: "",
                    status: "pending",
                    activeForm: nil,
                    owner: nil,
                    blocks: [],
                    blockedBy: [],
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            issues: [
                IssueDTO(
                    sessionID: "sess-wt-fallback",
                    taskID: "lease-wt",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "branch-a",
                    relatedWorktree: "worktree-a",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                ),
                IssueDTO(
                    sessionID: "sess-wt-fallback",
                    taskID: "issue-wt",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "branch-b",
                    relatedWorktree: "worktree-b",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                ),
                IssueDTO(
                    sessionID: "sess-wt-fallback",
                    taskID: "issue-branch",
                    missionID: "m-1",
                    parallelMode: "exclusive",
                    riskLevel: "low",
                    requiresHumanApproval: false,
                    acceptanceCriteria: [],
                    expectedArtifacts: [],
                    relatedBranch: "branch-c",
                    relatedWorktree: "",
                    relatedPR: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            leases: [
                LeaseDTO(
                    id: "lease-wt-1",
                    sessionID: "sess-wt-fallback",
                    taskID: "lease-wt",
                    agentID: "agent-a",
                    state: "active",
                    expiresAt: "2099-01-01T00:00:00",
                    worktreeName: "lease-worktree",
                    createdAt: "",
                    updatedAt: ""
                ),
                LeaseDTO(
                    id: "lease-wt-2",
                    sessionID: "sess-wt-fallback",
                    taskID: "issue-wt",
                    agentID: "agent-b",
                    state: "active",
                    expiresAt: "2099-01-01T00:00:00",
                    worktreeName: "",
                    createdAt: "",
                    updatedAt: ""
                )
            ],
            failures: [],
            events: []
        )

        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)

        let leaseWT = try #require(presentation.rows.first { $0.taskID == "lease-wt" })
        #expect(leaseWT.worktreeLabel == "lease-worktree")

        let issueWT = try #require(presentation.rows.first { $0.taskID == "issue-wt" })
        #expect(issueWT.worktreeLabel == "worktree-b")

        let issueBranch = try #require(presentation.rows.first { $0.taskID == "issue-branch" })
        #expect(issueBranch.worktreeLabel == "branch-c")
    }

    // MARK: - Helpers

    private func loadZHSnapshot() throws -> WorkbenchSnapshotDTO {
        let data = try loadFixture(named: "workbench_snapshot_zh")
        return try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
    }

    private func loadFixture(named: String) throws -> Data {
        let fixturesURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/\(named).json")
        return try Data(contentsOf: fixturesURL)
    }
}
