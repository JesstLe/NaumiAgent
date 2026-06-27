import Foundation

/// Paginated agent profiles returned by `GET /workbench/sessions/{id}/agents`.
public struct AgentProfilesDTO: Decodable, Equatable, Sendable {
    public let agentProfiles: [AgentProfileDTO]
    public let status: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case agentProfiles = "agent_profiles"
        case status
        case limit
    }

    public init(agentProfiles: [AgentProfileDTO], status: String?, limit: Int) {
        self.agentProfiles = agentProfiles
        self.status = status
        self.limit = limit
    }
}
