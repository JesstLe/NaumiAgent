import AppKit
import Foundation

public enum WorktreeLocalAction: Equatable, Sendable {
    case revealInFinder
    case openTerminal
}

public enum WorktreeLocalActionError: Error, Equatable, Sendable {
    case emptyPath
    case pathDoesNotExist(String)
    case pathIsNotDirectory(String)
    case permissionDenied(String)
    case launchFailed(String)

    public func localizedMessage(locale: AppLocale) -> String {
        switch self {
        case .emptyPath:
            return locale == .zhCN ? "工作区路径为空" : "Worktree path is empty"
        case .pathDoesNotExist(let path):
            return locale == .zhCN ? "工作区路径不存在：\(path)" : "Worktree path does not exist: \(path)"
        case .pathIsNotDirectory(let path):
            return locale == .zhCN ? "工作区路径不是目录：\(path)" : "Worktree path is not a directory: \(path)"
        case .permissionDenied(let path):
            return locale == .zhCN
                ? "没有权限访问工作区：\(path)"
                : "Permission denied for worktree: \(path)"
        case .launchFailed(let reason):
            return locale == .zhCN ? "无法打开工作区：\(reason)" : "Unable to open worktree: \(reason)"
        }
    }
}

public protocol WorktreeLocalActionLaunching: AnyObject {
    func revealInFinder(url: URL) throws
    func openTerminal(url: URL) throws
}

public final class WorktreeLocalActionExecutor {
    private let launcher: any WorktreeLocalActionLaunching
    private let fileManager: FileManager

    public init(
        launcher: any WorktreeLocalActionLaunching = MacWorktreeLocalActionLauncher(),
        fileManager: FileManager = .default
    ) {
        self.launcher = launcher
        self.fileManager = fileManager
    }

    public func perform(_ action: WorktreeLocalAction, path: String) throws {
        let url = try validatedDirectoryURL(path: path)
        switch action {
        case .revealInFinder:
            try launcher.revealInFinder(url: url)
        case .openTerminal:
            try launcher.openTerminal(url: url)
        }
    }

    private func validatedDirectoryURL(path: String) throws -> URL {
        let trimmedPath = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedPath.isEmpty else {
            throw WorktreeLocalActionError.emptyPath
        }

        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: trimmedPath, isDirectory: &isDirectory) else {
            throw WorktreeLocalActionError.pathDoesNotExist(trimmedPath)
        }
        guard isDirectory.boolValue else {
            throw WorktreeLocalActionError.pathIsNotDirectory(trimmedPath)
        }
        // A path that exists but cannot be read is a distinct, actionable state:
        // the user needs to fix permissions, not re-create the worktree.
        guard fileManager.isReadableFile(atPath: trimmedPath) else {
            throw WorktreeLocalActionError.permissionDenied(trimmedPath)
        }
        return URL(fileURLWithPath: trimmedPath).standardizedFileURL
    }
}

public final class MacWorktreeLocalActionLauncher: WorktreeLocalActionLaunching {
    public init() {}

    public func revealInFinder(url: URL) throws {
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    public func openTerminal(url: URL) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = ["-a", "Terminal", url.path]

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            throw WorktreeLocalActionError.launchFailed(error.localizedDescription)
        }

        guard process.terminationStatus == 0 else {
            throw WorktreeLocalActionError.launchFailed("open exited with status \(process.terminationStatus)")
        }
    }
}
