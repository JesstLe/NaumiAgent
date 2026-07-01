import Foundation

/// Paginated approvals returned by `GET /workbench/sessions/{id}/approvals`.
public struct ApprovalsDTO: Decodable, Equatable, Sendable {
    public let approvals: [ApprovalDTO]
    public let state: String?
    public let missionID: String?
    public let taskID: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case approvals
        case state
        case missionID = "mission_id"
        case taskID = "task_id"
        case limit
    }

    public init(
        approvals: [ApprovalDTO],
        state: String?,
        missionID: String? = nil,
        taskID: String? = nil,
        limit: Int
    ) {
        self.approvals = approvals
        self.state = state
        self.missionID = missionID
        self.taskID = taskID
        self.limit = limit
    }
}
