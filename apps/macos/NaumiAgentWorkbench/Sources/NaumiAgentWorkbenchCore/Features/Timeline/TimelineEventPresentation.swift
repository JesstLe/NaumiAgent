import Foundation

/// Pure presentation model for a single row in the Timeline event list.
public struct TimelineEventPresentation: Equatable, Sendable, Identifiable {
    public let id: String
    public let type: String
    public let actor: String
    public let subjectID: String
    public let timestamp: String
    public let payloadSummary: String

    public init(event: EventDTO) {
        self.id = event.id
        self.type = event.type
        self.actor = event.actor
        self.subjectID = event.subjectID
        self.timestamp = event.timestamp
        self.payloadSummary = TimelineEventPresentation.compactSummary(for: event.payload)
    }

    /// Produces a compact, single-line summary of an event payload.
    ///
    /// - Object payloads are summarized as `key1=value1, key2=value2`.
    /// - Array payloads show the count and the first string item.
    /// - Scalar payloads render directly.
    /// - Empty payloads produce an empty string so the UI can omit the row.
    static func compactSummary(for payload: [String: JSONValue]) -> String {
        guard !payload.isEmpty else { return "" }

        let entries = payload.map { key, value -> String in
            "\(key)=\(TimelineEventPresentation.scalarSummary(of: value))"
        }
        .sorted()

        return entries.joined(separator: ", ")
    }

    private static func scalarSummary(of value: JSONValue) -> String {
        switch value {
        case .string(let text):
            return text
        case .number(let number):
            if number.rounded(.towardZero) == number,
               number >= Double(Int.min),
               number <= Double(Int.max) {
                return String(Int(number))
            }
            return String(number)
        case .bool(let flag):
            return flag ? "true" : "false"
        case .object(let object):
            return "{\(object.count)}"
        case .array(let array):
            if let first = array.first {
                return "[\(array.count)]: \(scalarSummary(of: first))"
            }
            return "[\(array.count)]"
        case .null:
            return "null"
        }
    }
}
