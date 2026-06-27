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
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}
