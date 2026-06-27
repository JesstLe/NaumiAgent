import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsConnectionControlTests {

    @Test func connectionControlStringsZhCN() {
        #expect(AppStrings.ConnectionControl.refreshButton(.zhCN) == "重试连接")
        #expect(AppStrings.ConnectionControl.refreshButtonHelp(.zhCN) == "手动刷新本地服务连接")
    }

    @Test func connectionControlStringsEnUS() {
        #expect(AppStrings.ConnectionControl.refreshButton(.enUS) == "Refresh Connection")
        #expect(AppStrings.ConnectionControl.refreshButtonHelp(.enUS) == "Manually refresh the local daemon connection")
    }

    @Test func allConnectionControlStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.ConnectionControl.refreshButton,
            AppStrings.ConnectionControl.refreshButtonHelp,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
