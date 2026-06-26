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
        #expect(row.worktreeLabel == nil)
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
