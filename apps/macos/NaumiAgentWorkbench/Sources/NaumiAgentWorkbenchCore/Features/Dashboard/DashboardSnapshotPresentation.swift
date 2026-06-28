import Foundation

/// Pure presentation model derived from ``WorkbenchSnapshotDTO``.
/// Keeps SwiftUI-agnostic logic testable without View infrastructure.
public struct DashboardSnapshotPresentation: Equatable, Sendable {
    public let currentMission: DashboardMissionSummary?
    public let workbench: DashboardWorkbenchPresentation
    public let agentRows: [DashboardAgentRow]
    public let taskRows: [DashboardTaskRow]
    public let issueRows: [DashboardIssueRow]
    public let failureRows: [DashboardFailureRow]
    public let recentEventRows: [DashboardEventRow]

    public init(snapshot: WorkbenchSnapshotDTO) {
        let currentMission = snapshot.missions.first.map {
            DashboardMissionSummary(id: $0.id, title: $0.title, status: $0.status)
        }
        self.currentMission = currentMission

        self.agentRows = Array(snapshot.agentProfiles.prefix(5)).map { profile in
            DashboardAgentRow(
                id: profile.id,
                name: profile.name,
                role: profile.role,
                status: profile.status,
                capabilityCount: profile.capabilities.count,
                maxParallelTasks: profile.maxParallelTasks
            )
        }

        let issueByTaskID = Dictionary(grouping: snapshot.issues, by: \.taskID)
        let taskByID = Dictionary(uniqueKeysWithValues: snapshot.tasks.map { ($0.id, $0) })

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

        self.workbench = DashboardWorkbenchPresentation(
            mission: currentMission,
            tasks: snapshot.tasks,
            issues: snapshot.issues,
            agentProfiles: snapshot.agentProfiles,
            leases: snapshot.leases,
            failures: snapshot.failures,
            events: snapshot.events,
            taskByID: taskByID
        )
    }

    public func validationRerunCommand(validationRuns: [ValidationRunDTO]) -> DashboardValidationRerunCommand? {
        let targetTaskID = failureRows.first?.taskID ?? taskRows.first?.id
        guard let targetTaskID, !targetTaskID.isEmpty else { return nil }

        let matchingRun = validationRuns.last {
            $0.taskID == targetTaskID && $0.status.lowercased() == "failed"
        } ?? validationRuns.last {
            $0.taskID == targetTaskID
        } ?? validationRuns.last {
            $0.status.lowercased() == "failed"
        } ?? validationRuns.last

        let actor = matchingRun?.actor.trimmingCharacters(in: .whitespacesAndNewlines)
        let command = matchingRun?.command.filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        let cwd = matchingRun?.cwd.trimmingCharacters(in: .whitespacesAndNewlines)

        return DashboardValidationRerunCommand(
            taskID: targetTaskID,
            actor: actor.flatMap { $0.isEmpty ? nil : $0 } ?? "Dashboard",
            command: command.flatMap { $0.isEmpty ? nil : $0 } ?? ["pytest", "tests/unit", "-q"],
            cwd: cwd?.isEmpty == false ? cwd : nil
        )
    }
}

public struct DashboardValidationRerunCommand: Equatable, Sendable {
    public let taskID: String
    public let actor: String
    public let command: [String]
    public let cwd: String?

    public var canSubmit: Bool {
        !taskID.isEmpty && !actor.isEmpty && !command.isEmpty
    }
}

public struct DashboardWorkbenchPresentation: Equatable, Sendable {
    public let leftMissionTitle: String?
    public let leftIssueCount: Int
    public let leftTaskCount: Int
    public let leftFailureCount: Int
    public let canvasNodes: [DashboardCanvasNode]
    public let inspector: DashboardInspectorSummary?
    public let auditRows: [DashboardAuditRow]

