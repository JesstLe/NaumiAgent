import SwiftUI

struct ChatConversationView<Composer: View>: View {
    let messages: [ChatMessageDTO]
    let execution: ChatExecutionPresentation?
    let locale: AppLocale
    let onPermissionDecision: (ChatPermissionDecision) -> Void
    let onReview: () -> Void
    @ViewBuilder let composer: () -> Composer

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { reader in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 24) {
                        if messages.isEmpty {
                            ContentUnavailableView {
                                Label(
                                    AppStrings.Chat.emptyMessages(locale),
                                    systemImage: "bubble.left.and.bubble.right"
                                )
                            }
                            .frame(maxWidth: .infinity, minHeight: 520)
                        } else {
                            ForEach(messages, id: \.id) { message in
                                ChatMessageRow(
                                    message: message,
                                    locale: locale,
                                    showsLinkedIssue: hasLinkedIssue(message)
                                )
                                .id(message.id)
                            }
                        }

                        if let execution {
                            ChatRunTimeline(
                                execution: execution,
                                locale: locale,
                                onPermissionDecision: onPermissionDecision,
                                onReview: onReview
                            )
                            .id(execution.id)
                        }
                    }
                    .frame(maxWidth: 760)
                    .padding(.horizontal, 28)
                    .padding(.top, 28)
                    .padding(.bottom, 136)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: messages.count) { _, _ in
                    guard let lastID = messages.last?.id else { return }
                    withAnimation(.easeOut(duration: 0.18)) {
                        reader.scrollTo(lastID, anchor: .bottom)
                    }
                }
                .onChange(of: execution) { _, execution in
                    guard let execution else { return }
                    withAnimation(.easeOut(duration: 0.18)) {
                        reader.scrollTo(execution.id, anchor: .bottom)
                    }
                }
            }
            .overlay(alignment: .bottom) {
                composer()
                    .frame(maxWidth: 720)
                    .padding(.horizontal, 22)
                    .padding(.bottom, 16)
            }
        }
        .background(WorkbenchComponentTheme.surface(.canvas))
    }

    private func hasLinkedIssue(_ message: ChatMessageDTO) -> Bool {
        guard case .object? = message.metadata["workbench_issue"] else { return false }
        return true
    }
}
