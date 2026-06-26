import Foundation

/// Mission entity returned in workbench snapshots.
public struct MissionDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let title: String
    public let goal: String
    public let status: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case title
        case goal
        case status
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}
