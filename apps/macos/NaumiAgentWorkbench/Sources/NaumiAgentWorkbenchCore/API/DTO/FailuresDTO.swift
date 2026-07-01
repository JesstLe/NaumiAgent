import Foundation

/// Paginated failures returned by `GET /workbench/sessions/{id}/failures`.
public struct FailuresDTO: Decodable, Equatable, Sendable {
    public let failures: [FailureDTO]
    public let taskID: String?
    public let status: String?
    public let kind: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case failures
        case taskID = "task_id"
        case status
        case kind
        case limit
    }

    public init(failures: [FailureDTO], taskID: String?, status: String?, kind: String? = nil, limit: Int) {
        self.failures = failures
        self.taskID = taskID
        self.status = status
        self.kind = kind
        self.limit = limit
    }
}
