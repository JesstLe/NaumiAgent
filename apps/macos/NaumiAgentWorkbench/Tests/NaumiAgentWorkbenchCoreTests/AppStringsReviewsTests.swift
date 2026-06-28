import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsReviewsTests {

    @Test func reviewsStringsZhCN() {
        #expect(AppStrings.Reviews.title(.zhCN) == "验证审查")
        #expect(AppStrings.Reviews.runCount(.zhCN, count: 3) == "3 条验证")
        #expect(AppStrings.Reviews.approvalCount(.zhCN, count: 3) == "3 条待审批")
        #expect(AppStrings.Reviews.refreshButton(.zhCN) == "刷新")
        #expect(AppStrings.Reviews.emptyRuns(.zhCN) == "暂无验证记录")
        #expect(AppStrings.Reviews.emptyApprovals(.zhCN) == "暂无待审批请求")
        #expect(AppStrings.Reviews.statusLabel(.zhCN) == "状态")
        #expect(AppStrings.Reviews.taskIDLabel(.zhCN) == "任务 ID")
        #expect(AppStrings.Reviews.actorLabel(.zhCN) == "执行者")
        #expect(AppStrings.Reviews.exitCodeLabel(.zhCN) == "退出码")
        #expect(AppStrings.Reviews.commandLabel(.zhCN) == "命令")
        #expect(AppStrings.Reviews.cwdLabel(.zhCN) == "工作目录")
        #expect(AppStrings.Reviews.completedAtLabel(.zhCN) == "完成时间")
        #expect(AppStrings.Reviews.outputLabel(.zhCN) == "输出摘要")
        #expect(AppStrings.Reviews.statusPassed(.zhCN) == "通过")
        #expect(AppStrings.Reviews.statusFailed(.zhCN) == "失败")
        #expect(AppStrings.Reviews.statusUnknown(.zhCN, status: "x") == "未知: x")
        #expect(AppStrings.Reviews.runValidationSectionTitle(.zhCN) == "运行验证")
        #expect(AppStrings.Reviews.pendingApprovalsSectionTitle(.zhCN) == "待审批")
        #expect(AppStrings.Reviews.approveButton(.zhCN) == "同意")
        #expect(AppStrings.Reviews.rejectButton(.zhCN) == "拒绝")
        #expect(AppStrings.Reviews.decisionNoteLabel(.zhCN) == "审批备注")
        #expect(AppStrings.Reviews.requesterLabel(.zhCN) == "请求人")
        #expect(AppStrings.Reviews.titleLabel(.zhCN) == "标题")
        #expect(AppStrings.Reviews.detailLabel(.zhCN) == "详情")
        #expect(AppStrings.Reviews.createdAtLabel(.zhCN) == "创建时间")
        #expect(AppStrings.Reviews.updatedAtLabel(.zhCN) == "更新时间")
        #expect(AppStrings.Reviews.runButton(.zhCN) == "运行")
        #expect(AppStrings.Reviews.processingLabel(.zhCN) == "处理中…")
        #expect(AppStrings.Reviews.convertToProposalButton(.zhCN) == "转为提案")
        #expect(AppStrings.Reviews.convertingToProposalLabel(.zhCN) == "正在转为提案…")
        #expect(AppStrings.Reviews.keepWorktreeButton(.zhCN) == "保留工作区")
        #expect(AppStrings.Reviews.keepingWorktreeLabel(.zhCN) == "正在保留工作区…")
    }

    @Test func reviewsStringsEnUS() {
        #expect(AppStrings.Reviews.title(.enUS) == "Validation Reviews")
        #expect(AppStrings.Reviews.runCount(.enUS, count: 3) == "3 validation runs")
        #expect(AppStrings.Reviews.approvalCount(.enUS, count: 3) == "3 pending approvals")
        #expect(AppStrings.Reviews.refreshButton(.enUS) == "Refresh")
        #expect(AppStrings.Reviews.emptyRuns(.enUS) == "No validation runs yet")
        #expect(AppStrings.Reviews.emptyApprovals(.enUS) == "No pending approvals")
        #expect(AppStrings.Reviews.statusLabel(.enUS) == "Status")
        #expect(AppStrings.Reviews.taskIDLabel(.enUS) == "Task ID")
        #expect(AppStrings.Reviews.actorLabel(.enUS) == "Actor")
        #expect(AppStrings.Reviews.exitCodeLabel(.enUS) == "Exit Code")
        #expect(AppStrings.Reviews.commandLabel(.enUS) == "Command")
        #expect(AppStrings.Reviews.cwdLabel(.enUS) == "Working Directory")
        #expect(AppStrings.Reviews.completedAtLabel(.enUS) == "Completed At")
        #expect(AppStrings.Reviews.outputLabel(.enUS) == "Output Summary")
        #expect(AppStrings.Reviews.statusPassed(.enUS) == "Passed")
        #expect(AppStrings.Reviews.statusFailed(.enUS) == "Failed")
        #expect(AppStrings.Reviews.statusUnknown(.enUS, status: "x") == "Unknown: x")
        #expect(AppStrings.Reviews.runValidationSectionTitle(.enUS) == "Run Validation")
        #expect(AppStrings.Reviews.pendingApprovalsSectionTitle(.enUS) == "Pending Approvals")
        #expect(AppStrings.Reviews.approveButton(.enUS) == "Approve")
        #expect(AppStrings.Reviews.rejectButton(.enUS) == "Reject")
        #expect(AppStrings.Reviews.decisionNoteLabel(.enUS) == "Decision Note")
        #expect(AppStrings.Reviews.requesterLabel(.enUS) == "Requester")
        #expect(AppStrings.Reviews.titleLabel(.enUS) == "Title")
        #expect(AppStrings.Reviews.detailLabel(.enUS) == "Detail")
        #expect(AppStrings.Reviews.createdAtLabel(.enUS) == "Created At")
        #expect(AppStrings.Reviews.updatedAtLabel(.enUS) == "Updated At")
        #expect(AppStrings.Reviews.runButton(.enUS) == "Run")
        #expect(AppStrings.Reviews.processingLabel(.enUS) == "Processing…")
        #expect(AppStrings.Reviews.convertToProposalButton(.enUS) == "Convert to Proposal")
        #expect(AppStrings.Reviews.convertingToProposalLabel(.enUS) == "Converting…")
        #expect(AppStrings.Reviews.keepWorktreeButton(.enUS) == "Keep Worktree")
        #expect(AppStrings.Reviews.keepingWorktreeLabel(.enUS) == "Keeping…")
    }

    @Test func allReviewsStringsAreNonEmpty() {
        let strings: [(AppLocale) -> String] = [
            AppStrings.Reviews.title,
            { AppStrings.Reviews.runCount($0, count: 1) },
            { AppStrings.Reviews.approvalCount($0, count: 1) },
            AppStrings.Reviews.refreshButton,
            AppStrings.Reviews.emptyRuns,
            AppStrings.Reviews.emptyApprovals,
            AppStrings.Reviews.statusLabel,
            AppStrings.Reviews.taskIDLabel,
            AppStrings.Reviews.actorLabel,
            AppStrings.Reviews.exitCodeLabel,
            AppStrings.Reviews.commandLabel,
            AppStrings.Reviews.cwdLabel,
            AppStrings.Reviews.completedAtLabel,
            AppStrings.Reviews.outputLabel,
            AppStrings.Reviews.statusPassed,
            AppStrings.Reviews.statusFailed,
            { AppStrings.Reviews.statusUnknown($0, status: "x") },
            AppStrings.Reviews.runValidationSectionTitle,
            AppStrings.Reviews.pendingApprovalsSectionTitle,
            AppStrings.Reviews.approveButton,
            AppStrings.Reviews.rejectButton,
            AppStrings.Reviews.decisionNoteLabel,
            AppStrings.Reviews.requesterLabel,
            AppStrings.Reviews.titleLabel,
            AppStrings.Reviews.detailLabel,
            AppStrings.Reviews.createdAtLabel,
            AppStrings.Reviews.updatedAtLabel,
            AppStrings.Reviews.runButton,
            AppStrings.Reviews.processingLabel,
            AppStrings.Reviews.convertToProposalButton,
            AppStrings.Reviews.convertingToProposalLabel,
            AppStrings.Reviews.keepWorktreeButton,
            AppStrings.Reviews.keepingWorktreeLabel,
        ]

        for string in strings {
            #expect(!string(.zhCN).isEmpty)
            #expect(!string(.enUS).isEmpty)
        }
    }
}
