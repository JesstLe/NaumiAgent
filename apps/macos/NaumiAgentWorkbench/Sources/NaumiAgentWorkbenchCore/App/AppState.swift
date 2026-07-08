import Foundation
import Observation

/// Shared root state for the SwiftUI workbench shell.
@Observable
@MainActor
public final class AppState: Sendable {
    public var selectedWorkspace: String? = nil
    public var selectedSessionID: String? = nil
    public var currentRoute: AppRoute = .dashboard
    public var connectionState: ConnectionState = .disconnected
    public var daemonStatus: DaemonStatusDTO? = nil
    public var capabilities: CapabilitiesDTO? = nil

    /// Wall-clock time of the most recent connection health check, regardless
    /// of whether it succeeded. Drives the "last checked" display.
    public var lastHealthCheckAt: Date? = nil

    /// Ring buffer of recent connection-attempt outcomes. Newest entries are
    /// appended at the end; the buffer is capped to `connectionLogCapacity`.
    public var connectionLog: [ConnectionLogEntry] = []

    /// Maximum number of connection-log entries retained.
    public static let connectionLogCapacity: Int = 50

    /// Appends a connection-log entry and trims the buffer to the capacity,
    /// keeping the most recent entries.
    public func recordConnectionLog(
        state: ConnectionState,
        message: String? = nil,
        at date: Date = Date()
    ) {
        lastHealthCheckAt = date
        connectionLog.append(
            ConnectionLogEntry(date: date, state: state, message: message)
        )
        let overflow = connectionLog.count - Self.connectionLogCapacity
        if overflow > 0 {
            connectionLog.removeFirst(overflow)
        }
    }

    // MARK: - App-managed (supervised) daemon
    public var supervisedDaemonState: SupervisedDaemonState = .idle
    public var supervisedDaemonStatus: SupervisedDaemonStatus? = nil
    public var supervisedDaemonFailureMessage: String? = nil

    public var sessions: [SessionDTO] = []
    public var chatMessages: [ChatMessageDTO] = []
    public var snapshot: WorkbenchSnapshotDTO? = nil
    public var timelineEvents: [EventDTO] = []
    public var validationRuns: [ValidationRunDTO] = []
    public var contextSnapshots: [ContextSnapshotDTO] = []
    public var approvals: [ApprovalDTO] = []
    public var failures: [FailureDTO] = []
    public var issues: [IssueDTO] = []
    public var leases: [LeaseDTO] = []
    public var worktrees: [WorktreeDTO] = []
    public var missions: [MissionDTO] = []
    public var agentProfiles: [AgentProfileDTO] = []
    public var decisions: [DecisionDTO] = []
    public var intentLocks: [IntentLockDTO] = []
    public var selectedEvent: EventDTO? = nil
    public var selectedValidationRun: ValidationRunDTO? = nil
    public var selectedContextSnapshot: ContextSnapshotDTO? = nil
    public var selectedFailure: FailureDTO? = nil
    public var selectedIssue: IssueDTO? = nil
    public var selectedLease: LeaseDTO? = nil
    public var selectedWorktree: WorktreeDTO? = nil
    public var selectedMission: MissionDTO? = nil
    public var selectedAgentProfile: AgentProfileDTO? = nil
    public var selectedDecision: DecisionDTO? = nil
    public var selectedIntentLock: IntentLockDTO? = nil
    public var selectedApproval: ApprovalDTO? = nil
    public var lastError: APIError? = nil
    public var locale: AppLocale = .default
    public var isPreviewFixture: Bool = false

    public init() {}

    public enum ConnectionState: String, Equatable, Sendable, CaseIterable {
        case disconnected
        case connecting
        case connected
        case stale
        case authFailed
        case protocolMismatch

        public func displayName(locale: AppLocale) -> String {
            switch self {
            case .connected:
                return AppStrings.Connection.connected(locale)
            case .connecting:
                return AppStrings.Connection.connecting(locale)
            case .disconnected:
                return AppStrings.Connection.disconnected(locale)
            case .stale:
                return AppStrings.Connection.stale(locale)
            case .authFailed:
                return AppStrings.Connection.authFailed(locale)
            case .protocolMismatch:
                return AppStrings.Connection.protocolMismatch(locale)
            }
        }

        /// Whether this state represents a recoverable problem the user can act on.
        public var isFailure: Bool {
            switch self {
            case .disconnected, .authFailed, .protocolMismatch, .stale:
                return true
            case .connected, .connecting:
                return false
            }
        }

        /// Whether this state should hard-disable write actions in the UI.
        /// Only an incompatible protocol version makes writes unsafe; other
        /// failures simply leave no connected session to act on.
        public var disablesWrites: Bool {
            self == .protocolMismatch
        }
    }

    /// Whether the UI should allow write actions right now. False whenever the
    /// daemon speaks an incompatible protocol version.
    public var canPerformWrites: Bool {
        !connectionState.disablesWrites
    }
}

/// Lifecycle state of an app-supervised local daemon process.
public enum SupervisedDaemonState: String, Equatable, Sendable, CaseIterable {
    case idle
    case starting
    case running
    case stopping
    case failed
    case exited
}

/// Snapshot of a running supervised daemon: chosen port, pid, endpoint.
public struct SupervisedDaemonStatus: Equatable, Sendable {
    public let port: Int
    public let pid: Int32
    public let endpoint: URL
    public let startedAt: Date

    public init(port: Int, pid: Int32, endpoint: URL, startedAt: Date = Date()) {
        self.port = port
        self.pid = pid
        self.endpoint = endpoint
        self.startedAt = startedAt
    }
}
