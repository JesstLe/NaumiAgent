import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

final class MockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = MockURLProtocol.requestHandler else {
            fatalError("MockURLProtocol.requestHandler is not set")
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

@Suite(.serialized)
final class WorkbenchAPIClientTests {

    deinit {
        MockURLProtocol.requestHandler = nil
    }

    @Test func fetchCapabilities() async throws {
        let json = Data(
            """
            {"supports_daemon_management":false,"supports_workspace_registry":false,"supports_validation_runner":true,"supports_cloud_sync":false,"supported_locales":["zh-CN","en-US"],"protocol_version":1}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/capabilities" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let capabilities = try await client.fetchCapabilities()

        #expect(capabilities.protocolVersion == 1)
        #expect(capabilities.supportsValidationRunner)
        #expect(!capabilities.supportsDaemonManagement)
        #expect(!capabilities.supportsCloudSync)
        #expect(capabilities.supportedLocales == ["zh-CN", "en-US"])
    }

    @Test func fetchDaemonStatus() async throws {
        let json = Data(
            """
            {"status":"running","version":"0.1.0","pid":12345,"host":"127.0.0.1","port":8765,"started_at":"2026-06-27T06:00:00","workspace_count":3}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/daemon/status" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let status = try await client.fetchDaemonStatus()

        #expect(status.status == "running")
        #expect(status.version == "0.1.0")
        #expect(status.pid == 12345)
        #expect(status.host == "127.0.0.1")
        #expect(status.port == 8765)
        #expect(status.workspaceCount == 3)
    }

    @Test func fetchBootstrap() async throws {
        let json = Data(
            """
            {"daemon_status":{"status":"running","version":"0.1.0","pid":12345,"host":"127.0.0.1","port":8765,"started_at":"2026-06-27T06:00:00","workspace_count":3},"capabilities":{"supports_daemon_management":false,"supports_workspace_registry":false,"supports_validation_runner":true,"supports_cloud_sync":false,"supported_locales":["zh-CN","en-US"],"protocol_version":1},"sessions":[{"id":"sess-latest","title":"Mac 工作台","model":"gpt-5","created_at":"2026-06-27T08:00:00+00:00","updated_at":"2026-06-27T09:00:00+00:00","message_count":2,"total_tokens":128,"total_cost_usd":0.012,"status":"active"}],"total_sessions":1,"selected_session_id":"sess-latest","snapshot":{"session_id":"sess-latest","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/bootstrap?page_size=1" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let bootstrap = try await client.fetchBootstrap(pageSize: 1)

        #expect(bootstrap.daemonStatus.host == "127.0.0.1")
        #expect(bootstrap.capabilities.protocolVersion == 1)
        #expect(bootstrap.selectedSessionID == "sess-latest")
        #expect(bootstrap.totalSessions == 1)
        #expect(bootstrap.sessions.first?.id == "sess-latest")
        #expect(bootstrap.snapshot?.sessionID == "sess-latest")
    }

    @Test func httpErrorThrowsAPIError() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 404,
                httpVersion: nil,
                headerFields: nil
            )!
            return (response, Data())
        }

        let client = makeClient()
        await #expect(throws: APIError.httpStatus(404)) {
            try await client.fetchCapabilities()
        }
    }

    @Test func cannotConnectToHostThrowsNetworkFailure() async {
        MockURLProtocol.requestHandler = { _ in
            throw URLError(.cannotConnectToHost)
        }

        let client = makeClient()
        do {
            _ = try await client.fetchCapabilities()
            Issue.record("Expected fetchCapabilities() to throw")
        } catch {
            if case .networkFailure(let detail) = error {
                #expect(!detail.isEmpty)
            } else {
                Issue.record("Expected APIError.networkFailure, got \(error)")
            }
        }
    }

    @Test func fetchSessions() async throws {
        let json = Data(
            """
            {"sessions":[{"id":"sess-001","title":"Test Session","model":"gpt-4o","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00","message_count":5,"total_tokens":200,"total_cost_usd":0.002,"status":"active"}],"total":1,"page":1,"page_size":1}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/sessions?page=1&page_size=1" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let list = try await client.fetchSessions(page: 1, pageSize: 1)

        #expect(list.total == 1)
        #expect(list.page == 1)
        #expect(list.pageSize == 1)
        #expect(list.sessions.count == 1)

        let session = try #require(list.sessions.first)
        #expect(session.id == "sess-001")
        #expect(session.title == "Test Session")
        #expect(session.model == "gpt-4o")
        #expect(session.createdAt == "2026-06-27T06:00:00")
        #expect(session.updatedAt == "2026-06-27T06:30:00")
        #expect(session.messageCount == 5)
        #expect(session.totalTokens == 200)
        #expect(session.totalCostUSD == 0.002)
        #expect(session.status == "active")
    }

    @Test func createSessionUsesPOSTAndEncodesBody() async throws {
        let json = Data(
            """
            {"id":"sess-new","title":"Mac 工作台","model":"gpt-5","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","message_count":0,"total_tokens":0,"total_cost_usd":0.0,"status":"active"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/sessions" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard request.value(forHTTPHeaderField: "Content-Type") == "application/json" else {
                fatalError("Missing JSON content type")
            }
            guard let bodyData = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Missing body")
            }
            let body = try JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
            guard body?["title"] as? String == "Mac 工作台",
                  body?["model"] as? String == "gpt-5",
                  body?["system_prompt"] as? String == "你是本地工作台协调者" else {
                fatalError("Unexpected body: \(String(describing: body))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let session = try await client.createSession(
            title: "Mac 工作台",
            model: "gpt-5",
            systemPrompt: "你是本地工作台协调者"
        )

        #expect(session.id == "sess-new")
        #expect(session.title == "Mac 工作台")
        #expect(session.model == "gpt-5")
        #expect(session.messageCount == 0)
        #expect(session.totalTokens == 0)
    }

    @Test func fetchEvents() async throws {
        let json = Data(
            """
            {"events":[{"id":"evt-001","session_id":"sess-001","type":"mission.created","actor":"Human","subject_id":"mission-001","payload":{"title":"Mac Workbench"},"timestamp":"2026-06-27T06:00:00"}],"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/events?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchEvents(sessionID: "sess-001", limit: 50)

        #expect(response.limit == 50)
        #expect(response.events.count == 1)

        let event = try #require(response.events.first)
        #expect(event.id == "evt-001")
        #expect(event.sessionID == "sess-001")
        #expect(event.type == "mission.created")
        #expect(event.actor == "Human")
        #expect(event.subjectID == "mission-001")
        #expect(event.timestamp == "2026-06-27T06:00:00")
        #expect(event.payload == ["title": .string("Mac Workbench")])
    }

    @Test func fetchEventsWithFilters() async throws {
        let sessionID = "sess/中文"
        let eventType = "issue.claimed"
        let subjectID = "task/审查"
        let actor = "后端智能体"
        let json = Data(
            """
            {"events":[{"id":"evt-002","session_id":"sess/中文","type":"issue.claimed","actor":"后端智能体","subject_id":"task/审查","payload":{"lease_id":"lease-001"},"timestamp":"2026-06-27T06:10:00"}],"event_type":"issue.claimed","subject_id":"task/审查","actor":"后端智能体","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/events",
                  query["limit"] == "25",
                  query["type"] == eventType,
                  query["subject_id"] == subjectID,
                  query["actor"] == actor else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchEvents(
            sessionID: sessionID,
            eventType: eventType,
            subjectID: subjectID,
            actor: actor,
            limit: 25
        )

        #expect(response.eventType == eventType)
        #expect(response.subjectID == subjectID)
        #expect(response.actor == actor)
        #expect(response.limit == 25)

        let event = try #require(response.events.first)
        #expect(event.sessionID == sessionID)
        #expect(event.type == eventType)
        #expect(event.actor == actor)
        #expect(event.subjectID == subjectID)
        #expect(event.payload == ["lease_id": .string("lease-001")])
    }

    @Test func fetchValidationRunsWithTaskID() async throws {
        let taskID = "task 001/审查"
        let json = Data(
            """
            {"validation_runs":[{"id":"run-001","session_id":"sess-001","task_id":"task 001/审查","actor":"ValidationRunner","command":["pytest","test.py"],"cwd":"/workspace","status":"passed","exit_code":0,"output":"ok","started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:01"}],"task_id":"task 001/审查","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.path == "/api/v1/workbench/sessions/sess-001/validation-runs",
                  query["limit"] == "25",
                  query["task_id"] == taskID else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchValidationRuns(sessionID: "sess-001", taskID: taskID, limit: 25)

        #expect(response.taskID == taskID)
        #expect(response.limit == 25)
        #expect(response.validationRuns.count == 1)

        let run = try #require(response.validationRuns.first)
        #expect(run.id == "run-001")
        #expect(run.sessionID == "sess-001")
        #expect(run.taskID == taskID)
        #expect(run.actor == "ValidationRunner")
        #expect(run.command == ["pytest", "test.py"])
        #expect(run.cwd == "/workspace")
        #expect(run.status == "passed")
        #expect(run.exitCode == 0)
        #expect(run.output == "ok")
        #expect(run.startedAt == "2026-06-27T06:00:00")
        #expect(run.completedAt == "2026-06-27T06:00:01")
    }

    @Test func fetchValidationRunsWithoutTaskID() async throws {
        let json = Data(
            """
            {"validation_runs":[],"task_id":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/validation-runs?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchValidationRuns(sessionID: "sess-001", taskID: nil, limit: 50)

        #expect(response.taskID == nil)
        #expect(response.limit == 50)
        #expect(response.validationRuns.isEmpty)
    }

    @Test func fetchContextSnapshotsWithTaskIDAndAgentID() async throws {
        let taskID = "task 001/审查"
        let agentID = "agent 001/测试"
        let json = Data(
            """
            {"context_snapshots":[{"id":"snap-001","session_id":"sess-001","agent_id":"agent 001/测试","task_id":"task 001/审查","health":"good","reasons":["上下文健康"],"created_at":"2026-06-27T06:00:00"}],"task_id":"task 001/审查","agent_id":"agent 001/测试","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.path == "/api/v1/workbench/sessions/sess-001/context-snapshots",
                  query["limit"] == "25",
                  query["task_id"] == taskID,
                  query["agent_id"] == agentID else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchContextSnapshots(
            sessionID: "sess-001",
            taskID: taskID,
            agentID: agentID,
            limit: 25
        )

        #expect(response.taskID == taskID)
        #expect(response.agentID == agentID)
        #expect(response.limit == 25)
        #expect(response.contextSnapshots.count == 1)

        let snapshot = try #require(response.contextSnapshots.first)
        #expect(snapshot.id == "snap-001")
        #expect(snapshot.sessionID == "sess-001")
        #expect(snapshot.agentID == agentID)
        #expect(snapshot.taskID == taskID)
        #expect(snapshot.health == "good")
        #expect(snapshot.reasons == ["上下文健康"])
        #expect(snapshot.createdAt == "2026-06-27T06:00:00")
    }

    @Test func fetchContextSnapshotsWithoutOptionalFilters() async throws {
        let json = Data(
            """
            {"context_snapshots":[],"task_id":null,"agent_id":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/context-snapshots?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchContextSnapshots(
            sessionID: "sess-001",
            taskID: nil,
            agentID: nil,
            limit: 50
        )

        #expect(response.taskID == nil)
        #expect(response.agentID == nil)
        #expect(response.limit == 50)
        #expect(response.contextSnapshots.isEmpty)
    }

    @Test func recordContextHealthUsesPOSTAndEncodesPathAndBody() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/审查"
        let snapshotJSON = Data(
            """
            {"id":"snap-001","session_id":"sess/中文","agent_id":"agent-001","task_id":"task/审查","health":"good","reasons":["上下文健康"],"created_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/issues/task%2F%E5%AE%A1%E6%9F%A5/context-health" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["agent_id"] as? String == "agent-001",
                  json?["minutes_since_sync"] as? Int == 5,
                  json?["token_load_ratio"] as? Double == 0.75,
                  json?["policy_conflict"] as? Bool == false,
                  json?["actor"] as? String == "Human" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, snapshotJSON)
        }

        let client = makeClient()
        let snapshot = try await client.recordContextHealth(
            sessionID: sessionID,
            taskID: taskID,
            agentID: "agent-001",
            minutesSinceSync: 5,
            tokenLoadRatio: 0.75,
            policyConflict: false,
            actor: "Human"
        )

        #expect(snapshot.id == "snap-001")
        #expect(snapshot.sessionID == sessionID)
        #expect(snapshot.agentID == "agent-001")
        #expect(snapshot.taskID == taskID)
        #expect(snapshot.health == "good")
        #expect(snapshot.reasons == ["上下文健康"])
        #expect(snapshot.createdAt == "2026-06-27T06:00:00")
    }

    @Test func fetchApprovalsWithState() async throws {
        let sessionID = "sess 中文"
        let state = "waiting"
        let json = Data(
            """
            {"approvals":[{"id":"approval-001","session_id":"sess 中文","mission_id":"mission-001","task_id":"task-001","state":"waiting","title":"允许重构","detail":"保持测试通过","requester":"Agent-A","reviewer":"","decision_note":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}],"state":"waiting","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/approvals",
                  query["limit"] == "25",
                  query["state"] == state else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchApprovals(
            sessionID: sessionID,
            state: state,
            limit: 25
        )

        #expect(response.state == state)
        #expect(response.limit == 25)
        #expect(response.approvals.count == 1)

        let approval = try #require(response.approvals.first)
        #expect(approval.id == "approval-001")
        #expect(approval.sessionID == sessionID)
        #expect(approval.state == "waiting")
        #expect(approval.title == "允许重构")
    }

    @Test func fetchApprovalsWithoutState() async throws {
        let json = Data(
            """
            {"approvals":[],"state":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/approvals?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchApprovals(
            sessionID: "sess-001",
            state: nil,
            limit: 50
        )

        #expect(response.state == nil)
        #expect(response.limit == 50)
        #expect(response.approvals.isEmpty)
    }

    @Test func fetchFailuresWithFilters() async throws {
        let sessionID = "sess 中文"
        let taskID = "task 001/审查"
        let status = "open"
        let json = Data(
            """
            {"failures":[{"id":"failure-001","session_id":"sess 中文","task_id":"task 001/审查","kind":"test_failed","title":"测试失败","detail":"保持测试通过","source_id":"run-001","status":"open","created_at":"2026-06-27T06:00:00"}],"task_id":"task 001/审查","status":"open","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/failures",
                  query["limit"] == "25",
                  query["task_id"] == taskID,
                  query["status"] == status else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchFailures(
            sessionID: sessionID,
            taskID: taskID,
            status: status,
            limit: 25
        )

        #expect(response.taskID == taskID)
        #expect(response.status == status)
        #expect(response.limit == 25)
        #expect(response.failures.count == 1)

        let failure = try #require(response.failures.first)
        #expect(failure.id == "failure-001")
        #expect(failure.sessionID == sessionID)
        #expect(failure.taskID == taskID)
        #expect(failure.kind == "test_failed")
        #expect(failure.title == "测试失败")
        #expect(failure.detail == "保持测试通过")
        #expect(failure.sourceID == "run-001")
        #expect(failure.status == "open")
        #expect(failure.createdAt == "2026-06-27T06:00:00")
    }

    @Test func fetchFailuresWithoutFilters() async throws {
        let json = Data(
            """
            {"failures":[],"task_id":null,"status":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/failures?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchFailures(
            sessionID: "sess-001",
            taskID: nil,
            status: nil,
            limit: 50
        )

        #expect(response.taskID == nil)
        #expect(response.status == nil)
        #expect(response.limit == 50)
        #expect(response.failures.isEmpty)
    }

    @Test func fetchIssuesWithFilters() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let riskLevel = "high"
        let json = Data(
            """
            {"issues":[{"session_id":"sess 中文","task_id":"task-001","mission_id":"mission 中文","parallel_mode":"exclusive","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["通过测试"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}],"mission_id":"mission 中文","risk_level":"high","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/issues",
                  query["limit"] == "25",
                  query["mission_id"] == missionID,
                  query["risk_level"] == riskLevel else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchIssues(
            sessionID: sessionID,
            missionID: missionID,
            riskLevel: riskLevel,
            limit: 25
        )

        #expect(response.missionID == missionID)
        #expect(response.riskLevel == riskLevel)
        #expect(response.limit == 25)
        #expect(response.issues.count == 1)

        let issue = try #require(response.issues.first)
        #expect(issue.sessionID == sessionID)
        #expect(issue.taskID == "task-001")
        #expect(issue.missionID == missionID)
        #expect(issue.parallelMode == "exclusive")
        #expect(issue.riskLevel == "high")
        #expect(issue.acceptanceCriteria == ["通过测试"])
    }

    @Test func fetchIssuesWithoutFilters() async throws {
        let json = Data(
            """
            {"issues":[],"mission_id":null,"risk_level":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/issues?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchIssues(
            sessionID: "sess-001",
            missionID: nil,
            riskLevel: nil,
            limit: 50
        )

        #expect(response.missionID == nil)
        #expect(response.riskLevel == nil)
        #expect(response.limit == 50)
        #expect(response.issues.isEmpty)
    }

    @Test func fetchLeasesWithFilters() async throws {
        let sessionID = "sess 中文"
        let taskID = "task 001/审查"
        let agentID = "agent 001/测试"
        let state = "active"
        let json = Data(
            """
            {"leases":[{"id":"lease-001","session_id":"sess 中文","task_id":"task 001/审查","agent_id":"agent 001/测试","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}],"state":"active","task_id":"task 001/审查","agent_id":"agent 001/测试","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/leases",
                  query["limit"] == "25",
                  query["state"] == state,
                  query["task_id"] == taskID,
                  query["agent_id"] == agentID else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchLeases(
            sessionID: sessionID,
            state: state,
            taskID: taskID,
            agentID: agentID,
            limit: 25
        )

        #expect(response.state == state)
        #expect(response.taskID == taskID)
        #expect(response.agentID == agentID)
        #expect(response.limit == 25)
        #expect(response.leases.count == 1)

        let lease = try #require(response.leases.first)
        #expect(lease.id == "lease-001")
        #expect(lease.sessionID == sessionID)
        #expect(lease.taskID == taskID)
        #expect(lease.agentID == agentID)
        #expect(lease.state == state)
        #expect(lease.worktreeName == "wt-001")
    }

    @Test func fetchLeasesWithoutFilters() async throws {
        let json = Data(
            """
            {"leases":[],"state":null,"task_id":null,"agent_id":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/leases?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchLeases(
            sessionID: "sess-001",
            state: nil,
            taskID: nil,
            agentID: nil,
            limit: 50
        )

        #expect(response.state == nil)
        #expect(response.taskID == nil)
        #expect(response.agentID == nil)
        #expect(response.limit == 50)
        #expect(response.leases.isEmpty)
    }

    @Test func fetchMissionsWithStatus() async throws {
        let json = Data(
            """
            {"missions":[{"id":"mission-001","session_id":"sess-001","title":"Mac 工作台","goal":"补齐 API 调用面","status":"active","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}],"status":"active","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.path == "/api/v1/workbench/sessions/sess-001/missions",
                  query["limit"] == "25",
                  query["status"] == "active" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchMissions(sessionID: "sess-001", status: "active", limit: 25)

        #expect(response.status == "active")
        #expect(response.limit == 25)
        #expect(response.missions.count == 1)

        let mission = try #require(response.missions.first)
        #expect(mission.id == "mission-001")
        #expect(mission.sessionID == "sess-001")
        #expect(mission.title == "Mac 工作台")
        #expect(mission.goal == "补齐 API 调用面")
        #expect(mission.status == "active")
    }

    @Test func fetchMissionsWithoutStatus() async throws {
        let json = Data(
            """
            {"missions":[],"status":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchMissions(sessionID: "sess-001", status: nil, limit: 50)

        #expect(response.status == nil)
        #expect(response.limit == 50)
        #expect(response.missions.isEmpty)
    }

    @Test func fetchAgentProfilesWithStatus() async throws {
        let sessionID = "sess 中文"
        let status = "busy"
        let json = Data(
            """
            {"agent_profiles":[{"id":"agent-a","session_id":"sess 中文","name":"后端智能体","role":"coder","capabilities":["api","swift-client"],"permissions":["read","write"],"max_parallel_tasks":2,"status":"busy","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"}],"status":"busy","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/agents",
                  query["limit"] == "25",
                  query["status"] == status else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchAgentProfiles(
            sessionID: sessionID,
            status: status,
            limit: 25
        )

        #expect(response.status == status)
        #expect(response.limit == 25)
        #expect(response.agentProfiles.count == 1)

        let profile = try #require(response.agentProfiles.first)
        #expect(profile.id == "agent-a")
        #expect(profile.sessionID == sessionID)
        #expect(profile.name == "后端智能体")
        #expect(profile.role == "coder")
        #expect(profile.capabilities == ["api", "swift-client"])
        #expect(profile.permissions == ["read", "write"])
        #expect(profile.maxParallelTasks == 2)
        #expect(profile.status == status)
    }

    @Test func fetchAgentProfilesWithoutStatus() async throws {
        let json = Data(
            """
            {"agent_profiles":[],"status":null,"limit":50}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/agents?limit=50" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchAgentProfiles(sessionID: "sess-001", status: nil, limit: 50)

        #expect(response.status == nil)
        #expect(response.limit == 50)
        #expect(response.agentProfiles.isEmpty)
    }

    @Test func registerAgentProfileUsesPOSTAndEncodesPathAndBody() async throws {
        let sessionID = "sess/中文"
        let agentID = "agent/后端"
        let profileJSON = Data(
            """
            {"id":"agent/后端","session_id":"sess/中文","name":"后端智能体","role":"coder","capabilities":["api","swift-client"],"permissions":["read","write"],"max_parallel_tasks":2,"status":"busy","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/agents/agent%2F%E5%90%8E%E7%AB%AF" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["name"] as? String == "后端智能体",
                  json?["role"] as? String == "coder",
                  let capabilities = json?["capabilities"] as? [String],
                  capabilities == ["api", "swift-client"],
                  let permissions = json?["permissions"] as? [String],
                  permissions == ["read", "write"],
                  json?["max_parallel_tasks"] as? Int == 2,
                  json?["status"] as? String == "busy",
                  json?["actor"] as? String == "Human" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, profileJSON)
        }

        let client = makeClient()
        let profile = try await client.registerAgentProfile(
            sessionID: sessionID,
            agentID: agentID,
            name: "后端智能体",
            role: "coder",
            capabilities: ["api", "swift-client"],
            permissions: ["read", "write"],
            maxParallelTasks: 2,
            status: "busy",
            actor: "Human"
        )

        #expect(profile.id == agentID)
        #expect(profile.sessionID == sessionID)
        #expect(profile.name == "后端智能体")
        #expect(profile.role == "coder")
        #expect(profile.capabilities == ["api", "swift-client"])
        #expect(profile.permissions == ["read", "write"])
        #expect(profile.maxParallelTasks == 2)
        #expect(profile.status == "busy")
    }

    @Test func claimIssue() async throws {
        let leaseJSON = Data(
            """
            {"id":"lease-001","session_id":"sess-001","task_id":"task-001","agent_id":"agent-001","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/issues/task-001/claim" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["agent_id"] as? String == "agent-001",
                  json?["duration_minutes"] as? Int == 60,
                  json?["worktree_name"] as? String == "wt-001" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, leaseJSON)
        }

        let client = makeClient()
        let lease = try await client.claimIssue(
            sessionID: "sess-001",
            taskID: "task-001",
            agentID: "agent-001",
            durationMinutes: 60,
            worktreeName: "wt-001"
        )

        #expect(lease.id == "lease-001")
        #expect(lease.sessionID == "sess-001")
        #expect(lease.taskID == "task-001")
        #expect(lease.agentID == "agent-001")
        #expect(lease.state == "active")
        #expect(lease.worktreeName == "wt-001")
    }

    @Test func releaseLease() async throws {
        let leaseJSON = Data(
            """
            {"id":"lease-001","session_id":"sess-001","task_id":"task-001","agent_id":"agent-001","state":"released","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/leases/lease-001/release" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, leaseJSON)
        }

        let client = makeClient()
        let lease = try await client.releaseLease(sessionID: "sess-001", leaseID: "lease-001")

        #expect(lease.id == "lease-001")
        #expect(lease.state == "released")
    }

    @Test func expireLeasesUsesPOSTAndEncodesPath() async throws {
        let sessionID = "sess 中文"
        let responseJSON = Data(
            """
            {"expired":[{"id":"lease-001","session_id":"sess 中文","task_id":"task-001","agent_id":"agent-001","state":"expired","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}]}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/leases/expire" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient()
        let result = try await client.expireLeases(sessionID: sessionID)

        #expect(result.expired.count == 1)
        let lease = try #require(result.expired.first)
        #expect(lease.id == "lease-001")
        #expect(lease.sessionID == sessionID)
        #expect(lease.state == "expired")
    }

    @Test func createMission() async throws {
        let missionJSON = Data(
            """
            {"id":"mission-001","session_id":"sess-001","title":"Mac 工作台","goal":"补齐 API 调用面","status":"active","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["title"] as? String == "Mac 工作台",
                  json?["goal"] as? String == "补齐 API 调用面" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, missionJSON)
        }

        let client = makeClient()
        let mission = try await client.createMission(
            sessionID: "sess-001",
            title: "Mac 工作台",
            goal: "补齐 API 调用面"
        )

        #expect(mission.id == "mission-001")
        #expect(mission.sessionID == "sess-001")
        #expect(mission.title == "Mac 工作台")
        #expect(mission.goal == "补齐 API 调用面")
        #expect(mission.status == "active")
    }

    @Test func createMissionEncodesPathComponents() async throws {
        let sessionID = "sess 中文"
        let missionJSON = Data(
            """
            {"id":"mission-002","session_id":"sess 中文","title":"Title","goal":"Goal","status":"active","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, missionJSON)
        }

        let client = makeClient()
        let mission = try await client.createMission(
            sessionID: sessionID,
            title: "Title",
            goal: "Goal"
        )

        #expect(mission.sessionID == sessionID)
    }

    @Test func attachIssue() async throws {
        let issueJSON = Data(
            """
            {"session_id":"sess-001","task_id":"task-001","mission_id":"mission-001","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":false,"acceptance_criteria":["通过 Swift 编译"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions/mission-001/issues" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["task_id"] as? String == "task-001",
                  let criteria = json?["acceptance_criteria"] as? [String],
                  criteria == ["通过 Swift 编译"],
                  json?["parallel_mode"] as? String == "exclusive",
                  json?["risk_level"] as? String == "medium" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, issueJSON)
        }

        let client = makeClient()
        let issue = try await client.attachIssue(
            sessionID: "sess-001",
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: ["通过 Swift 编译"],
            parallelMode: "exclusive",
            riskLevel: "medium"
        )

        #expect(issue.sessionID == "sess-001")
        #expect(issue.taskID == "task-001")
        #expect(issue.missionID == "mission-001")
        #expect(issue.parallelMode == "exclusive")
        #expect(issue.riskLevel == "medium")
        #expect(issue.acceptanceCriteria == ["通过 Swift 编译"])
    }

    @Test func createIssueUsesPOSTAndEncodesBackingTaskFields() async throws {
        let issueJSON = Data(
            """
            {"session_id":"sess-001","task_id":"task-009","mission_id":"mission-001","parallel_mode":"cooperative","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["dashboard 刷新后可见","可被 Agent claim"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions/mission-001/issues" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["title"] as? String == "实现 Issue 创建 API",
                  json?["description"] as? String == "创建 backing task 并绑定 metadata",
                  let blockedBy = json?["blocked_by"] as? [String],
                  blockedBy == ["1"],
                  let criteria = json?["acceptance_criteria"] as? [String],
                  criteria == ["dashboard 刷新后可见", "可被 Agent claim"],
                  json?["parallel_mode"] as? String == "cooperative",
                  json?["risk_level"] as? String == "high" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, issueJSON)
        }

        let client = makeClient()
        let issue = try await client.createIssue(
            sessionID: "sess-001",
            missionID: "mission-001",
            title: "实现 Issue 创建 API",
            description: "创建 backing task 并绑定 metadata",
            blockedBy: ["1"],
            acceptanceCriteria: ["dashboard 刷新后可见", "可被 Agent claim"],
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(issue.sessionID == "sess-001")
        #expect(issue.taskID == "task-009")
        #expect(issue.missionID == "mission-001")
        #expect(issue.parallelMode == "cooperative")
        #expect(issue.riskLevel == "high")
    }

    @Test func attachIssueEncodesPathComponentsAndBody() async throws {
        let missionID = "mission 中文"
        let taskID = "task 001/审查"
        let issueJSON = Data(
            """
            {"session_id":"sess-001","task_id":"task 001/审查","mission_id":"mission 中文","parallel_mode":"cooperative","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["criteria 1"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions/mission%20%E4%B8%AD%E6%96%87/issues" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["task_id"] as? String == taskID,
                  let criteria = json?["acceptance_criteria"] as? [String],
                  criteria == ["criteria 1"] else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, issueJSON)
        }

        let client = makeClient()
        let issue = try await client.attachIssue(
            sessionID: "sess-001",
            missionID: missionID,
            taskID: taskID,
            acceptanceCriteria: ["criteria 1"],
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(issue.missionID == missionID)
        #expect(issue.taskID == taskID)
    }

    @Test func runValidationUsesPOSTAndEncodesPath() async throws {
        let sessionID = "sess 中文"
        let resultJSON = Data(
            """
            {"id":"run-001","status":"passed","exit_code":0,"output":"ok"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/validation-runs" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["task_id"] as? String == "task-001",
                  json?["actor"] as? String == "Human",
                  let argv = json?["argv"] as? [String],
                  argv == ["pytest"],
                  json?["cwd"] as? String == "/workspace" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, resultJSON)
        }

        let client = makeClient()
        let result = try await client.runValidation(
            sessionID: sessionID,
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(result.id == "run-001")
        #expect(result.status == "passed")
        #expect(result.exitCode == 0)
        #expect(result.output == "ok")
    }

    @Test func createIntentLockUsesPOSTAndEncodesPathAndBody() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let lockJSON = Data(
            """
            {"id":"lock-001","session_id":"sess 中文","mission_id":"mission 中文","rule":"禁止修改 core 模块","blocked_paths":["src/secret"],"allowed_paths":["src/secret/README.md"],"require_proposal_for_risk":"high","active":true,"created_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/intent-locks" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["actor"] as? String == "Planner-Agent",
                  json?["rule"] as? String == "禁止修改 core 模块",
                  let blockedPaths = json?["blocked_paths"] as? [String],
                  blockedPaths == ["src/secret"],
                  let allowedPaths = json?["allowed_paths"] as? [String],
                  allowedPaths == ["src/secret/README.md"],
                  json?["require_proposal_for_risk"] as? String == "high" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, lockJSON)
        }

        let client = makeClient()
        let lock = try await client.createIntentLock(
            sessionID: sessionID,
            missionID: missionID,
            actor: "Planner-Agent",
            rule: "禁止修改 core 模块",
            blockedPaths: ["src/secret"],
            allowedPaths: ["src/secret/README.md"],
            requireProposalForRisk: "high"
        )

        #expect(lock.id == "lock-001")
        #expect(lock.sessionID == sessionID)
        #expect(lock.missionID == missionID)
        #expect(lock.rule == "禁止修改 core 模块")
        #expect(lock.blockedPaths == ["src/secret"])
        #expect(lock.allowedPaths == ["src/secret/README.md"])
        #expect(lock.requireProposalForRisk == "high")
        #expect(lock.active == true)
        #expect(lock.createdAt == "2026-06-27T06:00:00")
    }

    @Test func createDecisionUsesPOSTAndEncodesPathAndBody() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let decisionJSON = Data(
            """
            {"id":"decision-001","session_id":"sess 中文","mission_id":"mission 中文","kind":"architecture","title":"采用 FastAPI","content":"使用 FastAPI 承载 Workbench API","actor":"Planner-Agent","created_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/decisions" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["actor"] as? String == "Planner-Agent",
                  json?["kind"] as? String == "architecture",
                  json?["title"] as? String == "采用 FastAPI",
                  json?["content"] as? String == "使用 FastAPI 承载 Workbench API" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, decisionJSON)
        }

        let client = makeClient()
        let decision = try await client.createDecision(
            sessionID: sessionID,
            missionID: missionID,
            kind: "architecture",
            title: "采用 FastAPI",
            content: "使用 FastAPI 承载 Workbench API",
            actor: "Planner-Agent"
        )

        #expect(decision.id == "decision-001")
        #expect(decision.sessionID == sessionID)
        #expect(decision.missionID == missionID)
        #expect(decision.kind == "architecture")
        #expect(decision.title == "采用 FastAPI")
        #expect(decision.content == "使用 FastAPI 承载 Workbench API")
        #expect(decision.actor == "Planner-Agent")
        #expect(decision.createdAt == "2026-06-27T06:00:00")
    }

    @Test func resolveApprovalUsesPOSTAndEncodesPathAndBody() async throws {
        let sessionID = "sess 中文"
        let approvalID = "approval 001 审批"
        let approvalJSON = Data(
            """
            {"id":"approval 001 审批","session_id":"sess 中文","mission_id":"mission-001","task_id":"task-001","state":"approved","title":"允许重构","detail":"保持测试通过","requester":"Agent-A","reviewer":"Human","decision_note":"同意","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:01"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/approvals/approval%20001%20%E5%AE%A1%E6%89%B9/resolve" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["actor"] as? String == "Human",
                  json?["state"] as? String == "approved",
                  json?["decision_note"] as? String == "同意" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, approvalJSON)
        }

        let client = makeClient()
        let approval = try await client.resolveApproval(
            sessionID: sessionID,
            approvalID: approvalID,
            actor: "Human",
            state: "approved",
            decisionNote: "同意"
        )

        #expect(approval.id == approvalID)
        #expect(approval.sessionID == sessionID)
        #expect(approval.missionID == "mission-001")
        #expect(approval.taskID == "task-001")
        #expect(approval.state == "approved")
        #expect(approval.title == "允许重构")
        #expect(approval.detail == "保持测试通过")
        #expect(approval.requester == "Agent-A")
        #expect(approval.reviewer == "Human")
        #expect(approval.decisionNote == "同意")
        #expect(approval.createdAt == "2026-06-27T06:00:00")
        #expect(approval.updatedAt == "2026-06-27T06:00:01")
    }

    @Test func fetchSnapshotEncodesSlashInSessionID() async throws {
        let sessionID = "sess/中文"
        let snapshotJSON = Data(
            """
            {"session_id":"sess/中文","missions":[],"tasks":[],"issues":[],"failures":[],"events":[]}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/snapshot" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, snapshotJSON)
        }

        let client = makeClient()
        let snapshot = try await client.fetchSnapshot(sessionID: sessionID)

        #expect(snapshot.sessionID == sessionID)
        #expect(snapshot.missions.isEmpty)
        #expect(snapshot.tasks.isEmpty)
        #expect(snapshot.issues.isEmpty)
        #expect(snapshot.failures.isEmpty)
        #expect(snapshot.events.isEmpty)
    }

    @Test func claimIssueEncodesSlashInPathComponents() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/审查"
        let leaseJSON = Data(
            """
            {"id":"lease-001","session_id":"sess/中文","task_id":"task/审查","agent_id":"agent-001","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/issues/task%2F%E5%AE%A1%E6%9F%A5/claim" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["agent_id"] as? String == "agent-001",
                  json?["duration_minutes"] as? Int == 60,
                  json?["worktree_name"] as? String == "wt-001" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, leaseJSON)
        }

        let client = makeClient()
        let lease = try await client.claimIssue(
            sessionID: sessionID,
            taskID: taskID,
            agentID: "agent-001",
            durationMinutes: 60,
            worktreeName: "wt-001"
        )

        #expect(lease.sessionID == sessionID)
        #expect(lease.taskID == taskID)
    }

    @Test func resolveApprovalEncodesSlashInPathComponents() async throws {
        let sessionID = "sess/中文"
        let approvalID = "approval/人工"
        let approvalJSON = Data(
            """
            {"id":"approval/人工","session_id":"sess/中文","mission_id":"mission-001","task_id":"task-001","state":"approved","title":"允许重构","detail":"保持测试通过","requester":"Agent-A","reviewer":"Human","decision_note":"同意","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:01"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/approvals/approval%2F%E4%BA%BA%E5%B7%A5/resolve" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["actor"] as? String == "Human",
                  json?["state"] as? String == "approved",
                  json?["decision_note"] as? String == "同意" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, approvalJSON)
        }

        let client = makeClient()
        let approval = try await client.resolveApproval(
            sessionID: sessionID,
            approvalID: approvalID,
            actor: "Human",
            state: "approved",
            decisionNote: "同意"
        )

        #expect(approval.id == approvalID)
        #expect(approval.sessionID == sessionID)
    }

    @Test func bearerTokenIsSentOnGET() async throws {
        let json = Data(
            """
            {"supports_daemon_management":false,"supports_workspace_registry":false,"supports_validation_runner":true,"supports_cloud_sync":false,"supported_locales":["zh-CN","en-US"],"protocol_version":1}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token" else {
                fatalError("Missing or incorrect Authorization header")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient(bearerToken: "test-token")
        let capabilities = try await client.fetchCapabilities()

        #expect(capabilities.protocolVersion == 1)
    }

    @Test func bearerTokenIsSentOnPOST() async throws {
        let leaseJSON = Data(
            """
            {"id":"lease-001","session_id":"sess-001","task_id":"task-001","agent_id":"agent-001","state":"released","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token" else {
                fatalError("Missing or incorrect Authorization header")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, leaseJSON)
        }

        let client = makeClient(bearerToken: "test-token")
        let lease = try await client.releaseLease(sessionID: "sess-001", leaseID: "lease-001")

        #expect(lease.id == "lease-001")
    }

    @Test func nilBearerTokenOmitsAuthorization() async throws {
        let json = Data(
            """
            {"supports_daemon_management":false,"supports_workspace_registry":false,"supports_validation_runner":true,"supports_cloud_sync":false,"supported_locales":["zh-CN","en-US"],"protocol_version":1}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.value(forHTTPHeaderField: "Authorization") == nil else {
                fatalError("Authorization header should not be set when bearerToken is nil")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let capabilities = try await client.fetchCapabilities()

        #expect(capabilities.protocolVersion == 1)
    }

    @Test func emptyBearerTokenOmitsAuthorization() async throws {
        let json = Data(
            """
            {"supports_daemon_management":false,"supports_workspace_registry":false,"supports_validation_runner":true,"supports_cloud_sync":false,"supported_locales":["zh-CN","en-US"],"protocol_version":1}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.value(forHTTPHeaderField: "Authorization") == nil else {
                fatalError("Authorization header should not be set when bearerToken is empty")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient(bearerToken: "")
        let capabilities = try await client.fetchCapabilities()

        #expect(capabilities.protocolVersion == 1)
    }

    // MARK: - Helpers

    private func makeClient(bearerToken: String? = nil) -> WorkbenchAPIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: configuration)
        return WorkbenchAPIClient(session: session, bearerToken: bearerToken)
    }

    @Test func fetchIntentLocksEncodesPathAndDecodesResponse() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let json = Data(
            """
            {"intent_locks":[{"id":"lock-001","session_id":"sess 中文","mission_id":"mission 中文","rule":"禁止修改 core 模块","blocked_paths":["src/secret"],"allowed_paths":["src/secret/README.md"],"require_proposal_for_risk":"high","active":true,"created_at":"2026-06-27T06:00:00"}],"mission_id":"mission 中文"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/intent-locks" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchIntentLocks(sessionID: sessionID, missionID: missionID)

        #expect(response.missionID == missionID)
        #expect(response.intentLocks.count == 1)

        let lock = try #require(response.intentLocks.first)
        #expect(lock.id == "lock-001")
        #expect(lock.sessionID == sessionID)
        #expect(lock.missionID == missionID)
        #expect(lock.rule == "禁止修改 core 模块")
        #expect(lock.blockedPaths == ["src/secret"])
        #expect(lock.allowedPaths == ["src/secret/README.md"])
        #expect(lock.requireProposalForRisk == "high")
        #expect(lock.active == true)
        #expect(lock.createdAt == "2026-06-27T06:00:00")
    }

    @Test func fetchDecisionsEncodesPathAndDecodesResponse() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let json = Data(
            """
            {"decisions":[{"id":"decision-001","session_id":"sess 中文","mission_id":"mission 中文","kind":"architecture","title":"采用 FastAPI","content":"使用 FastAPI 承载 Workbench API","actor":"Planner-Agent","created_at":"2026-06-27T06:00:00"}],"mission_id":"mission 中文"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/decisions" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchDecisions(sessionID: sessionID, missionID: missionID)

        #expect(response.missionID == missionID)
        #expect(response.decisions.count == 1)

        let decision = try #require(response.decisions.first)
        #expect(decision.id == "decision-001")
        #expect(decision.sessionID == sessionID)
        #expect(decision.missionID == missionID)
        #expect(decision.kind == "architecture")
        #expect(decision.title == "采用 FastAPI")
        #expect(decision.content == "使用 FastAPI 承载 Workbench API")
        #expect(decision.actor == "Planner-Agent")
        #expect(decision.createdAt == "2026-06-27T06:00:00")
    }

    @Test func fetchIntentLocksEncodesSlashInPathComponents() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/审查"
        let json = Data(
            """
            {"intent_locks":[],"mission_id":"mission/审查"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E5%AE%A1%E6%9F%A5/intent-locks" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchIntentLocks(sessionID: sessionID, missionID: missionID)

        #expect(response.missionID == missionID)
        #expect(response.intentLocks.isEmpty)
    }

    @Test func fetchDecisionsEncodesSlashInPathComponents() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/审查"
        let json = Data(
            """
            {"decisions":[],"mission_id":"mission/审查"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E5%AE%A1%E6%9F%A5/decisions" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "GET" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        let response = try await client.fetchDecisions(sessionID: sessionID, missionID: missionID)

        #expect(response.missionID == missionID)
        #expect(response.decisions.isEmpty)
    }
}

private extension InputStream {
    func httpBodyStreamData() -> Data? {
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        open()
        defer { close() }
        while hasBytesAvailable {
            let bytesRead = read(&buffer, maxLength: buffer.count)
            if bytesRead > 0 {
                data.append(buffer, count: bytesRead)
            } else {
                break
            }
        }
        return data.isEmpty ? nil : data
    }
}
