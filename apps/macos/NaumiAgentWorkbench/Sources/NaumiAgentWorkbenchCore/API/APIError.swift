import Foundation

/// Errors thrown by `WorkbenchAPIClient`. 中文默认消息 + 英文 fallback。
public enum APIError: Error, Equatable, Sendable {
    case invalidURL
    case invalidResponse
    case authFailed
    case missingSelectedSession
    case capabilityUnavailable(String)
    case protocolVersionMismatch(expected: Int, actual: Int)
    case httpStatus(Int)
    case decodingFailed(String)
    case networkFailure(String)

    public func localizedMessage(locale: AppLocale) -> String {
        switch self {
        case .invalidURL:
            return AppStrings.Error.invalidURL(locale)
        case .invalidResponse:
            return AppStrings.Error.invalidResponse(locale)
        case .authFailed:
            return AppStrings.Error.authFailed(locale)
        case .missingSelectedSession:
            return AppStrings.Error.missingSelectedSession(locale)
        case .capabilityUnavailable(let capability):
            return AppStrings.Error.capabilityUnavailable(locale, capability: capability)
        case .protocolVersionMismatch(let expected, let actual):
            return AppStrings.Error.protocolVersionMismatch(
                locale,
                expected: expected,
                actual: actual
            )
        case .httpStatus(let code):
            return AppStrings.Error.httpStatus(locale, code: code)
        case .decodingFailed:
            return AppStrings.Error.decodingFailed(locale)
        case .networkFailure:
            return AppStrings.Error.networkFailure(locale)
        }
    }

    public var technicalDetail: String {
        switch self {
        case .invalidURL:
            return "invalidURL"
        case .invalidResponse:
            return "invalidResponse"
        case .authFailed:
            return "authFailed"
        case .missingSelectedSession:
            return "missingSelectedSession"
        case .capabilityUnavailable(let capability):
            return "capabilityUnavailable(\(capability))"
        case .protocolVersionMismatch(let expected, let actual):
            return "protocolVersionMismatch(expected:\(expected),actual:\(actual))"
        case .httpStatus(let code):
            return "httpStatus(\(code))"
        case .decodingFailed(let detail):
            return "decodingFailed(\(detail))"
        case .networkFailure(let detail):
            return "networkFailure(\(detail))"
        }
    }
}
