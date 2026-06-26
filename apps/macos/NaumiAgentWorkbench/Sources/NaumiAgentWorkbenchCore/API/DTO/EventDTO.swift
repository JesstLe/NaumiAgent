import Foundation

/// Audit event returned in workbench snapshots.
public struct EventDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let type: String
    public let actor: String
    public let subjectID: String
    public let payload: [String: JSONValue]
    public let timestamp: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case type
        case actor
        case subjectID = "subject_id"
        case payload
        case timestamp
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionID = try container.decode(String.self, forKey: .sessionID)
        type = try container.decode(String.self, forKey: .type)
        actor = try container.decode(String.self, forKey: .actor)
        subjectID = try container.decode(String.self, forKey: .subjectID)
        payload = try container.decodeIfPresent([String: JSONValue].self, forKey: .payload) ?? [:]
        timestamp = try container.decode(String.self, forKey: .timestamp)
    }
}
