import Foundation

public struct ChatRunStepDTO: Decodable, Equatable, Sendable, Identifiable {
    public var id: String { "\(sequence)-\(eventID)" }
    public let sequence: Int
    public let stage: String
    public let status: String
    public let summary: String
    public let detail: String
    public let eventID: String
    public let startedAt: String
    public let completedAt: String
    public let metadata: [String: JSONValue]

    public enum CodingKeys: String, CodingKey {
        case sequence, stage, status, summary, detail, metadata
        case eventID = "event_id"
        case startedAt = "started_at"
        case completedAt = "completed_at"
    }

    public init(
        sequence: Int,
        stage: String,
        status: String,
        summary: String,
        detail: String = "",
        eventID: String = "",
        startedAt: String = "",
        completedAt: String = "",
        metadata: [String: JSONValue] = [:]
    ) {
        self.sequence = sequence
        self.stage = stage
        self.status = status
        self.summary = summary
        self.detail = detail
        self.eventID = eventID
        self.startedAt = startedAt
        self.completedAt = completedAt
        self.metadata = metadata
    }
}

public struct ChatArtifactDTO: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let kind: String
    public let title: String
    public let summary: [String: JSONValue]
    public let status: String
    public let createdAt: String
    public let metadata: [String: JSONValue]

    public enum CodingKeys: String, CodingKey {
        case id, kind, title, summary, status, metadata
        case createdAt = "created_at"
    }

    public init(
        id: String,
        kind: String,
        title: String,
        summary: [String: JSONValue] = [:],
        status: String,
        createdAt: String = "",
        metadata: [String: JSONValue] = [:]
    ) {
        self.id = id
        self.kind = kind
        self.title = title
        self.summary = summary
        self.status = status
        self.createdAt = createdAt
        self.metadata = metadata
    }
}

public struct ChatRunDTO: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let sessionID: String
    public let userMessageID: String
    public let assistantMessageID: String
    public let status: String
    public let startedAt: String
    public let updatedAt: String
    public let completedAt: String
    public let steps: [ChatRunStepDTO]
    public let artifacts: [ChatArtifactDTO]

    public enum CodingKeys: String, CodingKey {
        case id, status, steps, artifacts
        case sessionID = "session_id"
        case userMessageID = "user_message_id"
        case assistantMessageID = "assistant_message_id"
        case startedAt = "started_at"
        case updatedAt = "updated_at"
        case completedAt = "completed_at"
    }

    public init(
        id: String,
        sessionID: String,
        userMessageID: String,
        assistantMessageID: String = "",
        status: String,
        startedAt: String,
        updatedAt: String,
        completedAt: String = "",
        steps: [ChatRunStepDTO] = [],
        artifacts: [ChatArtifactDTO] = []
    ) {
        self.id = id
        self.sessionID = sessionID
        self.userMessageID = userMessageID
        self.assistantMessageID = assistantMessageID
        self.status = status
        self.startedAt = startedAt
        self.updatedAt = updatedAt
        self.completedAt = completedAt
        self.steps = steps
        self.artifacts = artifacts
    }
}

public struct ChatRunsDTO: Decodable, Equatable, Sendable {
    public let runs: [ChatRunDTO]
    public let total: Int

    public init(runs: [ChatRunDTO], total: Int) {
        self.runs = runs
        self.total = total
    }
}

public struct ChatRunCancelDTO: Decodable, Equatable, Sendable {
    public let status: String

    public init(status: String) {
        self.status = status
    }
}

public protocol ChatRunProviding: Sendable {
    func fetchChatRuns(sessionID: String, limit: Int) async throws(APIError) -> ChatRunsDTO
    func fetchChatRun(sessionID: String, runID: String) async throws(APIError) -> ChatRunDTO
    func cancelChatRun(sessionID: String, runID: String) async throws(APIError) -> ChatRunCancelDTO
}
