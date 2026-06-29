import Foundation

/// API command for loading a selected Dashboard agent profile detail.
public struct DashboardAgentSelectionCommand: Equatable, Sendable {
    public let agentID: String

    public init?(agent: DashboardAgentRow) {
        let trimmedAgentID = agent.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedAgentID.isEmpty else {
            return nil
        }

        self.agentID = trimmedAgentID
    }
}
