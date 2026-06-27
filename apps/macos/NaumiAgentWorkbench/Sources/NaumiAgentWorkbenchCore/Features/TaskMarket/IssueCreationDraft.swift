import Foundation

public struct IssueCreationDraft: Equatable, Sendable {
    public var missionID: String
    public var title: String
    public var description: String
    public var blockedByText: String
    public var acceptanceCriteriaText: String
    public var parallelMode: String
    public var riskLevel: String

    public init(
        missionID: String = "",
        title: String = "",
        description: String = "",
        blockedByText: String = "",
        acceptanceCriteriaText: String = "",
        parallelMode: String = "exclusive",
        riskLevel: String = "medium"
    ) {
        self.missionID = missionID
        self.title = title
        self.description = description
        self.blockedByText = blockedByText
        self.acceptanceCriteriaText = acceptanceCriteriaText
        self.parallelMode = parallelMode
        self.riskLevel = riskLevel
    }

    public var trimmedMissionID: String {
        missionID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedTitle: String {
        title.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedDescription: String {
        description.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var blockedBy: [String] {
        splitLines(blockedByText)
    }

    public var acceptanceCriteria: [String] {
        splitLines(acceptanceCriteriaText)
    }

    public var canSubmit: Bool {
        !trimmedMissionID.isEmpty && !trimmedTitle.isEmpty && !trimmedDescription.isEmpty
    }

    private func splitLines(_ text: String) -> [String] {
        text.split(whereSeparator: \.isNewline)
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }
}
