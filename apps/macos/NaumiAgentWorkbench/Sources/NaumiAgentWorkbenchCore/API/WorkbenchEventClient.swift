import Foundation

/// Typed messages emitted by the Workbench event WebSocket.
public enum WorkbenchEventStreamMessage: Equatable, Sendable {
    case connected(sessionID: String)
    case event(EventDTO)
    case error(message: String)
    case ignored(type: String)
}

/// Minimal task surface used by `WorkbenchEventClient`.
public protocol WorkbenchWebSocketTasking: Sendable {
    func start() async
    func cancelStream(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) async
    func receiveMessage() async throws -> URLSessionWebSocketTask.Message
}

/// Injectable WebSocket transport for production `URLSession` and tests.
public protocol WorkbenchWebSocketTransporting: Sendable {
    func makeWebSocketTask(with request: URLRequest) async -> WorkbenchWebSocketTasking
}

extension URLSessionWebSocketTask: WorkbenchWebSocketTasking {
    public func start() async {
        resume()
    }

    public func cancelStream(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) async {
        cancel(with: closeCode, reason: reason)
    }

    public func receiveMessage() async throws -> URLSessionWebSocketTask.Message {
        try await receive()
    }
}

extension URLSession: WorkbenchWebSocketTransporting {
    public func makeWebSocketTask(with request: URLRequest) async -> WorkbenchWebSocketTasking {
        webSocketTask(with: request)
    }
}

/// WebSocket client for Workbench audit/event updates.
///
/// Snapshot remains the source of truth. This client only gives SwiftUI a typed
/// event channel so higher layers can treat incoming events as refresh hints.
public actor WorkbenchEventClient: Sendable {
    public let baseURL: URL
    private let transport: WorkbenchWebSocketTransporting
    private let bearerToken: String?

    public init(
        baseURL: URL = URL(string: "http://127.0.0.1:8765/api/v1/")!,
        transport: WorkbenchWebSocketTransporting = URLSession.shared,
        bearerToken: String? = nil
    ) {
        let baseURLString = baseURL.absoluteString
        if baseURLString.hasSuffix("/") {
            self.baseURL = baseURL
        } else {
            self.baseURL = URL(string: baseURLString + "/")!
        }
        self.transport = transport
        self.bearerToken = bearerToken
    }

    public static func eventStreamURL(baseURL: URL, sessionID: String) throws(APIError) -> URL {
        var components = URLComponents(url: normalizedBaseURL(baseURL), resolvingAgainstBaseURL: false)
        switch components?.scheme {
        case "http":
            components?.scheme = "ws"
        case "https":
            components?.scheme = "wss"
        default:
            throw .invalidURL
        }

        let basePath = components?.percentEncodedPath.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? ""
        let encodedSessionID = encodePathSegment(sessionID)
        let streamPath = [basePath, "workbench", "sessions", encodedSessionID, "events", "stream"]
            .filter { !$0.isEmpty }
            .joined(separator: "/")
        components?.percentEncodedPath = "/" + streamPath

        guard let url = components?.url else {
            throw .invalidURL
        }
        return url
    }

    public func connect(sessionID: String) async throws(APIError) -> WorkbenchEventStream {
        let url = try Self.eventStreamURL(baseURL: baseURL, sessionID: sessionID)
        var request = URLRequest(url: url)
        if let bearerToken, !bearerToken.isEmpty {
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        }

        let task = await transport.makeWebSocketTask(with: request)
        await task.start()
        return WorkbenchEventStream(task: task)
    }

    private static func normalizedBaseURL(_ baseURL: URL) -> URL {
        let absolute = baseURL.absoluteString
        if absolute.hasSuffix("/") {
            return baseURL
        }
        return URL(string: absolute + "/") ?? baseURL
    }

    private static func encodePathSegment(_ segment: String) -> String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        return segment.addingPercentEncoding(withAllowedCharacters: allowed) ?? segment
    }
}

public struct WorkbenchEventStream: Sendable {
    private let task: WorkbenchWebSocketTasking
    private let decoder: JSONDecoder

    public init(task: WorkbenchWebSocketTasking, decoder: JSONDecoder = JSONDecoder()) {
        self.task = task
        self.decoder = decoder
    }

    public func next() async throws(APIError) -> WorkbenchEventStreamMessage {
        let message: URLSessionWebSocketTask.Message
        do {
            message = try await task.receiveMessage()
        } catch {
            throw .networkFailure(String(describing: error))
        }

        let data: Data
        switch message {
        case .string(let text):
            data = Data(text.utf8)
        case .data(let payload):
            data = payload
        @unknown default:
            throw .decodingFailed("unsupported WebSocket message")
        }

        do {
            let envelope = try decoder.decode(WorkbenchEventEnvelope.self, from: data)
            return try envelope.streamMessage()
        } catch let error as APIError {
            throw error
        } catch {
            throw .decodingFailed(String(describing: error))
        }
    }

    public func cancel() async {
        await task.cancelStream(with: .goingAway, reason: nil)
    }
}

private struct WorkbenchEventEnvelope: Decodable {
    let type: String
    let sessionID: String?
    let event: EventDTO?
    let message: String?

    private enum CodingKeys: String, CodingKey {
        case type
        case sessionID = "session_id"
        case event
        case message
    }

    func streamMessage() throws(APIError) -> WorkbenchEventStreamMessage {
        switch type {
        case "connected":
            return .connected(sessionID: sessionID ?? "")
        case "workbench.event":
            guard let event else {
                throw APIError.decodingFailed("workbench.event missing event payload")
            }
            return .event(event)
        case "error":
            return .error(message: message ?? "")
        default:
            return .ignored(type: type)
        }
    }
}
