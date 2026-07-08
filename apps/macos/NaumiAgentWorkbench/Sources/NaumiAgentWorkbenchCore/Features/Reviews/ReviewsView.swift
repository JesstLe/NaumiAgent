import SwiftUI

/// Reviews visual prototype aligned with the human approval reference screen.
public struct ReviewsView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    @State private var selectedReviewID: String?
    @State private var selectedValidationRunID: String?
    @State private var selectedTab = "Details"
    @State private var validationDraft = ValidationRunDraft(
        commandLine: "pytest tests/unit/test_workbench_market.py -q"
    )
    @State private var isRunningValidation = false
    @State private var approvalDraft = ApprovalResolveDraft()
    @State private var isResolvingApproval = false
    @State private var isConvertingToProposal = false
    @State private var isKeepingWorktree = false

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        let presentation = ReviewsDesignPresentation(
            approvals: appState.approvals,
            validationRuns: appState.validationRuns,
            snapshot: appState.snapshot,
            policy: RealDataPolicy(isPreviewFixture: appState.isPreviewFixture)
        )
        let selected = selectedReview(presentation)
        let layout = WorkbenchScaledPageLayout.reviews

        ScaledWorkbenchPage(layout: layout) {
            reviewLayoutContent(presentation: presentation, selected: selected)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            if let selected {
                selectedReviewID = selected.id
                validationDraft = presentation.defaultValidationDraft(for: selected)
            } else {
                selectedReviewID = nil
            }
        }
    }

    private func reviewLayoutContent(
        presentation: ReviewsDesignPresentation,
        selected: ReviewDesignItem?
    ) -> some View {
        HStack(alignment: .top, spacing: 0) {
            reviewQueueRail(presentation)
                .frame(
                    width: 304,
                    height: WorkbenchScaledPageLayout.reviews.baseHeight,
                    alignment: .topLeading
                )
            Divider()
            reviewMain(presentation: presentation, selected: selected)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            Divider()
            reviewInspector(presentation: presentation, selected: selected)
                .frame(
                    width: 340,
                    height: WorkbenchScaledPageLayout.reviews.baseHeight,
                    alignment: .topLeading
                )
        }
        .frame(
            width: WorkbenchScaledPageLayout.reviews.baseWidth,
            height: WorkbenchScaledPageLayout.reviews.baseHeight,
            alignment: .topLeading
        )
    }

    private func selectedReview(_ presentation: ReviewsDesignPresentation) -> ReviewDesignItem? {
        let queueSelected = presentation.reviewQueues
            .flatMap(\.items)
            .first { $0.id == selectedReviewID }
            ?? presentation.selectedReview

        guard let queueSelected else {
            return nil
        }

        guard let loadedApproval = appState.selectedApproval,
              loadedApproval.id == queueSelected.id else {
            return queueSelected
        }

        return selectedReviewPresentation(approval: loadedApproval, fallback: queueSelected)
    }

    private func selectedReviewPresentation(
        approval: ApprovalDTO,
        fallback: ReviewDesignItem
    ) -> ReviewDesignItem {
        ReviewDesignItem(
            id: approval.id,
            taskID: approval.taskID.isEmpty ? fallback.taskID : approval.taskID,
            title: approval.title.isEmpty ? fallback.title : approval.title,
            number: fallback.number,
            agent: approval.requester.isEmpty ? fallback.agent : approval.requester,
            worktree: fallback.worktree,
            time: approval.updatedAt.isEmpty ? fallback.time : String(approval.updatedAt.suffix(5)),
            risk: fallback.risk,
            tone: reviewTone(forApprovalState: approval.state, fallback: fallback.tone)
        )
    }

    private func selectedValidationChecks(_ checks: [ReviewDesignCheck]) -> [ReviewDesignCheck] {
        guard let selectedRun = appState.selectedValidationRun,
              selectedRun.id == selectedValidationRunID else {
            return checks
        }

        return checks.map { check in
            guard check.id == selectedRun.id else { return check }
            return validationCheckPresentation(run: selectedRun, fallback: check)
        }
    }

    private func validationCheckPresentation(
        run: ValidationRunDTO,
        fallback: ReviewDesignCheck
    ) -> ReviewDesignCheck {
        ReviewDesignCheck(
            runID: run.id,
            name: run.command.isEmpty ? fallback.name : run.command.joined(separator: " "),
            status: run.status.isEmpty ? fallback.status : run.status,
            time: run.completedAt.isEmpty ? fallback.time : String(run.completedAt.suffix(5))
        )
    }

    private func reviewQueueRail(_ presentation: ReviewsDesignPresentation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Text(AppStrings.Reviews.title(appState.locale))
                    .font(.system(size: 17, weight: .semibold))
                    .lineLimit(1)
                Spacer()
                Image(systemName: "magnifyingglass")
                Image(systemName: "line.3.horizontal.decrease")
                Image(systemName: "square.and.pencil")
            }
            .foregroundStyle(.primary)
            .frame(height: 34, alignment: .center)
            .padding(.top, 6)

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if presentation.reviewQueues.isEmpty {
                        VStack(spacing: 8) {
                            Image(systemName: "checkmark.seal")
                                .font(.system(size: 26))
                                .foregroundStyle(.secondary)
                            Text(AppStrings.Reviews.emptyApprovals(appState.locale))
                                .font(.caption)
                                .fontWeight(.medium)
                            Text(AppStrings.Reviews.emptyApprovalHint(appState.locale))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 24)
                    }

                    ForEach(presentation.reviewQueues) { queue in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text(queue.title)
                                    .font(.caption)
                                    .fontWeight(.semibold)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Text("\(queue.badge)")
                                    .font(.caption2)
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 3)
                                    .background(color(forTone: queue.items.first?.tone ?? "gray").opacity(0.12))
                                    .foregroundStyle(color(forTone: queue.items.first?.tone ?? "gray"))
                                    .clipShape(Capsule())
                            }

                            ForEach(queue.items) { item in
                                reviewQueueCard(item)
                                    .contentShape(Rectangle())
                                    .onTapGesture {
                                        selectReview(item, presentation: presentation)
                                    }
                            }
                        }
                    }
                }
            }
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func reviewQueueCard(_ item: ReviewDesignItem) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: item.id == selectedReviewID ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(item.id == selectedReviewID ? .blue : .secondary)
                Text(item.title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Spacer()
                Text(item.risk)
                    .font(.caption)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(color(forTone: item.tone).opacity(0.12))
                    .foregroundStyle(color(forTone: item.tone))
                    .clipShape(RoundedRectangle(cornerRadius: 5))
            }
            HStack {
                Image(systemName: "arrow.triangle.branch")
                    .foregroundStyle(.secondary)
                Text("#\(item.number) by \(item.agent)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            HStack {
                Text(appState.locale == .zhCN ? "工作区：" : "Worktree:")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(item.worktree)
                    .font(.caption)
                    .lineLimit(1)
                Spacer()
                Text(item.time)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(11)
        .background(item.id == selectedReviewID ? Color.accentColor.opacity(0.10) : Color(nsColor: .windowBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(item.id == selectedReviewID ? Color.accentColor : Color.secondary.opacity(0.12), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func selectReview(
        _ review: ReviewDesignItem,
        presentation: ReviewsDesignPresentation
    ) {
        selectedReviewID = review.id
        selectedValidationRunID = nil
        validationDraft = presentation.defaultValidationDraft(for: review)
        guard !appState.isPreviewFixture,
              let command = ReviewSelectionCommand(review: review) else {
            return
        }

        Task {
            await daemonController.loadApproval(approvalID: command.approvalID)
        }
    }

    private func reviewMain(presentation: ReviewsDesignPresentation, selected: ReviewDesignItem?) -> some View {
        let validationChecks = selectedValidationChecks(presentation.validationChecks)

        return VStack(spacing: 0) {
            if let selected {
                metaStrip(selected)
                validationSummary(validationChecks)
                    .padding(14)
                HStack(spacing: 0) {
                    filesChanged(presentation.fileChanges)
                        .frame(width: 192)
                    Divider()
                    diffViewer(presentation.diffRows)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                Divider()
                reviewTimeline(presentation.timeline)
                    .frame(height: 168)
            } else {
                reviewsEmptyState
            }
        }
    }

    private var reviewsEmptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "checkmark.seal")
                .font(.system(size: 34))
                .foregroundStyle(.secondary)
            Text(AppStrings.Reviews.emptyApprovals(appState.locale))
                .font(.system(size: 16, weight: .semibold))
            Text(AppStrings.Reviews.emptyApprovalHint(appState.locale))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func metaStrip(_ selected: ReviewDesignItem) -> some View {
        HStack(spacing: 16) {
            metaItem(icon: "clock", text: appState.locale == .zhCN ? "打开：2026-06-27 09:28" : "Opened: Jun 27, 2026 09:28")
            metaItem(icon: "arrow.clockwise", text: appState.locale == .zhCN ? "更新：2026-06-27 09:36" : "Updated: Jun 27, 2026 09:36")
            metaItem(icon: "folder", text: appState.locale == .zhCN ? "工作区：\(selected.worktree)" : "Worktree: \(selected.worktree)")
            metaItem(icon: "arrow.triangle.branch", text: appState.locale == .zhCN ? "基线：main" : "Base: main")
            metaItem(icon: "arrow.left.arrow.right", text: appState.locale == .zhCN ? "对比：issue-3-market" : "Compare: issue-3-market")
            Spacer()
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func metaItem(icon: String, text: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon)
                .foregroundStyle(.secondary)
            Text(text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.82)
        }
    }

    private func validationSummary(_ checks: [ReviewDesignCheck]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text(appState.locale == .zhCN ? "验证摘要" : "Validation Summary")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Text("2 / 2 passed")
                    .font(.caption)
                    .fontWeight(.semibold)
            }

            HStack(spacing: 0) {
                ForEach(checks) { check in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(check.name)
                            .font(.caption)
                            .lineLimit(1)
                        HStack {
                            Circle()
                                .fill(check.status == "passed" ? .green : .secondary)
                                .frame(width: 6, height: 6)
                            Text(check.status)
                                .font(.caption2)
                                .foregroundStyle(check.status == "passed" ? .green : .secondary)
                            Spacer()
                            Text(check.time)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    if check.id != checks.last?.id {
                        Divider()
                    }
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .padding(12)
        .background(Color.green.opacity(0.06))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.green.opacity(0.25), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func filesChanged(_ files: [ReviewDesignFile]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(appState.locale == .zhCN ? "变更文件 (6)" : "FILES CHANGED (6)")
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 10)
                .padding(.top, 12)

            ForEach(files) { file in
                HStack(spacing: 8) {
                    Image(systemName: file.name.contains(".py") ? "doc.text" : "folder")
                        .foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(file.name)
                            .font(.caption)
                            .lineLimit(1)
                        if file.selected {
                            Text(file.path)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    Spacer()
                    Text(file.status)
                        .font(.caption2)
                        .foregroundStyle(statusTone(file.status))
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(file.selected ? Color.accentColor.opacity(0.12) : Color.clear)
            }

            Spacer()
            HStack(spacing: 10) {
                legend("A", "Added", .green)
                legend("M", "Modified", .orange)
                legend("D", "Deleted", .red)
            }
            .padding(10)
        }
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func legend(_ code: String, _ text: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Text(code)
                .font(.caption2)
                .fontWeight(.bold)
                .foregroundStyle(color)
            Text(text)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private func diffViewer(_ rows: [ReviewDesignDiffRow]) -> some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "doc.text.magnifyingglass")
                Text("src/naumi_agent/workbench/market.py")
                    .font(.caption)
                    .fontWeight(.semibold)
                Spacer()
                Picker("", selection: .constant("Side-by-side")) {
                    Text(appState.locale == .zhCN ? "并排" : "Side-by-side").tag("Side-by-side")
                    Text(appState.locale == .zhCN ? "统一" : "Unified").tag("Unified")
                }
                .labelsHidden()
                .frame(width: 150)
                Image(systemName: "square.on.square")
                Image(systemName: "gearshape")
                Image(systemName: "ellipsis")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            HStack(spacing: 0) {
                Text("main")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                Divider()
                Text(appState.locale == .zhCN ? "issue-3-market（当前）" : "issue-3-market (Current)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
            }
            .frame(height: 30)
            .background(Color.secondary.opacity(0.05))

            ScrollView {
                VStack(spacing: 0) {
                    ForEach(rows) { row in
                        HStack(spacing: 0) {
                            diffLine(row.number, row.old, tone: row.tone == "changed" ? "removed" : "normal")
                            Divider()
                            diffLine(row.number, row.new, tone: row.tone)
                        }
                    }
                }
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
    }

    private func diffLine(_ number: Int, _ text: String, tone: String) -> some View {
        HStack(spacing: 8) {
            Text("\(number)")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 34, alignment: .trailing)
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .lineLimit(1)
            Spacer()
        }
        .padding(.horizontal, 8)
        .frame(height: 28)
        .background(diffBackground(tone))
    }

    private func reviewTimeline(_ events: [ReviewDesignTimelineEvent]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(appState.locale == .zhCN ? "审查时间线" : "REVIEW TIMELINE")
                    .font(.caption)
                    .fontWeight(.semibold)
                Picker("", selection: .constant("all")) {
                    Text(appState.locale == .zhCN ? "全部事件" : "All Events").tag("all")
                }
                .labelsHidden()
                .frame(width: 118)
                Spacer()
            }

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 5) {
                GridRow {
                    timelineHeader(appState.locale == .zhCN ? "时间" : "Time")
                    timelineHeader(appState.locale == .zhCN ? "事件" : "Event")
                    timelineHeader(appState.locale == .zhCN ? "执行者" : "Actor")
                    timelineHeader(appState.locale == .zhCN ? "详情" : "Details", isDetails: true)
                }
                ForEach(events) { event in
                    GridRow {
                        Text(event.time).font(.caption2).foregroundStyle(.secondary)
                        HStack {
                            Circle().fill(color(forTone: event.tone)).frame(width: 7, height: 7)
                            Text(event.event).font(.caption2).foregroundStyle(color(forTone: event.tone))
                        }
                        Text(event.actor).font(.caption2).foregroundStyle(.blue)
                        Text(event.detail).font(.caption2).lineLimit(1)
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func timelineHeader(_ text: String, isDetails: Bool = false) -> some View {
        Text(text)
            .font(.caption2)
            .foregroundStyle(.secondary)
            .frame(minWidth: isDetails ? 330 : 92, alignment: .leading)
    }

    private func reviewInspector(presentation: ReviewsDesignPresentation, selected: ReviewDesignItem?) -> some View {
        let validationChecks = selectedValidationChecks(presentation.validationChecks)

        return VStack(alignment: .leading, spacing: 14) {
            if let selected {
                reviewInspectorContent(
                    presentation: presentation,
                    selected: selected,
                    validationChecks: validationChecks
                )
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "doc.text.magnifyingglass")
                        .font(.system(size: 28))
                        .foregroundStyle(.secondary)
                    Text(AppStrings.Reviews.selectApprovalPrompt(appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            Spacer(minLength: 0)
        }
        .padding(14)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func reviewInspectorContent(
        presentation: ReviewsDesignPresentation,
        selected: ReviewDesignItem,
        validationChecks: [ReviewDesignCheck]
    ) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Picker("", selection: $selectedTab) {
                Text(appState.locale == .zhCN ? "详情" : "Details").tag("Details")
                Text(appState.locale == .zhCN ? "检查" : "Checks").tag("Checks")
            }
            .pickerStyle(.segmented)

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    inspectorCard(title: appState.locale == .zhCN ? "风险分析" : "Risk Analysis") {
                        HStack {
                            Text(appState.locale == .zhCN ? "为什么是高风险？" : "Why high risk?")
                                .font(.caption)
                                .fontWeight(.semibold)
                            Spacer()
                            Text(selected.risk)
                                .font(.caption)
                                .fontWeight(.semibold)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(color(forTone: selected.tone).opacity(0.12))
                                .foregroundStyle(color(forTone: selected.tone))
                                .clipShape(RoundedRectangle(cornerRadius: 5))
                        }
                        bullet(appState.locale == .zhCN ? "影响任务市场的租约语义" : "Affects lease semantics in the task market")
                        bullet(appState.locale == .zhCN ? "可能影响多智能体并发" : "Potential impact on multi-agent concurrency")
                        bullet(appState.locale == .zhCN ? "触及核心后端服务" : "Touches core backend service")
                        Text(appState.locale == .zhCN ? "查看风险拆解 ->" : "View risk breakdown ->")
                            .font(.caption)
                            .foregroundStyle(.blue)
                    }

                    inspectorCard(title: appState.locale == .zhCN ? "上下文健康" : "Context Health") {
                        HStack {
                            Text(appState.locale == .zhCN ? "引用文件均为最新且一致。" : "All referenced files are current and consistent.")
                                .font(.caption)
                            Spacer()
                            StatusBadge(text: "Good", color: .green)
                        }
                    }

                    inspectorCard(title: appState.locale == .zhCN ? "验证运行" : "Validation Runs") {
                        ForEach(validationChecks) { check in
                            validationRunRow(check)
                                .contentShape(Rectangle())
                                .onTapGesture {
                                    selectValidationRun(check)
                                }
                        }
                        Text(appState.locale == .zhCN ? "查看完整验证报告 ->" : "View full validation report ->")
                            .font(.caption)
                            .foregroundStyle(.blue)
                        Divider()
                        validationCommandPanel(selected: selected)
                    }

                    inspectorCard(title: appState.locale == .zhCN ? "智能体审查" : "Agent Reviews") {
                        ForEach(presentation.agentReviews) { note in
                            HStack(alignment: .top, spacing: 9) {
                                Text(note.initials)
                                    .font(.caption)
                                    .fontWeight(.bold)
                                    .frame(width: 28, height: 28)
                                    .background(Color.purple.opacity(0.16))
                                    .foregroundStyle(.purple)
                                    .clipShape(Circle())
                                VStack(alignment: .leading, spacing: 3) {
                                    HStack {
                                        Text(note.agent).font(.caption).fontWeight(.semibold)
                                        Spacer()
                                        Text(note.time).font(.caption2).foregroundStyle(.secondary)
                                    }
                                    Text(note.body)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }

                    inspectorCard(title: appState.locale == .zhCN ? "审批决策" : "Approval Decision") {
                        Text(appState.locale == .zhCN ? "Required: 人工审批" : "Required: Human approval")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        TextField(AppStrings.Reviews.actorLabel(appState.locale), text: $approvalDraft.actor)
                            .textFieldStyle(.roundedBorder)
                        TextField(AppStrings.Reviews.decisionNoteLabel(appState.locale), text: $approvalDraft.decisionNote)
                            .textFieldStyle(.roundedBorder)
                        Button {
                            resolveApproval(selected, state: .approved)
                        } label: {
                            Label(
                                isResolvingApproval
                                    ? AppStrings.Reviews.processingLabel(appState.locale)
                                    : AppStrings.Reviews.approveButton(appState.locale),
                                systemImage: "checkmark"
                            )
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.green)
                        .disabled(!approvalDraft.canResolve || isResolvingApproval)

                        Button {
                            resolveApproval(selected, state: .rejected)
                        } label: {
                            Label(
                                isResolvingApproval
                                    ? AppStrings.Reviews.processingLabel(appState.locale)
                                    : (appState.locale == .zhCN ? "请求修改" : "Request Changes"),
                                systemImage: "arrow.clockwise"
                            )
                                .frame(maxWidth: .infinity)
                        }
                            .buttonStyle(.borderedProminent)
                            .tint(.orange)
                            .disabled(!approvalDraft.canResolve || isResolvingApproval)

                        Button {
                            convertToProposal(selected)
                        } label: {
                            Label(
                                isConvertingToProposal
                                    ? AppStrings.Reviews.convertingToProposalLabel(appState.locale)
                                    : AppStrings.Reviews.convertToProposalButton(appState.locale),
                                systemImage: "doc.badge.plus"
                            )
                                .frame(maxWidth: .infinity)
                        }
                            .buttonStyle(.bordered)
                            .disabled(proposalConversionCommand(for: selected) == nil || isConvertingToProposal)
                        Button {
                            keepWorktree(selected)
                        } label: {
                            Label(
                                isKeepingWorktree
                                    ? AppStrings.Reviews.keepingWorktreeLabel(appState.locale)
                                    : AppStrings.Reviews.keepWorktreeButton(appState.locale),
                                systemImage: "folder.badge.gearshape"
                            )
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .disabled(worktreeKeepCommand(for: selected) == nil || isKeepingWorktree)
                    }
                }
                .padding(.bottom, 4)
            }
            .scrollIndicators(.hidden)
        }
    }

    private func validationRunRow(_ check: ReviewDesignCheck) -> some View {
        let isSelected = check.id == selectedValidationRunID

        return HStack {
            Circle()
                .fill(check.status == "passed" ? .green : .secondary)
                .frame(width: 7, height: 7)
            Text(check.name)
                .font(.caption)
                .lineLimit(1)
            Spacer()
            Text(check.status)
                .font(.caption2)
                .foregroundStyle(check.status == "passed" ? .green : .secondary)
            Text(check.time)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 6)
        .background(isSelected ? Color.accentColor.opacity(0.10) : Color.clear)
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(isSelected ? Color.accentColor.opacity(0.55) : Color.clear, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func selectValidationRun(_ check: ReviewDesignCheck) {
        guard let command = ReviewValidationSelectionCommand(check: check) else {
            selectedValidationRunID = nil
            return
        }

        selectedValidationRunID = command.runID
        guard !appState.isPreviewFixture else {
            return
        }

        Task {
            await daemonController.loadValidationRun(runID: command.runID)
        }
    }

    private func validationCommandPanel(selected: ReviewDesignItem) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(AppStrings.Reviews.runValidationSectionTitle(appState.locale))
                .font(.caption)
                .fontWeight(.semibold)

            TextField(AppStrings.Reviews.taskIDLabel(appState.locale), text: $validationDraft.taskID)
                .textFieldStyle(.roundedBorder)
            TextField(AppStrings.Reviews.actorLabel(appState.locale), text: $validationDraft.actor)
                .textFieldStyle(.roundedBorder)
            TextField(AppStrings.Reviews.commandLabel(appState.locale), text: $validationDraft.commandLine)
                .textFieldStyle(.roundedBorder)
            TextField(AppStrings.Reviews.cwdLabel(appState.locale), text: $validationDraft.cwd)
                .textFieldStyle(.roundedBorder)

            Button {
                runValidation()
            } label: {
                Label(
                    isRunningValidation
                        ? AppStrings.Reviews.processingLabel(appState.locale)
                        : AppStrings.Reviews.runButton(appState.locale),
                    systemImage: "play.circle"
                )
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!validationDraft.canSubmit || isRunningValidation)
            .onChange(of: selected.id) { _, _ in
                validationDraft.taskID = selected.taskID
            }
        }
    }

    private func runValidation() {
        guard validationDraft.canSubmit, !isRunningValidation else { return }
        let draft = validationDraft
        isRunningValidation = true
        Task {
            await daemonController.runValidation(
                taskID: draft.trimmedTaskID,
                actor: draft.trimmedActor,
                argv: draft.argv,
                cwd: draft.normalizedCWD
            )
            isRunningValidation = false
        }
    }

    private func resolveApproval(_ selected: ReviewDesignItem, state: ReviewApprovalResolutionState) {
        guard let command = ReviewApprovalResolutionCommand(
            review: selected,
            draft: approvalDraft,
            state: state
        ), !isResolvingApproval else {
            return
        }

        isResolvingApproval = true
        Task {
            await daemonController.resolveApproval(
                approvalID: command.approvalID,
                actor: command.actor,
                state: command.state,
                decisionNote: command.decisionNote
            )
            isResolvingApproval = false
            if appState.lastError == nil {
                approvalDraft.decisionNote = ""
            }
        }
    }

    private func convertToProposal(_ selected: ReviewDesignItem) {
        guard let command = proposalConversionCommand(for: selected), !isConvertingToProposal else {
            return
        }

        isConvertingToProposal = true
        Task {
            await daemonController.createDecision(
                missionID: command.missionID,
                actor: command.actor,
                kind: command.kind,
                title: command.title,
                content: command.content
            )
            isConvertingToProposal = false
        }
    }

    private func proposalConversionCommand(for selected: ReviewDesignItem) -> ReviewProposalConversionCommand? {
        ReviewProposalConversionCommand(
            review: selected,
            missionID: appState.snapshot?.missions.first?.id ?? "",
            actor: approvalDraft.trimmedActor,
            decisionNote: approvalDraft.trimmedDecisionNote,
            locale: appState.locale
        )
    }

    private func keepWorktree(_ selected: ReviewDesignItem) {
        guard let command = worktreeKeepCommand(for: selected), !isKeepingWorktree else {
            return
        }

        isKeepingWorktree = true
        Task {
            await daemonController.keepWorktree(
                name: command.name,
                actor: command.actor,
                reason: command.reason
            )
            isKeepingWorktree = false
        }
    }

    private func worktreeKeepCommand(for selected: ReviewDesignItem) -> ReviewWorktreeKeepCommand? {
        ReviewWorktreeKeepCommand(
            review: selected,
            worktrees: appState.worktrees,
            actor: approvalDraft.trimmedActor,
            locale: appState.locale
        )
    }

    private func inspectorCard<Content: View>(
        title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title)
                .font(.system(size: 13, weight: .semibold))
            content()
        }
        .padding(12)
        .background(Color(nsColor: .controlBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.secondary.opacity(0.14), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Text("•")
            Text(text)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private func statusTone(_ status: String) -> Color {
        switch status {
        case "A":
            return .green
        case "M":
            return .orange
        case "D":
            return .red
        default:
            return .secondary
        }
    }

    private func diffBackground(_ tone: String) -> Color {
        switch tone {
        case "added":
            return Color.green.opacity(0.08)
        case "changed", "removed":
            return Color.red.opacity(0.07)
        default:
            return Color.clear
        }
    }

    private func reviewTone(forApprovalState state: String, fallback: String) -> String {
        switch state.lowercased() {
        case "approved":
            return "green"
        case "rejected":
            return "orange"
        case "waiting":
            return fallback
        default:
            return fallback
        }
    }

    private func color(forTone tone: String) -> Color {
        switch tone {
        case "red":
            return .red
        case "orange":
            return .orange
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
