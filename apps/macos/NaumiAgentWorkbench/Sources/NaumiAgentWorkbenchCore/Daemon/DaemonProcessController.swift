import Foundation

/// Supervises a locally-launched NaumiAgent daemon process: resolves the
/// binary, picks a free loopback port, launches with piped output, waits for
/// health, and tears it down on request or unexpected exit.
///
/// The controller never bundles a Python runtime or installs a LaunchAgent; it
/// only manages an existing `naumi-agent` binary the user already has.
@MainActor
public final class DaemonProcessController {
    public static let defaultHealthTimeout: TimeInterval = 10

    public let appState: AppState
    public let configuration: DaemonLaunchConfiguration
    public let launchSettingsStore: DaemonLaunchSettingsStore
    private let commandResolver: DaemonCommandResolving
    private let portProbe: DaemonPortProbing
    private let logStore: DaemonLogStoring
    private let spawner: DaemonProcessSpawning
    private let healthChecker: DaemonHealthChecking
    private let healthTimeout: TimeInterval
    /// Invoked once the supervised daemon passes its health check, with the
    /// resolved endpoint so the app can re-point its connection at it.
    public var onReady: ((URL) async -> Void)?

    private var currentProcess: SpawnedDaemonProcess?
    private var readerTasks: [Task<Void, Never>] = []
    private var exitWatcher: Task<Void, Never>?

    public init(
        appState: AppState,
        configuration: DaemonLaunchConfiguration = .default,
        launchSettingsStore: DaemonLaunchSettingsStore = .default,
        commandResolver: DaemonCommandResolving = DaemonCommandResolver(),
        portProbe: DaemonPortProbing = DaemonPortProbe(),
        logStore: DaemonLogStoring = DaemonLogStore(),
        spawner: DaemonProcessSpawning = FoundationDaemonProcessSpawner(),
        healthChecker: DaemonHealthChecking = HTTPDaemonHealthChecker(),
        healthTimeout: TimeInterval = defaultHealthTimeout,
        onReady: ((URL) async -> Void)? = nil
    ) {
        self.appState = appState
        self.configuration = configuration
        self.launchSettingsStore = launchSettingsStore
        self.commandResolver = commandResolver
        self.portProbe = portProbe
        self.logStore = logStore
        self.spawner = spawner
        self.healthChecker = healthChecker
        self.healthTimeout = healthTimeout
        self.onReady = onReady
    }

    /// Convenience accessor for the most recent redacted log lines.
    public func currentLogLines() async -> [DaemonLogLine] {
        await logStore.lines()
    }

    /// Clears the captured log buffer.
    public func clearLog() async {
        await logStore.clear()
    }

    /// Resolves, launches, and waits for health on a supervised daemon.
    /// On success, sets the app state to `.running` and calls `onReady`.
    public func start() async {
        guard appState.supervisedDaemonState != .starting,
              appState.supervisedDaemonState != .running else {
            return
        }

        appState.supervisedDaemonState = .starting
        appState.supervisedDaemonFailureMessage = nil

        let hint = launchSettingsStore.load().executablePath ?? configuration.executablePath
        let resolvedExecutable = commandResolver.resolve(executablePath: hint)

        guard let port = await selectPort() else {
            appState.supervisedDaemonState = .failed
            appState.supervisedDaemonFailureMessage = Self.noFreePortMessage(
                range: configuration.portRange,
                locale: appState.locale
            )
            return
        }

        let arguments = configuration.launchArguments(forPort: port)
        let environment = ProcessInfo.processInfo.environment

        let process: SpawnedDaemonProcess
        do {
            process = try spawner.spawn(
                executable: resolvedExecutable,
                arguments: arguments,
                environment: environment
            )
        } catch {
            appState.supervisedDaemonState = .failed
            appState.supervisedDaemonFailureMessage = error.localizedDescription
            return
        }

        currentProcess = process
        startPipeReaders(process: process)
        startExitWatcher(process: process)

        let endpoint = configuration.endpointURL(forPort: port)

        // The supervised daemon inherits the caller's environment; bearer-token
        // auth is configured separately (M19) and not enforced at launch time.
        let healthy = await healthChecker.waitForHealth(
            endpoint: endpoint,
            bearerToken: nil,
            timeout: healthTimeout
        )

        guard healthy else {
            await terminateCurrentProcess()
            appState.supervisedDaemonState = .failed
            appState.supervisedDaemonFailureMessage = Self.healthTimeoutMessage(locale: appState.locale)
            return
        }

        let status = SupervisedDaemonStatus(port: port, pid: process.processIdentifier, endpoint: endpoint)
        appState.supervisedDaemonStatus = status
        appState.supervisedDaemonState = .running
        appState.supervisedDaemonFailureMessage = nil

        // Persist the chosen port so the next launch prefers it.
        let prior = launchSettingsStore.load()
        try? launchSettingsStore.save(
            DaemonLaunchSettings(
                executablePath: prior.executablePath ?? hint,
                preferredPort: port
            )
        )

        if let onReady {
            await onReady(endpoint)
        }
    }

