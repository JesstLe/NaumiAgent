import Foundation

/// Presentation policy for persisted chat history.
public enum ChatMessagePresentation {
    /// System prompts remain in stored history for model context, but are not
    /// conversation bubbles for the person using the Workbench.
    public static func displayMessages(from messages: [ChatMessageDTO]) -> [ChatMessageDTO] {
        messages.filter { $0.role != "system" }
    }
}
