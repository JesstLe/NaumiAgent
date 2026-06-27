import Foundation

/// Dashboard-level summary for the audit timeline page.
public struct TimelineDashboardPresentation: Equatable, Sendable {
    public let events: [TimelineEventPresentation]
    public let totalCount: Int
    public let actorCount: Int
    public let typeBuckets: [TimelineTypeBucket]
    public let latestEvent: TimelineEventPresentation?

    public init(events: [EventDTO]) {
        let presented = events.map(TimelineEventPresentation.init)
        self.events = presented
        self.totalCount = presented.count
        self.actorCount = Set(presented.map(\.actor)).count
        self.latestEvent = presented.last
        self.typeBuckets = Dictionary(grouping: presented, by: \.type)
            .map { type, events in
                TimelineTypeBucket(type: type, count: events.count)
            }
            .sorted { left, right in
                if left.count == right.count {
                    return left.type < right.type
                }
                return left.count > right.count
            }
    }
}

public struct TimelineTypeBucket: Equatable, Sendable, Identifiable {
    public var id: String { type }
    public let type: String
    public let count: Int
}
