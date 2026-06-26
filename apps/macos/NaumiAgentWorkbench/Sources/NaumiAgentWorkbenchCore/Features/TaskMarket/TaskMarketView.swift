import SwiftUI

/// Task Market page with minimal claim/release actions.
///
/// Issue selection drives the inspector. The inspector exposes the command form
/// used to claim an open issue or release an existing lease. All mutations go
/// through ``DaemonController`` and the REST API; the local snapshot is only
/// updated by a fresh fetch after a successful write.
public struct TaskMarketView: View {
    @Bindable public var appState: AppState
    public let daemonController: DaemonController

    @State private var selectedTaskID: String? = nil
    @State private var agentID: String = "local-agent"
    @State private var durationMinutes: Int = 30
    @State private var worktreeName: String = ""
    @State private var isProcessing: Bool = false

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                if let snapshot = appState.snapshot {
                    marketContent(snapshot: snapshot)
                } else {
                    emptyState(text: AppStrings.TaskMarket.noSnapshot(appState.locale))
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(AppStrings.TaskMarket.title(appState.locale))
    }

    // MARK: - Header

    private var header: some View {
        Text(AppStrings.TaskMarket.title(appState.locale))
            .font(.largeTitle)
            .fontWeight(.bold)
    }

    // MARK: - Market Content

    private func marketContent(snapshot: WorkbenchSnapshotDTO) -> some View {
        let presentation = TaskMarketSnapshotPresentation(snapshot: snapshot)
        let selectedRow = presentation.rows.first { $0.taskID == selectedTaskID }
            ?? presentation.rows.first

        return VStack(alignment: .leading, spacing: 20) {
            summaryStrip(summary: presentation.summary)
            if presentation.rows.isEmpty {
                emptyState(text: AppStrings.TaskMarket.emptyIssues(appState.locale))
            } else {
                HSplitView {
                    issueTable(rows: presentation.rows)
                        .frame(minWidth: 360)
                    inspector(row: selectedRow)
                        .frame(minWidth: 240)
                }
                .frame(minHeight: 320)
            }
        }
    }

    // MARK: - Summary Strip

    private func summaryStrip(summary: TaskMarketSummary) -> some View {
        HStack(spacing: 12) {
            summaryCard(
                title: AppStrings.TaskMarket.totalIssues(appState.locale),
                count: summary.totalIssues,
                color: .blue
            )
            summaryCard(
                title: AppStrings.TaskMarket.openIssues(appState.locale),
                count: summary.openIssues,
                color: .green
            )
            summaryCard(
                title: AppStrings.TaskMarket.claimedIssues(appState.locale),
                count: summary.claimedIssues,
                color: .purple
            )
            summaryCard(
                title: AppStrings.TaskMarket.blockedIssues(appState.locale),
                count: summary.blockedIssues,
                color: .orange
            )
            summaryCard(
                title: AppStrings.TaskMarket.approvalRequiredIssues(appState.locale),
                count: summary.approvalRequiredIssues,
                color: .red
            )
        }
    }

