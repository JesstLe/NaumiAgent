import Foundation

/// Which daemon output stream a log line came from.
public enum DaemonLogSource: String, Sendable, Equatable {
    case stdout
    case stderr
}

/// One captured line of daemon output, already redacted of secrets.
public struct DaemonLogLine: Identifiable, Hashable, Sendable {
    public let id: UUID
    public let date: Date
    public let source: DaemonLogSource
    public let text: String

    public init(id: UUID = UUID(), date: Date = Date(), source: DaemonLogSource, text: String) {
        self.id = id
        self.date = date
        self.source = source
        self.text = text
    }
}

/// Masks secret-bearing fragments in raw daemon output before they are stored
/// or shown. Redaction is deliberately conservative: any key/value pair whose
/// key looks credential-like has its value replaced with `***`, and standalone
/// `Bearer <token>` schemes are masked too. Multi-word values (such as the
/// `Bearer abc123` after an `Authorization:` header) are masked to end of line.
public enum DaemonLogRedactor {
    /// Ordered (pattern, template) pairs. Earlier patterns run first so a
    /// header like `Authorization: Bearer abc123` is consumed before the
    /// standalone-bearer pattern would re-scan it.
    static let rules: [(NSRegularExpression, String)] = {
        func regex(_ pattern: String) -> NSRegularExpression {
            // swiftformat:disable:next forceTry
            try! NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
        }
        // Keyed secrets: "key<:|=> rest-of-line".
        let keyed = regex(#"(authorization|token|api[_-]?key|apikey|secret|password)(\s*[:=]\s*).+"#)
        // Standalone scheme word + token, e.g. a logged "Bearer abc123".
        let bearer = regex(#"bearer\s+\S+"#)
        return [
            (keyed, "$1$2***"),
            (bearer, "Bearer ***")
        ]
    }()

    public static func redact(_ text: String) -> String {
        var working = text
        for (regex, template) in rules {
            let range = NSRange(working.startIndex..., in: working)
            working = regex.stringByReplacingMatches(
                in: working,
                options: [],
                range: range,
                withTemplate: template
            )
        }
        return working
    }
}

/// Abstraction over the log ring buffer so the process controller can be tested
/// with an in-memory double that records appended lines.
public protocol DaemonLogStoring: Sendable {
    func append(_ line: DaemonLogLine) async
    func append(text: String, source: DaemonLogSource) async
    func lines() async -> [DaemonLogLine]
    func clear() async
}

/// Capped ring buffer of redacted daemon output lines. Newest lines are kept;
/// overflow drops the oldest. An actor serializes appends from the two output
/// pipes and reads from the UI.
public actor DaemonLogStore: DaemonLogStoring {
    public static let capacity: Int = 1000

    private var buffer: [DaemonLogLine] = []

    public init() {}

    public func append(_ line: DaemonLogLine) async {
        buffer.append(line)
        let overflow = buffer.count - Self.capacity
        if overflow > 0 {
            buffer.removeFirst(overflow)
        }
    }

    public func append(text: String, source: DaemonLogSource) async {
        // Redact at the boundary so secrets never enter the buffer.
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: true) {
            await append(
                DaemonLogLine(source: source, text: DaemonLogRedactor.redact(String(rawLine))))
        }
    }

    public func lines() async -> [DaemonLogLine] {
        buffer
    }

    public func clear() async {
        buffer.removeAll()
    }

    public var count: Int { buffer.count }
}
