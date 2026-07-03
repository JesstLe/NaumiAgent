import Foundation

/// Typed messages emitted by the Workbench event WebSocket.
public enum WorkbenchEventStreamMessage: Equatable, Sendable {
    case connected(sessionID: String)
    case snapshot(WorkbenchSnapshotDTO)
    case event(EventDTO)
    case refreshComplete(count: Int)
    case pong
    case error(message: String)
    case ignored(type: String)
}

/// Event stream returned by `WorkbenchEventProviding`.
public protocol WorkbenchEventStreaming: Sendable {
    func next() async throws(APIError) -> WorkbenchEventStreamMessage
    func sendPing() async throws(APIError)
    func requestRefresh(
        eventType: String?,
        subjectID: String?,
        actor: String?,
        since: String?,
        limit: Int
    ) async throws(APIError)
    func cancel() async
}

/// Event client abstraction used by `DaemonController`.
public protocol WorkbenchEventProviding: Sendable {
    func connect(sessionID: String) async throws(APIError) -> any WorkbenchEventStreaming
}

/// Optional event-provider capability for daemon-supplied stream URL templates.
public protocol WorkbenchEventStreamTemplateConfiguring: Sendable {
    func setEventStreamURLTemplate(_ template: String?) async
}

/// Minimal task surface used by `WorkbenchEventClient`.
public protocol WorkbenchWebSocketTasking: Sendable {
    func start() async
    func cancelStream(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) async
    func receiveMessage() async throws -> URLSessionWebSocketTask.Message
    func sendMessage(_ message: URLSessionWebSocketTask.Message) async throws
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

    public func sendMessage(_ message: URLSessionWebSocketTask.Message) async throws {
        try await send(message)
    }
}

extension URLSession: WorkbenchWebSocketTransporting {
    public func makeWebSocketTask(with request: URLRequest) async -> WorkbenchWebSocketTasking {
        webSocketTask(with: request)
    }
}

