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

    // MARK: - Helpers

    private func makeClient() -> WorkbenchAPIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: configuration)
        return WorkbenchAPIClient(session: session)
    }
}
