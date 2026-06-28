import Foundation

/// Lease entity returned in workbench snapshots.
public struct LeaseDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let taskID: String
    public let agentID: String
    public let state: String
    public let expiresAt: String
    public let worktreeName: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case taskID = "task_id"
        case agentID = "agent_id"
        case state
        case expiresAt = "expires_at"
        case worktreeName = "worktree_name"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

/// Response returned when a lease mutation asks the backend for a fresh snapshot.
public struct LeaseSnapshotDTO: Decodable, Equatable, Sendable {
    public let lease: LeaseDTO
    public let snapshot: WorkbenchSnapshotDTO
}
