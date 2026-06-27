import Foundation

/// Paginated leases returned by `GET /workbench/sessions/{id}/leases`.
public struct LeasesDTO: Decodable, Equatable, Sendable {
    public let leases: [LeaseDTO]
    public let state: String?
    public let taskID: String?
    public let agentID: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case leases
        case state
        case taskID = "task_id"
        case agentID = "agent_id"
        case limit
    }

    public init(leases: [LeaseDTO], state: String?, taskID: String?, agentID: String?, limit: Int) {
        self.leases = leases
        self.state = state
        self.taskID = taskID
        self.agentID = agentID
        self.limit = limit
    }
}
