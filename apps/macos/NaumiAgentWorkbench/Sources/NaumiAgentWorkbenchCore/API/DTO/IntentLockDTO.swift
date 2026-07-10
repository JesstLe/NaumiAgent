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
    public let createdBy: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case missionID = "mission_id"
        case rule
        case blockedPaths = "blocked_paths"
        case allowedPaths = "allowed_paths"
        case requireProposalForRisk = "require_proposal_for_risk"
        case active
        case createdBy = "created_by"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
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
        createdBy: String = "Human",
        createdAt: String,
        updatedAt: String = ""
    ) {
        self.id = id
        self.sessionID = sessionID
        self.missionID = missionID
        self.rule = rule
        self.blockedPaths = blockedPaths
        self.allowedPaths = allowedPaths
        self.requireProposalForRisk = requireProposalForRisk
        self.active = active
        self.createdBy = createdBy
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionID = try container.decodeIfPresent(String.self, forKey: .sessionID) ?? ""
        missionID = try container.decodeIfPresent(String.self, forKey: .missionID) ?? ""
        rule = try container.decode(String.self, forKey: .rule)
        blockedPaths = try container.decodeIfPresent([String].self, forKey: .blockedPaths) ?? []
        allowedPaths = try container.decodeIfPresent([String].self, forKey: .allowedPaths) ?? []
        requireProposalForRisk = try container.decodeIfPresent(String.self, forKey: .requireProposalForRisk) ?? "high"
        active = try container.decodeIfPresent(Bool.self, forKey: .active) ?? true
        createdBy = try container.decodeIfPresent(String.self, forKey: .createdBy) ?? "Human"
        createdAt = try container.decode(String.self, forKey: .createdAt)
        updatedAt = try container.decodeIfPresent(String.self, forKey: .updatedAt) ?? createdAt
    }
}

public struct IntentLockSnapshotDTO: Decodable, Equatable, Sendable {
    public let intentLock: IntentLockDTO
    public let snapshot: WorkbenchSnapshotDTO

    public enum CodingKeys: String, CodingKey {
        case intentLock = "intent_lock"
        case snapshot
    }

    public init(intentLock: IntentLockDTO, snapshot: WorkbenchSnapshotDTO) {
        self.intentLock = intentLock
        self.snapshot = snapshot
    }
}
