import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsDashboardAgentTests {

    @Test func agentStatusStringsZhCN() {
        #expect(AppStrings.Dashboard.agentStatusIdle(.zhCN) == "空闲")
        #expect(AppStrings.Dashboard.agentStatusBusy(.zhCN) == "忙碌")
        #expect(AppStrings.Dashboard.agentStatusStale(.zhCN) == "过期")
        #expect(AppStrings.Dashboard.agentStatusOffline(.zhCN) == "离线")
    }

    @Test func agentStatusStringsEnUS() {
        #expect(AppStrings.Dashboard.agentStatusIdle(.enUS) == "Idle")
        #expect(AppStrings.Dashboard.agentStatusBusy(.enUS) == "Busy")
        #expect(AppStrings.Dashboard.agentStatusStale(.enUS) == "Stale")
        #expect(AppStrings.Dashboard.agentStatusOffline(.enUS) == "Offline")
    }

    @Test func agentInspectorStringsZhCN() {
        #expect(AppStrings.Dashboard.lastHeartbeatLabel(.zhCN) == "上次心跳")
        #expect(AppStrings.Dashboard.currentIssueLabel(.zhCN) == "当前任务")
        #expect(AppStrings.Dashboard.currentLeaseLabel(.zhCN) == "当前租约")
        #expect(AppStrings.Dashboard.permissionsLabel(.zhCN) == "权限")
        #expect(AppStrings.Dashboard.noHeartbeat(.zhCN) == "无")
        #expect(AppStrings.Dashboard.permissionRiskWarning(.zhCN) == "警告：该智能体拥有高风险权限（写/删/执行/管理员）。")
    }

    @Test func agentInspectorStringsEnUS() {
        #expect(AppStrings.Dashboard.lastHeartbeatLabel(.enUS) == "Last Heartbeat")
        #expect(AppStrings.Dashboard.currentIssueLabel(.enUS) == "Current Issue")
        #expect(AppStrings.Dashboard.currentLeaseLabel(.enUS) == "Current Lease")
        #expect(AppStrings.Dashboard.permissionsLabel(.enUS) == "Permissions")
        #expect(AppStrings.Dashboard.noHeartbeat(.enUS) == "None")
        #expect(AppStrings.Dashboard.permissionRiskWarning(.enUS) == "Warning: this agent has high-risk permissions (write/delete/execute/admin).")
    }

    @Test func allAgentDashboardStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Dashboard.agentStatusIdle,
            AppStrings.Dashboard.agentStatusBusy,
            AppStrings.Dashboard.agentStatusStale,
            AppStrings.Dashboard.agentStatusOffline,
            AppStrings.Dashboard.lastHeartbeatLabel,
            AppStrings.Dashboard.currentIssueLabel,
            AppStrings.Dashboard.currentLeaseLabel,
            AppStrings.Dashboard.permissionsLabel,
            AppStrings.Dashboard.noHeartbeat,
            AppStrings.Dashboard.permissionRiskWarning,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
