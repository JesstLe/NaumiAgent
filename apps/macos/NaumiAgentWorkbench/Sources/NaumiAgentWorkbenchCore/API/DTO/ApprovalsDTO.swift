import Foundation

/// Paginated approvals returned by `GET /workbench/sessions/{id}/approvals`.
public struct ApprovalsDTO: Decodable, Equatable, Sendable {
    public let approvals: [ApprovalDTO]
    public let state: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case approvals
        case state
        case limit
    }

    public init(approvals: [ApprovalDTO], state: String?, limit: Int) {
        self.approvals = approvals
        self.state = state
        self.limit = limit
    }
}
