import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsSessionSelectorTests {

    @Test func sessionSelectorStringsZhCN() {
        #expect(AppStrings.SessionSelector.sectionTitle(.zhCN) == "会话")
        #expect(AppStrings.SessionSelector.refreshButton(.zhCN) == "刷新")
        #expect(AppStrings.SessionSelector.emptySessions(.zhCN) == "未加载会话")
        #expect(AppStrings.SessionSelector.messageCountLabel(.zhCN, count: 3) == "3 条消息")
    }

    @Test func sessionSelectorStringsEnUS() {
        #expect(AppStrings.SessionSelector.sectionTitle(.enUS) == "Sessions")
        #expect(AppStrings.SessionSelector.refreshButton(.enUS) == "Refresh")
        #expect(AppStrings.SessionSelector.emptySessions(.enUS) == "No sessions loaded")
        #expect(AppStrings.SessionSelector.messageCountLabel(.enUS, count: 3) == "3 messages")
    }

    @Test func allSessionSelectorStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.SessionSelector.sectionTitle,
            AppStrings.SessionSelector.refreshButton,
            AppStrings.SessionSelector.emptySessions,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }

        #expect(!AppStrings.SessionSelector.messageCountLabel(.zhCN, count: 0).isEmpty)
        #expect(!AppStrings.SessionSelector.messageCountLabel(.enUS, count: 0).isEmpty)
    }
}
