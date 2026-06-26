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

    // MARK: - Private

    private func get<T: Decodable & Sendable>(path: String) async throws(APIError) -> T {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw .invalidURL
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(from: url)
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
}
