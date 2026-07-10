import Foundation

/// Decision entity returned by `POST /workbench/sessions/{id}/missions/{id}/decisions`.
public struct DecisionDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let missionID: String
    public let kind: String
    public let title: String
    public let content: String
    public let actor: String
    /// How strongly the decision constrains actions: advisory / required / blocking.
    public let strength: String
    public let createdAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case missionID = "mission_id"
        case kind
        case title
        case content
        case actor
        case strength
        case createdAt = "created_at"
    }

    public init(
        id: String,
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String,
        strength: String = "required",
        createdAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.missionID = missionID
        self.kind = kind
        self.title = title
        self.content = content
        self.actor = actor
        self.strength = strength
        self.createdAt = createdAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionID = try container.decodeIfPresent(String.self, forKey: .sessionID) ?? ""
        missionID = try container.decodeIfPresent(String.self, forKey: .missionID) ?? ""
        kind = try container.decode(String.self, forKey: .kind)
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? ""
        content = try container.decodeIfPresent(String.self, forKey: .content) ?? ""
        actor = try container.decodeIfPresent(String.self, forKey: .actor) ?? ""
        strength = try container.decodeIfPresent(String.self, forKey: .strength) ?? "required"
        createdAt = try container.decode(String.self, forKey: .createdAt)
    }

    /// Localized label explaining the decision's enforcement strength.
    public func strengthLabel(locale: AppLocale) -> String {
        switch strength.lowercased() {
        case "blocking":
            return locale == .zhCN ? "阻断" : "Blocking"
        case "advisory":
            return locale == .zhCN ? "建议" : "Advisory"
        default:
            return locale == .zhCN ? "必须遵守" : "Required"
        }
    }
}

public struct DecisionSnapshotDTO: Decodable, Equatable, Sendable {
    public let decision: DecisionDTO
    public let snapshot: WorkbenchSnapshotDTO

    public init(decision: DecisionDTO, snapshot: WorkbenchSnapshotDTO) {
        self.decision = decision
        self.snapshot = snapshot
    }
}
