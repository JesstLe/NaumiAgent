import Foundation

/// Failure record returned in workbench snapshots.
public struct FailureDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let taskID: String
    public let kind: String
    public let title: String
    public let detail: String
    public let sourceID: String
    public let status: String
    public let createdAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case taskID = "task_id"
        case kind
        case title
        case detail
        case sourceID = "source_id"
        case status
        case createdAt = "created_at"
    }
}
