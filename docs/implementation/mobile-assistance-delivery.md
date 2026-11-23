# November 23 assistance and workspace preview

Review: [PR #109](https://github.com/SkyVex12/career-pilot/pull/109). This preview adds mobile guest assistance, workspace management, screen detail selection and explicit host approval for remote control. Ask displays accepted questions reliably, call-ended notices float and expire, and call choices are **For interviews** and **For work**. Work includes daily updates, plans and blockers. Historical standup/online-test records remain readable.

This supplements the [earlier six-feature delivery](six-feature-delivery.md). Current scope and acceptance are recorded in the [progress ledger](mobile-assistance-parity.md); older packages do not contain these changes.

## Installable previews

| Platform | Available file | Evidence and distribution status |
| --- | --- | --- |
| Android phones and LDPlayer | `C:/Users/A/Downloads/CoRuby-Assistance-HTTP-Test-6fc13b9.apk` | ARM64 and x86_64; bundled application, no Metro server needed. Development signing, HTTP/WS testing enabled. [Package verification](evidence/mobile-assistance-http-test-2026-11-23.json). |
| Windows x86_64 | `C:/Users/A/Downloads/CoRuby-Assistance-Desktop-eca3894.exe` | Embedded cloud preview with existing GCP configuration; no installer/updater or production signing. [Build receipt](evidence/desktop-assistance-windows-2026-11-23.json). |
| macOS 13+ Intel and Apple Silicon | `.implementation/artifacts/macos/eca3894/CoRuby-Assistance-Mac-universal-eca3894.dmg` | Universal CI preview; downloaded checksum and bundled architecture/minimum-OS receipt verified. [Build receipt](evidence/desktop-assistance-macos-2026-11-23.json). |
| iOS 16.4+ simulator on Apple Silicon | `.implementation/artifacts/ios/assistance-6fc13b9/CareerPilot-simulator.zip` | Simulator build without distribution signing; linked Hermes/LiveKit frameworks and final feature markers verified. Not installable on iPhone. [Build receipt](evidence/mobile-assistance-ios-2026-11-23.json). |

The Android APK is 86,839,562 bytes, SHA256 `71ade339cfe712bb6054db2a3437041769dbc3d0792f3402b306147b0a5adab8`. It uses the separate `app.careerpilot.mobile.httptest` package and preserves the normal secure app. An existing HTTP Test installation can be updated if its signing certificate matches. Previous downloaded APKs were preserved.

A separate secure Android Release/R8 compilation passed with cleartext disabled. Its x86_64 development-signed artifact is compile evidence, not the phone download above: [receipt](evidence/mobile-assistance-secure-r8-2026-11-23.json).

## Connect and exercise the features

Deploy the matching API, gateway and worker before evaluating the new capabilities on a shared server. The APK does not update server authorization, WebSocket routing or work-context behavior. Use the existing release process, migrations, authentication and provider configuration described in [activation setup](six-feature-delivery.md#activation-checklist); the current recognition catalog is documented in [language setup](call-languages.md).

1. Install the HTTP Test APK and enter the API origin. Leave **Live gateway** empty when the gateway is served at the same origin; otherwise enter its reachable `/v1/companion` WebSocket URL. The normal secure app requires HTTPS/WSS. `127.0.0.1` means the phone/emulator itself unless an explicit local ADB reverse tunnel is configured. Use the intended server's current address, not an old screenshot or a cloud wake-service URL.
2. Sign in to the same account/profile as desktop for your own companion. For **Assist a call**, use the separate account that the host invited. Open **Call → Assist a call**, accept the invitation and join. The host controls transcript, guidance, private notes, voice and screen permissions. Voice requires an explicit microphone action.
3. Start screen sharing on the host's desktop. On the assistant, choose 720p, 1080p, 1440p or 4K detail and zoom as needed. The reported capture dimensions reflect the actual available source size. Select **Request control**; the desktop host must approve that named request. Decline, revoke, leave, timeout and ending the share remove control.
4. In **Work → Companies**, save a daily log, add a document and open contextual Ask. In **Work → Ask**, send a question, follow its pending state and open a returned source. **Work → Personal AI** manages private preparation requests, published briefs, exclusions and connected clients.
5. In **Interview prep → Applications**, add or update a role/resume, schedule an interview and prepare a call or practice with that context. Calendar connection remains optional and requires the provider registration described in [calendar setup](calendar-provider-setup.md). Available connections, sync and meeting context are managed inside preparation.

## Evidence boundaries

Automated client/backend checks, native compilation and [isolated Android UI/API acceptance](evidence/mobile-assistance-native-2026-11-23.json) are separate evidence. The native trials use fictional accounts and responses; they do not prove a live model or calendar-provider result. No app-scoped crash, ANR or unhandled exception was observed in the retained test buffers. No production service, installed user application or real call was changed.

The local media trial reached signaling, but neither native nor browser participants established a successful ICE media pair. The bounded ADB relay received native STUN requests without a LiveKit UDP response. Consequently, decoded shared-screen video, RTP voice and mobile-to-desktop control remain unverified in this environment. A routed staging media service and paired devices are required for that acceptance; a successful TLS connection alone is insufficient.

Physical microphone/Bluetooth/interruption behavior, actual macOS capture and permissions, real provider OAuth, and production signing/store distribution remain external release checks. In-person host private listening requires pausing phone microphone capture; remote text input currently supports US physical-key mapping. These limitations are not represented as successful device certification.
