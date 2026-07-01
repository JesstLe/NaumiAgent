import Foundation

/// List of decisions returned by `GET /workbench/sessions/{id}/missions/{id}/decisions`.
public struct DecisionsDTO: Decodable, Equatable, Sendable {
    public let decisions: [DecisionDTO]
    public let missionID: String
    public let kind: String?

    public enum CodingKeys: String, CodingKey {
        case decisions
        case missionID = "mission_id"
        case kind
    }

    public init(decisions: [DecisionDTO], missionID: String, kind: String? = nil) {
        self.decisions = decisions
        self.missionID = missionID
        self.kind = kind
    }
}
