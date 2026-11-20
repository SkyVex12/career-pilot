# Native background controls: integration and verification

Implementation: `apps/desktop/src-tauri/src/background.rs`.

The module adds optional close-to-tray behavior, the existing Ruby app icon in the Windows tray/macOS menu bar, Open Copilot, Pause call suggestions, and Exit completely. Preferences live in the app config directory in `background.json`, independently of existing settings. No credentials are stored there. Defaults preserve foreground startup and ordinary window-close behavior.

## Integration contract

- Register `mod background;` in the Tauri library.
- During setup call `background::setup(app.handle(), remote::end)?`.
- Before existing window event handling call `background::on_window_event(window, event)` and return when it consumes the close.
- The existing single-instance callback calls `background::second_instance(app, &args)`. A second `--background` launch never restores/focuses the workspace; a normal second launch does.
- Register `background_status`, `background_set_preferences`, and optional `background_native_activity` commands.
- Skip startup backend wake/auth fetching when `background::background_start_requested()` is true. The frontend must obtain `background_status` before its startup API/device discovery work and remain idle while `background_start` is true. Listen for `copilot://background`; Open Copilot clears the flag and emits this event. Recheck status after installing the event listener to avoid a lost startup event.
- Background preference changes emit the same event. Future detection adapters must honor `suggestions_paused` before presenting suggestions.
- The native module itself has no backend/API, audio capture, transcription, screen capture, or network loop. Opt-in detection uses a five-second bounded local metadata sample; no UI tree is inspected while idle without microphone activity or an already tracked call. The surrounding app must retain that invariant for hidden startup.

`background_set_preferences` takes `{ preferences: { launch_at_login, keep_running, suggestions_paused } }`. The returned status distinguishes configured preferences from whether the OS startup entry exists. OS policy or user startup controls may still prevent launch. Corrupt preference files are logged and startup uses opt-in defaults.

## Login startup

Windows uses the current user's `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `RubyCopilot`, with a quoted executable path and `--background`. No elevation or shell is needed; the registry command suppresses its console window. Disabling removes only this named value.

macOS writes `~/Library/LaunchAgents/com.copilot.desktop.login.plist`, with XML-escaped executable path and `--background` in a separate `ProgramArguments` element. This uses the long-established LaunchAgent mechanism, without post-Ventura APIs or architecture-specific binaries. There is no KeepAlive restart loop. Enabling/disabling takes effect at the next login; changes do not spawn the app immediately. OS login-item policy and installed app location still matter. macOS login behavior still requires an interactive machine trial. The native app and Apple bridges compiled and passed native tests on the GitHub macOS runner; this Windows host does not validate Mac login/logout behavior.

The login entry uses the executable's current location. Re-enable launch at login after moving the application or changing install location.

## Native activity inspection and limitations

`background_native_activity` is a user-requested Windows diagnostic, not a detection promise. It inspects WASAPI session-control metadata across active capture/render devices on a worker thread. It never creates `IAudioClient`, `IAudioCaptureClient`, or an audio stream. Results include active input/output flags by external process ID, optional foreground process ID, and device inspection warnings. Ruby's own process ID and system PID zero are excluded. Additional child/browser processes are not claimed to be fully attributed.

The diagnostic response itself still has `call_evidence: false`. Windows opt-in detection is now available separately in `call_detection.rs`: microphone-session identity plus app-specific accessible mute and leave/hang-up controls are both required for a new candidate. Two observations over at least five seconds debounce starts. A local call key prevents repeated suggestions across dismissal and short reconnects; absence for 120 seconds can offer an end review, never end a session. Process-only recognition cannot trigger a suggestion. Copilot input and unknown executables are excluded. Browser subprocess attribution uses matching browser executable identity, so exact service and caller/title are not claimed. Desktop/emulator microphone attribution is exact PID to avoid cross-instance activity attribution.

Preferences add `call_suggestions` (false by default) and `disabled_apps`. Windows shell notification state suppresses prompts when Windows reports notifications inappropriate; this is not certification of every Focus Assist/DND policy version. Pause, per-app disabling and existing sessions suppress new prompts. Accepting opens setup, not capture. A separate nonfocused window has Start Copilot/Not now; end offers invoke existing confirmation. Accepted actions are retained locally until the main frontend consumes them, including background cold startup.

[Microsoft UI Automation guidance](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-findall) informs worker-thread queries and top-level-only desktop enumeration. Traversal is bounded per known application; only button names are inspected transiently, never document text, title, URLs, screenshots, or accessibility actions. [Windows notification state](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shqueryusernotificationstate) gates suggestion display. Native event subscriptions could reduce fallback polling further; they are not implemented.

