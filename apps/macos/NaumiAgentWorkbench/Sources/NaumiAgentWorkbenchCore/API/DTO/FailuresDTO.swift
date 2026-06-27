import Foundation

/// Paginated failures returned by `GET /workbench/sessions/{id}/failures`.
public struct FailuresDTO: Decodable, Equatable, Sendable {
    public let failures: [FailureDTO]
    public let taskID: String?
    public let status: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case failures
        case taskID = "task_id"
        case status
        case limit
    }

    public init(failures: [FailureDTO], taskID: String?, status: String?, limit: Int) {
        self.failures = failures
        self.taskID = taskID
        self.status = status
        self.limit = limit
    }
}
