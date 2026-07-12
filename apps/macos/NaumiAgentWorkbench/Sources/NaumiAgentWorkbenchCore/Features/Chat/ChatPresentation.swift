import Foundation

public enum ChatMessageStyle: Equatable, Sendable {
    case compactBubble
    case document
}

public struct ChatIssueSummary: Equatable, Sendable, Identifiable {
    public let id: String
    public let title: String
    public let riskLevel: String
    public let status: String
    public let owner: String?

    public init(id: String, title: String, riskLevel: String, status: String, owner: String?) {
        self.id = id
        self.title = title
        self.riskLevel = riskLevel
        self.status = status
        self.owner = owner
    }
}

public enum ChatPresentation {
    public static func style(forRole role: String) -> ChatMessageStyle {
        role == "user" ? .compactBubble : .document
    }

    public static func roleLabel(for role: String, locale: AppLocale) -> String {
        switch role {
        case "user":
            return locale == .zhCN ? "你" : "You"
        case "assistant":
            return "NaumiAgent"
        default:
            return role
        }
    }

    public static func issueSummaries(
        from issues: [IssueDTO],
        taskTitlesByID: [String: String] = [:]
    ) -> [ChatIssueSummary] {
        issues
            .map { issue in
                ChatIssueSummary(
                    id: issue.taskID,
                    title: issue.task?.subject ?? taskTitlesByID[issue.taskID] ?? issue.taskID,
                    riskLevel: issue.riskLevel,
                    status: issue.task?.status ?? "pending",
                    owner: issue.task?.owner
                )
            }
            .sorted { lhs, rhs in
                let leftRank = riskRank(lhs.riskLevel)
                let rightRank = riskRank(rhs.riskLevel)
                return leftRank == rightRank ? lhs.id < rhs.id : leftRank < rightRank
            }
    }

    public static func riskColorName(_ risk: String) -> String {
        switch risk.lowercased() {
        case "critical": "red"
        case "high": "orange"
        case "medium": "yellow"
        default: "green"
        }
    }

    private static func riskRank(_ risk: String) -> Int {
        switch risk.lowercased() {
        case "critical": 0
        case "high": 1
        case "medium": 2
        default: 3
        }
    }
}
