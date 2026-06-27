import Foundation

/// User input for creating an intent lock from the Settings page.
public struct IntentLockDraft: Equatable {
    public var missionID: String
    public var actor: String
    public var rule: String
    public var blockedPathsText: String
    public var allowedPathsText: String
    public var requireProposalForRisk: String

    public init(
        missionID: String = "",
        actor: String = "Human",
        rule: String = "",
        blockedPathsText: String = "",
        allowedPathsText: String = "",
        requireProposalForRisk: String = "high"
    ) {
        self.missionID = missionID
        self.actor = actor
        self.rule = rule
        self.blockedPathsText = blockedPathsText
        self.allowedPathsText = allowedPathsText
        self.requireProposalForRisk = requireProposalForRisk
    }

    public var trimmedMissionID: String {
        missionID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedActor: String {
        actor.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedRule: String {
        rule.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var blockedPaths: [String] {
        paths(from: blockedPathsText)
    }

    public var allowedPaths: [String] {
        paths(from: allowedPathsText)
    }

    public var canSubmit: Bool {
        !trimmedMissionID.isEmpty && !trimmedActor.isEmpty && !trimmedRule.isEmpty
    }

    private func paths(from text: String) -> [String] {
        text
            .components(separatedBy: CharacterSet(charactersIn: ",\n"))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }
}
