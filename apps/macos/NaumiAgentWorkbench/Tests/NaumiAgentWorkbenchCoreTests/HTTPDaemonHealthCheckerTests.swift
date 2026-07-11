import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

@Suite(.serialized)
struct HTTPDaemonHealthCheckerTests {
    @Test func returnsTrueForSuccessfulDaemonStatusResponse() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SuccessfulHealthURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let checker = HTTPDaemonHealthChecker(session: session, pollInterval: 0)
        let endpoint = try #require(URL(string: "http://127.0.0.1:8765/api/v1/"))

        let isHealthy = await checker.waitForHealth(
            endpoint: endpoint,
            bearerToken: nil,
            timeout: 0.1
        )

        #expect(isHealthy)
    }
}

private final class SuccessfulHealthURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: nil
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data())
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
