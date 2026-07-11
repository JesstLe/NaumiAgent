import Testing
import Foundation
@testable import NaumiAgentWorkbenchCore

struct AppLocaleTests {

    @Test
    func storedOrDefaultReturnsDefaultWhenNoValueStored() {
        // Clear any previously stored value so the test starts clean.
        let key = "naumi.workbench.locale"
        UserDefaults.standard.removeObject(forKey: key)

        #expect(AppLocale.storedOrDefault() == .zhCN)
    }

    @Test
    func persistAndRestoreRoundTripsChosenLocale() {
        AppLocale.enUS.persist()

        #expect(AppLocale.storedOrDefault() == .enUS)

        // Restore the default to avoid leaking state into other tests.
        AppLocale.zhCN.persist()
        #expect(AppLocale.storedOrDefault() == .zhCN)
    }

    @Test @MainActor
    func appStateLoadsStoredLocaleOnInit() {
        AppLocale.enUS.persist()
        let state = AppState()
        #expect(state.locale == .enUS)
        // Restore default.
        AppLocale.zhCN.persist()
    }
}
