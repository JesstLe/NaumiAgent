import Foundation

/// Demo-mode helpers for loading static fixtures without talking to a daemon.
public enum WorkbenchPreviewLoader {
    private static let fixtureNames: [AppLocale: String] = [
        .zhCN: "workbench_snapshot_zh.json",
        .enUS: "workbench_snapshot_en.json",
    ]

    public enum Error: Swift.Error {
        case malformedLocaleArgument
        case fixtureNotFound(String)
        case cannotLoadFixture(String, underlying: Swift.Error)
    }

    public enum PreviewMode: Equatable, Sendable {
        case disabled
        case enabled(AppLocale)
        case malformed
    }

    /// Returns true if preview flag exists, even when locale token is not recognized.
    public static func requestedMode(from arguments: [String]) -> PreviewMode {
        guard let flagIndex = arguments.firstIndex(of: "--preview-fixture") else {
            return .disabled
        }

        guard arguments.indices.contains(flagIndex + 1) else {
            return .malformed
        }

        guard let locale = locale(for: arguments[flagIndex + 1]) else {
            return .malformed
        }

        return .enabled(locale)
    }

    public static func locale(for argument: String) -> AppLocale? {
        switch argument.lowercased() {
        case "zh", "zh-cn", "zh_cn":
            return .zhCN
        case "en", "en-us", "en_us":
            return .enUS
        default:
            return nil
        }
    }

    public static func requestedRoute(from arguments: [String]) -> AppRoute? {
        guard let flagIndex = arguments.firstIndex(of: "--preview-route"),
              arguments.indices.contains(flagIndex + 1) else {
            return nil
        }

        return route(for: arguments[flagIndex + 1])
    }

    public static func route(for argument: String) -> AppRoute? {
        switch argument.lowercased() {
        case "dashboard", "overview":
            return .dashboard
        case "taskmarket", "task-market", "market":
            return .taskMarket
        case "worktrees", "worktree", "workspaces":
            return .worktrees
        case "reviews", "review":
            return .reviews
        case "timeline", "events":
            return .timeline
        case "settings", "setting":
            return .settings
        default:
            return nil
        }
    }

    @MainActor
    public static func applyPreviewState(
        locale: AppLocale,
        to appState: AppState,
        fixtureDirectory: URL? = nil
    ) throws {
        let fixtureURL = try resolveFixtureURL(
            for: locale,
            fixtureDirectory: fixtureDirectory
        )
        let data = try Data(contentsOf: fixtureURL)
        let snapshot = try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)

        appState.locale = locale
        appState.connectionState = .connected
        appState.lastError = nil
        appState.snapshot = snapshot
        appState.selectedSessionID = snapshot.sessionID
        appState.selectedWorkspace = nil
        appState.currentRoute = .dashboard
        appState.isPreviewFixture = true

        appState.sessions = [previewSession(from: snapshot, locale: locale)]
        appState.missions = snapshot.missions
        appState.agentProfiles = snapshot.agentProfiles
        appState.validationRuns = previewValidationRuns(from: snapshot, locale: locale)
        appState.contextSnapshots = previewContextSnapshots(from: snapshot, locale: locale)
        appState.approvals = previewApprovals(from: snapshot, locale: locale)
        appState.timelineEvents = snapshot.events
        appState.issues = snapshot.issues
        appState.failures = snapshot.failures
        appState.leases = snapshot.leases

