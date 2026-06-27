import Foundation

/// REST client for the NaumiAgent Workbench Kernel.
///
/// SwiftUI 不直接读写 SQLite / 跑 git / pytest；所有业务状态通过此 client 访问本地 API。
public actor WorkbenchAPIClient: Sendable, WorkbenchAPIProviding {
    public let baseURL: URL
    public let session: URLSession

    /// - Parameters:
    ///   - baseURL: Default `http://127.0.0.1:8765/api/v1`.
    ///   - session: Inject a custom `URLSession` for previews/tests.
    public init(
        baseURL: URL = URL(string: "http://127.0.0.1:8765/api/v1/")!,
        session: URLSession = .shared
    ) {
        // Ensure the base URL ends with a slash so relative paths resolve correctly.
        let baseURLString = baseURL.absoluteString
        if baseURLString.hasSuffix("/") {
            self.baseURL = baseURL
        } else {
            self.baseURL = URL(string: baseURLString + "/")!
        }
        self.session = session
    }

    public func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO {
        try await get(path: "workbench/daemon/status")
    }

    public func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO {
        try await get(path: "workbench/capabilities")
    }

    public func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO {
        try await get(path: "workbench/sessions/\(sessionID)/snapshot")
    }

    public func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        try await get(path: "sessions?page=\(page)&page_size=\(pageSize)")
    }

    public func fetchEvents(sessionID: String, limit: Int) async throws(APIError) -> WorkbenchEventsDTO {
        try await get(path: "workbench/sessions/\(sessionID)/events?limit=\(limit)")
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
            path: "workbench/sessions/\(sessionID)/validation-runs",
            queryItems: queryItems
        )
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
            path: "workbench/sessions/\(sessionID)/context-snapshots",
            queryItems: queryItems
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
            path: "workbench/sessions/\(sessionID)/approvals",
            queryItems: queryItems
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
            path: "workbench/sessions/\(sessionID)/issues/\(taskID)/claim",
            body: body
        )
    }

    public func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        try await post(path: "workbench/sessions/\(sessionID)/leases/\(leaseID)/release")
    }

    public func expireLeases(sessionID: String) async throws(APIError) -> ExpiredLeasesDTO {
        try await post(path: "workbench/sessions/\(sessionID)/leases/expire")
    }

    public func createMission(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionDTO {
        let body = CreateMissionRequest(title: title, goal: goal)
        return try await post(
            path: "workbench/sessions/\(sessionID)/missions",
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
            path: "workbench/sessions/\(sessionID)/missions/\(missionID)/issues",
            body: body
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
            path: "workbench/sessions/\(sessionID)/missions/\(missionID)/intent-locks",
            body: body
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
            path: "workbench/sessions/\(sessionID)/missions/\(missionID)/decisions",
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
            path: "workbench/sessions/\(sessionID)/approvals/\(approvalID)/resolve",
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
            path: "workbench/sessions/\(sessionID)/validation-runs",
            body: body
        )
    }

    // MARK: - Private

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

    /// Builds an absolute URL from a relative path, percent-encoding any characters
    /// in the path segment that are not valid (spaces, Chinese characters, etc.).
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

        guard let encodedPath = pathPart.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) else {
            return nil
        }
        return URL(string: encodedPath + queryPart, relativeTo: baseURL)?.absoluteURL
    }

    private func performRequest<T: Decodable & Sendable>(_ request: URLRequest) async throws(APIError) -> T {
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

    /// Payload for `POST /workbench/sessions/{session_id}/issues/{task_id}/claim`.
    private struct ClaimIssueRequest: Encodable, Sendable {
        let agentID: String
        let durationMinutes: Int
        let worktreeName: String
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
