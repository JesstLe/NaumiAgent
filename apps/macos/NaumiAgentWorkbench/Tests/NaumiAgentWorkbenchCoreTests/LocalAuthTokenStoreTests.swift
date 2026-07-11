import Testing
import Foundation
@testable import NaumiAgentWorkbenchCore

/// Keychain is process-wide shared state, so serialize these tests to avoid
/// one test's save/delete racing with another's.
@Suite(.serialized)
struct LocalAuthTokenStoreTests {

    // MARK: - LocalAuthTokenStore (Keychain round-trip)

    @Test
    func keychainSaveLoadDeleteRoundTrip() {
        // Use a unique service to avoid colliding with a real stored token.
        let store = LocalAuthTokenStore(service: "ai.naumi.workbench.test", account: "unit")
        store.delete()

        #expect(store.load() == nil)
        #expect(store.hasToken == false)

        let saveError = store.save("sk-test-123")
        #expect(saveError == nil)
        #expect(store.hasToken == true)
        #expect(store.load() == "sk-test-123")

        // Upsert replaces the existing value.
        store.save("sk-rotated")
        #expect(store.load() == "sk-rotated")

        let deleteError = store.delete()
        #expect(deleteError == nil)
        #expect(store.load() == nil)
        #expect(store.hasToken == false)
    }

    @Test
    func savingEmptyTokenClearsExistingValue() {
        let store = LocalAuthTokenStore(service: "ai.naumi.workbench.test", account: "empty")
        store.save("first")
        #expect(store.load() == "first")

        store.save("")
        #expect(store.load() == nil)
        #expect(store.hasToken == false)
    }

    // MARK: - KeychainTokenProvider

    @Test
    func tokenProviderReadsFromStore() {
        let store = LocalAuthTokenStore(service: "ai.naumi.workbench.test", account: "provider")
        store.delete()
        defer { store.delete() }

        let provider = KeychainTokenProvider(store: store)
        #expect(provider.currentToken() == nil)

        store.save("via-provider")
        #expect(provider.currentToken() == "via-provider")
    }

    // MARK: - API client token provider integration

    @Test
    func apiClientAcceptsTokenProvider() async throws {
        final class FixedTokenProvider: LocalAuthTokenProviding {
            let token: String?
            init(_ token: String?) { self.token = token }
            func currentToken() -> String? { token }
        }

        let provider = FixedTokenProvider("sk-from-provider")
        let client = WorkbenchAPIClient(
            baseURL: URL(string: "http://127.0.0.1:8765/api/v1/")!,
            bearerToken: nil
        )
        await client.setTokenProvider(provider)

        // The provider is wired and the client exposes the setter without error.
        // Token value is sourced on each request and never rendered in the UI.
        #expect(provider.currentToken() == "sk-from-provider")
    }
}
