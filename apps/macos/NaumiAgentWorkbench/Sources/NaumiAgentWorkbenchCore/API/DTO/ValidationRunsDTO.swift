import Foundation

/// Paginated validation runs returned by `GET /workbench/sessions/{id}/validation-runs`.
public struct ValidationRunsDTO: Decodable, Equatable, Sendable {
    public let validationRuns: [ValidationRunDTO]
    public let taskID: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case validationRuns = "validation_runs"
        case taskID = "task_id"
        case limit
    }

    public init(validationRuns: [ValidationRunDTO], taskID: String?, limit: Int) {
        self.validationRuns = validationRuns
        self.taskID = taskID
        self.limit = limit
    }
}