        appState.daemonStatus = DaemonStatusDTO(
            status: "running",
            version: "preview",
            pid: 4242,
            host: "127.0.0.1",
            port: 8765,
            startedAt: ISO8601DateFormatter().string(from: .init()),
            workspaceCount: 1
        )
        appState.capabilities = CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: [AppLocale.zhCN.rawValue, AppLocale.enUS.rawValue],
            protocolVersion: 1
        )
    }

    private static func resolveFixtureURL(
        for locale: AppLocale,
        fixtureDirectory: URL?
    ) throws -> URL {
        guard let fixtureFile = fixtureNames[locale] else {
            throw Error.malformedLocaleArgument
        }

        let searchRoots = orderedSearchRoots(fixtureDirectory: fixtureDirectory)
        for root in searchRoots {
            let candidate = root.appendingPathComponent(fixtureFile)
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
        }

        throw Error.fixtureNotFound(fixtureFile)
    }

    private static func orderedSearchRoots(fixtureDirectory: URL?) -> [URL] {
        var roots: [URL] = []
        if let explicit = fixtureDirectory {
            return [explicit]
        }
        if let resource = Bundle.main.resourceURL {
            roots.append(resource.appendingPathComponent("Fixtures"))
        }
        if let executable = Bundle.main.executableURL {
            roots.append(executable.deletingLastPathComponent())
        }

        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        roots.append(cwd.appendingPathComponent("Fixtures"))
        roots.append(cwd.deletingLastPathComponent().appendingPathComponent("Fixtures"))

        var unique: [URL] = []
        for root in roots {
            if !unique.contains(where: { $0.path == root.path }) {
                unique.append(root)
            }
        }
        return unique
    }

    private static func previewSession(
        from snapshot: WorkbenchSnapshotDTO,
        locale: AppLocale
    ) -> SessionDTO {
        let title = snapshot.missions.first?.title
            ?? (locale == .zhCN ? "未命名会话" : "Unnamed Session")

        return SessionDTO(
            id: snapshot.sessionID,
            title: title,
            model: "preview",
            createdAt: "",
            updatedAt: "",
            messageCount: snapshot.events.count,
            totalTokens: 0,
            totalCostUSD: 0.0,
            status: "active"
        )
    }

    private static func previewValidationRuns(
        from snapshot: WorkbenchSnapshotDTO,
        locale: AppLocale
    ) -> [ValidationRunDTO] {
        let firstTaskID = snapshot.tasks.first?.id ?? "task-preview"
        let secondTaskID = snapshot.tasks.dropFirst().first?.id ?? firstTaskID
        let commandOne = locale == .zhCN
            ? ["ruff", "check", "src/"]
            : ["ruff", "check", "src/"]
        let commandTwo = locale == .zhCN
            ? ["pytest", "tests/unit/test_workbench_market.py", "-q"]
            : ["pytest", "tests/unit/test_workbench_market.py", "-q"]

        return [
            ValidationRunDTO(
                id: "preview-validation-1",
                sessionID: snapshot.sessionID,
                taskID: firstTaskID,
                actor: "Backend-Agent",
                command: commandOne,
                cwd: "/Users/lv/Workspace/NaumiAgent",
                status: "passed",
                exitCode: 0,
                output: locale == .zhCN ? "静态检查通过" : "Static checks passed",
                startedAt: "2026-06-27T09:28:00",
                completedAt: "2026-06-27T09:28:18"
            ),
            ValidationRunDTO(
                id: "preview-validation-2",
                sessionID: snapshot.sessionID,
                taskID: secondTaskID,
                actor: "Test-Agent",
                command: commandTwo,
                cwd: "/Users/lv/Workspace/NaumiAgent",
                status: "passed",
                exitCode: 0,
                output: locale == .zhCN ? "12 passed" : "12 passed",
                startedAt: "2026-06-27T09:29:00",
                completedAt: "2026-06-27T09:29:31"
            )
        ]
    }

    private static func previewContextSnapshots(
        from snapshot: WorkbenchSnapshotDTO,
        locale: AppLocale
    ) -> [ContextSnapshotDTO] {
        let firstTaskID = snapshot.tasks.first?.id ?? "task-preview"
        let secondTaskID = snapshot.tasks.dropFirst().first?.id ?? firstTaskID
        return [
            ContextSnapshotDTO(
                id: "preview-context-1",
                sessionID: snapshot.sessionID,
                agentID: "Backend-Agent",
                taskID: firstTaskID,
                health: "good",
                reasons: locale == .zhCN
                    ? ["引用文件已同步", "租约上下文完整"]
                    : ["Referenced files are current", "Lease context is complete"],
                createdAt: "2026-06-27T09:35:00"
            ),
            ContextSnapshotDTO(
                id: "preview-context-2",
                sessionID: snapshot.sessionID,
                agentID: "Reviewer-Agent",
                taskID: secondTaskID,
                health: "stale",
                reasons: locale == .zhCN
                    ? ["分支已更新", "需要刷新差异上下文"]
                    : ["Branch was updated", "Diff context needs refresh"],
                createdAt: "2026-06-27T09:18:00"
            ),
            ContextSnapshotDTO(
                id: "preview-context-3",
                sessionID: snapshot.sessionID,
                agentID: "Planner-Agent",
                taskID: firstTaskID,
                health: "conflicted",
                reasons: locale == .zhCN
                    ? ["目标锁策略存在冲突"]
                    : ["Intent lock policy conflict"],
                createdAt: "2026-06-27T09:09:00"
            )
        ]
    }

    private static func previewApprovals(
        from snapshot: WorkbenchSnapshotDTO,
        locale: AppLocale
    ) -> [ApprovalDTO] {
        let missionID = snapshot.missions.first?.id ?? "mission-preview"
        let firstTaskID = snapshot.tasks.first?.id ?? "task-preview"
        let secondTaskID = snapshot.tasks.dropFirst().first?.id ?? firstTaskID

        return [
            ApprovalDTO(
                id: "preview-approval-1",
                sessionID: snapshot.sessionID,
                missionID: missionID,
                taskID: firstTaskID,
                state: "waiting",
                title: locale == .zhCN ? "任务市场租约策略" : "Task Market Lease Policy",
                detail: locale == .zhCN
                    ? "高风险并发路径需要人工确认。"
                    : "High-risk concurrency path requires human confirmation.",
                requester: "Backend-Agent",
                reviewer: "Human",
                decisionNote: "",
                createdAt: "2026-06-27T09:28:00",
                updatedAt: "2026-06-27T09:36:00"
            ),
            ApprovalDTO(
                id: "preview-approval-2",
                sessionID: snapshot.sessionID,
                missionID: missionID,
                taskID: secondTaskID,
                state: "waiting",
                title: locale == .zhCN ? "验证失败卡片" : "Validation Failure Cards",
                detail: locale == .zhCN
                    ? "需要确认失败卡片的文案和重试动作。"
                    : "Confirm failure-card copy and retry action.",
                requester: "Test-Agent",
                reviewer: "Human",
                decisionNote: "",
                createdAt: "2026-06-27T09:21:00",
                updatedAt: "2026-06-27T09:31:00"
            )
        ]
    }
}
