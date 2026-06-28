import Foundation

/// REST client for the NaumiAgent Workbench Kernel.
///
/// SwiftUI 不直接读写 SQLite / 跑 git / pytest；所有业务状态通过此 client 访问本地 API。
public actor WorkbenchAPIClient: Sendable, WorkbenchAPIProviding {
    public let baseURL: URL
    public let session: URLSession
    private let bearerToken: String?

    /// - Parameters:
    ///   - baseURL: Default `http://127.0.0.1:8765/api/v1`.
    ///   - session: Inject a custom `URLSession` for previews/tests.
    ///   - bearerToken: Optional `Authorization: Bearer <token>` token.
    public init(
        baseURL: URL = URL(string: "http://127.0.0.1:8765/api/v1/")!,
        session: URLSession = .shared,
        bearerToken: String? = nil
    ) {
        // Ensure the base URL ends with a slash so relative paths resolve correctly.
        let baseURLString = baseURL.absoluteString
        if baseURLString.hasSuffix("/") {
            self.baseURL = baseURL
        } else {
            self.baseURL = URL(string: baseURLString + "/")!
        }
        self.session = session
        self.bearerToken = bearerToken
    }

    public func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO {
        try await get(path: "workbench/daemon/status")
    }

    public func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO {
        try await get(path: "workbench/capabilities")
    }

    public func fetchBootstrap(pageSize: Int = 1) async throws(APIError) -> WorkbenchBootstrapDTO {
        try await get(
            path: "workbench/bootstrap",
            queryItems: [URLQueryItem(name: "page_size", value: String(pageSize))]
        )
    }

    public func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "snapshot"))
    }

    public func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        try await get(path: "sessions?page=\(page)&page_size=\(pageSize)")
    }

    public func createSession(
        title: String?,
        model: String?,
        systemPrompt: String?
    ) async throws(APIError) -> SessionDTO {
        let body = CreateSessionRequest(
            title: title,
            systemPrompt: systemPrompt,
            model: model
        )
        return try await post(path: "sessions", body: body)
    }

    public func fetchEvents(
        sessionID: String,
        eventType: String? = nil,
        subjectID: String? = nil,
        actor: String? = nil,
        limit: Int
    ) async throws(APIError) -> WorkbenchEventsDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let eventType, !eventType.isEmpty {
            queryItems.append(URLQueryItem(name: "type", value: eventType))
        }
        if let subjectID, !subjectID.isEmpty {
            queryItems.append(URLQueryItem(name: "subject_id", value: subjectID))
        }
        if let actor, !actor.isEmpty {
            queryItems.append(URLQueryItem(name: "actor", value: actor))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "events"),
            queryItems: queryItems
        )
    }

    public func fetchEvent(sessionID: String, eventID: String) async throws(APIError) -> EventDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "events", eventID))
    }

    public func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let taskID, !taskID.isEmpty {
            queryItems.append(URLQueryItem(name: "task_id", value: taskID))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "validation-runs"),
            queryItems: queryItems
        )
    }

    public func fetchValidationRun(sessionID: String, runID: String) async throws(APIError) -> ValidationRunDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "validation-runs", runID))
    }

    public func fetchContextSnapshots(
        sessionID: String,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> ContextSnapshotsDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let taskID, !taskID.isEmpty {
            queryItems.append(URLQueryItem(name: "task_id", value: taskID))
        }
        if let agentID, !agentID.isEmpty {
            queryItems.append(URLQueryItem(name: "agent_id", value: agentID))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "context-snapshots"),
            queryItems: queryItems
        )
    }

    public func fetchContextSnapshot(sessionID: String, snapshotID: String) async throws(APIError) -> ContextSnapshotDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "context-snapshots", snapshotID))
    }

    public func recordContextHealth(
        sessionID: String,
        taskID: String,
        agentID: String,
        minutesSinceSync: Int,
        tokenLoadRatio: Double,
        policyConflict: Bool,
        actor: String
    ) async throws(APIError) -> ContextSnapshotDTO {
        let body = RecordContextHealthRequest(
            agentID: agentID,
            minutesSinceSync: minutesSinceSync,
            tokenLoadRatio: tokenLoadRatio,
            policyConflict: policyConflict,
            actor: actor
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "issues", taskID, "context-health"),
            body: body
        )
    }

    public func fetchApprovals(
        sessionID: String,
        state: String?,
        limit: Int
    ) async throws(APIError) -> ApprovalsDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let state, !state.isEmpty {
            queryItems.append(URLQueryItem(name: "state", value: state))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "approvals"),
            queryItems: queryItems
        )
    }

    public func fetchApproval(sessionID: String, approvalID: String) async throws(APIError) -> ApprovalDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "approvals", approvalID))
    }

    public func fetchFailures(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> FailuresDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let taskID, !taskID.isEmpty {
            queryItems.append(URLQueryItem(name: "task_id", value: taskID))
        }
        if let status, !status.isEmpty {
            queryItems.append(URLQueryItem(name: "status", value: status))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "failures"),
            queryItems: queryItems
        )
    }

    public func fetchFailure(sessionID: String, failureID: String) async throws(APIError) -> FailureDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "failures", failureID))
    }

    public func fetchIssues(
        sessionID: String,
        missionID: String?,
        riskLevel: String?,
        limit: Int
    ) async throws(APIError) -> IssuesDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let missionID, !missionID.isEmpty {
            queryItems.append(URLQueryItem(name: "mission_id", value: missionID))
        }
        if let riskLevel, !riskLevel.isEmpty {
            queryItems.append(URLQueryItem(name: "risk_level", value: riskLevel))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "issues"),
            queryItems: queryItems
        )
    }

    public func fetchIssue(sessionID: String, taskID: String) async throws(APIError) -> IssueDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "issues", taskID))
    }

    public func fetchLeases(
        sessionID: String,
        state: String?,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> LeasesDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let state, !state.isEmpty {
            queryItems.append(URLQueryItem(name: "state", value: state))
        }
        if let taskID, !taskID.isEmpty {
            queryItems.append(URLQueryItem(name: "task_id", value: taskID))
        }
        if let agentID, !agentID.isEmpty {
            queryItems.append(URLQueryItem(name: "agent_id", value: agentID))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "leases"),
            queryItems: queryItems
        )
    }

    public func fetchLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "leases", leaseID))
    }

    public func fetchWorktrees(
        sessionID: String,
        taskID: String?,
        status: String?,
        limit: Int
    ) async throws(APIError) -> WorktreesDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let taskID, !taskID.isEmpty {
            queryItems.append(URLQueryItem(name: "task_id", value: taskID))
        }
        if let status, !status.isEmpty {
            queryItems.append(URLQueryItem(name: "status", value: status))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "worktrees"),
            queryItems: queryItems
        )
    }

    public func fetchWorktree(sessionID: String, name: String) async throws(APIError) -> WorktreeDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "worktrees", name))
    }

    public func keepWorktree(
        sessionID: String,
        name: String,
        actor: String,
        reason: String
    ) async throws(APIError) -> WorktreeDTO {
        let body = KeepWorktreeRequest(actor: actor, reason: reason)
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "worktrees", name, "keep"),
            body: body
        )
    }

    public func removeWorktree(
        sessionID: String,
        name: String,
        discardChanges: Bool
    ) async throws(APIError) -> WorktreeRemovalDTO {
        try await delete(
            path: encodePath("workbench", "sessions", sessionID, "worktrees", name),
            queryItems: [URLQueryItem(name: "discard_changes", value: discardChanges ? "true" : "false")]
        )
    }

    public func fetchMissions(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> MissionsDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let status, !status.isEmpty {
            queryItems.append(URLQueryItem(name: "status", value: status))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "missions"),
            queryItems: queryItems
        )
    }

    public func fetchMission(sessionID: String, missionID: String) async throws(APIError) -> MissionDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "missions", missionID))
    }

    public func fetchAgentProfiles(
        sessionID: String,
        status: String?,
        limit: Int
    ) async throws(APIError) -> AgentProfilesDTO {
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let status, !status.isEmpty {
            queryItems.append(URLQueryItem(name: "status", value: status))
        }
        return try await get(
            path: encodePath("workbench", "sessions", sessionID, "agents"),
            queryItems: queryItems
        )
    }

    public func fetchAgentProfile(sessionID: String, agentID: String) async throws(APIError) -> AgentProfileDTO {
        try await get(path: encodePath("workbench", "sessions", sessionID, "agents", agentID))
    }

    public func registerAgentProfile(
        sessionID: String,
        agentID: String,
        name: String,
        role: String,
        capabilities: [String],
        permissions: [String],
        maxParallelTasks: Int,
        status: String,
        actor: String
    ) async throws(APIError) -> AgentProfileDTO {
        let body = RegisterAgentProfileRequest(
            name: name,
            role: role,
            capabilities: capabilities,
            permissions: permissions,
            maxParallelTasks: maxParallelTasks,
            status: status,
            actor: actor
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "agents", agentID),
            body: body
        )
    }

    public func claimIssue(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseDTO {
        let body = ClaimIssueRequest(
            agentID: agentID,
            durationMinutes: durationMinutes,
            worktreeName: worktreeName
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "issues", taskID, "claim"),
            body: body
        )
    }

    public func claimIssueWithSnapshot(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseSnapshotDTO {
        let body = ClaimIssueRequest(
            agentID: agentID,
            durationMinutes: durationMinutes,
            worktreeName: worktreeName
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "issues", taskID, "claim") + "?include_snapshot=true",
            body: body
        )
    }

    public func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        try await post(path: encodePath("workbench", "sessions", sessionID, "leases", leaseID, "release"))
    }

    public func expireLeases(sessionID: String) async throws(APIError) -> ExpiredLeasesDTO {
        try await post(path: encodePath("workbench", "sessions", sessionID, "leases", "expire"))
    }

    public func createMission(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionDTO {
        let body = CreateMissionRequest(title: title, goal: goal)
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "missions"),
            body: body
        )
    }

    public func attachIssue(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO {
        let body = AttachIssueRequest(
            taskID: taskID,
            acceptanceCriteria: acceptanceCriteria,
            parallelMode: parallelMode,
            riskLevel: riskLevel
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "issues"),
            body: body
        )
    }

    public func createIssue(
        sessionID: String,
        missionID: String,
        title: String,
        description: String,
        blockedBy: [String],
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO {
        let body = CreateIssueRequest(
            title: title,
            description: description,
            blockedBy: blockedBy,
            acceptanceCriteria: acceptanceCriteria,
            parallelMode: parallelMode,
            riskLevel: riskLevel
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "issues"),
            body: body
        )
    }

    public func fetchIntentLocks(
        sessionID: String,
        missionID: String
    ) async throws(APIError) -> IntentLocksDTO {
        try await get(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "intent-locks")
        )
    }

    public func fetchIntentLock(
        sessionID: String,
        missionID: String,
        lockID: String
    ) async throws(APIError) -> IntentLockDTO {
        try await get(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "intent-locks", lockID)
        )
    }

    public func createIntentLock(
        sessionID: String,
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async throws(APIError) -> IntentLockDTO {
        let body = CreateIntentLockRequest(
            actor: actor,
            rule: rule,
            blockedPaths: blockedPaths,
            allowedPaths: allowedPaths,
            requireProposalForRisk: requireProposalForRisk
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "intent-locks"),
            body: body
        )
    }

    public func fetchDecisions(
        sessionID: String,
        missionID: String
    ) async throws(APIError) -> DecisionsDTO {
        try await get(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "decisions")
        )
    }

    public func fetchDecision(
        sessionID: String,
        missionID: String,
        decisionID: String
    ) async throws(APIError) -> DecisionDTO {
        try await get(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "decisions", decisionID)
        )
    }

    public func createDecision(
        sessionID: String,
        missionID: String,
        kind: String,
        title: String,
        content: String,
        actor: String
    ) async throws(APIError) -> DecisionDTO {
        let body = CreateDecisionRequest(
            actor: actor,
            kind: kind,
            title: title,
            content: content
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "missions", missionID, "decisions"),
            body: body
        )
    }

    public func resolveApproval(
        sessionID: String,
        approvalID: String,
        actor: String,
        state: String,
        decisionNote: String
    ) async throws(APIError) -> ApprovalDTO {
        let body = ResolveApprovalRequest(
            actor: actor,
            state: state,
            decisionNote: decisionNote
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "approvals", approvalID, "resolve"),
            body: body
        )
    }

    public func runValidation(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultDTO {
        let body = RunValidationRequest(
            taskID: taskID,
            actor: actor,
            argv: argv,
            cwd: cwd
        )
        return try await post(
            path: encodePath("workbench", "sessions", sessionID, "validation-runs"),
            body: body
        )
    }

    // MARK: - Private

    /// Builds a relative path from individual components, percent-encoding each one
    /// separately so that `/` inside a dynamic ID becomes `%2F` instead of a route separator.
    private func encodePath(_ components: String...) -> String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        return components
            .map { $0.addingPercentEncoding(withAllowedCharacters: allowed) ?? $0 }
            .joined(separator: "/")
    }

    private func get<T: Decodable & Sendable>(path: String) async throws(APIError) -> T {
        guard let url = url(for: path) else {
            throw .invalidURL
        }

        return try await performRequest(URLRequest(url: url))
    }

    private func get<T: Decodable & Sendable>(
        path: String,
        queryItems: [URLQueryItem]
    ) async throws(APIError) -> T {
        guard let url = url(for: path),
              var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw .invalidURL
        }
        components.queryItems = queryItems
        guard let requestURL = components.url else {
            throw .invalidURL
        }

        return try await performRequest(URLRequest(url: requestURL))
    }

    private func post<T: Decodable & Sendable, B: Encodable & Sendable>(
        path: String,
        body: B
    ) async throws(APIError) -> T {
        guard let url = url(for: path) else {
            throw .invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        do {
            request.httpBody = try encoder.encode(body)
        } catch {
            throw .decodingFailed(String(describing: error))
        }

        return try await performRequest(request)
    }

    private func post<T: Decodable & Sendable>(path: String) async throws(APIError) -> T {
        guard let url = url(for: path) else {
            throw .invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        return try await performRequest(request)
    }

    private func delete<T: Decodable & Sendable>(
        path: String,
        queryItems: [URLQueryItem]
    ) async throws(APIError) -> T {
        guard let url = url(for: path),
              var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw .invalidURL
        }
        components.queryItems = queryItems
        guard let requestURL = components.url else {
            throw .invalidURL
        }

        var request = URLRequest(url: requestURL)
        request.httpMethod = "DELETE"
        return try await performRequest(request)
    }

    /// Builds an absolute URL from a relative path that is already percent-encoded.
    /// Query strings are preserved unchanged so existing callers such as
    /// `fetchSessions(page:pageSize:)` continue to work.
    private func url(for path: String) -> URL? {
        let pathPart: String
        let queryPart: String
        if let queryRange = path.range(of: "?") {
            pathPart = String(path[..<queryRange.lowerBound])
            queryPart = String(path[queryRange.lowerBound...])
        } else {
            pathPart = path
            queryPart = ""
        }

        return URL(string: pathPart + queryPart, relativeTo: baseURL)?.absoluteURL
    }

    private func performRequest<T: Decodable & Sendable>(_ request: URLRequest) async throws(APIError) -> T {
        var request = request
        if let token = bearerToken, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw .networkFailure(error.localizedDescription)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw .invalidResponse
        }

        guard (200..<300).contains(httpResponse.statusCode) else {
            throw .httpStatus(httpResponse.statusCode)
        }

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .useDefaultKeys
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw .decodingFailed(String(describing: error))
        }
    }

    /// Payload for `POST /sessions`.
    private struct CreateSessionRequest: Encodable, Sendable {
        let title: String?
        let systemPrompt: String?
        let model: String?
    }

    /// Payload for `POST /workbench/sessions/{session_id}/issues/{task_id}/claim`.
    private struct ClaimIssueRequest: Encodable, Sendable {
        let agentID: String
        let durationMinutes: Int
        let worktreeName: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/issues/{task_id}/context-health`.
    private struct RecordContextHealthRequest: Encodable, Sendable {
        let agentID: String
        let minutesSinceSync: Int
        let tokenLoadRatio: Double
        let policyConflict: Bool
        let actor: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/worktrees/{name}/keep`.
    private struct KeepWorktreeRequest: Encodable, Sendable {
        let actor: String
        let reason: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/missions`.
    private struct CreateMissionRequest: Encodable, Sendable {
        let title: String
        let goal: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/missions/{mission_id}/issues`.
    private struct AttachIssueRequest: Encodable, Sendable {
        let taskID: String
        let acceptanceCriteria: [String]
        let parallelMode: String
        let riskLevel: String
    }

    /// Payload for creating a new backing task and attaching it as an issue.
    private struct CreateIssueRequest: Encodable, Sendable {
        let title: String
        let description: String
        let blockedBy: [String]
        let acceptanceCriteria: [String]
        let parallelMode: String
        let riskLevel: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/agents/{agent_id}`.
    private struct RegisterAgentProfileRequest: Encodable, Sendable {
        let name: String
        let role: String
        let capabilities: [String]
        let permissions: [String]
        let maxParallelTasks: Int
        let status: String
        let actor: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/validation-runs`.
    private struct RunValidationRequest: Encodable, Sendable {
        let taskID: String
        let actor: String
        let argv: [String]
        let cwd: String?
    }

    /// Payload for `POST /workbench/sessions/{session_id}/missions/{mission_id}/intent-locks`.
    private struct CreateIntentLockRequest: Encodable, Sendable {
        let actor: String
        let rule: String
        let blockedPaths: [String]
        let allowedPaths: [String]
        let requireProposalForRisk: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/missions/{mission_id}/decisions`.
    private struct CreateDecisionRequest: Encodable, Sendable {
        let actor: String
        let kind: String
        let title: String
        let content: String
    }

    /// Payload for `POST /workbench/sessions/{session_id}/approvals/{approval_id}/resolve`.
    private struct ResolveApprovalRequest: Encodable, Sendable {
        let actor: String
        let state: String
        let decisionNote: String
    }
}
