# Six-feature development delivery

[Draft PR #98](https://github.com/SkyVex12/career-pilot/pull/98). Branch: `codex/six-feature-program`, based on cleaned main `ca13ba8`. This is a reviewable development implementation; no production migration, merge, deployment or store publication was performed.

## Interface

Primary navigation remains Call, Interview prep, Work context and Past calls. Practice lives inside Interview prep; Ask lives inside Work context. Both preserve local state while opening supporting material. Settings contains background/call-suggestion and optional calendar controls. Context from my AI is an expandable Work section. Mobile groups the same flows under Call, Interview prep, Work and History.

## Available artifacts

| Platform | Artifact | Distribution status |
| --- | --- | --- |
| Windows | `.implementation/artifacts/windows/CareerPilot-six-feature-preview.exe` | Standalone debug executable with bundled frontend; unsigned, no updater; not installed automatically |
| macOS | [Universal DMG artifact](https://github.com/SkyVex12/career-pilot/actions/runs/44655879546/artifacts/14594572590) | Build and mounted-app verification passed; Apple Silicon/Intel, macOS 13+; ad-hoc signed, not notarized |
| Android | `.implementation/artifacts/mobile/CareerPilot-0.1.0-arm64-preview.apk` | Bundled arm64 preview; verified development signature; Android 7+ (API 24), target API 36 |
| iOS | [Simulator app artifact](https://github.com/SkyVex12/career-pilot/actions/runs/44655330798/artifacts/14592999166) | Native Release build passed on Xcode 26.4; unsigned arm64 simulator app, not an iPhone install package |

Windows artifact: 50,902,016 bytes, SHA256 `5CFA1C61269DD17178DA7CCEB6BF9D3FA7B5890D9E85DDC8118252E700176255`. Build command, after frontend production build: from `apps/desktop`, `node node_modules/@tauri-apps/cli/tauri.js build --debug --no-bundle --config ../../target/build-config/desktop-debug.json --runner C:/Users/A/.cargo/bin/cargo.exe -- --offline`. The local JSON config only sets `build.beforeBuildCommand` to an empty string; it does not embed credentials or production URLs. Select the configured backend in app Settings. The local defaults require the repository's API/gateway stack; a standalone executable does not include servers.

Android preview: 25,670,829 bytes, SHA256 `1d9ffe9b0d2b69737387269645b9aa5b9eff05df0572fb3aacf812f24568372e`. The development key is intentionally public and is not a production signing identity. See [mobile build evidence](../../apps/mobile/BUILD-VALIDATION.md) for reproducible commands, platform versions and the iOS receipt.

The iOS simulator artifacts are available from [run 44655330798](https://github.com/SkyVex12/career-pilot/actions/runs/44655330798) for fourteen days. Local artifact-storage TLS timeouts prevented downloading and inspecting the binary/Podfile.lock; native CI compilation succeeded.

Mac native tests, bridge availability checks, Universal packaging, mounted-DMG architecture/minimum-OS verification and strict ad-hoc signature verification passed in [run 44655879546](https://github.com/SkyVex12/career-pilot/actions/runs/44655879546), source `e852e6130e51f6c8388ac81601b65c1fb9ada4c3`. The GitHub artifact archive is 27,448,627 bytes, SHA256 `1e8e2253b5c89a665e2ed9426a609e243d30748ff8af542a65bdae7f610e7296`; this is the archive digest, not the enclosed DMG digest. Artifact retention is fourteen days. The local download also hit an artifact-storage TLS timeout. CI logs verify the mounted app; the exact DMG digest is in the artifact's `verification.json` and is not claimed as locally inspected.

The Mac preview follows the existing release configuration and pins the repository's Stage 0 backend URLs. New routes need a compatible migrated backend deployment before those features can run there. No backend was deployed by this task.

## Setup references

Apply all new migrations with the repository migration command in the intended development/staging environment before using new routes. Do not apply them to production merely to try a development build. Existing backend authentication, encryption keys, storage and configured model providers remain required.

- [Ask and practice](ask-and-practice.md): provider configuration, source lifecycle, speech capture and deletion.
- [Languages](call-languages.md): English default; Spanish/Vietnamese candidates remain gated until representative speech and corpus checks pass.
- [Calendar](calendar-provider-setup.md): Google/Outlook client registrations, exact callbacks and minimal read-only scopes.
- [Background controls](background-native-integration.md): opt-in behavior, native signals and honest per-application support matrix.
- [Personal AI](personal-ai-context.md): authenticated request/publish workflow, privacy, connection and deployment requirements.
- [Mobile](../../apps/mobile/README.md): public API/gateway origins, SecureStore sign-in, Android/iOS prerequisites and packaging.
- [Companion backend](companion-backend.md): desktop capture reuse, grants, command receipts and reconnect behavior.

## Verification

- Desktop: 457 tests across 50 files, TypeScript and production build pass. Screenshot/axe checks report no violations, page errors or horizontal overflow in tested desktop/narrow fixtures.
- Backend final integrated suite: 1,704 tests pass against local migrated PostgreSQL/Valkey/object-store stack (351.86 seconds). Global Ruff, format, Pyright and protocol generation checks pass.
- Windows native: client 81 tests, desktop 96 passed/3 ignored, strict Clippy and rustfmt pass. Actual read-only WASAPI/UIA diagnostics pass, with no real call candidates present.
- Actual providers: one Ask streaming/citation check, one English synthetic TTS/STT round trip, two practice plan/feedback requests. Fictional content only; no private user recordings. These are adapter checks, not broad quality or physical-device certification.
- Actual Codex MCP host: local HTTP request/publish/refresh/private retrieval/delete path passed using an OAuth-issued scoped token against a disposable migrated database. Focused OAuth/personal-AI backend35 and UI6 tests pass; independent security review found no blocking bypass.
- Mobile: 16 deterministic tests, strict TypeScript and Android/iOS Hermes exports pass; 65 CI/release planner tests pass. Native packaging evidence is recorded separately.

## External validation and configuration

Real Google/Microsoft consent requires owner-provided OAuth applications. Remote ChatGPT connection/confirmation requires a reachable HTTPS service and host access. Real conferencing apps, browser calls, emulators, phone links, Focus/DND policies, hardware microphone/playback/echo, mobile interruptions/Bluetooth and long-call battery behavior still need the corresponding applications, peers and devices. ADB reports no attached phone. Production signing/notarization and store delivery require owner identities/accounts. No such coverage is inferred from fixtures or compiled binaries.

The complete requirement-by-requirement status and exact evidence remain in [the progress ledger](six-feature-progress.md). External verification limits do not prevent the implemented manual/text workflows.
