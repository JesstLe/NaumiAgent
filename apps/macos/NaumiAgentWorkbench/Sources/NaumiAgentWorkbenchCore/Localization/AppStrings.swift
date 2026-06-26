import Foundation

/// Centralized user-facing strings. 默认中文，en-US fallback。
public enum AppStrings {

    // MARK: - Navigation
    public enum Navigation {
        public static func dashboard(_ locale: AppLocale) -> String {
            locale == .zhCN ? "总览" : "Dashboard"
        }

        public static func taskMarket(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务市场" : "Task Market"
        }

        public static func worktrees(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作区" : "Worktrees"
        }

        public static func reviews(_ locale: AppLocale) -> String {
            locale == .zhCN ? "审查" : "Reviews"
        }

        public static func timeline(_ locale: AppLocale) -> String {
            locale == .zhCN ? "时间线" : "Timeline"
        }

        public static func settings(_ locale: AppLocale) -> String {
            locale == .zhCN ? "设置" : "Settings"
        }
    }

    // MARK: - Connection State
    public enum Connection {
        public static func connected(_ locale: AppLocale) -> String {
            locale == .zhCN ? "已连接" : "Connected"
        }

        public static func connecting(_ locale: AppLocale) -> String {
            locale == .zhCN ? "连接中" : "Connecting"
        }

        public static func disconnected(_ locale: AppLocale) -> String {
            locale == .zhCN ? "未连接" : "Disconnected"
        }

        public static func stale(_ locale: AppLocale) -> String {
            locale == .zhCN ? "连接失效" : "Connection Stale"
        }
    }

    // MARK: - Dashboard
    public enum Dashboard {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作台总览" : "Workbench Dashboard"
        }

        public static func daemonSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "本地服务状态" : "Daemon Status"
        }

        public static func daemonStatusLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "状态" : "Status"
        }

        public static func daemonHostLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "地址" : "Host"
        }

        public static func daemonPIDLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "进程 ID" : "PID"
        }

        public static func daemonWorkspaceCountLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作区" : "Workspaces"
        }

        public static func countsSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "会话统计" : "Session Counts"
        }

        public static func missionsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "目标" : "Missions"
        }

        public static func tasksLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务" : "Tasks"
        }

        public static func issuesLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "问题" : "Issues"
        }

        public static func failuresLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "失败" : "Failures"
        }

        public static func eventsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "事件" : "Events"
        }

        public static func emptySnapshot(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无快照数据" : "No snapshot data"
        }
    }

    // MARK: - Errors
    public enum Error {
        public static func unknown(_ locale: AppLocale) -> String {
            locale == .zhCN ? "未知错误" : "Unknown error"
        }

        public static func invalidURL(_ locale: AppLocale) -> String {
            locale == .zhCN ? "无效的接口地址" : "Invalid API URL"
        }

        public static func invalidResponse(_ locale: AppLocale) -> String {
            locale == .zhCN ? "接口返回异常" : "Invalid response"
        }

        public static func httpStatus(_ locale: AppLocale, code: Int) -> String {
            locale == .zhCN
                ? "HTTP 错误 \(code)"
                : "HTTP error \(code)"
        }

        public static func decodingFailed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "数据解析失败" : "Failed to decode response"
        }

        public static func networkFailure(_ locale: AppLocale) -> String {
            locale == .zhCN ? "网络请求失败" : "Network request failed"
        }
    }
}
