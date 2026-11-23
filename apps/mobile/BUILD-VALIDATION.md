# Native build validation

Validated with Node 24.19.0, Expo SDK 58.0.26 and React Native 0.88.1.

## Language selection and live switching (2026-11-23)

The current mobile source consumes the 96-language Deepgram catalog, searches English/native names and codes, preserves profile defaults and RTL labels, and changes live language only after a correlated server acknowledgement. Relay receipts do not prematurely clear the pending change. In-person switching is available between recordings, and older gateways leave live switching disabled.

- Strict TypeScript and all **86 mobile tests** pass. The fictional native fixture has **12 passing tests**, including both acknowledgement orders, rejection, idempotent retry and reconnect restoration. Ruff formatting and lint pass for the fixture and its tests.
- Secure Android Hermes export passes: 866 modules and a 2,263,715-byte bundle. Local output: `.implementation/mobile-language96-android-export`; log: `.implementation/mobile-language96-android-export.log`.
- These local build attempts **did not produce or install a fresh APK**. The initial configured Gradle attempt could not resolve `org.gradle.toolchains.foojay-resolver-convention:1.0.0` from the plugin repositories. A bounded host retry was stopped before Gradle reported any build tasks. Logs are `.implementation/mobile-language96-native-build.log` and `.implementation/mobile-language96-native-build-host-cache.log`. The earlier artifacts below do not validate the current source; any hosted preview result must be checked against its source revision in Actions.
- Actual Android UI acceptance was **not completed**: the existing isolated `CareerPilotValidation` instance failed to reach a running Android/ADB state after a bounded launch retry. Both instance 1 and the user's instance 0 are stopped at completion; instance 0 and installed production apps were not modified. No device screenshots, speech-quality results or native crash-free claim are made for this source. The fixture scenarios in [NATIVE-ACCEPTANCE.md](scripts/NATIVE-ACCEPTANCE.md) are prepared for a functioning development emulator.

Local machine-readable evidence: `.implementation/mobile-language96-validation.receipt.json`. Earlier native validation below remains tied to its recorded source revisions.

## Current completion follow-up (2026-11-20)

The current app retains four primary tabs: Call, Interview prep, Work and History. Searchable profile/context selectors, compact navigation, source sheets, focused practice controls and phone/landscape layouts replace the earlier rows of oversized buttons. Work preserves its in-memory conversation and draft while opening a source or changing tabs. Server addresses survive launch, account tokens remain origin-scoped, and explicit companion attachment can reuse a fresh authorized device/profile/session grant after process recreation.

The backend catalog supports 24 spoken recognition/text languages, with interviewer audio for six of them when configured. Mobile reads the server's catalog and per-session speech capabilities. Original resumes and private briefs retain backend authorization/preparation rules. A bounded post-sign-in capability check reports older or unavailable servers in the account panel without claiming proxy/provider readiness. These are implementation descriptions, not physical speech-quality results.

