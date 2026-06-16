// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "FitbyteWorkspace",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "Fitbyte", targets: ["FitbyteApp"]),
    ],
    targets: [
        .executableTarget(
            name: "FitbyteApp",
            path: "macos/Fitbyte/Sources/FitbyteApp"
        ),
    ]
)
