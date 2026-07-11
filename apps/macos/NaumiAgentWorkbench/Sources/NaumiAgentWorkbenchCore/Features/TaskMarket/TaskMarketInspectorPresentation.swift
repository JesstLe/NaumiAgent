import Foundation

/// Evidence shown for the selected Task Market issue in real mode.
public struct TaskMarketInspectorPresentation: Equatable, Sendable {
    public let createdAt: String
    public let contextHealth: String?
    public let validationCount: Int
    public let acceptanceCriteria: [String]

    public init(issue: TaskMarketDesignIssue, snapshot: WorkbenchSnapshotDTO?) {
        let task = snapshot?.tasks.first { $0.id == issue.taskID }
        let issueRecord = snapshot?.issues.first { $0.taskID == issue.taskID }
        let latestContext = snapshot?.contextSnapshots
            .filter { $0.taskID == issue.taskID }
            .max { $0.createdAt < $1.createdAt }

        createdAt = task?.createdAt ?? issueRecord?.createdAt ?? ""
        contextHealth = latestContext?.health
        validationCount = snapshot?.validationRuns.filter { $0.taskID == issue.taskID }.count ?? 0
        acceptanceCriteria = issueRecord?.acceptanceCriteria ?? []
    }

    public func createdAtLabel(locale: AppLocale) -> String {
        guard !createdAt.isEmpty else { return "-" }
        let normalized = createdAt.replacingOccurrences(of: "T", with: " ")
        guard let dotIndex = normalized.firstIndex(of: ".") else { return normalized }
        return String(normalized[..<dotIndex])
    }

    public func acceptanceCriteriaLabel(locale: AppLocale) -> String {
        let criteria = acceptanceCriteria
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !criteria.isEmpty else {
            return locale == .zhCN ? "未设置" : "Not set"
        }
        return criteria.joined(separator: locale == .zhCN ? "；" : "; ")
    }
}
