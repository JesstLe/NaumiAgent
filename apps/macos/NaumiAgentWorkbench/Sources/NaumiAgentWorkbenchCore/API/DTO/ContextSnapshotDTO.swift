import Foundation

/// Single context health snapshot returned by
/// `GET /workbench/sessions/{id}/context-snapshots`.
public struct ContextSnapshotDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let agentID: String
    public let taskID: String
    public let health: String
    public let reasons: [String]
    public let createdAt: String
    public let task: TaskDTO?

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case agentID = "agent_id"
        case taskID = "task_id"
        case health
        case reasons
        case createdAt = "created_at"
        case task
    }

    public init(
        id: String,
        sessionID: String,
        agentID: String,
        taskID: String,
        health: String,
        reasons: [String],
        createdAt: String,
        task: TaskDTO? = nil
    ) {
        self.id = id
        self.sessionID = sessionID
        self.agentID = agentID
        self.taskID = taskID
        self.health = health
        self.reasons = reasons
        self.createdAt = createdAt
        self.task = task
    }
}

/// Response returned when recording context health asks the backend for a fresh snapshot.
public struct ContextHealthSnapshotDTO: Decodable, Equatable, Sendable {
    public let contextSnapshot: ContextSnapshotDTO
    public let snapshot: WorkbenchSnapshotDTO

    public enum CodingKeys: String, CodingKey {
        case contextSnapshot = "context_snapshot"
        case snapshot
    }

    public init(contextSnapshot: ContextSnapshotDTO, snapshot: WorkbenchSnapshotDTO) {
        self.contextSnapshot = contextSnapshot
        self.snapshot = snapshot
    }
}
