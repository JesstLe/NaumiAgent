import Foundation

/// Audit event returned in workbench snapshots.
public struct EventDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let type: String
    public let actor: String
    public let subjectID: String
    public let payload: [String: JSONValue]
    public let timestamp: String
    public let task: TaskDTO?
    public let severity: String
    public let correlationID: String?
    public let parentEventID: String?

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case type
        case actor
        case subjectID = "subject_id"
        case payload
        case timestamp
        case task
        case severity
        case correlationID = "correlation_id"
        case parentEventID = "parent_event_id"
    }

    public init(
        id: String,
        sessionID: String,
        type: String,
        actor: String,
        subjectID: String,
        payload: [String: JSONValue],
        timestamp: String,
        task: TaskDTO? = nil,
        severity: String = "info",
        correlationID: String? = nil,
        parentEventID: String? = nil
    ) {
        self.id = id
        self.sessionID = sessionID
        self.type = type
        self.actor = actor
        self.subjectID = subjectID
        self.payload = payload
        self.timestamp = timestamp
        self.task = task
        self.severity = severity
        self.correlationID = correlationID
        self.parentEventID = parentEventID
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionID = try container.decode(String.self, forKey: .sessionID)
        type = try container.decode(String.self, forKey: .type)
        actor = try container.decode(String.self, forKey: .actor)
        subjectID = try container.decode(String.self, forKey: .subjectID)
        payload = try container.decodeIfPresent([String: JSONValue].self, forKey: .payload) ?? [:]
        timestamp = try container.decode(String.self, forKey: .timestamp)
        task = try container.decodeIfPresent(TaskDTO.self, forKey: .task)
        // Backward compatible: older daemons omit severity/correlation/parent.
        severity = try container.decodeIfPresent(String.self, forKey: .severity) ?? "info"
        correlationID = try container.decodeIfPresent(String.self, forKey: .correlationID)
        parentEventID = try container.decodeIfPresent(String.self, forKey: .parentEventID)
    }
}
