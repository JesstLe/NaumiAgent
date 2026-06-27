import SwiftUI

/// Minimal usable dashboard homepage.
/// Displays connection state, daemon/version, counts and the current snapshot content.
public struct DashboardView: View {
    @Bindable public var appState: AppState
    @State private var searchText = ""

    public init(appState: AppState) {
        self.appState = appState
    }

    private var canvasFilterLabels: [String] {
        if appState.locale == .zhCN {
            return ["问题", "智能体", "工作区", "验证", "审批", "依赖"]
        }
        return ["Issues", "Agents", "Worktrees", "Validations", "Approvals", "Dependencies"]
    }

    public var body: some View {
        Group {
            if let snapshot = appState.snapshot {
                workbenchLayout(
                    snapshot: snapshot,
                    presentation: DashboardSnapshotPresentation(snapshot: snapshot)
                )
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        header
                        if let status = appState.daemonStatus {
                            daemonCard(status: status)
                        }
                        countsGrid
                        if let lastError = appState.lastError {
                            errorCard(error: lastError)
                        }
                        emptyState
                    }
                    .padding()
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    // MARK: - Workbench Layout

    private func workbenchLayout(
        snapshot: WorkbenchSnapshotDTO,
        presentation: DashboardSnapshotPresentation
    ) -> some View {
        let layout = WorkbenchScaledPageLayout.dashboard
        let market = TaskMarketDesignPresentation(snapshot: snapshot)

        return GeometryReader { proxy in
            let scale = CGFloat(layout.scale(for: proxy.size))
            let scaledSize = layout.scaledSize(for: proxy.size)

            ZStack(alignment: .topLeading) {
                workbenchLayoutContent(presentation: presentation, market: market)
                    .frame(
                        width: layout.baseWidth,
                        height: layout.baseHeight,
                        alignment: .topLeading
                    )
                    .scaleEffect(scale, anchor: .topLeading)
                    .frame(width: scaledSize.width, height: scaledSize.height, alignment: .topLeading)
            }
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
            .clipped()
        }
    }

    private func workbenchLayoutContent(
        presentation: DashboardSnapshotPresentation,
        market: TaskMarketDesignPresentation
    ) -> some View {
        let auditTrailHeight: CGFloat = 112
        let dividerHeight: CGFloat = 1
        let mainHeight = CGFloat(WorkbenchScaledPageLayout.dashboard.baseHeight) - auditTrailHeight - dividerHeight

        return VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 0) {
                workbenchLeftRail(presentation: presentation, market: market)
                    .frame(width: 302)

                Divider()

                sharedCanvas(presentation: presentation, market: market)
                    .frame(minWidth: 620, maxWidth: .infinity, maxHeight: .infinity, alignment: .top)

                Divider()

                inspectorPanel(presentation: presentation)
                    .frame(width: 340, alignment: .top)
            }
            .frame(height: mainHeight, alignment: .top)
            .clipped()

            Divider()

            auditTrail(presentation: presentation)
                .frame(height: auditTrailHeight)
        }
        .frame(
            width: WorkbenchScaledPageLayout.dashboard.baseWidth,
            height: WorkbenchScaledPageLayout.dashboard.baseHeight,
            alignment: .topLeading
        )
    }

    private func workbenchHeader(presentation: DashboardSnapshotPresentation) -> some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                Text(AppStrings.Dashboard.title(appState.locale))
                    .font(.system(size: 18, weight: .semibold))
                Text(presentation.workbench.leftMissionTitle ?? AppStrings.Dashboard.title(appState.locale))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            StatusBadge(
                text: appState.connectionState.displayName(locale: appState.locale),
                color: connectionColor
            )

            Spacer()

