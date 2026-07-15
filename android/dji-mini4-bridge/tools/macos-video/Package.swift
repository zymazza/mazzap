// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "VeilDJINativeVideo",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "veil-dji-video", targets: ["VeilDJIVideo"]),
    ],
    targets: [
        .executableTarget(name: "VeilDJIVideo"),
        .testTarget(
            name: "VeilDJIVideoTests",
            dependencies: ["VeilDJIVideo"]
        ),
    ]
)
