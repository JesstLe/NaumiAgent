import SwiftUI

/// Standalone audit-event timeline for the selected session.
public struct TimelineView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = TimelineDashboardPresentation(events: appState.timelineEvents)

        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header(presentation: presentation)
                if let lastError = appState.lastError {
                    errorCard(error: lastError)
                }
                summaryStrip(presentation: presentation)
                dashboardGrid(presentation: presentation)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            guard !appState.isPreviewFixture else { return }
            await daemonController.refreshEvents(limit: 50)
        }
    }

    private func header(presentation: TimelineDashboardPresentation) -> some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Text(AppStrings.Timeline.title(appState.locale))
                    .font(.system(size: 22, weight: .semibold))
                Text(subtitleText(presentation: presentation))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Button {
                if !appState.isPreviewFixture {
                    Task {
                        await daemonController.refreshEvents(limit: 50)
                    }
                }
            } label: {
                Label(AppStrings.Timeline.refreshButton(appState.locale), systemImage: "arrow.clockwise")
            }
            .buttonStyle(.bordered)
        }
    }

    private func summaryStrip(presentation: TimelineDashboardPresentation) -> some View {
        HStack(spacing: 12) {
            metricCard(
                title: appState.locale == .zhCN ? "事件总数" : "Events",
                value: "\(presentation.totalCount)",
                systemImage: "clock.arrow.circlepath"
            )
            metricCard(
                title: appState.locale == .zhCN ? "执行者" : "Actors",
                value: "\(presentation.actorCount)",
                systemImage: "person.2",
                tint: .purple
            )
            metricCard(
                title: appState.locale == .zhCN ? "事件类型" : "Event Types",
                value: "\(presentation.typeBuckets.count)",
                systemImage: "tag"
            )
            metricCard(
                title: appState.locale == .zhCN ? "最近事件" : "Latest",
                value: presentation.latestEvent?.timestamp ?? "-",
                systemImage: "bolt.circle",
                tint: .orange
            )
        }
    }

    private func dashboardGrid(presentation: TimelineDashboardPresentation) -> some View {
        HStack(alignment: .top, spacing: 14) {
            panel(title: appState.locale == .zhCN ? "事件流" : "Event Stream") {
                if presentation.events.isEmpty {
                    emptyState
                } else {
                    VStack(spacing: 10) {
                        ForEach(presentation.events.reversed()) { event in
                            eventRow(event: event, isLatest: event.id == presentation.latestEvent?.id)
                        }
                    }
                }
            }
            .frame(minWidth: 420, maxWidth: .infinity, alignment: .top)

            VStack(spacing: 14) {
                latestEventPanel(event: presentation.latestEvent)
                typeDistributionPanel(presentation: presentation)
            }
            .frame(width: 340, alignment: .top)
        }
    }

    private func latestEventPanel(event: TimelineEventPresentation?) -> some View {
        panel(title: appState.locale == .zhCN ? "最新事件详情" : "Latest Event") {
            if let event {
                VStack(alignment: .leading, spacing: 13) {
                    HStack(spacing: 10) {
                        Image(systemName: iconName(for: event.type))
                            .font(.system(size: 19, weight: .semibold))
                            .foregroundStyle(color(for: event.type))
                        Text(event.type)
                            .font(.system(size: 18, weight: .semibold))
                            .lineLimit(1)
                    }
                    twoColumnDetail(
                        leftLabel: AppStrings.Timeline.actorLabel(appState.locale),
                        leftValue: event.actor,
                        rightLabel: AppStrings.Timeline.subjectLabel(appState.locale),
                        rightValue: event.subjectID
                    )
                    detailBlock(label: appState.locale == .zhCN ? "时间" : "Time", value: event.timestamp)
                    if !event.payloadSummary.isEmpty {
                        detailBlock(label: appState.locale == .zhCN ? "载荷" : "Payload", value: event.payloadSummary)
                    }
                }
            } else {
                emptyState
            }
        }
    }

    private func typeDistributionPanel(presentation: TimelineDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "事件类型分布" : "Event Type Mix") {
            if presentation.typeBuckets.isEmpty {
                emptyState
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(presentation.typeBuckets) { bucket in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(bucket.type)
                                    .font(.system(size: 13, weight: .medium))
                                    .lineLimit(1)
                                Spacer()
                                Text("\(bucket.count)")
                                    .font(.system(size: 13, weight: .semibold))
                                    .foregroundStyle(.secondary)
                            }
                            GeometryReader { proxy in
                                let width = max(8, proxy.size.width * CGFloat(bucket.count) / CGFloat(max(1, presentation.totalCount)))
                                RoundedRectangle(cornerRadius: 3)
                                    .fill(color(for: bucket.type).opacity(0.65))
                                    .frame(width: width, height: 6)
                            }
                            .frame(height: 6)
                        }
                        .padding(.vertical, 4)
                    }
                }
            }
        }
    }

    private func eventRow(event: TimelineEventPresentation, isLatest: Bool) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 4) {
                Circle()
                    .fill(color(for: event.type))
                    .frame(width: 10, height: 10)
                Rectangle()
                    .fill(Color.secondary.opacity(0.18))
                    .frame(width: 1, height: 42)
            }
            .padding(.top, 4)

            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Label(event.type, systemImage: iconName(for: event.type))
                        .font(.system(size: 14, weight: .semibold))
                        .labelStyle(.titleAndIcon)
                        .lineLimit(1)
                    if isLatest {
                        Text(appState.locale == .zhCN ? "最新" : "Latest")
                            .font(.caption)
                            .fontWeight(.semibold)
                            .foregroundStyle(Color.accentColor)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(Color.accentColor.opacity(0.12))
                            .clipShape(Capsule())
                    }
                    Spacer()
                    Text(event.timestamp)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                HStack(spacing: 16) {
                    compactDetail(label: AppStrings.Timeline.actorLabel(appState.locale), value: event.actor)
                    compactDetail(label: AppStrings.Timeline.subjectLabel(appState.locale), value: event.subjectID)
                }

                if !event.payloadSummary.isEmpty {
                    Text(event.payloadSummary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        }
        .padding(12)
        .background(isLatest ? Color.accentColor.opacity(0.10) : Color(nsColor: .controlBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(isLatest ? Color.accentColor.opacity(0.65) : Color.secondary.opacity(0.13), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func metricCard(title: String, value: String, systemImage: String, tint: Color = .accentColor) -> some View {
        HStack(spacing: 12) {
            Image(systemName: systemImage)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 28, height: 28)
                .background(tint.opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(value)
                    .font(.system(size: value.count > 12 ? 12 : 19, weight: .semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(height: 74)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func panel<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.system(size: 14, weight: .semibold))
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Color.secondary.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func twoColumnDetail(
        leftLabel: String,
        leftValue: String,
        rightLabel: String,
        rightValue: String
    ) -> some View {
        HStack(spacing: 18) {
            detailBlock(label: leftLabel, value: leftValue)
            detailBlock(label: rightLabel, value: rightValue)
        }
    }

    private func compactDetail(label: String, value: String) -> some View {
        HStack(spacing: 5) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption)
                .fontWeight(.medium)
                .lineLimit(1)
        }
    }

    private func detailBlock(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func errorCard(error: APIError) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Dashboard.errorSection(appState.locale))
                .font(.headline)
                .foregroundStyle(.red)
            Text(error.localizedMessage(locale: appState.locale))
                .font(.body)
                .foregroundStyle(.red)
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "clock.badge.questionmark")
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
            Text(AppStrings.Timeline.emptyEvents(appState.locale))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 26)
    }

    private func subtitleText(presentation: TimelineDashboardPresentation) -> String {
        let count = AppStrings.Timeline.eventCount(appState.locale, count: presentation.totalCount)
        guard let sessionID = appState.selectedSessionID else { return count }
        return "\(count) · \(sessionID)"
    }

    private func iconName(for type: String) -> String {
        if type.contains("validation") {
            return "checkmark.seal"
        }
        if type.contains("approval") {
            return "hand.raised"
        }
        if type.contains("task") {
            return "checklist"
        }
        if type.contains("mission") {
            return "scope"
        }
        return "circle.hexagongrid"
    }

    private func color(for type: String) -> Color {
        if type.contains("validation") {
            return .green
        }
        if type.contains("approval") {
            return .purple
        }
        if type.contains("task") {
            return .blue
        }
        if type.contains("mission") {
            return .orange
        }
        return .secondary
    }
}

#if NAUMI_WORKBENCH_LOCAL_PREVIEWS
struct TimelineView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        state.locale = .zhCN
        state.selectedSessionID = "sess-preview"
        state.timelineEvents = [
            EventDTO(
                id: "evt-1",
                sessionID: "sess-preview",
                type: "mission.created",
                actor: "Human",
                subjectID: "mission-1",
                payload: ["title": .string("Mac 工作台")],
                timestamp: "2026-06-27T06:00:00"
            ),
            EventDTO(
                id: "evt-2",
                sessionID: "sess-preview",
                type: "task.updated",
                actor: "Planner-Agent",
                subjectID: "task-1",
                payload: ["status": .string("leased")],
                timestamp: "2026-06-27T06:04:00"
            )
        ]
        return TimelineView(
            appState: state,
            daemonController: DaemonController(
                appState: state,
                apiProvider: PreviewWorkbenchAPIProvider()
            )
        )
        .frame(minWidth: 900, minHeight: 560)
    }
}

