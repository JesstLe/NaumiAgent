import Foundation

/// Agent capability profile returned in workbench snapshots.
public struct AgentProfileDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let name: String
    public let role: String
    public let capabilities: [String]
    public let permissions: [String]
    public let maxParallelTasks: Int
    public let status: String
    public let lastHeartbeatAt: String
    public let currentIssue: IssueDTO?
    public let currentLease: LeaseDTO?
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case name
        case role
        case capabilities
        case permissions
        case maxParallelTasks = "max_parallel_tasks"
        case status
        case lastHeartbeatAt = "last_heartbeat_at"
        case currentIssue = "current_issue"
        case currentLease = "current_lease"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(
        id: String,
        sessionID: String,
        name: String,
        role: String,
        capabilities: [String],
        permissions: [String],
        maxParallelTasks: Int,
        status: String,
        lastHeartbeatAt: String = "",
        currentIssue: IssueDTO? = nil,
        currentLease: LeaseDTO? = nil,
        createdAt: String,
        updatedAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.name = name
        self.role = role
        self.capabilities = capabilities
        self.permissions = permissions
        self.maxParallelTasks = maxParallelTasks
        self.status = status
        self.lastHeartbeatAt = lastHeartbeatAt
        self.currentIssue = currentIssue
        self.currentLease = currentLease
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionID = try container.decodeIfPresent(String.self, forKey: .sessionID) ?? ""
        name = try container.decodeIfPresent(String.self, forKey: .name) ?? ""
        role = try container.decodeIfPresent(String.self, forKey: .role) ?? ""
        capabilities = try container.decodeIfPresent([String].self, forKey: .capabilities) ?? []
        permissions = try container.decodeIfPresent([String].self, forKey: .permissions) ?? []
        maxParallelTasks = try container.decodeIfPresent(Int.self, forKey: .maxParallelTasks) ?? 1
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "idle"
        lastHeartbeatAt = try container.decodeIfPresent(String.self, forKey: .lastHeartbeatAt) ?? ""
        currentIssue = try container.decodeIfPresent(IssueDTO.self, forKey: .currentIssue)
        currentLease = try container.decodeIfPresent(LeaseDTO.self, forKey: .currentLease)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        updatedAt = try container.decodeIfPresent(String.self, forKey: .updatedAt) ?? createdAt
    }
}

/// Result returned by `POST /workbench/sessions/{id}/agents/{agent_id}?include_snapshot=true`.
public struct AgentProfileSnapshotDTO: Decodable, Equatable, Sendable {
    public let agentProfile: AgentProfileDTO
    public let snapshot: WorkbenchSnapshotDTO

    public enum CodingKeys: String, CodingKey {
        case agentProfile = "agent_profile"
        case snapshot
    }

    public init(agentProfile: AgentProfileDTO, snapshot: WorkbenchSnapshotDTO) {
        self.agentProfile = agentProfile
        self.snapshot = snapshot
    }
}
