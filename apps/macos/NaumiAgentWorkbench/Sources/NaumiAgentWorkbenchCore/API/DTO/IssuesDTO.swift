import Foundation

/// Paginated issues returned by `GET /workbench/sessions/{id}/issues`.
public struct IssuesDTO: Decodable, Equatable, Sendable {
    public let issues: [IssueDTO]
    public let missionID: String?
    public let riskLevel: String?
    public let status: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case issues
        case missionID = "mission_id"
        case riskLevel = "risk_level"
        case status
        case limit
    }

    public init(
        issues: [IssueDTO],
        missionID: String?,
        riskLevel: String?,
        status: String? = nil,
        limit: Int
    ) {
        self.issues = issues
        self.missionID = missionID
        self.riskLevel = riskLevel
        self.status = status
        self.limit = limit
    }
}
