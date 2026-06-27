import Foundation

/// Intent lock entity returned by `POST /workbench/sessions/{id}/missions/{id}/intent-locks`.
public struct IntentLockDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let missionID: String
    public let rule: String
    public let blockedPaths: [String]
    public let allowedPaths: [String]
    public let requireProposalForRisk: String
    public let active: Bool
    public let createdAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case missionID = "mission_id"
        case rule
        case blockedPaths = "blocked_paths"
        case allowedPaths = "allowed_paths"
        case requireProposalForRisk = "require_proposal_for_risk"
        case active
        case createdAt = "created_at"
    }

    public init(
        id: String,
        sessionID: String,
        missionID: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String,
        active: Bool,
        createdAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.missionID = missionID
        self.rule = rule
        self.blockedPaths = blockedPaths
        self.allowedPaths = allowedPaths
        self.requireProposalForRisk = requireProposalForRisk
        self.active = active
        self.createdAt = createdAt
    }
}
