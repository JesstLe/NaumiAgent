import Foundation

/// Full workbench snapshot returned by `GET /workbench/sessions/{id}/snapshot`.
///
/// Snapshot 是真相；SwiftUI 不自行推导最终状态。
public struct WorkbenchSnapshotDTO: Decodable, Equatable, Sendable {
    public let sessionID: String
    public let missions: [MissionDTO]
    public let agentProfiles: [AgentProfileDTO]
    public let tasks: [TaskDTO]
    public let issues: [IssueDTO]
    public let leases: [LeaseDTO]
    public let failures: [FailureDTO]
    public let events: [EventDTO]

    public enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case missions
        case agentProfiles = "agent_profiles"
        case tasks
        case issues
        case leases
        case failures
        case events
    }

    public init(
        sessionID: String,
        missions: [MissionDTO],
        agentProfiles: [AgentProfileDTO] = [],
        tasks: [TaskDTO],
        issues: [IssueDTO],
        leases: [LeaseDTO] = [],
        failures: [FailureDTO],
        events: [EventDTO]
    ) {
        self.sessionID = sessionID
        self.missions = missions
        self.agentProfiles = agentProfiles
        self.tasks = tasks
        self.issues = issues
        self.leases = leases
        self.failures = failures
        self.events = events
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionID = try container.decode(String.self, forKey: .sessionID)
        missions = try container.decode([MissionDTO].self, forKey: .missions)
        agentProfiles = try container.decodeIfPresent([AgentProfileDTO].self, forKey: .agentProfiles) ?? []
        tasks = try container.decode([TaskDTO].self, forKey: .tasks)
        issues = try container.decode([IssueDTO].self, forKey: .issues)
        leases = try container.decodeIfPresent([LeaseDTO].self, forKey: .leases) ?? []
        failures = try container.decode([FailureDTO].self, forKey: .failures)
        events = try container.decode([EventDTO].self, forKey: .events)
    }
}
