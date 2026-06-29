import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsTimelineTests {

    @Test func timelineStringsZhCN() {
        #expect(AppStrings.Timeline.title(.zhCN) == "审计时间线")
        #expect(AppStrings.Timeline.eventCount(.zhCN, count: 3) == "3 条事件")
        #expect(AppStrings.Timeline.actorLabel(.zhCN) == "执行者")
        #expect(AppStrings.Timeline.subjectLabel(.zhCN) == "对象")
        #expect(AppStrings.Timeline.eventTypeLabel(.zhCN) == "事件类型")
        #expect(AppStrings.Timeline.sinceLabel(.zhCN) == "起始时间")
        #expect(AppStrings.Timeline.applyFilterButton(.zhCN) == "应用筛选")
        #expect(AppStrings.Timeline.clearFilterButton(.zhCN) == "清除")
        #expect(AppStrings.Timeline.refreshButton(.zhCN) == "刷新")
        #expect(AppStrings.Timeline.emptyEvents(.zhCN) == "暂无事件")
    }

    @Test func timelineStringsEnUS() {
        #expect(AppStrings.Timeline.title(.enUS) == "Audit Timeline")
        #expect(AppStrings.Timeline.eventCount(.enUS, count: 3) == "3 events")
        #expect(AppStrings.Timeline.actorLabel(.enUS) == "Actor")
        #expect(AppStrings.Timeline.subjectLabel(.enUS) == "Subject")
        #expect(AppStrings.Timeline.eventTypeLabel(.enUS) == "Event Type")
        #expect(AppStrings.Timeline.sinceLabel(.enUS) == "Since")
        #expect(AppStrings.Timeline.applyFilterButton(.enUS) == "Apply Filter")
        #expect(AppStrings.Timeline.clearFilterButton(.enUS) == "Clear")
        #expect(AppStrings.Timeline.refreshButton(.enUS) == "Refresh")
        #expect(AppStrings.Timeline.emptyEvents(.enUS) == "No events")
    }

    @Test func allTimelineStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Timeline.title,
            { AppStrings.Timeline.eventCount($0, count: 1) },
            AppStrings.Timeline.actorLabel,
            AppStrings.Timeline.subjectLabel,
            AppStrings.Timeline.eventTypeLabel,
            AppStrings.Timeline.sinceLabel,
            AppStrings.Timeline.applyFilterButton,
            AppStrings.Timeline.clearFilterButton,
            AppStrings.Timeline.refreshButton,
            AppStrings.Timeline.emptyEvents,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
