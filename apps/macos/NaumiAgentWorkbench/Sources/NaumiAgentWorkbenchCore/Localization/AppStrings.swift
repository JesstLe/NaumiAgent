import Foundation

/// Centralized user-facing strings. 默认中文，en-US fallback。
public enum AppStrings {

    // MARK: - Session Selector
    public enum SessionSelector {
        public static func sectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "会话" : "Sessions"
        }

        public static func refreshButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新" : "Refresh"
        }

        public static func emptySessions(_ locale: AppLocale) -> String {
            locale == .zhCN ? "未加载会话" : "No sessions loaded"
        }

        public static func messageCountLabel(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN ? "\(count) 条消息" : "\(count) messages"
        }
    }

    // MARK: - Mission Composer
    public enum MissionComposer {
        public static func newMissionButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "新建 Mission" : "New Mission"
        }

        public static func sheetTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "新建 Mission" : "New Mission"
        }

        public static func titleFieldLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "标题" : "Title"
        }

        public static func goalFieldLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "目标" : "Goal"
        }

        public static func cancelButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "取消" : "Cancel"
        }

        public static func createButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建" : "Create"
        }
    }

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

    // MARK: - Global Status
    public enum GlobalStatus {
        public static func noMission(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无 Mission" : "No Mission"
        }

        public static func activeAgents(_ locale: AppLocale) -> String {
            locale == .zhCN ? "智能体" : "Agents"
        }

        public static func openIssues(_ locale: AppLocale) -> String {
            locale == .zhCN ? "开放问题" : "Open Issues"
        }

        public static func blocked(_ locale: AppLocale) -> String {
            locale == .zhCN ? "阻塞" : "Blocked"
        }

        public static func pendingApproval(_ locale: AppLocale) -> String {
            locale == .zhCN ? "待审批" : "Pending Approval"
        }

        public static func failedValidations(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验证失败" : "Failed Validations"
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

    // MARK: - Connection Control
    public enum ConnectionControl {
        public static func refreshButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "重试连接" : "Refresh Connection"
        }

        public static func refreshButtonHelp(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "手动刷新本地服务连接"
                : "Manually refresh the local daemon connection"
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

        public static func agentsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "智能体" : "Agents"
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

        public static func agentsSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "智能体状态" : "Agent Status"
        }

        public static func failuresSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "失败验证" : "Validation Failures"
        }

        public static func eventsSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "最近事件" : "Recent Events"
        }

        public static func sharedCanvasSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "共享画板" : "Shared Canvas"
        }

        public static func issueBacklogSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "问题队列" : "Issue Backlog"
        }

        public static func inspectorSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "检查器" : "Inspector"
        }

        public static func auditTrailSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "审计流" : "Audit Trail"
        }

        public static func searchPlaceholder(_ locale: AppLocale) -> String {
            locale == .zhCN ? "搜索任务、分支、智能体" : "Search tasks, branches, agents"
        }

        public static func humanApprovalLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "人工审批" : "Human Approval"
        }

        public static func approvalRequiredValue(_ locale: AppLocale) -> String {
            locale == .zhCN ? "需要" : "Required"
        }

        public static func approvalNotRequiredValue(_ locale: AppLocale) -> String {
            locale == .zhCN ? "不需要" : "Not Required"
        }

        public static func gitWorktreesLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "Git 工作区" : "Git Worktrees"
        }

        public static func validationRunsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验证运行" : "Validation Runs"
        }

        public static func noSelection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无选中对象" : "No selection"
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

        public static func roleLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "角色" : "Role"
        }

        public static func capabilitiesLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "能力数" : "Capabilities"
        }

        public static func maxParallelTasksLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "并行上限" : "Parallel Limit"
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

        public static func emptyAgents(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无智能体" : "No agents"
        }

        public static func emptyFailures(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无失败" : "No failures"
        }

        public static func emptyEvents(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无事件" : "No events"
        }

        public static func validationStateTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验证状态" : "Validation State"
        }

        public static func contextHealthTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "上下文健康" : "Context Health"
        }

        public static func rerunValidationButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "重新运行验证" : "Re-run Validation"
        }

        public static func refreshContextButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新上下文" : "Refresh Context"
        }

        public static func runningValidationLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验证中…" : "Running…"
        }

        public static func refreshingContextLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新中…" : "Refreshing…"
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

        public static func commandSectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "操作" : "Actions"
        }

        public static func agentIDLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "代理 ID" : "Agent ID"
        }

        public static func durationLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "时长（分钟）" : "Duration (minutes)"
        }

        public static func claimButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "认领" : "Claim"
        }

        public static func releaseButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "释放租约" : "Release Lease"
        }

        public static func activeLeasesTitle(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN ? "活跃租约 (\(count))" : "Active Leases (\(count))"
        }

        public static func viewAllLeasesButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "查看全部租约" : "View All Leases"
        }

        public static func openWorktreeButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "打开工作区" : "Open Worktree"
        }

        public static func reclaimLeaseButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "回收租约" : "Reclaim Lease"
        }

        public static func releasingLeaseLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "释放中…" : "Releasing…"
        }

        public static func createIssueButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "新建 Issue" : "New Issue"
        }

        public static func createIssueSectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "新建 Issue" : "New Issue"
        }

        public static func missionIDLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "目标 ID" : "Mission ID"
        }

        public static func issueTitleLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "标题" : "Title"
        }

        public static func issueDescriptionLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "描述" : "Description"
        }

        public static func blockedByLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "阻塞依赖" : "Blocked By"
        }

        public static func blockedByHelp(_ locale: AppLocale) -> String {
            locale == .zhCN ? "每行一个任务 ID" : "One task ID per line"
        }

        public static func acceptanceCriteriaHelp(_ locale: AppLocale) -> String {
            locale == .zhCN ? "每行一条验收标准" : "One acceptance criterion per line"
        }

        public static func createIssueSubmitButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建 Issue" : "Create Issue"
        }

        public static func attachIssueButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "挂载任务" : "Attach Task"
        }

        public static func attachIssueSectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "挂载已有任务" : "Attach Existing Task"
        }

        public static func attachIssueSubmitButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "挂载为 Issue" : "Attach as Issue"
        }

        public static func processingLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "处理中…" : "Processing…"
        }

    }

    // MARK: - Worktrees
    public enum Worktrees {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作区" : "Worktrees"
        }

        public static func subtitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "Git 工作区 / 上下文健康 / Agent 负载" : "Git worktrees, context health, and agent load"
        }

        public static func snapshotCount(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN ? "\(count) 条快照" : "\(count) snapshots"
        }

        public static func refreshButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新" : "Refresh"
        }

        public static func emptySnapshots(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无上下文快照" : "No context snapshots"
        }

        public static func healthLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "健康状态" : "Health"
        }

        public static func taskIDLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务 ID" : "Task ID"
        }

        public static func agentIDLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "代理 ID" : "Agent ID"
        }

        public static func createdAtLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建时间" : "Created At"
        }

        public static func reasonsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "原因" : "Reasons"
        }

        public static func healthGood(_ locale: AppLocale) -> String {
            locale == .zhCN ? "健康" : "Good"
        }

        public static func healthStale(_ locale: AppLocale) -> String {
            locale == .zhCN ? "过期" : "Stale"
        }

        public static func healthOverloaded(_ locale: AppLocale) -> String {
            locale == .zhCN ? "过载" : "Overloaded"
        }

        public static func healthMissing(_ locale: AppLocale) -> String {
            locale == .zhCN ? "缺失" : "Missing"
        }

        public static func healthConflicted(_ locale: AppLocale) -> String {
            locale == .zhCN ? "冲突" : "Conflicted"
        }

        public static func healthUnknown(_ locale: AppLocale, health: String) -> String {
            locale == .zhCN ? "未知: \(health)" : "Unknown: \(health)"
        }

        public static func recordContextButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "记录上下文" : "Record Context"
        }

        public static func recordContextSectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "记录上下文健康" : "Record Context Health"
        }

        public static func minutesSinceSyncLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "距上次同步（分钟）" : "Minutes Since Sync"
        }

        public static func tokenLoadLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "Token 负载" : "Token Load"
        }

        public static func policyConflictLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "存在策略冲突" : "Policy Conflict"
        }

        public static func recordContextSubmitButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "保存快照" : "Save Snapshot"
        }

        public static func processingLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "处理中…" : "Processing…"
        }
    }

    // MARK: - Reviews
    public enum Reviews {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验证审查" : "Validation Reviews"
        }

        public static func runCount(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN ? "\(count) 条验证" : "\(count) validation runs"
        }

        public static func approvalCount(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN ? "\(count) 条待审批" : "\(count) pending approvals"
        }

        public static func refreshButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新" : "Refresh"
        }

        public static func emptyRuns(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无验证记录"
                : "No validation runs yet"
        }

        public static func emptyApprovals(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无待审批请求" : "No pending approvals"
        }

        public static func statusLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "状态" : "Status"
        }

        public static func taskIDLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务 ID" : "Task ID"
        }

        public static func actorLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "执行者" : "Actor"
        }

        public static func exitCodeLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "退出码" : "Exit Code"
        }

        public static func commandLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "命令" : "Command"
        }

        public static func cwdLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作目录" : "Working Directory"
        }

        public static func completedAtLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "完成时间" : "Completed At"
        }

        public static func outputLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "输出摘要" : "Output Summary"
        }

        public static func statusPassed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "通过" : "Passed"
        }

        public static func statusFailed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "失败" : "Failed"
        }

        public static func statusUnknown(_ locale: AppLocale, status: String) -> String {
            locale == .zhCN ? "未知: \(status)" : "Unknown: \(status)"
        }

        public static func runValidationSectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "运行验证" : "Run Validation"
        }

        public static func pendingApprovalsSectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "待审批" : "Pending Approvals"
        }

        public static func approveButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "同意" : "Approve"
        }

        public static func rejectButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "拒绝" : "Reject"
        }

        public static func decisionNoteLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "审批备注" : "Decision Note"
        }

        public static func requesterLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "请求人" : "Requester"
        }

        public static func titleLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "标题" : "Title"
        }

        public static func detailLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "详情" : "Detail"
        }

        public static func createdAtLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建时间" : "Created At"
        }

        public static func updatedAtLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "更新时间" : "Updated At"
        }

        public static func runButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "运行" : "Run"
        }

        public static func processingLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "处理中…" : "Processing…"
        }

        public static func convertToProposalButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "转为提案" : "Convert to Proposal"
        }

        public static func convertingToProposalLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "正在转为提案…" : "Converting…"
        }

        public static func keepWorktreeButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "保留工作区" : "Keep Worktree"
        }

        public static func keepingWorktreeLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "正在保留工作区…" : "Keeping…"
        }
    }

    // MARK: - Timeline
    public enum Timeline {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "审计时间线" : "Audit Timeline"
        }

        public static func eventCount(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN ? "\(count) 条事件" : "\(count) events"
        }

        public static func actorLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "执行者" : "Actor"
        }

        public static func subjectLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "对象" : "Subject"
        }

        public static func eventTypeLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "事件类型" : "Event Type"
        }

        public static func applyFilterButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "应用筛选" : "Apply Filter"
        }

        public static func clearFilterButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "清除" : "Clear"
        }

        public static func refreshButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新" : "Refresh"
        }

        public static func emptyEvents(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无事件" : "No events"
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

        public static func missingSelectedSession(_ locale: AppLocale) -> String {
            locale == .zhCN ? "请先选择一个会话" : "Select a session first"
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
            if locale == .zhCN {
                return "无法连接本地 NaumiAgent 服务，请先启动：\nnaumi-agent api --host 127.0.0.1 --port 8765"
            }
            return "Cannot reach the local NaumiAgent service. Start it with:\nnaumi-agent api --host 127.0.0.1 --port 8765"
        }

        public static func capabilityUnavailable(_ locale: AppLocale, capability: String) -> String {
            let name = localizedCapabilityName(locale, capability: capability)
            return locale == .zhCN
                ? "当前 daemon 不支持「\(name)」"
                : "The daemon does not support '\(name)'"
        }

        public static func protocolVersionMismatch(
            _ locale: AppLocale,
            expected: Int,
            actual: Int
        ) -> String {
            locale == .zhCN
                ? "Workbench 协议版本不兼容：需要 \(expected)，当前 daemon 返回 \(actual)"
                : "Workbench protocol mismatch: expected \(expected), daemon returned \(actual)"
        }

        private static func localizedCapabilityName(_ locale: AppLocale, capability: String) -> String {
            switch capability {
            case "validation_runner":
                return locale == .zhCN ? "验证运行器" : "validation runner"
            default:
                return capability
            }
        }
    }

    // MARK: - Settings
    public enum Settings {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "设置" : "Settings"
        }

        public static func languageSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "语言" : "Language"
        }

        public static func governanceSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "治理策略" : "Governance Policies"
        }

        public static func currentLanguageLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "当前语言" : "Current Language"
        }

        public static func highRiskApprovalPolicy(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "高风险动作需要人工审批"
                : "High-risk actions require human approval"
        }

        public static func localDaemonPolicy(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "本地 daemon 仅监听 127.0.0.1"
                : "Local daemon only listens on 127.0.0.1"
        }

        public static func writeViaWorkbenchAPIPolicy(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "写操作必须经 Workbench API 转发"
                : "Write operations must go through the Workbench API"
        }

        public static func createIntentLockSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建意图锁" : "Create Intent Lock"
        }

        public static func missionIDFieldLabel(_ locale: AppLocale) -> String {
            "Mission ID"
        }

        public static func actorFieldLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "执行者" : "Actor"
        }

        public static func ruleFieldLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "规则" : "Rule"
        }

        public static func blockedPathsFieldLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "阻塞路径" : "Blocked Paths"
        }

        public static func allowedPathsFieldLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "允许路径" : "Allowed Paths"
        }

        public static func requireProposalForRiskLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "需提案的风险等级" : "Require Proposal For Risk"
        }

        public static func createIntentLockButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建意图锁" : "Create Intent Lock"
        }

        public static func processingLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "处理中…" : "Processing…"
        }
    }

    // MARK: - Governance Risk Level
    public enum GovernanceRiskLevel {
        public static func low(_ locale: AppLocale) -> String {
            locale == .zhCN ? "低" : "Low"
        }

        public static func medium(_ locale: AppLocale) -> String {
            locale == .zhCN ? "中" : "Medium"
        }

        public static func high(_ locale: AppLocale) -> String {
            locale == .zhCN ? "高" : "High"
        }

        public static func critical(_ locale: AppLocale) -> String {
            locale == .zhCN ? "严重" : "Critical"
        }
    }
}
