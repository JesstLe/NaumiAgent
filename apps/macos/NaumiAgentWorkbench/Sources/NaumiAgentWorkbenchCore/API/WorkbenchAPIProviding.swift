import Foundation

/// Abstraction over the NaumiAgent Workbench REST API.
///
/// Allows `WorkbenchAPIClient` to be replaced by fakes in tests,
/// while keeping the real `URLSession` behavior in production.
public protocol WorkbenchAPIProviding: Sendable {
    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO
    func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO
    func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO
    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO

    /// Fetches the most recent audit events for the given session.
    func fetchEvents(sessionID: String, limit: Int) async throws(APIError) -> WorkbenchEventsDTO

    /// Claims an open issue for the given agent, creating a new lease.
    func claimIssue(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseDTO

    /// Releases an existing lease, returning the updated lease record.
    func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO
}
