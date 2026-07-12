import Foundation

public enum ChatComposerMode: String, CaseIterable, Equatable, Sendable {
    case chat
    case createIssue
    case linkIssue

    public var showsIssueDetails: Bool { self == .createIssue }
    public var showsIssuePicker: Bool { self == .linkIssue }
}

public enum ChatRuntimeMode: String, CaseIterable, Equatable, Sendable, Codable {
    case `default`
    case plan
    case bypass
}

public struct ChatComposerSessionState: Equatable, Sendable {
    public var draftMessage = ""
    public var mode: ChatComposerMode = .chat
    public var selectedMissionID = ""
    public var issueTitle = ""
    public var issueDescription = ""
    public var acceptanceCriteria = ""
    public var parallelMode = "exclusive"
    public var riskLevel = "medium"
    public var linkedIssueID = ""
    public var selectedSources: [ChatSourceReferenceDTO] = []
    public var runtimeMode: ChatRuntimeMode = .default

    public init() {}

    public mutating func resetAfterSuccessfulSubmission() {
        draftMessage = ""
        mode = .chat
        issueTitle = ""
        issueDescription = ""
        acceptanceCriteria = ""
        linkedIssueID = ""
        selectedSources = []
    }
}

public enum ChatComposerPrimaryAction: Equatable, Sendable {
    case send
    case stop
    case retry
}

public struct ChatComposerPresentation: Equatable, Sendable {
    public let primaryAction: ChatComposerPrimaryAction
    public let isEditorEnabled: Bool

    public init(isSending: Bool, hasError: Bool) {
        if isSending {
            primaryAction = .stop
        } else if hasError {
            primaryAction = .retry
        } else {
            primaryAction = .send
        }
        isEditorEnabled = true
    }

    public static func canSend(draft: String, isSending: Bool) -> Bool {
        !isSending && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}
