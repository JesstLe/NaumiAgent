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

        appState.sessions = [previewSession(from: snapshot, locale: locale)]
        appState.missions = snapshot.missions
        appState.agentProfiles = snapshot.agentProfiles
        appState.validationRuns = []
        appState.contextSnapshots = []
        appState.approvals = []
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
}
