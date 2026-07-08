import Foundation

/// A session row shown in the session rail / session menu.
public struct SessionRailItem: Identifiable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let isSelected: Bool

    public init(id: String, title: String, isSelected: Bool) {
        self.id = id
        self.title = title
        self.isSelected = isSelected
    }
}

/// A workspace row shown in the workspace switcher.
public struct WorkspaceSwitcherItem: Identifiable, Equatable, Sendable {
    public let id: String
    public let displayTitle: String
    public let root: String
    public let isSelected: Bool

    public init(id: String, displayTitle: String, root: String, isSelected: Bool) {
        self.id = id
        self.displayTitle = displayTitle
        self.root = root
        self.isSelected = isSelected
    }
}

/// Pure presentation model that derives the workspace switcher and session rail
/// contents from the registry and the live sessions list.
public struct WorkspaceSwitcherPresentation: Equatable {
    public let activeWorkspaceTitle: String
    public let activeSessionTitle: String
    public let hasActiveSession: Bool
    public let workspaces: [WorkspaceSwitcherItem]
    public let recentSessions: [SessionRailItem]

    public init(
        registry: WorkspaceRegistry,
        sessions: [SessionDTO],
        selectedSessionID: String?,
        activeWorkspaceLabel: String?
    ) {
        if let entry = registry.selectedEntry, !entry.name.isEmpty {
            self.activeWorkspaceTitle = entry.name
        } else if let activeWorkspaceLabel, !activeWorkspaceLabel.isEmpty {
            self.activeWorkspaceTitle = activeWorkspaceLabel
        } else {
            self.activeWorkspaceTitle = ""
        }

        let selectedSession = sessions.first { $0.id == selectedSessionID }
        self.hasActiveSession = selectedSession != nil
        self.activeSessionTitle = selectedSession?.title ?? ""

        self.workspaces = registry.entries.map { entry in
            WorkspaceSwitcherItem(
                id: entry.root,
                displayTitle: entry.name.isEmpty ? entry.root : entry.name,
                root: entry.root,
                isSelected: entry.root == registry.selectedRoot
            )
        }

        // Recent sessions: registry order for the selected workspace, resolved
        // against the live sessions list, with the active one flagged.
        let recentIDs = registry.selectedEntry?.recentSessionIDs ?? []
        let sessionByID = Dictionary(sessions.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        self.recentSessions = recentIDs.compactMap { id in
            guard let session = sessionByID[id] else { return nil }
            return SessionRailItem(
                id: session.id,
                title: session.title ?? session.id,
                isSelected: session.id == selectedSessionID
            )
        }
    }
}
