import Foundation

public struct ContextHealthRecordDraft: Equatable, Sendable {
    public var taskID: String
    public var agentID: String
    public var minutesSinceSync: Int
    public var tokenLoadPercent: Double
    public var policyConflict: Bool
    public var actor: String

    public init(
        taskID: String = "",
        agentID: String = "",
        minutesSinceSync: Int = 0,
        tokenLoadPercent: Double = 25,
        policyConflict: Bool = false,
        actor: String = "Human"
    ) {
        self.taskID = taskID
        self.agentID = agentID
        self.minutesSinceSync = minutesSinceSync
        self.tokenLoadPercent = tokenLoadPercent
        self.policyConflict = policyConflict
        self.actor = actor
    }

    public var trimmedTaskID: String {
        taskID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedAgentID: String {
        agentID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var tokenLoadRatio: Double {
        min(max(tokenLoadPercent, 0), 100) / 100
    }

    public var trimmedActor: String {
        actor.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var canSubmit: Bool {
        !trimmedTaskID.isEmpty
            && !trimmedAgentID.isEmpty
            && !trimmedActor.isEmpty
    }
}
