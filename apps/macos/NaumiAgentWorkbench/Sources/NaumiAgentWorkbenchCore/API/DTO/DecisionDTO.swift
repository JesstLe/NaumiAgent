import Foundation

/// Decision entity returned by `POST /workbench/sessions/{id}/missions/{id}/decisions`.
public struct DecisionDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let missionID: String
    public let kind: String
    public let title: String
    public let content: String
    public let actor: String
    public let createdAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case missionID = "mission_id"
        case kind
        case title
        case content
        case actor
        case createdAt = "created_at"
    }

    public init(
        id: String,
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String,
        createdAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.missionID = missionID
        self.kind = kind
        self.title = title
        self.content = content
        self.actor = actor
        self.createdAt = createdAt
    }
}