| Target | Implemented evidence adapter | Incoming | Actual active calls | Audio access |
| --- | --- | --- | --- | --- |
| Zoom, Teams, Slack, Discord, WhatsApp (Windows) | English accessible mute + app-specific leave/hang-up + same-PID microphone session | Unverified | Unverified | Unverified |
| Chrome/Edge/Firefox/Brave web calls | English leave/hang-up + microphone controls + browser microphone process | Unverified | Unverified; generic call only | Unverified |
| BlueStacks/LDPlayer, Phone Link | Accessible English call controls + exact-PID microphone activity | Unverified | Unverified; no Android app/caller inference | Phone-side audio not assumed |
| macOS 13+ / FaceTime / iPhone relay | NSWorkspace bundle identity + Accessibility call controls + CoreAudio default-input running state | Unverified | Source implemented; real calls unverified | Global device activity does not establish per-app audio routing |
| Samsung Flow, AirDroid, Smart Connect | Official-capability evaluation complete below; no validated adapter, disabled | Vendor notifications vary; Career Pilot unverified | Career Pilot unverified | Route-specific; see evaluation |

Localization, inaccessible/minimized controls, privileged application windows and process-separated native apps may prevent detection. No app is certified supported solely by these adapters. Real app calls, DND, device changes, audio routing, all macOS architectures and incoming-call behavior require device validation.

## Validation

Run `cargo test -p copilot-desktop --lib background:: --offline` for preference persistence/corruption handling, opt-in defaults, close/exit policy, background argument recognition, startup path quoting, plist escaping, and own-process exclusion.

Run the Windows-host smoke test explicitly with:

`cargo test -p copilot-desktop --lib background::native_tests::windows_audio_session_inspection_does_not_claim_calls --offline -- --ignored --nocapture`

That smoke test verifies actual read-only WASAPI enumeration on the available host. It does not verify any conferencing app, incoming call, notifications, permission prompts, login/logout behavior, tray interactions, or macOS. Those require real desktop/platform testing. Unit tests do not change login registration or start audio capture.

Latest Windows validation: native background suite 13 passed, two host diagnostics excluded by default. Explicit UI Automation/WASAPI diagnostic passed on this host (zero candidate calls, notifications allowed); this is not a live call trial. Desktop local suggestion tests: 2 passed, proving acceptance does not issue an API/start-session call and dismissal does not stop a call.

## macOS 13 source implementation

`call_detection_macos.rs` uses NSWorkspace running application identities, bounded public Accessibility button attributes and CoreAudio default-input device activity. It does not use the post-Ventura per-process audio APIs, screen recording, process injection, screenshots, or source text. Explicit Settings permission action opens Accessibility settings and requests notification authorization; no permission prompt occurs during hidden startup. AX IPC calls have a messaging timeout and traversal budget. Missing permission fails closed. Source adapters include Zoom, Teams, Slack, Discord, WhatsApp, FaceTime (including relay when visible controls/audio are available) and major browsers. Unknown apps remain excluded. macOS device activity is global, so actual per-app audio availability remains unverified.

Suggestions use the public UserNotifications framework rather than custom windows on macOS; Focus/DND and notification preferences remain controlled by the OS. Clicking a delivered notification opens the normal setup or end-review flow and never creates or stops a call. The bridge retains its weak notification delegate for the app lifetime and clears only its own delivered/pending notification identifier. It uses APIs available before macOS 13 and architecture-neutral public bindings.

