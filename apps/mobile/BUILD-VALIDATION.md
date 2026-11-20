# Native build validation

Validated with Node 24.19.0, Expo SDK 58.0.26 and React Native 0.88.1.

- Strict TypeScript: passed.
- Native client contract/lifecycle tests: 16 passed.
- Android and iOS Hermes exports: passed (about 1.9 MB each).
- CI planner tests: 41 passed; mobile has separate path-scoped validation and optional native builds.
- Android ARM64 Release preview: native C++/Kotlin/Java compilation, release lint, R8 and APK packaging passed (658 tasks). Portable Temurin JDK17.0.21, Gradle9.4.1, Android SDK37, build tools36/37, NDK27.1.12297006 and CMake3.22.1 were installed under ignored `.implementation/mobile-toolchain`, with official download checksums verified. Windows Ninja object paths required the optional `scripts/windows-cmake.init.gradle` workaround.
- iOS ARM64 Release simulator: native compilation and bundled application packaging passed on macOS26 with Xcode26.4 and signing disabled. [Successful CI run](https://github.com/SkyVex12/career-pilot/actions/runs/44655330798), source revision `e852e6130e51f6c8388ac81601b65c1fb9ada4c3`. Both native builds include the JS bundle and do not require Metro.

## Android artifact

Workspace artifact: `.implementation/artifacts/mobile/CareerPilot-0.1.0-arm64-preview.apk` (25,670,829 bytes). SHA256: `1d9ffe9b0d2b69737387269645b9aa5b9eff05df0572fb3aacf812f24568372e`.

Android tooling verified package `app.careerpilot.mobile`, version `0.1.0`/code1, ARM64 ABI, minimum SDK24 and target SDK36. APK v2 signature verification passed using the standard Android development certificate. The packaged Hermes bundle and native audio library are present. This installable preview is not an owner-signed store release. A machine-readable receipt is next to the APK.

## iOS artifact

The successful CI run contains `career-pilot-ios-simulator` (the zipped Release simulator `.app`) and `career-pilot-ios-build-evidence` (Xcode log and generated `Podfile.lock`). These artifacts expire fourteen days after the run. Local downloads encountered repeated artifact-storage TLS timeouts; the compiled artifact is available through the linked CI run, but its binary and lockfile were not independently inspected locally. Do not substitute an invented CocoaPods lockfile for the generated build evidence.

## Device validation boundary

ADB device enumeration succeeded and reported no attached devices. Physical device checks remain: Windows/macOS companion with Android/iPhone, headphone/Bluetooth routing, microphone prompts/interruptions, Wi-Fi/cellular transitions, screen-sharing configurations, lock/unlock, long-call battery, large text/landscape and screen-reader behavior. Configured backend provider validation is recorded separately in the program ledger; no physical phone-audio success is inferred from deterministic tests or native builds.

The iOS simulator app cannot be installed on a physical iPhone. Owner signing and device provisioning are required for iPhone distribution. No store publishing, production deployment, or device installation was performed.
