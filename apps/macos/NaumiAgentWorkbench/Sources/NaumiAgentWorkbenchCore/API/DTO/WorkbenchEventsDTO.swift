import Foundation

/// Paginated audit events returned by `GET /workbench/sessions/{id}/events`.
public struct WorkbenchEventsDTO: Decodable, Equatable, Sendable {
    public let events: [EventDTO]
    public let eventType: String?
    public let subjectID: String?
    public let actor: String?
    public let since: String?
    public let severity: String?
    public let correlationID: String?
    public let parentEventID: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case events
        case eventType = "event_type"
        case subjectID = "subject_id"
        case actor
        case since
        case severity
        case correlationID = "correlation_id"
        case parentEventID = "parent_event_id"
        case limit
    }

    public init(
        events: [EventDTO],
        eventType: String? = nil,
        subjectID: String? = nil,
        actor: String? = nil,
        since: String? = nil,
        severity: String? = nil,
        correlationID: String? = nil,
        parentEventID: String? = nil,
        limit: Int
    ) {
        self.events = events
        self.eventType = eventType
        self.subjectID = subjectID
        self.actor = actor
        self.since = since
        self.severity = severity
        self.correlationID = correlationID
        self.parentEventID = parentEventID
        self.limit = limit
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        events = try container.decode([EventDTO].self, forKey: .events)
        eventType = try container.decodeIfPresent(String.self, forKey: .eventType)
        subjectID = try container.decodeIfPresent(String.self, forKey: .subjectID)
        actor = try container.decodeIfPresent(String.self, forKey: .actor)
        since = try container.decodeIfPresent(String.self, forKey: .since)
        severity = try container.decodeIfPresent(String.self, forKey: .severity)
        correlationID = try container.decodeIfPresent(String.self, forKey: .correlationID)
        parentEventID = try container.decodeIfPresent(String.self, forKey: .parentEventID)
        limit = try container.decode(Int.self, forKey: .limit)
    }
}
