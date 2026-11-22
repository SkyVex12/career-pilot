# Production UI polish

Follow-up on `codex/production-ui-polish`, preserving the six-feature implementation and the pending Hushed work. This is a UI and lifecycle improvement, not certification of external services or device integrations.

## Desktop

- Shared light/dark colors, clearer input boundaries, consistent spacing and controls, and responsive Settings sections. Interview practice remains in Interview prep; Ask remains in Work context. The four primary navigation tabs are unchanged.
- Default 920 × 720 Call setup fits on one page with optional sections closed. Smaller windows use one form scroll area with a fixed Start action; the shell no longer adds a second scrollbar. Historical work entries use the surrounding page scroll area. Other pages expose a keyboard-focusable page scroller.
- Server startup/failure and update notices appear in a dismissible bottom-right stack outside page layout. They never change page geometry. The stack clears the Call footer, scrolls independently in the minimum 400 × 240 window, and stays hidden in compact window modes. Progress ticks do not reopen dismissed notices; new phases and failures can. A late retry failure cannot replace a newer native server status.
- Calendar and language settings use visible disclosures. Calendar settings are also reachable from Settings → Account, with honest setup/reconnect states and recoverable loading/errors. Changing profiles clears the previous calendar view and prevents a delayed authorization from opening. Disconnect remains available when consent expires. Successful saves retain edited values if the subsequent refresh fails.
- Ask, practice feedback, work context, personal-AI setup, and call history use clearer content hierarchy and loading/error/empty states. Past-call search explicitly covers loaded calls. Companion-display status distinguishes an authorization from an active connection and ignores prior-call responses.
- Stealth toggles no longer mutate the hidden overlay's capture affinity/DWM surface. Every app-owned window-show path reconciles the latest setting before displaying the window. Both transparent main and overlay surfaces are repaired after affinity changes. This addresses the source path associated with the reported white rectangle; the exact user's screen has not been replayed in the running app.

## Mobile

The native app uses the same coral/neutral palette, a two-step connection/sign-in screen, optional advanced gateway settings, profile switching, accessible controls, keyboard-aware forms, clearer call discovery/connection states, and readable Work, practice, history and live guidance. Restoring a saved server connection can be canceled. Connection instructions distinguish the Android device's localhost from the computer. Changing workspace derives a new automatic live gateway; intentional custom gateways remain visible in expanded advanced settings. Production transport restrictions remain enforced.

## Validation

- Desktop: 564 interface tests across 55 files; strict TypeScript; 10 release-script checks.
- Windows native: 183 tests passed, 6 existing opt-in tests ignored. The 9 Stealth tests include actual HWND checks for hidden ON → OFF and OFF → ON, deferred affinity, and pre-show readback for main and overlay. Rust formatting and default/release-feature all-target Clippy with warnings denied passed.
- Browser UI: **232 screen/viewport/theme states**, **4 standalone overlay cases**, **50 notification lifecycle states** and **60 notification action reachability checks** passed. Light/dark, default and minimum native window sizes, keyboard/focus, page scrolling, and axe checks are covered. Overlay Copy controls remain fully opaque for readable text. Reproducible harness and curated evidence: [UI review](../design/screens/production-ui/README.md). The harness uses fictional data and IPC; it does not prove provider behavior or native zoom.
- Mobile: strict TypeScript and **77 deterministic tests** passed, including switching servers with automatic/custom gateways. The fictional native fixture's authenticated capabilities route has **11 passing tests**; Ruff checks/format pass. Native Android screens and preview packaging use the separate CareerPilotValidation emulator; the user's running instance is untouched.
- Actual Android fixture journeys covered sign-in, call discovery/attachment, a live answer, Return to latest, saved Ask conversations and citations, History, the 24-language selector, text practice and feedback. Portrait, compact landscape and tablet layouts were visually inspected. Changing a restored workspace kept the gateway automatic, then signed in and connected guidance using the changed address. [Mobile review and screenshots](../design/screens/production-ui/mobile/README.md) and [receipt](../design/screens/production-ui/mobile/receipt.json) record the final artifact and source hashes. These are fictional local API journeys, not real-provider or physical-device certification. LDPlayer did not display a standard soft keyboard, so physical-device keyboard geometry remains unverified.

## Delivery and remaining limits

New previews preserve earlier installers. Windows delivery is `.implementation/artifacts/windows/CareerPilot-production-ui-preview.exe`, **51,991,040 bytes**, SHA256 `24985e0e71a845b13e60904a4e71fa32d70e452de1647c7110a47a9644e8c713`. Built at `5767e6b` with desktop changes through `09aca9b`; PE x86_64, matching copy hash, unsigned status and unchanged source were checked. The intermediate `6509af8` preview is preserved separately. [Windows receipt](evidence/production-ui-windows-2026-11-22.json).

Android delivery is `.implementation/artifacts/android/CareerPilot-production-ui-preview.apk`, **56,087,530 bytes**, SHA256 `65067c17d821ff21e45abb4d46eebd8bdbc4690ca012f3ee6373c616ade1d776`. Source matches `5767e6b`; Android v2 development signature and arm64-v8a/x86_64 libraries were verified. Only the isolated validation emulator received this APK.

Windows preview is unsigned; Android preview is development-signed and permits HTTP for testing. Neither is a production store release. A new-process launch is needed to pick up the native window fix and clear an artifact already painted by an older running build. The new Windows executable was not installed or launched during validation.

Existing calendar OAuth registration, public TLS, mobile distribution credentials, iOS/macOS device acceptance and real Hushed incoming-call/audio acceptance retain their prior verification status in [the six-feature ledger](six-feature-progress.md). No production deployment, migration, account sign-in, real call, or store publication was performed for this UI milestone.
