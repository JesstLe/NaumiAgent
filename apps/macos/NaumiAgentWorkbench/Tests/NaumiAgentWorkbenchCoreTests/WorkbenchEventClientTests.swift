import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

@Suite
final class WorkbenchEventClientTests {
    @Test func eventStreamURLUsesWebSocketSchemeAndEncodedSessionID() throws {
        let url = try WorkbenchEventClient.eventStreamURL(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1")!,
            sessionID: "sess/中文"
        )

        #expect(
            url.absoluteString
                == "ws://127.0.0.1:8765/api/v1/workbench/sessions/sess%2F%E4%B8%AD%E6%96%87/events/stream"
        )
    }

    @Test func eventStreamURLPreservesHTTPSAsWSS() throws {
        let url = try WorkbenchEventClient.eventStreamURL(
            baseURL: URL(string: "https://localhost:9000/api/v1/")!,
            sessionID: "sess-001"
        )

        #expect(url.absoluteString == "wss://localhost:9000/api/v1/workbench/sessions/sess-001/events/stream")
    }

    @Test func eventStreamURLCanRequestInitialSnapshot() throws {
        let url = try WorkbenchEventClient.eventStreamURL(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            sessionID: "sess-001",
            includeSnapshot: true
        )

        #expect(
            url.absoluteString
                == "ws://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/events/stream?include_snapshot=true"
        )
    }

    @Test func connectStartsTaskAndAddsBearerToken() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [
            .string(#"{"type":"connected","session_id":"sess-001"}"#),
        ])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport,
            bearerToken: "local-token"
        )

        let stream = try await client.connect(sessionID: "sess-001")
        let request = try #require(await transport.requests.first)

        #expect(
            request.url?.absoluteString
                == "ws://127.0.0.1:8765/api/v1/workbench/sessions/sess-001/events/stream?include_snapshot=true"
        )
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer local-token")
        #expect(await transport.task.resumeCallCount == 1)

        let message = try await stream.next()
        #expect(message == .connected(sessionID: "sess-001"))
    }

    @Test func nextDecodesWorkbenchEventEnvelope() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [
            .string(
                """
                {"type":"workbench.event","event":{"id":"evt-001","session_id":"sess-001","type":"issue.claimed","actor":"Backend-Agent","subject_id":"task-001","payload":{"lease_id":"lease-001"},"timestamp":"2026-06-27T06:00:00"}}
                """
            ),
        ])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport
        )

        let stream = try await client.connect(sessionID: "sess-001")
        let message = try await stream.next()

        guard case .event(let event) = message else {
            Issue.record("Expected workbench event, got \(message)")
            return
        }
        #expect(event.id == "evt-001")
        #expect(event.type == "issue.claimed")
        #expect(event.payload["lease_id"] == .string("lease-001"))
    }

    @Test func nextDecodesWorkbenchSnapshotEnvelope() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [
            .string(
                """
                {"type":"workbench/snapshot","version":1,"payload":{"session_id":"sess-001","missions":[],"agent_profiles":[],"tasks":[],"issues":[],"leases":[],"failures":[],"events":[]}}
                """
            ),
        ])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport
        )

        let stream = try await client.connect(sessionID: "sess-001")
        let message = try await stream.next()

        guard case .snapshot(let snapshot) = message else {
            Issue.record("Expected workbench snapshot, got \(message)")
            return
        }
        #expect(snapshot.sessionID == "sess-001")
        #expect(snapshot.missions.isEmpty)
    }

    @Test func nextDecodesRefreshCompleteEnvelope() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [
            .string(#"{"type":"refresh_complete","count":12}"#),
        ])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport
        )

        let stream = try await client.connect(sessionID: "sess-001")
        let message = try await stream.next()

        #expect(message == .refreshComplete(count: 12))
    }

    @Test func nextDecodesPongEnvelope() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [
            .string(#"{"type":"pong"}"#),
        ])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport
        )

        let stream = try await client.connect(sessionID: "sess-001")
        let message = try await stream.next()

        #expect(message == .pong)
    }

    @Test func sendPingSendsPingMessage() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport
        )

        let stream = try await client.connect(sessionID: "sess-001")
        try await stream.sendPing()

        let sentMessages = await transport.task.sentMessages
        #expect(sentMessages.count == 1)

        guard case .string(let text) = try #require(sentMessages.first) else {
            Issue.record("Expected ping message to be sent as text JSON")
            return
        }
        let payload = try #require(text.data(using: .utf8))
        let json = try #require(
            try JSONSerialization.jsonObject(with: payload) as? [String: Any]
        )
        #expect(json["type"] as? String == "ping")
    }

    @Test func requestRefreshSendsFilteredRefreshMessage() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport
        )

        let stream = try await client.connect(sessionID: "sess-001")
        try await stream.requestRefresh(
            eventType: "validation.passed",
            subjectID: "task-001",
            actor: "Backend-Agent",
            since: "2026-06-27T10:00:00+00:00",
            limit: 25
        )

        let sentMessages = await transport.task.sentMessages
        #expect(sentMessages.count == 1)

        guard case .string(let text) = try #require(sentMessages.first) else {
            Issue.record("Expected refresh message to be sent as text JSON")
            return
        }
        let payload = try #require(text.data(using: .utf8))
        let json = try #require(
            try JSONSerialization.jsonObject(with: payload) as? [String: Any]
        )
        #expect(json["type"] as? String == "refresh")
        #expect(json["event_type"] as? String == "validation.passed")
        #expect(json["subject_id"] as? String == "task-001")
        #expect(json["actor"] as? String == "Backend-Agent")
        #expect(json["since"] as? String == "2026-06-27T10:00:00+00:00")
        #expect(json["limit"] as? Int == 25)
    }

    @Test func nextRejectsMalformedEventEnvelope() async throws {
        let transport = RecordingWorkbenchWebSocketTransport(messages: [
            .string(#"{"type":"workbench.event"}"#),
        ])
        let client = WorkbenchEventClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            transport: transport
        )

        let stream = try await client.connect(sessionID: "sess-001")
        await #expect(throws: APIError.decodingFailed("workbench.event missing event payload")) {
            _ = try await stream.next()
        }
    }
}

private actor RecordingWorkbenchWebSocketTransport: WorkbenchWebSocketTransporting {
    let task: RecordingWorkbenchWebSocketTask
    private(set) var requests: [URLRequest] = []

    init(messages: [URLSessionWebSocketTask.Message]) {
        self.task = RecordingWorkbenchWebSocketTask(messages: messages)
    }

    func makeWebSocketTask(with request: URLRequest) -> WorkbenchWebSocketTasking {
        requests.append(request)
        return task
    }
}

private actor RecordingWorkbenchWebSocketTask: WorkbenchWebSocketTasking {
    private var messages: [URLSessionWebSocketTask.Message]
    private(set) var resumeCallCount = 0
    private(set) var cancelCallCount = 0
    private(set) var sentMessages: [URLSessionWebSocketTask.Message] = []

    init(messages: [URLSessionWebSocketTask.Message]) {
        self.messages = messages
    }

    func start() {
        resumeCallCount += 1
    }

    func cancelStream(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        cancelCallCount += 1
    }

    func sendMessage(_ message: URLSessionWebSocketTask.Message) async throws {
        sentMessages.append(message)
    }

    func receiveMessage() async throws -> URLSessionWebSocketTask.Message {
        guard !messages.isEmpty else {
            throw URLError(.networkConnectionLost)
        }
        return messages.removeFirst()
    }
}
