import SwiftUI

/// Minimal usable dashboard homepage.
/// Displays connection state, daemon/version, counts and the current snapshot content.
public struct DashboardView: View {
    @Bindable public var appState: AppState
    @State private var searchText = ""

    public init(appState: AppState) {
        self.appState = appState
    }

    public var body: some View {
        Group {
            if let snapshot = appState.snapshot {
                workbenchLayout(presentation: DashboardSnapshotPresentation(snapshot: snapshot))
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
        .navigationTitle(AppStrings.Dashboard.title(appState.locale))
    }

    // MARK: - Workbench Layout

    private func workbenchLayout(presentation: DashboardSnapshotPresentation) -> some View {
        VStack(spacing: 0) {
            workbenchHeader(presentation: presentation)
                .padding(.horizontal, 18)
                .padding(.vertical, 12)

            Divider()

            HStack(spacing: 0) {
                workbenchLeftRail(presentation: presentation)
                    .frame(width: 244)

                Divider()

                sharedCanvas(presentation: presentation)
                    .frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity)

                Divider()

                inspectorPanel(presentation: presentation)
                    .frame(width: 294)
            }

            Divider()

            auditTrail(presentation: presentation)
                .frame(height: 112)
        }
        .frame(minWidth: 980, minHeight: 640)
    }

    private func workbenchHeader(presentation: DashboardSnapshotPresentation) -> some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                Text("NaumiAgent Workbench")
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

    private func workbenchLeftRail(presentation: DashboardSnapshotPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            TextField(AppStrings.Dashboard.searchPlaceholder(appState.locale), text: $searchText)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12))

            VStack(alignment: .leading, spacing: 8) {
                railSectionTitle(AppStrings.Dashboard.missionSection(appState.locale))
                if let mission = presentation.currentMission {
                    railRow(
                        icon: "scope",
                        title: mission.title,
                        subtitle: mission.status,
                        color: .indigo
                    )
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                railSectionTitle(AppStrings.Dashboard.issueBacklogSection(appState.locale))
                if presentation.taskRows.isEmpty {
                    emptyListLabel(AppStrings.Dashboard.emptyTasks(appState.locale))
                } else {
                    ForEach(presentation.taskRows, id: \.id) { row in
                        railRow(
                            icon: row.riskLevel == nil ? "checkmark.circle" : "exclamationmark.triangle",
                            title: row.subject,
                            subtitle: row.status,
                            color: row.riskLevel == nil ? .green : .orange
                        )
                    }
                }
            }

            Spacer(minLength: 8)
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
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

    private func sharedCanvas(presentation: DashboardSnapshotPresentation) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(AppStrings.Dashboard.sharedCanvasSection(appState.locale))
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                StatusBadge(text: AppStrings.Dashboard.validationRunsLabel(appState.locale), color: .teal)
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)

            ZStack {
                dottedCanvasBackground
                canvasConnectors

                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 188, maximum: 230), spacing: 14)],
                    alignment: .center,
                    spacing: 14
                ) {
                    ForEach(presentation.workbench.canvasNodes, id: \.id) { node in
                        canvasNodeView(node: node)
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

            Spacer(minLength: 0)
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
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
