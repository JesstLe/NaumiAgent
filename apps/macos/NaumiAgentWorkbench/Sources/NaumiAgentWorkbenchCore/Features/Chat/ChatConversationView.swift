import SwiftUI

struct ChatConversationScrollSignal: Equatable {
    let messageIDs: [String]
    let execution: ChatExecutionPresentation?

    init(messages: [ChatMessageDTO], execution: ChatExecutionPresentation?) {
        messageIDs = messages.map(\.id)
        self.execution = execution
    }
}

struct ChatConversationView<Composer: View>: View {
    let messages: [ChatMessageDTO]
    let execution: ChatExecutionPresentation?
    let locale: AppLocale
    let onPermissionDecision: (ChatPermissionDecision) -> Void
    let onReview: () -> Void
    @ViewBuilder let composer: () -> Composer
    private let bottomAnchorID = "chat-conversation-bottom"

    var body: some View {
        let scrollSignal = ChatConversationScrollSignal(
            messages: messages,
            execution: execution
        )

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

                        Color.clear
                            .frame(height: 1)
                            .id(bottomAnchorID)
                    }
                    .frame(maxWidth: 760)
                    .padding(.horizontal, 28)
                    .padding(.top, 28)
                    .padding(.bottom, 136)
                    .frame(maxWidth: .infinity)
                }
                .defaultScrollAnchor(.bottom)
                .onAppear {
                    scrollToBottom(reader, animated: false)
                }
                .onChange(of: scrollSignal) { _, _ in
                    scrollToBottom(reader, animated: true)
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

    private func scrollToBottom(_ reader: ScrollViewProxy, animated: Bool) {
        Task { @MainActor in
            await Task.yield()
            if animated {
                withAnimation(.easeOut(duration: 0.18)) {
                    reader.scrollTo(bottomAnchorID, anchor: .bottom)
                }
            } else {
                reader.scrollTo(bottomAnchorID, anchor: .bottom)
            }
        }
    }

    private func hasLinkedIssue(_ message: ChatMessageDTO) -> Bool {
        guard case .object? = message.metadata["workbench_issue"] else { return false }
        return true
    }
}
