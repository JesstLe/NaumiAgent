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
        #expect(AppStrings.Chat.executionStage(.zhCN, stage: .cancelled) == "已取消")
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
        #expect(AppStrings.Chat.executionStage(.enUS, stage: .cancelled) == "Cancelled")
    }

    @Test func composerAndInspectorStringsAreBilingual() {
        #expect(AppStrings.Chat.addSource(.zhCN) == "添加来源")
        #expect(AppStrings.Chat.taskLinkage(.zhCN) == "任务联动")
        #expect(AppStrings.Chat.stopButton(.zhCN) == "停止")
        #expect(AppStrings.Chat.environmentSection(.zhCN) == "环境信息")
        #expect(AppStrings.Chat.backgroundProcessesSection(.zhCN) == "后台进程")
        #expect(AppStrings.Chat.sourcesSection(.zhCN) == "来源")

        #expect(AppStrings.Chat.addSource(.enUS) == "Add source")
        #expect(AppStrings.Chat.taskLinkage(.enUS) == "Task linkage")
        #expect(AppStrings.Chat.stopButton(.enUS) == "Stop")
        #expect(AppStrings.Chat.environmentSection(.enUS) == "Environment")
        #expect(AppStrings.Chat.backgroundProcessesSection(.enUS) == "Background processes")
        #expect(AppStrings.Chat.sourcesSection(.enUS) == "Sources")
    }
}
