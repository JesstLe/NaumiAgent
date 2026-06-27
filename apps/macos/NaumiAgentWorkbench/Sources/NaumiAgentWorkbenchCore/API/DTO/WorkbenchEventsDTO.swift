import Foundation

/// Paginated audit events returned by `GET /workbench/sessions/{id}/events`.
public struct WorkbenchEventsDTO: Decodable, Equatable, Sendable {
    public let events: [EventDTO]
    public let eventType: String?
    public let subjectID: String?
    public let actor: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case events
        case eventType = "event_type"
        case subjectID = "subject_id"
        case actor
        case limit
    }

    public init(
        events: [EventDTO],
        eventType: String? = nil,
        subjectID: String? = nil,
        actor: String? = nil,
        limit: Int
    ) {
        self.events = events
        self.eventType = eventType
        self.subjectID = subjectID
        self.actor = actor
        self.limit = limit
    }
}
