import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

@MainActor
struct DaemonProcessControllerTests {

    // MARK: - Launch settings store

    @Test func launchSettingsStoreRoundTripsValues() throws {
        let url = makeTemporaryURL()
        let store = DaemonLaunchSettingsStore(url: url)
        try store.save(DaemonLaunchSettings(executablePath: "/usr/local/bin/naumi-agent", preferredPort: 8770))
        let loaded = store.load()
        #expect(loaded.executablePath == "/usr/local/bin/naumi-agent")
        #expect(loaded.preferredPort == 8770)
    }

    @Test func launchSettingsStoreFallsBackToDefaultOnCorrupt() {
        let url = makeTemporaryURL()
        try? Data("{ not json ".utf8).write(to: url)
        let store = DaemonLaunchSettingsStore(url: url)
        let loaded = store.load()
        #expect(loaded == .default)
    }

    @Test func launchSettingsStoreDefaultsWhenMissing() {
        let store = DaemonLaunchSettingsStore(url: makeTemporaryURL())
        #expect(store.load() == .default)
    }

    // MARK: - Log redaction

    @Test func redactorMasksBearerAndTokenValues() {
        #expect(DaemonLogRedactor.redact("Authorization: Bearer abc123") == "Authorization: ***")
        #expect(DaemonLogRedactor.redact("token=supersecret") == "token=***")
        #expect(DaemonLogRedactor.redact("api_key: sk-live-9999") == "api_key: ***")
        #expect(DaemonLogRedactor.redact("password=hunter2") == "password=***")
    }

    @Test func redactorMasksStandaloneBearerScheme() {
        #expect(DaemonLogRedactor.redact("curl -H Bearer s3cr3t") == "curl -H Bearer ***")
    }

    @Test func redactorPreservesNonSecretLines() {
        let line = "INFO uvicorn started on 127.0.0.1:8765"
        #expect(DaemonLogRedactor.redact(line) == line)
    }

    @Test func logStoreCapsToCapacity() async {
        let store = DaemonLogStore()
        for index in 0..<(DaemonLogStore.capacity + 50) {
            await store.append(text: "line \(index)", source: .stdout)
        }
        let lines = await store.lines()
        #expect(lines.count == DaemonLogStore.capacity)
        // Oldest dropped: first kept line is index 50.
        #expect(lines.first?.text == "line 50")
    }

    @Test func logStoreRedactsOnAppend() async {
        let store = DaemonLogStore()
        await store.append(text: "Authorization: Bearer xyz", source: .stderr)
        let lines = await store.lines()
        #expect(lines.first?.text == "Authorization: ***")
        #expect(lines.first?.source == .stderr)
    }

    // MARK: - Launch configuration

    @Test func launchArgumentsIncludeHostAndPort() {
        let config = DaemonLaunchConfiguration(host: "127.0.0.1")
        #expect(config.launchArguments(forPort: 8765) == ["serve", "--host", "127.0.0.1", "--port", "8765"])
    }

    @Test func endpointURLUsesHostAndPort() {
        let config = DaemonLaunchConfiguration(host: "127.0.0.1")
        #expect(config.endpointURL(forPort: 8771).absoluteString == "http://127.0.0.1:8771/api/v1/")
    }

    // MARK: - Controller happy path

    @Test func startLaunchesOnPreferredPortAndPersistsIt() async throws {
        let appState = AppState()
        let storeURL = makeTemporaryURL()
        let store = DaemonLaunchSettingsStore(url: storeURL)
        // Persist a preferred port that the probe will report as free.
        try store.save(DaemonLaunchSettings(executablePath: nil, preferredPort: 8766))

        let probe = FakePortProbe(availablePorts: [8766, 8767, 8768])
        let spawner = FakeProcessSpawner()
        var readyEndpoint: URL?
        let controller = DaemonProcessController(
            appState: appState,
            configuration: .default,
            launchSettingsStore: store,
            commandResolver: StubCommandResolver(resolved: "/usr/local/bin/naumi-agent"),
            portProbe: probe,
            logStore: DaemonLogStore(),
            spawner: spawner,
            healthChecker: FakeHealthChecker(healthy: true),
            onReady: { endpoint in readyEndpoint = endpoint }
        )

        await controller.start()

        #expect(appState.supervisedDaemonState == .running)
        #expect(spawner.capturedExecutable == "/usr/local/bin/naumi-agent")
        #expect(spawner.capturedArguments == ["serve", "--host", "127.0.0.1", "--port", "8766"])
        let status = try #require(appState.supervisedDaemonStatus)
        #expect(status.port == 8766)
        #expect(status.endpoint.absoluteString == "http://127.0.0.1:8766/api/v1/")
        // Preferred port persisted.
        #expect(store.load().preferredPort == 8766)
        // onReady invoked with the endpoint.
        #expect(readyEndpoint?.absoluteString == "http://127.0.0.1:8766/api/v1/")
    }

