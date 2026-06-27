import Foundation

/// Paginated missions returned by `GET /workbench/sessions/{id}/missions`.
public struct MissionsDTO: Decodable, Equatable, Sendable {
    public let missions: [MissionDTO]
    public let status: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case missions
        case status
        case limit
    }

    public init(missions: [MissionDTO], status: String?, limit: Int) {
        self.missions = missions
        self.status = status
        self.limit = limit
    }
}
