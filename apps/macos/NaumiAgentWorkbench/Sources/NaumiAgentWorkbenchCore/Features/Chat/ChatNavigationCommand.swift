import Foundation

public enum ChatNavigationCommand: Equatable, Sendable {
    case issue(taskID: String)
    case mission(id: String)
    case run(id: String)
    case review

    @MainActor
    public func apply(to appState: AppState) {
        switch self {
        case .issue(let taskID):
            guard let issue = appState.issues.first(where: { $0.taskID == taskID }) else {
                return
            }
            appState.selectedIssue = issue
            appState.currentRoute = .taskMarket
        case .mission(let id):
            guard let mission = appState.missions.first(where: { $0.id == id }) else {
                return
            }
            appState.selectedMission = mission
            appState.currentRoute = .dashboard
        case .run(let id):
            guard let run = appState.chatRuns.first(where: { $0.id == id }) else {
                return
            }
            appState.selectedChatRun = run
            appState.currentRoute = .chat
        case .review:
            appState.currentRoute = .reviews
        }
    }
}

public struct ChatSourceOpenCommand: Equatable, Sendable {
    public let fileURL: URL

    public init?(source: ChatSourceReferenceDTO, workspaceRoot: String) {
        let root = URL(fileURLWithPath: workspaceRoot, isDirectory: true).standardizedFileURL
        let candidate = root.appendingPathComponent(source.path).standardizedFileURL
        let rootPrefix = root.path.hasSuffix("/") ? root.path : root.path + "/"
        guard candidate.path.hasPrefix(rootPrefix) else { return nil }
        fileURL = candidate
    }
}
