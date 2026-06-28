import Foundation

/// Mission entity returned in workbench snapshots.
public struct MissionDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let title: String
    public let goal: String
    public let status: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case title
        case goal
        case status
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(
        id: String,
        sessionID: String,
        title: String,
        goal: String,
        status: String,
        createdAt: String,
        updatedAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.title = title
        self.goal = goal
        self.status = status
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }
}

/// Response returned when mission creation asks the backend for a fresh snapshot.
public struct MissionSnapshotDTO: Decodable, Equatable, Sendable {
    public let mission: MissionDTO
    public let snapshot: WorkbenchSnapshotDTO

    public init(mission: MissionDTO, snapshot: WorkbenchSnapshotDTO) {
        self.mission = mission
        self.snapshot = snapshot
    }
}
