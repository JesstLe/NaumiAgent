import Foundation

/// A user-visible phase for a single assistant turn.
public enum ChatExecutionStage: Equatable, Sendable {
    case preparing
    case analyzing
    case runningTool
    case awaitingApproval
    case composing
    case creatingLinkedIssue
    case completed
    case failed
}

/// Safe metadata needed to let a user approve or deny a tool operation.
public struct ChatPermissionRequest: Equatable, Sendable {
    public let callID: String
    public let toolName: String
    public let reason: String
    public let riskLevel: String

    public init(callID: String, toolName: String, reason: String, riskLevel: String) {
        self.callID = callID
        self.toolName = toolName
        self.reason = reason
        self.riskLevel = riskLevel
    }
}

/// Pure state reducer used by the chat UI while a streamed response is running.
///
/// This model intentionally never reads model thinking content or tool arguments.
public struct ChatExecutionPresentation: Equatable, Sendable, Identifiable {
    public let id: String
    public private(set) var stage: ChatExecutionStage
    public let startedAt: Date
    public private(set) var completedAt: Date?
    public private(set) var activeToolName: String?
    public private(set) var permission: ChatPermissionRequest?
    public private(set) var partialResponse: String
    public private(set) var toolResultSummary: String?
    public private(set) var failureMessage: String?
    public private(set) var isResolvingPermission: Bool

    public init(
        id: String,
        stage: ChatExecutionStage = .preparing,
        startedAt: Date = Date()
    ) {
        self.id = id
        self.stage = stage
        self.startedAt = startedAt
        self.completedAt = nil
        self.activeToolName = nil
        self.permission = nil
        self.partialResponse = ""
        self.toolResultSummary = nil
        self.failureMessage = nil
        self.isResolvingPermission = false
    }

    public func applying(_ event: ChatStreamEvent, at date: Date = Date()) -> Self {
        var next = self

        switch event.type {
        case .turnStart, .thinkingStart, .thinkingDelta, .thinkingEnd:
            next.stage = .analyzing

        case .toolCallStart:
            next.stage = .runningTool
            next.activeToolName = event.stringValue(for: "name")

        case .toolCallEnd:
            next.stage = .analyzing
            next.activeToolName = nil
            if event.stringValue(for: "name") == "delegate_task",
               event.stringValue(for: "status") == "success",
               let content = event.stringValue(for: "content"), !content.isEmpty {
                next.toolResultSummary = Self.compactSummary(content)
            }

        case .toolCallError:
            next.stage = .failed
            next.activeToolName = nil
            next.failureMessage = event.stringValue(for: "message")
                ?? event.stringValue(for: "content")
                ?? "工具执行未完成。"
            next.isResolvingPermission = false
            next.completedAt = date

        case .permissionRequest:
            next.applyPermissionEvent(event, at: date)

        case .agentStart:
            next.stage = .composing

        case .tokenDelta:
            next.stage = .composing
            if let token = event.stringValue(for: "token") {
                next.partialResponse += token
            }

        case .agentEnd:
            next.stage = .completed
            next.activeToolName = nil
            next.permission = nil
            next.isResolvingPermission = false
            next.completedAt = date

        case .agentError:
            next.stage = .failed
            next.activeToolName = nil
            next.permission = nil
            next.failureMessage = event.stringValue(for: "message") ?? "本次对话未能完成。"
            next.isResolvingPermission = false
            next.completedAt = date

        case .unknown:
            break
        }

        return next
    }

    public func resolvingPermission() -> Self {
        guard stage == .awaitingApproval, permission != nil else { return self }
        var next = self
        next.isResolvingPermission = true
        return next
    }

    public func failing(with message: String, at date: Date = Date()) -> Self {
        var next = self
        next.stage = .failed
        next.activeToolName = nil
        next.permission = nil
        next.failureMessage = message
        next.isResolvingPermission = false
        next.completedAt = date
        return next
    }

    private mutating func applyPermissionEvent(_ event: ChatStreamEvent, at date: Date) {
        switch event.stringValue(for: "status") {
        case "needs_confirmation":
            guard let callID = event.stringValue(for: "call_id"),
                  let toolName = event.stringValue(for: "tool_name") else {
                return
            }
            stage = .awaitingApproval
            activeToolName = toolName
            permission = ChatPermissionRequest(
                callID: callID,
                toolName: toolName,
                reason: event.stringValue(for: "reason") ?? "此操作需要你的确认。",
                riskLevel: event.stringValue(for: "risk_level") ?? "unknown"
            )
            isResolvingPermission = false

        case "confirmed", "bypass_enabled":
            stage = .runningTool
            activeToolName = event.stringValue(for: "tool_name") ?? activeToolName
            permission = nil
            isResolvingPermission = false

        case "denied", "confirmation_error":
            stage = .failed
            activeToolName = nil
            permission = nil
            failureMessage = event.stringValue(for: "reason") ?? "权限请求未获批准。"
            isResolvingPermission = false
            completedAt = date

        default:
            break
        }
    }

    private static func compactSummary(_ content: String) -> String {
        let maximumLength = 420
        guard content.count > maximumLength else { return content }
        return "\(content.prefix(maximumLength))..."
    }
}

private extension ChatStreamEvent {
    func stringValue(for key: String) -> String? {
        guard case .string(let value) = data[key] else { return nil }
        return value
    }
}
