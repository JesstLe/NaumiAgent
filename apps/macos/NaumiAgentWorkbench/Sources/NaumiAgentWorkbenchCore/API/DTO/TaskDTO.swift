import Foundation

/// Task entity returned in workbench snapshots.
public struct TaskDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let subject: String
    public let description: String
    public let status: String
    public let activeForm: String?
    public let owner: String?
    public let blocks: [String]
    public let blockedBy: [String]
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case subject
        case description
        case status
        case activeForm = "active_form"
        case owner
        case blocks
        case blockedBy = "blocked_by"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}