- Final Android product source: `ed4ef21d64ced73d06ba6346e614c815e13c4bd3`, adding shared account-scoped grant retirement across profile remounts and explicit retry for failed practice setup choices. It includes the earlier malformed-snapshot recovery and terminal WebSocket queued-frame guard. Strict mobile TypeScript and **74 deterministic tests** pass. Local test log: `.implementation/mobile-completion-final-tests.log`.
- Final hosted checks and native packaging passed in [run 44690564500](https://github.com/SkyVex12/career-pilot/actions/runs/44690564500), revision `d8b9048` with unchanged `ed4ef21` mobile product inputs: 74 tests, TypeScript, secure Android/iOS exports, Android arm64 Release (6m53s), and unsigned arm64 iOS Release simulator compilation on Xcode 26.4.1. The [hosted receipt](../../docs/implementation/evidence/mobile-completion-hosted-2026-11-20.json) records artifact IDs, archive hashes and successful logs. A single bounded final download failed at storage TLS; no local hosted APK/app or generated Podfile.lock inspection is claimed. The prior `b3736ae` run 44688789078 is historical candidate evidence.
- Final HTTP-test APK: `.implementation/artifacts/mobile/CareerPilot-0.1.0-completion-http-test.apk`, containing arm64-v8a and x86_64 native libraries. Older APKs and receipts below are preserved.
- Scoped native acceptance completed on the isolated **CareerPilotValidation** Android 14 x86_64 emulator, serial `emulator-5556`. The user's existing emulator and account data were not changed.

The [native acceptance receipt](../../docs/implementation/evidence/mobile-native-2026-11-20.json) identifies the exact package/source for each check. The broad `b3736ae` suite covers Work, practice, preparation, history, companion command/replay/reconnect, error recovery, process restoration and compact/large-text/dark layouts. Malformed-snapshot recovery disables commands, offers Retry, restores live state and successfully executes another Answer command. That suite's final crash buffer is empty; exit records contain intentional updates/force-stops, with no observed ANR. Targeted `ed4ef21` checks passed setup 503 → Retry → restored 24-language picker without remounting, plus malformed companion snapshot → disabled commands → Retry → live answer and executed command receipt. The final package crash buffer was empty; the receipt records nine selected screenshots with per-source provenance. This does not establish the original user-reported ANR cause.

These journeys use a fictional loopback-only fixture, with no real model, calendar or speech calls. Physical audio/network/device coverage and visible on-screen keyboard occlusion remain outside this acceptance. The receipt identifies selected screenshots and their exact source revisions; earlier failure screenshots are excluded from success evidence. Reproduction and scenario assertions: [scripts/NATIVE-ACCEPTANCE.md](scripts/NATIVE-ACCEPTANCE.md).

### Final native package verification (`ed4ef21`)

The APK is **56,081,446 bytes**, SHA256 `d5c911784be74861a074d4e30ec557b006a48ea7809b2fecb0d8deac84931b7b`. It exactly matches the Gradle output. The final incremental build completed in 58 seconds: 661 tasks, 22 executed and 639 up-to-date; release lint passed. Log: `.implementation/mobile-completion-lifecycle-build.log`. All 108 tracked mobile/non-documentation and local dotenv input hashes remained unchanged during verification. Mobile product source matches the commit; only its documentation is modified. Unrelated workspace changes are outside the APK input scope.

Android build-tools 36.0.0 `aapt`, `apksigner` and `zipalign` verified package `app.careerpilot.mobile.httptest`, label **Career Pilot HTTP Test**, version `0.1.0-http-test`/code1, minSDK24/targetSDK36, non-debuggable status, backup disabled, test-only deep links, explicit cleartext permission and the APK v2 Android development signature. The ordinary main manifest still disables cleartext; this is an explicitly labeled test artifact, not an owner-signed store release.

Both ARM64 and x86_64 contain actual 64-bit ELF app, Hermes, audio and SVG native modules. The embedded Hermes bytecode bundle is 1,811,180 bytes; no Metro server is required. Neither the old Stage0 IP nor the local fixture's `:8765` endpoint is embedded. Enter the intended public API/gateway addresses or restore this installation's saved addresses.

Machine-readable package evidence: [mobile-completion-android-2026-11-20.json](../../docs/implementation/evidence/mobile-completion-android-2026-11-20.json) and the APK's adjacent `.receipt.json`. Both identify the verified native package and link the separate native acceptance receipt. The static package verifier did not access an emulator.

The superseded `b3736ae` package was **56,078,642 bytes**, SHA256 `a08a14581b7d8de299cf09d5942bf694e51ace2bf46eba12109cdf3971f2ec90`, with a 1,808,376-byte Hermes bundle and 69 passing mobile tests. Its 58-second build log is `.implementation/mobile-completion-socket-guard-build.log`. The binary and full receipt are preserved as `.implementation/artifacts/mobile/CareerPilot-0.1.0-completion-http-test.b3736ae-candidate.apk` and the adjacent `.receipt.json`. The broad native acceptance suite remains valid for that source; it is not substituted for the final package's targeted checks.

The superseded `a9af830` candidate was 56,078,310 bytes, SHA256 `34d29a166922808b8a479cdb7522d5a671c25cb134bd5d2596229084aa092453`, with a 1,808,044-byte Hermes bundle. Its build took 52 seconds (661 tasks, 22 executed); [CI run 44686436871](https://github.com/SkyVex12/career-pilot/actions/runs/44686436871) passed typecheck, tests and exports at that earlier source. Its full receipt is preserved as `.implementation/artifacts/mobile/CareerPilot-0.1.0-completion-http-test.a9af830-candidate.receipt.json`; it is not the delivered APK.

## Original secure-preview baseline (2026-11-19)

- Strict TypeScript: passed.
- Native client contract/lifecycle tests: 16 passed.
- Android and iOS Hermes exports: passed (about 1.9 MB each).
- CI planner tests: 41 passed; mobile has separate path-scoped validation and optional native builds.
- Android ARM64 Release preview: native C++/Kotlin/Java compilation, release lint, R8 and APK packaging passed (658 tasks). Portable Temurin JDK17.0.21, Gradle9.4.1, Android SDK37, build tools36/37, NDK27.1.12297006 and CMake3.22.1 were installed under ignored `.implementation/mobile-toolchain`, with official download checksums verified. Windows Ninja object paths required the optional `scripts/windows-cmake.init.gradle` workaround.
- iOS ARM64 Release simulator: native compilation and bundled application packaging passed on macOS26 with Xcode26.4 and signing disabled. [Successful CI run](https://github.com/SkyVex12/career-pilot/actions/runs/44655330798), source revision `e852e6130e51f6c8388ac81601b65c1fb9ada4c3`. Both native builds include the JS bundle and do not require Metro.

## Original Android artifact

Workspace artifact: `.implementation/artifacts/mobile/CareerPilot-0.1.0-arm64-preview.apk` (25,670,829 bytes). SHA256: `1d9ffe9b0d2b69737387269645b9aa5b9eff05df0572fb3aacf812f24568372e`.

Android tooling verified package `app.careerpilot.mobile`, version `0.1.0`/code1, ARM64 ABI, minimum SDK24 and target SDK36. APK v2 signature verification passed using the standard Android development certificate. The packaged Hermes bundle and native audio library are present. This installable preview is not an owner-signed store release. A machine-readable receipt is next to the APK.

## Original iOS artifact

The successful CI run contains `career-pilot-ios-simulator` (the zipped Release simulator `.app`) and `career-pilot-ios-build-evidence` (Xcode log and generated `Podfile.lock`). These artifacts expire fourteen days after the run. Local downloads encountered repeated artifact-storage TLS timeouts; the compiled artifact is available through the linked CI run, but its binary and lockfile were not independently inspected locally. Do not substitute an invented CocoaPods lockfile for the generated build evidence.

## HTTP test follow-up (2026-11-19)

- Thirty deterministic mobile tests and strict TypeScript passed. Android and iOS secure Hermes exports passed; HTTP test native compilation, release lint and packaging passed (618 Gradle tasks, 39 executed in the final incremental build).
- APK: `.implementation/artifacts/mobile/CareerPilot-0.1.0-http-test-arm64.apk` (31,930,539 bytes). SHA256: `fa46785face54e7370c1466db2d9631397e568837ec09e449a853254371df094`. The original secure-preview APK above is preserved.
- Android tooling verified `app.careerpilot.mobile.httptest`, label **Career Pilot HTTP Test**, version `0.1.0-http-test`/code1, ARM64, minSDK24/targetSDK36, APK v2 development signature, explicit cleartext permission, non-debuggable application and distinct test deep-link schemes. The 1,536,140-byte Hermes bundle, native audio library, prefilled HTTP/WS endpoints and canonical token routes are present. No Metro server is required. A machine-readable `.receipt.json` accompanies the APK.
- The test variant alone skips Java shrinking. The successful bounded-memory Windows build used one Gradle worker, disabled parallel execution, `--no-daemon` and `JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=4 -XX:TieredStopAtLevel=1`. Normal release shrinking and HTTPS/WSS policy remain enabled. The Windows path workaround additionally used the installed Microsoft-signed Ninja1.13.2 in the workspace CMake SDK; original Ninja1.10.2 preserved, no global tool/registry change.
- Actual unauthenticated mobile `Api`/`loadAuthConfig` readiness succeeded at 22:54:52 CST on 2026-11-19: Stage0 exposes Google and Career-OS, with email disabled. Public health returned200. The build prefills `http://35.184.116.247` and `ws://35.184.116.247/v1/companion`; the public IP may change after VM restart. This verifies server connection/capability discovery, not authenticated user acceptance.
- Career-OS browser PKCE uses a deliberate localhost callback-paste fallback. Desktop Google users must first link Career-OS to the same account. Native Google sign-in, production TLS, device installation and real-user browser sign-in remain unverified. HTTP/WS carries credentials and content unencrypted; this explicitly labeled build is for test accounts/content.

## Device validation boundary

At the original secure-preview checkpoint, ADB enumeration reported no attached devices. Current native acceptance uses the separate Android 14 emulator described above; this is not a physical phone trial. Physical device checks remain: Windows/macOS companion with Android/iPhone, headphone/Bluetooth routing, microphone prompts/interruptions, Wi-Fi/cellular transitions, screen-sharing configurations, lock/unlock, long-call battery, large text/landscape and screen-reader behavior. Configured backend provider validation is recorded separately in the program ledger; no physical phone-audio success is inferred from deterministic tests, emulator journeys or native builds.

The iOS simulator app cannot be installed on a physical iPhone. Owner signing and device provisioning are required for iPhone distribution. No store publishing, production deployment or physical-device installation was performed. The delivered HTTP-test APK was installed only in the isolated validation emulator; the hosted secure APK was not device-tested.
