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
    case cancelled
}

public enum ChatExecutionStepKind: Equatable, Sendable {
    case analysis
    case tool
    case response
    case linkedIssue
}

public enum ChatExecutionStepStatus: Equatable, Sendable {
    case running
    case awaitingApproval
    case completed
    case failed
    case cancelled
}

public struct ChatExecutionStep: Equatable, Sendable, Identifiable {
    public let id: String
    public let kind: ChatExecutionStepKind
    public var status: ChatExecutionStepStatus
    public var title: String
    public var detail: String?
    public let startedAt: Date
    public var completedAt: Date?
}

public enum ChatArtifactKind: Equatable, Sendable {
    case command
    case task
    case validation
    case fileChange
    case subagent
}

public struct ChatArtifactPresentation: Equatable, Sendable, Identifiable {
    public let id: String
    public let kind: ChatArtifactKind
    public let title: String
    public let summary: String
    public let status: String
}

public struct ChatRunSummary: Equatable, Sendable {
    public let stage: ChatExecutionStage
    public let seconds: Int

    public init(stage: ChatExecutionStage, seconds: Int) {
        self.stage = stage
        self.seconds = max(seconds, 0)
    }

    public var isCollapsedByDefault: Bool {
        stage == .completed
    }
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
    public private(set) var steps: [ChatExecutionStep]
    public private(set) var artifacts: [ChatArtifactPresentation]
    public private(set) var serverRunID: String?

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
        self.steps = []
        self.artifacts = []
        self.serverRunID = nil
    }

    public func applying(_ event: ChatStreamEvent, at date: Date = Date()) -> Self {
        var next = self
        if let runID = event.stringValue(for: "run_id"), !runID.isEmpty {
            next.serverRunID = runID
        }

        switch event.type {
        case .turnStart, .thinkingStart, .thinkingDelta, .thinkingEnd:
            next.stage = .analyzing
            next.upsertStep(
                id: "analysis",
                kind: .analysis,
                status: .running,
                title: "分析请求",
                at: date
            )

        case .toolCallStart:
            next.stage = .runningTool
            next.activeToolName = event.stringValue(for: "name")
            next.upsertStep(
                id: event.stringValue(for: "call_id") ?? event.id,
                kind: .tool,
                status: .running,
                title: next.activeToolName ?? "tool",
                at: date
            )

        case .toolCallEnd:
            next.stage = .analyzing
            next.activeToolName = nil
            let toolName = event.stringValue(for: "name") ?? "tool"
            let callID = event.stringValue(for: "call_id") ?? event.id
            let content = event.stringValue(for: "content") ?? ""
            next.upsertStep(
                id: callID,
                kind: .tool,
                status: .completed,
                title: toolName,
                detail: toolName == "delegate_task" ? Self.compactSummary(content) : nil,
                at: date,
                completedAt: date
            )
            if event.stringValue(for: "name") == "delegate_task",
               event.stringValue(for: "status") == "success",
               let content = event.stringValue(for: "content"), !content.isEmpty {
                next.toolResultSummary = Self.compactSummary(content)
                next.upsertArtifact(
                    ChatArtifactPresentation(
                        id: callID,
                        kind: .subagent,
                        title: "delegate_task",
                        summary: Self.compactSummary(content),
                        status: "success"
                    )
                )
            }

        case .toolCallError:
            next.stage = .failed
            next.activeToolName = nil
            next.failureMessage = event.stringValue(for: "message")
                ?? event.stringValue(for: "content")
                ?? "工具执行未完成。"
            next.isResolvingPermission = false
            next.completedAt = date
            next.upsertStep(
                id: event.stringValue(for: "call_id") ?? event.id,
                kind: .tool,
                status: .failed,
                title: event.stringValue(for: "name") ?? "tool",
                detail: next.failureMessage,
                at: date,
                completedAt: date
            )

        case .permissionRequest:
            next.applyPermissionEvent(event, at: date)

        case .agentStart:
            next.stage = .composing
            next.upsertStep(
                id: "response",
                kind: .response,
                status: .running,
                title: "生成答复",
                at: date
            )

        case .tokenDelta:
            next.stage = .composing
            next.upsertStep(
                id: "response",
                kind: .response,
                status: .running,
                title: "生成答复",
                at: date
            )
            if let token = event.stringValue(for: "token") {
                next.partialResponse += token
            }

        case .agentEnd:
            next.stage = .completed
            next.activeToolName = nil
            next.permission = nil
            next.isResolvingPermission = false
            next.completedAt = date
            next.completeStep(id: "analysis", at: date)
            next.completeStep(id: "response", at: date)

        case .agentError:
            next.stage = .failed
            next.activeToolName = nil
            next.permission = nil
            next.failureMessage = event.stringValue(for: "message") ?? "本次对话未能完成。"
            next.isResolvingPermission = false
            next.completedAt = date
            next.failRunningSteps(at: date)

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

    public func cancelling(at date: Date = Date()) -> Self {
        var next = self
        next.stage = .cancelled
        next.activeToolName = nil
        next.permission = nil
        next.isResolvingPermission = false
        next.completedAt = date
        for index in next.steps.indices where next.steps[index].status == .running {
            next.steps[index].status = .cancelled
            next.steps[index].completedAt = date
        }
        return next
    }

    public static func restoring(_ run: ChatRunDTO) -> Self {
        let formatter = ISO8601DateFormatter()
        let startedAt = formatter.date(from: run.startedAt) ?? Date()
        var restored = ChatExecutionPresentation(
            id: run.id,
            stage: restoredStage(status: run.status, steps: run.steps),
            startedAt: startedAt
        )
        restored.serverRunID = run.id
        if !run.completedAt.isEmpty {
            restored.completedAt = formatter.date(from: run.completedAt)
        }
        restored.steps = run.steps.map { step in
            ChatExecutionStep(
                id: "\(step.sequence)-\(step.eventID)",
                kind: restoredStepKind(step.stage),
                status: restoredStepStatus(step.status),
                title: step.summary,
                detail: step.detail.isEmpty ? nil : step.detail,
                startedAt: formatter.date(from: step.startedAt) ?? startedAt,
                completedAt: formatter.date(from: step.completedAt)
            )
        }
        restored.artifacts = run.artifacts.compactMap(restoredArtifact)
        return restored
    }

    private static func restoredStage(
        status: String,
        steps: [ChatRunStepDTO]
    ) -> ChatExecutionStage {
        switch status {
        case "completed": return .completed
        case "failed": return .failed
        case "cancelled": return .cancelled
        default:
            switch steps.last?.stage {
            case "approval": return .awaitingApproval
            case "tool", "subagent": return .runningTool
            case "response": return .composing
            default: return .analyzing
            }
        }
    }

    private static func restoredStepKind(_ stage: String) -> ChatExecutionStepKind {
        switch stage {
        case "tool", "approval", "subagent": return .tool
        case "response": return .response
        case "linked_issue": return .linkedIssue
        default: return .analysis
        }
    }

    private static func restoredStepStatus(_ status: String) -> ChatExecutionStepStatus {
        switch status {
        case "completed": return .completed
        case "failed": return .failed
        case "cancelled": return .cancelled
        case "awaiting_approval", "needs_confirmation": return .awaitingApproval
        default: return .running
        }
    }

    private static func restoredArtifact(
        _ artifact: ChatArtifactDTO
    ) -> ChatArtifactPresentation? {
        let kind: ChatArtifactKind
        switch artifact.kind {
        case "command": kind = .command
        case "task", "linked_issue": kind = .task
        case "validation": kind = .validation
        case "file_change": kind = .fileChange
        case "subagent": kind = .subagent
        default: return nil
        }
        let summary = artifact.summary.values.compactMap { value -> String? in
            guard case .string(let text) = value else { return nil }
            return text
        }.joined(separator: " · ")
        return ChatArtifactPresentation(
            id: artifact.id,
            kind: kind,
            title: artifact.title,
            summary: summary,
            status: artifact.status
        )
    }

    private mutating func applyPermissionEvent(_ event: ChatStreamEvent, at date: Date) {
        let callID = event.stringValue(for: "call_id") ?? event.id
        let toolName = event.stringValue(for: "tool_name") ?? activeToolName ?? "tool"
        switch event.stringValue(for: "status") {
        case "needs_confirmation":
            guard event.stringValue(for: "call_id") != nil,
                  event.stringValue(for: "tool_name") != nil else {
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
            upsertStep(
                id: callID,
                kind: .tool,
                status: .awaitingApproval,
                title: toolName,
                detail: permission?.reason,
                at: date
            )

        case "confirmed", "bypass_enabled":
            stage = .runningTool
            activeToolName = event.stringValue(for: "tool_name") ?? activeToolName
            permission = nil
            isResolvingPermission = false
            upsertStep(
                id: callID,
                kind: .tool,
                status: .running,
                title: toolName,
                at: date
            )

        case "denied", "confirmation_error":
            stage = .failed
            activeToolName = nil
            permission = nil
            failureMessage = event.stringValue(for: "reason") ?? "权限请求未获批准。"
            isResolvingPermission = false
            completedAt = date
            upsertStep(
                id: callID,
                kind: .tool,
                status: .failed,
                title: toolName,
                detail: failureMessage,
                at: date,
                completedAt: date
            )

        default:
            break
        }
    }

    private static func compactSummary(_ content: String) -> String {
        let maximumLength = 420
        guard content.count > maximumLength else { return content }
        return "\(content.prefix(maximumLength))..."
    }

    private mutating func upsertStep(
        id: String,
        kind: ChatExecutionStepKind,
        status: ChatExecutionStepStatus,
        title: String,
        detail: String? = nil,
        at date: Date,
        completedAt: Date? = nil
    ) {
        if let index = steps.firstIndex(where: { $0.id == id }) {
            steps[index].status = status
            steps[index].title = title
            if let detail { steps[index].detail = detail }
            if let completedAt { steps[index].completedAt = completedAt }
            return
        }
        steps.append(
            ChatExecutionStep(
                id: id,
                kind: kind,
                status: status,
                title: title,
                detail: detail,
                startedAt: date,
                completedAt: completedAt
            )
        )
    }

    private mutating func completeStep(id: String, at date: Date) {
        guard let index = steps.firstIndex(where: { $0.id == id }) else { return }
        steps[index].status = .completed
        steps[index].completedAt = date
    }

    private mutating func failRunningSteps(at date: Date) {
        for index in steps.indices where steps[index].status == .running {
            steps[index].status = .failed
            steps[index].completedAt = date
        }
    }

    private mutating func upsertArtifact(_ artifact: ChatArtifactPresentation) {
        if let index = artifacts.firstIndex(where: { $0.id == artifact.id }) {
            artifacts[index] = artifact
        } else {
            artifacts.append(artifact)
        }
    }
}

private extension ChatStreamEvent {
    func stringValue(for key: String) -> String? {
        guard case .string(let value) = data[key] else { return nil }
        return value
    }
}
