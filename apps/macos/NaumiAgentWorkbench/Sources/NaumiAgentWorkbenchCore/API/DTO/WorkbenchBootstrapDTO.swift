import Foundation

/// Startup payload returned by `GET /workbench/bootstrap`.
///
/// Lets the Mac app initialize daemon, capability, session, and snapshot state
/// from one local API call before subscribing to heavier refresh paths.
public struct WorkbenchBootstrapDTO: Decodable, Equatable, Sendable {
    public let daemonStatus: DaemonStatusDTO
    public let capabilities: CapabilitiesDTO
    public let sessions: [SessionDTO]
    public let totalSessions: Int
    public let selectedSessionID: String?
    public let snapshot: WorkbenchSnapshotDTO?

    public enum CodingKeys: String, CodingKey {
        case daemonStatus = "daemon_status"
        case capabilities
        case sessions
        case totalSessions = "total_sessions"
        case selectedSessionID = "selected_session_id"
        case snapshot
    }
}
