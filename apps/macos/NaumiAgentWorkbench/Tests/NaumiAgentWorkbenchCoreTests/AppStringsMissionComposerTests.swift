import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsMissionComposerTests {

    @Test func missionComposerStringsZhCN() {
        #expect(AppStrings.MissionComposer.newMissionButton(.zhCN) == "新建 Mission")
        #expect(AppStrings.MissionComposer.sheetTitle(.zhCN) == "新建 Mission")
        #expect(AppStrings.MissionComposer.titleFieldLabel(.zhCN) == "标题")
        #expect(AppStrings.MissionComposer.goalFieldLabel(.zhCN) == "目标")
        #expect(AppStrings.MissionComposer.cancelButton(.zhCN) == "取消")
        #expect(AppStrings.MissionComposer.createButton(.zhCN) == "创建")
    }

    @Test func missionComposerStringsEnUS() {
        #expect(AppStrings.MissionComposer.newMissionButton(.enUS) == "New Mission")
        #expect(AppStrings.MissionComposer.sheetTitle(.enUS) == "New Mission")
        #expect(AppStrings.MissionComposer.titleFieldLabel(.enUS) == "Title")
        #expect(AppStrings.MissionComposer.goalFieldLabel(.enUS) == "Goal")
        #expect(AppStrings.MissionComposer.cancelButton(.enUS) == "Cancel")
        #expect(AppStrings.MissionComposer.createButton(.enUS) == "Create")
    }

    @Test func allMissionComposerStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.MissionComposer.newMissionButton,
            AppStrings.MissionComposer.sheetTitle,
            AppStrings.MissionComposer.titleFieldLabel,
            AppStrings.MissionComposer.goalFieldLabel,
            AppStrings.MissionComposer.cancelButton,
            AppStrings.MissionComposer.createButton,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
