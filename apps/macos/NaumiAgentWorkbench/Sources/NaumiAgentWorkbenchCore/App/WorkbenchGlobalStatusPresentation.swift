import Foundation

/// Compact, route-independent status summary shown below the primary navigation.
public struct WorkbenchGlobalStatusPresentation: Equatable, Sendable {
    public let missionTitle: String
    public let items: [WorkbenchGlobalStatusItem]

    public init(
        snapshot: WorkbenchSnapshotDTO?,
        approvals: [ApprovalDTO],
        validationRuns: [ValidationRunDTO],
        failures: [FailureDTO],
        locale: AppLocale
    ) {
        let missionTitle = snapshot?.summary?.currentMissionTitle
            ?? snapshot?.missions.first?.title
            ?? AppStrings.GlobalStatus.noMission(locale)
        let taskByID = Dictionary(uniqueKeysWithValues: (snapshot?.tasks ?? []).map { ($0.id, $0) })
        let issues = snapshot?.issues ?? []
        let blockedIssueCount = issues.filter { issue in
            guard let task = taskByID[issue.taskID] else {
                return false
            }
            return task.status == "blocked" || !task.blockedBy.isEmpty
        }.count
        let openIssueCount = issues.filter { issue in
            taskByID[issue.taskID]?.status != "completed"
        }.count
        let pendingApprovalCount = approvals.filter { $0.state == "waiting" || $0.state == "pending" }.count
        let failedValidationCount = validationRuns.filter { $0.status == "failed" }.count
            + failures.filter { $0.status != "resolved" && $0.status != "closed" }.count
        let activeAgentsValue = snapshot?.summary?.activeAgents ?? snapshot?.agentProfiles.count ?? 0
        let openIssuesValue = snapshot?.summary?.openIssues ?? openIssueCount
        let blockedIssuesValue = snapshot?.summary?.blockedIssues ?? blockedIssueCount
        let pendingApprovalsValue = snapshot?.summary?.pendingApprovals ?? pendingApprovalCount
        let failedValidationsValue = snapshot?.summary?.failedValidations ?? failedValidationCount

        self.missionTitle = missionTitle
        self.items = [
            WorkbenchGlobalStatusItem(
                label: "Mission",
                value: missionTitle,
                systemImage: "scope",
                tone: .accent
            ),
            WorkbenchGlobalStatusItem(
                label: AppStrings.GlobalStatus.activeAgents(locale),
                value: "\(activeAgentsValue)",
                systemImage: "person.2",
                tone: .purple
            ),
            WorkbenchGlobalStatusItem(
                label: AppStrings.GlobalStatus.openIssues(locale),
                value: "\(openIssuesValue)",
                systemImage: "list.bullet.rectangle",
                tone: .blue
            ),
            WorkbenchGlobalStatusItem(
                label: AppStrings.GlobalStatus.blocked(locale),
                value: "\(blockedIssuesValue)",
                systemImage: "exclamationmark.triangle",
                tone: blockedIssuesValue > 0 ? .orange : .secondary
            ),
            WorkbenchGlobalStatusItem(
                label: AppStrings.GlobalStatus.pendingApproval(locale),
                value: "\(pendingApprovalsValue)",
                systemImage: "hand.raised",
                tone: pendingApprovalsValue > 0 ? .pink : .secondary
            ),
            WorkbenchGlobalStatusItem(
                label: AppStrings.GlobalStatus.failedValidations(locale),
                value: "\(failedValidationsValue)",
                systemImage: "xmark.octagon",
                tone: failedValidationsValue > 0 ? .red : .secondary
            ),
        ]
    }
}

public struct WorkbenchGlobalStatusItem: Identifiable, Equatable, Sendable {
    public let id: String
    public let label: String
    public let value: String
    public let systemImage: String
    public let tone: WorkbenchGlobalStatusTone

    public init(
        label: String,
        value: String,
        systemImage: String,
        tone: WorkbenchGlobalStatusTone
    ) {
        self.id = "\(label)-\(systemImage)"
        self.label = label
        self.value = value
        self.systemImage = systemImage
        self.tone = tone
    }
}

public enum WorkbenchGlobalStatusTone: Equatable, Sendable {
    case accent
    case blue
    case orange
    case pink
    case purple
    case red
    case secondary
}
