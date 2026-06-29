import SwiftUI

/// Standalone audit-event timeline for the selected session.
public struct TimelineView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController
    @State private var selectedEventID: String?
    @State private var filterDraft = TimelineEventFilterDraft()
    @State private var isRefreshingEvents = false

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = TimelineDashboardPresentation(events: appState.timelineEvents)
        let loadedSelectedEvent = appState.selectedEvent
            .map(TimelineEventPresentation.init)
            .flatMap { $0.id == selectedEventID ? $0 : nil }
        let selectedEvent = loadedSelectedEvent
            ?? presentation.events.first { $0.id == selectedEventID }
            ?? presentation.latestEvent

        VStack(spacing: 0) {
            header(presentation: presentation)
            Divider()

            HStack(spacing: 0) {
                eventRail(presentation: presentation)
                    .frame(width: 316)
                    .frame(maxHeight: .infinity)
                    .clipped()

                Divider()

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        summaryStrip(presentation: presentation)
                        causalChainPanel(presentation: presentation)
                        actorDistributionPanel(presentation: presentation)
                    }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                Divider()

                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        latestEventPanel(event: selectedEvent)
                        typeDistributionPanel(presentation: presentation)
                        if let lastError = appState.lastError {
                            errorCard(error: lastError)
                        }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(width: 334)
            }
        }
        .frame(minWidth: 1120, minHeight: 700)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            if selectedEventID == nil {
                selectedEventID = presentation.latestEvent?.id
            }
        }
        .task {
            guard !appState.isPreviewFixture else { return }
            await daemonController.refreshEvents(limit: 50)
        }
    }

    private func header(presentation: TimelineDashboardPresentation) -> some View {
        HStack(alignment: .center, spacing: 16) {
            Text(AppStrings.Timeline.title(appState.locale))
                .font(.system(size: 17, weight: .semibold))
            Text(subtitleText(presentation: presentation))
                .font(.caption)
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                if !appState.isPreviewFixture {
                    Task {
                        await refreshEventsUsingCurrentFilter()
                    }
                }
            } label: {
                Label(AppStrings.Timeline.refreshButton(appState.locale), systemImage: "arrow.clockwise")
            }
            .buttonStyle(.bordered)
            .disabled(isRefreshingEvents)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 11)
    }

    private func eventRail(presentation: TimelineDashboardPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label(appState.locale == .zhCN ? "审计事件" : "Audit Events", systemImage: "clock.arrow.circlepath")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Text("\(presentation.totalCount)")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.10))
                    .clipShape(Capsule())
            }

            if let latest = presentation.latestEvent {
                HStack(spacing: 8) {
                    Circle()
                        .fill(color(for: latest.type))
                        .frame(width: 8, height: 8)
                    Text(appState.locale == .zhCN ? "最新：\(latest.type)" : "Latest: \(latest.type)")
                        .font(.caption)
                        .lineLimit(1)
                    Spacer()
                }
                .padding(10)
                .background(Color.accentColor.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }

            filterPanel

            ScrollView {
                VStack(spacing: 10) {
                    if presentation.events.isEmpty {
                        emptyState
                    } else {
                        ForEach(presentation.events.reversed()) { event in
                            eventRow(
                                event: event,
                                isLatest: event.id == presentation.latestEvent?.id,
                                isSelected: event.id == selectedEventID
                            )
                                .contentShape(Rectangle())
                                .onTapGesture {
                                    selectEvent(event)
                                }
                        }
                    }
                }
            }
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private var filterPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(appState.locale == .zhCN ? "事件筛选" : "Event Filter")
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(.secondary)

            TextField(AppStrings.Timeline.eventTypeLabel(appState.locale), text: $filterDraft.eventType)
                .textFieldStyle(.roundedBorder)

            TextField(AppStrings.Timeline.actorLabel(appState.locale), text: $filterDraft.actor)
                .textFieldStyle(.roundedBorder)

            TextField(AppStrings.Timeline.subjectLabel(appState.locale), text: $filterDraft.subjectID)
                .textFieldStyle(.roundedBorder)

            TextField(AppStrings.Timeline.sinceLabel(appState.locale), text: $filterDraft.since)
                .textFieldStyle(.roundedBorder)

            HStack(spacing: 8) {
                Button {
                    Task {
                        await refreshEventsUsingCurrentFilter()
                    }
                } label: {
                    Label(
                        AppStrings.Timeline.applyFilterButton(appState.locale),
                        systemImage: "line.3.horizontal.decrease.circle"
                    )
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(appState.isPreviewFixture || isRefreshingEvents)

                Button {
                    filterDraft = TimelineEventFilterDraft()
                    Task {
                        await refreshEventsUsingCurrentFilter()
                    }
                } label: {
                    Label(AppStrings.Timeline.clearFilterButton(appState.locale), systemImage: "xmark.circle")
                }
                .buttonStyle(.bordered)
                .disabled(appState.isPreviewFixture || isRefreshingEvents || !filterDraft.hasFilters)
            }
            .controlSize(.small)
        }
        .padding(10)
        .background(Color(nsColor: .windowBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func refreshEventsUsingCurrentFilter() async {
        guard !appState.isPreviewFixture, !isRefreshingEvents else {
            return
        }
        let draft = filterDraft
        isRefreshingEvents = true
        await daemonController.refreshEvents(
            eventType: draft.eventTypeQueryValue,
            subjectID: draft.subjectIDQueryValue,
            actor: draft.actorQueryValue,
            since: draft.sinceQueryValue,
            limit: 50
        )
        isRefreshingEvents = false
    }

    private func selectEvent(_ event: TimelineEventPresentation) {
        selectedEventID = event.id
        guard !appState.isPreviewFixture,
              let command = TimelineEventSelectionCommand(event: event) else {
            return
        }

        Task {
            await daemonController.loadEvent(eventID: command.eventID)
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

    private func causalChainPanel(presentation: TimelineDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "最近因果链" : "Recent Causal Chain") {
            if presentation.causalChain.isEmpty {
                emptyState
            } else {
                VStack(spacing: 10) {
                    ForEach(presentation.causalChain) { step in
                        HStack(alignment: .top, spacing: 12) {
                            Text("\(step.order)")
                                .font(.system(size: 12, weight: .bold))
                                .foregroundStyle(.white)
                                .frame(width: 22, height: 22)
                                .background(color(for: step.type))
                                .clipShape(Circle())

                            VStack(alignment: .leading, spacing: 6) {
                                HStack(spacing: 8) {
                                    Label(step.type, systemImage: iconName(for: step.type))
                                        .font(.system(size: 13, weight: .semibold))
                                        .lineLimit(1)
                                    Spacer()
                                    Text(step.timestamp)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                HStack(spacing: 18) {
                                    compactDetail(label: AppStrings.Timeline.actorLabel(appState.locale), value: step.actor)
                                    compactDetail(label: AppStrings.Timeline.subjectLabel(appState.locale), value: step.subjectID)
                                }
                                if !step.payloadSummary.isEmpty {
                                    Text(step.payloadSummary)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(2)
                                }
                            }
                        }
                        .padding(12)
                        .background(Color(nsColor: .controlBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                }
            }
        }
    }

    private func actorDistributionPanel(presentation: TimelineDashboardPresentation) -> some View {
        panel(title: appState.locale == .zhCN ? "执行者分布" : "Actor Distribution") {
            if presentation.actorBuckets.isEmpty {
                emptyState
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(presentation.actorBuckets) { bucket in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(bucket.actor)
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
                                    .fill(Color.accentColor.opacity(0.65))
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

    private func eventRow(event: TimelineEventPresentation, isLatest: Bool, isSelected: Bool = false) -> some View {
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
                        .lineLimit(1)
                        .truncationMode(.middle)
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
        .background(isSelected ? Color.accentColor.opacity(0.10) : Color(nsColor: .controlBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(isSelected ? Color.accentColor.opacity(0.65) : Color.secondary.opacity(0.13), lineWidth: 1)
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
                .truncationMode(.middle)
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

#endif