    @Test func startFallsBackToNextFreePortWhenPreferredTaken() async {
        let appState = AppState()
        let storeURL = makeTemporaryURL()
        let store = DaemonLaunchSettingsStore(url: storeURL)

        // Preferred 8765 is NOT available; 8766 is.
        let probe = FakePortProbe(availablePorts: [8766])
        let spawner = FakeProcessSpawner()

        let controller = DaemonProcessController(
            appState: appState,
            launchSettingsStore: store,
            commandResolver: StubCommandResolver(resolved: "naumi-agent"),
            portProbe: probe,
            spawner: spawner,
            healthChecker: FakeHealthChecker(healthy: true)
        )

        await controller.start()

        #expect(appState.supervisedDaemonState == .running)
        #expect(appState.supervisedDaemonStatus?.port == 8766)
    }

    @Test func startFailsWhenNoFreePort() async {
        let appState = AppState()
        let controller = DaemonProcessController(
            appState: appState,
            launchSettingsStore: DaemonLaunchSettingsStore(url: makeTemporaryURL()),
            commandResolver: StubCommandResolver(resolved: "naumi-agent"),
            portProbe: FakePortProbe(availablePorts: []),
            spawner: FakeProcessSpawner(),
            healthChecker: FakeHealthChecker(healthy: true)
        )

        await controller.start()

        #expect(appState.supervisedDaemonState == .failed)
        #expect(appState.supervisedDaemonFailureMessage != nil)
        #expect(appState.supervisedDaemonStatus == nil)
    }

    @Test func startFailsWhenHealthCheckTimesOut() async {
        let appState = AppState()
        let spawner = FakeProcessSpawner()
        let controller = DaemonProcessController(
            appState: appState,
            launchSettingsStore: DaemonLaunchSettingsStore(url: makeTemporaryURL()),
            commandResolver: StubCommandResolver(resolved: "naumi-agent"),
            portProbe: FakePortProbe(availablePorts: [8765]),
            spawner: spawner,
            healthChecker: FakeHealthChecker(healthy: false)
        )

        await controller.start()

        #expect(appState.supervisedDaemonState == .failed)
        #expect(appState.supervisedDaemonFailureMessage != nil)
        // Process was terminated on health failure.
        #expect(spawner.terminateFlag.didTerminate)
    }

    @Test func startFailsWhenSpawnThrows() async {
        struct SpawnError: Error {}
        let appState = AppState()
        let controller = DaemonProcessController(
            appState: appState,
            launchSettingsStore: DaemonLaunchSettingsStore(url: makeTemporaryURL()),
            commandResolver: StubCommandResolver(resolved: "naumi-agent"),
            portProbe: FakePortProbe(availablePorts: [8765]),
            spawner: ThrowingProcessSpawner(error: SpawnError()),
            healthChecker: FakeHealthChecker(healthy: true)
        )

        await controller.start()

        #expect(appState.supervisedDaemonState == .failed)
        #expect(appState.supervisedDaemonFailureMessage != nil)
    }