    private func summaryCard(title: String, count: Int, color: Color) -> some View {
        VStack(spacing: 6) {
            Text("\(count)")
                .font(.system(size: 28, weight: .bold))
                .foregroundStyle(color)
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 12)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Issue Table

    private func issueTable(rows: [TaskMarketIssueRow]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            tableHeader
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.secondary.opacity(0.06))

            ForEach(rows, id: \.taskID) { row in
                tableRow(row: row)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        selectedTaskID = row.taskID
                    }
                    .background(
                        selectedTaskID == row.taskID
                            ? Color.accentColor.opacity(0.12)
                            : Color.clear
                    )
                if row.taskID != rows.last?.taskID {
                    Divider()
                        .padding(.horizontal, 12)
                }
            }
        }
        .background(Color.secondary.opacity(0.04))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .onAppear {
            if selectedTaskID == nil, let first = rows.first {
                selectedTaskID = first.taskID
            }
        }
    }

    private var tableHeader: some View {
        HStack(spacing: 8) {
            Text(AppStrings.TaskMarket.columnIssue(appState.locale))
                .frame(minWidth: 120, alignment: .leading)
            Text(AppStrings.TaskMarket.columnParallelMode(appState.locale))
                .frame(width: 80, alignment: .leading)
            Text(AppStrings.TaskMarket.columnRisk(appState.locale))
                .frame(width: 60, alignment: .leading)
            Text(AppStrings.TaskMarket.columnDependencies(appState.locale))
                .frame(width: 60, alignment: .leading)
            Text(AppStrings.TaskMarket.columnBids(appState.locale))
                .frame(width: 50, alignment: .leading)
            Text(AppStrings.TaskMarket.columnLease(appState.locale))
                .frame(width: 70, alignment: .leading)
            Text(AppStrings.TaskMarket.columnWorktree(appState.locale))
                .frame(minWidth: 80, alignment: .leading)
            Spacer()
            Text(AppStrings.TaskMarket.columnStatus(appState.locale))
                .frame(width: 80, alignment: .trailing)
        }
        .font(.caption)
        .fontWeight(.semibold)
        .foregroundStyle(.secondary)
    }

    private func tableRow(row: TaskMarketIssueRow) -> some View {
        HStack(spacing: 8) {
            Text(row.subject)
                .font(.body)
                .fontWeight(.medium)
                .lineLimit(1)
                .frame(minWidth: 120, alignment: .leading)

            Text(row.parallelMode)
                .frame(width: 80, alignment: .leading)

            Text(row.riskLevel)
                .frame(width: 60, alignment: .leading)
                .foregroundStyle(riskColor(for: row.riskLevel))

            Text("\(row.dependencyCount)")
                .frame(width: 60, alignment: .leading)

            Text("\(row.bidCount)")
                .frame(width: 50, alignment: .leading)
                .foregroundStyle(.secondary)

            Text(leaseText(for: row.leaseState))
                .frame(width: 70, alignment: .leading)
                .font(.caption)
                .fontWeight(.medium)
                .foregroundStyle(leaseColor(for: row.leaseState))

            Text(worktreeText(for: row.worktreeLabel))
                .font(.caption)
                .lineLimit(1)
                .frame(minWidth: 80, alignment: .leading)
                .foregroundStyle(row.worktreeLabel == nil ? .secondary : .primary)

            Spacer()

            StatusBadge(text: row.status, color: statusColor(for: row.status))
                .frame(width: 80, alignment: .trailing)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    // MARK: - Inspector

    private func inspector(row: TaskMarketIssueRow?) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(AppStrings.TaskMarket.inspectorTitle(appState.locale))
                .font(.headline)

            if let row = row {
                VStack(alignment: .leading, spacing: 12) {
                    inspectorItem(
                        label: AppStrings.TaskMarket.columnIssue(appState.locale),
                        value: row.subject
                    )
                    inspectorItem(
                        label: AppStrings.TaskMarket.columnStatus(appState.locale),
                        value: row.status
                    )
                    inspectorItem(
                        label: AppStrings.TaskMarket.columnRisk(appState.locale),
                        value: row.riskLevel
                    )
                    inspectorItem(
                        label: AppStrings.TaskMarket.columnParallelMode(appState.locale),
                        value: row.parallelMode
                    )
                    inspectorItem(
                        label: AppStrings.Dashboard.ownerLabel(appState.locale),
                        value: row.ownerLabel ?? AppStrings.TaskMarket.ownerPlaceholder(appState.locale)
                    )
                    if let agentID = row.leaseAgentID {
                        inspectorItem(
                            label: AppStrings.TaskMarket.leaseAgentLabel(appState.locale),
                            value: agentID
                        )
                    }
                    if let expiresAt = row.leaseExpiresAt {
                        inspectorItem(
                            label: AppStrings.TaskMarket.leaseExpiresAtLabel(appState.locale),
                            value: expiresAt
                        )
                    }
                    inspectorItem(
                        label: AppStrings.TaskMarket.columnWorktree(appState.locale),
                        value: worktreeText(for: row.worktreeLabel)
                    )
                    inspectorItem(
                        label: AppStrings.TaskMarket.acceptanceCriteriaTitle(appState.locale),
                        value: "\(row.acceptanceCriteriaCount)"
                    )

                    if row.requiresHumanApproval {
                        HStack(spacing: 6) {
                            Image(systemName: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                            Text(AppStrings.TaskMarket.requiresApprovalLabel(appState.locale))
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }
                    }

                    HStack(spacing: 6) {
                        Image(systemName: "info.circle")
                            .foregroundStyle(.secondary)
                        Text(AppStrings.TaskMarket.bidsNotAvailable(appState.locale))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    commandSection(row: row)
                }
            } else {
                Text(AppStrings.TaskMarket.emptyIssues(appState.locale))
                    .foregroundStyle(.secondary)
            }

            Spacer()
        }
        .padding()
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func inspectorItem(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.body)
                .fontWeight(.medium)
        }
    }

    // MARK: - Commands

    private func commandSection(row: TaskMarketIssueRow) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppStrings.TaskMarket.commandSectionTitle(appState.locale))
                .font(.headline)

            TextField(AppStrings.TaskMarket.agentIDLabel(appState.locale), text: $agentID)
                .textFieldStyle(.roundedBorder)

            Stepper(value: $durationMinutes, in: 1...1440) {
                Text("\(durationMinutes) \(AppStrings.TaskMarket.durationLabel(appState.locale))")
            }

            TextField(AppStrings.TaskMarket.columnWorktree(appState.locale), text: $worktreeName)
                .textFieldStyle(.roundedBorder)

            if row.leaseState == .open {
                Button {
                    performClaim(row: row)
                } label: {
                    Text(buttonTitle(for: .claim))
                }
                .disabled(!canClaim)
            } else if let leaseID = row.leaseID {
                Button {
                    performRelease(leaseID: leaseID)
                } label: {
                    Text(buttonTitle(for: .release))
                }
                .disabled(isProcessing)
            }
        }
        .padding(.top, 8)
    }

    private var canClaim: Bool {
        !isProcessing && !agentID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private enum Command {
        case claim
        case release
    }

    private func buttonTitle(for command: Command) -> String {
        let locale = appState.locale
        if isProcessing {
            return AppStrings.TaskMarket.processingLabel(locale)
        }
        switch command {
        case .claim:
            return AppStrings.TaskMarket.claimButton(locale)
        case .release:
            return AppStrings.TaskMarket.releaseButton(locale)
        }
    }

    private func performClaim(row: TaskMarketIssueRow) {
        let trimmedAgentID = agentID.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedWorktree = worktreeName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedAgentID.isEmpty else { return }

        isProcessing = true
        Task { @MainActor in
            await daemonController.claimIssue(
                taskID: row.taskID,
                agentID: trimmedAgentID,
                durationMinutes: durationMinutes,
                worktreeName: trimmedWorktree
            )
            isProcessing = false
        }
    }

    private func performRelease(leaseID: String) {
        isProcessing = true
        Task { @MainActor in
            await daemonController.releaseLease(leaseID: leaseID)
            isProcessing = false
        }
    }

    // MARK: - Empty State

    private func emptyState(text: String) -> some View {
        HStack {
            Spacer()
            VStack(spacing: 8) {
                Image(systemName: "tray")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text(text)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.vertical, 48)
    }

    // MARK: - Helpers

    private func leaseText(for state: TaskMarketLeaseState) -> String {
        switch state {
        case .claimed:
            return AppStrings.TaskMarket.leaseStateClaimed(appState.locale)
        case .open:
            return AppStrings.TaskMarket.leaseStateOpen(appState.locale)
        }
    }

    private func leaseColor(for state: TaskMarketLeaseState) -> Color {
        switch state {
        case .claimed:
            return .purple
        case .open:
            return .green
        }
    }

    private func worktreeText(for label: String?) -> String {
        label ?? AppStrings.TaskMarket.worktreePlaceholder(appState.locale)
    }

    private func statusColor(for status: String) -> Color {
        switch status.lowercased() {
        case "completed", "done", "closed", "resolved":
            return .green
        case "in_progress", "running", "active":
            return .blue
        case "blocked", "failed", "open":
            return .red
        case "planning", "pending", "waiting":
            return .orange
        default:
            return .secondary
        }
    }

    private func riskColor(for riskLevel: String) -> Color {
        switch riskLevel.lowercased() {
        case "low":
            return .green
        case "medium":
            return .orange
        case "high":
            return .red
        case "critical":
            return .purple
        default:
            return .secondary
        }
    }
}
