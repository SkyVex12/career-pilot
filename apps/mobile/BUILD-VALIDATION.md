# Native build validation

Validated with Node 24.19.0, Expo SDK 58.0.26 and React Native 0.88.1.

## Original secure-preview baseline (2026-11-19)

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

## HTTP test follow-up (2026-11-19)

- Thirty deterministic mobile tests and strict TypeScript passed. Android and iOS secure Hermes exports passed; HTTP test native compilation, release lint and packaging passed (618 Gradle tasks, 39 executed in the final incremental build).
- APK: `.implementation/artifacts/mobile/CareerPilot-0.1.0-http-test-arm64.apk` (31,930,539 bytes). SHA256: `fa46785face54e7370c1466db2d9631397e568837ec09e449a853254371df094`. The original secure-preview APK above is preserved.
- Android tooling verified `app.careerpilot.mobile.httptest`, label **Career Pilot HTTP Test**, version `0.1.0-http-test`/code1, ARM64, minSDK24/targetSDK36, APK v2 development signature, explicit cleartext permission, non-debuggable application and distinct test deep-link schemes. The 1,536,140-byte Hermes bundle, native audio library, prefilled HTTP/WS endpoints and canonical token routes are present. No Metro server is required. A machine-readable `.receipt.json` accompanies the APK.
- The test variant alone skips Java shrinking. The successful bounded-memory Windows build used one Gradle worker, disabled parallel execution, `--no-daemon` and `JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=4 -XX:TieredStopAtLevel=1`. Normal release shrinking and HTTPS/WSS policy remain enabled. The Windows path workaround additionally used the installed Microsoft-signed Ninja1.13.2 in the workspace CMake SDK; original Ninja1.10.2 preserved, no global tool/registry change.
- Actual unauthenticated mobile `Api`/`loadAuthConfig` readiness succeeded at 22:54:52 CST on 2026-11-19: Stage0 exposes Google and Career-OS, with email disabled. Public health returned200. The build prefills `http://35.184.116.247` and `ws://35.184.116.247/v1/companion`; the public IP may change after VM restart. This verifies server connection/capability discovery, not authenticated user acceptance.
- Career-OS browser PKCE uses a deliberate localhost callback-paste fallback. Desktop Google users must first link Career-OS to the same account. Native Google sign-in, production TLS, device installation and real-user browser sign-in remain unverified. HTTP/WS carries credentials and content unencrypted; this explicitly labeled build is for test accounts/content.

## Device validation boundary

ADB device enumeration succeeded and reported no attached devices. Physical device checks remain: Windows/macOS companion with Android/iPhone, headphone/Bluetooth routing, microphone prompts/interruptions, Wi-Fi/cellular transitions, screen-sharing configurations, lock/unlock, long-call battery, large text/landscape and screen-reader behavior. Configured backend provider validation is recorded separately in the program ledger; no physical phone-audio success is inferred from deterministic tests or native builds.

The iOS simulator app cannot be installed on a physical iPhone. Owner signing and device provisioning are required for iPhone distribution. No store publishing, production deployment, or device installation was performed.