    public init(
        mission: DashboardMissionSummary?,
        tasks: [TaskDTO],
        issues: [IssueDTO],
        agentProfiles: [AgentProfileDTO],
        leases: [LeaseDTO],
        failures: [FailureDTO],
        events: [EventDTO],
        taskByID: [String: TaskDTO]
    ) {
        leftMissionTitle = mission?.title
        leftIssueCount = issues.count
        leftTaskCount = tasks.count
        leftFailureCount = failures.count

        var nodes: [DashboardCanvasNode] = []
        if let mission {
            nodes.append(
                DashboardCanvasNode(
                    id: mission.id,
                    kind: .mission,
                    title: mission.title,
                    subtitle: "Mission",
                    status: mission.status
                )
            )
        }
        if let issue = issues.first {
            nodes.append(
                DashboardCanvasNode(
                    id: "issue-\(issue.taskID)",
                    kind: .issue,
                    title: taskByID[issue.taskID]?.subject ?? issue.taskID,
                    subtitle: issue.riskLevel,
                    status: issue.parallelMode
                )
            )
        }
        if !agentProfiles.isEmpty {
            nodes.append(
                DashboardCanvasNode(
                    id: "agents",
                    kind: .agents,
                    title: "\(agentProfiles.count)",
                    subtitle: "Agents",
                    status: agentProfiles.first?.status ?? ""
                )
            )
        }
        if !leases.isEmpty {
            nodes.append(
                DashboardCanvasNode(
                    id: "worktrees",
                    kind: .worktrees,
                    title: leases.first?.worktreeName ?? "\(leases.count)",
                    subtitle: "Git Worktrees",
                    status: leases.first?.state ?? ""
                )
            )
        }
        if !tasks.isEmpty {
            nodes.append(
                DashboardCanvasNode(
                    id: "validation",
                    kind: .validation,
                    title: "\(tasks.filter { $0.status == "completed" }.count)/\(tasks.count)",
                    subtitle: "Validation Runs",
                    status: "tracked"
                )
            )
        }
        if let failure = failures.first {
            nodes.append(
                DashboardCanvasNode(
                    id: failure.id,
                    kind: .failure,
                    title: failure.title,
                    subtitle: failure.kind,
                    status: failure.status
                )
            )
        }
        if let issue = issues.first(where: \.requiresHumanApproval) {
            nodes.append(
                DashboardCanvasNode(
                    id: "approval-\(issue.taskID)",
                    kind: .approval,
                    title: taskByID[issue.taskID]?.subject ?? issue.taskID,
                    subtitle: "Human Approval",
                    status: issue.riskLevel
                )
            )
        }
        canvasNodes = nodes

        if let issue = issues.first, let task = taskByID[issue.taskID] {
            inspector = DashboardInspectorSummary(
                title: task.subject,
                status: task.status,
                owner: task.owner,
                riskLevel: issue.riskLevel,
                parallelMode: issue.parallelMode,
                requiresHumanApproval: issue.requiresHumanApproval,
                acceptanceCriteriaCount: issue.acceptanceCriteria.count
            )
        } else if let task = tasks.first {
            inspector = DashboardInspectorSummary(
                title: task.subject,
                status: task.status,
                owner: task.owner,
                riskLevel: nil,
                parallelMode: nil,
                requiresHumanApproval: false,
                acceptanceCriteriaCount: nil
            )
        } else {
            inspector = nil
        }

        auditRows = Array(events.suffix(5)).map {
            DashboardAuditRow(
                id: $0.id,
                type: $0.type,
                actor: $0.actor,
                timestamp: $0.timestamp
            )
        }
    }
}

public enum DashboardCanvasNodeKind: String, Equatable, Sendable {
    case mission
    case issue
    case agents
    case worktrees
    case validation
    case failure
    case approval
}

public struct DashboardCanvasNode: Equatable, Sendable {
    public let id: String
    public let kind: DashboardCanvasNodeKind
    public let title: String
    public let subtitle: String
    public let status: String
}

public struct DashboardInspectorSummary: Equatable, Sendable {
    public let title: String
    public let status: String
    public let owner: String?
    public let riskLevel: String?
    public let parallelMode: String?
    public let requiresHumanApproval: Bool
    public let acceptanceCriteriaCount: Int?
}

public struct DashboardAuditRow: Equatable, Sendable {
    public let id: String
    public let type: String
    public let actor: String
    public let timestamp: String
}

public struct DashboardMissionSummary: Equatable, Sendable {
    public let id: String
    public let title: String
    public let status: String
}

public struct DashboardAgentRow: Equatable, Sendable {
    public let id: String
    public let name: String
    public let role: String
    public let status: String
    public let capabilityCount: Int
    public let maxParallelTasks: Int
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