Official references checked during implementation: [Apple Accessibility API](https://developer.apple.com/documentation/applicationservices/axuielement_h), [default input device metadata](https://developer.apple.com/documentation/coreaudio/kaudiohardwarepropertydefaultinputdevice), and [UserNotifications](https://developer.apple.com/documentation/usernotifications). Apple Silicon and Intel adapter source passed cross-target metadata/type checks against the real objc2 AppKit/UserNotifications signatures, using a tiny local harness that stubs only the Tauri app handoff. This does not link/package the full desktop app or validate operating-system behavior. The later full native Mac test run compiled and linked the app and Apple bridges on the actual macOS SDK. Universal packaging evidence is recorded in the delivery guide. Interactive signing/permission prompts, notification click behavior, Focus/DND, real app calls and device routing still require machine validation.

The small checked-in harness is `apps/desktop/native-checks/macos-call-adapter`. Reproduce with `cargo check --manifest-path apps/desktop/native-checks/macos-call-adapter/Cargo.toml --target aarch64-apple-darwin --offline` and the same command with `--target x86_64-apple-darwin` after installing the Rust standard-library targets. It imports the production modules directly; it does not claim full native app compilation.


## Additional phone-link evaluation (M01-019, 2026-11-19)

This completes the locally feasible provider-capability evaluation. It does not enable adapters based on process names or imply that third-party notifications are Career Pilot detection. No supported public call-state or call-audio integration API for these products was established by the reviewed documentation.

| Product | Vendor-documented behavior | Career Pilot integration decision |
| --- | --- | --- |
| Samsung Flow | Notification synchronization and Smart View phone-screen control are documented. Smart View lets users choose phone or PC speakers for phone sounds. The documentation does not establish cellular/VoIP microphone routing or a stable incoming/active-call event interface. | Keep automatic call detection disabled. Mirroring or media playback is insufficient evidence of an active call. A Galaxy phone/Windows PC trial must separately establish incoming controls, active-call controls and both audio directions. |
| AirDroid Personal | Mirrored notifications include phone-call notifications. Current call-management guidance places the headset on the phone; its PC-microphone support article explicitly says conversation audio still uses the phone. | Notification/call-control capability is not desktop capture support. Keep the adapter disabled. Do not suggest a desktop capture session based only on an AirDroid notification; use Career Pilot manual/companion paths with a separately verified audio source. |
| Lenovo/Motorola Smart Connect | Motorola/Lenovo app streaming can receive calls; output-device selection and Mobile Desktop call-volume/audio-device controls are documented. Some apps cannot stream protected audio. Features vary by hardware; Lenovo lists a restricted iPhone feature set. | Candidate for a future route-specific adapter, not universal support. Validate accessible call controls and actual WASAPI input/output on a paired supported Motorola/Lenovo device. Screen streaming alone cannot identify who is speaking or prove call-audio access. |

Sources inspected directly: [Samsung Flow overview](https://www.samsung.com/uk/support/mobile-devices/what-is-samsung-flow-and-how-do-i-use-it/) and [Smart View controls](https://www.samsung.com/us/support/answer/ANS10001406/); [AirDroid call notification setup](https://help.airdroid.com/hc/en-us/articles/360004159593-How-to-receive-phone-s-notifications-on-a-computer-with-AirDroid-Personal), [current call-management page](https://www.airdroid.com/personal/sms-notification-call-on-pc/) and [PC microphone limitation](https://help.airdroid.com/hc/en-us/articles/360007483153-Can-I-Make-a-Phone-Call-with-Computer-s-Mic-and-Speakers); [Motorola Android app streaming](https://en-gb.support.motorola.com/app/answers/detail/a_id/182078/p/30%2C), [Mobile Desktop controls](https://help.motorola.com/hc/apps/smartconnect/sc90/en-us/CG0707793606.html) and [Lenovo compatibility](https://support.lenovo.com/sg/en/solutions/ht517758).

The AirDroid microphone support article has an older publication date; its limitation is consistent with the current call-management page's phone-headset instructions. No broad claim is made about separate AirDroid Cast/Remote Support products. Samsung Flow is likewise distinct from Samsung Phone and Microsoft Phone Link.

A future physical trial must record product/OS/device versions, pairing method, incoming and active-control accessibility, mute/device/reconnect behavior, actual PC microphone and speaker routes, duplicate suppression and permission/DND behavior. Test notification mirroring and ordinary media as negative controls. Only enable the specific adapter/route proven by that trial. No accounts, downloads, paired devices, calls or permissions were changed for this evaluation.


### Post-merge notification follow-up (2026-11-19)

The user reported no prompt during a Google Meet call. Installed desktop 0.3.5 had call suggestions enabled, unpaused, and no applications excluded. A native probe on that host saw Browser microphone activity plus call controls and Windows allowed notifications. One probe took 20.96 seconds, consistent with the documented default UIA provider timeout. The installed application's exact failure path could not be recovered because the old implementation discarded inspection and presentation errors.

The follow-up batches/caches visible-button queries and bounds provider waits. Popup create/show failures now retry while the offer is pending; windows open on the main/primary monitor's work area without requesting focus. Native UI dispatch runs without holding detector locks, rechecks pending/pause/session state, and discards expired queued show operations. Transient notification suppression and pause defer unanswered prompts while preserving explicit dismissal.

Settings > Background > Check detection reports the last local sample: waiting for microphone, missing call controls, disabled application, notification policy, active Copilot session, pending suggestion, or native/presentation error. It does not scan twice, record audio, wake the backend, or start a session. Only known app categories, counts and booleans are cached; button text is transient.

After batching, a host probe took 546 ms and found no microphone/call (the call had ended by then); this is not a same-call latency benchmark or a verified popup. Original native 102 tests passed (3 diagnostics/capture tests excluded by default), strict Clippy and both Mac adapter metadata checks passed. The current integrated branch additionally passes the focused checks recorded above. Real-call popup acceptance/focus/mute/DND behavior still requires a trial with the patched executable.

References: [UIA caching](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-cachingforclients), [FindAllBuildCache](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-findallbuildcache), [provider transaction timeout](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomation2-put_transactiontimeout).
