import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct WorktreeLocalActionExecutorTests {

    @Test func revealInFinderValidatesDirectoryAndCallsLauncher() throws {
        let directory = try temporaryDirectory()
        let launcher = RecordingWorktreeLocalActionLauncher()
        let executor = WorktreeLocalActionExecutor(launcher: launcher)

        try executor.perform(.revealInFinder, path: directory.path)

        #expect(launcher.events == [.revealInFinder(directory.standardizedFileURL)])
    }

    @Test func openTerminalValidatesDirectoryAndCallsLauncher() throws {
        let directory = try temporaryDirectory()
        let launcher = RecordingWorktreeLocalActionLauncher()
        let executor = WorktreeLocalActionExecutor(launcher: launcher)

        try executor.perform(.openTerminal, path: directory.path)

        #expect(launcher.events == [.openTerminal(directory.standardizedFileURL)])
    }

    @Test func missingWorktreePathReturnsLocalizedMessage() throws {
        let missingPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("naumi-missing-\(UUID().uuidString)")
            .path
        let executor = WorktreeLocalActionExecutor(launcher: RecordingWorktreeLocalActionLauncher())

        #expect(throws: WorktreeLocalActionError.self) {
            try executor.perform(.revealInFinder, path: missingPath)
        }

        do {
            try executor.perform(.revealInFinder, path: missingPath)
        } catch let error as WorktreeLocalActionError {
            #expect(error.localizedMessage(locale: .zhCN) == "工作区路径不存在：\(missingPath)")
            #expect(error.localizedMessage(locale: .enUS) == "Worktree path does not exist: \(missingPath)")
        }
    }

    @Test func emptyPathReturnsLocalizedMessage() throws {
        let executor = WorktreeLocalActionExecutor(launcher: RecordingWorktreeLocalActionLauncher())

        do {
            try executor.perform(.openTerminal, path: "  ")
        } catch let error as WorktreeLocalActionError {
            #expect(error.localizedMessage(locale: .zhCN) == "工作区路径为空")
            #expect(error.localizedMessage(locale: .enUS) == "Worktree path is empty")
        }
    }

    @Test func filePathReturnsNotDirectoryMessage() throws {
        let fileURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("naumi-worktree-action-file-\(UUID().uuidString)")
        try Data("not a directory".utf8).write(to: fileURL)
        let executor = WorktreeLocalActionExecutor(launcher: RecordingWorktreeLocalActionLauncher())

        do {
            try executor.perform(.revealInFinder, path: fileURL.path)
        } catch let error as WorktreeLocalActionError {
            #expect(error.localizedMessage(locale: .zhCN) == "工作区路径不是目录：\(fileURL.path)")
            #expect(error.localizedMessage(locale: .enUS) == "Worktree path is not a directory: \(fileURL.path)")
        }
    }

    @Test func unreadableWorktreePathReturnsPermissionDeniedMessage() throws {
        // A directory that exists but cannot be read must surface a distinct
        // permission-denied state so the user knows to fix permissions, not
        // re-create the worktree. (No-op when running as root, which bypasses
        // Unix permission bits.)
        guard getuid() != 0 else { return }
        let directory = try temporaryDirectory()
        defer {
            // Restore permissions so the temp dir can be cleaned up.
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: directory.path
            )
        }
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o000],
            ofItemAtPath: directory.path
        )

        let executor = WorktreeLocalActionExecutor(launcher: RecordingWorktreeLocalActionLauncher())

        do {
            try executor.perform(.revealInFinder, path: directory.path)
            Issue.record("expected permissionDenied error")
        } catch let error as WorktreeLocalActionError {
            #expect(error.localizedMessage(locale: .zhCN) == "没有权限访问工作区：\(directory.path)")
            #expect(error.localizedMessage(locale: .enUS) == "Permission denied for worktree: \(directory.path)")
        }
    }

    private func temporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("naumi-worktree-action-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.standardizedFileURL
    }
}

private final class RecordingWorktreeLocalActionLauncher: WorktreeLocalActionLaunching {
    enum Event: Equatable {
        case revealInFinder(URL)
        case openTerminal(URL)
    }

    private(set) var events: [Event] = []

    func revealInFinder(url: URL) throws {
        events.append(.revealInFinder(url))
    }

    func openTerminal(url: URL) throws {
        events.append(.openTerminal(url))
    }
}
