import Testing
@testable import NaumiAgentWorkbenchCore

struct AppStringsErrorTests {

    @Test func capabilityUnavailableZhCN() {
        #expect(
            AppStrings.Error.capabilityUnavailable(.zhCN, capability: "validation_runner")
                == "当前 daemon 不支持「验证运行器」"
        )
    }

    @Test func capabilityUnavailableEnUS() {
        #expect(
            AppStrings.Error.capabilityUnavailable(.enUS, capability: "validation_runner")
                == "The daemon does not support 'validation runner'"
        )
    }

    @Test func capabilityUnavailableUnknownFallsBackToKey() {
        #expect(
            AppStrings.Error.capabilityUnavailable(.zhCN, capability: "unknown_feature")
                == "当前 daemon 不支持「unknown_feature」"
        )
        #expect(
            AppStrings.Error.capabilityUnavailable(.enUS, capability: "unknown_feature")
                == "The daemon does not support 'unknown_feature'"
        )
    }

    @Test func allCapabilityUnavailableStringsAreNonEmpty() {
        #expect(!AppStrings.Error.capabilityUnavailable(.zhCN, capability: "x").isEmpty)
        #expect(!AppStrings.Error.capabilityUnavailable(.enUS, capability: "x").isEmpty)
    }
}
