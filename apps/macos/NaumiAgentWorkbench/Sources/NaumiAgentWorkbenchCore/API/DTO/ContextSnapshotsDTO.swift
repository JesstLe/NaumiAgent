import Foundation

/// Paginated context health snapshots returned by
/// `GET /workbench/sessions/{id}/context-snapshots`.
public struct ContextSnapshotsDTO: Decodable, Equatable, Sendable {
    public let contextSnapshots: [ContextSnapshotDTO]
    public let taskID: String?
    public let agentID: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case contextSnapshots = "context_snapshots"
        case taskID = "task_id"
        case agentID = "agent_id"
        case limit
    }

    public init(
        contextSnapshots: [ContextSnapshotDTO],
        taskID: String?,
        agentID: String?,
        limit: Int
    ) {
        self.contextSnapshots = contextSnapshots
        self.taskID = taskID
        self.agentID = agentID
        self.limit = limit
    }
}
