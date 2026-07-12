import Testing
@testable import NaumiAgentWorkbenchCore

struct ChatMessagePresentationTests {

    @Test func displayMessagesHideSystemPromptsButKeepConversationOrder() {
        let messages = [
            ChatMessageDTO(id: "system", role: "system", content: "Internal instruction", timestamp: ""),
            ChatMessageDTO(id: "user", role: "user", content: "你好", timestamp: ""),
            ChatMessageDTO(id: "tool", role: "tool", content: "Internal tool output", timestamp: ""),
            ChatMessageDTO(id: "assistant", role: "assistant", content: "你好，有什么可以帮你？", timestamp: ""),
        ]

        let displayMessages = ChatMessagePresentation.displayMessages(from: messages)

        #expect(displayMessages.map(\.id) == ["user", "assistant"])
    }

    @Test func displayMessagesHideEmptyAssistantToolCallPlaceholders() {
        let messages = [
            ChatMessageDTO(id: "user", role: "user", content: "执行检查", timestamp: ""),
            ChatMessageDTO(id: "placeholder", role: "assistant", content: "  \n", timestamp: ""),
            ChatMessageDTO(id: "tool", role: "tool", content: "Internal tool output", timestamp: ""),
            ChatMessageDTO(id: "assistant", role: "assistant", content: "检查完成", timestamp: ""),
        ]

        let displayMessages = ChatMessagePresentation.displayMessages(from: messages)

        #expect(displayMessages.map(\.id) == ["user", "assistant"])
    }
}
