import Foundation

/// One line in the user-facing connection log shown by the daemon health panel.
///
/// Each entry records the outcome of a single connection attempt: when it
/// happened, the resulting connection state, and an optional human-readable
/// note (typically the underlying error description). Entries are immutable.
public struct ConnectionLogEntry: Hashable, Sendable, Identifiable {
    public let id: UUID
    public let date: Date
    public let state: AppState.ConnectionState
    public let message: String?

    public init(
        id: UUID = UUID(),
        date: Date = Date(),
        state: AppState.ConnectionState,
        message: String? = nil
    ) {
        self.id = id
        self.date = date
        self.state = state
        self.message = message
    }
}
