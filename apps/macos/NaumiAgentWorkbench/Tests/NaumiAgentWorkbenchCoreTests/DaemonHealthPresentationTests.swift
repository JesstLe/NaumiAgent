import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct DaemonHealthPresentationTests {

    // MARK: - Status text

    @Test func statusTextMatchesStateDisplayNameZhCN() {
        for state in AppState.ConnectionState.allCases {
            let presentation = DaemonHealthPresentation(
                locale: .zhCN,
                connectionState: state,
                lastHealthCheckAt: nil,
                connectionLog: []
            )
            #expect(presentation.statusText == state.displayName(locale: .zhCN))
        }
    }

    @Test func statusTextMatchesStateDisplayNameEnUS() {
        for state in AppState.ConnectionState.allCases {
            let presentation = DaemonHealthPresentation(
                locale: .enUS,
                connectionState: state,
                lastHealthCheckAt: nil,
                connectionLog: []
            )
            #expect(presentation.statusText == state.displayName(locale: .enUS))
        }
    }

    // MARK: - Write-disable flag + banner

    @Test func protocolMismatchDisablesWrites() {
        let presentation = DaemonHealthPresentation(
            locale: .zhCN,
            connectionState: .protocolMismatch,
            lastHealthCheckAt: nil,
            connectionLog: []
        )
        #expect(presentation.shouldDisableWrites == true)
        #expect(presentation.writesDisabledBanner != nil)
        #expect(presentation.writesDisabledBanner == AppStrings.DaemonHealth.writesDisabledBanner(.zhCN))
    }

    @Test func nonMismatchStatesKeepWritesEnabled() {
        let states: [AppState.ConnectionState] = [
            .connected, .connecting, .disconnected, .authFailed, .stale
        ]
        for state in states {
            let presentation = DaemonHealthPresentation(
                locale: .enUS,
                connectionState: state,
                lastHealthCheckAt: nil,
                connectionLog: []
            )
            #expect(presentation.shouldDisableWrites == false, "state: \(state)")
            #expect(presentation.writesDisabledBanner == nil, "state: \(state)")
        }
    }

    // MARK: - Next-action hints

    @Test func nextActionHintsZhCN() {
        let cases: [AppState.ConnectionState: String] = [
            .disconnected: "请在终端启动 NaumiAgent 守护进程后重试。",
            .authFailed: "请检查或更新 Bearer Token 后重试。",
            .protocolMismatch: "请升级 NaumiAgent 守护进程或本应用到兼容版本。",
            .stale: "连接已失效，正在尝试恢复。"
        ]
        for (state, expected) in cases {
            let presentation = DaemonHealthPresentation(
                locale: .zhCN,
                connectionState: state,
                lastHealthCheckAt: nil,
                connectionLog: []
            )
            #expect(presentation.nextActionText == expected, "state: \(state)")
        }
    }

    @Test func nextActionHintsEnUS() {
        let presentation = DaemonHealthPresentation(
            locale: .enUS,
            connectionState: .disconnected,
            lastHealthCheckAt: nil,
            connectionLog: []
        )
        #expect(presentation.nextActionText == "Start the NaumiAgent daemon in a terminal, then retry.")
    }

    @Test func nextActionEmptyForHealthyStates() {
        for state in [AppState.ConnectionState.connected, .connecting] {
            let presentation = DaemonHealthPresentation(
                locale: .zhCN,
                connectionState: state,
                lastHealthCheckAt: nil,
                connectionLog: []
            )
            #expect(presentation.nextActionText == "", "state: \(state)")
        }
    }

    // MARK: - Last-checked formatting

    @Test func lastCheckedNeverWhenNil() {
        let presentation = DaemonHealthPresentation(
            locale: .zhCN,
            connectionState: .connected,
            lastHealthCheckAt: nil,
            connectionLog: []
        )
        #expect(presentation.lastCheckedText == AppStrings.DaemonHealth.lastCheckedNever(.zhCN))
    }

    @Test func lastCheckedUsesInjectedFormatter() {
        let fixedDate = Date(timeIntervalSince1970: 1_700_000_000)
        let presentation = DaemonHealthPresentation(
            locale: .enUS,
            connectionState: .connected,
            lastHealthCheckAt: fixedDate,
            connectionLog: [],
            lastCheckedFormatter: { _, locale in "fixed-\(locale.rawValue)" }
        )
        #expect(presentation.lastCheckedText == "fixed-en-US")
    }

    @Test func defaultFormatterProducesNonEmptyLocalizedText() {
        let presentation = DaemonHealthPresentation(
            locale: .zhCN,
            connectionState: .connected,
            lastHealthCheckAt: Date(),
            connectionLog: []
        )
        #expect(!presentation.lastCheckedText.isEmpty)
        #expect(presentation.lastCheckedText != AppStrings.DaemonHealth.lastCheckedNever(.zhCN))
    }

    // MARK: - Recent log

    @Test func recentLogIsNewestFirstAndCapped() {
        let base = Date(timeIntervalSince1970: 1_000_000)
        var entries: [ConnectionLogEntry] = []
        // 12 entries, increasing timestamps.
        for index in 0..<12 {
            entries.append(
                ConnectionLogEntry(
                    date: base.addingTimeInterval(TimeInterval(index)),
                    state: index.isMultiple(of: 2) ? .connected : .disconnected
                )
            )
        }

        let presentation = DaemonHealthPresentation(
            locale: .zhCN,
            connectionState: .connected,
            lastHealthCheckAt: base,
            connectionLog: entries
        )

        #expect(presentation.recentLog.count == DaemonHealthPresentation.recentLogLimit)
        // Newest entry (index 11) should be first.
        #expect(presentation.recentLog.first?.state == .disconnected)
        #expect(presentation.recentLog.last?.date == entries[entries.count - DaemonHealthPresentation.recentLogLimit].date)
    }

    @Test func logLineIncludesMessageWhenPresent() {
        let entry = ConnectionLogEntry(
            date: Date(timeIntervalSince1970: 1_700_000_000),
            state: .protocolMismatch,
            message: "protocol v2 ≠ expected v1"
        )
        let presentation = DaemonHealthPresentation(
            locale: .enUS,
            connectionState: .protocolMismatch,
            lastHealthCheckAt: entry.date,
            connectionLog: [entry]
        )
        let line = presentation.logLine(for: entry)
        #expect(line.contains("Protocol Mismatch"))
        #expect(line.contains("protocol v2 ≠ expected v1"))
    }

    @Test func logLineOmitsMessageWhenAbsent() {
        let entry = ConnectionLogEntry(
            date: Date(timeIntervalSince1970: 1_700_000_000),
            state: .connected
        )
        let presentation = DaemonHealthPresentation(
            locale: .zhCN,
            connectionState: .connected,
            lastHealthCheckAt: entry.date,
            connectionLog: [entry]
        )
        let line = presentation.logLine(for: entry)
        #expect(line.contains("已连接"))
        #expect(!line.contains("· ·"))
    }

    // MARK: - Start command

    @Test func startCommandReflectsDefault() {
        let presentation = DaemonHealthPresentation(
            locale: .zhCN,
            connectionState: .disconnected,
            lastHealthCheckAt: nil,
            connectionLog: []
        )
        #expect(presentation.startCommand == "naumi serve --host 127.0.0.1 --port 8765")
    }
}
