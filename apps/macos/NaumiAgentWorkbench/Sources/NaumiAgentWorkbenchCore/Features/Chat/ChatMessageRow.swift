import AppKit
import SwiftUI

public struct ChatMessageRow: View {
    let message: ChatMessageDTO
    let locale: AppLocale
    let showsLinkedIssue: Bool
    @State private var isHovering = false

    public init(message: ChatMessageDTO, locale: AppLocale, showsLinkedIssue: Bool = false) {
        self.message = message
        self.locale = locale
        self.showsLinkedIssue = showsLinkedIssue
    }

    public var body: some View {
        let style = ChatPresentation.style(forRole: message.role)

        HStack(alignment: .top, spacing: 0) {
            if style == .compactBubble {
                Spacer(minLength: 112)
            }

            VStack(alignment: .leading, spacing: 7) {
                if style == .document {
                    Text(ChatPresentation.roleLabel(for: message.role, locale: locale))
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                }

                Text(message.content)
                    .font(.system(size: 14))
                    .lineSpacing(3)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)

                if showsLinkedIssue {
                    Label(
                        AppStrings.Chat.linkedIssueCreated(locale),
                        systemImage: "checkmark.circle"
                    )
                    .font(.caption)
                    .foregroundStyle(.green)
                }
                if isHovering {
                    Button(action: copyMessage) {
                        Image(systemName: "doc.on.doc")
                            .frame(width: 16, height: 16)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .help(AppStrings.Chat.copyMessage(locale))
                    .accessibilityLabel(AppStrings.Chat.copyMessage(locale))
                }
            }
            .padding(style == .compactBubble ? 12 : 0)
            .frame(maxWidth: style == .compactBubble ? 520 : .infinity, alignment: .leading)
            .background {
                if style == .compactBubble {
                    RoundedRectangle(cornerRadius: WorkbenchComponentTheme.cornerRadius)
                        .fill(WorkbenchComponentTheme.surface(.group))
                }
            }

            if style == .document {
                Spacer(minLength: 72)
            }
        }
        .frame(
            maxWidth: .infinity,
            alignment: style == .compactBubble ? .trailing : .leading
        )
        .onHover { isHovering = $0 }
    }

    private func copyMessage() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(message.content, forType: .string)
    }
}
