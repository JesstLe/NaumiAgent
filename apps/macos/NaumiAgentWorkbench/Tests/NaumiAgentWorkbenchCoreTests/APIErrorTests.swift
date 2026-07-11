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
        #expect(zhMessage.contains("naumi serve --host 127.0.0.1 --port 8765"))

        let enMessage = error.localizedMessage(locale: .enUS)
        #expect(enMessage.contains("Cannot reach the local NaumiAgent service"))
        #expect(enMessage.contains("naumi serve --host 127.0.0.1 --port 8765"))

        #expect(error.technicalDetail.contains(originalDetail))
        #expect(error.technicalDetail.contains("networkFailure"))
    }

    @Test func authFailedHasActionableLocalizedMessages() {
        let error = APIError.authFailed

        #expect(error.localizedMessage(locale: .zhCN) == "本地 daemon 认证失败，请检查访问令牌或重新连接")
        #expect(error.localizedMessage(locale: .enUS) == "Local daemon authentication failed. Check the access token or reconnect.")
        #expect(error.technicalDetail == "authFailed")
    }

    @Test func sessionUnavailableHasActionableLocalizedMessages() {
        let error = APIError.sessionUnavailable

        #expect(error.localizedMessage(locale: .zhCN) == "当前会话不可用，请刷新或选择另一个会话")
        #expect(error.localizedMessage(locale: .enUS) == "The current session is unavailable. Refresh or select another session.")
        #expect(error.technicalDetail == "sessionUnavailable")
    }

    @Test func serverErrorIncludesHTTPStatusAndBackendDetail() {
        let error = APIError.serverError(statusCode: 503, detail: "session registry unavailable")

        #expect(error.localizedMessage(locale: .zhCN) == "本地服务返回错误 503：session registry unavailable")
        #expect(error.localizedMessage(locale: .enUS) == "Local service returned error 503: session registry unavailable")
        #expect(error.technicalDetail == "serverError(statusCode:503,detail:session registry unavailable)")
    }
}
