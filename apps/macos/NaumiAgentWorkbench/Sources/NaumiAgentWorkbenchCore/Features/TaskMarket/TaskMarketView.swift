import SwiftUI

/// Task Market visual prototype aligned with the Mac workbench design reference.
public struct TaskMarketView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    @State private var selectedTaskID: String?
    @State private var searchText = ""
    @State private var autoRefresh = true

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = TaskMarketDesignPresentation(snapshot: appState.snapshot)
        let selected = presentation.rows.first { $0.taskID == selectedTaskID }
            ?? presentation.selectedIssue

        VStack(spacing: 0) {
            pageHeader
            Divider()

            HStack(spacing: 0) {
                filterRail(presentation: presentation)
                    .frame(width: 248)

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
                    .frame(width: 386)
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
        }
    }

    private var pageHeader: some View {
        HStack(spacing: 12) {
            Text(AppStrings.TaskMarket.title(appState.locale))
                .font(.system(size: 17, weight: .semibold))
            Text(appState.locale == .zhCN ? "认领 / 竞标 / 租约" : "Claim / Bid / Lease")
                .font(.caption)
                .foregroundStyle(.secondary)

            Spacer()

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

            Menu(appState.locale == .zhCN ? "Mission" : "Mission") {
                Button("Mac Agent Workbench MVP") {}
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 11)
    }

    private func filterRail(presentation: TaskMarketDesignPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(appState.locale == .zhCN ? "MISSION" : "MISSION")
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(.secondary)

            Picker("", selection: .constant("Mac Agent Workbench MVP")) {
                Text("Mac Agent Workbench MVP").tag("Mac Agent Workbench MVP")
            }
            .labelsHidden()

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
                    Spacer()
                    Text("\(filter.count)")
                        .font(.caption2)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 5))
                }
            }
        }
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
                Text("\(presentation.rows.count + 7) issues")
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
                            }
                            .background(selectedTaskID == row.taskID ? Color.accentColor.opacity(0.10) : Color.clear)
                        Divider()
                    }
                }
            }
        }
    }

    private var tableHeader: some View {
        HStack(spacing: 10) {
            Text("#").frame(width: 26, alignment: .leading)
            Text(AppStrings.TaskMarket.columnIssue(appState.locale)).frame(minWidth: 190, maxWidth: .infinity, alignment: .leading)
            Text(AppStrings.TaskMarket.columnParallelMode(appState.locale)).frame(width: 105, alignment: .leading)
            Text(AppStrings.TaskMarket.columnRisk(appState.locale)).frame(width: 76, alignment: .leading)
            Text(AppStrings.TaskMarket.columnDependencies(appState.locale)).frame(width: 98, alignment: .leading)
            Text(AppStrings.TaskMarket.columnBids(appState.locale)).frame(width: 56, alignment: .leading)
            Text(AppStrings.TaskMarket.columnLease(appState.locale)).frame(width: 92, alignment: .leading)
            Text(AppStrings.TaskMarket.columnWorktree(appState.locale)).frame(width: 120, alignment: .leading)
            Text(AppStrings.TaskMarket.columnStatus(appState.locale)).frame(width: 116, alignment: .leading)
        }
        .font(.caption)
        .fontWeight(.semibold)
        .foregroundStyle(.secondary)
    }

    private func designIssueRow(_ row: TaskMarketDesignIssue) -> some View {
        HStack(spacing: 10) {
            Text("\(row.number)")
                .font(.system(size: 13, weight: .medium))
                .frame(width: 26, alignment: .leading)

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
            .frame(minWidth: 190, maxWidth: .infinity, alignment: .leading)

            modeBadge(row.parallelMode)
                .frame(width: 105, alignment: .leading)
            riskBadge(row.risk)
                .frame(width: 76, alignment: .leading)
            Text(row.dependency)
                .font(.caption)
                .foregroundStyle(row.dependency.contains("Blocked") ? .red : .secondary)
                .frame(width: 98, alignment: .leading)
            Text("\(row.bids)")
                .font(.system(size: 13, weight: .semibold))
                .frame(width: 56, alignment: .leading)
            Text(row.lease)
                .font(.caption)
                .foregroundStyle(row.lease.contains("remaining") ? .green : .primary)
                .frame(width: 92, alignment: .leading)
            Text(row.worktree)
                .font(.caption)
                .foregroundStyle(row.worktree == "-" ? .secondary : .primary)
                .frame(width: 120, alignment: .leading)
            Text(row.status)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(statusColor(row.status))
                .frame(width: 116, alignment: .leading)
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

                HStack {
                    Text(appState.locale == .zhCN ? "智能体竞标 (3)" : "Agent Bids (3)")
                        .font(.headline)
                    Spacer()
                    Menu(appState.locale == .zhCN ? "置信度" : "Confidence") {
                        Button("Confidence") {}
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

    private func inspectorGrid(_ issue: TaskMarketDesignIssue) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 7) {
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "Mission" : "Mission")
                Text("Mac Agent Workbench MVP")
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "创建时间" : "Created")
                Text("May 22, 2025 09:14")
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "上下文健康" : "Context Health")
                StatusBadge(text: "Good", color: .green)
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "测试数" : "Tests")
                Text("8")
            }
            GridRow {
                inspectorLabel(appState.locale == .zhCN ? "风险摘要" : "Risk Summary")
                Text(issue.risk == "High" ? "Concurrency bugs, lost leases" : "Low blast radius")
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
                    StatusBadge(text: "Latest", color: .blue)
                }
                Spacer()
                Text("confidence")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(bid.confidence)
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(.green)
            }

            HStack {
                inspectorLabel("Est. Files:")
                Text(bid.estimate)
                inspectorLabel("ETA:")
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
                Spacer()
                Button(appState.locale == .zhCN ? "拒绝竞标" : "Reject Bid") {}
                    .buttonStyle(.bordered)
                    .foregroundStyle(.red)
            }
            .font(.caption)
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
            Text("Owner: \(lease.owner)")
                .font(.caption2)
            Text("Lease Expires: \(lease.time)")
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
            Text("Workspace: ~/naumi")
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