            compactMetric(
                label: AppStrings.Dashboard.tasksLabel(appState.locale),
                value: "\(presentation.workbench.leftTaskCount)"
            )
            compactMetric(
                label: AppStrings.Dashboard.issuesLabel(appState.locale),
                value: "\(presentation.workbench.leftIssueCount)"
            )
            compactMetric(
                label: AppStrings.Dashboard.failuresLabel(appState.locale),
                value: "\(presentation.workbench.leftFailureCount)"
            )
        }
    }

    private func compactMetric(label: String, value: String) -> some View {
        HStack(spacing: 6) {
            Text(value)
                .font(.system(size: 13, weight: .semibold))
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func workbenchLeftRail(
        presentation: DashboardSnapshotPresentation,
        market: TaskMarketDesignPresentation
    ) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField(AppStrings.Dashboard.searchPlaceholder(appState.locale), text: $searchText)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12))

            VStack(alignment: .leading, spacing: 8) {
                railSectionTitle(AppStrings.Dashboard.missionSection(appState.locale))
                if let mission = presentation.currentMission {
                    railRow(
                        icon: "scope",
                        title: mission.title,
                        subtitle: appState.locale == .zhCN ? "进行中" : "In Progress",
                        color: .indigo
                    )
                }
                railRow(
                    icon: "square.grid.2x2",
                    title: appState.locale == .zhCN ? "总览" : "Overview",
                    subtitle: "",
                    color: .secondary
                )
                railRow(
                    icon: "person.2",
                    title: AppStrings.Dashboard.agentsSection(appState.locale),
                    subtitle: "\(presentation.agentRows.count)",
                    color: .purple
                )
                railRow(
                    icon: "point.3.connected.trianglepath.dotted",
                    title: AppStrings.Dashboard.sharedCanvasSection(appState.locale),
                    subtitle: appState.locale == .zhCN ? "已选中" : "selected",
                    color: .blue
                )
                railRow(
                    icon: "checkmark.circle",
                    title: AppStrings.Dashboard.validationRunsLabel(appState.locale),
                    subtitle: "\(presentation.taskRows.count)",
                    color: .green
                )
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    railSectionTitle(AppStrings.Dashboard.issueBacklogSection(appState.locale))
                    Spacer()
                    Text(appState.locale == .zhCN ? "优先级" : "Priority")
                        .font(.caption2)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 5))
                }
                HStack(spacing: 4) {
                    miniFilter(appState.locale == .zhCN ? "全部" : "All", "\(market.rows.count)", .blue)
                    miniFilter(
                        appState.locale == .zhCN ? "活跃" : "Active",
                        "\(market.rows.filter { $0.status != "Completed" && $0.status != "Done" }.count)",
                        .secondary
                    )
                    miniFilter(
                        appState.locale == .zhCN ? "阻塞" : "Blocked",
                        "\(market.rows.filter { $0.status == "Blocked" }.count)",
                        .secondary
                    )
                    miniFilter(
                        appState.locale == .zhCN ? "完成" : "Done",
                        "\(market.rows.filter { $0.status == "Completed" || $0.status == "Done" }.count)",
                        .secondary
                    )
                }

                ScrollView {
                    VStack(spacing: 4) {
                        ForEach(market.rows) { row in
                            dashboardIssueRailRow(row)
                        }
                    }
                }
            }

            Spacer(minLength: 8)
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func miniFilter(_ title: String, _ count: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Text(title)
            Text(count)
                .foregroundStyle(.secondary)
        }
        .font(.caption2)
        .padding(.horizontal, 7)
        .padding(.vertical, 5)
        .background(color.opacity(0.10))
        .foregroundStyle(color == .secondary ? .primary : color)
        .clipShape(RoundedRectangle(cornerRadius: 5))
    }

    private func dashboardIssueRailRow(_ row: TaskMarketDesignIssue) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Circle()
                    .fill(statusDotColor(row.status))
                    .frame(width: 6, height: 6)
                    .padding(.top, 5)
                Text("\(row.number)")
                    .font(.system(size: 13, weight: .medium))
                    .frame(width: 18, alignment: .leading)
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(row.title)
                            .font(.system(size: 12, weight: .semibold))
                            .lineLimit(1)
                        Spacer()
                        Text(row.risk.replacingOccurrences(of: "High", with: "P1").replacingOccurrences(of: "Medium", with: "P2").replacingOccurrences(of: "Low", with: "P3").replacingOccurrences(of: "Critical", with: "P0"))
                            .font(.caption2)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(riskColor(row.risk).opacity(0.10))
                            .foregroundStyle(riskColor(row.risk))
                            .clipShape(RoundedRectangle(cornerRadius: 4))
                    }
                    Text(appState.locale == .zhCN
                        ? "智能体：\(row.number % 3 == 0 ? "Test-Agent" : "Backend-Agent")"
                        : "Agent: \(row.number % 3 == 0 ? "Test-Agent" : "Backend-Agent")"
                    )
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    HStack {
                        Text(row.status)
                            .font(.caption2)
                            .foregroundStyle(statusDotColor(row.status))
                        Spacer()
                        Image(systemName: "checklist")
                        Text("\(max(1, row.number % 4))/\(max(3, row.number % 6 + 2))")
                        Image(systemName: "link")
                        Text("\(row.bids)")
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
            }
        }
        .padding(8)
        .background(row.number == 3 ? Color.accentColor.opacity(0.10) : Color.clear)
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .stroke(row.number == 3 ? Color.accentColor : Color.secondary.opacity(0.12), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }

    private func railSectionTitle(_ title: String) -> some View {
        Text(title)
            .font(.caption)
            .fontWeight(.semibold)
            .foregroundStyle(.secondary)
            .textCase(.uppercase)
    }

    private func railRow(icon: String, title: String, subtitle: String, color: Color) -> some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 18)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 12, weight: .medium))
                    .lineLimit(1)
                Text(subtitle)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer(minLength: 4)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .background(Color(nsColor: .windowBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func sharedCanvas(
        presentation: DashboardSnapshotPresentation,
        market: TaskMarketDesignPresentation
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(AppStrings.Dashboard.sharedCanvasSection(appState.locale))
                    .font(.system(size: 14, weight: .semibold))
                    .lineLimit(1)
                    .layoutPriority(2)
                Spacer(minLength: 12)
                Text(appState.locale == .zhCN ? "显示：" : "Show:")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ForEach(canvasFilterLabels, id: \.self) { item in
                    Toggle(item, isOn: .constant(true))
                        .toggleStyle(.checkbox)
                        .font(.caption)
                }
            }
            .padding(.horizontal, 16)
            .frame(height: 44, alignment: .center)

            ZStack {
                dottedCanvasBackground
                canvasConnectors

                VStack(spacing: 18) {
                    HStack {
                        Spacer()
                        if let mission = presentation.workbench.canvasNodes.first(where: { $0.kind == .mission }) {
                            canvasNodeView(node: mission)
                                .frame(width: 260)
                        }
                        Spacer()
                    }

                    HStack(alignment: .center, spacing: 16) {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("ISSUES")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)
                            ForEach(Array(market.rows.prefix(4))) { row in
                                canvasIssueCard(row)
                            }
                        }
                        .frame(width: 220)

                        VStack(spacing: 14) {
                            ForEach(presentation.workbench.canvasNodes.filter { $0.kind == .agents }, id: \.id) { node in
                                canvasNodeView(node: node)
                            }
                            ForEach(["Planner-Agent", "Backend-Agent", "Test-Agent", "Reviewer-Agent"], id: \.self) { agent in
                                compactCanvasPill(agent, color: .purple)
                            }
                        }
                        .frame(width: 160)

                        VStack(spacing: 14) {
                            ForEach(presentation.workbench.canvasNodes.filter { $0.kind == .worktrees || $0.kind == .validation || $0.kind == .failure || $0.kind == .approval }, id: \.id) { node in
                                canvasNodeView(node: node)
                            }
                        }
                        .frame(width: 240)
                    }
                }
                .padding(24)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .padding(.horizontal, 16)
            .padding(.bottom, 16)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func canvasIssueCard(_ row: TaskMarketDesignIssue) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("\(row.number)")
                    .font(.caption2)
                    .fontWeight(.bold)
                    .frame(width: 18, height: 18)
                    .background(Color.secondary.opacity(0.12))
                    .clipShape(Circle())
                Text(row.title)
                    .font(.caption)
                    .fontWeight(.semibold)
                    .lineLimit(1)
                Spacer()
                Text(row.risk == "High" ? "P1" : row.risk == "Critical" ? "P0" : "P2")
                    .font(.caption2)
                    .foregroundStyle(riskColor(row.risk))
            }
            Text(appState.locale == .zhCN ? "状态：\(row.status)" : "Status: \(row.status)")
                .font(.caption2)
                .foregroundStyle(statusDotColor(row.status))
            Text(appState.locale == .zhCN
                ? "智能体：\(row.number % 3 == 0 ? "Test-Agent" : "Backend-Agent")"
                : "Agent: \(row.number % 3 == 0 ? "Test-Agent" : "Backend-Agent")"
            )
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(10)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .stroke(row.number == 3 ? Color.accentColor : Color.secondary.opacity(0.22), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }

    private func compactCanvasPill(_ title: String, color: Color) -> some View {
        HStack {
            Image(systemName: "person.fill")
                .foregroundStyle(color)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.caption)
                    .fontWeight(.medium)
                Text(title.contains("Backend") ? "Working on 2 issues" : "Idle")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Circle()
                .fill(title.contains("Backend") ? .blue : .green)
                .frame(width: 7, height: 7)
        }
        .padding(9)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .stroke(color.opacity(0.35), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }

    private var dottedCanvasBackground: some View {
        Canvas { context, size in
            for x in stride(from: 12.0, through: size.width, by: 24.0) {
                for y in stride(from: 12.0, through: size.height, by: 24.0) {
                    let dot = Path(ellipseIn: CGRect(x: x, y: y, width: 2, height: 2))
                    context.fill(dot, with: .color(Color.secondary.opacity(0.18)))
                }
            }
        }
        .allowsHitTesting(false)
    }

    private var canvasConnectors: some View {
        Canvas { context, size in
            var path = Path()
            path.move(to: CGPoint(x: size.width * 0.18, y: size.height * 0.28))
            path.addCurve(
                to: CGPoint(x: size.width * 0.72, y: size.height * 0.34),
                control1: CGPoint(x: size.width * 0.34, y: size.height * 0.18),
                control2: CGPoint(x: size.width * 0.58, y: size.height * 0.42)
            )
            path.move(to: CGPoint(x: size.width * 0.24, y: size.height * 0.62))
            path.addCurve(
                to: CGPoint(x: size.width * 0.78, y: size.height * 0.68),
                control1: CGPoint(x: size.width * 0.38, y: size.height * 0.74),
                control2: CGPoint(x: size.width * 0.62, y: size.height * 0.52)
            )
            context.stroke(path, with: .color(Color.accentColor.opacity(0.22)), lineWidth: 2)
        }
        .allowsHitTesting(false)
    }

    private func canvasNodeView(node: DashboardCanvasNode) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: iconName(for: node.kind))
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(color(for: node.kind))

                Text(label(for: node.kind))
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Spacer()
            }

            Text(node.title)
                .font(.system(size: 13, weight: .semibold))
                .lineLimit(2)
                .frame(minHeight: 34, alignment: .topLeading)

            HStack {
                Text(subtitle(for: node))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer()
                Text(node.status)
                    .font(.caption2)
                    .fontWeight(.medium)
                    .lineLimit(1)
            }
        }
        .padding(12)
        .frame(height: 126)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(color(for: node.kind).opacity(0.28), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .shadow(color: .black.opacity(0.05), radius: 10, y: 4)
    }

    private func inspectorPanel(presentation: DashboardSnapshotPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(AppStrings.Dashboard.inspectorSection(appState.locale))
                .font(.system(size: 14, weight: .semibold))
            Picker("", selection: .constant("Context")) {
                Text(appState.locale == .zhCN ? "上下文" : "Context").tag("Context")
                Text(appState.locale == .zhCN ? "差异" : "Diff").tag("Diff")
                Text(appState.locale == .zhCN ? "测试" : "Tests").tag("Tests")
                Text(appState.locale == .zhCN ? "风险" : "Risk").tag("Risk")
                Text(appState.locale == .zhCN ? "审批" : "Approval").tag("Approval")
            }
            .pickerStyle(.segmented)

            if let inspector = presentation.workbench.inspector {
                VStack(alignment: .leading, spacing: 10) {
                    Text(inspector.title)
                        .font(.system(size: 15, weight: .semibold))
                        .lineLimit(2)

                    StatusBadge(text: inspector.status, color: statusColor(for: inspector.status))

                    inspectorDetail(AppStrings.Dashboard.ownerLabel(appState.locale), inspector.owner ?? "-")
                    inspectorDetail(AppStrings.Dashboard.riskLabel(appState.locale), inspector.riskLevel ?? "-")
                    inspectorDetail(AppStrings.Dashboard.parallelModeLabel(appState.locale), inspector.parallelMode ?? "-")
                    inspectorDetail(
                        AppStrings.Dashboard.acceptanceCriteriaLabel(appState.locale),
                        inspector.acceptanceCriteriaCount.map(String.init) ?? "-"
                    )
                    inspectorDetail(
                        AppStrings.Dashboard.humanApprovalLabel(appState.locale),
                        inspector.requiresHumanApproval
                            ? AppStrings.Dashboard.approvalRequiredValue(appState.locale)
                            : AppStrings.Dashboard.approvalNotRequiredValue(appState.locale)
                    )
                }
                .padding(12)
                .background(Color.secondary.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                emptyListLabel(AppStrings.Dashboard.noSelection(appState.locale))
            }

            if let status = appState.daemonStatus {
                daemonCompact(status: status)
            }

            inspectorStateCard(
                title: appState.locale == .zhCN ? "验证状态" : "Validation State",
                tone: .red,
                lines: [
                    appState.locale == .zhCN ? "最近运行：#23 (09:36)" : "Latest Run: #23 (09:36)",
                    appState.locale == .zhCN ? "结果：pytest failed" : "Result: pytest failed",
                    appState.locale == .zhCN ? "测试：12 失败，3 通过" : "Tests: 12 failed, 3 passed"
                ]
            )

            inspectorStateCard(
                title: appState.locale == .zhCN ? "上下文健康" : "Context Health",
                tone: .orange,
                lines: [
                    appState.locale == .zhCN ? "整体：过期" : "Overall: Stale",
                    appState.locale == .zhCN ? "已分析文件：18" : "Files Analyzed: 18",
                    appState.locale == .zhCN ? "更新：18 分钟前" : "Last Updated: 18m ago"
                ]
            )

            Spacer(minLength: 0)
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func inspectorStateCard(title: String, tone: Color, lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(tone)
            ForEach(lines, id: \.self) { line in
                Text(line)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Button(title.contains("验证") || title.contains("Validation") ? "Re-run Validation" : "Refresh Context") {}
                .font(.caption)
                .buttonStyle(.bordered)
                .frame(maxWidth: .infinity)
        }
        .padding(12)
        .background(tone.opacity(0.07))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(tone.opacity(0.25), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func inspectorDetail(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(value)
                .font(.caption)
                .fontWeight(.medium)
                .lineLimit(1)
        }
    }

    private func daemonCompact(status: DaemonStatusDTO) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            railSectionTitle(AppStrings.Dashboard.daemonSection(appState.locale))
            inspectorDetail(AppStrings.Dashboard.daemonStatusLabel(appState.locale), status.status)
            inspectorDetail(AppStrings.Dashboard.daemonHostLabel(appState.locale), "\(status.host):\(status.port)")
            inspectorDetail(AppStrings.Dashboard.daemonPIDLabel(appState.locale), "\(status.pid)")
        }
        .padding(12)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func auditTrail(presentation: DashboardSnapshotPresentation) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Dashboard.auditTrailSection(appState.locale))
                .font(.system(size: 13, weight: .semibold))

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(presentation.workbench.auditRows, id: \.id) { row in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(row.type)
                                .font(.system(size: 12, weight: .medium))
                                .lineLimit(1)
                            Text("\(row.actor) · \(row.timestamp)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .frame(width: 210, alignment: .leading)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                }
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func iconName(for kind: DashboardCanvasNodeKind) -> String {
        switch kind {
        case .mission:
            return "scope"
        case .issue:
            return "exclamationmark.triangle"
        case .agents:
            return "person.2"
        case .worktrees:
            return "folder.badge.gearshape"
        case .validation:
            return "checkmark.seal"
        case .failure:
            return "xmark.octagon"
        case .approval:
            return "hand.raised"
        }
    }

    private func label(for kind: DashboardCanvasNodeKind) -> String {
        switch kind {
        case .mission:
            return AppStrings.Dashboard.missionSection(appState.locale)
        case .issue:
            return AppStrings.Dashboard.issueBacklogSection(appState.locale)
        case .agents:
            return AppStrings.Dashboard.agentsSection(appState.locale)
        case .worktrees:
            return AppStrings.Dashboard.gitWorktreesLabel(appState.locale)
        case .validation:
            return AppStrings.Dashboard.validationRunsLabel(appState.locale)
        case .failure:
            return AppStrings.Dashboard.failuresSection(appState.locale)
        case .approval:
            return AppStrings.Dashboard.humanApprovalLabel(appState.locale)
        }
    }

    private func subtitle(for node: DashboardCanvasNode) -> String {
        switch node.kind {
        case .mission:
            return AppStrings.Dashboard.missionSection(appState.locale)
        case .agents:
            return AppStrings.Dashboard.agentsLabel(appState.locale)
        case .worktrees:
            return AppStrings.Dashboard.gitWorktreesLabel(appState.locale)
        case .validation:
            return AppStrings.Dashboard.validationRunsLabel(appState.locale)
        case .approval:
            return AppStrings.Dashboard.humanApprovalLabel(appState.locale)
        case .issue, .failure:
            return node.subtitle
        }
    }

    private func color(for kind: DashboardCanvasNodeKind) -> Color {
        switch kind {
        case .mission:
            return .indigo
        case .issue:
            return .orange
        case .agents:
            return .blue
        case .worktrees:
            return .green
        case .validation:
            return .teal
        case .failure:
            return .red
        case .approval:
            return .purple
        }
    }

    private func riskColor(_ risk: String) -> Color {
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

    private func statusDotColor(_ status: String) -> Color {
        switch status.lowercased() {
        case "leased", "completed", "done", "passed":
            return .green
        case "blocked", "failed":
            return .red
        case "requires proposal", "in_progress", "active":
            return .blue
        default:
            return .secondary
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.Dashboard.title(appState.locale))
                .font(.largeTitle)
                .fontWeight(.bold)

            HStack(spacing: 12) {
                StatusBadge(
                    text: appState.connectionState.displayName(locale: appState.locale),
                    color: connectionColor
                )
                if let daemon = appState.daemonStatus {
                    StatusBadge(
                        text: "v\(daemon.version)",
                        color: .blue
                    )
                }
            }
        }
    }

    private var connectionColor: Color {
        switch appState.connectionState {
        case .connected:
            return .green
        case .connecting:
            return .orange
        case .disconnected, .stale:
            return .red
        }
    }

    // MARK: - Daemon Card

    private func daemonCard(status: DaemonStatusDTO) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Dashboard.daemonSection(appState.locale))
                .font(.headline)
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.daemonStatusLabel(appState.locale),
                    value: status.status
                )
                detailItem(
                    label: AppStrings.Dashboard.daemonHostLabel(appState.locale),
                    value: "\(status.host):\(status.port)"
                )
                detailItem(
                    label: AppStrings.Dashboard.daemonPIDLabel(appState.locale),
                    value: "\(status.pid)"
                )
                detailItem(
                    label: AppStrings.Dashboard.daemonWorkspaceCountLabel(appState.locale),
                    value: "\(status.workspaceCount)"
                )
            }
        }
        .padding()
        .background(Color.secondary.opacity(0.1))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func detailItem(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.body)
                .fontWeight(.medium)
        }
    }

    // MARK: - Counts

    private var countsGrid: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.Dashboard.countsSection(appState.locale))
                .font(.headline)

            LazyVGrid(
                columns: [
                    GridItem(.adaptive(minimum: 120), spacing: 12)
                ],
                spacing: 12
            ) {
                countCard(
                    title: AppStrings.Dashboard.missionsLabel(appState.locale),
                    count: appState.snapshot?.missions.count ?? 0,
                    color: .blue
                )
                countCard(
                    title: AppStrings.Dashboard.agentsLabel(appState.locale),
                    count: appState.snapshot?.agentProfiles.count ?? 0,
                    color: .green
                )
                countCard(
                    title: AppStrings.Dashboard.tasksLabel(appState.locale),
                    count: appState.snapshot?.tasks.count ?? 0,
                    color: .purple
                )
                countCard(
                    title: AppStrings.Dashboard.issuesLabel(appState.locale),
                    count: appState.snapshot?.issues.count ?? 0,
                    color: .orange
                )
                countCard(
                    title: AppStrings.Dashboard.failuresLabel(appState.locale),
                    count: appState.snapshot?.failures.count ?? 0,
                    color: .red
                )
                countCard(
                    title: AppStrings.Dashboard.eventsLabel(appState.locale),
                    count: appState.snapshot?.events.count ?? 0,
                    color: .teal
                )
            }
        }
    }

    private func countCard(title: String, count: Int, color: Color) -> some View {
        VStack(spacing: 8) {
            Text("\(count)")
                .font(.system(size: 32, weight: .bold))
                .foregroundStyle(color)
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding()
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Snapshot Content

    private func snapshotContent(snapshot: WorkbenchSnapshotDTO) -> some View {
        let presentation = DashboardSnapshotPresentation(snapshot: snapshot)

        return VStack(alignment: .leading, spacing: 20) {
            if let mission = presentation.currentMission {
                missionCard(mission: mission)
            }
            agentsSection(rows: presentation.agentRows)
            taskQueueSection(rows: presentation.taskRows)
            failuresSection(rows: presentation.failureRows)
            eventsSection(rows: presentation.recentEventRows)
        }
    }

    private func missionCard(mission: DashboardMissionSummary) -> some View {
        sectionCard(title: AppStrings.Dashboard.missionSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 8) {
                Text(mission.title)
                    .font(.title3)
                    .fontWeight(.semibold)
                HStack(spacing: 16) {
                    detailItem(
                        label: AppStrings.Dashboard.statusLabel(appState.locale),
                        value: mission.status
                    )
                }
            }
        }
    }

    private func agentsSection(rows: [DashboardAgentRow]) -> some View {
        sectionCard(title: AppStrings.Dashboard.agentsSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                if rows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyAgents(appState.locale))
                } else {
                    ForEach(rows, id: \.id) { row in
                        agentRowView(row: row)
                        if row.id != rows.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func agentRowView(row: DashboardAgentRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(row.name)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                StatusBadge(text: row.status, color: statusColor(for: row.status))
            }

            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.roleLabel(appState.locale),
                    value: row.role
                )
                detailItem(
                    label: AppStrings.Dashboard.capabilitiesLabel(appState.locale),
                    value: "\(row.capabilityCount)"
                )
                detailItem(
                    label: AppStrings.Dashboard.maxParallelTasksLabel(appState.locale),
                    value: "\(row.maxParallelTasks)"
                )
            }
        }
    }

    private func taskQueueSection(rows: [DashboardTaskRow]) -> some View {
        sectionCard(title: AppStrings.Dashboard.taskQueueSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                if rows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyTasks(appState.locale))
                } else {
                    ForEach(rows, id: \.id) { row in
                        taskRowView(row: row)
                        if row.id != rows.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func taskRowView(row: DashboardTaskRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(row.subject)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                StatusBadge(text: row.status, color: statusColor(for: row.status))
            }

            HStack(spacing: 16) {
                if let owner = row.owner {
                    detailItem(
                        label: AppStrings.Dashboard.ownerLabel(appState.locale),
                        value: owner
                    )
                }
                if let activeForm = row.activeForm {
                    detailItem(
                        label: AppStrings.Dashboard.activeFormLabel(appState.locale),
                        value: activeForm
                    )
                }
            }

            if row.riskLevel != nil || row.parallelMode != nil {
                HStack(spacing: 16) {
                    if let riskLevel = row.riskLevel {
                        detailItem(
                            label: AppStrings.Dashboard.riskLabel(appState.locale),
                            value: riskLevel
                        )
                    }
                    if let parallelMode = row.parallelMode {
                        detailItem(
                            label: AppStrings.Dashboard.parallelModeLabel(appState.locale),
                            value: parallelMode
                        )
                    }
                    if let count = row.acceptanceCriteriaCount {
                        detailItem(
                            label: AppStrings.Dashboard.acceptanceCriteriaLabel(appState.locale),
                            value: "\(count)"
                        )
                    }
                }
            }
        }
    }

    private func failuresSection(rows: [DashboardFailureRow]) -> some View {
        sectionCard(title: AppStrings.Dashboard.failuresSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                if rows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyFailures(appState.locale))
                } else {
                    ForEach(rows, id: \.id) { row in
                        failureRowView(row: row)
                        if row.id != rows.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func failureRowView(row: DashboardFailureRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(row.title)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                StatusBadge(text: row.status, color: .red)
            }
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.kindLabel(appState.locale),
                    value: row.kind
                )
                detailItem(
                    label: AppStrings.Dashboard.tasksLabel(appState.locale),
                    value: row.taskID
                )
            }
        }
    }

    private func eventsSection(rows: [DashboardEventRow]) -> some View {
        sectionCard(title: AppStrings.Dashboard.eventsSection(appState.locale)) {
            VStack(alignment: .leading, spacing: 12) {
                if rows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyEvents(appState.locale))
                } else {
                    ForEach(rows, id: \.id) { row in
                        eventRowView(row: row)
                        if row.id != rows.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func eventRowView(row: DashboardEventRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(row.type)
                    .font(.body)
                    .fontWeight(.medium)
                Spacer()
                Text(row.timestamp)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.actorLabel(appState.locale),
                    value: row.actor
                )
                detailItem(
                    label: AppStrings.Dashboard.subjectsLabel(appState.locale),
                    value: row.subjectID
                )
            }
        }
    }

    private func sectionCard<Content: View>(
        title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.headline)
            content()
        }
        .padding()
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func emptyListLabel(_ text: String) -> some View {
        HStack {
            Spacer()
            Text(text)
                .foregroundStyle(.secondary)
                .font(.callout)
            Spacer()
        }
        .padding(.vertical, 8)
    }

    private func statusColor(for status: String) -> Color {
        switch status.lowercased() {
        case "completed", "done", "closed", "resolved":
            return .green
        case "in_progress", "running", "active", "busy":
            return .blue
        case "blocked", "failed", "open":
            return .red
        case "planning", "pending", "waiting":
            return .orange
        default:
            return .secondary
        }
    }

    // MARK: - Error Card

    private func errorCard(error: APIError) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Dashboard.errorSection(appState.locale))
                .font(.headline)
                .foregroundStyle(.red)
            HStack(spacing: 16) {
                detailItem(
                    label: AppStrings.Dashboard.errorDetailLabel(appState.locale),
                    value: error.localizedMessage(locale: appState.locale)
                )
            }
        }
        .padding()
        .background(Color.red.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Empty State

    private var emptyState: some View {
        HStack {
            Spacer()
            VStack(spacing: 8) {
                Image(systemName: "tray")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text(AppStrings.Dashboard.emptySnapshot(appState.locale))
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.vertical, 32)
    }
}

#if DEBUG
struct DashboardView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        state.locale = .zhCN
        state.connectionState = .connected
        state.daemonStatus = DaemonStatusDTO(
            status: "running",
            version: "0.1.0",
            pid: 12345,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 3
        )
        return DashboardView(appState: state)
            .frame(minWidth: 640, minHeight: 420)
    }
}
#endif
