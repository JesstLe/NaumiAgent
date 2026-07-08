import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct CapabilitiesValidationAllowlistTests {

    private func makeCapabilities(
        allowed: [[String]] = []
    ) -> CapabilitiesDTO {
        CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: false,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1,
            allowedValidationCommands: allowed
        )
    }

    @Test func emptyAllowlistAllowsEverything() {
        // When the daemon does not advertise an allowlist, defer to the server.
        let capabilities = makeCapabilities(allowed: [])
        #expect(capabilities.isValidationCommandAllowed(["pytest"]))
        #expect(capabilities.isValidationCommandAllowed(["rm", "-rf", "/"]))
    }

    @Test func exactPrefixMatchIsAllowed() {
        let capabilities = makeCapabilities(allowed: [
            ["pytest"],
            ["swift", "test"],
        ])
        #expect(capabilities.isValidationCommandAllowed(["pytest", "tests/unit", "-q"]))
        #expect(capabilities.isValidationCommandAllowed(["swift", "test"]))
        #expect(capabilities.isValidationCommandAllowed(["swift", "test", "--parallel"]))
    }

    @Test func nonAllowlistedCommandIsRejected() {
        let capabilities = makeCapabilities(allowed: [
            ["pytest"],
            ["ruff"],
        ])
        #expect(!capabilities.isValidationCommandAllowed(["rm", "-rf", "/"]))
        #expect(!capabilities.isValidationCommandAllowed(["./evil.sh"]))
        #expect(!capabilities.isValidationCommandAllowed([]))
    }

    @Test func allowlistDecodesFromJSON() throws {
        let json = """
        {
            "supports_daemon_management": false,
            "supports_workspace_registry": false,
            "supports_validation_runner": true,
            "supports_event_stream": true,
            "supports_cloud_sync": false,
            "supported_locales": ["zh-CN", "en-US"],
            "default_locale": "zh-CN",
            "protocol_version": 1,
            "supported_resources": [],
            "supported_actions": [],
            "route_templates": {},
            "allowed_validation_commands": [["pytest"], ["swift", "test"]]
        }
        """.data(using: .utf8)!
        let capabilities = try JSONDecoder().decode(CapabilitiesDTO.self, from: json)
        #expect(capabilities.allowedValidationCommands == [["pytest"], ["swift", "test"]])
        #expect(capabilities.isValidationCommandAllowed(["pytest", "-x"]))
        #expect(!capabilities.isValidationCommandAllowed(["make", "boom"]))
    }

    @Test func missingAllowlistDefaultsToEmpty() throws {
        let json = """
        {
            "supports_daemon_management": false,
            "supports_workspace_registry": false,
            "supports_validation_runner": true,
            "supports_cloud_sync": false,
            "supported_locales": ["zh-CN"],
            "protocol_version": 1
        }
        """.data(using: .utf8)!
        let capabilities = try JSONDecoder().decode(CapabilitiesDTO.self, from: json)
        #expect(capabilities.allowedValidationCommands == [])
        #expect(capabilities.isValidationCommandAllowed(["pytest"]))
    }
}
