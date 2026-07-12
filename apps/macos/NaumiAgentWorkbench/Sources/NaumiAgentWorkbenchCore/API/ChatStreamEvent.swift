import Foundation

/// Events emitted by the Workbench message streaming endpoint.
public enum ChatStreamEventType: Equatable, Sendable {
    case turnStart
    case tokenDelta
    case thinkingStart
    case thinkingDelta
    case thinkingEnd
    case toolCallStart
    case toolCallEnd
    case toolCallError
    case agentStart
    case agentEnd
    case agentError
    case permissionRequest
    case unknown(String)
}

extension ChatStreamEventType: Decodable {
    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        switch value {
        case "turn_start":
            self = .turnStart
        case "token_delta":
            self = .tokenDelta
        case "thinking_start":
            self = .thinkingStart
        case "thinking_delta":
            self = .thinkingDelta
        case "thinking_end":
            self = .thinkingEnd
        case "tool_call_start":
            self = .toolCallStart
        case "tool_call_end":
            self = .toolCallEnd
        case "tool_call_error":
            self = .toolCallError
        case "agent_start":
            self = .agentStart
        case "agent_end":
            self = .agentEnd
        case "agent_error":
            self = .agentError
        case "permission_request":
            self = .permissionRequest
        default:
            self = .unknown(value)
        }
    }
}

/// One safe, server-provided event in a streamed assistant turn.
public struct ChatStreamEvent: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let type: ChatStreamEventType
    public let data: [String: JSONValue]
    public let timestamp: String
    public let sessionID: String
    public let turn: Int

    public init(
        id: String,
        type: ChatStreamEventType,
        data: [String: JSONValue],
        timestamp: String = "",
        sessionID: String = "",
        turn: Int = 0
    ) {
        self.id = id
        self.type = type
        self.data = data
        self.timestamp = timestamp
        self.sessionID = sessionID
        self.turn = turn
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case type
        case data
        case timestamp
        case sessionID = "session_id"
        case turn
    }
}

extension ChatStreamEvent {
    /// The daemon may keep the underlying HTTP connection alive after a turn.
    /// Only this final event marks a completed assistant stream.
    var terminatesChatStream: Bool {
        switch type {
        case .agentError:
            return true
        case .agentEnd:
            guard case .string(let status) = data["status"] else { return false }
            return status == "completed" || status == "failed"
        default:
            return false
        }
    }
}

/// A user decision for one daemon-issued tool permission request.
public enum ChatPermissionDecision: String, Sendable {
    case allow
    case deny
    case bypass
}

/// Optional capability implemented by API providers that support streamed chat.
public protocol ChatStreamingProviding: Sendable {
    func streamMessage(
        sessionID: String,
        content: String,
        onEvent: @escaping @Sendable (ChatStreamEvent) async -> Void
    ) async throws(APIError)

    func resolveChatPermission(
        sessionID: String,
        callID: String,
        decision: ChatPermissionDecision
    ) async throws(APIError)
}

public protocol ChatContextStreamingProviding: Sendable {
    func streamMessage(
        sessionID: String,
        content: String,
        sourceIDs: [String],
        linkedIssueID: String?,
        onEvent: @escaping @Sendable (ChatStreamEvent) async -> Void
    ) async throws(APIError)
}
