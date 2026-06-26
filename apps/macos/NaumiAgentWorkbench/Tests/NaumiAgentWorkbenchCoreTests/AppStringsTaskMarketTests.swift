import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsTaskMarketTests {

    @Test func coreTaskMarketStringsZhCN() {
        #expect(AppStrings.TaskMarket.title(.zhCN) == "任务市场")
        #expect(AppStrings.TaskMarket.totalIssues(.zhCN) == "全部问题")
        #expect(AppStrings.TaskMarket.openIssues(.zhCN) == "待认领")
        #expect(AppStrings.TaskMarket.claimedIssues(.zhCN) == "已认领")
        #expect(AppStrings.TaskMarket.blockedIssues(.zhCN) == "被阻塞")
        #expect(AppStrings.TaskMarket.approvalRequiredIssues(.zhCN) == "需审批")
        #expect(AppStrings.TaskMarket.columnIssue(.zhCN) == "任务")
        #expect(AppStrings.TaskMarket.columnParallelMode(.zhCN) == "并行模式")
        #expect(AppStrings.TaskMarket.columnRisk(.zhCN) == "风险")
        #expect(AppStrings.TaskMarket.columnDependencies(.zhCN) == "依赖")
        #expect(AppStrings.TaskMarket.columnBids(.zhCN) == "竞标")
        #expect(AppStrings.TaskMarket.columnLease(.zhCN) == "租约")
        #expect(AppStrings.TaskMarket.columnWorktree(.zhCN) == "工作区")
        #expect(AppStrings.TaskMarket.columnStatus(.zhCN) == "状态")
        #expect(AppStrings.TaskMarket.leaseStateClaimed(.zhCN) == "已认领")
        #expect(AppStrings.TaskMarket.leaseStateOpen(.zhCN) == "可认领")
        #expect(AppStrings.TaskMarket.worktreePlaceholder(.zhCN) == "未绑定")
        #expect(AppStrings.TaskMarket.ownerPlaceholder(.zhCN) == "无负责人")
        #expect(AppStrings.TaskMarket.emptyIssues(.zhCN) == "暂无可认领任务")
        #expect(AppStrings.TaskMarket.noSnapshot(.zhCN) == "暂无快照数据")
        #expect(AppStrings.TaskMarket.inspectorTitle(.zhCN) == "详情")
        #expect(AppStrings.TaskMarket.acceptanceCriteriaTitle(.zhCN) == "验收标准")
        #expect(AppStrings.TaskMarket.requiresApprovalLabel(.zhCN) == "需要人工审批")
        #expect(AppStrings.TaskMarket.bidsNotAvailable(.zhCN) == "当前快照未提供竞标数据")
        #expect(AppStrings.TaskMarket.leaseAgentLabel(.zhCN) == "租约代理")
        #expect(AppStrings.TaskMarket.leaseExpiresAtLabel(.zhCN) == "过期时间")
        #expect(AppStrings.TaskMarket.commandSectionTitle(.zhCN) == "操作")
        #expect(AppStrings.TaskMarket.agentIDLabel(.zhCN) == "代理 ID")
        #expect(AppStrings.TaskMarket.durationLabel(.zhCN) == "时长（分钟）")
        #expect(AppStrings.TaskMarket.claimButton(.zhCN) == "认领")
        #expect(AppStrings.TaskMarket.releaseButton(.zhCN) == "释放租约")
        #expect(AppStrings.TaskMarket.processingLabel(.zhCN) == "处理中…")
    }

    @Test func coreTaskMarketStringsEnUS() {
        #expect(AppStrings.TaskMarket.title(.enUS) == "Task Market")
        #expect(AppStrings.TaskMarket.totalIssues(.enUS) == "Total Issues")
        #expect(AppStrings.TaskMarket.openIssues(.enUS) == "Open")
        #expect(AppStrings.TaskMarket.claimedIssues(.enUS) == "Claimed")
        #expect(AppStrings.TaskMarket.blockedIssues(.enUS) == "Blocked")
        #expect(AppStrings.TaskMarket.approvalRequiredIssues(.enUS) == "Approval Required")
        #expect(AppStrings.TaskMarket.columnIssue(.enUS) == "Issue")
        #expect(AppStrings.TaskMarket.columnParallelMode(.enUS) == "Parallel Mode")
        #expect(AppStrings.TaskMarket.columnRisk(.enUS) == "Risk")
        #expect(AppStrings.TaskMarket.columnDependencies(.enUS) == "Dependencies")
        #expect(AppStrings.TaskMarket.columnBids(.enUS) == "Bids")
        #expect(AppStrings.TaskMarket.columnLease(.enUS) == "Lease")
        #expect(AppStrings.TaskMarket.columnWorktree(.enUS) == "Worktree")
        #expect(AppStrings.TaskMarket.columnStatus(.enUS) == "Status")
        #expect(AppStrings.TaskMarket.leaseStateClaimed(.enUS) == "Claimed")
        #expect(AppStrings.TaskMarket.leaseStateOpen(.enUS) == "Open")
        #expect(AppStrings.TaskMarket.worktreePlaceholder(.enUS) == "Unbound")
        #expect(AppStrings.TaskMarket.ownerPlaceholder(.enUS) == "No owner")
        #expect(AppStrings.TaskMarket.emptyIssues(.enUS) == "No issues available")
        #expect(AppStrings.TaskMarket.noSnapshot(.enUS) == "No snapshot data")
        #expect(AppStrings.TaskMarket.inspectorTitle(.enUS) == "Details")
        #expect(AppStrings.TaskMarket.acceptanceCriteriaTitle(.enUS) == "Acceptance Criteria")
        #expect(AppStrings.TaskMarket.requiresApprovalLabel(.enUS) == "Requires human approval")
        #expect(AppStrings.TaskMarket.bidsNotAvailable(.enUS) == "Bid data is not available in the current snapshot")
        #expect(AppStrings.TaskMarket.leaseAgentLabel(.enUS) == "Lease Agent")
        #expect(AppStrings.TaskMarket.leaseExpiresAtLabel(.enUS) == "Expires At")
        #expect(AppStrings.TaskMarket.commandSectionTitle(.enUS) == "Actions")
        #expect(AppStrings.TaskMarket.agentIDLabel(.enUS) == "Agent ID")
        #expect(AppStrings.TaskMarket.durationLabel(.enUS) == "Duration (minutes)")
        #expect(AppStrings.TaskMarket.claimButton(.enUS) == "Claim")
        #expect(AppStrings.TaskMarket.releaseButton(.enUS) == "Release Lease")
        #expect(AppStrings.TaskMarket.processingLabel(.enUS) == "Processing…")
    }

    @Test func allTaskMarketStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.TaskMarket.title,
            AppStrings.TaskMarket.totalIssues,
            AppStrings.TaskMarket.openIssues,
            AppStrings.TaskMarket.claimedIssues,
            AppStrings.TaskMarket.blockedIssues,
            AppStrings.TaskMarket.approvalRequiredIssues,
            AppStrings.TaskMarket.columnIssue,
            AppStrings.TaskMarket.columnParallelMode,
            AppStrings.TaskMarket.columnRisk,
            AppStrings.TaskMarket.columnDependencies,
            AppStrings.TaskMarket.columnBids,
            AppStrings.TaskMarket.columnLease,
            AppStrings.TaskMarket.columnWorktree,
            AppStrings.TaskMarket.columnStatus,
            AppStrings.TaskMarket.leaseStateClaimed,
            AppStrings.TaskMarket.leaseStateOpen,
            AppStrings.TaskMarket.worktreePlaceholder,
            AppStrings.TaskMarket.ownerPlaceholder,
            AppStrings.TaskMarket.emptyIssues,
            AppStrings.TaskMarket.noSnapshot,
            AppStrings.TaskMarket.inspectorTitle,
            AppStrings.TaskMarket.acceptanceCriteriaTitle,
            AppStrings.TaskMarket.requiresApprovalLabel,
            AppStrings.TaskMarket.bidsNotAvailable,
            AppStrings.TaskMarket.leaseAgentLabel,
            AppStrings.TaskMarket.leaseExpiresAtLabel,
            AppStrings.TaskMarket.commandSectionTitle,
            AppStrings.TaskMarket.agentIDLabel,
            AppStrings.TaskMarket.durationLabel,
            AppStrings.TaskMarket.claimButton,
            AppStrings.TaskMarket.releaseButton,
            AppStrings.TaskMarket.processingLabel,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
