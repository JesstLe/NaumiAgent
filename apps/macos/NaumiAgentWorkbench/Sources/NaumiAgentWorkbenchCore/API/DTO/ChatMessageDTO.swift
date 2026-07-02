import Foundation

/// Assistant message returned by the daily chat API.
public struct ChatMessageDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let role: String
    public let content: String
    public let timestamp: String
    public let metadata: [String: JSONValue]

    public init(
        id: String,
        role: String,
        content: String,
        timestamp: String,
        metadata: [String: JSONValue] = [:]
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.timestamp = timestamp
        self.metadata = metadata
    }
}

/// Paginated chat history returned by `GET /sessions/{session_id}/messages`.
public struct ChatMessageListDTO: Decodable, Equatable, Sendable {
    public let messages: [ChatMessageDTO]
    public let total: Int

    public init(messages: [ChatMessageDTO], total: Int) {
        self.messages = messages
        self.total = total
    }
}

/// Optional task-market draft sent with a daily chat message.
public struct ChatIssueDraftDTO: Encodable, Equatable, Sendable {
    public let missionID: String
    public let title: String
    public let description: String
    public let blockedBy: [String]
    public let acceptanceCriteria: [String]
    public let parallelMode: String
    public let riskLevel: String

    public init(
        missionID: String,
        title: String,
        description: String,
        blockedBy: [String] = [],
        acceptanceCriteria: [String] = [],
        parallelMode: String = "exclusive",
        riskLevel: String = "medium"
    ) {
        self.missionID = missionID
        self.title = title
        self.description = description
        self.blockedBy = blockedBy
        self.acceptanceCriteria = acceptanceCriteria
        self.parallelMode = parallelMode
        self.riskLevel = riskLevel
    }
}
