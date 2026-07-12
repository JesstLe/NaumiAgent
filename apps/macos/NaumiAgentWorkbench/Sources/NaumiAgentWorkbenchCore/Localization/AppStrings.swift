import Foundation

/// Centralized user-facing strings. 默认中文，en-US fallback。
public enum AppStrings {

    // MARK: - Debug
    public enum Debug {
        public static func previewFixtureBadge(_ locale: AppLocale) -> String {
            locale == .zhCN ? "预览数据" : "PREVIEW FIXTURE"
        }

        public static func previewFixtureBadgeHelp(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "当前为预览模式，数据来自本地 fixture，非真实后端状态"
                : "Preview mode: data comes from local fixtures, not the live daemon"
        }
    }

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

        public static func chat(_ locale: AppLocale) -> String {
            locale == .zhCN ? "对话" : "Chat"
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

    // MARK: - Chat
    public enum Chat {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "日常对话" : "Daily Chat"
        }

        public static func sessionSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "当前会话" : "Current Session"
        }

        public static func missionSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "关联 Mission" : "Linked Mission"
        }

        public static func messagePlaceholder(_ locale: AppLocale) -> String {
            locale == .zhCN ? "输入消息" : "Message"
        }

        public static func emptyMessages(_ locale: AppLocale) -> String {
            locale == .zhCN ? "还没有对话" : "No messages yet"
        }

        public static func createIssueToggle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "同时创建任务" : "Create issue too"
        }

        public static func issueTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务标题" : "Issue Title"
        }

        public static func issueDescription(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务描述" : "Issue Description"
        }

        public static func acceptanceCriteria(_ locale: AppLocale) -> String {
            locale == .zhCN ? "验收标准" : "Acceptance Criteria"
        }

        public static func riskLevel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "风险等级" : "Risk Level"
        }

        public static func parallelMode(_ locale: AppLocale) -> String {
            locale == .zhCN ? "并行模式" : "Parallel Mode"
        }

        public static func sendButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "发送" : "Send"
        }

        public static func stopButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "停止" : "Stop"
        }

        public static func addSource(_ locale: AppLocale) -> String {
            locale == .zhCN ? "添加来源" : "Add source"
        }

        public static func taskLinkage(_ locale: AppLocale) -> String {
            locale == .zhCN ? "任务联动" : "Task linkage"
        }

        public static func chatOnly(_ locale: AppLocale) -> String {
            locale == .zhCN ? "仅对话" : "Chat only"
        }

        public static func environmentSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "环境信息" : "Environment"
        }

        public static func changesSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "变更" : "Changes"
        }

        public static func workspaceSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "本地工作区" : "Local workspace"
        }

        public static func linkedObjectsSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "关联对象" : "Linked objects"
        }

        public static func backgroundProcessesSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "后台进程" : "Background processes"
        }

        public static func sourcesSection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "来源" : "Sources"
        }

        public static func noBackgroundProcesses(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无受管进程" : "No managed processes"
        }

        public static func noSources(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无来源" : "No sources"
        }

        public static func recentRuns(_ locale: AppLocale) -> String {
            locale == .zhCN ? "最近运行" : "Recent runs"
        }

        public static func sending(_ locale: AppLocale) -> String {
            locale == .zhCN ? "发送中" : "Sending"
        }

        public static func retryButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "重试发送" : "Retry Send"
        }

        public static func sendFailedHint(_ locale: AppLocale) -> String {
            locale == .zhCN ? "上次发送失败，内容已保留，可编辑后重试。" : "Last send failed. Content is kept — edit and retry."
        }

        public static func issueNeedsMission(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建关联任务需要先选择 Mission" : "Select a Mission to create a linked issue"
        }

        public static func highRiskNeedsCriteria(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "高风险任务至少需要一条验收标准"
                : "High-risk issues need at least one acceptance criterion"
        }

        public static func linkedIssueCreated(_ locale: AppLocale) -> String {
            locale == .zhCN ? "已创建关联任务" : "Linked issue created"
        }

        public static func noMission(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无 Mission" : "No Mission"
        }

        public static func executionFailed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "本次对话未能完成。" : "This conversation could not be completed."
        }

        public static func executionStage(_ locale: AppLocale, stage: ChatExecutionStage) -> String {
            switch (locale, stage) {
            case (.zhCN, .preparing):
                return "正在准备"
            case (.zhCN, .analyzing):
                return "正在分析请求"
            case (.zhCN, .runningTool):
                return "正在执行工具"
            case (.zhCN, .awaitingApproval):
                return "等待你的确认"
            case (.zhCN, .composing):
                return "正在生成答复"
            case (.zhCN, .creatingLinkedIssue):
                return "正在创建关联任务"
            case (.zhCN, .completed):
                return "答复已完成"
            case (.zhCN, .failed):
                return "对话未完成"
            case (.enUS, .preparing):
                return "Preparing"
            case (.enUS, .analyzing):
                return "Analyzing request"
            case (.enUS, .runningTool):
                return "Running tool"
            case (.enUS, .awaitingApproval):
                return "Waiting for your approval"
            case (.enUS, .composing):
                return "Writing response"
            case (.enUS, .creatingLinkedIssue):
                return "Creating linked issue"
            case (.enUS, .completed):
                return "Response complete"
            case (.enUS, .failed):
                return "Conversation incomplete"
            }
        }

        public static func executionElapsed(_ locale: AppLocale, seconds: Int) -> String {
            locale == .zhCN ? "已处理 \(seconds) 秒" : "Processing for \(seconds)s"
        }

        public static func executionTool(_ locale: AppLocale, toolName: String) -> String {
            locale == .zhCN ? "正在运行 \(toolName)" : "Running \(toolName)"
        }

        public static func subtaskResult(_ locale: AppLocale) -> String {
            locale == .zhCN ? "子任务结果" : "Subtask result"
        }

        public static func permissionRequired(_ locale: AppLocale) -> String {
            locale == .zhCN ? "此操作需要你的确认" : "This action needs your approval"
        }

        public static func permissionRisk(_ locale: AppLocale, level: String) -> String {
            switch (locale, level.lowercased()) {
            case (.zhCN, "low"):
                return "风险：低"
            case (.zhCN, "medium"):
                return "风险：中"
            case (.zhCN, "high"):
                return "风险：高"
            case (.zhCN, "critical"):
                return "风险：严重"
            case (.zhCN, _):
                return "风险：\(level)"
            case (.enUS, "low"):
                return "Risk: Low"
            case (.enUS, "medium"):
                return "Risk: Medium"
            case (.enUS, "high"):
                return "Risk: High"
            case (.enUS, "critical"):
                return "Risk: Critical"
            case (.enUS, _):
                return "Risk: \(level)"
            }
        }

        public static func allowOnce(_ locale: AppLocale) -> String {
            locale == .zhCN ? "允许一次" : "Allow once"
        }

        public static func deny(_ locale: AppLocale) -> String {
            locale == .zhCN ? "拒绝" : "Deny"
        }

        public static func resolvingApproval(_ locale: AppLocale) -> String {
            locale == .zhCN ? "正在提交确认" : "Submitting approval"
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

        public static func authFailed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "认证失败" : "Authentication Failed"
        }

        public static func protocolMismatch(_ locale: AppLocale) -> String {
            locale == .zhCN ? "协议版本不兼容" : "Protocol Mismatch"
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

    // MARK: - Connection Setup
    public enum ConnectionSetup {
        public static func title(_ locale: AppLocale) -> String {
            locale == .zhCN ? "连接本地服务" : "Connect to Local Daemon"
        }

        public static func endpointLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "服务地址" : "Endpoint"
        }

        public static func tokenLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "Bearer Token（可选）" : "Bearer Token (optional)"
        }

        public static func startCommandLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "启动命令" : "Start Command"
        }

        public static func startCommand(_ locale: AppLocale) -> String {
            "naumi serve --host 127.0.0.1 --port 8765"
        }

        public static func copyCommandButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "复制命令" : "Copy Command"
        }

        public static func retryButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "重试" : "Retry"
        }

        public static func saveButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "保存并连接" : "Save & Connect"
        }

        public static func connectingHint(_ locale: AppLocale) -> String {
            locale == .zhCN ? "正在连接…" : "Connecting…"
        }

        public static func reasonDisconnected(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "无法连接本地 NaumiAgent 服务。请确认守护进程已启动。"
                : "Cannot reach the local NaumiAgent daemon. Make sure it is running."
        }

        public static func reasonAuthFailed(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "认证失败：Bearer Token 被服务拒绝。"
                : "Authentication failed: the bearer token was rejected."
        }

        public static func reasonProtocolMismatch(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "协议版本不兼容：请升级 NaumiAgent 守护进程或客户端。"
                : "Protocol version mismatch: upgrade the NaumiAgent daemon or the client."
        }

        public static func reasonStale(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "连接已失效，正在尝试恢复。"
                : "The connection is stale; trying to recover."
        }

        /// Returns the human-readable explanation for a failure connection state.
        public static func reason(_ locale: AppLocale, for state: AppState.ConnectionState) -> String {
            switch state {
            case .authFailed:
                return reasonAuthFailed(locale)
            case .protocolMismatch:
                return reasonProtocolMismatch(locale)
            case .stale:
                return reasonStale(locale)
            case .disconnected:
                return reasonDisconnected(locale)
            case .connected, .connecting:
                return ""
            }
        }
    }

    // MARK: - Daemon Health
    public enum DaemonHealth {
        public static func sectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "本地服务健康" : "Daemon Health"
        }

        public static func statusLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "状态" : "Status"
        }

        public static func lastCheckedLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "上次检查" : "Last Checked"
        }

        public static func lastCheckedNever(_ locale: AppLocale) -> String {
            locale == .zhCN ? "尚未检查" : "Never checked"
        }

        public static func nextActionLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "建议操作" : "Next Action"
        }

        public static func connectionLogLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "连接日志" : "Connection Log"
        }

        public static func emptyLog(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无连接记录" : "No connection attempts yet"
        }

        public static func editEndpointButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "编辑地址" : "Edit Endpoint"
        }

        public static func writesDisabledBanner(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "协议版本不兼容，写入操作已禁用。"
                : "Protocol version mismatch — write actions are disabled."
        }

        /// Concrete next-step hint for a failure state. Empty for healthy states.
        public static func nextAction(_ locale: AppLocale, for state: AppState.ConnectionState) -> String {
            switch state {
            case .disconnected:
                return locale == .zhCN
                    ? "请在终端启动 NaumiAgent 守护进程后重试。"
                    : "Start the NaumiAgent daemon in a terminal, then retry."
            case .authFailed:
                return locale == .zhCN
                    ? "请检查或更新 Bearer Token 后重试。"
                    : "Check or update the bearer token, then retry."
            case .protocolMismatch:
                return locale == .zhCN
                    ? "请升级 NaumiAgent 守护进程或本应用到兼容版本。"
                    : "Upgrade the NaumiAgent daemon or this app to a compatible version."
            case .stale:
                return locale == .zhCN
                    ? "连接已失效，正在尝试恢复。"
                    : "The connection is stale; recovering."
            case .connected, .connecting:
                return ""
            }
        }
    }

    // MARK: - Supervised Daemon
    public enum SupervisedDaemon {
        public static func sectionTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "本地服务进程" : "Local Daemon Process"
        }

        public static func startButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "启动本地服务" : "Start Local Daemon"
        }

        public static func stopButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "停止本地服务" : "Stop Local Daemon"
        }

        public static func startingHint(_ locale: AppLocale) -> String {
            locale == .zhCN ? "正在启动…" : "Starting…"
        }

        public static func stoppingHint(_ locale: AppLocale) -> String {
            locale == .zhCN ? "正在停止…" : "Stopping…"
        }

        public static func pidLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "进程 ID" : "PID"
        }

        public static func portLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "端口" : "Port"
        }

        public static func endpointLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "服务地址" : "Endpoint"
        }

        public static func stateLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "状态" : "State"
        }

        public static func logLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "守护进程日志" : "Daemon Log"
        }

        public static func emptyLog(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无日志" : "No log output yet"
        }

        public static func refreshLogButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新日志" : "Refresh Log"
        }

        public static func clearLogButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "清空日志" : "Clear Log"
        }

        public static func failurePrefix(_ locale: AppLocale) -> String {
            locale == .zhCN ? "启动失败：" : "Failed to start: "
        }

        public static func exitedHint(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "守护进程意外退出，请重新启动或查看日志。"
                : "The daemon exited unexpectedly. Restart it or inspect the log."
        }

        public static func shutdownPromptTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "保留本地服务？" : "Keep the local daemon running?"
        }

        public static func shutdownPromptMessage(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "应用即将退出。是否保留正在运行的 NaumiAgent 守护进程？"
                : "The app is quitting. Keep the running NaumiAgent daemon alive?"
        }

        public static func shutdownKeepButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "保留并退出" : "Keep & Quit"
        }

        public static func shutdownStopButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "停止并退出" : "Stop & Quit"
        }

        public static func stateDisplayName(_ locale: AppLocale, for state: SupervisedDaemonState) -> String {
            switch state {
            case .idle:
                return locale == .zhCN ? "未启动" : "Idle"
            case .starting:
                return locale == .zhCN ? "启动中" : "Starting"
            case .running:
                return locale == .zhCN ? "运行中" : "Running"
            case .stopping:
                return locale == .zhCN ? "停止中" : "Stopping"
            case .failed:
                return locale == .zhCN ? "启动失败" : "Failed"
            case .exited:
                return locale == .zhCN ? "已退出" : "Exited"
            }
        }
    }

    // MARK: - Workspace Switcher
    public enum WorkspaceSwitcher {
        public static func workspaceLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作区" : "Workspace"
        }

        public static func sessionLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "会话" : "Session"
        }

        public static func noWorkspace(_ locale: AppLocale) -> String {
            locale == .zhCN ? "无工作区" : "No Workspace"
        }

        public static func noSession(_ locale: AppLocale) -> String {
            locale == .zhCN ? "未选择会话" : "No Session"
        }

        public static func recentSessionsTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "最近会话" : "Recent Sessions"
        }

        public static func knownWorkspacesTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "已知工作区" : "Known Workspaces"
        }

        public static func noRecentSessions(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无最近会话" : "No recent sessions"
        }

        public static func noKnownWorkspaces(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无已知工作区" : "No known workspaces"
        }

        public static func activeSessionTitle(_ locale: AppLocale, _ title: String) -> String {
            locale == .zhCN ? "当前会话：\(title)" : "Active session: \(title)"
        }
    }

    // MARK: - Worktree Validation
    public enum WorktreeValidation {
        public static func missingWorkspaceTitle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "工作区不可用" : "Workspace Unavailable"
        }

        public static func missingWorkspaceMessage(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "未找到有效的工作区路径，无法执行工作区操作。请先连接到本地服务。"
                : "No valid workspace path is available, so worktree operations are blocked. Connect to the local daemon first."
        }
    }

    // MARK: - Snapshot Freshness
    public enum SnapshotFreshness {
        public static func lastRefreshedLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "上次刷新" : "Last refreshed"
        }

        public static func neverRefreshed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "尚未刷新" : "Never refreshed"
        }

        public static func agoSuffix(_ locale: AppLocale) -> String {
            locale == .zhCN ? "前" : "ago"
        }

        public static func staleHint(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "数据可能已过期，已保留上次的内容。"
                : "Data may be stale; the last good snapshot is still shown."
        }

        public static func failureKeptOldDataHint(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "刷新失败，已保留上次的数据。"
                : "Refresh failed; the previous data is still shown."
        }

        public static func refreshButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "刷新快照" : "Refresh Snapshot"
        }
    }

    // MARK: - EventStreamStatus
    public enum EventStreamStatus {
        public static func statusIdle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "事件流未启动" : "Event stream idle"
        }

        public static func statusConnecting(_ locale: AppLocale) -> String {
            locale == .zhCN ? "正在连接事件流" : "Connecting event stream"
        }

        public static func statusConnected(_ locale: AppLocale) -> String {
            locale == .zhCN ? "事件流已连接" : "Event stream live"
        }

        public static func statusReconnecting(_ locale: AppLocale, attempt: Int, max: Int) -> String {
            locale == .zhCN
                ? "正在重连事件流（第 \(attempt)/\(max) 次）"
                : "Reconnecting event stream (attempt \(attempt)/\(max))"
        }

        public static func statusStale(_ locale: AppLocale) -> String {
            locale == .zhCN ? "事件流已断开" : "Event stream disconnected"
        }

        public static func statusStoppedBySessionSwitch(_ locale: AppLocale) -> String {
            locale == .zhCN ? "事件流已随会话切换停止" : "Event stream stopped on session switch"
        }

        public static func statusStoppedByAuthOrProtocol(_ locale: AppLocale) -> String {
            locale == .zhCN ? "事件流因鉴权或协议不匹配停止" : "Event stream stopped on auth/protocol error"
        }

        public static func liveLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "实时" : "Live"
        }

        public static func staleLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "延迟" : "Lag"
        }

        public static func lastConnectedLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "上次连接" : "Last connected"
        }

        public static func reconnectButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "重新连接事件流" : "Reconnect Event Stream"
        }

        public static func reconnectHelp(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "自动重连已达上限，请手动重连。"
                : "Automatic reconnect attempts are exhausted. Reconnect manually."
        }

        public static func stoppedByAuthHelp(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "鉴权失败或协议版本不匹配，请重新配置连接后再重连。"
                : "Auth failed or protocol version mismatch. Reconfigure the connection, then reconnect."
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

        public static func noMission(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无 Mission" : "No Mission"
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

        public static func agentStatusIdle(_ locale: AppLocale) -> String {
            locale == .zhCN ? "空闲" : "Idle"
        }

        public static func agentStatusBusy(_ locale: AppLocale) -> String {
            locale == .zhCN ? "忙碌" : "Busy"
        }

        public static func agentStatusStale(_ locale: AppLocale) -> String {
            locale == .zhCN ? "过期" : "Stale"
        }

        public static func agentStatusOffline(_ locale: AppLocale) -> String {
            locale == .zhCN ? "离线" : "Offline"
        }

        public static func lastHeartbeatLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "上次心跳" : "Last Heartbeat"
        }

        public static func currentIssueLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "当前任务" : "Current Issue"
        }

        public static func currentLeaseLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "当前租约" : "Current Lease"
        }

        public static func permissionsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "权限" : "Permissions"
        }

        public static func noHeartbeat(_ locale: AppLocale) -> String {
            locale == .zhCN ? "无" : "None"
        }

        public static func permissionRiskWarning(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "警告：该智能体拥有高风险权限（写/删/执行/管理员）。"
                : "Warning: this agent has high-risk permissions (write/delete/execute/admin)."
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

        public static func emptyLeases(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无活跃租约" : "No active leases"
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

        public static func bidCountTitle(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN ? "智能体竞标 (\(count))" : "Agent Bids (\(count))"
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

        public static func riskLevelLabel(_ risk: String, _ locale: AppLocale) -> String {
            switch risk.lowercased() {
            case "critical":
                return locale == .zhCN ? "严重" : "Critical"
            case "high":
                return locale == .zhCN ? "高" : "High"
            case "medium":
                return locale == .zhCN ? "中" : "Medium"
            case "low":
                return locale == .zhCN ? "低" : "Low"
            default:
                return risk
            }
        }

        public static func requiresProposalLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "需要提案" : "Requires proposal"
        }

        public static func leasedLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "已租用" : "Leased"
        }

        public static func blockedLabel(_ locale: AppLocale, count: Int) -> String {
            locale == .zhCN
                ? "被 \(count) 个任务阻塞"
                : "Blocked by \(count) task\(count == 1 ? "" : "s")"
        }

        public static func leaseExpiresLabel(_ locale: AppLocale, expiry: String) -> String {
            locale == .zhCN ? "过期于 \(expiry)" : "Expires \(expiry)"
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

        public static func emptyWorktrees(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无 Git 工作区" : "No Git worktrees"
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

        public static func emptyApprovalHint(_ locale: AppLocale) -> String {
            locale == .zhCN ? "Agent 完成高风险任务后会在此请求审批" : "Agents request approval here after high-risk work"
        }

        public static func emptyBids(_ locale: AppLocale) -> String {
            locale == .zhCN ? "还没有 Agent 投标记录" : "No agent bids yet"
        }

        public static func emptyBidsHint(_ locale: AppLocale) -> String {
            locale == .zhCN ? "Agent 认领任务后会显示竞标详情" : "Bid details appear after an agent claims a task"
        }

        public static func selectApprovalPrompt(_ locale: AppLocale) -> String {
            locale == .zhCN ? "选择左侧审批项以查看详情" : "Select an approval on the left to review details"
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

        public static func sinceLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "起始时间" : "Since"
        }

        public static func severityLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "严重级别" : "Severity"
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

        public static func authFailed(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "本地 daemon 认证失败，请检查访问令牌或重新连接"
                : "Local daemon authentication failed. Check the access token or reconnect."
        }

        public static func sessionUnavailable(_ locale: AppLocale) -> String {
            locale == .zhCN
                ? "当前会话不可用，请刷新或选择另一个会话"
                : "The current session is unavailable. Refresh or select another session."
        }

        public static func missingSelectedSession(_ locale: AppLocale) -> String {
            locale == .zhCN ? "请先选择一个会话" : "Select a session first"
        }

        public static func httpStatus(_ locale: AppLocale, code: Int) -> String {
            locale == .zhCN
                ? "HTTP 错误 \(code)"
                : "HTTP error \(code)"
        }

        public static func serverError(_ locale: AppLocale, statusCode: Int, detail: String) -> String {
            locale == .zhCN
                ? "本地服务返回错误 \(statusCode)：\(detail)"
                : "Local service returned error \(statusCode): \(detail)"
        }

        public static func decodingFailed(_ locale: AppLocale) -> String {
            locale == .zhCN ? "数据解析失败" : "Failed to decode response"
        }

        public static func networkFailure(_ locale: AppLocale) -> String {
            if locale == .zhCN {
                return "无法连接本地 NaumiAgent 服务，请先启动：\nnaumi serve --host 127.0.0.1 --port 8765"
            }
            return "Cannot reach the local NaumiAgent service. Start it with:\nnaumi serve --host 127.0.0.1 --port 8765"
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

        public static func deactivateIntentLockButton(_ locale: AppLocale) -> String {
            locale == .zhCN ? "停用意图锁" : "Deactivate Intent Lock"
        }

        public static func intentLockCreatedByLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "创建者" : "Created By"
        }

        public static func intentLockUpdatedAtLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "更新时间" : "Updated At"
        }

        public static func intentLockStatusActive(_ locale: AppLocale) -> String {
            locale == .zhCN ? "生效中" : "Active"
        }

        public static func intentLockStatusInactive(_ locale: AppLocale) -> String {
            locale == .zhCN ? "已停用" : "Inactive"
        }

        public static func decisionStrengthLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "决策强度" : "Decision Strength"
        }

        public static func policyHitHistorySection(_ locale: AppLocale) -> String {
            locale == .zhCN ? "策略命中记录" : "Policy Hit History"
        }

        public static func policyHitEmpty(_ locale: AppLocale) -> String {
            locale == .zhCN ? "暂无策略命中记录" : "No policy hits yet"
        }

        public static func policyHitReasonLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "命中原因" : "Reason"
        }

        public static func policyHitChangedPathsLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "受影响路径" : "Changed Paths"
        }

        public static func policyHitBlockedActionLabel(_ locale: AppLocale) -> String {
            locale == .zhCN ? "被阻塞动作" : "Blocked Action"
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

    // MARK: - Decision Strength
    public enum DecisionStrength {
        public static func advisory(_ locale: AppLocale) -> String {
            locale == .zhCN ? "建议" : "Advisory"
        }

        public static func required(_ locale: AppLocale) -> String {
            locale == .zhCN ? "必须遵守" : "Required"
        }

        public static func blocking(_ locale: AppLocale) -> String {
            locale == .zhCN ? "阻断" : "Blocking"
        }

        public static func label(_ locale: AppLocale, for strength: String) -> String {
            switch strength.lowercased() {
            case "blocking":
                return blocking(locale)
            case "advisory":
                return advisory(locale)
            default:
                return required(locale)
            }
        }
    }

    // MARK: - Policy Hit
    public enum PolicyHit {
        public static func blockedPathCountLabel(_ locale: AppLocale, count: Int) -> String {
            if locale == .zhCN {
                return count == 1 ? "\(count) 条路径" : "\(count) 条路径"
            }
            return count == 1 ? "\(count) path" : "\(count) paths"
        }
    }
}
