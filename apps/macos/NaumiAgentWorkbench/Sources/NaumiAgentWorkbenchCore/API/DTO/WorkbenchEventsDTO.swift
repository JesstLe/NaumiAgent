import Foundation

/// Paginated audit events returned by `GET /workbench/sessions/{id}/events`.
public struct WorkbenchEventsDTO: Decodable, Equatable, Sendable {
    public let events: [EventDTO]
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case events
        case limit
    }

    public init(events: [EventDTO], limit: Int) {
        self.events = events
        self.limit = limit
    }
}
