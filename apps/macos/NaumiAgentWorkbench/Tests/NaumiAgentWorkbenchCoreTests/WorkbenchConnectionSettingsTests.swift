import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct WorkbenchConnectionSettingsTests {

    @Test func defaultSettingsPointAtLocalhostDaemon() {
        let settings = WorkbenchConnectionSettings.default
        #expect(settings.baseURLString == "http://127.0.0.1:8765/api/v1/")
        #expect(settings.bearerToken == nil)
        #expect(settings.baseURL?.absoluteString == "http://127.0.0.1:8765/api/v1/")
    }

    @Test func baseURLAppendsTrailingSlash() {
        let settings = WorkbenchConnectionSettings(baseURLString: "http://127.0.0.1:9000/api/v1")
        #expect(settings.baseURL?.absoluteString == "http://127.0.0.1:9000/api/v1/")
    }

    @Test func baseURLRejectsInvalidString() {
        let settings = WorkbenchConnectionSettings(baseURLString: "")
        #expect(settings.baseURL == nil)
    }

    @Test func storeLoadsDefaultWhenFileMissing() throws {
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("connection-\(UUID().uuidString).json")
        let store = WorkbenchConnectionSettingsStore(url: url)

        let loaded = store.load()
        #expect(loaded == .default)
    }

    @Test func storeRoundTripsSavedSettings() throws {
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("connection-\(UUID().uuidString).json")
        let store = WorkbenchConnectionSettingsStore(url: url)

        let custom = WorkbenchConnectionSettings(
            baseURLString: "http://127.0.0.1:9000/api/v1/",
            bearerToken: "secret-token"
        )
        try store.save(custom)

        let reloaded = WorkbenchConnectionSettingsStore(url: url).load()
        #expect(reloaded == custom)
        #expect(reloaded.baseURLString == "http://127.0.0.1:9000/api/v1/")
        #expect(reloaded.bearerToken == "secret-token")
    }

    @Test func storeFallsBackToDefaultOnCorruptFile() throws {
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("connection-\(UUID().uuidString).json")
        try "{ not valid json".data(using: .utf8)!.write(to: url)

        let store = WorkbenchConnectionSettingsStore(url: url)
        #expect(store.load() == .default)
    }

    @Test func storeStripsEmptyBearerTokenToNil() throws {
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("connection-\(UUID().uuidString).json")
        let store = WorkbenchConnectionSettingsStore(url: url)

        try store.save(WorkbenchConnectionSettings(
            baseURLString: "http://127.0.0.1:8765/api/v1/",
            bearerToken: "   "
        ))

        let reloaded = WorkbenchConnectionSettingsStore(url: url).load()
        #expect(reloaded.bearerToken == nil)
    }

    @Test func settingsWithoutExplicitBaseURLIsLocalhostOnly() {
        // Local-first boundary: the default must never expose a non-loopback address.
        let settings = WorkbenchConnectionSettings.default
        let host = settings.baseURL?.host
        #expect(host == "127.0.0.1" || host == "localhost")
    }
}
