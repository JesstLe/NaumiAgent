import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct SnapshotFreshnessPresentationTests {

    // MARK: - Never refreshed

    @Test func neverRefreshedShowsPlaceholderAndIsNotStale() {
        let zh = SnapshotFreshnessPresentation(
            locale: .zhCN,
            lastRefreshedAt: nil,
            lastError: nil
        )
        #expect(zh.lastRefreshedText == AppStrings.SnapshotFreshness.neverRefreshed(.zhCN))
        #expect(zh.isStale == false)
        #expect(zh.failureSummary == nil)

        let en = SnapshotFreshnessPresentation(
            locale: .enUS,
            lastRefreshedAt: nil,
            lastError: nil
        )
        #expect(en.lastRefreshedText == AppStrings.SnapshotFreshness.neverRefreshed(.enUS))
        #expect(en.isStale == false)
    }

    // MARK: - Relative-elapsed formatting

    @Test func relativeElapsedSecondsZhCN() {
        let start = Date(timeIntervalSince1970: 1_000)
        let end = Date(timeIntervalSince1970: 1_045)
        let label = SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .zhCN)
        #expect(label == "45 秒前")
    }

    @Test func relativeElapsedSecondsEnUS() {
        let start = Date(timeIntervalSince1970: 1_000)
        let end = Date(timeIntervalSince1970: 1_007)
        let label = SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .enUS)
        #expect(label == "7 s ago")
    }

    @Test func relativeElapsedMinutesZhCN() {
        let start = Date(timeIntervalSince1970: 0)
        let end = Date(timeIntervalSince1970: 5 * 60 + 10)
        let label = SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .zhCN)
        #expect(label == "5 分钟前")
    }

    @Test func relativeElapsedMinutesEnUS() {
        let start = Date(timeIntervalSince1970: 0)
        let end = Date(timeIntervalSince1970: 12 * 60)
        let label = SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .enUS)
        #expect(label == "12 m ago")
    }

    @Test func relativeElapsedHours() {
        let start = Date(timeIntervalSince1970: 0)
        let end = Date(timeIntervalSince1970: 3 * 3600)
        #expect(
            SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .zhCN)
                == "3 小时前"
        )
        #expect(
            SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .enUS)
                == "3 h ago"
        )
    }

    @Test func relativeElapsedDays() {
        let start = Date(timeIntervalSince1970: 0)
        let end = Date(timeIntervalSince1970: 2 * 86_400)
        #expect(
            SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .zhCN)
                == "2 天前"
        )
        #expect(
            SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .enUS)
                == "2 d ago"
        )
    }

    @Test func relativeElapsedClampsNegativeToZeroSeconds() {
        // If the clock ever goes backwards, we never show a negative duration.
        let start = Date(timeIntervalSince1970: 2_000)
        let end = Date(timeIntervalSince1970: 1_500)
        let label = SnapshotFreshnessPresentation.relativeElapsed(from: start, to: end, locale: .enUS)
        #expect(label == "0 s ago")
    }

    @Test func lastRefreshedTextUsesRelativeElapsedWhenRefreshed() {
        let start = Date(timeIntervalSince1970: 1_000)
        let now = Date(timeIntervalSince1970: 1_030)
        let presentation = SnapshotFreshnessPresentation(
            locale: .zhCN,
            lastRefreshedAt: start,
            lastError: nil,
            now: now
        )
        #expect(presentation.lastRefreshedText == "30 秒前")
    }

    // MARK: - Stale detection

    @Test func freshSnapshotIsNotStale() {
        let start = Date(timeIntervalSince1970: 0)
        let now = Date(timeIntervalSince1970: 10)
        let presentation = SnapshotFreshnessPresentation(
            locale: .enUS,
            lastRefreshedAt: start,
            lastError: nil,
            now: now
        )
        #expect(presentation.isStale == false)
    }

    @Test func snapshotBecomesStaleAtThreshold() {
        let start = Date(timeIntervalSince1970: 0)
        let exactlyStale = Date(
            timeIntervalSince1970: SnapshotFreshnessPresentation.staleThresholdSeconds
        )
        let presentation = SnapshotFreshnessPresentation(
            locale: .enUS,
            lastRefreshedAt: start,
            lastError: nil,
            now: exactlyStale
        )
        #expect(presentation.isStale == true)
    }

    @Test func snapshotJustUnderThresholdIsFresh() {
        let start = Date(timeIntervalSince1970: 0)
        let justUnder = Date(
            timeIntervalSince1970: SnapshotFreshnessPresentation.staleThresholdSeconds - 0.01
        )
        let presentation = SnapshotFreshnessPresentation(
            locale: .enUS,
            lastRefreshedAt: start,
            lastError: nil,
            now: justUnder
        )
        #expect(presentation.isStale == false)
    }

    @Test func staleSnapshotRemainsStaleAfterLongGap() {
        let start = Date(timeIntervalSince1970: 0)
        let now = Date(timeIntervalSince1970: 7 * 86_400)
        let presentation = SnapshotFreshnessPresentation(
            locale: .enUS,
            lastRefreshedAt: start,
            lastError: nil,
            now: now
        )
        #expect(presentation.isStale == true)
    }

    // MARK: - Failure summary

    @Test func failureSummaryNilWhenNoError() {
        let presentation = SnapshotFreshnessPresentation(
            locale: .zhCN,
            lastRefreshedAt: Date(timeIntervalSince1970: 0),
            lastError: nil,
            now: Date(timeIntervalSince1970: 5)
        )
        #expect(presentation.failureSummary == nil)
    }

    @Test func failureSummaryMirrorsAPIErrorLocalizedMessage() {
        let now = Date(timeIntervalSince1970: 5)
        for error in [
            APIError.authFailed,
            APIError.networkFailure("connection refused"),
            APIError.serverError(statusCode: 500, detail: "boom"),
            APIError.protocolVersionMismatch(expected: 2, actual: 1)
        ] {
            let presentation = SnapshotFreshnessPresentation(
                locale: .zhCN,
                lastRefreshedAt: Date(timeIntervalSince1970: 0),
                lastError: error,
                now: now
            )
            #expect(presentation.failureSummary == error.localizedMessage(locale: .zhCN))
        }
    }

    @Test func failureSummaryDoesNotClearRefreshedText() {
        // A failure after a previous success must keep showing the last good
        // refresh time alongside the error, so the user knows old data is shown.
        let start = Date(timeIntervalSince1970: 1_000)
        let now = Date(timeIntervalSince1970: 1_020)
        let presentation = SnapshotFreshnessPresentation(
            locale: .enUS,
            lastRefreshedAt: start,
            lastError: .networkFailure("offline"),
            now: now
        )
        #expect(presentation.lastRefreshedText == "20 s ago")
        #expect(presentation.failureSummary == APIError.networkFailure("offline").localizedMessage(locale: .enUS))
    }
}
