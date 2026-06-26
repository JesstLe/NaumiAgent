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

        public static func pageUnderConstruction(_ locale: AppLocale) -> String {
            locale == .zhCN ? "页面建设中" : "Page under construction"
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

        public static func errorSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "连接错误" : "Connection Error"
        }

        public static func errorDetailLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "详情" : "Details"
        }

        public static func missionSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "当前目标" : "Current Mission"
        }

        public static func taskQueueSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务队列" : "Task Queue"
        }

        public static func failuresSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "失败验证" : "Validation Failures"
        }

        public static func eventsSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "最近事件" : "Recent Events"
        }

        public static func statusLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "状态" : "Status"
        }

        public static func ownerLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "负责人" : "Owner"
        }

        public static func riskLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "风险" : "Risk"
        }

        public static func activeFormLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "当前形态" : "Active Form"
        }

        public static func kindLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "类型" : "Kind"
        }

        public static func actorLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "执行者" : "Actor"
        }

        public static func parallelModeLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "并行模式" : "Parallel Mode"
        }

        public static func acceptanceCriteriaLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验收标准" : "Acceptance Criteria"
        }

        public static func subjectsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "对象" : "Subject"
        }

        public static func emptyTasks(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无任务" : "No tasks"
        }

        public static func emptyFailures(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无失败" : "No failures"
        }

        public static func emptyEvents(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无事件" : "No events"
        }
    }

    // MARK: - Task Market
    public enum TaskMarket {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务市场" : "Task Market"
        }

        public static func totalIssues(_ locale: AppLocale) -> String {
            locale == .zhCN ? "全部问题" : "Total Issues"
        }

        public static func openIssues(_ locale: AppLocale) -> String {
            locale == .zhCN ? "待认领" : "Open"
        }

        public static func claimedIssues(_ locale: AppLocale) -> String {
            locale == .zhCN ? "已认领" : "Claimed"
        }

        public static func blockedIssues(_ locale: AppLocale) -> String {
            locale == .zhCN ? "被阻塞" : "Blocked"
        }

        public static func approvalRequiredIssues(_ locale: AppLocale) -> String {
            locale == .zhCN ? "需审批" : "Approval Required"
        }

        public static func columnIssue(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务" : "Issue"
        }

        public static func columnParallelMode(_ locale: AppLocale) -> String {
            locale == .zhCN ? "并行模式" : "Parallel Mode"
        }

        public static func columnRisk(_ locale: AppLocale) -> String {
            locale == .zhCN ? "风险" : "Risk"
        }

        public static func columnDependencies(_ locale: AppLocale) -> String {
            locale == .zhCN ? "依赖" : "Dependencies"
        }

        public static func columnBids(_ locale: AppLocale) -> String {
            locale == .zhCN ? "竞标" : "Bids"
        }

        public static func columnLease(_ locale: AppLocale) -> String {
            locale == .zhCN ? "租约" : "Lease"
        }

        public static func columnWorktree(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作区" : "Worktree"
        }

        public static func columnStatus(_ locale: AppLocale) -> String {
            locale == .zhCN ? "状态" : "Status"
        }

        public static func leaseStateClaimed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "已认领" : "Claimed"
        }

        public static func leaseStateOpen(_ locale: AppLocale) -> String {
            locale == .zhCN ? "可认领" : "Open"
        }

        public static func worktreePlaceholder(_ locale: AppLocale) -> String {
            locale == .zhCN ? "未绑定" : "Unbound"
        }

        public static func ownerPlaceholder(_ locale: AppLocale) -> String {
            locale == .zhCN ? "无负责人" : "No owner"
        }

        public static func emptyIssues(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无可认领任务" : "No issues available"
        }

        public static func noSnapshot(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无快照数据" : "No snapshot data"
        }

        public static func inspectorTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "详情" : "Details"
        }

        public static func acceptanceCriteriaTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验收标准" : "Acceptance Criteria"
        }

        public static func requiresApprovalLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "需要人工审批" : "Requires human approval"
        }

        public static func bidsNotAvailable(_ locale: AppLocale) -> String {
            locale == .zhCN ? "当前快照未提供竞标数据" : "Bid data is not available in the current snapshot"
        }

        public static func leaseAgentLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "租约代理" : "Lease Agent"
        }

        public static func leaseExpiresAtLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "过期时间" : "Expires At"
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
