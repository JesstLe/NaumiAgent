import Foundation

/// User-entered audit-event filters used by the Timeline page.
public struct TimelineEventFilterDraft: Equatable, Sendable {
    public var eventType: String
    public var actor: String
    public var subjectID: String
    public var since: String
    public var severity: String

    public init(
        eventType: String = "",
        actor: String = "",
        subjectID: String = "",
        since: String = "",
        severity: String = ""
    ) {
        self.eventType = eventType
        self.actor = actor
        self.subjectID = subjectID
        self.since = since
        self.severity = severity
    }

    public var trimmedEventType: String {
        eventType.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedActor: String {
        actor.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedSubjectID: String {
        subjectID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedSince: String {
        since.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedSeverity: String {
        severity.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var eventTypeQueryValue: String? {
        trimmedEventType.isEmpty ? nil : trimmedEventType
    }

    public var actorQueryValue: String? {
        trimmedActor.isEmpty ? nil : trimmedActor
    }

    public var subjectIDQueryValue: String? {
        trimmedSubjectID.isEmpty ? nil : trimmedSubjectID
    }

    public var sinceQueryValue: String? {
        trimmedSince.isEmpty ? nil : trimmedSince
    }

    public var severityQueryValue: String? {
        trimmedSeverity.isEmpty ? nil : trimmedSeverity
    }

    public var hasFilters: Bool {
        eventTypeQueryValue != nil
            || actorQueryValue != nil
            || subjectIDQueryValue != nil
            || sinceQueryValue != nil
            || severityQueryValue != nil
    }
}
