import Foundation

/// Dashboard-level summary for the audit timeline page.
public struct TimelineDashboardPresentation: Equatable, Sendable {
    public let events: [TimelineEventPresentation]
    public let totalCount: Int
    public let actorCount: Int
    public let typeBuckets: [TimelineTypeBucket]
    public let latestEvent: TimelineEventPresentation?
    public let actorBuckets: [TimelineActorBucket]
    public let causalChain: [TimelineCausalStep]

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
        self.actorBuckets = Dictionary(grouping: presented, by: \.actor)
            .map { actor, events in
                TimelineActorBucket(actor: actor, count: events.count)
            }
            .sorted { left, right in
                if left.count == right.count {
                    return left.actor < right.actor
                }
                return left.count > right.count
            }
        self.causalChain = presented
            .reversed()
            .prefix(6)
            .enumerated()
            .map { index, event in
                TimelineCausalStep(
                    order: index + 1,
                    eventID: event.id,
                    type: event.type,
                    actor: event.actor,
                    subjectID: event.subjectID,
                    timestamp: event.timestamp,
                    payloadSummary: event.payloadSummary
                )
            }
    }
}

public struct TimelineTypeBucket: Equatable, Sendable, Identifiable {
    public var id: String { type }
    public let type: String
    public let count: Int
}

public struct TimelineActorBucket: Equatable, Sendable, Identifiable {
    public var id: String { actor }
    public let actor: String
    public let count: Int
}

public struct TimelineCausalStep: Equatable, Sendable, Identifiable {
    public var id: String { eventID }
    public let order: Int
    public let eventID: String
    public let type: String
    public let actor: String
    public let subjectID: String
    public let timestamp: String
    public let payloadSummary: String
}
