# Native build validation

Validated on Windows with Node 24.19.0, Expo SDK 58.0.26 and React Native 0.88.1.

- Strict TypeScript: passed.
- Native client contract/lifecycle tests: 16 passed.
- Android and iOS Hermes exports: passed (about 1.9 MB each).
- Generated Android Gradle project and iOS Xcode project: present.
- CI planner tests: 41 passed; mobile has its own path-scoped validation and optional native APK workflow.
- Portable Temurin JDK17.0.21, Android SDK platform37.0/buildtools37/CMake3.22.1 and Gradle9.4.1 installed under ignored `.implementation/mobile-toolchain`; official download checksums verified. NDK27.1.12297006 installed and verified; native APK build in progress. This row will be updated with the final build outcome.

The Android preview uses the standard development key and includes the JS bundle, so it does not require Metro. This is not an owner-signed store release. iOS packaging needs macOS/Xcode26.4 and owner signing/device provisioning. No store publishing, production deployment, or device installation was performed.

ADB device enumeration succeeded and reported no attached devices. Physical device checks are outstanding: Windows/macOS companion with Android/iPhone, headphone/Bluetooth routing, real microphone prompts/interruptions, Wi-Fi/cellular transitions, screen-sharing configurations, lock/unlock, long-call battery, large text/landscape and screen-reader behavior. Configured backend provider validation is recorded separately in the program ledger; no physical phone-audio success is inferred from deterministic tests or bundle exports.
