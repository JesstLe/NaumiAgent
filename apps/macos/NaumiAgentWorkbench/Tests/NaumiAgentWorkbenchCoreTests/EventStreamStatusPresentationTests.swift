import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct EventStreamStatusPresentationTests {

    // MARK: - Status text + tone mapping

    @Test func idleMapsToNeutralToneAndIdleText() {
        let p = EventStreamStatusPresentation(
            locale: .zhCN,
            status: .idle,
            reconnectAttempt: 0,
            maxReconnectAttempts: 5,
            lastConnectedAt: nil
        )
        #expect(p.statusText == AppStrings.EventStreamStatus.statusIdle(.zhCN))
        #expect(p.tone == .neutral)
        #expect(p.helpText == nil)
        #expect(p.showsManualReconnect == true)
    }

    @Test func connectedMapsToHealthyToneAndLiveLabel() {
        let p = EventStreamStatusPresentation(
            locale: .enUS,
            status: .connected,
            reconnectAttempt: 0,
            maxReconnectAttempts: 5,
            lastConnectedAt: Date(timeIntervalSince1970: 0)
        )
        #expect(p.shortLabel == AppStrings.EventStreamStatus.liveLabel(.enUS))
        #expect(p.statusText == AppStrings.EventStreamStatus.statusConnected(.enUS))
        #expect(p.tone == .healthy)
        #expect(p.showsManualReconnect == false)
    }

    @Test func reconnectingShowsAttemptFraction() {
        let p = EventStreamStatusPresentation(
            locale: .zhCN,
            status: .reconnecting,
            reconnectAttempt: 3,
            maxReconnectAttempts: 5,
            lastConnectedAt: nil
        )
        #expect(p.statusText == AppStrings.EventStreamStatus.statusReconnecting(.zhCN, attempt: 3, max: 5))
        #expect(p.tone == .warning)
        #expect(p.showsManualReconnect == false)
    }

    @Test func staleShowsReconnectHelpAndAllowsManualReconnect() {
        let p = EventStreamStatusPresentation(
            locale: .enUS,
            status: .stale,
            reconnectAttempt: 5,
            maxReconnectAttempts: 5,
            lastConnectedAt: nil
        )
        #expect(p.tone == .error)
        #expect(p.shortLabel == AppStrings.EventStreamStatus.staleLabel(.enUS))
        #expect(p.helpText == AppStrings.EventStreamStatus.reconnectHelp(.enUS))
        #expect(p.showsManualReconnect == true)
    }

    @Test func stoppedBySessionSwitchIsNeutralAndAllowsReconnect() {
        let p = EventStreamStatusPresentation(
            locale: .zhCN,
            status: .stoppedBySessionSwitch,
            reconnectAttempt: 0,
            maxReconnectAttempts: 5,
            lastConnectedAt: nil
        )
        #expect(p.statusText == AppStrings.EventStreamStatus.statusStoppedBySessionSwitch(.zhCN))
        #expect(p.tone == .neutral)
        #expect(p.showsManualReconnect == true)
    }

    @Test func stoppedByAuthOrProtocolIsErrorAndBlocksReconnect() {
        let p = EventStreamStatusPresentation(
            locale: .enUS,
            status: .stoppedByAuthOrProtocol,
            reconnectAttempt: 0,
            maxReconnectAttempts: 5,
            lastConnectedAt: nil
        )
        #expect(p.tone == .error)
        #expect(p.helpText == AppStrings.EventStreamStatus.stoppedByAuthHelp(.enUS))
        // Auth/protocol stop blocks the reconnect button until the user fixes config.
        #expect(p.showsManualReconnect == false)
    }

    // MARK: - Last-connected relative time

    @Test func lastConnectedTextNilWhenNeverConnected() {
        let p = EventStreamStatusPresentation(
            locale: .enUS,
            status: .idle,
            reconnectAttempt: 0,
            maxReconnectAttempts: 5,
            lastConnectedAt: nil
        )
        #expect(p.lastConnectedText == nil)
    }

    @Test func lastConnectedTextShowsRelativeElapsed() {
        let start = Date(timeIntervalSince1970: 1_000)
        let now = Date(timeIntervalSince1970: 1_045)
        let p = EventStreamStatusPresentation(
            locale: .enUS,
            status: .stale,
            reconnectAttempt: 5,
            maxReconnectAttempts: 5,
            lastConnectedAt: start,
            now: now
        )
        #expect(p.lastConnectedText == "45 s ago")
    }
}

struct EventStreamBackoffTests {

    @Test func firstAttemptUsesBaseDelay() {
        #expect(EventStreamBackoff.delay(forAttempt: 1) == .seconds(1))
    }

    @Test func delayDoublesEachAttempt() {
        // 1, 2, 4, 8, 16, 30, 30, 30 …
        #expect(EventStreamBackoff.delay(forAttempt: 2) == .seconds(2))
        #expect(EventStreamBackoff.delay(forAttempt: 3) == .seconds(4))
        #expect(EventStreamBackoff.delay(forAttempt: 4) == .seconds(8))
        #expect(EventStreamBackoff.delay(forAttempt: 5) == .seconds(16))
    }

    @Test func delayIsCappedAtMaxSeconds() {
        #expect(EventStreamBackoff.delay(forAttempt: 6) == .seconds(30))
        #expect(EventStreamBackoff.delay(forAttempt: 10) == .seconds(30))
        #expect(EventStreamBackoff.delay(forAttempt: 50) == .seconds(30))
    }

    @Test func nonPositiveAttemptFallsBackToBase() {
        #expect(EventStreamBackoff.delay(forAttempt: 0) == .seconds(1))
        #expect(EventStreamBackoff.delay(forAttempt: -3) == .seconds(1))
    }
}
