import Foundation

/// API command for loading a selected Dashboard worktree detail.
public struct DashboardWorktreeSelectionCommand: Equatable, Sendable {
    public let name: String

    public init?(node: DashboardCanvasNode) {
        guard node.kind == .worktrees else {
            return nil
        }

        let trimmedName = node.title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedName.isEmpty else {
            return nil
        }

        self.name = trimmedName
    }
}