/// WebSocket client for Workbench audit/event updates.
///
/// Snapshot remains the source of truth. The stream asks the daemon for an
/// initial snapshot, then carries event hints for lightweight follow-up refreshes.
public actor WorkbenchEventClient: Sendable, WorkbenchEventProviding, WorkbenchEventStreamTemplateConfiguring {
    public let baseURL: URL
    private let transport: WorkbenchWebSocketTransporting
    private let bearerToken: String?
    private var eventStreamURLTemplate: String?

    public init(
        baseURL: URL = URL(string: "http://127.0.0.1:8765/api/v1/")!,
        transport: WorkbenchWebSocketTransporting = URLSession.shared,
        bearerToken: String? = nil,
        eventStreamURLTemplate: String? = nil
    ) {
        let baseURLString = baseURL.absoluteString
        if baseURLString.hasSuffix("/") {
            self.baseURL = baseURL
        } else {
            self.baseURL = URL(string: baseURLString + "/")!
        }
        self.transport = transport
        self.bearerToken = bearerToken
        self.eventStreamURLTemplate = eventStreamURLTemplate
    }

    public static func eventStreamURL(
        baseURL: URL,
        sessionID: String,
        includeSnapshot: Bool = false,
        eventStreamURLTemplate: String? = nil
    ) throws(APIError) -> URL {
        if let eventStreamURLTemplate,
           !eventStreamURLTemplate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return try eventStreamURLFromTemplate(
                eventStreamURLTemplate,
                sessionID: sessionID,
                includeSnapshot: includeSnapshot
            )
        }

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
        if includeSnapshot {
            components?.queryItems = [URLQueryItem(name: "include_snapshot", value: "true")]
        }

        guard let url = components?.url else {
            throw .invalidURL
        }
        return url
    }

    public func connect(sessionID: String) async throws(APIError) -> any WorkbenchEventStreaming {
        let url = try Self.eventStreamURL(
            baseURL: baseURL,
            sessionID: sessionID,
            includeSnapshot: true,
            eventStreamURLTemplate: eventStreamURLTemplate
        )
        var request = URLRequest(url: url)
        if let bearerToken, !bearerToken.isEmpty {
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        }

        let task = await transport.makeWebSocketTask(with: request)
        await task.start()
        return WorkbenchEventStream(task: task)
    }

    public func setEventStreamURLTemplate(_ template: String?) async {
        eventStreamURLTemplate = template
    }

    private static func eventStreamURLFromTemplate(
        _ template: String,
        sessionID: String,
        includeSnapshot: Bool
    ) throws(APIError) -> URL {
        guard template.contains("{session_id}") else {
            throw .invalidURL
        }

        let encodedSessionID = encodePathSegment(sessionID)
        let expanded = template.replacingOccurrences(
            of: "{session_id}",
            with: encodedSessionID
        )
        guard var components = URLComponents(string: expanded) else {
            throw .invalidURL
        }
        guard components.scheme == "ws" || components.scheme == "wss" else {
            throw .invalidURL
        }

        if includeSnapshot {
            var queryItems = components.queryItems ?? []
            queryItems.removeAll { $0.name == "include_snapshot" }
            queryItems.append(URLQueryItem(name: "include_snapshot", value: "true"))
            components.queryItems = queryItems
        }

        guard let url = components.url else {
            throw .invalidURL
        }
        return url
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

public struct WorkbenchEventStream: Sendable, WorkbenchEventStreaming {
    private let task: WorkbenchWebSocketTasking
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    public init(
        task: WorkbenchWebSocketTasking,
        decoder: JSONDecoder = JSONDecoder(),
        encoder: JSONEncoder = JSONEncoder()
    ) {
        self.task = task
        self.decoder = decoder
        self.encoder = encoder
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

    public func sendPing() async throws(APIError) {
        let request = WorkbenchEventPingRequest()
        let data: Data
        do {
            data = try encoder.encode(request)
        } catch {
            throw .decodingFailed(String(describing: error))
        }

        guard let text = String(data: data, encoding: .utf8) else {
            throw .decodingFailed("ping request is not UTF-8 JSON")
        }

        do {
            try await task.sendMessage(.string(text))
        } catch {
            throw .networkFailure(String(describing: error))
        }
    }

    public func requestRefresh(
        eventType: String? = nil,
        subjectID: String? = nil,
        actor: String? = nil,
        since: String? = nil,
        limit: Int
    ) async throws(APIError) {
        let request = WorkbenchEventRefreshRequest(
            eventType: eventType,
            subjectID: subjectID,
            actor: actor,
            since: since,
            limit: limit
        )
        let data: Data
        do {
            data = try encoder.encode(request)
        } catch {
            throw .decodingFailed(String(describing: error))
        }

        guard let text = String(data: data, encoding: .utf8) else {
            throw .decodingFailed("refresh request is not UTF-8 JSON")
        }

        do {
            try await task.sendMessage(.string(text))
        } catch {
            throw .networkFailure(String(describing: error))
        }
    }
}

private struct WorkbenchEventPingRequest: Encodable {
    let type = "ping"
}

private struct WorkbenchEventRefreshRequest: Encodable {
    let type = "refresh"
    let eventType: String?
    let subjectID: String?
    let actor: String?
    let since: String?
    let limit: Int

    private enum CodingKeys: String, CodingKey {
        case type
        case eventType = "event_type"
        case subjectID = "subject_id"
        case actor
        case since
        case limit
    }
}

private struct WorkbenchEventEnvelope: Decodable {
    let type: String
    let sessionID: String?
    let event: EventDTO?
    let eventPayload: EventDTO?
    let snapshotPayload: WorkbenchSnapshotDTO?
    let message: String?
    let count: Int?

    private enum CodingKeys: String, CodingKey {
        case type
        case sessionID = "session_id"
        case event
        case payload
        case message
        case count
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        type = try container.decode(String.self, forKey: .type)
        sessionID = try container.decodeIfPresent(String.self, forKey: .sessionID)
        event = try container.decodeIfPresent(EventDTO.self, forKey: .event)
        message = try container.decodeIfPresent(String.self, forKey: .message)
        count = try container.decodeIfPresent(Int.self, forKey: .count)

        if type == "workbench/snapshot" {
            snapshotPayload = try container.decodeIfPresent(
                WorkbenchSnapshotDTO.self,
                forKey: .payload
            )
            eventPayload = nil
        } else if type == "workbench/event" {
            eventPayload = try container.decodeIfPresent(EventDTO.self, forKey: .payload)
            snapshotPayload = nil
        } else {
            eventPayload = nil
            snapshotPayload = nil
        }
    }

    func streamMessage() throws(APIError) -> WorkbenchEventStreamMessage {
        switch type {
        case "connected":
            return .connected(sessionID: sessionID ?? "")
        case "workbench/snapshot":
            guard let snapshotPayload else {
                throw APIError.decodingFailed("workbench/snapshot missing payload")
            }
            return .snapshot(snapshotPayload)
        case "workbench/event":
            guard let eventPayload else {
                throw APIError.decodingFailed("workbench/event missing event payload")
            }
            return .event(eventPayload)
        case "workbench.event":
            guard let event else {
                throw APIError.decodingFailed("workbench.event missing event payload")
            }
            return .event(event)
        case "refresh_complete":
            return .refreshComplete(count: count ?? 0)
        case "pong":
            return .pong
        case "error":
            return .error(message: message ?? "")
        default:
            return .ignored(type: type)
        }
    }
}
