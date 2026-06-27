import Testing
@testable import NaumiAgentWorkbenchCore

struct APIErrorTests {

    @Test func missingSelectedSessionHasLocalizedMessages() {
        let error = APIError.missingSelectedSession

        #expect(error.localizedMessage(locale: .zhCN) == "请先选择一个会话")
        #expect(error.localizedMessage(locale: .enUS) == "Select a session first")
        #expect(error.technicalDetail == "missingSelectedSession")
    }

    @Test func networkFailureHasActionableLocalizedMessages() {
        let originalDetail = "The operation couldn't be completed. (NSURLErrorDomain error -1004.)"
        let error = APIError.networkFailure(originalDetail)

        let zhMessage = error.localizedMessage(locale: .zhCN)
        #expect(zhMessage.contains("无法连接本地 NaumiAgent 服务"))
        #expect(zhMessage.contains("naumi-agent api --host 127.0.0.1 --port 8765"))

        let enMessage = error.localizedMessage(locale: .enUS)
        #expect(enMessage.contains("Cannot reach the local NaumiAgent service"))
        #expect(enMessage.contains("naumi-agent api --host 127.0.0.1 --port 8765"))

        #expect(error.technicalDetail.contains(originalDetail))
        #expect(error.technicalDetail.contains("networkFailure"))
    }
}
