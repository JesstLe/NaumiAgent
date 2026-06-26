import Testing
@testable import NaumiAgentWorkbenchCore

struct APIErrorTests {

    @Test func missingSelectedSessionHasLocalizedMessages() {
        let error = APIError.missingSelectedSession

        #expect(error.localizedMessage(locale: .zhCN) == "请先选择一个会话")
        #expect(error.localizedMessage(locale: .enUS) == "Select a session first")
        #expect(error.technicalDetail == "missingSelectedSession")
    }
}
