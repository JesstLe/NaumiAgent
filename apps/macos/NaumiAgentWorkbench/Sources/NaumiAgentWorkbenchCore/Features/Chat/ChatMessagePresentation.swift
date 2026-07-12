import Foundation

/// Presentation policy for persisted chat history.
public enum ChatMessagePresentation {
    /// System prompts and raw tool records remain in stored history for model
    /// context and audit, but are not conversation bubbles for the user.
    public static func displayMessages(from messages: [ChatMessageDTO]) -> [ChatMessageDTO] {
        messages.filter { message in
            !["system", "tool"].contains(message.role)
                && !message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }
}
