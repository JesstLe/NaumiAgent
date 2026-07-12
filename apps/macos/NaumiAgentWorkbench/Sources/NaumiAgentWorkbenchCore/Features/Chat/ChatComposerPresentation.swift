import Foundation

public enum ChatComposerMode: String, CaseIterable, Equatable, Sendable {
    case chat
    case createIssue
    case linkIssue

    public var showsIssueDetails: Bool { self == .createIssue }
    public var showsIssuePicker: Bool { self == .linkIssue }
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
