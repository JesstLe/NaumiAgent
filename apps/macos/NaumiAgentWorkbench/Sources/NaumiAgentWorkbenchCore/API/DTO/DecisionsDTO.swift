import Foundation

/// List of decisions returned by `GET /workbench/sessions/{id}/missions/{id}/decisions`.
public struct DecisionsDTO: Decodable, Equatable, Sendable {
    public let decisions: [DecisionDTO]
    public let missionID: String

    public enum CodingKeys: String, CodingKey {
        case decisions
        case missionID = "mission_id"
    }

    public init(decisions: [DecisionDTO], missionID: String) {
        self.decisions = decisions
        self.missionID = missionID
    }
}
