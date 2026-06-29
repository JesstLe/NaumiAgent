import Foundation

/// API command for loading a selected worktree detail.
public struct WorktreeSelectionCommand: Equatable, Sendable {
    public let name: String

    public init?(worktree: WorktreeManagementRow) {
        let trimmedName = worktree.name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedName.isEmpty else {
            return nil
        }

        self.name = trimmedName
    }
}
