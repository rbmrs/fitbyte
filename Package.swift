// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "ShrinkyWorkspace",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "Shrinky", targets: ["ShrinkyApp"]),
    ],
    targets: [
        .executableTarget(
            name: "ShrinkyApp",
            path: "macos/Shrinky/Sources/ShrinkyApp"
        ),
    ]
)
