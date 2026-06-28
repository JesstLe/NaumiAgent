import Foundation

/// Approval entity returned by `POST /workbench/sessions/{id}/approvals/{id}/resolve`.
public struct ApprovalDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let missionID: String
    public let taskID: String
    public let state: String
    public let title: String
    public let detail: String
    public let requester: String
    public let reviewer: String
    public let decisionNote: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case missionID = "mission_id"
        case taskID = "task_id"
        case state
        case title
        case detail
        case requester
        case reviewer
        case decisionNote = "decision_note"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(
        id: String,
        sessionID: String,
        missionID: String,
        taskID: String,
        state: String,
        title: String,
        detail: String,
        requester: String,
        reviewer: String,
        decisionNote: String,
        createdAt: String,
        updatedAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.missionID = missionID
        self.taskID = taskID
        self.state = state
        self.title = title
        self.detail = detail
        self.requester = requester
        self.reviewer = reviewer
        self.decisionNote = decisionNote
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }
}

public struct ApprovalSnapshotDTO: Decodable, Equatable, Sendable {
    public let approval: ApprovalDTO
    public let snapshot: WorkbenchSnapshotDTO

    public init(approval: ApprovalDTO, snapshot: WorkbenchSnapshotDTO) {
        self.approval = approval
        self.snapshot = snapshot
    }
}
