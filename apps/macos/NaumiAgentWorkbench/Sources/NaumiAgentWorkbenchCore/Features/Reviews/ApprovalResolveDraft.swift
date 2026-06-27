import Foundation

/// User input for resolving an approval request from the Reviews page.
public struct ApprovalResolveDraft: Equatable {
    public var actor: String
    public var decisionNote: String

    public init(
        actor: String = "Human",
        decisionNote: String = ""
    ) {
        self.actor = actor
        self.decisionNote = decisionNote
    }

    public var trimmedActor: String {
        actor.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedDecisionNote: String {
        decisionNote.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var canResolve: Bool {
        !trimmedActor.isEmpty
    }
}
