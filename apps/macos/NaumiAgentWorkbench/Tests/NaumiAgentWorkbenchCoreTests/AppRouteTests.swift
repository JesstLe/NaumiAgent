import Testing
@testable import NaumiAgentWorkbenchCore

struct AppRouteTests {

    @Test func displayNamesZhCN() {
        #expect(AppRoute.dashboard.displayName(locale: .zhCN) == "总览")
        #expect(AppRoute.taskMarket.displayName(locale: .zhCN) == "任务市场")
        #expect(AppRoute.worktrees.displayName(locale: .zhCN) == "工作区")
        #expect(AppRoute.reviews.displayName(locale: .zhCN) == "审查")
        #expect(AppRoute.timeline.displayName(locale: .zhCN) == "时间线")
        #expect(AppRoute.settings.displayName(locale: .zhCN) == "设置")
    }

    @Test func displayNamesEnUS() {
        #expect(AppRoute.dashboard.displayName(locale: .enUS) == "Dashboard")
        #expect(AppRoute.taskMarket.displayName(locale: .enUS) == "Task Market")
        #expect(AppRoute.worktrees.displayName(locale: .enUS) == "Worktrees")
        #expect(AppRoute.reviews.displayName(locale: .enUS) == "Reviews")
        #expect(AppRoute.timeline.displayName(locale: .enUS) == "Timeline")
        #expect(AppRoute.settings.displayName(locale: .enUS) == "Settings")
    }

    @Test func allCasesHaveDisplayNames() {
        for route in AppRoute.allCases {
            #expect(!route.displayName(locale: .zhCN).isEmpty)
            #expect(!route.displayName(locale: .enUS).isEmpty)
        }
    }

    @Test func topNavigationUsesReferenceOrder() {
        #expect(AppRoute.topNavigationRoutes == [
            .dashboard,
            .taskMarket,
            .worktrees,
            .reviews,
            .timeline,
            .settings
        ])
    }

    @Test func topNavigationRoutesShareWorkbenchDesignViewport() {
        for route in AppRoute.topNavigationRoutes {
            let layout = route.workbenchPageLayout

            #expect(layout.baseWidth == 1440)
            #expect(layout.baseHeight == 858)
        }
    }

    @Test func routePlaceholderStrings() {
        #expect(AppStrings.Navigation.pageUnderConstruction(.zhCN) == "页面建设中")
        #expect(AppStrings.Navigation.pageUnderConstruction(.enUS) == "Page under construction")
    }
}