    /// Gracefully stops the supervised daemon, clearing pid/endpoint state.
    public func stop() async {
        guard appState.supervisedDaemonState == .running
                || appState.supervisedDaemonState == .failed
                || appState.supervisedDaemonState == .exited else {
            appState.supervisedDaemonState = .idle
            return
        }

        appState.supervisedDaemonState = .stopping
        await terminateCurrentProcess()
        appState.supervisedDaemonStatus = nil
        appState.supervisedDaemonState = .idle
    }

    // MARK: - Internals

    /// Picks the preferred port when free, otherwise scans the configured range.
    private func selectPort() async -> Int? {
        if let preferred = effectivePreferredPort(),
           configuration.portRange.contains(preferred),
           await portProbe.isPortAvailable(preferred, host: configuration.host) {
            return preferred
        }
        return await portProbe.findAvailablePort(in: configuration.portRange, host: configuration.host)
    }

    private func effectivePreferredPort() -> Int? {
        launchSettingsStore.load().preferredPort ?? configuration.preferredPort
    }

    private func startPipeReaders(process: SpawnedDaemonProcess) {
        let store = logStore
        readerTasks.append(
            Task.detached(priority: .utility) { [store] in
                for await data in process.stdout {
                    if let text = String(data: data, encoding: .utf8) {
                        await store.append(text: text, source: .stdout)
                    }
                }
            }
        )
        readerTasks.append(
            Task.detached(priority: .utility) { [store] in
                for await data in process.stderr {
                    if let text = String(data: data, encoding: .utf8) {
                        await store.append(text: text, source: .stderr)
                    }
                }
            }
        )
    }

    private func startExitWatcher(process: SpawnedDaemonProcess) {
        exitWatcher?.cancel()
        exitWatcher = Task.detached(priority: .utility) { [weak self] in
            _ = process.waitForExit()
            guard !Task.isCancelled else { return }
            await MainActor.run {
                self?.handleUnexpectedExit()
            }
        }
    }

    private func handleUnexpectedExit() {
        // Only react when we believed the daemon was up.
        guard appState.supervisedDaemonState == .running else { return }
        appState.supervisedDaemonStatus = nil
        appState.supervisedDaemonState = .exited
        appState.connectionState = .stale
        currentProcess = nil
    }

    private func terminateCurrentProcess() async {
        exitWatcher?.cancel()
        exitWatcher = nil
        for task in readerTasks { task.cancel() }
        readerTasks.removeAll()
        currentProcess?.terminate()
        currentProcess = nil
        // Give the OS a beat to reclaim the port and flush final log output.
        try? await Task.sleep(nanoseconds: 150_000_000)
        // Drain any final buffered output so the log view is complete.
        for _ in 0..<2 {
            _ = await logStore.lines()
        }
    }

    // MARK: - Localized messages

    static func noFreePortMessage(range: ClosedRange<Int>, locale: AppLocale) -> String {
        locale == .zhCN
            ? "在 \(range.lowerBound)-\(range.upperBound) 范围内未找到可用端口。"
            : "No free port found in \(range.lowerBound)-\(range.upperBound)."
    }

    static func healthTimeoutMessage(locale: AppLocale) -> String {
        locale == .zhCN
            ? "本地服务启动后未在规定时间内通过健康检查。"
            : "The local daemon did not pass its health check in time."
    }
}
