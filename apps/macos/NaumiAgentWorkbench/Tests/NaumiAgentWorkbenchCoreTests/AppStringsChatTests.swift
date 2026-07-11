import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsChatTests {

    @Test func executionStringsDefaultToChinese() {
        #expect(AppStrings.Chat.executionStage(.zhCN, stage: .preparing) == "正在准备")
        #expect(AppStrings.Chat.executionStage(.zhCN, stage: .analyzing) == "正在分析请求")
        #expect(AppStrings.Chat.executionStage(.zhCN, stage: .runningTool) == "正在执行工具")
        #expect(AppStrings.Chat.executionStage(.zhCN, stage: .awaitingApproval) == "等待你的确认")
        #expect(AppStrings.Chat.executionElapsed(.zhCN, seconds: 12) == "已处理 12 秒")
        #expect(AppStrings.Chat.executionTool(.zhCN, toolName: "bash_run") == "正在运行 bash_run")
        #expect(AppStrings.Chat.permissionRequired(.zhCN) == "此操作需要你的确认")
        #expect(AppStrings.Chat.permissionRisk(.zhCN, level: "medium") == "风险：中")
        #expect(AppStrings.Chat.allowOnce(.zhCN) == "允许一次")
        #expect(AppStrings.Chat.deny(.zhCN) == "拒绝")
    }

    @Test func executionStringsSupportEnglish() {
        #expect(AppStrings.Chat.executionStage(.enUS, stage: .preparing) == "Preparing")
        #expect(AppStrings.Chat.executionStage(.enUS, stage: .analyzing) == "Analyzing request")
        #expect(AppStrings.Chat.executionElapsed(.enUS, seconds: 12) == "Processing for 12s")
        #expect(AppStrings.Chat.executionTool(.enUS, toolName: "bash_run") == "Running bash_run")
        #expect(AppStrings.Chat.permissionRequired(.enUS) == "This action needs your approval")
        #expect(AppStrings.Chat.permissionRisk(.enUS, level: "medium") == "Risk: Medium")
        #expect(AppStrings.Chat.allowOnce(.enUS) == "Allow once")
        #expect(AppStrings.Chat.deny(.enUS) == "Deny")
    }
}
