// swift-tools-version:6.0
// The swift-tools-version declares the minimum version of Swift required to build this package.

import PackageDescription

let package = Package(
    name: "NaumiAgentWorkbench",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(
            name: "NaumiAgentWorkbenchCore",
            targets: ["NaumiAgentWorkbenchCore"]
        ),
        .executable(
            name: "NaumiAgentWorkbench",
            targets: ["NaumiAgentWorkbench"]
        ),
        .executable(
            name: "NaumiAgentWorkbenchSnapshot",
            targets: ["NaumiAgentWorkbenchSnapshot"]
        ),
    ],
    targets: [
        .target(
            name: "NaumiAgentWorkbenchCore",
            path: "Sources/NaumiAgentWorkbenchCore"
        ),
        .executableTarget(
            name: "NaumiAgentWorkbench",
            dependencies: ["NaumiAgentWorkbenchCore"],
            path: "Sources/NaumiAgentWorkbench"
        ),
        .executableTarget(
            name: "NaumiAgentWorkbenchSnapshot",
            dependencies: ["NaumiAgentWorkbenchCore"],
            path: "Tools/NaumiAgentWorkbenchSnapshot"
        ),
        .testTarget(
            name: "NaumiAgentWorkbenchCoreTests",
            dependencies: ["NaumiAgentWorkbenchCore"],
            path: "Tests/NaumiAgentWorkbenchCoreTests"
        ),
    ]
)
