import Foundation

/// Pure presentation model for a single validation run row in Reviews.
public struct ValidationRunPresentation: Equatable, Sendable, Identifiable {
    public let id: String
    public let taskID: String
    public let actor: String
    public let status: String
    public let exitCode: Int
    public let commandLine: String
    public let cwd: String
    public let completedAt: String
    public let outputSummary: String

    public init(run: ValidationRunDTO) {
        self.id = run.id
        self.taskID = run.taskID
        self.actor = run.actor
        self.status = run.status
        self.exitCode = run.exitCode
        self.commandLine = run.command.joined(separator: " ")
        self.cwd = run.cwd
        self.completedAt = run.completedAt
        self.outputSummary = ValidationRunPresentation.compactOutput(run.output)
    }

    /// Returns a localized, human-readable status label.
    public func statusLabel(locale: AppLocale) -> String {
        switch status.lowercased() {
        case "passed":
            return AppStrings.Reviews.statusPassed(locale)
        case "failed":
            return AppStrings.Reviews.statusFailed(locale)
        default:
            return AppStrings.Reviews.statusUnknown(locale, status: status)
        }
    }

    /// Trims surrounding whitespace and limits the output to a single short summary.
    static func compactOutput(_ output: String, maxLength: Int = 200) -> String {
        let trimmed = output
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
        guard trimmed.count > maxLength else { return trimmed }
        let endIndex = trimmed.index(trimmed.startIndex, offsetBy: maxLength)
        return String(trimmed[..<endIndex]) + "…"
    }
}