    @Test func stopClearsRunningStateAndTerminates() async throws {
        let appState = AppState()
        let spawner = FakeProcessSpawner()
        let controller = DaemonProcessController(
            appState: appState,
            launchSettingsStore: DaemonLaunchSettingsStore(url: makeTemporaryURL()),
            commandResolver: StubCommandResolver(resolved: "naumi-agent"),
            portProbe: FakePortProbe(availablePorts: [8765]),
            spawner: spawner,
            healthChecker: FakeHealthChecker(healthy: true)
        )

        await controller.start()
        #expect(appState.supervisedDaemonState == .running)

        await controller.stop()
        #expect(appState.supervisedDaemonState == .idle)
        #expect(appState.supervisedDaemonStatus == nil)
        #expect(spawner.terminateFlag.didTerminate)
    }

    @Test func unexpectedExitMarksStale() async throws {
        let appState = AppState()
        let spawner = FakeProcessSpawner()
        let controller = DaemonProcessController(
            appState: appState,
            launchSettingsStore: DaemonLaunchSettingsStore(url: makeTemporaryURL()),
            commandResolver: StubCommandResolver(resolved: "naumi-agent"),
            portProbe: FakePortProbe(availablePorts: [8765]),
            spawner: spawner,
            healthChecker: FakeHealthChecker(healthy: true)
        )

        await controller.start()
        #expect(appState.supervisedDaemonState == .running)

        // Simulate the process exiting on its own.
        spawner.signalExit()
        // Let the detached exit watcher run.
        await waitForState(appState, expected: .exited)

        #expect(appState.supervisedDaemonState == .exited)
        #expect(appState.supervisedDaemonStatus == nil)
        #expect(appState.connectionState == .stale)
    }

    // MARK: - Helpers

    private func makeTemporaryURL() -> URL {
        URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("naumi-launch-\(UUID().uuidString).json")
    }

    private func waitForState(_ appState: AppState, expected: SupervisedDaemonState, timeoutMS: Int = 800) async {
        let deadline = Date().addingTimeInterval(TimeInterval(timeoutMS) / 1000)
        while appState.supervisedDaemonState != expected, Date() < deadline {
            try? await Task.sleep(nanoseconds: 5_000_000)
            await Task.yield()
        }
    }
}

// MARK: - Test doubles

private struct StubCommandResolver: DaemonCommandResolving {
    let resolved: String
    func resolve(executablePath hint: String?) -> String { resolved }
}

private final class FakePortProbe: DaemonPortProbing, @unchecked Sendable {
    var availablePorts: Set<Int>
    init(availablePorts: Set<Int>) { self.availablePorts = availablePorts }
    func isPortAvailable(_ port: Int, host: String) async -> Bool {
        availablePorts.contains(port)
    }
    func findAvailablePort(in range: ClosedRange<Int>, host: String) async -> Int? {
        for port in range where availablePorts.contains(port) {
            return port
        }
        return nil
    }
}

private final class TerminationFlag: @unchecked Sendable {
    var didTerminate = false
}

private final class FakeProcessSpawner: DaemonProcessSpawning, @unchecked Sendable {
    let terminateFlag = TerminationFlag()
    let exitSemaphore = DispatchSemaphore(value: 0)
    var capturedExecutable: String?
    var capturedArguments: [String]?
    var nextPid: Int32 = 4242

    func spawn(executable: String, arguments: [String], environment: [String: String]) throws -> SpawnedDaemonProcess {
        capturedExecutable = executable
        capturedArguments = arguments
        let flag = terminateFlag
        let semaphore = exitSemaphore
        return SpawnedDaemonProcess(
            processIdentifier: nextPid,
            stdout: AsyncStream { $0.finish() },
            stderr: AsyncStream { $0.finish() },
            terminate: { flag.didTerminate = true },
            waitForExit: { semaphore.wait(); return 0 }
        )
    }

    func signalExit() {
        exitSemaphore.signal()
    }
}

private struct ThrowingProcessSpawner: DaemonProcessSpawning {
    let error: Error
    func spawn(executable: String, arguments: [String], environment: [String: String]) throws -> SpawnedDaemonProcess {
        throw error
    }
}

private final class FakeHealthChecker: DaemonHealthChecking, @unchecked Sendable {
    let healthy: Bool
    init(healthy: Bool) { self.healthy = healthy }
    func waitForHealth(endpoint: URL, bearerToken: String?, timeout: TimeInterval) async -> Bool {
        healthy
    }
}
