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

    // MARK: - Helpers

    private func makeClient() -> WorkbenchAPIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: configuration)
        return WorkbenchAPIClient(session: session)
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
