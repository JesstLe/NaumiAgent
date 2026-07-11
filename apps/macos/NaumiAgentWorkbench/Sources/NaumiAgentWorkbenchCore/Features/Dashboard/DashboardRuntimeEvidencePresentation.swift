import Foundation

/// Runtime evidence shown by the dashboard. Preview data must never be mixed
/// into this model: every value comes from a daemon record or an explicit empty state.
public struct DashboardRuntimeEvidencePresentation: Equatable, Sendable {
    public let latestValidationRun: ValidationRunDTO?
    public let latestContextSnapshot: ContextSnapshotDTO?

    public init(
        validationRuns: [ValidationRunDTO],
        contextSnapshots: [ContextSnapshotDTO]
    ) {
        latestValidationRun = validationRuns.max {
            Self.validationTimestamp($0) < Self.validationTimestamp($1)
        }
        latestContextSnapshot = contextSnapshots.max { $0.createdAt < $1.createdAt }
    }

    public func validationLines(locale: AppLocale) -> [String] {
        guard let latestValidationRun else {
            return locale == .zhCN ? ["暂无验证记录"] : ["No validation runs recorded"]
        }

        let command = latestValidationRun.command.joined(separator: " ")
        if locale == .zhCN {
            return [
                "最近运行：\(latestValidationRun.id)",
                "结果：\(latestValidationRun.status)",
                command.isEmpty ? "命令：未记录" : "命令：\(command)",
            ]
        }
        return [
            "Latest run: \(latestValidationRun.id)",
            "Result: \(latestValidationRun.status)",
            command.isEmpty ? "Command: not recorded" : "Command: \(command)",
        ]
    }

    public func contextLines(locale: AppLocale) -> [String] {
        guard let latestContextSnapshot else {
            return locale == .zhCN ? ["暂无上下文健康记录"] : ["No context health records"]
        }

        let prefix = locale == .zhCN
        var lines = [
            prefix ? "整体：\(latestContextSnapshot.health)" : "Overall: \(latestContextSnapshot.health)",
            prefix
                ? "更新：\(Self.displayTimestamp(latestContextSnapshot.createdAt))"
                : "Updated: \(Self.displayTimestamp(latestContextSnapshot.createdAt))",
        ]
        if let reason = latestContextSnapshot.reasons.first(where: {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }) {
            lines.append(reason)
        }
        return lines
    }

    private static func validationTimestamp(_ run: ValidationRunDTO) -> String {
        run.completedAt.isEmpty ? run.startedAt : run.completedAt
    }

    private static func displayTimestamp(_ timestamp: String) -> String {
        let withoutFraction = timestamp.split(separator: ".", maxSplits: 1).first.map(String.init) ?? timestamp
        return withoutFraction.replacingOccurrences(of: "T", with: " ")
    }
}