@MainActor
private final class PreviewWorkbenchAPIProvider: WorkbenchAPIProviding {
    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO {
        DaemonStatusDTO(
            status: "running",
            version: "0.1.0",
            pid: 1,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 0
        )
    }

    func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO {
        CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: false,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1
        )
    }

    func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO {
        WorkbenchSnapshotDTO(sessionID: sessionID, missions: [], tasks: [], issues: [], failures: [], events: [])
    }

    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        SessionListDTO(sessions: [], total: 0, page: page, pageSize: pageSize)
    }

    func createSession(title: String?, model: String?, systemPrompt: String?) async throws(APIError) -> SessionDTO {
        SessionDTO(
            id: "preview-session",
            title: title,
            model: model ?? "preview",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00",
            messageCount: 0,
            totalTokens: 0,
            totalCostUSD: 0,
            status: "active"
        )
    }

    func fetchEvents(
        sessionID: String,
        eventType: String?,
        subjectID: String?,
        actor: String?,
        limit: Int
    ) async throws(APIError) -> WorkbenchEventsDTO {
        WorkbenchEventsDTO(events: [], eventType: eventType, subjectID: subjectID, actor: actor, limit: limit)
    }

    func fetchValidationRuns(sessionID: String, taskID: String?, limit: Int) async throws(APIError) -> ValidationRunsDTO {
        ValidationRunsDTO(validationRuns: [], taskID: taskID, limit: limit)
    }

    func fetchContextSnapshots(sessionID: String, limit: Int) async throws(APIError) -> ContextSnapshotsDTO {
        ContextSnapshotsDTO(snapshots: [], limit: limit)
    }

    func fetchApprovals(status: String?, limit: Int) async throws(APIError) -> ApprovalsDTO {
        ApprovalsDTO(approvals: [], status: status, limit: limit)
    }

    func fetchAgentProfiles(sessionID: String?, status: String?, limit: Int) async throws(APIError) -> AgentProfilesDTO {
        AgentProfilesDTO(agents: [], status: status, limit: limit)
    }

    func createMission(sessionID: String, title: String, goal: String) async throws(APIError) -> MissionDTO {
        MissionDTO(
            id: "preview-mission",
            sessionID: sessionID,
            title: title,
            goal: goal,
            status: "active",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:00:00"
        )
    }

    func createIntentLock(
        missionID: String,
        actor: String,
        rule: String,
        blockedPaths: [String],
        allowedPaths: [String],
        requireProposalForRisk: String
    ) async throws(APIError) -> IntentLockDTO {
        IntentLockDTO(
            id: "preview-lock",
            missionID: missionID,
            actor: actor,
            rule: rule,
            blockedPaths: blockedPaths,
            allowedPaths: allowedPaths,
            requireProposalForRisk: requireProposalForRisk,
            createdAt: "2026-06-27T06:00:00"
        )
    }

    func createApprovalDecision(
        approvalID: String,
        decision: String,
        reviewer: String,
        note: String?
    ) async throws(APIError) -> DecisionDTO {
        DecisionDTO(
            id: "preview-decision",
            approvalID: approvalID,
            decision: decision,
            reviewer: reviewer,
            note: note,
            createdAt: "2026-06-27T06:00:00"
        )
    }

    func runValidation(
        sessionID: String,
        taskID: String,
        command: [String],
        cwd: String?,
        actor: String
    ) async throws(APIError) -> ValidationRunDTO {
        ValidationRunDTO(
            id: "preview-run",
            sessionID: sessionID,
            taskID: taskID,
            command: command,
            cwd: cwd,
            status: "passed",
            exitCode: 0,
            outputSummary: "preview",
            actor: actor,
            completedAt: "2026-06-27T06:00:00"
        )
    }
}
#endif
