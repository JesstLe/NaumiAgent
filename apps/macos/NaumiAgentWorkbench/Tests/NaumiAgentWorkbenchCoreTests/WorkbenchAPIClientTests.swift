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
            {"supports_daemon_management":false,"supports_workspace_registry":false,"supports_validation_runner":true,"supports_event_stream":true,"supports_cloud_sync":false,"supported_locales":["zh-CN","en-US"],"default_locale":"zh-CN","protocol_version":1,"supported_resources":["snapshot","missions","messages"],"supported_actions":["create_session","send_message","send_message_with_issue"],"route_templates":{"create_session":"/workbench/sessions","send_message":"/sessions/{session_id}/messages","send_message_with_issue":"/sessions/{session_id}/messages"}}
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
        #expect(capabilities.supportsEventStream)
        #expect(!capabilities.supportsDaemonManagement)
        #expect(!capabilities.supportsCloudSync)
        #expect(capabilities.supportedLocales == ["zh-CN", "en-US"])
        #expect(capabilities.defaultLocale == "zh-CN")
        #expect(capabilities.supportedResources == ["snapshot", "missions", "messages"])
        #expect(capabilities.supportedActions == [
            "create_session",
            "send_message",
            "send_message_with_issue",
        ])
        #expect(capabilities.routeTemplate(for: "send_message_with_issue") == "/sessions/{session_id}/messages")
        #expect(capabilities.supportsAction("send_message_with_issue"))
        #expect(!capabilities.supportsAction("unknown_action"))
    }

    @Test func fetchCapabilitiesDefaultsToChineseForLegacyDaemon() async throws {
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

        #expect(capabilities.defaultLocale == "zh-CN")
        #expect(capabilities.supportsEventStream)
        #expect(capabilities.supportedResources == [])
        #expect(capabilities.supportedActions == [])
        #expect(capabilities.routeTemplates == [:])
    }

    @Test func fetchDaemonStatus() async throws {
        let json = Data(
            """
            {"status":"running","version":"0.1.0","pid":12345,"host":"127.0.0.1","port":8765,"started_at":"2026-06-27T06:00:00","workspace_count":3,"workspace_root":"/Users/lv/Workspace/NaumiAgent","workspace_name":"NaumiAgent","api_base_url":"http://127.0.0.1:8765/api/v1","workbench_base_url":"http://127.0.0.1:8765/api/v1/workbench","event_stream_url_template":"ws://127.0.0.1:8765/api/v1/workbench/sessions/{session_id}/events/stream","auth_mode":"dev_token"}
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
        #expect(status.workspaceRoot == "/Users/lv/Workspace/NaumiAgent")
        #expect(status.workspaceName == "NaumiAgent")
        #expect(status.apiBaseURL == "http://127.0.0.1:8765/api/v1")
        #expect(status.workbenchBaseURL == "http://127.0.0.1:8765/api/v1/workbench")
        #expect(status.eventStreamURLTemplate == "ws://127.0.0.1:8765/api/v1/workbench/sessions/{session_id}/events/stream")
        #expect(status.authMode == "dev_token")
    }

    @Test func fetchDaemonStatusDefaultsMacFieldsForLegacyDaemon() async throws {
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

        #expect(status.workspaceRoot == "")
        #expect(status.workspaceName == "")
        #expect(status.apiBaseURL == "http://127.0.0.1:8765/api/v1")
        #expect(status.workbenchBaseURL == "http://127.0.0.1:8765/api/v1/workbench")
        #expect(status.eventStreamURLTemplate == "ws://127.0.0.1:8765/api/v1/workbench/sessions/{session_id}/events/stream")
        #expect(status.authMode == "unknown")
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

    @Test func sessionNotFoundHTTPDetailThrowsSessionUnavailable() async {
        let json = Data(#"{"detail":"Session not found"}"#.utf8)
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 404,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        await #expect(throws: APIError.sessionUnavailable) {
            try await client.fetchSnapshot(sessionID: "missing-session")
        }
    }

    @Test func serverHTTPDetailThrowsServerError() async {
        let json = Data(#"{"detail":"session registry unavailable"}"#.utf8)
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 503,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient()
        await #expect(throws: APIError.serverError(statusCode: 503, detail: "session registry unavailable")) {
            try await client.fetchSessions(page: 1, pageSize: 20)
        }
    }

    @Test func unauthorizedHTTPStatusThrowsAuthFailed() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 401,
                httpVersion: nil,
                headerFields: nil
            )!
            return (response, Data())
        }

        let client = makeClient()
        await #expect(throws: APIError.authFailed) {
            try await client.fetchCapabilities()
        }
    }

    @Test func forbiddenHTTPStatusThrowsAuthFailed() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 403,
                httpVersion: nil,
                headerFields: nil
            )!
            return (response, Data())
        }

        let client = makeClient()
        await #expect(throws: APIError.authFailed) {
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
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions?page=1&page_size=1" else {
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

    @Test func createWorkbenchSessionUsesWorkbenchPOSTAndDecodesBootstrap() async throws {
        let json = Data(
            """
            {"daemon_status":{"status":"running","version":"0.1.0","pid":12345,"host":"127.0.0.1","port":8765,"started_at":"2026-06-27T06:00:00","workspace_count":3},"capabilities":{"supports_daemon_management":false,"supports_workspace_registry":false,"supports_validation_runner":true,"supports_cloud_sync":false,"supported_locales":["zh-CN","en-US"],"protocol_version":1},"sessions":[{"id":"sess-new","title":"Mac 工作台","model":"gpt-5","created_at":"2026-06-27T06:00:00+00:00","updated_at":"2026-06-27T06:00:00+00:00","message_count":0,"total_tokens":0,"total_cost_usd":0.0,"status":"active"}],"total_sessions":3,"selected_session_id":"sess-new","snapshot":{"session_id":"sess-new","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions" else {
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
        let bootstrap = try await client.createWorkbenchSession(
            title: "Mac 工作台",
            model: "gpt-5",
            systemPrompt: "你是本地工作台协调者"
        )

        #expect(bootstrap.selectedSessionID == "sess-new")
        #expect(bootstrap.sessions.first?.id == "sess-new")
        #expect(bootstrap.totalSessions == 3)
        #expect(bootstrap.snapshot?.sessionID == "sess-new")
    }

    @Test func sendMessageCanIncludeWorkbenchIssueDraft() async throws {
        let json = Data(
            """
            {"id":"msg-001","role":"assistant","content":"已记录，并创建 Issue。","timestamp":"2026-07-02T08:00:00","metadata":{"workbench_issue":{"task_id":"task-chat-1"},"workbench_snapshot":{"session_id":"sess 中文"}}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/sessions/sess%20%E4%B8%AD%E6%96%87/messages" else {
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
            let issue = body?["workbench_issue"] as? [String: Any]
            guard body?["content"] as? String == "把登录失败问题记录成任务",
                  body?["stream"] as? Bool == false,
                  issue?["mission_id"] as? String == "mission-1",
                  issue?["title"] as? String == "修复登录失败",
                  issue?["description"] as? String == "用户输入正确密码后仍然失败。",
                  issue?["acceptance_criteria"] as? [String] == ["正确密码可以登录"],
                  issue?["parallel_mode"] as? String == "exclusive",
                  issue?["risk_level"] as? String == "high" else {
                fatalError("Unexpected body: \(String(describing: body))")
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
        let message = try await client.sendMessage(
            sessionID: "sess 中文",
            content: "把登录失败问题记录成任务",
            workbenchIssue: ChatIssueDraftDTO(
                missionID: "mission-1",
                title: "修复登录失败",
                description: "用户输入正确密码后仍然失败。",
                acceptanceCriteria: ["正确密码可以登录"],
                riskLevel: "high"
            )
        )

        #expect(message.id == "msg-001")
        #expect(message.role == "assistant")
        #expect(message.content == "已记录，并创建 Issue。")
        #expect(message.metadata["workbench_issue"] == .object(["task_id": .string("task-chat-1")]))
    }

    @Test func fetchMessagesLoadsPersistedChatHistory() async throws {
        let json = Data(
            """
            {"messages":[{"id":"msg-1","role":"user","content":"把登录失败记录成任务","timestamp":"2026-07-02T08:00:00","metadata":{"source":"chat"}},{"id":"msg-2","role":"assistant","content":"已创建关联任务。","timestamp":"2026-07-02T08:00:03","metadata":{"workbench_issue":{"task_id":"task-chat-1"}}}],"total":2}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/sessions/sess%20%E4%B8%AD%E6%96%87/messages?page=1&page_size=50" else {
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
        let history = try await client.fetchMessages(
            sessionID: "sess 中文",
            page: 1,
            pageSize: 50
        )

        #expect(history.total == 2)
        #expect(history.messages.map(\.id) == ["msg-1", "msg-2"])
        #expect(history.messages[0].metadata["source"] == .string("chat"))
        #expect(history.messages[1].metadata["workbench_issue"] == .object(["task_id": .string("task-chat-1")]))
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

    @Test func fetchEventsUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let json = Data(
            """
            {"events":[],"limit":10}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/audit-events",
                  query["limit"] == "10" else {
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

        let client = makeClient(routeTemplates: [
            "events": "/workbench-v2/sessions/{session_id}/audit-events",
        ])
        let response = try await client.fetchEvents(sessionID: sessionID, limit: 10)

        #expect(response.events == [])
        #expect(response.limit == 10)
    }

    @Test func fetchEventsWithFilters() async throws {
        let sessionID = "sess/中文"
        let eventType = "issue.claimed"
        let subjectID = "task/审查"
        let actor = "后端智能体"
        let since = "2026-06-27T10:00:00+00:00"
        let json = Data(
            """
            {"events":[{"id":"evt-002","session_id":"sess/中文","type":"issue.claimed","actor":"后端智能体","subject_id":"task/审查","payload":{"lease_id":"lease-001"},"timestamp":"2026-06-27T10:10:00","task":{"id":"task/审查","session_id":"sess/中文","subject":"查看审计事件","description":"时间线详情需要任务摘要","status":"in_progress","active_form":"issue-event-detail","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T10:00:00","updated_at":"2026-06-27T10:10:00"}}],"event_type":"issue.claimed","subject_id":"task/审查","actor":"后端智能体","since":"2026-06-27T10:00:00+00:00","limit":25}
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
                  query["actor"] == actor,
                  query["since"] == since else {
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
            since: since,
            limit: 25
        )

        #expect(response.eventType == eventType)
        #expect(response.subjectID == subjectID)
        #expect(response.actor == actor)
        #expect(response.since == since)
        #expect(response.limit == 25)

        let event = try #require(response.events.first)
        #expect(event.sessionID == sessionID)
        #expect(event.type == eventType)
        #expect(event.actor == actor)
        #expect(event.subjectID == subjectID)
        #expect(event.payload == ["lease_id": .string("lease-001")])
        #expect(event.task?.subject == "查看审计事件")
        #expect(event.task?.status == "in_progress")
        #expect(event.task?.activeForm == "issue-event-detail")
        #expect(event.task?.owner == "Backend-Agent")
    }

    @Test func fetchEventEncodesPathComponentsAndDecodesPayload() async throws {
        let sessionID = "sess/中文"
        let eventID = "event/人工 审批"
        let json = Data(
            """
            {"id":"event/人工 审批","session_id":"sess/中文","type":"approval.requested","actor":"Reviewer-Agent","subject_id":"approval-001","payload":{"risk":"high","requires_human":true,"task_id":"task-001"},"timestamp":"2026-06-27T06:12:00","task":{"id":"task-001","session_id":"sess/中文","subject":"人工审批详情","description":"审批事件需要任务上下文","status":"blocked","active_form":"approval-event-detail","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:12:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/events/event%2F%E4%BA%BA%E5%B7%A5%20%E5%AE%A1%E6%89%B9" else {
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
        let event = try await client.fetchEvent(sessionID: sessionID, eventID: eventID)

        #expect(event.id == eventID)
        #expect(event.sessionID == sessionID)
        #expect(event.type == "approval.requested")
        #expect(event.actor == "Reviewer-Agent")
        #expect(event.subjectID == "approval-001")
        #expect(event.payload == ["risk": .string("high"), "requires_human": .bool(true), "task_id": .string("task-001")])
        #expect(event.task?.subject == "人工审批详情")
        #expect(event.task?.status == "blocked")
        #expect(event.task?.activeForm == "approval-event-detail")
        #expect(event.task?.owner == "Reviewer-Agent")
    }

    @Test func fetchValidationRunsWithTaskID() async throws {
        let taskID = "task 001/审查"
        let status = "failed"
        let json = Data(
            """
            {"validation_runs":[{"id":"run-001","session_id":"sess-001","task_id":"task 001/审查","actor":"ValidationRunner","command":["pytest","test.py"],"cwd":"/workspace","status":"failed","exit_code":1,"output":"failed","started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:01","task":{"id":"task 001/审查","session_id":"sess-001","subject":"验证任务市场租约","description":"Reviews 页需要展示验证对应任务","status":"in_progress","active_form":"issue-validation-market","owner":"Validation-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}],"task_id":"task 001/审查","status":"failed","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.path == "/api/v1/workbench/sessions/sess-001/validation-runs",
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
        let response = try await client.fetchValidationRuns(
            sessionID: "sess-001",
            taskID: taskID,
            status: status,
            limit: 25
        )

        #expect(response.taskID == taskID)
        #expect(response.status == status)
        #expect(response.limit == 25)
        #expect(response.validationRuns.count == 1)

        let run = try #require(response.validationRuns.first)
        #expect(run.id == "run-001")
        #expect(run.sessionID == "sess-001")
        #expect(run.taskID == taskID)
        #expect(run.actor == "ValidationRunner")
        #expect(run.command == ["pytest", "test.py"])
        #expect(run.cwd == "/workspace")
        #expect(run.status == "failed")
        #expect(run.exitCode == 1)
        #expect(run.output == "failed")
        #expect(run.startedAt == "2026-06-27T06:00:00")
        #expect(run.completedAt == "2026-06-27T06:00:01")
        #expect(run.task?.subject == "验证任务市场租约")
        #expect(run.task?.status == "in_progress")
        #expect(run.task?.activeForm == "issue-validation-market")
        #expect(run.task?.owner == "Validation-Agent")
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
        let response = try await client.fetchValidationRuns(
            sessionID: "sess-001",
            taskID: nil,
            status: nil,
            limit: 50
        )

        #expect(response.taskID == nil)
        #expect(response.status == nil)
        #expect(response.limit == 50)
        #expect(response.validationRuns.isEmpty)
    }

    @Test func fetchValidationRunEncodesPathComponentsAndDecodesOutput() async throws {
        let sessionID = "sess/中文"
        let runID = "run/验证 001"
        let json = Data(
            """
            {"id":"run/验证 001","session_id":"sess/中文","task_id":"task/审查","actor":"ValidationRunner","command":["pytest","tests/unit/test_workbench_market.py","-q"],"cwd":"/Users/lv/Workspace/NaumiAgent","status":"failed","exit_code":1,"output":"2 failed, 3 passed","started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:03","task":{"id":"task/审查","session_id":"sess/中文","subject":"验证审查证据","description":"详情面板需要任务摘要","status":"blocked","active_form":"issue-validation-evidence","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/validation-runs/run%2F%E9%AA%8C%E8%AF%81%20001" else {
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
        let run = try await client.fetchValidationRun(sessionID: sessionID, runID: runID)

        #expect(run.id == runID)
        #expect(run.sessionID == sessionID)
        #expect(run.taskID == "task/审查")
        #expect(run.actor == "ValidationRunner")
        #expect(run.command == ["pytest", "tests/unit/test_workbench_market.py", "-q"])
        #expect(run.cwd == "/Users/lv/Workspace/NaumiAgent")
        #expect(run.status == "failed")
        #expect(run.exitCode == 1)
        #expect(run.output == "2 failed, 3 passed")
        #expect(run.completedAt == "2026-06-27T06:00:03")
        #expect(run.task?.subject == "验证审查证据")
        #expect(run.task?.status == "blocked")
        #expect(run.task?.activeForm == "issue-validation-evidence")
        #expect(run.task?.owner == "Reviewer-Agent")
    }

    @Test func fetchContextSnapshotsWithTaskIDAndAgentID() async throws {
        let taskID = "task 001/审查"
        let agentID = "agent 001/测试"
        let health = "stale"
        let json = Data(
            """
            {"context_snapshots":[{"id":"snap-001","session_id":"sess-001","agent_id":"agent 001/测试","task_id":"task 001/审查","health":"good","reasons":["上下文健康"],"created_at":"2026-06-27T06:00:00","task":{"id":"task 001/审查","session_id":"sess-001","subject":"同步上下文健康","description":"Worktrees 页需要显示任务上下文","status":"blocked","active_form":"issue-context-health","owner":"Context-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}],"task_id":"task 001/审查","agent_id":"agent 001/测试","health":"stale","limit":25}
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
                  query["agent_id"] == agentID,
                  query["health"] == health else {
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
            health: health,
            limit: 25
        )

        #expect(response.taskID == taskID)
        #expect(response.agentID == agentID)
        #expect(response.health == health)
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
        #expect(snapshot.task?.subject == "同步上下文健康")
        #expect(snapshot.task?.status == "blocked")
        #expect(snapshot.task?.activeForm == "issue-context-health")
        #expect(snapshot.task?.owner == "Context-Agent")
    }

    @Test func fetchContextSnapshotsWithoutOptionalFilters() async throws {
        let json = Data(
            """
            {"context_snapshots":[],"task_id":null,"agent_id":null,"health":null,"limit":50}
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
            health: nil,
            limit: 50
        )

        #expect(response.taskID == nil)
        #expect(response.agentID == nil)
        #expect(response.health == nil)
        #expect(response.limit == 50)
        #expect(response.contextSnapshots.isEmpty)
    }

    @Test func fetchContextSnapshotEncodesPathComponentsAndDecodesReasons() async throws {
        let sessionID = "sess 中文"
        let snapshotID = "snap/上下文 001"
        let json = Data(
            """
            {"id":"snap/上下文 001","session_id":"sess 中文","agent_id":"agent-001","task_id":"task-001","health":"stale","reasons":["超过 20 分钟未同步","存在策略冲突"],"created_at":"2026-06-27T06:10:00","task":{"id":"task-001","session_id":"sess 中文","subject":"修复上下文陈旧","description":"Inspector 详情需要任务摘要","status":"in_progress","active_form":"issue-context-stale","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/context-snapshots/snap%2F%E4%B8%8A%E4%B8%8B%E6%96%87%20001" else {
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
        let snapshot = try await client.fetchContextSnapshot(sessionID: sessionID, snapshotID: snapshotID)

        #expect(snapshot.id == snapshotID)
        #expect(snapshot.sessionID == sessionID)
        #expect(snapshot.agentID == "agent-001")
        #expect(snapshot.taskID == "task-001")
        #expect(snapshot.health == "stale")
        #expect(snapshot.reasons == ["超过 20 分钟未同步", "存在策略冲突"])
        #expect(snapshot.createdAt == "2026-06-27T06:10:00")
        #expect(snapshot.task?.subject == "修复上下文陈旧")
        #expect(snapshot.task?.status == "in_progress")
        #expect(snapshot.task?.activeForm == "issue-context-stale")
        #expect(snapshot.task?.owner == "Reviewer-Agent")
    }

    @Test func recordContextHealthUsesPOSTAndEncodesPathAndBody() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/审查"
        let snapshotJSON = Data(
            """
            {"id":"snap-001","session_id":"sess/中文","agent_id":"agent-001","task_id":"task/审查","health":"good","reasons":["上下文健康"],"created_at":"2026-06-27T06:00:00","task":{"id":"task/审查","session_id":"sess/中文","subject":"同步上下文健康","description":"写操作返回需要保留任务摘要","status":"blocked","active_form":"issue-context-health","owner":"Context-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}}
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
        #expect(snapshot.task?.subject == "同步上下文健康")
        #expect(snapshot.task?.status == "blocked")
        #expect(snapshot.task?.activeForm == "issue-context-health")
        #expect(snapshot.task?.owner == "Context-Agent")
    }

    @Test func recordContextHealthWithSnapshotRequestsFreshSnapshot() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/审查"
        let responseJSON = Data(
            """
            {"context_snapshot":{"id":"snap-001","session_id":"sess/中文","agent_id":"agent-001","task_id":"task/审查","health":"good","reasons":["上下文健康"],"created_at":"2026-06-27T06:00:00","task":{"id":"task/审查","session_id":"sess/中文","subject":"同步上下文健康","description":"写操作返回需要保留任务摘要","status":"blocked","active_form":"issue-context-health","owner":"Context-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}},"snapshot":{"session_id":"sess/中文","summary":{"current_mission_title":"上下文刷新","active_agents":1,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/issues/task%2F%E5%AE%A1%E6%9F%A5/context-health?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.recordContextHealthWithSnapshot(
            sessionID: sessionID,
            taskID: taskID,
            agentID: "agent-001",
            minutesSinceSync: 5,
            tokenLoadRatio: 0.75,
            policyConflict: false,
            actor: "Human"
        )

        #expect(response.contextSnapshot.id == "snap-001")
        #expect(response.contextSnapshot.sessionID == sessionID)
        #expect(response.contextSnapshot.taskID == taskID)
        #expect(response.contextSnapshot.task?.subject == "同步上下文健康")
        #expect(response.contextSnapshot.task?.status == "blocked")
        #expect(response.snapshot.sessionID == sessionID)
        #expect(response.snapshot.summary?.currentMissionTitle == "上下文刷新")
    }

    @Test func recordContextHealthWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/上下文"
        let responseJSON = Data(
            """
            {"context_snapshot":{"id":"snap-template","session_id":"sess/中文","agent_id":"agent-001","task_id":"task/上下文","health":"stale","reasons":["超过 20 分钟未同步"],"created_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/issues/task%2F%E4%B8%8A%E4%B8%8B%E6%96%87/context-health?include_snapshot=true" else {
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
                  json?["minutes_since_sync"] as? Int == 25,
                  json?["token_load_ratio"] as? Double == 0.5,
                  json?["policy_conflict"] as? Bool == true,
                  json?["actor"] as? String == "Human" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "record_context_health": "/workbench-v2/sessions/{session_id}/issues/{task_id}/context-health",
        ])
        let response = try await client.recordContextHealthWithSnapshot(
            sessionID: sessionID,
            taskID: taskID,
            agentID: "agent-001",
            minutesSinceSync: 25,
            tokenLoadRatio: 0.5,
            policyConflict: true,
            actor: "Human"
        )

        #expect(response.contextSnapshot.id == "snap-template")
        #expect(response.contextSnapshot.taskID == taskID)
        #expect(response.contextSnapshot.health == "stale")
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func fetchApprovalsWithState() async throws {
        let sessionID = "sess 中文"
        let state = "waiting"
        let missionID = "mission 001/审查"
        let taskID = "task 001/审批"
        let json = Data(
            """
            {"approvals":[{"id":"approval-001","session_id":"sess 中文","mission_id":"mission 001/审查","task_id":"task 001/审批","state":"waiting","title":"允许重构","detail":"保持测试通过","requester":"Agent-A","reviewer":"","decision_note":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task 001/审批","session_id":"sess 中文","subject":"审查高风险审批","description":"审批队列需要显示任务上下文","status":"blocked","active_form":"issue-risk-approval","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}],"state":"waiting","mission_id":"mission 001/审查","task_id":"task 001/审批","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/approvals",
                  query["limit"] == "25",
                  query["state"] == state,
                  query["mission_id"] == missionID,
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
        let response = try await client.fetchApprovals(
            sessionID: sessionID,
            state: state,
            missionID: missionID,
            taskID: taskID,
            limit: 25
        )

        #expect(response.state == state)
        #expect(response.missionID == missionID)
        #expect(response.taskID == taskID)
        #expect(response.limit == 25)
        #expect(response.approvals.count == 1)

        let approval = try #require(response.approvals.first)
        #expect(approval.id == "approval-001")
        #expect(approval.sessionID == sessionID)
        #expect(approval.missionID == missionID)
        #expect(approval.taskID == taskID)
        #expect(approval.state == "waiting")
        #expect(approval.title == "允许重构")
        #expect(approval.task?.subject == "审查高风险审批")
        #expect(approval.task?.status == "blocked")
        #expect(approval.task?.activeForm == "issue-risk-approval")
        #expect(approval.task?.owner == "Reviewer-Agent")
    }

    @Test func fetchApprovalsWithoutState() async throws {
        let json = Data(
            """
            {"approvals":[],"state":null,"mission_id":null,"task_id":null,"limit":50}
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
            missionID: nil,
            taskID: nil,
            limit: 50
        )

        #expect(response.state == nil)
        #expect(response.missionID == nil)
        #expect(response.taskID == nil)
        #expect(response.limit == 50)
        #expect(response.approvals.isEmpty)
    }

    @Test func fetchApprovalEncodesPathComponentsAndDecodesDecisionContext() async throws {
        let sessionID = "sess 中文"
        let approvalID = "approval/审查 001"
        let json = Data(
            """
            {"id":"approval/审查 001","session_id":"sess 中文","mission_id":"mission-001","task_id":"task-001","state":"waiting","title":"请求审批","detail":"高风险变更需要人工确认","requester":"Backend-Agent","reviewer":"","decision_note":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","task":{"id":"task-001","session_id":"sess 中文","subject":"确认高风险审批","description":"Inspector 审批详情需要任务摘要","status":"in_progress","active_form":"issue-approval-detail","owner":"Governance-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/approvals/approval%2F%E5%AE%A1%E6%9F%A5%20001" else {
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
        let approval = try await client.fetchApproval(sessionID: sessionID, approvalID: approvalID)

        #expect(approval.id == approvalID)
        #expect(approval.sessionID == sessionID)
        #expect(approval.missionID == "mission-001")
        #expect(approval.taskID == "task-001")
        #expect(approval.state == "waiting")
        #expect(approval.title == "请求审批")
        #expect(approval.detail == "高风险变更需要人工确认")
        #expect(approval.requester == "Backend-Agent")
        #expect(approval.reviewer == "")
        #expect(approval.decisionNote == "")
        #expect(approval.createdAt == "2026-06-27T06:00:00")
        #expect(approval.updatedAt == "2026-06-27T06:10:00")
        #expect(approval.task?.subject == "确认高风险审批")
        #expect(approval.task?.status == "in_progress")
        #expect(approval.task?.activeForm == "issue-approval-detail")
        #expect(approval.task?.owner == "Governance-Agent")
    }

    @Test func fetchFailuresWithFilters() async throws {
        let sessionID = "sess 中文"
        let taskID = "task 001/审查"
        let status = "open"
        let kind = "test_failed"
        let json = Data(
            """
            {"failures":[{"id":"failure-001","session_id":"sess 中文","task_id":"task 001/审查","kind":"test_failed","title":"测试失败","detail":"保持测试通过","source_id":"run-001","status":"open","created_at":"2026-06-27T06:00:00"}],"task_id":"task 001/审查","status":"open","kind":"test_failed","limit":25}
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
                  query["status"] == status,
                  query["kind"] == kind else {
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
            kind: kind,
            limit: 25
        )

        #expect(response.taskID == taskID)
        #expect(response.status == status)
        #expect(response.kind == kind)
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
            {"failures":[],"task_id":null,"status":null,"kind":null,"limit":50}
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
            kind: nil,
            limit: 50
        )

        #expect(response.taskID == nil)
        #expect(response.status == nil)
        #expect(response.kind == nil)
        #expect(response.limit == 50)
        #expect(response.failures.isEmpty)
    }

    @Test func fetchFailureEncodesPathComponentsAndDecodesDiagnostics() async throws {
        let sessionID = "sess 中文"
        let failureID = "failure/测试 001"
        let json = Data(
            """
            {"id":"failure/测试 001","session_id":"sess 中文","task_id":"task-001","kind":"test_failed","title":"DTO 解码测试失败","detail":"pytest tests/unit/test_dto.py -q failed with 2 failures","source_id":"run-001","status":"open","created_at":"2026-06-27T06:00:00","task":{"id":"task-001","session_id":"sess 中文","subject":"修复 DTO 解码测试","description":"失败诊断卡片需要显示任务上下文","status":"blocked","active_form":"issue-dto-failure","owner":"Test-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/failures/failure%2F%E6%B5%8B%E8%AF%95%20001" else {
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
        let failure = try await client.fetchFailure(sessionID: sessionID, failureID: failureID)

        #expect(failure.id == failureID)
        #expect(failure.sessionID == sessionID)
        #expect(failure.taskID == "task-001")
        #expect(failure.kind == "test_failed")
        #expect(failure.title == "DTO 解码测试失败")
        #expect(failure.detail == "pytest tests/unit/test_dto.py -q failed with 2 failures")
        #expect(failure.sourceID == "run-001")
        #expect(failure.status == "open")
        #expect(failure.createdAt == "2026-06-27T06:00:00")
        #expect(failure.task?.subject == "修复 DTO 解码测试")
        #expect(failure.task?.status == "blocked")
        #expect(failure.task?.activeForm == "issue-dto-failure")
        #expect(failure.task?.owner == "Test-Agent")
    }

    @Test func fetchIssuesWithFilters() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let riskLevel = "high"
        let status = "blocked"
        let json = Data(
            """
            {"issues":[{"session_id":"sess 中文","task_id":"task-001","mission_id":"mission 中文","parallel_mode":"exclusive","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["通过测试"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-001","session_id":"sess 中文","subject":"任务市场租约策略","description":"让列表显示真实任务事实","status":"blocked","active_form":"issue-1-market-lease","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:30:00","updated_at":"2026-06-27T05:45:00"}}],"mission_id":"mission 中文","risk_level":"high","status":"blocked","limit":25}
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
                  query["risk_level"] == riskLevel,
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
        let response = try await client.fetchIssues(
            sessionID: sessionID,
            missionID: missionID,
            riskLevel: riskLevel,
            status: status,
            limit: 25
        )

        #expect(response.missionID == missionID)
        #expect(response.riskLevel == riskLevel)
        #expect(response.status == status)
        #expect(response.limit == 25)
        #expect(response.issues.count == 1)

        let issue = try #require(response.issues.first)
        #expect(issue.sessionID == sessionID)
        #expect(issue.taskID == "task-001")
        #expect(issue.missionID == missionID)
        #expect(issue.parallelMode == "exclusive")
        #expect(issue.riskLevel == "high")
        #expect(issue.acceptanceCriteria == ["通过测试"])
        #expect(issue.task?.subject == "任务市场租约策略")
        #expect(issue.task?.status == "blocked")
        #expect(issue.task?.activeForm == "issue-1-market-lease")
        #expect(issue.task?.owner == "Backend-Agent")
    }

    @Test func fetchIssuesUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/治理"
        let json = Data(
            """
            {"issues":[{"session_id":"sess/中文","task_id":"task-template","mission_id":"mission/治理","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":true,"acceptance_criteria":["任务市场可见"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}],"mission_id":"mission/治理","risk_level":"medium","status":"pending","limit":12}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/issues",
                  query["limit"] == "12",
                  query["mission_id"] == missionID,
                  query["risk_level"] == "medium",
                  query["status"] == "pending" else {
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

        let client = makeClient(routeTemplates: [
            "issues": "/workbench-v2/sessions/{session_id}/issues",
        ])
        let response = try await client.fetchIssues(
            sessionID: sessionID,
            missionID: missionID,
            riskLevel: "medium",
            status: "pending",
            limit: 12
        )

        #expect(response.missionID == missionID)
        #expect(response.riskLevel == "medium")
        #expect(response.status == "pending")
        #expect(response.limit == 12)
        #expect(response.issues.first?.taskID == "task-template")
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
            status: nil,
            limit: 50
        )

        #expect(response.missionID == nil)
        #expect(response.riskLevel == nil)
        #expect(response.status == nil)
        #expect(response.limit == 50)
        #expect(response.issues.isEmpty)
    }

    @Test func fetchIssueEncodesPathComponentsAndDecodesGovernanceMetadata() async throws {
        let sessionID = "sess 中文"
        let taskID = "task/市场 001"
        let json = Data(
            """
            {"session_id":"sess 中文","task_id":"task/市场 001","mission_id":"mission-001","parallel_mode":"exclusive","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["通过验证","更新审查说明"],"expected_artifacts":["src/naumi_agent/workbench/market.py"],"related_branch":"issue/task-market","related_worktree":"wt-task-market","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","task":{"id":"task/市场 001","session_id":"sess 中文","subject":"任务市场租约策略","description":"检查器详情页直接读取任务事实","status":"in_progress","active_form":"issue-detail-api","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/issues/task%2F%E5%B8%82%E5%9C%BA%20001" else {
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
        let issue = try await client.fetchIssue(sessionID: sessionID, taskID: taskID)

        #expect(issue.sessionID == sessionID)
        #expect(issue.taskID == taskID)
        #expect(issue.missionID == "mission-001")
        #expect(issue.parallelMode == "exclusive")
        #expect(issue.riskLevel == "high")
        #expect(issue.requiresHumanApproval == true)
        #expect(issue.acceptanceCriteria == ["通过验证", "更新审查说明"])
        #expect(issue.expectedArtifacts == ["src/naumi_agent/workbench/market.py"])
        #expect(issue.relatedBranch == "issue/task-market")
        #expect(issue.relatedWorktree == "wt-task-market")
        #expect(issue.task?.subject == "任务市场租约策略")
        #expect(issue.task?.status == "in_progress")
        #expect(issue.task?.activeForm == "issue-detail-api")
        #expect(issue.task?.owner == "Backend-Agent")
        #expect(issue.relatedPR == "")
        #expect(issue.createdAt == "2026-06-27T06:00:00")
        #expect(issue.updatedAt == "2026-06-27T06:10:00")
    }

    @Test func fetchIssueUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/审查"
        let json = Data(
            """
            {"session_id":"sess/中文","task_id":"task/审查","mission_id":"mission-template","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":true,"acceptance_criteria":["详情面可打开"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/issues/task%2F%E5%AE%A1%E6%9F%A5" else {
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

        let client = makeClient(routeTemplates: [
            "issue": "/workbench-v2/sessions/{session_id}/issues/{task_id}",
        ])
        let issue = try await client.fetchIssue(sessionID: sessionID, taskID: taskID)

        #expect(issue.sessionID == sessionID)
        #expect(issue.taskID == taskID)
        #expect(issue.missionID == "mission-template")
        #expect(issue.acceptanceCriteria == ["详情面可打开"])
    }

    @Test func fetchLeasesWithFilters() async throws {
        let sessionID = "sess 中文"
        let taskID = "task 001/审查"
        let agentID = "agent 001/测试"
        let state = "active"
        let json = Data(
            """
            {"leases":[{"id":"lease-001","session_id":"sess 中文","task_id":"task 001/审查","agent_id":"agent 001/测试","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task 001/审查","session_id":"sess 中文","subject":"实现租约详情","description":"任务市场需要直接显示租约所属任务","status":"in_progress","active_form":"issue-lease-detail","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}],"state":"active","task_id":"task 001/审查","agent_id":"agent 001/测试","limit":25}
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
        #expect(lease.task?.subject == "实现租约详情")
        #expect(lease.task?.status == "in_progress")
        #expect(lease.task?.activeForm == "issue-lease-detail")
        #expect(lease.task?.owner == "Backend-Agent")
    }

    @Test func fetchLeasesUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/租约"
        let agentID = "agent/后端"
        let json = Data(
            """
            {"leases":[{"id":"lease-template","session_id":"sess/中文","task_id":"task/租约","agent_id":"agent/后端","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-template","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}],"state":"active","task_id":"task/租约","agent_id":"agent/后端","limit":18}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/leases",
                  query["limit"] == "18",
                  query["state"] == "active",
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

        let client = makeClient(routeTemplates: [
            "leases": "/workbench-v2/sessions/{session_id}/leases",
        ])
        let response = try await client.fetchLeases(
            sessionID: sessionID,
            state: "active",
            taskID: taskID,
            agentID: agentID,
            limit: 18
        )

        #expect(response.state == "active")
        #expect(response.taskID == taskID)
        #expect(response.agentID == agentID)
        #expect(response.limit == 18)
        #expect(response.leases.first?.id == "lease-template")
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

    @Test func fetchLeaseEncodesPathComponentsAndDecodesWorktreeBinding() async throws {
        let sessionID = "sess 中文"
        let leaseID = "lease/任务 001"
        let json = Data(
            """
            {"id":"lease/任务 001","session_id":"sess 中文","task_id":"task-001","agent_id":"agent-001","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-task-market","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","task":{"id":"task-001","session_id":"sess 中文","subject":"查看租约详情","description":"Inspector 需要租约任务摘要","status":"in_progress","active_form":"issue-lease-inspector","owner":"Agent-A","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/leases/lease%2F%E4%BB%BB%E5%8A%A1%20001" else {
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
        let lease = try await client.fetchLease(sessionID: sessionID, leaseID: leaseID)

        #expect(lease.id == leaseID)
        #expect(lease.sessionID == sessionID)
        #expect(lease.taskID == "task-001")
        #expect(lease.agentID == "agent-001")
        #expect(lease.state == "active")
        #expect(lease.expiresAt == "2026-06-27T08:00:00")
        #expect(lease.worktreeName == "wt-task-market")
        #expect(lease.createdAt == "2026-06-27T06:00:00")
        #expect(lease.updatedAt == "2026-06-27T06:10:00")
        #expect(lease.task?.subject == "查看租约详情")
        #expect(lease.task?.status == "in_progress")
        #expect(lease.task?.activeForm == "issue-lease-inspector")
        #expect(lease.task?.owner == "Agent-A")
    }

    @Test func fetchLeaseUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let leaseID = "lease/详情"
        let json = Data(
            """
            {"id":"lease/详情","session_id":"sess/中文","task_id":"task-template","agent_id":"agent-template","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-template","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/leases/lease%2F%E8%AF%A6%E6%83%85" else {
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

        let client = makeClient(routeTemplates: [
            "lease": "/workbench-v2/sessions/{session_id}/leases/{lease_id}",
        ])
        let lease = try await client.fetchLease(sessionID: sessionID, leaseID: leaseID)

        #expect(lease.id == leaseID)
        #expect(lease.sessionID == sessionID)
        #expect(lease.taskID == "task-template")
        #expect(lease.worktreeName == "wt-template")
    }

    @Test func fetchWorktreesWithFilters() async throws {
        let sessionID = "sess 中文"
        let taskID = "task 001/审查"
        let json = Data(
            """
            {"worktrees":[{"name":"wt-api","path":"/repo/.naumi/worktrees/wt-api","branch":"naumi/worktree-wt-api","base_ref":"abc123","status":"clean","task_id":"task 001/审查","dirty_files":0,"commits_ahead":0,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","kept_reason":"","metadata":{"owner":"Backend-Agent"},"removable":true,"task":{"id":"task 001/审查","session_id":"sess 中文","subject":"检查 API 工作区","description":"工作区列表需要任务摘要","status":"in_progress","active_form":"issue-worktree-api","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}],"task_id":"task 001/审查","status":"clean","limit":25}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/worktrees",
                  query["limit"] == "25",
                  query["task_id"] == taskID,
                  query["status"] == "clean" else {
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
        let response = try await client.fetchWorktrees(
            sessionID: sessionID,
            taskID: taskID,
            status: "clean",
            limit: 25
        )

        #expect(response.taskID == taskID)
        #expect(response.status == "clean")
        #expect(response.limit == 25)
        #expect(response.worktrees.count == 1)

        let worktree = try #require(response.worktrees.first)
        #expect(worktree.name == "wt-api")
        #expect(worktree.path == "/repo/.naumi/worktrees/wt-api")
        #expect(worktree.branch == "naumi/worktree-wt-api")
        #expect(worktree.baseRef == "abc123")
        #expect(worktree.status == "clean")
        #expect(worktree.taskID == taskID)
        #expect(worktree.dirtyFiles == 0)
        #expect(worktree.commitsAhead == 0)
        #expect(worktree.keptReason == "")
        #expect(worktree.metadata == ["owner": "Backend-Agent"])
        #expect(worktree.removable)
        #expect(worktree.task?.subject == "检查 API 工作区")
        #expect(worktree.task?.status == "in_progress")
        #expect(worktree.task?.activeForm == "issue-worktree-api")
        #expect(worktree.task?.owner == "Backend-Agent")
    }

    @Test func fetchWorktreesUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/工作区"
        let json = Data(
            """
            {"worktrees":[{"name":"wt-template","path":"/repo/.naumi/worktrees/wt-template","branch":"naumi/worktree-wt-template","base_ref":"abc123","status":"active","task_id":"task/工作区","dirty_files":1,"commits_ahead":2,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","kept_reason":"","metadata":{},"removable":true}],"task_id":"task/工作区","status":"active","limit":16}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/worktrees",
                  query["limit"] == "16",
                  query["task_id"] == taskID,
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

        let client = makeClient(routeTemplates: [
            "worktrees": "/workbench-v2/sessions/{session_id}/worktrees",
        ])
        let response = try await client.fetchWorktrees(
            sessionID: sessionID,
            taskID: taskID,
            status: "active",
            limit: 16
        )

        #expect(response.taskID == taskID)
        #expect(response.status == "active")
        #expect(response.limit == 16)
        #expect(response.worktrees.first?.name == "wt-template")
    }

    @Test func fetchWorktreeEncodesPathComponents() async throws {
        let sessionID = "sess/中文"
        let worktreeName = "wt-审查"
        let json = Data(
            """
            {"name":"wt-审查","path":"/repo/.naumi/worktrees/wt-review","branch":"naumi/worktree-wt-review","base_ref":"abc123","status":"dirty","task_id":"task-1","dirty_files":2,"commits_ahead":1,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:05:00","kept_reason":"","metadata":{"agent_id":"Reviewer-Agent"},"removable":false,"task":{"id":"task-1","session_id":"sess/中文","subject":"查看工作区详情","description":"Inspector 工作区详情需要任务摘要","status":"blocked","active_form":"issue-worktree-detail","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/worktrees/wt-%E5%AE%A1%E6%9F%A5" else {
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
        let worktree = try await client.fetchWorktree(sessionID: sessionID, name: worktreeName)

        #expect(worktree.name == worktreeName)
        #expect(worktree.status == "dirty")
        #expect(worktree.dirtyFiles == 2)
        #expect(worktree.commitsAhead == 1)
        #expect(worktree.metadata == ["agent_id": "Reviewer-Agent"])
        #expect(!worktree.removable)
        #expect(worktree.task?.subject == "查看工作区详情")
        #expect(worktree.task?.status == "blocked")
        #expect(worktree.task?.activeForm == "issue-worktree-detail")
        #expect(worktree.task?.owner == "Backend-Agent")
    }

    @Test func fetchWorktreeUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let worktreeName = "wt/详情"
        let json = Data(
            """
            {"name":"wt/详情","path":"/repo/.naumi/worktrees/wt-template","branch":"naumi/worktree-wt-template","base_ref":"abc123","status":"active","task_id":"task-template","dirty_files":1,"commits_ahead":2,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","kept_reason":"","metadata":{},"removable":true}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/worktrees/wt%2F%E8%AF%A6%E6%83%85" else {
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

        let client = makeClient(routeTemplates: [
            "worktree": "/workbench-v2/sessions/{session_id}/worktrees/{name}",
        ])
        let worktree = try await client.fetchWorktree(sessionID: sessionID, name: worktreeName)

        #expect(worktree.name == worktreeName)
        #expect(worktree.taskID == "task-template")
        #expect(worktree.status == "active")
        #expect(worktree.removable)
    }

    @Test func keepWorktreeUsesPOSTAndEncodesBody() async throws {
        let json = Data(
            """
            {"name":"wt-api","path":"/repo/.naumi/worktrees/wt-api","branch":"naumi/worktree-wt-api","base_ref":"abc123","status":"kept","task_id":"task-1","dirty_files":2,"commits_ahead":1,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","kept_reason":"等待人工审查","metadata":{},"removable":false,"task":{"id":"task-1","session_id":"sess-001","subject":"保留工作区","description":"保留后 Inspector 仍需要任务摘要","status":"blocked","active_form":"issue-worktree-keep","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/worktrees/wt-api/keep" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let payload = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard payload?["actor"] as? String == "Reviewer-Agent",
                  payload?["reason"] as? String == "等待人工审查" else {
                fatalError("Unexpected body: \(String(describing: payload))")
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
        let worktree = try await client.keepWorktree(
            sessionID: "sess-001",
            name: "wt-api",
            actor: "Reviewer-Agent",
            reason: "等待人工审查"
        )

        #expect(worktree.name == "wt-api")
        #expect(worktree.status == "kept")
        #expect(worktree.keptReason == "等待人工审查")
        #expect(!worktree.removable)
        #expect(worktree.task?.subject == "保留工作区")
        #expect(worktree.task?.status == "blocked")
        #expect(worktree.task?.activeForm == "issue-worktree-keep")
        #expect(worktree.task?.owner == "Reviewer-Agent")
    }

    @Test func keepWorktreeUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let worktreeName = "wt/保留"
        let json = Data(
            """
            {"name":"wt/保留","path":"/repo/.naumi/worktrees/wt-keep","branch":"naumi/worktree-wt-keep","base_ref":"abc123","status":"kept","task_id":"task-1","dirty_files":2,"commits_ahead":1,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","kept_reason":"等待人工审查","metadata":{},"removable":false}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/worktrees/wt%2F%E4%BF%9D%E7%95%99/keep" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let payload = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard payload?["actor"] as? String == "Reviewer-Agent",
                  payload?["reason"] as? String == "等待人工审查" else {
                fatalError("Unexpected body: \(String(describing: payload))")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, json)
        }

        let client = makeClient(routeTemplates: [
            "keep_worktree": "/workbench-v2/sessions/{session_id}/worktrees/{name}/keep",
        ])
        let worktree = try await client.keepWorktree(
            sessionID: sessionID,
            name: worktreeName,
            actor: "Reviewer-Agent",
            reason: "等待人工审查"
        )

        #expect(worktree.name == worktreeName)
        #expect(worktree.status == "kept")
        #expect(worktree.keptReason == "等待人工审查")
        #expect(!worktree.removable)
    }

    @Test func keepWorktreeWithSnapshotRequestsFreshSnapshot() async throws {
        let responseJSON = Data(
            """
            {"worktree":{"name":"wt-api","path":"/repo/.naumi/worktrees/wt-api","branch":"naumi/worktree-wt-api","base_ref":"abc123","status":"kept","task_id":"task-1","dirty_files":2,"commits_ahead":1,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","kept_reason":"等待人工审查","metadata":{},"removable":false,"task":{"id":"task-1","session_id":"sess-001","subject":"保留工作区并刷新","description":"include_snapshot 外层 worktree 也需要任务摘要","status":"blocked","active_form":"issue-worktree-keep-snapshot","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}},"snapshot":{"session_id":"sess-001","summary":{"current_mission_title":"保留工作区后刷新","active_agents":0,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/worktrees/wt-api/keep?include_snapshot=true" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let payload = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard payload?["actor"] as? String == "Reviewer-Agent",
                  payload?["reason"] as? String == "等待人工审查" else {
                fatalError("Unexpected body: \(String(describing: payload))")
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
        let response = try await client.keepWorktreeWithSnapshot(
            sessionID: "sess-001",
            name: "wt-api",
            actor: "Reviewer-Agent",
            reason: "等待人工审查"
        )

        #expect(response.worktree.name == "wt-api")
        #expect(response.worktree.status == "kept")
        #expect(response.worktree.task?.subject == "保留工作区并刷新")
        #expect(response.worktree.task?.status == "blocked")
        #expect(response.worktree.task?.activeForm == "issue-worktree-keep-snapshot")
        #expect(response.snapshot.sessionID == "sess-001")
        #expect(response.snapshot.summary?.currentMissionTitle == "保留工作区后刷新")
    }

    @Test func keepWorktreeWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let worktreeName = "wt/审查"
        let responseJSON = Data(
            """
            {"worktree":{"name":"wt/审查","path":"/repo/.naumi/worktrees/wt-review","branch":"naumi/worktree-wt-review","base_ref":"abc123","status":"kept","task_id":"task-1","dirty_files":2,"commits_ahead":1,"created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","kept_reason":"等待人工审查","metadata":{},"removable":false},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/worktrees/wt%2F%E5%AE%A1%E6%9F%A5/keep?include_snapshot=true" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let payload = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard payload?["actor"] as? String == "Reviewer-Agent",
                  payload?["reason"] as? String == "等待人工审查" else {
                fatalError("Unexpected body: \(String(describing: payload))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "keep_worktree": "/workbench-v2/sessions/{session_id}/worktrees/{name}/keep",
        ])
        let response = try await client.keepWorktreeWithSnapshot(
            sessionID: sessionID,
            name: worktreeName,
            actor: "Reviewer-Agent",
            reason: "等待人工审查"
        )

        #expect(response.worktree.name == worktreeName)
        #expect(response.worktree.status == "kept")
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func removeWorktreeUsesDELETEAndDiscardQuery() async throws {
        let json = Data(
            """
            {"name":"wt-dirty","discard_changes":true,"message":"已删除 worktree：wt-dirty"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%2F001/worktrees/wt-dirty",
                  query["discard_changes"] == "true" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "DELETE" else {
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
        let result = try await client.removeWorktree(
            sessionID: "sess/001",
            name: "wt-dirty",
            discardChanges: true
        )

        #expect(result.name == "wt-dirty")
        #expect(result.discardChanges)
        #expect(result.message == "已删除 worktree：wt-dirty")
    }

    @Test func removeWorktreeWithSnapshotRequestsFreshSnapshot() async throws {
        let responseJSON = Data(
            """
            {"removal":{"name":"wt-dirty","discard_changes":true,"message":"已删除 worktree：wt-dirty"},"snapshot":{"session_id":"sess/001","summary":{"current_mission_title":"删除工作区后刷新","active_agents":0,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%2F001/worktrees/wt-dirty",
                  query["discard_changes"] == "true",
                  query["include_snapshot"] == "true" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "DELETE" else {
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
        let response = try await client.removeWorktreeWithSnapshot(
            sessionID: "sess/001",
            name: "wt-dirty",
            discardChanges: true
        )

        #expect(response.removal.name == "wt-dirty")
        #expect(response.removal.discardChanges)
        #expect(response.snapshot.sessionID == "sess/001")
        #expect(response.snapshot.summary?.currentMissionTitle == "删除工作区后刷新")
    }

    @Test func removeWorktreeWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let worktreeName = "wt/清理"
        let responseJSON = Data(
            """
            {"removal":{"name":"wt/清理","discard_changes":false,"message":"已删除 worktree：wt/清理"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/worktrees/wt%2F%E6%B8%85%E7%90%86",
                  query["discard_changes"] == "false",
                  query["include_snapshot"] == "true" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "DELETE" else {
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

        let client = makeClient(routeTemplates: [
            "delete_worktree": "/workbench-v2/sessions/{session_id}/worktrees/{name}",
        ])
        let response = try await client.removeWorktreeWithSnapshot(
            sessionID: sessionID,
            name: worktreeName,
            discardChanges: false
        )

        #expect(response.removal.name == worktreeName)
        #expect(!response.removal.discardChanges)
        #expect(response.snapshot.sessionID == sessionID)
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

    @Test func fetchMissionEncodesPathComponentsAndDecodesGoal() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission/总览 001"
        let json = Data(
            """
            {"id":"mission/总览 001","session_id":"sess 中文","title":"Mac 工作台","goal":"补齐 Mission 详情 API","status":"planning","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%2F%E6%80%BB%E8%A7%88%20001" else {
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
        let mission = try await client.fetchMission(sessionID: sessionID, missionID: missionID)

        #expect(mission.id == missionID)
        #expect(mission.sessionID == sessionID)
        #expect(mission.title == "Mac 工作台")
        #expect(mission.goal == "补齐 Mission 详情 API")
        #expect(mission.status == "planning")
        #expect(mission.createdAt == "2026-06-27T06:00:00")
        #expect(mission.updatedAt == "2026-06-27T06:10:00")
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

    @Test func fetchAgentProfileEncodesPathComponentsAndDecodesCapabilities() async throws {
        let sessionID = "sess/中文"
        let agentID = "agent/后端"
        let json = Data(
            """
            {"id":"agent/后端","session_id":"sess/中文","name":"后端智能体","role":"coder","capabilities":["api","swift-client"],"permissions":["read","write"],"max_parallel_tasks":2,"status":"busy","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/agents/agent%2F%E5%90%8E%E7%AB%AF" else {
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
        let profile = try await client.fetchAgentProfile(sessionID: sessionID, agentID: agentID)

        #expect(profile.id == agentID)
        #expect(profile.sessionID == sessionID)
        #expect(profile.name == "后端智能体")
        #expect(profile.role == "coder")
        #expect(profile.capabilities == ["api", "swift-client"])
        #expect(profile.permissions == ["read", "write"])
        #expect(profile.maxParallelTasks == 2)
        #expect(profile.status == "busy")
        #expect(profile.createdAt == "2026-06-27T06:00:00")
        #expect(profile.updatedAt == "2026-06-27T06:10:00")
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

    @Test func registerAgentProfileWithSnapshotRequestsFreshSnapshot() async throws {
        let sessionID = "sess/中文"
        let agentID = "agent/后端"
        let responseJSON = Data(
            """
            {"agent_profile":{"id":"agent/后端","session_id":"sess/中文","name":"后端智能体","role":"coder","capabilities":["api","swift-client"],"permissions":["read","write"],"max_parallel_tasks":2,"status":"busy","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"},"snapshot":{"session_id":"sess/中文","summary":{"current_mission_title":"注册智能体后刷新","active_agents":1,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[{"id":"agent/后端","session_id":"sess/中文","name":"后端智能体","role":"coder","capabilities":["api","swift-client"],"permissions":["read","write"],"max_parallel_tasks":2,"status":"busy","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"}],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/agents/agent%2F%E5%90%8E%E7%AB%AF?include_snapshot=true" else {
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
                  json?["actor"] as? String == "Human" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.registerAgentProfileWithSnapshot(
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

        #expect(response.agentProfile.id == agentID)
        #expect(response.agentProfile.status == "busy")
        #expect(response.snapshot.sessionID == sessionID)
        #expect(response.snapshot.summary?.currentMissionTitle == "注册智能体后刷新")
        #expect(response.snapshot.agentProfiles.map(\.id) == [agentID])
    }

    @Test func registerAgentProfileWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let agentID = "agent/规划"
        let responseJSON = Data(
            """
            {"agent_profile":{"id":"agent/规划","session_id":"sess/中文","name":"规划智能体","role":"planner","capabilities":["planning"],"permissions":["read"],"max_parallel_tasks":1,"status":"idle","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[{"id":"agent/规划","session_id":"sess/中文","name":"规划智能体","role":"planner","capabilities":["planning"],"permissions":["read"],"max_parallel_tasks":1,"status":"idle","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00"}],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/agents/agent%2F%E8%A7%84%E5%88%92?include_snapshot=true" else {
                fatalError("Unexpected URL: \(String(describing: request.url))")
            }
            guard request.httpMethod == "POST" else {
                fatalError("Unexpected method: \(String(describing: request.httpMethod))")
            }
            guard let body = request.httpBody ?? request.httpBodyStream?.httpBodyStreamData() else {
                fatalError("Expected a request body")
            }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard json?["name"] as? String == "规划智能体",
                  json?["role"] as? String == "planner",
                  json?["actor"] as? String == "Human" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "upsert_agent_profile": "/workbench-v2/sessions/{session_id}/agents/{agent_id}",
        ])
        let response = try await client.registerAgentProfileWithSnapshot(
            sessionID: sessionID,
            agentID: agentID,
            name: "规划智能体",
            role: "planner",
            capabilities: ["planning"],
            permissions: ["read"],
            maxParallelTasks: 1,
            status: "idle",
            actor: "Human"
        )

        #expect(response.agentProfile.id == agentID)
        #expect(response.agentProfile.role == "planner")
        #expect(response.snapshot.sessionID == sessionID)
        #expect(response.snapshot.agentProfiles.map(\.id) == [agentID])
    }

    @Test func claimIssue() async throws {
        let leaseJSON = Data(
            """
            {"id":"lease-001","session_id":"sess-001","task_id":"task-001","agent_id":"agent-001","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-001","session_id":"sess-001","subject":"认领 API Client","description":"认领后任务市场需要直接显示任务摘要","status":"in_progress","active_form":"issue-claim-api","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}
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
        #expect(lease.task?.subject == "认领 API Client")
        #expect(lease.task?.status == "in_progress")
        #expect(lease.task?.activeForm == "issue-claim-api")
        #expect(lease.task?.owner == "Backend-Agent")
    }

    @Test func claimIssueWithSnapshotRequestsFreshSnapshot() async throws {
        let responseJSON = Data(
            """
            {"lease":{"id":"lease-001","session_id":"sess-001","task_id":"task-001","agent_id":"agent-001","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess-001","summary":{"current_mission_title":"认领后刷新","active_agents":1,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/issues/task-001/claim?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.claimIssueWithSnapshot(
            sessionID: "sess-001",
            taskID: "task-001",
            agentID: "agent-001",
            durationMinutes: 60,
            worktreeName: "wt-001"
        )

        #expect(response.lease.id == "lease-001")
        #expect(response.snapshot.sessionID == "sess-001")
        #expect(response.snapshot.summary?.currentMissionTitle == "认领后刷新")
    }

    @Test func claimIssueWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let taskID = "task/认领"
        let responseJSON = Data(
            """
            {"lease":{"id":"lease-template","session_id":"sess/中文","task_id":"task/认领","agent_id":"agent-001","state":"active","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-template","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/issues/task%2F%E8%AE%A4%E9%A2%86/claim?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "claim_issue": "/workbench-v2/sessions/{session_id}/issues/{task_id}/claim",
        ])
        let response = try await client.claimIssueWithSnapshot(
            sessionID: sessionID,
            taskID: taskID,
            agentID: "agent-001",
            durationMinutes: 60,
            worktreeName: "wt-template"
        )

        #expect(response.lease.id == "lease-template")
        #expect(response.lease.taskID == taskID)
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func releaseLease() async throws {
        let leaseJSON = Data(
            """
            {"id":"lease-001","session_id":"sess-001","task_id":"task-001","agent_id":"agent-001","state":"released","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00","task":{"id":"task-001","session_id":"sess-001","subject":"释放租约","description":"释放后仍要保留任务上下文","status":"pending","active_form":"issue-release-api","owner":"Agent-A","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T06:30:00"}}
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
        #expect(lease.task?.subject == "释放租约")
        #expect(lease.task?.status == "pending")
        #expect(lease.task?.activeForm == "issue-release-api")
        #expect(lease.task?.owner == "Agent-A")
    }

    @Test func releaseLeaseWithSnapshotRequestsFreshSnapshot() async throws {
        let responseJSON = Data(
            """
            {"lease":{"id":"lease-001","session_id":"sess-001","task_id":"task-001","agent_id":"agent-001","state":"released","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00"},"snapshot":{"session_id":"sess-001","summary":{"current_mission_title":"释放后刷新","active_agents":0,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/leases/lease-001/release?include_snapshot=true" else {
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
        let response = try await client.releaseLeaseWithSnapshot(sessionID: "sess-001", leaseID: "lease-001")

        #expect(response.lease.id == "lease-001")
        #expect(response.lease.state == "released")
        #expect(response.snapshot.sessionID == "sess-001")
        #expect(response.snapshot.summary?.currentMissionTitle == "释放后刷新")
    }

    @Test func releaseLeaseWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let leaseID = "lease/释放"
        let responseJSON = Data(
            """
            {"lease":{"id":"lease/释放","session_id":"sess/中文","task_id":"task-001","agent_id":"agent-001","state":"released","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/leases/lease%2F%E9%87%8A%E6%94%BE/release?include_snapshot=true" else {
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

        let client = makeClient(routeTemplates: [
            "release_lease": "/workbench-v2/sessions/{session_id}/leases/{lease_id}/release",
        ])
        let response = try await client.releaseLeaseWithSnapshot(sessionID: sessionID, leaseID: leaseID)

        #expect(response.lease.id == leaseID)
        #expect(response.lease.state == "released")
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func expireLeasesUsesPOSTAndEncodesPath() async throws {
        let sessionID = "sess 中文"
        let responseJSON = Data(
            """
            {"expired":[{"id":"lease-001","session_id":"sess 中文","task_id":"task-001","agent_id":"agent-001","state":"expired","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-001","session_id":"sess 中文","subject":"回收过期租约","description":"过期列表需要任务摘要","status":"pending","active_form":"issue-expire-api","owner":"Agent-A","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T06:00:00"}}]}
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
        #expect(lease.task?.subject == "回收过期租约")
        #expect(lease.task?.status == "pending")
        #expect(lease.task?.activeForm == "issue-expire-api")
        #expect(lease.task?.owner == "Agent-A")
    }

    @Test func expireLeasesWithSnapshotRequestsFreshSnapshot() async throws {
        let sessionID = "sess 中文"
        let responseJSON = Data(
            """
            {"expired":[{"id":"lease-001","session_id":"sess 中文","task_id":"task-001","agent_id":"agent-001","state":"expired","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00"}],"snapshot":{"session_id":"sess 中文","summary":{"current_mission_title":"租约过期后刷新","active_agents":0,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/leases/expire?include_snapshot=true" else {
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
        let response = try await client.expireLeasesWithSnapshot(sessionID: sessionID)

        #expect(response.expired.map(\.id) == ["lease-001"])
        #expect(response.expired.first?.state == "expired")
        #expect(response.snapshot.sessionID == sessionID)
        #expect(response.snapshot.summary?.currentMissionTitle == "租约过期后刷新")
    }

    @Test func expireLeasesWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let responseJSON = Data(
            """
            {"expired":[{"id":"lease-过期","session_id":"sess/中文","task_id":"task-001","agent_id":"agent-001","state":"expired","expires_at":"2026-06-27T08:00:00","worktree_name":"wt-001","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:30:00"}],"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/leases/expire?include_snapshot=true" else {
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

        let client = makeClient(routeTemplates: [
            "expire_leases": "/workbench-v2/sessions/{session_id}/leases/expire",
        ])
        let response = try await client.expireLeasesWithSnapshot(sessionID: sessionID)

        #expect(response.expired.map(\.id) == ["lease-过期"])
        #expect(response.expired.first?.state == "expired")
        #expect(response.snapshot.sessionID == sessionID)
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

    @Test func createMissionWithSnapshotRequestsFreshSnapshot() async throws {
        let responseJSON = Data(
            """
            {"mission":{"id":"mission-001","session_id":"sess-001","title":"Mac 工作台","goal":"补齐 API 调用面","status":"active","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess-001","summary":{"current_mission_title":"Mac 工作台","active_agents":0,"open_issues":0,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[{"id":"mission-001","session_id":"sess-001","title":"Mac 工作台","goal":"补齐 API 调用面","status":"active","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"}],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.createMissionWithSnapshot(
            sessionID: "sess-001",
            title: "Mac 工作台",
            goal: "补齐 API 调用面"
        )

        #expect(response.mission.id == "mission-001")
        #expect(response.snapshot.sessionID == "sess-001")
        #expect(response.snapshot.summary?.currentMissionTitle == "Mac 工作台")
        #expect(response.snapshot.missions == [response.mission])
    }

    @Test func createMissionWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let responseJSON = Data(
            """
            {"mission":{"id":"mission-template","session_id":"sess/中文","title":"模板 Mission","goal":"使用 daemon route template","status":"active","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/missions?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "create_mission": "/workbench-v2/sessions/{session_id}/missions",
        ])
        let response = try await client.createMissionWithSnapshot(
            sessionID: sessionID,
            title: "模板 Mission",
            goal: "使用 daemon route template"
        )

        #expect(response.mission.id == "mission-template")
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func attachIssue() async throws {
        let issueJSON = Data(
            """
            {"session_id":"sess-001","task_id":"task-001","mission_id":"mission-001","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":false,"acceptance_criteria":["通过 Swift 编译"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-001","session_id":"sess-001","subject":"绑定现有任务","description":"Task Market 立即展示任务摘要","status":"in_progress","active_form":"issue-attach-api","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}}
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
        #expect(issue.task?.subject == "绑定现有任务")
        #expect(issue.task?.status == "in_progress")
        #expect(issue.task?.activeForm == "issue-attach-api")
        #expect(issue.task?.owner == "Backend-Agent")
    }

    @Test func attachIssueWithSnapshotRequestsFreshSnapshot() async throws {
        let responseJSON = Data(
            """
            {"issue":{"session_id":"sess-001","task_id":"task-001","mission_id":"mission-001","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":false,"acceptance_criteria":["通过 Swift 编译"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-001","session_id":"sess-001","subject":"绑定现有任务","description":"Task Market 立即展示任务摘要","status":"in_progress","active_form":"issue-attach-api","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}},"snapshot":{"session_id":"sess-001","summary":{"current_mission_title":"Mac 工作台","active_agents":0,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[{"session_id":"sess-001","task_id":"task-001","mission_id":"mission-001","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":false,"acceptance_criteria":["通过 Swift 编译"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-001","session_id":"sess-001","subject":"绑定现有任务","description":"Task Market 立即展示任务摘要","status":"in_progress","active_form":"issue-attach-api","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}}],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions/mission-001/issues?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.attachIssueWithSnapshot(
            sessionID: "sess-001",
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: ["通过 Swift 编译"],
            parallelMode: "exclusive",
            riskLevel: "medium"
        )

        #expect(response.issue.taskID == "task-001")
        #expect(response.issue.missionID == "mission-001")
        #expect(response.issue.task?.subject == "绑定现有任务")
        #expect(response.issue.task?.status == "in_progress")
        #expect(response.snapshot.sessionID == "sess-001")
        #expect(response.snapshot.summary?.openIssues == 1)
        #expect(response.snapshot.issues == [response.issue])
    }

    @Test func attachIssueWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/治理"
        let taskID = "task/挂接"
        let responseJSON = Data(
            """
            {"issue":{"session_id":"sess/中文","task_id":"task/挂接","mission_id":"mission/治理","parallel_mode":"cooperative","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["Dashboard 立即刷新"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E6%B2%BB%E7%90%86/issues?include_snapshot=true" else {
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
                  criteria == ["Dashboard 立即刷新"],
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
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "mission_issues": "/workbench-v2/sessions/{session_id}/missions/{mission_id}/issues",
        ])
        let response = try await client.attachIssueWithSnapshot(
            sessionID: sessionID,
            missionID: missionID,
            taskID: taskID,
            acceptanceCriteria: ["Dashboard 立即刷新"],
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(response.issue.taskID == taskID)
        #expect(response.issue.missionID == missionID)
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func createIssueUsesPOSTAndEncodesBackingTaskFields() async throws {
        let issueJSON = Data(
            """
            {"session_id":"sess-001","task_id":"task-009","mission_id":"mission-001","parallel_mode":"cooperative","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["dashboard 刷新后可见","可被 Agent claim"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-009","session_id":"sess-001","subject":"实现 Issue 创建 API","description":"创建 backing task 并绑定 metadata","status":"pending","active_form":null,"owner":null,"blocks":[],"blocked_by":["1"],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}}
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
        #expect(issue.task?.subject == "实现 Issue 创建 API")
        #expect(issue.task?.status == "pending")
        #expect(issue.task?.blockedBy == ["1"])
    }

    @Test func createIssueWithSnapshotRequestsFreshSnapshot() async throws {
        let responseJSON = Data(
            """
            {"issue":{"session_id":"sess-001","task_id":"task-009","mission_id":"mission-001","parallel_mode":"cooperative","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["dashboard 刷新后可见","可被 Agent claim"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-009","session_id":"sess-001","subject":"实现 Issue 创建 API","description":"创建 backing task 并绑定 metadata","status":"pending","active_form":null,"owner":null,"blocks":[],"blocked_by":["1"],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}},"snapshot":{"session_id":"sess-001","summary":{"current_mission_title":"Mac 工作台","active_agents":0,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[{"session_id":"sess-001","task_id":"task-009","mission_id":"mission-001","parallel_mode":"cooperative","risk_level":"high","requires_human_approval":true,"acceptance_criteria":["dashboard 刷新后可见","可被 Agent claim"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00","task":{"id":"task-009","session_id":"sess-001","subject":"实现 Issue 创建 API","description":"创建 backing task 并绑定 metadata","status":"pending","active_form":null,"owner":null,"blocks":[],"blocked_by":["1"],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}}],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/missions/mission-001/issues?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.createIssueWithSnapshot(
            sessionID: "sess-001",
            missionID: "mission-001",
            title: "实现 Issue 创建 API",
            description: "创建 backing task 并绑定 metadata",
            blockedBy: ["1"],
            acceptanceCriteria: ["dashboard 刷新后可见", "可被 Agent claim"],
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(response.issue.taskID == "task-009")
        #expect(response.issue.missionID == "mission-001")
        #expect(response.issue.task?.subject == "实现 Issue 创建 API")
        #expect(response.issue.task?.status == "pending")
        #expect(response.snapshot.sessionID == "sess-001")
        #expect(response.snapshot.summary?.openIssues == 1)
        #expect(response.snapshot.issues == [response.issue])
    }

    @Test func createIssueWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/治理"
        let responseJSON = Data(
            """
            {"issue":{"session_id":"sess/中文","task_id":"task-template","mission_id":"mission/治理","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":true,"acceptance_criteria":["可在任务市场看到"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E6%B2%BB%E7%90%86/issues?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "create_issue": "/workbench-v2/sessions/{session_id}/missions/{mission_id}/issues",
        ])
        let response = try await client.createIssueWithSnapshot(
            sessionID: sessionID,
            missionID: missionID,
            title: "模板 Issue",
            description: "使用 daemon route template",
            blockedBy: [],
            acceptanceCriteria: ["可在任务市场看到"],
            parallelMode: "exclusive",
            riskLevel: "medium"
        )

        #expect(response.issue.taskID == "task-template")
        #expect(response.issue.missionID == missionID)
        #expect(response.snapshot.sessionID == sessionID)
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
            {"id":"run-001","session_id":"sess 中文","task_id":"task-001","actor":"Human","command":["pytest"],"cwd":"/workspace","status":"passed","exit_code":0,"output":"ok","task":{"id":"task-001","session_id":"sess 中文","subject":"运行验证","description":"写操作返回需要保留任务摘要","status":"in_progress","active_form":"issue-validation-run","owner":"Validation-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"},"started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:01"}
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
        #expect(result.sessionID == sessionID)
        #expect(result.taskID == "task-001")
        #expect(result.actor == "Human")
        #expect(result.command == ["pytest"])
        #expect(result.cwd == "/workspace")
        #expect(result.status == "passed")
        #expect(result.exitCode == 0)
        #expect(result.output == "ok")
        #expect(result.task?.subject == "运行验证")
        #expect(result.task?.status == "in_progress")
        #expect(result.task?.activeForm == "issue-validation-run")
        #expect(result.task?.owner == "Validation-Agent")
        #expect(result.startedAt == "2026-06-27T06:00:00")
        #expect(result.completedAt == "2026-06-27T06:00:01")
    }

    @Test func runValidationWithSnapshotRequestsFreshSnapshot() async throws {
        let sessionID = "sess 中文"
        let resultJSON = Data(
            """
            {"validation_run":{"id":"run-001","session_id":"sess 中文","task_id":"task-001","actor":"Human","command":["pytest"],"cwd":"/workspace","status":"passed","exit_code":0,"output":"ok","task":{"id":"task-001","session_id":"sess 中文","subject":"运行验证","description":"写操作返回需要保留任务摘要","status":"in_progress","active_form":"issue-validation-run","owner":"Validation-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"},"started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:01"},"snapshot":{"session_id":"sess 中文","summary":{"current_mission_title":"验证后刷新","active_agents":2,"open_issues":1,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/validation-runs?include_snapshot=true" else {
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
        let result = try await client.runValidationWithSnapshot(
            sessionID: sessionID,
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(result.validationRun.id == "run-001")
        #expect(result.validationRun.status == "passed")
        #expect(result.validationRun.exitCode == 0)
        #expect(result.validationRun.output == "ok")
        #expect(result.validationRun.task?.subject == "运行验证")
        #expect(result.validationRun.task?.status == "in_progress")
        #expect(result.snapshot.sessionID == "sess 中文")
        #expect(result.snapshot.summary?.currentMissionTitle == "验证后刷新")
    }

    @Test func runValidationWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let resultJSON = Data(
            """
            {"validation_run":{"id":"run-template","session_id":"sess/中文","task_id":"task-001","actor":"Human","command":["pytest"],"cwd":"/workspace","status":"passed","exit_code":0,"output":"ok","started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:01"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/validation-runs?include_snapshot=true" else {
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

        let client = makeClient(routeTemplates: [
            "run_validation": "/workbench-v2/sessions/{session_id}/validation-runs",
        ])
        let result = try await client.runValidationWithSnapshot(
            sessionID: sessionID,
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(result.validationRun.id == "run-template")
        #expect(result.validationRun.status == "passed")
        #expect(result.snapshot.sessionID == sessionID)
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

    @Test func createIntentLockWithSnapshotRequestsFreshSnapshot() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let responseJSON = Data(
            """
            {"intent_lock":{"id":"lock-001","session_id":"sess 中文","mission_id":"mission 中文","rule":"禁止修改 core 模块","blocked_paths":["src/secret"],"allowed_paths":["src/secret/README.md"],"require_proposal_for_risk":"high","active":true,"created_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess 中文","summary":{"current_mission_title":"治理规则更新","active_agents":0,"open_issues":0,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/intent-locks?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.createIntentLockWithSnapshot(
            sessionID: sessionID,
            missionID: missionID,
            actor: "Planner-Agent",
            rule: "禁止修改 core 模块",
            blockedPaths: ["src/secret"],
            allowedPaths: ["src/secret/README.md"],
            requireProposalForRisk: "high"
        )

        #expect(response.intentLock.id == "lock-001")
        #expect(response.intentLock.missionID == missionID)
        #expect(response.snapshot.sessionID == sessionID)
        #expect(response.snapshot.summary?.currentMissionTitle == "治理规则更新")
    }

    @Test func createIntentLockWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/治理"
        let responseJSON = Data(
            """
            {"intent_lock":{"id":"lock-template","session_id":"sess/中文","mission_id":"mission/治理","rule":"高风险需要人工审批","blocked_paths":["src/core"],"allowed_paths":["src/core/README.md"],"require_proposal_for_risk":"critical","active":true,"created_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E6%B2%BB%E7%90%86/intent-locks?include_snapshot=true" else {
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
                  json?["rule"] as? String == "高风险需要人工审批",
                  let blockedPaths = json?["blocked_paths"] as? [String],
                  blockedPaths == ["src/core"],
                  let allowedPaths = json?["allowed_paths"] as? [String],
                  allowedPaths == ["src/core/README.md"],
                  json?["require_proposal_for_risk"] as? String == "critical" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "create_intent_lock": "/workbench-v2/sessions/{session_id}/missions/{mission_id}/intent-locks",
        ])
        let response = try await client.createIntentLockWithSnapshot(
            sessionID: sessionID,
            missionID: missionID,
            actor: "Planner-Agent",
            rule: "高风险需要人工审批",
            blockedPaths: ["src/core"],
            allowedPaths: ["src/core/README.md"],
            requireProposalForRisk: "critical"
        )

        #expect(response.intentLock.id == "lock-template")
        #expect(response.intentLock.missionID == missionID)
        #expect(response.intentLock.requireProposalForRisk == "critical")
        #expect(response.snapshot.sessionID == sessionID)
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

    @Test func createDecisionWithSnapshotRequestsFreshSnapshot() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let responseJSON = Data(
            """
            {"decision":{"id":"decision-001","session_id":"sess 中文","mission_id":"mission 中文","kind":"architecture","title":"采用 FastAPI","content":"使用 FastAPI 承载 Workbench API","actor":"Planner-Agent","created_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess 中文","summary":{"current_mission_title":"治理决策更新","active_agents":0,"open_issues":0,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/decisions?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.createDecisionWithSnapshot(
            sessionID: sessionID,
            missionID: missionID,
            kind: "architecture",
            title: "采用 FastAPI",
            content: "使用 FastAPI 承载 Workbench API",
            actor: "Planner-Agent"
        )

        #expect(response.decision.id == "decision-001")
        #expect(response.decision.missionID == missionID)
        #expect(response.snapshot.sessionID == sessionID)
        #expect(response.snapshot.summary?.currentMissionTitle == "治理决策更新")
    }

    @Test func createDecisionWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/治理"
        let responseJSON = Data(
            """
            {"decision":{"id":"decision-template","session_id":"sess/中文","mission_id":"mission/治理","kind":"policy","title":"保留人工审批","content":"高风险任务必须经过人工审批","actor":"Planner-Agent","created_at":"2026-06-27T06:00:00"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E6%B2%BB%E7%90%86/decisions?include_snapshot=true" else {
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
                  json?["kind"] as? String == "policy",
                  json?["title"] as? String == "保留人工审批",
                  json?["content"] as? String == "高风险任务必须经过人工审批" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 201,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "create_decision": "/workbench-v2/sessions/{session_id}/missions/{mission_id}/decisions",
        ])
        let response = try await client.createDecisionWithSnapshot(
            sessionID: sessionID,
            missionID: missionID,
            kind: "policy",
            title: "保留人工审批",
            content: "高风险任务必须经过人工审批",
            actor: "Planner-Agent"
        )

        #expect(response.decision.id == "decision-template")
        #expect(response.decision.missionID == missionID)
        #expect(response.decision.kind == "policy")
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func resolveApprovalUsesPOSTAndEncodesPathAndBody() async throws {
        let sessionID = "sess 中文"
        let approvalID = "approval 001 审批"
        let approvalJSON = Data(
            """
            {"id":"approval 001 审批","session_id":"sess 中文","mission_id":"mission-001","task_id":"task-001","state":"approved","title":"允许重构","detail":"保持测试通过","requester":"Agent-A","reviewer":"Human","decision_note":"同意","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:01","task":{"id":"task-001","session_id":"sess 中文","subject":"审批 API 合同","description":"确认审查页保留任务上下文","status":"in_progress","active_form":"issue-risk-approval","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}}
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
        #expect(approval.task?.id == "task-001")
        #expect(approval.task?.subject == "审批 API 合同")
        #expect(approval.task?.status == "in_progress")
        #expect(approval.task?.activeForm == "issue-risk-approval")
        #expect(approval.task?.owner == "Reviewer-Agent")
    }

    @Test func resolveApprovalWithSnapshotRequestsFreshSnapshot() async throws {
        let sessionID = "sess 中文"
        let approvalID = "approval 001 审批"
        let responseJSON = Data(
            """
            {"approval":{"id":"approval 001 审批","session_id":"sess 中文","mission_id":"mission-001","task_id":"task-001","state":"approved","title":"允许重构","detail":"保持测试通过","requester":"Agent-A","reviewer":"Human","decision_note":"同意","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:01","task":{"id":"task-001","session_id":"sess 中文","subject":"审批 API 合同","description":"确认审查页保留任务上下文","status":"in_progress","active_form":"issue-risk-approval","owner":"Reviewer-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:59:00","updated_at":"2026-06-27T05:59:30"}},"snapshot":{"session_id":"sess 中文","summary":{"current_mission_title":"审批已更新","active_agents":0,"open_issues":0,"blocked_issues":0,"pending_approvals":0,"failed_validations":0},"missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/approvals/approval%20001%20%E5%AE%A1%E6%89%B9/resolve?include_snapshot=true" else {
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
            return (response, responseJSON)
        }

        let client = makeClient()
        let response = try await client.resolveApprovalWithSnapshot(
            sessionID: sessionID,
            approvalID: approvalID,
            actor: "Human",
            state: "approved",
            decisionNote: "同意"
        )

        #expect(response.approval.id == approvalID)
        #expect(response.approval.state == "approved")
        #expect(response.approval.task?.id == "task-001")
        #expect(response.approval.task?.subject == "审批 API 合同")
        #expect(response.snapshot.sessionID == sessionID)
        #expect(response.snapshot.summary?.currentMissionTitle == "审批已更新")
    }

    @Test func resolveApprovalWithSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let approvalID = "approval/人工审批"
        let responseJSON = Data(
            """
            {"approval":{"id":"approval/人工审批","session_id":"sess/中文","mission_id":"mission-001","task_id":"task-001","state":"approved","title":"允许发布","detail":"验证已通过","requester":"Agent-A","reviewer":"Human","decision_note":"证据充分","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:00:01"},"snapshot":{"session_id":"sess/中文","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/approvals/approval%2F%E4%BA%BA%E5%B7%A5%E5%AE%A1%E6%89%B9/resolve?include_snapshot=true" else {
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
                  json?["decision_note"] as? String == "证据充分" else {
                fatalError("Unexpected body: \(String(describing: json))")
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseJSON)
        }

        let client = makeClient(routeTemplates: [
            "resolve_approval": "/workbench-v2/sessions/{session_id}/approvals/{approval_id}/resolve",
        ])
        let response = try await client.resolveApprovalWithSnapshot(
            sessionID: sessionID,
            approvalID: approvalID,
            actor: "Human",
            state: "approved",
            decisionNote: "证据充分"
        )

        #expect(response.approval.id == approvalID)
        #expect(response.approval.state == "approved")
        #expect(response.approval.decisionNote == "证据充分")
        #expect(response.snapshot.sessionID == sessionID)
    }

    @Test func fetchSnapshotEncodesSlashInSessionID() async throws {
        let sessionID = "sess/中文"
        let snapshotJSON = Data(
            """
            {"session_id":"sess/中文","missions":[],"tasks":[{"id":"task-001","session_id":"sess/中文","subject":"首屏 Issue 任务摘要","description":"snapshot issues 直接携带 task","status":"in_progress","active_form":"issue-snapshot-task","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}],"issues":[{"session_id":"sess/中文","task_id":"task-001","mission_id":"mission-001","parallel_mode":"exclusive","risk_level":"medium","requires_human_approval":true,"acceptance_criteria":["首屏可见任务标题"],"expected_artifacts":[],"related_branch":"","related_worktree":"","related_pr":"","created_at":"2026-06-27T06:00:00","updated_at":"2026-06-27T06:10:00","task":{"id":"task-001","session_id":"sess/中文","subject":"首屏 Issue 任务摘要","description":"snapshot issues 直接携带 task","status":"in_progress","active_form":"issue-snapshot-task","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}],"validation_runs":[{"id":"run-001","session_id":"sess/中文","task_id":"task-001","actor":"ValidationRunner","command":["pytest","test_a.py"],"cwd":"/workspace","status":"passed","exit_code":0,"output":"ok","started_at":"2026-06-27T06:00:00","completed_at":"2026-06-27T06:00:01","task":{"id":"task-001","session_id":"sess/中文","subject":"首屏 Issue 任务摘要","description":"snapshot issues 直接携带 task","status":"in_progress","active_form":"issue-snapshot-task","owner":"Backend-Agent","blocks":[],"blocked_by":[],"created_at":"2026-06-27T05:00:00","updated_at":"2026-06-27T05:10:00"}}],"failures":[],"events":[]}
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
        #expect(snapshot.tasks.first?.subject == "首屏 Issue 任务摘要")
        #expect(snapshot.issues.first?.taskID == "task-001")
        #expect(snapshot.issues.first?.task?.subject == "首屏 Issue 任务摘要")
        #expect(snapshot.issues.first?.task?.activeForm == "issue-snapshot-task")
        #expect(snapshot.validationRuns.first?.id == "run-001")
        #expect(snapshot.validationRuns.first?.task?.subject == "首屏 Issue 任务摘要")
        #expect(snapshot.validationRuns.first?.task?.activeForm == "issue-snapshot-task")
        #expect(snapshot.failures.isEmpty)
        #expect(snapshot.events.isEmpty)
    }

    @Test func fetchSnapshotUsesConfiguredRouteTemplate() async throws {
        let sessionID = "sess/中文"
        let snapshotJSON = Data(
            """
            {"session_id":"sess/中文","missions":[],"tasks":[],"issues":[],"failures":[],"events":[]}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench-v2/sessions/sess%2F%E4%B8%AD%E6%96%87/snapshot" else {
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

        let client = makeClient(routeTemplates: [
            "snapshot": "/workbench-v2/sessions/{session_id}/snapshot",
        ])
        let snapshot = try await client.fetchSnapshot(sessionID: sessionID)

        #expect(snapshot.sessionID == sessionID)
        #expect(snapshot.tasks.isEmpty)
        #expect(snapshot.issues.isEmpty)
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

    private func makeClient(
        bearerToken: String? = nil,
        routeTemplates: [String: String] = [:]
    ) -> WorkbenchAPIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: configuration)
        return WorkbenchAPIClient(
            session: session,
            bearerToken: bearerToken,
            routeTemplates: routeTemplates
        )
    }

    @Test func fetchIntentLocksEncodesPathAndDecodesResponse() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let active = true
        let json = Data(
            """
            {"intent_locks":[{"id":"lock-001","session_id":"sess 中文","mission_id":"mission 中文","rule":"禁止修改 core 模块","blocked_paths":["src/secret"],"allowed_paths":["src/secret/README.md"],"require_proposal_for_risk":"high","active":true,"created_at":"2026-06-27T06:00:00"}],"mission_id":"mission 中文","active":true}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/intent-locks",
                  query["active"] == "true" else {
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
        let response = try await client.fetchIntentLocks(
            sessionID: sessionID,
            missionID: missionID,
            active: active
        )

        #expect(response.missionID == missionID)
        #expect(response.active == active)
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

    @Test func fetchIntentLockEncodesPathComponentsAndDecodesPolicy() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/审查"
        let lockID = "lock/核心 001"
        let json = Data(
            """
            {"id":"lock/核心 001","session_id":"sess/中文","mission_id":"mission/审查","rule":"高风险变更必须先提案","blocked_paths":["src/naumi_agent/core"],"allowed_paths":["docs/adr"],"require_proposal_for_risk":"high","active":true,"created_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E5%AE%A1%E6%9F%A5/intent-locks/lock%2F%E6%A0%B8%E5%BF%83%20001" else {
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
        let lock = try await client.fetchIntentLock(sessionID: sessionID, missionID: missionID, lockID: lockID)

        #expect(lock.id == lockID)
        #expect(lock.sessionID == sessionID)
        #expect(lock.missionID == missionID)
        #expect(lock.rule == "高风险变更必须先提案")
        #expect(lock.blockedPaths == ["src/naumi_agent/core"])
        #expect(lock.allowedPaths == ["docs/adr"])
        #expect(lock.requireProposalForRisk == "high")
        #expect(lock.active)
        #expect(lock.createdAt == "2026-06-27T06:00:00")
    }

    @Test func fetchDecisionsEncodesPathAndDecodesResponse() async throws {
        let sessionID = "sess 中文"
        let missionID = "mission 中文"
        let kind = "policy"
        let json = Data(
            """
            {"decisions":[{"id":"decision-001","session_id":"sess 中文","mission_id":"mission 中文","kind":"policy","title":"采用 FastAPI","content":"使用 FastAPI 承载 Workbench API","actor":"Planner-Agent","created_at":"2026-06-27T06:00:00"}],"mission_id":"mission 中文","kind":"policy"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
            let query = Dictionary(
                uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            )
            guard components?.percentEncodedPath == "/api/v1/workbench/sessions/sess%20%E4%B8%AD%E6%96%87/missions/mission%20%E4%B8%AD%E6%96%87/decisions",
                  query["kind"] == kind else {
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
        let response = try await client.fetchDecisions(
            sessionID: sessionID,
            missionID: missionID,
            kind: kind
        )

        #expect(response.missionID == missionID)
        #expect(response.kind == kind)
        #expect(response.decisions.count == 1)

        let decision = try #require(response.decisions.first)
        #expect(decision.id == "decision-001")
        #expect(decision.sessionID == sessionID)
        #expect(decision.missionID == missionID)
        #expect(decision.kind == "policy")
        #expect(decision.title == "采用 FastAPI")
        #expect(decision.content == "使用 FastAPI 承载 Workbench API")
        #expect(decision.actor == "Planner-Agent")
        #expect(decision.createdAt == "2026-06-27T06:00:00")
    }

    @Test func fetchDecisionEncodesPathComponentsAndDecodesGovernanceRecord() async throws {
        let sessionID = "sess/中文"
        let missionID = "mission/审查"
        let decisionID = "decision/架构 001"
        let json = Data(
            """
            {"id":"decision/架构 001","session_id":"sess/中文","mission_id":"mission/审查","kind":"architecture","title":"采用本地 REST API","content":"SwiftUI 只通过 Workbench API 读取治理状态","actor":"Planner-Agent","created_at":"2026-06-27T06:00:00"}
            """.utf8
        )

        MockURLProtocol.requestHandler = { request in
            guard request.url?.absoluteString == "http://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/missions/mission%2F%E5%AE%A1%E6%9F%A5/decisions/decision%2F%E6%9E%B6%E6%9E%84%20001" else {
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
        let decision = try await client.fetchDecision(
            sessionID: sessionID,
            missionID: missionID,
            decisionID: decisionID
        )

        #expect(decision.id == decisionID)
        #expect(decision.sessionID == sessionID)
        #expect(decision.missionID == missionID)
        #expect(decision.kind == "architecture")
        #expect(decision.title == "采用本地 REST API")
        #expect(decision.content == "SwiftUI 只通过 Workbench API 读取治理状态")
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
        let response = try await client.fetchIntentLocks(
            sessionID: sessionID,
            missionID: missionID,
            active: nil
        )

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
        let response = try await client.fetchDecisions(
            sessionID: sessionID,
            missionID: missionID,
            kind: nil
        )

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
