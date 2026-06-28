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
    public var sessions: [SessionDTO] = []
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
    public var selectedEvent: EventDTO? = nil
    public var selectedValidationRun: ValidationRunDTO? = nil
    public var selectedContextSnapshot: ContextSnapshotDTO? = nil
    public var selectedFailure: FailureDTO? = nil
    public var selectedIssue: IssueDTO? = nil
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
            }
        }
    }
}
