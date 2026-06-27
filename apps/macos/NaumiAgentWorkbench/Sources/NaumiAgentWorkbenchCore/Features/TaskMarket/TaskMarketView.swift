import SwiftUI

/// Task Market visual prototype aligned with the Mac workbench design reference.
public struct TaskMarketView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    @State private var selectedTaskID: String?
    @State private var searchText = ""
    @State private var autoRefresh = true
    @State private var claimAgentID = "Backend-Agent"
    @State private var claimDurationMinutes = 45
    @State private var claimWorktreeName = ""
    @State private var isClaimingIssue = false
    @State private var isPresentingIssueComposer = false
    @State private var issueDraft = IssueCreationDraft()
    @State private var isCreatingIssue = false
    @State private var isPresentingIssueAttachment = false
    @State private var attachmentDraft = IssueAttachmentDraft()
    @State private var isAttachingIssue = false

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = TaskMarketDesignPresentation(snapshot: appState.snapshot)
        let selected = presentation.rows.first { $0.taskID == selectedTaskID }
            ?? presentation.selectedIssue

        VStack(spacing: 0) {
            pageHeader(selected: selected)
            Divider()

            HStack(spacing: 0) {
                filterRail(presentation: presentation)
                    .frame(width: 240, alignment: .leading)
                    .frame(maxHeight: .infinity)
                    .clipped()

                Divider()

                VStack(spacing: 0) {
                    marketTable(presentation: presentation)
                    Divider()
                    activeLeasesStrip(presentation.activeLeases)
                        .frame(height: 124)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                Divider()

                issueInspector(issue: selected, bids: presentation.bids)
                    .frame(width: 320, alignment: .leading)
                    .frame(maxHeight: .infinity)
                    .clipped()
            }

            Divider()
            footer
        }
        .frame(minWidth: 1120, minHeight: 700)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            if selectedTaskID == nil {
                selectedTaskID = presentation.selectedIssue?.taskID
            }
            if claimWorktreeName.isEmpty, let selected {
                claimWorktreeName = selected.defaultClaimWorktreeName
            }
            if issueDraft.trimmedMissionID.isEmpty {
                issueDraft.missionID = currentMissionID
            }
            if attachmentDraft.trimmedMissionID.isEmpty {
                attachmentDraft.missionID = currentMissionID
            }
        }
        .onChange(of: currentMissionID) { _, missionID in
            if issueDraft.trimmedMissionID.isEmpty {
                issueDraft.missionID = missionID
            }
            if attachmentDraft.trimmedMissionID.isEmpty {
                attachmentDraft.missionID = missionID
            }
        }
        .sheet(isPresented: $isPresentingIssueComposer) {
            IssueCreationSheet(
                appState: appState,
                daemonController: daemonController,
                draft: $issueDraft,
                isCreatingIssue: $isCreatingIssue,
                onCreated: {
                    issueDraft = issueDraftForCurrentMission()
                }
            )
        }
        .sheet(isPresented: $isPresentingIssueAttachment) {
            IssueAttachmentSheet(
                appState: appState,
                daemonController: daemonController,
                draft: $attachmentDraft,
                isAttachingIssue: $isAttachingIssue,
                onAttached: {
                    attachmentDraft = attachmentDraftForCurrentMission(selected: selected)
                }
            )
        }
    }

    private func pageHeader(selected: TaskMarketDesignIssue?) -> some View {
        HStack(spacing: 12) {
            Text(AppStrings.TaskMarket.title(appState.locale))
                .font(.system(size: 17, weight: .semibold))
            Text(appState.locale == .zhCN ? "认领 / 竞标 / 租约" : "Claim / Bid / Lease")
                .font(.caption)
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                issueDraft = issueDraftForCurrentMission()
                isPresentingIssueComposer = true
            } label: {
                Label(
                    AppStrings.TaskMarket.createIssueButton(appState.locale),
                    systemImage: "plus.circle"
                )
            }
            .buttonStyle(.borderedProminent)
            .disabled(currentMissionID.isEmpty)

            Button {
                attachmentDraft = attachmentDraftForCurrentMission(selected: selected)
                isPresentingIssueAttachment = true
            } label: {
                Label(
                    AppStrings.TaskMarket.attachIssueButton(appState.locale),
                    systemImage: "link.badge.plus"
                )
            }
            .buttonStyle(.bordered)
            .disabled(currentMissionID.isEmpty)

            Button {
                autoRefresh.toggle()
            } label: {
                Label(
                    autoRefresh
                        ? (appState.locale == .zhCN ? "暂停智能体" : "Pause Agents")
                        : (appState.locale == .zhCN ? "恢复智能体" : "Resume Agents"),
                    systemImage: autoRefresh ? "pause.fill" : "play.fill"
                )
            }
            .buttonStyle(.bordered)

            Menu(appState.locale == .zhCN ? "目标" : "Mission") {
                Button(currentMissionTitle) {}
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 11)
    }

    private func filterRail(presentation: TaskMarketDesignPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(appState.locale == .zhCN ? "目标" : "MISSION")
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                Text(currentMissionTitle)
                    .font(.caption)
                    .fontWeight(.medium)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 6)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.10))
            .clipShape(RoundedRectangle(cornerRadius: 6))

            HStack(spacing: 6) {
                Circle()
                    .fill(.green)
                    .frame(width: 7, height: 7)
                Text(appState.locale == .zhCN ? "进行中" : "In Progress")
                    .font(.caption)
            }

            Divider()

            filterGroup(
                title: appState.locale == .zhCN ? "风险等级" : "Risk Level",
                filters: presentation.filters.riskLevels
            )
            filterGroup(
                title: appState.locale == .zhCN ? "并行模式" : "Parallel Mode",
                filters: presentation.filters.parallelModes
            )
            filterGroup(
                title: appState.locale == .zhCN ? "依赖状态" : "Dependency Status",
                filters: presentation.filters.dependencyStates
            )
            filterGroup(
                title: appState.locale == .zhCN ? "上下文健康" : "Context Health",
                filters: presentation.filters.contextHealth
            )

            Spacer()

            Button {
            } label: {
                Label(appState.locale == .zhCN ? "保存筛选视图..." : "Save Filter View...", systemImage: "square.and.arrow.down")
            }
            .buttonStyle(.bordered)
            .frame(maxWidth: .infinity)
        }
        .padding(14)
        .frame(width: 240, alignment: .leading)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func filterGroup(title: String, filters: [TaskMarketDesignFilter]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(title)
                    .font(.caption)
                    .fontWeight(.semibold)
                Spacer()
                if title.contains("Risk") || title.contains("风险") {
                    Text(appState.locale == .zhCN ? "重置" : "Reset")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            ForEach(filters, id: \.label) { filter in
                HStack(spacing: 7) {
                    Image(systemName: "square")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                    Circle()
                        .fill(color(forTone: filter.tone))
                        .frame(width: 6, height: 6)
                    Text(filter.label)
                        .font(.caption)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text("\(filter.count)")
                        .font(.caption2)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 5))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func marketTable(presentation: TaskMarketDesignPresentation) -> some View {
        VStack(spacing: 0) {
            HStack {
                TextField(appState.locale == .zhCN ? "搜索 issue..." : "Search issues...", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 270)
                Button {
                } label: {
                    Image(systemName: "line.3.horizontal.decrease.circle")
                }
                .help(appState.locale == .zhCN ? "筛选" : "Filter")

                Spacer()
                Text(appState.locale == .zhCN
                    ? "\(presentation.rows.count) 个 issue"
                    : "\(presentation.rows.count) issues"
                )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button(appState.locale == .zhCN ? "列" : "Columns") {}
                    .buttonStyle(.bordered)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)

            tableHeader
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .background(Color.secondary.opacity(0.06))

            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(presentation.rows) { row in
                        designIssueRow(row)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                selectedTaskID = row.taskID
                                claimWorktreeName = row.defaultClaimWorktreeName
                            }
                            .background(selectedTaskID == row.taskID ? Color.accentColor.opacity(0.10) : Color.clear)
                        Divider()
                    }
                }
            }
        }
    }

    private var tableHeader: some View {
        HStack(spacing: 8) {
            Text("#").frame(width: 22, alignment: .leading)
            Text(AppStrings.TaskMarket.columnIssue(appState.locale)).frame(minWidth: 170, maxWidth: .infinity, alignment: .leading)
            Text(AppStrings.TaskMarket.columnParallelMode(appState.locale)).frame(width: 82, alignment: .leading)
            Text(AppStrings.TaskMarket.columnRisk(appState.locale)).frame(width: 62, alignment: .leading)
            Text(AppStrings.TaskMarket.columnDependencies(appState.locale)).frame(width: 76, alignment: .leading)
            Text(AppStrings.TaskMarket.columnBids(appState.locale)).frame(width: 44, alignment: .leading)
            Text(AppStrings.TaskMarket.columnLease(appState.locale)).frame(width: 82, alignment: .leading)
            Text(AppStrings.TaskMarket.columnWorktree(appState.locale)).frame(width: 95, alignment: .leading)
            Text(AppStrings.TaskMarket.columnStatus(appState.locale)).frame(width: 90, alignment: .leading)
        }
        .font(.caption)
        .fontWeight(.semibold)
        .foregroundStyle(.secondary)
    }

    private func designIssueRow(_ row: TaskMarketDesignIssue) -> some View {
        HStack(spacing: 8) {
            Text("\(row.number)")
                .font(.system(size: 13, weight: .medium))
                .frame(width: 22, alignment: .leading)

            VStack(alignment: .leading, spacing: 4) {
                Text(row.title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Text(row.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Text(row.tag)
                    .font(.caption2)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.secondary.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
            }
            .frame(minWidth: 170, maxWidth: .infinity, alignment: .leading)

            modeBadge(row.parallelMode)
                .frame(width: 82, alignment: .leading)
            riskBadge(row.risk)
                .frame(width: 62, alignment: .leading)
            Text(row.dependency)
                .font(.caption)
                .foregroundStyle(row.dependency.contains("Blocked") ? .red : .secondary)
                .lineLimit(1)
                .frame(width: 76, alignment: .leading)
            Text("\(row.bids)")
                .font(.system(size: 13, weight: .semibold))
                .frame(width: 44, alignment: .leading)
            Text(row.lease)
                .font(.caption)
                .foregroundStyle(row.lease.contains("remaining") ? .green : .primary)
                .lineLimit(1)
                .frame(width: 82, alignment: .leading)
            Text(row.worktree)
                .font(.caption)
                .foregroundStyle(row.worktree == "-" ? .secondary : .primary)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 95, alignment: .leading)
            Text(row.status)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(statusColor(row.status))
                .lineLimit(1)
                .frame(width: 90, alignment: .leading)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 10)
    }

    private func issueInspector(issue: TaskMarketDesignIssue?, bids: [TaskMarketDesignBid]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(issue.map { "Issue #\($0.number)" } ?? "Issue")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Image(systemName: "xmark")
                    .foregroundStyle(.secondary)
            }

            if let issue {
                Text(issue.title)
                    .font(.system(size: 17, weight: .semibold))
                HStack {
                    riskBadge(issue.risk)
                    modeBadge(issue.parallelMode)
                    Text(appState.locale == .zhCN ? "需要方案" : "Requires proposal")
                        .font(.caption)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 5))
                }

                Text(issue.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)

                inspectorGrid(issue)

                Divider()

                claimCommandPanel(issue)

                Divider()

                HStack {
                    Text(appState.locale == .zhCN ? "智能体竞标 (3)" : "Agent Bids (3)")
                        .font(.headline)
                    Spacer()
                    Menu(appState.locale == .zhCN ? "置信度" : "Confidence") {
                        Button(appState.locale == .zhCN ? "置信度" : "Confidence") {}
                    }
                }

                ScrollView {
                    VStack(spacing: 10) {
                        ForEach(bids) { bid in
                            bidCard(bid)
                        }
                    }
                }

                Button {
                } label: {
                    Label(appState.locale == .zhCN ? "邀请更多智能体" : "Invite More Agents", systemImage: "person.2.badge.plus")
                }
                .buttonStyle(.bordered)
                .frame(maxWidth: .infinity)
            }

            Spacer(minLength: 0)
        }
        .padding(16)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func claimCommandPanel(_ issue: TaskMarketDesignIssue) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(AppStrings.TaskMarket.commandSectionTitle(appState.locale))
                .font(.headline)

            TextField(AppStrings.TaskMarket.agentIDLabel(appState.locale), text: $claimAgentID)
                .textFieldStyle(.roundedBorder)

            Stepper(
                "\(AppStrings.TaskMarket.durationLabel(appState.locale)): \(claimDurationMinutes)",
                value: $claimDurationMinutes,
                in: 1...240,
                step: 5
            )
            .font(.caption)

            TextField(
                AppStrings.TaskMarket.columnWorktree(appState.locale),
                text: $claimWorktreeName
            )
            .textFieldStyle(.roundedBorder)

            Button {
                claimIssue(issue)
            } label: {
                Label(
                    isClaimingIssue
                        ? AppStrings.TaskMarket.processingLabel(appState.locale)
                        : AppStrings.TaskMarket.claimButton(appState.locale),
                    systemImage: "hand.raised"
                )
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!issue.canClaim || isClaimingIssue)

            if let reason = issue.claimDisabledReason(locale: appState.locale) {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func claimIssue(_ issue: TaskMarketDesignIssue) {
        guard issue.canClaim, !isClaimingIssue else { return }
        let agentID = claimAgentID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !agentID.isEmpty else {
            appState.lastError = .networkFailure(
                appState.locale == .zhCN ? "代理 ID 不能为空" : "Agent ID is required"
            )
            return
        }
        let worktreeName = claimWorktreeName.trimmingCharacters(in: .whitespacesAndNewlines)
        isClaimingIssue = true
        Task {
            await daemonController.claimIssue(
                taskID: issue.taskID,
                agentID: agentID,
                durationMinutes: claimDurationMinutes,
                worktreeName: worktreeName.isEmpty ? issue.defaultClaimWorktreeName : worktreeName
            )
            isClaimingIssue = false
        }
    }

    private func inspectorGrid(_ issue: TaskMarketDesignIssue) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 7) {
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "目标" : "Mission")
                Text(currentMissionTitle)
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "创建时间" : "Created")
                Text(appState.locale == .zhCN ? "2026-06-27 09:14" : "Jun 27, 2026 09:14")
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "上下文健康" : "Context Health")
                StatusBadge(text: appState.locale == .zhCN ? "健康" : "Good", color: .green)
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "测试数" : "Tests")
                Text("8")
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "风险摘要" : "Risk Summary")
                Text(issue.risk == "High"
                    ? (appState.locale == .zhCN ? "并发缺陷、租约丢失" : "Concurrency bugs, lost leases")
                    : (appState.locale == .zhCN ? "影响面较低" : "Low blast radius")
                )
            }
        }
        .font(.caption)
    }

    private func inspectorLabel(_ text: String) -> some View {
        Text(text)
            .foregroundStyle(.secondary)
    }

    private func bidCard(_ bid: TaskMarketDesignBid) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Image(systemName: "person.crop.square")
                    .foregroundStyle(.purple)
                Text(bid.agent)
                    .font(.system(size: 14, weight: .semibold))
                if bid.isLatest {
                    StatusBadge(text: appState.locale == .zhCN ? "最新" : "Latest", color: .blue)
                }
                Spacer()
                Text(appState.locale == .zhCN ? "置信度" : "confidence")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(bid.confidence)
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(.green)
            }

            HStack {
                inspectorLabel(appState.locale == .zhCN ? "预计文件：" : "Est. Files:")
                Text(bid.estimate)
                inspectorLabel(appState.locale == .zhCN ? "预计耗时：" : "ETA:")
                Text(bid.eta)
            }
            .font(.caption)

            Text(bid.note)
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack {
                Button(appState.locale == .zhCN ? "分配" : "Assign") {}
                    .buttonStyle(.borderedProminent)
                Button(appState.locale == .zhCN ? "请求方案" : "Request Proposal") {}
                    .buttonStyle(.bordered)
                Button(appState.locale == .zhCN ? "拒绝竞标" : "Reject Bid") {}
                    .buttonStyle(.bordered)
                    .foregroundStyle(.red)
            }
            .font(.caption)
            .controlSize(.small)
        }
        .padding(12)
        .background(Color(nsColor: .controlBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.purple.opacity(0.35), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func activeLeasesStrip(_ leases: [TaskMarketDesignLease]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(appState.locale == .zhCN ? "活跃租约 (4)" : "Active Leases (4)")
                    .font(.headline)
                Button(appState.locale == .zhCN ? "查看全部租约" : "View All Leases") {}
                    .buttonStyle(.bordered)
                Spacer()
                Image(systemName: "xmark")
                    .foregroundStyle(.secondary)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(leases) { lease in
                        leaseCard(lease)
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func leaseCard(_ lease: TaskMarketDesignLease) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("#\(lease.number) \(lease.title)")
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                Spacer()
            }
            Text(lease.worktree)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(appState.locale == .zhCN ? "负责人：\(lease.owner)" : "Owner: \(lease.owner)")
                .font(.caption2)
            Text(appState.locale == .zhCN ? "租约到期：\(lease.time)" : "Lease Expires: \(lease.time)")
                .font(.caption2)
                .foregroundStyle(color(forTone: lease.tone))
            HStack {
                Circle()
                    .fill(color(forTone: lease.tone))
                    .frame(width: 6, height: 6)
                Text(lease.status)
                    .font(.caption2)
                Spacer()
                Button(lease.tone == "red" ? "Reclaim" : "Open Worktree") {}
                    .font(.caption2)
            }
        }
        .padding(9)
        .frame(width: 230)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .stroke(color(forTone: lease.tone).opacity(0.55), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }

    private var footer: some View {
        HStack {
            Circle()
                .fill(.green)
                .frame(width: 8, height: 8)
            Text(appState.locale == .zhCN ? "已连接本地 NaumiAgent Runtime" : "Connected to local NaumiAgent Runtime")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Toggle(appState.locale == .zhCN ? "自动刷新" : "Auto-refresh", isOn: $autoRefresh)
                .toggleStyle(.switch)
                .font(.caption)
            Text(appState.locale == .zhCN ? "工作区：~/naumi" : "Workspace: ~/naumi")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("v0.3.0")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var currentMissionTitle: String {
        appState.snapshot?.missions.first?.title
            ?? (appState.locale == .zhCN ? "Mac Agent Workbench MVP" : "Mac Agent Workbench MVP")
    }

    private var currentMissionID: String {
        appState.snapshot?.missions.first?.id
            ?? appState.missions.first?.id
            ?? ""
    }

    private func issueDraftForCurrentMission() -> IssueCreationDraft {
        IssueCreationDraft(missionID: currentMissionID)
    }

    private func attachmentDraftForCurrentMission(selected: TaskMarketDesignIssue?) -> IssueAttachmentDraft {
        IssueAttachmentDraft(
            missionID: currentMissionID,
            taskID: selected?.taskID ?? "",
            parallelMode: selected?.parallelMode.lowercased() ?? "exclusive",
            riskLevel: selected?.risk.lowercased() ?? "medium"
        )
    }

    private func modeBadge(_ mode: String) -> some View {
        HStack(spacing: 4) {
            Text(String(mode.prefix(1)).uppercased())
                .font(.caption2)
                .fontWeight(.bold)
                .frame(width: 16, height: 16)
                .background(color(forMode: mode).opacity(0.14))
                .foregroundStyle(color(forMode: mode))
                .clipShape(RoundedRectangle(cornerRadius: 4))
            Text(mode)
                .font(.caption)
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }

    private func riskBadge(_ risk: String) -> some View {
        Text(risk)
            .font(.caption)
            .fontWeight(.medium)
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background(color(forRisk: risk).opacity(0.12))
            .foregroundStyle(color(forRisk: risk))
            .clipShape(RoundedRectangle(cornerRadius: 5))
    }

    private func color(forRisk risk: String) -> Color {
        switch risk.lowercased() {
        case "critical":
            return .red
        case "high":
            return .orange
        case "medium":
            return .yellow
        case "low":
            return .green
        default:
            return .secondary
        }
    }

    private func color(forMode mode: String) -> Color {
        switch mode.lowercased() {
        case "exclusive":
            return .blue
        case "competitive":
            return .orange
        case "exploratory":
            return .purple
        default:
            return .secondary
        }
    }

    private func statusColor(_ status: String) -> Color {
        switch status.lowercased() {
        case "leased":
            return .green
        case "blocked":
            return .red
        case "requires proposal":
            return .blue
        default:
            return .primary
        }
    }

    private func color(forTone tone: String) -> Color {
        switch tone {
        case "red":
            return .red
        case "orange":
            return .orange
        case "yellow":
            return .yellow
        case "green":
            return .green
        case "blue":
            return .blue
        case "purple":
            return .purple
        default:
            return .secondary
        }
    }
}

private struct IssueCreationSheet: View {
    let appState: AppState
    let daemonController: DaemonController
    @Binding var draft: IssueCreationDraft
    @Binding var isCreatingIssue: Bool
    let onCreated: () -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(AppStrings.TaskMarket.createIssueSectionTitle(appState.locale))
                .font(.headline)

            Form {
                TextField(AppStrings.TaskMarket.missionIDLabel(appState.locale), text: $draft.missionID)

                TextField(
                    AppStrings.TaskMarket.issueTitleLabel(appState.locale),
                    text: $draft.title
                )

                VStack(alignment: .leading, spacing: 6) {
                    Text(AppStrings.TaskMarket.issueDescriptionLabel(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextEditor(text: $draft.description)
                        .frame(minHeight: 74)
                        .overlay(
                            RoundedRectangle(cornerRadius: 5)
                                .stroke(Color.secondary.opacity(0.25), lineWidth: 1)
                        )
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text(AppStrings.TaskMarket.blockedByLabel(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextEditor(text: $draft.blockedByText)
                        .frame(minHeight: 46)
                        .overlay(
                            RoundedRectangle(cornerRadius: 5)
                                .stroke(Color.secondary.opacity(0.25), lineWidth: 1)
                        )
                    Text(AppStrings.TaskMarket.blockedByHelp(appState.locale))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text(AppStrings.TaskMarket.acceptanceCriteriaTitle(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextEditor(text: $draft.acceptanceCriteriaText)
                        .frame(minHeight: 58)
                        .overlay(
                            RoundedRectangle(cornerRadius: 5)
                                .stroke(Color.secondary.opacity(0.25), lineWidth: 1)
                        )
                    Text(AppStrings.TaskMarket.acceptanceCriteriaHelp(appState.locale))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                Picker(AppStrings.TaskMarket.columnParallelMode(appState.locale), selection: $draft.parallelMode) {
                    Text("exclusive").tag("exclusive")
                    Text("cooperative").tag("cooperative")
                    Text("competitive").tag("competitive")
                    Text("exploratory").tag("exploratory")
                }

                Picker(AppStrings.TaskMarket.columnRisk(appState.locale), selection: $draft.riskLevel) {
                    Text("low").tag("low")
                    Text("medium").tag("medium")
                    Text("high").tag("high")
                    Text("critical").tag("critical")
                }
            }
            .frame(minWidth: 420)

            HStack {
                Spacer()

                Button(AppStrings.MissionComposer.cancelButton(appState.locale)) {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)

                Button {
                    createIssue()
                } label: {
                    Label(
                        isCreatingIssue
                            ? AppStrings.TaskMarket.processingLabel(appState.locale)
                            : AppStrings.TaskMarket.createIssueSubmitButton(appState.locale),
                        systemImage: "plus.circle"
                    )
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(!draft.canSubmit || isCreatingIssue)
            }
        }
        .padding()
        .frame(minWidth: 460, minHeight: 520)
    }

    private func createIssue() {
        guard draft.canSubmit, !isCreatingIssue else { return }
        let submission = draft
        isCreatingIssue = true
        Task {
            await daemonController.createIssue(
                missionID: submission.trimmedMissionID,
                title: submission.trimmedTitle,
                description: submission.trimmedDescription,
                blockedBy: submission.blockedBy,
                acceptanceCriteria: submission.acceptanceCriteria,
                parallelMode: submission.parallelMode,
                riskLevel: submission.riskLevel
            )
            isCreatingIssue = false
            if appState.lastError == nil {
                onCreated()
                dismiss()
            }
        }
    }
}

private struct IssueAttachmentSheet: View {
    let appState: AppState
    let daemonController: DaemonController
    @Binding var draft: IssueAttachmentDraft
    @Binding var isAttachingIssue: Bool
    let onAttached: () -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(AppStrings.TaskMarket.attachIssueSectionTitle(appState.locale))
                .font(.headline)

            Form {
                TextField(AppStrings.TaskMarket.missionIDLabel(appState.locale), text: $draft.missionID)
                TextField(AppStrings.Reviews.taskIDLabel(appState.locale), text: $draft.taskID)

                VStack(alignment: .leading, spacing: 6) {
                    Text(AppStrings.TaskMarket.acceptanceCriteriaTitle(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextEditor(text: $draft.acceptanceCriteriaText)
                        .frame(minHeight: 72)
                        .overlay(
                            RoundedRectangle(cornerRadius: 5)
                                .stroke(Color.secondary.opacity(0.25), lineWidth: 1)
                        )
                    Text(AppStrings.TaskMarket.acceptanceCriteriaHelp(appState.locale))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                Picker(AppStrings.TaskMarket.columnParallelMode(appState.locale), selection: $draft.parallelMode) {
                    Text("exclusive").tag("exclusive")
                    Text("cooperative").tag("cooperative")
                    Text("competitive").tag("competitive")
                    Text("exploratory").tag("exploratory")
                }

                Picker(AppStrings.TaskMarket.columnRisk(appState.locale), selection: $draft.riskLevel) {
                    Text("low").tag("low")
                    Text("medium").tag("medium")
                    Text("high").tag("high")
                    Text("critical").tag("critical")
                }
            }
            .frame(minWidth: 420)

            HStack {
                Spacer()

                Button(AppStrings.MissionComposer.cancelButton(appState.locale)) {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)

                Button {
                    attachIssue()
                } label: {
                    Label(
                        isAttachingIssue
                            ? AppStrings.TaskMarket.processingLabel(appState.locale)
                            : AppStrings.TaskMarket.attachIssueSubmitButton(appState.locale),
                        systemImage: "link.badge.plus"
                    )
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(!draft.canSubmit || isAttachingIssue)
            }
        }
        .padding()
        .frame(minWidth: 460, minHeight: 360)
    }

    private func attachIssue() {
        guard draft.canSubmit, !isAttachingIssue else { return }
        let submission = draft
        isAttachingIssue = true
        Task {
            await daemonController.attachIssue(
                missionID: submission.trimmedMissionID,
                taskID: submission.trimmedTaskID,
                acceptanceCriteria: submission.acceptanceCriteria,
                parallelMode: submission.parallelMode,
                riskLevel: submission.riskLevel
            )
            isAttachingIssue = false
            if appState.lastError == nil {
                onAttached()
                dismiss()
            }
        }
    }
}
