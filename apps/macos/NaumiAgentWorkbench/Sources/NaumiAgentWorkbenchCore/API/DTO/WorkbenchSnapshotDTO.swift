import Foundation

/// Full workbench snapshot returned by `GET /workbench/sessions/{id}/snapshot`.
///
/// Snapshot 是真相；SwiftUI 不自行推导最终状态。
public struct WorkbenchSnapshotDTO: Decodable, Equatable, Sendable {
    public let sessionID: String
    public let missions: [MissionDTO]
    public let tasks: [TaskDTO]
    public let issues: [IssueDTO]
    public let failures: [FailureDTO]
    public let events: [EventDTO]

    public enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case missions
        case tasks
        case issues
        case failures
        case events
    }
}
