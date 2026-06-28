import Foundation

/// Worktree metadata returned by the Workbench worktree API.
public struct WorktreeDTO: Decodable, Equatable, Sendable {
    public let name: String
    public let path: String
    public let branch: String
    public let baseRef: String
    public let status: String
    public let taskID: String
    public let dirtyFiles: Int
    public let commitsAhead: Int
    public let createdAt: String
    public let updatedAt: String
    public let keptReason: String
    public let metadata: [String: String]
    public let removable: Bool

    public enum CodingKeys: String, CodingKey {
        case name
        case path
        case branch
        case baseRef = "base_ref"
        case status
        case taskID = "task_id"
        case dirtyFiles = "dirty_files"
        case commitsAhead = "commits_ahead"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case keptReason = "kept_reason"
        case metadata
        case removable
    }
}

/// Filtered worktree collection returned by `GET /workbench/sessions/{id}/worktrees`.
public struct WorktreesDTO: Decodable, Equatable, Sendable {
    public let worktrees: [WorktreeDTO]
    public let taskID: String?
    public let status: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case worktrees
        case taskID = "task_id"
        case status
        case limit
    }
}

/// Result returned by `DELETE /workbench/sessions/{id}/worktrees/{name}`.
public struct WorktreeRemovalDTO: Decodable, Equatable, Sendable {
    public let name: String
    public let discardChanges: Bool
    public let message: String

    public enum CodingKeys: String, CodingKey {
        case name
        case discardChanges = "discard_changes"
        case message
    }
}

/// Result returned by `POST /workbench/sessions/{id}/worktrees/{name}/keep?include_snapshot=true`.
public struct WorktreeSnapshotDTO: Decodable, Equatable, Sendable {
    public let worktree: WorktreeDTO
    public let snapshot: WorkbenchSnapshotDTO

    public enum CodingKeys: String, CodingKey {
        case worktree
        case snapshot
    }

    public init(worktree: WorktreeDTO, snapshot: WorkbenchSnapshotDTO) {
        self.worktree = worktree
        self.snapshot = snapshot
    }
}
