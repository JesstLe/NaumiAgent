import Foundation

/// Pure presentation model derived from ``WorkbenchSnapshotDTO``.
/// Keeps SwiftUI-agnostic logic testable without View infrastructure.
public struct DashboardSnapshotPresentation: Equatable, Sendable {
    public let currentMission: DashboardMissionSummary?
    public let taskRows: [DashboardTaskRow]
    public let issueRows: [DashboardIssueRow]
    public let failureRows: [DashboardFailureRow]
    public let recentEventRows: [DashboardEventRow]

    public init(snapshot: WorkbenchSnapshotDTO) {
        self.currentMission = snapshot.missions.first.map {
            DashboardMissionSummary(id: $0.id, title: $0.title, status: $0.status)
        }

        let issueByTaskID = Dictionary(
            grouping: snapshot.issues,
            by: \.taskID
        )

        // Keep the first screen dense but scannable.
        self.taskRows = Array(snapshot.tasks.prefix(5)).map { task in
            let issue = issueByTaskID[task.id]?.first
            return DashboardTaskRow(
                id: task.id,
                subject: task.subject,
                status: task.status,
                owner: task.owner,
                activeForm: task.activeForm,
                riskLevel: issue?.riskLevel,
                parallelMode: issue?.parallelMode,
                acceptanceCriteriaCount: issue?.acceptanceCriteria.count
            )
        }

        self.issueRows = snapshot.issues.map { issue in
            DashboardIssueRow(
                taskID: issue.taskID,
                missionID: issue.missionID,
                riskLevel: issue.riskLevel,
                parallelMode: issue.parallelMode,
                requiresHumanApproval: issue.requiresHumanApproval
            )
        }

        self.failureRows = Array(snapshot.failures.prefix(5)).map { failure in
            DashboardFailureRow(
                id: failure.id,
                title: failure.title,
                kind: failure.kind,
                status: failure.status,
                taskID: failure.taskID
            )
        }

        // Events are assumed chronological; take the trailing window.
        self.recentEventRows = Array(snapshot.events.suffix(5)).map { event in
            DashboardEventRow(
                id: event.id,
                type: event.type,
                actor: event.actor,
                subjectID: event.subjectID,
                timestamp: event.timestamp
            )
        }
    }
}

public struct DashboardMissionSummary: Equatable, Sendable {
    public let id: String
    public let title: String
    public let status: String
}

public struct DashboardTaskRow: Equatable, Sendable {
    public let id: String
    public let subject: String
    public let status: String
    public let owner: String?
    public let activeForm: String?
    public let riskLevel: String?
    public let parallelMode: String?
    public let acceptanceCriteriaCount: Int?
}

public struct DashboardIssueRow: Equatable, Sendable {
    public let taskID: String
    public let missionID: String
    public let riskLevel: String
    public let parallelMode: String
    public let requiresHumanApproval: Bool
}

public struct DashboardFailureRow: Equatable, Sendable {
    public let id: String
    public let title: String
    public let kind: String
    public let status: String
    public let taskID: String
}

public struct DashboardEventRow: Equatable, Sendable {
    public let id: String
    public let type: String
    public let actor: String
    public let subjectID: String
    public let timestamp: String
}
