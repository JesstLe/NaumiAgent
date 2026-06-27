import Foundation

public struct IssueAttachmentDraft: Equatable, Sendable {
    public var missionID: String
    public var taskID: String
    public var acceptanceCriteriaText: String
    public var parallelMode: String
    public var riskLevel: String

    public init(
        missionID: String = "",
        taskID: String = "",
        acceptanceCriteriaText: String = "",
        parallelMode: String = "exclusive",
        riskLevel: String = "medium"
    ) {
        self.missionID = missionID
        self.taskID = taskID
        self.acceptanceCriteriaText = acceptanceCriteriaText
        self.parallelMode = parallelMode
        self.riskLevel = riskLevel
    }

    public var trimmedMissionID: String {
        missionID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedTaskID: String {
        taskID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var acceptanceCriteria: [String] {
        acceptanceCriteriaText.split(whereSeparator: \.isNewline)
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    public var canSubmit: Bool {
        !trimmedMissionID.isEmpty && !trimmedTaskID.isEmpty
    }
}
